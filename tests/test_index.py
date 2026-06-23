from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.index import index_db_path
from codex_batch_runner.queue import create_task, save_task


def run_cli(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, stdout.getvalue()


def write_config(tmp: str) -> Path:
    root = Path(tmp)
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "tasks"),
                "log_dir": str(root / "logs"),
                "event_dir": str(root / "events" / "private-event-stream"),
                "lock_file": str(root / "runner.lock"),
                "state_file": str(root / "state.json"),
            }
        ),
        encoding="utf-8",
    )
    return config_path


class IndexCommandTests(unittest.TestCase):
    def test_index_rebuild_dry_run_reports_plan_without_writing_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="task-a")

            code, output = run_cli(["--config", str(config_path), "index", "rebuild", "--dry-run", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("dry-run", report["mode"])
            self.assertFalse(report["wrote_db"])
            self.assertEqual(1, report["source_task_files"])
            self.assertEqual(1, report["indexed_tasks"])
            self.assertEqual(1, report["indexed_events"])
            self.assertFalse(index_db_path(config).exists())

    def test_index_rebuild_apply_writes_deterministic_projection_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            parent = create_task(config, "parent work", tmp, task_id="parent")
            child = create_task(config, "child work", tmp, task_id="child", depends_on=["parent"])
            child["status"] = "completed"
            child["review_status"] = "accepted"
            child["git_status"] = {"dirty": False, "ahead": 0, "has_unpushed": False}
            save_task(config, child)

            code, output = run_cli(["--config", str(config_path), "index", "rebuild", "--apply", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["wrote_db"])
            self.assertEqual(2, report["indexed_tasks"])
            self.assertEqual(1, report["indexed_dependencies"])
            self.assertTrue(index_db_path(config).exists())

            with sqlite3.connect(index_db_path(config)) as conn:
                statuses = dict(conn.execute("SELECT task_id, status FROM tasks").fetchall())
                deps = conn.execute("SELECT task_id, depends_on FROM task_dependencies").fetchall()
                review = conn.execute("SELECT review_status FROM task_review_state WHERE task_id = 'child'").fetchone()[0]
                git_dirty = conn.execute("SELECT git_dirty FROM task_git_metadata WHERE task_id = 'child'").fetchone()[0]

            self.assertEqual("runnable", statuses[parent["id"]])
            self.assertEqual("completed", statuses[child["id"]])
            self.assertEqual([("child", "parent")], deps)
            self.assertEqual("accepted", review)
            self.assertEqual(0, git_dirty)

            code, output = run_cli(["--config", str(config_path), "index", "status", "--json"])
            status = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(1, status["schema_version"])
            self.assertEqual(2, status["indexed_tasks"])
            self.assertEqual(2, status["source_task_files"])
            self.assertEqual([], status["warnings"])

    def test_index_status_reports_missing_schema_mismatch_and_corrupt_db_without_breaking_json_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="fallback-task")

            code, output = run_cli(["--config", str(config_path), "index", "status", "--json"])
            missing_status = json.loads(output)
            self.assertEqual(0, code)
            self.assertIn("missing", " ".join(missing_status["warnings"]))

            db_path = index_db_path(config)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute("INSERT INTO index_metadata (key, value) VALUES ('schema_version', '0')")
                conn.commit()

            code, output = run_cli(["--config", str(config_path), "index", "status", "--json"])
            mismatch_status = json.loads(output)
            self.assertEqual(0, code)
            self.assertIn("schema mismatch", " ".join(mismatch_status["warnings"]))

            db_path.write_bytes(b"not a sqlite database")
            code, output = run_cli(["--config", str(config_path), "index", "status", "--json"])
            corrupt_status = json.loads(output)
            self.assertEqual(0, code)
            self.assertIn("unreadable", " ".join(corrupt_status["warnings"]))

            code, output = run_cli(["--config", str(config_path), "list", "--json"])
            tasks = json.loads(output)
            self.assertEqual(0, code)
            self.assertEqual(["fallback-task"], [task["id"] for task in tasks])

            code, output = run_cli(["--config", str(config_path), "events", "--json"])
            events = json.loads(output)
            self.assertEqual(0, code)
            self.assertEqual(["task_created"], [event["event_type"] for event in events])

    def test_index_rebuild_excludes_local_runtime_paths_and_source_file_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "private-repo-root"
            repo_root.mkdir()
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", str(repo_root), task_id="path-sensitive", title="Public path test")
            task["project_root"] = str(repo_root)
            task["cwd"] = str(repo_root / "runtime-cwd")
            task["execution_worktree_path"] = str(repo_root / "private-worktree")
            save_task(config, task)
            event_file = config.event_dir / "private-runtime-events.jsonl"
            event_file.parent.mkdir(parents=True, exist_ok=True)
            event_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_type": "path_event_without_id",
                        "occurred_at": "2026-01-01T00:00:00+00:00",
                        "task_id": "path-sensitive",
                        "project_id": "public-project",
                        "project_root": str(repo_root),
                        "summary": "path event summary",
                        "payload": {"safe_count": 1},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            code, _ = run_cli(["--config", str(config_path), "index", "rebuild", "--apply"])

            self.assertEqual(0, code)
            dumped = dump_sqlite_text(index_db_path(config))
            forbidden = [
                tmp,
                str(repo_root),
                str(task["cwd"]),
                str(config.event_dir),
                str(event_file),
                event_file.name,
                "private-worktree",
                "path-sensitive.json",
            ]
            for value in forbidden:
                self.assertNotIn(value, dumped)
            self.assertIn("Public path test", dumped)
            self.assertIn("path_event_without_id", dumped)

    def test_index_rebuild_excludes_sensitive_raw_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "SECRET_PROMPT api_key=abc123", tmp, task_id="sensitive", title="Public title")
            task["next_prompt"] = "SECRET_NEXT_PROMPT"
            task["session_id"] = "SECRET_SESSION"
            task["thread_id"] = "SECRET_THREAD"
            task["stdout"] = "SECRET_STDOUT"
            task["stderr"] = "SECRET_STDERR"
            task["environment"] = {"TOKEN": "SECRET_ENV"}
            task["last_result"] = {"summary": "SECRET_RESULT"}
            save_task(config, task)
            raw_event = {
                "schema_version": 1,
                "event_id": "raw-sensitive-event",
                "event_type": "raw_event",
                "occurred_at": "2026-01-01T00:00:00+00:00",
                "task_id": "sensitive",
                "summary": "raw event",
                "payload": {
                    "prompt": "SECRET_EVENT_PROMPT",
                    "thread_id": "SECRET_EVENT_THREAD",
                    "message": "token=SECRET_EVENT_TOKEN",
                },
            }
            event_file = config.event_dir / "2026-01-01.jsonl"
            event_file.parent.mkdir(parents=True, exist_ok=True)
            event_file.write_text(json.dumps(raw_event) + "\n", encoding="utf-8")

            code, _ = run_cli(["--config", str(config_path), "index", "rebuild", "--apply"])

            self.assertEqual(0, code)
            dumped = dump_sqlite_text(index_db_path(config))
            forbidden = [
                "SECRET_PROMPT",
                "abc123",
                "SECRET_NEXT_PROMPT",
                "SECRET_SESSION",
                "SECRET_THREAD",
                "SECRET_STDOUT",
                "SECRET_STDERR",
                "SECRET_ENV",
                "SECRET_RESULT",
                "SECRET_EVENT_PROMPT",
                "SECRET_EVENT_THREAD",
                "SECRET_EVENT_TOKEN",
            ]
            for value in forbidden:
                self.assertNotIn(value, dumped)
            self.assertIn("Public title", dumped)
            self.assertIn("[REDACTED]", dumped)


def dump_sqlite_text(path: Path) -> str:
    values: list[str] = []
    with sqlite3.connect(path) as conn:
        table_names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in table_names:
            for row in conn.execute(f"SELECT * FROM {table}"):
                values.append(json.dumps(row, ensure_ascii=False, default=str))
    return "\n".join(values)


if __name__ == "__main__":
    unittest.main()

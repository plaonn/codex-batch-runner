from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.config import Config
from codex_batch_runner.dashboard_data import build_dashboard_overview
from codex_batch_runner.index import build_rebuild_report
from codex_batch_runner.queue import create_task, save_task


def write_config(tmp: str) -> Path:
    root = Path(tmp)
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "tasks"),
                "log_dir": str(root / "logs"),
                "event_dir": str(root / "events"),
                "lock_file": str(root / "runner.lock"),
                "state_file": str(root / "state.json"),
            }
        ),
        encoding="utf-8",
    )
    return config_path


class DashboardDataTests(unittest.TestCase):
    def test_dashboard_overview_prefers_fresh_sqlite_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(str(write_config(tmp)))
            create_task(config, "new work", tmp, task_id="ready", title="Ready task")
            review = create_task(config, "review work", tmp, task_id="review", title="Review task")
            review["status"] = "completed"
            review["review_status"] = None
            save_task(config, review)
            accepted = create_task(config, "accepted work", tmp, task_id="accepted", title="Accepted task")
            accepted["status"] = "completed"
            accepted["review_status"] = "accepted"
            accepted["execution_mode"] = "git_worktree"
            accepted["execution_apply_status"] = "pending"
            save_task(config, accepted)
            failed = create_task(config, "failed work", tmp, task_id="failed", title="Failed task")
            failed["status"] = "failed"
            save_task(config, failed)

            report = build_rebuild_report(config, apply=True)
            overview = build_dashboard_overview(config)

            self.assertTrue(report["wrote_db"])
            self.assertEqual("sqlite_index", overview["data_source"])
            self.assertFalse(overview["fallback_used"])
            self.assertEqual([], overview["warnings"])
            self.assertEqual(4, overview["tasks"]["total"])
            self.assertEqual(1, overview["tasks"]["by_status"]["runnable"])
            self.assertEqual(1, overview["review"]["backlog"]["total"])
            self.assertEqual(1, overview["review"]["accepted_unapplied"])
            self.assertEqual(1, overview["failures"]["failed_or_blocked"])
            self.assertGreaterEqual(overview["recent_events"]["by_type"]["task_created"], 4)

    def test_dashboard_overview_warns_and_falls_back_when_index_is_missing_or_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(str(write_config(tmp)))
            create_task(config, "first work", tmp, task_id="first", title="First task")

            missing = build_dashboard_overview(config)

            self.assertEqual("canonical_fallback", missing["data_source"])
            self.assertTrue(missing["fallback_used"])
            self.assertIn("missing", " ".join(missing["warnings"]))
            self.assertEqual(1, missing["tasks"]["total"])

            report = build_rebuild_report(config, apply=True)
            self.assertTrue(report["wrote_db"])
            create_task(config, "second work", tmp, task_id="second", title="Second task")

            stale = build_dashboard_overview(config)

            self.assertEqual("canonical_fallback", stale["data_source"])
            self.assertTrue(stale["fallback_used"])
            self.assertIn("stale", " ".join(stale["warnings"]))
            self.assertEqual(2, stale["tasks"]["total"])

    def test_dashboard_overview_omits_raw_private_fields_from_fallback_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(str(write_config(tmp)))
            task = create_task(
                config,
                "SECRET_PROMPT api_key=abc123",
                tmp,
                task_id="privacy",
                project_id="public-project",
                title="Public title",
            )
            task["next_prompt"] = "SECRET_NEXT_PROMPT"
            task["session_id"] = "SECRET_SESSION"
            task["thread_id"] = "SECRET_THREAD"
            task["stdout"] = "SECRET_STDOUT"
            task["stderr"] = "SECRET_STDERR"
            task["execution_worktree_path"] = str(Path(tmp) / "private-worktree")
            task["last_result"] = {"summary": "SECRET_RESULT"}
            save_task(config, task)
            event_file = config.event_dir / "2026-01-01.jsonl"
            event_file.parent.mkdir(parents=True, exist_ok=True)
            event_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_id": "private-event",
                        "event_type": "privacy_event",
                        "occurred_at": "2026-01-01T00:00:00+00:00",
                        "task_id": "privacy",
                        "project_id": "public-project",
                        "project_root": str(Path(tmp) / "private-repo"),
                        "summary": "SECRET_EVENT_SUMMARY",
                        "payload": {
                            "prompt": "SECRET_EVENT_PROMPT",
                            "thread_id": "SECRET_EVENT_THREAD",
                            "session_id": "SECRET_EVENT_SESSION",
                            "log": "SECRET_EVENT_LOG",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            overview = build_dashboard_overview(config)
            dumped = json.dumps(overview, ensure_ascii=False, sort_keys=True)

            self.assertEqual("canonical_fallback", overview["data_source"])
            forbidden = [
                tmp,
                "SECRET_PROMPT",
                "abc123",
                "SECRET_NEXT_PROMPT",
                "SECRET_SESSION",
                "SECRET_THREAD",
                "SECRET_STDOUT",
                "SECRET_STDERR",
                "SECRET_RESULT",
                "SECRET_EVENT_SUMMARY",
                "SECRET_EVENT_PROMPT",
                "SECRET_EVENT_THREAD",
                "SECRET_EVENT_SESSION",
                "SECRET_EVENT_LOG",
                "private-worktree",
                "private-repo",
            ]
            for value in forbidden:
                self.assertNotIn(value, dumped)
            self.assertNotIn("prompt", dumped)
            self.assertNotIn("payload", dumped)
            self.assertEqual(1, overview["tasks"]["total"])
            self.assertIn("privacy_event", overview["recent_events"]["by_type"])


if __name__ == "__main__":
    unittest.main()

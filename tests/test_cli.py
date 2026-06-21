from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.evidence import rate_limit_dir
from codex_batch_runner.fs import write_json_atomic
from codex_batch_runner.queue import create_task, load_task, save_task


def write_config(
    tmp: str,
    trigger_command: list[str] | None = None,
    dependency_requires_accepted_review: bool = False,
) -> Path:
    root = Path(tmp)
    data = {
        "queue_dir": str(root / "tasks"),
        "log_dir": str(root / "logs"),
        "event_dir": str(root / "events"),
        "lock_file": str(root / "runner.lock"),
        "state_file": str(root / "state.json"),
        "dependency_requires_accepted_review": dependency_requires_accepted_review,
    }
    if trigger_command is not None:
        data["post_mutation_trigger_command"] = trigger_command
    config_path = root / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return config_path


def run_cli(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, stdout.getvalue()


def run_cli_with_stderr(args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def set_status(config: Config, task_id: str, status: str, last_error: str | None = None) -> None:
    task = load_task(config, task_id)
    task["status"] = status
    task["last_error"] = last_error
    save_task(config, task)


def list_lines(output: str) -> list[str]:
    return output.strip().splitlines()


def fixed_table_rows(output: str) -> list[dict[str, str]]:
    lines = list_lines(output)
    if not lines:
        return []
    headers = lines[0].split()
    starts = [lines[0].index(header) for header in headers]
    rows = []
    for line in lines[1:]:
        row = {}
        for index, header in enumerate(headers):
            start = starts[index]
            end = starts[index + 1] if index + 1 < len(starts) else None
            row[header] = line[start:end].strip()
        rows.append(row)
    return rows


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, stdout=subprocess.PIPE, text=True)
    return result.stdout.strip()


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, stdout=subprocess.PIPE)
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test User")


def write_plan(tmp: str, data: dict) -> Path:
    path = Path(tmp) / "queue-plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class CliTests(unittest.TestCase):
    def test_post_mutation_trigger_runs_after_enqueue_and_review_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger)
            config = Config.load(str(config_path))

            self.assertEqual(
                (0, "task\n"),
                run_cli(["--config", str(config_path), "enqueue", "--cwd", tmp, "--id", "task", "--prompt", "work"]),
            )
            task = load_task(config, "task")
            task["status"] = "completed"
            save_task(config, task)

            for args in (
                ["accept", "task", "--reason", "verified"],
                ["reject", "task", "--reason", "needs work"],
                ["reject", "task", "--follow-up", "--reason", "needs more"],
            ):
                with self.subTest(args=args):
                    code, _ = run_cli(["--config", str(config_path), *args])
                    self.assertEqual(0, code)

            self.assertEqual(["x", "x", "x", "x"], marker.read_text(encoding="utf-8").splitlines())

    def test_post_mutation_trigger_runs_after_resolve_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="task")
            set_status(config, "task", "failed", "failed")

            self.assertEqual(
                0,
                run_cli(["--config", str(config_path), "resolve", "task", "--resolution", "manual"])[0],
            )
            self.assertEqual(0, run_cli(["--config", str(config_path), "archive", "task"])[0])

            self.assertEqual(["x", "x"], marker.read_text(encoding="utf-8").splitlines())

    def test_post_mutation_trigger_is_not_called_for_read_only_or_run_next_without_follow_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="task")
            set_status(config, "task", "completed")

            for args in (
                ["list"],
                ["show", "task"],
                ["summary", "task"],
                ["review-bundle", "task"],
                ["logs", "task"],
                ["transcript", "task"],
                ["follow", "task", "--poll-interval", "0", "--max-polls", "1"],
                ["doctor"],
                ["events"],
                ["rate-limits"],
                ["prune"],
                ["run-next"],
            ):
                with self.subTest(args=args):
                    code, _ = run_cli(["--config", str(config_path), *args])
                    self.assertIn(code, {0, 1})

            self.assertFalse(marker.exists())

    def test_post_mutation_trigger_failure_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, [sys.executable, "-c", "import sys; sys.exit(7)"])

            code, output, stderr = run_cli_with_stderr(
                ["--config", str(config_path), "enqueue", "--cwd", tmp, "--id", "task", "--prompt", "work"]
            )

            self.assertEqual(0, code)
            self.assertEqual("task\n", output)
            self.assertIn("warning: post-mutation trigger exited with status 7", stderr)
            self.assertEqual("runnable", load_task(Config.load(str(config_path)), "task")["status"])

    def test_enqueue_records_project_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--id",
                    "metadata",
                    "--project",
                    "project-a",
                    "--category",
                    "implementation",
                    "--label",
                    "queue",
                    "--label",
                    "review",
                    "--created-by",
                    "test",
                    "--prompt",
                    "work",
                ]
            )
            task = load_task(Config.load(str(config_path)), "metadata")

            self.assertEqual(0, code)
            self.assertEqual("metadata\n", output)
            self.assertEqual("project-a", task["project_id"])
            self.assertEqual("implementation", task["category"])
            self.assertEqual(["queue", "review"], task["labels"])
            self.assertEqual("test", task["created_by"])

    def test_list_filters_by_project_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(
                config,
                "work",
                tmp,
                task_id="match",
                project_id="project-a",
                category="implementation",
                labels=["queue", "review"],
            )
            other_dir = Path(tmp) / "other"
            other_dir.mkdir()
            create_task(
                config,
                "work",
                str(other_dir),
                task_id="other",
                project_id="project-b",
                category="docs",
                labels=["readme"],
            )

            filters = (
                ["--project", "project-a"],
                ["--project-root", tmp],
                ["--cwd", tmp],
                ["--category", "implementation"],
                ["--label", "queue"],
            )
            for filter_args in filters:
                with self.subTest(filter_args=filter_args):
                    code, output = run_cli(["--config", str(config_path), "list", *filter_args])

                    self.assertEqual(0, code)
                    self.assertEqual(["ID", "STATUS", "PROJECT", "ATTEMPTS", "DEPS", "FLAGS"], list_lines(output)[0].split())
                    rows = {row["ID"]: row for row in fixed_table_rows(output)}
                    self.assertEqual("runnable", rows["match"]["STATUS"])
                    self.assertNotIn("other", rows)

    def test_list_filters_legacy_task_by_cwd_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="legacy")
            for field in ("schema_version", "project_root", "project_id", "category", "labels", "created_by"):
                task.pop(field, None)
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--project", Path(tmp).name])

            self.assertEqual(0, code)
            self.assertEqual(["ID", "STATUS", "PROJECT", "ATTEMPTS", "DEPS", "FLAGS"], list_lines(output)[0].split())
            self.assertEqual("runnable", fixed_table_rows(output)[0]["STATUS"])

            code, output = run_cli(["--config", str(config_path), "list", "--project-root", tmp])

            self.assertEqual(0, code)
            self.assertEqual("runnable", fixed_table_rows(output)[0]["STATUS"])

    def test_list_default_shows_reviewable_completed_and_hides_accepted_and_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id, status in (
                ("runnable", "runnable"),
                ("resume", "needs_resume"),
                ("running", "running"),
                ("blocked", "blocked_user"),
                ("failed", "failed"),
                ("completed", "completed"),
                ("accepted", "completed"),
                ("rejected", "completed"),
                ("needs-followup", "completed"),
                ("archived", "archived"),
            ):
                create_task(config, task_id, tmp, task_id=task_id)
                set_status(config, task_id, status)
            accepted = load_task(config, "accepted")
            accepted["review_status"] = "accepted"
            save_task(config, accepted)
            rejected = load_task(config, "rejected")
            rejected["review_status"] = "rejected"
            save_task(config, rejected)
            needs_followup = load_task(config, "needs-followup")
            needs_followup["review_status"] = "needs_followup"
            save_task(config, needs_followup)

            code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in fixed_table_rows(output)}
            self.assertEqual("runnable", rows["runnable"]["STATUS"])
            self.assertEqual("needs_resume", rows["resume"]["STATUS"])
            self.assertEqual("running", rows["running"]["STATUS"])
            self.assertEqual("blocked_user", rows["blocked"]["STATUS"])
            self.assertEqual("failed", rows["failed"]["STATUS"])
            self.assertEqual("review=unreviewed", rows["completed"]["FLAGS"])
            self.assertEqual("review=rejected", rows["rejected"]["FLAGS"])
            self.assertEqual("review=needs_followup", rows["needs-followup"]["FLAGS"])
            self.assertNotIn("accepted", rows)
            self.assertNotIn("archived", rows)

    def test_list_review_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id, review in (
                ("unreviewed", None),
                ("accepted", "accepted"),
                ("rejected", "rejected"),
                ("followup", "needs_followup"),
            ):
                create_task(config, task_id, tmp, task_id=task_id)
                set_status(config, task_id, "completed")
                if review:
                    task = load_task(config, task_id)
                    task["review_status"] = review
                    save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--unreviewed"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in fixed_table_rows(output)}
            self.assertEqual("completed", rows["unreviewed"]["STATUS"])
            self.assertNotIn("accepted", rows)
            self.assertNotIn("rejected", rows)
            self.assertNotIn("followup", rows)

            code, output = run_cli(["--config", str(config_path), "list", "--needs-review"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in fixed_table_rows(output)}
            self.assertEqual("completed", rows["unreviewed"]["STATUS"])
            self.assertEqual("completed", rows["rejected"]["STATUS"])
            self.assertEqual("completed", rows["followup"]["STATUS"])
            self.assertNotIn("accepted", rows)

    def test_list_all_includes_completed_and_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "completed", tmp, task_id="completed")
            create_task(config, "archived", tmp, task_id="archived")
            set_status(config, "completed", "completed")
            set_status(config, "archived", "archived")

            code, output = run_cli(["--config", str(config_path), "list", "--all"])

            self.assertEqual(0, code)
            self.assertEqual(["ID", "STATUS", "PROJECT", "ATTEMPTS", "DEPS", "FLAGS"], list_lines(output)[0].split())
            rows = {row["ID"]: row for row in fixed_table_rows(output)}
            self.assertEqual("completed", rows["completed"]["STATUS"])
            self.assertEqual("archived", rows["archived"]["STATUS"])

    def test_status_filter_can_show_archived_without_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "archived", tmp, task_id="archived")
            create_task(config, "runnable", tmp, task_id="runnable")
            set_status(config, "archived", "archived")

            code, output = run_cli(["--config", str(config_path), "list", "--status", "archived"])

            self.assertEqual(0, code)
            self.assertEqual(["ID", "STATUS", "PROJECT", "ATTEMPTS", "DEPS", "FLAGS"], list_lines(output)[0].split())
            rows = {row["ID"]: row for row in fixed_table_rows(output)}
            self.assertEqual("archived", rows["archived"]["STATUS"])
            self.assertNotIn("runnable", rows)

    def test_list_failed_task_shows_one_line_last_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "failed", tmp, task_id="failed")
            set_status(config, "failed", "failed", "first line\nsecond\tline")

            code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            self.assertIn("last_error=first line second line", output)

    def test_archive_command_marks_task_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "task", tmp, task_id="task")

            code, output = run_cli(["--config", str(config_path), "archive", "task"])
            task = load_task(config, "task")

            self.assertEqual(0, code)
            self.assertEqual("task\tarchived\n", output)
            self.assertEqual("archived", task["status"])
            self.assertEqual("runnable", task["previous_status"])
            self.assertIsNotNone(task["archived_at"])

    def test_resolve_command_records_resolution_and_hides_from_default_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "task", tmp, task_id="failed")
            set_status(config, "failed", "failed", "not worth retrying")

            code, output = run_cli(
                ["--config", str(config_path), "resolve", "failed", "--resolution", "wont_fix", "--reason", "obsolete"]
            )
            task = load_task(config, "failed")

            self.assertEqual(0, code)
            self.assertEqual("failed\tresolved\twont_fix\n", output)
            self.assertEqual("failed", task["status"])
            self.assertEqual("wont_fix", task["resolution"])
            self.assertEqual("obsolete", task["resolution_reason"])
            self.assertIsNotNone(task["resolved_at"])

            code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            self.assertNotIn("failed", {row["ID"] for row in fixed_table_rows(output)})

            code, output = run_cli(["--config", str(config_path), "list", "--all"])
            rows = {row["ID"]: row for row in fixed_table_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("failed", rows["failed"]["STATUS"])
            self.assertEqual("-", rows["failed"]["DEPS"])
            self.assertEqual("last_error=not worth retrying resolution=wont_fix", rows["failed"]["FLAGS"])

            code, output = run_cli(["--config", str(config_path), "summary", "failed"])

            self.assertEqual(0, code)
            self.assertIn("resolution: wont_fix", output)
            self.assertIn("resolution_reason: obsolete", output)

    def test_rate_limits_lists_sanitized_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            write_json_atomic(
                rate_limit_dir(config) / "event.json",
                {
                    "task_id": "task-rate",
                    "detected_at": "2026-06-20T12:00:00+00:00",
                    "attempt": 3,
                    "matched_markers": ["usage limit"],
                    "cooldown_until": "2026-06-20T12:30:00+00:00",
                    "stderr_excerpt": "usage limit reached",
                    "error_excerpt": "try again later",
                    "original_log_path": str(Path(tmp) / "logs" / "task-rate" / "attempt-3.jsonl"),
                },
            )

            code, output = run_cli(["--config", str(config_path), "rate-limits"])

            self.assertEqual(0, code)
            self.assertIn("task-rate", output)
            self.assertIn("attempt=3", output)
            self.assertIn("markers=usage limit", output)

            code, output = run_cli(["--config", str(config_path), "rate-limits", "--json"])
            events = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("task-rate", events[0]["task_id"])
            self.assertEqual(["usage limit"], events[0]["matched_markers"])

    def test_prune_dry_run_reports_archived_and_accepted_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            archived = create_task(config, "old archived", tmp, task_id="old-archived")
            accepted = create_task(config, "old accepted", tmp, task_id="old-accepted")
            unreviewed = create_task(config, "old unreviewed", tmp, task_id="old-unreviewed")
            for task in (archived, accepted, unreviewed):
                task["status"] = "completed"
                task["completed_at"] = "2000-01-01T00:00:00+00:00"
            archived["status"] = "archived"
            archived["archived_at"] = "2000-01-02T00:00:00+00:00"
            accepted["review_status"] = "accepted"
            accepted["reviewed_at"] = "2000-01-03T00:00:00+00:00"
            unreviewed["review_status"] = "unreviewed"
            for task in (archived, accepted, unreviewed):
                save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "prune", "--older-than-days", "30"])

            self.assertEqual(0, code)
            self.assertIn("mode: dry-run", output)
            self.assertIn("old-archived\tarchived\t2000-01-02T00:00:00+00:00", output)
            self.assertIn("old-accepted\tcompleted_accepted\t2000-01-03T00:00:00+00:00", output)
            self.assertNotIn("old-unreviewed", output)
            self.assertTrue((config.queue_dir / "old-archived.json").exists())
            self.assertTrue((config.queue_dir / "old-accepted.json").exists())

    def test_prune_default_does_not_delete_task_or_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "done", tmp, task_id="done")
            log_path = config.log_dir / "done" / "attempt-1.jsonl"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("{}\n", encoding="utf-8")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["reviewed_at"] = "2000-01-01T00:00:00+00:00"
            task["log_paths"] = [str(log_path)]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "prune"])

            self.assertEqual(0, code)
            self.assertIn("would-delete", output)
            self.assertTrue((config.queue_dir / "done.json").exists())
            self.assertTrue(log_path.exists())

    def test_prune_json_output_is_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "done", tmp, task_id="json-task")
            task["status"] = "archived"
            task["archived_at"] = "2000-01-01T00:00:00+00:00"
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "prune", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["dry_run"])
            self.assertEqual("dry-run", report["mode"])
            self.assertEqual(1, report["candidate_count"])
            self.assertEqual("json-task", report["candidates"][0]["task_id"])
            self.assertEqual("task", report["candidates"][0]["files"][0]["kind"])

    def test_prune_apply_deletes_only_paths_inside_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "done", tmp, task_id="safe")
            safe_log = config.log_dir / "safe" / "attempt-1.jsonl"
            safe_log.parent.mkdir(parents=True)
            safe_log.write_text("{}\n", encoding="utf-8")
            outside_log = Path(tmp) / "outside.jsonl"
            outside_log.write_text("{}\n", encoding="utf-8")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["reviewed_at"] = "2000-01-01T00:00:00+00:00"
            task["log_paths"] = [str(safe_log), str(outside_log)]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json"])
            report = json.loads(output)
            files = report["candidates"][0]["files"]
            outside = [file for file in files if file["path"] == str(outside_log.resolve())][0]

            self.assertEqual(0, code)
            self.assertFalse((config.queue_dir / "safe.json").exists())
            self.assertFalse(safe_log.exists())
            self.assertTrue(outside_log.exists())
            self.assertFalse(outside["safe"])
            self.assertFalse(outside["deleted"])
            self.assertEqual("outside configured log_dir", outside["reason"])

    def test_prune_dry_run_reports_old_event_jsonl_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))

            code, output = run_cli(["--config", str(config_path), "prune", "--older-than-days", "30"])

            self.assertEqual(0, code)
            self.assertIn("mode: dry-run", output)
            self.assertIn("event candidates:", output)
            self.assertIn(f"event\twould-delete\t{old_event.resolve()}", output)
            self.assertTrue(old_event.exists())

    def test_prune_apply_deletes_safe_old_event_jsonl_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertFalse(old_event.exists())
            self.assertEqual(1, report["event_candidate_count"])
            self.assertTrue(report["event_candidates"][0]["deleted"])
            self.assertEqual("event", report["event_candidates"][0]["kind"])

    def test_prune_does_not_delete_event_jsonl_resolved_outside_event_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            outside_event = Path(tmp) / "outside-event.jsonl"
            outside_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(outside_event, (946684800, 946684800))
            event_link = config.event_dir / "linked.jsonl"
            event_link.symlink_to(outside_event)

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json"])
            report = json.loads(output)
            event = report["event_candidates"][0]

            self.assertEqual(0, code)
            self.assertTrue(outside_event.exists())
            self.assertTrue(event_link.exists())
            self.assertFalse(event["safe"])
            self.assertFalse(event["deleted"])
            self.assertEqual(str(outside_event.resolve()), event["path"])
            self.assertEqual("outside configured event_dir", event["reason"])

    def test_prune_skips_event_file_when_cursor_has_not_fully_processed_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n{"event_type":"task_started"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))
            cursor = Path(tmp) / "notify-state.json"
            cursor.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "current_event_file": str(old_event),
                        "current_byte_offset": 1,
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(
                ["--config", str(config_path), "prune", "--apply", "--json", "--notifier-cursor-state", str(cursor)]
            )
            report = json.loads(output)
            event = report["event_candidates"][0]

            self.assertEqual(0, code)
            self.assertTrue(old_event.exists())
            self.assertFalse(event["deleted"])
            self.assertTrue(event["skipped"])
            self.assertEqual("notifier cursor has not fully processed this event file", event["reason"])

    def test_prune_malformed_cursor_warns_and_skips_event_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))
            cursor = Path(tmp) / "notify-state.json"
            cursor.write_text("{not json\n", encoding="utf-8")

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json", "--notifier-cursor-state", str(cursor)])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(old_event.exists())
            self.assertTrue(report["warnings"])
            self.assertTrue(report["event_candidates"][0]["skipped"])
            self.assertEqual("notifier cursor safety warning", report["event_candidates"][0]["reason"])

    def test_prune_cursor_outside_event_dir_warns_and_skips_event_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))
            outside_event = Path(tmp) / "outside.jsonl"
            cursor = Path(tmp) / "notify-state.json"
            cursor.write_text(json.dumps({"schema_version": 1, "current_event_file": str(outside_event)}), encoding="utf-8")

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json", "--notifier-cursor-state", str(cursor)])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(old_event.exists())
            self.assertIn("outside event_dir", report["warnings"][0])
            self.assertTrue(report["event_candidates"][0]["skipped"])

    def test_prune_deletes_old_event_when_cursor_is_beyond_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            newer_event = config.event_dir / "2000-01-02.jsonl"
            newer_event.write_text('{"event_type":"task_started"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))
            os.utime(newer_event, (946684800, 946684800))
            cursor = Path(tmp) / "notify-state.json"
            cursor.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "current_event_file": str(newer_event),
                        "current_byte_offset": 0,
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json", "--notifier-cursor-state", str(cursor)])
            report = json.loads(output)
            by_path = {event["path"]: event for event in report["event_candidates"]}

            self.assertEqual(0, code)
            self.assertFalse(old_event.exists())
            self.assertTrue(newer_event.exists())
            self.assertTrue(by_path[str(old_event.resolve())]["deleted"])
            self.assertTrue(by_path[str(newer_event.resolve())]["skipped"])

    def test_apply_plan_dry_run_accepts_valid_dependency_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a")
            create_task(config, "synthetic work", tmp, task_id="task-b")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": {"type": "operator", "id": "test"},
                    "reason": "implementation order changed",
                    "operations": [
                        {
                            "op": "dependency_changes",
                            "task_id": "task-b",
                            "fields": {"add": ["task-a"]},
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run"])

            self.assertEqual(0, code)
            self.assertIn("mode: dry-run", output)
            self.assertIn("valid: true", output)
            self.assertIn("op[0]\tdependency_changes\ttasks=task-b\twould_change=yes", output)
            self.assertEqual("runnable", load_task(config, "task-b")["status"])

    def test_apply_plan_dry_run_rejects_missing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "pause stale task",
                    "operations": [{"op": "pause", "task_id": "missing"}],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run"])

            self.assertEqual(1, code)
            self.assertIn("valid: false", output)
            self.assertIn("task not found: missing", output)

    def test_apply_plan_dry_run_rejects_running_task_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="running-task")
            set_status(config, "running-task", "running")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "replan current work",
                    "operations": [{"op": "replan", "task_id": "running-task"}],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run"])

            self.assertEqual(1, code)
            self.assertIn("operation targets running task: running-task", output)

    def test_apply_plan_dry_run_rejects_dependency_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a", depends_on=["task-b"])
            create_task(config, "synthetic work", tmp, task_id="task-b")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "bad dependency rewrite",
                    "operations": [
                        {
                            "op": "dependency_changes",
                            "task_id": "task-b",
                            "fields": {"add": ["task-a"]},
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run"])

            self.assertEqual(1, code)
            self.assertIn("dependency graph would contain a cycle", output)

    def test_apply_plan_requires_dry_run_until_apply_is_implemented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "validate only",
                    "operations": [],
                },
            )

            code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "apply-plan", str(plan_path)])

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("apply mode is not implemented yet", stderr)

    def test_apply_plan_json_output_is_machine_readable_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": {"type": "operator", "id": "test"},
                    "reason": "record safe note",
                    "operations": [
                        {
                            "op": "append_note",
                            "task_id": "task-a",
                            "fields": {
                                "note": "public-safe summary",
                                "next_prompt": "raw prompt must not appear",
                                "session_id": "session-must-not-appear",
                            },
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["ok"])
            self.assertEqual("dry-run", report["mode"])
            self.assertEqual(["task-a"], report["operations"][0]["task_ids"])
            self.assertEqual("[redacted]", report["operations"][0]["sanitized"]["fields"]["next_prompt"])
            self.assertEqual("[redacted]", report["operations"][0]["sanitized"]["fields"]["session_id"])
            self.assertNotIn("raw prompt must not appear", output)

    def test_prune_skips_non_jsonl_event_dir_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            cursor = config.event_dir / "notify-state.json"
            cursor.write_text('{"offset":0}\n', encoding="utf-8")
            text_log = config.event_dir / "2000-01-01.log"
            text_log.write_text("not jsonl\n", encoding="utf-8")
            for path in (cursor, text_log):
                os.utime(path, (946684800, 946684800))

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(0, report["event_candidate_count"])
            self.assertTrue(cursor.exists())
            self.assertTrue(text_log.exists())

    def test_accept_and_reject_update_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "done", tmp, task_id="done")
            create_task(config, "follow", tmp, task_id="follow")
            set_status(config, "done", "completed")
            set_status(config, "follow", "completed")

            code, output = run_cli(["--config", str(config_path), "accept", "done", "--reason", "verified"])
            accepted = load_task(config, "done")

            self.assertEqual(0, code)
            self.assertEqual("done\taccepted\n", output)
            self.assertEqual("accepted", accepted["review_status"])
            self.assertEqual("verified", accepted["review_reason"])

            code, output = run_cli(
                ["--config", str(config_path), "reject", "follow", "--follow-up", "--reason", "needs tests"]
            )
            rejected = load_task(config, "follow")

            self.assertEqual(0, code)
            self.assertEqual("follow\tneeds_followup\n", output)
            self.assertEqual("needs_followup", rejected["review_status"])
            self.assertEqual("needs tests", rejected["review_reason"])

    def test_accept_rejects_non_completed_task_without_mutating_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="running")
            set_status(config, "running", "running")

            code, output, stderr = run_cli_with_stderr(
                ["--config", str(config_path), "accept", "running", "--reason", "too early"]
            )
            task = load_task(config, "running")

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("requires completed task status", stderr)
            self.assertIsNone(task["review_status"])

    def test_reject_remains_available_for_non_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="running")
            set_status(config, "running", "running")

            code, output = run_cli(["--config", str(config_path), "reject", "running", "--reason", "bad state"])
            task = load_task(config, "running")

            self.assertEqual(0, code)
            self.assertEqual("running\trejected\n", output)
            self.assertEqual("rejected", task["review_status"])

    def test_list_all_shows_completed_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "done", tmp, task_id="done")
            set_status(config, "done", "completed")

            code, output = run_cli(["--config", str(config_path), "list", "--all"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in fixed_table_rows(output)}
            self.assertEqual("completed", rows["done"]["STATUS"])
            self.assertEqual("review=unreviewed", rows["done"]["FLAGS"])

    def test_list_table_output_includes_header_project_deps_and_empty_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="plain", project_id="project-a")
            create_task(config, "work", tmp, task_id="parent", project_id="project-a")
            create_task(config, "work", tmp, task_id="child", depends_on=["parent"], project_id="project-a")
            parent = load_task(config, "parent")
            parent["status"] = "completed"
            parent["review_status"] = "accepted"
            save_task(config, parent)

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a"])
            lines = list_lines(output)
            rows = {row["ID"]: row for row in fixed_table_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual(["ID", "STATUS", "PROJECT", "ATTEMPTS", "DEPS", "FLAGS"], lines[0].split())
            self.assertNotIn("\t", output)
            self.assertEqual(
                {"STATUS": "runnable", "PROJECT": "project-a", "ATTEMPTS": "0", "DEPS": "-", "FLAGS": "-"},
                {key: rows["plain"][key] for key in ("STATUS", "PROJECT", "ATTEMPTS", "DEPS", "FLAGS")},
            )
            self.assertEqual("parent", rows["child"]["DEPS"])
            self.assertEqual("-", rows["child"]["FLAGS"])

    def test_list_table_aligns_columns_and_sorts_by_created_at_then_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id, created_at, project_id in (
                ("later", "2026-06-20T12:00:00+09:00", "project-a"),
                ("same-b", "2026-06-19T12:00:00+09:00", "project-b"),
                ("same-a", "2026-06-19T12:00:00+09:00", "project-c"),
            ):
                task = create_task(config, "work", tmp, task_id=task_id, project_id=project_id)
                task["created_at"] = created_at
                save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--all"])
            lines = list_lines(output)
            rows = fixed_table_rows(output)
            json_code, json_output = run_cli(["--config", str(config_path), "list", "--all", "--json"])

            self.assertEqual(0, code)
            self.assertEqual(0, json_code)
            self.assertNotIn("\t", output)
            self.assertEqual(["same-a", "same-b", "later"], [row["ID"] for row in rows])
            self.assertEqual(["same-a", "same-b", "later"], [task["id"] for task in json.loads(json_output)])
            status_start = lines[0].index("STATUS")
            project_start = lines[0].index("PROJECT")
            for line in lines[1:]:
                self.assertEqual(status_start, line.index("runnable"))
                self.assertGreaterEqual(line.index("project-"), project_start)

    def test_list_summary_and_review_next_report_unaccepted_dependency_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, dependency_requires_accepted_review=True)
            config = Config.load(str(config_path))
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "unreviewed"
            save_task(config, dep)
            child = create_task(config, "child", tmp, task_id="child", depends_on=["dep"], project_id="project-a")
            child["status"] = "completed"
            child["review_status"] = "unreviewed"
            child["last_result"] = {"status": "completed", "changed_files": [], "verification": ["unit"]}
            save_task(config, child)

            list_code, list_output = run_cli(["--config", str(config_path), "list", "--all"])
            summary_code, summary_output = run_cli(["--config", str(config_path), "summary", "child"])
            review_code, review_output = run_cli(
                ["--config", str(config_path), "review-next", "--dry-run", "--project", "project-a"]
            )

            self.assertEqual(0, list_code)
            self.assertIn("blocked_by=dep:not_accepted", list_output)
            self.assertEqual(0, summary_code)
            self.assertIn("dependency_blockers:\n- dep: not_accepted", summary_output)
            self.assertEqual(0, review_code)
            self.assertIn(
                "dependencies: ready=false requires_accepted_review=true blocked_by=dep:not_accepted",
                review_output,
            )

    def test_list_table_project_column_uses_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="legacy")
            task.pop("project_id", None)
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list"])
            rows = {row["ID"]: row for row in fixed_table_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual(Path(tmp).name, rows["legacy"]["PROJECT"])
            self.assertEqual("-", rows["legacy"]["DEPS"])
            self.assertEqual("-", rows["legacy"]["FLAGS"])

    def test_list_verbose_adds_compact_summary_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="verbose", project_id="project-a")
            task["status"] = "failed"
            task["last_result"] = {
                "status": "failed",
                "summary": "first line\nsecond\tline",
            }
            task["last_run"] = {
                "command_kind": "exec",
                "returncode": 1,
                "duration_seconds": 2.5,
                "log_path": str(Path(tmp) / "logs" / "verbose" / "attempt-1.jsonl"),
            }
            task["last_error"] = "error line one\nline two"
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--verbose"])
            lines = list_lines(output)
            rows = {row["ID"]: row for row in fixed_table_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual(
                ["ID", "STATUS", "PROJECT", "ATTEMPTS", "DEPS", "FLAGS", "LAST_RESULT", "LAST_RUN", "LAST_ERROR"],
                lines[0].split(),
            )
            self.assertEqual("failed", rows["verbose"]["STATUS"])
            self.assertEqual("last_error=error line one line two", rows["verbose"]["FLAGS"])
            self.assertEqual("status=failed summary=first line second line", rows["verbose"]["LAST_RESULT"])
            self.assertEqual("command=exec returncode=1 duration=2.5s", rows["verbose"]["LAST_RUN"])
            self.assertEqual("error line one line two", rows["verbose"]["LAST_ERROR"])

    def test_list_verbose_includes_result_push_and_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="push-meta", project_id="project-a")
            task["status"] = "completed"
            task["last_result"] = {
                "status": "completed",
                "summary": "done",
                "commits": ["abc1234 change"],
                "push_status": {"ahead": 1, "behind": 0},
            }
            task["git_status"] = {
                "branch": "main",
                "comparison_ref": "origin/main",
                "ahead": 1,
                "behind": 0,
                "dirty": False,
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--verbose", "--all"])
            rows = {row["ID"]: row for row in fixed_table_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("completed", rows["push-meta"]["STATUS"])
            self.assertEqual("review=unreviewed", rows["push-meta"]["FLAGS"])
            self.assertEqual(
                "status=completed summary=done commits=1 push_status=ahead=1 behind=0 "
                "git=branch=main compare=origin/main ahead=1 behind=0 dirty=false",
                rows["push-meta"]["LAST_RESULT"],
            )
            self.assertEqual("-", rows["push-meta"]["LAST_RUN"])
            self.assertEqual("-", rows["push-meta"]["LAST_ERROR"])

    def test_list_verbose_uses_dash_for_missing_summary_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="plain", project_id="project-a")

            code, output = run_cli(["--config", str(config_path), "list", "--verbose"])
            rows = {row["ID"]: row for row in fixed_table_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("-", rows["plain"]["DEPS"])
            self.assertEqual("-", rows["plain"]["FLAGS"])
            self.assertEqual("-", rows["plain"]["LAST_RESULT"])
            self.assertEqual("-", rows["plain"]["LAST_RUN"])
            self.assertEqual("-", rows["plain"]["LAST_ERROR"])

    def test_list_verbose_does_not_change_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="json-task", project_id="project-a")
            task["depends_on"] = []
            task["last_result"] = {"status": "completed", "summary": "done"}
            task["last_run"] = {"command_kind": "exec", "returncode": 0, "duration_seconds": 1.25}
            save_task(config, task)

            plain_code, plain_output = run_cli(["--config", str(config_path), "list", "--json"])
            verbose_code, verbose_output = run_cli(["--config", str(config_path), "list", "--json", "--verbose"])

            self.assertEqual(0, plain_code)
            self.assertEqual(0, verbose_code)
            self.assertEqual(json.loads(plain_output), json.loads(verbose_output))
            self.assertEqual([], json.loads(plain_output)[0]["depends_on"])

    def test_transcript_prints_sanitized_readable_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt", tmp, task_id="task-transcript")
            log_path = Path(tmp) / "logs" / "task-transcript" / "attempt-1.jsonl"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}}),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "agent_message", "message": "token=private-value done"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            task["log_paths"] = [str(log_path)]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "transcript", "task-transcript"])

            self.assertEqual(0, code)
            self.assertIn("## attempt 1: attempt-1.jsonl", output)
            self.assertIn("### user", output)
            self.assertIn("hello", output)
            self.assertIn("token [REDACTED]", output)
            self.assertNotIn("private-value", output)

    def test_follow_prints_compact_sanitized_attempt_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt with token=private-value", tmp, task_id="task-follow")
            task["status"] = "completed"
            log_path = config.log_dir / "task-follow" / "attempt-1.jsonl"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "raw prompt"}}),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "agent_message", "message": "working token=private-value"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "function_call",
                                "name": "exec_command",
                                "arguments": '{"cmd": "python3 -m unittest", "cwd": "/Users/alice/repo"}',
                            }
                        ),
                        json.dumps({"type": "function_call_output", "output": '{"exit_code": 0, "output": "ok"}'}),
                        json.dumps({"type": "error", "message": "usage limit reached; try again later"}),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "response": {
                                    "task_id": "task-follow",
                                    "status": "completed",
                                    "summary": "done with secret=private-value",
                                    "next_prompt": "private continuation",
                                    "changed_files": ["README.md"],
                                    "verification": ["unit tests"],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            task["log_paths"] = [str(log_path)]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "follow", "task-follow", "--lines", "20"])

            self.assertEqual(0, code)
            self.assertIn("==> attempt-1.jsonl <==", output)
            self.assertIn("assistant: working token [REDACTED]", output)
            self.assertIn("command start: exec_command", output)
            self.assertIn("/Users/[USER]/repo", output)
            self.assertIn("command finish: exit=0", output)
            self.assertIn("rate-limit: markers=", output)
            self.assertIn('final: {"changed_files": ["README.md"]', output)
            self.assertIn('"next_prompt": "[REDACTED]"', output)
            self.assertNotIn("raw prompt", output)
            self.assertNotIn("private-value", output)
            self.assertNotIn("private continuation", output)

    def test_follow_tails_initial_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt", tmp, task_id="tail")
            task["status"] = "completed"
            log_path = config.log_dir / "tail" / "attempt-1.jsonl"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                "\n".join(
                    json.dumps({"type": "agent_message", "message": f"line {index}"}) for index in range(4)
                )
                + "\n",
                encoding="utf-8",
            )
            task["log_paths"] = [str(log_path)]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "follow", "tail", "--lines", "2"])

            self.assertEqual(0, code)
            self.assertNotIn("line 0", output)
            self.assertNotIn("line 1", output)
            self.assertIn("line 2", output)
            self.assertIn("line 3", output)

    def test_follow_waits_for_running_task_log_to_appear_and_exits_when_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt", tmp, task_id="running-follow")
            task["status"] = "running"
            save_task(config, task)
            log_path = config.log_dir / "running-follow" / "attempt-1.jsonl"

            def finish_task() -> None:
                time.sleep(0.02)
                log_path.parent.mkdir(parents=True)
                log_path.write_text(
                    json.dumps({"type": "agent_message", "message": "appeared"}) + "\n",
                    encoding="utf-8",
                )
                loaded = load_task(config, "running-follow")
                loaded["status"] = "completed"
                loaded["log_paths"] = [str(log_path)]
                save_task(config, loaded)

            worker = threading.Thread(target=finish_task)
            worker.start()
            try:
                code, output = run_cli(
                    ["--config", str(config_path), "follow", "running-follow", "--poll-interval", "0.005"]
                )
            finally:
                worker.join(timeout=1)

            self.assertEqual(0, code)
            self.assertIn("==> attempt-1.jsonl <==", output)
            self.assertIn("assistant: appeared", output)

    def test_summary_prints_compact_review_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "prompt",
                tmp,
                task_id="task-summary",
                project_id="project-a",
                category="implementation",
                labels=["queue"],
                created_by="test",
            )
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "task-summary",
                "status": "completed",
                "summary": "Implemented token=private-value handling.",
                "next_prompt": "",
                "commits": ["abc1234 redact private handling"],
                "push_status": {"ahead": 1, "behind": 0},
                "changed_files": ["src/example.py"],
                "verification": ["python3 -m unittest"],
            }
            task["git_status"] = {
                "branch": "main",
                "comparison_ref": "origin/main",
                "ahead": 1,
                "behind": 0,
                "has_unpushed": True,
                "dirty": False,
                "unpushed_commits": ["abc1234 redact private handling"],
            }
            task["log_paths"] = [str(Path(tmp) / "logs" / "attempt-1.jsonl")]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "summary", "task-summary"])

            self.assertEqual(0, code)
            self.assertIn("# task task-summary", output)
            self.assertIn("status: completed", output)
            self.assertIn("review_status: unreviewed", output)
            self.assertIn("project_id: project-a", output)
            self.assertIn("category: implementation", output)
            self.assertIn("labels: queue", output)
            self.assertIn("summary:", output)
            self.assertIn("Implemented token [REDACTED] handling.", output)
            self.assertIn("commits:", output)
            self.assertIn("- abc1234 redact private handling", output)
            self.assertIn("push_status:", output)
            self.assertIn("ahead: 1", output)
            self.assertIn("## git_status", output)
            self.assertIn("has_unpushed: True", output)
            self.assertIn("- src/example.py", output)
            self.assertIn("- python3 -m unittest", output)
            self.assertIn("## logs", output)
            self.assertNotIn("private-value", output)

    def test_summary_shows_dependency_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "dependency", tmp, task_id="dep")
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            code, output = run_cli(["--config", str(config_path), "summary", "child"])

            self.assertEqual(0, code)
            self.assertIn("dependencies: dep", output)
            self.assertIn("dependencies_ready: false", output)
            self.assertIn("blocked_by: dep", output)

    def test_review_bundle_prints_human_report_with_working_tree_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            (repo / "file.txt").write_text("base\nchange\n", encoding="utf-8")
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            dep = create_task(config, "dependency", str(repo), task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "accepted"
            save_task(config, dep)
            task = create_task(config, "Implement token=private-value handling.", str(repo), task_id="bundle", depends_on=["dep"])
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "bundle",
                "status": "completed",
                "summary": "Changed token=private-value handling.",
                "next_prompt": "",
                "changed_files": ["file.txt"],
                "verification": ["python3 -m unittest"],
            }
            task["last_run"] = {"command_kind": "exec", "returncode": 0, "duration_seconds": 1.2}
            task["log_paths"] = ["/Users/example/.codex-batch-runner/logs/attempt.jsonl"]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-bundle", "bundle"])

            self.assertEqual(0, code)
            self.assertIn("# review bundle bundle", output)
            self.assertIn("## prompt_excerpt", output)
            self.assertIn("token [REDACTED]", output)
            self.assertIn('"kind": "working_tree"', output)
            self.assertIn("+change", output)
            self.assertIn("python3 -m unittest", output)
            self.assertIn("transcript_contents_included: False", output)
            self.assertNotIn("private-value", output)
            self.assertNotIn("/Users/example", output)

    def test_review_bundle_json_output_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="bundle-json", project_id="project-a")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "bundle-json",
                "status": "completed",
                "summary": "done",
                "changed_files": ["README.md"],
                "verification": ["unit tests"],
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-bundle", "bundle-json", "--json"])
            bundle = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("bundle-json", bundle["task"]["id"])
            self.assertEqual("completed", bundle["status"])
            self.assertEqual(["unit tests"], bundle["verification"])
            self.assertFalse(bundle["transcript_contents_included"])
            self.assertIn("git_repository", bundle)
            self.assertIn("safety_policy", bundle)

    def test_review_bundle_reports_missing_git_fallback_without_guessing_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="no-git")
            task["status"] = "completed"
            task["last_result"] = {
                "task_id": "no-git",
                "status": "completed",
                "summary": "done",
                "commits": ["local change without hash"],
                "changed_files": [],
                "verification": [],
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-bundle", "no-git", "--json"])
            bundle = json.loads(output)

            self.assertEqual(0, code)
            self.assertFalse(bundle["git_repository"]["available"])
            self.assertEqual("unavailable", bundle["commit_information"]["status"])
            self.assertIn("git repository unavailable", bundle["git_diff"]["warnings"])

    def test_review_bundle_sanitizes_obvious_secret_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "Use api_key=abc123 and bearer secret-token in /Users/alice/repo.", tmp, task_id="sanitize")
            task["status"] = "failed"
            task["last_error"] = "password=hunter2 in /Users/alice/repo"
            task["last_result"] = {
                "task_id": "sanitize",
                "status": "failed",
                "summary": "secret=private-value",
                "changed_files": [],
                "verification": ["TOKEN=private-value command"],
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-bundle", "sanitize", "--json"])

            self.assertEqual(0, code)
            self.assertNotIn("abc123", output)
            self.assertNotIn("hunter2", output)
            self.assertNotIn("private-value", output)
            self.assertNotIn("/Users/alice", output)
            self.assertIn("[REDACTED]", output)

    def test_review_next_dry_run_selects_oldest_review_needed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id, completed_at in (("newer", "2026-01-02T00:00:00+00:00"), ("older", "2026-01-01T00:00:00+00:00")):
                task = create_task(config, "work", tmp, task_id=task_id)
                task["status"] = "completed"
                task["review_status"] = "unreviewed"
                task["completed_at"] = completed_at
                task["last_result"] = {
                    "task_id": task_id,
                    "status": "completed",
                    "summary": f"{task_id} done",
                    "changed_files": ["README.md"],
                    "verification": ["unit tests"],
                }
                task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
                save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run"])

            self.assertEqual(0, code)
            self.assertIn("selected: true", output)
            self.assertIn("task_id: older", output)
            self.assertIn("dry_run: no task state changed", output)

    def test_review_next_dry_run_noops_when_no_review_needed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="accepted")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run"])

            self.assertEqual(0, code)
            self.assertIn("selected: false", output)
            self.assertIn("no completed task needs review", output)

    def test_review_next_dry_run_filters_by_project_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            other_root = Path(tmp) / "other"
            other_root.mkdir()
            for task_id, cwd, project_id, category, labels in (
                ("match", tmp, "project-a", "implementation", ["queue"]),
                ("other", str(other_root), "project-b", "docs", ["readme"]),
            ):
                task = create_task(config, "work", cwd, task_id=task_id, project_id=project_id, category=category, labels=labels)
                task["status"] = "completed"
                task["review_status"] = "unreviewed"
                task["last_result"] = {
                    "task_id": task_id,
                    "status": "completed",
                    "summary": "done",
                    "changed_files": [],
                    "verification": ["unit tests"],
                }
                task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
                save_task(config, task)

            filters = (
                ["--project", "project-a"],
                ["--project-root", tmp],
                ["--category", "implementation"],
                ["--label", "queue"],
            )
            for filter_args in filters:
                with self.subTest(filter_args=filter_args):
                    code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run", *filter_args])

                    self.assertEqual(0, code)
                    self.assertIn("task_id: match", output)
                    self.assertNotIn("task_id: other", output)

    def test_review_next_json_output_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="json-review", project_id="project-a")
            task["status"] = "completed"
            task["review_status"] = "needs_followup"
            task["last_result"] = {
                "task_id": "json-review",
                "status": "completed",
                "summary": "done",
                "changed_files": ["README.md"],
                "verification": ["unit tests"],
            }
            task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["selected"])
            self.assertEqual("json-review", report["task_id"])
            self.assertEqual("needs_followup", report["review_status"])
            self.assertFalse(report["mutated"])
            self.assertIn("gates", report)
            self.assertIn("bundle", report)
            self.assertEqual("json-review", report["bundle"]["task"]["id"])

    def test_review_next_dry_run_does_not_mutate_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="readonly")
            task["status"] = "completed"
            task["review_status"] = "rejected"
            task["last_result"] = {
                "task_id": "readonly",
                "status": "completed",
                "summary": "done",
                "changed_files": [],
                "verification": ["unit tests"],
            }
            task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
            save_task(config, task)
            before = load_task(config, "readonly")

            code, _ = run_cli(["--config", str(config_path), "review-next", "--dry-run"])
            after = load_task(config, "readonly")

            self.assertEqual(0, code)
            self.assertEqual(before, after)

    def test_review_next_requires_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "review-next"])

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("auto-apply is not implemented yet", stderr)

    def test_transcript_includes_codex_session_log_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt", tmp, task_id="task-session")
            task["session_id"] = "session-123"
            save_task(config, task)
            session_path = (
                Path(tmp)
                / "codex-home"
                / "sessions"
                / "2026"
                / "06"
                / "20"
                / "rollout-2026-06-20T00-00-00-session-123.jsonl"
            )
            session_path.parent.mkdir(parents=True)
            session_path.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "session summary"}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"CODEX_HOME": str(Path(tmp) / "codex-home")}):
                code, output = run_cli(["--config", str(config_path), "transcript", "task-session"])

            self.assertEqual(0, code)
            self.assertIn("## codex session: rollout-2026-06-20T00-00-00-session-123.jsonl", output)
            self.assertIn("### assistant", output)
            self.assertIn("session summary", output)


if __name__ == "__main__":
    unittest.main()

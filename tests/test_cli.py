from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.evidence import rate_limit_dir
from codex_batch_runner.fs import write_json_atomic
from codex_batch_runner.queue import create_task, load_task, save_task


def write_config(tmp: str) -> Path:
    root = Path(tmp)
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "tasks"),
                "log_dir": str(root / "logs"),
                "lock_file": str(root / "runner.lock"),
                "state_file": str(root / "state.json"),
            }
        ),
        encoding="utf-8",
    )
    return config_path


def run_cli(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, stdout.getvalue()


def set_status(config: Config, task_id: str, status: str, last_error: str | None = None) -> None:
    task = load_task(config, task_id)
    task["status"] = status
    task["last_error"] = last_error
    save_task(config, task)


class CliTests(unittest.TestCase):
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
            self.assertIn("runnable\trunnable", output)
            self.assertIn("resume\tneeds_resume", output)
            self.assertIn("running\trunning", output)
            self.assertIn("blocked\tblocked_user", output)
            self.assertIn("failed\tfailed", output)
            self.assertIn("completed\tcompleted\tattempts=0 [review=unreviewed]", output)
            self.assertIn("rejected\tcompleted\tattempts=0 [review=rejected]", output)
            self.assertIn("needs-followup\tcompleted\tattempts=0 [review=needs_followup]", output)
            self.assertNotIn("accepted\tcompleted", output)
            self.assertNotIn("archived\tarchived", output)

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
            self.assertIn("unreviewed\tcompleted", output)
            self.assertNotIn("accepted\tcompleted", output)
            self.assertNotIn("rejected\tcompleted", output)
            self.assertNotIn("followup\tcompleted", output)

            code, output = run_cli(["--config", str(config_path), "list", "--needs-review"])

            self.assertEqual(0, code)
            self.assertIn("unreviewed\tcompleted", output)
            self.assertIn("rejected\tcompleted", output)
            self.assertIn("followup\tcompleted", output)
            self.assertNotIn("accepted\tcompleted", output)

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
            self.assertIn("completed\tcompleted", output)
            self.assertIn("archived\tarchived", output)

    def test_status_filter_can_show_archived_without_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "archived", tmp, task_id="archived")
            create_task(config, "runnable", tmp, task_id="runnable")
            set_status(config, "archived", "archived")

            code, output = run_cli(["--config", str(config_path), "list", "--status", "archived"])

            self.assertEqual(0, code)
            self.assertIn("archived\tarchived", output)
            self.assertNotIn("runnable\trunnable", output)

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

    def test_list_all_shows_completed_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "done", tmp, task_id="done")
            set_status(config, "done", "completed")

            code, output = run_cli(["--config", str(config_path), "list", "--all"])

            self.assertEqual(0, code)
            self.assertIn("done\tcompleted\tattempts=0 [review=unreviewed]", output)

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

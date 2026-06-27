from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.events import list_events, sanitize_payload, write_event
from codex_batch_runner.queue import archive_task, create_task, load_task, save_task, set_resolution, set_review_status
from codex_batch_runner.runner import run_next


def run_cli(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, stdout.getvalue()


def write_config(tmp: str, mode: str = "success") -> Path:
    root = Path(tmp)
    config_path = root / "config.json"
    fake_codex = Path(__file__).parent / "fixtures" / "fake_codex.py"
    config_path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "tasks"),
                "log_dir": str(root / "logs"),
                "event_dir": str(root / "events"),
                "lock_file": str(root / "runner.lock"),
                "state_file": str(root / "state.json"),
                "codex_command": ["python3", str(fake_codex), mode],
                "codex_resume_command": ["python3", str(fake_codex), mode, "resume", "{session_id}"],
            }
        ),
        encoding="utf-8",
    )
    return config_path


class EventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict("os.environ", {"CBR_CONFIG": ""}, clear=False)
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

    def test_write_event_appends_date_partitioned_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = create_task(config, "private prompt", tmp, task_id="task-event", project_id="project-a")

            event = write_event(
                config,
                "custom_event",
                task=task,
                source="test",
                summary="custom summary",
                payload={"status": "runnable"},
            )
            events = list_events(config, task_id="task-event", limit=10)

            self.assertEqual(2, len(events))
            self.assertEqual("custom_event", events[0]["event_type"])
            self.assertEqual(event["event_id"], events[0]["event_id"])
            self.assertEqual("project-a", events[0]["project_id"])
            self.assertTrue((config.event_dir / f"{event['occurred_at'][:10]}.jsonl").exists())

    def test_sanitize_payload_redacts_prompts_ids_and_secrets(self) -> None:
        payload = sanitize_payload(
            {
                "prompt": "raw private prompt",
                "next_prompt": "continue with private details",
                "session_id": "session-private",
                "thread_id": "thread-private",
                "nested": {
                    "token": "secret-token",
                    "message": "api_key=abc123 bearer xyz987",
                },
            }
        )
        text = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("raw private prompt", text)
        self.assertNotIn("session-private", text)
        self.assertNotIn("thread-private", text)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("abc123", text)
        self.assertNotIn("xyz987", text)
        self.assertEqual("[REDACTED]", payload["prompt"])

    def test_events_command_lists_json_and_human_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="listed")

            code, output = run_cli(["--config", str(config_path), "events", "--task-id", "listed", "--json"])
            events = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(1, len(events))
            self.assertEqual("task_created", events[0]["event_type"])

            code, output = run_cli(["--config", str(config_path), "events", "--task-id", "listed", "--limit", "1"])

            self.assertEqual(0, code)
            self.assertIn("OCCURRED_AT\tTYPE\tTASK\tSUMMARY", output)
            self.assertIn("task_created\tlisted", output)

    def test_commands_emit_representative_task_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="flow")

            self.assertEqual("completed", run_next(config).status)
            set_review_status(config, "flow", "accepted", "verified")
            task = load_task(config, "flow")
            task["status"] = "failed"
            save_task(config, task)
            set_resolution(config, "flow", "manual", "handled")
            archive_task(config, "flow")
            event_types = [event["event_type"] for event in list_events(config, task_id="flow", limit=20)]

            self.assertIn("task_created", event_types)
            self.assertIn("task_started", event_types)
            self.assertIn("task_completed", event_types)
            self.assertIn("task_reviewed", event_types)
            self.assertIn("task_resolved", event_types)
            self.assertIn("task_archived", event_types)

    def test_rate_limit_detection_emits_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, mode="rate_limit")
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="limited")

            outcome = run_next(config)
            events = list_events(config, task_id="limited", limit=10)

            self.assertEqual("needs_resume", outcome.status)
            self.assertIn("rate_limit_detected", [event["event_type"] for event in events])


if __name__ == "__main__":
    unittest.main()

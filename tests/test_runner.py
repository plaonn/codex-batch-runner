from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.config import Config
from codex_batch_runner.codex import CodexResult
from codex_batch_runner.queue import create_task, load_task
from codex_batch_runner.runner import apply_codex_result, run_next
from codex_batch_runner.state import load_state


FIXTURE = Path(__file__).parent / "fixtures" / "fake_codex.py"


def make_config(tmp: str, mode: str) -> Config:
    base = Config.load(root=Path(tmp))
    return Config(
        root=base.root,
        queue_dir=base.queue_dir,
        log_dir=base.log_dir,
        lock_file=base.lock_file,
        state_file=base.state_file,
        codex_command=[sys.executable, str(FIXTURE), mode],
        codex_resume_command=[sys.executable, str(FIXTURE), mode, "resume", "{session_id}"],
        stale_lock_seconds=base.stale_lock_seconds,
        rate_limit_cooldown_seconds=1800,
        default_max_attempts=base.default_max_attempts,
    )


class RunnerTests(unittest.TestCase):
    def test_run_next_completes_task_and_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(config, "do it", tmp, task_id="task-1")

            outcome = run_next(config)
            task = load_task(config, "task-1")

            self.assertEqual("completed", outcome.status)
            self.assertEqual("completed", task["status"])
            self.assertEqual(1, task["attempts"])
            self.assertTrue(task["log_paths"])
            self.assertTrue(Path(task["log_paths"][0]).exists())
            self.assertEqual("synthetic-session", task["session_id"])

    def test_run_next_stores_needs_resume_next_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "needs_resume")
            create_task(config, "do part", tmp, task_id="task-2")

            outcome = run_next(config)
            task = load_task(config, "task-2")

            self.assertEqual("needs_resume", outcome.status)
            self.assertEqual("needs_resume", task["status"])
            self.assertEqual("continue synthetic task", task["next_prompt"])

    def test_rate_limit_sets_task_and_global_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "rate_limit")
            create_task(config, "do it later", tmp, task_id="task-3")

            outcome = run_next(config)
            task = load_task(config, "task-3")
            state = load_state(config)

            self.assertEqual("runnable", outcome.status)
            self.assertEqual("runnable", task["status"])
            self.assertIsNotNone(task["cooldown_until"])
            self.assertEqual(task["cooldown_until"], state["global_cooldown_until"])

    def test_final_response_wins_over_stderr_rate_limit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            task = create_task(config, "do it", tmp, task_id="task-final")
            task["status"] = "running"
            result = CodexResult(
                returncode=0,
                log_path=Path(tmp) / "attempt.jsonl",
                stderr="OSLogRateLimit warning from plugin loader",
                events=[],
                final_response={
                    "task_id": "task-final",
                    "status": "completed",
                    "summary": "done",
                    "next_prompt": "",
                    "changed_files": [],
                    "verification": [],
                },
                session_id="thread-1",
                thread_id="thread-1",
                rate_limited=True,
            )

            apply_codex_result(config, task, result)
            loaded = load_task(config, "task-final")

            self.assertEqual("completed", loaded["status"])
            self.assertIsNone(loaded["cooldown_until"])

    def test_malformed_final_json_retries_until_max_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "malformed")
            task = create_task(config, "bad", tmp, task_id="task-4")
            task["max_attempts"] = 1
            from codex_batch_runner.queue import save_task

            save_task(config, task)
            outcome = run_next(config)
            task = load_task(config, "task-4")

            self.assertEqual("failed", outcome.status)
            self.assertEqual("failed", task["status"])


if __name__ == "__main__":
    unittest.main()

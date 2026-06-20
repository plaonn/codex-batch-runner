from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from codex_batch_runner.config import Config
import codex_batch_runner.runner as runner_module
from codex_batch_runner.codex import CodexResult
from codex_batch_runner.evidence import list_rate_limit_evidence
from codex_batch_runner.queue import create_task, load_task, save_task
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


def missing_command_config(tmp: str) -> Config:
    base = Config.load(root=Path(tmp))
    return Config(
        root=base.root,
        queue_dir=base.queue_dir,
        log_dir=base.log_dir,
        lock_file=base.lock_file,
        state_file=base.state_file,
        codex_command=[str(Path(tmp) / "missing-codex-command")],
        codex_resume_command=[str(Path(tmp) / "missing-codex-command"), "resume", "{session_id}"],
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
            self.assertEqual("unreviewed", task["review_status"])
            self.assertIsNone(task["reviewed_at"])
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

    def test_run_next_uses_thread_id_as_resume_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            task = create_task(config, "do part", tmp, task_id="task-thread-resume")
            task["status"] = "needs_resume"
            task["next_prompt"] = "continue"
            task["thread_id"] = "thread-only"

            save_task(config, task)
            seen_prompts = []

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                seen_prompts.append(prompt)
                return CodexResult(
                    returncode=0,
                    log_path=Path(tmp) / "attempt.jsonl",
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": "task-thread-resume",
                        "status": "completed",
                        "summary": "done",
                        "next_prompt": "",
                        "changed_files": [],
                        "verification": [],
                    },
                    session_id="thread-only",
                    thread_id="thread-only",
                    rate_limited=False,
                    rate_limit_markers=[],
                )

            with patch.object(runner_module, "run_codex", fake_run_codex):
                run_next(config)

            self.assertEqual(1, len(seen_prompts))
            self.assertIn("continue", seen_prompts[0])
            self.assertNotIn("do part", seen_prompts[0])
            self.assertNotIn("resume_unavailable: true", seen_prompts[0])
            task = load_task(config, "task-thread-resume")
            self.assertTrue(task["resume_requested"])
            self.assertFalse(task["resume_unavailable"])

    def test_resume_without_identifier_records_resume_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            task = create_task(config, "original", tmp, task_id="task-resume-unavailable")
            task["status"] = "needs_resume"
            task["next_prompt"] = "continue without session"
            save_task(config, task)
            seen_prompts = []

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                seen_prompts.append(prompt)
                return CodexResult(
                    returncode=0,
                    log_path=Path(tmp) / "attempt.jsonl",
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": "task-resume-unavailable",
                        "status": "completed",
                        "summary": "done",
                        "next_prompt": "",
                        "changed_files": [],
                        "verification": [],
                    },
                    session_id=None,
                    thread_id=None,
                    rate_limited=False,
                    rate_limit_markers=[],
                )

            with patch.object(runner_module, "run_codex", fake_run_codex):
                run_next(config)

            loaded = load_task(config, "task-resume-unavailable")
            self.assertEqual(1, len(seen_prompts))
            self.assertIn("resume_unavailable: true", seen_prompts[0])
            self.assertIn("continue without session", seen_prompts[0])
            self.assertTrue(loaded["resume_requested"])
            self.assertTrue(loaded["resume_unavailable"])
            self.assertIsNotNone(loaded["resume_unavailable_at"])
            self.assertEqual(1, loaded["resume_unavailable_attempts"])

    def test_rate_limit_with_session_keeps_task_resumable_after_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "rate_limit")
            create_task(config, "do it later", tmp, task_id="task-3")

            outcome = run_next(config)
            task = load_task(config, "task-3")
            state = load_state(config)

            self.assertEqual("needs_resume", outcome.status)
            self.assertEqual("needs_resume", task["status"])
            self.assertEqual("synthetic-session", task["session_id"])
            self.assertIsNotNone(task["cooldown_until"])
            self.assertEqual(task["cooldown_until"], state["global_cooldown_until"])

    def test_rate_limit_without_resume_id_preserves_runnable_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            task = create_task(config, "do it later", tmp, task_id="task-no-resume")
            task["status"] = "running"
            task["attempts"] = 1
            result = CodexResult(
                returncode=1,
                log_path=Path(tmp) / "attempt.jsonl",
                stderr="usage limit reached, try again later",
                events=[],
                final_response=None,
                session_id=None,
                thread_id=None,
                rate_limited=True,
                rate_limit_markers=["usage limit", "try again"],
            )

            apply_codex_result(config, task, result)
            loaded = load_task(config, "task-no-resume")

            self.assertEqual("runnable", loaded["status"])
            self.assertIsNotNone(loaded["cooldown_until"])

    def test_rate_limit_evidence_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            task = create_task(config, "prompt containing private details", tmp, task_id="task-evidence")
            task["status"] = "running"
            task["attempts"] = 2
            result = CodexResult(
                returncode=1,
                log_path=Path(tmp) / "logs" / "attempt-2.jsonl",
                stderr="usage limit reached token=secret-value\n" + ("x" * 800),
                events=[
                    {"type": "session.started", "session_id": "synthetic-session", "thread_id": "synthetic-thread"},
                    {"type": "error", "message": "429 quota reached api_key=abc123"},
                ],
                final_response=None,
                session_id="synthetic-session",
                thread_id="synthetic-thread",
                rate_limited=True,
                rate_limit_markers=["usage limit", "429", "quota"],
            )

            apply_codex_result(config, task, result)
            events = list_rate_limit_evidence(config)

            self.assertEqual(1, len(events))
            evidence = events[0]
            evidence_text = json.dumps(evidence, ensure_ascii=False)
            self.assertEqual("task-evidence", evidence["task_id"])
            self.assertEqual(2, evidence["attempt"])
            self.assertEqual(["429", "quota", "usage limit"], evidence["matched_markers"])
            self.assertIn("attempt-2.jsonl", evidence["original_log_path"])
            self.assertNotIn("prompt containing private details", evidence_text)
            self.assertNotIn("synthetic-session", evidence_text)
            self.assertNotIn("synthetic-thread", evidence_text)
            self.assertNotIn("secret-value", evidence_text)
            self.assertNotIn("abc123", evidence_text)
            self.assertLessEqual(len(evidence["stderr_excerpt"]), 503)

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
                rate_limit_markers=["rate limit"],
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

            save_task(config, task)
            outcome = run_next(config)
            task = load_task(config, "task-4")

            self.assertEqual("failed", outcome.status)
            self.assertEqual("failed", task["status"])

    def test_missing_codex_command_does_not_leave_task_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = missing_command_config(tmp)
            create_task(config, "do it", tmp, task_id="task-missing-command")

            outcome = run_next(config)
            task = load_task(config, "task-missing-command")

            self.assertEqual("runnable", outcome.status)
            self.assertEqual("runnable", task["status"])
            self.assertIn("No such file", task["last_error"])
            self.assertTrue(task["log_paths"])


if __name__ == "__main__":
    unittest.main()

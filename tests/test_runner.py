from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path

from codex_batch_runner.config import Config
import codex_batch_runner.runner as runner_module
from codex_batch_runner.codex import CodexResult
from codex_batch_runner.evidence import list_rate_limit_evidence
from codex_batch_runner.events import list_events
from codex_batch_runner.queue import create_task, load_task, save_task
from codex_batch_runner.runner import apply_codex_result, run_next
from codex_batch_runner.state import load_state
from codex_batch_runner.timeutil import iso_now
from codex_batch_runner.worktree import build_prepare_report


FIXTURE = Path(__file__).parent / "fixtures" / "fake_codex.py"


def make_config(tmp: str, mode: str, trigger_command: list[str] | None = None) -> Config:
    base = Config.load(root=Path(tmp))
    return Config(
        root=base.root,
        queue_dir=base.queue_dir,
        log_dir=base.log_dir,
        event_dir=base.event_dir,
        notifier_cursor_state_paths=base.notifier_cursor_state_paths,
        lock_file=base.lock_file,
        state_file=base.state_file,
        codex_command=[sys.executable, str(FIXTURE), mode],
        codex_resume_command=[sys.executable, str(FIXTURE), mode, "resume", "{session_id}"],
        post_mutation_trigger_command=trigger_command or [],
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
        event_dir=base.event_dir,
        notifier_cursor_state_paths=base.notifier_cursor_state_paths,
        lock_file=base.lock_file,
        state_file=base.state_file,
        codex_command=[str(Path(tmp) / "missing-codex-command")],
        codex_resume_command=[str(Path(tmp) / "missing-codex-command"), "resume", "{session_id}"],
        post_mutation_trigger_command=[],
        stale_lock_seconds=base.stale_lock_seconds,
        rate_limit_cooldown_seconds=1800,
        default_max_attempts=base.default_max_attempts,
    )


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test User")
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "file.txt")
    git(path, "commit", "-m", "initial")


def create_clean_completed_task(config: Config, repo: Path, task_id: str = "reviewable") -> dict:
    task = create_task(config, "work", str(repo), task_id=task_id)
    task["status"] = "completed"
    task["review_status"] = "unreviewed"
    task["completed_at"] = "2026-01-01T00:00:00+00:00"
    task["last_result"] = {
        "task_id": task_id,
        "status": "completed",
        "summary": "done",
        "changed_files": ["file.txt"],
        "verification": ["unit tests"],
    }
    task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
    save_task(config, task)
    return task


class RunnerTests(unittest.TestCase):
    def test_run_next_processes_one_task_and_triggers_after_lock_release_when_follow_up_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "Path(sys.argv[1]).write_text("
                    "'locked\\n' if Path(sys.argv[2]).exists() else 'unlocked\\n', encoding='utf-8')"
                ),
                str(marker),
                str(Path(tmp) / ".codex-batch-runner" / "runner.lock"),
            ]
            config = make_config(tmp, "success", trigger)
            create_task(config, "first", tmp, task_id="task-1")
            create_task(config, "second", tmp, task_id="task-2")

            outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("task-1", outcome.task_id)
            self.assertEqual("completed", load_task(config, "task-1")["status"])
            second = load_task(config, "task-2")
            self.assertEqual("runnable", second["status"])
            self.assertEqual(0, second["attempts"])
            self.assertEqual("unlocked\n", marker.read_text(encoding="utf-8"))

    def test_invalid_task_execution_profile_fails_before_codex_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            task = create_task(config, "work", tmp, task_id="bad-profile")
            task["execution_profile"] = "missing"
            save_task(config, task)

            with patch("codex_batch_runner.codex.subprocess.Popen", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            loaded = load_task(config, "bad-profile")
            self.assertEqual("failed", outcome.status)
            self.assertEqual("failed", loaded["status"])
            self.assertEqual(0, loaded["attempts"])
            self.assertIn("invalid execution profile", loaded["last_error"])

    def test_run_next_does_not_auto_review_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = make_config(tmp, "success")
            create_clean_completed_task(config, repo, "reviewable")

            with patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            self.assertEqual("empty", outcome.status)
            self.assertIsNone(outcome.task_id)
            self.assertEqual("unreviewed", load_task(config, "reviewable")["review_status"])

    def test_run_next_auto_review_disabled_preserves_runnable_first_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = make_config(tmp, "success")
            create_clean_completed_task(config, repo, "reviewable")
            create_task(config, "implementation", tmp, task_id="implementation")

            outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("implementation", outcome.task_id)
            self.assertEqual("unreviewed", load_task(config, "reviewable")["review_status"])

    def test_run_next_auto_review_accepts_one_completed_task_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(make_config(tmp, "success"), auto_review_mechanical_accept=True)
            create_clean_completed_task(config, repo, "reviewable-1")
            create_clean_completed_task(config, repo, "reviewable-2")

            with patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            self.assertEqual("review_accepted", outcome.status)
            self.assertEqual("reviewable-1", outcome.task_id)
            self.assertEqual("accepted", load_task(config, "reviewable-1")["review_status"])
            self.assertEqual("unreviewed", load_task(config, "reviewable-2")["review_status"])
            self.assertFalse(outcome.review["auto_review"]["reviewer_codex_invoked"])

    def test_run_next_auto_review_takes_precedence_over_runnable_task_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(make_config(tmp, "success"), auto_review_mechanical_accept=True)
            create_clean_completed_task(config, repo, "reviewable")
            create_task(config, "implementation", tmp, task_id="implementation")

            with patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            self.assertEqual("review_accepted", outcome.status)
            self.assertEqual("reviewable", outcome.task_id)
            self.assertEqual("accepted", load_task(config, "reviewable")["review_status"])
            self.assertEqual("runnable", load_task(config, "implementation")["status"])

    def test_run_next_non_actionable_auto_review_does_not_starve_runnable_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(make_config(tmp, "success"), auto_review_mechanical_accept=True)
            reviewable = create_clean_completed_task(config, repo, "reviewable")
            reviewable["last_result"]["verification"] = []
            save_task(config, reviewable)
            create_task(config, "implementation", tmp, task_id="implementation")

            outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("implementation", outcome.task_id)
            self.assertEqual("unreviewed", load_task(config, "reviewable")["review_status"])

    def test_run_next_auto_review_does_not_repeat_same_needs_human_reviewer_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(
                make_config(tmp, "reviewer_needs_human"),
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
            )
            create_clean_completed_task(config, repo, "reviewable")

            first = run_next(config)
            task = load_task(config, "reviewable")
            reviewer_logs = list((config.log_dir / "reviewable").glob("reviewer-*.jsonl"))

            create_task(config, "implementation", tmp, task_id="implementation")
            success_config = replace(config, codex_command=[sys.executable, str(FIXTURE), "success"])
            second = run_next(success_config)
            reviewer_logs_after = list((config.log_dir / "reviewable").glob("reviewer-*.jsonl"))

            self.assertEqual("review_needed", first.status)
            self.assertEqual("reviewable", first.task_id)
            self.assertTrue(first.review["auto_review"]["reviewer_codex_invoked"])
            self.assertEqual("needs_human", task["reviewer_codex"]["decision"])
            self.assertEqual("needs_human", task["reviewer_codex_backoff"]["decision"])
            self.assertEqual("completed", second.status)
            self.assertEqual("implementation", second.task_id)
            self.assertEqual(reviewer_logs, reviewer_logs_after)

    def test_run_next_auto_review_skips_backed_off_oldest_candidate_for_next_review_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(
                make_config(tmp, "reviewer_needs_human"),
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
            )
            old = create_clean_completed_task(config, repo, "old-review")
            old["completed_at"] = "2026-01-01T00:00:00+00:00"
            save_task(config, old)
            new = create_clean_completed_task(config, repo, "new-review")
            new["completed_at"] = "2026-01-02T00:00:00+00:00"
            save_task(config, new)

            first = run_next(config)
            pass_config = replace(config, codex_command=[sys.executable, str(FIXTURE), "reviewer_pass"])
            second = run_next(pass_config)

            self.assertEqual("review_needed", first.status)
            self.assertEqual("old-review", first.task_id)
            self.assertEqual("review_accepted", second.status)
            self.assertEqual("new-review", second.task_id)
            self.assertEqual("unreviewed", load_task(config, "old-review")["review_status"])
            self.assertEqual("accepted", load_task(config, "new-review")["review_status"])
            self.assertEqual(
                [{"task_id": "old-review", "decision": "needs_human", "reason": "synthetic reviewer needs human input", "recorded_at": load_task(config, "old-review")["reviewer_codex_backoff"]["recorded_at"]}],
                second.review["skipped_review_candidates"],
            )

    def test_run_next_auto_review_respects_runner_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(make_config(tmp, "success"), auto_review_mechanical_accept=True)
            create_clean_completed_task(config, repo, "locked-review")
            config.lock_file.parent.mkdir(parents=True, exist_ok=True)
            config.lock_file.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "hostname": socket.gethostname(),
                        "created_at": iso_now(),
                        "task_id": "running-task",
                    }
                ),
                encoding="utf-8",
            )

            outcome = run_next(config)

            self.assertEqual("locked", outcome.status)
            self.assertEqual("unreviewed", load_task(config, "locked-review")["review_status"])

    def test_run_next_auto_review_triggers_when_dependent_task_becomes_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "Path(sys.argv[1]).write_text("
                    "'locked\\n' if Path(sys.argv[2]).exists() else 'unlocked\\n', encoding='utf-8')"
                ),
                str(marker),
                str(Path(tmp) / ".codex-batch-runner" / "runner.lock"),
            ]
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(
                make_config(tmp, "success", trigger),
                auto_review_mechanical_accept=True,
                dependency_requires_accepted_review=True,
            )
            create_clean_completed_task(config, repo, "dependency")
            create_task(config, "child", tmp, task_id="child", depends_on=["dependency"])

            with patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            self.assertEqual("review_accepted", outcome.status)
            self.assertEqual("accepted", load_task(config, "dependency")["review_status"])
            self.assertEqual("runnable", load_task(config, "child")["status"])
            self.assertEqual("unlocked\n", marker.read_text(encoding="utf-8"))

    def test_run_next_auto_review_triggers_when_another_review_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "Path(sys.argv[1]).write_text("
                    "'locked\\n' if Path(sys.argv[2]).exists() else 'unlocked\\n', encoding='utf-8')"
                ),
                str(marker),
                str(Path(tmp) / ".codex-batch-runner" / "runner.lock"),
            ]
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(make_config(tmp, "success", trigger), auto_review_mechanical_accept=True)
            create_clean_completed_task(config, repo, "reviewable-1")
            create_clean_completed_task(config, repo, "reviewable-2")

            with patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            self.assertEqual("review_accepted", outcome.status)
            self.assertEqual("reviewable-1", outcome.task_id)
            self.assertEqual("accepted", load_task(config, "reviewable-1")["review_status"])
            self.assertEqual("unreviewed", load_task(config, "reviewable-2")["review_status"])
            self.assertEqual("unlocked\n", marker.read_text(encoding="utf-8"))

    def test_run_next_mutation_free_auto_review_does_not_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('x\\n', encoding='utf-8')",
                str(marker),
            ]
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(make_config(tmp, "success", trigger), auto_review_mechanical_accept=True)
            reviewable = create_clean_completed_task(config, repo, "reviewable")
            reviewable["last_result"]["verification"] = []
            save_task(config, reviewable)

            with patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            self.assertEqual("review_needed", outcome.status)
            self.assertEqual("reviewable", outcome.task_id)
            self.assertEqual("unreviewed", load_task(config, "reviewable")["review_status"])
            self.assertFalse(marker.exists())

    def test_run_next_auto_review_does_not_trigger_when_cooldown_becomes_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('x\\n', encoding='utf-8')",
                str(marker),
            ]
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(make_config(tmp, "success", trigger), auto_review_mechanical_accept=True)
            create_clean_completed_task(config, repo, "reviewable")
            create_task(config, "implementation", tmp, task_id="implementation")

            with (
                patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")),
                patch("codex_batch_runner.runner.in_global_cooldown", side_effect=[False, True]),
            ):
                outcome = run_next(config)

            self.assertEqual("review_accepted", outcome.status)
            self.assertEqual("accepted", load_task(config, "reviewable")["review_status"])
            self.assertFalse(marker.exists())

    def test_run_next_does_not_trigger_without_eligible_follow_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('x\\n', encoding='utf-8')",
                str(marker),
            ]
            config = make_config(tmp, "success", trigger)
            create_task(config, "first", tmp, task_id="task-1")
            create_task(config, "blocked", tmp, task_id="task-2", depends_on=["missing-dependency"])

            outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("task-1", outcome.task_id)
            self.assertFalse(marker.exists())

    def test_run_next_skips_child_with_unaccepted_completed_dependency_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(make_config(tmp, "success"), dependency_requires_accepted_review=True)
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "unreviewed"
            save_task(config, dep)
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])
            create_task(config, "ready", tmp, task_id="ready")

            outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("ready", outcome.task_id)
            self.assertEqual("runnable", load_task(config, "child")["status"])

    def test_run_next_does_not_trigger_when_global_cooldown_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('x\\n', encoding='utf-8')",
                str(marker),
            ]
            config = make_config(tmp, "success", trigger)
            create_task(config, "work", tmp, task_id="task-1")
            task = load_task(config, "task-1")
            task["status"] = "running"
            result = CodexResult(
                returncode=1,
                log_path=Path(tmp) / "attempt.jsonl",
                command_kind="exec",
                resume_id_used=None,
                stderr="usage limit reached, try again later",
                events=[],
                final_response=None,
                session_id=None,
                thread_id=None,
                rate_limited=True,
                rate_limit_markers=["usage limit", "try again"],
            )

            apply_codex_result(config, task, result)
            outcome = run_next(config)

            self.assertEqual("cooldown", outcome.status)
            self.assertFalse(marker.exists())

    def test_post_run_trigger_failure_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success", [sys.executable, "-c", "import sys; sys.exit(7)"])
            create_task(config, "first", tmp, task_id="task-1")
            create_task(config, "second", tmp, task_id="task-2")

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("completed", load_task(config, "task-1")["status"])
            self.assertEqual("runnable", load_task(config, "task-2")["status"])
            self.assertIn("warning: post-run trigger exited with status 7", stderr.getvalue())

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
            self.assertEqual(1, task["run_count"])
            self.assertTrue(task["log_paths"])
            self.assertTrue(Path(task["log_paths"][0]).exists())
            self.assertEqual("synthetic-session", task["session_id"])
            self.assertEqual("exec", task["last_run"]["command_kind"])
            self.assertEqual(0, task["last_run"]["returncode"])
            self.assertIsNone(task["last_run"]["resume_id_used"])
            self.assertIsNotNone(task["last_run"]["duration_seconds"])

    def test_run_next_worktree_disabled_uses_original_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(config, "do it", tmp, task_id="task-disabled-worktree")
            seen_cwds = []

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                seen_cwds.append(task["cwd"])
                return CodexResult(
                    returncode=0,
                    log_path=Path(tmp) / "attempt.jsonl",
                    command_kind="exec",
                    resume_id_used=None,
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": "task-disabled-worktree",
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
                outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual([tmp], seen_cwds)
            self.assertNotIn("execution_worktree_status", load_task(config, "task-disabled-worktree"))

    def test_run_next_worktree_task_runs_codex_in_prepared_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(make_config(tmp, "success"), worktree_mode="task", worktree_root=root / "worktrees")
            create_task(config, "do it", str(repo), task_id="task-worktree")
            seen_cwds = []

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                seen_cwds.append(Path(task["cwd"]))
                return CodexResult(
                    returncode=0,
                    log_path=root / "attempt.jsonl",
                    command_kind="exec",
                    resume_id_used=None,
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": "task-worktree",
                        "status": "completed",
                        "summary": "done",
                        "next_prompt": "",
                        "changed_files": ["file.txt"],
                        "verification": ["unit tests"],
                    },
                    session_id=None,
                    thread_id=None,
                    rate_limited=False,
                    rate_limit_markers=[],
                )

            with patch.object(runner_module, "run_codex", fake_run_codex):
                outcome = run_next(config)

            task = load_task(config, "task-worktree")
            worktree_path = Path(task["execution_worktree_path"])
            self.assertEqual("completed", outcome.status)
            self.assertEqual(str(repo), task["cwd"])
            self.assertEqual([worktree_path], seen_cwds)
            self.assertEqual("git_worktree", task["execution_mode"])
            self.assertEqual("retained", task["execution_worktree_status"])
            self.assertTrue(worktree_path.is_dir())
            self.assertEqual(str(worktree_path), task["git_status"]["root"])

    def test_run_next_worktree_completed_task_commits_reported_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(make_config(tmp, "success"), worktree_mode="task", worktree_root=root / "worktrees")
            create_task(config, "do it", str(repo), task_id="task-worktree-commit")

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                Path(task["cwd"], "file.txt").write_text("changed\n", encoding="utf-8")
                Path(task["cwd"], "new.txt").write_text("new\n", encoding="utf-8")
                return CodexResult(
                    returncode=0,
                    log_path=root / "attempt.jsonl",
                    command_kind="exec",
                    resume_id_used=None,
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": "task-worktree-commit",
                        "status": "completed",
                        "summary": "done",
                        "next_prompt": "",
                        "changed_files": ["file.txt", "new.txt"],
                        "verification": ["unit tests"],
                    },
                    session_id=None,
                    thread_id=None,
                    rate_limited=False,
                    rate_limit_markers=[],
                )

            with patch.object(runner_module, "run_codex", fake_run_codex):
                outcome = run_next(config)

            task = load_task(config, "task-worktree-commit")
            worktree_path = Path(task["execution_worktree_path"])
            base = task["execution_base_head"]
            rev_list = git(worktree_path, "rev-list", "--count", f"{base}..HEAD").stdout.strip()
            status = git(worktree_path, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()

            self.assertEqual("completed", outcome.status)
            self.assertEqual("completed", task["status"])
            self.assertEqual("retained", task["execution_worktree_status"])
            self.assertEqual("1", rev_list)
            self.assertEqual("", status)
            self.assertFalse(task["git_status"]["dirty"])
            self.assertTrue(task["last_result"]["commits"])
            self.assertEqual("not_pushed", task["last_result"]["push_status"]["status"])
            self.assertEqual("base\n", (repo / "file.txt").read_text(encoding="utf-8"))

    def test_run_next_worktree_prepare_failure_does_not_call_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            git(repo, "branch", "cbr/task-conflict")
            config = replace(make_config(tmp, "success"), worktree_mode="task", worktree_root=root / "worktrees")
            create_task(config, "do it", str(repo), task_id="task-conflict")

            with patch.object(runner_module, "run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            task = load_task(config, "task-conflict")
            self.assertEqual("failed", outcome.status)
            self.assertEqual("failed", task["status"])
            self.assertEqual(0, task["attempts"])
            self.assertEqual(1, task["failure_count"])
            self.assertEqual("recovery_required", task["execution_worktree_status"])
            self.assertIn("existing branch", task["last_error"])

    def test_run_next_worktree_resume_requires_retained_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(make_config(tmp, "success"), worktree_mode="task", worktree_root=Path(tmp) / "worktrees")
            task = create_task(config, "original", tmp, task_id="task-resume-no-worktree")
            task["status"] = "needs_resume"
            task["next_prompt"] = "continue"
            task["thread_id"] = "thread-1"
            save_task(config, task)

            with patch.object(runner_module, "run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            task = load_task(config, "task-resume-no-worktree")
            self.assertEqual("failed", outcome.status)
            self.assertEqual("failed", task["status"])
            self.assertEqual(0, task["attempts"])
            self.assertIn("requires an existing retained git worktree", task["last_error"])

    def test_run_next_worktree_resume_uses_existing_retained_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(make_config(tmp, "success"), worktree_mode="task", worktree_root=root / "worktrees")
            task = create_task(config, "original", str(repo), task_id="task-resume-worktree")
            self.assertTrue(build_prepare_report(config, "task-resume-worktree", apply=True)["applied"])
            task = load_task(config, "task-resume-worktree")
            worktree_path = Path(task["execution_worktree_path"])
            task["status"] = "needs_resume"
            task["next_prompt"] = "continue"
            task["thread_id"] = "thread-1"
            task["execution_worktree_status"] = "retained"
            save_task(config, task)
            seen_cwds = []

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                seen_cwds.append(Path(task["cwd"]))
                return CodexResult(
                    returncode=0,
                    log_path=root / "attempt.jsonl",
                    command_kind="resume",
                    resume_id_used="thread-1",
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": "task-resume-worktree",
                        "status": "completed",
                        "summary": "done",
                        "next_prompt": "",
                        "changed_files": [],
                        "verification": [],
                    },
                    session_id="thread-1",
                    thread_id="thread-1",
                    rate_limited=False,
                    rate_limit_markers=[],
                )

            with patch.object(runner_module, "run_codex", fake_run_codex):
                outcome = run_next(config)

            task = load_task(config, "task-resume-worktree")
            self.assertEqual("completed", outcome.status)
            self.assertEqual([worktree_path], seen_cwds)
            self.assertEqual("retained", task["execution_worktree_status"])
            self.assertEqual(1, task["resume_count"])

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
                    command_kind="resume",
                    resume_id_used="thread-only",
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
            self.assertEqual(1, task["resume_count"])
            self.assertEqual("resume", task["last_run"]["command_kind"])
            self.assertEqual("thread-only", task["last_run"]["resume_id_used"])

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
                    command_kind="exec",
                    resume_id_used=None,
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
            self.assertEqual("exec", loaded["last_run"]["command_kind"])
            self.assertIsNone(loaded["last_run"]["resume_id_used"])

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
            self.assertEqual(1, task["rate_limit_count"])
            self.assertEqual(1, task["run_count"])
            self.assertEqual("exec", task["last_run"]["command_kind"])
            self.assertEqual(1, task["last_run"]["returncode"])

    def test_rate_limit_without_resume_id_preserves_runnable_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            task = create_task(config, "do it later", tmp, task_id="task-no-resume")
            task["status"] = "running"
            task["attempts"] = 1
            result = CodexResult(
                returncode=1,
                log_path=Path(tmp) / "attempt.jsonl",
                command_kind="exec",
                resume_id_used=None,
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
            self.assertEqual(1, loaded["rate_limit_count"])
            self.assertEqual(1, loaded["last_run"]["returncode"])

    def test_rate_limit_evidence_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            task = create_task(config, "prompt containing private details", tmp, task_id="task-evidence")
            task["status"] = "running"
            task["attempts"] = 2
            result = CodexResult(
                returncode=1,
                log_path=Path(tmp) / "logs" / "attempt-2.jsonl",
                command_kind="exec",
                resume_id_used=None,
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
                command_kind="exec",
                resume_id_used=None,
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

    def test_apply_result_preserves_optional_metadata_and_records_git_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            repo = root / "repo"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, stdout=subprocess.PIPE)
            git(repo, "config", "user.email", "test@example.invalid")
            git(repo, "config", "user.name", "Test User")
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            git(repo, "remote", "add", "origin", str(remote))
            git(repo, "push", "-u", "origin", "main")
            (repo / "file.txt").write_text("base\nchange\n", encoding="utf-8")
            git(repo, "commit", "-am", "local change")

            config = make_config(tmp, "success")
            task = create_task(config, "do it", str(repo), task_id="task-git")
            task["status"] = "running"
            result = CodexResult(
                returncode=0,
                log_path=root / "attempt.jsonl",
                command_kind="exec",
                resume_id_used=None,
                stderr="",
                events=[],
                final_response={
                    "task_id": "task-git",
                    "status": "completed",
                    "summary": "done",
                    "next_prompt": "",
                    "changed_files": ["file.txt"],
                    "verification": ["unit tests"],
                    "commits": ["local change"],
                    "push_status": {"ahead": 1, "behind": 0},
                },
                session_id=None,
                thread_id=None,
                rate_limited=False,
                rate_limit_markers=[],
            )

            apply_codex_result(config, task, result)
            loaded = load_task(config, "task-git")

            self.assertEqual(["local change"], loaded["last_result"]["commits"])
            self.assertEqual({"ahead": 1, "behind": 0}, loaded["last_result"]["push_status"])
            self.assertEqual(1, loaded["git_status"]["ahead"])
            self.assertEqual(0, loaded["git_status"]["behind"])
            self.assertTrue(loaded["git_status"]["has_unpushed"])
            self.assertIn("local change", " ".join(loaded["git_status"]["unpushed_commits"]))

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

    def test_watchdog_terminates_empty_stdout_startup_stall_as_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                make_config(tmp, "hang_empty"),
                codex_startup_stall_seconds=1,
                codex_first_meaningful_timeout_seconds=5,
                codex_watchdog_grace_seconds=1,
            )
            create_task(config, "do it", tmp, task_id="task-empty-stall")

            outcome = run_next(config)
            task = load_task(config, "task-empty-stall")

            self.assertEqual("runnable", outcome.status)
            self.assertEqual("runnable", task["status"])
            self.assertIn("codex startup stalled before any JSONL output", task["last_error"])
            self.assertTrue(task["last_progress"]["stdout_empty"])
            self.assertTrue(task["last_progress"]["terminated_by_watchdog"])
            self.assertEqual("startup_stall", task["last_progress"]["watchdog_reason"])
            self.assertIsNotNone(task["cooldown_until"])

    def test_watchdog_terminates_startup_only_jsonl_as_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                make_config(tmp, "hang_startup"),
                codex_startup_stall_seconds=5,
                codex_first_meaningful_timeout_seconds=1,
                codex_watchdog_grace_seconds=1,
            )
            create_task(config, "do it", tmp, task_id="task-startup-stall")

            outcome = run_next(config)
            task = load_task(config, "task-startup-stall")

            self.assertEqual("needs_resume", outcome.status)
            self.assertEqual("needs_resume", task["status"])
            self.assertEqual("synthetic-session", task["session_id"])
            self.assertIn("codex turn stalled before meaningful JSONL events", task["last_error"])
            self.assertFalse(task["last_progress"]["stdout_empty"])
            self.assertTrue(task["last_progress"]["only_startup_events"])
            self.assertEqual(3, task["last_progress"]["jsonl_event_count"])
            self.assertEqual("first_meaningful_timeout", task["last_progress"]["watchdog_reason"])

    def test_watchdog_allows_meaningful_progress_before_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                make_config(tmp, "meaningful_then_hang"),
                codex_startup_stall_seconds=1,
                codex_first_meaningful_timeout_seconds=1,
                codex_mid_run_idle_seconds=1,
            )
            create_task(config, "do it", tmp, task_id="task-progress")

            outcome = run_next(config)
            task = load_task(config, "task-progress")

            self.assertEqual("completed", outcome.status)
            self.assertEqual("completed", task["status"])
            self.assertNotIn("last_progress", task)
            self.assertNotIn("watchdog_reason", task["last_run"])

    def test_watchdog_treats_item_progress_as_meaningful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                make_config(tmp, "item_progress_then_exit"),
                codex_startup_stall_seconds=1,
                codex_first_meaningful_timeout_seconds=1,
                codex_mid_run_idle_seconds=1,
            )
            create_task(config, "do it", tmp, task_id="task-item-progress")

            outcome = run_next(config)
            task = load_task(config, "task-item-progress")

            self.assertEqual("needs_resume", outcome.status)
            self.assertEqual("needs_resume", task["status"])
            self.assertNotIn("watchdog_reason", task["last_run"])
            self.assertIn("missing final JSON response", task["last_error"])

    def test_watchdog_warns_but_does_not_kill_mid_run_idle_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                make_config(tmp, "meaningful_idle_forever"),
                codex_startup_stall_seconds=1,
                codex_first_meaningful_timeout_seconds=1,
                codex_mid_run_idle_seconds=1,
                codex_mid_run_idle_kill_enabled=False,
            )
            create_task(config, "do it", tmp, task_id="task-idle")

            outcome = run_next(config)
            task = load_task(config, "task-idle")

            self.assertEqual("needs_resume", outcome.status)
            self.assertEqual("needs_resume", task["status"])
            self.assertNotIn("watchdog_reason", task["last_run"])
            self.assertIn("missing final JSON response", task["last_error"])

    def test_startup_stall_event_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(make_config(tmp, "hang_startup"), codex_first_meaningful_timeout_seconds=1)
            create_task(config, "prompt token=private-value", tmp, task_id="task-stall-event")

            run_next(config)
            events = list_events(config, task_id="task-stall-event", limit=10)
            stall_events = [event for event in events if event["event_type"] == "task_startup_stalled"]

            self.assertEqual(1, len(stall_events))
            text = json.dumps(stall_events[0], ensure_ascii=False)
            self.assertIn("codex turn stalled before meaningful JSONL events", text)
            self.assertNotIn("prompt token=private-value", text)
            self.assertNotIn("synthetic-session", text)
            self.assertNotIn("synthetic-thread", text)

    def test_run_next_recovers_dead_pid_lock_and_stale_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            stale = create_task(config, "was running", tmp, task_id="stale-running")
            stale["status"] = "running"
            stale["started_at"] = "2000-01-01T00:00:00+00:00"
            save_task(config, stale)
            create_task(config, "do it", tmp, task_id="next-task")
            config.lock_file.write_text(
                json.dumps(
                    {
                        "created_at": "2999-01-01T00:00:00+00:00",
                        "hostname": socket.gethostname(),
                        "pid": 424242,
                        "task_id": "stale-running",
                    }
                ),
                encoding="utf-8",
            )

            with patch("codex_batch_runner.lock.pid_exists", return_value=False):
                outcome = run_next(config)

            recovered = load_task(config, "stale-running")
            processed = load_task(config, "next-task")

            self.assertEqual("completed", outcome.status)
            self.assertEqual("completed", recovered["status"])
            self.assertEqual("runnable", processed["status"])
            self.assertFalse(config.lock_file.exists())


if __name__ == "__main__":
    unittest.main()

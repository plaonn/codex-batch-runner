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
from codex_batch_runner.prompts import build_prompt
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
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict("os.environ", {"CBR_CONFIG": ""}, clear=False)
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

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

    def test_run_next_runs_codex_cli_maintenance_after_last_task_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                make_config(tmp, "success"),
                codex_cli_update_command=["codex", "update"],
                codex_cli_smoke_command=["cbr", "doctor"],
                codex_cli_maintenance_on_empty=True,
            )
            create_task(config, "work", tmp, task_id="task-1")

            with patch.object(
                runner_module,
                "run_codex_cli_maintenance",
                return_value={"status": "succeeded", "applied": True},
            ) as maintenance:
                outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("task-1", outcome.task_id)
            self.assertEqual({"status": "succeeded", "applied": True}, outcome.maintenance)
            maintenance.assert_called_once_with(config)

    def test_run_next_does_not_run_codex_cli_maintenance_on_empty_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                make_config(tmp, "success"),
                codex_cli_update_command=["codex", "update"],
                codex_cli_smoke_command=["cbr", "doctor"],
                codex_cli_maintenance_on_empty=True,
            )

            with patch.object(runner_module, "run_codex_cli_maintenance") as maintenance:
                outcome = run_next(config)

            self.assertEqual("empty", outcome.status)
            self.assertIsNone(outcome.maintenance)
            maintenance.assert_not_called()

    def test_run_next_defers_codex_cli_maintenance_when_follow_up_work_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                make_config(tmp, "success"),
                codex_cli_update_command=["codex", "update"],
                codex_cli_smoke_command=["cbr", "doctor"],
                codex_cli_maintenance_on_empty=True,
            )
            create_task(config, "first", tmp, task_id="task-1")
            create_task(config, "second", tmp, task_id="task-2")

            with patch.object(runner_module, "run_codex_cli_maintenance") as maintenance:
                outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("task-1", outcome.task_id)
            self.assertIsNone(outcome.maintenance)
            self.assertEqual("runnable", load_task(config, "task-2")["status"])
            maintenance.assert_not_called()

    def test_concurrent_run_next_can_claim_different_project_after_first_claim_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_1 = Path(tmp) / "project-1"
            project_2 = Path(tmp) / "project-2"
            project_1.mkdir()
            project_2.mkdir()
            config = replace(
                make_config(tmp, "success"),
                max_total_running=2,
                max_running_per_project=1,
                capacity_pools={"codex": {"max_running": 2}},
            )
            create_task(config, "first", str(project_1), task_id="task-1")
            create_task(config, "second", str(project_2), task_id="task-2")
            nested_outcomes = []

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                self.assertFalse(config.lock_file.exists())
                if task["id"] == "task-1":
                    nested_outcomes.append(run_next(config))
                return CodexResult(
                    returncode=0,
                    log_path=Path(tmp) / f"{task['id']}.jsonl",
                    command_kind="exec",
                    resume_id_used=None,
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": task["id"],
                        "status": "completed",
                        "summary": "done",
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
            self.assertEqual("task-1", outcome.task_id)
            self.assertEqual(1, len(nested_outcomes))
            self.assertEqual("completed", nested_outcomes[0].status)
            self.assertEqual("task-2", nested_outcomes[0].task_id)
            self.assertEqual("completed", load_task(config, "task-1")["status"])
            self.assertEqual("completed", load_task(config, "task-2")["status"])

    def test_concurrent_run_next_respects_per_project_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                make_config(tmp, "success"),
                max_total_running=2,
                max_running_per_project=1,
                capacity_pools={"codex": {"max_running": 2}},
            )
            create_task(config, "first", tmp, task_id="task-1")
            create_task(config, "second", tmp, task_id="task-2")
            nested_outcomes = []

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                self.assertFalse(config.lock_file.exists())
                if task["id"] == "task-1":
                    nested_outcomes.append(run_next(config))
                return CodexResult(
                    returncode=0,
                    log_path=Path(tmp) / f"{task['id']}.jsonl",
                    command_kind="exec",
                    resume_id_used=None,
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": task["id"],
                        "status": "completed",
                        "summary": "done",
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
            self.assertEqual("task-1", outcome.task_id)
            self.assertEqual(1, len(nested_outcomes))
            self.assertEqual("empty", nested_outcomes[0].status)
            self.assertEqual("runnable", load_task(config, "task-2")["status"])

    def test_finalize_guard_skips_result_when_active_run_id_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(config, "work", tmp, task_id="task-1")

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                claimed = load_task(config, task["id"])
                claimed["active_run_id"] = "newer-run"
                save_task(config, claimed)
                return CodexResult(
                    returncode=0,
                    log_path=Path(tmp) / "task-1.jsonl",
                    command_kind="exec",
                    resume_id_used=None,
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": task["id"],
                        "status": "completed",
                        "summary": "done",
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

            task = load_task(config, "task-1")
            self.assertEqual("stale_finalization", outcome.status)
            self.assertEqual("running", task["status"])
            self.assertEqual("newer-run", task["active_run_id"])
            self.assertNotEqual("completed", task.get("last_result", {}).get("status"))

    def test_invalid_task_model_requirement_fails_before_codex_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            task = create_task(config, "work", tmp, task_id="bad-requirement")
            task["model_requirement_vector"] = {"dimensions": {"reasoning_depth": "extreme"}}
            save_task(config, task)

            with patch("codex_batch_runner.codex.subprocess.Popen", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            loaded = load_task(config, "bad-requirement")
            self.assertEqual("failed", outcome.status)
            self.assertEqual("failed", loaded["status"])
            self.assertEqual(0, loaded["attempts"])
            self.assertIn("invalid execution config", loaded["last_error"])

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

    def test_run_next_runs_codex_cli_maintenance_after_last_auto_review_accept_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            config = replace(
                make_config(tmp, "success"),
                auto_review_mechanical_accept=True,
                codex_cli_update_command=["codex", "update"],
                codex_cli_smoke_command=["cbr", "doctor"],
                codex_cli_maintenance_on_empty=True,
            )
            create_clean_completed_task(config, repo, "reviewable")

            with (
                patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")),
                patch.object(
                    runner_module,
                    "run_codex_cli_maintenance",
                    return_value={"status": "succeeded", "applied": True},
                ) as maintenance,
            ):
                outcome = run_next(config)

            self.assertEqual("review_accepted", outcome.status)
            self.assertEqual("reviewable", outcome.task_id)
            self.assertEqual("accepted", load_task(config, "reviewable")["review_status"])
            self.assertEqual({"status": "succeeded", "applied": True}, outcome.maintenance)
            maintenance.assert_called_once_with(config)

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

    def test_run_next_auto_review_does_not_trigger_when_runner_pause_becomes_active(self) -> None:
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

            with patch("codex_batch_runner.runner.is_runner_paused", side_effect=[False, True]):
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

    def test_run_next_triggers_after_implementation_completion_when_auto_review_is_immediately_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            origin = Path(tmp) / "origin.git"
            git(origin.parent, "init", "--bare", str(origin))
            git(repo, "remote", "add", "origin", str(origin))
            git(repo, "push", "-u", "origin", "main")
            config = replace(make_config(tmp, "success"), auto_review_mechanical_accept=True)
            create_task(config, "implementation", str(repo), task_id="implementation")

            with patch.object(runner_module, "run_post_run_trigger") as trigger_mock:
                outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("implementation", outcome.task_id)
            self.assertEqual("completed", load_task(config, "implementation")["status"])
            trigger_mock.assert_called_once_with(config)
            self.assertFalse(config.lock_file.exists())

    def test_run_next_does_not_trigger_after_implementation_completion_for_non_actionable_auto_review_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(make_config(tmp, "success"), auto_review_mechanical_accept=True)
            create_task(config, "implementation", tmp, task_id="implementation")

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                return CodexResult(
                    returncode=0,
                    log_path=Path(tmp) / "attempt.jsonl",
                    command_kind="exec",
                    resume_id_used=None,
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": "implementation",
                        "status": "completed",
                        "summary": "done",
                        "next_prompt": "",
                        "changed_files": ["README.md"],
                        "verification": [],
                    },
                    session_id=None,
                    thread_id=None,
                    rate_limited=False,
                    rate_limit_markers=[],
                )

            with (
                patch.object(runner_module, "run_codex", fake_run_codex),
                patch.object(runner_module, "run_post_run_trigger") as trigger_mock,
            ):
                outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("implementation", outcome.task_id)
            trigger_mock.assert_not_called()

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

    def test_run_next_returns_paused_after_stale_recovery_without_invoking_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            stale = create_task(config, "stale", tmp, task_id="stale-task")
            stale["status"] = "running"
            stale["started_at"] = None
            save_task(config, stale)
            create_task(config, "ready", tmp, task_id="ready-task")
            config.state_file.write_text(
                json.dumps(
                    {
                        "runner_pause": {
                            "active": True,
                            "reason": "operator drain window",
                            "paused_at": "2026-06-22T00:00:00+00:00",
                            "paused_by": "ops",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)

            self.assertEqual("paused", outcome.status)
            self.assertIn("operator drain window", outcome.message)
            recovered = load_task(config, "stale-task")
            self.assertEqual("runnable", recovered["status"])
            self.assertEqual("recovered stale running task", recovered["last_error"])
            self.assertEqual("runnable", load_task(config, "ready-task")["status"])
            self.assertIsNone(load_state(config)["last_task_id"])

    def test_run_next_rechecks_runner_pause_before_claiming_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(config, "ready", tmp, task_id="ready-task")
            config.state_file.write_text(
                json.dumps(
                    {
                        "runner_pause": {
                            "active": True,
                            "reason": "operator drain window",
                            "paused_at": "2026-06-22T00:00:00+00:00",
                            "paused_by": "ops",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("codex_batch_runner.runner.is_runner_paused", side_effect=[False, True]),
                patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")),
            ):
                outcome = run_next(config)

            self.assertEqual("paused", outcome.status)
            self.assertIn("operator drain window", outcome.message)
            self.assertEqual("runnable", load_task(config, "ready-task")["status"])
            self.assertIsNone(load_state(config)["last_task_id"])

    def test_run_next_does_not_trigger_when_runner_pause_is_active(self) -> None:
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
            create_task(config, "second", tmp, task_id="task-2")

            with patch("codex_batch_runner.runner.is_runner_paused", side_effect=[False, False, True]):
                outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual("task-1", outcome.task_id)
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

    def test_run_next_shell_task_success_captures_log_and_completes_without_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(
                config,
                "shell gate",
                tmp,
                task_id="shell-ok",
                execution_backend="shell",
                shell_command=[sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            )

            with patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)
            task = load_task(config, "shell-ok")
            log_text = Path(task["log_paths"][0]).read_text(encoding="utf-8")

            self.assertEqual("completed", outcome.status)
            self.assertEqual("completed", task["status"])
            self.assertEqual("unreviewed", task["review_status"])
            self.assertEqual("shell", task["execution_backend"])
            self.assertEqual("shell", task["last_run"]["command_kind"])
            self.assertEqual("shell", task["last_run"]["execution_backend"])
            self.assertEqual(0, task["last_run"]["returncode"])
            self.assertEqual([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"], task["last_run"]["command"])
            self.assertEqual("completed", task["last_result"]["status"])
            self.assertIn("out", log_text)
            self.assertIn("err", log_text)

    def test_run_next_shell_task_failure_blocks_dependent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(
                config,
                "shell gate",
                tmp,
                task_id="shell-fail",
                execution_backend="shell",
                shell_command=[sys.executable, "-c", "import sys; print('bad'); sys.exit(3)"],
            )
            create_task(config, "child", tmp, task_id="child", depends_on=["shell-fail"])

            outcome = run_next(config)
            failed = load_task(config, "shell-fail")
            child = load_task(config, "child")
            second = run_next(config)

            self.assertEqual("failed", outcome.status)
            self.assertEqual("failed", failed["status"])
            self.assertEqual(3, failed["last_run"]["returncode"])
            self.assertEqual("failed", failed["last_result"]["status"])
            self.assertIn("exited with 3", failed["last_error"])
            self.assertEqual("runnable", child["status"])
            self.assertEqual("empty", second.status)

    def test_run_next_shell_task_timeout_fails_without_large_output_in_task_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(make_config(tmp, "success"), shell_task_timeout_seconds=1)
            create_task(
                config,
                "shell timeout",
                tmp,
                task_id="shell-timeout",
                execution_backend="shell",
                shell_command=[sys.executable, "-c", "import time; print('before'); time.sleep(5)"],
            )

            outcome = run_next(config)
            task = load_task(config, "shell-timeout")
            task_json = json.dumps(task)

            self.assertEqual("failed", outcome.status)
            self.assertEqual("failed", task["status"])
            self.assertTrue(task["last_run"]["timed_out"])
            self.assertEqual(1, task["last_run"]["timeout_seconds"])
            self.assertIn("timed out", task["last_error"])
            self.assertNotIn("before" * 100, task_json)

    def test_run_next_external_json_command_completes_and_sets_unreviewed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(
                config,
                "external work",
                tmp,
                task_id="external-ok",
                execution_backend="external-json-command",
                external_command=[
                    sys.executable,
                    "-c",
                    "import json, sys; print(json.dumps({'task_id':'external-ok','status':'completed','summary':'ok','changed_files':['file.txt'],'verification':['checked']}))",
                ],
            )

            with patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")):
                outcome = run_next(config)
            task = load_task(config, "external-ok")
            log_text = Path(task["log_paths"][0]).read_text(encoding="utf-8")

            self.assertEqual("completed", outcome.status)
            self.assertEqual("completed", task["status"])
            self.assertEqual("unreviewed", task["review_status"])
            self.assertEqual("external-json-command", task["last_run"]["execution_backend"])
            self.assertEqual("completed", task["last_result"]["status"])
            self.assertIn("stdout:", log_text)

    def test_run_next_external_json_command_missing_command_fails_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            task = create_task(config, "external work", tmp, task_id="external-missing")
            task["execution_backend"] = "external-json-command"
            task["external_command"] = []
            save_task(config, task)

            outcome = run_next(config)
            task = load_task(config, "external-missing")

            self.assertEqual("failed", outcome.status)
            self.assertIn("external_command argv list", task["last_error"])
            self.assertFalse(task["log_paths"])

    def test_run_next_external_json_command_needs_resume_uses_resume_unavailable_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(
                config,
                "external work",
                tmp,
                task_id="external-resume",
                execution_backend="external-json-command",
                external_command=[
                    sys.executable,
                    "-c",
                    "import json; print(json.dumps({'task_id':'external-resume','status':'needs_resume','summary':'more','changed_files':[],'verification':[],'next_prompt':'continue this'}))",
                ],
            )

            first = run_next(config)
            task = load_task(config, "external-resume")
            task["external_command"] = [
                sys.executable,
                "-c",
                "import json, sys; prompt=sys.argv[-1]; expected='continue ' + 'this'; assert 'resume_unavailable: true' in prompt; assert expected in prompt; print(json.dumps({'task_id':'external-resume','status':'completed','summary':'done','changed_files':[],'verification':['resumed']}))",
            ]
            save_task(config, task)
            second = run_next(config)
            task = load_task(config, "external-resume")

            self.assertEqual("needs_resume", first.status)
            self.assertEqual("completed", second.status)
            self.assertNotIn("continue this", json.dumps(task["last_run"]))
            self.assertTrue(task["resume_unavailable"])
            self.assertEqual(1, task["resume_unavailable_attempts"])

    def test_run_next_external_json_command_invalid_json_fails_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(
                config,
                "secret prompt token=private",
                tmp,
                task_id="external-invalid",
                execution_backend="external-json-command",
                external_command=[sys.executable, "-c", "value = 'token=' + 'raw-stdout'; print('not json with ' + value)"],
            )

            outcome = run_next(config)
            task = load_task(config, "external-invalid")
            events = list_events(config, task_id="external-invalid", limit=10)
            event_text = json.dumps(events, ensure_ascii=False)

            self.assertEqual("failed", outcome.status)
            self.assertEqual("invalid final JSON response", task["last_error"])
            self.assertNotIn("token=raw-stdout", json.dumps(task))
            self.assertNotIn("token=private", event_text)
            self.assertNotIn("token=raw-stdout", event_text)

    def test_run_next_external_json_command_task_id_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(
                config,
                "external work",
                tmp,
                task_id="external-mismatch",
                execution_backend="external-json-command",
                external_command=[
                    sys.executable,
                    "-c",
                    "import json; print(json.dumps({'task_id':'other','status':'completed','summary':'ok','changed_files':[],'verification':[]}))",
                ],
            )

            outcome = run_next(config)
            task = load_task(config, "external-mismatch")

            self.assertEqual("failed", outcome.status)
            self.assertEqual("final JSON task_id mismatch", task["last_error"])

    def test_run_next_external_json_command_nonzero_without_final_json_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(
                config,
                "external work",
                tmp,
                task_id="external-nonzero",
                execution_backend="external-json-command",
                external_command=[sys.executable, "-c", "import sys; print('bad'); sys.exit(7)"],
            )

            outcome = run_next(config)
            task = load_task(config, "external-nonzero")

            self.assertEqual("failed", outcome.status)
            self.assertEqual(7, task["last_run"]["returncode"])
            self.assertIn("invalid final JSON response", task["last_error"])

    def test_run_next_external_json_command_nonzero_failed_final_json_records_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            create_task(
                config,
                "external work",
                tmp,
                task_id="external-reported-fail",
                execution_backend="external-json-command",
                external_command=[
                    sys.executable,
                    "-c",
                    "import json, sys; print(json.dumps({'task_id':'external-reported-fail','status':'failed','summary':'worker failed','changed_files':[],'verification':['reported']})); sys.exit(7)",
                ],
            )

            outcome = run_next(config)
            task = load_task(config, "external-reported-fail")

            self.assertEqual("failed", outcome.status)
            self.assertEqual("worker failed", task["last_error"])
            self.assertEqual("failed", task["last_result"]["status"])
            self.assertEqual(7, task["last_run"]["returncode"])

    def test_run_next_external_json_command_timeout_fails_and_writes_log_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(make_config(tmp, "success"), external_json_command_timeout_seconds=1)
            create_task(
                config,
                "external work",
                tmp,
                task_id="external-timeout",
                execution_backend="external-json-command",
                external_command=[sys.executable, "-c", "import time; print('before'); time.sleep(5)"],
            )

            outcome = run_next(config)
            task = load_task(config, "external-timeout")
            log_text = Path(task["log_paths"][0]).read_text(encoding="utf-8")

            self.assertEqual("failed", outcome.status)
            self.assertTrue(task["last_run"]["timed_out"])
            self.assertEqual(1, task["last_run"]["timeout_seconds"])
            self.assertIn("timed out", task["last_error"])
            self.assertIn("timeout_seconds: 1", log_text)
            self.assertIn("timed_out: true", log_text)

    def test_run_next_external_json_command_worktree_mode_uses_task_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_repo(repo)
            config = replace(make_config(tmp, "success"), worktree_mode="task", worktree_root=root / "worktrees")
            create_task(
                config,
                "external work",
                str(repo),
                task_id="external-worktree",
                execution_backend="external-json-command",
                external_command=[
                    sys.executable,
                    "-c",
                    "import json, pathlib; pathlib.Path('external.txt').write_text('worktree\\n'); print(json.dumps({'task_id':'external-worktree','status':'completed','summary':'ok','changed_files':['external.txt'],'verification':['checked']}))",
                ],
            )

            outcome = run_next(config)
            task = load_task(config, "external-worktree")
            worktree_path = Path(task["execution_worktree_path"])
            base = task["execution_base_head"]
            rev_list = git(worktree_path, "rev-list", "--count", f"{base}..HEAD").stdout.strip()
            status = git(worktree_path, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()

            self.assertEqual("completed", outcome.status)
            self.assertEqual("retained", task["execution_worktree_status"])
            self.assertTrue((worktree_path / "external.txt").exists())
            self.assertFalse((repo / "external.txt").exists())
            self.assertEqual("1", rev_list)
            self.assertEqual("", status)
            self.assertFalse(task["git_status"]["dirty"])
            self.assertTrue(task["execution_commit"])
            self.assertIn(task["execution_commit"], task["last_result"]["commits"])
            self.assertEqual("not_pushed", task["last_result"]["push_status"]["status"])
            self.assertEqual("", git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip())

    def test_run_next_external_json_command_worktree_unsafe_changed_files_remains_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_repo(repo)
            config = replace(make_config(tmp, "success"), worktree_mode="task", worktree_root=root / "worktrees")
            create_task(
                config,
                "external work",
                str(repo),
                task_id="external-worktree-unsafe",
                execution_backend="external-json-command",
                external_command=[
                    sys.executable,
                    "-c",
                    "import json, pathlib; pathlib.Path('external.txt').write_text('worktree\\n'); print(json.dumps({'task_id':'external-worktree-unsafe','status':'completed','summary':'ok','changed_files':['../external.txt'],'verification':['checked']}))",
                ],
            )

            outcome = run_next(config)
            task = load_task(config, "external-worktree-unsafe")
            worktree_path = Path(task["execution_worktree_path"])
            status = git(worktree_path, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()

            self.assertEqual("completed", outcome.status)
            self.assertEqual("retained", task["execution_worktree_status"])
            self.assertNotIn("execution_commit", task)
            self.assertNotIn("commits", task["last_result"])
            self.assertNotIn("push_status", task["last_result"])
            self.assertIn("no safe changed_files", task["execution_commit_warning"])
            self.assertTrue(status)
            self.assertTrue(task["git_status"]["dirty"])

    def test_run_next_external_json_command_worktree_rejects_worker_created_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_repo(repo)
            config = replace(make_config(tmp, "success"), worktree_mode="task", worktree_root=root / "worktrees")
            create_task(
                config,
                "external work",
                str(repo),
                task_id="external-worktree-worker-commit",
                execution_backend="external-json-command",
                external_command=[
                    sys.executable,
                    "-c",
                    (
                        "import json, pathlib, subprocess; "
                        "pathlib.Path('external.txt').write_text('worktree\\n', encoding='utf-8'); "
                        "subprocess.run(['git','add','external.txt'], check=True); "
                        "subprocess.run(['git','commit','-m','worker commit'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True); "
                        "print(json.dumps({'task_id':'external-worktree-worker-commit','status':'completed','summary':'ok','changed_files':['external.txt'],'verification':['checked']}))"
                    ),
                ],
            )

            outcome = run_next(config)
            task = load_task(config, "external-worktree-worker-commit")
            worktree_path = Path(task["execution_worktree_path"])
            base = task["execution_base_head"]
            rev_list = git(worktree_path, "rev-list", "--count", f"{base}..HEAD").stdout.strip()
            status = git(worktree_path, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()

            self.assertEqual("failed", outcome.status)
            self.assertEqual("failed", task["status"])
            self.assertEqual("retained", task["execution_worktree_status"])
            self.assertIn("worker-created local commit", task["last_error"])
            self.assertIn("must not commit or push", task["last_error"])
            self.assertNotIn("execution_commit", task)
            self.assertEqual("failed", task["last_result"]["status"])
            self.assertEqual("1", rev_list)
            self.assertEqual("", status)

    def test_run_next_records_resolved_execution_config_in_last_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            config = replace(
                config,
                model_selection_rules=[
                    {
                        "name": "high-capability",
                        "when": {"reasoning_depth": "high"},
                        "model": "gpt-5",
                        "config_overrides": {"model_reasoning_effort": "high"},
                    }
                ],
            )
            create_task(
                config,
                "do it",
                tmp,
                task_id="task-critical-worktree",
                labels=["worktree-apply"],
            )

            outcome = run_next(config)
            task = load_task(config, "task-critical-worktree")

            self.assertEqual("completed", outcome.status)
            resolved = task["last_run"]["resolved_execution_config"]
            self.assertEqual("high-capability", resolved["selection_rule"])
            self.assertEqual("explicit_model", resolved["model_source"])
            self.assertEqual("gpt-5", resolved["model"])
            self.assertEqual("high", resolved["model_requirement_vector"]["dimensions"]["reasoning_depth"])
            self.assertEqual(["model_reasoning_effort"], resolved["config_override_keys"])

    def test_run_next_records_cli_default_model_source_for_no_model_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            config = replace(
                config,
                model_selection_rules=[
                    {
                        "name": "high-capability",
                        "when": {"reasoning_depth": "high"},
                        "config_overrides": {"model_reasoning_effort": "high"},
                    }
                ],
            )
            create_task(
                config,
                "do it",
                tmp,
                task_id="task-critical-worktree",
                labels=["worktree-apply"],
            )

            outcome = run_next(config)
            task = load_task(config, "task-critical-worktree")

            self.assertEqual("completed", outcome.status)
            resolved = task["last_run"]["resolved_execution_config"]
            self.assertEqual("high-capability", resolved["selection_rule"])
            self.assertEqual("cli_default", resolved["model_source"])
            self.assertIsNone(resolved["model"])

    def test_run_next_records_execution_target_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            config = replace(
                config,
                execution_targets={
                    "high_capability_current": {
                        "model": "gpt-5",
                        "config_overrides": {"model_reasoning_effort": "high"},
                    }
                },
                model_selection_rules=[
                    {
                        "name": "high-capability",
                        "when": {"reasoning_depth": "high"},
                        "execution_target": "high_capability_current",
                    }
                ],
            )
            create_task(
                config,
                "do it",
                tmp,
                task_id="task-critical-worktree",
                labels=["worktree-apply"],
            )

            outcome = run_next(config)
            task = load_task(config, "task-critical-worktree")

            self.assertEqual("completed", outcome.status)
            resolved = task["last_run"]["resolved_execution_config"]
            self.assertEqual("high-capability", resolved["selection_rule"])
            self.assertEqual("target_alias", resolved["model_source"])
            self.assertEqual("high_capability_current", resolved["execution_target"])
            self.assertEqual("gpt-5", resolved["model"])
            self.assertEqual(["model_reasoning_effort"], resolved["config_override_keys"])

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
            seen_prompts = []

            def fake_run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
                seen_cwds.append(Path(task["cwd"]))
                seen_prompts.append(prompt)
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
            self.assertEqual(1, len(seen_prompts))
            self.assertIn(f"cwd: {worktree_path}", seen_prompts[0])
            self.assertIn("execution_mode: git_worktree", seen_prompts[0])
            self.assertIn(f"execution_worktree_path: {worktree_path}", seen_prompts[0])
            self.assertIn(f"original_task_cwd: {repo}", seen_prompts[0])
            self.assertIn(
                "Use cwd/execution_worktree_path as the current process cwd for edits, tests, and commits.",
                seen_prompts[0],
            )
            self.assertNotIn(f"cwd: {repo}", seen_prompts[0].splitlines())
            self.assertEqual("git_worktree", task["execution_mode"])
            self.assertEqual("retained", task["execution_worktree_status"])
            self.assertTrue(worktree_path.is_dir())
            self.assertEqual(str(worktree_path), task["git_status"]["root"])

    def test_external_json_command_git_worktree_prompt_disallows_commit_and_push(self) -> None:
        prompt = build_prompt(
            {
                "id": "external-prompt",
                "cwd": "/repo",
                "prompt": "make a change",
                "execution_mode": "git_worktree",
                "execution_worktree_path": "/worktrees/external-prompt",
            },
            execution_cwd="/worktrees/external-prompt",
            execution_backend="external-json-command",
        )

        self.assertIn("execution_mode: git_worktree", prompt)
        self.assertIn("Use cwd/execution_worktree_path as the current process cwd for edits and tests.", prompt)
        self.assertIn("Do not create local commits or push", prompt)
        self.assertIn("report safe relative changed_files so cbr can create the review commit", prompt)
        self.assertNotIn("edits, tests, and commits", prompt)

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

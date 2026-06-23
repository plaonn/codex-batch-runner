from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.events import list_events
from codex_batch_runner.queue import create_task, load_task, save_task, task_path
from codex_batch_runner.worktree import sanitize_branch_name


def run_cli(args: list[str]) -> tuple[int, dict]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, json.loads(stdout.getvalue())


def run_cli_text(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, stdout.getvalue()


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test User")
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "file.txt")
    git(path, "commit", "-m", "initial")


def write_config(root: Path, *, worktree_mode: str = "task", worktree_root: Path | None = None) -> Path:
    data = {
        "queue_dir": str(root / "tasks"),
        "log_dir": str(root / "logs"),
        "event_dir": str(root / "events"),
        "lock_file": str(root / "runner.lock"),
        "state_file": str(root / "state.json"),
        "worktree_mode": worktree_mode,
        "worktree_root": str(worktree_root or root / "worktrees"),
        "codex_command": [sys.executable, "-c", "raise SystemExit('codex must not run')"],
    }
    path = root / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def create_applied_worktree_task(config_path: Path, config: Config, repo: Path, task_id: str) -> dict:
    create_task(config, "work", str(repo), task_id=task_id)
    assert run_cli(["--config", str(config_path), "worktree", "prepare", task_id, "--apply", "--json"])[0] == 0
    task = load_task(config, task_id)
    worktree = Path(task["execution_worktree_path"])
    (worktree / "file.txt").write_text(f"base\n{task_id} change\n", encoding="utf-8")
    git(worktree, "commit", "-am", f"{task_id} change")
    task["status"] = "completed"
    task["review_status"] = "accepted"
    save_task(config, task)
    assert run_cli(["--config", str(config_path), "worktree", "apply", task_id, "--apply", "--json"])[0] == 0
    return load_task(config, task_id)


def create_cleaned_applied_worktree_task(config_path: Path, config: Config, repo: Path, task_id: str) -> dict:
    task = create_applied_worktree_task(config_path, config, repo, task_id)
    assert run_cli(["--config", str(config_path), "worktree", "cleanup", task_id, "--apply", "--json"])[0] == 0
    return load_task(config, task_id)


def create_completed_worktree_task(
    config_path: Path,
    config: Config,
    repo: Path,
    task_id: str,
    *,
    review_status: str,
    status: str = "completed",
) -> dict:
    create_task(config, "work", str(repo), task_id=task_id)
    assert run_cli(["--config", str(config_path), "worktree", "prepare", task_id, "--apply", "--json"])[0] == 0
    task = load_task(config, task_id)
    worktree = Path(task["execution_worktree_path"])
    (worktree / "file.txt").write_text(f"base\n{task_id} discarded change\n", encoding="utf-8")
    git(worktree, "commit", "-am", f"{task_id} discarded change")
    task["status"] = status
    task["review_status"] = review_status
    save_task(config, task)
    return load_task(config, task_id)


class WorktreeTests(unittest.TestCase):
    def test_branch_name_sanitizes_task_id_for_git_ref(self) -> None:
        self.assertEqual("cbr/task-a-b-c", sanitize_branch_name(" task/a b~c "))
        self.assertEqual("cbr/task", sanitize_branch_name("@{"))

    def test_prepare_dry_run_does_not_create_branch_or_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="task-dry-run")

            with patch("codex_batch_runner.runner.run_codex", side_effect=AssertionError("unexpected Codex call")):
                code, report = run_cli(["--config", str(config_path), "worktree", "prepare", "task-dry-run", "--dry-run", "--json"])

            self.assertEqual(0, code)
            self.assertFalse(report["applied"])
            self.assertEqual("absent", report["classification"]["status"])
            self.assertNotIn("execution_branch", load_task(config, "task-dry-run"))
            branches = git(repo, "branch", "--list", "cbr/task-dry-run")
            self.assertEqual("", branches)

    def test_prepare_apply_creates_worktree_and_sanitized_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="task/apply 1")

            code, report = run_cli(["--config", str(config_path), "worktree", "prepare", "task/apply 1", "--apply", "--json"])

            self.assertEqual(0, code)
            self.assertTrue(report["applied"])
            self.assertEqual("cbr/task-apply-1", report["branch"])
            task = load_task(config, "task/apply 1")
            self.assertEqual("git_worktree", task["execution_mode"])
            self.assertEqual("prepared", task["execution_worktree_status"])
            self.assertTrue(Path(task["execution_worktree_path"]).is_dir())
            self.assertIn("cbr/task-apply-1", git(repo, "branch", "--list", "cbr/task-apply-1"))
            events = list_events(config, task_id="task/apply 1", limit=0)
            self.assertTrue(any(event["event_type"] == "task_worktree_prepared" for event in events))
            self.assertNotIn("prompt", json.dumps(events))

    def test_prepare_requires_task_worktree_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root, worktree_mode="disabled")
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="disabled")

            code, report = run_cli(["--config", str(config_path), "worktree", "prepare", "disabled", "--apply", "--json"])

            self.assertEqual(1, code)
            self.assertIn("worktree_mode is disabled", report["errors"][0])
            self.assertNotIn("execution_branch", load_task(config, "disabled"))

    def test_prepare_blocks_path_outside_configured_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_task(config, "work", str(repo), task_id="escape")
            task["execution_worktree_path"] = str(root / "outside")
            task["execution_branch"] = "cbr/escape"
            task["execution_base_head"] = git(repo, "rev-parse", "HEAD")
            task["execution_mode"] = "git_worktree"
            task["execution_worktree_status"] = "retained"
            task["execution_repo_root"] = str(repo)
            task["execution_apply_status"] = "applied"
            task["execution_applied_at"] = "2026-01-01T00:00:00+00:00"
            task["execution_applied_head"] = git(repo, "rev-parse", "HEAD")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)

            code, report = run_cli(["--config", str(config_path), "worktree", "cleanup", "escape", "--dry-run", "--json"])

            self.assertEqual(1, code)
            self.assertIn("worktree path must be inside configured worktree_root", report["errors"][0])

    def test_prepare_classifies_existing_unlinked_branch_as_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            git(repo, "branch", "cbr/existing")
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="existing")

            code, report = run_cli(["--config", str(config_path), "worktree", "prepare", "existing", "--dry-run", "--json"])

            self.assertEqual(1, code)
            self.assertEqual("existing_branch", report["classification"]["status"])
            self.assertIn("existing branch", report["errors"][0])

    def test_cleanup_refuses_accepted_but_not_applied_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="cleanup")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "cleanup", "--apply", "--json"])[0])

            code, report = run_cli(["--config", str(config_path), "worktree", "cleanup", "cleanup", "--apply", "--json"])
            self.assertEqual(1, code)
            self.assertIn("only allowed", report["errors"][0])

            task = load_task(config, "cleanup")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)
            worktree_path = Path(task["execution_worktree_path"])

            code, report = run_cli(["--config", str(config_path), "worktree", "cleanup", "cleanup", "--apply", "--json"])

            self.assertEqual(1, code)
            self.assertFalse(report["applied"])
            self.assertIn("execution_apply_status=applied", report["errors"][0])
            self.assertTrue(worktree_path.exists())
            self.assertNotIn("execution_cleaned_at", load_task(config, "cleanup"))
            list_code, list_output = run_cli_text(["--config", str(config_path), "list", "--color=never"])
            self.assertEqual(0, list_code)
            self.assertIn("accepted_unapplied", list_output)
            self.assertIn("not applied", list_output)

    def test_cleanup_refuses_needs_followup_without_terminal_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_completed_worktree_task(
                config_path,
                config,
                repo,
                "cleanup-followup",
                review_status="needs_followup",
            )
            worktree_path = Path(task["execution_worktree_path"])

            code, report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-followup", "--dry-run", "--json"]
            )

            self.assertEqual(1, code)
            self.assertIn("terminal discard resolution", report["errors"][0])
            self.assertNotIn("review_status=rejected", report["errors"][0])
            self.assertTrue(worktree_path.exists())
            self.assertNotIn("execution_cleaned_at", load_task(config, "cleanup-followup"))

    def test_cleanup_refuses_archived_needs_followup_without_terminal_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_completed_worktree_task(
                config_path,
                config,
                repo,
                "cleanup-archived-followup",
                review_status="needs_followup",
                status="archived",
            )
            worktree_path = Path(task["execution_worktree_path"])

            code, report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-archived-followup", "--dry-run", "--json"]
            )

            self.assertEqual(1, code)
            self.assertIn("terminal discard resolution", report["errors"][0])
            self.assertNotIn("review_status=rejected", report["errors"][0])
            self.assertTrue(worktree_path.exists())
            self.assertNotIn("execution_cleaned_at", load_task(config, "cleanup-archived-followup"))

    def test_cleanup_dry_run_reports_candidate_after_applied_worktree_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_applied_worktree_task(config_path, config, repo, "cleanup-dry")
            worktree_path = Path(task["execution_worktree_path"])

            code, report = run_cli(["--config", str(config_path), "worktree", "cleanup", "cleanup-dry", "--dry-run", "--json"])
            summary_code, summary = run_cli_text(["--config", str(config_path), "summary", "cleanup-dry"])
            bundle_code, bundle_output = run_cli_text(["--config", str(config_path), "review-bundle", "cleanup-dry", "--json"])
            bundle = json.loads(bundle_output)
            list_code, list_output = run_cli_text(["--config", str(config_path), "list", "--all", "--color=never"])

            self.assertEqual(0, code)
            self.assertFalse(report["applied"])
            self.assertEqual("applied", report["apply_status"])
            self.assertEqual("applied", report["cleanup_kind"])
            self.assertEqual("execution_apply_status=applied", report["cleanup_reason"])
            self.assertEqual("cleanup_candidate", report["classification"]["status"])
            self.assertTrue(worktree_path.exists())
            self.assertEqual(task["execution_worktree_status"], load_task(config, "cleanup-dry")["execution_worktree_status"])
            self.assertNotIn("execution_cleaned_at", load_task(config, "cleanup-dry"))
            self.assertEqual(0, summary_code)
            self.assertIn("apply_status: applied", summary)
            self.assertEqual(0, bundle_code)
            self.assertEqual("applied", bundle["task_worktree"]["metadata"]["apply_status"])
            self.assertEqual(0, list_code)
            self.assertIn("applied to main", list_output)

    def test_cleanup_after_applied_task_preserves_branch_task_logs_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_applied_worktree_task(config_path, config, repo, "cleanup")
            worktree_path = Path(task["execution_worktree_path"])
            config.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = config.log_dir / "cleanup.log"
            log_path.write_text("sanitized log\n", encoding="utf-8")
            task["log_paths"] = [str(log_path)]
            save_task(config, task)
            event_files_before = sorted(config.event_dir.rglob("*.jsonl"))
            self.assertTrue(event_files_before)

            code, report = run_cli(["--config", str(config_path), "worktree", "cleanup", "cleanup", "--apply", "--json"])

            self.assertEqual(0, code)
            self.assertTrue(report["applied"])
            self.assertEqual("applied", report["apply_status"])
            self.assertEqual("applied", report["cleanup_kind"])
            self.assertFalse(worktree_path.exists())
            loaded = load_task(config, "cleanup")
            self.assertEqual("cleaned", loaded["execution_worktree_status"])
            self.assertEqual("applied", loaded["execution_apply_status"])
            self.assertEqual("applied", loaded["execution_cleanup_kind"])
            self.assertEqual("execution_apply_status=applied", loaded["execution_cleanup_reason"])
            self.assertTrue(loaded["execution_cleanup_branch_retained"])
            self.assertTrue(loaded["execution_cleanup_result_applied"])
            self.assertTrue(task_path(config, "cleanup").exists())
            self.assertTrue(log_path.exists())
            self.assertTrue(all(path.exists() for path in event_files_before))
            self.assertIn("cbr/cleanup", git(repo, "branch", "--list", "cbr/cleanup"))
            events = list_events(config, task_id="cleanup", limit=0)
            self.assertTrue(any(event["event_type"] == "task_worktree_cleaned" for event in events))
            self.assertNotIn("prompt", json.dumps(events))

    def test_branch_prune_dry_run_reports_applied_cleaned_branch_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_cleaned_applied_worktree_task(config_path, config, repo, "branch-prune-dry")

            code, report = run_cli(
                ["--config", str(config_path), "worktree", "branch-prune", "branch-prune-dry", "--dry-run", "--json"]
            )
            text_code, text_output = run_cli_text(
                ["--config", str(config_path), "worktree", "branch-prune", "branch-prune-dry", "--dry-run"]
            )

            self.assertEqual(0, code)
            self.assertFalse(report["applied"])
            self.assertTrue(report["gates_ok"])
            self.assertEqual("eligible", report["classification"]["status"])
            self.assertEqual(task["execution_applied_head"], report["expected_head"])
            self.assertEqual(task["execution_applied_head"], report["branch_head"])
            self.assertIn("cbr/branch-prune-dry", git(repo, "branch", "--list", "cbr/branch-prune-dry"))
            self.assertEqual(0, text_code)
            self.assertIn("classification: eligible", text_output)

    def test_branch_prune_apply_deletes_local_branch_only_and_records_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_cleaned_applied_worktree_task(config_path, config, repo, "branch-prune-apply")
            worktree_path = Path(task["execution_worktree_path"])

            code, report = run_cli(
                ["--config", str(config_path), "worktree", "branch-prune", "branch-prune-apply", "--apply", "--json"]
            )
            loaded = load_task(config, "branch-prune-apply")
            events = list_events(config, task_id="branch-prune-apply", limit=0)

            self.assertEqual(0, code)
            self.assertTrue(report["applied"])
            self.assertEqual("pruned", report["classification"]["status"])
            self.assertEqual("", git(repo, "branch", "--list", "cbr/branch-prune-apply"))
            self.assertFalse(worktree_path.exists())
            self.assertEqual("cleaned", loaded["execution_worktree_status"])
            self.assertEqual("pruned", loaded["execution_branch_prune_status"])
            self.assertEqual(task["execution_applied_head"], loaded["execution_branch_pruned_head"])
            self.assertFalse(loaded["execution_cleanup_branch_retained"])
            self.assertTrue(task_path(config, "branch-prune-apply").exists())
            prune_event = next(event for event in events if event["event_type"] == "task_worktree_branch_pruned")
            self.assertEqual("pruned", prune_event["payload"]["execution_branch_prune_status"])
            self.assertNotIn("prompt", json.dumps(events))

    def test_branch_prune_rejects_protected_and_non_cbr_branch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_task(config, "work", str(repo), task_id="branch-prune-protected")
            head = git(repo, "rev-parse", "HEAD")
            task.update(
                {
                    "status": "completed",
                    "review_status": "accepted",
                    "execution_mode": "git_worktree",
                    "execution_branch": "main",
                    "execution_repo_root": str(repo),
                    "execution_worktree_status": "cleaned",
                    "execution_apply_status": "applied",
                    "execution_applied_head": head,
                    "execution_cleanup_kind": "applied",
                    "execution_cleanup_result_applied": True,
                }
            )
            save_task(config, task)

            protected_code, protected_report = run_cli(
                ["--config", str(config_path), "worktree", "branch-prune", "branch-prune-protected", "--dry-run", "--json"]
            )
            task["execution_branch"] = "feature/task"
            save_task(config, task)
            non_cbr_code, non_cbr_report = run_cli(
                ["--config", str(config_path), "worktree", "branch-prune", "branch-prune-protected", "--dry-run", "--json"]
            )

            self.assertEqual(1, protected_code)
            self.assertTrue(any("only deletes local cbr/* task branches" in error for error in protected_report["errors"]))
            self.assertIn("main", git(repo, "branch", "--list", "main"))
            self.assertEqual(1, non_cbr_code)
            self.assertTrue(any("only deletes local cbr/* task branches" in error for error in non_cbr_report["errors"]))

    def test_branch_prune_rejects_checked_out_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_cleaned_applied_worktree_task(config_path, config, repo, "branch-prune-checked-out")
            checkout_path = root / "checked-out"
            git(repo, "worktree", "add", str(checkout_path), task["execution_branch"])

            code, report = run_cli(
                ["--config", str(config_path), "worktree", "branch-prune", "branch-prune-checked-out", "--dry-run", "--json"]
            )

            self.assertEqual(1, code)
            self.assertIn("branch prune refuses a branch checked out", report["errors"][0])
            self.assertIn("cbr/branch-prune-checked-out", git(repo, "branch", "--list", "cbr/branch-prune-checked-out"))

    def test_branch_prune_missing_branch_is_noop_report_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_cleaned_applied_worktree_task(config_path, config, repo, "branch-prune-missing")
            git(repo, "branch", "-d", "cbr/branch-prune-missing")

            code, report = run_cli(
                ["--config", str(config_path), "worktree", "branch-prune", "branch-prune-missing", "--apply", "--json"]
            )

            self.assertEqual(0, code)
            self.assertFalse(report["applied"])
            self.assertFalse(report["branch_exists"])
            self.assertEqual("missing", report["classification"]["status"])
            self.assertEqual("cleaned", load_task(config, "branch-prune-missing")["execution_worktree_status"])

    def test_branch_prune_rejects_unreviewed_and_discard_cleanup_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            unreviewed = create_completed_worktree_task(
                config_path,
                config,
                repo,
                "branch-prune-unreviewed",
                review_status="unreviewed",
            )
            unreviewed["execution_worktree_status"] = "cleaned"
            unreviewed["execution_cleanup_kind"] = "applied"
            unreviewed["execution_cleanup_result_applied"] = True
            unreviewed["execution_apply_status"] = "applied"
            unreviewed["execution_applied_head"] = git(repo, "rev-parse", unreviewed["execution_branch"])
            save_task(config, unreviewed)
            followup = create_completed_worktree_task(
                config_path,
                config,
                repo,
                "branch-prune-followup",
                review_status="needs_followup",
            )
            followup["resolution"] = "superseded"
            save_task(config, followup)
            self.assertEqual(
                0,
                run_cli(["--config", str(config_path), "worktree", "cleanup", "branch-prune-followup", "--apply", "--json"])[0],
            )

            unreviewed_code, unreviewed_report = run_cli(
                ["--config", str(config_path), "worktree", "branch-prune", "branch-prune-unreviewed", "--dry-run", "--json"]
            )
            followup_code, followup_report = run_cli(
                ["--config", str(config_path), "worktree", "branch-prune", "branch-prune-followup", "--dry-run", "--json"]
            )

            self.assertEqual(1, unreviewed_code)
            self.assertTrue(any("completed+accepted" in error for error in unreviewed_report["errors"]))
            self.assertEqual(1, followup_code)
            self.assertIn("discard-cleaned branches are retained", followup_report["errors"][0])

    def test_cleanup_refuses_missing_stale_and_recovery_required_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))

            missing = create_task(config, "work", str(repo), task_id="cleanup-missing")
            missing["status"] = "completed"
            missing["review_status"] = "accepted"
            missing["execution_mode"] = "git_worktree"
            missing["execution_apply_status"] = "applied"
            missing["execution_applied_at"] = "2026-01-01T00:00:00+00:00"
            missing["execution_applied_head"] = git(repo, "rev-parse", "HEAD")
            save_task(config, missing)

            stale = create_applied_worktree_task(config_path, config, repo, "cleanup-stale")
            stale_path = Path(stale["execution_worktree_path"])
            git(repo, "worktree", "remove", str(stale_path))

            recovery = create_applied_worktree_task(config_path, config, repo, "cleanup-recovery")
            recovery["execution_worktree_status"] = "recovery_required"
            save_task(config, recovery)

            missing_code, missing_report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-missing", "--dry-run", "--json"]
            )
            stale_code, stale_report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-stale", "--dry-run", "--json"]
            )
            recovery_code, recovery_report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-recovery", "--dry-run", "--json"]
            )

            self.assertEqual(1, missing_code)
            self.assertIn("missing:", missing_report["errors"][0])
            self.assertEqual(1, stale_code)
            self.assertEqual("missing", stale_report["classification"]["status"])
            self.assertIn("already absent", stale_report["errors"][0])
            self.assertEqual(1, recovery_code)
            self.assertEqual("recovery_required", recovery_report["classification"]["status"])
            self.assertIn("marked recovery_required", recovery_report["errors"][0])

    def test_cleanup_allows_superseded_retained_worktree_and_records_discard_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_completed_worktree_task(
                config_path,
                config,
                repo,
                "cleanup-superseded",
                review_status="needs_followup",
            )
            task["resolution"] = "superseded"
            task["resolution_reason"] = "fixed by follow-up"
            save_task(config, task)
            worktree_path = Path(task["execution_worktree_path"])

            dry_code, dry_report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-superseded", "--dry-run", "--json"]
            )
            text_code, text_output = run_cli_text(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-superseded", "--dry-run"]
            )

            self.assertEqual(0, dry_code)
            self.assertFalse(dry_report["applied"])
            self.assertEqual("-", dry_report["apply_status"])
            self.assertEqual("discard", dry_report["cleanup_kind"])
            self.assertEqual("resolution=superseded", dry_report["cleanup_reason"])
            self.assertEqual("cleanup_candidate", dry_report["classification"]["status"])
            self.assertEqual(0, text_code)
            self.assertIn("cleanup_kind: discard", text_output)
            self.assertIn("cleanup_reason: resolution=superseded", text_output)
            self.assertTrue(worktree_path.exists())
            self.assertNotIn("execution_cleaned_at", load_task(config, "cleanup-superseded"))

            apply_code, apply_report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-superseded", "--apply", "--json"]
            )
            loaded = load_task(config, "cleanup-superseded")
            events = list_events(config, task_id="cleanup-superseded", limit=0)
            clean_event = next(event for event in events if event["event_type"] == "task_worktree_cleaned")
            summary_code, summary = run_cli_text(["--config", str(config_path), "summary", "cleanup-superseded"])
            bundle_code, bundle_output = run_cli_text(
                ["--config", str(config_path), "review-bundle", "cleanup-superseded", "--json"]
            )
            bundle = json.loads(bundle_output)
            list_code, list_output = run_cli_text(["--config", str(config_path), "list", "--all", "--color=never"])
            doctor_code, doctor_output = run_cli_text(["--config", str(config_path), "doctor"])

            self.assertEqual(0, apply_code)
            self.assertTrue(apply_report["applied"])
            self.assertEqual("discard", apply_report["cleanup_kind"])
            self.assertIn("explicit discard", apply_report["classification"]["reason"])
            self.assertFalse(worktree_path.exists())
            self.assertEqual("cleaned", loaded["execution_worktree_status"])
            self.assertEqual("discard", loaded["execution_cleanup_kind"])
            self.assertEqual("resolution=superseded", loaded["execution_cleanup_reason"])
            self.assertTrue(loaded["execution_cleanup_branch_retained"])
            self.assertFalse(loaded["execution_cleanup_result_applied"])
            self.assertNotIn("execution_apply_status", loaded)
            self.assertIn("cbr/cleanup-superseded", git(repo, "branch", "--list", "cbr/cleanup-superseded"))
            self.assertTrue(task_path(config, "cleanup-superseded").exists())
            self.assertEqual("discard", clean_event["payload"]["execution_cleanup_kind"])
            self.assertFalse(clean_event["payload"]["execution_cleanup_result_applied"])
            self.assertNotIn("prompt", json.dumps(events))
            self.assertEqual(0, summary_code)
            self.assertIn("cleanup_kind: discard", summary)
            self.assertIn("cleanup_result_applied: False", summary)
            self.assertEqual(0, bundle_code)
            self.assertFalse(bundle["task_worktree"]["recovery_required"])
            self.assertFalse(bundle["task_worktree"]["path_exists"])
            self.assertEqual("discard", bundle["task_worktree"]["metadata"]["cleanup_kind"])
            self.assertEqual(0, list_code)
            self.assertIn("cleanup-superseded", list_output)
            self.assertIn("resolved", list_output)
            self.assertEqual(0, doctor_code)
            self.assertIn("recovery_required: 0", doctor_output)

    def test_cleanup_allows_rejected_retained_worktree_as_discard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_completed_worktree_task(
                config_path,
                config,
                repo,
                "cleanup-rejected",
                review_status="rejected",
            )
            worktree_path = Path(task["execution_worktree_path"])

            code, report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-rejected", "--apply", "--json"]
            )
            loaded = load_task(config, "cleanup-rejected")

            self.assertEqual(0, code)
            self.assertTrue(report["applied"])
            self.assertEqual("discard", report["cleanup_kind"])
            self.assertEqual("review_status=rejected", report["cleanup_reason"])
            self.assertFalse(worktree_path.exists())
            self.assertEqual("cleaned", loaded["execution_worktree_status"])
            self.assertEqual("discard", loaded["execution_cleanup_kind"])
            self.assertFalse(loaded["execution_cleanup_result_applied"])
            self.assertNotIn("execution_apply_status", loaded)

    def test_cleanup_allows_archived_rejected_retained_worktree_as_discard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_completed_worktree_task(
                config_path,
                config,
                repo,
                "cleanup-archived-rejected",
                review_status="rejected",
                status="archived",
            )
            worktree_path = Path(task["execution_worktree_path"])

            dry_code, dry_report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-archived-rejected", "--dry-run", "--json"]
            )
            text_code, text_output = run_cli_text(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-archived-rejected", "--dry-run"]
            )

            self.assertEqual(0, dry_code)
            self.assertFalse(dry_report["applied"])
            self.assertEqual("discard", dry_report["cleanup_kind"])
            self.assertEqual("review_status=rejected", dry_report["cleanup_reason"])
            self.assertEqual("cleanup_candidate", dry_report["classification"]["status"])
            self.assertEqual(0, text_code)
            self.assertIn("cleanup_kind: discard", text_output)
            self.assertIn("cleanup_reason: review_status=rejected", text_output)
            self.assertTrue(worktree_path.exists())
            self.assertNotIn("execution_cleaned_at", load_task(config, "cleanup-archived-rejected"))

            apply_code, apply_report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-archived-rejected", "--apply", "--json"]
            )
            loaded = load_task(config, "cleanup-archived-rejected")
            events = list_events(config, task_id="cleanup-archived-rejected", limit=0)

            self.assertEqual(0, apply_code)
            self.assertTrue(apply_report["applied"])
            self.assertEqual("discard", apply_report["cleanup_kind"])
            self.assertEqual("review_status=rejected", apply_report["cleanup_reason"])
            self.assertIn("explicit discard", apply_report["classification"]["reason"])
            self.assertFalse(worktree_path.exists())
            self.assertEqual("cleaned", loaded["execution_worktree_status"])
            self.assertEqual("discard", loaded["execution_cleanup_kind"])
            self.assertEqual("review_status=rejected", loaded["execution_cleanup_reason"])
            self.assertTrue(loaded["execution_cleanup_branch_retained"])
            self.assertFalse(loaded["execution_cleanup_result_applied"])
            self.assertNotIn("execution_apply_status", loaded)
            self.assertIn("cbr/cleanup-archived-rejected", git(repo, "branch", "--list", "cbr/cleanup-archived-rejected"))
            self.assertTrue(task_path(config, "cleanup-archived-rejected").exists())
            self.assertTrue(any(event["event_type"] == "task_worktree_cleaned" for event in events))

    def test_cleanup_allows_archived_terminal_resolution_retained_worktree_as_discard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_completed_worktree_task(
                config_path,
                config,
                repo,
                "cleanup-archived-resolution",
                review_status="needs_followup",
                status="archived",
            )
            task["resolution"] = "manual"
            task["resolution_reason"] = "handled outside cbr"
            save_task(config, task)
            worktree_path = Path(task["execution_worktree_path"])

            dry_code, dry_report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-archived-resolution", "--dry-run", "--json"]
            )
            apply_code, apply_report = run_cli(
                ["--config", str(config_path), "worktree", "cleanup", "cleanup-archived-resolution", "--apply", "--json"]
            )
            loaded = load_task(config, "cleanup-archived-resolution")

            self.assertEqual(0, dry_code)
            self.assertEqual("discard", dry_report["cleanup_kind"])
            self.assertEqual("resolution=manual", dry_report["cleanup_reason"])
            self.assertEqual("cleanup_candidate", dry_report["classification"]["status"])
            self.assertEqual(0, apply_code)
            self.assertTrue(apply_report["applied"])
            self.assertEqual("discard", apply_report["cleanup_kind"])
            self.assertEqual("resolution=manual", loaded["execution_cleanup_reason"])
            self.assertFalse(worktree_path.exists())
            self.assertEqual("cleaned", loaded["execution_worktree_status"])

    def test_summary_and_review_bundle_include_sanitized_worktree_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="reporting")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "reporting", "--apply", "--json"])[0])
            task = load_task(config, "reporting")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "reporting",
                "status": "completed",
                "summary": "done",
                "changed_files": ["file.txt"],
                "verification": ["unit tests"],
            }
            save_task(config, task)

            summary_code, summary = run_cli_text(["--config", str(config_path), "summary", "reporting"])
            bundle_code, bundle_output = run_cli_text(["--config", str(config_path), "review-bundle", "reporting", "--json"])
            bundle = json.loads(bundle_output)

            self.assertEqual(0, summary_code)
            self.assertIn("## worktree", summary)
            self.assertIn("execution_mode: git_worktree", summary)
            self.assertIn("branch: cbr/reporting", summary)
            self.assertEqual(0, bundle_code)
            self.assertEqual("git_worktree", bundle["task_worktree"]["metadata"]["execution_mode"])
            self.assertEqual("cbr/reporting", bundle["task_worktree"]["metadata"]["branch"])
            self.assertTrue(bundle["task_worktree"]["path_exists"])
            self.assertNotIn("execution_worktree_path", bundle["task"])

    def test_review_bundle_distinguishes_main_and_task_worktree_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="split-state")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "split-state", "--apply", "--json"])[0])
            task = load_task(config, "split-state")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "split-state",
                "status": "completed",
                "summary": "done",
                "changed_files": ["file.txt"],
                "verification": ["unit tests"],
            }
            task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
            save_task(config, task)
            (repo / "file.txt").write_text("main dirty\n", encoding="utf-8")

            bundle_code, bundle_output = run_cli_text(["--config", str(config_path), "review-bundle", "split-state", "--json"])
            bundle = json.loads(bundle_output)
            review_code, review_output = run_cli_text(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            review = json.loads(review_output)

            self.assertEqual(0, bundle_code)
            self.assertEqual("task_worktree", bundle["current_git_repository"]["inspection_scope"])
            self.assertFalse(bundle["current_git_repository"]["dirty"])
            self.assertTrue(bundle["current_main_repository"]["dirty"])
            self.assertFalse(bundle["current_task_worktree_repository"]["dirty"])
            self.assertEqual(0, review_code)
            self.assertTrue(review["gates_ok"])
            self.assertFalse(review["bundle"]["current_task_worktree_repository"]["dirty"])
            self.assertTrue(review["bundle"]["current_main_repository"]["dirty"])

    def test_review_bundle_infers_task_worktree_branch_commit_from_execution_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="branch-commit")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "branch-commit", "--apply", "--json"])[0])
            task = load_task(config, "branch-commit")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\ntask change\n", encoding="utf-8")
            git(worktree, "add", "file.txt")
            git(worktree, "commit", "-m", "task change")
            task_commit = git(worktree, "rev-parse", "HEAD")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "branch-commit",
                "status": "completed",
                "summary": "done",
                "changed_files": ["file.txt"],
                "verification": ["unit tests"],
            }
            task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
            save_task(config, task)

            bundle_code, bundle_output = run_cli_text(["--config", str(config_path), "review-bundle", "branch-commit", "--json"])
            bundle = json.loads(bundle_output)
            review_code, review_output = run_cli_text(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            review = json.loads(review_output)
            gate_details = {gate["name"]: gate for gate in review["gates"]}

            self.assertEqual(0, bundle_code)
            self.assertEqual("worktree_branch", bundle["commit_information"]["source"])
            self.assertEqual("inferred", bundle["commit_information"]["status"])
            self.assertEqual([task_commit], bundle["commit_information"]["inferred_commits"])
            self.assertEqual("ancestor", bundle["commit_information"]["ancestry"]["status"])
            self.assertEqual("commit", bundle["git_diff"]["kind"])
            self.assertEqual(task_commit, bundle["git_diff"]["ref"])
            self.assertIn("file.txt", bundle["git_diff"]["stat"])
            self.assertIn("+task change", bundle["git_diff"]["diff"])
            self.assertEqual(0, review_code)
            self.assertTrue(gate_details["no_unpushed_commits"]["ok"])
            self.assertIn("worktree_branch review unit", gate_details["no_unpushed_commits"]["detail"])
            self.assertTrue(gate_details["commit_ancestry_acceptable"]["ok"])
            self.assertEqual("commit", review["bundle"]["git_diff_summary"]["kind"])

    def test_review_bundle_keeps_dirty_task_worktree_diff_when_branch_has_no_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="dirty-worktree")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "dirty-worktree", "--apply", "--json"])[0])
            task = load_task(config, "dirty-worktree")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\ndirty change\n", encoding="utf-8")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "dirty-worktree",
                "status": "completed",
                "summary": "done",
                "changed_files": ["file.txt"],
                "verification": ["unit tests"],
            }
            task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": True}
            save_task(config, task)

            bundle_code, bundle_output = run_cli_text(["--config", str(config_path), "review-bundle", "dirty-worktree", "--json"])
            bundle = json.loads(bundle_output)

            self.assertEqual(0, bundle_code)
            self.assertEqual("not_inferred", bundle["commit_information"]["status"])
            self.assertEqual("working_tree", bundle["git_diff"]["kind"])
            self.assertTrue(bundle["git_diff"]["dirty"])
            self.assertIn("task worktree is dirty", " ".join(bundle["git_diff"]["warnings"]))
            self.assertIn("+dirty change", bundle["git_diff"]["diff"])

    def test_accept_and_reject_follow_up_report_worktree_linkage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            for task_id in ("accept-link", "follow-link"):
                create_task(config, "work", str(repo), task_id=task_id)
                self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", task_id, "--apply", "--json"])[0])
                task = load_task(config, task_id)
                task["status"] = "completed"
                task["review_status"] = "unreviewed"
                save_task(config, task)

            accept_code, accept_output = run_cli_text(["--config", str(config_path), "accept", "accept-link", "--reason", "verified"])
            reject_code, reject_output = run_cli_text(
                ["--config", str(config_path), "reject", "follow-link", "--follow-up", "--reason", "needs fix"]
            )
            follow = load_task(config, "follow-link")
            bundle_code, bundle_output = run_cli_text(["--config", str(config_path), "review-bundle", "follow-link", "--json"])
            bundle = json.loads(bundle_output)

            self.assertEqual(0, accept_code)
            self.assertIn("work (accept-link)\taccepted", accept_output)
            self.assertNotIn("\naccept-link\taccepted", "\n" + accept_output)
            self.assertIn("worktree\tmode=git_worktree branch=cbr/accept-link status=prepared", accept_output)
            self.assertEqual(0, reject_code)
            self.assertIn("follow-link\tneeds_followup", reject_output)
            self.assertIn("worktree\tmode=git_worktree branch=cbr/follow-link status=prepared", reject_output)
            self.assertIn("follow_up\tsource_task=follow-link source_branch=cbr/follow-link task_generation=not_created", reject_output)
            self.assertEqual("needs_fix", follow["chain_status"])
            self.assertEqual("follow-link", follow["root_task_id"])
            self.assertEqual("cbr/follow-link", follow["review_follow_up"]["source_branch"])
            self.assertEqual("not_created", follow["review_follow_up"]["task_generation"])
            self.assertEqual(0, bundle_code)
            self.assertEqual("cbr/follow-link", bundle["review_follow_up"]["source_branch"])

    def test_review_next_blocks_auto_review_for_recovery_required_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            task = create_task(config, "work", str(repo), task_id="stale-worktree")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "stale-worktree",
                "status": "completed",
                "summary": "done",
                "changed_files": ["file.txt"],
                "verification": ["unit tests"],
            }
            task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
            task["execution_mode"] = "git_worktree"
            task["execution_branch"] = "cbr/stale-worktree"
            task["execution_base_ref"] = "HEAD"
            task["execution_base_head"] = git(repo, "rev-parse", "HEAD")
            task["execution_worktree_status"] = "retained"
            task["execution_worktree_path"] = str(root / "worktrees" / "missing")
            task["execution_worktree_root"] = str(root / "worktrees")
            task["execution_repo_root"] = str(repo)
            save_task(config, task)

            code, output = run_cli_text(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            report = json.loads(output)
            gate_status = {gate["name"]: gate for gate in report["gates"]}
            apply_code, apply_report = run_cli(
                ["--config", str(config_path), "review-next", "--apply", "--mechanical-auto-accept", "--json"]
            )

            self.assertEqual(0, code)
            self.assertTrue(report["selected"])
            self.assertIn("worktree_report", report)
            self.assertTrue(report["worktree_report"]["recovery_required"])
            self.assertIn("worktree_path", report["worktree_report"]["stale_metadata"])
            self.assertFalse(report["gates_ok"])
            self.assertFalse(gate_status["worktree_metadata_recoverable"]["ok"])
            self.assertIn("worktree_path", gate_status["worktree_metadata_recoverable"]["detail"])
            self.assertIn("recovery_required=true", gate_status["worktree_metadata_recoverable"]["detail"])
            self.assertEqual(0, apply_code)
            self.assertFalse(apply_report["mutated"])
            self.assertEqual("needs_human", apply_report["auto_review"]["decision"])
            self.assertIn("worktree_metadata_recoverable", apply_report["auto_review"]["failing_gates"])
            self.assertEqual("unreviewed", load_task(config, "stale-worktree")["review_status"])

    def test_apply_dry_run_reports_fast_forward_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="apply-dry")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "apply-dry", "--apply", "--json"])[0])
            task = load_task(config, "apply-dry")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\ndry-run change\n", encoding="utf-8")
            git(worktree, "commit", "-am", "dry-run change")
            branch_head = git(worktree, "rev-parse", "HEAD")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)
            main_head = git(repo, "rev-parse", "HEAD")

            code, report = run_cli(["--config", str(config_path), "worktree", "apply", "apply-dry", "--dry-run", "--json"])

            self.assertEqual(0, code)
            self.assertFalse(report["applied"])
            self.assertTrue(report["gates_ok"])
            self.assertEqual("cbr/apply-dry", report["branch"])
            self.assertEqual(task["execution_base_head"], report["base_head"])
            self.assertEqual(branch_head, report["branch_head"])
            self.assertEqual(main_head, report["main_head"])
            self.assertEqual(1, report["commit_summary"]["count"])
            self.assertEqual(main_head, git(repo, "rev-parse", "HEAD"))
            self.assertNotIn("execution_apply_status", load_task(config, "apply-dry"))

    def test_apply_fast_forwards_accepted_worktree_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="apply-ok")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "apply-ok", "--apply", "--json"])[0])
            task = load_task(config, "apply-ok")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\napplied change\n", encoding="utf-8")
            git(worktree, "commit", "-am", "applied change")
            branch_head = git(worktree, "rev-parse", "HEAD")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)

            code, report = run_cli(["--config", str(config_path), "worktree", "apply", "apply-ok", "--apply", "--json"])

            self.assertEqual(0, code)
            self.assertTrue(report["applied"])
            self.assertEqual(branch_head, git(repo, "rev-parse", "HEAD"))
            self.assertEqual("base\napplied change\n", (repo / "file.txt").read_text(encoding="utf-8"))
            loaded = load_task(config, "apply-ok")
            self.assertEqual("applied", loaded["execution_apply_status"])
            self.assertEqual(branch_head, loaded["execution_applied_head"])
            self.assertEqual("main", loaded["execution_apply_target"])
            events = list_events(config, task_id="apply-ok", limit=0)
            self.assertTrue(any(event["event_type"] == "task_worktree_applied" for event in events))
            self.assertNotIn("prompt", json.dumps(events))

    def test_accept_fast_forwards_worktree_branch_after_review_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="accept-apply")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "accept-apply", "--apply", "--json"])[0])
            task = load_task(config, "accept-apply")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\naccepted change\n", encoding="utf-8")
            git(worktree, "commit", "-am", "accepted change")
            branch_head = git(worktree, "rev-parse", "HEAD")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "accept", "accept-apply", "--reason", "verified", "--json"])

            self.assertEqual(0, code)
            self.assertEqual("applied", output["post_accept"]["status"])
            self.assertEqual(branch_head, git(repo, "rev-parse", "HEAD"))
            loaded = load_task(config, "accept-apply")
            self.assertEqual("accepted", loaded["review_status"])
            self.assertEqual("applied", loaded["execution_apply_status"])
            self.assertEqual(branch_head, loaded["execution_applied_head"])

    def test_review_next_auto_accept_applies_fast_forward_worktree_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
            config_data["auto_review_mechanical_accept"] = True
            config_path.write_text(json.dumps(config_data), encoding="utf-8")
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="auto-apply")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "auto-apply", "--apply", "--json"])[0])
            task = load_task(config, "auto-apply")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\nauto change\n", encoding="utf-8")
            git(worktree, "commit", "-am", "auto change")
            branch_head = git(worktree, "rev-parse", "HEAD")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["completed_at"] = "2026-01-01T00:00:00+00:00"
            task["last_result"] = {
                "task_id": "auto-apply",
                "status": "completed",
                "summary": "done",
                "changed_files": ["file.txt"],
                "verification": ["unit tests"],
            }
            task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
            save_task(config, task)

            code, report = run_cli(["--config", str(config_path), "review-next", "--apply", "--json"])

            self.assertEqual(0, code)
            self.assertEqual("accepted", report["auto_review"]["decision"])
            self.assertEqual("applied", report["post_accept"]["status"])
            self.assertEqual(branch_head, git(repo, "rev-parse", "HEAD"))
            loaded = load_task(config, "auto-apply")
            self.assertEqual("accepted", loaded["review_status"])
            self.assertEqual("applied", loaded["execution_apply_status"])

    def test_review_next_auto_accept_clean_stale_rebase_requires_re_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
            config_data["auto_review_mechanical_accept"] = True
            config_path.write_text(json.dumps(config_data), encoding="utf-8")
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="auto-rebase")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "auto-rebase", "--apply", "--json"])[0])
            task = load_task(config, "auto-rebase")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\nauto branch change\n", encoding="utf-8")
            git(worktree, "commit", "-am", "auto branch change")
            (repo / "main.txt").write_text("main moved\n", encoding="utf-8")
            git(repo, "add", "main.txt")
            git(repo, "commit", "-m", "main moved")
            moved_head = git(repo, "rev-parse", "HEAD")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["chain_status"] = "awaiting_review"
            task["completed_at"] = "2026-01-01T00:00:00+00:00"
            task["last_result"] = {
                "task_id": "auto-rebase",
                "status": "completed",
                "summary": "done",
                "changed_files": ["file.txt"],
                "verification": ["unit tests"],
            }
            task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
            save_task(config, task)

            code, report = run_cli(["--config", str(config_path), "review-next", "--apply", "--json"])

            self.assertEqual(0, code)
            self.assertEqual("rebased_re_review", report["auto_review"]["decision"])
            self.assertEqual("rebased_awaiting_re_review", report["post_accept"]["status"])
            self.assertEqual(moved_head, git(repo, "rev-parse", "HEAD"))
            loaded = load_task(config, "auto-rebase")
            self.assertEqual("unreviewed", loaded["review_status"])
            self.assertEqual("awaiting_review", loaded["chain_status"])
            self.assertEqual("rebased", loaded["execution_rebase_status"])
            self.assertNotIn("execution_apply_status", loaded)

    def test_apply_refuses_unaccepted_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="apply-unaccepted")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "apply-unaccepted", "--apply", "--json"])[0])
            task = load_task(config, "apply-unaccepted")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\nchange\n", encoding="utf-8")
            git(worktree, "commit", "-am", "change")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            save_task(config, task)
            main_head = git(repo, "rev-parse", "HEAD")

            code, report = run_cli(["--config", str(config_path), "worktree", "apply", "apply-unaccepted", "--apply", "--json"])

            self.assertEqual(1, code)
            self.assertIn("status=completed and review_status=accepted", " ".join(report["errors"]))
            self.assertEqual(main_head, git(repo, "rev-parse", "HEAD"))

    def test_apply_refuses_dirty_main_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="apply-dirty-main")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "apply-dirty-main", "--apply", "--json"])[0])
            task = load_task(config, "apply-dirty-main")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\nbranch change\n", encoding="utf-8")
            git(worktree, "commit", "-am", "branch change")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            code, report = run_cli(["--config", str(config_path), "worktree", "apply", "apply-dirty-main", "--apply", "--json"])

            self.assertEqual(1, code)
            self.assertIn("main worktree must be clean", " ".join(report["errors"]))
            self.assertNotIn("execution_apply_status", load_task(config, "apply-dirty-main"))

    def test_apply_dry_run_reports_stale_base_rebase_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="apply-stale-main")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "apply-stale-main", "--apply", "--json"])[0])
            task = load_task(config, "apply-stale-main")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\nbranch change\n", encoding="utf-8")
            git(worktree, "commit", "-am", "branch change")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)
            (repo / "main.txt").write_text("main moved\n", encoding="utf-8")
            git(repo, "add", "main.txt")
            git(repo, "commit", "-m", "main moved")
            moved_head = git(repo, "rev-parse", "HEAD")
            branch_head = git(worktree, "rev-parse", "HEAD")

            code, report = run_cli(["--config", str(config_path), "worktree", "apply", "apply-stale-main", "--dry-run", "--json"])

            self.assertEqual(0, code)
            self.assertFalse(report["applied"])
            self.assertTrue(report["gates_ok"])
            self.assertEqual("stale_base_rebase", report["apply_strategy"])
            self.assertEqual("clean", report["rebase"]["status"])
            self.assertEqual("unreviewed", report["rebase"]["review_status_after_clean_rebase"])
            self.assertEqual(moved_head, git(repo, "rev-parse", "HEAD"))
            self.assertEqual(branch_head, git(worktree, "rev-parse", "HEAD"))
            loaded = load_task(config, "apply-stale-main")
            self.assertEqual("accepted", loaded["review_status"])
            self.assertNotIn("execution_rebase_status", loaded)

    def test_apply_rebases_stale_base_and_requires_re_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="apply-rebase")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "apply-rebase", "--apply", "--json"])[0])
            task = load_task(config, "apply-rebase")
            old_base = task["execution_base_head"]
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\nbranch change\n", encoding="utf-8")
            git(worktree, "commit", "-am", "branch change")
            old_branch_head = git(worktree, "rev-parse", "HEAD")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["chain_status"] = "accepted"
            save_task(config, task)
            (repo / "main.txt").write_text("main moved\n", encoding="utf-8")
            git(repo, "add", "main.txt")
            git(repo, "commit", "-m", "main moved")
            moved_head = git(repo, "rev-parse", "HEAD")

            code, report = run_cli(["--config", str(config_path), "worktree", "apply", "apply-rebase", "--apply", "--json"])

            self.assertEqual(0, code)
            self.assertFalse(report["applied"])
            self.assertTrue(report["rebased"])
            self.assertEqual("stale_base_rebase", report["apply_strategy"])
            self.assertEqual("rebased", report["rebase"]["status"])
            self.assertEqual(moved_head, git(repo, "rev-parse", "HEAD"))
            loaded = load_task(config, "apply-rebase")
            new_branch_head = git(worktree, "rev-parse", "HEAD")
            self.assertEqual("unreviewed", loaded["review_status"])
            self.assertEqual("awaiting_review", loaded["chain_status"])
            self.assertEqual("awaiting_review", report["rebase"]["chain_status"])
            self.assertEqual(moved_head, loaded["execution_base_head"])
            self.assertEqual("rebased", loaded["execution_rebase_status"])
            self.assertEqual(old_base, loaded["execution_rebased_from_base"])
            self.assertEqual(moved_head, loaded["execution_rebased_onto"])
            self.assertEqual(old_branch_head, loaded["execution_rebased_from_head"])
            self.assertEqual(new_branch_head, loaded["execution_rebased_head"])
            self.assertNotEqual(old_branch_head, new_branch_head)
            self.assertNotIn("execution_apply_status", loaded)
            self.assertIn("branch change", (worktree / "file.txt").read_text(encoding="utf-8"))
            self.assertIn("main moved", (worktree / "main.txt").read_text(encoding="utf-8"))
            events = list_events(config, task_id="apply-rebase", limit=0)
            self.assertTrue(any(event["event_type"] == "task_worktree_rebased" for event in events))
            self.assertNotIn("prompt", json.dumps(events))
            summary_code, summary = run_cli_text(["--config", str(config_path), "summary", "apply-rebase"])
            list_code, list_output = run_cli_text(["--config", str(config_path), "list", "--all", "--color=never"])
            self.assertEqual(0, summary_code)
            self.assertIn("rebase_status: rebased", summary)
            self.assertEqual(0, list_code)
            self.assertIn("rebased; re-review needed", list_output)

    def test_apply_stale_base_rebase_conflict_queues_single_conflict_fix_subtask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="apply-rebase-conflict")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "apply-rebase-conflict", "--apply", "--json"])[0])
            task = load_task(config, "apply-rebase-conflict")
            worktree = Path(task["execution_worktree_path"])
            (worktree / "file.txt").write_text("base\nbranch change\n", encoding="utf-8")
            git(worktree, "commit", "-am", "branch change")
            branch_head = git(worktree, "rev-parse", "HEAD")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)
            (repo / "file.txt").write_text("base\nmain change\n", encoding="utf-8")
            git(repo, "commit", "-am", "main conflicting change")
            moved_head = git(repo, "rev-parse", "HEAD")

            code, report = run_cli(["--config", str(config_path), "worktree", "apply", "apply-rebase-conflict", "--apply", "--json"])

            self.assertEqual(1, code)
            self.assertFalse(report["applied"])
            self.assertEqual("stale_base_rebase", report["apply_strategy"])
            self.assertEqual("blocked", report["rebase"]["status"])
            self.assertIn("conflict-fix subtask", " ".join(report["errors"]))
            self.assertEqual("queued", report["conflict_fix"]["status"])
            fix_task_id = report["conflict_fix"]["task_id"]
            self.assertEqual(moved_head, git(repo, "rev-parse", "HEAD"))
            self.assertEqual(branch_head, git(worktree, "rev-parse", "HEAD"))
            loaded = load_task(config, "apply-rebase-conflict")
            self.assertEqual("accepted", loaded["review_status"])
            self.assertEqual("blocked", loaded["execution_rebase_status"])
            self.assertEqual("queued", loaded["execution_conflict_fix_status"])
            self.assertEqual(fix_task_id, loaded["execution_conflict_fix_task_id"])
            self.assertEqual(fix_task_id, loaded["last_conflict_fix_task_id"])
            self.assertIn(fix_task_id, loaded["blocking_subtask_ids"])
            self.assertIn("could not apply", loaded["execution_rebase_blocker"])
            fix_task = load_task(config, fix_task_id)
            self.assertEqual("runnable", fix_task["status"])
            self.assertEqual([], fix_task["depends_on"])
            self.assertEqual("worktree_conflict_fix", fix_task["subtask_type"])
            self.assertEqual("apply-rebase-conflict", fix_task["subtask_for"])
            self.assertEqual("apply-rebase-conflict", fix_task["root_task_id"])
            self.assertEqual("apply-rebase-conflict", fix_task["parent_task_id"])
            self.assertTrue(fix_task["blocks_root_completion"])
            self.assertIn("Port the parent task branch changes onto current main", fix_task["prompt"])

            second_code, second_report = run_cli(
                ["--config", str(config_path), "worktree", "apply", "apply-rebase-conflict", "--apply", "--json"]
            )

            self.assertEqual(1, second_code)
            self.assertEqual(fix_task_id, second_report["conflict_fix"]["task_id"])
            queued_fix_tasks = [
                item
                for item in config.queue_dir.glob("*.json")
                if load_task(config, item.stem).get("subtask_type") == "worktree_conflict_fix"
            ]
            self.assertEqual(1, len(queued_fix_tasks))
            events = list_events(config, task_id="apply-rebase-conflict", limit=0)
            self.assertTrue(any(event["event_type"] == "task_worktree_conflict_fix_enqueued" for event in events))
            list_code, list_output = run_cli_text(["--config", str(config_path), "list", "--all", "--color=never"])
            self.assertEqual(0, list_code)
            self.assertIn("conflict fix queued", list_output)

    def test_apply_refuses_branch_with_no_commits_after_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="apply-empty")
            self.assertEqual(0, run_cli(["--config", str(config_path), "worktree", "prepare", "apply-empty", "--apply", "--json"])[0])
            task = load_task(config, "apply-empty")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)
            main_head = git(repo, "rev-parse", "HEAD")

            code, report = run_cli(["--config", str(config_path), "worktree", "apply", "apply-empty", "--dry-run", "--json"])

            self.assertEqual(1, code)
            self.assertIn("no commits after execution_base_head", " ".join(report["errors"]))
            self.assertEqual(0, report["commit_summary"]["count"])
            self.assertEqual(main_head, git(repo, "rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()

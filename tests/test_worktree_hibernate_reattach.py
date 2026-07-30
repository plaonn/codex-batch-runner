from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_batch_runner.worktree as worktree_module
from codex_batch_runner.config import Config
from codex_batch_runner.queue import load_task
from codex_batch_runner.review_bundle import build_review_bundle
from codex_batch_runner.worktree import (
    WORKTREE_HIBERNATION_CONTRACT,
    build_hibernate_report,
    build_reattach_report,
)
from codex_batch_runner.worktree_hibernation import build_worktree_hibernation_plan


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def fixture(root: Path, *, status: str = "completed") -> tuple[Config, Path, Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "CBR Test")
    git(repo, "config", "user.email", "cbr@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    worktree_root = root / "worktrees"
    worktree_root.mkdir()
    worktree = worktree_root / "hibernate-me"
    branch = "cbr/hibernate-me"
    git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    (worktree / "change.txt").write_text("change\n", encoding="utf-8")
    git(worktree, "add", "change.txt")
    git(worktree, "commit", "-m", "change")
    head = git(worktree, "rev-parse", "HEAD")
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "tasks"),
                "log_dir": str(root / "logs"),
                "event_dir": str(root / "events"),
                "lock_file": str(root / "runner.lock"),
                "state_file": str(root / "state.json"),
                "worktree_mode": "task",
                "worktree_root": str(worktree_root),
            }
        ),
        encoding="utf-8",
    )
    config = Config.load(str(config_path))
    config.queue_dir.mkdir(parents=True)
    task = {
        "id": "hibernate-me",
        "cwd": str(repo),
        "project_root": str(repo),
        "status": status,
        "review_status": "unreviewed",
        "execution_mode": "git_worktree",
        "execution_repo_root": str(repo),
        "execution_worktree_path": str(worktree),
        "execution_worktree_status": "retained",
        "execution_branch": branch,
        "execution_base_ref": "HEAD",
        "execution_base_head": base,
        "execution_branch_head": head,
        "execution_mutation_provenance_history": [{"fixture": True}],
        "last_result": {"changed_files": ["change.txt"], "verification": ["tests"]},
    }
    (config.queue_dir / "hibernate-me.json").write_text(
        json.dumps(task), encoding="utf-8"
    )
    return config, repo, worktree, head


SAFE_PROVENANCE = {"status": "mutation_observed", "unsafe_or_unreported": False}


class WorktreeHibernateReattachTests(unittest.TestCase):
    def test_hibernate_and_reattach_preserve_exact_branch_review_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, repo, worktree, head = fixture(Path(tmp))
            with patch(
                "codex_batch_runner.worktree_hibernation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                dry_run = build_hibernate_report(config, "hibernate-me")
                applied = build_hibernate_report(
                    config, "hibernate-me", apply=True
                )
            self.assertFalse(dry_run["errors"])
            self.assertFalse(worktree.exists())
            self.assertTrue(applied["applied"])
            task = load_task(config, "hibernate-me")
            self.assertEqual("hibernated", task["execution_worktree_status"])
            self.assertNotIn("execution_worktree_path", task)
            self.assertEqual(
                WORKTREE_HIBERNATION_CONTRACT,
                task["execution_hibernation_contract"],
            )
            self.assertEqual(head, git(repo, "rev-parse", "cbr/hibernate-me"))

            plan = build_worktree_hibernation_plan(config, task_id="hibernate-me")
            self.assertEqual(
                "hibernated_current", plan["items"][0]["reconciliation"]["status"]
            )
            self.assertTrue(plan["items"][0]["reattach"]["compatible"])
            bundle = build_review_bundle(task)
            self.assertEqual(
                "branch_only_repository",
                bundle["current_task_repository"]["inspection_scope"],
            )
            self.assertIsNone(bundle["current_task_worktree_repository"])
            self.assertEqual(
                "hibernated_worktree_branch",
                bundle["commit_information"]["source"],
            )
            self.assertEqual("commit", bundle["git_diff"]["kind"])

            reattach_dry_run = build_reattach_report(config, "hibernate-me")
            reattached = build_reattach_report(
                config, "hibernate-me", apply=True
            )
            self.assertFalse(reattach_dry_run["errors"])
            self.assertTrue(reattached["applied"])
            task = load_task(config, "hibernate-me")
            self.assertEqual("retained", task["execution_worktree_status"])
            self.assertTrue(Path(task["execution_worktree_path"]).is_dir())
            self.assertEqual(head, git(Path(task["execution_worktree_path"]), "rev-parse", "HEAD"))

    def test_missing_path_is_never_reattach_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, repo, worktree, _ = fixture(Path(tmp))
            git(repo, "worktree", "remove", str(worktree))
            report = build_reattach_report(config, "hibernate-me")
            self.assertIn("intentional hibernated state", report["errors"][0])

    def test_active_or_dirty_task_fails_closed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, _, worktree, _ = fixture(Path(tmp), status="running")
            (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with patch(
                "codex_batch_runner.worktree_hibernation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_hibernate_report(
                    config, "hibernate-me", apply=True
                )
            self.assertTrue(report["errors"])
            self.assertTrue(worktree.is_dir())
            self.assertEqual(
                "retained",
                load_task(config, "hibernate-me")["execution_worktree_status"],
            )

    def test_ignored_untracked_file_is_still_dirty_for_disposable_hibernate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, repo, worktree, _ = fixture(Path(tmp))
            (repo / ".git" / "info" / "exclude").write_text(
                "ignored.txt\n", encoding="utf-8"
            )
            (worktree / "ignored.txt").write_text("local state\n", encoding="utf-8")
            with patch(
                "codex_batch_runner.worktree_hibernation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_hibernate_report(
                    config, "hibernate-me", apply=True
                )
            self.assertIn("dirty_worktree", report["errors"][0])
            self.assertTrue(worktree.is_dir())

    def test_branch_head_change_blocks_reattach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, repo, _, _ = fixture(Path(tmp))
            with patch(
                "codex_batch_runner.worktree_hibernation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                self.assertTrue(
                    build_hibernate_report(
                        config, "hibernate-me", apply=True
                    )["applied"]
                )
            git(repo, "checkout", "cbr/hibernate-me")
            (repo / "later.txt").write_text("later\n", encoding="utf-8")
            git(repo, "add", "later.txt")
            git(repo, "commit", "-m", "later")
            git(repo, "checkout", "main")
            report = build_reattach_report(config, "hibernate-me")
            self.assertIn("does not match", report["errors"][0])
            bundle = build_review_bundle(load_task(config, "hibernate-me"))
            self.assertEqual(
                "checkpoint_mismatch", bundle["commit_information"]["status"]
            )
            self.assertEqual("none", bundle["git_diff"]["kind"])
            self.assertFalse(bundle["changed_files"]["git_name_status"])

    def test_hibernate_mutation_failure_records_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, _, worktree, _ = fixture(Path(tmp))
            original_git = worktree_module.git

            def fail_remove(cwd: Path, *args: str) -> str:
                if args[:2] == ("worktree", "remove"):
                    raise subprocess.CalledProcessError(1, ["git", *args])
                return original_git(cwd, *args)

            with (
                patch(
                    "codex_batch_runner.worktree_hibernation._provenance",
                    return_value=SAFE_PROVENANCE,
                ),
                patch("codex_batch_runner.worktree.git", side_effect=fail_remove),
            ):
                report = build_hibernate_report(
                    config, "hibernate-me", apply=True
                )
            self.assertTrue(report["errors"])
            self.assertTrue(worktree.is_dir())
            self.assertEqual(
                "recovery_required",
                load_task(config, "hibernate-me")["execution_worktree_status"],
            )

    def test_reattach_mutation_failure_records_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, _, _, _ = fixture(Path(tmp))
            with patch(
                "codex_batch_runner.worktree_hibernation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                self.assertTrue(
                    build_hibernate_report(
                        config, "hibernate-me", apply=True
                    )["applied"]
                )
            original_git = worktree_module.git

            def fail_add(cwd: Path, *args: str) -> str:
                if args[:2] == ("worktree", "add"):
                    raise subprocess.CalledProcessError(1, ["git", *args])
                return original_git(cwd, *args)

            with patch("codex_batch_runner.worktree.git", side_effect=fail_add):
                report = build_reattach_report(
                    config, "hibernate-me", apply=True
                )
            self.assertTrue(report["errors"])
            task = load_task(config, "hibernate-me")
            self.assertEqual("recovery_required", task["execution_worktree_status"])
            self.assertNotIn("execution_worktree_path", task)


if __name__ == "__main__":
    unittest.main()

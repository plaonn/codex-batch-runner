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


def run_cli(args: list[str]) -> tuple[int, dict]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, json.loads(stdout.getvalue())


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


def write_config(root: Path, repo: Path) -> Path:
    data = {
        "root": str(repo),
        "queue_dir": str(root / "tasks"),
        "log_dir": str(root / "logs"),
        "event_dir": str(root / "events"),
        "lock_file": str(root / "runner.lock"),
        "state_file": str(root / "state.json"),
        "worktree_mode": "task",
        "worktree_root": str(root / "task-worktrees"),
        "codex_command": [sys.executable, "-c", "raise SystemExit('codex must not run')"],
    }
    path = root / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def create_direct_worktree(repo: Path, path: Path, branch: str, *, commit: bool, merge: bool, dirty: bool) -> None:
    git(repo, "worktree", "add", "-b", branch, str(path), "main")
    if commit:
        filename = branch.replace("/", "-") + ".txt"
        (path / filename).write_text(f"{branch}\n", encoding="utf-8")
        git(path, "add", filename)
        git(path, "commit", "-m", f"{branch} change")
    if merge:
        git(repo, "merge", "--ff-only", branch)
    if dirty:
        (path / "dirty.txt").write_text("dirty\n", encoding="utf-8")


class DirectWorktreeMaintenanceTests(unittest.TestCase):
    def test_dry_run_classifies_all_direct_worktree_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "demo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root, repo)

            create_direct_worktree(repo, root / "demo-merged-clean", "codex/merged-clean", commit=True, merge=True, dirty=False)
            create_direct_worktree(repo, root / "demo-merged-dirty", "codex/merged-dirty", commit=True, merge=True, dirty=True)
            create_direct_worktree(repo, root / "demo-unmerged-clean", "codex/unmerged-clean", commit=True, merge=False, dirty=False)
            create_direct_worktree(repo, root / "demo-unmerged-dirty", "codex/unmerged-dirty", commit=True, merge=False, dirty=True)

            code, report = run_cli(["--config", str(config_path), "maintenance", "direct-worktrees", "--dry-run", "--json"])

            self.assertEqual(0, code)
            by_branch = {candidate["branch"]: candidate for candidate in report["candidates"]}
            self.assertEqual("merged+clean", by_branch["codex/merged-clean"]["classification"])
            self.assertEqual("merged+dirty", by_branch["codex/merged-dirty"]["classification"])
            self.assertEqual("unmerged+clean", by_branch["codex/unmerged-clean"]["classification"])
            self.assertEqual("unmerged+dirty", by_branch["codex/unmerged-dirty"]["classification"])
            self.assertTrue(by_branch["codex/merged-clean"]["eligible"])
            self.assertFalse(by_branch["codex/merged-dirty"]["eligible"])
            self.assertFalse(by_branch["codex/unmerged-clean"]["eligible"])
            self.assertFalse(by_branch["codex/unmerged-dirty"]["eligible"])
            self.assertEqual(1, report["summary"]["eligible"])
            self.assertEqual(3, report["summary"]["blocked"])

    def test_dry_run_reports_branch_and_path_allowlist_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "demo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root, repo)

            create_direct_worktree(repo, root / "demo-feature", "feature/refused", commit=True, merge=False, dirty=False)
            create_direct_worktree(repo, root / "other-codex", "codex/bad-path", commit=True, merge=False, dirty=False)

            code, report = run_cli(["--config", str(config_path), "maintenance", "direct-worktrees", "--dry-run", "--json"])

            self.assertEqual(0, code)
            by_branch = {candidate["branch"]: candidate for candidate in report["candidates"]}
            self.assertEqual("refused", by_branch["feature/refused"]["classification"])
            self.assertIn("branch is outside codex/ namespace", by_branch["feature/refused"]["blockers"])
            self.assertEqual("refused", by_branch["codex/bad-path"]["classification"])
            self.assertIn("path is not a sibling demo-* worktree", by_branch["codex/bad-path"]["blockers"])
            self.assertEqual(2, report["summary"]["refused"])
            self.assertEqual([], report["eligible"])

    def test_apply_removes_only_merged_clean_with_non_force_branch_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "demo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root, repo)
            config = Config.load(str(config_path))

            merged_clean = root / "demo-merged-clean"
            merged_dirty = root / "demo-merged-dirty"
            unmerged_clean = root / "demo-unmerged-clean"
            unmerged_dirty = root / "demo-unmerged-dirty"
            create_direct_worktree(repo, merged_clean, "codex/merged-clean", commit=True, merge=True, dirty=False)
            create_direct_worktree(repo, merged_dirty, "codex/merged-dirty", commit=True, merge=True, dirty=True)
            create_direct_worktree(repo, unmerged_clean, "codex/unmerged-clean", commit=True, merge=False, dirty=False)
            create_direct_worktree(repo, unmerged_dirty, "codex/unmerged-dirty", commit=True, merge=False, dirty=True)

            from codex_batch_runner import direct_worktrees

            original_run_git = direct_worktrees.run_git
            calls: list[tuple[str, ...]] = []

            def recording_run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
                calls.append(args)
                return original_run_git(cwd, *args)

            with patch("codex_batch_runner.direct_worktrees.run_git", side_effect=recording_run_git):
                code, report = run_cli(["--config", str(config_path), "maintenance", "direct-worktrees", "--apply", "--json"])

            self.assertEqual(0, code)
            self.assertEqual("applied", report["status"])
            self.assertFalse(merged_clean.exists())
            self.assertTrue(merged_dirty.exists())
            self.assertTrue(unmerged_clean.exists())
            self.assertTrue(unmerged_dirty.exists())
            self.assertEqual("", git(repo, "branch", "--list", "codex/merged-clean"))
            self.assertIn("codex/merged-dirty", git(repo, "branch", "--list", "codex/merged-dirty"))
            self.assertIn("codex/unmerged-clean", git(repo, "branch", "--list", "codex/unmerged-clean"))
            self.assertIn("codex/unmerged-dirty", git(repo, "branch", "--list", "codex/unmerged-dirty"))
            self.assertIn(("branch", "-d", "codex/merged-clean"), calls)
            self.assertFalse(any("-D" in call or "--force" in call for call in calls))

            events = list_events(config, limit=0)
            event = next(item for item in events if item["event_type"] == "direct_worktree_cleaned")
            self.assertEqual("codex/merged-clean", event["payload"]["branch"])
            self.assertEqual("demo-merged-clean", event["payload"]["path"])
            self.assertTrue(event["payload"]["worktree_removed"])
            self.assertTrue(event["payload"]["branch_deleted"])
            self.assertNotIn("prompt", json.dumps(events))

    def test_apply_reports_partial_when_branch_delete_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "demo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(root, repo)

            worktree = root / "demo-merged-clean"
            create_direct_worktree(repo, worktree, "codex/merged-clean", commit=True, merge=True, dirty=False)

            from codex_batch_runner import direct_worktrees

            original_run_git = direct_worktrees.run_git
            calls: list[tuple[str, ...]] = []

            def failing_branch_delete(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
                calls.append(args)
                if args == ("branch", "-d", "codex/merged-clean"):
                    return subprocess.CompletedProcess(
                        args=["git", "-C", str(cwd), *args],
                        returncode=1,
                        stdout="",
                        stderr="error: simulated branch delete failure",
                    )
                return original_run_git(cwd, *args)

            with patch("codex_batch_runner.direct_worktrees.run_git", side_effect=failing_branch_delete):
                code, report = run_cli(["--config", str(config_path), "maintenance", "direct-worktrees", "--apply", "--json"])

            self.assertEqual(1, code)
            self.assertEqual("partial", report["status"])
            self.assertFalse(worktree.exists())
            self.assertIn("codex/merged-clean", git(repo, "branch", "--list", "codex/merged-clean"))
            self.assertEqual("partial", report["results"][0]["status"])
            self.assertTrue(report["results"][0]["worktree_removed"])
            self.assertFalse(report["results"][0]["branch_deleted"])
            self.assertIn("branch deletion failed", report["results"][0]["blockers"][0])
            self.assertIn(("branch", "-d", "codex/merged-clean"), calls)
            self.assertFalse(any("-D" in call or "--force" in call for call in calls))

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
from codex_batch_runner.queue import create_task, load_task, save_task
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

    def test_cleanup_refuses_unaccepted_task_and_removes_accepted_worktree(self) -> None:
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

            self.assertEqual(0, code)
            self.assertTrue(report["applied"])
            self.assertFalse(worktree_path.exists())
            self.assertEqual("cleaned", load_task(config, "cleanup")["execution_worktree_status"])
            self.assertIn("cbr/cleanup", git(repo, "branch", "--list", "cbr/cleanup"))

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
            self.assertIn("accept-link\taccepted", accept_output)
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

    def test_review_next_reports_worktree_warnings_without_failing_gates(self) -> None:
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

            self.assertEqual(0, code)
            self.assertTrue(report["selected"])
            self.assertIn("worktree_report", report)
            self.assertTrue(report["worktree_report"]["recovery_required"])
            self.assertIn("worktree_path", report["worktree_report"]["stale_metadata"])
            self.assertTrue(report["gates_ok"])


if __name__ == "__main__":
    unittest.main()

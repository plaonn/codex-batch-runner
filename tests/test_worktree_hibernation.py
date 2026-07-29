from __future__ import annotations

import json
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.worktree_hibernation import (
    WorktreeHibernationPlanValidationError,
    build_worktree_hibernation_plan,
    validate_worktree_hibernation_plan,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def write_config(root: Path) -> Path:
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "runtime" / "tasks"),
                "log_dir": str(root / "runtime" / "logs"),
                "event_dir": str(root / "runtime" / "events"),
                "lock_file": str(root / "runtime" / "runner.lock"),
                "state_file": str(root / "runtime" / "state.json"),
            }
        ),
        encoding="utf-8",
    )
    return config_path


def create_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "CBR Test")
    git(repo, "config", "user.email", "cbr@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def create_worktree_task(
    config: Config,
    repo: Path,
    base: str,
    *,
    task_id: str = "reviewable",
    status: str = "completed",
) -> tuple[dict[str, object], Path]:
    branch = f"cbr/{task_id}"
    worktree = repo.parent / f"worktree-{task_id}"
    git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    (worktree / "change.txt").write_text("change\n", encoding="utf-8")
    git(worktree, "add", "change.txt")
    git(worktree, "commit", "-m", "task change")
    head = git(worktree, "rev-parse", "HEAD")
    task: dict[str, object] = {
        "id": task_id,
        "project_id": "example-project",
        "status": status,
        "review_status": "unreviewed",
        "attempts": 1,
        "execution_mode": "git_worktree",
        "execution_repo_root": str(repo),
        "execution_worktree_path": str(worktree),
        "execution_worktree_status": "retained",
        "execution_branch": branch,
        "execution_base_head": base,
        "execution_branch_head": head,
        "execution_mutation_provenance_history": [{"fixture": True}],
    }
    config.queue_dir.mkdir(parents=True)
    (config.queue_dir / f"{task_id}.json").write_text(
        json.dumps(task), encoding="utf-8"
    )
    return task, worktree


SAFE_PROVENANCE = {
    "status": "mutation_observed",
    "unsafe_or_unreported": False,
}


class WorktreeHibernationPlanTests(unittest.TestCase):
    def test_public_example_validates(self) -> None:
        example_path = (
            Path(__file__).parents[1]
            / "examples"
            / "worktree-hibernation-plan-v1.example.json"
        )
        report = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertEqual(report, validate_worktree_hibernation_plan(report))

    def test_missing_runtime_directory_is_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            report = build_worktree_hibernation_plan(config)
            self.assertEqual(0, report["summary"]["task_count"])
            self.assertFalse(config.queue_dir.exists())
            self.assertFalse(config.lock_file.exists())
            self.assertFalse(config.state_file.exists())

    def test_attached_clean_checkpoint_is_branch_review_and_hibernation_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            _, worktree = create_worktree_task(config, repo, base)
            with patch(
                "codex_batch_runner.worktree_hibernation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                first = build_worktree_hibernation_plan(config)
                second = build_worktree_hibernation_plan(config)
            item = first["items"][0]
            self.assertEqual("attached_current", item["reconciliation"]["status"])
            self.assertTrue(item["branch_only_review"]["compatible"])
            self.assertTrue(item["hibernation"]["compatible"])
            self.assertFalse(item["reattach"]["compatible"])
            self.assertEqual(first, second)
            rendered = json.dumps(first, sort_keys=True)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn(str(worktree), rendered)

    def test_missing_path_is_not_inferred_as_hibernation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            _, worktree = create_worktree_task(config, repo, base)
            git(repo, "worktree", "remove", str(worktree))
            with patch(
                "codex_batch_runner.worktree_hibernation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_hibernation_plan(config)
            item = report["items"][0]
            self.assertEqual(
                "missing_path_branch_present", item["reconciliation"]["status"]
            )
            self.assertTrue(item["branch_only_review"]["compatible"])
            self.assertFalse(item["hibernation"]["compatible"])
            self.assertIn(
                "intentional_hibernation_not_supported_v1",
                item["reattach"]["reason_codes"],
            )

    def test_needs_resume_requires_same_retained_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            _, worktree = create_worktree_task(
                config, repo, base, task_id="resume", status="needs_resume"
            )
            git(repo, "worktree", "remove", str(worktree))
            report = build_worktree_hibernation_plan(config)
            item = report["items"][0]
            self.assertFalse(item["resume"]["compatible"])
            self.assertIn(
                "resume_incompatible_recreated_cwd",
                item["resume"]["reason_codes"],
            )
            self.assertFalse(item["hibernation"]["compatible"])

    def test_missing_provenance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task, _ = create_worktree_task(config, repo, base)
            task.pop("execution_mutation_provenance_history")
            (config.queue_dir / "reviewable.json").write_text(
                json.dumps(task), encoding="utf-8"
            )
            report = build_worktree_hibernation_plan(config)
            item = report["items"][0]
            self.assertTrue(item["branch_only_review"]["compatible"])
            self.assertFalse(item["hibernation"]["compatible"])
            self.assertIn(
                "missing_mutation_provenance",
                item["hibernation"]["reason_codes"],
            )

    def test_pool_lease_is_a_separate_fail_closed_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task, _ = create_worktree_task(config, repo, base)
            task["execution_worktree_pool"] = True
            (config.queue_dir / "reviewable.json").write_text(
                json.dumps(task), encoding="utf-8"
            )
            with patch(
                "codex_batch_runner.worktree_hibernation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_hibernation_plan(config)
            item = report["items"][0]
            self.assertTrue(item["branch_only_review"]["compatible"])
            self.assertFalse(item["pool_lease"]["compatible"])
            self.assertIn(
                "pool_metadata_incomplete", item["pool_lease"]["reason_codes"]
            )
            self.assertIn(
                "pool_lease_inconsistent", item["hibernation"]["reason_codes"]
            )

    def test_unavailable_repository_is_not_reported_as_missing_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            config.queue_dir.mkdir(parents=True)
            task = {
                "id": "unavailable",
                "status": "completed",
                "execution_mode": "git_worktree",
                "execution_repo_root": str(root / "missing-repository"),
                "execution_worktree_path": str(root / "missing-worktree"),
                "execution_worktree_status": "retained",
                "execution_branch": "cbr/unavailable",
                "execution_base_head": "a" * 40,
                "execution_branch_head": "b" * 40,
            }
            (config.queue_dir / "unavailable.json").write_text(
                json.dumps(task), encoding="utf-8"
            )
            report = build_worktree_hibernation_plan(config)
            item = report["items"][0]
            self.assertEqual("registry_unavailable", item["reconciliation"]["status"])
            self.assertIn(
                "repository_unavailable",
                item["branch_only_review"]["reason_codes"],
            )

    def test_unknown_task_tokens_are_not_reflected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            config.queue_dir.mkdir(parents=True)
            private_value = "private value that must not be reflected"
            (config.queue_dir / "unknown.json").write_text(
                json.dumps(
                    {
                        "id": "unknown",
                        "status": "completed",
                        "execution_mode": private_value,
                        "execution_worktree_status": private_value,
                    }
                ),
                encoding="utf-8",
            )
            report = build_worktree_hibernation_plan(config)
            item = report["items"][0]
            self.assertEqual("unknown", item["execution_mode"])
            self.assertEqual("unknown", item["worktree_status"])
            self.assertNotIn(private_value, json.dumps(report))

    def test_validator_rejects_mutation_or_digest_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            report = build_worktree_hibernation_plan(config)
            report["mutation"]["supported"] = True
            with self.assertRaises(WorktreeHibernationPlanValidationError):
                validate_worktree_hibernation_plan(report)

    def test_cli_has_no_apply_mode_and_supports_exact_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            config.queue_dir.mkdir(parents=True)
            (config.queue_dir / "plain.json").write_text(
                json.dumps({"id": "plain", "status": "completed"}),
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--config",
                        str(config_path),
                        "worktree",
                        "hibernation-plan",
                        "plain",
                        "--json",
                    ]
                )
            self.assertEqual(0, code)
            self.assertEqual(
                "plain", json.loads(output.getvalue())["items"][0]["task_id"]
            )
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(
                    [
                        "--config",
                        str(config_path),
                        "worktree",
                        "hibernation-plan",
                        "--apply",
                    ]
                )


if __name__ == "__main__":
    unittest.main()

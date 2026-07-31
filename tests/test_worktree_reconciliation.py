from __future__ import annotations

import copy
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.worktree_reconciliation import (
    WorktreeReconciliationPlanValidationError,
    build_worktree_reconciliation_plan,
    validate_worktree_reconciliation_plan,
)


SAFE_PROVENANCE = {
    "status": "mutation_observed",
    "unsafe_or_unreported": False,
}


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


def create_task(
    config: Config,
    repo: Path,
    base: str,
    *,
    task_id: str,
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
        "execution_mode": "git_worktree",
        "execution_repo_root": str(repo),
        "execution_worktree_path": str(worktree),
        "execution_worktree_status": "retained",
        "execution_branch": branch,
        "execution_base_head": base,
        "execution_branch_head": head,
        "execution_mutation_provenance_history": [{"fixture": True}],
    }
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    save_task(config, task)
    return task, worktree


def save_task(config: Config, task: dict[str, object]) -> None:
    (config.queue_dir / f"{task['id']}.json").write_text(
        json.dumps(task), encoding="utf-8"
    )


class WorktreeReconciliationPlanTests(unittest.TestCase):
    def test_public_example_validates(self) -> None:
        example = (
            Path(__file__).parents[1]
            / "examples"
            / "worktree-reconciliation-plan-v1.example.json"
        )
        report = json.loads(example.read_text(encoding="utf-8"))
        self.assertEqual(report, validate_worktree_reconciliation_plan(report))

    def test_empty_runtime_is_read_only_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            first = build_worktree_reconciliation_plan(config)
            second = build_worktree_reconciliation_plan(config)
            self.assertEqual(first, second)
            self.assertFalse(config.queue_dir.exists())
            self.assertFalse(config.lock_file.exists())
            self.assertFalse(config.state_file.exists())

    def test_attached_current_and_terminal_cleanup_are_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            current, _ = create_task(config, repo, base, task_id="attached-current")
            terminal, terminal_path = create_task(
                config, repo, base, task_id="terminal-cleanup"
            )
            git(repo, "worktree", "remove", str(terminal_path))
            terminal["execution_worktree_status"] = "cleaned"
            terminal["execution_cleanup_kind"] = "discard"
            terminal["execution_cleanup_reason"] = "review_status=rejected"
            terminal["execution_cleanup_branch_retained"] = True
            terminal["execution_cleanup_result_applied"] = False
            save_task(config, terminal)
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            items = {item["task_id"]: item for item in report["items"]}
            self.assertEqual("no_action", items[current["id"]]["action_class"])
            self.assertEqual("no_action", items[terminal["id"]]["action_class"])

    def test_missing_path_branch_present_is_manual_not_hibernated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            _, worktree = create_task(config, repo, base, task_id="missing-path")
            git(repo, "worktree", "remove", str(worktree))
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual("manual_review", item["action_class"])
            self.assertEqual([], item["metadata_delta"])
            self.assertIn(
                "missing_path_is_not_lifecycle_evidence", item["reason_codes"]
            )

    def test_missing_branch_is_unrecoverable_without_owner_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task, worktree = create_task(config, repo, base, task_id="missing-branch")
            git(repo, "worktree", "remove", str(worktree))
            git(repo, "branch", "-D", str(task["execution_branch"]))
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual(
                "unrecoverable_without_owner_decision", item["action_class"]
            )
            self.assertIn("missing_branch", item["reason_codes"])

    def test_registry_mismatch_is_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            _, worktree = create_task(config, repo, base, task_id="registry-mismatch")
            git(repo, "worktree", "remove", str(worktree))
            worktree.mkdir()
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            self.assertEqual(
                "unrecoverable_without_owner_decision",
                report["items"][0]["action_class"],
            )

    def test_intentional_hibernation_is_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task, worktree = create_task(config, repo, base, task_id="hibernated")
            git(repo, "worktree", "remove", str(worktree))
            task["execution_worktree_status"] = "hibernated"
            task["execution_hibernation_contract"] = "worktree-hibernation-v1"
            task["execution_hibernation_base_head"] = base
            task["execution_hibernation_branch_head"] = task["execution_branch_head"]
            save_task(config, task)
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            self.assertEqual("no_action", report["items"][0]["action_class"])
            self.assertIn(
                "intentional_hibernation_current",
                report["items"][0]["reason_codes"],
            )

    def test_dirty_and_active_states_stay_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            _, dirty_path = create_task(config, repo, base, task_id="dirty")
            (dirty_path / "uncheckpointed.txt").write_text("local\n", encoding="utf-8")
            active, _ = create_task(
                config, repo, base, task_id="active", status="needs_resume"
            )
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            items = {item["task_id"]: item for item in report["items"]}
            self.assertEqual("manual_review", items["dirty"]["action_class"])
            self.assertEqual("manual_review", items[active["id"]]["action_class"])

    def test_missing_or_ambiguous_provenance_cannot_be_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            _, worktree = create_task(config, repo, base, task_id="missing-provenance")
            git(repo, "worktree", "remove", str(worktree))
            report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual("manual_review", item["action_class"])
            self.assertIn("ambiguous_or_missing_provenance", item["reason_codes"])

    def test_independently_proven_cleanup_status_is_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task, worktree = create_task(config, repo, base, task_id="metadata-only")
            head = str(task["execution_branch_head"])
            git(repo, "merge", "--ff-only", str(task["execution_branch"]))
            git(repo, "worktree", "remove", str(worktree))
            task.update(
                {
                    "review_status": "accepted",
                    "execution_apply_status": "applied",
                    "execution_applied_head": head,
                    "execution_apply_target": "main",
                    "execution_cleanup_kind": "applied",
                    "execution_cleanup_reason": "execution_apply_status=applied",
                    "execution_cleanup_branch_retained": True,
                    "execution_cleanup_result_applied": True,
                    "execution_cleaned_at": "2030-01-01T00:00:00Z",
                }
            )
            save_task(config, task)
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                first = build_worktree_reconciliation_plan(config)
                second = build_worktree_reconciliation_plan(config)
            item = first["items"][0]
            self.assertEqual("exact_repair_candidate", item["action_class"])
            self.assertEqual(
                [
                    {
                        "field": "execution_worktree_status",
                        "before": "retained",
                        "after": "cleaned",
                    }
                ],
                item["metadata_delta"],
            )
            self.assertFalse(first["authority"]["repair_authority_granted"])
            self.assertEqual(first, second)
            rendered = json.dumps(first, sort_keys=True)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn(str(worktree), rendered)
            self.assertNotIn(str(task["execution_branch"]), rendered)

    def test_validator_rejects_source_report_and_action_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = build_worktree_reconciliation_plan(
                Config.load(str(write_config(root)))
            )
            authority_tamper = copy.deepcopy(report)
            authority_tamper["authority"]["repair_supported"] = True
            with self.assertRaises(WorktreeReconciliationPlanValidationError):
                validate_worktree_reconciliation_plan(authority_tamper)

            source_tamper = copy.deepcopy(report)
            source_tamper["report_digest"] = "sha256:" + "0" * 64
            with self.assertRaises(WorktreeReconciliationPlanValidationError):
                validate_worktree_reconciliation_plan(source_tamper)

    def test_source_digest_tamper_is_rejected_even_with_report_redigested(self) -> None:
        example = (
            Path(__file__).parents[1]
            / "examples"
            / "worktree-reconciliation-plan-v1.example.json"
        )
        report = json.loads(example.read_text(encoding="utf-8"))
        report["items"][0]["source_snapshot_digest"] = "sha256:" + "0" * 64
        from codex_batch_runner.worktree_reconciliation import _digest

        report["report_digest"] = _digest(
            {key: value for key, value in report.items() if key != "report_digest"}
        )
        with self.assertRaises(WorktreeReconciliationPlanValidationError):
            validate_worktree_reconciliation_plan(report)

    def test_action_tamper_is_rejected_even_with_report_redigested(self) -> None:
        example = (
            Path(__file__).parents[1]
            / "examples"
            / "worktree-reconciliation-plan-v1.example.json"
        )
        report = json.loads(example.read_text(encoding="utf-8"))
        report["items"][0]["action_class"] = "manual_review"
        from codex_batch_runner.worktree_reconciliation import _digest

        report["report_digest"] = _digest(
            {key: value for key, value in report.items() if key != "report_digest"}
        )
        with self.assertRaises(WorktreeReconciliationPlanValidationError):
            validate_worktree_reconciliation_plan(report)

    def test_cli_exact_task_project_filter_and_no_apply_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            config.queue_dir.mkdir(parents=True)
            save_task(
                config,
                {
                    "id": "plain",
                    "project_id": "example-project",
                    "status": "completed",
                },
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--config",
                        str(config_path),
                        "worktree",
                        "reconciliation-plan",
                        "plain",
                        "--project",
                        "example-project",
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
                        "reconciliation-plan",
                        "--apply",
                    ]
                )


if __name__ == "__main__":
    unittest.main()

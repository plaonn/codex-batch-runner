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
from codex_batch_runner.queue import create_task as create_queue_task
from codex_batch_runner.queue import load_task
from codex_batch_runner.worktree_reconciliation import (
    WorktreeReconciliationPlanValidationError,
    _digest,
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
                "worktree_root": str(root),
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


def save_pool_state(
    root: Path,
    repo: Path,
    *,
    status: str,
    task_id: str | None,
    branch: str | None,
    last_released_at: str | None = None,
) -> None:
    (root / ".pool-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "slots": [
                    {
                        "slot_id": "slot-01",
                        "repo_root": str(repo.resolve()),
                        "path": str((root / "pool-slot").resolve()),
                        "policy_fingerprint": "policy-v1",
                        "status": status,
                        "task_id": task_id,
                        "branch": branch,
                        "last_released_at": last_released_at,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def make_exact_candidate(
    config: Config,
    repo: Path,
    base: str,
    *,
    task_id: str,
) -> dict[str, object]:
    task, worktree = create_task(config, repo, base, task_id=task_id)
    head = str(task["execution_branch_head"])
    git(repo, "merge", "--ff-only", str(task["execution_branch"]))
    git(repo, "worktree", "remove", str(worktree))
    task.update(
        {
            "review_status": "accepted",
            "execution_apply_status": "applied",
            "execution_applied_head": head,
            "execution_applied_at": "2030-01-01T00:00:00Z",
            "execution_apply_target": "main",
            "execution_cleanup_kind": "applied",
            "execution_cleanup_reason": "execution_apply_status=applied",
            "execution_cleanup_branch_retained": True,
            "execution_cleanup_result_applied": True,
            "execution_cleaned_at": "2030-01-01T00:01:00Z",
        }
    )
    save_task(config, task)
    return task


def redigest(report: dict[str, object]) -> None:
    report["report_digest"] = _digest(
        {key: value for key, value in report.items() if key != "report_digest"}
    )


def run_cli(args: list[str]) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(args)
    return code, json.loads(output.getvalue())


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
            terminal["review_status"] = "rejected"
            terminal["execution_worktree_status"] = "cleaned"
            terminal["execution_cleanup_kind"] = "discard"
            terminal["execution_cleanup_reason"] = "review_status=rejected"
            terminal["execution_cleanup_branch_retained"] = True
            terminal["execution_cleanup_result_applied"] = False
            terminal["execution_cleaned_at"] = "2030-01-01T00:00:00Z"
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
            task["execution_hibernation_kind"] = "disposable"
            task["execution_hibernation_base_head"] = base
            task["execution_hibernation_branch_head"] = task["execution_branch_head"]
            task["execution_hibernated_at"] = "2030-01-01T00:00:00Z"
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
            task = make_exact_candidate(config, repo, base, task_id="metadata-only")
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
            self.assertNotIn(str(root / "worktree-metadata-only"), rendered)
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
        redigest(report)
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
        redigest(report)
        with self.assertRaises(WorktreeReconciliationPlanValidationError):
            validate_worktree_reconciliation_plan(report)

    def test_nonexistent_base_cannot_be_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="nonexistent-base")
            task["execution_base_head"] = "a" * 40
            save_task(config, task)
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual(
                "unrecoverable_without_owner_decision", item["action_class"]
            )
            self.assertIn("base_head_not_current", item["reason_codes"])
            self.assertIsNone(
                item["source_snapshot"]["git_observations"]["observed_base_head"]
            )

    def test_non_ancestor_base_cannot_be_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="unrelated-base")
            tree = git(repo, "rev-parse", f"{base}^{{tree}}")
            unrelated = git(repo, "commit-tree", tree, "-m", "unrelated base")
            task["execution_base_head"] = unrelated
            save_task(config, task)
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual(
                "unrecoverable_without_owner_decision", item["action_class"]
            )
            self.assertIn("base_not_ancestor_of_checkpoint", item["reason_codes"])
            self.assertFalse(
                item["source_snapshot"]["git_observations"][
                    "base_is_ancestor_of_checkpoint"
                ]
            )

    def test_missing_applied_at_cannot_be_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(
                config, repo, base, task_id="missing-applied-at"
            )
            task.pop("execution_applied_at")
            save_task(config, task)
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual("manual_review", item["action_class"])
            self.assertIn("apply_containment_unproven", item["reason_codes"])
            self.assertIsNone(
                item["source_snapshot"]["apply_evidence"]["applied_at_digest"]
            )

    def test_exact_applied_cleanup_receipt_allows_terminal_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="applied-cleaned")
            task["execution_worktree_status"] = "cleaned"
            save_task(config, task)
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual("no_action", item["action_class"])
            self.assertEqual(
                "terminal_cleanup_current",
                item["derived"]["reconciliation_status"],
            )

    def test_released_pool_cleanup_can_be_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="pooled-released")
            task.update(
                {
                    "execution_worktree_pool": True,
                    "execution_worktree_pool_slot_id": "slot-01",
                    "execution_worktree_policy_fingerprint": "policy-v1",
                    "execution_worktree_lease_status": "released",
                    "execution_worktree_pool_released_at": "2030-01-01T00:02:00Z",
                }
            )
            pool_slot = root / "pool-slot"
            git(repo, "worktree", "add", "--detach", str(pool_slot), base)
            task["execution_worktree_path"] = str(pool_slot.resolve())
            save_task(config, task)
            save_pool_state(
                root,
                repo,
                status="idle",
                task_id=None,
                branch=None,
                last_released_at="2030-01-01T00:02:00Z",
            )
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual("exact_repair_candidate", item["action_class"])
            self.assertEqual("released", item["derived"]["pool_consistency"])
            rendered = json.dumps(item)
            self.assertNotIn("slot-01", rendered)
            self.assertNotIn("policy-v1", rendered)

    def test_real_pooled_cleanup_is_terminal_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _ = create_repo(root)
            (repo / ".cbr.toml").write_text(
                """
[worktree]
copy = []
retain = []

[worktree.pool]
max_slots = 1
idle_ttl_hours = 24
""",
                encoding="utf-8",
            )
            git(repo, "add", ".cbr.toml")
            git(repo, "commit", "-m", "add pool policy")
            config_path = write_config(root)
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
            config_data["worktree_mode"] = "task"
            config_path.write_text(json.dumps(config_data), encoding="utf-8")
            config = Config.load(str(config_path))
            create_queue_task(
                config,
                "owner pool cleanup",
                str(repo),
                task_id="pooled-owner-cleanup",
            )
            prepare_code, _ = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "prepare",
                    "pooled-owner-cleanup",
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(0, prepare_code)
            task = load_task(config, "pooled-owner-cleanup")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["execution_mutation_provenance_history"] = [{"fixture": True}]
            save_task(config, task)
            cleanup_code, _ = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "cleanup",
                    "pooled-owner-cleanup",
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(0, cleanup_code)
            cleaned = load_task(config, "pooled-owner-cleanup")
            slot = Path(str(cleaned["execution_worktree_path"]))
            self.assertEqual("cleaned", cleaned["execution_worktree_status"])
            self.assertEqual("released", cleaned["execution_worktree_lease_status"])
            self.assertTrue(slot.is_dir())

            task_path = config.queue_dir / "pooled-owner-cleanup.json"
            pool_path = root / ".pool-state.json"
            before = (
                task_path.read_bytes(),
                pool_path.read_bytes(),
                git(repo, "worktree", "list", "--porcelain"),
            )
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            self.assertEqual(before[0], task_path.read_bytes())
            self.assertEqual(before[1], pool_path.read_bytes())
            self.assertEqual(before[2], git(repo, "worktree", "list", "--porcelain"))
            item = report["items"][0]
            self.assertEqual("no_action", item["action_class"])
            self.assertEqual(
                "terminal_cleanup_current",
                item["derived"]["reconciliation_status"],
            )
            self.assertEqual(
                item["source_snapshot"]["git_observations"]["path_registry_ref"],
                item["source_snapshot"]["pool_evidence"]["observed_pool_path_ref"],
            )
            self.assertIsNone(
                item["source_snapshot"]["git_observations"]["branch_registry_ref"]
            )

            tampered = copy.deepcopy(report)
            tampered_item = tampered["items"][0]
            tampered_item["source_snapshot"]["pool_evidence"][
                "observed_pool_path_ref"
            ] = "path:" + "0" * 16
            tampered_item["source_snapshot_digest"] = _digest(
                tampered_item["source_snapshot"]
            )
            redigest(tampered)
            with self.assertRaises(WorktreeReconciliationPlanValidationError):
                validate_worktree_reconciliation_plan(tampered)

            pool_state = json.loads(pool_path.read_text(encoding="utf-8"))
            pool_state["slots"][0]["path"] = str(root / "mismatched-slot")
            pool_path.write_text(json.dumps(pool_state), encoding="utf-8")
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                mismatch = build_worktree_reconciliation_plan(config)["items"][0]
            self.assertEqual("manual_review", mismatch["action_class"])
            self.assertEqual(
                "cleanup_evidence_invalid",
                mismatch["derived"]["reconciliation_status"],
            )

    def test_leased_or_ambiguous_pool_blocks_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="pooled-conflict")
            task.update(
                {
                    "execution_worktree_pool": True,
                    "execution_worktree_pool_slot_id": "slot-01",
                    "execution_worktree_policy_fingerprint": "policy-v1",
                    "execution_worktree_lease_status": "leased",
                }
            )
            save_task(config, task)
            save_pool_state(
                root,
                repo,
                status="leased",
                task_id="pooled-conflict",
                branch=str(task["execution_branch"]),
            )
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                leased = build_worktree_reconciliation_plan(config)["items"][0]
            self.assertEqual("manual_review", leased["action_class"])
            self.assertIn("active_pool_lease", leased["reason_codes"])

            save_pool_state(
                root,
                repo,
                status="leased",
                task_id="different-owner",
                branch=str(task["execution_branch"]),
            )
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                conflicting = build_worktree_reconciliation_plan(config)["items"][0]
            self.assertEqual("manual_review", conflicting["action_class"])
            self.assertIn("pool_evidence_ambiguous", conflicting["reason_codes"])

            task.pop("execution_worktree_pool_slot_id")
            save_task(config, task)
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                ambiguous = build_worktree_reconciliation_plan(config)["items"][0]
            self.assertEqual("manual_review", ambiguous["action_class"])
            self.assertIn("pool_evidence_ambiguous", ambiguous["reason_codes"])

    def test_non_pool_row_rejects_injected_pool_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="pool-injection")
            task["execution_worktree_lease_status"] = "released"
            task["execution_worktree_pool_released_at"] = "2030-01-01T00:02:00Z"
            save_task(config, task)
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                item = build_worktree_reconciliation_plan(config)["items"][0]
            self.assertEqual("manual_review", item["action_class"])
            self.assertEqual("ambiguous", item["derived"]["pool_consistency"])

    def test_pool_evidence_changes_source_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="pool-digest")
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                baseline = build_worktree_reconciliation_plan(config)["items"][0][
                    "source_snapshot_digest"
                ]
                for field, value in (
                    ("execution_worktree_pool", True),
                    ("execution_worktree_pool_slot_id", "slot-01"),
                    ("execution_worktree_policy_fingerprint", "policy-v1"),
                    ("execution_worktree_lease_status", "released"),
                    (
                        "execution_worktree_pool_released_at",
                        "2030-01-01T00:02:00Z",
                    ),
                ):
                    changed = copy.deepcopy(task)
                    changed[field] = value
                    save_task(config, changed)
                    digest = build_worktree_reconciliation_plan(config)["items"][0][
                        "source_snapshot_digest"
                    ]
                    self.assertNotEqual(baseline, digest, field)

    def test_real_cleanup_then_branch_prune_is_terminal_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            repo, base = create_repo(root)
            task, _ = create_task(config, repo, base, task_id="owner-pruned")
            head = str(task["execution_branch_head"])
            git(repo, "merge", "--ff-only", str(task["execution_branch"]))
            task.update(
                {
                    "review_status": "accepted",
                    "execution_apply_status": "applied",
                    "execution_applied_head": head,
                    "execution_applied_at": "2030-01-01T00:00:00Z",
                    "execution_apply_target": "main",
                }
            )
            save_task(config, task)
            cleanup_code, _ = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "cleanup",
                    "owner-pruned",
                    "--apply",
                    "--json",
                ]
            )
            prune_code, _ = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "branch-prune",
                    "owner-pruned",
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(0, cleanup_code)
            self.assertEqual(0, prune_code)
            report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual("no_action", item["action_class"])
            self.assertEqual(
                "terminal_cleanup_current",
                item["derived"]["reconciliation_status"],
            )
            self.assertFalse(
                item["source_snapshot"]["cleanup_evidence"]["branch_retained"]
            )
            self.assertEqual(
                "pruned",
                item["source_snapshot"]["branch_prune_evidence"]["status"],
            )

    def test_conflict_fix_shaped_cleanup_is_terminal_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="conflict-parent")
            (repo / "conflict-fix.txt").write_text("ported\n", encoding="utf-8")
            git(repo, "add", "conflict-fix.txt")
            git(repo, "commit", "-m", "apply conflict fix")
            applied_head = git(repo, "rev-parse", "HEAD")
            task.update(
                {
                    "execution_worktree_status": "cleaned",
                    "execution_applied_head": applied_head,
                    "execution_apply_via_task_id": "conflict-fix-child",
                    "execution_conflict_fix_status": "applied",
                    "execution_conflict_fix_task_id": "conflict-fix-child",
                    "execution_conflict_fix_queued_at": "2030-01-01T00:00:30Z",
                    "chain_status": "accepted",
                }
            )
            save_task(config, task)
            report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertNotEqual(
                task["execution_branch_head"], task["execution_applied_head"]
            )
            self.assertEqual("no_action", item["action_class"])
            self.assertEqual(
                "terminal_cleanup_current",
                item["derived"]["reconciliation_status"],
            )

    def test_malformed_conflict_fix_or_prune_receipt_is_not_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(
                config, repo, base, task_id="malformed-owner-receipt"
            )
            (repo / "malformed-fix.txt").write_text("ported\n", encoding="utf-8")
            git(repo, "add", "malformed-fix.txt")
            git(repo, "commit", "-m", "malformed conflict fix")
            task.update(
                {
                    "execution_worktree_status": "cleaned",
                    "execution_applied_head": git(repo, "rev-parse", "HEAD"),
                    "execution_apply_via_task_id": "child-a",
                    "execution_conflict_fix_status": "applied",
                    "execution_conflict_fix_task_id": "child-b",
                    "execution_conflict_fix_queued_at": "2030-01-01T00:00:30Z",
                    "chain_status": "accepted",
                }
            )
            save_task(config, task)
            conflict_item = build_worktree_reconciliation_plan(config)["items"][0]
            self.assertEqual("manual_review", conflict_item["action_class"])

            task["execution_apply_via_task_id"] = None
            task["execution_conflict_fix_status"] = None
            task["execution_conflict_fix_task_id"] = None
            task["execution_conflict_fix_queued_at"] = None
            task["chain_status"] = None
            task["execution_cleanup_branch_retained"] = False
            task["execution_branch_prune_status"] = "pruned"
            task["execution_branch_prune_reason"] = "execution_apply_status=applied"
            task["execution_branch_pruned_at"] = "2030-01-01T00:03:00Z"
            task["execution_branch_pruned_head"] = "c" * 40
            git(repo, "branch", "-D", str(task["execution_branch"]))
            save_task(config, task)
            prune_item = build_worktree_reconciliation_plan(config)["items"][0]
            self.assertEqual("manual_review", prune_item["action_class"])

    def test_cleaned_enum_without_terminal_receipt_is_not_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task, worktree = create_task(
                config, repo, base, task_id="cleaned-enum-only"
            )
            git(repo, "worktree", "remove", str(worktree))
            task["execution_worktree_status"] = "cleaned"
            save_task(config, task)
            report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual("manual_review", item["action_class"])
            self.assertIn("invalid_terminal_cleanup_evidence", item["reason_codes"])

    def test_malformed_hibernation_is_not_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task, worktree = create_task(
                config, repo, base, task_id="malformed-hibernation"
            )
            git(repo, "worktree", "remove", str(worktree))
            task["execution_worktree_status"] = "hibernated"
            task["execution_hibernation_contract"] = "worktree-hibernation-v1"
            task["execution_hibernation_kind"] = "disposable"
            task["execution_hibernation_base_head"] = base
            task["execution_hibernation_branch_head"] = "b" * 40
            task["execution_hibernated_at"] = "2030-01-01T00:00:00Z"
            save_task(config, task)
            report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            self.assertEqual("manual_review", item["action_class"])
            self.assertIn("malformed_hibernation_evidence", item["reason_codes"])

    def test_attached_current_derived_tamper_is_rejected_after_redigest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            _, worktree = create_task(config, repo, base, task_id="missing")
            git(repo, "worktree", "remove", str(worktree))
            report = build_worktree_reconciliation_plan(config)
            report["items"][0]["derived"]["reconciliation_status"] = "attached_current"
            redigest(report)
            with self.assertRaises(WorktreeReconciliationPlanValidationError):
                validate_worktree_reconciliation_plan(report)

    def test_cleanup_eligibility_tamper_is_rejected_after_redigest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            make_exact_candidate(config, repo, base, task_id="cleanup-tamper")
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            report["items"][0]["derived"]["cleanup_eligibility"] = "discard"
            redigest(report)
            with self.assertRaises(WorktreeReconciliationPlanValidationError):
                validate_worktree_reconciliation_plan(report)

    def test_provenance_derived_tamper_is_rejected_after_redigest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            _, worktree = create_task(config, repo, base, task_id="provenance-tamper")
            git(repo, "worktree", "remove", str(worktree))
            report = build_worktree_reconciliation_plan(config)
            report["items"][0]["derived"]["provenance_status"] = "complete"
            redigest(report)
            with self.assertRaises(WorktreeReconciliationPlanValidationError):
                validate_worktree_reconciliation_plan(report)

    def test_containment_source_inconsistency_is_rejected_after_redigest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            make_exact_candidate(config, repo, base, task_id="containment-tamper")
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                report = build_worktree_reconciliation_plan(config)
            item = report["items"][0]
            item["source_snapshot"]["apply_evidence"]["applied_head"] = None
            item["source_snapshot_digest"] = _digest(item["source_snapshot"])
            redigest(report)
            with self.assertRaises(WorktreeReconciliationPlanValidationError):
                validate_worktree_reconciliation_plan(report)

    def test_relevant_canonical_evidence_changes_source_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="digest-binding")
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ):
                baseline = build_worktree_reconciliation_plan(config)["items"][0][
                    "source_snapshot_digest"
                ]
                for field, value in (
                    ("resolution", "manual"),
                    ("chain_status", "needs_fix"),
                    ("execution_applied_at", "2030-01-02T00:00:00Z"),
                    ("execution_cleaned_at", "2030-01-02T00:00:00Z"),
                ):
                    changed = copy.deepcopy(task)
                    changed[field] = value
                    save_task(config, changed)
                    digest = build_worktree_reconciliation_plan(config)["items"][0][
                        "source_snapshot_digest"
                    ]
                    self.assertNotEqual(baseline, digest, field)
                changed = copy.deepcopy(task)
                changed["execution_mutation_provenance_history"].append(
                    {"fixture": "changed"}
                )
                save_task(config, changed)
                digest = build_worktree_reconciliation_plan(config)["items"][0][
                    "source_snapshot_digest"
                ]
                self.assertNotEqual(baseline, digest, "provenance")

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

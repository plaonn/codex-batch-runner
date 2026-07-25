from __future__ import annotations

import copy
import socket
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_batch_runner.config import Config
from codex_batch_runner.execution_delegation import (
    ExecutionDelegationError,
    append_preexecution_delegation_receipt,
    build_execution_delegation_contract,
    preexecution_delegation_view,
    record_delegation_recovery,
    record_pre_worker_snapshot_phase,
    require_preexecution_delegation_receipt,
    validate_execution_delegation_contract,
    validate_preexecution_delegation_receipt,
)
from codex_batch_runner.execution_mutation_provenance import (
    attach_execution_mutation_snapshot,
    capture_execution_mutation_snapshot,
)
from codex_batch_runner.execution_report import build_execution_report
from codex_batch_runner.lock import FileLock
from codex_batch_runner.model_requirements import ResolvedExecutionConfig
from codex_batch_runner.queue import (
    create_task,
    load_task,
    recover_stale_running_tasks,
    save_delegation_transition_locked,
    save_task,
)


def repository(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "public@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Public Test"],
        check=True,
    )
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)


def contract(task_id: str = "delegated-task") -> dict:
    return build_execution_delegation_contract(
        task_id=task_id,
        task_revision="public-task-r1",
        task_class="bounded-write-isolated",
        issuer_source_kind="adopted-task-contract",
        authority_revision="public-authority-r1",
        policy_revision="public-policy-r1",
        execution_revision="public-execution-r1",
        review_revision="public-review-r1",
        side_effect_boundary={
            "cbr_controlled_repository_write_allowed": True,
            "external_state_mutation_allowed": False,
            "credential_access_allowed": False,
            "deployment_or_publication_allowed": False,
            "destructive_action_allowed": False,
        },
    )


def settings() -> ResolvedExecutionConfig:
    return ResolvedExecutionConfig(
        requirement_vector={"schema_version": 1},
        selection_rule="execution-target-selector-v1",
        selection_reason="public-static-selection",
        model="public-model-v1",
        model_source="target-alias",
        execution_target="exact-target-v1",
        config_overrides={"model_reasoning_effort": "high"},
        worker_role="implementer",
        selected_target_snapshot={
            "target_id": "exact-target-v1",
            "target": {
                "target_id": "exact-target-v1",
                "worker_family": "public-worker-family",
                "worker_id": "public-worker-v1",
                "execution_surface": "codex",
                "execution_backend": "codex",
            },
            "inventory_schema_version": 1,
            "inventory_snapshot_id": "sha256:public-inventory",
            "constraint_registry_version": "public-constraints-v1",
            "selection_policy_version": "execution-target-selector-v1",
        },
    )


def running_task(root: Path, *, attempt: int = 1, claim_id: str = "claim-r1") -> dict:
    return {
        "id": "delegated-task",
        "status": "running",
        "attempts": attempt,
        "run_count": attempt,
        "active_run_id": claim_id,
        "execution_backend": "codex",
        "worker_target": "exact-target-v1",
        "execution_delegation_contract": contract(),
        "preexecution_delegation_receipt_history": [],
        "preexecution_delegation_phase_history": [],
        "execution_mutation_snapshot_history": [],
        "execution_worktree_path": str(root),
    }


def bind_pre_worker(task: dict, root: Path, receipt: dict) -> None:
    snapshot = capture_execution_mutation_snapshot(task, root, phase="pre_worker")
    attach_execution_mutation_snapshot(task, snapshot)
    record_pre_worker_snapshot_phase(
        task,
        receipt_id=receipt["receipt_id"],
        snapshot_id=snapshot["snapshot_id"],
    )


class ExecutionDelegationTests(unittest.TestCase):
    def test_contract_is_deterministic_strict_and_public_safe(self) -> None:
        first = contract()
        self.assertEqual(first, contract())
        self.assertFalse(first["issuer"]["external_issuer_authenticated"])
        self.assertEqual("unknown", first["scope"]["global_provenance"])

        tampered = copy.deepcopy(first)
        tampered["binding"]["task_revision"] = "other"
        with self.assertRaisesRegex(ExecutionDelegationError, "contract_digest"):
            validate_execution_delegation_contract(tampered)

        private = copy.deepcopy(first)
        private["issuer"]["thread_id"] = "private"
        with self.assertRaisesRegex(ExecutionDelegationError, "issuer"):
            validate_execution_delegation_contract(private)

    def test_contract_is_admitted_before_create_and_immutable_after_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            cfg = Config.load(root=root)
            created = create_task(
                cfg,
                "public bounded objective",
                str(root),
                task_id="delegated-task",
                execution_delegation_contract=contract(),
            )
            self.assertEqual(contract(), created["execution_delegation_contract"])

            created["execution_delegation_contract"] = None
            with self.assertRaisesRegex(ValueError, "immutable after enqueue"):
                save_task(cfg, created)

            legacy = create_task(
                cfg, "legacy objective", str(root), task_id="legacy-task"
            )
            legacy["execution_delegation_contract"] = contract("legacy-task")
            with self.assertRaisesRegex(ValueError, "immutable after enqueue"):
                save_task(cfg, legacy)
            self.assertIsNone(
                load_task(cfg, "legacy-task")["execution_delegation_contract"]
            )

    def test_receipt_binds_exact_target_and_requires_pre_worker_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            task = running_task(root)
            receipt = append_preexecution_delegation_receipt(
                task, execution_settings=settings(), active_run_id="claim-r1"
            )
            assert receipt is not None
            self.assertEqual(
                "exact-target-v1", receipt["target"]["target_id"]
            )
            self.assertNotIn("worker_id", receipt["target"])
            self.assertTrue(
                receipt["target"]["worker_identity_digest"].startswith(
                    "sha256:"
                )
            )
            self.assertEqual(
                receipt,
                append_preexecution_delegation_receipt(
                    task, execution_settings=settings(), active_run_id="claim-r1"
                ),
            )
            with self.assertRaisesRegex(
                ExecutionDelegationError, "worker invocation blocked"
            ):
                require_preexecution_delegation_receipt(task)

            bind_pre_worker(task, root, receipt)
            require_preexecution_delegation_receipt(task)
            view = preexecution_delegation_view(task)
            self.assertEqual(
                "verified-local-preexecution-binding", view["status"]
            )
            self.assertFalse(view["scope"]["external_issuer_authenticated"])
            self.assertEqual("unknown", view["scope"]["global_provenance"])

    def test_target_backend_tamper_and_receipt_substitution_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            task = running_task(root)
            divergent = settings()
            assert divergent.selected_target_snapshot is not None
            divergent.selected_target_snapshot["target"]["target_id"] = (
                "different-target"
            )
            with self.assertRaisesRegex(
                ExecutionDelegationError, "identifiers are divergent"
            ):
                append_preexecution_delegation_receipt(
                    task,
                    execution_settings=divergent,
                    active_run_id="claim-r1",
                )

            task = running_task(root)
            task["orchestration_dispatch_receipt"] = {"status": "accepted"}
            self.assertEqual(
                "insufficient", preexecution_delegation_view(task)["status"]
            )

            task = running_task(root)
            receipt = append_preexecution_delegation_receipt(
                task, execution_settings=settings(), active_run_id="claim-r1"
            )
            assert receipt is not None
            tampered = copy.deepcopy(receipt)
            tampered["target"]["worker_identity_digest"] = "sha256:" + "b" * 64
            with self.assertRaisesRegex(ExecutionDelegationError, "receipt_id"):
                validate_preexecution_delegation_receipt(tampered)

            future = copy.deepcopy(receipt)
            future["recorded_at"] = (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat()
            with self.assertRaisesRegex(ExecutionDelegationError, "future"):
                validate_preexecution_delegation_receipt(future)

            private_target = settings()
            assert private_target.selected_target_snapshot is not None
            private_target.selected_target_snapshot["target"]["worker_id"] = (
                "operator@example.invalid"
            )
            private_task = running_task(root)
            private_task["worker_target"] = None
            with self.assertRaisesRegex(
                ExecutionDelegationError, "public-safe identifier"
            ):
                append_preexecution_delegation_receipt(
                    private_task,
                    execution_settings=private_target,
                    active_run_id="claim-r1",
                )

    def test_receipt_only_crash_recovers_and_retry_chains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            task = running_task(root)
            first = append_preexecution_delegation_receipt(
                task, execution_settings=settings(), active_run_id="claim-r1"
            )
            assert first is not None
            record_delegation_recovery(task)
            task["attempts"] = 2
            task["run_count"] = 2
            task["active_run_id"] = "claim-r2"
            second = append_preexecution_delegation_receipt(
                task, execution_settings=settings(), active_run_id="claim-r2"
            )
            assert second is not None
            self.assertEqual(first["receipt_id"], second["sequence"]["predecessor"])
            bind_pre_worker(task, root, second)
            require_preexecution_delegation_receipt(task)

            task["active_run_id"] = "other-claim"
            self.assertEqual(
                "insufficient", preexecution_delegation_view(task)["status"]
            )

    def test_receipt_after_pre_worker_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            task = running_task(root)
            receipt = append_preexecution_delegation_receipt(
                task, execution_settings=settings(), active_run_id="claim-r1"
            )
            assert receipt is not None
            receipt["recorded_at"] = "2000-01-02T00:00:00+00:00"
            snapshot = capture_execution_mutation_snapshot(
                task,
                root,
                phase="pre_worker",
                captured_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            )
            attach_execution_mutation_snapshot(task, snapshot)
            record_pre_worker_snapshot_phase(
                task,
                receipt_id=receipt["receipt_id"],
                snapshot_id=snapshot["snapshot_id"],
            )
            view = preexecution_delegation_view(task)
            self.assertEqual("insufficient", view["status"])
            self.assertIn(
                "delegation_receipt_was_recorded_after_pre_worker_snapshot",
                view["insufficiency_reasons"],
            )

    def test_duplicate_divergent_and_incomplete_attempts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            task = running_task(root)
            receipt = append_preexecution_delegation_receipt(
                task, execution_settings=settings(), active_run_id="claim-r1"
            )
            assert receipt is not None
            with self.assertRaisesRegex(ExecutionDelegationError, "divergent"):
                changed = settings()
                assert changed.selected_target_snapshot is not None
                changed.selected_target_snapshot["target"]["worker_family"] = (
                    "other-family"
                )
                append_preexecution_delegation_receipt(
                    task,
                    execution_settings=changed,
                    active_run_id="claim-r1",
                )

            task["preexecution_delegation_receipt_history"].append(
                copy.deepcopy(receipt)
            )
            self.assertEqual(
                "insufficient", preexecution_delegation_view(task)["status"]
            )

            resumed = running_task(root, attempt=2, claim_id="claim-r2")
            with self.assertRaisesRegex(
                ExecutionDelegationError, "prior delegation receipt"
            ):
                append_preexecution_delegation_receipt(
                    resumed,
                    execution_settings=settings(),
                    active_run_id="claim-r2",
                )

    def test_receipt_only_crash_is_visible_to_diagnostic_not_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            cfg = Config.load(root=root)
            task = create_task(
                cfg,
                "public bounded objective",
                str(root),
                task_id="delegated-task",
                execution_delegation_contract=contract(),
            )
            task.update(
                {
                    "status": "running",
                    "attempts": 1,
                    "run_count": 1,
                    "active_run_id": "claim-r1",
                    "execution_backend": "codex",
                }
            )
            append_preexecution_delegation_receipt(
                task, execution_settings=settings(), active_run_id="claim-r1"
            )
            with FileLock(cfg.lock_file, cfg.stale_lock_seconds):
                save_delegation_transition_locked(cfg, task)

            diagnostic = build_execution_report(cfg, purpose="diagnostic")
            routing = build_execution_report(cfg, purpose="routing")
            self.assertEqual(1, diagnostic["row_count"])
            self.assertEqual(
                "insufficient",
                diagnostic["rows"][0]["preexecution_delegation"]["status"],
            )
            self.assertEqual(0, routing["row_count"])

    def test_terminal_posthoc_receipt_cannot_enter_canonical_task_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            cfg = Config.load(root=root)
            task = create_task(
                cfg,
                "public bounded objective",
                str(root),
                task_id="delegated-task",
                execution_delegation_contract=contract(),
            )
            task.update(
                {
                    "status": "completed",
                    "attempts": 1,
                    "run_count": 1,
                    "active_run_id": "forged-claim",
                    "execution_backend": "codex",
                }
            )
            receipt = append_preexecution_delegation_receipt(
                task,
                execution_settings=settings(),
                active_run_id="forged-claim",
            )
            assert receipt is not None
            bind_pre_worker(task, root, receipt)

            with self.assertRaisesRegex(ValueError, "runner-owned"):
                save_task(cfg, task)
            with self.assertRaisesRegex(
                ValueError, "delegation history transition"
            ):
                with FileLock(cfg.lock_file, cfg.stale_lock_seconds):
                    save_delegation_transition_locked(cfg, task)
            persisted = load_task(cfg, "delegated-task")
            self.assertEqual(
                [], persisted["preexecution_delegation_receipt_history"]
            )

    def test_canonical_receipt_only_crash_recovery_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            cfg = Config.load(root=root)
            task = create_task(
                cfg,
                "public bounded objective",
                str(root),
                task_id="delegated-task",
                execution_delegation_contract=contract(),
            )
            task.update(
                {
                    "status": "running",
                    "attempts": 1,
                    "run_count": 1,
                    "active_run_id": "claim-r1",
                    "active_runner_hostname": socket.gethostname(),
                    "active_runner_pid": 999_999_999,
                    "started_at": "2000-01-01T00:00:00+00:00",
                    "execution_backend": "codex",
                }
            )
            append_preexecution_delegation_receipt(
                task,
                execution_settings=settings(),
                active_run_id="claim-r1",
            )
            with FileLock(cfg.lock_file, cfg.stale_lock_seconds):
                save_delegation_transition_locked(cfg, task)

            with FileLock(cfg.lock_file, cfg.stale_lock_seconds):
                self.assertEqual(
                    ["delegated-task"], recover_stale_running_tasks(cfg)
                )
            recovered = load_task(cfg, "delegated-task")
            self.assertEqual("runnable", recovered["status"])
            self.assertNotIn("active_run_id", recovered)
            self.assertEqual(
                [
                    "preexecution_receipt_appended",
                    "attempt_recovered_before_pre_worker",
                ],
                [
                    item["phase"]
                    for item in recovered[
                        "preexecution_delegation_phase_history"
                    ]
                ],
            )

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from codex_batch_runner.config import Config
from codex_batch_runner.execution_report import build_execution_report
from codex_batch_runner.execution_delegation import (
    append_preexecution_delegation_receipt,
    build_execution_delegation_contract,
    record_pre_worker_snapshot_phase,
)
from codex_batch_runner.execution_mutation_provenance import (
    attach_execution_mutation_provenance,
    build_execution_mutation_provenance,
)
from codex_batch_runner.natural_execution_attestation import (
    attach_natural_execution_attestation,
    build_natural_execution_attestation,
)
from codex_batch_runner.model_requirements import ResolvedExecutionConfig
from codex_batch_runner.lock import FileLock
from codex_batch_runner.queue import (
    create_task,
    load_task,
    save_delegation_transition_locked,
    save_task,
)
from codex_batch_runner.review_outcome_evidence import (
    attach_review_outcome_evidence,
    build_review_outcome_evidence,
)
from codex_batch_runner.scoped_readonly_certification import (
    ScopedReadonlyCertificationError,
    build_scoped_readonly_certification_report,
    build_scoped_readonly_certification_report_bundle,
    project_scoped_readonly_certification,
    validate_scoped_readonly_certification_projection,
)
from tests.test_execution_mutation_provenance import repository, snapshot
from tests.test_natural_execution_attestation import RECORDED_AT, closed_task


def delegation_settings() -> ResolvedExecutionConfig:
    return ResolvedExecutionConfig(
        requirement_vector={"schema_version": 1},
        selection_rule="execution-target-selector-v1",
        selection_reason="public-static-selection",
        model="public-model-v1",
        model_source="target-alias",
        execution_target="exact-target-v1",
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


def stable_id(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


EVALUATED_AT = RECORDED_AT + timedelta(days=1)
TARGET_SNAPSHOT_ID = stable_id(delegation_settings().selected_target_snapshot)
CANDIDATE = {
    "worker_id": "public-worker-v1",
    "target_snapshot_id": TARGET_SNAPSHOT_ID,
    "task_class": "readonly-objective",
}
MAPPING = {
    **CANDIDATE,
    "worker_family": "public-worker-family",
    "target_id": "exact-target-v1",
    "mapping_revision": "public-mapping-v1",
}


def runtime_config(root: Path) -> Config:
    exclude = root / ".git" / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8")
    if ".cbr-test/" not in existing:
        exclude.write_text(existing + "\n.cbr-test/\n", encoding="utf-8")
    return Config.load(root=root / ".cbr-test")


def sample(
    root: Path,
    index: int,
    *,
    mapping: dict | None = None,
    outcome: str = "pass",
    cbr_write_allowed: bool = False,
) -> dict:
    task_id = f"readonly-task-{index}"
    config = runtime_config(root)
    closed = closed_task(root, task_id=task_id)
    if outcome == "fail":
        closed["review_outcome_evidence_history"] = []
        attach_review_outcome_evidence(
            closed,
            build_review_outcome_evidence(
                closed,
                acceptance_method="external_review",
                accepted=False,
                objective_status="failed",
                semantic_status="needs_fix",
                reviewer_kind="external",
                reviewer_role="independent",
                decision_confidence="high",
                anchor_semantic_review=True,
                actual_identity="public-reviewer-v1",
                actual_identity_source="provider_observed",
                actual_identity_confidence="provider_observed",
                review_policy_version="review-v1",
                rubric_version="rubric-v1",
            ),
        )
    contract = build_execution_delegation_contract(
        task_id=task_id,
        task_revision="public-readonly-task-r1",
        task_class="readonly-objective",
        issuer_source_kind="adopted-task-contract",
        authority_revision="public-readonly-authority-r1",
        policy_revision="public-readonly-policy-r1",
        execution_revision="public-readonly-execution-r1",
        review_revision="public-readonly-review-r1",
        side_effect_boundary={
            "cbr_controlled_repository_write_allowed": cbr_write_allowed,
            "external_state_mutation_allowed": False,
            "credential_access_allowed": False,
            "deployment_or_publication_allowed": False,
            "destructive_action_allowed": False,
        },
    )
    value = create_task(
        config,
        "public readonly objective",
        str(root),
        task_id=task_id,
        execution_delegation_contract=contract,
    )
    value.update(
        {
            "execution_mode": "git_worktree",
            "execution_worktree_path": str(root),
            "execution_worktree_status": "retained",
            "execution_branch": "main",
            "execution_base_head": (
                subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                .stdout.strip()
            ),
        }
    )
    value["status"] = "running"
    value["attempts"] = 1
    value["run_count"] = 1
    value["active_run_id"] = f"claim-{index}"
    value["execution_backend"] = "codex"
    value["worker_target"] = "exact-target-v1"
    value["active_execution_target_snapshot"] = (
        delegation_settings().selected_target_snapshot
    )
    receipt = append_preexecution_delegation_receipt(
        value,
        execution_settings=delegation_settings(),
        active_run_id=f"claim-{index}",
    )
    assert receipt is not None
    with FileLock(config.lock_file, config.stale_lock_seconds):
        save_delegation_transition_locked(config, value)
    snapshot(value, root, "pre_worker", at=RECORDED_AT - timedelta(minutes=3))
    pre_worker = value["execution_mutation_snapshot_history"][-1]
    record_pre_worker_snapshot_phase(
        value,
        receipt_id=receipt["receipt_id"],
        snapshot_id=pre_worker["snapshot_id"],
    )
    with FileLock(config.lock_file, config.stale_lock_seconds):
        save_delegation_transition_locked(config, value)
    for field in (
        "review_policy_version",
        "review_rubric_version",
        "last_run",
        "execution_evidence_history",
        "review_outcome_evidence_history",
    ):
        value[field] = copy.deepcopy(closed.get(field))
    snapshot(
        value,
        root,
        "post_worker_pre_cbr_commit",
        at=RECORDED_AT - timedelta(minutes=2),
        reported=[],
    )
    value["status"] = "completed"
    value.pop("active_run_id", None)
    snapshot(
        value,
        root,
        "terminal_closure",
        at=RECORDED_AT - timedelta(minutes=1),
    )
    mutation = build_execution_mutation_provenance(
        value,
        recorded_at=RECORDED_AT - timedelta(seconds=30),
        producer_revision="runner-vmp-imp-1",
    )
    attach_execution_mutation_provenance(value, mutation)
    effective_mapping = mapping or MAPPING
    natural = build_natural_execution_attestation(
        value,
        mapping=effective_mapping,
        evidence_class="natural-objective-run",
        scenario="objective_outcome",
        outcome=outcome,
        mutation_provenance="unknown",
        attestor_revision="attestor-v1",
        recorded_at=RECORDED_AT,
        mutation_record=mutation,
    )
    attach_natural_execution_attestation(value, natural)
    save_task(config, value)
    return {
        "task_id": task_id,
        "natural_attestation_id": natural["attestation_id"],
        "mutation_provenance_id": mutation["provenance_id"],
        "delegation_receipt_id": receipt["receipt_id"],
    }


def conflicting_failure(root: Path, source: dict) -> dict:
    config = runtime_config(root)
    value = load_task(config, source["task_id"])
    value["review_outcome_evidence_history"] = []
    attach_review_outcome_evidence(
        value,
        build_review_outcome_evidence(
            value,
            acceptance_method="external_review",
            accepted=False,
            objective_status="failed",
            semantic_status="needs_fix",
            reviewer_kind="external",
            reviewer_role="independent",
            decision_confidence="high",
            anchor_semantic_review=True,
            actual_identity="public-reviewer-v1",
            actual_identity_source="provider_observed",
            actual_identity_confidence="provider_observed",
            review_policy_version="review-v1",
            rubric_version="rubric-v1",
        ),
    )
    mutation = value["execution_mutation_provenance_history"][-1]
    natural = build_natural_execution_attestation(
        value,
        mapping=MAPPING,
        evidence_class="natural-objective-run",
        scenario="objective_outcome",
        outcome="fail",
        mutation_provenance="unknown",
        attestor_revision="attestor-v1",
        recorded_at=RECORDED_AT,
        mutation_record=mutation,
    )
    attach_natural_execution_attestation(value, natural)
    save_task(config, value)
    return {
        **source,
        "natural_attestation_id": natural["attestation_id"],
    }


def superseding_attestation(root: Path, source: dict) -> dict:
    config = runtime_config(root)
    value = load_task(config, source["task_id"])
    mutation = value["execution_mutation_provenance_history"][-1]
    natural = build_natural_execution_attestation(
        value,
        mapping=MAPPING,
        evidence_class="natural-objective-run",
        scenario="objective_outcome",
        outcome="pass",
        mutation_provenance="unknown",
        attestor_revision="attestor-v1",
        recorded_at=RECORDED_AT + timedelta(seconds=1),
        mutation_record=mutation,
        supersedes_attestation_id=source["natural_attestation_id"],
    )
    attach_natural_execution_attestation(value, natural)
    save_task(config, value)
    return {
        **source,
        "natural_attestation_id": natural["attestation_id"],
    }


def redigest(value: dict, field: str) -> None:
    body = copy.deepcopy(value)
    body.pop(field)
    encoded = json.dumps(
        body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    value[field] = "sha256:" + hashlib.sha256(encoded).hexdigest()


class ScopedReadonlyCertificationTests(unittest.TestCase):
    def test_twenty_exact_samples_reach_scoped_advisory_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            samples = [sample(root, index) for index in range(20)]
            result = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=samples,
                evaluated_at=EVALUATED_AT,
            )
            self.assertEqual("eligible-scoped-readonly", result["status"])
            self.assertEqual(20, result["sample_count"])
            self.assertEqual(1.0, result["objective_pass_ratio"])
            self.assertEqual(0, result["adverse_count"])
            self.assertEqual("unknown", result["global_provenance"])
            self.assertFalse(result["actual_canary"])
            self.assertFalse(result["promotion_authority"])
            self.assertFalse(result["routing_mutation_allowed"])
            self.assertFalse(result["worker_selection_or_dispatch_allowed"])
            report = build_scoped_readonly_certification_report(
                result,
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=samples,
                as_of=EVALUATED_AT,
            )
            self.assertEqual("eligible-scoped-readonly", report["status"])
            self.assertEqual(CANDIDATE, report["candidate"])
            self.assertEqual(
                "cbr-controlled-task-repository-worktree", report["scope"]
            )
            bundle = build_scoped_readonly_certification_report_bundle(
                runtime_config(root),
                [
                    load_task(runtime_config(root), item["task_id"])
                    for item in samples
                ],
                as_of=EVALUATED_AT,
            )
            self.assertEqual(1, bundle["cohort_count"])
            self.assertEqual(
                "eligible-scoped-readonly",
                bundle["reports"][0]["status"],
            )
            self.assertEqual("unknown", bundle["global_provenance"])

    def test_nineteen_samples_are_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            result = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=[sample(root, index) for index in range(19)],
                evaluated_at=EVALUATED_AT,
            )
            self.assertEqual("insufficient", result["status"])
            self.assertIn("insufficient_samples", result["reasons"])

    def test_ninety_five_percent_with_one_adverse_signal_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            samples = [sample(root, index) for index in range(19)]
            samples.append(sample(root, 19, outcome="fail"))
            result = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=samples,
                evaluated_at=EVALUATED_AT,
            )
            self.assertEqual(20, result["sample_count"])
            self.assertEqual(0.95, result["objective_pass_ratio"])
            self.assertEqual(1, result["adverse_count"])
            self.assertEqual("disabled", result["status"])
            self.assertIn("adverse_signal_observed", result["reasons"])

    def test_mixed_cohort_is_disabled_without_global_semantic_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            samples = [sample(root, index) for index in range(19)]
            other = {**MAPPING, "mapping_revision": "public-mapping-v2"}
            samples.append(sample(root, 19, mapping=other))
            result = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=samples,
                evaluated_at=EVALUATED_AT,
            )
            self.assertEqual("disabled", result["status"])
            self.assertIn("mixed_cohort", result["reasons"])
            self.assertFalse(
                result["existing_global_worker_certification_semantics_changed"]
            )

    def test_conflicting_same_execution_is_order_independent_and_disabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            passes = [sample(root, index) for index in range(21)]
            failure = conflicting_failure(root, passes[0])
            first = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=[*passes, failure],
                evaluated_at=EVALUATED_AT,
            )
            second = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=[failure, *passes],
                evaluated_at=EVALUATED_AT,
            )
            for value in (first, second):
                self.assertEqual("disabled", value["status"])
                self.assertEqual(20, value["sample_count"])
                self.assertIn("conflicting_sample", value["reasons"])
            config = runtime_config(root)
            bundle = build_scoped_readonly_certification_report_bundle(
                config,
                [load_task(config, item["task_id"]) for item in passes],
                as_of=EVALUATED_AT,
            )
            self.assertEqual("disabled", bundle["reports"][0]["status"])
            self.assertIn(
                "conflicting_sample", bundle["reports"][0]["reasons"]
            )

    def test_superseded_attestation_cannot_count_as_current_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            original = sample(root, 0)
            correction = superseding_attestation(root, original)
            stale = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=[original],
                evaluated_at=EVALUATED_AT,
            )
            current = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=[correction],
                evaluated_at=EVALUATED_AT,
            )
            self.assertEqual(0, stale["sample_count"])
            self.assertEqual(1, current["sample_count"])

    def test_execution_report_limit_does_not_limit_projection_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            config = runtime_config(root)
            for task_id in ("first", "second"):
                task = create_task(
                    config, task_id, str(root), task_id=task_id
                )
                task["status"] = "completed"
                task["last_run"] = {
                    "execution_backend": "codex",
                    "command_kind": "exec",
                    "returncode": 0,
                    "started_at": "2026-07-24T00:00:00+00:00",
                    "finished_at": "2026-07-24T00:00:01+00:00",
                    "duration_seconds": 1,
                }
                save_task(config, task)
            captured: list[list[dict]] = []

            def bundle(
                _config: Config,
                tasks: list[dict],
                *,
                as_of: object,
            ) -> dict:
                del as_of
                captured.append(tasks)
                return {"contract": "test-bundle"}

            with mock.patch(
                "codex_batch_runner.execution_report."
                "build_scoped_readonly_certification_report_bundle",
                side_effect=bundle,
            ):
                report = build_execution_report(
                    config, purpose="diagnostic", limit=1
                )
            self.assertEqual(1, report["row_count"])
            self.assertEqual(2, len(captured[0]))

    def test_synthetic_provider_boundary_and_manual_shapes_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            invalid = []
            for index, evidence_class in enumerate(
                (
                "synthetic-boundary",
                "provider-observation",
                "natural-boundary-event",
                )
            ):
                item = sample(root, index)
                config = runtime_config(root)
                task = load_task(config, item["task_id"])
                task["natural_execution_attestation_history"][-1]["evidence"][
                    "class"
                ] = evidence_class
                save_task(config, task)
                invalid.append(item)
            invalid.append({"manual": True})
            result = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=invalid,
                evaluated_at=EVALUATED_AT,
            )
            self.assertEqual("insufficient", result["status"])
            self.assertEqual(0, result["sample_count"])
            self.assertEqual(4, len(result["excluded_samples"]))

    def test_expired_conflicting_missing_and_candidate_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            valid = sample(root, 0)
            expired = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=[valid],
                evaluated_at=RECORDED_AT + timedelta(days=31),
            )
            self.assertEqual(0, expired["sample_count"])
            self.assertTrue(expired["excluded_samples"])
            missing = sample(root, 1)
            config = runtime_config(root)
            missing_task = load_task(config, missing["task_id"])
            missing_task["natural_execution_attestation_history"][-1][
                "mutation_binding"
            ] = None
            save_task(config, missing_task)
            mismatch = sample(root, 2)
            mismatch_task = load_task(config, mismatch["task_id"])
            mismatch_task["natural_execution_attestation_history"][-1][
                "binding"
            ]["target_id"] = "other"
            save_task(config, mismatch_task)
            detached = copy.deepcopy(valid)
            detached["delegation_receipt_id"] = "sha256:" + "f" * 64
            result = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=[missing, mismatch, detached, valid, valid],
                evaluated_at=EVALUATED_AT,
            )
            self.assertEqual(1, result["sample_count"])
            self.assertEqual(4, len(result["excluded_samples"]))
            wrong_candidate = {**CANDIDATE, "worker_id": "other"}
            rejected = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=wrong_candidate,
                samples=[valid],
                evaluated_at=EVALUATED_AT,
            )
            self.assertEqual(0, rejected["sample_count"])

    def test_nonisolated_dirty_unsafe_resume_and_privacy_are_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            variants = []
            for index, (reason, attribution) in enumerate(
                (
                ("non_isolated_execution_root", None),
                ("pre_existing_dirt", "pre_existing_dirt"),
                ("unsafe_or_unreported_paths", "unsafe_or_unreported_paths"),
                ("resume_or_crash_gap", None),
                )
            ):
                item = sample(root, index)
                config = runtime_config(root)
                task = load_task(config, item["task_id"])
                mutation = task[
                    "execution_mutation_provenance_history"
                ][-1]
                mutation["provenance"] = (
                    "mutation_possible"
                    if reason in {"pre_existing_dirt", "unsafe_or_unreported_paths"}
                    else "unknown"
                )
                mutation["fail_closed_reasons"] = [reason]
                if attribution:
                    mutation["attribution"][attribution] = True
                save_task(config, task)
                variants.append(item)
            private = sample(root, 4)
            config = runtime_config(root)
            private_task = load_task(config, private["task_id"])
            private_task["natural_execution_attestation_history"][-1][
                "private"
            ] = {"credential": "not-public"}
            save_task(config, private_task)
            variants.append(private)
            variants.append(sample(root, 10, cbr_write_allowed=True))
            result = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=variants,
                evaluated_at=EVALUATED_AT,
            )
            self.assertEqual(0, result["sample_count"])
            self.assertEqual(6, len(result["excluded_samples"]))
            encoded = str(result)
            self.assertNotIn("not-public", encoded)
            with self.assertRaisesRegex(
                ScopedReadonlyCertificationError, "safe identifier"
            ):
                project_scoped_readonly_certification(
                    config=runtime_config(root),
                    candidate={
                        **CANDIDATE,
                        "worker_id": "operator@example.invalid",
                    },
                    samples=[],
                    evaluated_at=EVALUATED_AT,
                )

    def test_projection_digest_future_and_expiry_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            sources = [sample(root, 0)]
            value = project_scoped_readonly_certification(
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=sources,
                evaluated_at=EVALUATED_AT,
            )
            tampered = copy.deepcopy(value)
            tampered["status"] = "eligible-scoped-readonly"
            with self.assertRaisesRegex(
                ScopedReadonlyCertificationError, "status does not match"
            ):
                validate_scoped_readonly_certification_projection(tampered)
            with self.assertRaisesRegex(
                ScopedReadonlyCertificationError, "future"
            ):
                build_scoped_readonly_certification_report(
                    value,
                    config=runtime_config(root),
                    candidate=CANDIDATE,
                    samples=sources,
                    as_of=EVALUATED_AT - timedelta(seconds=1),
                )
            expired = build_scoped_readonly_certification_report(
                value,
                config=runtime_config(root),
                candidate=CANDIDATE,
                samples=sources,
                as_of=EVALUATED_AT + timedelta(days=30),
            )
            self.assertEqual("disabled", expired["status"])
            self.assertIn("projection_expired", expired["reasons"])

            forged = copy.deepcopy(value)
            forged.update(
                {
                    "status": "eligible-scoped-readonly",
                    "sample_count": 20,
                    "passed_count": 20,
                    "objective_pass_ratio": 1.0,
                    "adverse_count": 0,
                    "sample_ids": [
                        "sha256:" + f"{index:064x}" for index in range(20)
                    ],
                    "reasons": [],
                }
            )
            redigest(forged, "projection_id")
            validate_scoped_readonly_certification_projection(forged)
            with self.assertRaisesRegex(
                ScopedReadonlyCertificationError,
                "does not match verified source samples",
            ):
                build_scoped_readonly_certification_report(
                    forged,
                    config=runtime_config(root),
                    candidate=CANDIDATE,
                    samples=sources,
                    as_of=EVALUATED_AT,
                )


if __name__ == "__main__":
    unittest.main()

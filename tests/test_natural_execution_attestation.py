from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_batch_runner.config import Config
from codex_batch_runner.execution_evidence_v3 import (
    attach_execution_evidence_v3,
    build_codex_execution_evidence_v3,
)
from codex_batch_runner.execution_report import task_execution_row
from codex_batch_runner.model_requirements import ResolvedExecutionConfig
from codex_batch_runner.natural_execution_attestation import (
    CONTRACT_VERSION,
    NaturalExecutionAttestationError,
    attach_natural_boundary_event,
    attach_natural_execution_attestation,
    build_natural_boundary_event,
    build_natural_execution_attestation,
    build_natural_execution_attestation_report,
    build_worker_certification_evidence,
    natural_execution_attestation_view,
    validate_natural_execution_attestation,
)
from codex_batch_runner.review_outcome_evidence import (
    attach_review_outcome_evidence,
    build_review_outcome_evidence,
)
RECORDED_AT = datetime(2030, 1, 2, 4, 0, tzinfo=timezone.utc)
CANDIDATE = {
    "worker_id": "public-worker-v1",
    "target_snapshot_id": "public-worker-snapshot-v1",
    "task_class": "bounded-write-isolated",
}
MAPPING = {
    **CANDIDATE,
    "worker_family": "public-worker-family",
    "target_id": "exact-target-v1",
    "mapping_revision": "public-mapping-v1",
}


def settings() -> ResolvedExecutionConfig:
    return ResolvedExecutionConfig(
        requirement_vector={
            "schema_version": 2,
            "derivation_version": "requirement-rubric-v1",
            "revision_id": "public-requirement-v1",
            "quality_requirements": {},
            "hard_constraints": {},
            "utility_preferences": {},
        },
        selection_rule="execution-target-selector-v1",
        selection_reason="automatic_static_non_learned",
        model="public-model-v1",
        execution_target="exact-target-v1",
        config_overrides={"model_reasoning_effort": "high"},
        selected_target_snapshot={
            "target_id": "exact-target-v1",
            "target": {
                "target_id": "exact-target-v1",
                "execution_surface": "codex",
                "execution_backend": "codex",
                "model": "public-model-v1",
                "reasoning_effort": "high",
            },
            "inventory_schema_version": 1,
            "inventory_snapshot_id": "sha256:public-inventory",
            "constraint_registry_version": "public-constraints-v1",
            "selection_policy_version": "execution-target-selector-v1",
        },
    )


def config(root: Path) -> Config:
    return Config.load(root=root)


def closed_task(
    root: Path,
    *,
    provider_observed: bool = True,
    task_id: str = "public-natural-task",
) -> dict:
    value = {
        "id": task_id,
        "status": "completed",
        "attempts": 1,
        "review_policy_version": "review-v1",
        "review_rubric_version": "rubric-v1",
        "last_run": {},
    }
    events = (
        [{
            "type": "turn.completed",
            "model": "public-model-v1",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }]
        if provider_observed
        else []
    )
    execution = build_codex_execution_evidence_v3(
        value, SimpleNamespace(events=events), settings(), config(root)
    )
    attach_execution_evidence_v3(value, execution)
    review = build_review_outcome_evidence(
        value,
        acceptance_method="reviewer_pass",
        accepted=True,
        objective_status="passed",
        semantic_status="pass",
        reviewer_kind="codex",
        reviewer_role="independent",
        decision_confidence="high",
        anchor_semantic_review=True,
        actual_identity="public-reviewer-v1",
        actual_identity_source="provider_observed",
        actual_identity_confidence="provider_observed",
        review_policy_version="review-v1",
        rubric_version="rubric-v1",
    )
    attach_review_outcome_evidence(value, review)
    return value


def build(value: dict, **overrides: object) -> dict:
    arguments = {
        "mapping": MAPPING,
        "evidence_class": "natural-objective-run",
        "scenario": "objective_outcome",
        "outcome": "pass",
        "mutation_provenance": "unknown",
        "attestor_revision": "attestor-v1",
        "recorded_at": RECORDED_AT,
    }
    arguments.update(overrides)
    return build_natural_execution_attestation(value, **arguments)


def redigest(record: dict) -> None:
    body = copy.deepcopy(record)
    body.pop("attestation_id")
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    record["attestation_id"] = "sha256:" + hashlib.sha256(encoded).hexdigest()


def redigest_boundary(record: dict) -> None:
    body = copy.deepcopy(record)
    body.pop("event_id")
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    record["event_id"] = "sha256:" + hashlib.sha256(encoded).hexdigest()


class NaturalExecutionAttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict(
            "os.environ", {"CBR_CONFIG": ""}, clear=False
        )
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

    def test_exact_closure_builds_stable_report_only_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = closed_task(root)
            record = build(value)
            attach_natural_execution_attestation(value, record)
            row = task_execution_row(config(root), value, as_of=RECORDED_AT)

        self.assertEqual(CONTRACT_VERSION, record["contract"])
        self.assertEqual(
            value["last_run"]["execution_evidence_id"],
            record["binding"]["execution_evidence_id"],
        )
        self.assertEqual(
            value["review_outcome_evidence_history"][-1]["evidence_id"],
            record["review"]["evidence_id"],
        )
        self.assertTrue(record["report_only"])
        self.assertFalse(record["routing_mutation_allowed"])
        self.assertFalse(record["promotion_authority"])
        self.assertFalse(record["provider_observation"]["quality_attested"])
        self.assertTrue(record["attestation_id"].startswith("sha256:"))
        self.assertEqual(
            1,
            row["natural_execution_attestation"]["effective_record_count"],
        )
        self.assertFalse(
            row["natural_execution_attestation"]["eligibility"]["live_routing"]
        )

    def test_unknown_mutation_is_reported_but_policy_ineligible_for_worker_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = build(closed_task(Path(tmp)))

        report = build_natural_execution_attestation_report(
            [record], as_of=RECORDED_AT
        )
        self.assertEqual(1, report["eligibility"]["natural_record_count"])
        self.assertEqual(0, report["eligibility"]["natural_worker_evidence_count"])
        self.assertEqual(
            ["unknown_or_unverified_mutation_provenance"],
            report["eligibility"]["natural_policy_ineligible_reasons"],
        )
        with self.assertRaisesRegex(
            NaturalExecutionAttestationError, "policy-eligible"
        ):
            build_worker_certification_evidence(
                [record],
                candidate=CANDIDATE,
                as_of=RECORDED_AT,
            )

    def test_report_keeps_natural_provider_and_synthetic_classes_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            value = closed_task(Path(tmp))
            objective = build(value)
            provider = build(
                value,
                evidence_class="provider-observation",
                scenario="provider_observation",
                outcome="pass",
            )
            synthetic = build(
                value,
                evidence_class="synthetic-boundary",
                scenario="timeout",
                outcome="pass",
            )

        report = build_natural_execution_attestation_report(
            [objective, provider, synthetic], as_of=RECORDED_AT
        )
        self.assertEqual(1, len(report["classes"]["natural-objective-run"]))
        self.assertEqual(1, len(report["classes"]["provider-observation"]))
        self.assertEqual(1, len(report["classes"]["synthetic-boundary"]))
        self.assertEqual(1, report["eligibility"]["natural_record_count"])
        self.assertEqual(0, report["eligibility"]["natural_worker_evidence_count"])
        self.assertFalse(report["eligibility"]["live_routing"])
        self.assertFalse(report["eligibility"]["promotion_authority"])

    def test_append_only_correction_requires_existing_same_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            value = closed_task(Path(tmp))
            original = build(value)
            attach_natural_execution_attestation(value, original)
            correction = build(
                value,
                recorded_at=RECORDED_AT + timedelta(seconds=1),
                supersedes_attestation_id=original["attestation_id"],
            )
            attach_natural_execution_attestation(value, correction)

        self.assertEqual(2, len(value["natural_execution_attestation_history"]))
        report = build_natural_execution_attestation_report(
            value["natural_execution_attestation_history"],
            as_of=RECORDED_AT + timedelta(seconds=2),
        )
        self.assertEqual(1, report["effective_record_count"])
        self.assertEqual(1, report["superseded_record_count"])

    def test_report_cannot_bypass_supersession_binding_or_chronology(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_task = closed_task(root, task_id="first-supersession")
            other_task = closed_task(root, task_id="other-supersession")
            original = build(
                first_task, recorded_at=RECORDED_AT + timedelta(seconds=1)
            )
            wrong_binding = build(
                other_task,
                recorded_at=RECORDED_AT + timedelta(seconds=2),
                supersedes_attestation_id=original["attestation_id"],
            )
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "preserve exact binding"
            ):
                build_natural_execution_attestation_report(
                    [original, wrong_binding],
                    as_of=RECORDED_AT + timedelta(seconds=3),
                )

            earlier = build(
                first_task,
                recorded_at=RECORDED_AT,
                supersedes_attestation_id=original["attestation_id"],
            )
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "strictly later"
            ):
                build_natural_execution_attestation_report(
                    [original, earlier],
                    as_of=RECORDED_AT + timedelta(seconds=3),
                )

    def test_report_rejects_future_conflicts_and_provider_only_worker_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            value = closed_task(Path(tmp))
            objective = build(value)
            conflict = build(
                value, recorded_at=RECORDED_AT + timedelta(seconds=1)
            )
            provider = build(
                value,
                evidence_class="provider-observation",
                scenario="provider_observation",
                outcome="pass",
            )

        with self.assertRaisesRegex(
            NaturalExecutionAttestationError, "conflicting"
        ):
            build_natural_execution_attestation_report(
                [objective, conflict],
                as_of=RECORDED_AT + timedelta(seconds=2),
            )
        with self.assertRaisesRegex(NaturalExecutionAttestationError, "future"):
            build_natural_execution_attestation_report(
                [conflict], as_of=RECORDED_AT
            )
        with self.assertRaisesRegex(
            NaturalExecutionAttestationError, "policy-eligible natural"
        ):
            build_worker_certification_evidence(
                [provider],
                candidate=CANDIDATE,
                as_of=RECORDED_AT,
            )

    def test_natural_boundary_requires_canonical_terminal_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            value = closed_task(Path(tmp))
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "boundary event"
            ):
                build(
                    value,
                    evidence_class="natural-boundary-event",
                    scenario="timeout",
                    outcome="pass",
                    mutation_provenance="unknown",
                )

            value["last_run"]["timed_out"] = True
            boundary = build_natural_boundary_event(
                value, scenario="timeout", observed_at=RECORDED_AT
            )
            attach_natural_boundary_event(value, boundary)
            record = build(
                value,
                evidence_class="natural-boundary-event",
                scenario="timeout",
                outcome="pass",
                mutation_provenance="unknown",
                boundary_event=boundary,
            )

        self.assertTrue(record["source_digests"]["boundary"].startswith("sha256:"))
        report = build_natural_execution_attestation_report(
            [record],
            as_of=RECORDED_AT,
            verified_boundary_events=[boundary],
        )
        self.assertEqual(1, report["eligibility"]["natural_record_count"])
        self.assertEqual(0, report["eligibility"]["natural_worker_evidence_count"])
        with self.assertRaisesRegex(
            NaturalExecutionAttestationError, "policy-eligible"
        ):
            build_worker_certification_evidence(
                [record],
                candidate=CANDIDATE,
                as_of=RECORDED_AT,
                verified_boundary_events=[boundary],
            )

    def test_natural_boundary_rejects_replay_tampering_and_future_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            value = closed_task(Path(tmp))
            value["last_run"]["timed_out"] = True
            boundary = build_natural_boundary_event(
                value, scenario="timeout", observed_at=RECORDED_AT
            )

            value["last_run"]["timed_out"] = False
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "not present"
            ):
                build(
                    value,
                    evidence_class="natural-boundary-event",
                    scenario="timeout",
                    outcome="pass",
                    mutation_provenance="unknown",
                    boundary_event=boundary,
                )

            value["last_run"]["timed_out"] = True
            tampered = copy.deepcopy(boundary)
            tampered["source_digest"] = "sha256:" + ("0" * 64)
            redigest_boundary(tampered)
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "current canonical closure"
            ):
                build(
                    value,
                    evidence_class="natural-boundary-event",
                    scenario="timeout",
                    outcome="pass",
                    mutation_provenance="unknown",
                    boundary_event=tampered,
                )

            future = build_natural_boundary_event(
                value,
                scenario="timeout",
                observed_at=RECORDED_AT + timedelta(seconds=1),
            )
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "future natural boundary"
            ):
                build(
                    value,
                    evidence_class="natural-boundary-event",
                    scenario="timeout",
                    outcome="pass",
                    mutation_provenance="unknown",
                    boundary_event=future,
                )

    def test_forged_boundary_cannot_attach_or_enter_detached_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            value = closed_task(Path(tmp))
            objective = build(value)
            forged = copy.deepcopy(objective)
            forged["evidence"] = {
                "class": "natural-boundary-event",
                "scenario": "timeout",
                "outcome": "pass",
                "mutation_provenance": "unknown",
            }
            forged["source_digests"]["boundary"] = "sha256:" + ("0" * 64)
            forged["boundary_event"] = {
                "schema_version": 1,
                "contract": "cbr-natural-boundary-event-v1",
                "observed_at": RECORDED_AT.isoformat(),
                "binding": {
                    "task_id": value["id"],
                    "attempt": value["attempts"],
                    "execution_evidence_id": value["last_run"][
                        "execution_evidence_id"
                    ],
                },
                "scenario": "timeout",
                "outcome": "pass",
                "mutation_provenance": "unknown",
                "source_digest": "sha256:" + ("1" * 64),
                "report_only": True,
                "mutation_allowed": False,
                "event_id": "sha256:" + ("2" * 64),
            }
            redigest_boundary(forged["boundary_event"])
            forged["source_digests"]["boundary"] = hashlib.sha256(
                json.dumps(
                    forged["boundary_event"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest()
            forged["source_digests"]["boundary"] = (
                "sha256:" + forged["source_digests"]["boundary"]
            )
            redigest(forged)

            with self.assertRaisesRegex(
                NaturalExecutionAttestationError,
                "not present|verified task history",
            ):
                attach_natural_execution_attestation(value, forged)
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "not verified for report"
            ):
                build_natural_execution_attestation_report(
                    [forged], as_of=RECORDED_AT
                )

    def test_task_report_preserves_verified_boundary_across_retry_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = closed_task(root)
            value["last_run"]["timed_out"] = True
            boundary = build_natural_boundary_event(
                value, scenario="timeout", observed_at=RECORDED_AT
            )
            attach_natural_boundary_event(value, boundary)
            record = build(
                value,
                evidence_class="natural-boundary-event",
                scenario="timeout",
                outcome="pass",
                mutation_provenance="unknown",
                boundary_event=boundary,
            )
            attach_natural_execution_attestation(value, record)

            value["attempts"] = 2
            value["last_run"] = {"timed_out": False}
            execution = build_codex_execution_evidence_v3(
                value,
                SimpleNamespace(
                    events=[{
                        "type": "turn.completed",
                        "model": "public-model-v1",
                    }]
                ),
                settings(),
                config(root),
            )
            attach_execution_evidence_v3(value, execution)
            attach_review_outcome_evidence(
                value,
                build_review_outcome_evidence(
                    value,
                    acceptance_method="reviewer_pass",
                    accepted=True,
                    objective_status="passed",
                    semantic_status="pass",
                    reviewer_kind="codex",
                    actual_identity="public-reviewer-v1",
                    actual_identity_source="provider_observed",
                    actual_identity_confidence="provider_observed",
                    review_policy_version="review-v1",
                    rubric_version="rubric-v1",
                ),
            )
            report = natural_execution_attestation_view(
                value, as_of=RECORDED_AT + timedelta(seconds=1)
            )

        self.assertEqual(1, report["effective_record_count"])
        self.assertEqual(
            1, len(report["classes"]["natural-boundary-event"])
        )

    def test_review_binding_rejects_cross_task_and_mechanical_or_failed_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = closed_task(root, task_id="source-task")
            target = closed_task(root, task_id="target-task")
            target["review_outcome_evidence_history"] = copy.deepcopy(
                source["review_outcome_evidence_history"]
            )
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "review evidence is not bound"
            ):
                build(target)

            mechanical = closed_task(root, task_id="mechanical-task")
            mechanical["review_outcome_evidence_history"] = []
            attach_review_outcome_evidence(
                mechanical,
                build_review_outcome_evidence(
                    mechanical,
                    acceptance_method="mechanical_safe",
                    accepted=True,
                    objective_status="passed",
                    semantic_status="not_performed",
                    reviewer_kind="none",
                    review_policy_version="review-v1",
                    rubric_version="rubric-v1",
                ),
            )
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "mechanical"
            ):
                build(mechanical)

            needs_fix = closed_task(root, task_id="needs-fix-task")
            needs_fix["review_outcome_evidence_history"] = []
            attach_review_outcome_evidence(
                needs_fix,
                build_review_outcome_evidence(
                    needs_fix,
                    acceptance_method="human_accept",
                    accepted=True,
                    objective_status="passed",
                    semantic_status="needs_fix",
                    reviewer_kind="human",
                    review_policy_version="review-v1",
                    rubric_version="rubric-v1",
                ),
            )
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "objective outcome"
            ):
                build(needs_fix)

            absent = closed_task(root, task_id="absent-reviewer-task")
            absent["review_outcome_evidence_history"] = []
            attach_review_outcome_evidence(
                absent,
                build_review_outcome_evidence(
                    absent,
                    acceptance_method="human_accept",
                    accepted=True,
                    objective_status="passed",
                    semantic_status="pass",
                    reviewer_kind="none",
                    review_policy_version="review-v1",
                    rubric_version="rubric-v1",
                ),
            )
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "reviewer authority"
            ):
                build(absent)

    def test_attach_preserves_task_binding_and_unknown_blocks_worker_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_task = closed_task(root, task_id="first-task")
            second_task = closed_task(root, task_id="second-task")
            first = build(first_task)
            second = build(
                second_task,
                mapping={**MAPPING, "mapping_revision": "public-mapping-v2"},
            )

            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "destination task"
            ):
                attach_natural_execution_attestation(second_task, first)
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "policy-eligible"
            ):
                build_worker_certification_evidence(
                    [first, second],
                    candidate=CANDIDATE,
                    as_of=RECORDED_AT,
                )

    def test_attach_rejects_stale_review_and_unknown_blocks_policy_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_task = closed_task(root, task_id="stale-review-task")
            first = build(first_task)
            attach_review_outcome_evidence(
                first_task,
                build_review_outcome_evidence(
                    first_task,
                    acceptance_method="human_accept",
                    accepted=False,
                    objective_status="failed",
                    semantic_status="needs_fix",
                    reviewer_kind="human",
                    review_policy_version="review-v1",
                    rubric_version="rubric-v1",
                ),
            )
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "current destination closure"
            ):
                attach_natural_execution_attestation(first_task, first)

            second_task = closed_task(root, task_id="other-review-policy-task")
            second_task["review_outcome_evidence_history"] = []
            attach_review_outcome_evidence(
                second_task,
                build_review_outcome_evidence(
                    second_task,
                    acceptance_method="reviewer_pass",
                    accepted=True,
                    objective_status="passed",
                    semantic_status="pass",
                    reviewer_kind="codex",
                    reviewer_role="independent",
                    decision_confidence="high",
                    actual_identity="public-reviewer-v1",
                    actual_identity_source="provider_observed",
                    actual_identity_confidence="provider_observed",
                    review_policy_version="review-v2",
                    rubric_version="rubric-v2",
                ),
            )
            second = build(second_task)
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "policy-eligible"
            ):
                build_worker_certification_evidence(
                    [first, second],
                    candidate=CANDIDATE,
                    as_of=RECORDED_AT,
                )

    def test_natural_mutation_is_unknown_without_verified_mutation_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            value = closed_task(Path(tmp))
            value["last_result"] = {
                "changed_files": ["public-file.txt"],
                "commits": ["public-commit"],
            }
            record = build(value)
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "mutation provenance is unknown"
            ):
                build(value, mutation_provenance="no_mutation")

        self.assertEqual("unknown", record["evidence"]["mutation_provenance"])

    def test_missing_nonterminal_future_and_unbound_closures_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            value = closed_task(Path(tmp))
            missing = copy.deepcopy(value)
            missing["review_outcome_evidence_history"] = []
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "review outcome"
            ):
                build(missing)

            nonterminal = copy.deepcopy(value)
            nonterminal["status"] = "running"
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "nonterminal"
            ):
                build(nonterminal)

            future = copy.deepcopy(value)
            future["execution_evidence_history"][0]["captured_at"] = (
                RECORDED_AT + timedelta(days=1)
            ).isoformat()
            with self.assertRaises(NaturalExecutionAttestationError):
                build(future)

            unbound = copy.deepcopy(value)
            unbound["review_outcome_evidence_history"][-1]["cohort"]["components"][
                "execution_cohort_id"
            ] = "sha256:other"
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "not bound"
            ):
                build(unbound)

    def test_mismatched_mapping_outcome_digest_and_private_key_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            value = closed_task(Path(tmp))
            wrong_mapping = {**MAPPING, "target_id": "other-target"}
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "target_id"
            ):
                build(value, mapping=wrong_mapping)
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "objective outcome"
            ):
                build(value, outcome="fail")

            record = build(value)
            tampered = copy.deepcopy(record)
            tampered["binding"]["task_id"] = "other-task"
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "digest mismatch"
            ):
                validate_natural_execution_attestation(tampered)

            private = copy.deepcopy(record)
            private["review"]["session_id"] = "private"
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "fields|forbidden"
            ):
                validate_natural_execution_attestation(private)

            extra_privacy = copy.deepcopy(record)
            extra_privacy["privacy"]["extra_flag"] = False
            redigest(extra_privacy)
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "privacy"
            ):
                validate_natural_execution_attestation(extra_privacy)

            short_digest = copy.deepcopy(record)
            short_digest["source_digests"]["review"] = "sha256:abc"
            redigest(short_digest)
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError, "source digests"
            ):
                validate_natural_execution_attestation(short_digest)

    def test_checked_in_representative_fixture_is_strict_and_public_safe(self) -> None:
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "natural-execution-attestation-v1.json"
        )
        record = validate_natural_execution_attestation(
            json.loads(fixture.read_text(encoding="utf-8"))
        )
        serialized = json.dumps(record, sort_keys=True)

        self.assertEqual("natural-objective-run", record["evidence"]["class"])
        self.assertNotIn('"session_id":', serialized)
        self.assertNotIn('"thread_id":', serialized)
        self.assertNotIn('"transcript":', serialized)
        self.assertNotIn('"credential":', serialized)
        self.assertNotIn("/Users/", serialized)


if __name__ == "__main__":
    unittest.main()

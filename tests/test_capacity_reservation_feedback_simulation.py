from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.capacity_reservation_feedback_simulation import (
    CapacityReservationFeedbackSimulationError,
    MUTATION_FIELDS,
    POLICY_REVISION,
    simulate_capacity_reservation_feedback,
    stable_digest,
    validate_capacity_reservation_feedback_simulation_report,
    validate_capacity_reservation_feedback_simulation_request,
)
from tests.test_provider_resource_authority import mapping_v2, policy
from codex_batch_runner.provider_resource_authority import resource_gate_key
from codex_batch_runner.cli import main


NOW = "2030-01-02T05:00:00+00:00"
EARLY = "2030-01-02T04:00:00+00:00"
LATE = "2030-01-02T06:00:00+00:00"


def request() -> dict:
    scope = {
        "project_id": "project",
        "repository_id": "repo",
        "task_class": "class",
        "task_id": "task",
        "attempt_id": "attempt-1",
        "target_id": "target-a",
        "opted_in": True,
    }
    revisions = {
        "mapping_revision": "mapping-r2",
        "currentness_revision": "current-r1",
        "policy_revision": "policy-r1",
        "selector_revision": "selector-r1",
        "resume_revision": "resume-r1",
        "simulation_policy_revision": POLICY_REVISION,
    }
    canonical_key = resource_gate_key("provider-a", "quota-a", "scope-a", "primary")
    resource = {
        "canonical_key": canonical_key,
        "mapping_status": "exact",
        "mapping_revision": "mapping-r2",
        "policy_revision": "policy-r1",
        "observed_at": EARLY,
        "expires_at": LATE,
    }
    mapping = mapping_v2()
    admission_policy = policy()
    return {
        "schema_version": 1,
        "contract": "capacity-reservation-feedback-simulation-request-v1",
        "scope": scope,
        "revisions": revisions,
        "currentness": {
            "revision": "current-r1",
            "status": "current",
            "observed_at": EARLY,
            "expires_at": LATE,
        },
        "mapping": mapping,
        "admission_policy": admission_policy,
        "selector_binding": {
            "status": "eligible",
            "baseline_digest": stable_digest({"baseline": 1}),
            "resume_binding": "resume-r1",
            "selector_revision": "selector-r1",
            "resume_revision": "resume-r1",
            "eligible_target_ids": ["target-a"],
        },
        "global_admission": {
            "status": "allowed",
            "decision_key": "global-decision",
            "wake_key": "global-wake",
        },
        "resource": resource,
        "replay": {"evaluated_at": NOW},
        "predecessor_events": [],
        "reservation": {
            "task_id": "task",
            "attempt_id": "attempt-1",
            "target_id": "target-a",
            "resource_key": canonical_key,
            "evidence_digest": stable_digest({"e": 1}),
            "policy_revision": "policy-r1",
            "expires_at": LATE,
            "authoritative_wake_at": LATE,
        },
        "feedback": {
            "event_id": "feedback-1",
            "task_id": "task",
            "attempt_id": "attempt-1",
            "target_id": "target-a",
            "resource_key": canonical_key,
            "outcome": "unknown",
            "fresh_exact_bound": False,
            "predecessor_event_id": None,
        },
        "retry_budget": {
            "task_id": "task",
            "attempt_id": "attempt-1",
            "remaining": 2,
            "automatic_retries": 0,
            "provider_quota_bound": False,
            "task_attempt_limit_bound": False,
        },
    }


class CapacityReservationFeedbackTests(unittest.TestCase):
    def test_exact_bound_report_is_report_only(self) -> None:
        report = simulate_capacity_reservation_feedback(request())
        self.assertEqual("would_reserve", report["preview"])
        self.assertEqual("would_not_retry", report["retry_preview"]["status"])
        self.assertEqual("unknown", report["feedback_preview"]["outcome"])
        self.assertTrue(report["simulation_only"])
        for field in MUTATION_FIELDS:
            self.assertEqual([], report[field])

    def test_global_precedence_fails_before_reservation(self) -> None:
        source = request()
        source["global_admission"]["status"] = "unknown"
        report = simulate_capacity_reservation_feedback(source)
        self.assertEqual("fail_closed", report["preview"])
        self.assertEqual(["global_admission_unknown"], report["reason_codes"])

    def test_currentness_and_expiry_fail_closed(self) -> None:
        stale = request()
        stale["currentness"]["status"] = "stale"
        self.assertEqual(
            "fail_closed", simulate_capacity_reservation_feedback(stale)["preview"]
        )
        expired = request()
        expired["replay"]["evaluated_at"] = LATE
        self.assertEqual(
            "fail_closed", simulate_capacity_reservation_feedback(expired)["preview"]
        )

    def test_predecessor_lineage_and_duplicate_conflict_rejected(self) -> None:
        source = request()
        digest = stable_digest({"one": 1})
        source["predecessor_events"] = [
            {
                "event_id": "e1",
                "predecessor_event_id": None,
                "observed_at": "2030-01-02T04:10:00+00:00",
                "evidence_digest": digest,
            },
            {
                "event_id": "e2",
                "predecessor_event_id": "wrong",
                "observed_at": "2030-01-02T04:20:00+00:00",
                "evidence_digest": digest,
            },
        ]
        with self.assertRaises(CapacityReservationFeedbackSimulationError):
            validate_capacity_reservation_feedback_simulation_request(source)

    def test_half_open_is_one_exact_recovery_candidate(self) -> None:
        source = request()
        source["feedback"]["outcome"] = "recovery"
        source["feedback"]["fresh_exact_bound"] = True
        report = simulate_capacity_reservation_feedback(source)
        self.assertEqual(
            [resource_gate_key("provider-a", "quota-a", "scope-a", "primary")],
            report["half_open_preview"]["candidate_resource_keys"],
        )
        source["feedback"]["fresh_exact_bound"] = False
        self.assertEqual(
            [],
            simulate_capacity_reservation_feedback(source)["half_open_preview"][
                "candidate_resource_keys"
            ],
        )

    def test_exact_binding_unknown_fields_and_forgery_rejected(self) -> None:
        source = request()
        source["scope"]["extra"] = True
        with self.assertRaises(CapacityReservationFeedbackSimulationError):
            validate_capacity_reservation_feedback_simulation_request(source)
        report = simulate_capacity_reservation_feedback(request())
        forged = copy.deepcopy(report)
        forged["preview"] = "would_retry"
        with self.assertRaises(CapacityReservationFeedbackSimulationError):
            validate_capacity_reservation_feedback_simulation_report(forged)

    def test_retry_is_separate_and_zero_automatic(self) -> None:
        source = request()
        source["retry_budget"]["automatic_retries"] = 1
        with self.assertRaises(CapacityReservationFeedbackSimulationError):
            validate_capacity_reservation_feedback_simulation_request(source)

    def test_standalone_cli_never_loads_or_mutates_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request()), encoding="utf-8")
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    0,
                    main(
                        [
                            "capacity-reservation-feedback-simulate",
                            "--request-json",
                            str(request_path),
                            "--json",
                        ]
                    ),
                )
            self.assertEqual(
                before, {path.name: path.read_bytes() for path in root.iterdir()}
            )
            self.assertEqual("would_reserve", json.loads(stdout.getvalue())["preview"])

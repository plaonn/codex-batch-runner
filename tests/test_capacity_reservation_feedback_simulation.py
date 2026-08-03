from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.capacity_reservation_feedback_simulation import (
    MANUAL_OVERRIDE_BINDING_GAP,
    MUTATION_FIELDS,
    POLICY_REVISION,
    RETRY_POLICY_REVISION,
    CapacityReservationFeedbackSimulationError,
    simulate_capacity_reservation_feedback,
    stable_digest,
    validate_capacity_reservation_feedback_simulation_report,
    validate_capacity_reservation_feedback_simulation_request,
)
from codex_batch_runner.capacity_target_ordering_simulation import (
    simulate_capacity_target_ordering_activation,
)
from codex_batch_runner.cli import main
from codex_batch_runner.provider_resource_authority import (
    resource_gate_decision_key,
    resource_gate_key,
    resource_gate_wake_key,
)
from tests.test_capacity_target_ordering_simulation import simulation_request
from tests.test_provider_capacity_shadow import request as shadow_request
from tests.test_provider_resource_authority import policy

NOW = "2030-01-02T04:00:00+00:00"
OBSERVED = "2030-01-02T03:50:00+00:00"
CURRENTNESS_EXPIRES = "2030-01-02T05:30:00+00:00"
TARGET_RESET = "2030-01-02T05:00:00+00:00"
GLOBAL_RESET = "2030-01-02T05:15:00+00:00"


def evidence(body: dict) -> dict:
    return {"body": copy.deepcopy(body), "evidence_digest": stable_digest(body)}


def gate_decision(
    *,
    provider_id: str,
    quota_identity_id: str,
    scope_id: str,
    window_id: str,
    reset_at: str,
    action: str = "allow",
) -> dict:
    item = {
        "schema_version": 1,
        "contract": "provider-resource-gate-decision-v1",
        "decision_key": "placeholder",
        "resource_key": resource_gate_key(
            provider_id, quota_identity_id, scope_id, window_id
        ),
        "wake_key": resource_gate_wake_key(
            provider_id, quota_identity_id, scope_id, window_id, reset_at
        ),
        "policy_revision": "policy-r1",
        "mapping_revision": "mapping-r1",
        "provider_id": provider_id,
        "quota_identity_id": quota_identity_id,
        "scope_id": scope_id,
        "window_id": window_id,
        "observed_at": OBSERVED,
        "reset_at": reset_at,
        "action": action,
        "global_coverage": {
            "status": "not_covered",
            "global_reset_at": None,
        },
        "supersedes_decision_key": None,
    }
    item["decision_key"] = resource_gate_decision_key(item)
    return item


def gate_status(action: str) -> str:
    if action == "allow":
        return "allowed"
    if action in {"defer", "covered_by_global"}:
        return "gated"
    return "unknown"


def gate_tuple(decision: dict) -> dict:
    return {
        "resource_key": decision["resource_key"],
        "decision_key": decision["decision_key"],
        "wake_key": decision["wake_key"],
        "status": gate_status(decision["action"]),
    }


def gate_state(decisions: list[dict]) -> dict:
    active = [
        {
            "resource_key": item["resource_key"],
            "decision_key": item["decision_key"],
            "wake_key": item["wake_key"],
            "reset_at": item["reset_at"],
            "status": "active",
        }
        for item in decisions
        if gate_status(item["action"]) == "gated"
    ]
    return {
        "schema_version": 1,
        "contract": "provider-resource-gate-state-v1",
        "migration": {
            "mode": "typed_primary_scalar_compatibility",
            "legacy_scalar_role": "global_gate_only",
            "rollback_mode": "disable_typed_evaluation_preserve_records",
            "evidence_history": "append_only",
        },
        "active_gates": active,
    }


def build_request() -> dict:
    shadow = shadow_request()
    selector_report = simulate_capacity_target_ordering_activation(
        simulation_request(shadow)
    )
    mapping = copy.deepcopy(shadow["provider_resource_mapping"])
    admission_policy = policy()
    admission_policy["allowed_mapping_revisions"] = ["mapping-r1"]
    admission_policy["target_rules"][0]["provider_id"] = "provider-example"
    scope = {
        **selector_report["scope"],
        "task_id": "task-a",
        "attempt_id": "attempt-1",
        "target_id": selector_report["counterfactual_target_id"],
    }
    revisions = {
        "mapping_revision": "mapping-r1",
        "currentness_revision": "currentness-r1",
        "policy_revision": "policy-r1",
        "selector_revision": selector_report["revisions"]["selector_policy_revision"],
        "resume_revision": "resume-r1",
        "retry_policy_revision": RETRY_POLICY_REVISION,
        "simulation_policy_revision": POLICY_REVISION,
    }
    binding = next(
        item for item in mapping["bindings"] if item["target_id"] == scope["target_id"]
    )
    resource = {
        "target_id": scope["target_id"],
        "binding_id": binding["binding_id"],
        "provider_id": binding["provider_id"],
        "quota_identity_id": binding["quota_identity_id"],
        "scope_id": binding["observation_scope"]["scope_id"],
        "window_id": "primary",
        "canonical_key": resource_gate_key(
            binding["provider_id"],
            binding["quota_identity_id"],
            binding["observation_scope"]["scope_id"],
            "primary",
        ),
        "mapping_revision": "mapping-r1",
        "policy_revision": "policy-r1",
        "identity_authority": "source_attested",
        "verified_at": binding["verified_at"],
        "expires_at": binding["expires_at"],
    }
    source_scope = {
        field: scope[field]
        for field in (
            "project_id",
            "repository_id",
            "task_class",
            "opt_in_scope_id",
        )
    }
    currentness = evidence(
        {
            "target_id": scope["target_id"],
            "resource_key": resource["canonical_key"],
            "scope": source_scope,
            "mapping_revision": "mapping-r1",
            "policy_revision": "policy-r1",
            "currentness_revision": "currentness-r1",
            "mapping_artifact_digest": stable_digest(mapping),
            "policy_artifact_digest": stable_digest(admission_policy),
            "observed_at": OBSERVED,
            "expires_at": CURRENTNESS_EXPIRES,
            "identity_authority": "source_attested",
        }
    )
    selector = {
        "activation_report": selector_report,
        "activation_report_digest": stable_digest(selector_report),
        **selector_report["simulation_request"]["global_gate"],
        "eligible_target_ids": selector_report["baseline_order"],
        "immutable_baseline_digest": stable_digest(
            {
                "baseline": selector_report["baseline"],
                "baseline_order": selector_report["baseline_order"],
            }
        ),
        "immutable_baseline_order": selector_report["baseline_order"],
        "selected_target_id": selector_report["counterfactual_target_id"],
        "selector_revision": revisions["selector_revision"],
        "resume_target_id": selector_report["resume_target_id"],
        "resume_revision": revisions["resume_revision"],
        "manual_override_binding_resolved": False,
    }
    global_decision = gate_decision(
        provider_id="global",
        quota_identity_id="global",
        scope_id=scope["opt_in_scope_id"],
        window_id="admission",
        reset_at=GLOBAL_RESET,
    )
    target_decision = gate_decision(
        provider_id=resource["provider_id"],
        quota_identity_id=resource["quota_identity_id"],
        scope_id=resource["scope_id"],
        window_id=resource["window_id"],
        reset_at=TARGET_RESET,
    )
    decisions = [global_decision, target_decision]
    gates = {
        "state": gate_state(decisions),
        "decisions": decisions,
        "global": gate_tuple(global_decision),
        "target": gate_tuple(target_decision),
    }
    reservation_body = {
        "task_id": scope["task_id"],
        "attempt_id": scope["attempt_id"],
        "target_id": scope["target_id"],
        "resource_key": resource["canonical_key"],
        "policy_revision": revisions["policy_revision"],
        "mapping_digest": stable_digest(mapping),
        "policy_digest": stable_digest(admission_policy),
        "currentness_digest": currentness["evidence_digest"],
        "selector_digest": selector["activation_report_digest"],
        "gate_digest": stable_digest(gates),
        "authoritative_reset_at": TARGET_RESET,
        "authoritative_wake_at": TARGET_RESET,
        "expires_at": TARGET_RESET,
    }
    feedback_body = {
        "event_id": "feedback-1",
        "task_id": scope["task_id"],
        "attempt_id": scope["attempt_id"],
        "target_id": scope["target_id"],
        "resource_key": resource["canonical_key"],
        "outcome": "unknown",
        "predecessor_event_id": None,
        "observed_at": "2030-01-02T03:59:00+00:00",
    }
    retry_body = {
        "task_id": scope["task_id"],
        "attempt_id": scope["attempt_id"],
        "resume_target_id": selector["resume_target_id"],
        "resume_revision": revisions["resume_revision"],
        "retry_policy_revision": revisions["retry_policy_revision"],
        "remaining": 2,
        "automatic_retries": 0,
        "cooldown_inactive": True,
        "dependencies_satisfied": True,
        "resume_stop_inactive": True,
        "operator_stop_inactive": True,
        "task_attempt_boundary_preserved": True,
        "provider_quota_bound": False,
        "task_attempt_limit_bound": False,
    }
    return {
        "schema_version": 1,
        "contract": "capacity-reservation-feedback-simulation-request-v1",
        "scope": scope,
        "revisions": revisions,
        "mapping": mapping,
        "admission_policy": admission_policy,
        "currentness_evidence": currentness,
        "selector_binding": selector,
        "resource": resource,
        "gates": gates,
        "replay": {"evaluated_at": NOW},
        "predecessor_events": [],
        "reservation": evidence(reservation_body),
        "feedback": {**evidence(feedback_body), "recovery_evidence": None},
        "retry_budget": evidence(retry_body),
    }


def rebind_reservation(source: dict) -> None:
    decisions = source["gates"]["decisions"]
    target = next(
        item
        for item in decisions
        if item["decision_key"] == source["gates"]["target"]["decision_key"]
    )
    wake = min((item["reset_at"] for item in decisions), key=parse_time)
    candidates = (
        target["reset_at"],
        wake,
        source["currentness_evidence"]["body"]["expires_at"],
        source["resource"]["expires_at"],
    )
    body = source["reservation"]["body"]
    body.update(
        {
            "mapping_digest": stable_digest(source["mapping"]),
            "policy_digest": stable_digest(source["admission_policy"]),
            "currentness_digest": source["currentness_evidence"]["evidence_digest"],
            "selector_digest": source["selector_binding"]["activation_report_digest"],
            "gate_digest": stable_digest(source["gates"]),
            "authoritative_reset_at": target["reset_at"],
            "authoritative_wake_at": wake,
            "expires_at": min(candidates, key=parse_time),
        }
    )
    source["reservation"]["evidence_digest"] = stable_digest(body)


def parse_time(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def set_gate(
    source: dict, name: str, *, action: str, reset_at: str | None = None
) -> None:
    old_key = source["gates"][name]["decision_key"]
    decision = next(
        item for item in source["gates"]["decisions"] if item["decision_key"] == old_key
    )
    decision["action"] = action
    if reset_at is not None:
        decision["reset_at"] = reset_at
    decision["wake_key"] = resource_gate_wake_key(
        decision["provider_id"],
        decision["quota_identity_id"],
        decision["scope_id"],
        decision["window_id"],
        decision["reset_at"],
    )
    decision["decision_key"] = resource_gate_decision_key(decision)
    source["gates"][name] = gate_tuple(decision)
    source["gates"]["state"] = gate_state(source["gates"]["decisions"])
    rebind_reservation(source)


def add_predecessor(source: dict, *, outcome: str = "failure") -> None:
    item = evidence(
        {
            "event_id": "event-1",
            "predecessor_event_id": None,
            "observed_at": "2030-01-02T03:55:00+00:00",
            "outcome": outcome,
        }
    )
    source["predecessor_events"] = [item]
    source["feedback"]["body"]["predecessor_event_id"] = "event-1"
    source["feedback"]["evidence_digest"] = stable_digest(source["feedback"]["body"])


def make_recovery(source: dict) -> None:
    body = source["feedback"]["body"]
    body["outcome"] = "recovery"
    source["feedback"]["evidence_digest"] = stable_digest(body)
    recovery_body = {
        "observed_at": body["observed_at"],
        "expires_at": "2030-01-02T04:10:00+00:00",
        "currentness_revision": source["revisions"]["currentness_revision"],
        "currentness_digest": source["currentness_evidence"]["evidence_digest"],
        "target_id": source["scope"]["target_id"],
        "resource_key": source["resource"]["canonical_key"],
        "scope": source["currentness_evidence"]["body"]["scope"],
        "global_decision_key": source["gates"]["global"]["decision_key"],
        "global_wake_key": source["gates"]["global"]["wake_key"],
        "target_decision_key": source["gates"]["target"]["decision_key"],
        "target_wake_key": source["gates"]["target"]["wake_key"],
        "predecessor_event_id": body["predecessor_event_id"],
        "identity_authority": "source_attested",
    }
    source["feedback"]["recovery_evidence"] = evidence(recovery_body)


class CapacityReservationFeedbackTests(unittest.TestCase):
    def assert_rejected(self, source: dict) -> None:
        with self.assertRaises(CapacityReservationFeedbackSimulationError):
            validate_capacity_reservation_feedback_simulation_request(source)

    def test_exact_bound_report_is_report_only_and_manual_override_fail_closed(
        self,
    ) -> None:
        report = simulate_capacity_reservation_feedback(build_request())
        self.assertEqual("fail_closed", report["preview"])
        self.assertEqual([MANUAL_OVERRIDE_BINDING_GAP], report["reason_codes"])
        self.assertFalse(report["manual_override_binding_resolved"])
        self.assertEqual([], report["half_open_preview"]["candidate_resource_keys"])
        self.assertTrue(report["simulation_only"])
        for field in MUTATION_FIELDS:
            self.assertEqual([], report[field])

    def test_future_verified_at_rejected(self) -> None:
        source = build_request()
        source["mapping"]["bindings"][0]["verified_at"] = "2030-01-02T04:01:00Z"
        source["resource"]["verified_at"] = "2030-01-02T04:01:00Z"
        self.assert_rejected(source)

    def test_missing_applicable_policy_rule_rejected(self) -> None:
        source = build_request()
        source["admission_policy"]["target_rules"] = []
        self.assert_rejected(source)

    def test_artifact_digest_drift_rejected(self) -> None:
        source = build_request()
        source["currentness_evidence"]["body"]["mapping_artifact_digest"] = (
            stable_digest({"different": True})
        )
        source["currentness_evidence"]["evidence_digest"] = stable_digest(
            source["currentness_evidence"]["body"]
        )
        self.assert_rejected(source)

    def test_mapping_and_policy_revision_drift_rejected(self) -> None:
        for field in ("mapping_revision", "policy_revision"):
            with self.subTest(field=field):
                source = build_request()
                source["revisions"][field] = "drift-r2"
                self.assert_rejected(source)

    def test_currentness_future_or_expired_rejected(self) -> None:
        for field, value in (
            ("observed_at", "2030-01-02T04:01:00+00:00"),
            ("expires_at", NOW),
        ):
            with self.subTest(field=field):
                source = build_request()
                source["currentness_evidence"]["body"][field] = value
                source["currentness_evidence"]["evidence_digest"] = stable_digest(
                    source["currentness_evidence"]["body"]
                )
                self.assert_rejected(source)

    def test_arbitrary_valid_hex_reservation_evidence_rejected(self) -> None:
        source = build_request()
        source["reservation"]["evidence_digest"] = "sha256:" + "a" * 64
        self.assert_rejected(source)

    def test_reservation_binds_every_artifact_digest(self) -> None:
        fields = (
            "mapping_digest",
            "policy_digest",
            "currentness_digest",
            "selector_digest",
            "gate_digest",
        )
        for field in fields:
            with self.subTest(field=field):
                source = build_request()
                source["reservation"]["body"][field] = stable_digest({"drift": field})
                source["reservation"]["evidence_digest"] = stable_digest(
                    source["reservation"]["body"]
                )
                self.assert_rejected(source)

    def test_selector_report_and_resume_mismatch_rejected(self) -> None:
        source = build_request()
        source["selector_binding"]["resume_target_id"] = source["scope"]["target_id"]
        self.assert_rejected(source)
        source = build_request()
        source["selector_binding"]["immutable_baseline_order"] = [
            "target-b",
            "target-a",
        ]
        self.assert_rejected(source)

    def test_selector_report_forgery_rejected_even_with_outer_digest(self) -> None:
        source = build_request()
        source["selector_binding"]["activation_report"]["decision"] = "fail_closed"
        source["selector_binding"]["activation_report_digest"] = stable_digest(
            source["selector_binding"]["activation_report"]
        )
        self.assert_rejected(source)

    def test_global_gate_key_mismatch_rejected(self) -> None:
        source = build_request()
        source["gates"]["global"]["decision_key"] = "decision-" + "a" * 64
        self.assert_rejected(source)

    def test_target_gate_key_mismatch_rejected(self) -> None:
        source = build_request()
        source["gates"]["target"]["wake_key"] = "wake-" + "b" * 64
        self.assert_rejected(source)

    def test_global_gate_precedes_target_gate(self) -> None:
        source = build_request()
        set_gate(source, "target", action="defer")
        set_gate(source, "global", action="defer")
        report = simulate_capacity_reservation_feedback(source)
        self.assertEqual(
            ["global_gate_gated", MANUAL_OVERRIDE_BINDING_GAP],
            report["reason_codes"],
        )
        self.assertNotIn("target_gate_gated", report["reason_codes"])

    def test_target_gate_applies_after_global_allowed(self) -> None:
        source = build_request()
        set_gate(source, "target", action="defer")
        report = simulate_capacity_reservation_feedback(source)
        self.assertEqual(
            ["target_gate_gated", MANUAL_OVERRIDE_BINDING_GAP],
            report["reason_codes"],
        )

    def test_recovery_is_validated_but_manual_override_keeps_zero_candidates(
        self,
    ) -> None:
        source = build_request()
        make_recovery(source)
        report = simulate_capacity_reservation_feedback(source)
        self.assertEqual([], report["half_open_preview"]["candidate_resource_keys"])
        self.assertEqual(
            MANUAL_OVERRIDE_BINDING_GAP, report["half_open_preview"]["reason"]
        )

    def test_recovery_stale_rejected(self) -> None:
        source = build_request()
        make_recovery(source)
        recovery = source["feedback"]["recovery_evidence"]
        recovery["body"]["expires_at"] = "2030-01-02T03:59:30+00:00"
        recovery["evidence_digest"] = stable_digest(recovery["body"])
        self.assert_rejected(source)

    def test_recovery_digest_mismatch_rejected(self) -> None:
        source = build_request()
        make_recovery(source)
        source["feedback"]["recovery_evidence"]["evidence_digest"] = (
            "sha256:" + "c" * 64
        )
        self.assert_rejected(source)

    def test_recovery_gate_key_mismatch_rejected(self) -> None:
        source = build_request()
        make_recovery(source)
        recovery = source["feedback"]["recovery_evidence"]
        recovery["body"]["target_wake_key"] = "wake-" + "d" * 64
        recovery["evidence_digest"] = stable_digest(recovery["body"])
        self.assert_rejected(source)

    def test_retry_blockers_fail_closed_without_retry(self) -> None:
        for field in (
            "cooldown_inactive",
            "dependencies_satisfied",
            "resume_stop_inactive",
            "operator_stop_inactive",
            "task_attempt_boundary_preserved",
        ):
            with self.subTest(field=field):
                source = build_request()
                source["retry_budget"]["body"][field] = False
                source["retry_budget"]["evidence_digest"] = stable_digest(
                    source["retry_budget"]["body"]
                )
                report = simulate_capacity_reservation_feedback(source)
                self.assertEqual("unsafe", report["retry_preview"]["safety_status"])
                self.assertEqual("would_not_retry", report["retry_preview"]["status"])
                self.assertIn("retry_", report["reason_codes"][0])

    def test_retry_digest_and_revision_mismatch_rejected(self) -> None:
        source = build_request()
        source["retry_budget"]["evidence_digest"] = "sha256:" + "e" * 64
        self.assert_rejected(source)
        source = build_request()
        source["retry_budget"]["body"]["retry_policy_revision"] = "retry-r2"
        source["retry_budget"]["evidence_digest"] = stable_digest(
            source["retry_budget"]["body"]
        )
        self.assert_rejected(source)

    def test_mixed_offset_earliest_boundary_is_parsed_not_lexical(self) -> None:
        source = build_request()
        set_gate(
            source,
            "global",
            action="allow",
            reset_at="2030-01-02T05:30:00+01:00",
        )
        self.assertEqual(
            "2030-01-02T05:30:00+01:00",
            source["reservation"]["body"]["expires_at"],
        )
        validate_capacity_reservation_feedback_simulation_request(source)

    def test_duplicate_lineage_id_or_digest_rejected(self) -> None:
        source = build_request()
        add_predecessor(source)
        duplicate = copy.deepcopy(source["predecessor_events"][0])
        duplicate["body"]["predecessor_event_id"] = "event-1"
        duplicate["body"]["observed_at"] = "2030-01-02T03:56:00+00:00"
        duplicate["evidence_digest"] = stable_digest(duplicate["body"])
        source["predecessor_events"].append(duplicate)
        self.assert_rejected(source)

    def test_feedback_id_and_digest_reuse_rejected(self) -> None:
        source = build_request()
        add_predecessor(source)
        source["feedback"]["body"]["event_id"] = "event-1"
        source["feedback"]["evidence_digest"] = stable_digest(
            source["feedback"]["body"]
        )
        self.assert_rejected(source)

    def test_failure_and_unknown_observations_are_retained(self) -> None:
        source = build_request()
        add_predecessor(source, outcome="failure")
        report = simulate_capacity_reservation_feedback(source)
        self.assertEqual(
            ["failure", "unknown"],
            [item["outcome"] for item in report["event_results"]],
        )
        self.assertEqual("unknown", report["feedback_preview"]["outcome"])

    def test_strict_request_bool_and_int_types_reject_zero_one(self) -> None:
        source = build_request()
        source["scope"]["opted_in"] = 1
        self.assert_rejected(source)
        source = build_request()
        source["retry_budget"]["body"]["remaining"] = True
        source["retry_budget"]["evidence_digest"] = stable_digest(
            source["retry_budget"]["body"]
        )
        self.assert_rejected(source)

    def test_uppercase_sha256_rejected(self) -> None:
        source = build_request()
        source["reservation"]["evidence_digest"] = "sha256:" + "A" * 64
        self.assert_rejected(source)

    def test_standalone_report_rejects_bool_substitution_and_replay_forgery(
        self,
    ) -> None:
        report = simulate_capacity_reservation_feedback(build_request())
        forged = copy.deepcopy(report)
        forged["simulation_only"] = 1
        with self.assertRaises(CapacityReservationFeedbackSimulationError):
            validate_capacity_reservation_feedback_simulation_report(forged)
        forged = copy.deepcopy(report)
        forged["activation_authority"] = 0
        with self.assertRaises(CapacityReservationFeedbackSimulationError):
            validate_capacity_reservation_feedback_simulation_report(forged)
        forged = copy.deepcopy(report)
        forged["feedback_preview"]["outcome"] = "success"
        replay_body = copy.deepcopy(forged)
        replay_body.pop("replay_digest")
        forged["replay_digest"] = stable_digest(replay_body)
        with self.assertRaises(CapacityReservationFeedbackSimulationError):
            validate_capacity_reservation_feedback_simulation_report(forged)

    def test_mutation_arrays_are_separate_and_exactly_empty(self) -> None:
        report = simulate_capacity_reservation_feedback(build_request())
        forged = copy.deepcopy(report)
        forged["queue_mutations"] = forged["retry_mutations"]
        forged["queue_mutations"].append({})
        with self.assertRaises(CapacityReservationFeedbackSimulationError):
            validate_capacity_reservation_feedback_simulation_report(forged)

    def test_standalone_cli_uses_no_config_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_path = root / "request.json"
            request_path.write_text(json.dumps(build_request()), encoding="utf-8")
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
            self.assertEqual("fail_closed", json.loads(stdout.getvalue())["preview"])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    2,
                    main(
                        [
                            "--config",
                            str(root / "must-not-load.json"),
                            "capacity-reservation-feedback-simulate",
                            "--request-json",
                            str(request_path),
                        ]
                    ),
                )
            self.assertIn("--config is not supported", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

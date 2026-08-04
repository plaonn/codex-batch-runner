from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.capacity_reservation_feedback_simulation import (
    MUTATION_FIELDS,
    POLICY_REVISION,
    RETRY_POLICY_REVISION,
    CapacityReservationFeedbackSimulationError,
    simulate_capacity_reservation_feedback,
    stable_digest,
    validate_capacity_reservation_feedback_simulation_report,
    validate_capacity_reservation_feedback_simulation_request,
)
from codex_batch_runner.execution_target_selector_decision_envelope import (
    PRODUCER_ID,
    PRODUCER_REVISION,
    build_execution_target_selector_decision_envelope,
    selector_input_digest,
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
from tests.test_capacity_target_ordering_simulation import (
    alternative_shadow_request,
    simulation_request,
)
from tests.test_provider_capacity_shadow import (
    bundle as capacity_bundle,
    request as shadow_request,
)
from tests.test_provider_resource_authority import policy

NOW = "2030-01-02T04:00:00+00:00"
OBSERVED = "2030-01-02T03:50:00+00:00"
CURRENTNESS_EXPIRES = "2030-01-02T05:30:00+00:00"
TARGET_RESET = "2030-01-02T05:00:00+00:00"
GLOBAL_RESET = "2030-01-02T05:15:00+00:00"


def evidence(body: dict) -> dict:
    return {"body": copy.deepcopy(body), "evidence_digest": stable_digest(body)}


def routing_override(
    mode: str, target_id: str, *, allow_fallback: bool = False
) -> dict:
    return {
        "mode": mode,
        "target_id": target_id,
        "reason": "bounded-operator-choice",
        "scope": "single_task",
        "allow_fallback": allow_fallback,
        "provenance": "operator_override",
    }


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
        "canonical_task_source_revision": "task-source-r1",
        "task_attempts_before_claim": 0,
        "attempt": 1,
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
        "manual_override_binding_resolved": True,
    }
    envelope_task = {
        "task_id": scope["task_id"],
        "canonical_task_source_revision": scope["canonical_task_source_revision"],
        "task_attempts_before_claim": scope["task_attempts_before_claim"],
        "attempt": scope["attempt"],
    }
    envelope_scope = {
        field: scope[field]
        for field in ("project_id", "repository_id", "task_class", "opt_in_scope_id")
    }
    envelope_inputs = {
        field: selector_report["revisions"][field]
        for field in (
            "requirement_revision",
            "inventory_snapshot_id",
            "selector_policy_revision",
        )
    }
    envelope_inputs["selector_input_digest"] = selector_input_digest(
        task=envelope_task,
        scope=envelope_scope,
        requirement_revision=envelope_inputs["requirement_revision"],
        inventory_snapshot_id=envelope_inputs["inventory_snapshot_id"],
        selector_policy_revision=envelope_inputs["selector_policy_revision"],
    )
    projection = {
        "task_id": scope["task_id"],
        "canonical_task_source_revision": scope["canonical_task_source_revision"],
        "routing_override": None,
    }
    source = {
        "status": "authoritative_absence",
        "producer_id": PRODUCER_ID,
        "producer_revision": PRODUCER_REVISION,
        "source_revision": "task-source-r1",
        "source_projection": projection,
        "source_projection_digest": stable_digest(projection),
    }
    currentness_body = {
        "producer_id": PRODUCER_ID,
        "producer_revision": PRODUCER_REVISION,
        "source_revision": source["source_revision"],
        "identity_authority": "source_attested",
        "observed_at": OBSERVED,
        "expires_at": CURRENTNESS_EXPIRES,
        "source_projection_digest": source["source_projection_digest"],
    }
    envelope_currentness = {
        **currentness_body,
        "currentness_digest": stable_digest(currentness_body),
    }
    selector["decision_envelopes"] = [
        build_execution_target_selector_decision_envelope(
            {
                "schema_version": 1,
                "contract": "execution-target-selector-decision-envelope-request-v1",
                "evaluated_at": NOW,
                "task": envelope_task,
                "scope": envelope_scope,
                "selector_inputs": envelope_inputs,
                "manual_override_source": source,
                "currentness": envelope_currentness,
                "baseline_report": selector_report,
            }
        )
    ]
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
        "selector_envelope_digest": selector["decision_envelopes"][0][
            "artifact_digest"
        ],
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
            "selector_envelope_digest": source["selector_binding"][
                "decision_envelopes"
            ][0]["artifact_digest"],
            "gate_digest": stable_digest(source["gates"]),
            "authoritative_reset_at": target["reset_at"],
            "authoritative_wake_at": wake,
            "expires_at": min(candidates, key=parse_time),
        }
    )
    source["reservation"]["evidence_digest"] = stable_digest(body)


def set_selector_override(source: dict, override: dict | None) -> None:
    producer_request = copy.deepcopy(
        source["selector_binding"]["decision_envelopes"][0]["producer_request"]
    )
    manual = producer_request["manual_override_source"]
    manual["status"] = "authoritative_absence" if override is None else "present"
    manual["source_projection"]["routing_override"] = copy.deepcopy(override)
    manual["source_projection_digest"] = stable_digest(manual["source_projection"])
    envelope_currentness = producer_request["currentness"]
    envelope_currentness["source_projection_digest"] = manual[
        "source_projection_digest"
    ]
    body = copy.deepcopy(envelope_currentness)
    body.pop("currentness_digest")
    envelope_currentness["currentness_digest"] = stable_digest(body)
    source["selector_binding"]["decision_envelopes"] = [
        build_execution_target_selector_decision_envelope(producer_request)
    ]
    source["selector_binding"]["selected_target_id"] = source["selector_binding"][
        "decision_envelopes"
    ][0]["selected_target_id"]
    rebind_reservation(source)


def set_selector_report(source: dict, selector_report: dict) -> None:
    selector = source["selector_binding"]
    selector.update(
        {
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
            "selector_revision": selector_report["revisions"][
                "selector_policy_revision"
            ],
            "resume_target_id": selector_report["resume_target_id"],
        }
    )
    source["revisions"]["selector_revision"] = selector["selector_revision"]
    producer_request = copy.deepcopy(
        selector["decision_envelopes"][0]["producer_request"]
    )
    producer_request["baseline_report"] = selector_report
    inputs = producer_request["selector_inputs"]
    for field in (
        "requirement_revision",
        "inventory_snapshot_id",
        "selector_policy_revision",
    ):
        inputs[field] = selector_report["revisions"][field]
    inputs["selector_input_digest"] = selector_input_digest(
        task=producer_request["task"],
        scope=producer_request["scope"],
        requirement_revision=inputs["requirement_revision"],
        inventory_snapshot_id=inputs["inventory_snapshot_id"],
        selector_policy_revision=inputs["selector_policy_revision"],
    )
    selector["decision_envelopes"] = [
        build_execution_target_selector_decision_envelope(producer_request)
    ]
    selector["selected_target_id"] = selector["decision_envelopes"][0][
        "selected_target_id"
    ]
    rebind_reservation(source)


def retarget_request(source: dict, target_id: str) -> None:
    binding = next(
        item for item in source["mapping"]["bindings"] if item["target_id"] == target_id
    )
    source["scope"]["target_id"] = target_id
    rule = copy.deepcopy(source["admission_policy"]["target_rules"][0])
    rule.update({"target_id": target_id, "provider_id": binding["provider_id"]})
    source["admission_policy"]["target_rules"] = [rule]
    resource = {
        "target_id": target_id,
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
        "mapping_revision": source["revisions"]["mapping_revision"],
        "policy_revision": source["revisions"]["policy_revision"],
        "identity_authority": "source_attested",
        "verified_at": binding["verified_at"],
        "expires_at": binding["expires_at"],
    }
    source["resource"] = resource
    currentness = source["currentness_evidence"]
    currentness["body"].update(
        {
            "target_id": target_id,
            "resource_key": resource["canonical_key"],
            "mapping_artifact_digest": stable_digest(source["mapping"]),
            "policy_artifact_digest": stable_digest(source["admission_policy"]),
        }
    )
    currentness["evidence_digest"] = stable_digest(currentness["body"])
    target_decision = gate_decision(
        provider_id=resource["provider_id"],
        quota_identity_id=resource["quota_identity_id"],
        scope_id=resource["scope_id"],
        window_id=resource["window_id"],
        reset_at=TARGET_RESET,
    )
    old_target_key = source["gates"]["target"]["decision_key"]
    source["gates"]["decisions"] = [
        target_decision if item["decision_key"] == old_target_key else item
        for item in source["gates"]["decisions"]
    ]
    source["gates"]["target"] = gate_tuple(target_decision)
    source["gates"]["state"] = gate_state(source["gates"]["decisions"])
    source["reservation"]["body"].update(
        {"target_id": target_id, "resource_key": resource["canonical_key"]}
    )
    source["feedback"]["body"].update(
        {"target_id": target_id, "resource_key": resource["canonical_key"]}
    )
    source["feedback"]["evidence_digest"] = stable_digest(source["feedback"]["body"])
    rebind_reservation(source)


def parse_time(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def set_gate(
    source: dict, name: str, *, action: str, reset_at: str | None = None
) -> None:
    updates = {"action": action}
    if reset_at is not None:
        updates["reset_at"] = reset_at
    update_gate_decision(source, name, **updates)


def update_gate_decision(source: dict, name: str, **updates: str) -> None:
    old_key = source["gates"][name]["decision_key"]
    decision = next(
        item for item in source["gates"]["decisions"] if item["decision_key"] == old_key
    )
    decision.update(updates)
    decision["resource_key"] = resource_gate_key(
        decision["provider_id"],
        decision["quota_identity_id"],
        decision["scope_id"],
        decision["window_id"],
    )
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

    def test_exact_bound_report_is_report_only_and_selector_envelope_resolved(
        self,
    ) -> None:
        report = simulate_capacity_reservation_feedback(build_request())
        self.assertEqual("would_reserve", report["preview"])
        self.assertEqual(
            ["report_only_reservation_preview_eligible"], report["reason_codes"]
        )
        self.assertTrue(report["manual_override_binding_resolved"])
        self.assertTrue(report["report_only"])
        self.assertFalse(report["selection_authority"])
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
            "selector_envelope_digest",
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

    def test_valid_override_envelope_precedes_capacity_in_branch_3a(self) -> None:
        source = build_request()
        target = source["scope"]["target_id"]
        set_selector_override(
            source,
            {
                "mode": "preference",
                "target_id": target,
                "reason": "bounded-operator-choice",
                "scope": "single_task",
                "allow_fallback": False,
                "provenance": "operator_override",
            },
        )
        report = simulate_capacity_reservation_feedback(source)
        envelope = report["simulation_request"]["selector_binding"][
            "decision_envelopes"
        ][0]
        self.assertEqual("operator_preference", envelope["disposition"])
        self.assertEqual("would_reserve", report["preview"])

    def test_preference_and_pin_skip_replay_valid_capacity_only_fail_closed(
        self,
    ) -> None:
        stale_shadow = shadow_request(
            capacity_bundle=capacity_bundle(freshness_status="stale")
        )
        stale_report = simulate_capacity_target_ordering_activation(
            simulation_request(stale_shadow)
        )
        missing_shadow = shadow_request()
        missing_shadow["preeligible_targets"][1]["binding"]["observation_id"] = (
            "missing-observation"
        )
        missing_report = simulate_capacity_target_ordering_activation(
            simulation_request(missing_shadow)
        )
        for report, mode, target in (
            (stale_report, "preference", "target-a"),
            (missing_report, "pin", "target-b"),
        ):
            with self.subTest(mode=mode, target=target):
                self.assertEqual("fail_closed", report["decision"])
                self.assertEqual(
                    {
                        "hard_constraints": "pass",
                        "exact_target_eligibility": "pass",
                        "quality_floor": "pass",
                    },
                    report["simulation_request"]["global_gate"],
                )
                source = build_request()
                set_selector_report(source, report)
                set_selector_override(source, routing_override(mode, target))
                if target != source["scope"]["target_id"]:
                    retarget_request(source, target)
                result = simulate_capacity_reservation_feedback(source)
                self.assertEqual("would_reserve", result["preview"])
                self.assertEqual(
                    target,
                    result["simulation_request"]["selector_binding"][
                        "selected_target_id"
                    ],
                )

    def test_override_target_can_differ_from_ordering_v1_counterfactual(self) -> None:
        alternative_report = simulate_capacity_target_ordering_activation(
            simulation_request(alternative_shadow_request())
        )
        self.assertEqual("target-b", alternative_report["counterfactual_target_id"])
        pin_baseline = build_request()
        set_selector_report(pin_baseline, alternative_report)
        set_selector_override(pin_baseline, routing_override("pin", "target-a"))
        first = simulate_capacity_reservation_feedback(pin_baseline)
        self.assertEqual("target-a", first["scope"]["target_id"])
        self.assertEqual("would_reserve", first["preview"])

        preference_alternative = build_request()
        self.assertEqual(
            "target-a",
            preference_alternative["selector_binding"]["activation_report"][
                "counterfactual_target_id"
            ],
        )
        set_selector_override(
            preference_alternative,
            routing_override("preference", "target-b"),
        )
        retarget_request(preference_alternative, "target-b")
        second = simulate_capacity_reservation_feedback(preference_alternative)
        self.assertEqual("target-b", second["scope"]["target_id"])
        self.assertEqual("would_reserve", second["preview"])

    def test_preference_fallback_uses_immutable_baseline_and_skips_v1(self) -> None:
        alternative_report = simulate_capacity_target_ordering_activation(
            simulation_request(alternative_shadow_request())
        )
        source = build_request()
        set_selector_report(source, alternative_report)
        set_selector_override(
            source,
            routing_override("preference", "target-unavailable", allow_fallback=True),
        )
        result = simulate_capacity_reservation_feedback(source)
        envelope = result["simulation_request"]["selector_binding"][
            "decision_envelopes"
        ][0]
        self.assertEqual("target-b", alternative_report["counterfactual_target_id"])
        self.assertEqual("operator_preference_fallback", envelope["disposition"])
        self.assertEqual("target-a", envelope["selected_target_id"])
        self.assertEqual("would_reserve", result["preview"])

    def test_authoritative_absence_obeys_replay_valid_v1_fail_closed(self) -> None:
        stale_report = simulate_capacity_target_ordering_activation(
            simulation_request(
                shadow_request(
                    capacity_bundle=capacity_bundle(freshness_status="stale")
                )
            )
        )
        source = build_request()
        set_selector_report(source, stale_report)
        result = simulate_capacity_reservation_feedback(source)
        envelope = result["simulation_request"]["selector_binding"][
            "decision_envelopes"
        ][0]
        self.assertEqual("authoritative_absence", envelope["disposition"])
        self.assertEqual("fail_closed", result["preview"])
        self.assertEqual(
            ["selector_activation_report_fail_closed"], result["reason_codes"]
        )

    def test_valid_override_does_not_bypass_global_or_selector_gates(self) -> None:
        global_gate = build_request()
        set_selector_override(global_gate, routing_override("preference", "target-a"))
        set_gate(global_gate, "global", action="defer")
        global_result = simulate_capacity_reservation_feedback(global_gate)
        self.assertEqual(["global_gate_gated"], global_result["reason_codes"])

        for field in (
            "hard_constraints",
            "exact_target_eligibility",
            "quality_floor",
        ):
            with self.subTest(field=field):
                gate = {
                    "hard_constraints": "pass",
                    "exact_target_eligibility": "pass",
                    "quality_floor": "pass",
                }
                gate[field] = "fail"
                ordering_report = simulate_capacity_target_ordering_activation(
                    simulation_request(shadow_request(), global_gate=gate)
                )
                source = build_request()
                set_selector_report(source, ordering_report)
                set_selector_override(
                    source, routing_override("preference", "target-a")
                )
                result = simulate_capacity_reservation_feedback(source)
                self.assertEqual("fail_closed", result["preview"])
                self.assertEqual(
                    ["selector_" + field + "_not_pass"], result["reason_codes"]
                )

    def test_fail_closed_override_envelope_never_reserves_or_retries(self) -> None:
        source = build_request()
        set_selector_override(
            source,
            {
                "mode": "pin",
                "target_id": "target-unavailable",
                "reason": "bounded-operator-choice",
                "scope": "single_task",
                "allow_fallback": False,
                "provenance": "operator_override",
            },
        )
        report = simulate_capacity_reservation_feedback(source)
        self.assertEqual("fail_closed", report["preview"])
        self.assertEqual(["manual_pin_unavailable"], report["reason_codes"])
        self.assertEqual("not_reserved", report["reservation_preview"]["status"])
        self.assertEqual("would_not_retry", report["retry_preview"]["status"])
        self.assertEqual([], report["half_open_preview"]["candidate_resource_keys"])

    def test_missing_duplicate_or_stale_envelope_rejected_for_branch_3a(self) -> None:
        missing = build_request()
        missing["selector_binding"]["decision_envelopes"] = []
        duplicate = build_request()
        duplicate["selector_binding"]["decision_envelopes"].append(
            copy.deepcopy(duplicate["selector_binding"]["decision_envelopes"][0])
        )
        duplicate_divergent = build_request()
        alternative = build_request()
        set_selector_override(
            alternative,
            {
                "mode": "preference",
                "target_id": alternative["scope"]["target_id"],
                "reason": "bounded-operator-choice",
                "scope": "single_task",
                "allow_fallback": False,
                "provenance": "operator_override",
            },
        )
        duplicate_divergent["selector_binding"]["decision_envelopes"].append(
            alternative["selector_binding"]["decision_envelopes"][0]
        )
        stale = build_request()
        producer_request = copy.deepcopy(
            stale["selector_binding"]["decision_envelopes"][0]["producer_request"]
        )
        producer_request["currentness"]["expires_at"] = "2030-01-02T04:05:00+00:00"
        body = copy.deepcopy(producer_request["currentness"])
        body.pop("currentness_digest")
        producer_request["currentness"]["currentness_digest"] = stable_digest(body)
        producer_request["evaluated_at"] = "2030-01-02T04:01:00+00:00"
        stale["selector_binding"]["decision_envelopes"] = [
            build_execution_target_selector_decision_envelope(producer_request)
        ]
        stale["replay"]["evaluated_at"] = "2030-01-02T04:06:00+00:00"
        for source in (missing, duplicate, duplicate_divergent, stale):
            with self.subTest():
                self.assert_rejected(source)

    def test_global_gate_key_mismatch_rejected(self) -> None:
        source = build_request()
        source["gates"]["global"]["decision_key"] = "decision-" + "a" * 64
        self.assert_rejected(source)

    def test_target_gate_key_mismatch_rejected(self) -> None:
        source = build_request()
        source["gates"]["target"]["wake_key"] = "wake-" + "b" * 64
        self.assert_rejected(source)

    def test_unrelated_but_canonical_global_gate_rejected(self) -> None:
        source = build_request()
        update_gate_decision(
            source,
            "global",
            provider_id="unrelated-provider",
            quota_identity_id="unrelated-quota",
            scope_id="unrelated-scope",
            window_id="unrelated-window",
        )
        self.assert_rejected(source)

    def test_future_target_gate_decision_rejected(self) -> None:
        source = build_request()
        update_gate_decision(
            source,
            "target",
            observed_at="2030-01-02T04:01:00+00:00",
        )
        self.assert_rejected(source)

    def test_expired_target_reset_and_wake_rejected(self) -> None:
        source = build_request()
        set_gate(
            source,
            "target",
            action="allow",
            reset_at="2030-01-02T03:59:00+00:00",
        )
        self.assert_rejected(source)

    def test_expired_reservation_earliest_boundary_rejected(self) -> None:
        source = build_request()
        source["reservation"]["body"]["expires_at"] = NOW
        source["reservation"]["evidence_digest"] = stable_digest(
            source["reservation"]["body"]
        )
        self.assert_rejected(source)

    def test_global_gate_precedes_target_gate(self) -> None:
        source = build_request()
        set_gate(source, "target", action="defer")
        set_gate(source, "global", action="defer")
        report = simulate_capacity_reservation_feedback(source)
        self.assertEqual(
            ["global_gate_gated"],
            report["reason_codes"],
        )
        self.assertNotIn("target_gate_gated", report["reason_codes"])

    def test_target_gate_applies_after_global_allowed(self) -> None:
        source = build_request()
        set_gate(source, "target", action="defer")
        report = simulate_capacity_reservation_feedback(source)
        self.assertEqual(
            ["target_gate_gated"],
            report["reason_codes"],
        )

    def test_recovery_is_validated_and_previews_one_exact_candidate(
        self,
    ) -> None:
        source = build_request()
        make_recovery(source)
        report = simulate_capacity_reservation_feedback(source)
        self.assertEqual(
            [source["resource"]["canonical_key"]],
            report["half_open_preview"]["candidate_resource_keys"],
        )
        self.assertEqual(
            "source_attested_recovery_preview_eligible",
            report["half_open_preview"]["reason"],
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

    def test_feedback_must_be_strictly_after_predecessor(self) -> None:
        for observed_at in (
            "2030-01-02T03:54:00+00:00",
            "2030-01-02T03:55:00+00:00",
        ):
            with self.subTest(observed_at=observed_at):
                source = build_request()
                add_predecessor(source)
                source["feedback"]["body"]["observed_at"] = observed_at
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

    def test_standalone_report_recursively_rejects_nested_bool_int_aliases(
        self,
    ) -> None:
        source = build_request()
        source["retry_budget"]["body"]["remaining"] = 0
        source["retry_budget"]["evidence_digest"] = stable_digest(
            source["retry_budget"]["body"]
        )
        report = simulate_capacity_reservation_feedback(source)
        for field, forged_value in (
            ("remaining_preview", False),
            ("separate_from_provider_quota", 1),
        ):
            with self.subTest(field=field):
                forged = copy.deepcopy(report)
                forged["retry_preview"][field] = forged_value
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
            self.assertEqual("would_reserve", json.loads(stdout.getvalue())["preview"])
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

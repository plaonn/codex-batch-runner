from __future__ import annotations

import copy
import unittest

from codex_batch_runner.capacity_gate_lifecycle_simulation import (
    LIFECYCLE_POLICY_REVISION,
    MUTATION_FIELDS,
    REPORT_CONTRACT,
    REQUEST_CONTRACT,
    ROLLBACK_RULE_ID,
    CapacityGateLifecycleSimulationError,
    simulate_capacity_gate_lifecycle_activation,
    stable_digest,
    validate_capacity_gate_lifecycle_simulation_report,
    validate_capacity_gate_lifecycle_simulation_request,
)
from codex_batch_runner.capacity_target_ordering_simulation import (
    simulate_capacity_target_ordering_activation,
)
from codex_batch_runner.provider_resource_authority import (
    GATE_STATE_CONTRACT,
    resource_gate_decision_key,
    resource_gate_key,
    resource_gate_wake_key,
)
from tests.test_capacity_target_ordering_simulation import (
    simulation_request as target_ordering_request,
)
from tests.test_provider_capacity_shadow import request as shadow_request
from tests.test_provider_resource_authority import mapping_v2, policy


NOW = "2030-01-02T06:30:00+00:00"
FIRST_OBSERVED = "2030-01-02T04:00:00+00:00"
FIRST_RESET = "2030-01-02T05:00:00+00:00"
SECOND_OBSERVED = "2030-01-02T05:01:00+00:00"
SECOND_RESET = "2030-01-02T07:00:00+00:00"


def gate_decision(
    *,
    observed_at: str = FIRST_OBSERVED,
    reset_at: str = FIRST_RESET,
    action: str = "defer",
    coverage_status: str = "not_covered",
    global_reset_at: str | None = None,
    supersedes: str | None = None,
    policy_revision: str = "policy-r1",
    mapping_revision: str = "mapping-r2",
) -> dict:
    value = {
        "schema_version": 1,
        "contract": "provider-resource-gate-decision-v1",
        "decision_key": "placeholder",
        "resource_key": resource_gate_key(
            "provider-a",
            "quota-a",
            "scope-a",
            "primary",
        ),
        "wake_key": resource_gate_wake_key(
            "provider-a",
            "quota-a",
            "scope-a",
            "primary",
            reset_at,
        ),
        "policy_revision": policy_revision,
        "mapping_revision": mapping_revision,
        "provider_id": "provider-a",
        "quota_identity_id": "quota-a",
        "scope_id": "scope-a",
        "window_id": "primary",
        "observed_at": observed_at,
        "reset_at": reset_at,
        "action": action,
        "global_coverage": {
            "status": coverage_status,
            "global_reset_at": global_reset_at,
        },
        "supersedes_decision_key": supersedes,
    }
    value["decision_key"] = resource_gate_decision_key(value)
    return value


def gate_state(*decisions: dict) -> dict:
    return {
        "schema_version": 1,
        "contract": GATE_STATE_CONTRACT,
        "migration": {
            "mode": "typed_primary_scalar_compatibility",
            "legacy_scalar_role": "global_gate_only",
            "rollback_mode": "disable_typed_evaluation_preserve_records",
            "evidence_history": "append_only",
        },
        "active_gates": [
            {
                "resource_key": decision["resource_key"],
                "decision_key": decision["decision_key"],
                "wake_key": decision["wake_key"],
                "reset_at": decision["reset_at"],
                "status": "active",
            }
            for decision in decisions
        ],
    }


def baseline(*decisions: dict) -> dict:
    state = gate_state(*decisions)
    history = [copy.deepcopy(decision) for decision in decisions]
    return {
        "gate_state": state,
        "gate_state_digest": stable_digest(state),
        "evidence_history": history,
        "evidence_history_digest": stable_digest(history),
        "legacy_scalar": {
            "role": "global_gate_only",
            "target_gate_projected": False,
        },
    }


def currentness(*, status: str = "current") -> dict:
    body = {
        "revision": "currentness-r1",
        "status": status,
        "mapping_revision": "mapping-r2",
        "admission_policy_revision": "policy-r1",
        "observed_at": "2030-01-02T03:50:00+00:00",
        "expires_at": "2030-01-02T07:00:00+00:00",
    }
    return {**body, "binding_digest": stable_digest(body)}


def evidence(
    *,
    kind: str = "threshold",
    synthetic: bool = False,
    remaining: int = 5,
    unit: str = "percent",
    freshness: str = "current",
    mapping_status: str = "exact",
) -> dict:
    return {
        "kind": kind,
        "synthetic": synthetic,
        "freshness": freshness,
        "mapping_status": mapping_status,
        "currentness_revision": "currentness-r1",
        "remaining": {"value": remaining, "unit": unit},
        "threshold": {"value": 5, "unit": unit},
    }


def event(
    decision: dict,
    *,
    event_id: str = "event-1",
    event_type: str = "decision",
    predecessor_event_id: str | None = None,
    global_gate_observation_id: str = "global-1",
    revalidates_decision_key: str | None = None,
    event_evidence: dict | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "predecessor_event_id": predecessor_event_id,
        "observed_at": decision["observed_at"],
        "global_gate_observation_id": global_gate_observation_id,
        "revalidates_decision_key": revalidates_decision_key,
        "decision": copy.deepcopy(decision),
        "evidence": copy.deepcopy(event_evidence or evidence()),
    }


def selector_report(*, resume_target_id: str | None = None) -> dict:
    return simulate_capacity_target_ordering_activation(
        target_ordering_request(
            shadow_request(),
            resume_target_id=resume_target_id,
        )
    )


def request(
    *,
    baseline_value: dict | None = None,
    events: list[dict] | None = None,
    observations: list[dict] | None = None,
    currentness_value: dict | None = None,
    mapping_value: dict | None = None,
    policy_value: dict | None = None,
    selector_report_value: dict | None = None,
    evaluated_at: str = NOW,
    rollback_active: bool = False,
) -> dict:
    mapping_item = copy.deepcopy(mapping_value or mapping_v2())
    policy_item = copy.deepcopy(policy_value or policy())
    currentness_item = copy.deepcopy(currentness_value or currentness())
    selector = copy.deepcopy(selector_report_value or selector_report())
    return {
        "schema_version": 1,
        "contract": REQUEST_CONTRACT,
        "scope": {
            "project_id": "public-project",
            "repository_id": "public-repository",
            "task_class": "bounded-capacity-objective",
            "opt_in_scope_id": "gate-lifecycle-opt-in-r1",
            "opted_in": True,
            "target_id": "target-a",
            "remaining_unit": "percent",
            "resource": {
                "provider_id": "provider-a",
                "quota_identity_id": "quota-a",
                "scope_id": "scope-a",
                "window_id": "primary",
            },
        },
        "baseline": copy.deepcopy(baseline_value or baseline()),
        "mapping": mapping_item,
        "admission_policy": policy_item,
        "revisions": {
            "mapping_revision": mapping_item["mapping_revision"],
            "admission_policy_revision": policy_item["policy_revision"],
            "currentness_revision": currentness_item["revision"],
            "lifecycle_policy_revision": LIFECYCLE_POLICY_REVISION,
        },
        "currentness": currentness_item,
        "selector_binding": {
            "report": selector,
            "report_digest": stable_digest(selector),
        },
        "global_gate_observations": copy.deepcopy(
            observations
            or [
                {
                    "observation_id": "global-1",
                    "status": "allowed",
                    "observed_at": "2030-01-02T03:59:00+00:00",
                    "reset_at": None,
                }
            ]
        ),
        "replay": {
            "evaluated_at": evaluated_at,
            "reset_grace_seconds": policy_item["timing"]["reset_grace_seconds"],
            "typed_evaluation_enabled": not rollback_active,
        },
        "rollback_rule": {
            "rule_id": ROLLBACK_RULE_ID,
            "disable_behavior": "stop_new_target_decisions",
            "typed_state_behavior": "preserve_append_only_evidence",
            "legacy_scalar_behavior": "remain_global_only",
            "rollback_active": rollback_active,
        },
        "events": copy.deepcopy(events or []),
    }


class CapacityGateLifecycleSimulationTests(unittest.TestCase):
    def test_first_low_resource_preview_preserves_baseline(self) -> None:
        decision = gate_decision()
        source = request(events=[event(decision)])
        before = copy.deepcopy(source["baseline"])

        report = simulate_capacity_gate_lifecycle_activation(source)

        self.assertEqual(REPORT_CONTRACT, report["contract"])
        self.assertEqual("would_defer", report["preview"])
        self.assertEqual(before, report["baseline"])
        self.assertEqual([], before["gate_state"]["active_gates"])
        self.assertEqual(
            decision["decision_key"],
            report["counterfactual_gate_state"]["active_gates"][0]["decision_key"],
        )
        self.assertEqual(
            "deferred",
            report["task_disposition_preview"]["counterfactual"],
        )
        self.assertEqual(
            decision["wake_key"],
            report["wake_registry_preview"]["would_register"][0]["wake_key"],
        )
        self.assert_report_only(report)

    def test_identical_decision_is_idempotent_and_conflict_rejects(
        self,
    ) -> None:
        decision = gate_decision()
        duplicate = event(
            decision,
            event_id="event-2",
            predecessor_event_id="event-1",
        )
        report = simulate_capacity_gate_lifecycle_activation(
            request(events=[event(decision), duplicate])
        )
        self.assertEqual("no_change", report["preview"])
        self.assertEqual(1, len(report["counterfactual_evidence_history"]))
        self.assertEqual(
            "idempotent_decision_duplicate",
            report["event_results"][1]["reason_codes"][0],
        )

        conflict_decision = copy.deepcopy(decision)
        conflict_decision["global_coverage"]["status"] = "not_evaluated"
        conflict = event(
            conflict_decision,
            event_id="event-2",
            predecessor_event_id="event-1",
        )
        with self.assertRaises(CapacityGateLifecycleSimulationError):
            validate_capacity_gate_lifecycle_simulation_request(
                request(events=[event(decision), conflict])
            )

    def test_covering_global_reset_never_previews_target_wake(self) -> None:
        decision = gate_decision(
            action="covered_by_global",
            coverage_status="covered",
            global_reset_at=FIRST_RESET,
        )
        report = simulate_capacity_gate_lifecycle_activation(
            request(
                events=[event(decision)],
                observations=[
                    {
                        "observation_id": "global-1",
                        "status": "gated",
                        "observed_at": "2030-01-02T03:59:00+00:00",
                        "reset_at": FIRST_RESET,
                    }
                ],
            )
        )
        self.assertEqual("covered_by_global", report["preview"])
        self.assertEqual(
            {
                "would_register": [],
                "would_replace": [],
                "would_remove": [],
            },
            report["wake_registry_preview"],
        )
        self.assertEqual([], report["counterfactual_gate_state"]["active_gates"])
        self.assert_report_only(report)

    def test_latest_global_observation_has_fixed_precedence(self) -> None:
        decision = gate_decision()
        observations = [
            {
                "observation_id": "global-allowed",
                "status": "allowed",
                "observed_at": "2030-01-02T03:50:00+00:00",
                "reset_at": None,
            },
            {
                "observation_id": "global-1",
                "status": "unknown",
                "observed_at": "2030-01-02T03:59:00+00:00",
                "reset_at": None,
            },
        ]
        stale_reference = event(
            decision,
            global_gate_observation_id="global-allowed",
        )
        stale = simulate_capacity_gate_lifecycle_activation(
            request(events=[stale_reference], observations=observations)
        )
        self.assertEqual("fail_closed", stale["preview"])
        self.assertEqual(
            ["global_gate_observation_not_latest"],
            stale["reason_codes"],
        )

        unknown = simulate_capacity_gate_lifecycle_activation(
            request(events=[event(decision)], observations=observations)
        )
        self.assertEqual("fail_closed", unknown["preview"])
        self.assertEqual(["global_gate_unknown"], unknown["reason_codes"])

    def test_same_time_global_observations_are_rejected_as_ambiguous(self) -> None:
        observations = [
            {
                "observation_id": "global-unknown",
                "status": "unknown",
                "observed_at": "2030-01-02T03:59:00+00:00",
                "reset_at": None,
            },
            {
                "observation_id": "global-1",
                "status": "allowed",
                "observed_at": "2030-01-02T03:59:00+00:00",
                "reset_at": None,
            },
        ]
        with self.assertRaisesRegex(
            CapacityGateLifecycleSimulationError,
            "strictly ordered",
        ):
            validate_capacity_gate_lifecycle_simulation_request(
                request(events=[event(gate_decision())], observations=observations)
            )

    def test_later_reset_supersedes_and_older_or_equal_fails_closed(
        self,
    ) -> None:
        original = gate_decision()
        later = gate_decision(
            observed_at="2030-01-02T04:10:00+00:00",
            reset_at="2030-01-02T06:00:00+00:00",
            supersedes=original["decision_key"],
        )
        report = simulate_capacity_gate_lifecycle_activation(
            request(
                baseline_value=baseline(original),
                events=[event(later)],
            )
        )
        self.assertEqual("would_supersede_gate", report["preview"])
        self.assertEqual(
            later["decision_key"],
            report["counterfactual_gate_state"]["active_gates"][0]["decision_key"],
        )
        self.assertEqual(
            original["decision_key"],
            report["wake_registry_preview"]["would_replace"][0][
                "previous_decision_key"
            ],
        )

        older = gate_decision(
            observed_at="2030-01-02T04:10:00+00:00",
            reset_at="2030-01-02T04:50:00+00:00",
            supersedes=original["decision_key"],
        )
        failed = simulate_capacity_gate_lifecycle_activation(
            request(
                baseline_value=baseline(original),
                events=[event(older)],
            )
        )
        self.assertEqual("fail_closed", failed["preview"])
        self.assertEqual(
            baseline(original)["gate_state"],
            failed["counterfactual_gate_state"],
        )

    def test_pre_reset_wake_remains_gated(self) -> None:
        original = gate_decision()
        recovery = gate_decision(
            observed_at="2030-01-02T04:30:00+00:00",
            reset_at="2030-01-02T06:00:00+00:00",
            action="allow",
        )
        report = simulate_capacity_gate_lifecycle_activation(
            request(
                baseline_value=baseline(original),
                events=[
                    event(
                        recovery,
                        event_type="wake_revalidation",
                        revalidates_decision_key=original["decision_key"],
                        event_evidence=evidence(
                            kind="recovery",
                            remaining=60,
                        ),
                    )
                ],
                evaluated_at="2030-01-02T04:45:00+00:00",
            )
        )
        self.assertEqual("would_revalidate_wake", report["preview"])
        self.assertEqual(
            original["decision_key"],
            report["counterfactual_gate_state"]["active_gates"][0]["decision_key"],
        )
        self.assertEqual(
            {
                "would_register": [],
                "would_replace": [],
                "would_remove": [],
            },
            report["wake_registry_preview"],
        )

    def test_post_grace_recovery_releases_counterfactually(self) -> None:
        original = gate_decision()
        recovery = gate_decision(
            observed_at=SECOND_OBSERVED,
            reset_at=SECOND_RESET,
            action="allow",
        )
        report = simulate_capacity_gate_lifecycle_activation(
            request(
                baseline_value=baseline(original),
                events=[
                    event(
                        recovery,
                        event_type="wake_revalidation",
                        revalidates_decision_key=original["decision_key"],
                        event_evidence=evidence(
                            kind="recovery",
                            remaining=60,
                        ),
                    )
                ],
            )
        )
        self.assertEqual("would_release", report["preview"])
        self.assertEqual([], report["counterfactual_gate_state"]["active_gates"])
        self.assertEqual(
            "released",
            report["task_disposition_preview"]["counterfactual"],
        )
        self.assertEqual(
            original["wake_key"],
            report["wake_registry_preview"]["would_remove"][0]["wake_key"],
        )

    def test_post_grace_continued_low_resource_regates(self) -> None:
        original = gate_decision()
        continued = gate_decision(
            observed_at=SECOND_OBSERVED,
            reset_at=SECOND_RESET,
            supersedes=original["decision_key"],
        )
        report = simulate_capacity_gate_lifecycle_activation(
            request(
                baseline_value=baseline(original),
                events=[
                    event(
                        continued,
                        event_type="wake_revalidation",
                        revalidates_decision_key=original["decision_key"],
                    )
                ],
            )
        )
        self.assertEqual("would_supersede_gate", report["preview"])
        self.assertNotEqual(original["decision_key"], continued["decision_key"])
        self.assertNotEqual(original["wake_key"], continued["wake_key"])

    def test_threshold_evidence_never_hard_excludes(self) -> None:
        report = simulate_capacity_gate_lifecycle_activation(
            request(events=[event(gate_decision())])
        )
        self.assertEqual("would_defer", report["preview"])
        self.assertFalse(report["hard_exclusion_authority"])
        self.assertEqual([], report["hard_exclusion_mutations"])

    def test_synthetic_confirmed_exhaustion_mechanics_are_non_authoritative(
        self,
    ) -> None:
        decision = gate_decision()
        report = simulate_capacity_gate_lifecycle_activation(
            request(
                events=[
                    event(
                        decision,
                        event_evidence=evidence(
                            kind="confirmed_exhaustion",
                            synthetic=True,
                            remaining=0,
                        ),
                    )
                ]
            )
        )
        self.assertEqual("would_hard_exclude", report["preview"])
        self.assertEqual(
            "hard_excluded",
            report["task_disposition_preview"]["counterfactual"],
        )
        self.assertFalse(report["natural_evidence_authority"])
        self.assertFalse(report["hard_exclusion_authority"])
        self.assert_report_only(report)

        unsupported = request(
            events=[
                event(
                    decision,
                    event_evidence=evidence(
                        kind="confirmed_exhaustion",
                        synthetic=False,
                        remaining=0,
                    ),
                )
            ]
        )
        with self.assertRaisesRegex(
            CapacityGateLifecycleSimulationError,
            "no trusted v1 authority",
        ):
            validate_capacity_gate_lifecycle_simulation_request(unsupported)

    def test_hard_exclusion_cannot_silently_replace_active_gate(self) -> None:
        original = gate_decision()
        unbound = gate_decision(
            observed_at=SECOND_OBSERVED,
            reset_at=SECOND_RESET,
        )
        failed = simulate_capacity_gate_lifecycle_activation(
            request(
                baseline_value=baseline(original),
                events=[
                    event(
                        unbound,
                        event_evidence=evidence(
                            kind="confirmed_exhaustion",
                            synthetic=True,
                            remaining=0,
                        ),
                    )
                ],
            )
        )
        self.assertEqual("fail_closed", failed["preview"])
        self.assertEqual(
            ["hard_exclusion_predecessor_mismatch"],
            failed["reason_codes"],
        )

        bound = gate_decision(
            observed_at=SECOND_OBSERVED,
            reset_at=SECOND_RESET,
            supersedes=original["decision_key"],
        )
        preview = simulate_capacity_gate_lifecycle_activation(
            request(
                baseline_value=baseline(original),
                events=[
                    event(
                        bound,
                        event_evidence=evidence(
                            kind="confirmed_exhaustion",
                            synthetic=True,
                            remaining=0,
                        ),
                    )
                ],
            )
        )
        self.assertEqual("would_hard_exclude", preview["preview"])
        self.assertEqual(
            original["decision_key"],
            preview["wake_registry_preview"]["would_replace"][0][
                "previous_decision_key"
            ],
        )
        self.assertEqual(
            bound["wake_key"],
            preview["wake_registry_preview"]["would_replace"][0]["wake_key"],
        )

    def test_first_gate_rejects_dangling_predecessor(self) -> None:
        for kind, synthetic, expected_reason in (
            ("threshold", False, "new_gate_predecessor_without_active_gate"),
            (
                "confirmed_exhaustion",
                True,
                "hard_exclusion_predecessor_without_active_gate",
            ),
        ):
            with self.subTest(kind=kind):
                decision = gate_decision(supersedes="decision-does-not-exist")
                report = simulate_capacity_gate_lifecycle_activation(
                    request(
                        events=[
                            event(
                                decision,
                                event_evidence=evidence(
                                    kind=kind,
                                    synthetic=synthetic,
                                    remaining=0,
                                ),
                            )
                        ]
                    )
                )
                self.assertEqual("fail_closed", report["preview"])
                self.assertEqual([expected_reason], report["reason_codes"])

    def test_rollback_preserves_baseline_and_legacy_scalar(self) -> None:
        original = gate_decision()
        source = request(
            baseline_value=baseline(original),
            events=[event(original)],
            rollback_active=True,
        )
        report = simulate_capacity_gate_lifecycle_activation(source)
        self.assertEqual("no_change", report["preview"])
        self.assertEqual(
            source["baseline"]["gate_state"],
            report["counterfactual_gate_state"],
        )
        self.assertEqual(
            source["baseline"]["evidence_history"],
            report["counterfactual_evidence_history"],
        )
        self.assertEqual(
            {
                "role": "global_gate_only",
                "target_gate_projected": False,
            },
            report["baseline"]["legacy_scalar"],
        )

    def test_incomplete_or_drifted_authority_fails_closed(self) -> None:
        decision = gate_decision()
        ambiguous_mapping = mapping_v2()
        duplicate = copy.deepcopy(ambiguous_mapping["bindings"][0])
        duplicate["binding_id"] = "binding-b"
        ambiguous_mapping["bindings"].append(duplicate)
        cases = (
            request(
                events=[event(decision)],
                currentness_value=currentness(status="stale"),
            ),
            request(
                events=[event(decision)],
                mapping_value=ambiguous_mapping,
            ),
            request(
                events=[
                    event(
                        decision,
                        event_evidence=evidence(freshness="stale"),
                    )
                ]
            ),
            request(
                events=[
                    event(
                        decision,
                        event_evidence=evidence(mapping_status="missing"),
                    )
                ]
            ),
            request(
                events=[
                    event(
                        decision,
                        event_evidence=evidence(unit="requests"),
                    )
                ]
            ),
        )
        for source in cases:
            with self.subTest():
                report = simulate_capacity_gate_lifecycle_activation(source)
                self.assertEqual("fail_closed", report["preview"])
                self.assertEqual(
                    source["baseline"]["gate_state"],
                    report["counterfactual_gate_state"],
                )

    def test_revision_resume_and_sequence_failures_fail_closed(self) -> None:
        drifted = gate_decision(policy_revision="policy-other")
        first = event(gate_decision())
        later = gate_decision(
            observed_at="2030-01-02T04:10:00+00:00",
            reset_at="2030-01-02T06:00:00+00:00",
        )
        broken = event(
            later,
            event_id="event-2",
            predecessor_event_id="wrong-event",
        )
        out_of_order = event(
            gate_decision(
                observed_at="2030-01-02T03:30:00+00:00",
                reset_at="2030-01-02T06:00:00+00:00",
            ),
            event_id="event-2",
            predecessor_event_id="event-1",
        )
        cases = (
            request(events=[event(drifted)]),
            request(
                events=[event(gate_decision())],
                selector_report_value=selector_report(resume_target_id="target-b"),
            ),
            request(events=[first, broken]),
            request(events=[first, out_of_order]),
        )
        for source in cases:
            with self.subTest():
                report = simulate_capacity_gate_lifecycle_activation(source)
                self.assertEqual("fail_closed", report["preview"])

    def test_baseline_active_gate_must_match_current_revisions(self) -> None:
        old = gate_decision(policy_revision="policy-old")
        report = simulate_capacity_gate_lifecycle_activation(
            request(
                baseline_value=baseline(old),
                events=[event(gate_decision())],
            )
        )
        self.assertEqual("fail_closed", report["preview"])
        self.assertEqual(
            ["baseline_active_gate_revision_mismatch"],
            report["reason_codes"],
        )

    def test_baseline_active_gate_requires_latest_defer_lineage(self) -> None:
        covered = gate_decision(
            action="covered_by_global",
            coverage_status="covered",
            global_reset_at=FIRST_RESET,
        )
        with self.assertRaisesRegex(
            CapacityGateLifecycleSimulationError,
            "must bind a defer decision",
        ):
            validate_capacity_gate_lifecycle_simulation_request(
                request(baseline_value=baseline(covered))
            )

        first = gate_decision()
        dangling = gate_decision(
            observed_at="2030-01-02T04:10:00+00:00",
            reset_at="2030-01-02T06:00:00+00:00",
            supersedes="decision-does-not-exist",
        )
        dangling_baseline = baseline(dangling)
        dangling_baseline["evidence_history"] = [first, dangling]
        dangling_baseline["evidence_history_digest"] = stable_digest(
            dangling_baseline["evidence_history"]
        )
        with self.assertRaisesRegex(
            CapacityGateLifecycleSimulationError,
            "immediately previous defer",
        ):
            validate_capacity_gate_lifecycle_simulation_request(
                request(baseline_value=dangling_baseline)
            )

        unbound = gate_decision(
            observed_at="2030-01-02T04:10:00+00:00",
            reset_at="2030-01-02T06:00:00+00:00",
        )
        unbound_baseline = baseline(unbound)
        unbound_baseline["evidence_history"] = [first, unbound]
        unbound_baseline["evidence_history_digest"] = stable_digest(
            unbound_baseline["evidence_history"]
        )
        with self.assertRaisesRegex(
            CapacityGateLifecycleSimulationError,
            "immediately previous defer",
        ):
            validate_capacity_gate_lifecycle_simulation_request(
                request(baseline_value=unbound_baseline)
            )

        second = gate_decision(
            observed_at="2030-01-02T04:10:00+00:00",
            reset_at="2030-01-02T06:00:00+00:00",
            supersedes=first["decision_key"],
        )
        divergent = gate_decision(
            observed_at="2030-01-02T04:20:00+00:00",
            reset_at="2030-01-02T07:00:00+00:00",
            supersedes=first["decision_key"],
        )
        divergent_baseline = baseline(divergent)
        divergent_baseline["evidence_history"] = [first, second, divergent]
        divergent_baseline["evidence_history_digest"] = stable_digest(
            divergent_baseline["evidence_history"]
        )
        with self.assertRaisesRegex(
            CapacityGateLifecycleSimulationError,
            "immediately previous defer",
        ):
            validate_capacity_gate_lifecycle_simulation_request(
                request(baseline_value=divergent_baseline)
            )

    def test_wake_duplicate_does_not_bypass_active_gate_binding(self) -> None:
        active = gate_decision()
        report = simulate_capacity_gate_lifecycle_activation(
            request(
                baseline_value=baseline(active),
                events=[
                    event(
                        active,
                        event_type="wake_revalidation",
                        revalidates_decision_key="decision-wrong",
                        event_evidence=evidence(
                            kind="recovery",
                            remaining=60,
                        ),
                    )
                ],
            )
        )
        self.assertEqual("fail_closed", report["preview"])
        self.assertEqual(
            ["wake_revalidation_active_gate_mismatch"],
            report["reason_codes"],
        )

    def test_strict_standalone_replay_rejects_forgery(self) -> None:
        source = request(events=[event(gate_decision())])
        first = simulate_capacity_gate_lifecycle_activation(source)
        second = simulate_capacity_gate_lifecycle_activation(copy.deepcopy(source))
        self.assertEqual(first, second)
        self.assertEqual(
            first["input_digest"], stable_digest(first["simulation_request"])
        )
        body = copy.deepcopy(first)
        claimed = body.pop("replay_digest")
        self.assertEqual(claimed, stable_digest(body))

        forged = copy.deepcopy(first)
        forged["preview"] = "would_hard_exclude"
        forged["reason_codes"] = ["synthetic_confirmed_exhaustion_mechanics_only"]
        forged_body = copy.deepcopy(forged)
        forged_body.pop("replay_digest")
        forged["replay_digest"] = stable_digest(forged_body)
        with self.assertRaises(CapacityGateLifecycleSimulationError):
            validate_capacity_gate_lifecycle_simulation_report(forged)

    def test_selector_binding_rejects_forged_predecessor_report(self) -> None:
        source = request(events=[event(gate_decision())])
        source["selector_binding"]["report"]["decision"] = "fail_closed"
        source["selector_binding"]["report_digest"] = stable_digest(
            source["selector_binding"]["report"]
        )
        with self.assertRaises(CapacityGateLifecycleSimulationError):
            validate_capacity_gate_lifecycle_simulation_request(source)

    def test_request_validator_rejects_non_finite_quantities(self) -> None:
        source = request(events=[event(gate_decision())])
        source["events"][0]["evidence"]["remaining"]["value"] = float("nan")
        with self.assertRaisesRegex(
            CapacityGateLifecycleSimulationError,
            "non-negative number",
        ):
            validate_capacity_gate_lifecycle_simulation_request(source)

    def test_permutation_and_malformed_literal_are_rejected_or_fail_closed(
        self,
    ) -> None:
        first_decision = gate_decision()
        second_decision = gate_decision(
            observed_at="2030-01-02T04:10:00+00:00",
            reset_at="2030-01-02T06:00:00+00:00",
            supersedes=first_decision["decision_key"],
        )
        first = event(first_decision)
        second = event(
            second_decision,
            event_id="event-2",
            predecessor_event_id="event-1",
        )
        permuted = request(events=[second, first])
        self.assertEqual(
            "fail_closed",
            simulate_capacity_gate_lifecycle_activation(permuted)["preview"],
        )

        malformed = request(events=[first])
        malformed["schema_version"] = True
        with self.assertRaises(CapacityGateLifecycleSimulationError):
            validate_capacity_gate_lifecycle_simulation_request(malformed)

    def assert_report_only(self, report: dict) -> None:
        self.assertTrue(report["simulation_only"])
        for field in (
            "activation_authority",
            "runtime_gate_mutation",
            "automatic_defer",
            "automatic_wake",
            "hard_exclusion_authority",
            "natural_evidence_authority",
            "live_routing",
            "default_routing",
            "worker_promotion",
            "provider_promotion",
        ):
            self.assertFalse(report[field])
        for field in MUTATION_FIELDS:
            self.assertEqual([], report[field])


if __name__ == "__main__":
    unittest.main()

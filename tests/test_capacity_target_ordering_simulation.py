from __future__ import annotations

import copy
import unittest

from codex_batch_runner.capacity_target_ordering_simulation import (
    MUTATION_FIELDS,
    REPORT_CONTRACT,
    REQUEST_CONTRACT,
    ROLLBACK_RULE_ID,
    SIMULATION_POLICY_REVISION,
    CapacityTargetOrderingSimulationError,
    simulate_capacity_target_ordering_activation,
    stable_digest,
    validate_capacity_target_ordering_simulation_report,
    validate_capacity_target_ordering_simulation_request,
)
from codex_batch_runner.provider_capacity_contract import (
    build_capacity_bundle,
    project_provider_resource_snapshot_capacity,
)
from codex_batch_runner.provider_capacity_shadow import (
    evaluate_capacity_shadow,
)
from tests.test_provider_capacity_shadow import (
    NOW,
    bundle,
    observation,
    request,
)


SCOPE = {
    "task_class": "bounded-readonly-objective",
    "project_id": "public-project",
    "repository_id": "public-repository",
    "opt_in_scope_id": "capacity-ordering-opt-in-r1",
    "opted_in": True,
}
GLOBAL_PASS = {
    "hard_constraints": "pass",
    "exact_target_eligibility": "pass",
    "quality_floor": "pass",
}


def simulation_request(
    shadow_request: dict,
    *,
    global_gate: dict | None = None,
    resume_target_id: str | None = None,
) -> dict:
    shadow_report = evaluate_capacity_shadow(shadow_request)
    revisions = shadow_request["revisions"]
    return {
        "schema_version": 1,
        "contract": REQUEST_CONTRACT,
        "evaluated_at": shadow_request["evaluated_at"],
        "scope": copy.deepcopy(SCOPE),
        "revisions": {
            "requirement_revision": revisions["requirement_revision"],
            "inventory_snapshot_id": revisions["inventory_snapshot_id"],
            "selector_policy_revision": revisions["selector_policy_revision"],
            "mapping_revision": revisions["mapping_revision"],
            "authority_revision": revisions["authority_revision"],
            "capacity_bundle_revision": revisions["capacity_bundle_revision"],
            "currentness_digest": stable_digest(shadow_request["revision_currentness"]),
            "simulation_policy_revision": SIMULATION_POLICY_REVISION,
        },
        "global_gate": copy.deepcopy(global_gate or GLOBAL_PASS),
        "resume_target_id": resume_target_id,
        "baseline_binding": {
            "decision_digest": shadow_report["baseline"]["decision_digest"],
            "selected_target_id": shadow_report["baseline"]["selected_target_id"],
            "ordered_eligible_target_ids": shadow_report["preeligible_target_ids"],
        },
        "shadow_binding": {
            "request_digest": stable_digest(shadow_request),
            "report_digest": shadow_report["report_hash"],
        },
        "rollback_rule": {
            "rule_id": ROLLBACK_RULE_ID,
            "on_any_ineligible_input": "keep_baseline",
            "baseline_source": "immutable_shadow_baseline",
            "mutation_allowed": False,
        },
        "shadow_request": copy.deepcopy(shadow_request),
        "shadow_report": shadow_report,
    }


def alternative_shadow_request(
    *,
    remaining_unit: str = "percent",
    window_duration_seconds: int = 18_000,
) -> dict:
    first = observation()
    second_snapshot = copy.deepcopy(first["canonical_snapshot"])
    second_snapshot["snapshot_id"] = "snapshot-r2"
    second_snapshot["resource"]["quota_identity"]["id"] = "quota-second"
    second_snapshot["windows"][0]["remaining"].update(
        {"value": 95, "unit": remaining_unit}
    )
    second_snapshot["windows"][0]["window_duration_seconds"] = window_duration_seconds
    second = project_provider_resource_snapshot_capacity(
        second_snapshot,
        evaluated_at=NOW,
        max_age_seconds=300,
    )
    first_resource = first["resources"][0]
    second_resource = second["resources"][0]
    capacity_bundle = build_capacity_bundle(
        [first, second],
        pools=[
            {
                "pool_id": "pool-first",
                "provider_id": "provider-example",
                "resource_ids": [first_resource["resource_id"]],
                "binding_status": "explicit",
                "source_revision": "mapping-r1",
            },
            {
                "pool_id": "pool-second",
                "provider_id": "provider-example",
                "resource_ids": [second_resource["resource_id"]],
                "binding_status": "explicit",
                "source_revision": "mapping-r1",
            },
        ],
    )
    value = request(capacity_bundle=capacity_bundle)
    value["revisions"]["capacity_bundle_revision"] = capacity_bundle["bundle_id"]
    value["revision_currentness"]["current_revisions"]["capacity_bundle_revision"] = (
        capacity_bundle["bundle_id"]
    )
    value["provider_resource_lineage"]["snapshot_ids"] = [
        "snapshot-r1",
        "snapshot-r2",
    ]
    for index, (target, resource, observed, quota, pool) in enumerate(
        (
            (
                value["preeligible_targets"][0],
                first_resource,
                first,
                "quota-shared",
                "pool-first",
            ),
            (
                value["preeligible_targets"][1],
                second_resource,
                second,
                "quota-second",
                "pool-second",
            ),
        )
    ):
        target["binding"].update(
            {
                "observation_id": observed["observation_id"],
                "resource_id": resource["resource_id"],
                "quota_identity_id": quota,
                "capacity_pool": pool,
                "constraint_id": resource["constraints"][0]["constraint_id"],
                "remaining_unit": resource["constraints"][0]["remaining"]["unit"],
            }
        )
        value["provider_resource_mapping"]["bindings"][index].update(
            {
                "capacity_pool": pool,
                "quota_identity_id": quota,
            }
        )
    return value


def three_target_shadow_request() -> dict:
    value = request()
    third = copy.deepcopy(value["preeligible_targets"][1])
    third["target_id"] = "target-c"
    third["selector_rank"] = 2
    third["binding"].update(
        {
            "binding_id": "binding-target-c",
            "target_id": "target-c",
            "model_id": "model-c",
        }
    )
    value["preeligible_targets"].append(third)
    mapping = copy.deepcopy(value["provider_resource_mapping"]["bindings"][1])
    mapping.update(
        {
            "binding_id": "binding-target-c",
            "target_id": "target-c",
        }
    )
    value["provider_resource_mapping"]["bindings"].append(mapping)
    value["baseline"]["selector_order"].append("target-c")
    value["baseline"]["decision"]["ranked_target_ids"].append("target-c")
    value["baseline"]["decision_digest"] = stable_digest(value["baseline"]["decision"])
    return value


class CapacityTargetOrderingSimulationTests(unittest.TestCase):
    def test_counterfactual_reorders_only_already_eligible_targets(self) -> None:
        source = alternative_shadow_request()
        baseline_before = copy.deepcopy(source["baseline"])

        report = simulate_capacity_target_ordering_activation(
            simulation_request(source)
        )

        self.assertEqual(REPORT_CONTRACT, report["contract"])
        self.assertEqual("would_select_alternative", report["decision"])
        self.assertEqual("target-a", report["baseline"]["selected_target_id"])
        self.assertEqual(["target-a", "target-b"], report["baseline_order"])
        self.assertEqual("target-b", report["counterfactual_target_id"])
        self.assertEqual(["target-b", "target-a"], report["counterfactual_order"])
        self.assertEqual(baseline_before, report["baseline"])
        self.assertEqual(
            {"target-a", "target-b"},
            set(report["counterfactual_order"]),
        )
        self.assert_report_only(report)

    def test_keep_baseline_is_deterministic_and_digest_bound(self) -> None:
        source = simulation_request(request())
        first = simulate_capacity_target_ordering_activation(source)
        second = simulate_capacity_target_ordering_activation(copy.deepcopy(source))

        self.assertEqual(first, second)
        self.assertEqual("keep_baseline", first["decision"])
        self.assertEqual(first["baseline_order"], first["counterfactual_order"])
        body = copy.deepcopy(first)
        claimed = body.pop("simulation_digest")
        self.assertEqual(claimed, stable_digest(body))
        self.assert_report_only(first)

    def test_revision_drift_stale_and_untrusted_identity_fail_closed(self) -> None:
        drift = request()
        drift["revision_currentness"]["current_revisions"]["mapping_revision"] = (
            "mapping-r2"
        )
        cases = (
            drift,
            request(capacity_bundle=bundle(freshness_status="stale")),
            request(capacity_bundle=bundle(verified_identity=False)),
        )
        for source in cases:
            with self.subTest():
                report = simulate_capacity_target_ordering_activation(
                    simulation_request(source)
                )
                self.assertEqual("fail_closed", report["decision"])
                self.assertEqual(
                    report["baseline_order"],
                    report["counterfactual_order"],
                )
                self.assert_report_only(report)

    def test_conflict_and_unit_window_mismatch_fail_closed(self) -> None:
        first = observation()
        conflicting_snapshot = copy.deepcopy(first["canonical_snapshot"])
        conflicting_snapshot["snapshot_id"] = "snapshot-r2"
        conflicting_snapshot["windows"][0]["remaining"]["value"] = 50
        conflicting = project_provider_resource_snapshot_capacity(
            conflicting_snapshot,
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        conflict_source = request(
            capacity_bundle=build_capacity_bundle([first, conflicting])
        )
        unit_mismatch = alternative_shadow_request(remaining_unit="requests")
        window_mismatch = alternative_shadow_request(window_duration_seconds=3_600)
        for source in (conflict_source, unit_mismatch, window_mismatch):
            with self.subTest():
                report = simulate_capacity_target_ordering_activation(
                    simulation_request(source)
                )
                self.assertEqual("fail_closed", report["decision"])
                self.assert_report_only(report)

    def test_missing_and_ambiguous_evidence_never_select_alternative(
        self,
    ) -> None:
        missing = request()
        missing["preeligible_targets"][1]["binding"]["observation_id"] = (
            "missing-observation"
        )
        missing_report = simulate_capacity_target_ordering_activation(
            simulation_request(missing)
        )
        self.assertEqual("fail_closed", missing_report["decision"])

        ambiguous = simulation_request(request())
        ambiguous["shadow_request"]["provider_resource_mapping"]["bindings"].append(
            copy.deepcopy(
                ambiguous["shadow_request"]["provider_resource_mapping"]["bindings"][1]
            )
        )
        with self.assertRaises(CapacityTargetOrderingSimulationError):
            simulate_capacity_target_ordering_activation(ambiguous)

    def test_global_gates_precede_capacity(self) -> None:
        for field in GLOBAL_PASS:
            gate = copy.deepcopy(GLOBAL_PASS)
            gate[field] = "fail"
            report = simulate_capacity_target_ordering_activation(
                simulation_request(alternative_shadow_request(), global_gate=gate)
            )
            self.assertEqual("fail_closed", report["decision"])
            self.assertIn(f"global_{field}_fail", report["reason_codes"])
            self.assertEqual(report["baseline_order"], report["counterfactual_order"])

    def test_resume_target_pinning_precedes_capacity(self) -> None:
        pinned = simulate_capacity_target_ordering_activation(
            simulation_request(
                alternative_shadow_request(),
                resume_target_id="target-a",
            )
        )
        self.assertEqual("keep_baseline", pinned["decision"])
        self.assertIn("resume_target_pinned", pinned["reason_codes"])
        mismatch = simulate_capacity_target_ordering_activation(
            simulation_request(
                alternative_shadow_request(),
                resume_target_id="target-b",
            )
        )
        self.assertEqual("fail_closed", mismatch["decision"])
        self.assertIn("resume_target_baseline_mismatch", mismatch["reason_codes"])

    def test_strict_request_rejects_malformed_or_drifted_bindings(self) -> None:
        source = simulation_request(request())
        malformed = copy.deepcopy(source)
        malformed["unexpected"] = True
        drifted = copy.deepcopy(source)
        drifted["baseline_binding"]["selected_target_id"] = "target-b"
        forged_shadow = copy.deepcopy(source)
        forged_shadow["shadow_report"]["baseline"]["selected_target_id"] = "target-b"
        ineligible = simulation_request(request())
        ineligible["shadow_request"]["preeligible_targets"][1]["quality_floor_pass"] = (
            False
        )
        for value in (malformed, drifted, forged_shadow, ineligible):
            with self.subTest():
                with self.assertRaises(CapacityTargetOrderingSimulationError):
                    validate_capacity_target_ordering_simulation_request(value)

    def test_report_validator_rejects_authority_or_digest_tampering(self) -> None:
        report = simulate_capacity_target_ordering_activation(
            simulation_request(request())
        )
        authority = copy.deepcopy(report)
        authority["live_routing"] = True
        digest = copy.deepcopy(report)
        digest["decision"] = "would_select_alternative"
        baseline = copy.deepcopy(report)
        baseline["baseline"]["decision"]["selected_target_id"] = "target-b"
        baseline_body = copy.deepcopy(baseline)
        baseline_body.pop("simulation_digest")
        baseline["simulation_digest"] = stable_digest(baseline_body)
        for value in (authority, digest, baseline):
            with self.subTest():
                with self.assertRaises(CapacityTargetOrderingSimulationError):
                    validate_capacity_target_ordering_simulation_report(value)

    def test_report_validator_replays_source_and_preserves_suffix_order(
        self,
    ) -> None:
        report = simulate_capacity_target_ordering_activation(
            simulation_request(three_target_shadow_request())
        )
        forged = copy.deepcopy(report)
        forged.update(
            {
                "decision": "would_select_alternative",
                "reason_codes": ["capacity_reorders_already_eligible_targets"],
                "counterfactual_target_id": "target-c",
                "counterfactual_order": ["target-c", "target-b", "target-a"],
                "input_digest": "sha256:" + ("f" * 64),
            }
        )
        body = copy.deepcopy(forged)
        body.pop("simulation_digest")
        forged["simulation_digest"] = stable_digest(body)
        with self.assertRaises(CapacityTargetOrderingSimulationError):
            validate_capacity_target_ordering_simulation_report(forged)

    def test_boolean_integer_literal_aliases_are_rejected(self) -> None:
        base = simulation_request(alternative_shadow_request())
        schema = copy.deepcopy(base)
        schema["schema_version"] = True
        opt_in = copy.deepcopy(base)
        opt_in["scope"]["opted_in"] = 1
        rollback = copy.deepcopy(base)
        rollback["rollback_rule"]["mutation_allowed"] = 0
        nested_gate = copy.deepcopy(base)
        nested_gate["shadow_request"]["preeligible_targets"][0][
            "hard_constraints_pass"
        ] = 1
        for value in (schema, opt_in, rollback, nested_gate):
            with self.subTest():
                with self.assertRaises(CapacityTargetOrderingSimulationError):
                    simulate_capacity_target_ordering_activation(value)

    def assert_report_only(self, report: dict) -> None:
        self.assertTrue(report["simulation_only"])
        for field in (
            "activation_authority",
            "live_routing",
            "default_routing",
            "automatic_substitution",
            "selection_or_dispatch_authority",
            "worker_promotion",
            "provider_promotion",
            "actual_canary",
            "synthetic_evidence_authority",
        ):
            self.assertFalse(report[field])
        for field in MUTATION_FIELDS:
            self.assertEqual([], report[field])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from codex_batch_runner.safe_exploration import ExplorationError, build_probe_record, exploration_admission


POLICY = {"exploration_policy_version": "exploration-reviewed-v1", "selection_probability": .1}


def context(**changes):
    values = {
        "probe_kind": "uncertainty_probe",
        "hard_constraints_pass": True,
        "unknown_policy": "reject",
        "failure_cost": "low",
        "boundaries": [],
        "objective_verification": "strong",
        "rollback_available": True,
        "baseline_fallback_available": True,
        "budget_remaining": 1,
        "target_state": "probe_only",
        "active_project_probes": 0,
        "same_target_region_adverse": False,
        "eligible_candidates": ["baseline", "probe"],
        "chosen_target": "probe",
        "baseline_target": "baseline",
    }
    values.update(changes)
    return values


class SafeExplorationTests(unittest.TestCase):
    def test_admitted_probe_records_probability_and_candidates(self) -> None:
        admission = exploration_admission(context(), POLICY)
        self.assertTrue(admission["admitted"])
        record = build_probe_record(admission, project_key="public-project", requirement_region_id="region-1")
        self.assertEqual(record["selection_probability"], .1)
        self.assertTrue(record["causally_comparable"])
        self.assertEqual(record["eligible_candidates"], ["baseline", "probe"])

    def test_high_cost_and_sensitive_boundaries_are_never_admitted(self) -> None:
        admission = exploration_admission(context(failure_cost="high", boundaries=["financial"]), POLICY)
        self.assertFalse(admission["admitted"])
        self.assertIn("failure_cost_not_eligible", admission["reasons"])
        self.assertIn("prohibited_boundary", admission["reasons"])

    def test_concurrency_budget_and_adverse_cooldown_guards(self) -> None:
        admission = exploration_admission(context(active_project_probes=1, budget_remaining=0, same_target_region_adverse=True), POLICY)
        self.assertEqual(
            set(admission["reasons"]),
            {"budget_exhausted", "project_probe_concurrency_limit", "same_target_region_cooldown"},
        )
        self.assertTrue(admission["cooldown_required"])

    def test_probe_only_unknown_policy_can_pass_hard_constraint_unknown(self) -> None:
        self.assertTrue(exploration_admission(context(hard_constraints_pass=False, unknown_policy="probe_only"), POLICY)["admitted"])

    def test_exploration_rate_is_never_invented(self) -> None:
        with self.assertRaisesRegex(ExplorationError, "selection probability"):
            exploration_admission(context(), {"exploration_policy_version": "v1"})
        denied = exploration_admission(context(target_state="degraded"), POLICY)
        with self.assertRaisesRegex(ExplorationError, "not admitted"):
            build_probe_record(denied, project_key="project", requirement_region_id="region")

    def test_all_probe_kinds_are_versioned_enum(self) -> None:
        for kind in ("downshift_probe", "availability_probe", "uncertainty_probe", "upshift_guard"):
            self.assertTrue(exploration_admission(context(probe_kind=kind), POLICY)["admitted"])
        self.assertFalse(exploration_admission(context(probe_kind="thompson_sampling"), POLICY)["admitted"])


if __name__ == "__main__":
    unittest.main()

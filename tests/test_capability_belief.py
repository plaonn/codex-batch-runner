from __future__ import annotations

import unittest

from codex_batch_runner.capability_belief import (
    CapabilityBeliefError,
    attach_outcome_projection,
    build_outcome_projection,
    rebuild_capability_report,
    transition_trust_state,
)


VERSIONS = {
    "execution_evidence": "execution-evidence-v3",
    "requirement": "model-requirement-v2",
    "rubric": "requirement-rubric-v1",
    "constraint_registry": "constraint-registry-v1",
    "target_contract": "execution-target-v1",
    "quality_outcome": "quality-outcome-v1",
    "review_policy": "review-policy-v2",
    "review_rubric": "review-rubric-v2",
    "posterior": "capability-posterior-v1",
    "decay": "decay-reviewed-v1",
    "drift": "capability-drift-v1",
    "exploration": "safe-exploration-v1",
    "cohort": "capability-cohort-v1",
}
EPOCH = {
    "epoch_id": "epoch-1",
    "model_alias": "alias-revision-1",
    "cli_major": "1",
    "provider_behavior": "provider-contract-1",
    "target_contract": "execution-target-v1",
    "review_outcome_contract": "review-outcome-v1",
}
POLICY = {
    "decay_policy_version": "decay-reviewed-v1",
    "half_life_days": 10,
    "beta_prior": {"alpha": 1, "beta": 1},
    "dirichlet_prior": {
        "first_pass_pass": 1,
        "minor_fix": 1,
        "major_fix": 1,
        "reject": 1,
        "indeterminate": 1,
    },
    "drift_policy": {
        "version": "drift-reviewed-v1",
        "freshness_days": 30,
        "recent_window_days": 7,
        "baseline_window_days": 30,
        "minimum_effective_samples": 1,
        "adverse_floor_pass_delta": .25,
    },
}


def projection(**changes):
    values = {
        "root_lineage_id": "root-1",
        "attempt": 0,
        "captured_at": "2026-07-12T00:00:00+00:00",
        "target_id": "codex-exact-high",
        "requirement_region": {"semantic_reasoning": 750, "instruction_fidelity": 1000},
        "versions": VERSIONS,
        "epoch": EPOCH,
        "first_pass_outcome": "first_pass_pass",
        "recovery_outcome": "first_pass_pass",
        "token_usage": {"cached_input": 10, "uncached_input": 20, "output": 30, "reasoning": 40},
        "latency_seconds": 8,
    }
    values.update(changes)
    return build_outcome_projection(**values)


class CapabilityBeliefTests(unittest.TestCase):
    def test_projection_is_append_only_and_exact_v3_only(self) -> None:
        history = []
        record = projection()
        attach_outcome_projection(history, record)
        attach_outcome_projection(history, record)
        self.assertEqual(len(history), 1)
        with self.assertRaisesRegex(CapabilityBeliefError, "exact execution evidence v3"):
            projection(versions={**VERSIONS, "execution_evidence": "execution-evidence-v2"})

    def test_five_anchor_regions_only(self) -> None:
        with self.assertRaisesRegex(CapabilityBeliefError, "five accepted anchor bins"):
            projection(requirement_region={"semantic_reasoning": 600})

    def test_root_lineage_is_one_sample_and_first_pass_stays_separate(self) -> None:
        records = [
            projection(first_pass_outcome="major_fix", recovery_outcome="major_fix"),
            projection(attempt=1, captured_at="2026-07-12T01:00:00+00:00", first_pass_captured_at="2026-07-12T00:00:00+00:00", first_pass_outcome="major_fix", recovery_outcome="first_pass_pass"),
        ]
        report = rebuild_capability_report(records, policy=POLICY, as_of="2026-07-13T00:00:00+00:00")
        self.assertEqual(report["independent_root_count"], 1)
        self.assertEqual(report["deduplicated_attempt_count"], 1)
        quality = report["cohorts"][0]["quality"]
        self.assertGreater(quality["weighted_first_pass_outcomes"]["major_fix"], 0)
        self.assertGreater(quality["weighted_recovery_inclusive_outcomes"]["first_pass_pass"], 0)
        self.assertEqual(quality["beta_floor_pass"]["alpha"], 1)
        self.assertGreater(quality["beta_floor_pass"]["beta"], 1)

    def test_indeterminate_does_not_update_beta(self) -> None:
        report = rebuild_capability_report(
            [projection(first_pass_outcome="indeterminate", recovery_outcome="indeterminate")],
            policy=POLICY,
            as_of="2026-07-13T00:00:00+00:00",
        )
        beta = report["cohorts"][0]["quality"]["beta_floor_pass"]
        self.assertEqual((beta["alpha"], beta["beta"]), (1, 1))

    def test_availability_failure_is_not_quality_failure_and_latency_is_censored(self) -> None:
        record = projection(availability_outcome="timeout", latency_censored=True)
        report = rebuild_capability_report([record], policy=POLICY, as_of="2026-07-13T00:00:00+00:00")
        cohort = report["cohorts"][0]
        self.assertEqual(cohort["quality"]["beta_floor_pass"]["mean"], .5)
        self.assertGreater(cohort["availability"]["weighted_outcomes"]["timeout"], 0)
        self.assertEqual(cohort["latency"]["completed"]["count"], 0)
        self.assertEqual(cohort["latency"]["censored"]["count"], 1)
        self.assertGreater(cohort["latency"]["censored"]["weighted_count"], 0)

    def test_decay_and_weighted_token_summaries_are_rebuildable(self) -> None:
        report = rebuild_capability_report(
            [projection(root_lineage_id="root-1"), projection(root_lineage_id="root-2", captured_at="2026-07-02T00:00:00+00:00")],
            policy=POLICY,
            as_of="2026-07-13T00:00:00+00:00",
        )
        cohort = report["cohorts"][0]
        self.assertGreater(cohort["sample"]["first_pass_effective_size"], 1)
        for field in ("cached_input", "uncached_input", "output", "reasoning"):
            self.assertIsNotNone(cohort["tokens"][field]["p95"])
            self.assertIsNotNone(cohort["tokens"][field]["log1p_weighted_mean"])

    def test_policy_values_must_be_explicit_and_versioned(self) -> None:
        with self.assertRaisesRegex(CapabilityBeliefError, "half-life"):
            rebuild_capability_report([projection()], policy={**POLICY, "half_life_days": 0}, as_of="2026-07-13T00:00:00+00:00")

    def test_only_accepted_trust_transitions(self) -> None:
        self.assertEqual(transition_trust_state("trusted", "degraded"), "degraded")
        self.assertEqual(transition_trust_state("unavailable", "cooldown"), "cooldown")
        with self.assertRaisesRegex(CapabilityBeliefError, "invalid trust transition"):
            transition_trust_state("trusted", "unavailable")

    def test_import_validation_rejects_tampered_derived_fields(self) -> None:
        record = projection(availability_outcome="timeout", latency_censored=True)
        record["quality"]["availability_excluded"] = False
        with self.assertRaisesRegex(CapabilityBeliefError, "derived quality eligibility"):
            rebuild_capability_report([record], policy=POLICY, as_of="2026-07-13T00:00:00+00:00")

    def test_cross_cohort_retry_is_not_a_second_sample(self) -> None:
        with self.assertRaisesRegex(CapabilityBeliefError, "cohort boundary"):
            rebuild_capability_report(
                [projection(), projection(attempt=1, target_id="other-target", first_pass_captured_at="2026-07-12T00:00:00+00:00")],
                policy=POLICY,
                as_of="2026-07-13T00:00:00+00:00",
            )

    def test_retry_does_not_refresh_first_pass_decay_weight(self) -> None:
        retry = projection(
            attempt=1,
            captured_at="2026-07-12T00:00:00+00:00",
            first_pass_captured_at="2026-06-20T00:00:00+00:00",
        )
        report = rebuild_capability_report([retry], policy=POLICY, as_of="2026-07-13T00:00:00+00:00")
        beta = report["cohorts"][0]["quality"]["beta_floor_pass"]
        self.assertLess(beta["alpha"], 1.5)

    def test_drift_is_read_only_and_uses_explicit_policy_thresholds(self) -> None:
        records = [
            projection(root_lineage_id="old", captured_at="2026-06-20T00:00:00+00:00"),
            projection(root_lineage_id="new", captured_at="2026-07-12T00:00:00+00:00", first_pass_outcome="reject", recovery_outcome="reject"),
        ]
        drift = rebuild_capability_report(records, policy=POLICY, as_of="2026-07-13T00:00:00+00:00")["cohorts"][0]["freshness_and_drift"]
        self.assertEqual(drift["status"], "adverse")
        self.assertEqual(drift["proposed_transition"], {"from": "trusted", "to": "degraded"})
        self.assertFalse(drift["mutation"]["applied"])


if __name__ == "__main__":
    unittest.main()

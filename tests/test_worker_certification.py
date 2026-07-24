from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_batch_runner.worker_certification import (
    BOUNDARY_SCENARIOS,
    INITIAL_CANARY_BASIS_POINTS,
    WorkerCertificationError,
    certify_worker,
    simulate_report_only_canary,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "worker-certification-bounded-write-v1.json"
)
NOW = datetime(2030, 1, 2, 4, 0, tzinfo=timezone.utc)


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class WorkerCertificationTests(unittest.TestCase):
    def test_bounded_write_matrix_covers_every_required_boundary(self) -> None:
        value = fixture()
        result = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(result["state"], "eligible-bounded-write")
        self.assertEqual(result["advisory_decision"], "eligible")
        self.assertEqual(
            {item["scenario"] for item in result["coverage"]},
            BOUNDARY_SCENARIOS,
        )
        self.assertTrue(
            all(
                item["status"] == "passed"
                for item in result["coverage"]
                if item["required"]
            )
        )
        self.assertTrue(result["comparability"]["execution_quality"])
        self.assertFalse(result["comparability"]["token_cost"])
        self.assertFalse(result["comparability"]["monetary_cost"])
        self.assertEqual(result["expires_at"], "2030-02-01T04:00:00+00:00")

    def test_synthetic_matrix_cannot_promote_without_natural_samples(self) -> None:
        value = fixture()
        synthetic = [
            item for item in value["evidence"]["records"]
            if item["evidence_class"] == "synthetic"
        ]
        value["evidence"]["records"] = synthetic

        result = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(result["state"], "experimental-private")
        self.assertEqual(result["advisory_decision"], "unknown")
        self.assertFalse(result["comparability"]["execution_quality"])

    def test_missing_cost_attestation_does_not_block_quality_certification(self) -> None:
        value = fixture()
        result = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(result["state"], "eligible-bounded-write")
        self.assertFalse(result["comparability"]["token_cost"])
        self.assertFalse(result["comparability"]["monetary_cost"])

        natural = value["evidence"]["records"][-1]
        natural["token_usage_attested"] = True
        attested = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )
        self.assertTrue(attested["comparability"]["token_cost"])
        self.assertFalse(attested["comparability"]["monetary_cost"])

    def test_unknown_cost_field_is_rejected(self) -> None:
        value = fixture()
        value["evidence"]["records"][-1]["cost"] = 1
        with self.assertRaisesRegex(
            WorkerCertificationError, "unsupported fields"
        ):
            certify_worker(
                value["candidate"],
                value["evidence"],
                evaluated_at=NOW,
            )

    def test_large_high_quality_natural_cohort_is_only_a_default_candidate(self) -> None:
        value = fixture()
        natural = value["evidence"]["records"][-1]
        natural["sample_count"] = 100
        natural["passed_count"] = 99

        result = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(result["state"], "default-candidate")
        self.assertEqual(result["advisory_decision"], "eligible")
        self.assertFalse(result["live_routing"])
        self.assertFalse(result["mutation_allowed"])

    def test_mutation_observed_disables_and_forbids_fallback(self) -> None:
        value = fixture()
        value["evidence"]["records"][0]["outcome"] = "fail"
        value["evidence"]["records"][0]["mutation_provenance"] = "mutation_observed"

        result = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(result["state"], "disabled")
        self.assertFalse(
            result["fallback"]["safe_if_selected_execution_fails"]
        )
        self.assertIn("mutation_observed", result["reasons"])

    def test_unknown_natural_outcome_cannot_satisfy_sample_threshold(self) -> None:
        value = fixture()
        value["evidence"]["records"][-1]["outcome"] = "unknown"

        result = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(result["state"], "experimental-private")
        self.assertEqual(result["natural_evidence"]["sample_count"], 0)

    def test_required_pass_with_uncertain_mutation_cannot_prove_fallback(self) -> None:
        value = fixture()
        value["evidence"]["records"][0]["mutation_provenance"] = (
            "mutation_possible"
        )

        result = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(result["state"], "eligible-bounded-write")
        self.assertFalse(
            result["fallback"]["safe_if_selected_execution_fails"]
        )

    def test_natural_objective_mutation_disables_certification_and_fallback(self) -> None:
        value = fixture()
        value["evidence"]["records"][-1]["mutation_provenance"] = (
            "mutation_observed"
        )

        result = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(result["state"], "disabled")
        self.assertIn("mutation_observed", result["reasons"])
        self.assertFalse(
            result["fallback"]["safe_if_selected_execution_fails"]
        )

    def test_evidence_cannot_be_reused_for_another_candidate(self) -> None:
        value = fixture()
        value["candidate"]["worker_id"] = "different-worker"

        with self.assertRaisesRegex(
            WorkerCertificationError,
            "evidence.worker_id does not match candidate",
        ):
            certify_worker(
                value["candidate"],
                value["evidence"],
                evaluated_at=NOW,
            )

    def test_natural_objective_aggregate_cannot_be_duplicated(self) -> None:
        value = fixture()
        duplicate = copy.deepcopy(value["evidence"]["records"][-1])
        duplicate["evidence_id"] = "natural-quality-copy"
        value["evidence"]["records"].append(duplicate)

        with self.assertRaisesRegex(
            WorkerCertificationError,
            "only one bound natural objective aggregate",
        ):
            certify_worker(
                value["candidate"],
                value["evidence"],
                evaluated_at=NOW,
            )

    def test_synthetic_and_natural_provider_failures_remain_distinct(self) -> None:
        value = fixture()
        value["evidence"]["records"].append(
            {
                "evidence_id": "natural-auth",
                "evidence_class": "natural",
                "scenario": "auth_failure",
                "outcome": "unknown",
                "mutation_provenance": "unknown",
            }
        )

        result = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )
        auth = next(
            item for item in result["coverage"]
            if item["scenario"] == "auth_failure"
        )

        self.assertEqual(
            auth["status_by_evidence_class"],
            {"natural": "unknown", "synthetic": "passed"},
        )
        self.assertEqual(auth["status"], "passed")

    def test_certification_is_pure_and_explicitly_non_mutating(self) -> None:
        value = fixture()
        before = copy.deepcopy(value)

        result = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(value, before)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["mutation_allowed"])
        self.assertFalse(result["live_routing"])
        self.assertFalse(result["routing_policy_mutation"])
        self.assertFalse(result["active_config_mutation"])
        self.assertIn("queue", result["prohibited_mutation_surfaces"])

    def test_report_only_canary_is_deterministic_and_has_no_apply_surface(self) -> None:
        value = fixture()
        certification = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        first = simulate_report_only_canary(
            certification,
            cohort_key="task-123",
            candidate=value["candidate"],
            evidence=value["evidence"],
            evaluated_at=NOW,
        )
        second = simulate_report_only_canary(
            certification,
            cohort_key="task-123",
            candidate=value["candidate"],
            evidence=value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first["initial_canary_basis_points"],
            INITIAL_CANARY_BASIS_POINTS,
        )
        self.assertIn(first["report_only_lane"], {"baseline", "canary"})
        self.assertTrue(first["read_only"])
        self.assertFalse(first["mutation_allowed"])
        self.assertFalse(first["live_routing"])
        self.assertFalse(first["routing_policy_mutation"])
        self.assertNotIn("apply", first)
        self.assertNotIn("selected_target", first)

    def test_canary_bucket_is_stable_across_revalidation_time(self) -> None:
        value = fixture()
        first_certification = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )
        second_certification = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW + timedelta(days=1),
        )

        first = simulate_report_only_canary(
            first_certification,
            cohort_key="task-123",
            candidate=value["candidate"],
            evidence=value["evidence"],
            evaluated_at=NOW,
        )
        second = simulate_report_only_canary(
            second_certification,
            cohort_key="task-123",
            candidate=value["candidate"],
            evidence=value["evidence"],
            evaluated_at=NOW + timedelta(days=1),
        )

        self.assertNotEqual(
            first["certification_id"], second["certification_id"]
        )
        self.assertEqual(
            first["bucket_basis_points"], second["bucket_basis_points"]
        )
        self.assertEqual(first["report_only_lane"], second["report_only_lane"])

    def test_ineligible_certification_always_keeps_baseline(self) -> None:
        value = fixture()
        synthetic = [
            item for item in value["evidence"]["records"]
            if item["evidence_class"] == "synthetic"
        ]
        value["evidence"]["records"] = synthetic
        certification = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        report = simulate_report_only_canary(
            certification,
            cohort_key="task-123",
            candidate=value["candidate"],
            evidence=value["evidence"],
            evaluated_at=NOW,
        )

        self.assertEqual(report["report_only_lane"], "baseline")
        self.assertIn("certification_not_eligible", report["reasons"])

    def test_expired_certification_keeps_baseline(self) -> None:
        value = fixture()
        certification = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        report = simulate_report_only_canary(
            certification,
            cohort_key="task-123",
            candidate=value["candidate"],
            evidence=value["evidence"],
            evaluated_at=NOW + timedelta(days=30),
        )

        self.assertEqual(report["report_only_lane"], "baseline")
        self.assertIn("certification_expired", report["reasons"])

    def test_adverse_signal_forces_baseline_and_rollback_recommendation(self) -> None:
        value = fixture()
        certification = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )

        report = simulate_report_only_canary(
            certification,
            cohort_key="task-123",
            candidate=value["candidate"],
            evidence=value["evidence"],
            evaluated_at=NOW,
            adverse_signals=1,
        )

        self.assertEqual(report["report_only_lane"], "baseline")
        self.assertEqual(
            report["rollback_recommendation"], "rollback_recommended"
        )
        self.assertIn("adverse_signal_observed", report["reasons"])

    def test_tampered_certification_cannot_enter_simulation(self) -> None:
        value = fixture()
        certification = certify_worker(
            value["candidate"],
            value["evidence"],
            evaluated_at=NOW,
        )
        certification["state"] = "default-candidate"

        with self.assertRaisesRegex(
            WorkerCertificationError, "does not match candidate and evidence"
        ):
            simulate_report_only_canary(
                certification,
                cohort_key="task-123",
                candidate=value["candidate"],
                evidence=value["evidence"],
                evaluated_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()

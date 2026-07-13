from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.config import Config
from codex_batch_runner.evaluation import derive_evaluation_row
from codex_batch_runner.execution_evidence import derive_execution_evidence_rows
from codex_batch_runner.queue import create_task, save_task
from codex_batch_runner.review_outcome_evidence import (
    ReviewOutcomeEvidenceError,
    attach_review_outcome_evidence,
    build_review_outcome_evidence,
    review_outcome_view,
)
from codex_batch_runner.routing_report import summarize_review_outcome_exclusions, summarize_review_outcome_strata
from codex_batch_runner.routing_evaluation_report import build_routing_evaluation_report


def task(task_id: str, *, size: str = "small", review_policy: str = "review-v1") -> dict:
    return {
        "id": task_id,
        "status": "completed",
        "review_status": "accepted",
        "attempts": 1,
        "routing_size": size,
        "routing_risk": "low",
        "verification_scope": ["unit"],
        "review_policy_version": review_policy,
        "review_rubric_version": "rubric-v1",
        "execution_backend": "codex",
        "last_run": {"execution_backend": "codex", "resolved_execution_config": {"selection_rule": "standard"}},
        "last_result": {"task_id": task_id, "status": "completed", "verification": ["unit"]},
    }


def reviewer_pass(task_value: dict, *, anchor: bool = True) -> dict:
    return build_review_outcome_evidence(
        task_value,
        acceptance_method="reviewer_pass",
        accepted=True,
        objective_status="passed",
        semantic_status="pass",
        reviewer_kind="codex",
        reviewer_role="anchor",
        decision_confidence="high",
        anchor_semantic_review=anchor,
        actual_identity="provider-reviewer-v1",
        actual_identity_source="provider_observed",
        actual_identity_confidence="provider_observed",
    )


class ReviewOutcomeEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict("os.environ", {"CBR_CONFIG": ""}, clear=False)
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

    def test_append_only_history_and_unknown_identity_do_not_infer_role_or_plan(self) -> None:
        value = task("review-append")
        record = build_review_outcome_evidence(
            value,
            acceptance_method="human_accept",
            accepted=True,
            objective_status="passed",
            semantic_status="pass",
            reviewer_kind="human",
            reviewer_role="release-approver",
            decision_confidence="high",
            anchor_semantic_review=True,
        )
        attach_review_outcome_evidence(value, record)
        attach_review_outcome_evidence(value, record)

        self.assertEqual(1, len(value["review_outcome_evidence_history"]))
        identity = review_outcome_view(value)["reviewer"]["actual_identity"]
        self.assertEqual("unknown", identity["status"])
        self.assertIsNone(identity["value"])
        self.assertEqual("release-approver", review_outcome_view(value)["reviewer"]["role"])
        self.assertNotIn("review_outcome_evidence_id", value.get("last_run", {}))
        self.assertTrue(derive_evaluation_row(value)["objective_checks"]["required_checks_passed"])

    def test_false_identity_attribution_requires_provider_or_wrapper_observation(self) -> None:
        with self.assertRaisesRegex(ReviewOutcomeEvidenceError, "provider/wrapper observed"):
            build_review_outcome_evidence(
                task("review-identity"),
                acceptance_method="reviewer_pass",
                accepted=True,
                objective_status="passed",
                semantic_status="pass",
                reviewer_kind="codex",
                actual_identity="planned-model-name",
                actual_identity_source="planned_model",
                actual_identity_confidence="high",
            )

    def test_report_stratifies_methods_and_reports_anchor_coverage_without_threshold(self) -> None:
        reviewer_task = task("reviewer-pass")
        attach_review_outcome_evidence(reviewer_task, reviewer_pass(reviewer_task))
        human_task = task("human-accept")
        attach_review_outcome_evidence(
            human_task,
            build_review_outcome_evidence(
                human_task,
                acceptance_method="human_accept",
                accepted=True,
                objective_status="passed",
                semantic_status="pass",
                reviewer_kind="human",
                reviewer_role="approver",
                decision_confidence="high",
                anchor_semantic_review=False,
            ),
        )

        strata = summarize_review_outcome_strata([derive_evaluation_row(reviewer_task), derive_evaluation_row(human_task)])
        by_method = {entry["comparability_components"]["acceptance_method"]: entry for entry in strata}
        reviewer_cell = by_method["reviewer_pass"]["worker_cells"][0]
        human_cell = by_method["human_accept"]["worker_cells"][0]

        self.assertEqual(2, len(strata))
        self.assertFalse(by_method["reviewer_pass"]["cross_method_aggregation"])
        self.assertEqual(1, reviewer_cell["matched_anchor_semantic_review_coverage"]["numerator"])
        self.assertEqual(1, reviewer_cell["matched_anchor_semantic_review_coverage"]["denominator"])
        self.assertIsNone(reviewer_cell["matched_anchor_semantic_review_coverage"]["numerical_threshold"])
        self.assertTrue(reviewer_cell["quality_comparable"])
        self.assertFalse(human_cell["quality_comparable"])
        self.assertIsNone(human_cell["quality_rate"])

    def test_mismatched_anchor_cohort_and_legacy_are_excluded_from_quality_coverage(self) -> None:
        source = task("source-small")
        stale_record = reviewer_pass(source)
        mismatched = task("target-large", size="large")
        attach_review_outcome_evidence(mismatched, stale_record)
        legacy = task("legacy")

        mismatch_row = derive_evaluation_row(mismatched)
        legacy_row = derive_evaluation_row(legacy)
        strata = summarize_review_outcome_strata([mismatch_row, legacy_row])
        mismatch_cell = next(entry for entry in strata if entry["key"] == "review-outcome-cohort-mismatch")["worker_cells"][0]
        legacy_cell = next(entry for entry in strata if entry["key"] == "legacy-review-unknown")["worker_cells"][0]
        exclusions = {entry["key"] for entry in summarize_review_outcome_exclusions([mismatch_row, legacy_row])}

        self.assertEqual(0, mismatch_cell["matched_anchor_semantic_review_coverage"]["denominator"])
        self.assertFalse(mismatch_cell["quality_comparable"])
        self.assertEqual(0, legacy_cell["matched_anchor_semantic_review_coverage"]["denominator"])
        self.assertIn("review_outcome_cohort_mismatch", exclusions)
        self.assertIn("legacy_review_outcome_evidence", exclusions)

    def test_supplemental_record_dual_reads_review_outcome_without_execution_evidence_rewrite(self) -> None:
        value = task("supplemental-review")
        record = reviewer_pass(value)
        rows = derive_execution_evidence_rows(
            [
                {
                    "record_kind": "review_outcome_evidence_v1",
                    "work_id": "synthetic-public-work",
                    "execution_backend": "codex",
                    "routing_size": "small",
                    "routing_risk": "low",
                    "verification_scope": ["unit"],
                    "status": "completed",
                    "last_result": {"status": "completed", "verification": ["unit"]},
                    "review_outcome_evidence": record,
                }
            ]
        )

        self.assertEqual("review-outcome-evidence-v1", rows[0]["review_outcome"]["evidence_contract_version"])
        self.assertEqual("legacy-v1", rows[0]["execution_evidence"]["evidence_contract_version"])

    def test_checked_in_review_outcome_example_is_public_safe_and_dual_readable(self) -> None:
        fixture = Path(__file__).parents[1] / "examples" / "review-outcome-evidence-v1.example.json"
        rows = derive_execution_evidence_rows([json.loads(fixture.read_text(encoding="utf-8"))])
        serialized = json.dumps(rows, sort_keys=True)

        self.assertEqual("reviewer_pass", rows[0]["review_outcome"]["acceptance"]["method"])
        self.assertTrue(rows[0]["review_outcome"]["semantic_review"]["anchor"])
        self.assertNotIn("synthetic-review-outcome-work-001", serialized)
        self.assertFalse(rows[0]["privacy"]["session_or_thread_ids_included"])

    def test_routing_evaluation_report_exposes_review_outcome_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            value = create_task(
                config,
                "public-safe review outcome",
                tmp,
                task_id="report-review-outcome",
                routing_size="small",
                routing_risk="low",
                verification_scope=["unit"],
            )
            value.update(task("report-review-outcome"))
            attach_review_outcome_evidence(value, reviewer_pass(value))
            save_task(config, value)

            report = build_routing_evaluation_report(config)

        cell = report["evaluation_diagnostics"]["review_outcome_strata"][0]["worker_cells"][0]
        self.assertTrue(cell["quality_comparable"])
        self.assertEqual(1.0, cell["quality_rate"])
        self.assertEqual([], report["evaluation_diagnostics"]["review_outcome_exclusions"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest

from codex_batch_runner.execution_evidence_v2 import attach_execution_evidence, build_execution_evidence
from codex_batch_runner.evaluation import derive_evaluation_row
from codex_batch_runner.review_outcome_evidence import attach_review_outcome_evidence, build_review_outcome_evidence
from codex_batch_runner.routing_cost_evidence import (
    RoutingCostEvidenceError,
    attach_routing_cost_evidence,
    build_routing_cost_evidence,
    routing_cost_evidence_view,
    validate_routing_cost_evidence,
)


def comparable_task() -> dict:
    task = {
        "id": "synthetic-routing-task",
        "attempts": 1,
        "execution_surface": "cbr_batch",
        "routing_size": "small",
        "routing_risk": "low",
        "verification_scope": ["unit"],
        "prompt_contract_version": "prompt-v1",
        "context_contract_version": "context-v1",
        "review_policy_version": "review-v1",
        "review_rubric_version": "rubric-v1",
        "review_attempts": 1,
        "fix_attempts": 0,
        "last_run": {
            "execution_backend": "codex",
            "resolved_execution_config": {"model": "frontier-model", "reasoning_effort": "medium"},
        },
    }
    execution = build_execution_evidence(
        task,
        capture_source="synthetic_provider",
        actual_model="frontier-model-2026",
        actual_model_source="provider_response",
        actual_model_confidence="provider_observed",
        actual_model_unavailable_reason=None,
        token_usage={"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20, "reasoning_output_tokens": 5},
        token_usage_source="provider_response",
        token_usage_confidence="provider_observed",
        token_usage_unavailable_reason=None,
    )
    attach_execution_evidence(task, execution)
    review = build_review_outcome_evidence(
        task,
        acceptance_method="reviewer_pass",
        accepted=True,
        objective_status="passed",
        semantic_status="pass",
        reviewer_kind="codex",
        actual_identity="review-model-2026",
        actual_identity_source="provider_observed",
        actual_identity_confidence="provider_observed",
        review_policy_version="review-v1",
        rubric_version="rubric-v1",
    )
    attach_review_outcome_evidence(task, review)
    return task


class RoutingCostEvidenceTests(unittest.TestCase):
    def test_provider_attributed_contract_keeps_cost_and_quality_axes_separate(self) -> None:
        task = comparable_task()
        record = build_routing_cost_evidence(task)

        self.assertEqual("frontier-model", record["selection"]["planned_model"])
        self.assertEqual("frontier-model-2026", record["actual_model"]["value"])
        self.assertEqual("provider_attributed", record["usage"]["attribution"]["class"])
        self.assertEqual(
            {
                "uncached_input_tokens": 60,
                "cached_input_tokens": 40,
                "cache_write_tokens": None,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            },
            record["usage"]["values"],
        )
        self.assertEqual("passed", record["quality"]["objective_verification"])
        self.assertEqual("pass", record["quality"]["semantic_review"])
        self.assertTrue(record["cohort"]["comparability"]["joint_quality_cost"])
        self.assertNotIn("known_total_tokens", json.dumps(record))

    def test_window_estimate_is_separate_cohort_and_concurrent_run_is_excluded(self) -> None:
        task = comparable_task()
        estimated = build_routing_cost_evidence(
            task,
            attribution_class="window_estimated",
            attribution_source="usage_window_delta",
            usage={"uncached_input_tokens": 90, "output_tokens": 30},
            window_before=1000,
            window_after=1120,
        )
        confounded = build_routing_cost_evidence(
            task,
            attribution_class="concurrent_confounded",
            attribution_source="usage_window_delta",
            usage={"uncached_input_tokens": 90, "output_tokens": 30},
        )

        self.assertTrue(estimated["cohort"]["comparability"]["usage_cost"])
        self.assertFalse(confounded["cohort"]["comparability"]["usage_cost"])
        self.assertNotEqual(estimated["cohort"]["cohort_id"], confounded["cohort"]["cohort_id"])
        self.assertIn("usage_attribution_concurrent_confounded", confounded["cohort"]["exclusion_reasons"])

    def test_missing_contract_versions_and_legacy_review_remain_non_comparable(self) -> None:
        task = comparable_task()
        task.pop("prompt_contract_version")
        task["review_outcome_evidence_history"] = []
        record = build_routing_cost_evidence(task)

        self.assertFalse(record["cohort"]["comparability"]["quality"])
        self.assertFalse(record["cohort"]["comparability"]["usage_cost"])
        self.assertIn("prompt_contract_version_unversioned", record["cohort"]["exclusion_reasons"])
        self.assertIn("quality_evidence_non_comparable", record["cohort"]["exclusion_reasons"])

    def test_unknown_planned_reasoning_and_task_bucket_are_non_comparable(self) -> None:
        task = comparable_task()
        task["last_run"]["resolved_execution_config"].pop("reasoning_effort")
        task["routing_size"] = "unknown"
        record = build_routing_cost_evidence(task)

        self.assertFalse(record["cohort"]["comparability"]["joint_quality_cost"])
        self.assertIn("planned_reasoning_unversioned", record["cohort"]["exclusion_reasons"])
        self.assertIn("task_bucket_unversioned", record["cohort"]["exclusion_reasons"])

    def test_append_only_view_and_public_boundary(self) -> None:
        task = comparable_task()
        record = build_routing_cost_evidence(task)
        attach_routing_cost_evidence(task, record)
        attach_routing_cost_evidence(task, record)

        self.assertEqual(1, len(task["routing_cost_evidence_history"]))
        self.assertEqual(record["evidence_id"], routing_cost_evidence_view(task)["evidence_id"])
        self.assertEqual(
            record["cohort"]["cohort_id"],
            derive_evaluation_row(task)["routing_cost_evidence"]["cohort"]["cohort_id"],
        )
        leaked = dict(record)
        leaked["session_id"] = "private-session"
        with self.assertRaises(RoutingCostEvidenceError):
            validate_routing_cost_evidence(leaked)

    def test_unavailable_attribution_rejects_usage_values(self) -> None:
        with self.assertRaisesRegex(RoutingCostEvidenceError, "cannot include usage"):
            build_routing_cost_evidence(
                comparable_task(),
                attribution_class="unavailable",
                usage={"output_tokens": 10},
            )

    def test_validator_rejects_window_semantics_and_raw_context(self) -> None:
        record = build_routing_cost_evidence(comparable_task())
        record["usage"]["attribution"]["class"] = "window_estimated"
        with self.assertRaisesRegex(RoutingCostEvidenceError, "before and after"):
            validate_routing_cost_evidence(record)

        record = build_routing_cost_evidence(comparable_task())
        record["context"] = "private context"
        with self.assertRaisesRegex(RoutingCostEvidenceError, "forbidden key: context"):
            validate_routing_cost_evidence(record)


if __name__ == "__main__":
    unittest.main()

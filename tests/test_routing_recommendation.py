from __future__ import annotations

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.config import Config
from codex_batch_runner.cli import main
from codex_batch_runner.execution_evidence_v2 import attach_execution_evidence, build_execution_evidence
from codex_batch_runner.review_outcome_evidence import attach_review_outcome_evidence, build_review_outcome_evidence
from codex_batch_runner.routing_cost_evidence import build_routing_cost_evidence
from codex_batch_runner.routing_recommendation import build_routing_recommendation


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
        "review_attempts": 0,
        "fix_attempts": 0,
        "follow_up_count": 0,
        "last_run": {"execution_backend": "codex", "resolved_execution_config": {"model": "frontier-model", "reasoning_effort": "medium"}},
    }
    attach_execution_evidence(task, build_execution_evidence(task, capture_source="synthetic", actual_model="frontier-model-2026", actual_model_source="provider", actual_model_confidence="observed", actual_model_unavailable_reason=None, token_usage={"input_tokens": 100, "output_tokens": 20}, token_usage_source="provider", token_usage_confidence="observed", token_usage_unavailable_reason=None))
    attach_review_outcome_evidence(task, build_review_outcome_evidence(task, acceptance_method="reviewer_pass", accepted=True, objective_status="passed", semantic_status="pass", reviewer_kind="codex", actual_identity="reviewer", actual_identity_source="provider_observed", actual_identity_confidence="provider_observed", review_policy_version="review-v1", rubric_version="rubric-v1"))
    return task


def recommendation(config: Config, records: list[dict], *, surface: str = "cbr_batch") -> dict:
    return build_routing_recommendation(
        config,
        task_bucket="size=small risk=low verify=unit",
        execution_surface=surface,
        semantic_complexity="medium",
        failure_cost="medium",
        objective_verification="unit",
        expected_context="bounded",
        interaction_need="none",
        usage_pressure="normal",
        available_models=["cheap-model-2026", "frontier-model-2026", "xhigh"],
        routing_cost_records=records,
    )


class RoutingRecommendationTests(unittest.TestCase):
    def test_recommends_lowest_cost_quality_gated_model_and_keeps_surfaces_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            records = []
            for model, output in (("frontier-model-2026", 50), ("cheap-model-2026", 10)):
                for _ in range(5):
                    task = comparable_task()
                    task["last_run"]["resolved_execution_config"]["model"] = model
                    task["execution_evidence_history"][-1]["actual_model"]["value"] = model
                    records.append(build_routing_cost_evidence(task, usage={"uncached_input_tokens": 50, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": output, "reasoning_output_tokens": 0}))
            thread_task = comparable_task()
            thread_task["execution_surface"] = "user_owned_thread"
            records.append(build_routing_cost_evidence(thread_task))

            report = recommendation(config, records)

            self.assertEqual("recommended", report["status"])
            self.assertEqual("cheap-model-2026", report["recommendation"]["model"])
            self.assertEqual(10, report["recommendation"]["comparable_cohort_size"])
            self.assertTrue(report["read_only"])
            self.assertFalse(report["mutation_allowed"])

    def test_returns_static_fallback_for_sparse_or_non_comparable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            report = recommendation(config, [build_routing_cost_evidence(comparable_task())])

            self.assertEqual("insufficient", report["status"])
            self.assertIsNone(report["recommendation"]["model"])
            self.assertEqual("operator_baseline", report["recommendation"]["fallback"]["model"])
            self.assertEqual("insufficient_comparable_evidence", report["recommendation"]["reasoning"])

    def test_cli_json_and_human_output_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            records = [build_routing_cost_evidence(comparable_task()) for _ in range(5)]
            evidence_path = Path(tmp) / "evidence.json"
            evidence_path.write_text(json.dumps({"records": records}), encoding="utf-8")
            args = ["--config", str(Path(tmp) / "config.json"), "recommend-routing", "--task-bucket", "size=small risk=low verify=unit", "--execution-surface", "cbr_batch", "--semantic-complexity", "medium", "--failure-cost", "medium", "--objective-verification", "unit", "--expected-context", "bounded", "--interaction-need", "none", "--usage-pressure", "normal", "--available-model", "frontier-model-2026", "--routing-cost-evidence-json", str(evidence_path)]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(0, main(args + ["--json"]))
            report = json.loads(stdout.getvalue())
            self.assertEqual("recommended", report["status"])
            self.assertFalse(report["mutation_allowed"])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(0, main(args))
            self.assertIn("Routing recommendation (read-only)", stdout.getvalue())


def _config(tmp: str) -> Config:
    path = Path(tmp) / "config.json"
    path.write_text(json.dumps({"queue_dir": str(Path(tmp) / "tasks")}), encoding="utf-8")
    return Config.load(str(path))


if __name__ == "__main__":
    unittest.main()

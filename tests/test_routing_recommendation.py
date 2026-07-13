from __future__ import annotations

import json
import contextlib
import io
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from codex_batch_runner.config import Config
from codex_batch_runner.cli import main
from codex_batch_runner.execution_evidence_v3 import attach_execution_evidence_v3, build_codex_execution_evidence_v3
from codex_batch_runner.model_requirements import ResolvedExecutionConfig
from codex_batch_runner.review_outcome_evidence import attach_review_outcome_evidence, build_review_outcome_evidence
from codex_batch_runner.routing_cost_evidence import build_routing_cost_evidence
from codex_batch_runner.routing_cost_evidence import validate_routing_cost_evidence, RoutingCostEvidenceError
from codex_batch_runner.routing_recommendation import build_routing_recommendation


def comparable_task(*, model: str = "frontier-model-2026", effort: str = "medium") -> dict:
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
    vector = {
        "schema_version": 2, "derivation_version": "requirement-rubric-v1", "revision_id": "recommendation-v3",
        "quality_requirements": {}, "hard_constraints": {}, "utility_preferences": {},
    }
    selected = ResolvedExecutionConfig(
        requirement_vector=vector, selection_rule="execution-target-selector-v1",
        selection_reason="automatic_static_non_learned", model=model,
        execution_target=model + "-" + effort + "-v1", config_overrides={"model_reasoning_effort": effort},
        selected_target_snapshot={
            "target_id": model + "-" + effort + "-v1",
            "target": {"target_id": model + "-" + effort + "-v1", "execution_surface": "codex"},
            "inventory_schema_version": 1,
            "inventory_snapshot_id": "sha256:recommendation-inventory",
            "constraint_registry_version": "constraints-v1",
            "selection_policy_version": "execution-target-selector-v1",
        },
    )
    cfg = Config.load(root=Path.cwd())
    cfg = Config(**{**cfg.__dict__, "execution_target_inventory": {
        "schema_version": 1, "snapshot_id": "sha256:recommendation-inventory", "status": "current", "targets": {},
    }, "constraint_registry": {"schema_version": 1, "version": "constraints-v1", "constraints": {}}})
    attach_execution_evidence_v3(task, build_codex_execution_evidence_v3(
        task, SimpleNamespace(events=[{
            "type": "turn.completed", "model": model,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }]), selected, cfg,
    ))
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
        expected_context="context-v1",
        interaction_need="none",
        usage_pressure="normal",
        available_models=["cheap-model-2026", "frontier-model-2026", "frontier-model", "xhigh"],
        routing_cost_records=records,
    )


class RoutingRecommendationTests(unittest.TestCase):
    def test_returns_explicit_model_effort_fields_without_inventing_token_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            records = []
            for model, output in (("frontier-model-2026", 50), ("cheap-model-2026", 10)):
                for _ in range(5):
                    task = comparable_task(model=model)
                    records.append(build_routing_cost_evidence(task, usage={"uncached_input_tokens": 50, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": output, "reasoning_output_tokens": 0}))
            thread_task = comparable_task()
            thread_task["execution_surface"] = "user_owned_thread"
            records.append(build_routing_cost_evidence(thread_task))

            report = recommendation(config, records)

            self.assertEqual("insufficient", report["status"])
            self.assertIn("model", report["recommendation"])
            self.assertIn("reasoning_effort", report["recommendation"])
            self.assertIsNone(report["recommendation"]["model"])
            self.assertIsNone(report["recommendation"]["reasoning_effort"])
            self.assertEqual("operator_selected", report["recommendation"]["fallback"]["reasoning_effort"])
            self.assertEqual("normalized_cost_unavailable", report["recommendation"]["cost_evidence"]["reason"])
            self.assertEqual(10, report["recommendation"]["comparable_cohort_size"])
            self.assertEqual("cheap-model-2026", report["candidates"][0]["model"])
            self.assertEqual("medium", report["candidates"][0]["reasoning_effort"])
            self.assertNotIn("mean_token_cost", report["candidates"][0]["usage_evidence"])
            self.assertTrue(report["read_only"])
            self.assertFalse(report["mutation_allowed"])

    def test_returns_static_fallback_for_sparse_or_non_comparable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            report = recommendation(config, [build_routing_cost_evidence(comparable_task())])

            self.assertEqual("insufficient", report["status"])
            self.assertIsNone(report["recommendation"]["model"])
            self.assertEqual("operator_baseline", report["recommendation"]["fallback"]["model"])
            self.assertEqual("insufficient_comparable_evidence", report["recommendation"]["rationale"])

    def test_xhigh_reasoning_candidate_is_not_emitted_or_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            records = []
            for effort in ("medium", "xhigh"):
                for _ in range(5):
                    task = comparable_task(effort=effort)
                    records.append(build_routing_cost_evidence(task))

            report = recommendation(config, records)

            self.assertEqual(["medium"], [candidate["reasoning_effort"] for candidate in report["candidates"]])
            self.assertNotIn("xhigh", json.dumps(report["candidates"]))
            self.assertIsNone(report["recommendation"]["reasoning_effort"])

    def test_cached_components_are_preserved_without_equating_them_to_uncached_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            records = []
            for _ in range(5):
                task = comparable_task()
                records.append(build_routing_cost_evidence(task, usage={"uncached_input_tokens": 1, "cached_input_tokens": 1000, "cache_write_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}))

            report = recommendation(config, records)

            usage = report["candidates"][0]["usage_evidence"]
            self.assertEqual(1, usage["mean_components"]["uncached_input_tokens"])
            self.assertEqual(1000, usage["mean_components"]["cached_input_tokens"])
            self.assertFalse(usage["normalized_cost_available"])
            self.assertEqual("normalized_cost_unavailable", report["recommendation"]["cost_evidence"]["reason"])

    def test_tampered_exact_cohort_is_rejected_before_recommendation(self) -> None:
        record = build_routing_cost_evidence(comparable_task())
        record["cohort"]["components"]["selection_cohort"] = "override"
        record["cohort"]["comparability"]["joint_quality_cost"] = True
        with self.assertRaisesRegex(RoutingCostEvidenceError, "canonical record axes"):
            validate_routing_cost_evidence(record)

        record = build_routing_cost_evidence(comparable_task())
        record["target"].pop("selection_cohort")
        with self.assertRaisesRegex(RoutingCostEvidenceError, "automatic or override"):
            validate_routing_cost_evidence(record)

    def test_high_failure_cost_requires_matching_verified_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            record = build_routing_cost_evidence(comparable_task())
            record["quality"]["semantic_review"] = "not_performed"
            report = build_routing_recommendation(config, task_bucket="size=small risk=low verify=unit", execution_surface="cbr_batch", semantic_complexity="medium", failure_cost="high", objective_verification="unit", expected_context="context-v1", interaction_need="none", usage_pressure="high", routing_cost_records=[record])

            self.assertEqual("high_failure_cost_requires_matching_verified_cohort", report["recommendation"]["cost_evidence"]["reason"])
            self.assertTrue(report["safety_gate"]["usage_pressure_does_not_relax_quality"])

    def test_cli_json_and_human_output_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            records = [build_routing_cost_evidence(comparable_task()) for _ in range(5)]
            evidence_path = Path(tmp) / "evidence.json"
            evidence_path.write_text(json.dumps({"records": records}), encoding="utf-8")
            args = ["--config", str(Path(tmp) / "config.json"), "recommend-routing", "--task-bucket", "size=small risk=low verify=unit", "--execution-surface", "cbr_batch", "--semantic-complexity", "medium", "--failure-cost", "medium", "--objective-verification", "unit", "--expected-context", "context-v1", "--interaction-need", "none", "--usage-pressure", "normal", "--available-model", "frontier-model-2026", "--routing-cost-evidence-json", str(evidence_path)]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(0, main(args + ["--json"]))
            report = json.loads(stdout.getvalue())
            self.assertEqual("insufficient", report["status"])
            self.assertFalse(report["mutation_allowed"])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(0, main(args))
            self.assertIn("Routing recommendation (read-only)", stdout.getvalue())
            self.assertIn("reasoning_effort", stdout.getvalue())


def _config(tmp: str) -> Config:
    path = Path(tmp) / "config.json"
    path.write_text(json.dumps({"queue_dir": str(Path(tmp) / "tasks")}), encoding="utf-8")
    return Config.load(str(path))


if __name__ == "__main__":
    unittest.main()

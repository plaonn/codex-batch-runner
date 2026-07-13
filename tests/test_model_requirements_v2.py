import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_batch_runner.cli import parse_model_requirement_args, parse_routing_override_arg
from codex_batch_runner.config import Config
from codex_batch_runner.evaluation import derive_evaluation_row
from codex_batch_runner.model_requirements import (
    legacy_requirement_projection,
    model_requirement_vector_value,
    routing_override_value,
)
from codex_batch_runner.queue import create_task, load_task, save_task
from codex_batch_runner.review_next import enqueue_auto_fix_task


def requirement_v2(revision_id: str = "reqrev-issued-1") -> dict:
    return {
        "schema_version": 2,
        "derivation_version": "requirement-rubric-v1",
        "revision_id": revision_id,
        "quality_requirements": {
            axis: {"score": 500, "confidence": 750, "anchor": 500, "evidence_codes": []}
            for axis in (
                "semantic_reasoning",
                "context_integration",
                "planning_depth",
                "instruction_fidelity",
                "tool_execution_reliability",
                "adversarial_detection",
            )
        },
        "hard_constraints": {
            "required_execution_surfaces": ["codex"],
            "minimum_context_tokens": 200000,
            "interactive_input_required": False,
        },
        "utility_preferences": {"latency_weight": 250, "cost_weight": 500},
    }


class ModelRequirementV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict("os.environ", {"CBR_CONFIG": ""}, clear=False)
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

    def test_cli_accepts_complete_v2_and_override_without_defaults(self) -> None:
        args = SimpleNamespace(
            model_requirement_json=json.dumps(requirement_v2()),
            routing_override_json=json.dumps(
                {
                    "mode": "pin",
                    "target_id": "target-v1",
                    "reason": "operator request",
                    "scope": "single_task",
                    "allow_fallback": False,
                    "provenance": "operator_override",
                }
            ),
            **{
                name: None
                for name in (
                    "reasoning_depth",
                    "context_need",
                    "tool_reliability",
                    "latency_priority",
                    "cost_sensitivity",
                    "review_strictness",
                )
            },
        )
        self.assertEqual(requirement_v2(), parse_model_requirement_args(args))
        self.assertEqual("pin", parse_routing_override_arg(args)["mode"])

    def test_v2_round_trips_without_semantic_defaults(self) -> None:
        value = requirement_v2()
        self.assertEqual(value, model_requirement_vector_value("requirement", value))

    def test_v2_rejects_missing_revision_unknown_axis_and_unknown_evidence(self) -> None:
        missing_revision = requirement_v2()
        del missing_revision["revision_id"]
        with self.assertRaisesRegex(ValueError, "revision_id is required"):
            model_requirement_vector_value("requirement", missing_revision)

        unknown_axis = requirement_v2()
        unknown_axis["quality_requirements"]["role"] = unknown_axis["quality_requirements"].pop(
            "adversarial_detection"
        )
        with self.assertRaisesRegex(ValueError, "exactly the v2 axes"):
            model_requirement_vector_value("requirement", unknown_axis)

        unknown_code = requirement_v2()
        unknown_code["quality_requirements"]["semantic_reasoning"]["evidence_codes"] = ["INVENTED"]
        with self.assertRaisesRegex(ValueError, "unknown codes"):
            model_requirement_vector_value("requirement", unknown_code)

    def test_legacy_projection_is_deterministic_and_non_comparable(self) -> None:
        legacy = {"dimensions": {"reasoning_depth": "high"}, "confidence": "medium"}
        first = legacy_requirement_projection(legacy)
        second = legacy_requirement_projection(legacy)
        self.assertEqual(first, second)
        self.assertEqual("legacy-derived", first["derivation_identity"]["kind"])
        self.assertFalse(first["derivation_identity"]["exact_v2_cohort_eligible"])
        self.assertEqual(1, first["legacy_projection"]["schema_version"])
        self.assertTrue(
            all(
                axis == {"score": 0, "confidence": 0, "anchor": 0, "evidence_codes": []}
                for axis in first["quality_requirements"].values()
            )
        )
        self.assertEqual({"latency_weight": 0, "cost_weight": 0}, first["utility_preferences"])

        row = derive_evaluation_row({"id": "legacy", "model_requirement_vector": first})
        self.assertEqual("legacy-derived-non-comparable", row["worker"]["model_requirement_cohort"])
        self.assertFalse(row["worker"]["model_requirement_exact_v2_cohort_eligible"])

        exact_row = derive_evaluation_row({"id": "exact", "model_requirement_vector": requirement_v2()})
        self.assertEqual("requirement-v2-exact", exact_row["worker"]["model_requirement_cohort"])
        self.assertTrue(exact_row["worker"]["model_requirement_exact_v2_cohort_eligible"])

    def test_requirement_and_override_are_immutable_after_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = create_task(config, "work", tmp, task_id="immutable", model_requirement_vector=requirement_v2())
            task["model_requirement_vector"]["revision_id"] = "reqrev-rewritten"
            with self.assertRaisesRegex(ValueError, "immutable after enqueue"):
                save_task(config, task)

            loaded = load_task(config, "immutable")
            loaded["routing_override"] = routing_override_value(
                "routing_override",
                {
                    "mode": "pin",
                    "target_id": "target-v1",
                    "reason": "operator request",
                    "scope": "single_task",
                    "allow_fallback": False,
                    "provenance": "operator_override",
                },
            )
            with self.assertRaisesRegex(ValueError, "immutable after enqueue"):
                save_task(config, loaded)

    def test_auto_fix_child_gets_own_derived_revision_and_no_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            parent = create_task(
                config,
                "parent",
                tmp,
                task_id="parent",
                model_requirement_vector=requirement_v2("reqrev-parent"),
                routing_override={
                    "mode": "preference",
                    "target_id": "target-v1",
                    "reason": "operator request",
                    "scope": "single_task",
                    "allow_fallback": True,
                    "provenance": "operator_override",
                },
            )
            child = enqueue_auto_fix_task(
                config,
                parent,
                {"fix_task_draft": {"root_task_id": "parent", "review_cycle": 1}, "config": {}},
                {"findings": [], "finding_fingerprints": []},
            )
            self.assertNotEqual("reqrev-parent", child["model_requirement_vector"]["revision_id"])
            self.assertNotIn("derivation_identity", child["model_requirement_vector"])
            self.assertNotIn("routing_override", child)


if __name__ == "__main__":
    unittest.main()

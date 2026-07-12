import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "model-routing-contract.md"


class ModelRoutingContractDocsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = CONTRACT.read_text(encoding="utf-8")

    def fenced_json(self) -> list[dict[str, object]]:
        return [json.loads(value) for value in re.findall(r"```json\n(.*?)\n```", self.text, re.DOTALL)]

    def test_requirement_v2_axes_and_anchors_are_parser_checkable(self) -> None:
        requirement = next(value for value in self.fenced_json() if value.get("schema_version") == 2)
        self.assertEqual("requirement-rubric-v1", requirement["derivation_version"])
        self.assertEqual(
            {
                "semantic_reasoning",
                "context_integration",
                "planning_depth",
                "instruction_fidelity",
                "tool_execution_reliability",
                "adversarial_detection",
            },
            set(requirement["quality_requirements"]),
        )
        for axis in requirement["quality_requirements"].values():
            self.assertIn(axis["anchor"], {0, 250, 500, 750, 1000})
            self.assertGreaterEqual(axis["score"], 0)
            self.assertLessEqual(axis["score"], 1000)
        self.assertEqual({"latency_weight", "cost_weight"}, set(requirement["utility_preferences"]))

    def test_constraints_unknown_policy_and_override_boundary_are_explicit(self) -> None:
        values = self.fenced_json()
        constraints = next(value for value in values if "required_execution_surfaces" in value)
        override = next(value["routing_override"] for value in values if "routing_override" in value)
        self.assertEqual(
            {
                "required_execution_surfaces",
                "required_tools",
                "minimum_context_tokens",
                "allowed_reasoning_efforts",
                "forbidden_provider_families",
                "interactive_input_required",
                "independent_provider_required",
            },
            set(constraints),
        )
        for policy in ("reject", "probe_only", "soft_penalty", "ignore"):
            self.assertRegex(self.text, rf"`{policy}`:")
        self.assertEqual("single_task", override["scope"])
        self.assertEqual("operator_override", override["provenance"])
        self.assertIn("retry, review, fix, follow-up에 상속되지 않습니다", self.text)
        self.assertIn("Task에는 별도 model/provider/profile field를 허용하지 않습니다", self.text)

    def test_identity_migration_and_freeze_invariants_are_durable(self) -> None:
        for invariant in (
            "`selected_model == command_model`",
            "optional compliance attestation",
            "D0 -> D1 -> D2 -> D3 -> D4 -> D5 -> D6",
            "Global CBR dispatch",
            "fresh independent review",
        ):
            self.assertIn(invariant, self.text)

        requirements = (ROOT / "docs" / "requirements.md").read_text(encoding="utf-8")
        spec = (ROOT / "docs" / "spec.md").read_text(encoding="utf-8")
        execution = (ROOT / "docs" / "execution.md").read_text(encoding="utf-8")
        task_schema = (ROOT / "docs" / "task-schema.md").read_text(encoding="utf-8")
        for text in (requirements, spec, execution, task_schema):
            self.assertIn("model-routing-contract.md", text)
        self.assertIn("REQ-SEPARATE-ROUTING-EVIDENCE", requirements)
        self.assertIn("immutable requirement v2 revision", task_schema)
        self.assertIn("exact v2 cohort에서 제외", task_schema)


if __name__ == "__main__":
    unittest.main()

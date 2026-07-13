import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_batch_runner.config import Config
from codex_batch_runner.model_requirements import legacy_requirement_projection, resolve_execution_config
from codex_batch_runner.queue import create_task, load_task, save_task
from codex_batch_runner.review_next import issue_automatic_reviewer_work_unit, record_automatic_reviewer_evidence
from codex_batch_runner.reviewer_codex import run_reviewer_codex


AXES = (
    "semantic_reasoning", "context_integration", "planning_depth", "instruction_fidelity",
    "tool_execution_reliability", "adversarial_detection",
)


def exact_config(root: Path, *, quality: int = 750) -> Config:
    target = {
        "execution_surface": "codex",
        "model": "gpt-review-exact",
        "reasoning_effort": "high",
        "trust_state": "trusted",
        "static_fitness": {axis: quality for axis in AXES},
        "latency_score": 500,
        "cost_score": 500,
        "capabilities": {"required_execution_surfaces": ["codex"], "interactive_input_required": False},
        "capability_evidence": {
            "required_execution_surfaces": {"source": "surface_reported"},
            "interactive_input_required": {"source": "surface_reported"},
        },
    }
    path = root / "config.json"
    path.write_text(json.dumps({
        "codex_command": ["codex", "exec", "--json"],
        "execution_target_inventory": {
            "schema_version": 1,
            "snapshot_id": "sha256:review-test",
            "status": "current",
            "constraint_registry_version": "constraints-v1",
            "targets": {"review-exact-v1": target},
        },
        "constraint_registry": {
            "schema_version": 1,
            "version": "constraints-v1",
            "constraints": {
                "required_execution_surfaces": {"unknown_policy": "reject"},
                "interactive_input_required": {"unknown_policy": "reject"},
            },
        },
    }), encoding="utf-8")
    return Config.load(str(path), root=root)


class AutomaticReviewerRoutingV2Tests(unittest.TestCase):
    def test_issuer_stores_immutable_native_child_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(root=root)
            parent = create_task(config, "implement", tmp, task_id="parent")
            reviewer = issue_automatic_reviewer_work_unit(config, parent)
            stored = load_task(config, "parent")
            self.assertEqual(2, reviewer["model_requirement_vector"]["schema_version"])
            self.assertNotIn("derivation_identity", reviewer["model_requirement_vector"])
            self.assertNotEqual(
                parent["model_requirement_vector"]["revision_id"],
                reviewer["model_requirement_vector"]["revision_id"],
            )
            stored["automatic_reviewer_work_units"][0]["model_requirement_vector"]["revision_id"] = "mutated"
            with self.assertRaisesRegex(ValueError, "immutable after issuance"):
                save_task(config, stored)

    def test_exact_model_reasoning_argv_and_identity_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = exact_config(root)
            parent = create_task(config, "implement", tmp, task_id="parent")
            reviewer = issue_automatic_reviewer_work_unit(config, parent)
            payload = {
                "task_id": "parent:automatic-review:1", "decision": "needs_human", "confidence": "high",
                "reason": "bounded", "findings": [], "required_human_checks": [], "auto_fix_allowed": False,
                "auto_fix_risk": "low", "suggested_fix_prompt": "", "finding_fingerprints": [],
                "reviewer_limits": {"calls_used_this_run": 1, "fix_loops_used_for_task": 0,
                                    "cooldown_recommended_seconds": 0},
            }
            completed = SimpleNamespace(returncode=0, stdout=json.dumps({"result": payload}) + "\n", stderr="")
            with patch("codex_batch_runner.reviewer_codex.subprocess.run", return_value=completed) as invoked:
                outcome = run_reviewer_codex(config, reviewer, {}, calls_used_this_run=1)
            argv = invoked.call_args.args[0]
            self.assertIn("gpt-review-exact", argv)
            self.assertIn("model_reasoning_effort=high", argv)
            identity = outcome.execution_evidence["identity"]
            self.assertEqual("gpt-review-exact", identity["command_model"])
            self.assertEqual("high", identity["command_reasoning_effort"])
            self.assertEqual(
                reviewer["model_requirement_vector"]["revision_id"],
                outcome.execution_evidence["versions"]["requirement_revision_id"],
            )
            record_automatic_reviewer_evidence(
                config,
                "parent",
                {"decision": "needs_human", "confidence": "high"},
                outcome.execution_evidence,
            )
            stored = load_task(config, "parent")
            self.assertEqual(1, len(stored["automatic_reviewer_execution_evidence_history"]))
            review = stored["review_outcome_evidence_history"][-1]
            self.assertEqual(
                outcome.execution_evidence["cohort"]["cohort_id"],
                review["cohort"]["components"]["reviewer_execution_cohort_id"],
            )
            self.assertIn("execution_cohort_id", review["cohort"]["components"])
            self.assertIn("review_policy_version", review["cohort"]["components"])
            self.assertIn("rubric_version", review["cohort"]["components"])

    def test_selector_does_not_reinterpret_vector_from_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = exact_config(Path(tmp))
            parent = create_task(config, "implement", tmp, task_id="parent")
            reviewer = issue_automatic_reviewer_work_unit(config, parent)
            reviewer["worker_role"] = "implementer"
            first = resolve_execution_config(config, reviewer)
            reviewer["worker_role"] = "reviewer"
            second = resolve_execution_config(config, reviewer)
            self.assertEqual(first.requirement_vector, second.requirement_vector)
            self.assertEqual(first.execution_target, second.execution_target)

    def test_missing_or_ineligible_exact_target_fails_before_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(root=root)
            reviewer = {"id": "review", "cwd": tmp}
            with patch("codex_batch_runner.reviewer_codex.subprocess.run") as invoked:
                outcome = run_reviewer_codex(config, reviewer, {}, calls_used_this_run=1)
            self.assertFalse(outcome.invoked)
            self.assertIn("automatic reviewer requires native v2", outcome.reason)
            invoked.assert_not_called()

            config = exact_config(root, quality=250)
            parent = create_task(config, "high risk migration", tmp, task_id="high", routing_risk="high")
            reviewer = issue_automatic_reviewer_work_unit(config, parent)
            with patch("codex_batch_runner.reviewer_codex.subprocess.run") as invoked:
                outcome = run_reviewer_codex(config, reviewer, {}, calls_used_this_run=1)
            self.assertFalse(outcome.invoked)
            self.assertIn("below_quality_floor", outcome.reason)
            invoked.assert_not_called()

    def test_legacy_task_remains_readable_but_non_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            legacy = legacy_requirement_projection({"dimensions": {"reasoning_depth": "high"}})
            resolved = resolve_execution_config(config, {"id": "legacy", "model_requirement_vector": legacy})
            self.assertEqual("legacy-derived", resolved.requirement_vector["derivation_identity"]["kind"])
            self.assertEqual("cli_default", resolved.model_source)


if __name__ == "__main__":
    unittest.main()

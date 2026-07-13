from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.cli import main
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

        for boundary in (
            "credentials",
            "deployment",
            "destructive",
            "financial",
            "privacy",
            "public_private",
            "security",
        ):
            with self.subTest(boundary=boundary):
                decision = exploration_admission(context(boundaries=[boundary]), POLICY)
                self.assertFalse(decision["admitted"])
                self.assertIn("prohibited_boundary", decision["reasons"])

    def test_boundaries_require_a_list_of_non_empty_strings(self) -> None:
        for boundaries in ("security", [""], [1]):
            with self.subTest(boundaries=boundaries):
                with self.assertRaisesRegex(ExplorationError, "boundaries must be"):
                    exploration_admission(context(boundaries=boundaries), POLICY)

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

    def test_budget_and_concurrency_require_strict_non_negative_integers(self) -> None:
        malformed = ("1", None, True, -1)
        for field in ("budget_remaining", "active_project_probes"):
            for value in malformed:
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ExplorationError, rf"{field} must be a non-negative integer"):
                        exploration_admission(context(**{field: value}), POLICY)

    def test_cli_malformed_numeric_guards_return_sanitized_exit_one(self) -> None:
        malformed = ("1", None, True, -1)
        for field in ("budget_remaining", "active_project_probes"):
            for value in malformed:
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    config_path = root / "config.json"
                    context_path = root / "context.json"
                    policy_path = root / "policy.json"
                    config_path.write_text(json.dumps({"queue_dir": str(root / "tasks")}), encoding="utf-8")
                    context_path.write_text(json.dumps(context(**{field: value})), encoding="utf-8")
                    policy_path.write_text(json.dumps(POLICY), encoding="utf-8")
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        code = main([
                            "--config", str(config_path), "exploration-report",
                            "--context-json", str(context_path),
                            "--exploration-policy-json", str(policy_path),
                        ])
                    self.assertEqual(code, 1)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(stderr.getvalue(), f"error: {field} must be a non-negative integer\n")
                    self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

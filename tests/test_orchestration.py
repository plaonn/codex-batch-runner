from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_batch_runner.cli as cli_module
from codex_batch_runner.cli import main
from codex_batch_runner.orchestration import MUTATION_ORDER, VALIDATION_ORDER, build_orchestration_plan, error_plan, validate_manifest


def manifest(**changes: object) -> dict:
    value = {
        "schema_version": 1,
        "contract": "orchestration-intake-v1",
        "request_id": "sample-request",
        "idempotency_key": "sample-key",
        "source": {"kind": "codex_parent_thread", "collection_owner": "source_parent"},
        "summary": {"root_goal": "Sanitized goal", "requirement": "Sanitized requirement", "stop_condition": "Sanitized stop", "done_means": "Sanitized done"},
        "authority": {"decision_authority": "delegated_decision", "resolution": "resolved", "impact": "low", "approval_state": "not_required"},
        "work": {"kind": "implementation", "interaction": "none", "duration": "short", "persistence": "turn_bound", "resume": "not_needed", "dependency": "none", "collection": "immediate_parent", "context": "self_contained", "isolation": "worktree", "verification": "objective", "external_worker_boundary": "unavailable", "repository_scope": "present"},
        "mutation": {"allowed": ["tracked_files"], "prohibited": ["runtime_state", "external_state", "destructive"]},
        "automation_boundary": "advisory_only",
        "surface_preferences": ["codex_subagent", "cbr_batch", "codex_parent_thread", "external_worker", "codex_user_owned_thread"],
    }
    for key, changed in changes.items():
        value[key] = changed
    return value


class OrchestrationPlannerTests(unittest.TestCase):
    def cli_json(self, path: Path) -> tuple[int, dict]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["orchestration", "plan", "--manifest", str(path), "--json"])
        return code, json.loads(stdout.getvalue())

    def test_small_immediate_work_selects_subagent_with_ordered_exclusions(self) -> None:
        plan = build_orchestration_plan(validate_manifest(manifest()))
        self.assertEqual("ready", plan["decision_status"])
        self.assertEqual("codex_subagent", plan["recommended_surface"])
        self.assertEqual(["codex_parent_thread"], plan["fallback_surfaces"])
        self.assertEqual(["selected_first_eligible_surface", "selected_subagent"], plan["reason_codes"])
        self.assertEqual("cbr_batch", plan["excluded_surfaces"][0]["surface"])
        self.assertEqual(["persistence_incompatible"], plan["excluded_surfaces"][0]["reason_codes"])
        self.assertFalse(plan["mutation"]["allowed"])
        self.assertFalse(plan["mutation"]["applied"])

    def test_durable_unattended_work_can_select_cbr(self) -> None:
        value = manifest()
        value["work"]["duration"] = "long"
        value["work"]["persistence"] = "durable"
        value["work"]["resume"] = "required"
        value["work"]["dependency"] = "hard"
        value["work"]["collection"] = "durable_attention"
        value["surface_preferences"] = ["cbr_batch", "codex_subagent"]
        plan = build_orchestration_plan(validate_manifest(value))
        self.assertEqual("cbr_batch", plan["recommended_surface"])
        self.assertEqual(["selected_first_eligible_surface", "selected_cbr_batch"], plan["reason_codes"])

    def test_user_decision_is_not_routed_unattended(self) -> None:
        value = manifest()
        value["authority"] = {"decision_authority": "recommend_and_pause", "resolution": "needs_user_decision", "impact": "high", "approval_state": "required"}
        value["mutation"]["allowed"] = ["local_files"]
        plan = build_orchestration_plan(validate_manifest(value))
        self.assertEqual("needs_user_decision", plan["decision_status"])
        self.assertIsNone(plan["recommended_surface"])
        self.assertEqual([], plan["fallback_surfaces"])
        self.assertEqual(["obtain_user_decision"], plan["required_preflight"])

    def test_external_block_is_not_routed(self) -> None:
        value = manifest()
        value["authority"]["resolution"] = "blocked_external"
        value["work"]["interaction"] = "external_required"
        plan = build_orchestration_plan(validate_manifest(value))
        self.assertEqual("blocked", plan["decision_status"])
        self.assertIsNone(plan["recommended_surface"])
        self.assertEqual(["resolve_external_blocker"], plan["required_preflight"])

    def test_summary_is_not_classified_or_exposed(self) -> None:
        first = manifest()
        second = manifest()
        second["summary"] = {key: "Different sanitized text" for key in second["summary"]}
        plan_one = build_orchestration_plan(validate_manifest(first))
        plan_two = build_orchestration_plan(validate_manifest(second))
        self.assertEqual(plan_one["recommended_surface"], plan_two["recommended_surface"])
        self.assertNotEqual(plan_one["request_fingerprint"], plan_two["request_fingerprint"])
        self.assertNotIn("Different sanitized text", json.dumps(plan_two))

    def test_disallowed_surfaces_are_only_excluded(self) -> None:
        value = manifest()
        value["mutation"]["allowed"] = ["external_state"]
        value["mutation"]["prohibited"] = ["runtime_state", "destructive"]
        plan = build_orchestration_plan(validate_manifest(value))
        self.assertEqual("codex_parent_thread", plan["recommended_surface"])
        self.assertEqual([], plan["fallback_surfaces"])
        self.assertEqual({"codex_subagent", "cbr_batch", "external_worker", "codex_user_owned_thread"}, {item["surface"] for item in plan["excluded_surfaces"]})

    def test_invalid_cross_field_fails_closed(self) -> None:
        value = manifest()
        value["authority"]["approval_state"] = "required"
        with self.assertRaisesRegex(ValueError, "cross_field_conflict"):
            validate_manifest(value)

    def test_cli_is_config_independent_and_returns_structured_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(manifest()), encoding="utf-8")
            original = os.environ.get("CBR_CONFIG")
            os.environ["CBR_CONFIG"] = str(Path(tmp) / "missing-config.json")
            try:
                stdout = io.StringIO()
                with patch("codex_batch_runner.cli.Config.load", side_effect=AssertionError("must not load config")) as load:
                    with contextlib.redirect_stdout(stdout):
                        self.assertEqual(0, main(["orchestration", "plan", "--manifest", str(path), "--json"]))
                load.assert_not_called()
                self.assertEqual("ready", json.loads(stdout.getvalue())["decision_status"])
                path.write_text("{}", encoding="utf-8")
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(2, main(["orchestration", "plan", "--manifest", str(path), "--json"]))
                report = json.loads(stdout.getvalue())
                self.assertEqual("orchestration-plan-error-v1", report["contract"])
                self.assertEqual("invalid", report["decision_status"])
                self.assertEqual(["fields_invalid"], report["validation_errors"])
                self.assertEqual({"schema_version", "contract", "request_id", "decision_status", "recommended_surface", "fallback_surfaces", "reason_codes", "validation_errors", "excluded_surfaces", "unresolved_constraints", "required_preflight", "collection_owner", "mutation"}, set(report))
            finally:
                if original is None:
                    os.environ.pop("CBR_CONFIG", None)
                else:
                    os.environ["CBR_CONFIG"] = original

    def test_cli_rejects_global_config(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(2, main(["--config", "ignored.json", "orchestration", "plan", "--manifest", "missing.json"]))
        self.assertIn("--config is not supported", stderr.getvalue())

    def test_unhashable_list_items_return_structured_errors(self) -> None:
        for field in ("surface_preferences",):
            value = manifest()
            value[field] = [{}]
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "bad.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(2, main(["orchestration", "plan", "--manifest", str(path), "--json"]))
                self.assertEqual(["value_type_invalid"], json.loads(output.getvalue())["validation_errors"])
        value = manifest()
        value["mutation"]["allowed"] = [[]]
        value["mutation"]["prohibited"] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(2, main(["orchestration", "plan", "--manifest", str(path), "--json"]))
            self.assertEqual(["value_type_invalid"], json.loads(output.getvalue())["validation_errors"])

    def test_safe_identifier_matches_shared_public_class(self) -> None:
        value = manifest()
        value["request_id"] = "Upper.ID:1"
        self.assertEqual("Upper.ID:1", validate_manifest(value)["request_id"])

    def test_unexpected_orchestration_exception_uses_stderr_exit_one(self) -> None:
        with patch("codex_batch_runner.cli.load_manifest", side_effect=RuntimeError("unexpected")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(1, main(["orchestration", "plan", "--manifest", "any.json"]))
        self.assertIn("error: unexpected", stderr.getvalue())

    def test_validation_error_contract_orders_every_public_code(self) -> None:
        for code in VALIDATION_ORDER:
            report = error_plan((code,))
            self.assertEqual([code], report["validation_errors"])
            self.assertEqual("invalid", report["decision_status"])
        self.assertEqual(["input_unreadable", "cross_field_conflict"], error_plan(("cross_field_conflict", "input_unreadable"))["validation_errors"])

    def test_every_invalid_source_owner_pair_fails_closed(self) -> None:
        pairs = {"codex_parent_thread": "source_parent", "codex_user_owned_thread": "source_user", "todoist_task": "operator", "operator": "operator", "automation": "operator", "other": "external_owner"}
        owners = {"source_parent", "source_user", "operator", "external_owner"}
        for kind, valid_owner in pairs.items():
            for owner in owners - {valid_owner}:
                value = manifest()
                value["source"] = {"kind": kind, "collection_owner": owner}
                with self.assertRaisesRegex(ValueError, "cross_field_conflict"):
                    validate_manifest(value)

    def test_non_preferred_surface_is_neither_evaluated_nor_excluded(self) -> None:
        value = manifest()
        value["surface_preferences"] = ["codex_parent_thread"]
        plan = build_orchestration_plan(validate_manifest(value))
        self.assertEqual("codex_parent_thread", plan["recommended_surface"])
        self.assertEqual([], plan["excluded_surfaces"])

    def test_every_validation_code_is_emitted_by_real_cli_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases: list[tuple[str, bytes | None, str]] = [
                ("input_unreadable", None, "input_unreadable"),
                ("input_too_large", b"x" * (64 * 1024 + 1), "input_too_large"),
                ("input_not_utf8", b"\xff", "input_not_utf8"),
                ("input_json_invalid", b"{", "input_json_invalid"),
                ("input_not_object", b"[]", "input_not_object"),
            ]
            manifests: list[tuple[str, dict, str]] = []
            broken = manifest(); broken.pop("work"); manifests.append(("fields_invalid", broken, "fields_invalid"))
            broken = manifest(); broken["surface_preferences"] = [{}]; manifests.append(("value_type_invalid", broken, "value_type_invalid"))
            broken = manifest(); broken["surface_preferences"] = ["not-a-surface"]; manifests.append(("value_enum_invalid", broken, "value_enum_invalid"))
            broken = manifest(); broken["surface_preferences"] = ["codex_parent_thread"] * 9; manifests.append(("value_bounds_invalid", broken, "value_bounds_invalid"))
            broken = manifest(); broken["request_id"] = "bad space"; manifests.append(("unsafe_identifier", broken, "unsafe_identifier"))
            broken = manifest(); broken["prompt"] = "forbidden"; manifests.append(("sensitive_field_forbidden", broken, "sensitive_field_forbidden"))
            broken = manifest(); broken["surface_preferences"] = ["codex_parent_thread", "codex_parent_thread"]; manifests.append(("duplicate_list_item", broken, "duplicate_list_item"))
            broken = manifest(); broken["surface_preferences"] = []; manifests.append(("empty_surface_preferences", broken, "empty_surface_preferences"))
            broken = manifest(); broken["mutation"]["prohibited"] = ["tracked_files"]; manifests.append(("mutation_overlap", broken, "mutation_overlap"))
            broken = manifest(); broken["source"]["collection_owner"] = "operator"; manifests.append(("cross_field_conflict", broken, "cross_field_conflict"))
            for name, raw, expected in cases:
                path = root / name
                if raw is not None: path.write_bytes(raw)
                code, report = self.cli_json(path)
                self.assertEqual(2, code, name)
                self.assertEqual([expected], report["validation_errors"], name)
            for name, value, expected in manifests:
                path = root / f"{name}.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                code, report = self.cli_json(path)
                self.assertEqual(2, code, name)
                self.assertEqual([expected], report["validation_errors"], name)

    def test_global_outcomes_and_every_surface_selection(self) -> None:
        cases = []
        user = manifest(); user["authority"] = {"decision_authority": "recommend_and_pause", "resolution": "needs_user_decision", "impact": "low", "approval_state": "not_required"}; user["mutation"]["allowed"] = ["local_files"]
        cases.append((user, "needs_user_decision", ["authority_resolution_requires_user_decision"]))
        both = manifest(); both["authority"] = {"decision_authority": "recommend_and_pause", "resolution": "needs_user_decision", "impact": "low", "approval_state": "required"}; both["mutation"]["allowed"] = ["local_files"]
        cases.append((both, "needs_user_decision", ["authority_resolution_requires_user_decision", "approval_required"]))
        external = manifest(); external["authority"]["resolution"] = "blocked_external"; external["work"]["collection"] = "external_callback"
        cases.append((external, "blocked", ["authority_blocked_external"]))
        external_both = manifest(); external_both["authority"]["resolution"] = "blocked_external"; external_both["work"]["interaction"] = "external_required"
        cases.append((external_both, "blocked", ["authority_blocked_external", "external_interaction_required"]))
        for value, status, reasons in cases:
            plan = build_orchestration_plan(validate_manifest(value))
            self.assertEqual(status, plan["decision_status"])
            self.assertEqual(reasons, plan["reason_codes"])
            self.assertIsNone(plan["recommended_surface"])
            self.assertEqual([], plan["excluded_surfaces"])
        none = manifest(); none["surface_preferences"] = ["cbr_batch"]
        plan = build_orchestration_plan(validate_manifest(none))
        self.assertEqual(("blocked", ["no_eligible_surface"], ["surface_constraints_unsatisfied"], []), (plan["decision_status"], plan["reason_codes"], plan["unresolved_constraints"], plan["required_preflight"]))
        selected = {
            "codex_parent_thread": manifest(surface_preferences=["codex_parent_thread"]),
            "codex_user_owned_thread": manifest(work={**manifest()["work"], "interaction": "user_required"}, surface_preferences=["codex_user_owned_thread"]),
            "codex_subagent": manifest(surface_preferences=["codex_subagent"]),
            "cbr_batch": manifest(work={**manifest()["work"], "duration": "long"}, surface_preferences=["cbr_batch"]),
            "external_worker": manifest(work={**manifest()["work"], "external_worker_boundary": "verified_bounded"}, surface_preferences=["external_worker"]),
        }
        preflight = {"codex_parent_thread": [], "codex_user_owned_thread": ["confirm_user_continuation"], "codex_subagent": ["verify_immediate_parent_collection"], "cbr_batch": ["verify_cbr_admission"], "external_worker": ["verify_external_worker_contract"]}
        for surface, value in selected.items():
            plan = build_orchestration_plan(validate_manifest(value))
            self.assertEqual(["selected_first_eligible_surface", "selected_" + surface.removeprefix("codex_")], plan["reason_codes"])
            self.assertEqual(preflight[surface], plan["required_preflight"])

    def test_valid_plan_keys_constraints_and_cli_runtime_purity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); runtime = root / "runtime"; runtime.mkdir()
            for name in ("queue.json", "events.jsonl", "state.json", "config.json", "outbox.json"):
                (runtime / name).write_bytes((name + " sentinel").encode())
            path = root / "manifest.json"; path.write_text(json.dumps(manifest()), encoding="utf-8")
            before = {item.relative_to(root): item.read_bytes() for item in root.rglob("*") if item.is_file()}
            # D1 has no thread dependency: this CLI module exports no thread creation call-site.
            self.assertFalse(hasattr(cli_module, "create_thread"))
            self.assertFalse(hasattr(cli_module, "spawn_agent"))
            with patch("codex_batch_runner.cli.Config.load", side_effect=AssertionError("config")) as config_load, patch("codex_batch_runner.cli.list_tasks", side_effect=AssertionError("queue-read")), patch("codex_batch_runner.cli.create_task", side_effect=AssertionError("queue-write")), patch("codex_batch_runner.cli.archive_task", side_effect=AssertionError("queue-write")), patch("codex_batch_runner.cli.set_resolution", side_effect=AssertionError("queue-write")), patch("codex_batch_runner.cli.set_review_status", side_effect=AssertionError("queue-write")), patch("codex_batch_runner.cli.write_event_nonfatal", side_effect=AssertionError("event")), patch("codex_batch_runner.cli.list_parent_attention", side_effect=AssertionError("outbox")), patch("codex_batch_runner.cli.deliver_parent_attention", side_effect=AssertionError("external")), patch("codex_batch_runner.cli.run_snapshot_adapter", side_effect=AssertionError("external")), patch("subprocess.run", side_effect=AssertionError("run")), patch("subprocess.Popen", side_effect=AssertionError("popen")):
                code, plan = self.cli_json(path)
            config_load.assert_not_called()
            self.assertEqual(0, code)
            after = {item.relative_to(root): item.read_bytes() for item in root.rglob("*") if item.is_file()}
            self.assertEqual(before, after)
            self.assertEqual({"schema_version", "contract", "request_id", "request_fingerprint", "decision_status", "recommended_surface", "fallback_surfaces", "reason_codes", "excluded_surfaces", "unresolved_constraints", "required_preflight", "collection_owner", "execution_constraints", "mutation"}, set(plan))
            self.assertEqual({"decision_authority", "allowed_mutation_classes", "prohibited_mutation_classes", "automation_boundary"}, set(plan["execution_constraints"]))

    def test_authority_mutation_boundary_matrix(self) -> None:
        permitted = {"proposal_only": {"read_only"}, "recommend_and_pause": {"read_only", "local_files"}, "bounded_experiment": {"read_only", "local_files", "tracked_files"}, "delegated_decision": set(MUTATION_ORDER)}
        for authority, allowed_mutations in permitted.items():
            for mutation in MUTATION_ORDER:
                value = manifest()
                value["authority"]["decision_authority"] = authority
                value["mutation"] = {"allowed": [mutation], "prohibited": []}
                if mutation in allowed_mutations:
                    validate_manifest(value)
                else:
                    with self.assertRaisesRegex(ValueError, "cross_field_conflict"):
                        validate_manifest(value)
        valid_automatic = manifest(); valid_automatic["automation_boundary"] = "bounded_automatic"; validate_manifest(valid_automatic)
        invalid = []
        value = manifest(); value["authority"]["decision_authority"] = "proposal_only"; value["mutation"] = {"allowed": ["read_only"], "prohibited": []}; value["authority"]["approval_state"] = "granted"; invalid.append(value)
        value = manifest(); value["authority"]["decision_authority"] = "recommend_and_pause"; value["mutation"] = {"allowed": ["local_files"], "prohibited": []}; value["automation_boundary"] = "bounded_automatic"; invalid.append(value)
        value = manifest(); value["automation_boundary"] = "bounded_automatic"; value["authority"]["decision_authority"] = "proposal_only"; invalid.append(value)
        value = manifest(); value["automation_boundary"] = "bounded_automatic"; value["authority"]["resolution"] = "needs_user_decision"; invalid.append(value)
        value = manifest(); value["automation_boundary"] = "bounded_automatic"; value["authority"]["approval_state"] = "required"; value["authority"]["resolution"] = "needs_user_decision"; invalid.append(value)
        for value in invalid:
            with self.assertRaisesRegex(ValueError, "cross_field_conflict"):
                validate_manifest(value)

    def test_nfc_and_mutation_order_make_byte_identical_json(self) -> None:
        value = manifest()
        value["summary"]["root_goal"] = "Cafe\u0301"
        value["mutation"]["prohibited"] = ["destructive", "runtime_state", "external_state"]
        first = build_orchestration_plan(validate_manifest(value))
        value["summary"]["root_goal"] = "Café"
        second = build_orchestration_plan(validate_manifest(value))
        self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True), json.dumps(second, ensure_ascii=False, sort_keys=True))

    def test_planner_does_not_call_adapters(self) -> None:
        with patch("codex_batch_runner.orchestration.Path.read_bytes", side_effect=AssertionError("no file read")):
            plan = build_orchestration_plan(validate_manifest(manifest()))
        self.assertEqual("ready", plan["decision_status"])


if __name__ == "__main__":
    unittest.main()

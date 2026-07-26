from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.goal_reconciliation import (
    GoalExplainError,
    GoalReconciliationError,
    build_goal_explain_view,
    build_goal_reconciliation_report,
    load_goal_manifest,
    render_goal_explain_view,
    validate_goal_explain_view,
    validate_goal_manifest,
    validate_manifest_revision,
)
from codex_batch_runner.orchestration_selection import stable_digest
from codex_batch_runner.cli import main


def manifest(revision: int = 1, *, supersedes: dict | None = None) -> dict:
    value = {
        "schema_version": 1,
        "contract": "source-goal-manifest-v1",
        "goal_id": "goal-public-1",
        "source": {
            "owner_kind": "source_project",
            "source_ref": "public-goal-ref",
            "adapter_revision": "adapter-v1",
        },
        "revision": revision,
        "root_outcome": {
            "summary": "Public-safe outcome",
            "acceptance_references": ["acceptance-v1"],
        },
        "decision_authority": {
            "goal": "source_owner",
            "node_default": "codex_coordinator",
        },
        "automation_boundary": {
            "allowed_mutations": ["read_only"],
            "prohibited_mutations": [
                "dispatch",
                "source_write",
                "delivery",
                "acknowledgement",
                "completion",
            ],
            "attention_gates": ["manual-collection"],
        },
        "nodes": [
            {
                "node_id": "node-a",
                "executable_contract_digest": "sha256:" + "a" * 64,
                "authority": "codex_coordinator",
                "dependencies": [],
                "dependency_mode": "wait-for-acceptance",
                "required_outcome": "Verified package",
                "verification_references": ["tests-v1"],
                "terminal_contribution": "required",
            }
        ],
        "terminal_condition": {
            "required_nodes": ["node-a"],
            "root_acceptance_references": ["acceptance-v1"],
        },
        "supersedes": supersedes,
    }
    value["manifest_digest"] = stable_digest(value)
    return value


class GoalReconciliationTests(unittest.TestCase):
    def test_manifest_and_empty_readonly_projection_are_deterministic(self) -> None:
        value = manifest()
        first = build_goal_reconciliation_report(value)
        second = build_goal_reconciliation_report(copy.deepcopy(value))
        self.assertEqual(first, second)
        self.assertFalse(first["terminal_candidate"])
        self.assertEqual({"allowed": False, "applied": False}, first["mutation"])
        axes = first["nodes"][0]["axes"]
        self.assertEqual("observed", axes["contract_binding"]["status"])
        self.assertEqual("unknown", axes["selection"]["status"])
        self.assertEqual("unknown", axes["parent_collection"]["status"])
        self.assertEqual("unknown", axes["source_disposition"]["status"])

    def test_unknown_fields_and_private_values_fail_closed(self) -> None:
        for mutate in (
            lambda value: value.update({"unexpected": True}),
            lambda value: value["root_outcome"].update({"prompt": "secret"}),
            lambda value: value["root_outcome"].update(
                {"summary": "/Users/operator/private"}
            ),
            lambda value: value["root_outcome"].update({"summary": "/tmp/private"}),
            lambda value: value["root_outcome"].update(
                {"summary": "api_key=not-public"}
            ),
        ):
            value = manifest()
            mutate(value)
            with self.assertRaises(GoalReconciliationError):
                validate_goal_manifest(value)

    def test_digest_mismatch_dangling_and_cycle_are_rejected(self) -> None:
        value = manifest()
        value["manifest_digest"] = "sha256:" + "b" * 64
        with self.assertRaisesRegex(GoalReconciliationError, "digest"):
            validate_goal_manifest(value)
        value = manifest()
        value["nodes"][0]["dependencies"] = ["missing"]
        value["manifest_digest"] = stable_digest(
            {key: item for key, item in value.items() if key != "manifest_digest"}
        )
        with self.assertRaisesRegex(GoalReconciliationError, "dangling"):
            validate_goal_manifest(value)
        value = manifest()
        node = copy.deepcopy(value["nodes"][0])
        node["node_id"] = "node-b"
        node["dependencies"] = ["node-a"]
        value["nodes"][0]["dependencies"] = ["node-b"]
        value["nodes"].append(node)
        value["manifest_digest"] = stable_digest(
            {key: item for key, item in value.items() if key != "manifest_digest"}
        )
        with self.assertRaisesRegex(GoalReconciliationError, "cycle"):
            validate_goal_manifest(value)

    def test_binding_conflict_and_admitted_node_drift_are_rejected(self) -> None:
        old = manifest()
        report = build_goal_reconciliation_report(old)
        report["nodes"][0]["axes"]["admission"] = {
            "status": "observed",
            "reason_code": "cbr_admitted",
        }
        report["report_digest"] = stable_digest(
            {key: item for key, item in report.items() if key != "report_digest"}
        )
        new = manifest(
            2,
            supersedes={
                "goal_id": old["goal_id"],
                "revision": old["revision"],
                "manifest_digest": old["manifest_digest"],
            },
        )
        new["nodes"][0]["required_outcome"] = "Changed admitted contract"
        new["manifest_digest"] = stable_digest(
            {key: item for key, item in new.items() if key != "manifest_digest"}
        )
        with self.assertRaisesRegex(GoalReconciliationError, "in-place rewrite"):
            validate_manifest_revision(old, new, report)
        deleted = manifest(
            2,
            supersedes={
                "goal_id": old["goal_id"],
                "revision": old["revision"],
                "manifest_digest": old["manifest_digest"],
            },
        )
        deleted["nodes"] = []
        deleted["terminal_condition"]["required_nodes"] = []
        deleted["manifest_digest"] = stable_digest(
            {key: item for key, item in deleted.items() if key != "manifest_digest"}
        )
        # Use a valid replacement node so this tests admitted removal, not empty graph.
        replacement = copy.deepcopy(old["nodes"][0])
        replacement["node_id"] = "node-b"
        deleted["nodes"] = [replacement]
        deleted["terminal_condition"]["required_nodes"] = ["node-b"]
        deleted["manifest_digest"] = stable_digest(
            {key: item for key, item in deleted.items() if key != "manifest_digest"}
        )
        with self.assertRaisesRegex(GoalReconciliationError, "removal"):
            validate_manifest_revision(old, deleted, report)
        evidence = {
            "schema_version": 1,
            "contract": "goal-reconciliation-evidence-v1",
            "goal_id": old["goal_id"],
            "revision": 2,
            "manifest_digest": old["manifest_digest"],
            "nodes": [],
        }
        with self.assertRaisesRegex(GoalReconciliationError, "binding mismatch"):
            build_goal_reconciliation_report(old, evidence)

    def test_node_evidence_requires_exact_executable_contract_binding(self) -> None:
        value = manifest()
        expected = value["nodes"][0]["executable_contract_digest"]
        evidence = {
            "schema_version": 1,
            "contract": "goal-reconciliation-evidence-v1",
            "goal_id": value["goal_id"],
            "revision": value["revision"],
            "manifest_digest": value["manifest_digest"],
            "nodes": [
                {
                    "node_id": "node-a",
                    "executable_contract_digest": expected,
                    "cbr_selection_funnel": {
                        "source_contract_digest": "sha256:" + "b" * 64,
                        "surface_rows": [],
                    },
                }
            ],
        }
        with patch(
            "codex_batch_runner.goal_reconciliation.validate_selection_funnel",
            side_effect=lambda item: item,
        ):
            with self.assertRaisesRegex(
                GoalReconciliationError, "node evidence contract"
            ):
                build_goal_reconciliation_report(value, evidence)
            evidence["nodes"][0]["cbr_selection_funnel"]["source_contract_digest"] = (
                expected
            )
            report = build_goal_reconciliation_report(value, evidence)
        self.assertEqual("unknown", report["nodes"][0]["axes"]["admission"]["status"])

    def test_loader_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(manifest()), encoding="utf-8")
            before = sorted(item.name for item in Path(tmp).iterdir())
            self.assertEqual("goal-public-1", load_goal_manifest(path)["goal_id"])
            self.assertEqual(before, sorted(item.name for item in Path(tmp).iterdir()))

    def test_cli_is_readonly_and_fails_closed_for_tampered_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest()), encoding="utf-8")
            before = sorted(str(item.relative_to(root)) for item in root.rglob("*"))
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "orchestration",
                        "goal-reconcile",
                        "--goal-manifest",
                        str(path),
                        "--json",
                    ]
                )
            self.assertEqual(0, code)
            self.assertIn("goal-reconciliation-report-v1", stdout.getvalue())
            self.assertEqual(
                before, sorted(str(item.relative_to(root)) for item in root.rglob("*"))
            )
            value = manifest()
            value["manifest_digest"] = "sha256:" + "c" * 64
            path.write_text(json.dumps(value), encoding="utf-8")
            with contextlib.redirect_stderr(stderr):
                code = main(
                    ["orchestration", "goal-reconcile", "--goal-manifest", str(path)]
                )
            self.assertEqual(2, code)

    def test_goal_explain_view_is_deterministic_and_preserves_order_and_axes(
        self,
    ) -> None:
        m = manifest()
        m["nodes"].append(
            {
                "node_id": "node-b",
                "executable_contract_digest": "sha256:" + "b" * 64,
                "authority": "external_authority",
                "dependencies": ["node-a"],
                "dependency_mode": "wait-for-completion",
                "required_outcome": "External acceptance",
                "verification_references": ["external-tests-v1"],
                "terminal_contribution": "advisory",
            }
        )
        m["manifest_digest"] = stable_digest(
            {key: value for key, value in m.items() if key != "manifest_digest"}
        )
        rep = build_goal_reconciliation_report(m)
        view1 = build_goal_explain_view(m, rep)
        view2 = build_goal_explain_view(copy.deepcopy(m), copy.deepcopy(rep))
        self.assertEqual(view1, view2)
        self.assertEqual("goal-explain-view-v1", view1["contract"])
        self.assertEqual("source_owner", view1["goal"]["goal_decision_authority"])
        self.assertEqual("await_trusted_evidence", view1["next_decision"]["kind"])
        self.assertFalse(view1["terminal"]["report_candidate"])
        self.assertEqual({"allowed": False, "applied": False}, view1["mutation"])
        self.assertEqual(
            ["node-a", "node-b"], [row["node_id"] for row in view1["nodes"]]
        )
        self.assertEqual(len(m["nodes"]), len(view1["nodes"]))
        self.assertEqual("codex_coordinator", view1["nodes"][0]["authority"])
        self.assertEqual("wait-for-acceptance", view1["nodes"][0]["dependency_mode"])
        self.assertEqual("Verified package", view1["nodes"][0]["required_outcome"])
        self.assertEqual(11, len(view1["nodes"][0]["axes"]))
        self.assertEqual(
            {
                "manifest_digest": m["manifest_digest"],
                "report_digest": rep["report_digest"],
            },
            view1["input_digests"],
        )
        self.assertTrue(
            all(claim is False for claim in view1["authority_claims"].values())
        )
        self.assertEqual(
            view1,
            validate_goal_explain_view(copy.deepcopy(view1)),
        )

    def test_goal_explain_generic_unknown_is_not_conflict(self) -> None:
        m = manifest()
        rep = build_goal_reconciliation_report(m)
        rep["nodes"][0]["axes"]["admission"] = {
            "status": "unknown",
            "reason_code": "missing_adapter_evidence",
        }
        rep["report_digest"] = stable_digest(
            {key: value for key, value in rep.items() if key != "report_digest"}
        )
        view = build_goal_explain_view(m, rep)
        admission_issue = next(
            issue
            for issue in view["nodes"][0]["issues"]
            if issue["axis"] == "admission"
        )
        self.assertEqual("unknown", admission_issue["kind"])
        self.assertEqual("await_trusted_evidence", view["next_decision"]["kind"])

    def test_goal_explain_explicit_conflict_has_highest_priority(self) -> None:
        m = manifest()
        rep = build_goal_reconciliation_report(m)
        rep_conflict = copy.deepcopy(rep)
        rep_conflict["nodes"][0]["axes"]["admission"] = {
            "status": "unknown",
            "reason_code": "admitted_drift",
        }
        rep_conflict["nodes"][0]["axes"]["attention_recorded"] = {
            "status": "observed",
            "reason_code": "cbr_parent_attention_recorded",
        }
        rep_conflict["nodes"][0]["recommendations"] = [
            "attention_required",
            "blocked_conflict",
        ]
        rep_conflict["report_digest"] = stable_digest(
            {k: val for k, val in rep_conflict.items() if k != "report_digest"}
        )
        v_conflict = build_goal_explain_view(m, rep_conflict)
        self.assertEqual("resolve_input_conflict", v_conflict["next_decision"]["kind"])
        self.assertEqual("codex_coordinator", v_conflict["next_decision"]["owner"])
        self.assertIn(
            {"kind": "conflict", "axis": "admission", "reason_code": "admitted_drift"},
            v_conflict["nodes"][0]["issues"],
        )

    def test_goal_explain_next_decision_priority_after_conflict(self) -> None:
        m = manifest()
        rep = build_goal_reconciliation_report(m)
        rep_att = copy.deepcopy(rep)
        rep_att["nodes"][0]["axes"]["attention_recorded"] = {
            "status": "observed",
            "reason_code": "cbr_parent_attention_recorded",
        }
        rep_att["nodes"][0]["recommendations"] = [
            "attention_required",
            "blocked_conflict",
        ]
        rep_att["report_digest"] = stable_digest(
            {k: val for k, val in rep_att.items() if k != "report_digest"}
        )
        v_att = build_goal_explain_view(m, rep_att)
        self.assertEqual("collect_parent_attention", v_att["next_decision"]["kind"])
        self.assertEqual("codex_coordinator", v_att["next_decision"]["owner"])

        rep_disp = copy.deepcopy(rep)
        rep_disp["nodes"][0]["axes"]["apply"] = {
            "status": "observed",
            "reason_code": "cbr_applied",
        }
        rep_disp["nodes"][0]["recommendations"] = [
            "source_disposition_required",
            "blocked_conflict",
        ]
        rep_disp["report_digest"] = stable_digest(
            {k: val for k, val in rep_disp.items() if k != "report_digest"}
        )
        v_disp = build_goal_explain_view(m, rep_disp)
        self.assertEqual("record_source_disposition", v_disp["next_decision"]["kind"])
        self.assertEqual("source_owner", v_disp["next_decision"]["owner"])

        forged_ready = copy.deepcopy(rep)
        forged_ready["nodes"][0]["recommendations"] = ["dependency_ready"]
        forged_ready["report_digest"] = stable_digest(
            {k: val for k, val in forged_ready.items() if k != "report_digest"}
        )
        with self.assertRaisesRegex(
            GoalExplainError, "recommendation semantics mismatch"
        ):
            build_goal_explain_view(m, forged_ready)

        rep_ready = copy.deepcopy(rep)
        rep_ready["nodes"][0]["axes"]["source_disposition"] = {
            "status": "observed",
            "reason_code": "source_disposition_recorded",
        }
        rep_ready["nodes"][0]["recommendations"] = [
            "dependency_ready",
            "blocked_conflict",
        ]
        rep_ready["report_digest"] = stable_digest(
            {k: val for k, val in rep_ready.items() if k != "report_digest"}
        )
        v_ready = build_goal_explain_view(m, rep_ready)
        self.assertEqual(
            "consider_dependency_advance", v_ready["next_decision"]["kind"]
        )

    def test_goal_explain_attention_axes_remain_distinct(self) -> None:
        m = manifest()
        rep = build_goal_reconciliation_report(m)
        self.assertEqual(
            "unknown", build_goal_explain_view(m, rep)["attention"]["status"]
        )

        pending = copy.deepcopy(rep)
        for axis in (
            "attention_recorded",
            "attention_delivered",
            "attention_acknowledged",
        ):
            pending["nodes"][0]["axes"][axis] = {
                "status": "observed",
                "reason_code": "trusted_" + axis,
            }
        pending["nodes"][0]["axes"]["parent_collection"] = {
            "status": "not_observed",
            "reason_code": "collection_absent",
        }
        pending["nodes"][0]["recommendations"] = [
            "attention_required",
            "blocked_conflict",
        ]
        pending["report_digest"] = stable_digest(
            {key: value for key, value in pending.items() if key != "report_digest"}
        )
        pending_view = build_goal_explain_view(m, pending)
        self.assertEqual("observed", pending_view["attention"]["status"])
        self.assertEqual(
            "collect_parent_attention", pending_view["next_decision"]["kind"]
        )
        self.assertEqual(
            "not_observed",
            pending_view["nodes"][0]["axes"]["parent_collection"]["status"],
        )

        not_observed = copy.deepcopy(rep)
        for axis in ("attention_recorded", "parent_collection"):
            not_observed["nodes"][0]["axes"][axis] = {
                "status": "not_observed",
                "reason_code": axis + "_absent",
            }
        not_observed["report_digest"] = stable_digest(
            {
                key: value
                for key, value in not_observed.items()
                if key != "report_digest"
            }
        )
        self.assertEqual(
            "not_observed",
            build_goal_explain_view(m, not_observed)["attention"]["status"],
        )

        not_applicable = copy.deepcopy(rep)
        for axis in ("attention_recorded", "parent_collection"):
            not_applicable["nodes"][0]["axes"][axis] = {
                "status": "not_applicable",
                "reason_code": axis + "_not_applicable",
            }
        not_applicable["report_digest"] = stable_digest(
            {
                key: value
                for key, value in not_applicable.items()
                if key != "report_digest"
            }
        )
        self.assertEqual(
            "not_applicable",
            build_goal_explain_view(m, not_applicable)["attention"]["status"],
        )

    def test_goal_explain_binding_digest_and_node_mismatch_fail_closed(self) -> None:
        m = manifest()
        rep = build_goal_reconciliation_report(m)
        m_diff = copy.deepcopy(m)
        m_diff["goal_id"] = "goal-public-2"
        m_diff["manifest_digest"] = stable_digest(
            {k: val for k, val in m_diff.items() if k != "manifest_digest"}
        )
        with self.assertRaisesRegex(GoalExplainError, "binding mismatch"):
            build_goal_explain_view(m_diff, rep)

        revision_mismatch = copy.deepcopy(rep)
        revision_mismatch["revision"] = 2
        revision_mismatch["report_digest"] = stable_digest(
            {
                key: value
                for key, value in revision_mismatch.items()
                if key != "report_digest"
            }
        )
        with self.assertRaisesRegex(GoalExplainError, "binding mismatch"):
            build_goal_explain_view(m, revision_mismatch)

        invalid_digest = copy.deepcopy(rep)
        invalid_digest["report_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(GoalReconciliationError, "report digest"):
            build_goal_explain_view(m, invalid_digest)

        node_semantics = copy.deepcopy(rep)
        node_semantics["nodes"][0]["dependencies"] = ["other-node"]
        node_semantics["report_digest"] = stable_digest(
            {
                key: value
                for key, value in node_semantics.items()
                if key != "report_digest"
            }
        )
        with self.assertRaisesRegex(GoalExplainError, "node semantics mismatch"):
            build_goal_explain_view(m, node_semantics)

    def test_goal_explain_terminal_and_non_claims_remain_advisory(self) -> None:
        view = build_goal_explain_view(
            manifest(), build_goal_reconciliation_report(manifest())
        )
        self.assertEqual(
            {
                "report_candidate": False,
                "operator_status": "not_candidate",
                "source_completion_required": True,
                "required_node_ids": ["node-a"],
                "root_acceptance_references": ["acceptance-v1"],
                "reason_codes": ["reconciliation_report_not_terminal_candidate"],
            },
            view["terminal"],
        )
        self.assertFalse(view["authority_claims"]["root_completion"])
        self.assertFalse(view["authority_claims"]["goal_completion"])
        self.assertFalse(view["authority_claims"]["routing"])
        self.assertFalse(view["authority_claims"]["selection"])

        forged_terminal = copy.deepcopy(view)
        forged_terminal["terminal"]["report_candidate"] = True
        with self.assertRaisesRegex(GoalExplainError, "terminal boundary"):
            validate_goal_explain_view(forged_terminal)

    def test_cli_goal_explain_is_canonical_read_only_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m_path = root / "manifest.json"
            r_path = root / "report.json"
            m_val = manifest()
            r_val = build_goal_reconciliation_report(m_val)
            m_path.write_text(json.dumps(m_val), encoding="utf-8")
            r_path.write_text(json.dumps(r_val), encoding="utf-8")
            before = sorted(str(item.relative_to(root)) for item in root.rglob("*"))

            argv = [
                "orchestration",
                "goal-explain",
                "--goal-manifest",
                str(m_path),
                "--reconciliation-report",
                str(r_path),
                "--json",
            ]
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("codex_batch_runner.cli.Config.load") as config_load:
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = main(argv)
                config_load.assert_not_called()
            self.assertEqual(0, code)
            res = json.loads(stdout.getvalue())
            self.assertEqual("goal-explain-view-v1", res["contract"])
            self.assertEqual("goal-public-1", res["goal"]["goal_id"])
            first_bytes = stdout.getvalue()

            m_path.touch()
            r_path.touch()
            repeated = io.StringIO()
            with contextlib.redirect_stdout(repeated):
                self.assertEqual(0, main(argv))
            self.assertEqual(first_bytes, repeated.getvalue())
            self.assertEqual(
                before, sorted(str(item.relative_to(root)) for item in root.rglob("*"))
            )

            stdout_h = io.StringIO()
            with contextlib.redirect_stdout(stdout_h):
                code_h = main(
                    [
                        "orchestration",
                        "goal-explain",
                        "--goal-manifest",
                        str(m_path),
                        "--reconciliation-report",
                        str(r_path),
                    ]
                )
            self.assertEqual(0, code_h)
            human = stdout_h.getvalue()
            for section in (
                "Goal\n",
                "Binding\n",
                "Attention\n",
                "Nodes\n",
                "Next decision\n",
                "Terminal / non-claims\n",
            ):
                self.assertIn(section, human)

            m_val["root_outcome"]["summary"] = "/private/operator/value"
            m_val["manifest_digest"] = stable_digest(
                {key: value for key, value in m_val.items() if key != "manifest_digest"}
            )
            m_path.write_text(json.dumps(m_val), encoding="utf-8")
            stdout_e = io.StringIO()
            with contextlib.redirect_stdout(stdout_e):
                code_e = main(argv)
            self.assertEqual(2, code_e)
            error_output = stdout_e.getvalue()
            err_json = json.loads(error_output)
            self.assertEqual("goal-explain-error-v1", err_json["contract"])
            self.assertEqual("invalid", err_json["decision_status"])
            self.assertNotIn(str(m_path), error_output)
            self.assertNotIn("/private/operator/value", error_output)
            self.assertNotIn("goal-public-1", error_output)

    def test_goal_explain_renderer_validates_input(self) -> None:
        view = build_goal_explain_view(
            manifest(), build_goal_reconciliation_report(manifest())
        )
        self.assertIn("Terminal / non-claims", render_goal_explain_view(view))
        view["mutation"]["allowed"] = True
        with self.assertRaisesRegex(GoalExplainError, "authority boundary"):
            render_goal_explain_view(view)

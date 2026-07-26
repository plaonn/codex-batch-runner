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
    GoalReconciliationError,
    build_goal_reconciliation_report,
    load_goal_manifest,
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

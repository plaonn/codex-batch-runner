from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.events import list_events


def write_config(tmp: str, extra: dict) -> Path:
    root = Path(tmp)
    config_path = root / "config.json"
    data = {
        "queue_dir": str(root / "tasks"),
        "log_dir": str(root / "logs"),
        "event_dir": str(root / "events"),
        "lock_file": str(root / "runner.lock"),
        "state_file": str(root / "state.json"),
        "worktree_root": str(root / "worktrees"),
    }
    data.update(extra)
    config_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    return config_path


def run_cli(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, stdout.getvalue()


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_stale_proposal_report(root: Path) -> Path:
    proposal_path = root / "proposal.json"
    proposal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "policy_proposal_report",
                "proposal_class": "execution_target_freshness",
                "mode": "read_only",
                "generated_at": "2026-07-03T00:00:00+00:00",
                "mutation": {
                    "allowed": False,
                    "applied": False,
                    "prohibited_state_changes": [
                        "apply",
                        "config_rewrite",
                        "task_mutation",
                        "model_replacement",
                        "rule_replacement",
                    ],
                },
                "summary": {
                    "targets_checked": 1,
                    "fresh": 0,
                    "stale": 1,
                    "missing": 0,
                    "proposal_count": 1,
                },
                "items": [],
                "proposals": [
                    {
                        "proposal_id": "execution_target_freshness:balanced_current",
                        "proposal_class": "execution_target_freshness",
                        "target_alias": "balanced_current",
                        "status": "open",
                        "severity": "warning",
                        "reason": "review_after_days_elapsed",
                        "recommended_action": "review_execution_target_freshness",
                        "allowed_state_changes": ["none"],
                        "prohibited_state_changes": [
                            "apply",
                            "config_rewrite",
                            "task_mutation",
                            "model_replacement",
                            "rule_replacement",
                        ],
                        "selection_refs": [{"scope": "default_execution_config", "name": None}],
                    }
                ],
                "warnings": [],
                "errors": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return proposal_path


def write_stale_policy_preview(root: Path) -> Path:
    preview_path = root / "preview.json"
    preview_path.write_text(json.dumps(stale_policy_preview(), sort_keys=True), encoding="utf-8")
    return preview_path


def write_approved_stale_policy_approval(root: Path, preview: dict | None = None) -> Path:
    approval_path = root / "approval.json"
    approval_path.write_text(
        json.dumps(approved_stale_policy_approval(preview or stale_policy_preview()), sort_keys=True),
        encoding="utf-8",
    )
    return approval_path


def stale_policy_preview() -> dict:
    return {
        "schema_version": 1,
        "kind": "policy_proposal_preview",
        "source_schema_version": 1,
        "source_kind": "policy_proposal_report",
        "proposal_class": "execution_target_freshness",
        "mode": "read_only",
        "mutation": {
            "allowed": False,
            "applied": False,
            "prohibited_state_changes": [
                "apply",
                "config_rewrite",
                "task_mutation",
                "model_replacement",
                "rule_replacement",
            ],
        },
        "summary": {
            "proposal_count": 1,
            "apply_ready": 0,
            "blocked": 1,
            "would_change": "none",
        },
        "items": [
            {
                "proposal_id": "execution_target_freshness:balanced_current",
                "proposal_class": "execution_target_freshness",
                "target_alias": "balanced_current",
                "status": "open",
                "severity": "warning",
                "reason": "review_after_days_elapsed",
                "recommended_action": "review_execution_target_freshness",
                "target": "execution_targets.balanced_current.freshness",
                "would_change": "none",
                "apply_ready": False,
                "blocked_reason": "preview_only_no_apply_target",
                "selection_refs": [{"scope": "default_execution_config", "name": None}],
            }
        ],
        "warnings": [],
        "errors": [],
    }


def approved_stale_policy_approval(preview: dict) -> dict:
    item = preview["items"][0]
    return {
        "schema_version": 1,
        "kind": "policy_proposal_approval_template",
        "source_schema_version": 1,
        "source_kind": "policy_proposal_preview",
        "source_preview_sha256": canonical_json_sha256(preview),
        "proposal_class": "execution_target_freshness",
        "mode": "read_only",
        "created_at": "2026-07-03T00:00:00+00:00",
        "mutation": {
            "allowed": False,
            "applied": False,
            "prohibited_state_changes": [
                "apply",
                "config_rewrite",
                "task_mutation",
                "model_replacement",
                "rule_replacement",
            ],
        },
        "summary": {
            "proposal_count": 1,
            "approved_count": 0,
            "pending_count": 1,
        },
        "approvals": [
            {
                "proposal_id": item["proposal_id"],
                "proposal_class": item["proposal_class"],
                "target_alias": item["target_alias"],
                "target": item["target"],
                "recommended_action": item["recommended_action"],
                "source_item_sha256": canonical_json_sha256(item),
                "approved": True,
                "reviewer": "operator",
                "reviewed_at": "2026-07-03T00:00:00+00:00",
                "decision_note": "freshness reviewed",
            }
        ],
        "warnings": [],
        "errors": [],
    }


class PolicyProposalTests(unittest.TestCase):
    def test_execution_target_freshness_proposal_shape_for_stale_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {
                        "low_cost_current": {
                            "model": "gpt-5-small",
                            "freshness": {
                                "owner": "operator",
                                "last_reviewed_at": "2026-06-19",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "model_selection_rules": [
                        {
                            "name": "low-cost-docs",
                            "when": {"reasoning_depth": "low"},
                            "execution_target": "low_cost_current",
                        }
                    ],
                },
            )

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with (
                mock.patch("codex_batch_runner.doctor.utc_now", return_value=now),
                mock.patch("codex_batch_runner.policy_proposals.utc_now", return_value=now),
            ):
                code, output = run_cli(
                    ["--config", str(config_path), "policy-proposals", "execution-target-freshness", "--json"]
                )
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(
                {
                    "schema_version": 1,
                    "kind": "policy_proposal_report",
                    "proposal_class": "execution_target_freshness",
                    "mode": "read_only",
                    "generated_at": "2026-07-03T00:00:00+00:00",
                    "mutation": {
                        "allowed": False,
                        "applied": False,
                        "prohibited_state_changes": [
                            "apply",
                            "config_rewrite",
                            "task_mutation",
                            "model_replacement",
                            "rule_replacement",
                        ],
                    },
                    "summary": {
                        "targets_checked": 1,
                        "fresh": 0,
                        "stale": 1,
                        "missing": 0,
                        "proposal_count": 1,
                    },
                    "items": [
                        {
                            "target_alias": "low_cost_current",
                            "selection_refs": [{"scope": "model_selection_rule", "name": "low-cost-docs"}],
                            "freshness_status": "stale",
                            "freshness_reason": "review_after_days_elapsed",
                            "last_reviewed_at": "2026-06-19",
                            "review_after_days": 14,
                            "review_due_at": "2026-07-03",
                            "checked_at": "2026-07-03",
                            "proposal_id": "execution_target_freshness:low_cost_current",
                        }
                    ],
                    "proposals": [
                        {
                            "proposal_id": "execution_target_freshness:low_cost_current",
                            "proposal_class": "execution_target_freshness",
                            "target_alias": "low_cost_current",
                            "status": "open",
                            "severity": "warning",
                            "reason": "review_after_days_elapsed",
                            "recommended_action": "review_execution_target_freshness",
                            "allowed_state_changes": ["none"],
                            "prohibited_state_changes": [
                                "apply",
                                "config_rewrite",
                                "task_mutation",
                                "model_replacement",
                                "rule_replacement",
                            ],
                            "selection_refs": [{"scope": "model_selection_rule", "name": "low-cost-docs"}],
                        }
                    ],
                    "warnings": [],
                    "errors": [],
                },
                report,
            )
            self.assertNotIn("gpt-5-small", output)

    def test_execution_target_freshness_proposal_reports_fresh_without_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {
                        "balanced_current": {
                            "model": "gpt-5",
                            "freshness": {
                                "last_reviewed_at": "2026-07-03",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "default_execution_config": {"execution_target": "balanced_current"},
                },
            )

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with (
                mock.patch("codex_batch_runner.doctor.utc_now", return_value=now),
                mock.patch("codex_batch_runner.policy_proposals.utc_now", return_value=now),
            ):
                code, output = run_cli(
                    ["--config", str(config_path), "policy-proposals", "execution-target-freshness", "--json"]
                )
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(
                {
                    "targets_checked": 1,
                    "fresh": 1,
                    "stale": 0,
                    "missing": 0,
                    "proposal_count": 0,
                },
                report["summary"],
            )
            self.assertEqual([], report["proposals"])
            self.assertEqual("fresh", report["items"][0]["freshness_status"])
            self.assertIsNone(report["items"][0]["proposal_id"])

    def test_execution_target_freshness_proposal_reports_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {
                        "balanced_current": {
                            "model": "gpt-5",
                        }
                    },
                    "default_execution_config": {"execution_target": "balanced_current"},
                },
            )

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with (
                mock.patch("codex_batch_runner.doctor.utc_now", return_value=now),
                mock.patch("codex_batch_runner.policy_proposals.utc_now", return_value=now),
            ):
                code, output = run_cli(
                    ["--config", str(config_path), "policy-proposals", "execution-target-freshness", "--json"]
                )
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("missing", report["items"][0]["freshness_status"])
            self.assertEqual("target_freshness_not_configured", report["items"][0]["freshness_reason"])
            self.assertEqual("add_execution_target_freshness_metadata", report["proposals"][0]["recommended_action"])
            self.assertEqual(["none"], report["proposals"][0]["allowed_state_changes"])

    def test_execution_target_freshness_proposal_does_not_mutate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {
                        "balanced_current": {
                            "model": "gpt-5",
                        }
                    },
                    "default_execution_config": {"execution_target": "balanced_current"},
                },
            )
            tasks = root / "tasks"
            tasks.mkdir()
            task_path = tasks / "task-1.json"
            task_path.write_text(json.dumps({"id": "task-1", "status": "runnable"}, sort_keys=True), encoding="utf-8")
            state_path = root / "state.json"
            state_path.write_text(json.dumps({"runner_pause": {"active": False}}, sort_keys=True), encoding="utf-8")
            before_files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            before_contents = {name: (root / name).read_text(encoding="utf-8") for name in before_files}

            code, _ = run_cli(["--config", str(config_path), "policy-proposals", "execution-target-freshness", "--json"])

            after_files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            after_contents = {name: (root / name).read_text(encoding="utf-8") for name in after_files}
            self.assertEqual(0, code)
            self.assertEqual(before_files, after_files)
            self.assertEqual(before_contents, after_contents)
            self.assertFalse((root / "logs").exists())
            self.assertFalse((root / "events").exists())
            self.assertFalse((root / "runner.lock").exists())

    def test_policy_proposal_preview_shape_for_stale_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            proposal_path = write_stale_proposal_report(root)

            code, output = run_cli(
                ["--config", str(config_path), "policy-proposals", "preview", str(proposal_path), "--json"]
            )
            preview = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(
                {
                    "schema_version": 1,
                    "kind": "policy_proposal_preview",
                    "source_schema_version": 1,
                    "source_kind": "policy_proposal_report",
                    "proposal_class": "execution_target_freshness",
                    "mode": "read_only",
                    "mutation": {
                        "allowed": False,
                        "applied": False,
                        "prohibited_state_changes": [
                            "apply",
                            "config_rewrite",
                            "task_mutation",
                            "model_replacement",
                            "rule_replacement",
                        ],
                    },
                    "summary": {
                        "proposal_count": 1,
                        "apply_ready": 0,
                        "blocked": 1,
                        "would_change": "none",
                    },
                    "items": [
                        {
                            "proposal_id": "execution_target_freshness:balanced_current",
                            "proposal_class": "execution_target_freshness",
                            "target_alias": "balanced_current",
                            "status": "open",
                            "severity": "warning",
                            "reason": "review_after_days_elapsed",
                            "recommended_action": "review_execution_target_freshness",
                            "target": "execution_targets.balanced_current.freshness",
                            "would_change": "none",
                            "apply_ready": False,
                            "blocked_reason": "preview_only_no_apply_target",
                            "selection_refs": [{"scope": "default_execution_config", "name": None}],
                        }
                    ],
                    "warnings": [],
                    "errors": [],
                },
                preview,
            )

    def test_policy_proposal_preview_human_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            proposal_path = write_stale_proposal_report(root)

            code, output = run_cli(["--config", str(config_path), "policy-proposals", "preview", str(proposal_path)])

            self.assertEqual(0, code)
            self.assertIn("cbr policy-proposals preview", output)
            self.assertIn("proposal_count: 1", output)
            self.assertIn("target: execution_targets.balanced_current.freshness", output)
            self.assertIn("would_change: none", output)
            self.assertIn("apply_ready: false", output)

    def test_policy_proposal_preview_rejects_unsupported_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            proposal_path = root / "proposal.json"
            proposal_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "policy_proposal_report",
                        "proposal_class": "model_replacement",
                        "proposals": [],
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(
                ["--config", str(config_path), "policy-proposals", "preview", str(proposal_path), "--json"]
            )
            preview = json.loads(output)

            self.assertEqual(1, code)
            self.assertEqual(["unsupported proposal_class"], preview["errors"])
            self.assertEqual([], preview["items"])

    def test_policy_proposal_preview_does_not_mutate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            proposal_path = write_stale_proposal_report(root)
            tasks = root / "tasks"
            tasks.mkdir()
            task_path = tasks / "task-1.json"
            task_path.write_text(json.dumps({"id": "task-1", "status": "runnable"}, sort_keys=True), encoding="utf-8")
            state_path = root / "state.json"
            state_path.write_text(json.dumps({"runner_pause": {"active": False}}, sort_keys=True), encoding="utf-8")
            before_files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            before_contents = {name: (root / name).read_text(encoding="utf-8") for name in before_files}

            code, _ = run_cli(
                ["--config", str(config_path), "policy-proposals", "preview", str(proposal_path), "--json"]
            )

            after_files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            after_contents = {name: (root / name).read_text(encoding="utf-8") for name in after_files}
            self.assertEqual(0, code)
            self.assertEqual(before_files, after_files)
            self.assertEqual(before_contents, after_contents)
            self.assertFalse((root / "logs").exists())
            self.assertFalse((root / "events").exists())
            self.assertFalse((root / "runner.lock").exists())

    def test_policy_proposal_approval_template_shape_for_stale_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview = stale_policy_preview()
            preview_path = root / "preview.json"
            preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with mock.patch("codex_batch_runner.policy_proposals.utc_now", return_value=now):
                code, output = run_cli(
                    ["--config", str(config_path), "policy-proposals", "approval-template", str(preview_path), "--json"]
                )
            template = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(
                {
                    "schema_version": 1,
                    "kind": "policy_proposal_approval_template",
                    "source_schema_version": 1,
                    "source_kind": "policy_proposal_preview",
                    "source_preview_sha256": canonical_json_sha256(preview),
                    "proposal_class": "execution_target_freshness",
                    "mode": "read_only",
                    "created_at": "2026-07-03T00:00:00+00:00",
                    "mutation": {
                        "allowed": False,
                        "applied": False,
                        "prohibited_state_changes": [
                            "apply",
                            "config_rewrite",
                            "task_mutation",
                            "model_replacement",
                            "rule_replacement",
                        ],
                    },
                    "summary": {
                        "proposal_count": 1,
                        "approved_count": 0,
                        "pending_count": 1,
                    },
                    "approvals": [
                        {
                            "proposal_id": "execution_target_freshness:balanced_current",
                            "proposal_class": "execution_target_freshness",
                            "target_alias": "balanced_current",
                            "target": "execution_targets.balanced_current.freshness",
                            "recommended_action": "review_execution_target_freshness",
                            "source_item_sha256": canonical_json_sha256(preview["items"][0]),
                            "approved": False,
                            "reviewer": None,
                            "reviewed_at": None,
                            "decision_note": None,
                        }
                    ],
                    "warnings": [],
                    "errors": [],
                },
                template,
            )

    def test_policy_proposal_approval_template_human_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview_path = write_stale_policy_preview(root)

            code, output = run_cli(
                ["--config", str(config_path), "policy-proposals", "approval-template", str(preview_path)]
            )

            self.assertEqual(0, code)
            self.assertIn("cbr policy-proposals approval-template", output)
            self.assertIn("proposal_count: 1", output)
            self.assertIn("approved_count: 0", output)
            self.assertIn("target: execution_targets.balanced_current.freshness", output)
            self.assertIn("approved: false", output)

    def test_policy_proposal_approval_template_rejects_report_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            proposal_path = write_stale_proposal_report(root)

            code, output = run_cli(
                ["--config", str(config_path), "policy-proposals", "approval-template", str(proposal_path), "--json"]
            )
            template = json.loads(output)

            self.assertEqual(1, code)
            self.assertEqual(["unsupported policy proposal preview kind"], template["errors"])
            self.assertEqual([], template["approvals"])

    def test_policy_proposal_approval_template_does_not_mutate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview_path = write_stale_policy_preview(root)
            tasks = root / "tasks"
            tasks.mkdir()
            task_path = tasks / "task-1.json"
            task_path.write_text(json.dumps({"id": "task-1", "status": "runnable"}, sort_keys=True), encoding="utf-8")
            state_path = root / "state.json"
            state_path.write_text(json.dumps({"runner_pause": {"active": False}}, sort_keys=True), encoding="utf-8")
            before_files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            before_contents = {name: (root / name).read_text(encoding="utf-8") for name in before_files}

            code, _ = run_cli(
                ["--config", str(config_path), "policy-proposals", "approval-template", str(preview_path), "--json"]
            )

            after_files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            after_contents = {name: (root / name).read_text(encoding="utf-8") for name in after_files}
            self.assertEqual(0, code)
            self.assertEqual(before_files, after_files)
            self.assertEqual(before_contents, after_contents)
            self.assertFalse((root / "logs").exists())
            self.assertFalse((root / "events").exists())
            self.assertFalse((root / "runner.lock").exists())

    def test_policy_proposal_validate_approval_shape_for_approved_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview = stale_policy_preview()
            preview_path = root / "preview.json"
            preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
            approval_path = root / "approval.json"
            approval_path.write_text(
                json.dumps(approved_stale_policy_approval(preview), sort_keys=True),
                encoding="utf-8",
            )

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "validate-approval",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                    "--json",
                ]
            )
            validation = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(
                {
                    "schema_version": 1,
                    "kind": "policy_proposal_approval_validation",
                    "approval_schema_version": 1,
                    "approval_kind": "policy_proposal_approval_template",
                    "preview_schema_version": 1,
                    "preview_kind": "policy_proposal_preview",
                    "proposal_class": "execution_target_freshness",
                    "mode": "read_only",
                    "valid": True,
                    "mutation": {
                        "allowed": False,
                        "applied": False,
                        "prohibited_state_changes": [
                            "apply",
                            "config_rewrite",
                            "task_mutation",
                            "model_replacement",
                            "rule_replacement",
                        ],
                    },
                    "summary": {
                        "approval_count": 1,
                        "approved_count": 1,
                        "pending_count": 0,
                        "valid_approved_count": 1,
                        "invalid_count": 0,
                    },
                    "items": [
                        {
                            "proposal_id": "execution_target_freshness:balanced_current",
                            "proposal_class": "execution_target_freshness",
                            "target_alias": "balanced_current",
                            "target": "execution_targets.balanced_current.freshness",
                            "recommended_action": "review_execution_target_freshness",
                            "approved": True,
                            "validation_status": "approved",
                            "preview_item_found": True,
                            "source_item_sha256_matches": True,
                            "reviewer_present": True,
                            "reviewed_at_valid": True,
                            "decision_note_present": True,
                            "errors": [],
                        }
                    ],
                    "warnings": [],
                    "errors": [],
                },
                validation,
            )

    def test_policy_proposal_validate_approval_human_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview = stale_policy_preview()
            preview_path = root / "preview.json"
            preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
            approval_path = write_approved_stale_policy_approval(root, preview)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "validate-approval",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                ]
            )

            self.assertEqual(0, code)
            self.assertIn("cbr policy-proposals validate-approval", output)
            self.assertIn("valid: true", output)
            self.assertIn("approved_count: 1", output)
            self.assertIn("source_item_sha256_matches: true", output)

    def test_policy_proposal_validate_approval_rejects_preview_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview = stale_policy_preview()
            approval = approved_stale_policy_approval(preview)
            approval["source_preview_sha256"] = "0" * 64
            preview_path = root / "preview.json"
            approval_path = root / "approval.json"
            preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
            approval_path.write_text(json.dumps(approval, sort_keys=True), encoding="utf-8")

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "validate-approval",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                    "--json",
                ]
            )
            validation = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(validation["valid"])
            self.assertEqual(["source_preview_sha256 mismatch"], validation["errors"])
            self.assertEqual([], validation["items"])

    def test_policy_proposal_validate_approval_rejects_missing_approved_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview = stale_policy_preview()
            approval = approved_stale_policy_approval(preview)
            approval["approvals"][0]["reviewer"] = ""
            approval["approvals"][0]["reviewed_at"] = "not-a-date"
            approval["approvals"][0]["decision_note"] = ""
            preview_path = root / "preview.json"
            approval_path = root / "approval.json"
            preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
            approval_path.write_text(json.dumps(approval, sort_keys=True), encoding="utf-8")

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "validate-approval",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                    "--json",
                ]
            )
            validation = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(validation["valid"])
            self.assertEqual(1, validation["summary"]["invalid_count"])
            self.assertEqual(
                [
                    "approved item requires reviewer",
                    "approved item requires reviewed_at ISO datetime",
                    "approved item requires decision_note",
                ],
                validation["items"][0]["errors"],
            )

    def test_policy_proposal_validate_approval_rejects_target_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview = stale_policy_preview()
            approval = approved_stale_policy_approval(preview)
            approval["approvals"][0]["target"] = "execution_targets.other.freshness"
            preview_path = root / "preview.json"
            approval_path = root / "approval.json"
            preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
            approval_path.write_text(json.dumps(approval, sort_keys=True), encoding="utf-8")

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "validate-approval",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                    "--json",
                ]
            )
            validation = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(validation["valid"])
            self.assertEqual(["target does not match preview"], validation["items"][0]["errors"])

    def test_policy_proposal_validate_approval_does_not_mutate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview = stale_policy_preview()
            preview_path = root / "preview.json"
            preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
            approval_path = write_approved_stale_policy_approval(root, preview)
            tasks = root / "tasks"
            tasks.mkdir()
            task_path = tasks / "task-1.json"
            task_path.write_text(json.dumps({"id": "task-1", "status": "runnable"}, sort_keys=True), encoding="utf-8")
            state_path = root / "state.json"
            state_path.write_text(json.dumps({"runner_pause": {"active": False}}, sort_keys=True), encoding="utf-8")
            before_files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            before_contents = {name: (root / name).read_text(encoding="utf-8") for name in before_files}

            code, _ = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "validate-approval",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                    "--json",
                ]
            )

            after_files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            after_contents = {name: (root / name).read_text(encoding="utf-8") for name in after_files}
            self.assertEqual(0, code)
            self.assertEqual(before_files, after_files)
            self.assertEqual(before_contents, after_contents)
            self.assertFalse((root / "logs").exists())
            self.assertFalse((root / "events").exists())
            self.assertFalse((root / "runner.lock").exists())

    def test_policy_proposal_apply_dry_run_reports_before_after_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {
                        "balanced_current": {
                            "model": "gpt-5",
                            "freshness": {
                                "owner": "previous",
                                "last_reviewed_at": "2026-06-19",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "default_execution_config": {"execution_target": "balanced_current"},
                },
            )
            preview = stale_policy_preview()
            preview_path = write_stale_policy_preview(root)
            approval_path = write_approved_stale_policy_approval(root, preview)
            before = config_path.read_text(encoding="utf-8")

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with mock.patch("codex_batch_runner.doctor.utc_now", return_value=now):
                code, output = run_cli(
                    [
                        "--config",
                        str(config_path),
                        "policy-proposals",
                        "apply",
                        str(approval_path),
                        "--preview",
                        str(preview_path),
                        "--config-target",
                        str(config_path),
                        "--dry-run",
                        "--json",
                    ]
                )
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["valid"])
            self.assertEqual("dry_run", report["mode"])
            self.assertFalse(report["mutation"]["applied"])
            self.assertEqual(1, report["summary"]["eligible_count"])
            self.assertEqual(
                {"owner": "previous", "last_reviewed_at": "2026-06-19", "review_after_days": 14},
                report["items"][0]["before"]["freshness"],
            )
            self.assertEqual(
                {"owner": "operator", "last_reviewed_at": "2026-07-03", "review_after_days": 14},
                report["items"][0]["after"]["freshness"],
            )
            self.assertEqual(before, config_path.read_text(encoding="utf-8"))
            self.assertFalse((root / "events").exists())

    def test_policy_proposal_apply_requires_approve_for_apply_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {
                        "balanced_current": {
                            "model": "gpt-5",
                            "freshness": {
                                "owner": "previous",
                                "last_reviewed_at": "2026-06-19",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "default_execution_config": {"execution_target": "balanced_current"},
                },
            )
            preview_path = write_stale_policy_preview(root)
            approval_path = write_approved_stale_policy_approval(root, stale_policy_preview())
            before = config_path.read_text(encoding="utf-8")

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with mock.patch("codex_batch_runner.doctor.utc_now", return_value=now):
                code, output = run_cli(
                    [
                        "--config",
                        str(config_path),
                        "policy-proposals",
                        "apply",
                        str(approval_path),
                        "--preview",
                        str(preview_path),
                        "--config-target",
                        str(config_path),
                        "--apply",
                        "--json",
                    ]
                )
            report = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(report["valid"])
            self.assertIn("--apply requires --approve", report["errors"])
            self.assertEqual(before, config_path.read_text(encoding="utf-8"))

    def test_policy_proposal_apply_updates_only_execution_target_freshness_and_emits_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {
                        "balanced_current": {
                            "model": "gpt-5",
                            "codex_profile": "batch-normal",
                            "freshness": {
                                "owner": "previous",
                                "last_reviewed_at": "2026-06-19",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "default_execution_config": {"execution_target": "balanced_current"},
                    "model_selection_rules": [
                        {
                            "name": "balanced",
                            "when": {"reasoning_depth": "medium"},
                            "execution_target": "balanced_current",
                        }
                    ],
                },
            )
            preview_path = write_stale_policy_preview(root)
            approval_path = write_approved_stale_policy_approval(root, stale_policy_preview())

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with mock.patch("codex_batch_runner.doctor.utc_now", return_value=now):
                code, output = run_cli(
                    [
                        "--config",
                        str(config_path),
                        "policy-proposals",
                        "apply",
                        str(approval_path),
                        "--preview",
                        str(preview_path),
                        "--config-target",
                        str(config_path),
                        "--apply",
                        "--approve",
                        "--json",
                    ]
                )
            report = json.loads(output)
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
            events = list_events(Config.load(str(config_path)), limit=10)

            self.assertEqual(0, code)
            self.assertTrue(report["mutation"]["applied"])
            self.assertEqual(1, report["summary"]["applied_count"])
            self.assertEqual(
                {"owner": "operator", "last_reviewed_at": "2026-07-03", "review_after_days": 14},
                config_data["execution_targets"]["balanced_current"]["freshness"],
            )
            self.assertEqual("gpt-5", config_data["execution_targets"]["balanced_current"]["model"])
            self.assertEqual("batch-normal", config_data["execution_targets"]["balanced_current"]["codex_profile"])
            self.assertEqual("balanced_current", config_data["model_selection_rules"][0]["execution_target"])
            self.assertEqual("policy_proposal_applied", events[0]["event_type"])
            event_payload = events[0]["payload"]
            self.assertEqual("execution_target_freshness", event_payload["proposal_class"])
            self.assertNotIn(str(config_path), json.dumps(event_payload, sort_keys=True))
            self.assertNotIn("freshness reviewed", json.dumps(event_payload, sort_keys=True))

    def test_policy_proposal_apply_adds_missing_freshness_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {"balanced_current": {"model": "gpt-5"}},
                    "default_execution_config": {"execution_target": "balanced_current"},
                },
            )
            preview = stale_policy_preview()
            preview["items"][0]["reason"] = "target_freshness_not_configured"
            preview["items"][0]["recommended_action"] = "add_execution_target_freshness_metadata"
            preview_path = root / "preview.json"
            preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
            approval_path = write_approved_stale_policy_approval(root, preview)

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with mock.patch("codex_batch_runner.doctor.utc_now", return_value=now):
                code, output = run_cli(
                    [
                        "--config",
                        str(config_path),
                        "policy-proposals",
                        "apply",
                        str(approval_path),
                        "--preview",
                        str(preview_path),
                        "--config-target",
                        str(config_path),
                        "--apply",
                        "--approve",
                        "--json",
                    ]
                )
            report = json.loads(output)
            config_data = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(0, code)
            self.assertTrue(report["valid"])
            self.assertEqual(
                {"owner": "operator", "last_reviewed_at": "2026-07-03", "review_after_days": 14},
                config_data["execution_targets"]["balanced_current"]["freshness"],
            )

    def test_policy_proposal_apply_rejects_dirty_config_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {
                        "balanced_current": {
                            "model": "gpt-5",
                            "freshness": {
                                "owner": "operator",
                                "last_reviewed_at": "2026-07-03",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "default_execution_config": {"execution_target": "balanced_current"},
                },
            )
            preview_path = write_stale_policy_preview(root)
            approval_path = write_approved_stale_policy_approval(root, stale_policy_preview())
            before = config_path.read_text(encoding="utf-8")

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with mock.patch("codex_batch_runner.doctor.utc_now", return_value=now):
                code, output = run_cli(
                    [
                        "--config",
                        str(config_path),
                        "policy-proposals",
                        "apply",
                        str(approval_path),
                        "--preview",
                        str(preview_path),
                        "--config-target",
                        str(config_path),
                        "--dry-run",
                        "--json",
                    ]
                )
            report = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(report["valid"])
            self.assertIn("config target is dirty: expected freshness status stale, found fresh", report["items"][0]["errors"])
            self.assertEqual(before, config_path.read_text(encoding="utf-8"))

    def test_policy_proposal_apply_rejects_unsupported_repo_public_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview_path = write_stale_policy_preview(root)
            approval_path = write_approved_stale_policy_approval(root, stale_policy_preview())

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "apply",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                    "--config-target",
                    str(Path.cwd() / "docs" / "cli-reference.md"),
                    "--dry-run",
                    "--json",
                ]
            )
            report = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(report["valid"])
            self.assertIn("unsupported config target path: expected a JSON file", report["errors"])
            self.assertIn("unsupported config target path: repo public files are not mutable config targets", report["errors"])

            public_json_path = Path.cwd() / "docs" / "public-config.json"
            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "apply",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                    "--config-target",
                    str(public_json_path),
                    "--dry-run",
                    "--json",
                ]
            )
            report = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(report["valid"])
            self.assertEqual("repo_public_json", report["config_target"]["classification"])
            self.assertIn("unsupported config target path: repo public files are not mutable config targets", report["errors"])

            runtime_json_path = Path.cwd() / ".codex-batch-runner" / "config.json"
            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "apply",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                    "--config-target",
                    str(runtime_json_path),
                    "--dry-run",
                    "--json",
                ]
            )
            report = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(report["valid"])
            self.assertEqual("repo_runtime_json", report["config_target"]["classification"])
            self.assertIn("unsupported config target path: repo runtime state is not a mutable config target", report["errors"])

    def test_policy_proposal_apply_revalidates_source_preview_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {
                        "balanced_current": {
                            "model": "gpt-5",
                            "freshness": {
                                "owner": "previous",
                                "last_reviewed_at": "2026-06-19",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "default_execution_config": {"execution_target": "balanced_current"},
                },
            )
            preview = stale_policy_preview()
            approval = approved_stale_policy_approval(preview)
            approval["source_preview_sha256"] = "0" * 64
            preview_path = write_stale_policy_preview(root)
            approval_path = root / "approval.json"
            approval_path.write_text(json.dumps(approval, sort_keys=True), encoding="utf-8")

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with mock.patch("codex_batch_runner.doctor.utc_now", return_value=now):
                code, output = run_cli(
                    [
                        "--config",
                        str(config_path),
                        "policy-proposals",
                        "apply",
                        str(approval_path),
                        "--preview",
                        str(preview_path),
                        "--config-target",
                        str(config_path),
                        "--dry-run",
                        "--json",
                    ]
                )
            report = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(report["valid"])
            self.assertEqual(["approval validation failed"], report["errors"])

    def test_policy_proposal_apply_rejects_nonexistent_and_unparseable_config_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(tmp, {})
            preview_path = write_stale_policy_preview(root)
            approval_path = write_approved_stale_policy_approval(root, stale_policy_preview())

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "apply",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                    "--config-target",
                    str(root / "missing-config.json"),
                    "--dry-run",
                    "--json",
                ]
            )
            report = json.loads(output)

            self.assertEqual(1, code)
            self.assertEqual(["config target does not exist"], report["errors"])

            bad_config_path = root / "bad-config.json"
            bad_config_path.write_text("{", encoding="utf-8")
            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "policy-proposals",
                    "apply",
                    str(approval_path),
                    "--preview",
                    str(preview_path),
                    "--config-target",
                    str(bad_config_path),
                    "--dry-run",
                    "--json",
                ]
            )
            report = json.loads(output)

            self.assertEqual(1, code)
            self.assertTrue(report["errors"][0].startswith("failed to parse config target JSON"))

    def test_policy_proposal_apply_rejects_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                tmp,
                {
                    "execution_targets": {
                        "balanced_current": {
                            "model": "gpt-5",
                            "freshness": {
                                "owner": "previous",
                                "last_reviewed_at": "2026-06-19",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "default_execution_config": {"execution_target": "balanced_current"},
                },
            )
            preview = stale_policy_preview()
            preview["items"][0]["recommended_action"] = "replace_execution_target_model"
            preview_path = root / "preview.json"
            preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
            approval_path = write_approved_stale_policy_approval(root, preview)

            now = datetime(2026, 7, 3, tzinfo=timezone.utc)
            with mock.patch("codex_batch_runner.doctor.utc_now", return_value=now):
                code, output = run_cli(
                    [
                        "--config",
                        str(config_path),
                        "policy-proposals",
                        "apply",
                        str(approval_path),
                        "--preview",
                        str(preview_path),
                        "--config-target",
                        str(config_path),
                        "--dry-run",
                        "--json",
                    ]
                )
            report = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(report["valid"])
            self.assertEqual(["unsupported recommended_action"], report["items"][0]["errors"])

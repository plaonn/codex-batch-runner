from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from codex_batch_runner.cli import main


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

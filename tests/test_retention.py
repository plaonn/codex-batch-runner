from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.retention import (
    ACCEPTED_WORKTREE_UNAPPLIED,
    ACTIVE_TASK,
    CANONICAL_TASK_PROTECTED,
    CURSOR_UNCERTAINTY,
    INVALID_TASK_JSON,
    MISSING_ACTIVITY_TIMESTAMP,
    PROPOSAL_AGE_UNSPECIFIED,
    RECOVERY_REQUIRED,
    REVIEW_PENDING,
    RetentionInventoryValidationError,
    build_retention_inventory_report,
    validate_retention_inventory_report,
)

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)
OLD = "2026-01-01T00:00:00+00:00"


def write_config(root: Path) -> Path:
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "tasks"),
                "log_dir": str(root / "logs"),
                "event_dir": str(root / "events"),
                "lock_file": str(root / "runner.lock"),
                "state_file": str(root / "state.json"),
            }
        ),
        encoding="utf-8",
    )
    return config_path


def write_task(config: Config, task_id: str, **fields: object) -> Path:
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "id": task_id,
        "project_id": "example-project",
        "status": "completed",
        "review_status": "accepted",
        "created_at": OLD,
        "completed_at": OLD,
        "reviewed_at": OLD,
        **fields,
    }
    path = config.queue_dir / f"{task_id}.json"
    path.write_text(json.dumps(task), encoding="utf-8")
    return path


class RetentionInventoryTests(unittest.TestCase):
    def test_public_example_validates(self) -> None:
        example_path = (
            Path(__file__).parents[1]
            / "examples"
            / "retention-inventory-report-v1.example.json"
        )
        report = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertEqual(report, validate_retention_inventory_report(report))

    def test_missing_runtime_directories_are_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            report = build_retention_inventory_report(config, now=NOW)
            self.assertEqual(0, report["summary"]["task_count"])
            self.assertFalse(config.queue_dir.exists())
            self.assertFalse(config.log_dir.exists())
            self.assertFalse(config.event_dir.exists())
            self.assertFalse(config.lock_file.exists())
            self.assertFalse(config.state_file.exists())

    def test_old_accepted_task_requires_explicit_age_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            config.log_dir.mkdir()
            log_path = config.log_dir / "task.log"
            log_path.write_text("private transcript", encoding="utf-8")
            write_task(
                config,
                "old-accepted",
                log_paths=[str(log_path)],
                prompt="private prompt",
                thread_id="private-thread",
            )
            without_age = build_retention_inventory_report(config, now=NOW)
            with_age = build_retention_inventory_report(
                config, proposal_age_days=60, now=NOW
            )
            first = without_age["items"][0]
            second = with_age["items"][0]
            self.assertIn(PROPOSAL_AGE_UNSPECIFIED, first["eligibility"]["reason_codes"])
            self.assertFalse(first["eligibility"]["raw_log_prune_candidate"])
            self.assertTrue(second["eligibility"]["raw_log_prune_candidate"])
            self.assertTrue(second["eligibility"]["canonical_task_json_protected"])
            self.assertEqual("protected", second["artifacts"][0]["retention_status"])
            self.assertIn(CANONICAL_TASK_PROTECTED, second["artifacts"][0]["reason_codes"])
            rendered = json.dumps(with_age, sort_keys=True)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("private transcript", rendered)
            self.assertNotIn("private prompt", rendered)
            self.assertNotIn("private-thread", rendered)

    def test_hot_and_fail_closed_reason_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config, "active", status="running", review_status=None)
            write_task(config, "review", status="completed", review_status=None)
            write_task(
                config,
                "unapplied",
                execution_mode="git_worktree",
                execution_apply_status="not_applied",
            )
            write_task(config, "recovery", recovery_required=True)
            write_task(
                config,
                "missing-time",
                created_at=None,
                completed_at=None,
                reviewed_at=None,
            )
            (config.queue_dir / "malformed.json").write_text("{bad json", encoding="utf-8")
            report = build_retention_inventory_report(
                config, proposal_age_days=60, now=NOW
            )
            items = {item["task_id"]: item for item in report["items"]}
            self.assertIn(ACTIVE_TASK, items["active"]["eligibility"]["reason_codes"])
            self.assertIn(REVIEW_PENDING, items["review"]["eligibility"]["reason_codes"])
            self.assertIn(
                ACCEPTED_WORKTREE_UNAPPLIED,
                items["unapplied"]["eligibility"]["reason_codes"],
            )
            self.assertIn(RECOVERY_REQUIRED, items["recovery"]["eligibility"]["reason_codes"])
            self.assertIn(
                MISSING_ACTIVITY_TIMESTAMP,
                items["missing-time"]["eligibility"]["reason_codes"],
            )
            self.assertIn(INVALID_TASK_JSON, items["malformed"]["eligibility"]["reason_codes"])
            self.assertTrue(
                all(
                    item["eligibility"]["canonical_task_json_protected"]
                    for item in report["items"]
                )
            )

    def test_cursor_uncertainty_blocks_task_and_event_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config, "old-accepted")
            config.event_dir.mkdir()
            event = config.event_dir / "old.jsonl"
            event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(event, (946684800, 946684800))
            cursor = root / "cursor.json"
            cursor.write_text("{bad json", encoding="utf-8")
            report = build_retention_inventory_report(
                config,
                proposal_age_days=60,
                notifier_cursor_state_paths=[cursor],
                now=NOW,
            )
            self.assertEqual([CURSOR_UNCERTAINTY], report["cursor_safety"]["reason_codes"])
            self.assertTrue(report["items"][0]["eligibility"]["raw_log_prune_candidate"])
            self.assertIn(CURSOR_UNCERTAINTY, report["event_files"][0]["reason_codes"])
            self.assertFalse(report["event_files"][0]["prune_candidate"])
            self.assertNotIn(str(root), json.dumps(report))

    def test_report_is_repeatable_and_validator_rejects_mutation_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config, "old-accepted")
            first = build_retention_inventory_report(
                config, proposal_age_days=60, now=NOW
            )
            second = build_retention_inventory_report(
                config, proposal_age_days=60, now=NOW
            )
            self.assertEqual(first, second)
            validate_retention_inventory_report(first)
            first["mutation"]["performed"] = True
            with self.assertRaises(RetentionInventoryValidationError):
                validate_retention_inventory_report(first)

    def test_cli_has_no_apply_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "--config",
                        str(config_path),
                        "retention-inventory",
                        "--apply",
                    ]
                )
            self.assertEqual(2, raised.exception.code)

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_batch_runner.config import Config
from codex_batch_runner.fs import write_json_atomic as real_write_json_atomic
from codex_batch_runner.fs import write_json_atomic_create as real_write_json_atomic_create
from codex_batch_runner.lock import FileLock
from codex_batch_runner.retention import build_retention_inventory_report
from codex_batch_runner.retention_compaction import (
    RetentionCompactionError,
    apply_retention_compaction,
    build_retention_compaction_plan,
    validate_retention_compaction_bundle,
    validate_retention_compaction_plan,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
OLD = "2026-01-01T00:00:00+00:00"


def write_config(root: Path, *, cursor: Path | None = None) -> Path:
    config_path = root / "config.json"
    value = {
        "queue_dir": str(root / "runtime" / "tasks"),
        "log_dir": str(root / "runtime" / "logs"),
        "event_dir": str(root / "runtime" / "events"),
        "lock_file": str(root / "runtime" / "runner.lock"),
        "state_file": str(root / "runtime" / "state.json"),
    }
    if cursor is not None:
        value["notifier_cursor_state_paths"] = [str(cursor)]
    config_path.write_text(json.dumps(value), encoding="utf-8")
    return config_path


def write_task(config: Config, task_id: str = "private-session-like-task", **fields: object) -> Path:
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "id": task_id,
        "project_id": "private-account-project",
        "status": "completed",
        "review_status": "accepted",
        "created_at": OLD,
        "completed_at": OLD,
        "reviewed_at": OLD,
        "prompt": "do not retain this raw prompt",
        "thread_id": "thread-private-identifier",
        "stdout": "private stdout",
        **fields,
    }
    path = config.queue_dir / f"{task_id}.json"
    path.write_text(json.dumps(task), encoding="utf-8")
    return path


def inventory_path(
    root: Path,
    config: Config,
    *,
    now: datetime = NOW,
    cursor_paths: list[Path] | None = None,
) -> Path:
    report = build_retention_inventory_report(
        config,
        proposal_age_days=60,
        notifier_cursor_state_paths=cursor_paths,
        now=now,
    )
    path = root / "inventory.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


class RetentionCompactionTests(unittest.TestCase):
    def test_dry_run_is_default_and_apply_is_idempotent_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config)
            report_path = inventory_path(root, config)

            plan = build_retention_compaction_plan(
                config, report_path, "private-session-like-task", now=NOW
            )
            self.assertEqual("dry-run", plan["mode"])
            self.assertEqual("create", plan["action"])
            self.assertFalse((root / "runtime" / "retention").exists())

            with self.assertRaisesRegex(
                RetentionCompactionError, "confirm-operation-id"
            ):
                apply_retention_compaction(
                    config,
                    report_path,
                    "private-session-like-task",
                    confirm_operation_id=None,
                    now=NOW,
                )

            first = apply_retention_compaction(
                config,
                report_path,
                "private-session-like-task",
                confirm_operation_id=plan["operation_id"],
                now=NOW,
            )
            second = apply_retention_compaction(
                config,
                report_path,
                "private-session-like-task",
                confirm_operation_id=plan["operation_id"],
                now=NOW,
            )
            self.assertEqual("created", first["action"])
            self.assertTrue(first["mutation"]["performed"])
            self.assertEqual("noop", second["action"])
            self.assertFalse(second["mutation"]["performed"])

            store = root / "runtime" / "retention"
            self.assertEqual(1, len(list((store / "bundles").glob("*.json"))))
            self.assertEqual(
                1, len(list((store / "transactions").glob("*.json")))
            )
            index = json.loads((store / "restore-index-v1.json").read_text())
            self.assertEqual(1, len(index["entries"]))
            entry = next(iter(index["entries"].values()))
            self.assertFalse(entry["restore_action_supported"])
            self.assertFalse(entry["raw_artifact_restore_supported"])

            stored = "\n".join(
                path.read_text()
                for path in store.rglob("*.json")
            )
            for private in (
                str(root),
                "private-session-like-task",
                "private-account-project",
                "do not retain this raw prompt",
                "thread-private-identifier",
                "private stdout",
            ):
                self.assertNotIn(private, stored)

    def test_source_digest_drift_and_stale_report_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            task_path = write_task(config)
            report_path = inventory_path(root, config)
            task = json.loads(task_path.read_text())
            task["resolution"] = "manual"
            task_path.write_text(json.dumps(task), encoding="utf-8")
            with self.assertRaisesRegex(RetentionCompactionError, "digest changed"):
                build_retention_compaction_plan(
                    config, report_path, "private-session-like-task", now=NOW
                )

            task_path.write_text(
                json.dumps({key: value for key, value in task.items() if key != "resolution"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RetentionCompactionError, "stale"):
                build_retention_compaction_plan(
                    config,
                    report_path,
                    "private-session-like-task",
                    now=NOW + timedelta(seconds=301),
                )

    def test_malformed_active_review_failure_and_cursor_uncertainty_reject(self) -> None:
        scenarios = (
            {"status": "running", "review_status": None},
            {"status": "needs_resume", "review_status": None},
            {"status": "completed", "review_status": None},
            {"status": "failed", "review_status": None},
        )
        for fields in scenarios:
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = Config.load(str(write_config(root)))
                write_task(config, **fields)
                report_path = inventory_path(root, config)
                with self.assertRaisesRegex(RetentionCompactionError, "not eligible"):
                    build_retention_compaction_plan(
                        config, report_path, "private-session-like-task", now=NOW
                    )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config)
            malformed = root / "inventory.json"
            malformed.write_text("{bad json", encoding="utf-8")
            with self.assertRaisesRegex(RetentionCompactionError, "malformed"):
                build_retention_compaction_plan(
                    config, malformed, "private-session-like-task", now=NOW
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cursor = root / "cursor.json"
            cursor.write_text("{bad json", encoding="utf-8")
            config = Config.load(str(write_config(root, cursor=cursor)))
            write_task(config)
            report_path = inventory_path(root, config, cursor_paths=[cursor])
            with self.assertRaisesRegex(RetentionCompactionError, "cursor safety"):
                build_retention_compaction_plan(
                    config, report_path, "private-session-like-task", now=NOW
                )

    def test_partial_write_is_recovered_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config)
            report_path = inventory_path(root, config)
            plan = build_retention_compaction_plan(
                config, report_path, "private-session-like-task", now=NOW
            )
            calls = 0

            def fail_first_index(path: Path, value: object) -> None:
                nonlocal calls
                calls += 1
                if path.name == "restore-index-v1.json" and calls == 1:
                    raise OSError("synthetic index write failure")
                real_write_json_atomic(path, value)

            with patch(
                "codex_batch_runner.retention_compaction.write_json_atomic",
                side_effect=fail_first_index,
            ):
                with self.assertRaises(OSError):
                    apply_retention_compaction(
                        config,
                        report_path,
                        "private-session-like-task",
                        confirm_operation_id=plan["operation_id"],
                        now=NOW,
                    )

            store = root / "runtime" / "retention"
            transaction = json.loads(
                next((store / "transactions").glob("*.json")).read_text()
            )
            self.assertEqual("prepared", transaction["state"])
            recovered = apply_retention_compaction(
                config,
                report_path,
                "private-session-like-task",
                confirm_operation_id=plan["operation_id"],
                now=NOW,
            )
            self.assertEqual("recovered", recovered["action"])
            self.assertTrue(recovered["recovered_from_partial"])
            self.assertEqual(1, len(list((store / "bundles").glob("*.json"))))
            self.assertEqual(
                1,
                len(
                    json.loads((store / "restore-index-v1.json").read_text())[
                        "entries"
                    ]
                ),
            )

    def test_restore_index_digest_and_unsupported_restore_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config)
            report_path = inventory_path(root, config)
            plan = build_retention_compaction_plan(
                config, report_path, "private-session-like-task", now=NOW
            )
            apply_retention_compaction(
                config,
                report_path,
                "private-session-like-task",
                confirm_operation_id=plan["operation_id"],
                now=NOW,
            )
            index_path = root / "runtime" / "retention" / "restore-index-v1.json"
            index = json.loads(index_path.read_text())
            entry = next(iter(index["entries"].values()))
            entry["restore_action_supported"] = True
            index_path.write_text(json.dumps(index), encoding="utf-8")
            with self.assertRaisesRegex(RetentionCompactionError, "digest mismatch"):
                build_retention_compaction_plan(
                    config, report_path, "private-session-like-task", now=NOW
                )

    def test_partial_project_malformed_sources_cursor_drift_and_lock_reject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config)
            partial = build_retention_inventory_report(
                config, proposal_age_days=60, project_id="private-account-project", now=NOW
            )
            partial_path = root / "partial.json"
            partial_path.write_text(json.dumps(partial), encoding="utf-8")
            with self.assertRaisesRegex(RetentionCompactionError, "project-filtered"):
                build_retention_compaction_plan(
                    config, partial_path, "private-session-like-task", now=NOW
                )

            (config.queue_dir / "unrelated.json").write_text("{bad json", encoding="utf-8")
            malformed_tasks = inventory_path(root, config)
            with self.assertRaisesRegex(RetentionCompactionError, "malformed unrelated task"):
                build_retention_compaction_plan(
                    config, malformed_tasks, "private-session-like-task", now=NOW
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config)
            config.event_dir.mkdir(parents=True)
            event = config.event_dir / "events.jsonl"
            event.write_text("{bad event\n", encoding="utf-8")
            malformed_events = inventory_path(root, config)
            with self.assertRaisesRegex(RetentionCompactionError, "malformed unrelated event"):
                build_retention_compaction_plan(
                    config, malformed_events, "private-session-like-task", now=NOW
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cursor = root / "cursor.json"
            config = Config.load(str(write_config(root, cursor=cursor)))
            write_task(config)
            config.event_dir.mkdir(parents=True)
            event = config.event_dir / "events.jsonl"
            event.write_text('{}\n', encoding="utf-8")
            cursor.write_text(
                json.dumps({"last_processed_event_file": str(event)}), encoding="utf-8"
            )
            report_path = inventory_path(root, config, cursor_paths=[cursor])
            cursor.write_text(
                json.dumps(
                    {
                        "last_processed_event_file": str(event),
                        "recent_event_ids": ["changed"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RetentionCompactionError, "scope changed"):
                build_retention_compaction_plan(
                    config, report_path, "private-session-like-task", now=NOW
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config)
            report_path = inventory_path(root, config)
            plan = build_retention_compaction_plan(
                config, report_path, "private-session-like-task", now=NOW
            )
            lock = FileLock(config.lock_file, config.stale_lock_seconds)
            self.assertTrue(lock.acquire())
            try:
                with self.assertRaisesRegex(RetentionCompactionError, "active queue lock"):
                    apply_retention_compaction(
                        config,
                        report_path,
                        "private-session-like-task",
                        confirm_operation_id=plan["operation_id"],
                        now=NOW,
                    )
            finally:
                lock.release()

    def test_archived_runtime_inconsistency_is_not_authorized_by_inventory_candidate(self) -> None:
        scenarios = (
            {"archive_gate_result": {"status": "grandfathered"}},
            {"chain_status": "needs_fix"},
            {"active_run_id": "active-run"},
            {"recovery_required": True},
            {
                "execution_mode": "git_worktree",
                "execution_worktree_status": "retained",
                "execution_apply_status": "not_applied",
                "execution_cleanup_kind": "applied",
            },
            {
                "execution_mode": "git_worktree",
                "execution_worktree_status": "cleaned",
                "execution_apply_status": "applied",
                "execution_cleanup_kind": "applied",
                "execution_worktree_pool": {"slot_id": "opaque"},
                "execution_worktree_lease_status": "leased",
            },
        )
        for fields in scenarios:
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = Config.load(str(write_config(root)))
                base = {
                    "status": "archived",
                    "archived_at": OLD,
                    "previous_status": "completed",
                    "archive_gate_result": {"status": "passed"},
                    **fields,
                }
                write_task(config, **base)
                report_path = inventory_path(root, config)
                with self.assertRaisesRegex(
                    RetentionCompactionError, "not eligible|consistency rejected"
                ):
                    build_retention_compaction_plan(
                        config, report_path, "private-session-like-task", now=NOW
                    )

    def test_bundle_first_failure_recovers_and_strict_validators_reject_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            write_task(config)
            report_path = inventory_path(root, config)
            plan = build_retention_compaction_plan(
                config, report_path, "private-session-like-task", now=NOW
            )
            validate_retention_compaction_plan(plan)

            def fail_transaction(path: Path, value: object) -> None:
                if path.parent.name == "transactions":
                    raise OSError("synthetic journal failure")
                real_write_json_atomic_create(path, value)

            with patch(
                "codex_batch_runner.retention_compaction.write_json_atomic_create",
                side_effect=fail_transaction,
            ):
                with self.assertRaises(OSError):
                    apply_retention_compaction(
                        config,
                        report_path,
                        "private-session-like-task",
                        confirm_operation_id=plan["operation_id"],
                        now=NOW,
                    )
            store = root / "runtime" / "retention"
            self.assertEqual(1, len(list((store / "bundles").glob("*.json"))))
            self.assertFalse((store / "restore-index-v1.json").exists())
            recovered = apply_retention_compaction(
                config,
                report_path,
                "private-session-like-task",
                confirm_operation_id=plan["operation_id"],
                now=NOW,
            )
            self.assertEqual("recovered", recovered["action"])

            bundle = json.loads(next((store / "bundles").glob("*.json")).read_text())
            bundle["restore_contract"]["restore_action_supported"] = True
            with self.assertRaisesRegex(RetentionCompactionError, "overclaims"):
                validate_retention_compaction_bundle(bundle)
            forged_plan = dict(plan)
            forged_plan["action"] = "delete"
            with self.assertRaisesRegex(RetentionCompactionError, "semantics"):
                validate_retention_compaction_plan(forged_plan)


if __name__ == "__main__":
    unittest.main()

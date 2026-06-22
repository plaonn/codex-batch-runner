from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_batch_runner.config import Config
from codex_batch_runner.queue import (
    archive_task,
    create_task,
    dependency_status,
    load_task,
    recover_stale_running_tasks,
    save_task,
    select_next_task,
    set_resolution,
    set_review_status,
)


class QueueTests(unittest.TestCase):
    def test_select_skips_unmet_dependency_and_picks_ready_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            create_task(config, "blocked", tmp, task_id="blocked", depends_on=["missing"])
            ready = create_task(config, "ready", tmp, task_id="ready")

            selected = select_next_task(config)

            self.assertEqual(ready["id"], selected["id"])

    def test_select_allows_completed_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"

            save_task(config, dep)
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            selected = select_next_task(config)

            self.assertEqual("child", selected["id"])

    def test_dependency_status_preserves_completed_dependency_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"
            save_task(config, dep)
            child = create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            ready, blocked_by = dependency_status(child, {"dep": dep})

            self.assertTrue(ready)
            self.assertEqual([], blocked_by)

    def test_dependency_status_can_require_accepted_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"
            save_task(config, dep)
            child = create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            ready, blocked_by = dependency_status(child, {"dep": dep}, require_accepted_review=True)

            self.assertFalse(ready)
            self.assertEqual(["dep"], blocked_by)

    def test_select_skips_completed_unaccepted_dependency_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(Config.load(root=Path(tmp)), dependency_requires_accepted_review=True)
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "unreviewed"
            save_task(config, dep)
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])
            ready = create_task(config, "ready", tmp, task_id="ready")

            selected = select_next_task(config)

            self.assertEqual(ready["id"], selected["id"])

    def test_select_allows_completed_accepted_dependency_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(Config.load(root=Path(tmp)), dependency_requires_accepted_review=True)
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "accepted"
            save_task(config, dep)
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            selected = select_next_task(config)

            self.assertEqual("child", selected["id"])

    def test_dependency_status_blocks_accepted_unapplied_worktree_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "accepted"
            dep["execution_mode"] = "git_worktree"
            save_task(config, dep)
            child = create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            ready, blocked_by = dependency_status(child, {"dep": dep})

            self.assertFalse(ready)
            self.assertEqual(["dep"], blocked_by)

    def test_select_skips_child_with_accepted_unapplied_worktree_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "accepted"
            dep["execution_mode"] = "git_worktree"
            save_task(config, dep)
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])
            create_task(config, "ready", tmp, task_id="ready")

            selected = select_next_task(config)

            self.assertEqual("ready", selected["id"])

    def test_dependency_status_allows_applied_worktree_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "accepted"
            dep["execution_mode"] = "git_worktree"
            dep["execution_apply_status"] = "applied"
            save_task(config, dep)
            child = create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            ready, blocked_by = dependency_status(child, {"dep": dep})

            self.assertTrue(ready)
            self.assertEqual([], blocked_by)

    def test_recover_stale_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = create_task(config, "stale", tmp, task_id="stale")
            task["status"] = "running"
            task["started_at"] = "2000-01-01T00:00:00+00:00"

            save_task(config, task)

            recovered = recover_stale_running_tasks(config)
            loaded = load_task(config, "stale")

            self.assertEqual(["stale"], recovered)
            self.assertEqual("runnable", loaded["status"])

    def test_archive_task_preserves_task_file_and_previous_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            create_task(config, "done", tmp, task_id="done")

            archived = archive_task(config, "done")
            loaded = load_task(config, "done")

            self.assertEqual("archived", archived["status"])
            self.assertEqual("archived", loaded["status"])
            self.assertEqual("runnable", loaded["previous_status"])
            self.assertIsNotNone(loaded["archived_at"])

    def test_create_task_initializes_review_and_project_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))

            task = create_task(config, "work", tmp, task_id="review")

            self.assertEqual(1, task["schema_version"])
            self.assertIsNone(task["review_status"])
            self.assertIsNone(task["reviewed_at"])
            self.assertIsNone(task["review_reason"])
            self.assertEqual(str(Path(tmp).resolve()), task["project_root"])
            self.assertEqual(Path(tmp).name, task["project_id"])
            self.assertIsNone(task["category"])
            self.assertEqual([], task["labels"])
            self.assertIsNone(task["created_by"])
            self.assertIsNone(task["root_task_id"])
            self.assertIsNone(task["parent_task_id"])
            self.assertEqual(0, task["review_cycle"])
            self.assertEqual(0, task["review_attempts"])
            self.assertEqual(0, task["fix_attempts"])
            self.assertIsNone(task["chain_status"])
            self.assertEqual([], task["review_findings"])
            self.assertIsNone(task["last_review_decision"])
            self.assertFalse(task["auto_fix_allowed"])
            self.assertIsNone(task["auto_fix_budget"])
            self.assertIsNone(task["last_auto_fix_task_id"])
            self.assertEqual([], task["finding_fingerprints"])
            self.assertEqual("codex", task["execution_backend"])
            self.assertEqual("codex", task["capacity_pool"])
            self.assertEqual("normal", task["task_priority"])
            self.assertIsNone(task["shell_command"])
            self.assertIsNone(task["shell_timeout_seconds"])

    def test_create_task_accepts_scheduling_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))

            task = create_task(config, "work", tmp, task_id="scheduled", capacity_pool="spark", task_priority="high")

            self.assertEqual("spark", task["capacity_pool"])
            self.assertEqual("high", task["task_priority"])

    def test_create_task_rejects_invalid_task_priority_and_capacity_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))

            with self.assertRaisesRegex(ValueError, "task_priority must be one of"):
                create_task(config, "work", tmp, task_id="bad-priority", task_priority="urgent")
            with self.assertRaisesRegex(ValueError, "capacity_pool must be a non-empty string"):
                create_task(config, "work", tmp, task_id="bad-pool", capacity_pool="")

    def test_select_orders_by_project_priority_then_task_priority_then_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                Config.load(root=Path(tmp)),
                max_total_running=4,
                max_running_per_project=4,
                capacity_pools={"codex": {"max_running": 4}},
                project_priorities={"high-project": 10, "low-project": 20},
            )
            create_task(config, "low asap", tmp, task_id="low-asap", project_id="low-project", task_priority="asap")
            create_task(config, "high normal", tmp, task_id="high-normal", project_id="high-project")

            selected = select_next_task(config)

            self.assertEqual("high-normal", selected["id"])

    def test_select_orders_same_project_by_task_priority_then_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                Config.load(root=Path(tmp)),
                max_total_running=4,
                max_running_per_project=4,
                capacity_pools={"codex": {"max_running": 4}},
            )
            normal = create_task(config, "normal", tmp, task_id="normal", project_id="project")
            high = create_task(config, "high", tmp, task_id="high", project_id="project", task_priority="high")
            asap = create_task(config, "asap", tmp, task_id="asap", project_id="project", task_priority="asap")
            for task, created in (
                (normal, "2026-01-01T00:00:00+00:00"),
                (high, "2026-01-02T00:00:00+00:00"),
                (asap, "2026-01-03T00:00:00+00:00"),
            ):
                task["created_at"] = created
                save_task(config, task)

            selected = select_next_task(config)

            self.assertEqual("asap", selected["id"])

    def test_project_priority_aging_prevents_starvation_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                Config.load(root=Path(tmp)),
                max_total_running=4,
                max_running_per_project=4,
                capacity_pools={"codex": {"max_running": 4}},
                project_priorities={"older-low": 100, "newer-high": 99},
                project_priority_aging_hours=24,
            )
            old = create_task(config, "old", tmp, task_id="old", project_id="older-low")
            new = create_task(config, "new", tmp, task_id="new", project_id="newer-high")
            old["created_at"] = "2000-01-01T00:00:00+00:00"
            new["created_at"] = "2026-01-01T00:00:00+00:00"
            save_task(config, old)
            save_task(config, new)

            selected = select_next_task(config)

            self.assertEqual("old", selected["id"])

    def test_project_priority_aging_zero_uses_strict_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                Config.load(root=Path(tmp)),
                max_total_running=4,
                max_running_per_project=4,
                capacity_pools={"codex": {"max_running": 4}},
                project_priorities={"older-low": 100, "newer-high": 99},
                project_priority_aging_hours=0,
            )
            old = create_task(config, "old", tmp, task_id="old", project_id="older-low")
            new = create_task(config, "new", tmp, task_id="new", project_id="newer-high")
            old["created_at"] = "2000-01-01T00:00:00+00:00"
            new["created_at"] = "2026-01-01T00:00:00+00:00"
            save_task(config, old)
            save_task(config, new)

            selected = select_next_task(config)

            self.assertEqual("new", selected["id"])

    def test_select_skips_full_capacity_without_mutating_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "other"
            other.mkdir()
            config = replace(
                Config.load(root=Path(tmp)),
                max_total_running=2,
                max_running_per_project=1,
                capacity_pools={"codex": {"max_running": 2}},
            )
            running = create_task(config, "running", tmp, task_id="running")
            running["status"] = "running"
            save_task(config, running)
            blocked = create_task(config, "blocked same project", tmp, task_id="blocked")
            ready = create_task(config, "ready other project", str(other), task_id="ready")

            selected = select_next_task(config)

            self.assertEqual("ready", selected["id"])
            self.assertEqual("runnable", load_task(config, blocked["id"])["status"])

    def test_select_skips_unknown_or_full_capacity_pool_without_mutating_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spark = Path(tmp) / "spark"
            spark.mkdir()
            config = replace(
                Config.load(root=Path(tmp)),
                max_total_running=3,
                max_running_per_project=3,
                capacity_pools={"codex": {"max_running": 1}, "spark": {"max_running": 1}},
            )
            running = create_task(config, "running", tmp, task_id="running")
            running["status"] = "running"
            save_task(config, running)
            full_pool = create_task(config, "full", tmp, task_id="full-pool")
            unknown = create_task(config, "unknown", tmp, task_id="unknown", capacity_pool="missing")
            ready = create_task(config, "ready", str(spark), task_id="spark-ready", capacity_pool="spark")

            selected = select_next_task(config)

            self.assertEqual("spark-ready", selected["id"])
            self.assertEqual("runnable", load_task(config, full_pool["id"])["status"])
            self.assertEqual("runnable", load_task(config, unknown["id"])["status"])
            self.assertEqual("runnable", load_task(config, ready["id"])["status"])

    def test_create_task_accepts_project_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))

            task = create_task(
                config,
                "work",
                tmp,
                task_id="metadata",
                project_id="custom",
                category="maintenance",
                labels=["queue", "review"],
                created_by="test",
            )

            self.assertEqual("custom", task["project_id"])
            self.assertEqual("maintenance", task["category"])
            self.assertEqual(["queue", "review"], task["labels"])
            self.assertEqual("test", task["created_by"])

    def test_set_review_status_records_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = create_task(config, "done", tmp, task_id="done")
            task["status"] = "completed"
            save_task(config, task)

            task = set_review_status(config, "done", "accepted", "verified", require_completed=True)

            self.assertEqual("accepted", task["review_status"])
            self.assertEqual("verified", task["review_reason"])
            self.assertIsNotNone(task["reviewed_at"])

    def test_accept_review_status_requires_completed_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            create_task(config, "work", tmp, task_id="running")

            with self.assertRaisesRegex(ValueError, "requires completed task status"):
                set_review_status(config, "running", "accepted", "verified", require_completed=True)

    def test_reject_review_status_remains_available_for_non_completed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            create_task(config, "work", tmp, task_id="running")

            task = set_review_status(config, "running", "rejected", "operator stopped review")

            self.assertEqual("rejected", task["review_status"])

    def test_set_resolution_records_failed_task_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = create_task(config, "work", tmp, task_id="failed")
            task["status"] = "failed"
            save_task(config, task)

            resolved = set_resolution(config, "failed", "manual", "handled outside cbr")

            self.assertEqual("failed", resolved["status"])
            self.assertEqual("manual", resolved["resolution"])
            self.assertEqual("handled outside cbr", resolved["resolution_reason"])
            self.assertIsNotNone(resolved["resolved_at"])

    def test_set_resolution_records_completed_review_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = create_task(config, "work", tmp, task_id="superseded")
            task["status"] = "completed"
            task["review_status"] = "needs_followup"
            save_task(config, task)

            resolved = set_resolution(config, "superseded", "superseded", "handled by follow-up task")

            self.assertEqual("completed", resolved["status"])
            self.assertEqual("needs_followup", resolved["review_status"])
            self.assertEqual("superseded", resolved["resolution"])
            self.assertEqual("handled by follow-up task", resolved["resolution_reason"])
            self.assertIsNotNone(resolved["resolved_at"])

    def test_set_resolution_rejects_completed_unreviewed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = create_task(config, "work", tmp, task_id="unreviewed")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            save_task(config, task)

            with self.assertRaises(ValueError):
                set_resolution(config, "unreviewed", "manual", "skip review")

    def test_set_resolution_rejects_runnable_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            create_task(config, "work", tmp, task_id="task")

            with self.assertRaises(ValueError):
                set_resolution(config, "task", "manual", "not failed")


if __name__ == "__main__":
    unittest.main()

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

    def test_set_resolution_rejects_runnable_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            create_task(config, "work", tmp, task_id="task")

            with self.assertRaises(ValueError):
                set_resolution(config, "task", "manual", "not failed")


if __name__ == "__main__":
    unittest.main()

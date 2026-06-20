from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.config import Config
from codex_batch_runner.queue import (
    archive_task,
    create_task,
    load_task,
    recover_stale_running_tasks,
    select_next_task,
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
            from codex_batch_runner.queue import save_task

            save_task(config, dep)
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            selected = select_next_task(config)

            self.assertEqual("child", selected["id"])

    def test_recover_stale_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = create_task(config, "stale", tmp, task_id="stale")
            task["status"] = "running"
            task["started_at"] = "2000-01-01T00:00:00+00:00"
            from codex_batch_runner.queue import load_task, save_task

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

    def test_create_task_initializes_review_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))

            task = create_task(config, "work", tmp, task_id="review")

            self.assertIsNone(task["review_status"])
            self.assertIsNone(task["reviewed_at"])
            self.assertIsNone(task["review_reason"])

    def test_set_review_status_records_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            create_task(config, "done", tmp, task_id="done")

            task = set_review_status(config, "done", "accepted", "verified")

            self.assertEqual("accepted", task["review_status"])
            self.assertEqual("verified", task["review_reason"])
            self.assertIsNotNone(task["reviewed_at"])


if __name__ == "__main__":
    unittest.main()

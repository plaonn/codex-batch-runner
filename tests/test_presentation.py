from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from codex_batch_runner.config import Config
from codex_batch_runner.presentation import task_list_presentation
from codex_batch_runner.queue import create_task, save_task, set_resolution
from codex_batch_runner.timeutil import utc_now


class TaskListPresentationTests(unittest.TestCase):
    def test_ready_new_and_resume_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            new = create_task(config, "new work", tmp, task_id="new")
            resume = create_task(config, "resume work", tmp, task_id="resume")
            resume["status"] = "needs_resume"

            self.assert_projection(new, config, "ready", "new", "..new", "runnable")
            self.assert_projection(resume, config, "ready", "resume", "..resume", "needs_resume")

    def test_waiting_capacity_and_cooldown_projection_preserves_legacy_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            running = create_task(config, "running", tmp, task_id="running")
            running["status"] = "running"
            save_task(config, running)
            capacity = create_task(config, "capacity", tmp, task_id="capacity")
            cooldown = create_task(config, "cooldown", tmp, task_id="cooldown")
            cooldown["cooldown_until"] = (utc_now() + timedelta(minutes=5)).isoformat()

            capacity_projection = task_list_presentation(capacity, {"running": running, "capacity": capacity}, config)
            cooldown_projection = task_list_presentation(cooldown, {"cooldown": cooldown}, config)

            self.assertEqual(("waiting", "capacity", "||capacity", "runnable"), self.summary(capacity_projection))
            self.assertEqual([{"type": "capacity", "reason": "max_total_running"}], capacity_projection.blockers[:1])
            self.assertEqual(("waiting", "cooldown", "||cooldown", "runnable"), self.summary(cooldown_projection))

    def test_dependency_blocked_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            child = create_task(config, "child", tmp, task_id="child", depends_on=["missing"])

            projection = task_list_presentation(child, {"child": child}, config)

            self.assertEqual(("blocked", "dep", "##dep", "blocked_dependency"), self.summary(projection))
            self.assertEqual("dependency", projection.blockers[0]["type"])
            self.assertEqual("missing", projection.blockers[0]["id"])

    def test_running_projection(self) -> None:
        task = {"id": "running", "status": "running"}

        self.assertEqual(("running", "exec", ">>exec", "running"), self.summary(task_list_presentation(task)))

    def test_review_required_and_reviewer_decision_projection(self) -> None:
        review = {"id": "review", "status": "completed", "review_status": None}
        fix = {
            "id": "fix",
            "status": "completed",
            "review_status": "unreviewed",
            "reviewer_codex": {"decision": "needs_fix"},
        }
        failed_review = {
            "id": "failed-review",
            "status": "completed",
            "review_status": "unreviewed",
            "last_review_decision": "failed_review",
        }

        self.assertEqual(("action_required", "review", "??review", "awaiting_review"), self.summary(task_list_presentation(review)))
        self.assertEqual(("action_required", "fix", "??fix", "review_needs_fix"), self.summary(task_list_presentation(fix)))
        self.assertEqual(
            ("action_required", "error", "??error", "review_failed"),
            self.summary(task_list_presentation(failed_review)),
        )

    def test_pending_followup_and_apply_projection(self) -> None:
        followup = {"id": "follow", "status": "completed", "review_status": "needs_followup"}
        apply = {
            "id": "apply",
            "status": "completed",
            "review_status": "accepted",
            "execution_mode": "git_worktree",
            "execution_apply_status": "not_applied",
        }

        self.assertEqual(("pending", "followup", "++followup", "needs_followup"), self.summary(task_list_presentation(followup)))
        self.assertEqual(("pending", "apply", "++apply", "accepted_unapplied"), self.summary(task_list_presentation(apply)))

    def test_failed_error_projection(self) -> None:
        task = {"id": "failed", "status": "failed", "last_error": "boom"}

        self.assertEqual(("action_required", "error", "??error", "failed"), self.summary(task_list_presentation(task)))
        self.assertNotIn("last_error", task_list_presentation(task).metadata)

    def test_closed_resolved_discarded_archived_and_success_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            resolved = create_task(config, "resolved", tmp, task_id="resolved")
            resolved["status"] = "failed"
            save_task(config, resolved)
            resolved = set_resolution(config, "resolved", "manual")
            discarded = {
                "id": "discarded",
                "status": "completed",
                "review_status": "rejected",
                "execution_mode": "git_worktree",
                "execution_worktree_status": "cleaned",
                "execution_cleanup_kind": "discard",
                "execution_cleanup_result_applied": False,
            }
            archived = {"id": "archived", "status": "archived"}
            success = {"id": "success", "status": "completed", "review_status": "accepted"}

            self.assertEqual(("closed", "resolved", "--resolved", "resolved"), self.summary(task_list_presentation(resolved)))
            self.assertEqual(("closed", "rejected", "--rejected", "discarded"), self.summary(task_list_presentation(discarded)))
            self.assertEqual(("closed", "archived", "--archived", "archived"), self.summary(task_list_presentation(archived)))
            self.assertEqual(("closed", "success", "--success", "completed"), self.summary(task_list_presentation(success)))

    def assert_projection(
        self,
        task: dict,
        config: Config,
        phase: str,
        kind: str,
        status_label: str,
        legacy_status: str,
    ) -> None:
        self.assertEqual(
            (phase, kind, status_label, legacy_status),
            self.summary(task_list_presentation(task, {task["id"]: task}, config)),
        )

    def summary(self, projection) -> tuple[str, str, str, str]:
        return projection.phase, projection.kind, projection.status_label, projection.legacy_status


if __name__ == "__main__":
    unittest.main()

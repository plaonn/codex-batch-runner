from __future__ import annotations

from typing import Any

from .config import Config
from .events import emit_task_event, transition_payload
from .lock import FileLock
from .queue import load_task, save_task, task_title
from .timeutil import iso_now
from .worktree import build_apply_report, build_apply_report_locked


def accept_task_and_integrate(config: Config, task_id: str, reason: str | None, *, source: str) -> dict[str, Any]:
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire(task_id=task_id):
        locked_result = already_accepted_applied_result(config, task_id)
        if locked_result is not None:
            return locked_result
        return {
            "task": None,
            "post_accept": {"status": "locked", "errors": [f"another runner is active: {config.lock_file}"]},
        }
    try:
        task = mark_task_accepted_locked(config, task_id, reason, source=source)
        post_accept = integrate_accepted_worktree_locked(config, task_id)
        return {"task": load_task(config, task_id), "post_accept": post_accept}
    finally:
        lock.release()


def already_accepted_applied_result(config: Config, task_id: str) -> dict[str, Any] | None:
    try:
        task = load_task(config, task_id)
    except FileNotFoundError:
        return None
    if (
        task.get("status") == "completed"
        and task.get("review_status") == "accepted"
        and task.get("execution_mode") == "git_worktree"
        and task.get("execution_apply_status") == "applied"
    ):
        return {
            "task": task,
            "post_accept": {"status": "already_applied", "available": True, "should_wake": False},
        }
    return None


def mark_task_accepted_locked(config: Config, task_id: str, reason: str | None, *, source: str) -> dict[str, Any]:
    task = load_task(config, task_id)
    if task.get("status") != "completed":
        raise ValueError(f"accepted review requires completed task status, found {task.get('status')}")
    task["review_status"] = "accepted"
    task["reviewed_at"] = iso_now()
    task["review_reason"] = reason
    if task.get("chain_status"):
        task["chain_status"] = "accepted"
    save_task(config, task)
    emit_task_event(
        config,
        "task_reviewed",
        task,
        source=source,
        summary=f"accepted task {task_title(task)} ({task_id})",
        payload=transition_payload(task, review_status="accepted", reviewed_at=task.get("reviewed_at")),
    )
    return task


def integrate_accepted_worktree(
    config: Config,
    task_id: str,
    *,
    locked: bool,
) -> dict[str, Any]:
    if locked:
        return integrate_accepted_worktree_locked(config, task_id)
    return summarize_apply_report(build_apply_report(config, task_id, apply=True))


def integrate_accepted_worktree_locked(config: Config, task_id: str) -> dict[str, Any]:
    task = load_task(config, task_id)
    if task.get("execution_mode") != "git_worktree":
        return {"status": "not_worktree", "available": True, "should_wake": True}
    if task.get("execution_apply_status") == "applied":
        return {"status": "already_applied", "available": True, "should_wake": True}
    report = build_apply_report_locked(config, task_id, apply=True)
    return summarize_apply_report(report)


def summarize_apply_report(report: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": "apply_blocked",
        "available": False,
        "should_wake": False,
        "worktree_apply": report,
    }
    if report.get("applied"):
        summary.update({"status": "applied", "available": True, "should_wake": True})
    elif report.get("rebased"):
        summary.update({"status": "rebased_awaiting_re_review", "available": False, "should_wake": True})
    elif isinstance(report.get("conflict_fix"), dict) and report["conflict_fix"].get("task_id"):
        summary.update(
            {
                "status": "conflict_fix_subtask_queued",
                "available": False,
                "should_wake": True,
                "conflict_fix_task_id": report["conflict_fix"].get("task_id"),
                "conflict_fix_task_title": report["conflict_fix"].get("title"),
            }
        )
    elif report.get("errors"):
        summary.update({"status": "apply_blocked", "available": False, "should_wake": False})
    return summary

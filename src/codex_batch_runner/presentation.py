from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .queue import (
    RUNNABLE_STATUSES,
    capacity_blockers,
    dependency_blockers,
    is_in_cooldown,
    rejected_discarded_result,
)

PHASE_MARKERS = {
    "ready": "..",
    "waiting": "||",
    "blocked": "##",
    "running": ">>",
    "pending": "++",
    "action_required": "??",
    "closed": "--",
}


@dataclass(frozen=True)
class TaskListPresentation:
    phase: str
    kind: str
    status_label: str
    detail: str = ""
    blockers: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    legacy_status: str = ""


def task_list_presentation(
    task: dict,
    by_id: dict[str, dict] | None = None,
    config: Config | None = None,
    *,
    include_subtasks: bool = True,
) -> TaskListPresentation:
    by_id = by_id or {}
    status = str(task.get("status") or "-")
    dependency_items = _dependency_blockers(task, by_id, config)
    metadata = _safe_metadata(task)

    if status == "archived":
        return _presentation("closed", "archived", "archived", metadata=metadata, legacy_status="archived")
    if task.get("resolution") and status in {"failed", "blocked_user", "completed"}:
        return _presentation(
            "closed",
            "resolved",
            f"resolved: {task.get('resolution')}",
            metadata=metadata,
            legacy_status="resolved",
        )
    if rejected_discarded_result(task):
        return _presentation(
            "closed",
            "rejected",
            "rejected; discarded; not applied",
            metadata=metadata,
            legacy_status="discarded",
        )

    if status in RUNNABLE_STATUSES:
        if dependency_items:
            return _presentation(
                "blocked",
                "dep",
                "dependency blocked",
                blockers=dependency_items,
                metadata=metadata,
                legacy_status="blocked_dependency",
            )
        if is_in_cooldown(task):
            return _presentation("waiting", "cooldown", "cooldown", metadata=metadata, legacy_status=status)
        if config is not None:
            capacity = capacity_blockers(config, task)
            if capacity:
                return _presentation(
                    "waiting",
                    "capacity",
                    "waiting for capacity",
                    blockers=[{"type": "capacity", "reason": item} for item in capacity],
                    metadata=metadata,
                    legacy_status=status,
                )
        if include_subtasks:
            subtask = _subtask_presentation(task, by_id, config, metadata)
            if subtask:
                return subtask
        kind = "resume" if status == "needs_resume" else "new"
        detail = "ready to resume" if status == "needs_resume" else "ready"
        return _presentation("ready", kind, detail, metadata=metadata, legacy_status=status)

    if include_subtasks:
        subtask = _subtask_presentation(task, by_id, config, metadata)
        if subtask:
            return subtask
    if status == "usage_exhausted":
        return _presentation("waiting", "usage", "usage exhausted", metadata=metadata, legacy_status="usage_exhausted")
    if status == "cooldown":
        return _presentation("waiting", "cooldown", "cooldown", metadata=metadata, legacy_status="cooldown")
    if status == "running":
        return _presentation("running", "exec", "running", metadata=metadata, legacy_status="running")
    if status == "completed":
        return _completed_presentation(task, metadata)
    if status == "failed":
        return _presentation("action_required", "error", "failed", metadata=metadata, legacy_status="failed")
    if status == "blocked_user":
        return _presentation("action_required", "review", "blocked user", metadata=metadata, legacy_status="blocked_user")

    return _presentation("action_required", "error", status, metadata=metadata, legacy_status=status)


def task_list_status(task: dict, by_id: dict[str, dict] | None = None, config: Config | None = None) -> str:
    return task_list_presentation(task, by_id, config).legacy_status


def task_list_status_without_subtasks(task: dict, by_id: dict[str, dict], config: Config) -> str:
    return task_list_presentation(task, by_id, config, include_subtasks=False).legacy_status


def _completed_presentation(task: dict, metadata: dict[str, Any]) -> TaskListPresentation:
    review = _review_status(task)
    if review == "unreviewed":
        reviewer_decision = _pending_reviewer_decision(task)
        if reviewer_decision == "needs_fix":
            return _presentation(
                "action_required",
                "fix",
                "review needs fix",
                metadata=metadata,
                legacy_status="review_needs_fix",
            )
        if reviewer_decision == "pass":
            return _presentation(
                "action_required",
                "review",
                "review pass pending",
                metadata=metadata,
                legacy_status="review_pass_pending",
            )
        if reviewer_decision == "failed_review":
            return _presentation("action_required", "error", "review failed", metadata=metadata, legacy_status="review_failed")
        return _presentation("action_required", "review", "awaiting review", metadata=metadata, legacy_status="awaiting_review")
    if review == "rejected":
        return _presentation("action_required", "fix", "review rejected", metadata=metadata, legacy_status="review_rejected")
    if review == "needs_followup":
        return _presentation("pending", "followup", "needs follow-up", metadata=metadata, legacy_status="needs_followup")
    if review == "reviewing":
        return _presentation("action_required", "review", "reviewing", metadata=metadata, legacy_status="reviewing")
    if review == "accepted" and _accepted_worktree_not_applied(task):
        return _presentation("pending", "apply", "accepted; not applied", metadata=metadata, legacy_status="accepted_unapplied")
    return _presentation("closed", "success", "completed", metadata=metadata, legacy_status="completed")


def _subtask_presentation(
    task: dict,
    by_id: dict[str, dict],
    config: Config | None,
    metadata: dict[str, Any],
) -> TaskListPresentation | None:
    active = _active_blocking_subtasks(task, by_id)
    if not active:
        return None
    statuses = [_blocking_subtask_status(item, by_id, config) for item in active]
    blocked_statuses = {
        "missing",
        "failed",
        "blocked_user",
        "discarded",
        "review_failed",
        "review_rejected",
        "needs_followup",
        "review_needs_fix",
        "subtasks_blocked",
    }
    blockers = [
        {"type": "subtask", "id": str(item.get("id") if item else ""), "status": status}
        for item, status in zip(active, statuses)
    ]
    if any(status in blocked_statuses for status in statuses):
        return _presentation(
            "action_required",
            "fix",
            "subtasks blocked",
            blockers=blockers,
            metadata=metadata,
            legacy_status="subtasks_blocked",
        )
    return _presentation(
        "pending",
        "followup",
        "waiting on subtasks",
        blockers=blockers,
        metadata=metadata,
        legacy_status="waiting_subtasks",
    )


def _dependency_blockers(task: dict, by_id: dict[str, dict], config: Config | None) -> list[dict[str, str]]:
    if config is None:
        return []
    return [
        {"type": "dependency", **item}
        for item in dependency_blockers(
            task,
            by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )
    ]


def _presentation(
    phase: str,
    kind: str,
    detail: str,
    *,
    blockers: list[dict[str, str]] | None = None,
    metadata: dict[str, Any] | None = None,
    legacy_status: str,
) -> TaskListPresentation:
    return TaskListPresentation(
        phase=phase,
        kind=kind,
        status_label=f"{PHASE_MARKERS[phase]}{kind}",
        detail=detail,
        blockers=blockers or [],
        metadata=metadata or {},
        legacy_status=legacy_status,
    )


def _review_status(task: dict) -> str:
    if task.get("status") == "completed":
        return str(task.get("review_status") or "unreviewed")
    return str(task.get("review_status") or "")


def _pending_reviewer_decision(task: dict) -> str:
    if task.get("status") != "completed" or _review_status(task) != "unreviewed":
        return ""
    reviewer = task.get("reviewer_codex") if isinstance(task.get("reviewer_codex"), dict) else {}
    decision = str(reviewer.get("decision") or task.get("last_review_decision") or "")
    chain_status = str(task.get("chain_status") or "")
    if decision == "needs_fix" or chain_status == "needs_fix":
        return "needs_fix"
    if decision == "pass":
        return "pass"
    if decision == "failed_review":
        return "failed_review"
    return ""


def _accepted_worktree_not_applied(task: dict) -> bool:
    return (
        task.get("execution_mode") == "git_worktree"
        and _review_status(task) == "accepted"
        and task.get("execution_apply_status") != "applied"
    )


def _active_blocking_subtasks(task: dict, by_id: dict[str, dict]) -> list[dict | None]:
    active = []
    for task_id in _blocking_subtask_ids(task):
        subtask = by_id.get(task_id)
        if subtask and subtask.get("status") == "completed" and _review_status(subtask) == "accepted":
            continue
        active.append(subtask)
    return active


def _blocking_subtask_ids(task: dict) -> list[str]:
    ids = task.get("blocking_subtask_ids")
    if not isinstance(ids, list):
        return []
    return [str(item) for item in ids if str(item)]


def _blocking_subtask_status(task: dict | None, by_id: dict[str, dict], config: Config | None) -> str:
    if not task:
        return "missing"
    if config is None:
        return str(task.get("status") or "-")
    return task_list_presentation(task, by_id, config, include_subtasks=False).legacy_status


def _safe_metadata(task: dict) -> dict[str, Any]:
    keys = (
        "status",
        "review_status",
        "chain_status",
        "last_review_decision",
        "resolution",
        "execution_mode",
        "execution_apply_status",
        "execution_rebase_status",
        "execution_conflict_fix_status",
        "cooldown_until",
        "capacity_pool",
        "task_priority",
    )
    return {key: task.get(key) for key in keys if task.get(key) not in (None, "", [], {})}

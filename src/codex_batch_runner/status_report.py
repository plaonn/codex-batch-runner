from __future__ import annotations

from collections import Counter
from typing import Any

from .config import Config
from .cooldown import cooldown_status
from .dashboard_data import retained_tasks_readonly
from .lock import lock_status
from .queue import (
    RUNNABLE_STATUSES,
    capacity_blockers,
    dependency_status,
    is_in_cooldown,
    task_project_id,
)
from .routing_report import render_table
from .state import get_runner_pause, load_state
from .timeutil import utc_now


def build_status_report(config: Config) -> dict[str, Any]:
    warnings: list[str] = []
    tasks = retained_tasks_readonly(config, warnings)
    state = load_state(config)
    global_cooldown = _cooldown_entry(state.get("global_cooldown_until"), "global_cooldown_until")
    reviewer_cooldown = _cooldown_entry(
        state.get("reviewer_codex_cooldown_until"),
        "reviewer_codex_cooldown_until",
    )
    pause = get_runner_pause(config)
    lock = lock_status(config.lock_file, config.stale_lock_seconds)
    task_entries = _task_selection_entries(config, tasks)
    admissible = [entry for entry in task_entries if entry["admissible"]]
    review_backlog = [task for task in tasks if _needs_review(task)]
    accepted_unapplied = [task for task in tasks if _accepted_unapplied(task)]
    active_lock = bool(lock.get("exists") and not lock.get("stale"))
    can_enqueue = not bool(pause.get("active"))
    can_run_next = can_enqueue and not bool(global_cooldown.get("active")) and not active_lock
    admission_status = _admission_status(pause, global_cooldown, active_lock)
    return {
        "kind": "cbr_status",
        "generated_at": utc_now().isoformat(),
        "read_only": True,
        "mutation_allowed": False,
        "warnings": warnings,
        "admission": {
            "status": admission_status,
            "can_enqueue": can_enqueue,
            "can_run_next": can_run_next,
            "can_start_cbr_execution": can_run_next,
            "recommended_action": _recommended_action(
                admission_status,
                runnable_count=len(admissible),
                review_backlog_count=len(review_backlog),
                accepted_unapplied_count=len(accepted_unapplied),
            ),
            "blocked_reasons": _blocked_reasons(pause, global_cooldown, active_lock),
        },
        "pause": pause,
        "cooldowns": {
            "global": global_cooldown,
            "reviewer_codex": reviewer_cooldown,
        },
        "lock": lock,
        "queue": {
            "task_count": len(tasks),
            "by_status": dict(sorted(Counter(str(task.get("status") or "unknown") for task in tasks).items())),
            "runnable_status_count": sum(1 for task in tasks if task.get("status") in RUNNABLE_STATUSES),
            "admissible_count": len(admissible),
            "blocked_runnable_count": sum(
                1
                for entry in task_entries
                if entry["task_status"] in RUNNABLE_STATUSES and not entry["admissible"]
            ),
            "blocked_reasons": dict(
                sorted(
                    Counter(
                        reason
                        for entry in task_entries
                        if entry["task_status"] in RUNNABLE_STATUSES
                        for reason in entry.get("reasons", [])
                    ).items()
                )
            ),
            "capacity": _capacity_summary(config, tasks),
        },
        "review": {
            "needs_review_count": len(review_backlog),
            "accepted_unapplied_count": len(accepted_unapplied),
            "reviewer_codex_available": not bool(reviewer_cooldown.get("active")),
        },
    }


def render_status_report(report: dict[str, Any]) -> str:
    admission = report.get("admission") if isinstance(report.get("admission"), dict) else {}
    queue = report.get("queue") if isinstance(report.get("queue"), dict) else {}
    review = report.get("review") if isinstance(report.get("review"), dict) else {}
    cooldowns = report.get("cooldowns") if isinstance(report.get("cooldowns"), dict) else {}
    global_cooldown = cooldowns.get("global") if isinstance(cooldowns.get("global"), dict) else {}
    reviewer_cooldown = cooldowns.get("reviewer_codex") if isinstance(cooldowns.get("reviewer_codex"), dict) else {}
    lock = report.get("lock") if isinstance(report.get("lock"), dict) else {}
    capacity = queue.get("capacity") if isinstance(queue.get("capacity"), dict) else {}
    capacity_pools = capacity.get("capacity_pools") if isinstance(capacity.get("capacity_pools"), dict) else {}
    rows = [
        ["admission", str(admission.get("status") or "-"), str(admission.get("recommended_action") or "-")],
        ["can_enqueue", _yes_no(admission.get("can_enqueue")), "-"],
        ["can_run_next", _yes_no(admission.get("can_run_next")), "-"],
        [
            "global_cooldown",
            _active(global_cooldown),
            str(global_cooldown.get("global_cooldown_until") or "-"),
        ],
        [
            "reviewer_cooldown",
            _active(reviewer_cooldown),
            str(reviewer_cooldown.get("reviewer_codex_cooldown_until") or "-"),
        ],
        ["lock", "active" if lock.get("exists") and not lock.get("stale") else "clear", str(lock.get("path") or "-")],
        ["queue", f"tasks={queue.get('task_count', 0)}", f"admissible={queue.get('admissible_count', 0)}"],
        [
            "review",
            f"needs_review={review.get('needs_review_count', 0)}",
            f"accepted_unapplied={review.get('accepted_unapplied_count', 0)}",
        ],
    ]
    lines = [
        "# cbr status",
        "",
        "read_only: yes",
        "mutation_allowed: no",
        "",
        render_table(["SECTION", "STATUS", "DETAIL"], rows),
    ]
    if capacity_pools:
        pool_rows = []
        for name, raw_pool in capacity_pools.items():
            pool = raw_pool if isinstance(raw_pool, dict) else {}
            blocker_reason = pool.get("blocker_reason")
            pool_rows.append(
                [
                    str(name),
                    f"max={pool.get('max_running', 0)}",
                    f"running={pool.get('running', 0)}",
                    f"remaining={pool.get('remaining', 0)}",
                    str(blocker_reason or "available"),
                ]
            )
        lines.extend(["", "## capacity pools", "", render_table(
            ["POOL", "MAX", "RUNNING", "REMAINING", "BLOCKER"],
            pool_rows,
        )])
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    if warnings:
        lines.extend(["", "warnings:"])
        lines.extend(f"  - {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def _task_selection_entries(config: Config, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {task.get("id"): task for task in tasks}
    entries = []
    for task in tasks:
        reasons: list[str] = []
        if task.get("status") not in RUNNABLE_STATUSES:
            reasons.append("not_runnable_status")
        if is_in_cooldown(task):
            reasons.append("cooldown")
        deps_ready, _ = dependency_status(
            task,
            by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )
        if not deps_ready:
            reasons.append("dependency")
        if not reasons:
            reasons.extend(capacity_blockers(config, task, _running_capacity(tasks)))
        entries.append(
            {
                "task_id": task.get("id"),
                "task_status": task.get("status"),
                "project_id": task_project_id(task),
                "admissible": not reasons,
                "reasons": reasons,
            }
        )
    return entries


def _capacity_summary(config: Config, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    running = _running_capacity(tasks)
    running_by_pool = running["by_pool"]
    capacity_pools = {}
    for name, pool in sorted(config.capacity_pools.items()):
        max_running = int(pool["max_running"])
        running_count = int(running_by_pool[name]) if isinstance(running_by_pool, Counter) else 0
        remaining = max(0, max_running - running_count)
        blocked = remaining == 0
        capacity_pools[name] = {
            "max_running": max_running,
            "running": running_count,
            "remaining": remaining,
            "blocked": blocked,
            "blocker_reason": "capacity_pool_full" if blocked else None,
        }
    return {
        "max_total_running": config.max_total_running,
        "max_running_per_project": config.max_running_per_project,
        "capacity_pools": capacity_pools,
        "running_total": running["total"],
        "running_by_project": dict(sorted(running["by_project"].items())),
        "running_by_pool": dict(sorted(running_by_pool.items())),
    }


def _running_capacity(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    running = [task for task in tasks if task.get("status") == "running"]
    return {
        "total": len(running),
        "by_project": Counter(task_project_id(task) for task in running),
        "by_pool": Counter(str(task.get("capacity_pool") or "codex") for task in running),
    }


def _admission_status(pause: dict[str, Any], global_cooldown: dict[str, Any], active_lock: bool) -> str:
    if pause.get("active"):
        return "paused"
    if global_cooldown.get("active"):
        return "cooldown"
    if active_lock:
        return "locked"
    return "available"


def _blocked_reasons(pause: dict[str, Any], global_cooldown: dict[str, Any], active_lock: bool) -> list[str]:
    reasons = []
    if pause.get("active"):
        reasons.append("runner_pause")
    if global_cooldown.get("active"):
        reasons.append("global_cooldown")
    if active_lock:
        reasons.append("runner_lock")
    return reasons


def _cooldown_entry(value: object, until_key: str) -> dict[str, Any]:
    status = cooldown_status(str(value) if value else None)
    until = status.pop("global_cooldown_until", None)
    status[until_key] = until
    return status


def _recommended_action(
    admission_status: str,
    *,
    runnable_count: int,
    review_backlog_count: int,
    accepted_unapplied_count: int,
) -> str:
    if admission_status == "paused":
        return "do_not_enqueue_or_run"
    if admission_status == "cooldown":
        return "do_not_start_runner"
    if admission_status == "locked":
        return "wait_for_runner_lock"
    if accepted_unapplied_count:
        return "apply_accepted_worktree_tasks"
    if review_backlog_count:
        return "review_completed_tasks"
    if runnable_count:
        return "run_next_available"
    return "idle"


def _needs_review(task: dict[str, Any]) -> bool:
    return (
        task.get("status") == "completed"
        and not task.get("resolution")
        and not _rejected_discarded_result(task)
        and _review_status(task) in {"unreviewed", "rejected", "needs_followup"}
    )


def _accepted_unapplied(task: dict[str, Any]) -> bool:
    return (
        task.get("status") == "completed"
        and task.get("execution_mode") == "git_worktree"
        and _review_status(task) == "accepted"
        and task.get("execution_apply_status") != "applied"
    )


def _review_status(task: dict[str, Any]) -> str:
    if task.get("status") == "completed":
        return str(task.get("review_status") or "unreviewed")
    return str(task.get("review_status") or "")


def _rejected_discarded_result(task: dict[str, Any]) -> bool:
    return (
        task.get("status") in {"completed", "archived"}
        and task.get("review_status") == "rejected"
        and task.get("execution_mode") == "git_worktree"
        and task.get("execution_worktree_status") == "cleaned"
        and task.get("execution_cleanup_kind") == "discard"
        and task.get("execution_cleanup_result_applied") is False
    )


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def _active(status: dict[str, Any]) -> str:
    return "active" if status.get("active") else "inactive"

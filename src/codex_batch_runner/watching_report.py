from __future__ import annotations

from collections import Counter
from typing import Any

from .config import Config
from .decision_cards import build_decision_card_inventory
from .evidence import rate_limit_dir
from .fs import read_json
from .queue import discarded_review_result, list_tasks, task_labels, task_project_id, task_project_root
from .routing_report import DEFAULT_ROUTING_REPORT_LIMIT, render_table
from .state import get_runner_pause, load_state
from .timeutil import parse_time, utc_now


WATCHING_STATUSES = {
    "action_required",
    "ready_for_close_review",
    "continue_observing",
    "no_evidence",
}


def build_watching_evidence_report(
    config: Config,
    *,
    project_id: str | None = None,
    project_root: str | None = None,
    category: str | None = None,
    label: str | None = None,
    include_archived: bool = False,
    limit: int = DEFAULT_ROUTING_REPORT_LIMIT,
    execution_evidence_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    tasks = _filter_tasks(
        list_tasks(config),
        project_id=project_id,
        project_root=project_root,
        category=category,
        label=label,
        include_archived=include_archived,
    )
    decision_cards = build_decision_card_inventory(
        config,
        project_id=project_id,
        project_root=project_root,
        category=category,
        label=label,
        limit=limit,
        include_archived=include_archived,
        execution_evidence_records=execution_evidence_records,
        include_observations=True,
    )
    areas = [
        _queue_execution_area(tasks),
        _review_apply_area(tasks),
        _worktree_lifecycle_area(tasks),
        _cooldown_rate_limits_area(config),
        _routing_policy_area(decision_cards),
    ]
    status_counts = Counter(str(area.get("evidence_status") or "unknown") for area in areas)
    return {
        "kind": "watching_evidence_report",
        "generated_at": utc_now().isoformat(),
        "read_only": True,
        "mutation_allowed": False,
        "filters": {
            "project_id": project_id,
            "project_root": project_root,
            "category": category,
            "label": label,
            "include_archived": include_archived,
            "limit": limit,
        },
        "summary": {
            "area_count": len(areas),
            "action_required": status_counts.get("action_required", 0),
            "ready_for_close_review": status_counts.get("ready_for_close_review", 0),
            "continue_observing": status_counts.get("continue_observing", 0),
            "no_evidence": status_counts.get("no_evidence", 0),
            "next_action": watching_next_action(areas),
            "by_status": dict(sorted(status_counts.items())),
        },
        "areas": areas,
        "source_reports": [
            {
                "source": "decision-cards",
                "kind": decision_cards.get("kind"),
                "generated_at": decision_cards.get("generated_at"),
                "read_only": True,
                "mutation_allowed": False,
                "summary": decision_cards.get("summary"),
            }
        ],
    }


def watching_next_action(areas: list[dict[str, Any]]) -> str:
    statuses = {str(area.get("evidence_status") or "unknown") for area in areas}
    if "action_required" in statuses:
        return "resolve_action_required"
    if "ready_for_close_review" in statuses:
        return "review_close_candidates"
    if "continue_observing" in statuses:
        return "continue_observing"
    return "none"


def render_watching_evidence_report(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# watching evidence",
        "",
        "read_only: yes",
        "mutation_allowed: no",
        (
            "summary: "
            f"areas={summary.get('area_count', 0)} "
            f"action_required={summary.get('action_required', 0)} "
            f"ready_for_close_review={summary.get('ready_for_close_review', 0)} "
            f"continue_observing={summary.get('continue_observing', 0)} "
            f"no_evidence={summary.get('no_evidence', 0)}"
        ),
        f"next_action: {summary.get('next_action') or 'none'}",
        "",
        render_watching_area_table(_list_value(report.get("areas"))),
    ]
    return "\n".join(lines) + "\n"


def render_watching_area_table(areas: list[dict[str, Any]]) -> str:
    header = ["AREA", "STATUS", "NEXT_ACTION", "EVIDENCE"]
    rows = [
        [
            str(area.get("area") or "-"),
            str(area.get("evidence_status") or "-"),
            str(area.get("operator_next_action") or "-"),
            "; ".join(str(signal) for signal in _list_value(area.get("signals"))) or "-",
        ]
        for area in areas
    ]
    return render_table(header, rows)


def _filter_tasks(
    tasks: list[dict[str, Any]],
    *,
    project_id: str | None,
    project_root: str | None,
    category: str | None,
    label: str | None,
    include_archived: bool,
) -> list[dict[str, Any]]:
    filtered = list(tasks)
    if project_id:
        filtered = [task for task in filtered if task_project_id(task) == project_id]
    if project_root:
        filtered = [task for task in filtered if task_project_root(task) == project_root]
    if category:
        filtered = [task for task in filtered if task.get("category") == category]
    if label:
        filtered = [task for task in filtered if label in task_labels(task)]
    if not include_archived:
        filtered = [task for task in filtered if task.get("status") != "archived"]
    return filtered


def _queue_execution_area(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    by_status = Counter(str(task.get("status") or "unknown") for task in tasks)
    active = sum(by_status.get(status, 0) for status in ("runnable", "needs_resume", "running", "cooldown"))
    blockers = sum(
        1
        for task in tasks
        if task.get("status") in {"failed", "blocked_user", "usage_exhausted"} and not task.get("resolution")
    )
    completed = by_status.get("completed", 0)
    if blockers:
        status = "action_required"
        action = "resolve_failed_or_blocked_tasks"
    elif active:
        status = "continue_observing"
        action = "wait_for_active_queue_work"
    elif tasks:
        status = "ready_for_close_review"
        action = "review_queue_execution_watching_item"
    else:
        status = "no_evidence"
        action = "collect_cbr_task_runs"
    return _area(
        "queue_execution",
        status,
        action,
        [
            f"tasks={len(tasks)}",
            f"completed={completed}",
            f"active={active}",
            f"failed_or_blocked={blockers}",
        ],
        {"by_status": dict(sorted(by_status.items()))},
    )


def _review_apply_area(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    backlog = [task for task in tasks if _review_backlog(task)]
    accepted_unapplied = [task for task in tasks if _accepted_unapplied(task)]
    accepted = [task for task in tasks if task.get("status") == "completed" and _review_status(task) == "accepted"]
    if backlog or accepted_unapplied:
        status = "action_required"
        action = "review_or_apply_completed_tasks"
    elif accepted:
        status = "ready_for_close_review"
        action = "review_review_apply_watching_item"
    else:
        status = "no_evidence"
        action = "collect_reviewed_task_runs"
    return _area(
        "review_apply",
        status,
        action,
        [
            f"accepted={len(accepted)}",
            f"review_backlog={len(backlog)}",
            f"accepted_unapplied={len(accepted_unapplied)}",
        ],
        {
            "review_backlog_task_ids": _task_ids(backlog),
            "accepted_unapplied_task_ids": _task_ids(accepted_unapplied),
        },
    )


def _worktree_lifecycle_area(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    worktree_tasks = [task for task in tasks if task.get("execution_mode") == "git_worktree"]
    accepted_unapplied = [task for task in worktree_tasks if _accepted_unapplied(task)]
    recovery_required = [
        task
        for task in worktree_tasks
        if str(task.get("execution_worktree_status") or "") == "recovery_required"
    ]
    applied_or_cleaned = [
        task
        for task in worktree_tasks
        if task.get("execution_apply_status") == "applied" or task.get("execution_worktree_status") == "cleaned"
    ]
    if accepted_unapplied or recovery_required:
        status = "action_required"
        action = "apply_or_recover_worktree_tasks"
    elif applied_or_cleaned:
        status = "ready_for_close_review"
        action = "review_worktree_lifecycle_watching_item"
    elif worktree_tasks:
        status = "continue_observing"
        action = "wait_for_worktree_review_or_apply"
    else:
        status = "no_evidence"
        action = "collect_worktree_task_runs"
    return _area(
        "worktree_lifecycle",
        status,
        action,
        [
            f"worktree_tasks={len(worktree_tasks)}",
            f"applied_or_cleaned={len(applied_or_cleaned)}",
            f"accepted_unapplied={len(accepted_unapplied)}",
            f"recovery_required={len(recovery_required)}",
        ],
        {
            "accepted_unapplied_task_ids": _task_ids(accepted_unapplied),
            "recovery_required_task_ids": _task_ids(recovery_required),
        },
    )


def _cooldown_rate_limits_area(config: Config) -> dict[str, Any]:
    state = load_state(config)
    now = utc_now()
    global_until = parse_time(state.get("global_cooldown_until"))
    reviewer_until = parse_time(state.get("reviewer_codex_cooldown_until"))
    global_active = bool(global_until and global_until > now)
    reviewer_active = bool(reviewer_until and reviewer_until > now)
    pause = get_runner_pause(config)
    rate_limits = _list_rate_limit_evidence_readonly(config)
    if pause.get("active") or global_active or reviewer_active:
        status = "action_required"
        action = "wait_or_clear_pause_and_cooldowns"
    elif rate_limits:
        status = "ready_for_close_review"
        action = "review_cooldown_rate_limit_watching_item"
    else:
        status = "no_evidence"
        action = "collect_natural_rate_limit_evidence"
    return _area(
        "cooldown_rate_limits",
        status,
        action,
        [
            f"rate_limit_events={len(rate_limits)}",
            f"global_cooldown_active={str(global_active).lower()}",
            f"reviewer_cooldown_active={str(reviewer_active).lower()}",
            f"runner_pause_active={str(bool(pause.get('active'))).lower()}",
        ],
        {
            "global_cooldown_until": state.get("global_cooldown_until"),
            "reviewer_codex_cooldown_until": state.get("reviewer_codex_cooldown_until"),
            "runner_pause": pause,
        },
    )


def _routing_policy_area(decision_cards: dict[str, Any]) -> dict[str, Any]:
    summary = decision_cards.get("summary") if isinstance(decision_cards.get("summary"), dict) else {}
    next_action = str(summary.get("next_action") or "none")
    card_count = int(summary.get("card_count") or 0)
    if next_action in {"fix_invalid_decision_cards", "review_decision_cards"}:
        status = "action_required"
        action = next_action
    elif next_action == "continue_observing":
        status = "continue_observing"
        action = "collect_more_routing_evidence"
    elif card_count:
        status = "ready_for_close_review"
        action = "review_routing_policy_watching_item"
    else:
        status = "no_evidence"
        action = "collect_routing_policy_evidence"
    return _area(
        "routing_policy",
        status,
        action,
        [
            f"cards={card_count}",
            f"decision_required={int(summary.get('decision_required') or 0)}",
            f"not_ready={int(summary.get('not_ready') or 0)}",
            f"next_action={next_action}",
        ],
        {"decision_card_summary": summary},
    )


def _area(
    name: str,
    evidence_status: str,
    operator_next_action: str,
    signals: list[str],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if evidence_status not in WATCHING_STATUSES:
        raise ValueError(f"invalid watching evidence status: {evidence_status}")
    return {
        "area": name,
        "evidence_status": evidence_status,
        "operator_next_action": operator_next_action,
        "signals": signals,
        "evidence": evidence,
    }


def _review_status(task: dict[str, Any]) -> str:
    if task.get("status") == "completed":
        return str(task.get("review_status") or "unreviewed")
    return str(task.get("review_status") or "")


def _review_backlog(task: dict[str, Any]) -> bool:
    return (
        task.get("status") == "completed"
        and not task.get("resolution")
        and not discarded_review_result(task)
        and _review_status(task) in {"unreviewed", "rejected", "needs_followup", "reviewing"}
    )


def _accepted_unapplied(task: dict[str, Any]) -> bool:
    return (
        task.get("status") == "completed"
        and task.get("execution_mode") == "git_worktree"
        and _review_status(task) == "accepted"
        and task.get("execution_apply_status") != "applied"
    )


def _task_ids(tasks: list[dict[str, Any]]) -> list[str]:
    return [str(task.get("id") or "") for task in tasks if task.get("id")]


def _list_rate_limit_evidence_readonly(config: Config) -> list[dict[str, Any]]:
    directory = rate_limit_dir(config)
    if not directory.exists():
        return []
    events = []
    for path in sorted(directory.glob("*.json")):
        event = read_json(path)
        if isinstance(event, dict):
            events.append(event)
    events.sort(key=lambda item: (item.get("detected_at") or "", item.get("task_id") or "", item.get("attempt") or 0))
    return events


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []

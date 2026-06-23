from __future__ import annotations

from collections import defaultdict
from typing import Any

from .config import Config
from .queue import list_tasks, task_labels, task_project_id, task_project_root
from .timeutil import iso_now
from .transcript import sanitize

DEFAULT_ROUTING_REPORT_LIMIT = 50


def build_routing_report(
    config: Config,
    *,
    project_id: str | None = None,
    project_root: str | None = None,
    category: str | None = None,
    label: str | None = None,
    limit: int = DEFAULT_ROUTING_REPORT_LIMIT,
    include_archived: bool = False,
) -> dict[str, Any]:
    tasks = list_tasks(config)
    total_available = len(tasks)
    tasks = filter_tasks(
        tasks,
        project_id=project_id,
        project_root=project_root,
        category=category,
        label=label,
        include_archived=include_archived,
    )
    filtered_count = len(tasks)
    tasks.sort(key=lambda task: (str(task.get("created_at") or ""), str(task.get("id") or "")), reverse=True)
    if limit > 0:
        tasks = tasks[:limit]
    rows = [task_routing_row(task) for task in tasks]
    return {
        "generated_at": iso_now(),
        "filters": {
            "project": project_id,
            "project_root": project_root,
            "category": category,
            "label": label,
            "include_archived": include_archived,
            "limit": limit,
        },
        "total_available": total_available,
        "filtered_count": filtered_count,
        "task_count": len(rows),
        "task_rows": rows,
        "groups": {
            "profile": summarize_groups(group_rows(rows, "profile")),
            "category": summarize_groups(group_rows(rows, "category")),
            "label": summarize_groups(group_rows_by_label(rows)),
            "profile_category": summarize_groups(group_rows(rows, "profile_category")),
            "routing_experiment": summarize_groups(group_rows(rows, "routing_experiment")),
            "routing_size": summarize_groups(group_rows(rows, "routing_size")),
            "routing_risk": summarize_groups(group_rows(rows, "routing_risk")),
            "routing_risk_factor": summarize_groups(group_rows_by_risk_factor(rows)),
            "verification_scope": summarize_groups(group_rows_by_verification_scope(rows)),
            "profile_experiment": summarize_groups(group_rows(rows, "profile_experiment")),
        },
    }


def filter_tasks(
    tasks: list[dict[str, Any]],
    *,
    project_id: str | None,
    project_root: str | None,
    category: str | None,
    label: str | None,
    include_archived: bool,
) -> list[dict[str, Any]]:
    selected = tasks
    if not include_archived:
        selected = [task for task in selected if task.get("status") != "archived"]
    if project_id:
        selected = [task for task in selected if task_project_id(task) == project_id]
    if project_root:
        selected = [task for task in selected if task_project_root(task) == project_root]
    if category:
        selected = [task for task in selected if task.get("category") == category]
    if label:
        selected = [task for task in selected if label in task_labels(task)]
    return selected


def task_routing_row(task: dict[str, Any]) -> dict[str, Any]:
    profile = str(task.get("execution_profile") or "default")
    category = str(task.get("category") or "uncategorized")
    routing_experiment = str(task.get("routing_experiment") or "unspecified")
    routing_size = str(task.get("routing_size") or "unspecified")
    routing_risk = str(task.get("routing_risk") or "unspecified")
    reviewer = task.get("reviewer_codex") if isinstance(task.get("reviewer_codex"), dict) else {}
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else {}
    duration = number(last_run.get("duration_seconds"))
    attempts = int(task.get("attempts") or 0)
    review_status = completed_review_status(task)
    reviewer_decision = str(reviewer.get("decision") or task.get("last_review_decision") or "")
    return {
        "id": task.get("id"),
        "profile": profile,
        "category": category,
        "profile_category": f"{profile}/{category}",
        "labels": task_labels(task) or ["unlabeled"],
        "routing_reason": sanitize(task.get("routing_reason")) if task.get("routing_reason") else "",
        "routing_risk_factors": routing_risk_factors(task),
        "routing_experiment": sanitize(routing_experiment),
        "routing_size": sanitize(routing_size),
        "routing_risk": sanitize(routing_risk),
        "verification_scope": verification_scope(task),
        "profile_experiment": f"{profile}/{sanitize(routing_experiment)}",
        "status": str(task.get("status") or ""),
        "review_status": review_status,
        "reviewer_decision": reviewer_decision,
        "reviewer_confidence": str(reviewer.get("confidence") or ""),
        "attempts": attempts,
        "run_count": int(task.get("run_count") or 0),
        "duration_seconds": duration,
        "fix_attempts": int(task.get("fix_attempts") or 0),
        "is_auto_fix_task": task.get("subtask_type") == "auto_review_fix",
        "has_auto_fix": bool(task.get("last_auto_fix_task_id") or int(task.get("fix_attempts") or 0)),
    }


def completed_review_status(task: dict[str, Any]) -> str:
    if task.get("status") != "completed":
        return ""
    return str(task.get("review_status") or "unreviewed")


def number(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def group_rows(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "-")].append(row)
    return groups


def group_rows_by_label(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        labels = row.get("labels") if isinstance(row.get("labels"), list) else ["unlabeled"]
        for label in labels or ["unlabeled"]:
            groups[str(label)].append(row)
    return groups


def group_rows_by_risk_factor(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        factors = row.get("routing_risk_factors") if isinstance(row.get("routing_risk_factors"), list) else ["none"]
        for factor in factors or ["none"]:
            groups[str(factor)].append(row)
    return groups


def group_rows_by_verification_scope(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        scopes = row.get("verification_scope") if isinstance(row.get("verification_scope"), list) else ["none"]
        for scope in scopes or ["none"]:
            groups[str(scope)].append(row)
    return groups


def routing_risk_factors(task: dict[str, Any]) -> list[str]:
    factors = task.get("routing_risk_factors")
    if not isinstance(factors, list):
        return ["none"]
    cleaned = [sanitize(item) for item in factors if str(item).strip()]
    return cleaned or ["none"]


def verification_scope(task: dict[str, Any]) -> list[str]:
    scopes = task.get("verification_scope")
    if not isinstance(scopes, list):
        return ["none"]
    cleaned = [sanitize(item) for item in scopes if str(item).strip()]
    return cleaned or ["none"]


def summarize_groups(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [summarize_group(key, rows) for key, rows in sorted(groups.items())]


def summarize_group(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = len(rows)
    completed = count(rows, lambda row: row["status"] == "completed")
    accepted = count(rows, lambda row: row["review_status"] == "accepted")
    rejected = count(rows, lambda row: row["review_status"] == "rejected")
    needs_followup = count(rows, lambda row: row["review_status"] == "needs_followup")
    reviewer_needs_fix = count(rows, lambda row: row["reviewer_decision"] == "needs_fix")
    needs_fix_or_rejected = count(
        rows,
        lambda row: row["review_status"] in {"rejected", "needs_followup"} or row["reviewer_decision"] == "needs_fix",
    )
    first_pass_accepted = count(
        rows,
        lambda row: row["review_status"] == "accepted"
        and row["attempts"] <= 1
        and row["fix_attempts"] == 0
        and row["reviewer_decision"] not in {"needs_fix", "needs_human", "failed_review"},
    )
    duration_sum = sum(float(row["duration_seconds"]) for row in rows)
    attempts_sum = sum(int(row["attempts"]) for row in rows)
    run_count_sum = sum(int(row["run_count"]) for row in rows)
    return {
        "key": key,
        "tasks": tasks,
        "completed": completed,
        "accepted": accepted,
        "unreviewed": count(rows, lambda row: row["review_status"] == "unreviewed"),
        "rejected": rejected,
        "needs_followup": needs_followup,
        "reviewer_pass": count(rows, lambda row: row["reviewer_decision"] == "pass"),
        "reviewer_needs_fix": reviewer_needs_fix,
        "reviewer_needs_human": count(rows, lambda row: row["reviewer_decision"] == "needs_human"),
        "reviewer_failed_review": count(rows, lambda row: row["reviewer_decision"] == "failed_review"),
        "first_pass_accepted": first_pass_accepted,
        "first_pass_accept_rate": ratio(first_pass_accepted, completed),
        "needs_fix_or_rejected": needs_fix_or_rejected,
        "needs_fix_or_rejected_rate": ratio(needs_fix_or_rejected, completed),
        "auto_fix_tasks": count(rows, lambda row: bool(row["is_auto_fix_task"])),
        "roots_with_auto_fix": count(rows, lambda row: bool(row["has_auto_fix"])),
        "attempts_sum": attempts_sum,
        "avg_attempts": ratio(attempts_sum, tasks),
        "duration_seconds_sum": round(duration_sum, 3),
        "avg_duration_seconds": round(ratio(duration_sum, tasks), 3),
        "run_count_sum": run_count_sum,
        "cost_proxy": {
            "attempts": attempts_sum,
            "runs": run_count_sum,
            "duration_seconds": round(duration_sum, 3),
        },
    }


def count(rows: list[dict[str, Any]], predicate: Any) -> int:
    return sum(1 for row in rows if predicate(row))


def ratio(numerator: float, denominator: float) -> float:
    if not denominator:
        return 0.0
    return round(numerator / denominator, 3)


def render_routing_report(report: dict[str, Any]) -> str:
    lines = [
        "# routing report",
        f"tasks: {report.get('task_count')} of {report.get('filtered_count')} filtered",
    ]
    filters = report.get("filters") if isinstance(report.get("filters"), dict) else {}
    active_filters = [
        f"{key}={value}"
        for key, value in filters.items()
        if value not in (None, "", False) and not (key == "limit" and value == DEFAULT_ROUTING_REPORT_LIMIT)
    ]
    if active_filters:
        lines.append("filters: " + " ".join(active_filters))
    groups = report.get("groups") if isinstance(report.get("groups"), dict) else {}
    for group_name in (
        "profile",
        "category",
        "label",
        "profile_category",
        "routing_experiment",
        "routing_size",
        "routing_risk",
        "routing_risk_factor",
        "verification_scope",
        "profile_experiment",
    ):
        entries = groups.get(group_name) if isinstance(groups.get(group_name), list) else []
        lines.append("")
        lines.append(f"## by_{group_name}")
        lines.append(render_group_table(entries))
    return "\n".join(lines) + "\n"


def render_group_table(entries: list[dict[str, Any]]) -> str:
    header = ["KEY", "TASKS", "DONE", "ACCEPT", "1PASS", "FIX/REJ", "AUTO_FIX", "AVG_ATT", "AVG_DUR"]
    rows = [
        [
            str(entry.get("key") or "-"),
            str(entry.get("tasks") or 0),
            str(entry.get("completed") or 0),
            str(entry.get("accepted") or 0),
            str(entry.get("first_pass_accepted") or 0),
            percent_cell(entry.get("needs_fix_or_rejected_rate")),
            str(entry.get("auto_fix_tasks") or 0),
            format_float(entry.get("avg_attempts")),
            format_duration_seconds(entry.get("avg_duration_seconds")),
        ]
        for entry in entries
    ]
    return render_table(header, rows)


def percent_cell(value: object) -> str:
    return f"{round(number(value) * 100):d}%"


def format_float(value: object) -> str:
    return f"{number(value):.2f}"


def format_duration_seconds(value: object) -> str:
    seconds = number(value)
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}m"


def render_table(header: list[str], rows: list[list[str]]) -> str:
    if not rows:
        rows = [["-" for _ in header]]
    widths = [max(len(row[index]) for row in [header, *rows]) for index in range(len(header))]
    return "\n".join(render_table_row(row, widths) for row in [header, *rows])


def render_table_row(row: list[str], widths: list[int]) -> str:
    padded = [cell.ljust(widths[index]) for index, cell in enumerate(row[:-1])]
    return "  ".join([*padded, row[-1]])

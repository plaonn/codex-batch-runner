from __future__ import annotations

from hashlib import sha256
from typing import Any

from .config import Config
from .evaluation import derive_evaluation_row
from .queue import list_tasks, task_labels, task_project_id, task_project_root
from .timeutil import iso_now

DEFAULT_ROUTING_EVAL_REPORT_LIMIT = 50


def build_routing_evaluation_report(
    config: Config,
    *,
    project_id: str | None = None,
    project_root: str | None = None,
    category: str | None = None,
    label: str | None = None,
    limit: int = DEFAULT_ROUTING_EVAL_REPORT_LIMIT,
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
    rows = [derive_evaluation_row(task) for task in tasks]
    return {
        "generated_at": iso_now(),
        "filters": {
            "project": project_id,
            "project_root_filter_applied": bool(project_root),
            "project_root_hash": safe_hash(project_root) if project_root else None,
            "category": category,
            "label": label,
            "include_archived": include_archived,
            "limit": limit,
        },
        "total_available": total_available,
        "filtered_count": filtered_count,
        "row_count": len(rows),
        "evaluation_rows": rows,
        "privacy": {
            "raw_prompts_included": False,
            "raw_transcripts_included": False,
            "raw_logs_included": False,
            "session_or_thread_ids_included": False,
            "raw_paths_included": False,
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


def safe_hash(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def render_routing_evaluation_report(report: dict[str, Any]) -> str:
    lines = [
        "# routing evaluation report",
        f"rows: {report.get('row_count')} of {report.get('filtered_count')} filtered",
    ]
    filters = report.get("filters") if isinstance(report.get("filters"), dict) else {}
    active_filters = [
        f"{key}={value}"
        for key, value in filters.items()
        if key not in {"project_root_filter_applied", "project_root_hash"}
        and value not in (None, "", False)
        and not (key == "limit" and value == DEFAULT_ROUTING_EVAL_REPORT_LIMIT)
    ]
    if filters.get("project_root_filter_applied"):
        active_filters.append("project_root=filtered")
    if active_filters:
        lines.append("filters: " + " ".join(active_filters))
    lines.append("")
    lines.append("JSON output includes row-level derived evaluation sections; use --json for inspection.")
    return "\n".join(lines) + "\n"

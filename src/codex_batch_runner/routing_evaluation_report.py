from __future__ import annotations

from hashlib import sha256
from typing import Any

from .config import Config
from .evaluation import derive_evaluation_row
from .execution_evidence import derive_execution_evidence_rows
from .queue import list_tasks, task_labels, task_project_id, task_project_root
from .routing_report import (
    evidence_cohort_key,
    evidence_contract_key,
    group_evaluation_rows,
    render_probe_lanes,
    summarize_evidence_cohort_cell,
    summarize_evaluation_groups,
    summarize_review_outcome_exclusions,
    summarize_review_outcome_strata,
    summarize_task_bucket,
)
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
    execution_evidence_records: list[dict[str, Any]] | None = None,
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
    execution_evidence_rows = derive_execution_evidence_rows(execution_evidence_records)
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
        "evaluation_diagnostics": {
            "probe_lanes": summarize_probe_lanes(rows),
            "evidence_contracts": summarize_evaluation_groups(
                group_evaluation_rows(rows, evidence_contract_key), summarize_evidence_cohort_cell
            ),
            "evidence_cohorts": summarize_evaluation_groups(
                group_evaluation_rows(rows, evidence_cohort_key), summarize_evidence_cohort_cell
            ),
            "review_outcome_strata": summarize_review_outcome_strata(rows),
            "review_outcome_exclusions": summarize_review_outcome_exclusions(rows),
            "advisory": {
                "read_only": True,
                "mutation_allowed": False,
            },
        },
        "execution_evidence_count": len(execution_evidence_rows),
        "execution_evidence_rows": execution_evidence_rows,
        "execution_evidence_diagnostics": {
            "probe_lanes": summarize_probe_lanes(execution_evidence_rows),
            "evidence_contracts": summarize_evaluation_groups(
                group_evaluation_rows(execution_evidence_rows, evidence_contract_key), summarize_evidence_cohort_cell
            ),
            "evidence_cohorts": summarize_evaluation_groups(
                group_evaluation_rows(execution_evidence_rows, evidence_cohort_key), summarize_evidence_cohort_cell
            ),
            "review_outcome_strata": summarize_review_outcome_strata(execution_evidence_rows),
            "review_outcome_exclusions": summarize_review_outcome_exclusions(execution_evidence_rows),
            "advisory": {
                "read_only": True,
                "mutation_allowed": False,
                "queue_rows_included": False,
            },
        },
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


def summarize_probe_lanes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "advisory": {
            "read_only": True,
            "mutation_allowed": False,
            "baseline_label": "baseline",
            "probe_lane_families": ["probe", "guard"],
        },
        "by_lane_family": summarize_evaluation_groups(
            group_evaluation_rows(rows, probe_lane_key),
            summarize_task_bucket,
        ),
        "by_experiment": summarize_evaluation_groups(
            group_evaluation_rows(rows, routing_experiment_key),
            summarize_task_bucket,
        ),
        "by_task_bucket_lane": summarize_evaluation_groups(
            group_evaluation_rows(rows, task_bucket_lane_key),
            summarize_task_bucket,
        ),
        "by_model_requirement_lane": summarize_evaluation_groups(
            group_evaluation_rows(rows, model_requirement_lane_key),
            summarize_task_bucket,
        ),
    }


def routing_experiment_key(row: dict[str, Any]) -> str:
    routing = row.get("routing") if isinstance(row.get("routing"), dict) else {}
    return str(routing.get("routing_experiment") or "unknown")


def probe_lane_key(row: dict[str, Any]) -> str:
    experiment = routing_experiment_key(row).lower()
    if experiment in {"", "unspecified", "unknown", "none"}:
        return "unspecified"
    if experiment == "baseline":
        return "baseline"
    if experiment == "manual":
        return "manual"
    if experiment.endswith("_probe") or "probe" in experiment:
        return "probe"
    if experiment.endswith("_guard") or "guard" in experiment:
        return "guard"
    return "other"


def task_bucket_lane_key(row: dict[str, Any]) -> str:
    return f"{row.get('task_bucket_key') or 'unknown'} lane={probe_lane_key(row)}"


def model_requirement_lane_key(row: dict[str, Any]) -> str:
    worker = row.get("worker") if isinstance(row.get("worker"), dict) else {}
    return f"{worker.get('model_requirement_key') or 'unknown'}/lane={probe_lane_key(row)}"


def render_routing_evaluation_report(report: dict[str, Any]) -> str:
    lines = [
        "# routing evaluation report",
        f"rows: {report.get('row_count')} of {report.get('filtered_count')} filtered",
    ]
    if int(report.get("execution_evidence_count") or 0):
        lines.append(f"execution_evidence_rows: {report.get('execution_evidence_count')}")
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
    diagnostics = report.get("evaluation_diagnostics") if isinstance(report.get("evaluation_diagnostics"), dict) else {}
    probe_lanes = diagnostics.get("probe_lanes") if isinstance(diagnostics.get("probe_lanes"), dict) else {}
    if probe_lanes:
        lines.append("")
        lines.append("probe_lanes")
        lines.append(render_probe_lanes(probe_lanes))
    lines.append("")
    lines.append("JSON output includes row-level derived evaluation sections; use --json for inspection.")
    return "\n".join(lines) + "\n"

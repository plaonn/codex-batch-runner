from __future__ import annotations

from collections import defaultdict
from typing import Any

from .config import Config
from .evaluation import derive_evaluation_row
from .execution_profiles import small_profile_routing_candidate
from .queue import list_tasks, task_labels, task_project_id, task_project_root
from .timeutil import iso_now
from .transcript import sanitize

DEFAULT_ROUTING_REPORT_LIMIT = 50
POLICY_REVIEW_CLEAN_SAMPLE_THRESHOLD = 3


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
    evaluation_rows = [derive_evaluation_row(task) for task in tasks]
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
            "resolved_profile": summarize_groups(group_rows(rows, "resolved_profile")),
            "category": summarize_groups(group_rows(rows, "category")),
            "label": summarize_groups(group_rows_by_label(rows)),
            "profile_category": summarize_groups(group_rows(rows, "profile_category")),
            "routing_experiment": summarize_groups(group_rows(rows, "routing_experiment")),
            "routing_size": summarize_groups(group_rows(rows, "routing_size")),
            "routing_risk": summarize_groups(group_rows(rows, "routing_risk")),
            "routing_risk_factor": summarize_groups(group_rows_by_risk_factor(rows)),
            "verification_scope": summarize_groups(group_rows_by_verification_scope(rows)),
            "routing_decision": summarize_groups(group_rows(rows, "routing_decision")),
            "profile_routing_decision": summarize_groups(group_rows(rows, "profile_routing_decision")),
            "resolved_profile_routing_decision": summarize_groups(
                group_rows(rows, "resolved_profile_routing_decision")
            ),
            "small_profile_candidate": summarize_groups(group_rows(rows, "small_profile_candidate")),
            "profile_experiment": summarize_groups(group_rows(rows, "profile_experiment")),
        },
        "evaluation_diagnostics": summarize_evaluation_diagnostics(evaluation_rows),
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
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else {}
    resolved_profile = str(last_run.get("execution_profile") or task.get("execution_profile") or "default")
    category = str(task.get("category") or "uncategorized")
    routing_experiment = str(task.get("routing_experiment") or "unspecified")
    routing_size = str(task.get("routing_size") or "unspecified")
    routing_risk = str(task.get("routing_risk") or "unspecified")
    scopes = verification_scope(task)
    decision_key = routing_decision_key(routing_size, routing_risk, scopes)
    reviewer = task.get("reviewer_codex") if isinstance(task.get("reviewer_codex"), dict) else {}
    duration = number(last_run.get("duration_seconds"))
    attempts = int(task.get("attempts") or 0)
    review_status = completed_review_status(task)
    reviewer_decision = str(reviewer.get("decision") or task.get("last_review_decision") or "")
    candidate = small_profile_candidate(task, profile=profile, resolved_profile=resolved_profile)
    return {
        "id": task.get("id"),
        "profile": profile,
        "resolved_profile": resolved_profile,
        "category": category,
        "profile_category": f"{profile}/{category}",
        "labels": task_labels(task) or ["unlabeled"],
        "routing_reason": sanitize(task.get("routing_reason")) if task.get("routing_reason") else "",
        "routing_risk_factors": routing_risk_factors(task),
        "routing_experiment": sanitize(routing_experiment),
        "routing_size": sanitize(routing_size),
        "routing_risk": sanitize(routing_risk),
        "verification_scope": scopes,
        "routing_decision": decision_key,
        "profile_routing_decision": f"profile={sanitize(profile)} {decision_key}",
        "resolved_profile_routing_decision": f"profile={sanitize(resolved_profile)} {decision_key}",
        "small_profile_candidate": "candidate" if candidate else "not_candidate",
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


def small_profile_candidate(
    task: dict[str, Any],
    *,
    profile: str,
    resolved_profile: str,
) -> bool:
    return (
        small_profile_routing_candidate(task)
        and sanitize(profile) != "small"
        and sanitize(resolved_profile) != "small"
    )


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


def routing_decision_key(routing_size: str, routing_risk: str, scopes: list[str]) -> str:
    scope_key = "+".join(sorted(scopes or ["none"]))
    return f"size={sanitize(routing_size)} risk={sanitize(routing_risk)} verify={scope_key}"


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


def summarize_evaluation_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "row_count": len(rows),
        "policy_usage": summarize_policy_usage(rows),
        "worker_cells": summarize_evaluation_groups(group_evaluation_rows(rows, worker_cell_key), summarize_worker_cell),
        "reviewer_cells": summarize_evaluation_groups(group_evaluation_rows(rows, reviewer_cell_key), summarize_reviewer_cell),
        "policy_exclusions": summarize_exclusion_reasons(rows),
        "task_buckets": summarize_evaluation_groups(group_evaluation_rows(rows, task_bucket_key), summarize_task_bucket),
        "advisory": {
            "policy_review_clean_sample_threshold": POLICY_REVIEW_CLEAN_SAMPLE_THRESHOLD,
            "read_only": True,
        },
    }


def summarize_policy_usage(rows: list[dict[str, Any]]) -> dict[str, int]:
    keys = (
        "usable_for_worker_policy",
        "usable_for_reviewer_calibration",
        "usable_for_task_vector_evaluation",
        "usable_for_quota_debugging",
    )
    return {key: count(rows, lambda row, key=key: bool(policy_usage(row).get(key))) for key in keys}


def group_evaluation_rows(
    rows: list[dict[str, Any]],
    key_fn: Any,
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[key_fn(row)].append(row)
    return groups


def summarize_evaluation_groups(groups: dict[str, list[dict[str, Any]]], summary_fn: Any) -> list[dict[str, Any]]:
    return [summary_fn(key, rows) for key, rows in sorted(groups.items())]


def summarize_worker_cell(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    accepted_pass = count(rows, is_accepted_pass_sample)
    usable_accepted_pass = count(rows, lambda row: is_accepted_pass_sample(row) and usable_for_worker_policy(row))
    return {
        "key": key,
        "tasks": len(rows),
        "completed": count(rows, lambda row: outcomes(row).get("worker_terminal_status") == "completed"),
        "failed": count(rows, lambda row: outcomes(row).get("worker_terminal_status") == "failed"),
        "needs_resume": count(rows, lambda row: outcomes(row).get("worker_terminal_status") == "needs_resume"),
        "blocked_user": count(rows, lambda row: outcomes(row).get("worker_terminal_status") == "blocked_user"),
        "accepted": count(rows, lambda row: bool(outcomes(row).get("accepted"))),
        "reviewer_pass": count(rows, lambda row: reviewer(row).get("reviewer_decision") == "pass"),
        "accepted_pass": accepted_pass,
        "usable_for_worker_policy": count(rows, usable_for_worker_policy),
        "usable_accepted_pass": usable_accepted_pass,
        "required_checks_passed": count(rows, required_checks_passed),
        "policy_clean_sample_rate": ratio(usable_accepted_pass, len(rows)),
    }


def summarize_reviewer_cell(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "key": key,
        "tasks": len(rows),
        "reviewer_present": count(rows, lambda row: bool(reviewer(row).get("reviewer_codex_present"))),
        "reviewer_pass": count(rows, lambda row: reviewer(row).get("reviewer_decision") == "pass"),
        "reviewer_needs_fix": count(rows, lambda row: reviewer(row).get("reviewer_decision") == "needs_fix"),
        "reviewer_needs_human": count(rows, lambda row: reviewer(row).get("reviewer_decision") == "needs_human"),
        "reviewer_failed_review": count(rows, lambda row: reviewer(row).get("reviewer_decision") == "failed_review"),
        "accepted": count(rows, lambda row: bool(outcomes(row).get("accepted"))),
        "rejected": count(rows, lambda row: bool(outcomes(row).get("rejected"))),
        "needs_followup": count(rows, lambda row: bool(outcomes(row).get("needs_followup"))),
        "error_findings": sum(int(reviewer(row).get("error_finding_count") or 0) for row in rows),
        "required_human_checks": sum(int(reviewer(row).get("required_human_check_count") or 0) for row in rows),
        "usable_for_reviewer_calibration": count(
            rows,
            lambda row: bool(policy_usage(row).get("usable_for_reviewer_calibration")),
        ),
    }


def summarize_exclusion_reasons(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for reason in policy_exclusion_reasons(row):
            groups[str(reason or "unknown")].append(row)
    return [
        {
            "key": key,
            "rows": len(group_rows),
            "worker_policy_excluded": count(group_rows, lambda row: not usable_for_worker_policy(row)),
            "reviewer_calibration_excluded": count(
                group_rows,
                lambda row: not bool(policy_usage(row).get("usable_for_reviewer_calibration")),
            ),
            "task_vector_evaluation_excluded": count(
                group_rows,
                lambda row: not bool(policy_usage(row).get("usable_for_task_vector_evaluation")),
            ),
        }
        for key, group_rows in sorted(groups.items())
    ]


def policy_exclusion_reasons(row: dict[str, Any]) -> list[str]:
    reasons = row.get("exclusion_reasons") if isinstance(row.get("exclusion_reasons"), list) else []
    cleaned = [str(reason) for reason in reasons if str(reason or "").strip()]
    if usable_for_worker_policy(row):
        return cleaned or ["none"]
    derived = list(cleaned)
    if not bool(policy_usage(row).get("usable_for_reviewer_calibration")):
        derived.append("reviewer_unusable")
    if not bool(objective_checks(row).get("final_json_available")):
        derived.append("objective_unavailable")
    if not derived:
        derived.append("worker_policy_unusable")
    return sorted(set(derived))


def summarize_task_bucket(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    clean_samples = count(rows, clean_worker_policy_sample)
    usable_samples = count(rows, usable_for_worker_policy)
    return {
        "key": key,
        "tasks": len(rows),
        "usable_for_worker_policy": usable_samples,
        "clean_samples": clean_samples,
        "accepted_pass_clean_samples": count(rows, lambda row: clean_worker_policy_sample(row) and is_accepted_pass_sample(row)),
        "worker_cells": sorted({worker_cell_key(row) for row in rows}),
        "reviewer_cells": sorted({reviewer_cell_key(row) for row in rows}),
        "policy_review_candidate": clean_samples >= POLICY_REVIEW_CLEAN_SAMPLE_THRESHOLD,
        "policy_review_note": "advisory_read_only" if clean_samples >= POLICY_REVIEW_CLEAN_SAMPLE_THRESHOLD else "",
    }


def worker_cell_key(row: dict[str, Any]) -> str:
    return str(worker(row).get("worker_cell_key") or "unknown")


def reviewer_cell_key(row: dict[str, Any]) -> str:
    return str(reviewer(row).get("reviewer_cell_key") or "unknown")


def task_bucket_key(row: dict[str, Any]) -> str:
    return str(row.get("task_bucket_key") or "unknown")


def worker(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("worker")
    return value if isinstance(value, dict) else {}


def reviewer(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("reviewer")
    return value if isinstance(value, dict) else {}


def outcomes(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("outcomes")
    return value if isinstance(value, dict) else {}


def objective_checks(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("objective_checks")
    return value if isinstance(value, dict) else {}


def policy_usage(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("policy_usage")
    return value if isinstance(value, dict) else {}


def usable_for_worker_policy(row: dict[str, Any]) -> bool:
    return bool(policy_usage(row).get("usable_for_worker_policy"))


def required_checks_passed(row: dict[str, Any]) -> bool:
    return bool(objective_checks(row).get("required_checks_passed"))


def is_accepted_pass_sample(row: dict[str, Any]) -> bool:
    return bool(outcomes(row).get("accepted")) and reviewer(row).get("reviewer_decision") == "pass"


def clean_worker_policy_sample(row: dict[str, Any]) -> bool:
    return usable_for_worker_policy(row) and is_accepted_pass_sample(row) and required_checks_passed(row)


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
        "resolved_profile",
        "category",
        "label",
        "profile_category",
        "routing_experiment",
        "routing_size",
        "routing_risk",
        "routing_risk_factor",
        "verification_scope",
        "routing_decision",
        "profile_routing_decision",
        "resolved_profile_routing_decision",
        "small_profile_candidate",
        "profile_experiment",
    ):
        entries = groups.get(group_name) if isinstance(groups.get(group_name), list) else []
        lines.append("")
        lines.append(f"## by_{group_name}")
        lines.append(render_group_table(entries))
    diagnostics = report.get("evaluation_diagnostics") if isinstance(report.get("evaluation_diagnostics"), dict) else {}
    if diagnostics:
        lines.append("")
        lines.append("## evaluation_diagnostics")
        lines.append(render_evaluation_diagnostics(diagnostics))
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


def render_evaluation_diagnostics(diagnostics: dict[str, Any]) -> str:
    lines: list[str] = []
    policy_usage = diagnostics.get("policy_usage") if isinstance(diagnostics.get("policy_usage"), dict) else {}
    lines.append(
        "policy_usage: "
        + " ".join(
            f"{key}={policy_usage.get(key, 0)}"
            for key in (
                "usable_for_worker_policy",
                "usable_for_reviewer_calibration",
                "usable_for_task_vector_evaluation",
            )
        )
    )
    lines.append("")
    lines.append("worker_cells")
    lines.append(render_worker_cell_table(list_value(diagnostics.get("worker_cells"))[:10]))
    lines.append("")
    lines.append("reviewer_cells")
    lines.append(render_reviewer_cell_table(list_value(diagnostics.get("reviewer_cells"))[:10]))
    lines.append("")
    lines.append("policy_exclusions")
    lines.append(render_policy_exclusion_table(list_value(diagnostics.get("policy_exclusions"))[:10]))
    lines.append("")
    lines.append("task_buckets")
    lines.append(render_task_bucket_table(list_value(diagnostics.get("task_buckets"))[:10]))
    return "\n".join(lines)


def render_worker_cell_table(entries: list[dict[str, Any]]) -> str:
    header = ["WORKER_CELL", "TASKS", "ACCEPT/PASS", "USABLE", "CLEAN", "FAILED", "RESUME"]
    rows = [
        [
            str(entry.get("key") or "-"),
            str(entry.get("tasks") or 0),
            str(entry.get("accepted_pass") or 0),
            str(entry.get("usable_for_worker_policy") or 0),
            str(entry.get("usable_accepted_pass") or 0),
            str(entry.get("failed") or 0),
            str(entry.get("needs_resume") or 0),
        ]
        for entry in entries
    ]
    return render_table(header, rows)


def render_reviewer_cell_table(entries: list[dict[str, Any]]) -> str:
    header = ["REVIEWER_CELL", "TASKS", "PASS", "FIX", "HUMAN", "FAILED", "FOLLOWUP"]
    rows = [
        [
            str(entry.get("key") or "-"),
            str(entry.get("tasks") or 0),
            str(entry.get("reviewer_pass") or 0),
            str(entry.get("reviewer_needs_fix") or 0),
            str(entry.get("reviewer_needs_human") or 0),
            str(entry.get("reviewer_failed_review") or 0),
            str(entry.get("needs_followup") or 0),
        ]
        for entry in entries
    ]
    return render_table(header, rows)


def render_policy_exclusion_table(entries: list[dict[str, Any]]) -> str:
    header = ["REASON", "ROWS", "WORKER_EXCL", "REVIEW_EXCL", "VECTOR_EXCL"]
    rows = [
        [
            str(entry.get("key") or "-"),
            str(entry.get("rows") or 0),
            str(entry.get("worker_policy_excluded") or 0),
            str(entry.get("reviewer_calibration_excluded") or 0),
            str(entry.get("task_vector_evaluation_excluded") or 0),
        ]
        for entry in entries
    ]
    return render_table(header, rows)


def render_task_bucket_table(entries: list[dict[str, Any]]) -> str:
    header = ["TASK_BUCKET", "TASKS", "USABLE", "CLEAN", "CANDIDATE"]
    rows = [
        [
            str(entry.get("key") or "-"),
            str(entry.get("tasks") or 0),
            str(entry.get("usable_for_worker_policy") or 0),
            str(entry.get("clean_samples") or 0),
            "yes" if entry.get("policy_review_candidate") else "no",
        ]
        for entry in entries
    ]
    return render_table(header, rows)


def list_value(value: object) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else []


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

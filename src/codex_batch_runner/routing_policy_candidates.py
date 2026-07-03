from __future__ import annotations

import hashlib
from typing import Any

from .config import Config
from .routing_report import (
    DEFAULT_ROUTING_REPORT_LIMIT,
    TASK_BUCKET_ADVISORY_THRESHOLDS,
    build_routing_report,
    percent_cell,
    render_table,
    task_bucket_threshold_advisory,
)


def build_routing_policy_candidate_report(
    config: Config,
    *,
    project_id: str | None = None,
    project_root: str | None = None,
    category: str | None = None,
    label: str | None = None,
    limit: int = DEFAULT_ROUTING_REPORT_LIMIT,
    include_archived: bool = False,
    execution_evidence_records: list[dict[str, Any]] | None = None,
    include_non_reviewable: bool = False,
) -> dict[str, Any]:
    execution_evidence_records = _filter_execution_evidence_records(
        execution_evidence_records,
        project_id=project_id,
        project_root=project_root,
        category=category,
        label=label,
    )
    routing_report = build_routing_report(
        config,
        project_id=project_id,
        project_root=project_root,
        category=category,
        label=label,
        limit=limit,
        include_archived=include_archived,
        execution_evidence_records=execution_evidence_records,
    )
    buckets = _combined_task_buckets(routing_report)
    reviewable = [bucket for bucket in buckets if bucket.get("threshold_advisory_status") == "reviewable"]
    non_reviewable = [bucket for bucket in buckets if bucket.get("threshold_advisory_status") != "reviewable"]
    emitted_non_reviewable = non_reviewable if include_non_reviewable else []
    return {
        "generated_at": routing_report.get("generated_at"),
        "read_only": True,
        "mutation_allowed": False,
        "filters": routing_report.get("filters"),
        "source_report": {
            "kind": "routing-report",
            "task_count": routing_report.get("task_count", 0),
            "execution_evidence_count": routing_report.get("execution_evidence_count", 0),
            "diagnostics": ["evaluation_diagnostics.task_buckets", "execution_evidence_diagnostics.task_buckets"],
        },
        "advisory": {
            "read_only": True,
            "mutation_allowed": False,
            "thresholds": TASK_BUCKET_ADVISORY_THRESHOLDS,
            "default_scope": "reviewable_only",
        },
        "summary": {
            "task_bucket_count": len(buckets),
            "candidate_count": len(reviewable),
            "reviewable": len(reviewable),
            "insufficient_sample": _count_status(buckets, "insufficient_sample"),
            "below_threshold": _count_status(buckets, "below_threshold"),
            "non_reviewable_included": include_non_reviewable,
            "non_reviewable_emitted": len(emitted_non_reviewable),
        },
        "candidates": [_candidate_entry(bucket, reviewable=True) for bucket in reviewable],
        "non_reviewable_buckets": [_candidate_entry(bucket, reviewable=False) for bucket in emitted_non_reviewable],
    }


def _filter_execution_evidence_records(
    records: list[dict[str, Any]] | None,
    *,
    project_id: str | None,
    project_root: str | None,
    category: str | None,
    label: str | None,
) -> list[dict[str, Any]] | None:
    if not records:
        return records
    return [
        record
        for record in records
        if _execution_evidence_record_matches(
            record,
            project_id=project_id,
            project_root=project_root,
            category=category,
            label=label,
        )
    ]


def _execution_evidence_record_matches(
    record: dict[str, Any],
    *,
    project_id: str | None,
    project_root: str | None,
    category: str | None,
    label: str | None,
) -> bool:
    if project_id and record.get("project_id") != project_id:
        return False
    if project_root and record.get("project_root") != project_root:
        return False
    if category and record.get("category") != category:
        return False
    if label and label not in _list_value(record.get("labels")):
        return False
    return True


def _combined_task_buckets(report: dict[str, Any]) -> list[dict[str, Any]]:
    combined: dict[str, dict[str, Any]] = {}
    for source, diagnostics_key in (
        ("queue", "evaluation_diagnostics"),
        ("supplemental_execution_evidence", "execution_evidence_diagnostics"),
    ):
        diagnostics = report.get(diagnostics_key) if isinstance(report.get(diagnostics_key), dict) else {}
        for bucket in _list_value(diagnostics.get("task_buckets")):
            if not isinstance(bucket, dict):
                continue
            key = str(bucket.get("key") or "unknown")
            current = combined.setdefault(key, _empty_bucket(key))
            _merge_bucket(current, bucket, source)
    buckets = [_finalize_bucket(bucket) for bucket in combined.values()]
    buckets.sort(key=lambda bucket: (str(bucket.get("threshold_advisory_status") or ""), str(bucket.get("key") or "")))
    return buckets


def _empty_bucket(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "tasks": 0,
        "completed": 0,
        "accepted": 0,
        "first_pass_accepted": 0,
        "needs_fix_or_rejected": 0,
        "reviewer_needs_fix": 0,
        "reviewer_needs_human": 0,
        "reviewer_failed_review": 0,
        "required_human_checks": 0,
        "usable_for_worker_policy": 0,
        "clean_samples": 0,
        "accepted_pass_clean_samples": 0,
        "worker_cells": set(),
        "reviewer_cells": set(),
        "evidence_sources": {
            "queue": {"task_buckets": 0, "rows": 0},
            "supplemental_execution_evidence": {"task_buckets": 0, "rows": 0},
        },
    }


def _merge_bucket(current: dict[str, Any], bucket: dict[str, Any], source: str) -> None:
    for key in (
        "tasks",
        "completed",
        "accepted",
        "first_pass_accepted",
        "needs_fix_or_rejected",
        "reviewer_needs_fix",
        "reviewer_needs_human",
        "reviewer_failed_review",
        "required_human_checks",
        "usable_for_worker_policy",
        "clean_samples",
        "accepted_pass_clean_samples",
    ):
        current[key] += int(bucket.get(key) or 0)
    current["worker_cells"].update(str(item) for item in _list_value(bucket.get("worker_cells")))
    current["reviewer_cells"].update(str(item) for item in _list_value(bucket.get("reviewer_cells")))
    current["evidence_sources"][source]["task_buckets"] += 1
    current["evidence_sources"][source]["rows"] += int(bucket.get("tasks") or 0)


def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    completed = int(bucket.get("completed") or 0)
    first_pass_accept_rate = _ratio(int(bucket.get("first_pass_accepted") or 0), completed)
    needs_fix_or_rejected_rate = _ratio(int(bucket.get("needs_fix_or_rejected") or 0), completed)
    advisory_status, advisory_reasons = task_bucket_threshold_advisory(
        accepted=int(bucket.get("accepted") or 0),
        first_pass_accept_rate=first_pass_accept_rate,
        needs_fix_or_rejected_rate=needs_fix_or_rejected_rate,
        reviewer_needs_fix=int(bucket.get("reviewer_needs_fix") or 0),
        reviewer_needs_human=int(bucket.get("reviewer_needs_human") or 0),
        reviewer_failed_review=int(bucket.get("reviewer_failed_review") or 0),
        required_human_checks=int(bucket.get("required_human_checks") or 0),
    )
    bucket["first_pass_accept_rate"] = first_pass_accept_rate
    bucket["needs_fix_or_rejected_rate"] = needs_fix_or_rejected_rate
    bucket["worker_cells"] = sorted(bucket["worker_cells"])
    bucket["reviewer_cells"] = sorted(bucket["reviewer_cells"])
    bucket["threshold_advisory_status"] = advisory_status
    bucket["threshold_advisory_reasons"] = advisory_reasons
    bucket["threshold_advisory"] = {
        "status": advisory_status,
        "reasons": advisory_reasons,
        "thresholds": TASK_BUCKET_ADVISORY_THRESHOLDS,
        "read_only": True,
    }
    return bucket


def _candidate_entry(bucket: dict[str, Any], *, reviewable: bool) -> dict[str, Any]:
    advisory_status = str(bucket.get("threshold_advisory_status") or "unknown")
    reasons = [str(reason) for reason in _list_value(bucket.get("threshold_advisory_reasons"))]
    entry = {
        "candidate_id": _candidate_id(str(bucket.get("key") or "unknown")),
        "task_bucket_key": str(bucket.get("key") or "unknown"),
        "evidence": {
            "tasks": int(bucket.get("tasks") or 0),
            "completed": int(bucket.get("completed") or 0),
            "accepted": int(bucket.get("accepted") or 0),
            "first_pass_accepted": int(bucket.get("first_pass_accepted") or 0),
            "first_pass_accept_rate": bucket.get("first_pass_accept_rate"),
            "needs_fix_or_rejected": int(bucket.get("needs_fix_or_rejected") or 0),
            "needs_fix_or_rejected_rate": bucket.get("needs_fix_or_rejected_rate"),
            "reviewer_needs_fix": int(bucket.get("reviewer_needs_fix") or 0),
            "reviewer_needs_human": int(bucket.get("reviewer_needs_human") or 0),
            "reviewer_failed_review": int(bucket.get("reviewer_failed_review") or 0),
            "required_human_checks": int(bucket.get("required_human_checks") or 0),
            "usable_for_worker_policy": int(bucket.get("usable_for_worker_policy") or 0),
            "clean_samples": int(bucket.get("clean_samples") or 0),
            "accepted_pass_clean_samples": int(bucket.get("accepted_pass_clean_samples") or 0),
            "worker_cells": _list_value(bucket.get("worker_cells")),
            "reviewer_cells": _list_value(bucket.get("reviewer_cells")),
            "evidence_sources": bucket.get("evidence_sources"),
        },
        "advisory_status": advisory_status,
        "advisory_reasons": reasons,
        "thresholds": TASK_BUCKET_ADVISORY_THRESHOLDS,
        "read_only": True,
        "mutation_allowed": False,
        "recommended_next_step": "operator_review" if reviewable else _non_reviewable_next_step(advisory_status),
    }
    if not reviewable:
        entry["blocked_reason"] = advisory_status
        entry["rejection_reasons"] = reasons
    return entry


def _candidate_id(task_bucket_key: str) -> str:
    digest = hashlib.sha256(task_bucket_key.encode("utf-8")).hexdigest()[:12]
    return f"routing-policy-candidate-{digest}"


def _non_reviewable_next_step(advisory_status: str) -> str:
    if advisory_status == "insufficient_sample":
        return "collect_more_evidence"
    return "keep_current_policy"


def _count_status(buckets: list[dict[str, Any]], status: str) -> int:
    return sum(1 for bucket in buckets if bucket.get("threshold_advisory_status") == status)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def render_routing_policy_candidate_report(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# routing policy candidates",
        "",
        "read_only: yes",
        "mutation_allowed: no",
        "source: routing-report evaluation diagnostics",
        (
            "summary: "
            f"candidates={summary.get('candidate_count', 0)} "
            f"reviewable={summary.get('reviewable', 0)} "
            f"insufficient_sample={summary.get('insufficient_sample', 0)} "
            f"below_threshold={summary.get('below_threshold', 0)}"
        ),
        "",
        "## candidates",
        render_candidate_table(_list_value(report.get("candidates"))),
    ]
    non_reviewable = _list_value(report.get("non_reviewable_buckets"))
    if non_reviewable:
        lines.extend(["", "## non_reviewable_buckets", render_candidate_table(non_reviewable)])
    return "\n".join(lines) + "\n"


def render_candidate_table(entries: list[dict[str, Any]]) -> str:
    header = ["CANDIDATE_ID", "TASK_BUCKET", "ACCEPT", "1PASS", "FIX/REJ", "ADVISORY", "NEXT_STEP", "REASONS"]
    rows = [
        [
            str(entry.get("candidate_id") or "-"),
            str(entry.get("task_bucket_key") or "-"),
            str((entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}).get("accepted") or 0),
            percent_cell(
                (entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}).get(
                    "first_pass_accept_rate"
                )
            ),
            percent_cell(
                (entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}).get(
                    "needs_fix_or_rejected_rate"
                )
            ),
            str(entry.get("advisory_status") or "-"),
            str(entry.get("recommended_next_step") or "-"),
            ",".join(str(reason) for reason in _list_value(entry.get("advisory_reasons"))) or "-",
        ]
        for entry in entries
    ]
    return render_table(header, rows)

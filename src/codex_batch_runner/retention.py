from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

from .config import Config
from .prune import (
    CursorSafety,
    apply_cursor_safety,
    load_cursor_safety,
    prune_reason,
    safe_file,
    task_log_paths,
)
from .timeutil import parse_time, utc_now

RETENTION_INVENTORY_CONTRACT = "retention-inventory-report-v1"

HOT = "Hot"
WARM = "Warm"
COLD_CANDIDATE = "Cold-candidate"

CANONICAL_TASK_PROTECTED = "canonical_task_protected"
ACTIVE_TASK = "active_task"
RESUME_REQUIRED = "resume_required"
USER_DECISION_REQUIRED = "user_decision_required"
REVIEW_PENDING = "review_pending"
ACCEPTED_WORKTREE_UNAPPLIED = "accepted_worktree_unapplied"
RECOVERY_REQUIRED = "recovery_required"
UNRESOLVED_FAILURE = "unresolved_failure"
TERMINAL_STATE_INELIGIBLE = "terminal_state_ineligible"
INVALID_TASK_JSON = "invalid_task_json"
MISSING_ACTIVITY_TIMESTAMP = "missing_activity_timestamp"
INVALID_ACTIVITY_TIMESTAMP = "invalid_activity_timestamp"
PROPOSAL_AGE_UNSPECIFIED = "proposal_age_unspecified"
AGE_BELOW_PROPOSAL_THRESHOLD = "age_below_proposal_threshold"
CURSOR_UNCERTAINTY = "cursor_uncertainty"
ARTIFACT_OUTSIDE_CONFIGURED_ROOT = "artifact_outside_configured_root"
ELIGIBLE_PAST_PROPOSAL_THRESHOLD = "eligible_past_proposal_threshold"


class RetentionInventoryValidationError(ValueError):
    pass


def build_retention_inventory_report(
    config: Config,
    *,
    proposal_age_days: int | None = None,
    project_id: str | None = None,
    notifier_cursor_state_paths: list[Path] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a read-only, sanitized retention projection from canonical files."""
    if proposal_age_days is not None and proposal_age_days < 0:
        raise ValueError("proposal age days must be non-negative")

    observed_at = now or utc_now()
    cutoff = (
        observed_at - timedelta(days=proposal_age_days)
        if proposal_age_days is not None
        else None
    )
    queue_dir = config.queue_dir.expanduser().resolve()
    log_dir = config.log_dir.expanduser().resolve()
    event_dir = config.event_dir.expanduser().resolve()
    cursor_paths = (
        notifier_cursor_state_paths
        if notifier_cursor_state_paths is not None
        else config.notifier_cursor_state_paths
    )
    cursor_safety = load_cursor_safety(cursor_paths, event_dir)

    items: list[dict[str, Any]] = []
    if queue_dir.is_dir():
        for task_file in sorted(queue_dir.glob("*.json")):
            task = _read_task(task_file)
            if not isinstance(task, dict):
                if project_id is None:
                    items.append(_invalid_task_item(task_file))
                continue
            if project_id is not None and task.get("project_id") != project_id:
                continue
            items.append(
                _task_item(
                    task,
                    task_file,
                    log_dir=log_dir,
                    cutoff=cutoff,
                )
            )

    event_files = _event_items(event_dir, cutoff, cursor_safety)
    report: dict[str, Any] = {
        "schema_version": RETENTION_INVENTORY_CONTRACT,
        "mode": "report-only",
        "mutation": {
            "performed": False,
            "supported": False,
            "canonical_task_deletion_supported": False,
        },
        "observed_at": observed_at.isoformat(),
        "proposal_parameters": {
            "age_days": proposal_age_days,
            "age_is_policy": False,
            "project_filter_applied": project_id is not None,
        },
        "cursor_safety": {
            "configured_cursor_count": len(cursor_safety.cursor_paths),
            "block_all_event_pruning": cursor_safety.block_all_event_pruning,
            "cursor_scope_digest": _cursor_scope_digest(cursor_paths),
            "reason_codes": (
                [CURSOR_UNCERTAINTY]
                if cursor_safety.block_all_event_pruning or cursor_safety.warnings
                else []
            ),
        },
        "summary": _summary(items, event_files),
        "items": items,
        "event_files": event_files,
    }
    report["report_digest"] = _digest(report)
    validate_retention_inventory_report(report)
    return report


def validate_retention_inventory_report(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise RetentionInventoryValidationError("report must be an object")
    if report.get("schema_version") != RETENTION_INVENTORY_CONTRACT:
        raise RetentionInventoryValidationError("unsupported retention inventory schema")
    if report.get("mode") != "report-only":
        raise RetentionInventoryValidationError("retention inventory must be report-only")
    mutation = report.get("mutation")
    if mutation != {
        "performed": False,
        "supported": False,
        "canonical_task_deletion_supported": False,
    }:
        raise RetentionInventoryValidationError("retention inventory mutation boundary is invalid")
    items = report.get("items")
    event_files = report.get("event_files")
    if not isinstance(items, list) or not isinstance(event_files, list):
        raise RetentionInventoryValidationError("retention inventory items must be arrays")
    for item in items:
        if not isinstance(item, dict):
            raise RetentionInventoryValidationError("task inventory item must be an object")
        eligibility = item.get("eligibility")
        if not isinstance(eligibility, dict) or eligibility.get("canonical_task_json_protected") is not True:
            raise RetentionInventoryValidationError("canonical task JSON must be protected")
        artifacts = item.get("artifacts")
        if (
            not isinstance(artifacts, list)
            or not artifacts
            or not isinstance(artifacts[0], dict)
            or artifacts[0].get("retention_status") != "protected"
        ):
            raise RetentionInventoryValidationError("canonical task artifact must be protected")
        preview = item.get("compact_tombstone_preview")
        if not isinstance(preview, dict) or preview.get("writes_performed") is not False:
            raise RetentionInventoryValidationError("tombstone preview must not write")
        expected = _digest({key: value for key, value in preview.items() if key != "preview_digest"})
        if preview.get("preview_digest") != expected:
            raise RetentionInventoryValidationError("tombstone preview digest mismatch")
    expected_report_digest = _digest(
        {key: value for key, value in report.items() if key != "report_digest"}
    )
    if report.get("report_digest") != expected_report_digest:
        raise RetentionInventoryValidationError("retention inventory report digest mismatch")
    return report


def _task_item(
    task: dict[str, Any],
    task_file: Path,
    *,
    log_dir: Path,
    cutoff: datetime | None,
) -> dict[str, Any]:
    task_id = str(task.get("id") or task_file.stem)
    reason_codes = _task_blockers(task, cutoff)
    eligible = not reason_codes
    lifecycle = COLD_CANDIDATE if eligible else (_hot_or_warm(reason_codes))
    activity_timestamp, timestamp_source = _activity_timestamp(task)

    artifacts: list[dict[str, Any]] = [
        {
            "kind": "canonical_task_json",
            "artifact_ref": _artifact_ref("task", task_file.name),
            "exists": task_file.exists(),
            "safe": True,
            "lifecycle_class": WARM if lifecycle == COLD_CANDIDATE else lifecycle,
            "retention_status": "protected",
            "reason_codes": [CANONICAL_TASK_PROTECTED],
        }
    ]
    for index, raw_path in enumerate(task_log_paths(task)):
        candidate = safe_file("log", Path(raw_path).expanduser(), log_dir, "log_dir")
        artifact_identity = (
            Path(candidate.path).relative_to(log_dir).as_posix()
            if candidate.safe
            else f"outside-configured-root:{index}"
        )
        artifact_reasons = (
            [ELIGIBLE_PAST_PROPOSAL_THRESHOLD]
            if eligible and candidate.safe
            else list(reason_codes)
        )
        if not candidate.safe:
            artifact_reasons = _stable_reasons(
                [*artifact_reasons, ARTIFACT_OUTSIDE_CONFIGURED_ROOT]
            )
        artifacts.append(
            {
                "kind": "raw_execution_log",
                "artifact_ref": _artifact_ref("log", task_id, artifact_identity),
                "exists": candidate.exists,
                "safe": candidate.safe,
                "lifecycle_class": (
                    COLD_CANDIDATE if eligible and candidate.safe else lifecycle
                ),
                "retention_status": (
                    "prune_candidate" if eligible and candidate.safe else "retained"
                ),
                "reason_codes": artifact_reasons,
            }
        )

    tombstone_body = {
        "candidate": eligible,
        "writes_performed": False,
        "projected_reason_code": (
            ELIGIBLE_PAST_PROPOSAL_THRESHOLD if eligible else None
        ),
        "protected_source": "canonical_task_json",
        "projected_fields": [
            "task_id",
            "terminal_status",
            "review_or_resolution_disposition",
            "source_task_digest",
            "retention_reason_code",
            "policy_revision",
        ],
        "blocker_reason_codes": reason_codes,
    }
    tombstone_preview = {
        **tombstone_body,
        "preview_digest": _digest(tombstone_body),
    }
    return {
        "task_id": task_id,
        "project_id": task.get("project_id"),
        "status": task.get("status"),
        "review_status": task.get("review_status"),
        "resolution": task.get("resolution"),
        "source_task_digest": _digest(task),
        "activity_timestamp": activity_timestamp,
        "activity_timestamp_source": timestamp_source,
        "lifecycle_class": lifecycle,
        "eligibility": {
            "raw_log_prune_candidate": eligible,
            "canonical_task_json_protected": True,
            "reason_codes": (
                [ELIGIBLE_PAST_PROPOSAL_THRESHOLD] if eligible else reason_codes
            ),
        },
        "compact_tombstone_preview": tombstone_preview,
        "restore_capability": {
            "canonical_task_state": "full",
            "derived_index": "rebuildable",
            "raw_transcript": (
                "degraded_if_candidate_artifacts_pruned"
                if eligible
                else "retained_by_this_report"
            ),
            "deleted_artifact_reconstruction_claimed": False,
        },
        "artifacts": artifacts,
    }


def _task_blockers(
    task: dict[str, Any],
    cutoff: datetime | None,
) -> list[str]:
    status = task.get("status")
    reasons: list[str] = []
    if status in {"runnable", "running"}:
        reasons.append(ACTIVE_TASK)
    elif status == "needs_resume":
        reasons.append(RESUME_REQUIRED)
    elif status == "blocked_user":
        reasons.append(USER_DECISION_REQUIRED)
    elif status == "completed" and task.get("review_status") not in {"accepted", "rejected"}:
        reasons.append(REVIEW_PENDING)
    elif status == "failed" and not task.get("resolution"):
        reasons.append(UNRESOLVED_FAILURE)

    if (
        task.get("review_status") == "accepted"
        and task.get("execution_mode") == "git_worktree"
        and task.get("execution_apply_status") != "applied"
    ):
        reasons.append(ACCEPTED_WORKTREE_UNAPPLIED)
    if task.get("recovery_required") or task.get("execution_worktree_status") == "recovery_required":
        reasons.append(RECOVERY_REQUIRED)

    terminal_reason, _ = prune_reason(task)
    if terminal_reason is None:
        reasons.append(TERMINAL_STATE_INELIGIBLE)

    timestamp, _ = _activity_timestamp(task)
    parsed = parse_time(timestamp)
    if timestamp is None:
        reasons.append(MISSING_ACTIVITY_TIMESTAMP)
    elif parsed is None:
        reasons.append(INVALID_ACTIVITY_TIMESTAMP)
    if cutoff is None:
        reasons.append(PROPOSAL_AGE_UNSPECIFIED)
    elif parsed is not None and parsed > cutoff:
        reasons.append(AGE_BELOW_PROPOSAL_THRESHOLD)
    return _stable_reasons(reasons)


def _invalid_task_item(task_file: Path) -> dict[str, Any]:
    reasons = [INVALID_TASK_JSON, MISSING_ACTIVITY_TIMESTAMP]
    tombstone_body = {
        "candidate": False,
        "writes_performed": False,
        "projected_reason_code": None,
        "protected_source": "canonical_task_json",
        "projected_fields": [],
        "blocker_reason_codes": reasons,
    }
    return {
        "task_id": task_file.stem,
        "project_id": None,
        "status": "invalid",
        "review_status": None,
        "resolution": None,
        "source_task_digest": _file_digest(task_file),
        "activity_timestamp": None,
        "activity_timestamp_source": None,
        "lifecycle_class": WARM,
        "eligibility": {
            "raw_log_prune_candidate": False,
            "canonical_task_json_protected": True,
            "reason_codes": reasons,
        },
        "compact_tombstone_preview": {
            **tombstone_body,
            "preview_digest": _digest(tombstone_body),
        },
        "restore_capability": {
            "canonical_task_state": "invalid_but_retained",
            "derived_index": "blocked_invalid_source",
            "raw_transcript": "unknown",
            "deleted_artifact_reconstruction_claimed": False,
        },
        "artifacts": [
            {
                "kind": "canonical_task_json",
                "artifact_ref": _artifact_ref("task", task_file.name),
                "exists": task_file.exists(),
                "safe": True,
                "lifecycle_class": WARM,
                "retention_status": "protected",
                "reason_codes": [CANONICAL_TASK_PROTECTED, *reasons],
            }
        ],
    }


def _event_items(
    event_dir: Path,
    cutoff: datetime | None,
    cursor_safety: CursorSafety,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not event_dir.is_dir():
        return items
    for path in sorted(event_dir.rglob("*.jsonl")):
        candidate = safe_file("event", path, event_dir, "event_dir")
        reasons: list[str] = []
        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=utc_now().tzinfo)
        except OSError:
            continue
        if not candidate.safe:
            reasons.append(ARTIFACT_OUTSIDE_CONFIGURED_ROOT)
        if cutoff is None:
            reasons.append(PROPOSAL_AGE_UNSPECIFIED)
        elif modified_at > cutoff:
            reasons.append(AGE_BELOW_PROPOSAL_THRESHOLD)
        if cursor_safety.block_all_event_pruning:
            reasons.append(CURSOR_UNCERTAINTY)
        elif candidate.safe:
            checked = apply_cursor_safety(candidate, cursor_safety)
            if checked.skipped:
                reasons.append(CURSOR_UNCERTAINTY)
        reasons = _stable_reasons(reasons)
        eligible = not reasons
        items.append(
            {
                "kind": "event_log",
                "artifact_ref": _artifact_ref(
                    "event",
                    (
                        path.resolve().relative_to(event_dir).as_posix()
                        if candidate.safe
                        else "outside-configured-root"
                    ),
                ),
                "exists": candidate.exists,
                "safe": candidate.safe,
                "modified_at": modified_at.isoformat(),
                "source_digest": _file_digest(path),
                "lifecycle_class": COLD_CANDIDATE if eligible else WARM,
                "prune_candidate": eligible,
                "reason_codes": (
                    [ELIGIBLE_PAST_PROPOSAL_THRESHOLD] if eligible else reasons
                ),
            }
        )
    return items


def _summary(
    items: list[dict[str, Any]],
    event_files: list[dict[str, Any]],
) -> dict[str, Any]:
    lifecycle = {HOT: 0, WARM: 0, COLD_CANDIDATE: 0}
    for item in [*items, *event_files]:
        lifecycle[item["lifecycle_class"]] += 1
    return {
        "task_count": len(items),
        "event_file_count": len(event_files),
        "artifact_count": sum(len(item["artifacts"]) for item in items)
        + len(event_files),
        "lifecycle_class_counts": lifecycle,
        "raw_log_candidate_task_count": sum(
            bool(item["eligibility"]["raw_log_prune_candidate"]) for item in items
        ),
        "event_candidate_count": sum(
            bool(item["prune_candidate"]) for item in event_files
        ),
        "canonical_task_json_protected_count": len(items),
        "tombstone_candidate_count": sum(
            bool(item["compact_tombstone_preview"]["candidate"]) for item in items
        ),
    }


def _hot_or_warm(reason_codes: list[str]) -> str:
    hot_reasons = {
        ACTIVE_TASK,
        RESUME_REQUIRED,
        USER_DECISION_REQUIRED,
        REVIEW_PENDING,
        ACCEPTED_WORKTREE_UNAPPLIED,
        RECOVERY_REQUIRED,
    }
    return HOT if hot_reasons.intersection(reason_codes) else WARM


def _activity_timestamp(task: dict[str, Any]) -> tuple[str | None, str | None]:
    _, timestamp = prune_reason(task)
    if timestamp is not None:
        for key in ("archived_at", "reviewed_at", "completed_at", "updated_at", "created_at"):
            if task.get(key) == timestamp:
                return timestamp, key
    for key in ("updated_at", "created_at"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), key
    return None, None


def _read_task(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _artifact_ref(*parts: str) -> str:
    return _digest({"artifact": list(parts)})


def _file_digest(path: Path) -> str:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return _digest(None)


def _cursor_scope_digest(paths: list[Path]) -> str:
    states: list[dict[str, Any]] = []
    for path in paths:
        resolved = path.expanduser().resolve(strict=False)
        try:
            state = {
                "cursor_ref": _artifact_ref("cursor", str(resolved)),
                "exists": resolved.exists(),
                "source_digest": _file_digest(resolved) if resolved.exists() else None,
            }
        except OSError:
            state = {
                "cursor_ref": _artifact_ref("cursor", str(resolved)),
                "exists": False,
                "source_digest": None,
            }
        states.append(state)
    return _digest({"cursor_states": states})


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _stable_reasons(reasons: list[str]) -> list[str]:
    return sorted(set(reasons))


def render_retention_inventory_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lifecycle = summary["lifecycle_class_counts"]
    params = report["proposal_parameters"]
    lines = [
        "Retention inventory (report-only)",
        f"contract: {report['schema_version']}",
        f"observed_at: {report['observed_at']}",
        f"proposal_age_days: {params['age_days']}",
        "proposal_age_is_policy: false",
        (
            "inventory: "
            f"tasks={summary['task_count']} "
            f"events={summary['event_file_count']} "
            f"artifacts={summary['artifact_count']}"
        ),
        (
            "lifecycle: "
            f"Hot={lifecycle[HOT]} Warm={lifecycle[WARM]} "
            f"Cold-candidate={lifecycle[COLD_CANDIDATE]}"
        ),
        (
            "candidates: "
            f"task_logs={summary['raw_log_candidate_task_count']} "
            f"events={summary['event_candidate_count']} "
            f"tombstones={summary['tombstone_candidate_count']}"
        ),
        (
            "canonical_task_json: "
            f"protected={summary['canonical_task_json_protected_count']}"
        ),
        f"report_digest: {report['report_digest']}",
    ]
    return "\n".join(lines) + "\n"

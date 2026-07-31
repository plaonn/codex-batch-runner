from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .config import Config
from .queue import list_tasks_read_only
from .worktree import (
    cleanup_eligibility,
    git_optional,
    is_ancestor,
    local_branch_state,
    registry_entry_for_branch,
    registry_entry_for_path,
    worktree_registry,
)
from .worktree_hibernation import _provenance, _worktree_is_dirty


CONTRACT = "worktree-reconciliation-plan-v1"
HIBERNATION_CONTRACT = "worktree-hibernation-v1"
ACTION_CLASSES = {
    "no_action",
    "manual_review",
    "exact_repair_candidate",
    "unrecoverable_without_owner_decision",
}
REASON_CODES = {
    "active_or_resumable_task",
    "ambiguous_or_missing_provenance",
    "apply_containment_unproven",
    "attached_state_current",
    "base_head_not_current",
    "base_not_ancestor_of_checkpoint",
    "checkpoint_head_mismatch",
    "dirty_or_uncheckpointed",
    "exact_terminal_cleanup_receipt",
    "intentional_hibernation_current",
    "invalid_terminal_cleanup_evidence",
    "malformed_hibernation_evidence",
    "missing_branch",
    "missing_checkpoint",
    "missing_execution_base",
    "missing_path_is_not_lifecycle_evidence",
    "non_worktree_task",
    "registry_evidence_unavailable",
    "registry_mismatch",
    "result_review_apply_state_ambiguous",
    "terminal_cleanup_current",
}
TASK_STATUSES = {
    "runnable",
    "running",
    "needs_resume",
    "completed",
    "failed",
    "blocked_user",
    "archived",
    "unknown",
}
REVIEW_STATUSES = {
    "unreviewed",
    "accepted",
    "rejected",
    "needs_followup",
    "unknown",
}
APPLY_STATUSES = {"applied", "discarded", "unknown"}
WORKTREE_STATUSES = {
    "prepared",
    "running",
    "retained",
    "cleanup_candidate",
    "cleaned",
    "hibernated",
    "missing",
    "recovery_required",
    "unknown",
}
RESOLUTIONS = {"none", "duplicate", "manual", "superseded", "wont_fix", "other"}
CLEANUP_KINDS = {"applied", "discard", "no_change", "unknown"}
CLEANUP_REASONS = {
    "execution_apply_status=applied",
    "review_status=rejected",
    "resolution=duplicate",
    "resolution=manual",
    "resolution=superseded",
    "resolution=wont_fix",
    "already_contained",
    "unknown",
}
RECONCILIATION_STATUSES = {
    "attached_current",
    "cleanup_evidence_invalid",
    "dirty_or_uncheckpointed",
    "hibernated_current",
    "hibernation_evidence_invalid",
    "missing_path_branch_missing",
    "missing_path_branch_present",
    "not_applicable",
    "registry_path_mismatch",
    "registry_unavailable",
    "terminal_cleanup_current",
}
CLEANUP_ELIGIBILITY = {"applied", "discard", "no_change", "blocked"}
PROVENANCE_STATUSES = {"complete", "ambiguous_or_missing"}
CONTAINMENT_STATUSES = {"current", "not_current", "unavailable"}
HEX_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
OPAQUE_REF = re.compile(r"(?:repo|branch|path):[0-9a-f]{16}")
COMMIT = re.compile(r"[0-9a-f]{40,64}")


class WorktreeReconciliationPlanValidationError(ValueError):
    pass


def build_worktree_reconciliation_plan(
    config: Config,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic report; no queue lock or mutation surface is used."""
    tasks = [
        task
        for task in list_tasks_read_only(config)
        if (task_id is None or str(task.get("id")) == task_id)
        and (project_id is None or task.get("project_id") == project_id)
    ]
    if task_id is not None and not tasks:
        raise FileNotFoundError(f"task not found: {task_id}")

    registry_cache: dict[Path, list[dict[str, str]] | None] = {}
    items = [
        _task_plan(task, registry_cache)
        for task in sorted(tasks, key=lambda value: str(value.get("id") or ""))
    ]
    report: dict[str, Any] = {
        "schema_version": CONTRACT,
        "mode": "report-only",
        "authority": {
            "repair_authority_granted": False,
            "repair_supported": False,
            "mutation_performed": False,
        },
        "filters": {
            "task_id_applied": task_id is not None,
            "project_id_applied": project_id is not None,
        },
        "summary": _summary(items),
        "items": items,
    }
    report["report_digest"] = _digest(report)
    return validate_worktree_reconciliation_plan(report)


def validate_worktree_reconciliation_plan(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "mode",
        "authority",
        "filters",
        "summary",
        "items",
        "report_digest",
    }:
        raise WorktreeReconciliationPlanValidationError(
            "report fields are not canonical"
        )
    if value["schema_version"] != CONTRACT or value["mode"] != "report-only":
        raise WorktreeReconciliationPlanValidationError(
            "unsupported reconciliation plan"
        )
    if value["authority"] != {
        "repair_authority_granted": False,
        "repair_supported": False,
        "mutation_performed": False,
    }:
        raise WorktreeReconciliationPlanValidationError(
            "report-only authority boundary is invalid"
        )
    if (
        not isinstance(value["filters"], dict)
        or set(value["filters"]) != {"task_id_applied", "project_id_applied"}
        or any(not isinstance(item, bool) for item in value["filters"].values())
    ):
        raise WorktreeReconciliationPlanValidationError("filters are invalid")
    if not isinstance(value["items"], list):
        raise WorktreeReconciliationPlanValidationError("items must be an array")
    task_ids: list[str] = []
    for item in value["items"]:
        _validate_item(item)
        task_ids.append(item["task_id"])
    if task_ids != sorted(set(task_ids)):
        raise WorktreeReconciliationPlanValidationError(
            "items must be sorted by unique task id"
        )
    if value["summary"] != _summary(value["items"]):
        raise WorktreeReconciliationPlanValidationError("summary does not match items")
    if not isinstance(value["report_digest"], str) or not HEX_DIGEST.fullmatch(
        value["report_digest"]
    ):
        raise WorktreeReconciliationPlanValidationError("report digest is invalid")
    expected = _digest(
        {key: item for key, item in value.items() if key != "report_digest"}
    )
    if value["report_digest"] != expected:
        raise WorktreeReconciliationPlanValidationError("report digest mismatch")
    return value


def render_worktree_reconciliation_plan(report: dict[str, Any]) -> str:
    lines = [
        f"schema_version: {report['schema_version']}",
        "mode: report-only",
        "repair_authority_granted: false",
        f"tasks: {report['summary']['task_count']}",
        f"exact_repair_candidates: {report['summary']['action_by_class'].get('exact_repair_candidate', 0)}",
        f"report_digest: {report['report_digest']}",
    ]
    for item in report["items"]:
        lines.append(
            "  "
            f"task={item['task_id']} action={item['action_class']} "
            f"reconciliation={item['derived']['reconciliation_status']} "
            f"source_digest={item['source_snapshot_digest']}"
        )
    return "\n".join(lines) + "\n"


def _task_plan(
    task: dict[str, Any],
    registry_cache: dict[Path, list[dict[str, str]] | None],
) -> dict[str, Any]:
    snapshot = _source_snapshot(task, registry_cache)
    derived = _derive(snapshot)
    action, reasons, delta = _classify(snapshot, derived)
    return {
        "task_id": str(task.get("id") or ""),
        "action_class": action,
        "reason_codes": sorted(set(reasons)),
        "grandfathered_row": _grandfathered(derived),
        "metadata_delta": delta,
        "derived": derived,
        "source_snapshot": snapshot,
        "source_snapshot_digest": _digest(snapshot),
    }


def _source_snapshot(
    task: dict[str, Any],
    registry_cache: dict[Path, list[dict[str, str]] | None],
) -> dict[str, Any]:
    repo_root = _repo_root(task)
    branch = str(task.get("execution_branch") or "").strip()
    path = _path(task.get("execution_worktree_path"))
    registry = _registry(repo_root, registry_cache)
    path_exists = bool(path is not None and path.exists())
    path_entry = (
        registry_entry_for_path(registry, path)
        if registry is not None and path is not None
        else None
    )
    branch_entry = (
        registry_entry_for_branch(registry, branch)
        if registry is not None and branch
        else None
    )
    branch_state = (
        local_branch_state(repo_root, branch)
        if repo_root is not None and branch
        else {"exists": False, "head": None}
    )
    base_head = _commit(task.get("execution_base_head"))
    checkpoint_head = _commit(
        task.get("execution_branch_head")
        or task.get("execution_commit")
        or task.get("execution_hibernation_branch_head")
    )
    observed_base_head = _resolve_commit(repo_root, base_head)
    observed_checkpoint_head = _resolve_commit(repo_root, checkpoint_head)
    observed_branch_head = _commit(branch_state.get("head"))
    ancestry = (
        is_ancestor(repo_root, observed_base_head, observed_branch_head)
        if repo_root is not None
        and observed_base_head is not None
        and observed_branch_head is not None
        else None
    )
    attached = bool(
        path_exists
        and path_entry
        and branch_entry
        and _entry_path_ref(path_entry) == _entry_path_ref(branch_entry)
    )
    dirty = (
        _worktree_is_dirty(task, repo_root, path)
        if attached and path is not None
        else None
    )
    applied_head = _commit(task.get("execution_applied_head"))
    apply_target = str(task.get("execution_apply_target") or "").strip()
    observed_apply_target_head = _resolve_ref(repo_root, apply_target)
    containment = (
        is_ancestor(repo_root, applied_head, observed_apply_target_head)
        if repo_root is not None
        and applied_head is not None
        and observed_apply_target_head is not None
        else None
    )
    provenance = _provenance_evidence(task)
    cleanup_values = {
        key: task.get(key)
        for key in (
            "execution_cleanup_kind",
            "execution_cleanup_reason",
            "execution_cleanup_branch_retained",
            "execution_cleanup_result_applied",
            "execution_cleaned_at",
        )
    }
    return {
        "canonical_state": {
            "execution_mode": _enum(
                task.get("execution_mode"),
                {"git_worktree", "main_worktree", "unknown"},
            ),
            "task_status": _enum(task.get("status"), TASK_STATUSES),
            "review_status": _enum(task.get("review_status"), REVIEW_STATUSES),
            "apply_status": _enum(task.get("execution_apply_status"), APPLY_STATUSES),
            "worktree_status": _enum(
                task.get("execution_worktree_status"), WORKTREE_STATUSES
            ),
            "resolution": _resolution(task.get("resolution")),
            "resolution_digest": _optional_value_digest(task.get("resolution")),
            "followup_present": bool(task.get("chain_status")),
            "followup_digest": _optional_value_digest(task.get("chain_status")),
        },
        "git_observations": {
            "repository_ref": _opaque_ref("repo", repo_root),
            "branch_ref": _opaque_ref("branch", branch or None),
            "base_head": base_head,
            "observed_base_head": observed_base_head,
            "checkpoint_head": checkpoint_head,
            "observed_checkpoint_head": observed_checkpoint_head,
            "observed_branch_head": observed_branch_head,
            "base_is_ancestor_of_observed_branch": ancestry,
            "registry_available": registry is not None,
            "path_exists": path_exists,
            "path_registry_ref": _entry_path_ref(path_entry),
            "branch_registry_ref": _entry_path_ref(branch_entry),
            "worktree_dirty": dirty,
        },
        "apply_evidence": {
            "applied_head": applied_head,
            "applied_at_digest": _optional_value_digest(
                task.get("execution_applied_at")
            ),
            "apply_target_ref": _opaque_ref("branch", apply_target or None),
            "observed_apply_target_head": observed_apply_target_head,
            "applied_head_is_ancestor_of_target": containment,
        },
        "cleanup_evidence": {
            "kind": _enum(task.get("execution_cleanup_kind"), CLEANUP_KINDS),
            "reason": _enum(task.get("execution_cleanup_reason"), CLEANUP_REASONS),
            "branch_retained": _strict_bool(
                task.get("execution_cleanup_branch_retained")
            ),
            "result_applied": _strict_bool(
                task.get("execution_cleanup_result_applied")
            ),
            "cleaned_at_digest": _optional_value_digest(
                task.get("execution_cleaned_at")
            ),
            "receipt_digest": _value_digest(cleanup_values),
        },
        "hibernation_evidence": {
            "contract": _enum(
                task.get("execution_hibernation_contract"),
                {HIBERNATION_CONTRACT, "unknown"},
            ),
            "kind": _enum(
                task.get("execution_hibernation_kind"),
                {"disposable", "pooled", "unknown"},
            ),
            "base_head": _commit(task.get("execution_hibernation_base_head")),
            "branch_head": _commit(task.get("execution_hibernation_branch_head")),
            "hibernated_at_digest": _optional_value_digest(
                task.get("execution_hibernated_at")
            ),
        },
        "provenance_evidence": provenance,
    }


def _derive(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "reconciliation_status": _reconciliation_status(snapshot),
        "cleanup_eligibility": _cleanup_eligibility(snapshot),
        "provenance_status": _provenance_status(snapshot),
        "apply_containment": _containment_status(snapshot),
    }


def _reconciliation_status(snapshot: dict[str, Any]) -> str:
    state = snapshot["canonical_state"]
    git = snapshot["git_observations"]
    if state["execution_mode"] != "git_worktree":
        return "not_applicable"
    if not git["registry_available"]:
        return "registry_unavailable"
    if state["worktree_status"] == "hibernated":
        return (
            "hibernated_current"
            if _hibernation_evidence_valid(snapshot)
            else "hibernation_evidence_invalid"
        )
    if state["worktree_status"] == "cleaned":
        return (
            "terminal_cleanup_current"
            if _terminal_cleanup_evidence_valid(snapshot)
            else "cleanup_evidence_invalid"
        )
    path_ref = git["path_registry_ref"]
    branch_ref = git["branch_registry_ref"]
    attached = bool(
        git["path_exists"]
        and path_ref is not None
        and branch_ref is not None
        and path_ref == branch_ref
    )
    if attached:
        if (
            git["worktree_dirty"] is True
            or git["checkpoint_head"] is None
            or git["observed_checkpoint_head"] != git["checkpoint_head"]
            or git["observed_branch_head"] != git["checkpoint_head"]
        ):
            return "dirty_or_uncheckpointed"
        return "attached_current"
    if git["path_exists"] or path_ref is not None or branch_ref is not None:
        return "registry_path_mismatch"
    if git["observed_branch_head"] is None:
        return "missing_path_branch_missing"
    return "missing_path_branch_present"


def _cleanup_eligibility(snapshot: dict[str, Any]) -> str:
    state = snapshot["canonical_state"]
    apply_evidence = snapshot["apply_evidence"]
    task_view = {
        "execution_mode": state["execution_mode"],
        "status": state["task_status"],
        "review_status": state["review_status"],
        "resolution": None if state["resolution"] == "none" else state["resolution"],
        "execution_apply_status": state["apply_status"],
        "execution_applied_at": (
            "present" if apply_evidence["applied_at_digest"] is not None else None
        ),
        "execution_applied_head": apply_evidence["applied_head"],
    }
    eligibility = cleanup_eligibility(task_view)
    kind = eligibility.get("cleanup_kind")
    return kind if kind in {"applied", "discard", "no_change"} else "blocked"


def _provenance_status(snapshot: dict[str, Any]) -> str:
    evidence = snapshot["provenance_evidence"]
    if (
        evidence["history_count"] > 0
        and evidence["history_digest"] is not None
        and evidence["parse_valid"]
        and evidence["view_status"] in {"mutation_observed", "no_mutation_observed"}
        and not evidence["unsafe_or_unreported"]
    ):
        return "complete"
    return "ambiguous_or_missing"


def _containment_status(snapshot: dict[str, Any]) -> str:
    evidence = snapshot["apply_evidence"]
    if (
        evidence["applied_head"] is None
        or evidence["applied_at_digest"] is None
        or evidence["apply_target_ref"] is None
        or evidence["observed_apply_target_head"] is None
        or evidence["applied_head_is_ancestor_of_target"] is None
    ):
        return "unavailable"
    return (
        "current" if evidence["applied_head_is_ancestor_of_target"] else "not_current"
    )


def _classify(
    snapshot: dict[str, Any],
    derived: dict[str, Any],
) -> tuple[str, list[str], list[dict[str, str]]]:
    state = snapshot["canonical_state"]
    reconciliation = derived["reconciliation_status"]
    if state["execution_mode"] != "git_worktree":
        return "no_action", ["non_worktree_task"], []
    if reconciliation == "registry_unavailable":
        return (
            "unrecoverable_without_owner_decision",
            ["registry_evidence_unavailable"],
            [],
        )
    if reconciliation == "registry_path_mismatch":
        return (
            "unrecoverable_without_owner_decision",
            ["registry_mismatch"],
            [],
        )
    if reconciliation == "hibernation_evidence_invalid":
        return "manual_review", ["malformed_hibernation_evidence"], []
    if reconciliation == "cleanup_evidence_invalid":
        return "manual_review", ["invalid_terminal_cleanup_evidence"], []
    base_reasons = _base_binding_reasons(snapshot)
    if base_reasons:
        return "unrecoverable_without_owner_decision", base_reasons, []
    if reconciliation == "terminal_cleanup_current":
        return "no_action", ["terminal_cleanup_current"], []
    if reconciliation == "hibernated_current":
        return "no_action", ["intentional_hibernation_current"], []
    if reconciliation == "dirty_or_uncheckpointed":
        return "manual_review", ["dirty_or_uncheckpointed"], []
    if reconciliation == "attached_current":
        if state["task_status"] in {"runnable", "running", "needs_resume"}:
            return "manual_review", ["active_or_resumable_task"], []
        return "no_action", ["attached_state_current"], []
    if state["task_status"] in {"runnable", "running", "needs_resume"}:
        return "manual_review", ["active_or_resumable_task"], []
    if derived["provenance_status"] != "complete":
        return "manual_review", ["ambiguous_or_missing_provenance"], []
    if _exact_cleanup_status_candidate(snapshot, derived):
        return (
            "exact_repair_candidate",
            [
                "exact_terminal_cleanup_receipt",
                "missing_path_is_not_lifecycle_evidence",
            ],
            [
                {
                    "field": "execution_worktree_status",
                    "before": state["worktree_status"],
                    "after": "cleaned",
                }
            ],
        )
    if derived["cleanup_eligibility"] == "applied" and (
        derived["apply_containment"] != "current"
    ):
        return "manual_review", ["apply_containment_unproven"], []
    if reconciliation in {
        "missing_path_branch_present",
        "missing_path_branch_missing",
    }:
        return "manual_review", ["missing_path_is_not_lifecycle_evidence"], []
    return "manual_review", ["result_review_apply_state_ambiguous"], []


def _base_binding_reasons(snapshot: dict[str, Any]) -> list[str]:
    git = snapshot["git_observations"]
    reasons: list[str] = []
    if git["base_head"] is None:
        reasons.append("missing_execution_base")
    elif git["observed_base_head"] != git["base_head"]:
        reasons.append("base_head_not_current")
    if git["checkpoint_head"] is None or git["observed_checkpoint_head"] is None:
        reasons.append("missing_checkpoint")
    elif (
        git["observed_checkpoint_head"] != git["checkpoint_head"]
        or git["observed_branch_head"] != git["checkpoint_head"]
    ):
        reasons.append("checkpoint_head_mismatch")
    if git["observed_branch_head"] is None:
        reasons.append("missing_branch")
    if (
        git["observed_base_head"] is not None
        and git["observed_branch_head"] is not None
        and git["base_is_ancestor_of_observed_branch"] is not True
    ):
        reasons.append("base_not_ancestor_of_checkpoint")
    return sorted(set(reasons))


def _exact_cleanup_status_candidate(
    snapshot: dict[str, Any],
    derived: dict[str, Any],
) -> bool:
    state = snapshot["canonical_state"]
    return bool(
        derived["reconciliation_status"] == "missing_path_branch_present"
        and derived["cleanup_eligibility"] == "applied"
        and derived["provenance_status"] == "complete"
        and derived["apply_containment"] == "current"
        and state["task_status"] == "completed"
        and state["review_status"] == "accepted"
        and state["apply_status"] == "applied"
        and state["worktree_status"] in {"retained", "recovery_required"}
        and state["resolution"] == "none"
        and not state["followup_present"]
        and _terminal_cleanup_evidence_valid(snapshot)
    )


def _terminal_cleanup_evidence_valid(snapshot: dict[str, Any]) -> bool:
    state = snapshot["canonical_state"]
    evidence = snapshot["cleanup_evidence"]
    git = snapshot["git_observations"]
    if (
        evidence["cleaned_at_digest"] is None
        or evidence["receipt_digest"] is None
        or evidence["branch_retained"] is not True
        or git["path_exists"]
        or git["path_registry_ref"] is not None
        or git["branch_registry_ref"] is not None
        or git["observed_branch_head"] != git["checkpoint_head"]
    ):
        return False
    eligibility = _cleanup_eligibility(snapshot)
    if evidence["kind"] == "applied":
        return bool(
            eligibility == "applied"
            and state["apply_status"] == "applied"
            and evidence["reason"] == "execution_apply_status=applied"
            and evidence["result_applied"] is True
            and snapshot["apply_evidence"]["applied_head"] == git["checkpoint_head"]
            and _containment_status(snapshot) == "current"
        )
    if evidence["kind"] == "no_change":
        return bool(
            eligibility == "no_change"
            and evidence["reason"] == "already_contained"
            and evidence["result_applied"] is False
        )
    if evidence["kind"] == "discard":
        expected_reason = (
            "review_status=rejected"
            if state["review_status"] == "rejected"
            else f"resolution={state['resolution']}"
        )
        return bool(
            eligibility == "discard"
            and evidence["reason"] == expected_reason
            and evidence["result_applied"] is False
        )
    return False


def _hibernation_evidence_valid(snapshot: dict[str, Any]) -> bool:
    state = snapshot["canonical_state"]
    git = snapshot["git_observations"]
    evidence = snapshot["hibernation_evidence"]
    return bool(
        state["task_status"] == "completed"
        and evidence["contract"] == HIBERNATION_CONTRACT
        and evidence["kind"] in {"disposable", "pooled"}
        and evidence["base_head"] == git["base_head"]
        and evidence["branch_head"] == git["checkpoint_head"]
        and evidence["hibernated_at_digest"] is not None
        and not git["path_exists"]
        and git["path_registry_ref"] is None
        and git["branch_registry_ref"] is None
        and git["observed_branch_head"] == git["checkpoint_head"]
        and git["observed_checkpoint_head"] == git["checkpoint_head"]
        and git["observed_base_head"] == git["base_head"]
        and git["base_is_ancestor_of_observed_branch"] is True
    )


def _provenance_evidence(task: dict[str, Any]) -> dict[str, Any]:
    history = task.get("execution_mutation_provenance_history")
    count = len(history) if isinstance(history, list) else 0
    digest = _value_digest(history) if count else None
    value = _provenance(task)
    raw_status = str(value.get("status") or "")
    parse_valid = count > 0 and raw_status not in {"", "missing", "invalid"}
    return {
        "history_count": count,
        "history_digest": digest,
        "parse_valid": parse_valid,
        "view_status": (
            raw_status
            if raw_status in {"mutation_observed", "no_mutation_observed"}
            else "unknown"
        ),
        "unsafe_or_unreported": bool(value.get("unsafe_or_unreported")),
    }


def _registry(
    repo_root: Path | None,
    cache: dict[Path, list[dict[str, str]] | None],
) -> list[dict[str, str]] | None:
    if repo_root is None:
        return None
    if repo_root not in cache:
        try:
            cache[repo_root] = worktree_registry(repo_root)
        except (OSError, subprocess.SubprocessError):
            cache[repo_root] = None
    return cache[repo_root]


def _repo_root(task: dict[str, Any]) -> Path | None:
    raw = task.get("execution_repo_root") or task.get("project_root") or task.get("cwd")
    path = _path(raw)
    if path is None or not git_optional(path, "rev-parse", "--show-toplevel"):
        return None
    return path


def _path(value: object) -> Path | None:
    if value in (None, ""):
        return None
    try:
        return Path(str(value)).expanduser().resolve()
    except OSError:
        return None


def _resolve_commit(repo_root: Path | None, commit: str | None) -> str | None:
    if repo_root is None or commit is None:
        return None
    return _commit(
        git_optional(repo_root, "rev-parse", "--verify", f"{commit}^{{commit}}")
    )


def _resolve_ref(repo_root: Path | None, ref: str) -> str | None:
    if repo_root is None or not ref:
        return None
    return _commit(
        git_optional(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    )


def _entry_path_ref(entry: dict[str, str] | None) -> str | None:
    if not entry or not entry.get("path"):
        return None
    return _opaque_ref("path", entry["path"])


def _opaque_ref(kind: str, value: object) -> str | None:
    if value in (None, ""):
        return None
    return f"{kind}:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _commit(value: object) -> str | None:
    token = str(value or "").strip().lower()
    return token if COMMIT.fullmatch(token) else None


def _enum(value: object, allowed: set[str]) -> str:
    token = str(value or "").strip()
    return token if token in allowed else "unknown"


def _resolution(value: object) -> str:
    token = str(value or "").strip()
    if not token:
        return "none"
    return token if token in RESOLUTIONS - {"none", "other"} else "other"


def _strict_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_value_digest(value: object) -> str | None:
    return None if value in (None, "") else _value_digest(value)


def _value_digest(value: object) -> str:
    return _digest({"value": value})


def _grandfathered(derived: dict[str, Any]) -> bool:
    return derived["reconciliation_status"] in {
        "cleanup_evidence_invalid",
        "hibernation_evidence_invalid",
        "missing_path_branch_missing",
        "missing_path_branch_present",
        "registry_path_mismatch",
        "registry_unavailable",
    }


def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    actions = Counter(item["action_class"] for item in items)
    return {
        "task_count": len(items),
        "grandfathered_row_count": sum(item["grandfathered_row"] for item in items),
        "action_by_class": dict(sorted(actions.items())),
    }


def _validate_item(item: object) -> None:
    if not isinstance(item, dict) or set(item) != {
        "task_id",
        "action_class",
        "reason_codes",
        "grandfathered_row",
        "metadata_delta",
        "derived",
        "source_snapshot",
        "source_snapshot_digest",
    }:
        raise WorktreeReconciliationPlanValidationError("item fields are not canonical")
    if (
        not isinstance(item["task_id"], str)
        or not item["task_id"]
        or len(item["task_id"]) > 256
        or any(character.isspace() for character in item["task_id"])
        or "/" in item["task_id"]
        or "\\" in item["task_id"]
    ):
        raise WorktreeReconciliationPlanValidationError("task id is invalid")
    _validate_snapshot(item["source_snapshot"])
    expected_derived = _derive(item["source_snapshot"])
    if item["derived"] != expected_derived:
        raise WorktreeReconciliationPlanValidationError(
            "derived projection does not match source facts"
        )
    expected_action, expected_reasons, expected_delta = _classify(
        item["source_snapshot"], expected_derived
    )
    if (
        item["action_class"] not in ACTION_CLASSES
        or item["action_class"] != expected_action
        or item["reason_codes"] != sorted(set(expected_reasons))
        or item["metadata_delta"] != expected_delta
    ):
        raise WorktreeReconciliationPlanValidationError(
            "action projection does not match source facts"
        )
    if item["grandfathered_row"] is not _grandfathered(expected_derived):
        raise WorktreeReconciliationPlanValidationError(
            "grandfathered marker does not match source facts"
        )
    expected_digest = _digest(item["source_snapshot"])
    if item["source_snapshot_digest"] != expected_digest:
        raise WorktreeReconciliationPlanValidationError(
            "source snapshot digest mismatch"
        )


def _validate_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "canonical_state",
        "git_observations",
        "apply_evidence",
        "cleanup_evidence",
        "hibernation_evidence",
        "provenance_evidence",
    }:
        raise WorktreeReconciliationPlanValidationError(
            "source snapshot fields are invalid"
        )
    _validate_canonical_state(snapshot["canonical_state"])
    _validate_git_observations(snapshot["git_observations"])
    _validate_apply_evidence(snapshot["apply_evidence"])
    _validate_cleanup_evidence(snapshot["cleanup_evidence"])
    _validate_hibernation_evidence(snapshot["hibernation_evidence"])
    _validate_provenance_evidence(snapshot["provenance_evidence"])


def _validate_canonical_state(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "execution_mode",
        "task_status",
        "review_status",
        "apply_status",
        "worktree_status",
        "resolution",
        "resolution_digest",
        "followup_present",
        "followup_digest",
    }:
        raise WorktreeReconciliationPlanValidationError("canonical state is invalid")
    if (
        value["execution_mode"] not in {"git_worktree", "main_worktree", "unknown"}
        or value["task_status"] not in TASK_STATUSES
        or value["review_status"] not in REVIEW_STATUSES
        or value["apply_status"] not in APPLY_STATUSES
        or value["worktree_status"] not in WORKTREE_STATUSES
        or value["resolution"] not in RESOLUTIONS
        or not isinstance(value["followup_present"], bool)
    ):
        raise WorktreeReconciliationPlanValidationError(
            "canonical state enum is invalid"
        )
    _validate_optional_digest(value["resolution_digest"], "resolution digest")
    _validate_optional_digest(value["followup_digest"], "followup digest")
    if (value["resolution"] == "none") is not (value["resolution_digest"] is None):
        raise WorktreeReconciliationPlanValidationError(
            "resolution presence is inconsistent"
        )
    if value["followup_present"] is not (value["followup_digest"] is not None):
        raise WorktreeReconciliationPlanValidationError(
            "followup presence is inconsistent"
        )


def _validate_git_observations(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "repository_ref",
        "branch_ref",
        "base_head",
        "observed_base_head",
        "checkpoint_head",
        "observed_checkpoint_head",
        "observed_branch_head",
        "base_is_ancestor_of_observed_branch",
        "registry_available",
        "path_exists",
        "path_registry_ref",
        "branch_registry_ref",
        "worktree_dirty",
    }:
        raise WorktreeReconciliationPlanValidationError("git observations are invalid")
    for key in (
        "repository_ref",
        "branch_ref",
        "path_registry_ref",
        "branch_registry_ref",
    ):
        if value[key] is not None and not OPAQUE_REF.fullmatch(value[key]):
            raise WorktreeReconciliationPlanValidationError(f"{key} is invalid")
    for key in (
        "base_head",
        "observed_base_head",
        "checkpoint_head",
        "observed_checkpoint_head",
        "observed_branch_head",
    ):
        if value[key] is not None and not COMMIT.fullmatch(value[key]):
            raise WorktreeReconciliationPlanValidationError(f"{key} is invalid")
    if not isinstance(value["registry_available"], bool) or not isinstance(
        value["path_exists"], bool
    ):
        raise WorktreeReconciliationPlanValidationError(
            "git observation booleans are invalid"
        )
    for key in ("base_is_ancestor_of_observed_branch", "worktree_dirty"):
        if value[key] not in (True, False, None):
            raise WorktreeReconciliationPlanValidationError(f"{key} is invalid")
    if value["repository_ref"] is None and value["registry_available"]:
        raise WorktreeReconciliationPlanValidationError(
            "registry cannot be available without repository"
        )
    if value["branch_ref"] is None and (
        value["branch_registry_ref"] is not None
        or value["observed_branch_head"] is not None
    ):
        raise WorktreeReconciliationPlanValidationError(
            "branch observations require canonical branch binding"
        )
    if value["repository_ref"] is None and any(
        value[key] is not None
        for key in (
            "observed_base_head",
            "observed_checkpoint_head",
            "observed_branch_head",
        )
    ):
        raise WorktreeReconciliationPlanValidationError(
            "resolved Git observations require repository binding"
        )
    if not value["registry_available"] and (
        value["path_registry_ref"] is not None
        or value["branch_registry_ref"] is not None
    ):
        raise WorktreeReconciliationPlanValidationError(
            "registry refs require available registry"
        )
    if value["observed_base_head"] is not None and value["base_head"] is None:
        raise WorktreeReconciliationPlanValidationError(
            "observed base requires canonical base"
        )
    if (
        value["observed_checkpoint_head"] is not None
        and value["checkpoint_head"] is None
    ):
        raise WorktreeReconciliationPlanValidationError(
            "observed checkpoint requires canonical checkpoint"
        )
    ancestry_inputs = (
        value["observed_base_head"] is not None
        and value["observed_branch_head"] is not None
    )
    if (
        value["base_is_ancestor_of_observed_branch"] is not None
    ) is not ancestry_inputs:
        raise WorktreeReconciliationPlanValidationError(
            "base ancestry observation is inconsistent"
        )
    attached = (
        value["path_exists"]
        and value["path_registry_ref"] is not None
        and value["path_registry_ref"] == value["branch_registry_ref"]
    )
    if (value["worktree_dirty"] is not None) is not attached:
        raise WorktreeReconciliationPlanValidationError(
            "dirty observation is inconsistent with registry attachment"
        )


def _validate_apply_evidence(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "applied_head",
        "applied_at_digest",
        "apply_target_ref",
        "observed_apply_target_head",
        "applied_head_is_ancestor_of_target",
    }:
        raise WorktreeReconciliationPlanValidationError("apply evidence is invalid")
    for key in ("applied_head", "observed_apply_target_head"):
        if value[key] is not None and not COMMIT.fullmatch(value[key]):
            raise WorktreeReconciliationPlanValidationError(f"{key} is invalid")
    if value["apply_target_ref"] is not None and not OPAQUE_REF.fullmatch(
        value["apply_target_ref"]
    ):
        raise WorktreeReconciliationPlanValidationError("apply target ref is invalid")
    _validate_optional_digest(value["applied_at_digest"], "applied at digest")
    if value["applied_head_is_ancestor_of_target"] not in (True, False, None):
        raise WorktreeReconciliationPlanValidationError(
            "apply containment observation is invalid"
        )
    containment_inputs = (
        value["applied_head"] is not None
        and value["observed_apply_target_head"] is not None
    )
    if (
        value["applied_head_is_ancestor_of_target"] is not None
    ) is not containment_inputs:
        raise WorktreeReconciliationPlanValidationError(
            "apply containment inputs are inconsistent"
        )


def _validate_cleanup_evidence(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "reason",
        "branch_retained",
        "result_applied",
        "cleaned_at_digest",
        "receipt_digest",
    }:
        raise WorktreeReconciliationPlanValidationError("cleanup evidence is invalid")
    if value["kind"] not in CLEANUP_KINDS or value["reason"] not in CLEANUP_REASONS:
        raise WorktreeReconciliationPlanValidationError(
            "cleanup evidence enum is invalid"
        )
    if value["branch_retained"] not in (True, False, None) or value[
        "result_applied"
    ] not in (True, False, None):
        raise WorktreeReconciliationPlanValidationError(
            "cleanup evidence booleans are invalid"
        )
    _validate_optional_digest(value["cleaned_at_digest"], "cleaned at digest")
    if not isinstance(value["receipt_digest"], str) or not HEX_DIGEST.fullmatch(
        value["receipt_digest"]
    ):
        raise WorktreeReconciliationPlanValidationError(
            "cleanup receipt digest is invalid"
        )


def _validate_hibernation_evidence(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "contract",
        "kind",
        "base_head",
        "branch_head",
        "hibernated_at_digest",
    }:
        raise WorktreeReconciliationPlanValidationError(
            "hibernation evidence is invalid"
        )
    if value["contract"] not in {HIBERNATION_CONTRACT, "unknown"} or value[
        "kind"
    ] not in {"disposable", "pooled", "unknown"}:
        raise WorktreeReconciliationPlanValidationError(
            "hibernation evidence enum is invalid"
        )
    for key in ("base_head", "branch_head"):
        if value[key] is not None and not COMMIT.fullmatch(value[key]):
            raise WorktreeReconciliationPlanValidationError(f"{key} is invalid")
    _validate_optional_digest(value["hibernated_at_digest"], "hibernated at digest")


def _validate_provenance_evidence(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "history_count",
        "history_digest",
        "parse_valid",
        "view_status",
        "unsafe_or_unreported",
    }:
        raise WorktreeReconciliationPlanValidationError(
            "provenance evidence is invalid"
        )
    if (
        not isinstance(value["history_count"], int)
        or isinstance(value["history_count"], bool)
        or value["history_count"] < 0
        or not isinstance(value["parse_valid"], bool)
        or value["view_status"]
        not in {"mutation_observed", "no_mutation_observed", "unknown"}
        or not isinstance(value["unsafe_or_unreported"], bool)
    ):
        raise WorktreeReconciliationPlanValidationError(
            "provenance evidence values are invalid"
        )
    _validate_optional_digest(value["history_digest"], "provenance history digest")
    if (value["history_count"] > 0) is not (value["history_digest"] is not None):
        raise WorktreeReconciliationPlanValidationError(
            "provenance history presence is inconsistent"
        )
    if not value["parse_valid"] and value["view_status"] != "unknown":
        raise WorktreeReconciliationPlanValidationError(
            "invalid provenance parse cannot have complete view status"
        )
    if value["parse_valid"] and value["history_count"] == 0:
        raise WorktreeReconciliationPlanValidationError(
            "valid provenance parse requires history"
        )


def _validate_optional_digest(value: object, label: str) -> None:
    if value is not None and (
        not isinstance(value, str) or not HEX_DIGEST.fullmatch(value)
    ):
        raise WorktreeReconciliationPlanValidationError(f"{label} is invalid")


def _digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()

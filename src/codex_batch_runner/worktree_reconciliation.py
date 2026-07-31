from __future__ import annotations

import hashlib
import json
import re
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
)
from .worktree_hibernation import _provenance, _task_projection


CONTRACT = "worktree-reconciliation-plan-v1"
ACTION_CLASSES = {
    "no_action",
    "manual_review",
    "exact_repair_candidate",
    "unrecoverable_without_owner_decision",
}
REASON_CODES = {
    "active_or_resumable_task",
    "ambiguous_or_missing_provenance",
    "attached_state_current",
    "dirty_or_uncheckpointed",
    "exact_terminal_cleanup_receipt",
    "intentional_hibernation_current",
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
REGISTRY_STATES = {
    "available",
    "repository_unavailable",
    "registry_unavailable",
}
RECONCILIATION_STATUSES = {
    "attached_current",
    "dirty_or_uncheckpointed",
    "hibernated_current",
    "missing_path_branch_missing",
    "missing_path_branch_present",
    "not_applicable",
    "registry_path_mismatch",
    "registry_unavailable",
    "terminal_cleanup_current",
}
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
        _task_plan(config, task, registry_cache)
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
            f"reconciliation={item['source_snapshot']['classification']['reconciliation_status']} "
            f"source_digest={item['source_snapshot_digest']}"
        )
    return "\n".join(lines) + "\n"


def _task_plan(
    config: Config,
    task: dict[str, Any],
    registry_cache: dict[Path, list[dict[str, str]] | None],
) -> dict[str, Any]:
    projection = _task_projection(config, task, registry_cache)
    snapshot = _source_snapshot(task, projection, registry_cache)
    action, reasons, delta = _classify(task, projection, snapshot)
    return {
        "task_id": str(task.get("id") or ""),
        "action_class": action,
        "reason_codes": sorted(set(reasons)),
        "grandfathered_row": _grandfathered(projection),
        "metadata_delta": delta,
        "source_snapshot": snapshot,
        "source_snapshot_digest": _digest(snapshot),
    }


def _source_snapshot(
    task: dict[str, Any],
    projection: dict[str, Any],
    registry_cache: dict[Path, list[dict[str, str]] | None],
) -> dict[str, Any]:
    repo_root = _repo_root(task)
    branch = str(task.get("execution_branch") or "").strip()
    path = _path(task.get("execution_worktree_path"))
    registry = registry_cache.get(repo_root) if repo_root is not None else None
    if repo_root is None:
        registry_state = "repository_unavailable"
    elif registry is None:
        registry_state = "registry_unavailable"
    else:
        registry_state = "available"
    path_exists = bool(path is not None and path.exists())
    path_entry = (
        registry_entry_for_path(registry, path)
        if registry is not None and path is not None and path_exists
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
    return {
        "canonical_state": {
            "execution_mode": _enum(
                task.get("execution_mode"), {"git_worktree", "main_worktree", "unknown"}
            ),
            "task_status": _enum(task.get("status"), TASK_STATUSES),
            "review_status": _enum(task.get("review_status"), REVIEW_STATUSES),
            "apply_status": _enum(task.get("execution_apply_status"), APPLY_STATUSES),
            "worktree_status": _enum(
                task.get("execution_worktree_status"), WORKTREE_STATUSES
            ),
            "resolution_present": bool(task.get("resolution")),
            "followup_present": bool(task.get("chain_status")),
        },
        "git_binding": {
            "repository_ref": _opaque_ref("repo", repo_root),
            "branch_ref": _opaque_ref("branch", branch or None),
            "base_head": _commit(task.get("execution_base_head")),
            "checkpoint_head": _commit(
                task.get("execution_branch_head")
                or task.get("execution_commit")
                or task.get("execution_hibernation_branch_head")
            ),
            "observed_branch_head": _commit(branch_state.get("head")),
            "registry_state": registry_state,
            "path_exists": path_exists,
            "path_registry_ref": _entry_path_ref(path_entry),
            "branch_registry_ref": _entry_path_ref(branch_entry),
        },
        "classification": {
            "reconciliation_status": projection["reconciliation"]["status"],
            "branch_only_review_compatible": projection["branch_only_review"][
                "compatible"
            ],
            "hibernation_compatible": projection["hibernation"]["compatible"],
            "reattach_compatible": projection["reattach"]["compatible"],
            "resume_compatible": projection["resume"]["compatible"],
            "cleanup_eligibility": _cleanup_eligibility(task),
        },
        "cleanup_receipt": {
            "kind": _enum(
                task.get("execution_cleanup_kind"),
                {"applied", "discard", "no_change", "unknown"},
            ),
            "reason": _enum(
                task.get("execution_cleanup_reason"),
                {
                    "execution_apply_status=applied",
                    "review_status=rejected",
                    "resolution=duplicate",
                    "resolution=manual",
                    "resolution=superseded",
                    "resolution=wont_fix",
                    "already_contained",
                    "unknown",
                },
            ),
            "branch_retained": _strict_bool(
                task.get("execution_cleanup_branch_retained")
            ),
            "result_applied": _strict_bool(
                task.get("execution_cleanup_result_applied")
            ),
            "cleaned_at_present": bool(task.get("execution_cleaned_at")),
            "applied_head": _commit(task.get("execution_applied_head")),
            "apply_target_ref": _opaque_ref(
                "branch", str(task.get("execution_apply_target") or "").strip() or None
            ),
            "apply_target_contains_applied_head": _applied_target_contains_head(
                task, repo_root
            ),
        },
        "provenance": _safe_provenance(task),
    }


def _classify(
    task: dict[str, Any],
    projection: dict[str, Any],
    snapshot: dict[str, Any],
) -> tuple[str, list[str], list[dict[str, str]]]:
    del task, projection
    return _classify_snapshot(snapshot)


def _classify_snapshot(
    snapshot: dict[str, Any],
) -> tuple[str, list[str], list[dict[str, str]]]:
    classification = snapshot["classification"]
    reconciliation = classification["reconciliation_status"]
    state = snapshot["canonical_state"]
    binding = snapshot["git_binding"]
    provenance = snapshot["provenance"]

    if state["execution_mode"] != "git_worktree":
        return "no_action", ["non_worktree_task"], []
    if reconciliation == "terminal_cleanup_current":
        return "no_action", ["terminal_cleanup_current"], []
    if reconciliation == "hibernated_current":
        return "no_action", ["intentional_hibernation_current"], []
    if reconciliation == "attached_current":
        if state["task_status"] in {"runnable", "running", "needs_resume"}:
            return "manual_review", ["active_or_resumable_task"], []
        return "no_action", ["attached_state_current"], []
    if reconciliation == "dirty_or_uncheckpointed":
        return "manual_review", ["dirty_or_uncheckpointed"], []
    if reconciliation == "registry_path_mismatch":
        return (
            "unrecoverable_without_owner_decision",
            ["registry_mismatch"],
            [],
        )
    if binding["registry_state"] != "available":
        return (
            "unrecoverable_without_owner_decision",
            ["registry_evidence_unavailable"],
            [],
        )
    missing = []
    if binding["base_head"] is None:
        missing.append("missing_execution_base")
    if binding["checkpoint_head"] is None:
        missing.append("missing_checkpoint")
    if binding["observed_branch_head"] is None:
        missing.append("missing_branch")
    if missing:
        return "unrecoverable_without_owner_decision", missing, []
    if state["task_status"] in {"runnable", "running", "needs_resume"}:
        return "manual_review", ["active_or_resumable_task"], []
    if provenance["status"] != "complete":
        return "manual_review", ["ambiguous_or_missing_provenance"], []
    if _exact_cleanup_status_candidate(snapshot, reconciliation):
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
    if reconciliation in {
        "missing_path_branch_present",
        "missing_path_branch_missing",
    }:
        return "manual_review", ["missing_path_is_not_lifecycle_evidence"], []
    return (
        "manual_review",
        ["result_review_apply_state_ambiguous"],
        [],
    )


def _exact_cleanup_status_candidate(
    snapshot: dict[str, Any],
    reconciliation: str,
) -> bool:
    state = snapshot["canonical_state"]
    binding = snapshot["git_binding"]
    receipt = snapshot["cleanup_receipt"]
    return bool(
        reconciliation == "missing_path_branch_present"
        and snapshot["classification"]["cleanup_eligibility"] == "applied"
        and state["task_status"] in {"completed", "archived"}
        and state["review_status"] == "accepted"
        and state["apply_status"] == "applied"
        and state["worktree_status"] in {"retained", "recovery_required"}
        and receipt
        == {
            "kind": "applied",
            "reason": "execution_apply_status=applied",
            "branch_retained": True,
            "result_applied": True,
            "cleaned_at_present": True,
            "applied_head": receipt["applied_head"],
            "apply_target_ref": receipt["apply_target_ref"],
            "apply_target_contains_applied_head": True,
        }
        and receipt["applied_head"] is not None
        and receipt["apply_target_ref"] is not None
        and binding["checkpoint_head"] == binding["observed_branch_head"]
        and state["resolution_present"] is False
        and state["followup_present"] is False
    )


def _safe_provenance(task: dict[str, Any]) -> dict[str, Any]:
    value = _provenance(task)
    status = value.get("status")
    complete = (
        status in {"mutation_observed", "no_mutation_observed"}
        and value.get("unsafe_or_unreported") is False
    )
    return {
        "status": "complete" if complete else "ambiguous_or_missing",
        "unsafe_or_unreported": bool(value.get("unsafe_or_unreported")),
    }


def _cleanup_eligibility(task: dict[str, Any]) -> str:
    value = cleanup_eligibility(task)
    kind = value.get("cleanup_kind")
    return kind if kind in {"applied", "discard", "no_change"} else "blocked"


def _applied_target_contains_head(
    task: dict[str, Any], repo_root: Path | None
) -> bool | None:
    target = str(task.get("execution_apply_target") or "").strip()
    head = _commit(task.get("execution_applied_head"))
    if repo_root is None or not target or head is None:
        return None
    target_head = git_optional(
        repo_root, "rev-parse", "--verify", f"{target}^{{commit}}"
    )
    if not target_head:
        return None
    return is_ancestor(repo_root, head, target_head)


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


def _strict_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _grandfathered(projection: dict[str, Any]) -> bool:
    return projection["reconciliation"]["status"] in {
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
    if item["action_class"] not in ACTION_CLASSES:
        raise WorktreeReconciliationPlanValidationError("action class is invalid")
    if (
        not isinstance(item["reason_codes"], list)
        or item["reason_codes"] != sorted(set(item["reason_codes"]))
        or not item["reason_codes"]
        or any(reason not in REASON_CODES for reason in item["reason_codes"])
    ):
        raise WorktreeReconciliationPlanValidationError("reason codes are invalid")
    if not isinstance(item["grandfathered_row"], bool):
        raise WorktreeReconciliationPlanValidationError(
            "grandfathered marker is invalid"
        )
    _validate_snapshot(item["source_snapshot"])
    expected_grandfathered = item["source_snapshot"]["classification"][
        "reconciliation_status"
    ] in {
        "missing_path_branch_missing",
        "missing_path_branch_present",
        "registry_path_mismatch",
        "registry_unavailable",
    }
    if item["grandfathered_row"] is not expected_grandfathered:
        raise WorktreeReconciliationPlanValidationError(
            "grandfathered marker does not match source snapshot"
        )
    _validate_delta(item)
    expected_action, expected_reasons, expected_delta = _classify_snapshot(
        item["source_snapshot"]
    )
    if (
        item["action_class"] != expected_action
        or item["reason_codes"] != sorted(set(expected_reasons))
        or item["metadata_delta"] != expected_delta
    ):
        raise WorktreeReconciliationPlanValidationError(
            "action projection does not match source snapshot"
        )
    expected = _digest(item["source_snapshot"])
    if item["source_snapshot_digest"] != expected:
        raise WorktreeReconciliationPlanValidationError(
            "source snapshot digest mismatch"
        )


def _validate_delta(item: dict[str, Any]) -> None:
    delta = item["metadata_delta"]
    expected = (
        [
            {
                "field": "execution_worktree_status",
                "before": item["source_snapshot"]["canonical_state"]["worktree_status"],
                "after": "cleaned",
            }
        ]
        if item["action_class"] == "exact_repair_candidate"
        else []
    )
    if delta != expected or (
        delta and delta[0]["before"] not in {"retained", "recovery_required"}
    ):
        raise WorktreeReconciliationPlanValidationError("metadata delta is invalid")


def _validate_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "canonical_state",
        "git_binding",
        "classification",
        "cleanup_receipt",
        "provenance",
    }:
        raise WorktreeReconciliationPlanValidationError(
            "source snapshot fields are invalid"
        )
    state = snapshot["canonical_state"]
    if (
        not isinstance(state, dict)
        or set(state)
        != {
            "execution_mode",
            "task_status",
            "review_status",
            "apply_status",
            "worktree_status",
            "resolution_present",
            "followup_present",
        }
        or state["execution_mode"] not in {"git_worktree", "main_worktree", "unknown"}
        or state["task_status"] not in TASK_STATUSES
        or state["review_status"] not in REVIEW_STATUSES
        or state["apply_status"] not in APPLY_STATUSES
        or state["worktree_status"] not in WORKTREE_STATUSES
        or not isinstance(state["resolution_present"], bool)
        or not isinstance(state["followup_present"], bool)
    ):
        raise WorktreeReconciliationPlanValidationError("canonical state is invalid")
    binding = snapshot["git_binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "repository_ref",
        "branch_ref",
        "base_head",
        "checkpoint_head",
        "observed_branch_head",
        "registry_state",
        "path_exists",
        "path_registry_ref",
        "branch_registry_ref",
    }:
        raise WorktreeReconciliationPlanValidationError("git binding is invalid")
    for key in (
        "repository_ref",
        "branch_ref",
        "path_registry_ref",
        "branch_registry_ref",
    ):
        if binding[key] is not None and not OPAQUE_REF.fullmatch(binding[key]):
            raise WorktreeReconciliationPlanValidationError(f"{key} is invalid")
    for key in ("base_head", "checkpoint_head", "observed_branch_head"):
        if binding[key] is not None and not COMMIT.fullmatch(binding[key]):
            raise WorktreeReconciliationPlanValidationError(f"{key} is invalid")
    if binding["registry_state"] not in REGISTRY_STATES or not isinstance(
        binding["path_exists"], bool
    ):
        raise WorktreeReconciliationPlanValidationError("registry binding is invalid")
    classification = snapshot["classification"]
    if (
        not isinstance(classification, dict)
        or set(classification)
        != {
            "reconciliation_status",
            "branch_only_review_compatible",
            "hibernation_compatible",
            "reattach_compatible",
            "resume_compatible",
            "cleanup_eligibility",
        }
        or classification["reconciliation_status"] not in RECONCILIATION_STATUSES
        or any(
            classification[key] not in (True, False, None)
            for key in (
                "branch_only_review_compatible",
                "hibernation_compatible",
                "reattach_compatible",
                "resume_compatible",
            )
        )
        or classification["cleanup_eligibility"]
        not in {"applied", "discard", "no_change", "blocked"}
    ):
        raise WorktreeReconciliationPlanValidationError("classification is invalid")
    receipt = snapshot["cleanup_receipt"]
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {
            "kind",
            "reason",
            "branch_retained",
            "result_applied",
            "cleaned_at_present",
            "applied_head",
            "apply_target_ref",
            "apply_target_contains_applied_head",
        }
        or receipt["kind"] not in {"applied", "discard", "no_change", "unknown"}
        or receipt["reason"]
        not in {
            "execution_apply_status=applied",
            "review_status=rejected",
            "resolution=duplicate",
            "resolution=manual",
            "resolution=superseded",
            "resolution=wont_fix",
            "already_contained",
            "unknown",
        }
        or receipt["branch_retained"] not in (True, False, None)
        or receipt["result_applied"] not in (True, False, None)
        or not isinstance(receipt["cleaned_at_present"], bool)
        or (
            receipt["applied_head"] is not None
            and not COMMIT.fullmatch(receipt["applied_head"])
        )
        or (
            receipt["apply_target_ref"] is not None
            and not OPAQUE_REF.fullmatch(receipt["apply_target_ref"])
        )
        or receipt["apply_target_contains_applied_head"] not in (True, False, None)
    ):
        raise WorktreeReconciliationPlanValidationError("cleanup receipt is invalid")
    provenance = snapshot["provenance"]
    if (
        not isinstance(provenance, dict)
        or set(provenance) != {"status", "unsafe_or_unreported"}
        or provenance["status"] not in {"complete", "ambiguous_or_missing"}
        or not isinstance(provenance["unsafe_or_unreported"], bool)
    ):
        raise WorktreeReconciliationPlanValidationError("provenance is invalid")


def _digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()

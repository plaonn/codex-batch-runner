from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .execution_mutation_provenance import (
    ExecutionMutationProvenanceError,
    execution_mutation_provenance_view,
)
from .queue import list_tasks_read_only
from .worktree import (
    WORKTREE_HIBERNATION_CONTRACT,
    WORKTREE_RETAINED_STATUSES,
    git_optional,
    is_ancestor,
    local_branch_state,
    registry_entry_for_branch,
    registry_entry_for_path,
    rev_list,
    worktree_registry,
)
from .worktree_pool import load_worktree_pool_policy, validate_pool_lease


CONTRACT = "worktree-hibernation-plan-v1"

REASON_CODES = {
    "branch_only_review_candidate",
    "checkpoint_head_mismatch",
    "checkpoint_head_missing",
    "dirty_worktree",
    "execution_base_head_missing",
    "execution_branch_missing",
    "hibernation_candidate",
    "hibernated_intent_current",
    "intentional_hibernation_not_supported_v1",
    "missing_mutation_provenance",
    "missing_worktree_path",
    "mutation_provenance_incomplete",
    "needs_resume_requires_retained_cwd",
    "non_worktree_task",
    "not_completed",
    "pool_lease_current",
    "pool_lease_inconsistent",
    "pool_metadata_incomplete",
    "pool_not_applicable",
    "reattach_not_applicable",
    "reattach_candidate",
    "repository_unavailable",
    "registry_path_mismatch",
    "registry_unavailable",
    "resume_incompatible_recreated_cwd",
    "resume_not_applicable",
    "retained_cwd_available",
    "review_unit_empty",
    "review_unit_not_reconstructable",
    "task_active",
    "terminal_cleanup_owned_elsewhere",
    "unsafe_or_unreported_mutation",
    "worktree_attached_current",
    "worktree_path_unregistered",
    "worktree_status_not_retained",
}

RECONCILIATION_STATUSES = {
    "attached_current",
    "dirty_or_uncheckpointed",
    "missing_path_branch_missing",
    "missing_path_branch_present",
    "not_applicable",
    "registry_path_mismatch",
    "registry_unavailable",
    "terminal_cleanup_current",
    "hibernated_current",
}


class WorktreeHibernationPlanValidationError(ValueError):
    pass


def build_worktree_hibernation_plan(
    config: Config,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Build a read-only compatibility projection without changing runtime state."""
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
        _task_projection(config, task, registry_cache)
        for task in sorted(tasks, key=lambda value: str(value.get("id") or ""))
    ]
    report: dict[str, Any] = {
        "schema_version": CONTRACT,
        "mode": "report-only",
        "mutation": {
            "performed": False,
            "supported": False,
            "worktree_changes_supported": False,
            "task_state_changes_supported": False,
        },
        "filters": {
            "task_id_applied": task_id is not None,
            "project_id_applied": project_id is not None,
        },
        "summary": _summary(items),
        "items": items,
    }
    report["report_digest"] = _digest(report)
    return validate_worktree_hibernation_plan(report)


def validate_worktree_hibernation_plan(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorktreeHibernationPlanValidationError("report must be an object")
    if set(value) != {
        "schema_version",
        "mode",
        "mutation",
        "filters",
        "summary",
        "items",
        "report_digest",
    }:
        raise WorktreeHibernationPlanValidationError("report fields are not canonical")
    if value["schema_version"] != CONTRACT or value["mode"] != "report-only":
        raise WorktreeHibernationPlanValidationError(
            "unsupported worktree hibernation plan"
        )
    if value["mutation"] != {
        "performed": False,
        "supported": False,
        "worktree_changes_supported": False,
        "task_state_changes_supported": False,
    }:
        raise WorktreeHibernationPlanValidationError("mutation boundary is invalid")
    if (
        not isinstance(value["filters"], dict)
        or set(value["filters"])
        != {
            "task_id_applied",
            "project_id_applied",
        }
        or any(not isinstance(item, bool) for item in value["filters"].values())
    ):
        raise WorktreeHibernationPlanValidationError("filters are invalid")
    if not isinstance(value["items"], list):
        raise WorktreeHibernationPlanValidationError("items must be an array")
    task_ids: list[str] = []
    for item in value["items"]:
        _validate_item(item)
        task_ids.append(item["task_id"])
    if task_ids != sorted(set(task_ids)):
        raise WorktreeHibernationPlanValidationError(
            "items must be sorted by unique task id"
        )
    if value["summary"] != _summary(value["items"]):
        raise WorktreeHibernationPlanValidationError("summary does not match items")
    expected = _digest(
        {key: item for key, item in value.items() if key != "report_digest"}
    )
    if value["report_digest"] != expected:
        raise WorktreeHibernationPlanValidationError("report digest mismatch")
    return value


def render_worktree_hibernation_plan(report: dict[str, Any]) -> str:
    lines = [
        f"schema_version: {report['schema_version']}",
        "mode: report-only",
        "mutation_performed: false",
        f"tasks: {report['summary']['task_count']}",
        f"branch_only_review_candidates: {report['summary']['branch_only_review_candidates']}",
        f"hibernation_candidates: {report['summary']['hibernation_candidates']}",
        f"report_digest: {report['report_digest']}",
    ]
    for item in report["items"]:
        lines.append(
            "  "
            + " ".join(
                (
                    f"task={item['task_id']}",
                    f"reconciliation={item['reconciliation']['status']}",
                    f"branch_review={_bool_text(item['branch_only_review']['compatible'])}",
                    f"hibernate={_bool_text(item['hibernation']['compatible'])}",
                    f"resume={_bool_text(item['resume']['compatible'])}",
                )
            )
        )
    return "\n".join(lines) + "\n"


def _task_projection(
    config: Config,
    task: dict[str, Any],
    registry_cache: dict[Path, list[dict[str, str]] | None],
) -> dict[str, Any]:
    task_id = str(task.get("id") or "")
    raw_execution_mode = str(task.get("execution_mode") or "main_worktree")
    execution_mode = (
        raw_execution_mode
        if raw_execution_mode in {"git_worktree", "main_worktree"}
        else "unknown"
    )
    branch = str(task.get("execution_branch") or "").strip()
    base_head = str(task.get("execution_base_head") or "").strip()
    checkpoint = str(
        task.get("execution_branch_head") or task.get("execution_commit") or ""
    ).strip()
    worktree_status = _safe_token(task.get("execution_worktree_status"))
    repo_root = _repo_root(task)
    path = _worktree_path(task)

    if execution_mode != "git_worktree":
        return _item(
            task_id,
            execution_mode,
            worktree_status,
            None,
            _projection("not_applicable", ["non_worktree_task"]),
            _compatibility(False, ["non_worktree_task"]),
            _compatibility(False, ["non_worktree_task"]),
            _compatibility(False, ["reattach_not_applicable"]),
            _compatibility(None, ["resume_not_applicable"]),
            _compatibility(None, ["pool_not_applicable"]),
        )

    registry = _registry(repo_root, registry_cache)
    branch_state = (
        local_branch_state(repo_root, branch)
        if repo_root is not None and branch
        else {"exists": False, "head": None}
    )
    path_exists = bool(path and path.exists())
    path_entry = (
        registry_entry_for_path(registry, path.resolve())
        if registry is not None and path_exists and path is not None
        else None
    )
    branch_entry = (
        registry_entry_for_branch(registry, branch)
        if registry is not None and branch
        else None
    )
    attached = bool(
        path_exists
        and path_entry
        and path_entry.get("branch") == f"refs/heads/{branch}"
    )
    dirty = attached and _worktree_is_dirty(task, repo_root, path)
    reconciliation = _reconciliation(
        worktree_status=worktree_status,
        registry_available=registry is not None,
        path=path,
        path_exists=path_exists,
        path_entry=path_entry,
        branch_entry=branch_entry,
        branch_exists=bool(branch_state.get("exists")),
        attached=attached,
        dirty=dirty,
        checkpoint=checkpoint,
    )
    intentional_hibernation = (
        worktree_status == "hibernated"
        and task.get("execution_hibernation_contract")
        == WORKTREE_HIBERNATION_CONTRACT
    )
    if (
        intentional_hibernation
        and not path_exists
        and branch_entry is None
        and branch_state.get("head") == checkpoint
    ):
        reconciliation = _projection(
            "hibernated_current", ["hibernated_intent_current"]
        )
    branch_review = _branch_review(
        repo_root, branch, base_head, checkpoint, branch_state
    )
    provenance = _provenance(task)
    pool_lease = _pool_lease(
        config,
        task,
        repo_root=repo_root,
        path=path,
        branch=branch,
        attached=attached,
    )
    hibernation = _hibernation(
        task,
        worktree_status=worktree_status,
        attached=attached,
        dirty=dirty,
        branch_review=branch_review,
        provenance=provenance,
        pool_lease=pool_lease,
    )
    if (
        intentional_hibernation
        and task.get("status") == "completed"
        and reconciliation["status"] == "hibernated_current"
        and branch_review["compatible"] is True
        and task.get("execution_hibernation_base_head") == base_head
        and task.get("execution_hibernation_branch_head") == checkpoint
    ):
        reattach = _compatibility(True, ["reattach_candidate"])
    elif intentional_hibernation:
        reattach = _compatibility(
            False,
            [
                *reconciliation["reason_codes"],
                *branch_review["reason_codes"],
            ],
        )
    else:
        reattach = _compatibility(
            False,
            ["intentional_hibernation_not_supported_v1"],
        )
    if task.get("status") == "needs_resume":
        if attached and worktree_status in WORKTREE_RETAINED_STATUSES:
            resume = _compatibility(True, ["retained_cwd_available"])
        else:
            resume = _compatibility(
                False,
                [
                    "needs_resume_requires_retained_cwd",
                    "resume_incompatible_recreated_cwd",
                ],
            )
    else:
        resume = _compatibility(None, ["resume_not_applicable"])

    return _item(
        task_id,
        execution_mode,
        worktree_status,
        _repo_ref(repo_root),
        reconciliation,
        branch_review,
        hibernation,
        reattach,
        resume,
        pool_lease,
    )


def _reconciliation(
    *,
    worktree_status: str,
    registry_available: bool,
    path: Path | None,
    path_exists: bool,
    path_entry: dict[str, str] | None,
    branch_entry: dict[str, str] | None,
    branch_exists: bool,
    attached: bool,
    dirty: bool,
    checkpoint: str,
) -> dict[str, Any]:
    if worktree_status == "cleaned":
        return _projection(
            "terminal_cleanup_current", ["terminal_cleanup_owned_elsewhere"]
        )
    if attached:
        if dirty or not checkpoint:
            reasons = ["dirty_worktree"] if dirty else []
            if not checkpoint:
                reasons.append("checkpoint_head_missing")
            return _projection("dirty_or_uncheckpointed", reasons)
        return _projection("attached_current", ["worktree_attached_current"])
    if not registry_available:
        return _projection("registry_unavailable", ["registry_unavailable"])
    if path_exists and not path_entry:
        return _projection("registry_path_mismatch", ["worktree_path_unregistered"])
    if path_exists or (branch_entry and path is not None):
        return _projection("registry_path_mismatch", ["registry_path_mismatch"])
    reasons = ["missing_worktree_path"]
    if branch_exists:
        return _projection("missing_path_branch_present", reasons)
    reasons.append("execution_branch_missing")
    return _projection("missing_path_branch_missing", reasons)


def _branch_review(
    repo_root: Path | None,
    branch: str,
    base_head: str,
    checkpoint: str,
    branch_state: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    if not branch or not branch_state.get("exists"):
        reasons.append("execution_branch_missing")
    if not base_head:
        reasons.append("execution_base_head_missing")
    if not checkpoint:
        reasons.append("checkpoint_head_missing")
    if reasons or repo_root is None:
        if repo_root is None:
            reasons.append("repository_unavailable")
        return _compatibility(False, [*reasons, "review_unit_not_reconstructable"])
    branch_head = str(branch_state.get("head") or "")
    base = git_optional(repo_root, "rev-parse", "--verify", f"{base_head}^{{commit}}")
    checkpoint_head = git_optional(
        repo_root, "rev-parse", "--verify", f"{checkpoint}^{{commit}}"
    )
    if not base or not branch_head or not checkpoint_head:
        return _compatibility(False, ["review_unit_not_reconstructable"])
    if checkpoint_head != branch_head:
        return _compatibility(
            False,
            ["checkpoint_head_mismatch", "review_unit_not_reconstructable"],
        )
    if not is_ancestor(repo_root, base, branch_head):
        return _compatibility(False, ["review_unit_not_reconstructable"])
    try:
        commits = rev_list(repo_root, f"{base}..{branch_head}")
    except subprocess.SubprocessError:
        return _compatibility(False, ["review_unit_not_reconstructable"])
    if not commits:
        return _compatibility(False, ["review_unit_empty"])
    return _compatibility(True, ["branch_only_review_candidate"])


def _hibernation(
    task: dict[str, Any],
    *,
    worktree_status: str,
    attached: bool,
    dirty: bool,
    branch_review: dict[str, Any],
    provenance: dict[str, Any],
    pool_lease: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    if task.get("status") == "needs_resume":
        reasons.append("needs_resume_requires_retained_cwd")
    elif task.get("status") in {"runnable", "running"}:
        reasons.append("task_active")
    elif task.get("status") != "completed":
        reasons.append("not_completed")
    if worktree_status not in WORKTREE_RETAINED_STATUSES:
        reasons.append("worktree_status_not_retained")
    if not attached:
        reasons.append("missing_worktree_path")
    if dirty:
        reasons.append("dirty_worktree")
    if branch_review["compatible"] is not True:
        reasons.extend(branch_review["reason_codes"])
    if provenance["status"] == "missing":
        reasons.append("missing_mutation_provenance")
    elif provenance["status"] in {"invalid", "unknown", "mutation_possible"}:
        reasons.append("mutation_provenance_incomplete")
    if provenance["unsafe_or_unreported"]:
        reasons.append("unsafe_or_unreported_mutation")
    if pool_lease["compatible"] is False:
        reasons.extend(pool_lease["reason_codes"])
    reasons = _reasons(reasons)
    if reasons:
        return _compatibility(False, reasons)
    return _compatibility(True, ["hibernation_candidate"])


def _provenance(task: dict[str, Any]) -> dict[str, Any]:
    history = task.get("execution_mutation_provenance_history")
    if not isinstance(history, list) or not history:
        return {"status": "missing", "unsafe_or_unreported": False}
    try:
        view = execution_mutation_provenance_view(
            task, as_of=datetime.max.replace(tzinfo=timezone.utc)
        )
    except (ExecutionMutationProvenanceError, TypeError, ValueError):
        return {"status": "invalid", "unsafe_or_unreported": False}
    attribution = view.get("attribution")
    return {
        "status": str(view.get("provenance") or "unknown"),
        "unsafe_or_unreported": bool(
            isinstance(attribution, dict)
            and attribution.get("unsafe_or_unreported_paths")
        ),
    }


def _worktree_is_dirty(
    task: dict[str, Any],
    repo_root: Path | None,
    path: Path | None,
) -> bool:
    if path is None:
        return False
    porcelain = git_optional(path, "status", "--porcelain")
    ignored = git_optional(
        path,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
    )
    if porcelain is None or ignored is None:
        return True
    entries = porcelain.splitlines()
    entries.extend(f"?? {relative}" for relative in ignored.splitlines() if relative)
    if not entries:
        return False
    if not task.get("execution_worktree_pool") or repo_root is None:
        return True
    try:
        policy = load_worktree_pool_policy(repo_root)
    except (OSError, ValueError, subprocess.SubprocessError):
        return True
    if (
        policy is None
        or policy.fingerprint
        != str(task.get("execution_worktree_policy_fingerprint") or "")
    ):
        return True
    allowed = tuple(
        value.as_posix().rstrip("/") for value in (*policy.copy, *policy.retain)
    )
    for line in entries:
        if not line.startswith("?? "):
            return True
        relative = line[3:].strip().rstrip("/")
        if not any(
            relative == prefix or relative.startswith(f"{prefix}/")
            for prefix in allowed
        ):
            return True
    return False


def _pool_lease(
    config: Config,
    task: dict[str, Any],
    *,
    repo_root: Path | None,
    path: Path | None,
    branch: str,
    attached: bool,
) -> dict[str, Any]:
    if not task.get("execution_worktree_pool"):
        return _compatibility(None, ["pool_not_applicable"])
    fingerprint = str(task.get("execution_worktree_policy_fingerprint") or "").strip()
    if (
        repo_root is None
        or path is None
        or not branch
        or not fingerprint
        or not attached
    ):
        return _compatibility(
            False, ["pool_lease_inconsistent", "pool_metadata_incomplete"]
        )
    try:
        validate_pool_lease(
            config,
            repo_root,
            path,
            branch,
            str(task.get("id") or ""),
            fingerprint,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return _compatibility(False, ["pool_lease_inconsistent"])
    return _compatibility(True, ["pool_lease_current"])


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
    value = (
        task.get("execution_repo_root") or task.get("project_root") or task.get("cwd")
    )
    if not value:
        return None
    try:
        path = Path(str(value)).expanduser().resolve()
    except OSError:
        return None
    return path if git_optional(path, "rev-parse", "--show-toplevel") else None


def _worktree_path(task: dict[str, Any]) -> Path | None:
    value = task.get("execution_worktree_path")
    if not value:
        return None
    try:
        return Path(str(value)).expanduser().resolve()
    except OSError:
        return None


def _repo_ref(path: Path | None) -> str | None:
    if path is None:
        return None
    return "repo:" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _item(
    task_id: str,
    execution_mode: str,
    worktree_status: str,
    repository_ref: str | None,
    reconciliation: dict[str, Any],
    branch_review: dict[str, Any],
    hibernation: dict[str, Any],
    reattach: dict[str, Any],
    resume: dict[str, Any],
    pool_lease: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "execution_mode": execution_mode,
        "worktree_status": worktree_status or None,
        "repository_ref": repository_ref,
        "reconciliation": reconciliation,
        "branch_only_review": branch_review,
        "hibernation": hibernation,
        "reattach": reattach,
        "resume": resume,
        "pool_lease": pool_lease,
    }


def _projection(status: str, reasons: list[str]) -> dict[str, Any]:
    return {"status": status, "reason_codes": _reasons(reasons)}


def _compatibility(compatible: bool | None, reasons: list[str]) -> dict[str, Any]:
    return {"compatible": compatible, "reason_codes": _reasons(reasons)}


def _reasons(values: list[str]) -> list[str]:
    return sorted(set(values))


def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    reconciliation = Counter(item["reconciliation"]["status"] for item in items)
    return {
        "task_count": len(items),
        "git_worktree_task_count": sum(
            item["execution_mode"] == "git_worktree" for item in items
        ),
        "branch_only_review_candidates": sum(
            item["branch_only_review"]["compatible"] is True for item in items
        ),
        "hibernation_candidates": sum(
            item["hibernation"]["compatible"] is True for item in items
        ),
        "reattach_candidates": sum(
            item["reattach"]["compatible"] is True for item in items
        ),
        "resume_compatible": sum(
            item["resume"]["compatible"] is True for item in items
        ),
        "pool_lease_inconsistent": sum(
            item["pool_lease"]["compatible"] is False for item in items
        ),
        "reconciliation_by_status": dict(sorted(reconciliation.items())),
    }


def _validate_item(item: object) -> None:
    if not isinstance(item, dict) or set(item) != {
        "task_id",
        "execution_mode",
        "worktree_status",
        "repository_ref",
        "reconciliation",
        "branch_only_review",
        "hibernation",
        "reattach",
        "resume",
        "pool_lease",
    }:
        raise WorktreeHibernationPlanValidationError("item fields are not canonical")
    if not isinstance(item["task_id"], str) or not item["task_id"]:
        raise WorktreeHibernationPlanValidationError("task id is invalid")
    if (
        len(item["task_id"]) > 256
        or any(character.isspace() for character in item["task_id"])
        or "/" in item["task_id"]
        or "\\" in item["task_id"]
    ):
        raise WorktreeHibernationPlanValidationError("task id is not public-safe")
    if item["execution_mode"] not in {
        "git_worktree",
        "main_worktree",
        "unknown",
    }:
        raise WorktreeHibernationPlanValidationError("execution mode is invalid")
    if item["worktree_status"] is not None and not isinstance(
        item["worktree_status"], str
    ):
        raise WorktreeHibernationPlanValidationError("worktree status is invalid")
    if item["repository_ref"] is not None and not re.fullmatch(
        r"repo:[0-9a-f]{16}", item["repository_ref"]
    ):
        raise WorktreeHibernationPlanValidationError("repository ref is invalid")
    reconciliation = item["reconciliation"]
    if (
        not isinstance(reconciliation, dict)
        or set(reconciliation) != {"status", "reason_codes"}
        or reconciliation["status"] not in RECONCILIATION_STATUSES
    ):
        raise WorktreeHibernationPlanValidationError(
            "reconciliation projection is invalid"
        )
    _validate_reasons(reconciliation["reason_codes"])
    for key in (
        "branch_only_review",
        "hibernation",
        "reattach",
        "resume",
        "pool_lease",
    ):
        projection = item[key]
        if (
            not isinstance(projection, dict)
            or set(projection) != {"compatible", "reason_codes"}
            or projection["compatible"] not in (True, False, None)
        ):
            raise WorktreeHibernationPlanValidationError(f"{key} projection is invalid")
        _validate_reasons(projection["reason_codes"])


def _validate_reasons(reasons: object) -> None:
    if (
        not isinstance(reasons, list)
        or reasons != sorted(set(reasons))
        or any(reason not in REASON_CODES for reason in reasons)
    ):
        raise WorktreeHibernationPlanValidationError("reason codes are invalid")


def _digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_token(value: object) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    return token if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", token) else "unknown"


def _bool_text(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return str(value).lower()

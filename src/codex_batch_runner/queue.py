from __future__ import annotations

import re
import socket
import subprocess
from collections import Counter
from pathlib import Path

from .config import Config
from .events import emit_task_event, transition_payload
from .model_requirements import derive_model_requirement_vector, issue_native_requirement_v2, task_requirement_metadata
from .fs import ensure_dir, read_json, write_json_atomic
from .lock import lock_pid, pid_exists
from .timeutil import iso_now, parse_time, utc_now
from .worker_routing import planned_worker_capacity_pool

RUNNABLE_STATUSES = {"runnable", "needs_resume"}
DEFAULT_HIDDEN_LIST_STATUSES = {"completed", "archived"}
REVIEW_STATUSES = {"unreviewed", "accepted", "rejected", "needs_followup"}
RESOLUTIONS = {"wont_fix", "superseded", "manual", "smoke", "duplicate"}
RESOLVABLE_COMPLETED_REVIEW_STATUSES = {"rejected", "needs_followup"}
EXECUTION_BACKENDS = {"codex", "shell", "external-json-command"}
TASK_PRIORITIES = ("asap", "high", "normal", "low", "background")
TASK_PRIORITY_RANK = {name: index for index, name in enumerate(TASK_PRIORITIES)}
ROUTING_SIZES = ("tiny", "small", "medium", "large", "xlarge")
ROUTING_RISKS = ("low", "medium", "high")
VERIFICATION_SCOPES = ("none", "docs", "lint", "typecheck", "unit", "integration", "e2e", "smoke", "manual", "build")
CHAIN_STATUSES = {
    "awaiting_review",
    "reviewing",
    "needs_fix",
    "fixing",
    "accepted",
    "needs_human",
    "loop_limit_reached",
}
CHAIN_METADATA_FIELDS = (
    "subtask_type",
    "subtask_for",
    "review_followup_for",
    "blocks_root_completion",
    "root_task_id",
    "parent_task_id",
    "blocking_subtask_ids",
    "review_cycle",
    "review_attempts",
    "fix_attempts",
    "chain_status",
    "review_findings",
    "last_review_decision",
    "auto_fix_allowed",
    "auto_fix_budget",
    "last_auto_fix_task_id",
    "last_conflict_fix_task_id",
    "finding_fingerprints",
)
SCHEMA_VERSION = 1
TASK_TITLE_DISPLAY_LIMIT = 80


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")
    return slug or "task"


def task_path(config: Config, task_id: str) -> Path:
    return config.queue_dir / f"{task_id}.json"


def load_task(config: Config, task_id: str) -> dict:
    path = task_path(config, task_id)
    task = read_json(path)
    if task is None:
        raise FileNotFoundError(f"task not found: {task_id}")
    return task


def save_task(config: Config, task: dict, *, touch_updated_at: bool = True) -> None:
    path = task_path(config, task["id"])
    existing = read_json(path)
    if isinstance(existing, dict):
        for field in ("model_requirement_vector", "routing_override"):
            if existing.get(field) != task.get(field):
                raise ValueError(
                    f"{field} is immutable after enqueue; create a new task revision instead"
                )
        existing_reviewer_units = existing.get("automatic_reviewer_work_units", [])
        new_reviewer_units = task.get("automatic_reviewer_work_units", [])
        if not isinstance(existing_reviewer_units, list) or not isinstance(new_reviewer_units, list):
            raise ValueError("automatic_reviewer_work_units must be a list")
        if new_reviewer_units[: len(existing_reviewer_units)] != existing_reviewer_units:
            raise ValueError("automatic reviewer work units are immutable after issuance")
    if touch_updated_at:
        task["updated_at"] = iso_now()
    write_json_atomic(path, task)


def archive_task(config: Config, task_id: str) -> dict:
    task = load_task(config, task_id)
    previous_status = task.get("status")
    if task.get("status") != "archived":
        task["previous_status"] = previous_status
        task["status"] = "archived"
    task["archived_at"] = iso_now()
    save_task(config, task)
    emit_task_event(
        config,
        "task_archived",
        task,
        source="archive",
        summary=f"archived task {task_id}",
        payload=transition_payload(task, previous_status=previous_status, archived_at=task.get("archived_at")),
    )
    return task


def set_review_status(
    config: Config,
    task_id: str,
    review_status: str,
    reason: str | None = None,
    *,
    require_completed: bool = False,
) -> dict:
    if review_status not in REVIEW_STATUSES:
        raise ValueError(f"invalid review status: {review_status}")
    task = load_task(config, task_id)
    if require_completed and task.get("status") != "completed":
        raise ValueError(f"{review_status} review requires completed task status, found {task.get('status')}")
    task["review_status"] = review_status
    task["reviewed_at"] = iso_now()
    task["review_reason"] = reason
    if review_status == "needs_followup":
        apply_follow_up_linkage(task)
    elif review_status == "accepted" and task.get("chain_status"):
        task["chain_status"] = "accepted"
    save_task(config, task)
    emit_task_event(
        config,
        "task_reviewed",
        task,
        source="review",
        summary=f"reviewed task {task_id} as {review_status}",
        payload=transition_payload(task, review_status=review_status, reviewed_at=task.get("reviewed_at")),
    )
    if review_status == "needs_followup" and task.get("origin_parent_ref"):
        from .parent_attention import create_parent_attention
        create_parent_attention(
            config, parent_ref=str(task["origin_parent_ref"]), work_item_ref=str(task["id"]),
            completion_id=str(task.get("reviewed_at") or "review"), wake_reason="needs_follow_up",
            summary=str(reason or "review requires follow-up"),
        )
    return task


def apply_follow_up_linkage(task: dict) -> None:
    task_id = str(task.get("id") or "")
    task["root_task_id"] = task.get("root_task_id") or task_id
    task["parent_task_id"] = task.get("parent_task_id") or None
    task["review_cycle"] = int(task.get("review_cycle") or 0)
    task["chain_status"] = "needs_fix"
    linkage = {
        "source_task_id": task_id,
        "source_execution_mode": task.get("execution_mode") or "main_worktree",
        "source_branch": task.get("execution_branch"),
        "source_worktree_path": task.get("execution_worktree_path"),
        "source_worktree_status": task.get("execution_worktree_status"),
        "source_repo_root": task.get("execution_repo_root") or task.get("project_root"),
        "task_generation": "not_created",
        "recorded_at": task.get("reviewed_at"),
    }
    task["review_follow_up"] = {key: value for key, value in linkage.items() if value not in (None, "")}


def set_resolution(config: Config, task_id: str, resolution: str, reason: str | None = None) -> dict:
    if resolution not in RESOLUTIONS:
        raise ValueError(f"invalid resolution: {resolution}")
    task = load_task(config, task_id)
    if not is_resolvable_task(task):
        raise ValueError(
            "resolution can only be set on failed, blocked_user, or completed rejected/needs_followup tasks"
        )
    task["resolution"] = resolution
    task["resolved_at"] = iso_now()
    task["resolution_reason"] = reason
    save_task(config, task)
    emit_task_event(
        config,
        "task_resolved",
        task,
        source="resolve",
        summary=f"resolved task {task_id} as {resolution}",
        payload=transition_payload(task, resolution=resolution, resolved_at=task.get("resolved_at")),
    )
    return task


def is_resolvable_task(task: dict) -> bool:
    status = task.get("status")
    if status in {"failed", "blocked_user"}:
        return True
    if status != "completed":
        return False
    review = task.get("review_status") or "unreviewed"
    return review in RESOLVABLE_COMPLETED_REVIEW_STATUSES


def list_tasks(config: Config) -> list[dict]:
    ensure_dir(config.queue_dir)
    tasks = []
    for path in sorted(config.queue_dir.glob("*.json")):
        task = read_json(path)
        if isinstance(task, dict):
            tasks.append(task)
    return tasks


def rejected_discarded_result(task: dict) -> bool:
    """Return whether a rejected worktree result was conclusively discarded."""
    return (
        task.get("status") in {"completed", "archived"}
        and task.get("review_status") == "rejected"
        and task.get("execution_mode") == "git_worktree"
        and task.get("execution_worktree_status") == "cleaned"
        and task.get("execution_cleanup_kind") == "discard"
        and task.get("execution_cleanup_result_applied") is False
    )


def create_task(
    config: Config,
    prompt: str,
    cwd: str,
    task_id: str | None = None,
    depends_on: list[str] | None = None,
    project_id: str | None = None,
    category: str | None = None,
    labels: list[str] | None = None,
    created_by: str | None = None,
    title: str | None = None,
    description: str | None = None,
    model_requirement_vector: dict | None = None,
    routing_override: dict | None = None,
    routing_reason: str | None = None,
    routing_risk_factors: list[str] | None = None,
    routing_experiment: str | None = None,
    routing_size: str | None = None,
    routing_risk: str | None = None,
    verification_scope: list[str] | None = None,
    execution_backend: str | None = None,
    shell_command: list[str] | None = None,
    shell_timeout_seconds: int | None = None,
    external_command: list[str] | None = None,
    external_timeout_seconds: int | None = None,
    capacity_pool: str = "codex",
    task_priority: str = "normal",
    subtask_type: str | None = None,
    subtask_for: str | None = None,
    review_followup_for: str | None = None,
    blocks_root_completion: bool = False,
) -> dict:
    ensure_dir(config.queue_dir)
    now = iso_now()
    cwd_path = Path(cwd).expanduser()
    project_root = detect_project_root(cwd_path)
    resolved_project_id = project_id or project_root.name
    if not task_id:
        stamp = now.replace(":", "").replace("+", "Z").replace(".", "-")
        task_id = slugify(f"task-{stamp}")
    execution_backend_explicit = execution_backend is not None
    execution_backend = validate_execution_backend(execution_backend)
    capacity_pool = validate_capacity_pool(capacity_pool)
    task_priority = validate_task_priority(task_priority)
    routing_size = validate_optional_choice("routing_size", routing_size, ROUTING_SIZES)
    routing_risk = validate_optional_choice("routing_risk", routing_risk, ROUTING_RISKS)
    verification_scope = validate_choice_list("verification_scope", verification_scope, VERIFICATION_SCOPES)
    if execution_backend == "codex" and shell_command:
        raise ValueError("shell_command requires execution_backend=shell")
    if execution_backend == "codex" and external_command:
        raise ValueError("external_command requires execution_backend=external-json-command")
    if execution_backend == "shell" and external_command:
        raise ValueError("external_command requires execution_backend=external-json-command")
    if execution_backend == "external-json-command" and shell_command:
        raise ValueError("shell_command requires execution_backend=shell")
    shell_command = validate_shell_command(shell_command) if execution_backend == "shell" else None
    shell_timeout_seconds = validate_shell_timeout(shell_timeout_seconds) if shell_timeout_seconds is not None else None
    external_command = validate_external_command(external_command) if execution_backend == "external-json-command" else None
    external_timeout_seconds = (
        validate_external_timeout(external_timeout_seconds) if external_timeout_seconds is not None else None
    )
    path = task_path(config, task_id)
    if path.exists():
        raise FileExistsError(f"task already exists: {task_id}")
    task = {
        "schema_version": SCHEMA_VERSION,
        "id": task_id,
        "title": normalize_task_title(title) or title_from_prompt(prompt) or task_id,
        "description": clean_optional_text(description),
        "status": "runnable",
        "review_status": None,
        "reviewed_at": None,
        "review_reason": None,
        "subtask_type": clean_optional_text(subtask_type),
        "subtask_for": clean_optional_text(subtask_for),
        "review_followup_for": clean_optional_text(review_followup_for),
        "blocks_root_completion": bool(blocks_root_completion),
        "root_task_id": None,
        "parent_task_id": None,
        "blocking_subtask_ids": [],
        "review_cycle": 0,
        "review_attempts": 0,
        "fix_attempts": 0,
        "chain_status": None,
        "review_findings": [],
        "last_review_decision": None,
        "auto_fix_allowed": False,
        "auto_fix_budget": None,
        "last_auto_fix_task_id": None,
        "last_conflict_fix_task_id": None,
        "finding_fingerprints": [],
        "project_root": str(project_root),
        "project_id": resolved_project_id,
        "category": category,
        "labels": labels or [],
        "created_by": created_by,
        "routing_reason": clean_optional_text(routing_reason),
        "routing_risk_factors": clean_text_list(routing_risk_factors),
        "routing_experiment": clean_optional_text(routing_experiment),
        "routing_size": routing_size,
        "routing_risk": routing_risk,
        "verification_scope": verification_scope,
        "prompt": prompt,
        "next_prompt": None,
        "cwd": str(cwd_path),
        "execution_backend": execution_backend,
        "execution_backend_explicit": execution_backend_explicit,
        "capacity_pool": capacity_pool,
        "task_priority": task_priority,
        "shell_command": shell_command,
        "shell_timeout_seconds": shell_timeout_seconds,
        "external_command": external_command,
        "external_timeout_seconds": external_timeout_seconds,
        "session_id": None,
        "thread_id": None,
        "depends_on": depends_on or [],
        "attempts": 0,
        "max_attempts": config.default_max_attempts,
        "cooldown_until": None,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
        "log_paths": [],
    }
    requirement_metadata = task_requirement_metadata(
        model_requirement_vector=model_requirement_vector,
        routing_override=routing_override,
    )
    if not requirement_metadata and execution_backend == "codex":
        issuer = issue_native_requirement_v2 if task.get("subtask_type") else derive_model_requirement_vector
        requirement_metadata = {"model_requirement_vector": issuer(task)}
    task.update(requirement_metadata)
    write_json_atomic(path, task)
    emit_task_event(
        config,
        "task_created",
        task,
        actor=created_by or "cbr",
        source="enqueue",
        summary=f"created task {task_id}",
        payload=transition_payload(
            task,
            depends_on_count=len(task["depends_on"]),
            category=task.get("category"),
            labels=task.get("labels"),
            created_by=task.get("created_by"),
            title=task.get("title"),
            has_description=bool(task.get("description")),
            has_routing_reason=bool(task.get("routing_reason")),
            routing_risk_factor_count=len(task.get("routing_risk_factors") or []),
            routing_experiment=task.get("routing_experiment"),
            routing_size=task.get("routing_size"),
            routing_risk=task.get("routing_risk"),
            verification_scope_count=len(task.get("verification_scope") or []),
            subtask_type=task.get("subtask_type"),
            subtask_for=task.get("subtask_for"),
            review_followup_for=task.get("review_followup_for"),
            blocks_root_completion=task.get("blocks_root_completion"),
        ),
    )
    return task


def clean_optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def normalize_task_title(value: object | None) -> str | None:
    cleaned = clean_optional_text(value)
    if not cleaned:
        return None
    return truncate_text(cleaned, TASK_TITLE_DISPLAY_LIMIT)


def validate_execution_backend(value: object) -> str:
    backend = str(value or "codex").strip()
    if backend not in EXECUTION_BACKENDS:
        raise ValueError(f"invalid execution backend: {backend}")
    return backend


def validate_capacity_pool(value: object) -> str:
    if value is None:
        return "codex"
    pool = str(value).strip()
    if not pool:
        raise ValueError("capacity_pool must be a non-empty string")
    return pool


def validate_task_priority(value: object) -> str:
    priority = str(value or "normal").strip()
    if priority not in TASK_PRIORITY_RANK:
        raise ValueError("task_priority must be one of: " + ", ".join(TASK_PRIORITIES))
    return priority


def validate_optional_choice(name: str, value: object | None, choices: tuple[str, ...]) -> str | None:
    cleaned = clean_optional_text(value)
    if cleaned is None:
        return None
    if cleaned not in choices:
        raise ValueError(f"{name} must be one of: " + ", ".join(choices))
    return cleaned


def validate_choice_list(name: str, values: list[str] | None, choices: tuple[str, ...]) -> list[str]:
    cleaned = clean_text_list(values)
    invalid = [value for value in cleaned if value not in choices]
    if invalid:
        raise ValueError(f"{name} entries must be one of: " + ", ".join(choices))
    return cleaned


def validate_shell_command(value: object) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("shell_command must be a non-empty list of strings")
    if any(item == "" for item in value):
        raise ValueError("shell_command entries must be non-empty strings")
    return list(value)


def validate_shell_timeout(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("shell_timeout_seconds must be a positive integer")
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("shell_timeout_seconds must be a positive integer") from exc
    if seconds < 1:
        raise ValueError("shell_timeout_seconds must be a positive integer")
    return seconds


def validate_external_command(value: object) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("external_command must be a non-empty list of strings")
    if any(item == "" for item in value):
        raise ValueError("external_command entries must be non-empty strings")
    return list(value)


def validate_external_timeout(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("external_timeout_seconds must be a positive integer")
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("external_timeout_seconds must be a positive integer") from exc
    if seconds < 1:
        raise ValueError("external_timeout_seconds must be a positive integer")
    return seconds


def clean_text_list(values: list[str] | None) -> list[str]:
    if not values:
        return []
    cleaned = []
    for value in values:
        item = clean_optional_text(value)
        if item:
            cleaned.append(item)
    return cleaned


def chain_metadata(task: dict) -> dict:
    return {key: task.get(key) for key in CHAIN_METADATA_FIELDS if meaningful_chain_value(key, task.get(key))}


def meaningful_chain_value(key: str, value: object) -> bool:
    if value in (None, "", [], {}):
        return False
    if key in {"review_cycle", "review_attempts", "fix_attempts"} and value == 0:
        return False
    if key in {"auto_fix_allowed", "blocks_root_completion"} and value is False:
        return False
    return True


def title_from_prompt(prompt: str) -> str | None:
    for line in str(prompt or "").splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            return truncate_text(cleaned, TASK_TITLE_DISPLAY_LIMIT)
    return None


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def task_title(task: dict) -> str:
    title = normalize_task_title(task.get("title"))
    if title:
        return title
    prompt_title = title_from_prompt(str(task.get("prompt") or ""))
    if prompt_title:
        return prompt_title
    return str(task.get("id") or "task")


def detect_project_root(cwd: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return cwd.resolve()
    root = result.stdout.strip()
    return Path(root).expanduser().resolve() if root else cwd.resolve()


def task_project_root(task: dict) -> str:
    value = task.get("project_root") or task.get("cwd") or ""
    return str(Path(str(value)).expanduser().resolve()) if value else ""


def task_project_id(task: dict) -> str:
    if task.get("project_id"):
        return str(task.get("project_id"))
    root = task_project_root(task)
    return Path(root).name if root else ""


def task_capacity_pool(task: dict) -> str:
    return validate_capacity_pool(task.get("capacity_pool") or "codex")


def task_planned_capacity_pool(config: Config, task: dict) -> str:
    planned = planned_worker_capacity_pool(config, task)
    return validate_capacity_pool(planned or task.get("capacity_pool") or "codex")


def task_priority(task: dict) -> str:
    try:
        return validate_task_priority(task.get("task_priority") or "normal")
    except ValueError:
        return "normal"


def task_labels(task: dict) -> list[str]:
    labels = task.get("labels") or []
    return [str(label) for label in labels] if isinstance(labels, list) else []


def dependency_status(
    task: dict,
    by_id: dict[str, dict],
    *,
    require_accepted_review: bool = False,
) -> tuple[bool, list[str]]:
    blocked = [item["id"] for item in dependency_blockers(task, by_id, require_accepted_review=require_accepted_review)]
    return not blocked, blocked


def dependency_blockers(
    task: dict,
    by_id: dict[str, dict],
    *,
    require_accepted_review: bool = False,
) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    for dep_id in task.get("depends_on", []):
        dep = by_id.get(dep_id)
        dep_status = str(dep.get("status") or "") if dep else "missing"
        dep_review_status = dependency_review_status(dep)
        if not dep or dep.get("status") != "completed":
            blockers.append(
                {
                    "id": str(dep_id),
                    "reason": "not_completed",
                    "status": dep_status,
                    "review_status": dep_review_status,
                }
            )
        elif require_accepted_review and dep.get("review_status") != "accepted":
            blockers.append(
                {
                    "id": str(dep_id),
                    "reason": "not_accepted",
                    "status": dep_status,
                    "review_status": dep_review_status,
                }
            )
        elif dep.get("execution_mode") == "git_worktree" and dep.get("review_status") != "accepted":
            blockers.append(
                {
                    "id": str(dep_id),
                    "reason": "not_accepted",
                    "status": dep_status,
                    "review_status": dep_review_status,
                }
            )
        elif dep.get("execution_mode") == "git_worktree" and dep.get("execution_apply_status") != "applied":
            blockers.append(
                {
                    "id": str(dep_id),
                    "reason": "not_applied",
                    "status": dep_status,
                    "review_status": dep_review_status,
                }
            )
    return blockers


def dependency_review_status(task: dict | None) -> str:
    if not task:
        return ""
    if task.get("status") == "completed":
        return str(task.get("review_status") or "unreviewed")
    return str(task.get("review_status") or "")


def is_in_cooldown(task: dict) -> bool:
    until = parse_time(task.get("cooldown_until"))
    return bool(until and until > utc_now())


def select_next_task(config: Config) -> dict | None:
    report = selection_report(config)
    candidates = [item for item in report["tasks"] if item["admissible"]]
    candidates.sort(key=selection_entry_sort_key)
    return candidates[0]["task"] if candidates else None


def selection_report(config: Config) -> dict:
    tasks = list_tasks(config)
    by_id = {task.get("id"): task for task in tasks}
    running = running_capacity(tasks)
    preselected: list[tuple[dict, list[str]]] = []
    project_oldest: dict[str, str] = {}
    entries = []
    for task in tasks:
        reasons: list[str] = []
        if task.get("status") not in RUNNABLE_STATUSES:
            reasons.append("not_runnable_status")
        if is_in_cooldown(task):
            reasons.append("cooldown")
        deps_ready, _ = dependency_status(
            task,
            by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )
        if not deps_ready:
            reasons.append("dependency")
        preselected.append((task, reasons))
        if not reasons:
            project_key = priority_project_key(task)
            created_at = str(task.get("created_at") or "")
            if project_key not in project_oldest or created_at < project_oldest[project_key]:
                project_oldest[project_key] = created_at
    for task, reasons in preselected:
        if not reasons:
            reasons.extend(capacity_blockers(config, task, running))
        raw_priority = raw_project_priority(config, task)
        entries.append(
            {
                "task": task,
                "task_id": task.get("id"),
                "project_id": task_project_id(task),
                "project_root": task_project_root(task),
                "capacity_pool": task_planned_capacity_pool(config, task),
                "task_priority": task_priority(task),
                "admissible": not reasons,
                "reasons": reasons,
                "raw_project_priority": raw_priority,
                "effective_project_priority": effective_project_priority(
                    config,
                    task,
                    raw_priority=raw_priority,
                    created_at=project_oldest.get(priority_project_key(task)),
                ),
            }
        )
    return {
        "capacity": capacity_evidence(config, tasks),
        "tasks": entries,
    }


def running_capacity(tasks: list[dict]) -> dict[str, Counter[str] | int]:
    running = [task for task in tasks if task.get("status") == "running"]
    return {
        "total": len(running),
        "by_project": Counter(capacity_project_key(task) for task in running),
        "by_pool": Counter(task_capacity_pool(task) for task in running),
    }


def capacity_evidence(config: Config, tasks: list[dict]) -> dict:
    running = running_capacity(tasks)
    return {
        "max_total_running": config.max_total_running,
        "max_running_per_project": config.max_running_per_project,
        "capacity_pools": {name: dict(pool) for name, pool in sorted(config.capacity_pools.items())},
        "running_total": running["total"],
        "running_by_project": dict(sorted(running["by_project"].items())),  # type: ignore[union-attr]
        "running_by_pool": dict(sorted(running["by_pool"].items())),  # type: ignore[union-attr]
    }


def capacity_blockers(config: Config, task: dict, running: dict[str, Counter[str] | int] | None = None) -> list[str]:
    running = running or running_capacity(list_tasks(config))
    blockers: list[str] = []
    pool = task_planned_capacity_pool(config, task)
    pools = config.capacity_pools
    if pool not in pools:
        blockers.append("unknown_capacity_pool")
    if int(running["total"]) >= config.max_total_running:
        blockers.append("max_total_running")
    by_project = running["by_project"]
    if isinstance(by_project, Counter) and by_project[capacity_project_key(task)] >= config.max_running_per_project:
        blockers.append("max_running_per_project")
    by_pool = running["by_pool"]
    if pool in pools and isinstance(by_pool, Counter) and by_pool[pool] >= int(pools[pool]["max_running"]):
        blockers.append("capacity_pool_full")
    return blockers


def capacity_project_key(task: dict) -> str:
    return task_project_id(task) or task_project_root(task) or str(task.get("id") or "unknown")


def selection_sort_key(config: Config, task: dict) -> tuple[int, int, int, str, str]:
    raw_priority = raw_project_priority(config, task)
    return (
        effective_project_priority(config, task, raw_priority=raw_priority),
        raw_priority,
        TASK_PRIORITY_RANK[task_priority(task)],
        str(task.get("created_at") or ""),
        str(task.get("id") or ""),
    )


def selection_entry_sort_key(entry: dict) -> tuple[int, int, int, str, str]:
    return (
        int(entry["effective_project_priority"]),
        int(entry["raw_project_priority"]),
        TASK_PRIORITY_RANK[validate_task_priority(entry.get("task_priority") or "normal")],
        str(entry["task"].get("created_at") or ""),
        str(entry["task"].get("id") or ""),
    )


def priority_project_key(task: dict) -> str:
    return task_project_id(task) or task_project_root(task) or str(task.get("id") or "unknown")


def raw_project_priority(config: Config, task: dict) -> int:
    project_id = task_project_id(task)
    project_root = task_project_root(task)
    if project_id in config.project_priorities:
        return config.project_priorities[project_id]
    if project_root in config.project_priorities:
        return config.project_priorities[project_root]
    return config.default_project_priority


def effective_project_priority(
    config: Config,
    task: dict,
    *,
    raw_priority: int | None = None,
    created_at: object | None = None,
) -> int:
    raw = raw_project_priority(config, task) if raw_priority is None else raw_priority
    if config.project_priority_aging_hours <= 0:
        return raw
    created = parse_time(task.get("created_at") if created_at is None else created_at)
    if not created:
        return raw
    age_hours = max(0.0, (utc_now() - created).total_seconds() / 3600.0)
    return raw - int(age_hours // config.project_priority_aging_hours)


def recover_stale_running_tasks(config: Config) -> list[str]:
    recovered: list[str] = []
    for task in list_tasks(config):
        if task.get("status") != "running":
            continue
        runner_hostname = task.get("active_runner_hostname")
        runner_pid = lock_pid(task.get("active_runner_pid"))
        same_host = isinstance(runner_hostname, str) and runner_hostname == socket.gethostname()
        pid_alive = pid_exists(runner_pid) if same_host and runner_pid is not None else None
        dead_same_host_pid = pid_alive is False
        started_at = parse_time(task.get("started_at"))
        stale_by_age = not started_at or (utc_now() - started_at).total_seconds() > config.stale_lock_seconds
        if pid_alive is True or (not dead_same_host_pid and not stale_by_age):
            continue
        recovery_reason = "same_host_dead_runner_pid" if dead_same_host_pid else "stale_started_at"
        previous_status = str(task.get("status") or "")
        task["status"] = "needs_resume" if task.get("next_prompt") else "runnable"
        task["last_error"] = f"recovered stale running task: {recovery_reason}"
        task["running_recovered_at"] = iso_now()
        task["running_recovery_reason"] = recovery_reason
        task["running_recovery_runner_hostname"] = runner_hostname if isinstance(runner_hostname, str) else None
        task["running_recovery_runner_pid"] = runner_pid
        clear_active_run_metadata(task)
        save_task(config, task)
        emit_task_event(
            config,
            "task_mutated",
            task,
            source="stale-running-recovery",
            summary=f"recovered running task {task['id']}: {recovery_reason}",
            payload=transition_payload(
                task,
                previous_status=previous_status,
                mutation="stale_running_recovery",
                recovery_reason=recovery_reason,
                recovered_at=task.get("running_recovered_at"),
            ),
        )
        recovered.append(task["id"])
    return recovered


def clear_active_run_metadata(task: dict) -> None:
    for key in (
        "active_run_id",
        "active_runner_hostname",
        "active_runner_pid",
        "active_run_attempt",
        "active_run_started_at",
    ):
        task.pop(key, None)

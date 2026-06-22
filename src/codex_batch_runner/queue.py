from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import Config
from .events import emit_task_event, transition_payload
from .execution_profiles import task_execution_metadata
from .fs import ensure_dir, read_json, write_json_atomic
from .timeutil import iso_now, parse_time, utc_now

RUNNABLE_STATUSES = {"runnable", "needs_resume"}
DEFAULT_HIDDEN_LIST_STATUSES = {"completed", "archived"}
REVIEW_STATUSES = {"unreviewed", "accepted", "rejected", "needs_followup"}
RESOLUTIONS = {"wont_fix", "superseded", "manual", "smoke", "duplicate"}
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


def save_task(config: Config, task: dict) -> None:
    task["updated_at"] = iso_now()
    write_json_atomic(task_path(config, task["id"]), task)


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
    if task.get("status") not in {"failed", "blocked_user"}:
        raise ValueError("resolution can only be set on failed or blocked_user tasks")
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


def list_tasks(config: Config) -> list[dict]:
    ensure_dir(config.queue_dir)
    tasks = []
    for path in sorted(config.queue_dir.glob("*.json")):
        task = read_json(path)
        if isinstance(task, dict):
            tasks.append(task)
    return tasks


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
    execution_profile: str | None = None,
    model: str | None = None,
    codex_profile: str | None = None,
    codex_config_overrides: dict[str, str] | None = None,
    token_budget_hint: str | None = None,
    routing_reason: str | None = None,
    routing_risk_factors: list[str] | None = None,
    routing_experiment: str | None = None,
    subtask_type: str | None = None,
    subtask_for: str | None = None,
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
    if execution_profile and execution_profile not in config.execution_profiles:
        raise ValueError(f"unknown execution profile: {execution_profile}")
    path = task_path(config, task_id)
    if path.exists():
        raise FileExistsError(f"task already exists: {task_id}")
    task = {
        "schema_version": SCHEMA_VERSION,
        "id": task_id,
        "title": clean_optional_text(title) or title_from_prompt(prompt) or task_id,
        "description": clean_optional_text(description),
        "status": "runnable",
        "review_status": None,
        "reviewed_at": None,
        "review_reason": None,
        "subtask_type": clean_optional_text(subtask_type),
        "subtask_for": clean_optional_text(subtask_for),
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
        "prompt": prompt,
        "next_prompt": None,
        "cwd": str(cwd_path),
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
    task.update(
        task_execution_metadata(
            execution_profile=execution_profile,
            model=model,
            codex_profile=codex_profile,
            config_overrides=codex_config_overrides,
            token_budget_hint=token_budget_hint,
        )
    )
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
            subtask_type=task.get("subtask_type"),
            subtask_for=task.get("subtask_for"),
            blocks_root_completion=task.get("blocks_root_completion"),
        ),
    )
    return task


def clean_optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


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
            return truncate_text(cleaned, 80)
    return None


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def task_title(task: dict) -> str:
    title = clean_optional_text(task.get("title"))
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
    tasks = list_tasks(config)
    by_id = {task.get("id"): task for task in tasks}
    candidates = []
    for task in tasks:
        if task.get("status") not in RUNNABLE_STATUSES:
            continue
        if is_in_cooldown(task):
            continue
        deps_ready, _ = dependency_status(
            task,
            by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )
        if not deps_ready:
            continue
        candidates.append(task)
    candidates.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""))
    return candidates[0] if candidates else None


def recover_stale_running_tasks(config: Config) -> list[str]:
    recovered: list[str] = []
    for task in list_tasks(config):
        if task.get("status") != "running":
            continue
        started_at = parse_time(task.get("started_at"))
        stale = not started_at or (utc_now() - started_at).total_seconds() > config.stale_lock_seconds
        if not stale:
            continue
        task["status"] = "needs_resume" if task.get("next_prompt") else "runnable"
        task["last_error"] = "recovered stale running task"
        save_task(config, task)
        recovered.append(task["id"])
    return recovered

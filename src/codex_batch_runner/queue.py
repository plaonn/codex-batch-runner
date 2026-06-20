from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import Config
from .fs import ensure_dir, read_json, write_json_atomic
from .timeutil import iso_now, parse_time, utc_now

RUNNABLE_STATUSES = {"runnable", "needs_resume"}
DEFAULT_HIDDEN_LIST_STATUSES = {"completed", "archived"}
REVIEW_STATUSES = {"unreviewed", "accepted", "rejected", "needs_followup"}
RESOLUTIONS = {"wont_fix", "superseded", "manual", "smoke", "duplicate"}
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
    if task.get("status") != "archived":
        task["previous_status"] = task.get("status")
        task["status"] = "archived"
    task["archived_at"] = iso_now()
    save_task(config, task)
    return task


def set_review_status(config: Config, task_id: str, review_status: str, reason: str | None = None) -> dict:
    if review_status not in REVIEW_STATUSES:
        raise ValueError(f"invalid review status: {review_status}")
    task = load_task(config, task_id)
    task["review_status"] = review_status
    task["reviewed_at"] = iso_now()
    task["review_reason"] = reason
    save_task(config, task)
    return task


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
) -> dict:
    ensure_dir(config.queue_dir)
    now = iso_now()
    cwd_path = Path(cwd).expanduser()
    project_root = detect_project_root(cwd_path)
    resolved_project_id = project_id or project_root.name
    if not task_id:
        stamp = now.replace(":", "").replace("+", "Z").replace(".", "-")
        task_id = slugify(f"task-{stamp}")
    path = task_path(config, task_id)
    if path.exists():
        raise FileExistsError(f"task already exists: {task_id}")
    task = {
        "schema_version": SCHEMA_VERSION,
        "id": task_id,
        "status": "runnable",
        "review_status": None,
        "reviewed_at": None,
        "review_reason": None,
        "project_root": str(project_root),
        "project_id": resolved_project_id,
        "category": category,
        "labels": labels or [],
        "created_by": created_by,
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
    write_json_atomic(path, task)
    return task


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


def dependency_status(task: dict, by_id: dict[str, dict]) -> tuple[bool, list[str]]:
    blocked: list[str] = []
    for dep_id in task.get("depends_on", []):
        dep = by_id.get(dep_id)
        if not dep or dep.get("status") != "completed":
            blocked.append(dep_id)
    return not blocked, blocked


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
        deps_ready, _ = dependency_status(task, by_id)
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

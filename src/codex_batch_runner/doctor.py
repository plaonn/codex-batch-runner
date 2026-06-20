from __future__ import annotations

import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .config import Config
from .fs import read_json
from .queue import RUNNABLE_STATUSES, dependency_status, is_in_cooldown
from .state import load_state
from .timeutil import parse_time, utc_now


def build_doctor_report(config: Config) -> dict[str, Any]:
    tasks, task_warnings = load_tasks_for_doctor(config.queue_dir)
    by_id = {task.get("id"): task for task in tasks}
    codex_check = check_codex_command(config.codex_command)
    checks = [
        check_directory("queue_dir", config.queue_dir),
        check_directory("log_dir", config.log_dir),
        check_parent("lock_file_parent", config.lock_file),
        check_parent("state_file_parent", config.state_file),
        codex_check,
    ]
    checks.extend(task_warnings)
    report = {
        "ok": not any(check["level"] == "error" for check in checks),
        "paths": {
            "root": str(config.root),
            "queue_dir": str(config.queue_dir),
            "log_dir": str(config.log_dir),
            "lock_file": str(config.lock_file),
            "state_file": str(config.state_file),
        },
        "codex_command": {
            "command": config.codex_command,
            "executable": config.codex_command[0] if config.codex_command else None,
            "available": codex_check["level"] != "error",
        },
        "state": state_summary(config),
        "lock": lock_summary(config),
        "tasks": task_summary(tasks, by_id),
        "checks": checks,
    }
    return report


def load_tasks_for_doctor(queue_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not queue_dir.exists():
        return [], []
    if not queue_dir.is_dir():
        return [], [error("queue_dir", f"not a directory: {queue_dir}")]
    tasks: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for path in sorted(queue_dir.glob("*.json")):
        try:
            task = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(warning("task_json", f"skipping unreadable task file {path.name}: {exc}"))
            continue
        if isinstance(task, dict):
            tasks.append(task)
        else:
            warnings.append(warning("task_json", f"skipping non-object task file {path.name}"))
    return tasks, warnings


def check_directory(name: str, path: Path) -> dict[str, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return error(name, f"cannot create/access {path}: {exc}")
    if not path.is_dir():
        return error(name, f"not a directory: {path}")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return error(name, f"directory is not readable/writable/searchable: {path}")
    return ok(name, f"available: {path}")


def check_parent(name: str, path: Path) -> dict[str, str]:
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return error(name, f"cannot create/access {parent}: {exc}")
    if not parent.is_dir():
        return error(name, f"not a directory: {parent}")
    if not os.access(parent, os.R_OK | os.W_OK | os.X_OK):
        return error(name, f"directory is not readable/writable/searchable: {parent}")
    return ok(name, f"available: {parent}")


def check_codex_command(command: list[str]) -> dict[str, str]:
    if not command:
        return error("codex_command", "codex_command is empty")
    executable = command[0]
    if Path(executable).is_absolute() or "/" in executable:
        path = Path(executable).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return ok("codex_command", f"executable found: {path}")
        return error("codex_command", f"executable not available: {path}")
    resolved = shutil.which(executable)
    if resolved:
        return ok("codex_command", f"executable found: {resolved}")
    return error("codex_command", f"executable not found on PATH: {executable}")


def state_summary(config: Config) -> dict[str, Any]:
    state = load_state(config)
    cooldown_until = state.get("global_cooldown_until")
    parsed = parse_time(cooldown_until)
    return {
        "global_cooldown_until": cooldown_until,
        "global_cooldown_active": bool(parsed and parsed > utc_now()),
    }


def lock_summary(config: Config) -> dict[str, Any]:
    path = config.lock_file
    if not path.exists():
        return {"exists": False, "path": str(path), "age_seconds": None, "stale": False}
    created_at = None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            created_at = parse_time(data.get("created_at"))
    except (OSError, json.JSONDecodeError):
        created_at = None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if created_at:
        age = max(0, int((utc_now() - created_at).total_seconds()))
    elif mtime is not None:
        age = max(0, int(utc_now().timestamp() - mtime))
    else:
        age = None
    return {
        "exists": True,
        "path": str(path),
        "age_seconds": age,
        "stale": bool(age is None or age > config.stale_lock_seconds),
    }


def task_summary(tasks: list[dict[str, Any]], by_id: dict[Any, dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(str(task.get("status") or "unknown") for task in tasks)
    needs_review_count = 0
    resolved_count = 0
    runnable_count = 0
    cooldown_count = 0
    for task in tasks:
        status = task.get("status")
        if status == "completed" and review_status(task) in {"unreviewed", "rejected", "needs_followup"}:
            needs_review_count += 1
        if status in {"failed", "blocked_user"} and task.get("resolution"):
            resolved_count += 1
        if is_in_cooldown(task):
            cooldown_count += 1
            continue
        deps_ready, _ = dependency_status(task, by_id)
        if status in RUNNABLE_STATUSES and deps_ready:
            runnable_count += 1
    return {
        "total": len(tasks),
        "by_status": dict(sorted(status_counts.items())),
        "needs_review_completed": needs_review_count,
        "resolved_failed_or_blocked": resolved_count,
        "runnable": runnable_count,
        "cooldown": cooldown_count,
    }


def render_doctor_report(report: dict[str, Any]) -> str:
    lines = ["cbr doctor", "", "paths:"]
    for name, path in report["paths"].items():
        lines.append(f"  {name}: {path}")
    lines.extend(["", "checks:"])
    for check in report["checks"]:
        lines.append(f"  {check['level']}: {check['name']}: {check['message']}")
    state = report["state"]
    lines.extend(
        [
            "",
            "state:",
            f"  global_cooldown_until: {state.get('global_cooldown_until')}",
            f"  global_cooldown_active: {str(state.get('global_cooldown_active')).lower()}",
        ]
    )
    lock = report["lock"]
    lines.extend(
        [
            "",
            "lock:",
            f"  exists: {str(lock.get('exists')).lower()}",
            f"  age_seconds: {lock.get('age_seconds')}",
            f"  stale: {str(lock.get('stale')).lower()}",
        ]
    )
    tasks = report["tasks"]
    lines.extend(["", "tasks:", f"  total: {tasks['total']}", "  by_status:"])
    if tasks["by_status"]:
        for status, count in tasks["by_status"].items():
            lines.append(f"    {status}: {count}")
    else:
        lines.append("    none: 0")
    lines.extend(
        [
            f"  needs_review_completed: {tasks['needs_review_completed']}",
            f"  resolved_failed_or_blocked: {tasks['resolved_failed_or_blocked']}",
            f"  runnable: {tasks['runnable']}",
            f"  cooldown: {tasks['cooldown']}",
        ]
    )
    return "\n".join(lines) + "\n"


def ok(name: str, message: str) -> dict[str, str]:
    return {"name": name, "level": "ok", "message": message}


def warning(name: str, message: str) -> dict[str, str]:
    return {"name": name, "level": "warning", "message": message}


def error(name: str, message: str) -> dict[str, str]:
    return {"name": name, "level": "error", "message": message}


def review_status(task: dict[str, Any]) -> str:
    if task.get("status") == "completed":
        return str(task.get("review_status") or "unreviewed")
    return str(task.get("review_status") or "")

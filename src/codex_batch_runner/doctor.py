from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .config import Config
from .fs import read_json
from .queue import RUNNABLE_STATUSES, dependency_status, is_in_cooldown
from .state import load_state
from .timeutil import parse_time, utc_now

CODEX_VERSION_TIMEOUT_SECONDS = 2.0


def build_doctor_report(config: Config) -> dict[str, Any]:
    tasks, task_warnings = load_tasks_for_doctor(config.queue_dir)
    by_id = {task.get("id"): task for task in tasks}
    codex_info = inspect_codex_command(config.codex_command)
    codex_checks = codex_command_checks(codex_info)
    git = git_summary(config.root)
    checks = [
        check_directory("queue_dir", config.queue_dir),
        check_directory("log_dir", config.log_dir),
        check_directory("event_dir", config.event_dir),
        check_parent("lock_file_parent", config.lock_file),
        check_parent("state_file_parent", config.state_file),
        *codex_checks,
    ]
    checks.extend(task_warnings)
    checks.extend(warning("git", message) for message in git["warnings"])
    report = {
        "ok": not any(check["level"] == "error" for check in checks),
        "paths": {
            "root": str(config.root),
            "queue_dir": str(config.queue_dir),
            "log_dir": str(config.log_dir),
            "event_dir": str(config.event_dir),
            "lock_file": str(config.lock_file),
            "state_file": str(config.state_file),
        },
        "codex_command": codex_info,
        "state": state_summary(config),
        "lock": lock_summary(config),
        "git": git,
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


def inspect_codex_command(command: list[str]) -> dict[str, Any]:
    info: dict[str, Any] = {
        "command": command,
        "configured_executable": command[0] if command else None,
        "resolved_executable": None,
        "available": False,
        "version_output": None,
        "version_error": None,
        "version_timeout_seconds": CODEX_VERSION_TIMEOUT_SECONDS,
    }
    if not command:
        return info
    executable = command[0]
    if Path(executable).is_absolute() or "/" in executable:
        path = Path(executable).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            info["resolved_executable"] = str(path.resolve())
            info["available"] = True
            add_codex_version_info(info)
        return info
    resolved = shutil.which(executable)
    if resolved:
        info["resolved_executable"] = str(Path(resolved).resolve())
        info["available"] = True
        add_codex_version_info(info)
    return info


def add_codex_version_info(info: dict[str, Any]) -> None:
    resolved = info.get("resolved_executable")
    if not resolved:
        return
    try:
        result = subprocess.run(
            [str(resolved), "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=CODEX_VERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        info["version_error"] = f"codex --version timed out after {CODEX_VERSION_TIMEOUT_SECONDS:g}s"
        return
    except OSError as exc:
        info["version_error"] = f"cannot run codex --version: {exc}"
        return
    if result.returncode == 0:
        output = (result.stdout or result.stderr or "").strip()
        info["version_output"] = output if output else None
        if not output:
            info["version_error"] = "codex --version produced no output"
        return
    message = (result.stderr or result.stdout or "").strip()
    if message:
        message = message.splitlines()[-1]
    else:
        message = f"exited with {result.returncode}"
    info["version_error"] = f"codex --version failed: {message}"


def codex_command_checks(info: dict[str, Any]) -> list[dict[str, str]]:
    configured = info.get("configured_executable")
    resolved = info.get("resolved_executable")
    checks: list[dict[str, str]] = []
    if not configured:
        checks.append(error("codex_command", "codex_command is empty"))
        return checks
    if info.get("available"):
        checks.append(ok("codex_command", f"executable found: {resolved}"))
    elif Path(str(configured)).is_absolute() or "/" in str(configured):
        checks.append(error("codex_command", f"executable not available: {Path(str(configured)).expanduser()}"))
    else:
        checks.append(error("codex_command", f"executable not found on PATH: {configured}"))
    version_error = info.get("version_error")
    if version_error:
        checks.append(warning("codex_command_version", str(version_error)))
    return checks


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


def git_summary(root: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "available": False,
        "is_repository": False,
        "root": None,
        "branch": None,
        "dirty": None,
        "upstream": None,
        "comparison_ref": None,
        "ahead": None,
        "behind": None,
        "warnings": [],
    }
    if not shutil.which("git"):
        summary["warnings"].append("git executable not found on PATH")
        return summary

    summary["available"] = True
    repo_root = run_git(root, ["rev-parse", "--show-toplevel"])
    if repo_root.returncode != 0 or not repo_root.stdout.strip():
        summary["warnings"].append(f"not inside a git repository: {root}")
        return summary

    repo_path = Path(repo_root.stdout.strip()).expanduser().resolve()
    summary["is_repository"] = True
    summary["root"] = str(repo_path)

    branch = run_git(repo_path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if branch.returncode == 0 and branch.stdout.strip():
        summary["branch"] = branch.stdout.strip()
    else:
        head = run_git(repo_path, ["rev-parse", "--short", "HEAD"])
        summary["branch"] = f"HEAD ({head.stdout.strip()})" if head.returncode == 0 and head.stdout.strip() else "HEAD"

    status = run_git(repo_path, ["status", "--porcelain=v1", "--untracked-files=all"])
    if status.returncode == 0:
        summary["dirty"] = bool(status.stdout.strip())
    else:
        summary["warnings"].append(f"cannot read git dirty status: {clean_git_error(status)}")

    upstream, comparison_ref = git_comparison_ref(repo_path, summary["warnings"])
    summary["upstream"] = upstream
    if comparison_ref:
        summary["comparison_ref"] = comparison_ref
        ahead_behind = run_git(repo_path, ["rev-list", "--left-right", "--count", f"{comparison_ref}...HEAD"])
        if ahead_behind.returncode == 0:
            parts = ahead_behind.stdout.strip().split()
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                summary["behind"] = int(parts[0])
                summary["ahead"] = int(parts[1])
            else:
                summary["warnings"].append(f"cannot parse git ahead/behind output for {comparison_ref}")
        else:
            summary["warnings"].append(
                f"cannot read git ahead/behind against {comparison_ref}: {clean_git_error(ahead_behind)}"
            )

    return summary


def git_comparison_ref(repo_path: Path, warnings: list[str]) -> tuple[str | None, str | None]:
    upstream = run_git(repo_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if upstream.returncode == 0 and upstream.stdout.strip():
        value = upstream.stdout.strip()
        return value, value

    origin_main = run_git(repo_path, ["show-ref", "--verify", "--quiet", "refs/remotes/origin/main"])
    if origin_main.returncode == 0:
        warnings.append("no upstream configured; using origin/main for ahead/behind")
        return None, "origin/main"

    warnings.append("no upstream or local origin/main ref available for ahead/behind")
    return None, None


def run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", "-C", str(cwd), *args], 1, "", str(exc))


def clean_git_error(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text.splitlines()[-1] if text else f"git exited with {result.returncode}"


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
    codex = report["codex_command"]
    lines.extend(
        [
            "",
            "codex_command:",
            f"  configured_executable: {codex.get('configured_executable')}",
            f"  resolved_executable: {codex.get('resolved_executable')}",
            f"  available: {str(codex.get('available')).lower()}",
            f"  version_output: {codex.get('version_output')}",
        ]
    )
    if codex.get("version_error"):
        lines.append(f"  version_warning: {codex.get('version_error')}")
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
    git = report["git"]
    lines.extend(
        [
            "",
            "git:",
            f"  available: {str(git.get('available')).lower()}",
            f"  is_repository: {str(git.get('is_repository')).lower()}",
            f"  root: {git.get('root')}",
            f"  branch: {git.get('branch')}",
            f"  dirty: {format_optional_bool(git.get('dirty'))}",
            f"  upstream: {git.get('upstream')}",
            f"  comparison_ref: {git.get('comparison_ref')}",
            f"  ahead: {git.get('ahead')}",
            f"  behind: {git.get('behind')}",
        ]
    )
    if git.get("warnings"):
        lines.append("  warnings:")
        for message in git["warnings"]:
            lines.append(f"    - {message}")
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


def format_optional_bool(value: Any) -> str:
    if value is None:
        return "None"
    return str(value).lower()


def review_status(task: dict[str, Any]) -> str:
    if task.get("status") == "completed":
        return str(task.get("review_status") or "unreviewed")
    return str(task.get("review_status") or "")

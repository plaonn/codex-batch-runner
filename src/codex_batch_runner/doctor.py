from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .config import Config
from .execution_profiles import SAFE_CONFIG_OVERRIDE_KEYS, command_options, resolve_execution_settings
from .fs import read_json
from .lock import lock_status
from .queue import RUNNABLE_STATUSES, dependency_status, is_in_cooldown
from .state import load_state
from .timeutil import parse_time, utc_now
from .transcript import sanitize
from .worktree import worktree_task_counts

CODEX_VERSION_TIMEOUT_SECONDS = 2.0


def build_doctor_report(config: Config) -> dict[str, Any]:
    tasks, task_warnings = load_tasks_for_doctor(config.queue_dir)
    by_id = {task.get("id"): task for task in tasks}
    codex_info = inspect_codex_command(config.codex_command)
    profile = execution_profile_summary(config)
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
    checks.extend(
        {
            "name": f"execution_profile_{item['kind']}",
            "level": item["level"],
            "message": item["message"],
        }
        for item in profile["checks"]
    )
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
        "worktree": worktree_summary(config, tasks),
        "capacity": capacity_summary(config, tasks),
        "execution_profiles": profile,
        "auto_review": auto_review_summary(tasks, config),
        "tasks": task_summary(tasks, by_id, config),
        "checks": checks,
    }
    return report


def execution_profile_summary(config: Config) -> dict[str, Any]:
    profile_checks = []
    for reviewer in (False, True):
        settings = resolve_execution_settings(config, {}, reviewer=reviewer)
        options = command_options(settings)
        if options:
            for name, command in (
                ("codex_command", config.codex_command),
                ("codex_resume_command", config.codex_resume_command),
            ):
                if "exec" in command or "resume" in command:
                    continue
                profile_checks.append(
                    {
                        "kind": ("review" if reviewer else "default") + f"_{name}",
                        "level": "warning",
                        "message": f"profile options will be appended because {name} has no exec or resume token",
                    }
                )
    return {
        "default_execution_profile": config.default_execution_profile,
        "review_execution_profile": config.review_execution_profile,
        "configured": sorted(config.execution_profiles),
        "allowlisted_config_override_keys": sorted(SAFE_CONFIG_OVERRIDE_KEYS),
        "profiles": {
            name: {
                "has_model": bool(profile.get("model")),
                "has_codex_profile": bool(profile.get("codex_profile")),
                "config_override_keys": sorted((profile.get("config_overrides") or {}).keys()),
                "token_budget_hint": profile.get("token_budget_hint"),
            }
            for name, profile in sorted(config.execution_profiles.items())
        },
        "checks": profile_checks,
    }


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
    return lock_status(config.lock_file, config.stale_lock_seconds)


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


def task_summary(tasks: list[dict[str, Any]], by_id: dict[Any, dict[str, Any]], config: Config) -> dict[str, Any]:
    status_counts = Counter(str(task.get("status") or "unknown") for task in tasks)
    needs_review_count = 0
    resolved_count = 0
    runnable_count = 0
    cooldown_count = 0
    startup_stalled_count = 0
    running_no_progress: list[dict[str, Any]] = []
    recently_stalled: list[dict[str, Any]] = []
    for task in tasks:
        status = task.get("status")
        if task.get("startup_stalled_at") or startup_watchdog_progress(task):
            startup_stalled_count += 1
            recently_stalled.append(stall_evidence(task))
        if status == "running" and running_no_progress_candidate(task, config):
            running_no_progress.append(stall_evidence(task))
        if status == "completed" and review_status(task) in {"unreviewed", "rejected", "needs_followup"}:
            needs_review_count += 1
        if status in {"failed", "blocked_user"} and task.get("resolution"):
            resolved_count += 1
        if is_in_cooldown(task):
            cooldown_count += 1
            continue
        deps_ready, _ = dependency_status(
            task,
            by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )
        if status in RUNNABLE_STATUSES and deps_ready:
            runnable_count += 1
    return {
        "total": len(tasks),
        "by_status": dict(sorted(status_counts.items())),
        "needs_review_completed": needs_review_count,
        "resolved_failed_or_blocked": resolved_count,
        "runnable": runnable_count,
        "cooldown": cooldown_count,
        "startup_stalled": startup_stalled_count,
        "running_no_progress": running_no_progress[:10],
        "recently_stalled": recently_stalled[:10],
    }


def auto_review_summary(tasks: list[dict[str, Any]], config: Config) -> dict[str, Any]:
    reviewable_count = sum(
        1
        for task in tasks
        if task.get("status") == "completed" and review_status(task) in {"unreviewed", "rejected", "needs_followup"}
    )
    return {
        "mechanical_auto_accept_enabled": config.auto_review_mechanical_accept,
        "reviewer_codex_enabled": config.auto_review_codex_enabled,
        "reviewable_completed": reviewable_count,
    }


def worktree_summary(config: Config, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "mode": config.worktree_mode,
        "root": sanitize(config.worktree_root) if config.worktree_root is not None else None,
        "tasks": worktree_task_counts(tasks),
    }


def capacity_summary(config: Config, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    running_tasks = [task for task in tasks if task.get("status") == "running"]
    running_by_pool = Counter(capacity_pool_name(task) for task in running_tasks)
    running_by_project = Counter(capacity_project_key(task) for task in running_tasks)
    configured_pools = {
        name: {"max_running": pool["max_running"]} for name, pool in sorted(config.capacity_pools.items())
    }
    unknown_pools = sorted(name for name in running_by_pool if name not in configured_pools)
    over_pool_capacity = any(
        name not in configured_pools or count > configured_pools[name]["max_running"]
        for name, count in running_by_pool.items()
    )
    max_running_single_project = max(running_by_project.values(), default=0)
    over_total_capacity = len(running_tasks) > config.max_total_running
    over_project_capacity = max_running_single_project > config.max_running_per_project
    return {
        "max_total_running": config.max_total_running,
        "max_running_per_project": config.max_running_per_project,
        "capacity_pools": configured_pools,
        "running_total": len(running_tasks),
        "running_by_pool": dict(sorted(running_by_pool.items())),
        "running_projects": len(running_by_project),
        "max_running_single_project": max_running_single_project,
        "unknown_pools": unknown_pools,
        "over_total_capacity": over_total_capacity,
        "over_project_capacity": over_project_capacity,
        "over_pool_capacity": over_pool_capacity,
        "over_capacity": over_total_capacity or over_project_capacity or over_pool_capacity,
    }


def capacity_pool_name(task: dict[str, Any]) -> str:
    value = task.get("capacity_pool")
    if isinstance(value, str) and value.strip():
        return value
    return "codex"


def capacity_project_key(task: dict[str, Any]) -> str:
    for key in ("project_id", "project_root", "cwd"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return str(task.get("id") or "unknown")


def startup_watchdog_progress(task: dict[str, Any]) -> bool:
    progress = task.get("last_progress")
    return isinstance(progress, dict) and bool(progress.get("watchdog_reason"))


def running_no_progress_candidate(task: dict[str, Any], config: Config) -> bool:
    started = parse_time(task.get("started_at"))
    if not started:
        return False
    if (utc_now() - started).total_seconds() < config.codex_startup_stall_seconds:
        return False
    progress = task.get("last_progress")
    return not isinstance(progress, dict) or not progress.get("first_meaningful_event_at")


def stall_evidence(task: dict[str, Any]) -> dict[str, Any]:
    progress = task.get("last_progress")
    progress = progress if isinstance(progress, dict) else {}
    return {
        "id": task.get("id"),
        "status": task.get("status"),
        "started_at": task.get("started_at"),
        "startup_stalled_at": task.get("startup_stalled_at"),
        "last_error": task.get("last_error"),
        "watchdog_reason": progress.get("watchdog_reason"),
        "stdout_empty": progress.get("stdout_empty"),
        "only_startup_events": progress.get("only_startup_events"),
        "jsonl_event_count": progress.get("jsonl_event_count"),
        "first_jsonl_event_at": progress.get("first_jsonl_event_at"),
        "last_jsonl_event_at": progress.get("last_jsonl_event_at"),
        "first_meaningful_event_at": progress.get("first_meaningful_event_at"),
        "last_meaningful_event_type": progress.get("last_meaningful_event_type"),
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
    if lock.get("pid") is not None:
        lines.append(f"  pid: {lock.get('pid')}")
    if lock.get("pid_alive") is not None:
        lines.append(f"  pid_alive: {str(lock.get('pid_alive')).lower()}")
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
    worktree = report["worktree"]
    worktree_tasks = worktree.get("tasks") or {}
    lines.extend(
        [
            "",
            "worktree:",
            f"  mode: {worktree.get('mode')}",
            f"  root: {worktree.get('root')}",
            f"  retained: {worktree_tasks.get('retained')}",
            f"  recovery_required: {worktree_tasks.get('recovery_required')}",
            f"  missing_metadata: {worktree_tasks.get('missing_metadata')}",
        ]
    )
    by_status = worktree_tasks.get("by_status") or {}
    if by_status:
        lines.append("  by_status:")
        for status, count in by_status.items():
            lines.append(f"    {status}: {count}")
    capacity = report["capacity"]
    lines.extend(
        [
            "",
            "capacity:",
            f"  max_total_running: {capacity.get('max_total_running')}",
            f"  max_running_per_project: {capacity.get('max_running_per_project')}",
            f"  running_total: {capacity.get('running_total')}",
            f"  running_projects: {capacity.get('running_projects')}",
            f"  max_running_single_project: {capacity.get('max_running_single_project')}",
            f"  over_capacity: {str(capacity.get('over_capacity')).lower()}",
        ]
    )
    running_by_pool = capacity.get("running_by_pool") or {}
    capacity_pools = capacity.get("capacity_pools") or {}
    lines.append("  pools:")
    for name, pool in capacity_pools.items():
        lines.append(f"    {name}: max_running={pool.get('max_running')} running={running_by_pool.get(name, 0)}")
    for name in capacity.get("unknown_pools") or []:
        lines.append(f"    {name}: max_running=None running={running_by_pool.get(name, 0)}")
    auto_review = report["auto_review"]
    execution_profiles = report["execution_profiles"]
    lines.extend(
        [
            "",
            "execution_profiles:",
            f"  default_execution_profile: {execution_profiles.get('default_execution_profile')}",
            f"  review_execution_profile: {execution_profiles.get('review_execution_profile')}",
            "  configured: "
            + (", ".join(execution_profiles.get("configured") or []) if execution_profiles.get("configured") else "-"),
            "  allowlisted_config_override_keys: "
            + ", ".join(execution_profiles.get("allowlisted_config_override_keys") or []),
        ]
    )
    profiles = execution_profiles.get("profiles") or {}
    if profiles:
        lines.append("  profiles:")
        for name, profile in profiles.items():
            keys = profile.get("config_override_keys") or []
            lines.append(
                f"    {name}: model={str(profile.get('has_model')).lower()} "
                f"codex_profile={str(profile.get('has_codex_profile')).lower()} "
                f"config_overrides={','.join(keys) if keys else '-'}"
            )
    lines.extend(
        [
            "",
            "auto_review:",
            f"  mechanical_auto_accept_enabled: {str(auto_review.get('mechanical_auto_accept_enabled')).lower()}",
            f"  reviewer_codex_enabled: {str(auto_review.get('reviewer_codex_enabled')).lower()}",
            f"  reviewable_completed: {auto_review.get('reviewable_completed')}",
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
            f"  startup_stalled: {tasks['startup_stalled']}",
            f"  running_no_progress: {len(tasks['running_no_progress'])}",
        ]
    )
    if tasks["recently_stalled"]:
        lines.append("  recently_stalled:")
        for item in tasks["recently_stalled"]:
            lines.append(
                "    - "
                + " ".join(
                    str(part)
                    for part in (
                        item.get("id"),
                        f"status={item.get('status')}",
                        f"reason={item.get('watchdog_reason')}",
                        f"stdout_empty={format_optional_bool(item.get('stdout_empty'))}",
                        f"only_startup_events={format_optional_bool(item.get('only_startup_events'))}",
                    )
                )
            )
    if tasks["running_no_progress"]:
        lines.append("  running_no_progress_tasks:")
        for item in tasks["running_no_progress"]:
            lines.append(f"    - {item.get('id')} started_at={item.get('started_at')}")
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

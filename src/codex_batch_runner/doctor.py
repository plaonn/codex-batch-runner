from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .fs import read_json
from .lock import lock_status
from .model_requirements import SAFE_CONFIG_OVERRIDE_KEYS, command_options, resolve_execution_config
from .queue import RUNNABLE_STATUSES, capacity_blockers, dependency_status, is_in_cooldown
from .state import load_state
from .timeutil import parse_time, utc_now
from .transcript import sanitize
from .worktree import WORKTREE_RETAINED_STATUSES, sanitize_report_value, task_worktree_report, worktree_task_counts

CODEX_VERSION_TIMEOUT_SECONDS = 2.0
DOCTOR_TASK_BRANCH_HUMAN_DETAIL_LIMIT = 20


def build_doctor_report(config: Config) -> dict[str, Any]:
    tasks, task_warnings = load_tasks_for_doctor(config.queue_dir)
    by_id = {task.get("id"): task for task in tasks}
    codex_info = inspect_codex_command(config.codex_command)
    model_config = model_requirement_summary(config)
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
            "name": f"model_requirement_{item['kind']}",
            "level": item["level"],
            "message": item["message"],
        }
        for item in model_config["checks"]
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
        "model_requirements": model_config,
        "decision_cards": decision_card_summary(config),
        "auto_review": auto_review_summary(tasks, config),
        "tasks": task_summary(tasks, by_id, config),
        "checks": checks,
    }
    return report


def model_requirement_summary(config: Config) -> dict[str, Any]:
    config_checks = []
    for reviewer in (False, True):
        settings = resolve_execution_config(config, {}, reviewer=reviewer)
        options = command_options(settings)
        if options:
            for name, command in (
                ("codex_command", config.codex_command),
                ("codex_resume_command", config.codex_resume_command),
            ):
                if "exec" in command or "resume" in command:
                    continue
                config_checks.append(
                    {
                        "kind": ("review" if reviewer else "default") + f"_{name}",
                        "level": "warning",
                        "message": f"model selection options will be appended because {name} has no exec or resume token",
                    }
                )
    provenance = model_selection_provenance(config)
    config_checks.extend(model_selection_provenance_checks(provenance))
    return {
        "default_model_requirement_vector": config.default_model_requirement_vector,
        "review_model_requirement_vector": config.review_model_requirement_vector,
        "model_selection_rules": [rule.get("name") for rule in config.model_selection_rules],
        "execution_targets": sorted(config.execution_targets),
        "allowlisted_config_override_keys": sorted(SAFE_CONFIG_OVERRIDE_KEYS),
        "default_execution_config": {
            "has_model": bool(config.default_execution_config.get("model")),
            "execution_target": config.default_execution_config.get("execution_target"),
            "has_codex_profile": bool(config.default_execution_config.get("codex_profile")),
            "config_override_keys": sorted((config.default_execution_config.get("config_overrides") or {}).keys()),
            "budget_hint": config.default_execution_config.get("budget_hint"),
        },
        "rules": {
            str(rule.get("name")): {
                "when": rule.get("when"),
                "has_model": bool(rule.get("model")),
                "execution_target": rule.get("execution_target"),
                "has_codex_profile": bool(rule.get("codex_profile")),
                "config_override_keys": sorted((rule.get("config_overrides") or {}).keys()),
                "budget_hint": rule.get("budget_hint"),
            }
            for rule in config.model_selection_rules
        },
        "model_selection_provenance": provenance,
        "checks": config_checks,
    }


def model_selection_provenance(config: Config) -> dict[str, Any]:
    return {
        "implementer_selected": resolved_model_selection_provenance(
            resolve_execution_config(config, {}, reviewer=False),
            config,
        ),
        "reviewer_selected": resolved_model_selection_provenance(
            resolve_execution_config(config, {}, reviewer=True),
            config,
        ),
        "default_execution_config": configured_model_selection_provenance(
            "default_execution_config",
            config.default_execution_config,
            config,
        ),
        "rules": {
            str(rule.get("name")): configured_model_selection_provenance("model_selection_rule", rule, config)
            for rule in config.model_selection_rules
        },
    }


def resolved_model_selection_provenance(settings: Any, config: Config) -> dict[str, Any]:
    has_pin = settings.model_source == "explicit_model"
    return {
        "selection_rule": settings.selection_rule,
        "selection_reason": settings.selection_reason,
        "model_source": settings.model_source,
        "execution_target": settings.execution_target,
        "has_explicit_model_pin": has_pin,
        "uses_cli_default_model": settings.model_source == "cli_default",
        "freshness_metadata": selection_freshness_metadata(config, settings.model_source, settings.execution_target, has_pin),
    }


def configured_model_selection_provenance(kind: str, selection: dict[str, Any], config: Config) -> dict[str, Any]:
    target_alias = selection.get("execution_target")
    has_pin = bool(selection.get("model"))
    model_source = "target_alias" if target_alias else ("explicit_model" if has_pin else "cli_default")
    return {
        "kind": kind,
        "model_source": model_source,
        "execution_target": target_alias,
        "has_explicit_model_pin": has_pin,
        "uses_cli_default_model": model_source == "cli_default",
        "freshness_metadata": selection_freshness_metadata(config, model_source, target_alias, has_pin),
    }


def selection_freshness_metadata(
    config: Config,
    model_source: str,
    target_alias: str | None,
    has_pin: bool,
) -> dict[str, Any]:
    if model_source == "target_alias" and target_alias:
        return execution_target_freshness_metadata(config, target_alias)
    return model_pin_freshness_metadata(has_pin)


def execution_target_freshness_metadata(config: Config, target_alias: str) -> dict[str, Any]:
    target = config.execution_targets.get(target_alias) if isinstance(config.execution_targets, dict) else None
    freshness = target.get("freshness") if isinstance(target, dict) and isinstance(target.get("freshness"), dict) else {}
    if not freshness:
        return {"status": "absent", "reason": "target_freshness_not_configured"}
    metadata = {"status": "fresh", "reason": "execution_target"}
    metadata.update(freshness)
    reviewed = parse_time(str(freshness.get("last_reviewed_at") or ""))
    review_after_days = freshness.get("review_after_days")
    if reviewed is None or not isinstance(review_after_days, int):
        metadata["status"] = "absent"
        metadata["reason"] = "target_freshness_review_window_not_configured"
        return metadata
    review_due_date = reviewed.date() + timedelta(days=review_after_days)
    checked_date = utc_now().date()
    metadata["checked_at"] = checked_date.isoformat()
    metadata["review_due_at"] = review_due_date.isoformat()
    metadata["stale"] = checked_date >= review_due_date
    if metadata["stale"]:
        metadata["status"] = "stale"
        metadata["reason"] = "review_after_days_elapsed"
    return metadata


def model_pin_freshness_metadata(has_pin: bool) -> dict[str, str | None]:
    if not has_pin:
        return {"status": "not_applicable", "reason": "no_explicit_model_pin"}
    return {
        "status": "absent",
        "reason": "direct_model_pin_without_execution_target",
    }


def model_selection_provenance_checks(provenance: dict[str, Any]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for name in ("implementer_selected", "reviewer_selected"):
        selected = provenance.get(name) if isinstance(provenance.get(name), dict) else {}
        if selected.get("uses_cli_default_model"):
            checks.append(
                {
                    "kind": f"model_selection_{name}",
                    "level": "warning",
                    "message": "selected execution config relies on the Codex CLI default model because no model is configured",
                }
            )
    default = provenance.get("default_execution_config")
    if isinstance(default, dict) and default.get("has_explicit_model_pin"):
        checks.append(
            {
                "kind": "model_selection_default_execution_config_freshness",
                "level": "warning",
                "message": "default_execution_config has an explicit model pin without execution_target freshness metadata",
            }
        )
    checks.extend(
        target_freshness_checks(
            default if isinstance(default, dict) else {},
            kind="model_selection_default_execution_config_target_freshness",
            label="default_execution_config",
        )
    )
    rules = provenance.get("rules") if isinstance(provenance.get("rules"), dict) else {}
    for name, rule in rules.items():
        if isinstance(rule, dict) and rule.get("has_explicit_model_pin"):
            checks.append(
                {
                    "kind": f"model_selection_rule_{name}_freshness",
                    "level": "warning",
                    "message": "model_selection_rule has an explicit model pin without execution_target freshness metadata",
                }
            )
        checks.extend(
            target_freshness_checks(
                rule if isinstance(rule, dict) else {},
                kind=f"model_selection_rule_{name}_target_freshness",
                label="model_selection_rule",
            )
        )
    return checks


def target_freshness_checks(selection: dict[str, Any], *, kind: str, label: str) -> list[dict[str, str]]:
    if selection.get("model_source") != "target_alias":
        return []
    status = selection.get("freshness_metadata", {}).get("status")
    if status == "absent":
        return [
            {
                "kind": kind,
                "level": "warning",
                "message": f"{label} execution_target has no freshness metadata",
            }
        ]
    if status == "stale":
        return [
            {
                "kind": f"{kind}_stale",
                "level": "warning",
                "message": f"{label} execution_target freshness metadata is stale",
            }
        ]
    return []


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
    runner_pause = state.get("runner_pause") if isinstance(state.get("runner_pause"), dict) else {}
    return {
        "global_cooldown_until": cooldown_until,
        "global_cooldown_active": bool(parsed and parsed > utc_now()),
        "runner_pause": {
            "active": bool(runner_pause.get("active")),
            "reason": runner_pause.get("reason"),
            "paused_at": runner_pause.get("paused_at"),
            "paused_by": runner_pause.get("paused_by"),
        },
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
    resolved_review_completed_count = 0
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
        if (
            status == "completed"
            and not task.get("resolution")
            and review_status(task) in {"unreviewed", "rejected", "needs_followup"}
        ):
            needs_review_count += 1
        if status in {"failed", "blocked_user"} and task.get("resolution"):
            resolved_count += 1
        if status == "completed" and task.get("resolution"):
            resolved_review_completed_count += 1
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
        "resolved_review_completed": resolved_review_completed_count,
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
        if task.get("status") == "completed"
        and not task.get("resolution")
        and review_status(task) in {"unreviewed", "rejected", "needs_followup"}
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
        "task_branches": task_branch_lifecycle_summary(tasks),
    }


def task_branch_lifecycle_summary(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for task in tasks:
        if task.get("execution_mode") != "git_worktree":
            continue
        branch = str(task.get("execution_branch") or "").strip()
        worktree_report = task_worktree_report(task)
        item: dict[str, Any] = {
            "task_id": sanitize_report_value(task.get("id")),
            "status": task.get("status"),
            "review_status": task.get("review_status"),
            "branch": sanitize_report_value(branch) if branch else None,
            "worktree_status": task.get("execution_worktree_status"),
            "retained_metadata": str(task.get("execution_worktree_status") or "") in WORKTREE_RETAINED_STATUSES,
            "path_exists": worktree_report.get("path_exists"),
            "local_branch_exists": worktree_report.get("branch_exists"),
            "local_branch_head": worktree_report.get("branch_head"),
            "apply_status": task.get("execution_apply_status"),
            "applied_head": sanitize_report_value(task.get("execution_applied_head")),
            "cleanup_kind": task.get("execution_cleanup_kind"),
            "cleanup_result_applied": task.get("execution_cleanup_result_applied"),
            "cleanup_branch_retained": task.get("execution_cleanup_branch_retained"),
            "branch_prune_status": task.get("execution_branch_prune_status"),
            "branch_pruned_head": sanitize_report_value(task.get("execution_branch_pruned_head")),
            "branch_pruned_at": task.get("execution_branch_pruned_at"),
            "recovery_required": worktree_report.get("recovery_required"),
            "applied_metadata": worktree_report.get("applied_metadata"),
            "remote_task_branch": remote_task_branch_summary(task, branch),
        }
        items.append({key: value for key, value in item.items() if value not in (None, "", [])})
    return items


def remote_task_branch_summary(task: dict[str, Any], branch: str) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "configured_upstream": None,
        "known_remote_refs": [],
        "known": False,
        "warnings": [],
    }
    if not branch:
        return summary
    repo_value = task.get("execution_repo_root") or task.get("project_root") or task.get("cwd")
    if not repo_value:
        summary["warnings"].append("missing repository metadata")
        return summary
    try:
        repo_root = Path(str(repo_value)).expanduser()
        upstream = run_git(repo_root, ["for-each-ref", "--format=%(upstream:short)", f"refs/heads/{branch}"])
        if upstream.returncode == 0 and upstream.stdout.strip():
            summary["configured_upstream"] = sanitize(upstream.stdout.strip())
        elif upstream.returncode != 0:
            summary["warnings"].append(f"cannot inspect branch upstream: {clean_git_error(upstream)}")
        remotes = run_git(repo_root, ["for-each-ref", "--format=%(refname:short)", "refs/remotes"])
        if remotes.returncode == 0:
            suffix = f"/{branch}"
            refs = sorted(ref for ref in remotes.stdout.splitlines() if ref.strip().endswith(suffix))
            summary["known_remote_refs"] = [sanitize(ref) for ref in refs]
        else:
            summary["warnings"].append(f"cannot inspect remote refs: {clean_git_error(remotes)}")
    except OSError as exc:
        summary["warnings"].append("cannot inspect remote refs: " + sanitize(str(exc)))
    summary["known"] = bool(summary["configured_upstream"] or summary["known_remote_refs"])
    return summary


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
    capacity_blocked: Counter[str] = Counter()
    admissible_runnable = 0
    capacity_blocked_count = 0
    for task in tasks:
        if task.get("status") not in RUNNABLE_STATUSES or is_in_cooldown(task):
            continue
        blockers = capacity_blockers(config, task)
        if blockers:
            capacity_blocked_count += 1
            capacity_blocked.update(blockers)
        else:
            admissible_runnable += 1
    return {
        "max_total_running": config.max_total_running,
        "max_running_per_project": config.max_running_per_project,
        "capacity_pools": configured_pools,
        "running_total": len(running_tasks),
        "running_by_pool": dict(sorted(running_by_pool.items())),
        "running_projects": len(running_by_project),
        "max_running_single_project": max_running_single_project,
        "unknown_pools": unknown_pools,
        "admissible_runnable": admissible_runnable,
        "capacity_blocked_runnable": capacity_blocked_count,
        "capacity_blocked_reasons": dict(sorted(capacity_blocked.items())),
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


def decision_card_summary(config: Config) -> dict[str, Any]:
    from .decision_cards import build_decision_card_inventory, decision_card_next_action

    inventory = build_decision_card_inventory(config)
    summary = inventory.get("summary") if isinstance(inventory.get("summary"), dict) else {}
    card_count = int(summary.get("card_count") or 0)
    return {
        "read_only": True,
        "mutation_allowed": False,
        "card_count": card_count,
        "decision_required": summary.get("decision_required", 0),
        "approval_blocked": summary.get("approval_blocked", 0),
        "not_ready": summary.get("not_ready", 0),
        "next_action": summary.get("next_action") or decision_card_next_action(card_count),
        "by_source": summary.get("by_source") if isinstance(summary.get("by_source"), dict) else {},
        "by_recommendation": (
            summary.get("by_recommendation") if isinstance(summary.get("by_recommendation"), dict) else {}
        ),
        "by_blocked_reason": (
            summary.get("by_blocked_reason") if isinstance(summary.get("by_blocked_reason"), dict) else {}
        ),
        "source_reports": inventory.get("source_reports") if isinstance(inventory.get("source_reports"), list) else [],
    }


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
            f"  runner_pause_active: {str(bool((state.get('runner_pause') or {}).get('active'))).lower()}",
            f"  runner_pause_reason: {(state.get('runner_pause') or {}).get('reason')}",
            f"  runner_pause_paused_at: {(state.get('runner_pause') or {}).get('paused_at')}",
            f"  runner_pause_paused_by: {(state.get('runner_pause') or {}).get('paused_by')}",
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
    task_branches = worktree.get("task_branches") or []
    if task_branches:
        displayed_task_branches = task_branches[:DOCTOR_TASK_BRANCH_HUMAN_DETAIL_LIMIT]
        omitted_task_branch_count = max(0, len(task_branches) - len(displayed_task_branches))
        lines.append(f"  task_branches_total: {len(task_branches)}")
        lines.append(f"  task_branches_displayed: {len(displayed_task_branches)}")
        if omitted_task_branch_count:
            lines.append(f"  task_branches_omitted: {omitted_task_branch_count}")
        lines.append("  task_branches:")
        for item in displayed_task_branches:
            remote = item.get("remote_task_branch") if isinstance(item.get("remote_task_branch"), dict) else {}
            remote_known = str(bool(remote.get("known"))).lower()
            upstream = remote.get("configured_upstream") or "-"
            remote_refs = ",".join(remote.get("known_remote_refs") or []) or "-"
            lines.append(
                "    - "
                f"{item.get('task_id')} "
                f"branch={item.get('branch') or '-'} "
                f"worktree_status={item.get('worktree_status') or '-'} "
                f"retained_metadata={str(bool(item.get('retained_metadata'))).lower()} "
                f"local_branch_exists={format_optional_bool(item.get('local_branch_exists'))} "
                f"apply_status={item.get('apply_status') or '-'} "
                f"cleanup_kind={item.get('cleanup_kind') or '-'} "
                f"branch_prune_status={item.get('branch_prune_status') or '-'} "
                f"remote_known={remote_known} "
                f"upstream={upstream} "
                f"remote_refs={remote_refs}"
            )
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
            f"  admissible_runnable: {capacity.get('admissible_runnable')}",
            f"  capacity_blocked_runnable: {capacity.get('capacity_blocked_runnable')}",
            f"  over_capacity: {str(capacity.get('over_capacity')).lower()}",
        ]
    )
    blocked_reasons = capacity.get("capacity_blocked_reasons") or {}
    if blocked_reasons:
        lines.append("  blocked_reasons:")
        for reason, count in blocked_reasons.items():
            lines.append(f"    {reason}: {count}")
    running_by_pool = capacity.get("running_by_pool") or {}
    capacity_pools = capacity.get("capacity_pools") or {}
    lines.append("  pools:")
    for name, pool in capacity_pools.items():
        lines.append(f"    {name}: max_running={pool.get('max_running')} running={running_by_pool.get(name, 0)}")
    for name in capacity.get("unknown_pools") or []:
        lines.append(f"    {name}: max_running=None running={running_by_pool.get(name, 0)}")
    auto_review = report["auto_review"]
    model_requirements = report["model_requirements"]
    lines.extend(
        [
            "",
            "model_requirements:",
            "  model_selection_rules: "
            + (
                ", ".join(model_requirements.get("model_selection_rules") or [])
                if model_requirements.get("model_selection_rules")
                else "-"
            ),
            "  execution_targets: "
            + (
                ", ".join(model_requirements.get("execution_targets") or [])
                if model_requirements.get("execution_targets")
                else "-"
            ),
            "  allowlisted_config_override_keys: "
            + ", ".join(model_requirements.get("allowlisted_config_override_keys") or []),
        ]
    )
    rules = model_requirements.get("rules") or {}
    if rules:
        lines.append("  rules:")
        for name, rule in rules.items():
            keys = rule.get("config_override_keys") or []
            lines.append(
                f"    {name}: model={str(rule.get('has_model')).lower()} "
                f"target={rule.get('execution_target') or '-'} "
                f"codex_profile={str(rule.get('has_codex_profile')).lower()} "
                f"config_overrides={','.join(keys) if keys else '-'}"
            )
    provenance = model_requirements.get("model_selection_provenance") or {}
    lines.append("  model_selection_provenance:")
    default_provenance = provenance.get("default_execution_config") or {}
    lines.append(
        "    default_execution_config: "
        f"model_source={default_provenance.get('model_source') or '-'} "
        f"target={default_provenance.get('execution_target') or '-'} "
        f"explicit_pin={str(bool(default_provenance.get('has_explicit_model_pin'))).lower()} "
        f"freshness={freshness_status(default_provenance)}"
    )
    for label, selected in (
        ("implementer_selected", provenance.get("implementer_selected") or {}),
        ("reviewer_selected", provenance.get("reviewer_selected") or {}),
    ):
        lines.append(
            f"    {label}: "
            f"selection_rule={selected.get('selection_rule') or '-'} "
            f"model_source={selected.get('model_source') or '-'} "
            f"target={selected.get('execution_target') or '-'} "
            f"explicit_pin={str(bool(selected.get('has_explicit_model_pin'))).lower()} "
            f"freshness={freshness_status(selected)}"
        )
    provenance_rules = provenance.get("rules") or {}
    if provenance_rules:
        lines.append("    rules:")
        for name, rule in provenance_rules.items():
            lines.append(
                f"      {name}: model_source={rule.get('model_source') or '-'} "
                f"target={rule.get('execution_target') or '-'} "
                f"explicit_pin={str(bool(rule.get('has_explicit_model_pin'))).lower()} "
                f"freshness={freshness_status(rule)}"
            )
    decision_cards = report["decision_cards"]
    lines.extend(
        [
            "",
            "decision_cards:",
            f"  read_only: {str(decision_cards.get('read_only')).lower()}",
            f"  mutation_allowed: {str(decision_cards.get('mutation_allowed')).lower()}",
            f"  card_count: {decision_cards.get('card_count')}",
            f"  decision_required: {decision_cards.get('decision_required')}",
            f"  approval_blocked: {decision_cards.get('approval_blocked')}",
            f"  not_ready: {decision_cards.get('not_ready')}",
            "  open_decisions: " + ("none" if decision_cards.get("card_count") == 0 else "present"),
            f"  next_action: {decision_cards.get('next_action') or 'none'}",
        ]
    )
    for label, key in (
        ("by_source", "by_source"),
        ("by_recommendation", "by_recommendation"),
        ("by_blocked_reason", "by_blocked_reason"),
    ):
        group = decision_cards.get(key) if isinstance(decision_cards.get(key), dict) else {}
        if not group:
            continue
        lines.append(f"  {label}:")
        for name, count in group.items():
            lines.append(f"    {name}: {count}")
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
            f"  resolved_review_completed: {tasks['resolved_review_completed']}",
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


def freshness_status(item: dict[str, Any]) -> str:
    metadata = item.get("freshness_metadata") if isinstance(item.get("freshness_metadata"), dict) else {}
    status = metadata.get("status") or "-"
    reason = metadata.get("reason")
    return f"{status}({reason})" if reason else str(status)


def review_status(task: dict[str, Any]) -> str:
    if task.get("status") == "completed":
        return str(task.get("review_status") or "unreviewed")
    return str(task.get("review_status") or "")

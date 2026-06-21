from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .config import Config
from .events import transition_payload, write_event_nonfatal
from .lock import FileLock
from .queue import load_task, save_task
from .timeutil import iso_now


PREPARE_OK_STATUSES = {"runnable", "needs_resume"}
CLEANUP_OK_STATUSES = {"archived"}
WORKTREE_RETAINED_STATUSES = {"prepared", "running", "retained", "cleanup_candidate"}


def sanitize_branch_name(task_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id.strip())
    slug = re.sub(r"[-_.]{2,}", "-", slug).strip("-._")
    slug = slug.replace("@{", "-")
    if not slug:
        slug = "task"
    if slug.endswith(".lock"):
        slug = slug[: -len(".lock")] or "task"
    branch = f"cbr/{slug[:180]}"
    validate_branch_name(branch)
    return branch


def build_prepare_report(config: Config, task_id: str, *, apply: bool = False) -> dict[str, Any]:
    if apply:
        return _with_lock(config, task_id, lambda: _build_prepare_report_locked(config, task_id, apply=True))
    return _build_prepare_report_locked(config, task_id, apply=False)


def build_cleanup_report(config: Config, task_id: str, *, apply: bool = False) -> dict[str, Any]:
    if apply:
        return _with_lock(config, task_id, lambda: _build_cleanup_report_locked(config, task_id, apply=True))
    return _build_cleanup_report_locked(config, task_id, apply=False)


def _with_lock(config: Config, task_id: str, callback) -> dict[str, Any]:
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire(task_id=task_id):
        return {
            "task_id": task_id,
            "action": "locked",
            "applied": False,
            "errors": [f"another runner is active: {config.lock_file}"],
            "warnings": [],
        }
    try:
        return callback()
    finally:
        lock.release()


def _build_prepare_report_locked(config: Config, task_id: str, *, apply: bool) -> dict[str, Any]:
    task = load_task(config, task_id)
    report = base_report("prepare", task, apply)
    if config.worktree_mode != "task":
        report["errors"].append("worktree_mode is disabled; set worktree_mode=task to prepare task worktrees")
        return report
    if task.get("status") not in PREPARE_OK_STATUSES and task.get("execution_worktree_status") != "prepared":
        report["errors"].append(f"task status {task.get('status')} is not eligible for worktree prepare")
        return report

    try:
        repo = repo_context(task)
        branch = sanitize_branch_name(str(task.get("id") or task_id))
        worktree_path = guarded_worktree_path(config, branch)
        registry = worktree_registry(repo["repo_root"])
        branch_state = local_branch_state(repo["repo_root"], branch)
        classification = classify_prepare_state(task, branch, worktree_path, registry, branch_state)
        report.update(
            {
                "repo_root": str(repo["repo_root"]),
                "base_ref": repo["base_ref"],
                "base_head": repo["base_head"],
                "branch": branch,
                "worktree_path": str(worktree_path),
                "classification": classification,
            }
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        report["errors"].append(str(exc))
        return report

    if classification["status"] == "recovery_required":
        report["errors"].append(classification["reason"])
    elif classification["status"] == "prepared":
        report["warnings"].append(classification["reason"])
    elif branch_state["exists"]:
        report["errors"].append("existing branch is not linked to this task metadata")

    previous_base = task.get("execution_base_head")
    if previous_base and previous_base != repo["base_head"] and task.get("execution_worktree_status") != "prepared":
        report["errors"].append("stale base: task metadata records a different execution_base_head")

    if report["errors"] or not apply:
        return report

    if classification["status"] == "absent":
        git(repo["repo_root"], "worktree", "add", "-b", branch, str(worktree_path), repo["base_head"])

    task.update(
        {
            "execution_mode": "git_worktree",
            "execution_original_cwd": task.get("cwd"),
            "execution_repo_root": str(repo["repo_root"]),
            "execution_worktree_path": str(worktree_path),
            "execution_worktree_root": str(config.worktree_root),
            "execution_branch": branch,
            "execution_base_ref": repo["base_ref"],
            "execution_base_head": repo["base_head"],
            "execution_worktree_status": "prepared",
            "execution_prepared_at": iso_now(),
        }
    )
    save_task(config, task)
    report["applied"] = True
    report["classification"] = {**classification, "status": "prepared", "reason": "worktree prepared"}
    write_event_nonfatal(
        config,
        "task_worktree_prepared",
        task=task,
        source="worktree prepare",
        summary=f"prepared worktree for task {task_id}",
        payload=transition_payload(
            task,
            execution_mode="git_worktree",
            execution_branch=branch,
            execution_worktree_status="prepared",
        ),
    )
    return report


def _build_cleanup_report_locked(config: Config, task_id: str, *, apply: bool) -> dict[str, Any]:
    task = load_task(config, task_id)
    report = base_report("cleanup", task, apply)
    branch = str(task.get("execution_branch") or "")
    worktree_raw = task.get("execution_worktree_path")
    if not branch or not worktree_raw:
        report["classification"] = {"status": "missing", "reason": "task has no worktree metadata"}
        report["warnings"].append("task has no worktree metadata")
        return report
    if task.get("status") not in CLEANUP_OK_STATUSES and not (
        task.get("status") == "completed" and task.get("review_status") == "accepted"
    ):
        report["errors"].append("worktree cleanup is only allowed for archived or completed accepted tasks")
        return report
    try:
        validate_branch_name(branch)
        worktree_path = guarded_existing_worktree_path(config, Path(str(worktree_raw)))
        repo_root = Path(str(task.get("execution_repo_root") or task.get("project_root") or task.get("cwd"))).expanduser().resolve()
        registry = worktree_registry(repo_root)
        classification = classify_cleanup_state(task, branch, worktree_path, registry)
        report.update(
            {
                "repo_root": str(repo_root),
                "branch": branch,
                "worktree_path": str(worktree_path),
                "classification": classification,
            }
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        report["errors"].append(str(exc))
        return report

    if classification["status"] == "recovery_required":
        report["errors"].append(classification["reason"])
    if report["errors"] or not apply:
        return report

    if classification["status"] == "cleanup_candidate":
        git(repo_root, "worktree", "remove", str(worktree_path))
    task["execution_worktree_status"] = "cleaned"
    task["execution_cleaned_at"] = iso_now()
    save_task(config, task)
    report["applied"] = True
    report["classification"] = {**classification, "status": "cleaned", "reason": "worktree cleaned; branch retained"}
    write_event_nonfatal(
        config,
        "task_worktree_cleaned",
        task=task,
        source="worktree cleanup",
        summary=f"cleaned worktree for task {task_id}",
        payload=transition_payload(
            task,
            execution_mode=task.get("execution_mode"),
            execution_branch=branch,
            execution_worktree_status="cleaned",
        ),
    )
    return report


def base_report(action: str, task: dict[str, Any], apply: bool) -> dict[str, Any]:
    return {
        "task_id": task.get("id"),
        "action": action,
        "mode": "apply" if apply else "dry-run",
        "applied": False,
        "errors": [],
        "warnings": [],
    }


def repo_context(task: dict[str, Any]) -> dict[str, Any]:
    cwd = Path(str(task.get("cwd") or "")).expanduser()
    if not cwd:
        raise ValueError("task cwd is missing")
    repo_root = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
    base_head = git(repo_root, "rev-parse", "HEAD")
    return {"repo_root": repo_root, "base_ref": "HEAD", "base_head": base_head}


def guarded_worktree_path(config: Config, branch: str) -> Path:
    if config.worktree_root is None:
        raise ValueError("worktree_root is not configured")
    slug = branch.split("/", 1)[1]
    return guard_path_under_root(config.worktree_root, config.worktree_root / slug)


def guarded_existing_worktree_path(config: Config, path: Path) -> Path:
    if config.worktree_root is None:
        raise ValueError("worktree_root is not configured")
    return guard_path_under_root(config.worktree_root, path)


def guard_path_under_root(root: Path, path: Path) -> Path:
    root_resolved = root.expanduser().resolve()
    target = path.expanduser().resolve()
    if target == root_resolved or root_resolved not in target.parents:
        raise ValueError("worktree path must be inside configured worktree_root")
    if str(target) in {"/", ""}:
        raise ValueError("refusing unsafe worktree path")
    return target


def classify_prepare_state(
    task: dict[str, Any],
    branch: str,
    worktree_path: Path,
    registry: list[dict[str, str]],
    branch_state: dict[str, Any],
) -> dict[str, str]:
    registered = registry_entry_for_path(registry, worktree_path)
    registered_by_branch = registry_entry_for_branch(registry, branch)
    path_exists = worktree_path.exists()
    metadata_matches = task.get("execution_branch") == branch and Path(str(task.get("execution_worktree_path") or worktree_path)).expanduser().resolve() == worktree_path
    if registered and not path_exists:
        return {"status": "recovery_required", "reason": "git worktree registry points to a missing path"}
    if path_exists and not registered:
        return {"status": "recovery_required", "reason": "worktree path exists but is not registered by git"}
    if registered_by_branch and registered_by_branch is not registered:
        return {"status": "recovery_required", "reason": "branch is already checked out in a different worktree"}
    if registered and registered.get("branch") != f"refs/heads/{branch}":
        return {"status": "recovery_required", "reason": "registered worktree branch does not match task branch"}
    if registered and metadata_matches:
        return {"status": "prepared", "reason": "matching worktree already exists"}
    if registered:
        return {"status": "recovery_required", "reason": "existing worktree is not linked to this task metadata"}
    if branch_state["exists"] and not metadata_matches:
        return {"status": "existing_branch", "reason": "branch exists without matching task metadata"}
    return {"status": "absent", "reason": "worktree and branch are absent"}


def classify_cleanup_state(
    task: dict[str, Any],
    branch: str,
    worktree_path: Path,
    registry: list[dict[str, str]],
) -> dict[str, str]:
    registered = registry_entry_for_path(registry, worktree_path)
    if not worktree_path.exists() and not registered:
        return {"status": "missing", "reason": "worktree path and registry entry are already absent"}
    if worktree_path.exists() and not registered:
        return {"status": "recovery_required", "reason": "worktree path exists but is not registered by git"}
    if registered and not worktree_path.exists():
        return {"status": "recovery_required", "reason": "git worktree registry points to a missing path"}
    if registered and registered.get("branch") != f"refs/heads/{branch}":
        return {"status": "recovery_required", "reason": "registered worktree branch does not match task metadata"}
    if not any(worktree_path.iterdir()):
        return {"status": "recovery_required", "reason": "refusing to cleanup an empty worktree path"}
    return {"status": "cleanup_candidate", "reason": "worktree can be removed; branch will be retained"}


def local_branch_state(repo_root: Path, branch: str) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show-ref", "--verify", f"refs/heads/{branch}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        return {"exists": False, "head": None}
    return {"exists": True, "head": result.stdout.split()[0] if result.stdout.split() else None}


def worktree_registry(repo_root: Path) -> list[dict[str, str]]:
    output = git(repo_root, "worktree", "list", "--porcelain")
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                entries.append(current)
            current = {"path": value}
        else:
            current[key] = value
    if current:
        entries.append(current)
    return entries


def registry_entry_for_path(registry: list[dict[str, str]], path: Path) -> dict[str, str] | None:
    target = str(path)
    for entry in registry:
        if str(Path(entry.get("path", "")).expanduser().resolve()) == target:
            return entry
    return None


def registry_entry_for_branch(registry: list[dict[str, str]], branch: str) -> dict[str, str] | None:
    ref = f"refs/heads/{branch}"
    for entry in registry:
        if entry.get("branch") == ref:
            return entry
    return None


def validate_branch_name(branch: str) -> None:
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"invalid worktree branch name: {branch}")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def task_worktree_metadata(task: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"execution_mode": task.get("execution_mode") or "main_worktree"}
    for source, target in (
        ("execution_branch", "branch"),
        ("execution_base_ref", "base_ref"),
        ("execution_base_head", "base_head"),
        ("execution_worktree_status", "worktree_status"),
        ("execution_worktree_path", "worktree_path"),
        ("execution_worktree_root", "worktree_root"),
        ("execution_repo_root", "repo_root"),
        ("execution_original_cwd", "original_cwd"),
        ("execution_parent_task_id", "parent_task_id"),
        ("execution_merge_target", "merge_target"),
    ):
        value = task.get(source)
        if value not in (None, ""):
            metadata[target] = sanitize_report_value(value)
    return metadata


def task_worktree_report(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task_worktree_metadata(task)
    report: dict[str, Any] = {
        "metadata": metadata,
        "warnings": [],
        "missing_metadata": [],
        "stale_metadata": [],
        "recovery_required": False,
        "path_exists": None,
        "branch_exists": None,
    }
    if metadata.get("execution_mode") != "git_worktree":
        return report

    required = {
        "execution_branch": "branch",
        "execution_base_ref": "base_ref",
        "execution_base_head": "base_head",
        "execution_worktree_status": "worktree_status",
        "execution_worktree_path": "worktree_path",
    }
    for source, public_name in required.items():
        if not task.get(source):
            report["missing_metadata"].append(public_name)
    if report["missing_metadata"]:
        report["warnings"].append("git_worktree task has incomplete worktree metadata")

    status = str(task.get("execution_worktree_status") or "")
    if status == "recovery_required":
        report["recovery_required"] = True
        report["warnings"].append("task worktree metadata is marked recovery_required")

    path_value = task.get("execution_worktree_path")
    if path_value:
        try:
            worktree_path = Path(str(path_value)).expanduser()
            report["path_exists"] = worktree_path.exists()
            if not report["path_exists"] and status in WORKTREE_RETAINED_STATUSES:
                report["recovery_required"] = True
                report["stale_metadata"].append("worktree_path")
                report["warnings"].append("retained worktree metadata points to a missing path")
        except OSError as exc:
            report["recovery_required"] = True
            report["stale_metadata"].append("worktree_path")
            report["warnings"].append("cannot inspect worktree path: " + sanitize_report_value(exc))

    repo_value = task.get("execution_repo_root") or task.get("project_root") or task.get("cwd")
    branch = str(task.get("execution_branch") or "")
    if repo_value and branch:
        try:
            repo_root = Path(str(repo_value)).expanduser()
            branch_state = local_branch_state(repo_root, branch)
            report["branch_exists"] = branch_state.get("exists")
            if not branch_state.get("exists") and status in WORKTREE_RETAINED_STATUSES:
                report["recovery_required"] = True
                report["stale_metadata"].append("branch")
                report["warnings"].append("retained worktree metadata points to a missing branch")
            if branch_state.get("head"):
                report["branch_head"] = sanitize_report_value(branch_state.get("head"))
        except (OSError, subprocess.SubprocessError) as exc:
            report["warnings"].append("cannot inspect worktree branch: " + sanitize_report_value(exc))

    report["missing_metadata"] = sorted(set(report["missing_metadata"]))
    report["stale_metadata"] = sorted(set(report["stale_metadata"]))
    report["warnings"] = sorted(set(report["warnings"]))
    return report


def worktree_task_counts(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    retained = 0
    recovery_required = 0
    missing_metadata = 0
    for task in tasks:
        status = str(task.get("execution_worktree_status") or "")
        if status:
            by_status[status] = by_status.get(status, 0) + 1
        if status in WORKTREE_RETAINED_STATUSES:
            retained += 1
        report = task_worktree_report(task)
        if report.get("missing_metadata"):
            missing_metadata += 1
        if report.get("recovery_required"):
            recovery_required += 1
    return {
        "by_status": dict(sorted(by_status.items())),
        "retained": retained,
        "recovery_required": recovery_required,
        "missing_metadata": missing_metadata,
    }


def sanitize_report_value(value: object) -> Any:
    from .transcript import sanitize

    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize(value)


def render_worktree_report(report: dict[str, Any]) -> str:
    lines = [
        f"action: {report.get('action')}",
        f"mode: {report.get('mode')}",
        f"task_id: {report.get('task_id')}",
        f"applied: {str(bool(report.get('applied'))).lower()}",
    ]
    for key in ("branch", "worktree_path", "base_ref", "base_head"):
        if report.get(key):
            lines.append(f"{key}: {report.get(key)}")
    classification = report.get("classification")
    if isinstance(classification, dict):
        lines.append(f"classification: {classification.get('status')} ({classification.get('reason')})")
    for warning in report.get("warnings") or []:
        lines.append(f"warning: {warning}")
    for error in report.get("errors") or []:
        lines.append(f"error: {error}")
    return "\n".join(lines) + "\n"

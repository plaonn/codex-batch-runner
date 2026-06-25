from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config
from .events import write_event_nonfatal
from .lock import FileLock
from .worktree import (
    clean_git_result,
    git,
    git_optional,
    is_ancestor,
    local_branch_state,
    run_git,
    validate_branch_name,
    worktree_registry,
)

DIRECT_BRANCH_PREFIX = "codex/"
PROTECTED_BRANCHES = {"main", "master", "develop"}


def build_direct_worktrees_report(config: Config, *, apply: bool = False) -> dict[str, Any]:
    if apply:
        lock = FileLock(config.lock_file, config.stale_lock_seconds)
        if not lock.acquire(task_id="maintenance:direct-worktrees"):
            return {
                "action": "direct-worktrees",
                "mode": "apply",
                "status": "blocked",
                "applied": False,
                "errors": [f"another runner is active: {config.lock_file}"],
                "warnings": [],
                "eligible": [],
                "blocked": [],
                "candidates": [],
                "results": [],
            }
        try:
            return _build_direct_worktrees_report_locked(config, apply=True)
        finally:
            lock.release()
    return _build_direct_worktrees_report_locked(config, apply=False)


def _build_direct_worktrees_report_locked(config: Config, *, apply: bool) -> dict[str, Any]:
    report = base_direct_report(apply=apply)
    try:
        repo_root = Path(git(config.root, "rev-parse", "--show-toplevel")).resolve()
        target = git_optional(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD") or "HEAD"
        target_head = git(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
        task_root = config.worktree_root.expanduser().resolve() if config.worktree_root else None
        entries = worktree_registry(repo_root)
    except Exception as exc:
        report["status"] = "failed"
        report["errors"].append(str(exc))
        return report

    report.update(
        {
            "repo_root": str(repo_root),
            "target": target,
            "target_head": target_head,
            "worktree_root": str(task_root) if task_root else None,
            "allowlist": {
                "branch_prefix": DIRECT_BRANCH_PREFIX,
                "path_pattern": f"../{repo_root.name}-*",
            },
            "planned_action": "git worktree remove <path>; git branch -d <branch>",
        }
    )
    candidates = [
        classify_direct_worktree_entry(config, repo_root, task_root, target, entry)
        for entry in entries
        if should_inspect_entry(repo_root, task_root, entry)
    ]
    report["candidates"] = candidates
    report["eligible"] = [candidate for candidate in candidates if candidate.get("eligible")]
    report["blocked"] = [candidate for candidate in candidates if not candidate.get("eligible")]
    report["summary"] = direct_summary(candidates)

    if not apply:
        report["status"] = dry_run_status(report["eligible"], report["blocked"])
        return report

    if not report["eligible"]:
        report["status"] = "blocked"
        return report

    results: list[dict[str, Any]] = []
    for candidate in report["eligible"]:
        refreshed = refreshed_candidate(config, repo_root, task_root, target, candidate)
        if not refreshed.get("eligible"):
            result = apply_result_from_refreshed(refreshed, status="blocked")
            results.append(result)
            continue
        result = remove_direct_worktree(config, repo_root, target, refreshed)
        results.append(result)
    report["results"] = results
    report["applied"] = any(result.get("worktree_removed") for result in results)
    report["status"] = "partial" if any(result.get("status") != "cleaned" for result in results) else "applied"
    return report


def base_direct_report(*, apply: bool) -> dict[str, Any]:
    return {
        "action": "direct-worktrees",
        "mode": "apply" if apply else "dry-run",
        "status": "pending",
        "applied": False,
        "errors": [],
        "warnings": [],
        "eligible": [],
        "blocked": [],
        "candidates": [],
        "results": [],
    }


def should_inspect_entry(repo_root: Path, task_root: Path | None, entry: dict[str, str]) -> bool:
    path_value = entry.get("path")
    if not path_value:
        return False
    try:
        path = Path(path_value).expanduser().resolve()
    except OSError:
        return True
    if path == repo_root:
        return False
    if task_root and is_path_under(path, task_root):
        return False
    return True


def classify_direct_worktree_entry(
    config: Config,
    repo_root: Path,
    task_root: Path | None,
    target: str,
    entry: dict[str, str],
) -> dict[str, Any]:
    path = Path(entry.get("path", "")).expanduser().resolve()
    branch_ref = entry.get("branch") or ""
    branch = branch_ref.removeprefix("refs/heads/")
    head = entry.get("HEAD")
    candidate: dict[str, Any] = {
        "path": str(path),
        "display_path": display_path(repo_root, path),
        "branch": branch,
        "head": head,
        "dirty": None,
        "merged": None,
        "classification": "refused",
        "eligible": False,
        "blockers": [],
        "required_action": "inspect manually",
    }

    if not branch_ref.startswith("refs/heads/") or not branch:
        candidate["blockers"].append("worktree is detached or not on a local branch")
    elif not branch.startswith(DIRECT_BRANCH_PREFIX):
        candidate["blockers"].append(f"branch is outside {DIRECT_BRANCH_PREFIX} namespace")
    if not path_allowed(repo_root, path):
        candidate["blockers"].append(f"path is not a sibling {repo_root.name}-* worktree")
    if task_root and is_path_under(path, task_root):
        candidate["blockers"].append("path is under configured task worktree_root")
    if branch and protected_branch(branch, target):
        candidate["blockers"].append("branch is protected or matches the target branch")

    if candidate["blockers"]:
        candidate["required_action"] = "use an explicit git command after manual review"
        return candidate

    try:
        validate_branch_name(branch)
        status = git(path, "status", "--porcelain=v1", "--untracked-files=all")
        dirty = bool(status.strip())
        merged = is_ancestor(repo_root, branch, target)
        classification = direct_classification(merged=merged, dirty=dirty)
    except Exception as exc:
        candidate["blockers"].append(f"cannot inspect git state: {exc}")
        candidate["required_action"] = "inspect manually"
        return candidate

    candidate.update(
        {
            "dirty": dirty,
            "merged": merged,
            "classification": classification,
            "eligible": classification == "merged+clean",
            "required_action": required_action(classification),
        }
    )
    if not candidate["eligible"]:
        candidate["blockers"].append(candidate["required_action"])
    return candidate


def refreshed_candidate(
    config: Config,
    repo_root: Path,
    task_root: Path | None,
    target: str,
    previous: dict[str, Any],
) -> dict[str, Any]:
    path = Path(str(previous.get("path") or "")).expanduser().resolve()
    registry = worktree_registry(repo_root)
    for entry in registry:
        if not should_inspect_entry(repo_root, task_root, entry):
            continue
        try:
            entry_path = Path(entry.get("path", "")).expanduser().resolve()
        except OSError:
            continue
        if entry_path == path:
            return classify_direct_worktree_entry(config, repo_root, task_root, target, entry)
    refreshed = dict(previous)
    refreshed["eligible"] = False
    refreshed["blockers"] = [*list(refreshed.get("blockers") or []), "worktree registry entry disappeared before apply"]
    refreshed["required_action"] = "inspect manually"
    return refreshed


def remove_direct_worktree(config: Config, repo_root: Path, target: str, candidate: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(candidate["path"]))
    branch = str(candidate["branch"])
    result = {
        "path": str(path),
        "display_path": candidate.get("display_path"),
        "branch": branch,
        "head": candidate.get("head"),
        "target": target,
        "classification": candidate.get("classification"),
        "status": "pending",
        "worktree_removed": False,
        "branch_deleted": False,
        "blockers": [],
    }

    remove = run_git(repo_root, "worktree", "remove", str(path))
    if remove.returncode != 0:
        result["status"] = "failed"
        result["blockers"].append("worktree removal failed: " + clean_git_result(remove))
        write_direct_worktree_event(config, result)
        return result

    result["worktree_removed"] = True
    branch_state = local_branch_state(repo_root, branch)
    if not branch_state.get("exists"):
        result["branch_deleted"] = True
        result["status"] = "cleaned"
        write_direct_worktree_event(config, result)
        return result

    delete = run_git(repo_root, "branch", "-d", branch)
    if delete.returncode != 0:
        result["status"] = "partial"
        result["blockers"].append("branch deletion failed: " + clean_git_result(delete))
        write_direct_worktree_event(config, result)
        return result

    result["branch_deleted"] = True
    result["status"] = "cleaned"
    write_direct_worktree_event(config, result)
    return result


def apply_result_from_refreshed(candidate: dict[str, Any], *, status: str) -> dict[str, Any]:
    return {
        "path": candidate.get("path"),
        "display_path": candidate.get("display_path"),
        "branch": candidate.get("branch"),
        "head": candidate.get("head"),
        "classification": candidate.get("classification"),
        "status": status,
        "worktree_removed": False,
        "branch_deleted": False,
        "blockers": list(candidate.get("blockers") or ["candidate is no longer eligible"]),
    }


def write_direct_worktree_event(config: Config, result: dict[str, Any]) -> None:
    branch = str(result.get("branch") or "-")
    write_event_nonfatal(
        config,
        "direct_worktree_cleaned",
        source="maintenance direct-worktrees",
        summary=f"direct worktree cleanup {result.get('status')} for {branch}",
        payload={
            "branch": branch,
            "path": result.get("display_path"),
            "head": result.get("head"),
            "classification": result.get("classification"),
            "target": result.get("target"),
            "worktree_removed": result.get("worktree_removed"),
            "branch_deleted": result.get("branch_deleted"),
            "blockers": result.get("blockers") or [],
        },
    )


def direct_classification(*, merged: bool, dirty: bool) -> str:
    if merged and not dirty:
        return "merged+clean"
    if merged and dirty:
        return "merged+dirty"
    if not merged and not dirty:
        return "unmerged+clean"
    return "unmerged+dirty"


def required_action(classification: str) -> str:
    if classification == "merged+clean":
        return "cleanup"
    if classification == "merged+dirty":
        return "inspect/stash/commit/discard local modifications before cleanup"
    if classification == "unmerged+clean":
        return "merge/apply or abandon branch explicitly before cleanup"
    return "commit or discard modifications, then merge/apply or abandon explicitly"


def dry_run_status(eligible: list[dict[str, Any]], blocked: list[dict[str, Any]]) -> str:
    if eligible:
        return "ready"
    if blocked:
        return "blocked"
    return "ready"


def direct_summary(candidates: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "total": len(candidates),
        "eligible": 0,
        "blocked": 0,
        "merged_clean": 0,
        "merged_dirty": 0,
        "unmerged_clean": 0,
        "unmerged_dirty": 0,
        "refused": 0,
    }
    for candidate in candidates:
        if candidate.get("eligible"):
            summary["eligible"] += 1
        else:
            summary["blocked"] += 1
        key = str(candidate.get("classification") or "").replace("+", "_").replace("-", "_")
        if key in summary:
            summary[key] += 1
    return summary


def render_direct_worktrees_report(report: dict[str, Any]) -> str:
    lines = [
        "Direct worktree maintenance",
        f"status: {report.get('status')}",
        f"mode: {report.get('mode')}",
        f"target: {report.get('target') or '-'}",
    ]
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    if summary:
        lines.append(
            "summary: "
            + " ".join(
                f"{key}={summary.get(key)}"
                for key in ("total", "eligible", "blocked", "merged_clean", "merged_dirty", "unmerged_clean", "unmerged_dirty", "refused")
            )
        )
    append_candidate_lines(lines, "eligible", report.get("eligible"))
    append_candidate_lines(lines, "blocked", report.get("blocked"))
    append_result_lines(lines, report.get("results"))
    for warning in report.get("warnings") or []:
        lines.append(f"warning: {warning}")
    for error in report.get("errors") or []:
        lines.append(f"error: {error}")
    return "\n".join(lines) + "\n"


def append_candidate_lines(lines: list[str], title: str, value: object) -> None:
    candidates = value if isinstance(value, list) else []
    if not candidates:
        return
    lines.append(f"{title}:")
    for candidate in candidates:
        branch = candidate.get("branch") or "-"
        path = candidate.get("display_path") or candidate.get("path") or "-"
        classification = candidate.get("classification") or "-"
        action = candidate.get("required_action") or "-"
        lines.append(f"  - {branch} path={path} classification={classification} action={action}")


def append_result_lines(lines: list[str], value: object) -> None:
    results = value if isinstance(value, list) else []
    if not results:
        return
    lines.append("results:")
    for result in results:
        branch = result.get("branch") or "-"
        path = result.get("display_path") or result.get("path") or "-"
        status = result.get("status") or "-"
        removed = str(bool(result.get("worktree_removed"))).lower()
        deleted = str(bool(result.get("branch_deleted"))).lower()
        lines.append(f"  - {branch} path={path} status={status} worktree_removed={removed} branch_deleted={deleted}")
        for blocker in result.get("blockers") or []:
            lines.append(f"    blocker: {blocker}")


def path_allowed(repo_root: Path, path: Path) -> bool:
    parent = repo_root.parent.resolve()
    return path.parent.resolve() == parent and path.name.startswith(f"{repo_root.name}-")


def protected_branch(branch: str, target: str) -> bool:
    return branch in PROTECTED_BRANCHES or branch.startswith("release/") or branch.startswith("origin/") or branch == target


def is_path_under(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.expanduser().resolve()
        resolved_root = root.expanduser().resolve()
    except OSError:
        return False
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def display_path(repo_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo_root.parent))
    except ValueError:
        return path.name

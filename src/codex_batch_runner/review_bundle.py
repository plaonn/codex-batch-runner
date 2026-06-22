from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .queue import dependency_blockers, dependency_status, task_labels, task_project_id, task_project_root
from .summary import review_status
from .transcript import sanitize
from .worktree import task_worktree_report

MAX_PROMPT_EXCERPT_CHARS = 2000
COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)


def build_review_bundle(
    task: dict,
    by_id: dict[str, dict] | None = None,
    *,
    require_accepted_review: bool = False,
) -> dict[str, Any]:
    by_id = by_id or {}
    deps_ready, blocked_by = dependency_status(
        task,
        by_id,
        require_accepted_review=require_accepted_review,
    )
    last_result = sanitize_value(task.get("last_result")) if isinstance(task.get("last_result"), dict) else None
    repositories = inspect_repositories(task)
    task_repo = repositories.get("task")
    git_diff = build_git_diff(task, task_repo)
    changed_files = changed_file_summary(task, task_repo)

    return {
        "task": task_metadata(task),
        "prompt_excerpt": prompt_excerpt(task.get("prompt")),
        "status": task.get("status"),
        "review_status": review_status(task),
        "resolution": resolution_summary(task),
        "dependencies": dependency_summary(
            task,
            by_id,
            deps_ready,
            blocked_by,
            require_accepted_review=require_accepted_review,
        ),
        "blockers": blocked_by,
        "last_result": last_result,
        "last_run": sanitize_value(task.get("last_run")) if isinstance(task.get("last_run"), dict) else None,
        "task_worktree": sanitize_value(task_worktree_report(task)),
        "review_follow_up": sanitize_value(task.get("review_follow_up")) if isinstance(task.get("review_follow_up"), dict) else None,
        "changed_files": changed_files,
        "verification": result_list(last_result, "verification"),
        "last_error": sanitize(task.get("last_error")) if task.get("last_error") else None,
        "relevant_log_paths": sanitize_value(task.get("log_paths") or []),
        "task_git_status_snapshot": sanitize_value(task.get("git_status")) if isinstance(task.get("git_status"), dict) else None,
        "current_git_repository": public_repo(task_repo),
        "current_task_repository": public_repo(task_repo),
        "current_main_repository": public_repo(repositories.get("main")),
        "current_task_worktree_repository": public_repo(repositories.get("task_worktree")),
        "git_status": sanitize_value(task.get("git_status")) if isinstance(task.get("git_status"), dict) else None,
        "git_repository": public_repo(task_repo),
        "commit_information": commit_information(task, task_repo),
        "git_diff": git_diff,
        "safety_policy": safety_policy(),
        "transcript_contents_included": False,
    }


def task_metadata(task: dict) -> dict[str, Any]:
    fields = (
        "id",
        "cwd",
        "project_root",
        "project_id",
        "category",
        "created_by",
        "attempts",
        "max_attempts",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "reviewed_at",
        "resolved_at",
        "root_task_id",
        "parent_task_id",
        "review_cycle",
        "chain_status",
    )
    metadata = {key: sanitize_value(task.get(key)) for key in fields if task.get(key) is not None}
    labels = task_labels(task)
    if labels:
        metadata["labels"] = sanitize_value(labels)
    metadata["project_id"] = sanitize(task_project_id(task))
    metadata["project_root"] = sanitize(task_project_root(task))
    return metadata


def resolution_summary(task: dict) -> dict[str, Any] | None:
    if not task.get("resolution"):
        return None
    return {
        "resolution": sanitize(task.get("resolution")),
        "resolved_at": sanitize(task.get("resolved_at")),
        "reason": sanitize(task.get("resolution_reason")),
    }


def dependency_summary(
    task: dict,
    by_id: dict[str, dict],
    deps_ready: bool,
    blocked_by: list[str],
    *,
    require_accepted_review: bool = False,
) -> dict[str, Any]:
    blockers = dependency_blockers(task, by_id, require_accepted_review=require_accepted_review)
    blockers_by_id = {blocker["id"]: blocker for blocker in blockers}
    dependencies = []
    for dep_id in task.get("depends_on") or []:
        dep = by_id.get(dep_id)
        blocker = blockers_by_id.get(str(dep_id))
        dependencies.append(
            {
                "id": sanitize(dep_id),
                "status": sanitize(dep.get("status")) if dep else "missing",
                "review_status": review_status(dep) if dep else "",
                "ready": blocker is None,
                "blocker_reason": blocker["reason"] if blocker else "",
            }
        )
    return {
        "ready": deps_ready,
        "blocked_by": sanitize_value(blocked_by),
        "blockers": sanitize_value(blockers),
        "requires_accepted_review": require_accepted_review,
        "items": dependencies,
    }


def prompt_excerpt(prompt: object) -> str:
    text = sanitize(prompt)
    if len(text) <= MAX_PROMPT_EXCERPT_CHARS:
        return text
    return text[:MAX_PROMPT_EXCERPT_CHARS].rstrip() + "..."


def result_list(last_result: object, key: str) -> list[Any]:
    if not isinstance(last_result, dict):
        return []
    value = last_result.get(key)
    return value if isinstance(value, list) else []


def changed_file_summary(task: dict, repo: dict[str, Any] | None) -> dict[str, Any]:
    last_result = task.get("last_result") if isinstance(task.get("last_result"), dict) else {}
    reported = last_result.get("changed_files") if isinstance(last_result, dict) else []
    summary: dict[str, Any] = {
        "reported": sanitize_value(reported) if isinstance(reported, list) else [],
        "git_name_status": [],
        "warnings": [],
    }
    if not repo or not repo.get("_root_path"):
        summary["warnings"].append("git repository unavailable")
        return summary
    name_status = run_git(Path(repo["_root_path"]), ["status", "--porcelain=v1", "--untracked-files=all"])
    if name_status.returncode == 0:
        summary["git_name_status"] = sanitize_value([line for line in name_status.stdout.splitlines() if line.strip()])
    else:
        summary["warnings"].append("cannot read git status: " + sanitize(clean_git_error(name_status)))
    return summary


def inspect_repositories(task: dict) -> dict[str, dict[str, Any] | None]:
    task_repo = inspect_repo_at(task_execution_cwd(task), scope=task_execution_scope(task))
    main_repo = inspect_repo_at(main_repository_cwd(task), scope="main_repository")
    return {
        "task": task_repo,
        "main": main_repo,
        "task_worktree": task_repo if task.get("execution_mode") == "git_worktree" else None,
    }


def task_execution_cwd(task: dict) -> str | None:
    if task.get("execution_mode") == "git_worktree" and task.get("execution_worktree_path"):
        return str(task.get("execution_worktree_path"))
    return str(task.get("cwd") or task.get("project_root") or "") or None


def task_execution_scope(task: dict) -> str:
    return "task_worktree" if task.get("execution_mode") == "git_worktree" else "main_worktree"


def main_repository_cwd(task: dict) -> str | None:
    return str(task.get("execution_repo_root") or task.get("project_root") or task.get("cwd") or "") or None


def inspect_repo(task: dict) -> dict[str, Any] | None:
    return inspect_repo_at(task_execution_cwd(task), scope=task_execution_scope(task))


def inspect_repo_at(cwd: str | None, *, scope: str) -> dict[str, Any] | None:
    if not shutil.which("git"):
        return {"available": False, "reason": "git executable not found", "inspection_scope": scope}
    if not cwd:
        return {"available": False, "reason": "repository cwd unavailable", "inspection_scope": scope}
    workdir = Path(str(cwd)).expanduser()
    root = run_git(workdir, ["rev-parse", "--show-toplevel"])
    if root.returncode != 0 or not root.stdout.strip():
        return {"available": False, "reason": "repository cwd is not inside a git repository", "inspection_scope": scope}
    repo_root = Path(root.stdout.strip()).expanduser().resolve()
    status = run_git(repo_root, ["status", "--porcelain=v1", "--untracked-files=all"])
    branch = run_git(repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    head = run_git(repo_root, ["rev-parse", "--short", "HEAD"])
    name_status = [line for line in status.stdout.splitlines() if line.strip()] if status.returncode == 0 else []
    repo: dict[str, Any] = {
        "available": True,
        "inspection_scope": scope,
        "root": sanitize(repo_root),
        "_root_path": str(repo_root),
        "branch": sanitize(branch.stdout.strip()) if branch.returncode == 0 and branch.stdout.strip() else "HEAD",
        "head": sanitize(head.stdout.strip()) if head.returncode == 0 and head.stdout.strip() else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
        "name_status": sanitize_value(name_status),
        "status_error": sanitize(clean_git_error(status)) if status.returncode != 0 else None,
        "upstream": None,
        "comparison_ref": None,
        "ahead": None,
        "behind": None,
        "has_unpushed": None,
        "unpushed_commits": [],
        "warnings": [],
    }
    add_current_push_status(repo_root, repo)
    return repo


def add_current_push_status(repo_root: Path, repo: dict[str, Any]) -> None:
    comparison_ref = git_comparison_ref(repo_root, repo)
    if not comparison_ref:
        repo["warnings"].append("git comparison ref unavailable")
        return
    counts = run_git(repo_root, ["rev-list", "--left-right", "--count", f"{comparison_ref}...HEAD"])
    if counts.returncode != 0:
        repo["warnings"].append(f"cannot read ahead/behind against {comparison_ref}: {clean_git_error(counts)}")
        return
    parts = counts.stdout.strip().split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        repo["warnings"].append(f"cannot parse ahead/behind output for {comparison_ref}")
        return
    repo["behind"] = int(parts[0])
    repo["ahead"] = int(parts[1])
    repo["has_unpushed"] = repo["ahead"] > 0
    if repo["ahead"]:
        log = run_git(repo_root, ["log", "--format=%h %s", "--max-count=20", f"{comparison_ref}..HEAD"])
        if log.returncode == 0:
            repo["unpushed_commits"] = sanitize_value([line for line in log.stdout.splitlines() if line.strip()])
        else:
            repo["warnings"].append(f"cannot list unpushed commits: {clean_git_error(log)}")


def git_comparison_ref(repo_root: Path, repo: dict[str, Any]) -> str | None:
    upstream = run_git(repo_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if upstream.returncode == 0 and upstream.stdout.strip():
        value = upstream.stdout.strip()
        repo["upstream"] = sanitize(value)
        repo["comparison_ref"] = sanitize(value)
        return value

    branch = repo.get("branch")
    if isinstance(branch, str) and branch and branch != "HEAD":
        origin_branch = f"origin/{branch}"
        if git_ref_exists(repo_root, origin_branch):
            repo["comparison_ref"] = sanitize(origin_branch)
            return origin_branch
    if git_ref_exists(repo_root, "origin/main"):
        repo["comparison_ref"] = "origin/main"
        return "origin/main"
    return None


def git_ref_exists(repo_root: Path, ref: str) -> bool:
    result = run_git(repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    return result.returncode == 0 and bool(result.stdout.strip())


def public_repo(repo: dict[str, Any] | None) -> dict[str, Any] | None:
    if repo is None:
        return None
    return {key: value for key, value in repo.items() if key != "_root_path"}


def commit_information(task: dict, repo: dict[str, Any] | None) -> dict[str, Any]:
    info: dict[str, Any] = {"reported": [], "inferred_commits": [], "status": "unavailable", "warnings": []}
    last_result = task.get("last_result") if isinstance(task.get("last_result"), dict) else {}
    reported = last_result.get("commits") if isinstance(last_result, dict) else []
    if isinstance(reported, list):
        info["reported"] = sanitize_value(reported)
    elif reported:
        info["reported"] = sanitize_value([reported])
    if not repo or not repo.get("_root_path"):
        info["warnings"].append("git repository unavailable")
        return info
    candidates = infer_commit_hashes(task)
    existing = []
    repo_root = Path(repo["_root_path"])
    for candidate in candidates:
        rev = run_git(repo_root, ["rev-parse", "--verify", f"{candidate}^{{commit}}"])
        if rev.returncode == 0 and rev.stdout.strip():
            existing.append(rev.stdout.strip())
    unique = sorted(set(existing))
    info["inferred_commits"] = sanitize_value(unique)
    if len(unique) == 1:
        show = run_git(repo_root, ["show", "--no-patch", "--format=%H %s", unique[0]])
        info["status"] = "inferred"
        if show.returncode == 0:
            info["commit"] = sanitize(show.stdout.strip())
    elif len(unique) > 1:
        info["status"] = "ambiguous"
        info["warnings"].append("multiple commit hashes were inferable; diff omitted")
    else:
        info["status"] = "not_inferred"
        if info["reported"]:
            info["warnings"].append("reported commit metadata did not include an inferable commit hash")
    return info


def build_git_diff(task: dict, repo: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": "none", "stat": "", "diff": "", "warnings": []}
    if not repo or not repo.get("_root_path"):
        result["warnings"].append("git repository unavailable")
        return result
    repo_root = Path(repo["_root_path"])
    commits = commit_information(task, repo).get("inferred_commits") or []
    if len(commits) == 1:
        commit = commits[0]
        stat = run_git(repo_root, ["show", "--stat", "--format=", commit])
        diff = run_git(repo_root, ["show", "--format=", "--find-renames", commit])
        result["kind"] = "commit"
        result["ref"] = sanitize(commit)
        result["stat"] = sanitize_multiline(stat.stdout) if stat.returncode == 0 else ""
        result["diff"] = sanitize_multiline(diff.stdout) if diff.returncode == 0 else ""
        if stat.returncode != 0 or diff.returncode != 0:
            result["warnings"].append("cannot read commit diff/stat")
        return result
    if len(commits) > 1:
        result["kind"] = "ambiguous"
        result["warnings"].append("multiple commit hashes were inferable; diff omitted")
        return result
    if repo.get("dirty"):
        stat = run_git(repo_root, ["diff", "--stat"])
        diff = run_git(repo_root, ["diff", "--find-renames"])
        result["kind"] = "working_tree"
        result["stat"] = sanitize_multiline(stat.stdout) if stat.returncode == 0 else ""
        result["diff"] = sanitize_multiline(diff.stdout) if diff.returncode == 0 else ""
        if stat.returncode != 0 or diff.returncode != 0:
            result["warnings"].append("cannot read working tree diff/stat")
    else:
        result["warnings"].append("no inferred commit and working tree is clean")
    return result


def infer_commit_hashes(task: dict) -> list[str]:
    values: list[object] = []
    last_result = task.get("last_result") if isinstance(task.get("last_result"), dict) else {}
    if isinstance(last_result, dict):
        values.append(last_result.get("commits"))
    git_status = task.get("git_status") if isinstance(task.get("git_status"), dict) else {}
    if isinstance(git_status, dict):
        values.append(git_status.get("unpushed_commits"))
    text = json.dumps(values, ensure_ascii=False)
    return COMMIT_RE.findall(text)


def safety_policy() -> list[str]:
    return [
        "Do not expose local runtime state, real logs, prompts, session ids, thread ids, credentials, Telegram tokens, chat ids, or private queue contents.",
        "This bundle omits raw JSONL transcript contents by default.",
        "Obvious secrets and local user paths are redacted on output.",
        "The command is report-only and must not accept, reject, enqueue, or invoke Codex.",
    ]


def sanitize_value(value: object) -> Any:
    if isinstance(value, dict):
        return {str(key): sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize(value)


def sanitize_multiline(value: object) -> str:
    return "\n".join(sanitize(line) for line in str(value or "").splitlines()).rstrip()


def run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args=["git", *args], returncode=1, stdout="", stderr=str(exc))


def clean_git_error(result: subprocess.CompletedProcess[str]) -> str:
    return " ".join((result.stderr or result.stdout or "").split())


def render_review_bundle(bundle: dict[str, Any]) -> str:
    lines: list[str] = [f"# review bundle {bundle['task'].get('id')}"]
    append_scalar(lines, "status", bundle.get("status"))
    append_scalar(lines, "review_status", bundle.get("review_status"))
    append_object_section(lines, "task_metadata", bundle.get("task"))
    append_text_section(lines, "prompt_excerpt", bundle.get("prompt_excerpt"))
    append_object_section(lines, "dependencies", bundle.get("dependencies"))
    append_object_section(lines, "resolution", bundle.get("resolution"))
    append_object_section(lines, "last_result", bundle.get("last_result"))
    append_object_section(lines, "last_run", bundle.get("last_run"))
    append_object_section(lines, "task_worktree", bundle.get("task_worktree"))
    append_object_section(lines, "review_follow_up", bundle.get("review_follow_up"))
    append_object_section(lines, "changed_files", bundle.get("changed_files"))
    append_list_section(lines, "verification", bundle.get("verification"))
    append_text_section(lines, "last_error", bundle.get("last_error"))
    append_list_section(lines, "relevant_log_paths", bundle.get("relevant_log_paths"))
    append_object_section(lines, "task_git_status_snapshot", bundle.get("task_git_status_snapshot"))
    append_object_section(lines, "current_git_repository", bundle.get("current_git_repository"))
    append_object_section(lines, "current_task_repository", bundle.get("current_task_repository"))
    append_object_section(lines, "current_main_repository", bundle.get("current_main_repository"))
    append_object_section(lines, "current_task_worktree_repository", bundle.get("current_task_worktree_repository"))
    append_object_section(lines, "commit_information", bundle.get("commit_information"))
    append_object_section(lines, "git_diff", bundle.get("git_diff"))
    append_list_section(lines, "safety_policy", bundle.get("safety_policy"))
    append_scalar(lines, "transcript_contents_included", bundle.get("transcript_contents_included"))
    return "\n".join(lines).rstrip() + "\n"


def append_scalar(lines: list[str], key: str, value: object) -> None:
    lines.append(f"{key}: {value if value not in (None, '') else '-'}")


def append_text_section(lines: list[str], title: str, value: object) -> None:
    if value in (None, "", []):
        return
    lines.extend([f"## {title}", str(value)])


def append_list_section(lines: list[str], title: str, value: object) -> None:
    if not value:
        return
    lines.append(f"## {title}")
    if isinstance(value, list):
        lines.extend(f"- {item}" for item in value)
    else:
        lines.append(str(value))


def append_object_section(lines: list[str], title: str, value: object) -> None:
    if not value:
        return
    lines.append(f"## {title}")
    lines.append(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))

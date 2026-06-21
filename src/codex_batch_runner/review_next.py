from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

from .config import Config
from .queue import (
    dependency_status,
    list_tasks,
    task_labels,
    task_project_id,
    task_project_root,
)
from .review_bundle import build_review_bundle
from .summary import review_status
from .transcript import sanitize

REVIEW_NEEDED_STATUSES = {"unreviewed", "rejected", "needs_followup"}


def build_review_next_report(config: Config, filters: Namespace | None = None) -> dict[str, Any]:
    tasks = list_tasks(config)
    by_id = {task.get("id"): task for task in tasks}
    candidates = [task for task in tasks if is_review_needed(task)]
    candidates = apply_filters(candidates, filters)
    candidates.sort(key=review_sort_key)
    if not candidates:
        return {
            "mode": "dry-run",
            "selected": False,
            "task_id": None,
            "candidate_count": 0,
            "message": "no completed task needs review",
            "filters": filter_summary(filters),
            "gates": [],
            "dependencies": None,
            "bundle": None,
            "mutated": False,
        }

    task = candidates[0]
    bundle = build_review_bundle(
        task,
        by_id=by_id,
        require_accepted_review=config.dependency_requires_accepted_review,
    )
    gates = mechanical_gates(task, bundle)
    deps_ready, blocked_by = dependency_status(
        task,
        by_id,
        require_accepted_review=config.dependency_requires_accepted_review,
    )
    return {
        "mode": "dry-run",
        "selected": True,
        "task_id": task.get("id"),
        "candidate_count": len(candidates),
        "message": "selected oldest completed task needing review",
        "filters": filter_summary(filters),
        "gates_ok": all(gate["ok"] for gate in gates),
        "gates": gates,
        "dependencies": {
            "ready": deps_ready,
            "blocked_by": blocked_by,
            "blockers": bundle.get("dependencies", {}).get("blockers", []),
            "requires_accepted_review": config.dependency_requires_accepted_review,
            "items": bundle.get("dependencies", {}).get("items", []),
        },
        "review_status": review_status(task),
        "bundle": concise_bundle(bundle),
        "mutated": False,
    }


def is_review_needed(task: dict) -> bool:
    return task.get("status") == "completed" and review_status(task) in REVIEW_NEEDED_STATUSES


def apply_filters(tasks: list[dict], filters: Namespace | None) -> list[dict]:
    if filters is None:
        return tasks
    filtered = tasks
    if getattr(filters, "project_id", None):
        filtered = [task for task in filtered if task_project_id(task) == filters.project_id]
    if getattr(filters, "project_root", None):
        project_root = normalized_path(filters.project_root)
        filtered = [task for task in filtered if task_project_root(task) == project_root]
    if getattr(filters, "category", None):
        filtered = [task for task in filtered if task.get("category") == filters.category]
    if getattr(filters, "label", None):
        filtered = [task for task in filtered if filters.label in task_labels(task)]
    return filtered


def normalized_path(value: object) -> str:
    return str(Path(str(value)).expanduser().resolve()) if value else ""


def filter_summary(filters: Namespace | None) -> dict[str, Any]:
    if filters is None:
        return {}
    summary = {}
    for attr, key in (
        ("project_id", "project"),
        ("project_root", "project_root"),
        ("category", "category"),
        ("label", "label"),
    ):
        value = getattr(filters, attr, None)
        if value:
            summary[key] = str(value)
    return summary


def review_sort_key(task: dict) -> tuple[str, str, str]:
    timestamp = task.get("completed_at") or task.get("updated_at") or task.get("created_at") or ""
    return (str(timestamp), str(task.get("created_at") or ""), str(task.get("id") or ""))


def mechanical_gates(task: dict, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    last_result = task.get("last_result") if isinstance(task.get("last_result"), dict) else {}
    git_status = task.get("git_status") if isinstance(task.get("git_status"), dict) else {}
    repo = bundle.get("git_repository") if isinstance(bundle.get("git_repository"), dict) else {}
    changed_files = last_result.get("changed_files") if isinstance(last_result, dict) else None
    verification = last_result.get("verification") if isinstance(last_result, dict) else None
    deps = bundle.get("dependencies") if isinstance(bundle.get("dependencies"), dict) else {}

    return [
        gate(
            "final_status_completed",
            task.get("status") == "completed",
            f"task.status={task.get('status')}",
        ),
        gate(
            "final_result_completed",
            isinstance(last_result, dict) and last_result.get("status") == "completed",
            f"last_result.status={last_result.get('status') if isinstance(last_result, dict) else None}",
        ),
        gate(
            "no_last_error",
            not task.get("last_error"),
            "last_error is empty" if not task.get("last_error") else "last_error is present",
        ),
        gate(
            "verification_present",
            isinstance(verification, list) and bool(verification),
            f"verification_count={len(verification) if isinstance(verification, list) else 'missing'}",
        ),
        gate(
            "changed_files_reported",
            isinstance(changed_files, list),
            f"changed_files_count={len(changed_files) if isinstance(changed_files, list) else 'missing'}",
        ),
        gate("dependencies_ready", bool(deps.get("ready")), dependency_detail(deps)),
        gate("git_clean", repo.get("dirty") is False, git_clean_detail(repo)),
        gate("no_unpushed_commits", no_unpushed(git_status), unpushed_detail(git_status)),
    ]


def gate(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": sanitize(detail)}


def dependency_detail(deps: dict[str, Any]) -> str:
    blockers = deps.get("blockers") or []
    if blockers:
        return "blocked_by=" + ",".join(
            f"{item.get('id')}:{item.get('reason')}" if isinstance(item, dict) else str(item) for item in blockers
        )
    blocked_by = deps.get("blocked_by") or []
    if blocked_by:
        return "blocked_by=" + ",".join(str(item) for item in blocked_by)
    return "ready=true"


def git_clean_detail(repo: dict[str, Any]) -> str:
    if not repo:
        return "git repository unavailable"
    if repo.get("available") is False:
        return str(repo.get("reason") or "git repository unavailable")
    return f"dirty={repo.get('dirty')}"


def no_unpushed(git_status: dict[str, Any]) -> bool:
    if not git_status:
        return False
    if git_status.get("has_unpushed") is not None:
        return git_status.get("has_unpushed") is False
    ahead = git_status.get("ahead")
    return isinstance(ahead, int) and ahead == 0


def unpushed_detail(git_status: dict[str, Any]) -> str:
    if not git_status:
        return "git_status unavailable"
    return f"has_unpushed={git_status.get('has_unpushed')} ahead={git_status.get('ahead')}"


def concise_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    git_diff = bundle.get("git_diff") if isinstance(bundle.get("git_diff"), dict) else {}
    return {
        "task": bundle.get("task"),
        "prompt_excerpt": bundle.get("prompt_excerpt"),
        "status": bundle.get("status"),
        "review_status": bundle.get("review_status"),
        "dependencies": bundle.get("dependencies"),
        "last_result": bundle.get("last_result"),
        "last_run": bundle.get("last_run"),
        "changed_files": bundle.get("changed_files"),
        "verification": bundle.get("verification"),
        "last_error": bundle.get("last_error"),
        "git_status": bundle.get("git_status"),
        "git_repository": bundle.get("git_repository"),
        "commit_information": bundle.get("commit_information"),
        "git_diff_summary": {
            "kind": git_diff.get("kind"),
            "ref": git_diff.get("ref"),
            "stat": git_diff.get("stat"),
            "warnings": git_diff.get("warnings", []),
        },
        "safety_policy": bundle.get("safety_policy"),
        "transcript_contents_included": bundle.get("transcript_contents_included"),
    }


def render_review_next_report(report: dict[str, Any]) -> str:
    lines = [
        f"mode: {report['mode']}",
        f"selected: {str(report['selected']).lower()}",
        f"candidate_count: {report['candidate_count']}",
    ]
    if not report["selected"]:
        lines.append(f"message: {report['message']}")
        return "\n".join(lines) + "\n"

    bundle = report.get("bundle") or {}
    task = bundle.get("task") or {}
    lines.extend(
        [
            f"task_id: {report['task_id']}",
            f"review_status: {report['review_status']}",
            f"project_id: {task.get('project_id') or '-'}",
            f"project_root: {task.get('project_root') or '-'}",
            f"gates_ok: {str(report['gates_ok']).lower()}",
            "gates:",
        ]
    )
    for item in report.get("gates", []):
        status = "pass" if item["ok"] else "fail"
        lines.append(f"- {item['name']}: {status} ({item['detail']})")
    deps = report.get("dependencies") or {}
    blockers = deps.get("blockers") or []
    if blockers:
        blocked_by = ",".join(
            f"{item.get('id')}:{item.get('reason')}" if isinstance(item, dict) else str(item) for item in blockers
        )
    else:
        blocked_by = ",".join(deps.get("blocked_by") or []) or "-"
    lines.append(
        "dependencies: "
        f"ready={str(bool(deps.get('ready'))).lower()} "
        f"requires_accepted_review={str(bool(deps.get('requires_accepted_review'))).lower()} "
        f"blocked_by={blocked_by}"
    )
    append_result_summary(lines, bundle)
    lines.append("dry_run: no task state changed; reviewer Codex not invoked; auto-apply not implemented")
    return "\n".join(lines).rstrip() + "\n"


def append_result_summary(lines: list[str], bundle: dict[str, Any]) -> None:
    last_result = bundle.get("last_result") if isinstance(bundle.get("last_result"), dict) else {}
    if last_result.get("summary"):
        lines.append("summary: " + one_line(last_result.get("summary"), 240))
    changed_files = bundle.get("changed_files") if isinstance(bundle.get("changed_files"), dict) else {}
    reported = changed_files.get("reported") if isinstance(changed_files, dict) else []
    lines.append(f"changed_files_reported: {len(reported) if isinstance(reported, list) else 0}")
    verification = bundle.get("verification") if isinstance(bundle.get("verification"), list) else []
    lines.append(f"verification_count: {len(verification)}")
    git_repo = bundle.get("git_repository") if isinstance(bundle.get("git_repository"), dict) else {}
    if git_repo:
        lines.append(f"git: branch={git_repo.get('branch') or '-'} dirty={git_repo.get('dirty')}")
    commit_info = bundle.get("commit_information") if isinstance(bundle.get("commit_information"), dict) else {}
    if commit_info:
        reported_count = len(commit_info.get("reported") or [])
        lines.append(f"commit_information: status={commit_info.get('status')} reported={reported_count}")
    diff = bundle.get("git_diff_summary") if isinstance(bundle.get("git_diff_summary"), dict) else {}
    if diff:
        lines.append(f"git_diff: kind={diff.get('kind')} ref={diff.get('ref') or '-'}")


def one_line(value: object, limit: int) -> str:
    text = " ".join(sanitize(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."

from __future__ import annotations

from .queue import dependency_status, is_in_cooldown, task_labels, task_project_id, task_project_root
from .transcript import sanitize


def render_task_summary(task: dict, by_id: dict[str, dict] | None = None) -> str:
    lines = [
        f"# task {task.get('id')}",
        f"status: {task.get('status')}",
        f"review_status: {review_status(task)}",
        f"attempts: {task.get('attempts', 0)}",
        f"project_id: {task_project_id(task)}",
        f"project_root: {task_project_root(task)}",
    ]
    if task.get("category"):
        lines.append(f"category: {task.get('category')}")
    labels = task_labels(task)
    if labels:
        lines.append(f"labels: {', '.join(labels)}")
    if task.get("created_by"):
        lines.append(f"created_by: {task.get('created_by')}")
    if task.get("cwd"):
        lines.append(f"cwd: {task.get('cwd')}")
    if task.get("cooldown_until") or is_in_cooldown(task):
        lines.append(f"cooldown_until: {task.get('cooldown_until')}")

    if by_id is not None:
        deps_ready, blocked_by = dependency_status(task, by_id)
        deps = task.get("depends_on") or []
        if deps:
            lines.append(f"dependencies: {', '.join(str(dep) for dep in deps)}")
            lines.append(f"dependencies_ready: {str(deps_ready).lower()}")
        if blocked_by:
            lines.append(f"blocked_by: {', '.join(blocked_by)}")

    append_multiline_section(lines, "last_result", render_last_result(task.get("last_result")))
    append_section(lines, "last_error", task.get("last_error"))
    append_section(lines, "next_prompt", task.get("next_prompt"))

    log_paths = task.get("log_paths") or []
    if log_paths:
        lines.append("## logs")
        lines.extend(str(path) for path in log_paths)
    return "\n".join(lines).rstrip() + "\n"


def render_last_result(last_result: object) -> str:
    if not isinstance(last_result, dict):
        return ""
    lines = []
    if last_result.get("status"):
        lines.append(f"status: {last_result.get('status')}")
    if last_result.get("summary"):
        lines.extend(["summary:", sanitize(last_result.get("summary"))])
    changed_files = last_result.get("changed_files") or []
    if isinstance(changed_files, list) and changed_files:
        lines.append("changed_files:")
        lines.extend(f"- {sanitize(path)}" for path in changed_files)
    verification = last_result.get("verification") or []
    if isinstance(verification, list) and verification:
        lines.append("verification:")
        lines.extend(f"- {sanitize(item)}" for item in verification)
    if last_result.get("next_prompt"):
        lines.extend(["next_prompt:", sanitize(last_result.get("next_prompt"))])
    return "\n".join(lines)


def append_section(lines: list[str], title: str, value: object) -> None:
    if not value:
        return
    lines.append(f"## {title}")
    lines.append(sanitize(value))


def append_multiline_section(lines: list[str], title: str, value: str) -> None:
    if not value:
        return
    lines.append(f"## {title}")
    lines.append(value)


def review_status(task: dict) -> str:
    if task.get("status") == "completed":
        return str(task.get("review_status") or "unreviewed")
    return str(task.get("review_status") or "")

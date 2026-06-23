from __future__ import annotations

from .queue import (
    dependency_blockers,
    dependency_status,
    is_in_cooldown,
    task_capacity_pool,
    task_labels,
    task_priority,
    task_project_id,
    task_project_root,
    task_title,
)
from .transcript import sanitize
from .worktree import task_worktree_metadata


def render_task_summary(
    task: dict,
    by_id: dict[str, dict] | None = None,
    *,
    require_accepted_review: bool = False,
) -> str:
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
    append_execution_profile_summary(lines, task)
    append_scheduling_summary(lines, task)
    append_routing_summary(lines, task)
    append_counters(lines, task)
    append_chain_summary(lines, task)
    if task.get("cooldown_until") or is_in_cooldown(task):
        lines.append(f"cooldown_until: {task.get('cooldown_until')}")
    if task.get("resolution"):
        lines.append(f"resolution: {task.get('resolution')}")
        if task.get("resolved_at"):
            lines.append(f"resolved_at: {task.get('resolved_at')}")
        if task.get("resolution_reason"):
            lines.append(f"resolution_reason: {sanitize(task.get('resolution_reason'))}")
    if task.get("resume_unavailable"):
        lines.append("resume_unavailable: true")
        if task.get("resume_unavailable_at"):
            lines.append(f"resume_unavailable_at: {task.get('resume_unavailable_at')}")
        if task.get("resume_unavailable_attempts"):
            lines.append(f"resume_unavailable_attempts: {task.get('resume_unavailable_attempts')}")
    if task.get("startup_stalled_at"):
        lines.append(f"startup_stalled_at: {task.get('startup_stalled_at')}")
        lines.append(f"startup_stall_count: {task.get('startup_stall_count', 0)}")

    if by_id is not None:
        deps_ready, blocked_by = dependency_status(
            task,
            by_id,
            require_accepted_review=require_accepted_review,
        )
        blockers = dependency_blockers(task, by_id, require_accepted_review=require_accepted_review)
        deps = task.get("depends_on") or []
        if deps:
            lines.append(f"dependencies: {', '.join(str(dep) for dep in deps)}")
            lines.append(f"dependencies_ready: {str(deps_ready).lower()}")
        if blocked_by:
            lines.append(f"blocked_by: {', '.join(blocked_by)}")
            lines.append("dependency_blockers:")
            lines.extend(f"- {dependency_blocker_label(blocker, by_id)}: {blocker['reason']}" for blocker in blockers)

    append_multiline_section(lines, "last_result", render_last_result(task.get("last_result")))
    append_multiline_section(lines, "reviewer_codex", render_reviewer_codex(task.get("reviewer_codex")))
    append_multiline_section(lines, "worktree", render_worktree_metadata(task))
    append_multiline_section(lines, "git_status", render_git_status(task.get("git_status")))
    append_multiline_section(lines, "last_run", render_last_run(task.get("last_run")))
    append_multiline_section(lines, "last_progress", render_last_progress(task.get("last_progress")))
    append_section(lines, "last_error", task.get("last_error"))
    append_section(lines, "next_prompt", task.get("next_prompt"))

    log_paths = task.get("log_paths") or []
    if log_paths:
        lines.append("## logs")
        lines.extend(str(path) for path in log_paths)
    return "\n".join(lines).rstrip() + "\n"


def append_scheduling_summary(lines: list[str], task: dict) -> None:
    fields = [
        f"capacity_pool={task_capacity_pool(task)}",
        f"task_priority={task_priority(task)}",
    ]
    lines.append("scheduling: " + ", ".join(fields))


def render_last_result(last_result: object) -> str:
    if not isinstance(last_result, dict):
        return ""
    lines = []
    if last_result.get("status"):
        lines.append(f"status: {last_result.get('status')}")
    if last_result.get("summary"):
        lines.extend(["summary:", sanitize(last_result.get("summary"))])
    commits = last_result.get("commits") or []
    if isinstance(commits, list) and commits:
        lines.append("commits:")
        lines.extend(f"- {sanitize(item)}" for item in commits)
    elif commits:
        lines.extend(["commits:", sanitize(commits)])
    if last_result.get("push_status"):
        lines.extend(["push_status:", render_structured_value(last_result.get("push_status"))])
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


def append_chain_summary(lines: list[str], task: dict) -> None:
    fields = []
    for key in (
        "subtask_type",
        "subtask_for",
        "blocks_root_completion",
        "root_task_id",
        "parent_task_id",
        "blocking_subtask_ids",
        "review_cycle",
        "review_attempts",
        "fix_attempts",
        "chain_status",
        "last_review_decision",
        "auto_fix_allowed",
        "last_auto_fix_task_id",
        "last_conflict_fix_task_id",
    ):
        value = task.get(key)
        if meaningful_chain_value(key, value):
            fields.append(f"{key}={sanitize(value)}")
    if fields:
        lines.append("chain: " + ", ".join(fields))


def append_execution_profile_summary(lines: list[str], task: dict) -> None:
    fields = []
    if task.get("execution_backend") and task.get("execution_backend") != "codex":
        fields.append(f"execution_backend={sanitize(task.get('execution_backend'))}")
    shell_command = task.get("shell_command")
    if isinstance(shell_command, list) and shell_command:
        fields.append("shell_command=" + sanitize(shell_command))
    if task.get("shell_timeout_seconds"):
        fields.append(f"shell_timeout_seconds={sanitize(task.get('shell_timeout_seconds'))}")
    for key in ("execution_profile", "model", "codex_profile", "token_budget_hint"):
        value = task.get(key)
        if value not in (None, "", [], {}):
            fields.append(f"{key}={sanitize(value)}")
    overrides = task.get("codex_config_overrides")
    if isinstance(overrides, dict) and overrides:
        fields.append("codex_config_overrides=" + ",".join(sorted(str(key) for key in overrides)))
    if fields:
        lines.append("execution: " + ", ".join(fields))


def append_routing_summary(lines: list[str], task: dict) -> None:
    fields = []
    for key in ("routing_experiment", "routing_reason"):
        value = task.get(key)
        if value not in (None, "", [], {}):
            fields.append(f"{key}={sanitize(value)}")
    risk_factors = task.get("routing_risk_factors")
    if isinstance(risk_factors, list) and risk_factors:
        fields.append("routing_risk_factors=" + ",".join(sanitize(item) for item in risk_factors))
    if fields:
        lines.append("routing: " + ", ".join(fields))


def meaningful_chain_value(key: str, value: object) -> bool:
    if value in (None, "", [], {}):
        return False
    if key in {"review_cycle", "review_attempts", "fix_attempts"} and value == 0:
        return False
    if key in {"auto_fix_allowed", "blocks_root_completion"} and value is False:
        return False
    return True


def render_reviewer_codex(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    lines = []
    for key in (
        "decision",
        "confidence",
        "reason",
        "auto_fix_allowed",
        "auto_fix_risk",
        "suggested_fix_prompt",
    ):
        if value.get(key) not in (None, "", [], {}):
            lines.append(f"{key}: {display_value(sanitize(value.get(key)))}")
    findings = value.get("findings") if isinstance(value.get("findings"), list) else []
    if findings:
        lines.append("findings:")
        for finding in findings[:10]:
            if isinstance(finding, dict):
                lines.append("- " + sanitize(finding.get("summary") or finding))
            else:
                lines.append("- " + sanitize(finding))
    fingerprints = value.get("finding_fingerprints") if isinstance(value.get("finding_fingerprints"), list) else []
    if fingerprints:
        lines.append("finding_fingerprints:")
        lines.extend(f"- {sanitize(item)}" for item in fingerprints[:20])
    return "\n".join(lines)


def render_git_status(git_status: object) -> str:
    if not isinstance(git_status, dict):
        return ""
    lines = []
    for key in (
        "branch",
        "upstream",
        "comparison_ref",
        "ahead",
        "behind",
        "has_unpushed",
        "dirty",
        "inspected_at",
    ):
        if key in git_status:
            lines.append(f"{key}: {display_value(git_status.get(key))}")
    unpushed_commits = git_status.get("unpushed_commits") or []
    if isinstance(unpushed_commits, list) and unpushed_commits:
        lines.append("unpushed_commits:")
        lines.extend(f"- {sanitize(item)}" for item in unpushed_commits)
    warnings = git_status.get("warnings") or []
    if isinstance(warnings, list) and warnings:
        lines.append("warnings:")
        lines.extend(f"- {sanitize(item)}" for item in warnings)
    return "\n".join(lines)


def render_worktree_metadata(task: dict) -> str:
    metadata = task_worktree_metadata(task)
    if metadata == {"execution_mode": "main_worktree"}:
        return ""
    lines = []
    for key in (
        "execution_mode",
        "branch",
        "base_ref",
        "base_head",
        "apply_status",
        "applied_at",
        "applied_head",
        "apply_target",
        "apply_via_task_id",
        "rebase_status",
        "rebased_at",
        "rebased_onto",
        "rebased_head",
        "rebase_blocker",
        "rebase_blocked_at",
        "conflict_fix_status",
        "conflict_fix_task_id",
        "conflict_fix_queued_at",
        "worktree_status",
        "worktree_path",
        "worktree_root",
        "repo_root",
        "cleaned_at",
        "cleanup_kind",
        "cleanup_reason",
        "cleanup_branch_retained",
        "cleanup_result_applied",
    ):
        if key in metadata:
            lines.append(f"{key}: {display_value(metadata.get(key))}")
    return "\n".join(lines)


def dependency_blocker_label(blocker: dict[str, str], by_id: dict[str, dict]) -> str:
    dep_id = blocker["id"]
    dep = by_id.get(dep_id)
    if not dep:
        return dep_id
    label = task_title(dep)
    return f"{label} ({dep_id})" if label != dep_id else dep_id


def render_structured_value(value: object) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}: {sanitize(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(f"- {sanitize(item)}" for item in value)
    return sanitize(value)


def append_counters(lines: list[str], task: dict) -> None:
    counters = []
    for key in ("run_count", "resume_count", "rate_limit_count", "failure_count"):
        if task.get(key):
            counters.append(f"{key}={task.get(key)}")
    if counters:
        lines.append("counters: " + ", ".join(counters))


def render_last_run(last_run: object) -> str:
    if not isinstance(last_run, dict):
        return ""
    lines = []
    for key in (
        "execution_backend",
        "command_kind",
        "command",
        "returncode",
        "started_at",
        "finished_at",
        "duration_seconds",
        "timeout_seconds",
        "timed_out",
        "stdout_bytes",
        "stderr_bytes",
        "resume_id_used",
        "log_path",
        "execution_profile",
        "execution_profile_source",
        "execution_profile_reason",
        "model",
        "codex_profile",
        "config_override_keys",
        "token_budget_hint",
        "watchdog_reason",
    ):
        if key in last_run:
            lines.append(f"{key}: {display_value(last_run.get(key))}")
    return "\n".join(lines)


def render_last_progress(last_progress: object) -> str:
    if not isinstance(last_progress, dict):
        return ""
    lines = []
    for key in (
        "first_jsonl_event_at",
        "last_jsonl_event_at",
        "first_meaningful_event_at",
        "last_meaningful_event_at",
        "last_meaningful_event_type",
        "stdout_empty",
        "only_startup_events",
        "jsonl_event_count",
        "startup_event_count",
        "meaningful_event_count",
        "idle_warning",
        "terminated_by_watchdog",
        "watchdog_reason",
        "termination_signal",
    ):
        if key in last_progress:
            lines.append(f"{key}: {display_value(last_progress.get(key))}")
    return "\n".join(lines)


def display_value(value: object) -> object:
    return "-" if value is None else value


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

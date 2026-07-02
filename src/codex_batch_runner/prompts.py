from __future__ import annotations

import json


FINAL_SCHEMA = {
    "task_id": "string",
    "status": "completed | needs_resume | blocked_user | failed",
    "summary": "string",
    "next_prompt": "string",
    "changed_files": ["string"],
    "verification": ["string"],
    "commits": ["string, optional"],
    "push_status": "string or object, optional",
}


def build_prompt(
    task: dict,
    resume_unavailable: bool = False,
    execution_cwd: str | None = None,
    execution_backend: str | None = None,
) -> str:
    prompt = task.get("next_prompt") if task.get("status") == "needs_resume" and task.get("next_prompt") else task.get("prompt", "")
    is_git_worktree = task.get("execution_mode") == "git_worktree"
    worktree_path = execution_cwd or task.get("execution_worktree_path")
    cwd = worktree_path if is_git_worktree and worktree_path else execution_cwd or task.get("cwd")
    parts = [
        "You are executing exactly one queued Codex batch task.",
        "",
        "Hard rules:",
        "- Process only the task_id below.",
        "- Do not create or enqueue any new tasks.",
        "- Do not perform unrelated refactors.",
        "- End with a single JSON object and no surrounding prose.",
        "- If the work is incomplete but can continue automatically, return status needs_resume and a concrete next_prompt.",
        "- If user input is required, return status blocked_user.",
        "",
        f"task_id: {task.get('id')}",
        f"cwd: {cwd}",
    ]
    if is_git_worktree:
        original_cwd = task.get("cwd")
        worktree_usage = "Use cwd/execution_worktree_path as the current process cwd for edits and tests."
        if execution_backend == "external-json-command":
            worktree_usage = (
                "Use cwd/execution_worktree_path as the current process cwd for edits and tests. "
                "Do not create local commits or push; report safe relative changed_files so cbr can create the review commit."
            )
        elif execution_backend != "external-json-command":
            worktree_usage = "Use cwd/execution_worktree_path as the current process cwd for edits, tests, and commits."
        parts.extend(
            [
                "execution_mode: git_worktree",
                f"execution_worktree_path: {worktree_path or cwd}",
                f"original_task_cwd: {original_cwd}",
                worktree_usage,
                "Do not use original_task_cwd for repository commands during this task.",
            ]
        )
    if resume_unavailable:
        parts.append("resume_unavailable: true")
    parts.extend(
        [
            "",
            "Final response schema:",
            json.dumps(FINAL_SCHEMA, ensure_ascii=False, indent=2),
            "",
            "Task prompt:",
            prompt,
        ]
    )
    return "\n".join(parts)

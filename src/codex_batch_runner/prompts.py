from __future__ import annotations

import json


FINAL_SCHEMA = {
    "task_id": "string",
    "status": "completed | needs_resume | blocked_user | failed",
    "summary": "string",
    "next_prompt": "string",
    "changed_files": ["string"],
    "verification": ["string"],
}


def build_prompt(task: dict, resume_unavailable: bool = False) -> str:
    prompt = task.get("next_prompt") if task.get("status") == "needs_resume" and task.get("next_prompt") else task.get("prompt", "")
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
        f"cwd: {task.get('cwd')}",
    ]
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

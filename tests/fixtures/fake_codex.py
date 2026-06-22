from __future__ import annotations

import json
import sys
import time


def emit(value: dict) -> None:
    print(json.dumps(value), flush=True)


def main() -> int:
    mode = sys.argv[1]
    prompt = sys.argv[-1]
    task_id = "unknown"
    for line in prompt.splitlines():
        if line.startswith("task_id:"):
            task_id = line.split(":", 1)[1].strip()
            break
        if line.startswith("reviewer_task_id:"):
            task_id = line.split(":", 1)[1].strip()
            break

    if mode == "hang_empty":
        time.sleep(60)
        return 0

    emit({"type": "session.started", "session_id": "synthetic-session", "thread_id": "synthetic-thread"})

    if mode == "hang_startup":
        emit({"type": "thread.started", "thread_id": "synthetic-thread"})
        emit({"type": "turn.started"})
        time.sleep(60)
        return 0

    if mode == "meaningful_then_hang":
        emit({"type": "turn.started"})
        emit({"type": "agent.message", "message": "working"})
        time.sleep(1)
        emit(
            {
                "type": "turn.completed",
                "response": {
                    "task_id": task_id,
                    "status": "completed",
                    "summary": "done after idle",
                    "next_prompt": "",
                    "changed_files": [],
                    "verification": ["synthetic verification"],
                },
            }
        )
        return 0

    if mode == "meaningful_idle_forever":
        emit({"type": "turn.started"})
        emit({"type": "agent.message", "message": "working"})
        time.sleep(2)
        return 0

    if mode == "item_progress_then_exit":
        emit({"type": "turn.started"})
        emit({"type": "item.started", "item": {"type": "command_execution"}})
        emit({"type": "item.completed", "item": {"type": "command_execution"}})
        emit({"type": "item.started", "item": {"type": "file_change"}})
        emit({"type": "item.completed", "item": {"type": "file_change"}})
        time.sleep(2)
        return 0

    if mode == "success":
        emit(
            {
                "type": "turn.completed",
                "response": {
                    "task_id": task_id,
                    "status": "completed",
                    "summary": "done",
                    "next_prompt": "",
                    "changed_files": ["README.md"],
                    "verification": ["synthetic verification"],
                },
            }
        )
        return 0

    if mode == "reviewer_pass":
        emit(
            {
                "type": "turn.completed",
                "response": {
                    "task_id": task_id,
                    "decision": "pass",
                    "confidence": "high",
                    "reason": "bundle evidence supports accepting the task",
                    "findings": [
                        {
                            "severity": "info",
                            "summary": "verification reported",
                            "evidence": "review bundle includes synthetic verification",
                        }
                    ],
                    "required_human_checks": [],
                    "suggested_fix_prompt": "",
                    "reviewer_limits": {
                        "calls_used_this_run": 1,
                        "fix_loops_used_for_task": 0,
                        "cooldown_recommended_seconds": 0,
                    },
                },
            }
        )
        return 0

    if mode == "reviewer_needs_fix":
        emit(
            {
                "type": "turn.completed",
                "response": {
                    "task_id": task_id,
                    "decision": "needs_fix",
                    "confidence": "medium",
                    "reason": "documentation update is incomplete",
                    "findings": [
                        {
                            "severity": "warning",
                            "summary": "missing docs",
                            "evidence": "README change is not reflected in docs/spec.md",
                        }
                    ],
                    "required_human_checks": [],
                    "auto_fix_allowed": True,
                    "auto_fix_risk": "low",
                    "suggested_fix_prompt": "Update docs/spec.md to match the README behavior.",
                    "finding_fingerprints": ["missing-docs:docs-spec"],
                    "reviewer_limits": {
                        "calls_used_this_run": 1,
                        "fix_loops_used_for_task": 0,
                        "cooldown_recommended_seconds": 0,
                    },
                },
            }
        )
        return 0

    if mode == "reviewer_needs_fix_legacy":
        emit(
            {
                "type": "turn.completed",
                "response": {
                    "task_id": task_id,
                    "decision": "needs_fix",
                    "confidence": "medium",
                    "reason": "legacy reviewer result without optional fix-loop fields",
                    "findings": [
                        {
                            "severity": "warning",
                            "summary": "legacy finding",
                            "evidence": "legacy evidence",
                        }
                    ],
                    "required_human_checks": [],
                },
            }
        )
        return 0

    if mode == "reviewer_needs_human":
        emit(
            {
                "type": "turn.completed",
                "response": {
                    "task_id": task_id,
                    "decision": "needs_human",
                    "confidence": "low",
                    "reason": "synthetic reviewer needs human input",
                    "findings": [
                        {
                            "severity": "warning",
                            "summary": "manual check needed",
                            "evidence": "synthetic evidence",
                        }
                    ],
                    "required_human_checks": ["inspect manually"],
                    "auto_fix_allowed": False,
                    "auto_fix_risk": "high",
                    "suggested_fix_prompt": "",
                    "finding_fingerprints": ["manual-check"],
                    "reviewer_limits": {
                        "calls_used_this_run": 1,
                        "fix_loops_used_for_task": 0,
                        "cooldown_recommended_seconds": 0,
                    },
                },
            }
        )
        return 0

    if mode == "reviewer_invalid":
        emit({"type": "turn.completed", "response": {"task_id": task_id, "decision": "pass"}})
        return 0

    if mode == "needs_resume":
        emit(
            {
                "type": "turn.completed",
                "message": json.dumps(
                    {
                        "task_id": task_id,
                        "status": "needs_resume",
                        "summary": "partial",
                        "next_prompt": "continue synthetic task",
                        "changed_files": [],
                        "verification": [],
                    }
                ),
            }
        )
        return 0

    if mode == "rate_limit":
        emit({"type": "error", "message": "usage limit reached, try again later"})
        return 1

    if mode == "malformed":
        emit({"type": "turn.completed", "message": "not json"})
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

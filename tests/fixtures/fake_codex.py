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

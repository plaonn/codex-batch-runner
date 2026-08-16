from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def task_id_from_prompt(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.startswith("task_id:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def main() -> int:
    prompt = sys.argv[-1]
    state_dir = Path(os.environ["CHAMBER_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    hook = {
        "source": {
            "kind": os.environ.get("CHAMBER_CORRELATION_KIND"),
            "id": os.environ.get("CHAMBER_CORRELATION_ID"),
            "revision": os.environ.get("CHAMBER_CORRELATION_REVISION"),
        },
        "execution_id": os.environ.get("CHAMBER_EXECUTION_ID"),
    }
    (state_dir / "native-hook.json").write_text(
        json.dumps(hook, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "type": "turn.completed",
                "response": {
                    "task_id": task_id_from_prompt(prompt),
                    "status": "completed",
                    "summary": "live ordinary-flow evidence run",
                    "next_prompt": "",
                    "changed_files": [],
                    "verification": ["live adapter binding readback"],
                },
            }
        ),
        flush=True,
    )
    return 0


raise SystemExit(main())

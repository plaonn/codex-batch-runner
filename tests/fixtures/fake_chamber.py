from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def value(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def records_path() -> Path:
    state_dir = Path(os.environ["CHAMBER_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "settlements.jsonl"


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] != "settle":
        return 2
    execution_id = value(args, "--execution-id")
    status = value(args, "--status")
    source = {
        "kind": value(args, "--source-kind"),
        "id": value(args, "--source-id"),
        "revision": value(args, "--source-revision"),
    }
    path = records_path()
    existing = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ] if path.exists() else []
    idempotent = any(
        item.get("execution_id") == execution_id and item.get("status") == status
        for item in existing
    )
    record = {
        "execution_id": execution_id,
        "source": source,
        "status": status,
        "idempotent": idempotent,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "status": "idempotent" if idempotent else "recorded",
                "recorded": True,
                "idempotent": idempotent,
                "execution_id": execution_id,
                "automatic": True,
                "provenance": {"source": source, "status": status},
            }
        ),
        flush=True,
    )
    return 0


raise SystemExit(main())

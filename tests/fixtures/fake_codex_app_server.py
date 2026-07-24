from __future__ import annotations

import json
import sys
import time


def main() -> int:
    mode = sys.argv[1]
    initialize = json.loads(sys.stdin.readline())
    initialized = json.loads(sys.stdin.readline())
    request = json.loads(sys.stdin.readline())
    if (
        initialize.get("id") != 1
        or initialize.get("method") != "initialize"
        or initialized != {"method": "initialized"}
        or request != {"id": 2, "method": "account/rateLimits/read", "params": None}
    ):
        return 2
    print(json.dumps({"id": initialize["id"], "result": {"serverInfo": {"name": "fake", "version": "1"}}}), flush=True)
    if mode == "timeout":
        time.sleep(10)
        return 0
    if mode == "oversized":
        print("x" * 1000, flush=True)
        return 0
    if mode == "method-not-found":
        print(json.dumps({"id": request["id"], "error": {"code": -32601}}), flush=True)
        return 0
    if mode == "malformed":
        print("not-json", flush=True)
        return 0
    print(json.dumps({"id": request["id"], "result": {"rateLimits": {"limitId": "codex", "planType": "prolite", "primary": {"windowDurationMins": 300, "usedPercent": 25, "resetsAt": "2030-01-02T05:00:00Z"}, "secondary": {"windowDurationMins": 10080, "usedPercent": 80, "resetsAt": "2030-01-09T04:00:00Z"}}}}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import Config
from .fs import ensure_dir, read_json, write_json_atomic
from .timeutil import iso_now

MAX_EXCERPT_CHARS = 500
SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)(bearer)\s+([a-z0-9._~+/=-]+)"),
)


def rate_limit_dir(config: Config) -> Path:
    return config.log_dir.parent / "rate-limits"


def capture_rate_limit_evidence(
    config: Config,
    task: dict,
    result: Any,
    cooldown_until: str,
) -> Path:
    detected_at = iso_now()
    evidence = {
        "task_id": task.get("id"),
        "detected_at": detected_at,
        "attempt": int(task.get("attempts", 0)),
        "matched_markers": sorted(set(getattr(result, "rate_limit_markers", []) or [])),
        "cooldown_until": cooldown_until,
        "stderr_excerpt": sanitize_excerpt(getattr(result, "stderr", "") or ""),
        "error_excerpt": sanitize_excerpt(error_text(getattr(result, "events", []) or [])),
        "original_log_path": str(getattr(result, "log_path", "")),
    }
    path = rate_limit_dir(config) / evidence_filename(task.get("id"), detected_at, evidence["attempt"])
    write_json_atomic(path, evidence)
    return path


def list_rate_limit_evidence(config: Config) -> list[dict]:
    ensure_dir(rate_limit_dir(config))
    events = []
    for path in sorted(rate_limit_dir(config).glob("*.json")):
        event = read_json(path)
        if isinstance(event, dict):
            event["_path"] = str(path)
            events.append(event)
    events.sort(key=lambda item: (item.get("detected_at") or "", item.get("task_id") or "", item.get("attempt") or 0))
    return events


def evidence_filename(task_id: object, detected_at: str, attempt: int) -> str:
    safe_task_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(task_id or "task")).strip("-") or "task"
    safe_stamp = re.sub(r"[^0-9a-zA-Z_.-]+", "-", detected_at).strip("-")
    return f"{safe_stamp}-{safe_task_id}-attempt-{attempt}.json"


def error_text(events: list[dict]) -> str:
    snippets = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").lower()
        if "error" in event_type or event_type == "turn.failed":
            for key in ("message", "error", "reason"):
                value = event.get(key)
                if value:
                    snippets.append(str(value))
    return "\n".join(snippets)


def sanitize_excerpt(text: str) -> str:
    excerpt = " ".join(str(text).split())
    for pattern in SECRET_PATTERNS:
        excerpt = pattern.sub(lambda match: f"{match.group(1)} [REDACTED]", excerpt)
    if len(excerpt) > MAX_EXCERPT_CHARS:
        return excerpt[:MAX_EXCERPT_CHARS].rstrip() + "..."
    return excerpt

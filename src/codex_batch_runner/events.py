from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from .config import Config
from .fs import ensure_dir
from .timeutil import iso_now

SCHEMA_VERSION = 1
DEFAULT_EVENT_LIMIT = 20
MAX_STRING_CHARS = 500
MAX_LIST_ITEMS = 20

REDACTED = "[REDACTED]"
REDACTED_KEYS = {
    "authorization",
    "chat_id",
    "conversation_id",
    "credential",
    "credentials",
    "env",
    "environment",
    "events",
    "jsonl",
    "next_prompt",
    "password",
    "prompt",
    "raw_log",
    "secret",
    "session_id",
    "stderr",
    "stdout",
    "telegram_token",
    "thread_id",
    "token",
    "transcript",
}
SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)(bearer)\s+([a-z0-9._~+/=-]+)"),
)


def write_event(
    config: Config,
    event_type: str,
    *,
    task: dict[str, Any] | None = None,
    actor: str = "cbr",
    source: str | None = None,
    summary: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    occurred_at = iso_now()
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": occurred_at,
        "task_id": task.get("id") if task else None,
        "project_id": task_project_id(task) if task else None,
        "project_root": task_project_root(task) if task else None,
        "actor": sanitize_scalar(actor),
        "source": sanitize_scalar(source or "codex-batch-runner"),
        "summary": sanitize_scalar(summary or default_summary(event_type, task)),
        "payload": sanitize_payload(payload or {}),
    }
    append_event(config.event_dir, event)
    return event


def write_event_nonfatal(config: Config, event_type: str, **kwargs: Any) -> dict[str, Any] | None:
    try:
        return write_event(config, event_type, **kwargs)
    except OSError as exc:
        print(f"warning: event log write failed for {event_type}: {exc}", file=sys.stderr)
        return None


def append_event(event_dir: Path, event: dict[str, Any]) -> Path:
    ensure_dir(event_dir)
    date = str(event.get("occurred_at") or iso_now())[:10]
    path = event_dir / f"{date}.jsonl"
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(line)
        file.write("\n")
        file.flush()
    return path


def emit_task_event(
    config: Config,
    event_type: str,
    task: dict[str, Any],
    *,
    actor: str = "cbr",
    source: str | None = None,
    summary: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    write_event_nonfatal(
        config,
        event_type,
        task=task,
        actor=actor,
        source=source,
        summary=summary,
        payload=payload,
    )


def list_events(config: Config, *, task_id: str | None = None, limit: int = DEFAULT_EVENT_LIMIT) -> list[dict[str, Any]]:
    ensure_dir(config.event_dir)
    found: list[dict[str, Any]] = []
    for path in sorted(config.event_dir.glob("*.jsonl"), reverse=True):
        for event in read_jsonl(path):
            if task_id and event.get("task_id") != task_id:
                continue
            event["_path"] = str(path)
            found.append(event)
    found.sort(key=lambda item: (item.get("occurred_at") or "", item.get("event_id") or ""), reverse=True)
    if limit > 0:
        return found[:limit]
    return found


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    events.append(parsed)
    except OSError:
        return []
    return events


def render_events_human(events: list[dict[str, Any]]) -> str:
    lines = ["OCCURRED_AT\tTYPE\tTASK\tSUMMARY"]
    for event in events:
        lines.append(
            "\t".join(
                [
                    one_line(event.get("occurred_at") or "-"),
                    one_line(event.get("event_type") or "-"),
                    one_line(event.get("task_id") or "-"),
                    one_line(event.get("summary") or "-"),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def transition_payload(task: dict[str, Any], **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": task.get("status"),
        "review_status": task.get("review_status"),
        "resolution": task.get("resolution"),
        "attempts": task.get("attempts", 0),
        "run_count": task.get("run_count"),
    }
    payload.update(extra)
    return {key: value for key, value in payload.items() if value is not None}


def result_summary_payload(task: dict[str, Any]) -> dict[str, Any]:
    last_result = task.get("last_result")
    if not isinstance(last_result, dict):
        return {}
    payload: dict[str, Any] = {}
    if last_result.get("status"):
        payload["result_status"] = last_result.get("status")
    if last_result.get("summary"):
        payload["summary_excerpt"] = last_result.get("summary")
    if isinstance(last_result.get("changed_files"), list):
        payload["changed_files_count"] = len(last_result.get("changed_files") or [])
    if isinstance(last_result.get("verification"), list):
        payload["verification_count"] = len(last_result.get("verification") or [])
    if isinstance(last_result.get("commits"), list):
        payload["commits_count"] = len(last_result.get("commits") or [])
    return payload


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if should_redact_key(text_key):
                sanitized[text_key] = REDACTED
            else:
                sanitized[text_key] = sanitize_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value[:MAX_LIST_ITEMS]]
    if isinstance(value, tuple):
        return [sanitize_payload(item) for item in value[:MAX_LIST_ITEMS]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return sanitize_scalar(value)
    return sanitize_scalar(str(value))


def sanitize_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = " ".join(value.split())
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)} {REDACTED}", text)
    if len(text) > MAX_STRING_CHARS:
        return text[:MAX_STRING_CHARS].rstrip() + "..."
    return text


def should_redact_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in REDACTED_KEYS:
        return True
    return any(part in normalized for part in ("password", "secret", "token", "credential", "session_id", "thread_id"))


def task_project_id(task: dict[str, Any] | None) -> str | None:
    if not task:
        return None
    value = task.get("project_id")
    return str(value) if value else None


def task_project_root(task: dict[str, Any] | None) -> str | None:
    if not task:
        return None
    value = task.get("project_root") or task.get("cwd")
    return str(value) if value else None


def default_summary(event_type: str, task: dict[str, Any] | None) -> str:
    if task and task.get("id"):
        return f"{event_type} for {task.get('id')}"
    return event_type


def one_line(value: Any) -> str:
    return " ".join(str(value).split())

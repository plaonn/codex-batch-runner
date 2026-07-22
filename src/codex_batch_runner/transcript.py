from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .codex import parse_json_line

MAX_TEXT_CHARS = 2000
SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)(bearer)\s+([a-z0-9._~+/=-]+)"),
    re.compile(r"/Users/[^/\s]+"),
)


def render_task_transcript(
    task: dict,
    raw: bool = False,
    *,
    task_log_dir: Path | None = None,
) -> str:
    chunks: list[str] = render_task_summary(task)
    paths = [Path(str(path_text)) for path_text in task.get("log_paths") or []]
    if task_log_dir and task_log_dir.exists():
        for path in sorted(task_log_dir.glob("attempt-*.jsonl")):
            if path not in paths:
                paths.append(path)
    for index, path in enumerate(paths, start=1):
        chunks.append(f"## attempt {index}: {path.name}")
        if not path.exists():
            chunks.append("(log file missing)")
            continue
        if raw:
            chunks.append(path.read_text(encoding="utf-8"))
            continue
        chunks.extend(render_events(read_jsonl(path)))
    session_log = find_codex_session_log(task)
    if session_log:
        chunks.append(f"## codex session: {session_log.name}")
        if raw:
            chunks.append(session_log.read_text(encoding="utf-8"))
        else:
            chunks.extend(render_events(read_jsonl(session_log)))
    return "\n".join(chunks).rstrip() + "\n"


def render_task_summary(task: dict) -> list[str]:
    review_status = task.get("review_status")
    if not review_status and task.get("status") == "completed":
        review_status = "unreviewed"
    lines = [
        f"# task {task.get('id')}",
        f"status: {task.get('status')}",
        f"review_status: {review_status}",
        f"attempts: {task.get('attempts', 0)}",
    ]
    if task.get("last_error"):
        lines.extend(["## last_error", sanitize(task.get("last_error"))])
    if task.get("last_result"):
        lines.extend(["## last_result", compact_json(task.get("last_result"))])
    return lines


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    events = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            parsed = parse_json_line(line)
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def find_codex_session_log(task: dict) -> Path | None:
    session_id = task.get("session_id") or task.get("thread_id")
    if not session_id:
        return None
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return None
    matches = sorted(sessions_dir.glob(f"**/rollout-*{session_id}.jsonl"))
    return matches[-1] if matches else None


def render_events(events: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for event in events:
        rendered = render_event(event)
        if rendered:
            lines.extend(rendered)
    return lines or ["(no readable events)"]


def render_event(event: dict[str, Any]) -> list[str]:
    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    payload_type = payload.get("type") if isinstance(payload, dict) else None
    kind = str(payload_type or event_type)

    if kind == "user_message":
        text = payload.get("message") if isinstance(payload, dict) else event.get("message")
        if should_skip_message("user", text):
            return []
        return section("user", text)
    if kind == "agent_message":
        return section("assistant", payload.get("message") if isinstance(payload, dict) else event.get("message"))
    if kind == "message":
        role = value_at(payload or event, ("role",)) or "message"
        text = message_text(payload or event)
        if should_skip_message(str(role), text):
            return []
        return section(str(role), text)
    if kind == "function_call":
        name = value_at(payload or event, ("name",))
        args = value_at(payload or event, ("arguments",))
        return section("tool call", f"{name}\n{args}")
    if kind == "function_call_output":
        output = value_at(payload or event, ("output",))
        return section("tool output", output)
    if kind == "patch_apply_end":
        status = value_at(payload or event, ("status",)) or "patch"
        stdout = value_at(payload or event, ("stdout",))
        stderr = value_at(payload or event, ("stderr",))
        return section("patch", f"{status}\n{stdout}\n{stderr}")
    if kind in {"turn.completed", "turn.failed", "error"} or "error" in kind:
        return section(kind, compact_json(event))
    if has_final_response(event):
        return section("final", compact_json(event))
    return []


def has_final_response(value: Any) -> bool:
    if isinstance(value, dict):
        if {"task_id", "status", "summary", "changed_files", "verification"}.issubset(value.keys()):
            return True
        return any(has_final_response(child) for child in value.values())
    if isinstance(value, list):
        return any(has_final_response(child) for child in value)
    return False


def section(title: str, text: object) -> list[str]:
    body = sanitize(text)
    if not body:
        return []
    return [f"### {title}", body]


def sanitize(value: object) -> str:
    text = " ".join(str(value or "").split())
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(redact_match, text)
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS].rstrip() + "..."
    return text


def redact_match(match: re.Match[str]) -> str:
    if match.re.pattern.startswith("/Users/"):
        return "/Users/[USER]"
    return f"{match.group(1)} [REDACTED]"


def should_skip_message(role: str, text: object) -> bool:
    normalized = str(text or "").lstrip()
    if role in {"system", "developer"}:
        return True
    return normalized.startswith("# AGENTS.md instructions for ")


def value_at(value: object, keys: tuple[str, ...]) -> object:
    if not isinstance(value, dict):
        return None
    for key in keys:
        if key in value:
            return value[key]
    return None


def message_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    content = value.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return str(content or value.get("message") or "")


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .codex import find_response_object, parse_json_line
from .config import Config
from .fs import read_json
from .limits import matched_rate_limit_markers
from .transcript import message_text, sanitize, should_skip_message, value_at

RUNNING_STATUSES = {"running"}
DEFAULT_INITIAL_LINES = 80
DEFAULT_POLL_INTERVAL_SECONDS = 0.5


@dataclass
class FollowOptions:
    task_id: str
    initial_lines: int = DEFAULT_INITIAL_LINES
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_polls: int | None = None


def follow_task(config: Config, options: FollowOptions, stream: TextIO) -> None:
    offsets: dict[Path, int] = {}
    seen_paths: set[Path] = set()
    poll_count = 0

    while True:
        task = load_task_snapshot(config, options.task_id)
        paths = attempt_log_paths(config, task, options.task_id)
        printed = False
        for path in paths:
            if path not in seen_paths:
                print(f"==> {path.name} <==", file=stream)
                seen_paths.add(path)
            start_at_tail = path not in offsets
            printed = read_new_events(
                path,
                stream,
                offsets,
                initial_lines=options.initial_lines if start_at_tail else 0,
            ) or printed
        if should_stop_following(task, printed):
            break
        poll_count += 1
        if options.max_polls is not None and poll_count >= options.max_polls:
            break
        time.sleep(options.poll_interval_seconds)


def load_task_snapshot(config: Config, task_id: str) -> dict[str, Any]:
    task_path = config.queue_dir / f"{task_id}.json"
    data = read_json(task_path, None)
    if not isinstance(data, dict):
        raise FileNotFoundError(f"task not found: {task_id}")
    return data


def attempt_log_paths(config: Config, task: dict[str, Any], task_id: str) -> list[Path]:
    paths: list[Path] = []
    for path_text in task.get("log_paths") or []:
        path = Path(str(path_text)).expanduser()
        if path not in paths:
            paths.append(path)
    task_log_dir = config.log_dir / task_id
    if task_log_dir.exists():
        for path in sorted(task_log_dir.glob("attempt-*.jsonl")):
            if path not in paths:
                paths.append(path)
    return paths


def read_new_events(
    path: Path,
    stream: TextIO,
    offsets: dict[Path, int],
    *,
    initial_lines: int,
) -> bool:
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    previous = offsets.get(path)
    if previous is None:
        lines = text.splitlines()
        selected = lines[-initial_lines:] if initial_lines > 0 else []
        offsets[path] = len(text)
        return print_lines(selected, stream)
    if len(text) < previous:
        previous = 0
    if len(text) == previous:
        offsets[path] = len(text)
        return False
    chunk = text[previous:]
    offsets[path] = len(text)
    return print_lines(chunk.splitlines(), stream)


def print_lines(lines: list[str], stream: TextIO) -> bool:
    printed = False
    for line in lines:
        event = parse_json_line(line)
        if not isinstance(event, dict):
            continue
        for rendered in render_follow_event(event):
            print(rendered, file=stream)
            printed = True
    return printed


def render_follow_event(event: dict[str, Any]) -> list[str]:
    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    payload_type = payload.get("type") if isinstance(payload, dict) else None
    kind = str(payload_type or event_type)
    source = payload if isinstance(payload, dict) else event

    final_response = find_response_object(event)
    if final_response:
        return ["final: " + compact_json(sanitize_value(final_response))]

    markers = matched_rate_limit_markers(compact_json(event))
    if markers:
        return ["rate-limit: markers=" + ",".join(sorted(set(markers)))]

    if kind == "agent_message":
        return render_message("assistant", source.get("message"))
    if kind == "message":
        role = str(value_at(source, ("role",)) or "message")
        text = message_text(source)
        if role != "assistant" or should_skip_message(role, text):
            return []
        return render_message("assistant", text)
    if kind == "function_call":
        name = value_at(source, ("name",)) or "tool"
        arguments = value_at(source, ("arguments",))
        return ["command start: " + sanitize(f"{name} {arguments or ''}")]
    if kind == "function_call_output":
        output = value_at(source, ("output",))
        exit_code = extract_exit_code(output)
        prefix = f"command finish: exit={exit_code}" if exit_code is not None else "command finish:"
        rendered_output = sanitize(output)
        return [prefix + (f" {rendered_output}" if rendered_output else "")]
    if kind == "patch_apply_end":
        status = value_at(source, ("status",)) or "patch"
        return ["command finish: patch " + sanitize(status)]
    if kind in {"turn.failed", "error"} or "error" in kind:
        return [kind + ": " + compact_json(sanitize_value(error_summary(event)))]
    if kind == "turn.completed":
        return ["turn.completed: " + compact_json(sanitize_value(turn_summary(event)))]
    return []


def render_message(label: str, text: object) -> list[str]:
    body = sanitize(text)
    return [f"{label}: {body}"] if body else []


def extract_exit_code(value: object) -> object:
    parsed = value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = value
    if isinstance(parsed, dict):
        for key in ("exit_code", "exitCode", "returncode", "status"):
            if key in parsed:
                return parsed[key]
    return None


def error_summary(event: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"type": event.get("type")}
    for key in ("message", "error", "code", "status"):
        if key in event:
            summary[key] = event.get(key)
    payload = event.get("payload")
    if isinstance(payload, dict):
        for key in ("type", "message", "error", "code", "status"):
            if key in payload:
                summary[f"payload_{key}"] = payload.get(key)
    return summary


def turn_summary(event: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"type": event.get("type")}
    for key in ("message", "status"):
        if key in event:
            summary[key] = event.get(key)
    return summary


def sanitize_value(value: object) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if should_redact_key(text_key):
                sanitized[text_key] = "[REDACTED]"
            else:
                sanitized[text_key] = sanitize_value(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        return sanitize(value)
    return value


def should_redact_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in {"prompt", "next_prompt", "session_id", "thread_id"} or any(
        part in normalized for part in ("password", "secret", "token", "credential")
    )


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def should_stop_following(task: dict[str, Any], printed: bool) -> bool:
    return task.get("status") not in RUNNING_STATUSES and not printed

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .fs import ensure_dir
from .limits import looks_like_rate_limit


@dataclass
class CodexResult:
    returncode: int
    log_path: Path
    stderr: str
    events: list[dict[str, Any]]
    final_response: dict[str, Any] | None
    session_id: str | None
    thread_id: str | None
    rate_limited: bool


def format_command(template: list[str], task: dict, prompt: str) -> list[str]:
    values = {
        "session_id": task.get("session_id") or "",
        "thread_id": task.get("thread_id") or "",
        "task_id": task.get("id") or "",
    }
    return [part.format(**values) for part in template] + [prompt]


def run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
    log_dir = ensure_dir(config.log_dir / task["id"])
    log_path = log_dir / f"attempt-{attempt}.jsonl"
    use_resume = task.get("status") == "needs_resume" and task.get("session_id")
    command = format_command(config.codex_resume_command if use_resume else config.codex_command, task, prompt)
    stderr_chunks: list[str] = []
    events: list[dict[str, Any]] = []

    process = subprocess.Popen(
        command,
        cwd=task.get("cwd") or None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def read_stderr() -> None:
        assert process.stderr is not None
        for chunk in process.stderr:
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()

    assert process.stdout is not None
    with process.stdout, log_path.open("w", encoding="utf-8") as log_file:
        for line in process.stdout:
            log_file.write(line)
            parsed = parse_json_line(line)
            if isinstance(parsed, dict):
                events.append(parsed)

    returncode = process.wait()
    stderr_thread.join(timeout=5)
    if process.stderr:
        process.stderr.close()
    stderr = "".join(stderr_chunks)
    final_response = extract_final_response(events)
    session_id = first_recursive_value(events, ("session_id", "sessionId", "conversation_id"))
    thread_id = first_recursive_value(events, ("thread_id", "threadId"))
    raw_text = stderr + "\n" + "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    return CodexResult(
        returncode=returncode,
        log_path=log_path,
        stderr=stderr,
        events=events,
        final_response=final_response,
        session_id=str(session_id) if session_id else None,
        thread_id=str(thread_id) if thread_id else None,
        rate_limited=looks_like_rate_limit(raw_text),
    )


def parse_json_line(line: str) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def extract_final_response(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        found = find_response_object(event)
        if found:
            return found
    return None


def find_response_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if {"task_id", "status", "summary", "changed_files", "verification"}.issubset(value.keys()):
            return value
        for child in value.values():
            found = find_response_object(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = find_response_object(child)
            if found:
                return found
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return None
            return find_response_object(parsed)
    return None


def first_recursive_value(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key):
                return value[key]
        for child in value.values():
            found = first_recursive_value(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = first_recursive_value(child, keys)
            if found:
                return found
    return None

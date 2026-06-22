from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .execution_profiles import command_options, insert_command_options, resolve_execution_settings
from .fs import ensure_dir
from .limits import matched_rate_limit_markers
from .timeutil import iso_now


STARTUP_EVENT_TYPES = {"thread.started", "session.started", "turn.started"}
STARTUP_STALL_REASON = "startup_stall"
FIRST_MEANINGFUL_STALL_REASON = "first_meaningful_timeout"
MID_RUN_IDLE_REASON = "mid_run_idle_timeout"
TOTAL_RUNTIME_REASON = "total_runtime_timeout"


@dataclass
class CodexResult:
    returncode: int
    log_path: Path
    command_kind: str
    resume_id_used: str | None
    stderr: str
    events: list[dict[str, Any]]
    final_response: dict[str, Any] | None
    session_id: str | None
    thread_id: str | None
    rate_limited: bool
    rate_limit_markers: list[str]
    progress: dict[str, Any] | None = None
    watchdog_reason: str | None = None


@dataclass
class ProgressTracker:
    start_monotonic: float
    first_jsonl_event_at: str | None = None
    last_jsonl_event_at: str | None = None
    first_jsonl_event_monotonic: float | None = None
    last_jsonl_event_monotonic: float | None = None
    first_meaningful_event_at: str | None = None
    last_meaningful_event_at: str | None = None
    first_meaningful_event_monotonic: float | None = None
    last_meaningful_event_monotonic: float | None = None
    last_meaningful_event_type: str | None = None
    first_turn_started_monotonic: float | None = None
    stdout_line_count: int = 0
    jsonl_event_count: int = 0
    startup_event_count: int = 0
    meaningful_event_count: int = 0
    idle_warning: bool = False
    terminated_by_watchdog: bool = False
    termination_signal: str | None = None
    watchdog_reason: str | None = None

    def record_stdout_line(self) -> None:
        self.stdout_line_count += 1

    def record_event(self, event: dict[str, Any], now_monotonic: float) -> None:
        event_type = event_type_of(event)
        now_iso = iso_now()
        self.jsonl_event_count += 1
        if not self.first_jsonl_event_at:
            self.first_jsonl_event_at = now_iso
            self.first_jsonl_event_monotonic = now_monotonic
        self.last_jsonl_event_at = now_iso
        self.last_jsonl_event_monotonic = now_monotonic
        if event_type in STARTUP_EVENT_TYPES:
            self.startup_event_count += 1
        if event_type == "turn.started" and self.first_turn_started_monotonic is None:
            self.first_turn_started_monotonic = now_monotonic
        if is_meaningful_event(event):
            self.meaningful_event_count += 1
            if not self.first_meaningful_event_at:
                self.first_meaningful_event_at = now_iso
                self.first_meaningful_event_monotonic = now_monotonic
            self.last_meaningful_event_at = now_iso
            self.last_meaningful_event_monotonic = now_monotonic
            self.last_meaningful_event_type = event_type or "final_response"

    def as_dict(self) -> dict[str, Any]:
        return {
            "first_jsonl_event_at": self.first_jsonl_event_at,
            "last_jsonl_event_at": self.last_jsonl_event_at,
            "first_meaningful_event_at": self.first_meaningful_event_at,
            "last_meaningful_event_at": self.last_meaningful_event_at,
            "last_meaningful_event_type": self.last_meaningful_event_type,
            "stdout_empty": self.stdout_line_count == 0,
            "only_startup_events": self.jsonl_event_count > 0 and self.jsonl_event_count == self.startup_event_count,
            "stdout_line_count": self.stdout_line_count,
            "jsonl_event_count": self.jsonl_event_count,
            "startup_event_count": self.startup_event_count,
            "meaningful_event_count": self.meaningful_event_count,
            "idle_warning": self.idle_warning,
            "terminated_by_watchdog": self.terminated_by_watchdog,
            "watchdog_reason": self.watchdog_reason,
            "termination_signal": self.termination_signal,
        }


def format_command(template: list[str], task: dict, prompt: str) -> list[str]:
    resume_id = task.get("session_id") or task.get("thread_id") or ""
    values = {
        "session_id": resume_id,
        "thread_id": task.get("thread_id") or "",
        "task_id": task.get("id") or "",
    }
    return [part.format(**values) for part in template] + [prompt]


def format_command_with_profile(
    template: list[str],
    task: dict,
    prompt: str,
    config: Config,
    *,
    reviewer: bool = False,
) -> list[str]:
    base = format_command(template, task, prompt)
    settings = resolve_execution_settings(config, task, reviewer=reviewer)
    return [*insert_command_options(base[:-1], command_options(settings)), base[-1]]


def run_codex(config: Config, task: dict, prompt: str, attempt: int) -> CodexResult:
    log_dir = ensure_dir(config.log_dir / task["id"])
    log_path = log_dir / f"attempt-{attempt}.jsonl"
    use_resume = should_use_resume(task)
    resume_id_used = (task.get("session_id") or task.get("thread_id")) if use_resume else None
    command_kind = "resume" if use_resume else "exec"
    command = format_command_with_profile(
        config.codex_resume_command if use_resume else config.codex_command,
        task,
        prompt,
        config,
    )
    stderr_chunks: list[str] = []
    events: list[dict[str, Any]] = []

    try:
        process = subprocess.Popen(
            command,
            cwd=task.get("cwd") or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        log_path.write_text("", encoding="utf-8")
        return CodexResult(
            returncode=127,
            log_path=log_path,
            command_kind=command_kind,
            resume_id_used=str(resume_id_used) if resume_id_used else None,
            stderr=str(exc),
            events=[],
            final_response=None,
            session_id=None,
            thread_id=None,
            rate_limited=False,
            rate_limit_markers=[],
            progress=None,
            watchdog_reason=None,
        )

    def read_stderr() -> None:
        assert process.stderr is not None
        for chunk in process.stderr:
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()

    tracker = ProgressTracker(start_monotonic=time.monotonic())
    assert process.stdout is not None
    with process.stdout, log_path.open("w", encoding="utf-8") as log_file:
        read_stdout_with_watchdog(process, log_file, events, tracker, config)

    returncode = process.wait()
    stderr_thread.join(timeout=5)
    if process.stderr:
        process.stderr.close()
    stderr = "".join(stderr_chunks)
    final_response = extract_final_response(events)
    thread_id = first_recursive_value(events, ("thread_id", "threadId"))
    session_id = first_recursive_value(events, ("session_id", "sessionId", "conversation_id")) or thread_id
    raw_text = stderr + "\n" + "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    rate_limit_markers = matched_rate_limit_markers(raw_text)
    return CodexResult(
        returncode=returncode,
        log_path=log_path,
        command_kind=command_kind,
        resume_id_used=str(resume_id_used) if resume_id_used else None,
        stderr=stderr,
        events=events,
        final_response=final_response,
        session_id=str(session_id) if session_id else None,
        thread_id=str(thread_id) if thread_id else None,
        rate_limited=bool(rate_limit_markers),
        rate_limit_markers=rate_limit_markers,
        progress=tracker.as_dict(),
        watchdog_reason=tracker.watchdog_reason,
    )


def read_stdout_with_watchdog(
    process: subprocess.Popen[str],
    log_file: Any,
    events: list[dict[str, Any]],
    tracker: ProgressTracker,
    config: Config,
) -> None:
    assert process.stdout is not None
    line_queue: queue.Queue[str] = queue.Queue()

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line_queue.put(line)

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stdout_thread.start()
    while process.poll() is None:
        try:
            line = line_queue.get(timeout=0.1)
        except queue.Empty:
            line = None
        if line:
            record_stdout_line(line, log_file, events, tracker)
            drain_stdout_queue(line_queue, log_file, events, tracker)
        reason = watchdog_reason(tracker, config)
        if reason:
            terminate_for_watchdog(process, tracker, reason, config.codex_watchdog_grace_seconds)
            break
    stdout_thread.join(timeout=1)
    drain_stdout_queue(line_queue, log_file, events, tracker)


def drain_stdout_queue(
    line_queue: queue.Queue[str],
    log_file: Any,
    events: list[dict[str, Any]],
    tracker: ProgressTracker,
) -> None:
    while True:
        try:
            line = line_queue.get_nowait()
        except queue.Empty:
            break
        record_stdout_line(line, log_file, events, tracker)


def record_stdout_line(
    line: str,
    log_file: Any,
    events: list[dict[str, Any]],
    tracker: ProgressTracker,
) -> None:
    tracker.record_stdout_line()
    log_file.write(line)
    log_file.flush()
    parsed = parse_json_line(line)
    if isinstance(parsed, dict):
        events.append(parsed)
        tracker.record_event(parsed, time.monotonic())


def watchdog_reason(tracker: ProgressTracker, config: Config) -> str | None:
    now = time.monotonic()
    if config.codex_total_runtime_timeout_seconds and now - tracker.start_monotonic >= config.codex_total_runtime_timeout_seconds:
        return TOTAL_RUNTIME_REASON
    if not tracker.first_meaningful_event_at:
        if config.codex_startup_stall_seconds and now - tracker.start_monotonic >= config.codex_startup_stall_seconds:
            return STARTUP_STALL_REASON
        turn_start = tracker.first_turn_started_monotonic
        if (
            turn_start is not None
            and config.codex_first_meaningful_timeout_seconds
            and now - turn_start >= config.codex_first_meaningful_timeout_seconds
        ):
            return FIRST_MEANINGFUL_STALL_REASON
        return None
    idle_at = tracker.last_meaningful_event_monotonic or tracker.first_meaningful_event_monotonic
    if idle_at and config.codex_mid_run_idle_seconds and now - idle_at >= config.codex_mid_run_idle_seconds:
        tracker.idle_warning = True
        if config.codex_mid_run_idle_kill_enabled:
            return MID_RUN_IDLE_REASON
    return None


def terminate_for_watchdog(
    process: subprocess.Popen[str],
    tracker: ProgressTracker,
    reason: str,
    grace_seconds: int,
) -> None:
    tracker.watchdog_reason = reason
    tracker.terminated_by_watchdog = True
    process.terminate()
    tracker.termination_signal = "SIGTERM"
    try:
        process.wait(timeout=max(grace_seconds, 0))
    except subprocess.TimeoutExpired:
        process.kill()
        tracker.termination_signal = "SIGKILL"
        process.wait()


def should_use_resume(task: dict) -> bool:
    resume_requested = task.get("resume_requested") or task.get("status") == "needs_resume"
    return bool(resume_requested and (task.get("session_id") or task.get("thread_id")))


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


def event_type_of(event: dict[str, Any]) -> str | None:
    value = event.get("type") or event.get("event_type")
    return str(value).lower() if value else None


def is_meaningful_event(event: dict[str, Any]) -> bool:
    if find_response_object(event):
        return True
    event_type = event_type_of(event) or ""
    item_type = item_type_of(event) or ""
    if event_type in {"turn.completed", "turn.failed", "error"} or event_type.startswith("error"):
        return True
    if event_type in {"item.started", "item.completed"} and item_type in {
        "agent_message",
        "command_execution",
        "file_change",
        "tool_call",
        "tool_result",
    }:
        return True
    if "agent" in event_type and ("message" in event_type or "response" in event_type):
        return True
    if any(token in event_type or token in item_type for token in ("command", "exec", "tool")) and any(
        state in event_type for state in ("start", "complete", "finish", "output")
    ):
        return True
    if ("file" in event_type or "file" in item_type) and any(
        token in event_type or token in item_type for token in ("change", "patch", "edit", "modified")
    ):
        return True
    return False


def item_type_of(event: dict[str, Any]) -> str | None:
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    value = item.get("type") or item.get("kind")
    return str(value).lower() if value else None


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

from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .execution_evidence_v2 import ExecutionEvidenceV2Error, validate_external_attestation
from .execution_evidence_v3 import (
    ExecutionEvidenceV3Error,
    enforce_external_command_identity,
    validate_external_attestation_v3,
)
from .fs import ensure_dir
from .process_lifecycle import (
    normal_exit_lifecycle,
    resolve_process_lifecycle_policy,
    terminate_process_group,
)
from .timeutil import iso_now


FINAL_RESPONSE_STATUSES = {"completed", "needs_resume", "blocked_user", "failed"}
FINAL_RESPONSE_REQUIRED_KEYS = {"task_id", "status", "summary", "changed_files", "verification"}


@dataclass
class ExternalJsonCommandResult:
    command: list[str]
    returncode: int | None
    log_path: Path
    timed_out: bool
    timeout_seconds: int
    started_at: str
    finished_at: str
    duration_seconds: float
    stdout_bytes: int
    stderr_bytes: int
    final_response: dict[str, Any] | None
    error: str | None = None
    process_lifecycle: dict[str, Any] | None = None


def run_external_json_command_task(
    config: Config,
    task: dict[str, Any],
    prompt: str,
    attempt: int,
    *,
    execution_settings: Any = None,
) -> ExternalJsonCommandResult:
    settings = execution_settings or task.get("_resolved_execution_settings")
    command_argv = external_command(task, settings=settings)
    if settings is not None:
        enforce_external_command_identity(task, settings, command_argv, config)
    command = [*command_argv, prompt]
    timeout_seconds = int(task.get("external_timeout_seconds") or config.external_json_command_timeout_seconds)
    log_dir = ensure_dir(config.log_dir / task["id"])
    log_path = log_dir / f"attempt-{attempt}.external-json-command.log"
    started_at = iso_now()
    started_monotonic = time.monotonic()
    stdout = ""
    stderr = ""
    returncode: int | None = None
    timed_out = False
    error: str | None = None
    process_lifecycle: dict[str, Any] | None = None
    lifecycle_policy = resolve_process_lifecycle_policy(
        task,
        backend="external-json-command",
    )
    try:
        if lifecycle_policy is None:
            completed = subprocess.run(
                command,
                cwd=task.get("cwd") or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            returncode = completed.returncode
        else:
            process = subprocess.Popen(
                command,
                cwd=task.get("cwd") or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            drain = start_process_pipe_drain(process)
            try:
                process.wait(timeout=timeout_seconds)
                returncode = process.returncode
                process_lifecycle = normal_exit_lifecycle()
            except subprocess.TimeoutExpired:
                timed_out = True
                process_lifecycle = terminate_process_group(
                    process,
                    process_group_id=process.pid,
                    trigger="external_wall_timeout",
                    grace_seconds=lifecycle_policy.termination_grace_seconds,
                )
                error = (
                    f"external-json-command timed out after {timeout_seconds}s"
                )
                returncode = None
            finally:
                stdout_bytes_value, stderr_bytes_value = finish_process_pipe_drain(
                    process,
                    drain,
                    join_timeout_seconds=1,
                )
                stdout = stdout_bytes_value.decode("utf-8", errors="replace")
                stderr = stderr_bytes_value.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = output_text(exc.stdout)
        stderr = output_text(exc.stderr)
        error = f"external-json-command timed out after {timeout_seconds}s"
        returncode = None
    except OSError as exc:
        error = str(exc)
        returncode = 127
    finished_at = iso_now()
    duration_seconds = round(time.monotonic() - started_monotonic, 3)
    final_response, parse_error = parse_final_response(stdout)
    if parse_error and not error:
        error = parse_error
    write_external_json_command_log(
        log_path,
        command=command,
        cwd=str(task.get("cwd") or ""),
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        timeout_seconds=timeout_seconds,
        returncode=returncode,
        timed_out=timed_out,
        error=error,
        stdout=stdout,
        stderr=stderr,
    )
    return ExternalJsonCommandResult(
        command=command,
        returncode=returncode,
        log_path=log_path,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        stdout_bytes=len(stdout.encode("utf-8")),
        stderr_bytes=len(stderr.encode("utf-8")),
        final_response=final_response,
        error=error,
        process_lifecycle=process_lifecycle,
    )


@dataclass
class ProcessPipeDrain:
    stdout_chunks: list[bytes]
    stderr_chunks: list[bytes]
    errors: list[Exception]
    stop: threading.Event
    thread: threading.Thread


def start_process_pipe_drain(
    process: subprocess.Popen[bytes],
) -> ProcessPipeDrain:
    if process.stdout is None or process.stderr is None:
        raise ValueError("process lifecycle requires captured stdout and stderr")
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    errors: list[Exception] = []
    stop = threading.Event()

    def drain_pipes() -> None:
        selector = selectors.DefaultSelector()
        streams = (
            (process.stdout, stdout_chunks),
            (process.stderr, stderr_chunks),
        )
        try:
            for stream, chunks in streams:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, chunks)
            while selector.get_map() and not stop.is_set():
                for key, _events in selector.select(timeout=0.05):
                    _drain_ready_stream(selector, key.fileobj, key.data)
            for key in list(selector.get_map().values()):
                _drain_ready_stream(selector, key.fileobj, key.data)
        except Exception as exc:
            errors.append(exc)
        finally:
            selector.close()

    thread = threading.Thread(target=drain_pipes, daemon=True)
    thread.start()
    return ProcessPipeDrain(
        stdout_chunks=stdout_chunks,
        stderr_chunks=stderr_chunks,
        errors=errors,
        stop=stop,
        thread=thread,
    )


def finish_process_pipe_drain(
    process: subprocess.Popen[bytes],
    drain: ProcessPipeDrain,
    *,
    join_timeout_seconds: int,
) -> tuple[bytes, bytes]:
    drain.stop.set()
    drain.thread.join(timeout=max(1, join_timeout_seconds))
    if drain.thread.is_alive():
        raise RuntimeError("process lifecycle pipe drain did not stop")
    if drain.errors:
        raise RuntimeError("process lifecycle pipe drain failed") from drain.errors[0]
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    return b"".join(drain.stdout_chunks), b"".join(drain.stderr_chunks)


def _drain_ready_stream(
    selector: selectors.BaseSelector,
    stream: Any,
    chunks: list[bytes],
) -> None:
    while True:
        try:
            chunk = os.read(stream.fileno(), 65536)
        except BlockingIOError:
            return
        except OSError:
            try:
                selector.unregister(stream)
            except (KeyError, ValueError):
                pass
            return
        if not chunk:
            try:
                selector.unregister(stream)
            except (KeyError, ValueError):
                pass
            return
        chunks.append(chunk)


def external_command(task: dict[str, Any], *, settings: Any = None) -> list[str]:
    snapshot = getattr(settings, "selected_target_snapshot", None) or {}
    target = snapshot.get("target") if isinstance(snapshot.get("target"), dict) else {}
    command = target.get("external_command") if target else task.get("external_command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("external-json-command task requires non-empty external_command argv list")
    values = {
        "model": str(getattr(settings, "model", "") or ""),
        "reasoning_effort": str(
            (getattr(settings, "config_overrides", None) or {}).get("model_reasoning_effort") or ""
        ),
    }
    return [values.get(part[1:-1], part) if part in {"{model}", "{reasoning_effort}"} else part for part in command]


def parse_final_response(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    text = stdout.strip()
    if not text:
        return None, "missing final JSON response"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid final JSON response"
    if not isinstance(parsed, dict):
        return None, "invalid final JSON response"
    return parsed, None


def validate_final_response(final_response: dict[str, Any], task_id: str) -> str | None:
    missing = sorted(FINAL_RESPONSE_REQUIRED_KEYS - set(final_response))
    if missing:
        return "final JSON missing required key(s): " + ", ".join(missing)
    if final_response.get("task_id") != task_id:
        return "final JSON task_id mismatch"
    status = final_response.get("status")
    if status not in FINAL_RESPONSE_STATUSES:
        return "invalid final JSON status"
    if not isinstance(final_response.get("changed_files"), list):
        return "final JSON changed_files must be a list"
    if not isinstance(final_response.get("verification"), list):
        return "final JSON verification must be a list"
    if "execution_evidence" in final_response:
        try:
            evidence = final_response.get("execution_evidence")
            if isinstance(evidence, dict) and evidence.get("schema_version") == 3:
                validate_external_attestation_v3(evidence)
            else:
                validate_external_attestation(evidence)
        except (ExecutionEvidenceV2Error, ExecutionEvidenceV3Error) as exc:
            return str(exc)
    return None


def output_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def write_external_json_command_log(
    path: Path,
    *,
    command: list[str],
    cwd: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    timeout_seconds: int,
    returncode: int | None,
    timed_out: bool,
    error: str | None,
    stdout: str,
    stderr: str,
) -> None:
    lines = [
        "external-json-command task",
        f"command: {command!r}",
        f"cwd: {cwd}",
        f"started_at: {started_at}",
        f"finished_at: {finished_at}",
        f"duration_seconds: {duration_seconds}",
        f"timeout_seconds: {timeout_seconds}",
        f"returncode: {returncode}",
        f"timed_out: {str(timed_out).lower()}",
    ]
    if error:
        lines.append(f"error: {error}")
    lines.extend(["", "stdout:", stdout, "", "stderr:", stderr])
    path.write_text("\n".join(lines), encoding="utf-8")

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .fs import ensure_dir
from .timeutil import iso_now


@dataclass
class ShellResult:
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
    error: str | None = None


def run_shell_task(config: Config, task: dict, attempt: int) -> ShellResult:
    command = shell_command(task)
    timeout_seconds = int(task.get("shell_timeout_seconds") or config.shell_task_timeout_seconds)
    log_dir = ensure_dir(config.log_dir / task["id"])
    log_path = log_dir / f"attempt-{attempt}.shell.log"
    started_at = iso_now()
    started_monotonic = time.monotonic()
    stdout = ""
    stderr = ""
    returncode: int | None = None
    timed_out = False
    error: str | None = None
    try:
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
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = output_text(exc.stdout)
        stderr = output_text(exc.stderr)
        error = f"shell command timed out after {timeout_seconds}s"
        returncode = None
    except OSError as exc:
        error = str(exc)
        returncode = 127
    finished_at = iso_now()
    duration_seconds = round(time.monotonic() - started_monotonic, 3)
    write_shell_log(
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
    return ShellResult(
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
        error=error,
    )


def shell_command(task: dict) -> list[str]:
    command = task.get("shell_command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ValueError("shell task requires shell_command list")
    return list(command)


def output_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def write_shell_log(
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
        "shell task",
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

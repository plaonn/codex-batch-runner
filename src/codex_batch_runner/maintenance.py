from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .doctor import build_doctor_report
from .events import write_event_nonfatal
from .fs import ensure_dir, write_json_atomic
from .lock import FileLock, lock_status
from .queue import RUNNABLE_STATUSES, list_tasks
from .state import clear_runner_pause, get_runner_pause, load_state, set_runner_pause
from .timeutil import iso_now, parse_time, utc_now
from .triggers import run_post_mutation_trigger

CODEX_CLI_MAINTENANCE_REASON = "Codex CLI maintenance"


@dataclass
class MaintenanceCommandResult:
    command: list[str]
    returncode: int | None
    timed_out: bool
    timeout_seconds: int
    log_path: Path
    started_at: str
    finished_at: str
    duration_seconds: float
    stdout_bytes: int
    stderr_bytes: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def summary(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "timeout_seconds": self.timeout_seconds,
            "log_path": str(self.log_path),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "error": self.error,
        }


def build_codex_cli_maintenance_report(config: Config) -> dict[str, Any]:
    blockers = codex_cli_maintenance_blockers(config, include_lock=True)
    return {
        "status": "ready" if not blockers else "blocked",
        "applied": False,
        "blockers": blockers,
        "configured": {
            "codex_cli_update_command": config.codex_cli_update_command,
            "codex_cli_smoke_command": config.codex_cli_smoke_command,
            "timeout_seconds": config.shell_task_timeout_seconds,
        },
        "log_dir": str(codex_cli_maintenance_log_parent(config)),
    }


def run_codex_cli_maintenance(config: Config) -> dict[str, Any]:
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire():
        return maintenance_result("blocked", ["active runner lock exists"], applied=False)

    pause: dict[str, Any] | None = None
    try:
        blockers = codex_cli_maintenance_blockers(config, include_lock=False)
        if blockers:
            return maintenance_result("blocked", blockers, applied=False)
        paused_by = os.environ.get("USER") or os.environ.get("USERNAME") or "cbr"
        pause = set_runner_pause(config, CODEX_CLI_MAINTENANCE_REASON, paused_by)
    finally:
        lock.release()

    write_event_nonfatal(
        config,
        "runner_pause_updated",
        summary="runner pause set for Codex CLI maintenance",
        payload={"action": "set", "runner_pause": pause},
    )

    run_dir = ensure_dir(codex_cli_maintenance_log_parent(config) / maintenance_run_id())
    before_path = write_doctor_snapshot(config, run_dir / "doctor-before.json")

    update_result = run_logged_command(
        config.codex_cli_update_command,
        cwd=config.root,
        timeout_seconds=config.shell_task_timeout_seconds,
        log_path=run_dir / "update.log",
    )
    after_update_path = write_doctor_snapshot(config, run_dir / "doctor-after-update.json")

    if not update_result.ok:
        result = maintenance_result(
            "failed",
            ["update command failed"],
            applied=True,
            run_dir=run_dir,
            before_doctor_path=before_path,
            after_update_doctor_path=after_update_path,
            update_result=update_result,
            pause_cleared=False,
        )
        write_maintenance_event(config, result)
        return result

    smoke_result = run_logged_command(
        config.codex_cli_smoke_command,
        cwd=config.root,
        timeout_seconds=config.shell_task_timeout_seconds,
        log_path=run_dir / "smoke.log",
    )
    after_smoke_path = write_doctor_snapshot(config, run_dir / "doctor-after-smoke.json")

    if not smoke_result.ok:
        result = maintenance_result(
            "failed",
            ["smoke command failed"],
            applied=True,
            run_dir=run_dir,
            before_doctor_path=before_path,
            after_update_doctor_path=after_update_path,
            after_smoke_doctor_path=after_smoke_path,
            update_result=update_result,
            smoke_result=smoke_result,
            pause_cleared=False,
        )
        write_maintenance_event(config, result)
        return result

    pause_cleared = clear_maintenance_pause(config)
    status = "succeeded" if pause_cleared else "failed"
    result = maintenance_result(
        status,
        [] if pause_cleared else ["could not clear maintenance pause"],
        applied=True,
        run_dir=run_dir,
        before_doctor_path=before_path,
        after_update_doctor_path=after_update_path,
        after_smoke_doctor_path=after_smoke_path,
        update_result=update_result,
        smoke_result=smoke_result,
        pause_cleared=pause_cleared,
    )
    write_maintenance_event(config, result)
    if pause_cleared:
        run_post_mutation_trigger(config)
    return result


def codex_cli_maintenance_blockers(config: Config, *, include_lock: bool) -> list[str]:
    blockers: list[str] = []
    if include_lock:
        status = lock_status(config.lock_file, config.stale_lock_seconds)
        if status.get("exists") and not status.get("stale"):
            blockers.append("active runner lock exists")
    state = load_state(config)
    cooldown_until = parse_time(state.get("global_cooldown_until"))
    if cooldown_until and cooldown_until > utc_now():
        blockers.append("global cooldown is active")
    if get_runner_pause(config).get("active"):
        blockers.append("runner pause is already active")
    if not config.codex_cli_update_command:
        blockers.append("codex_cli_update_command is not configured")
    if not config.codex_cli_smoke_command:
        blockers.append("codex_cli_smoke_command is not configured")
    for task in list_tasks(config):
        status = task.get("status")
        if status in RUNNABLE_STATUSES or status == "running":
            blockers.append(f"task {task.get('id') or '<unknown>'} is {status}")
    return blockers


def clear_maintenance_pause(config: Config) -> bool:
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire():
        return False
    previous: dict[str, Any]
    current: dict[str, Any]
    try:
        active = get_runner_pause(config)
        if active.get("reason") != CODEX_CLI_MAINTENANCE_REASON:
            return False
        previous = clear_runner_pause(config)
        current = get_runner_pause(config)
    finally:
        lock.release()
    write_event_nonfatal(
        config,
        "runner_pause_updated",
        summary="runner pause cleared after Codex CLI maintenance",
        payload={"action": "clear", "runner_pause": current, "previous_runner_pause": previous},
    )
    return True


def run_logged_command(command: list[str], *, cwd: Path, timeout_seconds: int, log_path: Path) -> MaintenanceCommandResult:
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
            cwd=str(cwd),
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
        error = f"command timed out after {timeout_seconds}s"
    except OSError as exc:
        error = str(exc)
        returncode = 127

    finished_at = iso_now()
    duration_seconds = round(time.monotonic() - started_monotonic, 3)
    write_command_log(
        log_path,
        command=command,
        cwd=str(cwd),
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
    return MaintenanceCommandResult(
        command=command,
        returncode=returncode,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
        log_path=log_path,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        stdout_bytes=len(stdout.encode("utf-8")),
        stderr_bytes=len(stderr.encode("utf-8")),
        error=error,
    )


def write_command_log(
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
    ensure_dir(path.parent)
    lines = [
        "codex cli maintenance command",
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


def write_doctor_snapshot(config: Config, path: Path) -> str:
    report = build_doctor_report(config)
    write_json_atomic(path, report)
    return str(path)


def write_maintenance_event(config: Config, result: dict[str, Any]) -> None:
    write_event_nonfatal(
        config,
        "codex_cli_maintenance_completed",
        summary=f"Codex CLI maintenance {result.get('status')}",
        payload={
            "status": result.get("status"),
            "blockers": result.get("blockers"),
            "run_dir": result.get("run_dir"),
            "pause_cleared": result.get("pause_cleared"),
            "update": command_event_payload(result.get("update")),
            "smoke": command_event_payload(result.get("smoke")),
        },
    )


def command_event_payload(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "returncode": value.get("returncode"),
        "timed_out": value.get("timed_out"),
        "log_path": value.get("log_path"),
        "stdout_bytes": value.get("stdout_bytes"),
        "stderr_bytes": value.get("stderr_bytes"),
    }


def maintenance_result(
    status: str,
    blockers: list[str],
    *,
    applied: bool,
    run_dir: Path | None = None,
    before_doctor_path: str | None = None,
    after_update_doctor_path: str | None = None,
    after_smoke_doctor_path: str | None = None,
    update_result: MaintenanceCommandResult | None = None,
    smoke_result: MaintenanceCommandResult | None = None,
    pause_cleared: bool | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "applied": applied,
        "blockers": blockers,
        "pause_cleared": pause_cleared,
    }
    if run_dir is not None:
        result["run_dir"] = str(run_dir)
    if before_doctor_path:
        result["doctor_before_path"] = before_doctor_path
    if after_update_doctor_path:
        result["doctor_after_update_path"] = after_update_doctor_path
    if after_smoke_doctor_path:
        result["doctor_after_smoke_path"] = after_smoke_doctor_path
    if update_result is not None:
        result["update"] = update_result.summary()
    if smoke_result is not None:
        result["smoke"] = smoke_result.summary()
    return result


def render_codex_cli_maintenance_report(report: dict[str, Any]) -> str:
    lines = ["Codex CLI maintenance", f"status: {report.get('status')}"]
    if report.get("applied"):
        lines.append(f"run_dir: {report.get('run_dir') or '-'}")
        lines.append(f"pause_cleared: {str(bool(report.get('pause_cleared'))).lower()}")
        if report.get("doctor_before_path"):
            lines.append(f"doctor_before: {report.get('doctor_before_path')}")
        if report.get("doctor_after_update_path"):
            lines.append(f"doctor_after_update: {report.get('doctor_after_update_path')}")
        if report.get("doctor_after_smoke_path"):
            lines.append(f"doctor_after_smoke: {report.get('doctor_after_smoke_path')}")
        append_command_lines(lines, "update", report.get("update"))
        append_command_lines(lines, "smoke", report.get("smoke"))
    else:
        configured = report.get("configured") if isinstance(report.get("configured"), dict) else {}
        lines.append(f"update_configured: {str(bool(configured.get('codex_cli_update_command'))).lower()}")
        lines.append(f"smoke_configured: {str(bool(configured.get('codex_cli_smoke_command'))).lower()}")
        lines.append(f"timeout_seconds: {configured.get('timeout_seconds') or '-'}")
        lines.append(f"log_dir: {report.get('log_dir') or '-'}")
    blockers = report.get("blockers") or []
    if blockers:
        lines.append("blockers:")
        for blocker in blockers:
            lines.append(f"  - {blocker}")
    return "\n".join(lines) + "\n"


def append_command_lines(lines: list[str], label: str, value: object) -> None:
    if not isinstance(value, dict):
        return
    lines.append(f"{label}_returncode: {value.get('returncode')}")
    lines.append(f"{label}_timed_out: {str(bool(value.get('timed_out'))).lower()}")
    lines.append(f"{label}_log: {value.get('log_path')}")


def codex_cli_maintenance_log_parent(config: Config) -> Path:
    return config.log_dir / "maintenance" / "codex-cli"


def maintenance_run_id() -> str:
    return "run-" + iso_now().replace(":", "").replace("+", "Z").replace(".", "-")


def output_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

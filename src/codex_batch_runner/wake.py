from __future__ import annotations

import math
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath

from .config import Config
from .events import write_event_nonfatal
from .timeutil import utc_now

MANUAL_COOLDOWN_WAKE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class WakeScheduleResult:
    status: str
    message: str
    command: list[str]


def schedule_manual_cooldown_wake(config: Config, effective_cooldown_until: datetime) -> WakeScheduleResult:
    scheduler = config.manual_cooldown_wake_scheduler
    wake_command = config.manual_cooldown_wake_command
    if scheduler == "disabled":
        return record_wake_result(
            config,
            WakeScheduleResult("skipped", "manual cooldown one-shot wake disabled", []),
            effective_cooldown_until,
        )
    if not wake_command:
        return record_wake_result(
            config,
            WakeScheduleResult("skipped", "manual cooldown one-shot wake command not configured", []),
            effective_cooldown_until,
        )
    if is_direct_codex_command(wake_command):
        return record_wake_result(
            config,
            WakeScheduleResult("failed", "manual cooldown one-shot wake command must not invoke codex directly", []),
            effective_cooldown_until,
        )
    if scheduler == "macos_launchd":
        result = schedule_macos_launchd_wake(wake_command, effective_cooldown_until)
        return record_wake_result(config, result, effective_cooldown_until)
    return record_wake_result(
        config,
        WakeScheduleResult("failed", f"unsupported manual cooldown wake scheduler: {scheduler}", []),
        effective_cooldown_until,
    )


def schedule_macos_launchd_wake(wake_command: list[str], effective_cooldown_until: datetime) -> WakeScheduleResult:
    if platform.system() != "Darwin":
        return WakeScheduleResult("skipped", "macos_launchd scheduler is only available on macOS", [])
    delay_seconds = seconds_until(effective_cooldown_until)
    label = f"codex-batch-runner.cooldown-wake.{int(effective_cooldown_until.timestamp())}"
    command = [
        "launchctl",
        "submit",
        "-l",
        label,
        "--",
        "/bin/sh",
        "-c",
        'sleep "$1"; shift; exec "$@"',
        "cbr-cooldown-wake",
        str(delay_seconds),
        *wake_command,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=MANUAL_COOLDOWN_WAKE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return WakeScheduleResult("failed", "manual cooldown one-shot wake scheduling timed out", command)
    except OSError as exc:
        return WakeScheduleResult("failed", f"manual cooldown one-shot wake scheduling failed: {exc}", command)
    if result.returncode != 0:
        return WakeScheduleResult(
            "failed",
            f"manual cooldown one-shot wake scheduler exited with status {result.returncode}",
            command,
        )
    return WakeScheduleResult("scheduled", "manual cooldown one-shot wake scheduled", command)


def seconds_until(deadline: datetime) -> int:
    now = utc_now()
    target = deadline.astimezone(now.tzinfo) if deadline.tzinfo else deadline.replace(tzinfo=now.tzinfo)
    return max(0, int(math.ceil((target - now).total_seconds())))


def is_direct_codex_command(command: list[str]) -> bool:
    if not command:
        return False
    executable = PurePath(command[0]).name
    if executable == "codex":
        return True
    return executable == "env" and len(command) > 1 and PurePath(command[1]).name == "codex"


def record_wake_result(
    config: Config,
    result: WakeScheduleResult,
    effective_cooldown_until: datetime,
) -> WakeScheduleResult:
    event_type = {
        "scheduled": "cooldown_wake_scheduled",
        "skipped": "cooldown_wake_skipped",
        "failed": "cooldown_wake_failed",
    }.get(result.status, "cooldown_wake_failed")
    write_event_nonfatal(
        config,
        event_type,
        summary=result.message,
        payload={
            "status": result.status,
            "scheduler": config.manual_cooldown_wake_scheduler,
            "effective_cooldown_until": effective_cooldown_until.isoformat(),
            "message": result.message,
            "scheduler_command": result.command,
            "wake_command": config.manual_cooldown_wake_command,
        },
    )
    if result.status == "failed":
        print(f"warning: {result.message}", file=sys.stderr)
    return result

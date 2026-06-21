from __future__ import annotations

import errno
import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

from .timeutil import iso_now, parse_time, utc_now


class LockBusy(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: Path, stale_seconds: int) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.acquired = False

    def acquire(self, task_id: str | None = None) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at": iso_now(),
            "task_id": task_id,
        }
        encoded = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if self.is_stale():
                self.path.unlink(missing_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            else:
                return False
        with os.fdopen(fd, "wb") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        self.acquired = True
        return True

    def release(self) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False

    def is_stale(self) -> bool:
        return lock_status(self.path, self.stale_seconds)["stale"]

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise LockBusy(f"active lock exists: {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def lock_status(path: Path, stale_seconds: int) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "age_seconds": None,
            "stale": False,
            "pid": None,
            "hostname": None,
            "pid_alive": None,
        }

    data = read_lock_metadata(path)
    created_at = parse_time(data.get("created_at")) if data else None
    age = lock_age_seconds(path, created_at)
    pid = lock_pid(data.get("pid")) if data else None
    hostname = data.get("hostname") if data else None
    same_host = isinstance(hostname, str) and hostname == socket.gethostname()
    pid_alive = pid_exists(pid) if same_host and pid is not None else None
    dead_same_host_pid = same_host and pid is not None and pid_alive is False
    stale_by_age = age is None or age > stale_seconds

    return {
        "exists": True,
        "path": str(path),
        "age_seconds": age,
        "stale": bool(dead_same_host_pid or stale_by_age),
        "pid": pid,
        "hostname": hostname if isinstance(hostname, str) else None,
        "pid_alive": pid_alive,
    }


def read_lock_metadata(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def lock_age_seconds(path: Path, created_at: datetime | None) -> int | None:
    if created_at:
        return max(0, int((utc_now() - created_at).total_seconds()))
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return max(0, int(utc_now().timestamp() - mtime))


def lock_pid(value: object) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None

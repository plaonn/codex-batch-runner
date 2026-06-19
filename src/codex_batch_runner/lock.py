from __future__ import annotations

import errno
import json
import os
import socket
from pathlib import Path

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
        try:
            raw = self.path.read_text(encoding="utf-8")
            created_at = parse_time(json.loads(raw).get("created_at"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return True
        if not created_at:
            return True
        age = (utc_now() - created_at).total_seconds()
        return age > self.stale_seconds

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

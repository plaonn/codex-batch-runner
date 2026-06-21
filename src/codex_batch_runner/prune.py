from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .fs import ensure_dir, read_json
from .timeutil import parse_time, utc_now

DEFAULT_PRUNE_AGE_DAYS = 30


@dataclass(frozen=True)
class PruneFile:
    kind: str
    path: str
    exists: bool
    safe: bool
    deleted: bool = False
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "exists": self.exists,
            "safe": self.safe,
            "deleted": self.deleted,
            "reason": self.reason,
        }


def build_prune_report(config: Config, age_days: int, apply: bool = False) -> dict[str, Any]:
    if age_days < 0:
        raise ValueError("age days must be non-negative")

    queue_dir = config.queue_dir.expanduser().resolve()
    log_dir = config.log_dir.expanduser().resolve()
    event_dir = config.event_dir.expanduser().resolve()
    cutoff = utc_now() - timedelta(days=age_days)
    ensure_dir(queue_dir)
    ensure_dir(event_dir)

    candidates = []
    deleted_files = 0
    for task_file in sorted(queue_dir.glob("*.json")):
        task = read_json(task_file)
        if not isinstance(task, dict):
            continue
        reason, timestamp = prune_reason(task)
        parsed = parse_time(timestamp)
        if not reason or not parsed or parsed > cutoff:
            continue
        files = candidate_files(task, task_file, queue_dir, log_dir)
        if apply:
            files = [delete_file(file) for file in files]
        deleted_files += sum(1 for file in files if file.deleted)
        candidates.append(
            {
                "task_id": task.get("id"),
                "status": task.get("status"),
                "review_status": task.get("review_status"),
                "reason": reason,
                "timestamp": timestamp,
                "files": [file.as_dict() for file in files],
            }
        )

    event_candidates = event_candidate_files(event_dir, cutoff)
    if apply:
        event_candidates = [delete_file(file) for file in event_candidates]
    deleted_files += sum(1 for file in event_candidates if file.deleted)

    return {
        "mode": "apply" if apply else "dry-run",
        "dry_run": not apply,
        "age_days": age_days,
        "cutoff": cutoff.isoformat(),
        "queue_dir": str(queue_dir),
        "log_dir": str(log_dir),
        "event_dir": str(event_dir),
        "candidate_count": len(candidates) + len(event_candidates),
        "task_candidate_count": len(candidates),
        "event_candidate_count": len(event_candidates),
        "deleted_files": deleted_files,
        "candidates": candidates,
        "event_candidates": [file.as_dict() for file in event_candidates],
    }


def prune_reason(task: dict) -> tuple[str | None, str | None]:
    status = task.get("status")
    if status == "archived":
        timestamp = first_text(task, "archived_at", "updated_at", "completed_at", "reviewed_at", "created_at")
        return "archived", timestamp
    if status == "completed" and task.get("review_status") == "accepted":
        timestamp = first_text(task, "reviewed_at", "completed_at", "updated_at", "created_at")
        return "completed_accepted", timestamp
    return None, None


def first_text(task: dict, *keys: str) -> str | None:
    for key in keys:
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def candidate_files(task: dict, task_file: Path, queue_dir: Path, log_dir: Path) -> list[PruneFile]:
    files = [safe_file("task", task_file, queue_dir, "queue_dir")]
    for log_path in task_log_paths(task):
        files.append(safe_file("log", Path(log_path).expanduser(), log_dir, "log_dir"))
    return files


def task_log_paths(task: dict) -> list[str]:
    paths: list[str] = []
    for path in task.get("log_paths") or []:
        if isinstance(path, str) and path:
            paths.append(path)
    last_run = task.get("last_run")
    if isinstance(last_run, dict):
        path = last_run.get("log_path")
        if isinstance(path, str) and path:
            paths.append(path)
    return list(dict.fromkeys(paths))


def event_candidate_files(event_dir: Path, cutoff: datetime) -> list[PruneFile]:
    files: list[PruneFile] = []
    for path in sorted(event_dir.rglob("*.jsonl")):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        modified_at = datetime.fromtimestamp(mtime, tz=cutoff.tzinfo)
        if modified_at > cutoff:
            continue
        files.append(safe_file("event", path, event_dir, "event_dir"))
    return files


def safe_file(kind: str, path: Path, root: Path, root_name: str) -> PruneFile:
    resolved_root = root.expanduser().resolve()
    resolved_path = path.expanduser().resolve(strict=False)
    exists = resolved_path.exists()
    safe = is_relative_to(resolved_path, resolved_root)
    reason = None
    if not safe:
        reason = f"outside configured {root_name}"
    elif exists and not resolved_path.is_file():
        safe = False
        reason = "not a regular file"
    return PruneFile(kind=kind, path=str(resolved_path), exists=exists, safe=safe, reason=reason)


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def delete_file(file: PruneFile) -> PruneFile:
    if not file.safe or not file.exists:
        return file
    Path(file.path).unlink()
    return PruneFile(
        kind=file.kind,
        path=file.path,
        exists=file.exists,
        safe=file.safe,
        deleted=True,
        reason=file.reason,
    )

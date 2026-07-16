from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
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
    skipped: bool = False
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "exists": self.exists,
            "safe": self.safe,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CursorSafety:
    cursor_paths: list[str]
    warnings: list[str]
    block_all_event_pruning: bool
    current_event_file: Path | None = None
    current_byte_offset: int | None = None
    last_processed_event_file: Path | None = None


def build_prune_report(
    config: Config,
    age_days: int,
    apply: bool = False,
    notifier_cursor_state_paths: list[Path] | None = None,
) -> dict[str, Any]:
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

    cursor_paths = notifier_cursor_state_paths
    if cursor_paths is None:
        cursor_paths = config.notifier_cursor_state_paths
    cursor_safety = load_cursor_safety(cursor_paths, event_dir)
    event_candidates = event_candidate_files(event_dir, cutoff, cursor_safety)
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
        "notifier_cursor_state_paths": cursor_safety.cursor_paths,
        "warnings": cursor_safety.warnings,
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
    files = [
        PruneFile(
            kind="task",
            path=str(task_file),
            exists=task_file.exists(),
            safe=False,
            skipped=True,
            reason="canonical task retention requires a separate explicit deletion policy",
        )
    ]
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


def event_candidate_files(event_dir: Path, cutoff: datetime, cursor_safety: CursorSafety | None = None) -> list[PruneFile]:
    files: list[PruneFile] = []
    for path in sorted(event_dir.rglob("*.jsonl")):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        modified_at = datetime.fromtimestamp(mtime, tz=cutoff.tzinfo)
        if modified_at > cutoff:
            continue
        file = safe_file("event", path, event_dir, "event_dir")
        if cursor_safety and file.safe:
            file = apply_cursor_safety(file, cursor_safety)
        files.append(file)
    return files


def load_cursor_safety(cursor_paths: list[Path], event_dir: Path) -> CursorSafety:
    resolved_event_dir = event_dir.expanduser().resolve()
    resolved_cursor_paths = [path.expanduser().resolve(strict=False) for path in cursor_paths]
    warnings: list[str] = []
    current_event_file: Path | None = None
    current_byte_offset: int | None = None
    last_processed_event_file: Path | None = None
    block_all_event_pruning = False

    for cursor_path in resolved_cursor_paths:
        if not cursor_path.exists():
            warnings.append(f"notifier cursor state missing: {cursor_path}")
            block_all_event_pruning = True
            continue
        try:
            with cursor_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception as exc:
            warnings.append(f"notifier cursor state unreadable: {cursor_path}: {exc}")
            block_all_event_pruning = True
            continue
        if not isinstance(data, dict):
            warnings.append(f"notifier cursor state malformed: {cursor_path}: root must be an object")
            block_all_event_pruning = True
            continue

        parsed_current, parsed_offset, parsed_last, parse_warnings = parse_cursor_state(data, cursor_path, resolved_event_dir)
        if parse_warnings:
            warnings.extend(parse_warnings)
            block_all_event_pruning = True
            continue
        if parsed_current and current_event_file is None:
            current_event_file = parsed_current
            current_byte_offset = parsed_offset
        if parsed_last and last_processed_event_file is None:
            last_processed_event_file = parsed_last

    return CursorSafety(
        cursor_paths=[str(path) for path in resolved_cursor_paths],
        warnings=warnings,
        block_all_event_pruning=block_all_event_pruning,
        current_event_file=current_event_file,
        current_byte_offset=current_byte_offset,
        last_processed_event_file=last_processed_event_file,
    )


def parse_cursor_state(data: dict[str, Any], cursor_path: Path, event_dir: Path) -> tuple[Path | None, int | None, Path | None, list[str]]:
    warnings: list[str] = []
    current_event_file = cursor_file_value(data, "current_event_file", cursor_path, event_dir, warnings)
    last_processed_event_file = cursor_file_value(data, "last_processed_event_file", cursor_path, event_dir, warnings)
    current_byte_offset = cursor_offset_value(data, "current_byte_offset", cursor_path, warnings)
    legacy_offset = cursor_offset_value(data, "byte_offset", cursor_path, warnings)
    if current_byte_offset is None:
        current_byte_offset = legacy_offset
    if current_event_file is None and last_processed_event_file is None:
        warnings.append(
            f"notifier cursor state malformed: {cursor_path}: expected current_event_file or last_processed_event_file"
        )
    return current_event_file, current_byte_offset, last_processed_event_file, warnings


def cursor_file_value(data: dict[str, Any], key: str, cursor_path: Path, event_dir: Path, warnings: list[str]) -> Path | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"notifier cursor state malformed: {cursor_path}: {key} must be a path string")
        return None
    path = Path(value).expanduser()
    resolved = (event_dir / path).resolve(strict=False) if not path.is_absolute() else path.resolve(strict=False)
    if not is_relative_to(resolved, event_dir):
        warnings.append(f"notifier cursor state outside event_dir: {cursor_path}: {key}={resolved}")
        return None
    return resolved


def cursor_offset_value(data: dict[str, Any], key: str, cursor_path: Path, warnings: list[str]) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        warnings.append(f"notifier cursor state malformed: {cursor_path}: {key} must be a non-negative integer")
        return None
    return value


def apply_cursor_safety(file: PruneFile, cursor_safety: CursorSafety) -> PruneFile:
    if cursor_safety.block_all_event_pruning:
        return skipped_file(file, "notifier cursor safety warning")

    path = Path(file.path)
    current_event_file = cursor_safety.current_event_file
    if current_event_file is not None:
        if str(path) > str(current_event_file):
            return skipped_file(file, "notifier cursor has not reached this event file")
        if path == current_event_file and cursor_safety.current_byte_offset is None:
            return skipped_file(file, "notifier cursor byte offset is unknown")
        if path == current_event_file and cursor_safety.current_byte_offset is not None:
            try:
                size = path.stat().st_size
            except OSError:
                return skipped_file(file, "notifier cursor event file is not readable")
            if cursor_safety.current_byte_offset < size:
                return skipped_file(file, "notifier cursor has not fully processed this event file")

    last_processed_event_file = cursor_safety.last_processed_event_file
    if current_event_file is None and last_processed_event_file is not None and str(path) > str(last_processed_event_file):
        return skipped_file(file, "notifier cursor has not reached this event file")

    return file


def skipped_file(file: PruneFile, reason: str) -> PruneFile:
    return PruneFile(
        kind=file.kind,
        path=file.path,
        exists=file.exists,
        safe=file.safe,
        deleted=file.deleted,
        skipped=True,
        reason=reason,
    )


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
    if not file.safe or not file.exists or file.skipped:
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

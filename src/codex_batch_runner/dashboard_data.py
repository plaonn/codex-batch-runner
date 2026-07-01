from __future__ import annotations

import contextlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .config import Config
from .events import read_jsonl, sanitize_scalar
from .fs import read_json
from .index import SCHEMA_VERSION as INDEX_SCHEMA_VERSION
from .index import index_db_path, read_metadata, read_metadata_int, scalar_count
from .state import load_state
from .timeutil import parse_time, utc_now

DASHBOARD_DATA_VERSION = 1
DEFAULT_RECENT_EVENT_LIMIT = 10

REVIEW_BACKLOG_STATUSES = {"unreviewed", "rejected", "needs_followup", "reviewing"}
FAILED_OR_BLOCKED_STATUSES = {"failed", "blocked_user", "usage_exhausted"}


def build_dashboard_overview(
    config: Config,
    *,
    recent_event_limit: int = DEFAULT_RECENT_EVENT_LIMIT,
) -> dict[str, Any]:
    """Build a local read-only dashboard overview from the SQLite index or canonical files."""
    warnings: list[str] = []
    source = "sqlite_index"
    index_info = read_index_info(config, warnings)
    if index_info["usable"]:
        try:
            with contextlib.closing(sqlite3.connect(f"file:{index_info['path']}?mode=ro", uri=True)) as conn:
                task_rows = sqlite_task_rows(conn)
                event_rows = sqlite_event_rows(conn, recent_event_limit)
        except (OSError, sqlite3.DatabaseError) as exc:
            warnings.append(f"index database became unreadable: {type(exc).__name__}; canonical fallback used")
            source = "canonical_fallback"
            task_rows, event_rows = canonical_rows(config, warnings, recent_event_limit)
    else:
        source = "canonical_fallback"
        task_rows, event_rows = canonical_rows(config, warnings, recent_event_limit)

    return {
        "dashboard_data_version": DASHBOARD_DATA_VERSION,
        "data_source": source,
        "fallback_used": source == "canonical_fallback",
        "index": {
            "available": index_info["available"],
            "usable": index_info["usable"],
            "schema_version": index_info["schema_version"],
            "expected_schema_version": INDEX_SCHEMA_VERSION,
            "last_rebuild_at": index_info["last_rebuild_at"],
        },
        "warnings": warnings,
        "tasks": summarize_tasks(task_rows, config),
        "review": summarize_review(task_rows),
        "failures": summarize_failures(task_rows),
        "running": summarize_running(task_rows, config),
        "cooldowns": summarize_cooldowns(config, warnings),
        "recent_events": summarize_events(event_rows),
    }


def read_index_info(config: Config, warnings: list[str]) -> dict[str, Any]:
    db_path = index_db_path(config)
    info = {
        "path": db_path,
        "available": db_path.exists(),
        "usable": False,
        "schema_version": None,
        "last_rebuild_at": None,
    }
    if not db_path.exists():
        warnings.append("index database is missing; canonical fallback used")
        return info

    source = source_counts_readonly(config, warnings)
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            schema_version = read_metadata_int(conn, "schema_version")
            info["schema_version"] = schema_version
            info["last_rebuild_at"] = read_metadata(conn, "last_rebuild_at")
            if schema_version != INDEX_SCHEMA_VERSION:
                warnings.append(
                    f"index schema mismatch: found {schema_version}, expected {INDEX_SCHEMA_VERSION}; canonical fallback used"
                )
                return info
            indexed_tasks = scalar_count(conn, "tasks")
            indexed_events = scalar_count(conn, "events")
            index_warnings = index_staleness_warnings(conn, config, db_path, source, indexed_tasks, indexed_events)
            warnings.extend(index_warnings)
            info["usable"] = not index_warnings
    except sqlite3.DatabaseError as exc:
        warnings.append(f"index database is unreadable: {type(exc).__name__}; canonical fallback used")
    except OSError as exc:
        warnings.append(f"index database cannot be read: {type(exc).__name__}; canonical fallback used")
    return info


def sqlite_task_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT
          tasks.task_id,
          tasks.status,
          tasks.project_id,
          tasks.created_at,
          tasks.updated_at,
          tasks.started_at,
          tasks.completed_at,
          tasks.archived_at,
          tasks.resolution,
          tasks.chain_status,
          tasks.capacity_pool,
          tasks.task_priority,
          review.review_status,
          review.last_review_decision,
          git.execution_mode,
          git.execution_apply_status
        FROM tasks
        LEFT JOIN task_review_state AS review ON review.task_id = tasks.task_id
        LEFT JOIN task_git_metadata AS git ON git.task_id = tasks.task_id
        """
    )
    columns = [item[0] for item in cursor.description]
    rows = cursor.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def sqlite_event_rows(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT event_type, occurred_at, task_id, project_id
        FROM events
        ORDER BY occurred_at DESC, event_id DESC
        LIMIT ?
        """,
        (max(0, limit),),
    )
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def canonical_rows(
    config: Config,
    warnings: list[str],
    recent_event_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tasks = [canonical_task_row(task) for task in retained_tasks_readonly(config, warnings)]
    events = [canonical_event_row(event) for event in retained_events_readonly(config)]
    events.sort(key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("event_type") or "")), reverse=True)
    return tasks, events[: max(0, recent_event_limit)]


def canonical_task_row(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": safe_scalar(task.get("id")),
        "status": safe_scalar(task.get("status")),
        "project_id": safe_scalar(task.get("project_id")),
        "created_at": safe_scalar(task.get("created_at")),
        "updated_at": safe_scalar(task.get("updated_at")),
        "started_at": safe_scalar(task.get("started_at")),
        "completed_at": safe_scalar(task.get("completed_at")),
        "archived_at": safe_scalar(task.get("archived_at")),
        "resolution": safe_scalar(task.get("resolution")),
        "chain_status": safe_scalar(task.get("chain_status")),
        "capacity_pool": safe_scalar(task.get("capacity_pool")),
        "task_priority": safe_scalar(task.get("task_priority") or task.get("priority")),
        "review_status": safe_scalar(task.get("review_status")),
        "last_review_decision": safe_scalar(task.get("last_review_decision")),
        "execution_mode": safe_scalar(task.get("execution_mode")),
        "execution_apply_status": safe_scalar(task.get("execution_apply_status")),
    }


def canonical_event_row(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": safe_scalar(event.get("event_type")),
        "occurred_at": safe_scalar(event.get("occurred_at")),
        "task_id": safe_scalar(event.get("task_id")),
        "project_id": safe_scalar(event.get("project_id")),
    }


def summarize_tasks(rows: list[dict[str, Any]], config: Config) -> dict[str, Any]:
    by_status = Counter(str(row.get("status") or "unknown") for row in rows)
    active_statuses = {
        "runnable",
        "needs_resume",
        "running",
        "failed",
        "blocked_user",
        "usage_exhausted",
        "cooldown",
    }
    return {
        "total": len(rows),
        "active": sum(count for status, count in by_status.items() if status in active_statuses),
        "by_status": dict(sorted(by_status.items())),
        "runnable": by_status.get("runnable", 0),
        "needs_resume": by_status.get("needs_resume", 0),
        "capacity": {
            "max_total_running": config.max_total_running,
            "max_running_per_project": config.max_running_per_project,
            "running_total": by_status.get("running", 0),
        },
    }


def summarize_review(rows: list[dict[str, Any]]) -> dict[str, Any]:
    backlog = [row for row in rows if is_review_backlog(row)]
    accepted_unapplied = [row for row in rows if is_accepted_unapplied(row)]
    return {
        "backlog": {
            "total": len(backlog),
            "by_review_status": dict(sorted(Counter(review_status(row) for row in backlog).items())),
        },
        "accepted_unapplied": len(accepted_unapplied),
    }


def summarize_failures(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unresolved = [row for row in rows if not row.get("resolution")]
    by_status = Counter(str(row.get("status") or "unknown") for row in unresolved)
    return {
        "failed": by_status.get("failed", 0),
        "blocked_user": by_status.get("blocked_user", 0),
        "usage_exhausted": by_status.get("usage_exhausted", 0),
        "failed_or_blocked": sum(by_status.get(status, 0) for status in FAILED_OR_BLOCKED_STATUSES),
    }


def summarize_running(rows: list[dict[str, Any]], config: Config) -> dict[str, Any]:
    running = [row for row in rows if row.get("status") == "running"]
    stale = [row for row in running if running_row_is_stale(row, config)]
    return {
        "total": len(running),
        "stale_progress": len(stale),
    }


def summarize_cooldowns(config: Config, warnings: list[str]) -> dict[str, Any]:
    try:
        state = load_state(config)
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"state file is unreadable: {type(exc).__name__}; cooldown fields omitted")
        state = {}
    return {
        "global": cooldown_entry(state.get("global_cooldown_until"), state.get("last_rate_limit_at")),
        "reviewer_codex": cooldown_entry(
            state.get("reviewer_codex_cooldown_until"),
            state.get("last_reviewer_codex_rate_limit_at"),
        ),
    }


def summarize_events(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "recent_count": len(rows),
        "by_type": dict(sorted(Counter(str(row.get("event_type") or "unknown") for row in rows).items())),
        "recent": [
            {
                "event_type": safe_scalar(row.get("event_type")),
                "occurred_at": safe_scalar(row.get("occurred_at")),
                "task_id": safe_scalar(row.get("task_id")),
                "project_id": safe_scalar(row.get("project_id")),
            }
            for row in rows
        ],
    }


def is_review_backlog(row: dict[str, Any]) -> bool:
    return (
        row.get("status") == "completed"
        and not row.get("resolution")
        and review_status(row) in REVIEW_BACKLOG_STATUSES
    )


def is_accepted_unapplied(row: dict[str, Any]) -> bool:
    return (
        row.get("status") == "completed"
        and row.get("execution_mode") == "git_worktree"
        and review_status(row) == "accepted"
        and row.get("execution_apply_status") != "applied"
    )


def review_status(row: dict[str, Any]) -> str:
    if row.get("status") == "completed":
        return str(row.get("review_status") or "unreviewed")
    return str(row.get("review_status") or "")


def running_row_is_stale(row: dict[str, Any], config: Config) -> bool:
    now = utc_now()
    started = parse_time(row.get("started_at"))
    updated = parse_time(row.get("updated_at"))
    if started and (now - started).total_seconds() > config.stale_lock_seconds:
        return True
    if updated and (now - updated).total_seconds() > config.codex_mid_run_idle_seconds:
        return True
    return not started and not updated


def cooldown_entry(cooldown_until: Any, last_rate_limit_at: Any) -> dict[str, Any]:
    until = parse_time(cooldown_until)
    return {
        "active": bool(until and until > utc_now()),
        "cooldown_until": safe_scalar(cooldown_until),
        "last_rate_limit_at": safe_scalar(last_rate_limit_at),
    }


def index_staleness_warnings(
    conn: sqlite3.Connection,
    config: Config,
    db_path: Path,
    source: dict[str, int],
    indexed_tasks: int,
    indexed_events: int,
) -> list[str]:
    warnings: list[str] = []
    for key, current in (
        ("source_task_files", source["task_files"]),
        ("source_event_files", source["event_files"]),
        ("source_event_rows", source["event_rows"]),
    ):
        recorded = read_metadata_int(conn, key)
        if recorded is not None and recorded != current:
            warnings.append(
                f"index may be stale: {key} count mismatch "
                f"(current retained source {current}, indexed metadata {recorded}); canonical fallback used"
            )
    if indexed_tasks != source["valid_task_files"]:
        warnings.append(
            "index may be stale: retained task count mismatch "
            f"(current retained tasks {source['valid_task_files']}, indexed table {indexed_tasks}); canonical fallback used"
        )
    if indexed_events != source["event_rows"]:
        warnings.append(
            "index may be stale: retained event row count mismatch "
            f"(current retained event rows {source['event_rows']}, indexed table {indexed_events}); canonical fallback used"
        )
    if metadata_count_mismatch(conn, "indexed_tasks", indexed_tasks):
        warnings.append("index metadata mismatch: indexed_tasks differs from SQLite table; canonical fallback used")
    if metadata_count_mismatch(conn, "indexed_events", indexed_events):
        warnings.append("index metadata mismatch: indexed_events differs from SQLite table; canonical fallback used")
    if source_changed_after_rebuild(config, db_path):
        warnings.append("index may be stale: retained source files changed after last rebuild; canonical fallback used")
    return warnings


def metadata_count_mismatch(conn: sqlite3.Connection, key: str, current: int) -> bool:
    recorded = read_metadata_int(conn, key)
    return recorded is not None and recorded != current


def source_changed_after_rebuild(config: Config, db_path: Path) -> bool:
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        return True
    for root, pattern in ((config.queue_dir, "*.json"), (config.event_dir, "*.jsonl")):
        if not root.exists():
            continue
        try:
            if any(path.stat().st_mtime > db_mtime for path in root.glob(pattern)):
                return True
        except OSError:
            return True
    return False


def source_counts_readonly(config: Config, warnings: list[str]) -> dict[str, int]:
    task_files = 0
    valid_task_files = 0
    event_files = 0
    event_rows = 0
    if config.queue_dir.exists():
        for path in sorted(config.queue_dir.glob("*.json")):
            task_files += 1
            try:
                parsed = read_json(path)
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"skipped unreadable task file {path.name}: {type(exc).__name__}")
                continue
            if isinstance(parsed, dict):
                valid_task_files += 1
            else:
                warnings.append(f"skipped non-object task file {path.name}")
    if config.event_dir.exists():
        for path in sorted(config.event_dir.glob("*.jsonl")):
            event_files += 1
            event_rows += len(read_jsonl(path))
    return {
        "task_files": task_files,
        "valid_task_files": valid_task_files,
        "event_files": event_files,
        "event_rows": event_rows,
    }


def retained_tasks_readonly(config: Config, warnings: list[str]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    if not config.queue_dir.exists():
        return tasks
    for path in sorted(config.queue_dir.glob("*.json")):
        try:
            task = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"skipped unreadable task file {path.name}: {type(exc).__name__}")
            continue
        if isinstance(task, dict):
            tasks.append(task)
    return tasks


def retained_events_readonly(config: Config) -> list[dict[str, Any]]:
    if not config.event_dir.exists():
        return []
    events: list[dict[str, Any]] = []
    for path in sorted(config.event_dir.glob("*.jsonl")):
        events.extend(read_jsonl(path))
    return events


def safe_scalar(value: Any) -> str | None:
    if value is None:
        return None
    sanitized = sanitize_scalar(value)
    if sanitized is None:
        return None
    return str(sanitized)

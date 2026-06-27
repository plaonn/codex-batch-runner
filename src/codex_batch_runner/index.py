from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .events import read_jsonl, sanitize_payload, sanitize_scalar
from .fs import ensure_dir, read_json
from .timeutil import iso_now

SCHEMA_VERSION = 1
DB_FILENAME = "index.sqlite3"


def index_db_path(config: Config) -> Path:
    return config.queue_dir.parent / DB_FILENAME


def build_rebuild_report(config: Config, *, apply: bool) -> dict[str, Any]:
    source = source_counts(config)
    report: dict[str, Any] = {
        "ok": True,
        "mode": "apply" if apply else "dry-run",
        "dry_run": not apply,
        "db_path": str(index_db_path(config)),
        "schema_version": SCHEMA_VERSION,
        "source_task_files": source["task_files"],
        "source_event_files": source["event_files"],
        "source_event_rows": source["event_rows"],
        "indexed_tasks": 0,
        "indexed_events": 0,
        "indexed_dependencies": 0,
        "warnings": source["warnings"],
        "wrote_db": False,
        "last_rebuild_at": None,
    }
    if not apply:
        tasks = retained_tasks(config)
        events = retained_events(config)
        report.update(index_plan_counts(tasks, events))
        return report

    db_path = index_db_path(config)
    ensure_dir(db_path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{db_path.name}.", suffix=".tmp", dir=db_path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tasks = retained_tasks(config)
        events = retained_events(config)
        with sqlite3.connect(tmp_path) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            initialize_schema(conn)
            counts = populate_index(conn, tasks, events)
            rebuild_at = iso_now()
            write_metadata(conn, counts, source, rebuild_at)
            conn.commit()
        os.replace(tmp_path, db_path)
        report.update(counts)
        report["wrote_db"] = True
        report["last_rebuild_at"] = rebuild_at
        return report
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def build_status_report(config: Config) -> dict[str, Any]:
    db_path = index_db_path(config)
    source = source_counts(config)
    report: dict[str, Any] = {
        "ok": True,
        "db_path": str(db_path),
        "schema_version": None,
        "expected_schema_version": SCHEMA_VERSION,
        "source_task_files": source["task_files"],
        "source_event_files": source["event_files"],
        "source_event_rows": source["event_rows"],
        "retained_tasks": None,
        "retained_events": source["event_rows"],
        "indexed_tasks": None,
        "indexed_events": None,
        "last_rebuild_at": None,
        "warnings": list(source["warnings"]),
    }
    if not db_path.exists():
        report["warnings"].append("index database is missing; JSON/JSONL fallback remains authoritative")
        return report
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            schema_version = read_metadata_int(conn, "schema_version")
            report["schema_version"] = schema_version
            report["last_rebuild_at"] = read_metadata(conn, "last_rebuild_at")
            if schema_version != SCHEMA_VERSION:
                report["warnings"].append(
                    f"index schema mismatch: found {schema_version}, expected {SCHEMA_VERSION}; rebuild required"
                )
                return report
            report["indexed_tasks"] = scalar_count(conn, "tasks")
            report["indexed_events"] = scalar_count(conn, "events")
            add_count_mismatch_warnings(report, conn, config, source)
            add_freshness_warnings(report, config, db_path)
    except sqlite3.DatabaseError as exc:
        report["warnings"].append(
            f"index database is unreadable: {type(exc).__name__}; JSON/JSONL fallback remains authoritative"
        )
    except OSError as exc:
        report["warnings"].append(f"index database cannot be read: {exc}; JSON/JSONL fallback remains authoritative")
    return report


def retained_tasks(config: Config) -> list[dict[str, Any]]:
    ensure_dir(config.queue_dir)
    tasks = []
    for path in sorted(config.queue_dir.glob("*.json")):
        try:
            task = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(task, dict):
            tasks.append(task)
    return tasks


def retained_events(config: Config) -> list[dict[str, Any]]:
    ensure_dir(config.event_dir)
    events: list[dict[str, Any]] = []
    ordinal = 0
    for path in sorted(config.event_dir.glob("*.jsonl")):
        for line_no, event in enumerate(read_jsonl(path), start=1):
            ordinal += 1
            event["_source_ordinal"] = ordinal
            event["_source_line"] = line_no
            events.append(event)
    events.sort(key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("event_id") or ""), int(item["_source_ordinal"])))
    return events


def source_counts(config: Config) -> dict[str, Any]:
    warnings: list[str] = []
    task_files = 0
    event_files = 0
    event_rows = 0
    ensure_dir(config.queue_dir)
    ensure_dir(config.event_dir)
    for path in sorted(config.queue_dir.glob("*.json")):
        task_files += 1
        try:
            parsed = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"skipped unreadable task file {path.name}: {type(exc).__name__}")
            continue
        if not isinstance(parsed, dict):
            warnings.append(f"skipped non-object task file {path.name}")
    for path in sorted(config.event_dir.glob("*.jsonl")):
        event_files += 1
        event_rows += len(read_jsonl(path))
    return {"task_files": task_files, "event_files": event_files, "event_rows": event_rows, "warnings": warnings}


def index_plan_counts(tasks: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "indexed_tasks": len(tasks),
        "indexed_events": len(events),
        "indexed_dependencies": sum(len(list_value(task.get("depends_on"))) for task in tasks),
    }


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE index_metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE tasks (
          task_id TEXT PRIMARY KEY,
          schema_version INTEGER,
          title TEXT,
          description TEXT,
          status TEXT,
          project_id TEXT,
          category TEXT,
          labels_json TEXT NOT NULL,
          created_by TEXT,
          created_at TEXT,
          updated_at TEXT,
          started_at TEXT,
          completed_at TEXT,
          archived_at TEXT,
          attempts INTEGER,
          run_count INTEGER,
          capacity_pool TEXT,
          task_priority TEXT,
          execution_backend TEXT,
          model_requirement_json TEXT NOT NULL,
          routing_size TEXT,
          routing_risk TEXT,
          verification_scope_json TEXT NOT NULL,
          resolution TEXT,
          resolved_at TEXT,
          root_task_id TEXT,
          parent_task_id TEXT,
          subtask_type TEXT,
          subtask_for TEXT,
          blocks_root_completion INTEGER,
          chain_status TEXT
        );

        CREATE TABLE events (
          event_id TEXT PRIMARY KEY,
          schema_version INTEGER,
          event_type TEXT,
          occurred_at TEXT,
          task_id TEXT,
          project_id TEXT,
          actor TEXT,
          source TEXT,
          summary TEXT,
          payload_json TEXT NOT NULL,
          event_line INTEGER
        );

        CREATE TABLE task_dependencies (
          task_id TEXT NOT NULL,
          depends_on TEXT NOT NULL,
          position INTEGER NOT NULL,
          PRIMARY KEY (task_id, depends_on)
        );

        CREATE TABLE task_review_state (
          task_id TEXT PRIMARY KEY,
          review_status TEXT,
          reviewed_at TEXT,
          resolution TEXT,
          resolved_at TEXT,
          chain_status TEXT,
          review_cycle INTEGER,
          review_attempts INTEGER,
          fix_attempts INTEGER,
          last_review_decision TEXT,
          auto_fix_allowed INTEGER,
          last_auto_fix_task_id TEXT,
          last_conflict_fix_task_id TEXT
        );

        CREATE TABLE task_git_metadata (
          task_id TEXT PRIMARY KEY,
          execution_mode TEXT,
          execution_branch TEXT,
          execution_base_ref TEXT,
          execution_base_head TEXT,
          execution_head TEXT,
          execution_apply_status TEXT,
          execution_applied_at TEXT,
          execution_applied_head TEXT,
          execution_cleanup_status TEXT,
          execution_cleanup_kind TEXT,
          execution_rebase_status TEXT,
          execution_conflict_fix_status TEXT,
          git_dirty INTEGER,
          git_ahead INTEGER,
          git_has_unpushed INTEGER
        );

        CREATE INDEX idx_tasks_status ON tasks(status);
        CREATE INDEX idx_tasks_project ON tasks(project_id);
        CREATE INDEX idx_events_task_time ON events(task_id, occurred_at);
        CREATE INDEX idx_events_type_time ON events(event_type, occurred_at);
        """
    )


def populate_index(conn: sqlite3.Connection, tasks: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, int]:
    dep_count = 0
    for task in tasks:
        insert_task(conn, task)
        insert_review_state(conn, task)
        insert_git_metadata(conn, task)
        seen_dependencies = set()
        for position, dep_id in enumerate(list_value(task.get("depends_on"))):
            dep_text = sanitize_scalar(dep_id)
            if dep_text in seen_dependencies:
                continue
            seen_dependencies.add(dep_text)
            conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on, position) VALUES (?, ?, ?)",
                (text_value(task.get("id")), dep_text, position),
            )
            dep_count += 1
    for event in events:
        insert_event(conn, event)
    return {"indexed_tasks": len(tasks), "indexed_events": len(events), "indexed_dependencies": dep_count}


def insert_task(conn: sqlite3.Connection, task: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
          task_id, schema_version, title, description, status, project_id, category,
          labels_json, created_by, created_at, updated_at, started_at, completed_at, archived_at,
          attempts, run_count, capacity_pool, task_priority, execution_backend, model_requirement_json,
          routing_size, routing_risk, verification_scope_json, resolution, resolved_at, root_task_id,
          parent_task_id, subtask_type, subtask_for, blocks_root_completion, chain_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            text_value(task.get("id")),
            int_value(task.get("schema_version")),
            safe_text(task.get("title")),
            safe_text(task.get("description")),
            safe_text(task.get("status")),
            safe_text(task.get("project_id")),
            safe_text(task.get("category")),
            json_dumps_sanitized(list_value(task.get("labels"))),
            safe_text(task.get("created_by")),
            safe_text(task.get("created_at")),
            safe_text(task.get("updated_at")),
            safe_text(task.get("started_at")),
            safe_text(task.get("completed_at")),
            safe_text(task.get("archived_at")),
            int_value(task.get("attempts")),
            int_value(task.get("run_count")),
            safe_text(task.get("capacity_pool")),
            safe_text(task.get("task_priority") or task.get("priority")),
            safe_text(task.get("execution_backend")),
            json_dumps_sanitized(task.get("model_requirement_vector") if isinstance(task.get("model_requirement_vector"), dict) else {}),
            safe_text(task.get("routing_size")),
            safe_text(task.get("routing_risk")),
            json_dumps_sanitized(list_value(task.get("verification_scope"))),
            safe_text(task.get("resolution")),
            safe_text(task.get("resolved_at")),
            safe_text(task.get("root_task_id")),
            safe_text(task.get("parent_task_id")),
            safe_text(task.get("subtask_type")),
            safe_text(task.get("subtask_for")),
            bool_int(task.get("blocks_root_completion")),
            safe_text(task.get("chain_status")),
        ),
    )


def insert_event(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    event_id = text_value(event.get("event_id")) or f"generated-event-{int(event.get('_source_ordinal') or 0)}"
    conn.execute(
        """
        INSERT OR REPLACE INTO events (
          event_id, schema_version, event_type, occurred_at, task_id, project_id,
          actor, source, summary, payload_json, event_line
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            int_value(event.get("schema_version")),
            safe_text(event.get("event_type")),
            safe_text(event.get("occurred_at")),
            safe_text(event.get("task_id")),
            safe_text(event.get("project_id")),
            safe_text(event.get("actor")),
            safe_text(event.get("source")),
            safe_text(event.get("summary")),
            json_dumps_sanitized(sanitize_payload(event.get("payload") or {})),
            int_value(event.get("_source_line")),
        ),
    )


def insert_review_state(conn: sqlite3.Connection, task: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO task_review_state (
          task_id, review_status, reviewed_at, resolution, resolved_at, chain_status, review_cycle,
          review_attempts, fix_attempts, last_review_decision, auto_fix_allowed,
          last_auto_fix_task_id, last_conflict_fix_task_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            text_value(task.get("id")),
            safe_text(task.get("review_status")),
            safe_text(task.get("reviewed_at")),
            safe_text(task.get("resolution")),
            safe_text(task.get("resolved_at")),
            safe_text(task.get("chain_status")),
            int_value(task.get("review_cycle")),
            int_value(task.get("review_attempts")),
            int_value(task.get("fix_attempts")),
            safe_text(task.get("last_review_decision")),
            bool_int(task.get("auto_fix_allowed")),
            safe_text(task.get("last_auto_fix_task_id")),
            safe_text(task.get("last_conflict_fix_task_id")),
        ),
    )


def insert_git_metadata(conn: sqlite3.Connection, task: dict[str, Any]) -> None:
    git_status = task.get("git_status") if isinstance(task.get("git_status"), dict) else {}
    conn.execute(
        """
        INSERT INTO task_git_metadata (
          task_id, execution_mode, execution_branch, execution_base_ref, execution_base_head,
          execution_head, execution_apply_status, execution_applied_at, execution_applied_head,
          execution_cleanup_status, execution_cleanup_kind, execution_rebase_status,
          execution_conflict_fix_status, git_dirty, git_ahead, git_has_unpushed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            text_value(task.get("id")),
            safe_text(task.get("execution_mode")),
            safe_text(task.get("execution_branch")),
            safe_text(task.get("execution_base_ref")),
            safe_text(task.get("execution_base_head")),
            safe_text(task.get("execution_head")),
            safe_text(task.get("execution_apply_status")),
            safe_text(task.get("execution_applied_at")),
            safe_text(task.get("execution_applied_head")),
            safe_text(task.get("execution_cleanup_status")),
            safe_text(task.get("execution_cleanup_kind")),
            safe_text(task.get("execution_rebase_status")),
            safe_text(task.get("execution_conflict_fix_status")),
            bool_int(git_status.get("dirty")),
            int_value(git_status.get("ahead")),
            bool_int(git_status.get("has_unpushed")),
        ),
    )


def write_metadata(conn: sqlite3.Connection, counts: dict[str, int], source: dict[str, Any], rebuild_at: str) -> None:
    values = {
        "schema_version": str(SCHEMA_VERSION),
        "last_rebuild_at": rebuild_at,
        "source_task_files": str(source["task_files"]),
        "source_event_files": str(source["event_files"]),
        "source_event_rows": str(source["event_rows"]),
        "indexed_tasks": str(counts["indexed_tasks"]),
        "indexed_events": str(counts["indexed_events"]),
        "indexed_dependencies": str(counts["indexed_dependencies"]),
    }
    conn.executemany("INSERT INTO index_metadata (key, value) VALUES (?, ?)", sorted(values.items()))


def read_metadata(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM index_metadata WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def read_metadata_int(conn: sqlite3.Connection, key: str) -> int | None:
    value = read_metadata(conn, key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def scalar_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def add_count_mismatch_warnings(
    report: dict[str, Any],
    conn: sqlite3.Connection,
    config: Config,
    source: dict[str, Any],
) -> None:
    current_source_counts = {
        "source_task_files": source["task_files"],
        "source_event_files": source["event_files"],
        "source_event_rows": source["event_rows"],
    }
    for key, current in current_source_counts.items():
        recorded = read_metadata_int(conn, key)
        if recorded is not None and recorded != current:
            report["warnings"].append(
                f"index may be stale: {key} count mismatch "
                f"(current retained source {current}, indexed metadata {recorded})"
            )

    table_counts = {
        "indexed_tasks": report["indexed_tasks"],
        "indexed_events": report["indexed_events"],
        "indexed_dependencies": scalar_count(conn, "task_dependencies"),
    }
    for key, current in table_counts.items():
        recorded = read_metadata_int(conn, key)
        if recorded is not None and recorded != current:
            report["warnings"].append(
                f"index metadata mismatch: {key} recorded as {recorded}, SQLite table contains {current}"
            )

    retained_task_count = len(retained_tasks(config))
    retained_event_count = source["event_rows"]
    report["retained_tasks"] = retained_task_count
    report["retained_events"] = retained_event_count
    if report["indexed_tasks"] != retained_task_count:
        report["warnings"].append(
            "index may be stale: retained task count mismatch "
            f"(current retained tasks {retained_task_count}, indexed table {report['indexed_tasks']})"
        )
    if report["indexed_events"] != retained_event_count:
        report["warnings"].append(
            "index may be stale: retained event row count mismatch "
            f"(current retained event rows {retained_event_count}, indexed table {report['indexed_events']})"
        )


def add_freshness_warnings(report: dict[str, Any], config: Config, db_path: Path) -> None:
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        return
    for root, pattern, label in ((config.queue_dir, "*.json", "task"), (config.event_dir, "*.jsonl", "event")):
        try:
            stale = any(path.stat().st_mtime > db_mtime for path in root.glob(pattern))
        except OSError:
            stale = True
        if stale:
            report["warnings"].append(f"index may be stale: retained {label} files changed after last rebuild")
            return


def render_rebuild_report(report: dict[str, Any]) -> str:
    lines = [
        f"mode: {report['mode']}",
        f"db_path: {report['db_path']}",
        f"schema_version: {report['schema_version']}",
        f"source_task_files: {report['source_task_files']}",
        f"source_event_files: {report['source_event_files']}",
        f"source_event_rows: {report['source_event_rows']}",
        f"indexed_tasks: {report['indexed_tasks']}",
        f"indexed_events: {report['indexed_events']}",
        f"indexed_dependencies: {report['indexed_dependencies']}",
        f"wrote_db: {str(report['wrote_db']).lower()}",
    ]
    if report.get("last_rebuild_at"):
        lines.append(f"last_rebuild_at: {report['last_rebuild_at']}")
    for warning in report.get("warnings", []):
        lines.append(f"warning: {warning}")
    return "\n".join(lines) + "\n"


def render_status_report(report: dict[str, Any]) -> str:
    lines = [
        f"db_path: {report['db_path']}",
        f"schema_version: {report.get('schema_version') or '-'}",
        f"expected_schema_version: {report['expected_schema_version']}",
        f"source_task_files: {report['source_task_files']}",
        f"source_event_files: {report['source_event_files']}",
        f"source_event_rows: {report['source_event_rows']}",
        f"retained_tasks: {dash(report.get('retained_tasks'))}",
        f"retained_events: {dash(report.get('retained_events'))}",
        f"indexed_tasks: {dash(report.get('indexed_tasks'))}",
        f"indexed_events: {dash(report.get('indexed_events'))}",
        f"last_rebuild_at: {report.get('last_rebuild_at') or '-'}",
    ]
    for warning in report.get("warnings", []):
        lines.append(f"warning: {warning}")
    return "\n".join(lines) + "\n"


def safe_text(value: Any) -> str | None:
    if value is None:
        return None
    return text_value(sanitize_scalar(value))


def text_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return str(sanitize_payload(value))


def int_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def bool_int(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def json_dumps_sanitized(value: Any) -> str:
    return json.dumps(sanitize_payload(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def dash(value: Any) -> str:
    return "-" if value is None else str(value)

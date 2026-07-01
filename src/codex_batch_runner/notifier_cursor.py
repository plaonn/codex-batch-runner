from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

DEFAULT_RECENT_EVENT_IDS_LIMIT = 200


@dataclass(frozen=True)
class NotifierCursorState:
    current_event_file: Path
    current_byte_offset: int
    recent_event_ids: tuple[str, ...] = ()
    last_processed_event_id: str | None = None
    last_processed_occurred_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "current_event_file": str(self.current_event_file),
            "current_byte_offset": self.current_byte_offset,
        }
        if self.recent_event_ids:
            data["recent_event_ids"] = list(self.recent_event_ids)
        if self.last_processed_event_id:
            data["last_processed_event_id"] = self.last_processed_event_id
        if self.last_processed_occurred_at:
            data["last_processed_occurred_at"] = self.last_processed_occurred_at
        return data


@dataclass(frozen=True)
class NotifierCursorLoadResult:
    cursor_path: Path
    event_dir: Path
    valid: bool
    warnings: tuple[str, ...]
    state: NotifierCursorState | None = None


@dataclass(frozen=True)
class NotifierEventDecision:
    event: dict[str, Any]
    duplicate_key: str
    duplicate: bool


@dataclass(frozen=True)
class NotifierAdvancePlan:
    state: NotifierCursorState
    bytes_consumed: int
    decisions: tuple[NotifierEventDecision, ...]
    warnings: tuple[str, ...] = ()

    @property
    def events_to_notify(self) -> tuple[dict[str, Any], ...]:
        return tuple(decision.event for decision in self.decisions if not decision.duplicate)


def load_notifier_cursor_state(
    cursor_path: Path,
    event_dir: Path,
    *,
    recent_event_ids_limit: int = DEFAULT_RECENT_EVENT_IDS_LIMIT,
) -> NotifierCursorLoadResult:
    resolved_cursor_path = cursor_path.expanduser().resolve(strict=False)
    resolved_event_dir = event_dir.expanduser().resolve(strict=False)
    warnings: list[str] = []

    if not resolved_cursor_path.exists():
        return invalid_cursor(resolved_cursor_path, resolved_event_dir, f"notifier cursor state missing: {resolved_cursor_path}")
    try:
        with resolved_cursor_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception as exc:
        return invalid_cursor(
            resolved_cursor_path,
            resolved_event_dir,
            f"notifier cursor state unreadable: {resolved_cursor_path}: {exc}",
        )
    if not isinstance(data, dict):
        return invalid_cursor(
            resolved_cursor_path,
            resolved_event_dir,
            f"notifier cursor state malformed: {resolved_cursor_path}: root must be an object",
        )

    state = parse_notifier_cursor_state(
        data,
        resolved_cursor_path,
        resolved_event_dir,
        warnings,
        recent_event_ids_limit=recent_event_ids_limit,
    )
    return NotifierCursorLoadResult(
        cursor_path=resolved_cursor_path,
        event_dir=resolved_event_dir,
        valid=state is not None and not warnings,
        warnings=tuple(warnings),
        state=state if not warnings else None,
    )


def parse_notifier_cursor_state(
    data: dict[str, Any],
    cursor_path: Path,
    event_dir: Path,
    warnings: list[str],
    *,
    recent_event_ids_limit: int = DEFAULT_RECENT_EVENT_IDS_LIMIT,
) -> NotifierCursorState | None:
    current_event_file = cursor_file_value(data, "current_event_file", cursor_path, event_dir, warnings)
    current_byte_offset = cursor_offset_value(data, "current_byte_offset", cursor_path, warnings)
    recent_event_ids = cursor_recent_event_ids(data, cursor_path, warnings, recent_event_ids_limit=recent_event_ids_limit)
    last_processed_event_id = optional_text_value(data, "last_processed_event_id", cursor_path, warnings)
    last_processed_occurred_at = optional_text_value(data, "last_processed_occurred_at", cursor_path, warnings)

    if current_event_file is None:
        warnings.append(f"notifier cursor state malformed: {cursor_path}: current_event_file is required")
    if current_byte_offset is None:
        warnings.append(f"notifier cursor state malformed: {cursor_path}: current_byte_offset is required")
    if current_event_file is None or current_byte_offset is None:
        return None

    return NotifierCursorState(
        current_event_file=current_event_file,
        current_byte_offset=current_byte_offset,
        recent_event_ids=recent_event_ids,
        last_processed_event_id=last_processed_event_id,
        last_processed_occurred_at=last_processed_occurred_at,
    )


def plan_advance_for_records(
    state: NotifierCursorState,
    event_file: Path,
    records: list[dict[str, Any]],
    *,
    bytes_consumed: int,
    recent_event_ids_limit: int = DEFAULT_RECENT_EVENT_IDS_LIMIT,
) -> NotifierAdvancePlan:
    if bytes_consumed < 0:
        raise ValueError("bytes_consumed must be non-negative")

    resolved_event_file = event_file.expanduser().resolve(strict=False)
    current_keys = list(state.recent_event_ids)
    seen = set(current_keys)
    decisions: list[NotifierEventDecision] = []
    last_processed_event_id = state.last_processed_event_id
    last_processed_occurred_at = state.last_processed_occurred_at

    for record in records:
        key = duplicate_key(record)
        duplicate = key in seen
        decisions.append(NotifierEventDecision(event=record, duplicate_key=key, duplicate=duplicate))
        if not duplicate:
            seen.add(key)
            current_keys.append(key)
            event_id = text_value(record.get("event_id"))
            occurred_at = text_value(record.get("occurred_at"))
            if event_id:
                last_processed_event_id = event_id
            if occurred_at:
                last_processed_occurred_at = occurred_at

    bounded_recent_keys = tuple(current_keys[-recent_event_ids_limit:]) if recent_event_ids_limit > 0 else ()
    next_state = NotifierCursorState(
        current_event_file=resolved_event_file,
        current_byte_offset=state.current_byte_offset + bytes_consumed,
        recent_event_ids=bounded_recent_keys,
        last_processed_event_id=last_processed_event_id,
        last_processed_occurred_at=last_processed_occurred_at,
    )
    return NotifierAdvancePlan(state=next_state, bytes_consumed=bytes_consumed, decisions=tuple(decisions))


def plan_advance_from_jsonl_bytes(
    state: NotifierCursorState,
    event_file: Path,
    data: bytes,
    *,
    recent_event_ids_limit: int = DEFAULT_RECENT_EVENT_IDS_LIMIT,
) -> NotifierAdvancePlan:
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    bytes_consumed = complete_jsonl_byte_count(data)
    consumed = data[:bytes_consumed]
    offset = 0
    for line in consumed.splitlines(keepends=True):
        offset += len(line)
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            warnings.append(f"notifier event JSONL line ignored at byte {state.current_byte_offset + offset}: {exc}")
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            warnings.append(f"notifier event JSONL line ignored at byte {state.current_byte_offset + offset}: root must be an object")

    plan = plan_advance_for_records(
        state,
        event_file,
        records,
        bytes_consumed=bytes_consumed,
        recent_event_ids_limit=recent_event_ids_limit,
    )
    return NotifierAdvancePlan(
        state=plan.state,
        bytes_consumed=plan.bytes_consumed,
        decisions=plan.decisions,
        warnings=tuple(warnings),
    )


def read_jsonl_bytes_from_cursor(state: NotifierCursorState) -> bytes:
    with state.current_event_file.open("rb") as file:
        file.seek(state.current_byte_offset)
        return file.read()


def duplicate_key(event: dict[str, Any]) -> str:
    event_id = text_value(event.get("event_id"))
    if event_id:
        return f"event_id:{event_id}"
    return "fallback:{event_type}:{task_id}:{occurred_at}".format(
        event_type=text_value(event.get("event_type")) or "",
        task_id=text_value(event.get("task_id")) or "",
        occurred_at=text_value(event.get("occurred_at")) or "",
    )


def complete_jsonl_byte_count(data: bytes) -> int:
    if not data:
        return 0
    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return 0
    return last_newline + 1


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


def cursor_recent_event_ids(
    data: dict[str, Any],
    cursor_path: Path,
    warnings: list[str],
    *,
    recent_event_ids_limit: int,
) -> tuple[str, ...]:
    value = data.get("recent_event_ids")
    if value is None:
        return ()
    if not isinstance(value, list):
        warnings.append(f"notifier cursor state malformed: {cursor_path}: recent_event_ids must be a list")
        return ()
    event_ids: list[str] = []
    for item in value:
        text = text_value(item)
        if text is None:
            warnings.append(f"notifier cursor state malformed: {cursor_path}: recent_event_ids entries must be strings")
            continue
        event_ids.append(text)
    return tuple(event_ids[-recent_event_ids_limit:]) if recent_event_ids_limit > 0 else ()


def optional_text_value(data: dict[str, Any], key: str, cursor_path: Path, warnings: list[str]) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    text = text_value(value)
    if text is None:
        warnings.append(f"notifier cursor state malformed: {cursor_path}: {key} must be a string")
        return None
    return text


def text_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def invalid_cursor(cursor_path: Path, event_dir: Path, warning: str) -> NotifierCursorLoadResult:
    return NotifierCursorLoadResult(
        cursor_path=cursor_path,
        event_dir=event_dir,
        valid=False,
        warnings=(warning,),
        state=None,
    )


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True

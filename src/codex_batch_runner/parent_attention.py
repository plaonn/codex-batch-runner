from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .events import sanitize_payload, sanitize_scalar, write_event_nonfatal
from .fs import ensure_dir, read_json, write_json_atomic
from .timeutil import iso_now

SCHEMA_VERSION = 1
WAKE_REASONS = {"needs_review", "needs_decision", "needs_follow_up", "blocked_external", "completed"}
DELIVERY_STATES = {"pending", "retry_wait", "delivered", "acknowledged", "unavailable", "failed"}


@dataclass(frozen=True)
class DeliveryResult:
    event_id: str
    state: str
    attempted: bool
    message: str


def stable_event_id(parent_ref: str, work_item_ref: str, completion_id: str, wake_reason: str) -> str:
    raw = "\0".join((parent_ref, work_item_ref, completion_id, wake_reason)).encode()
    return "pa-" + hashlib.sha256(raw).hexdigest()[:32]


def create_parent_attention(
    config: Config,
    *,
    parent_ref: str | None,
    work_item_ref: str,
    completion_id: str,
    wake_reason: str,
    summary: str,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any] | None:
    if not parent_ref:
        return None
    if wake_reason not in WAKE_REASONS:
        raise ValueError(f"invalid parent attention wake_reason: {wake_reason}")
    event_id = stable_event_id(parent_ref, work_item_ref, completion_id, wake_reason)
    path = outbox_path(config, event_id)
    existing = read_json(path, None)
    if isinstance(existing, dict):
        return existing
    now = iso_now()
    record = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "parent_attention_required",
        "event_id": event_id,
        "parent_ref": sanitize_scalar(parent_ref),
        "work_item_ref": sanitize_scalar(work_item_ref),
        "completion_id": sanitize_scalar(completion_id),
        "wake_reason": wake_reason,
        "result": sanitize_payload({"summary": summary, "evidence_refs": evidence_refs or []}),
        "delivery": {
            "state": "pending",
            "attempts": 0,
            "max_attempts": config.parent_attention_max_attempts,
            "next_attempt_at": now,
            "last_attempt_at": None,
            "last_error": None,
            "delivered_at": None,
            "acknowledged_at": None,
        },
        "created_at": now,
        "updated_at": now,
    }
    ensure_dir(outbox_dir(config))
    write_json_atomic(path, record)
    write_event_nonfatal(
        config, "parent_attention_required", source="parent-attention-outbox",
        summary=f"parent attention required: {wake_reason}",
        payload={"parent_attention_event_id": event_id, "work_item_ref": work_item_ref, "wake_reason": wake_reason},
    )
    return record


def deliver_parent_attention(config: Config, event_id: str, *, now: datetime | None = None) -> DeliveryResult:
    record = load_parent_attention(config, event_id)
    delivery = record["delivery"]
    if delivery["state"] in {"delivered", "acknowledged"}:
        return DeliveryResult(event_id, delivery["state"], False, "already delivered")
    current = now or datetime.now(timezone.utc)
    next_at = parse_time(delivery.get("next_attempt_at"))
    if next_at and next_at > current:
        return DeliveryResult(event_id, delivery["state"], False, "retry backoff active")
    if not config.parent_attention_delivery_command:
        update_delivery(config, record, state="unavailable", error="delivery command is not configured")
        return DeliveryResult(event_id, "unavailable", False, "delivery command is not configured")
    attempts = int(delivery.get("attempts", 0)) + 1
    try:
        result = subprocess.run(
            config.parent_attention_delivery_command,
            input=json.dumps(delivery_payload(record), ensure_ascii=False), text=True,
            capture_output=True, timeout=config.parent_attention_delivery_timeout_seconds, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"adapter exited {result.returncode}")
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        terminal = attempts >= config.parent_attention_max_attempts
        state = "failed" if terminal else "retry_wait"
        delay = config.parent_attention_retry_base_seconds * (2 ** max(0, attempts - 1))
        update_delivery(
            config, record, state=state, attempts=attempts, error=str(exc),
            next_attempt_at=None if terminal else (current + timedelta(seconds=delay)).isoformat(),
        )
        return DeliveryResult(event_id, state, True, str(exc))
    update_delivery(config, record, state="delivered", attempts=attempts, delivered_at=current.isoformat(), error=None)
    return DeliveryResult(event_id, "delivered", True, "delivered; acknowledgement pending")


def acknowledge_parent_attention(config: Config, event_id: str) -> dict[str, Any]:
    record = load_parent_attention(config, event_id)
    if record["delivery"]["state"] not in {"delivered", "acknowledged"}:
        raise ValueError("parent attention event must be delivered before acknowledgement")
    update_delivery(config, record, state="acknowledged", acknowledged_at=iso_now(), error=None)
    return record


def list_parent_attention(config: Config) -> list[dict[str, Any]]:
    ensure_dir(outbox_dir(config))
    records = [read_json(path, None) for path in sorted(outbox_dir(config).glob("pa-*.json"))]
    return [record for record in records if isinstance(record, dict)]


def load_parent_attention(config: Config, event_id: str) -> dict[str, Any]:
    record = read_json(outbox_path(config, event_id), None)
    if not isinstance(record, dict):
        raise ValueError(f"parent attention event not found: {event_id}")
    return record


def outbox_path(config: Config, event_id: str) -> Path:
    if not event_id.startswith("pa-") or not event_id[3:].isalnum():
        raise ValueError("invalid parent attention event id")
    return outbox_dir(config) / f"{event_id}.json"


def outbox_dir(config: Config) -> Path:
    return config.parent_attention_outbox_dir or (config.log_dir.parent / "parent-attention-outbox")


def update_delivery(config: Config, record: dict[str, Any], *, state: str, attempts: int | None = None, error: str | None = None, next_attempt_at: str | None = None, delivered_at: str | None = None, acknowledged_at: str | None = None) -> None:
    if state not in DELIVERY_STATES:
        raise ValueError(f"invalid delivery state: {state}")
    delivery = record["delivery"]
    delivery["state"] = state
    if attempts is not None:
        delivery["attempts"] = attempts
        delivery["last_attempt_at"] = iso_now()
    delivery["last_error"] = sanitize_scalar(error) if error else None
    delivery["next_attempt_at"] = next_attempt_at
    if delivered_at is not None:
        delivery["delivered_at"] = delivered_at
    if acknowledged_at is not None:
        delivery["acknowledged_at"] = acknowledged_at
    record["updated_at"] = iso_now()
    write_json_atomic(outbox_path(config, record["event_id"]), record)
    write_event_nonfatal(
        config, "parent_attention_delivery_state", source="parent-attention-outbox",
        summary=f"parent attention delivery {state}",
        payload={"parent_attention_event_id": record["event_id"], "delivery_state": state, "attempts": delivery["attempts"]},
    )


def delivery_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("schema_version", "event_type", "event_id", "parent_ref", "work_item_ref", "completion_id", "wake_reason", "result")}


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

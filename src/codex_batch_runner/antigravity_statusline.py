"""Private, read-only projection of Antigravity statusLine quota JSON.

This module intentionally does not know where a statusLine document lives and
never invokes a command or network client.  Callers must supply already-read
JSON, then may persist only the projection using the bounded cache helpers.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from .fs import write_json_atomic


ANTIGRAVITY_STATUSLINE_CACHE_CONTRACT = "antigravity-statusline-cache-v1"
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}$")
_MAX_BUCKETS = 16


def collect_statusline_quota(
    value: object,
    *,
    collected_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a non-throwing, strict allowlist projection of statusLine JSON.

    The official source shape accepted here is ``version``, optional
    ``plan_tier``, and a ``quota`` map keyed by model/bucket ID. Each value
    may contain only ``remaining_fraction``, ``reset_time``, and
    ``reset_in_seconds``. All other input is deliberately discarded.
    """
    if not isinstance(value, dict):
        return _failure("invalid", "source_not_object")

    quota = value.get("quota")
    presence = {
        "version": "version" in value,
        "plan_tier": "plan_tier" in value,
        "quota": isinstance(quota, dict),
    }
    version = _safe_label(value.get("version"))
    plan_tier = _safe_label(value.get("plan_tier"))
    fingerprint = _format_fingerprint(value)
    collected = _aware_timestamp(collected_at) or datetime.now(timezone.utc)
    if not isinstance(quota, dict) or not quota or len(quota) > _MAX_BUCKETS:
        return _failure(
            "invalid",
            "quota_map_invalid",
            version=version,
            field_presence=presence,
            format_fingerprint=fingerprint,
        )

    projected_buckets: list[dict[str, Any]] = []
    for bucket_id in sorted(quota):
        projected = _project_bucket(bucket_id, quota.get(bucket_id))
        if projected is None:
            return _failure(
                "invalid",
                "quota_bucket_invalid",
                version=version,
                field_presence=presence,
                format_fingerprint=fingerprint,
            )
        projected_buckets.append(projected)

    return {
        "status": "observed",
        "reason": None,
        "cache": {
            "schema_version": 1,
            "contract": ANTIGRAVITY_STATUSLINE_CACHE_CONTRACT,
            "source_version": version,
            "field_presence": presence,
            "format_fingerprint": fingerprint,
            "collected_at": collected.isoformat(),
            "timestamp_provenance": "statusline_callback_received_at",
            "freshness_authority": "advisory_only",
            "plan_tier": plan_tier,
            "buckets": projected_buckets,
        },
    }


def write_statusline_cache(cache_path: str | Path, collection: dict[str, Any]) -> bool:
    """Atomically persist an observed projection; return false on any failure."""
    try:
        cache = collection.get("cache") if isinstance(collection, dict) else None
        if collection.get("status") != "observed" or not _is_cache(cache):
            return False
        write_json_atomic(Path(cache_path), cache)
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_statusline_cache(cache_path: str | Path, *, max_bytes: int = 65_536) -> dict[str, Any] | None:
    """Read one bounded, previously projected cache record, never raw source."""
    try:
        path = Path(cache_path)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            return None
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        cache = json.loads(raw.decode("utf-8"))
        return cache if _is_cache(cache) else None
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None


def _project_bucket(bucket_id: object, value: object) -> dict[str, Any] | None:
    safe_bucket_id = _safe_label(bucket_id)
    if safe_bucket_id is None or not isinstance(value, dict):
        return None
    remaining = value.get("remaining_fraction")
    if not _fraction(remaining):
        return None
    reset_time = value.get("reset_time")
    if reset_time is not None and not _reset_time(reset_time):
        return None
    reset_in_seconds = value.get("reset_in_seconds")
    if reset_in_seconds is not None and not _seconds(reset_in_seconds):
        return None
    # Do not calculate a duration from reset_in_seconds. It is only a source
    # field, and stays distinct from a quota window duration.
    return {
        "bucket_id": safe_bucket_id,
        "remaining_fraction": remaining,
        "reset_time": reset_time,
        "reset_in_seconds": reset_in_seconds,
    }


def _is_projected_bucket(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "bucket_id", "remaining_fraction", "reset_time", "reset_in_seconds"
    }:
        return False
    return (
        _safe_label(value.get("bucket_id")) is not None
        and _fraction(value.get("remaining_fraction"))
        and (value.get("reset_time") is None or _reset_time(value.get("reset_time")))
        and (value.get("reset_in_seconds") is None or _seconds(value.get("reset_in_seconds")))
    )


def _is_cache(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "contract", "source_version", "field_presence",
        "format_fingerprint", "collected_at", "timestamp_provenance",
        "freshness_authority", "plan_tier", "buckets"
    }:
        return False
    if value.get("schema_version") != 1 or value.get("contract") != ANTIGRAVITY_STATUSLINE_CACHE_CONTRACT:
        return False
    if value.get("source_version") is not None and not _safe_label(value.get("source_version")):
        return False
    if value.get("plan_tier") is not None and not _safe_label(value.get("plan_tier")):
        return False
    presence = value.get("field_presence")
    if not isinstance(presence, dict) or set(presence) != {"version", "plan_tier", "quota"}:
        return False
    if not all(isinstance(item, bool) for item in presence.values()):
        return False
    fingerprint = value.get("format_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return False
    if _aware_timestamp(value.get("collected_at")) is None:
        return False
    if value.get("timestamp_provenance") != "statusline_callback_received_at":
        return False
    if value.get("freshness_authority") != "advisory_only":
        return False
    buckets = value.get("buckets")
    return isinstance(buckets, list) and len(buckets) <= _MAX_BUCKETS and all(_is_projected_bucket(item) for item in buckets)


def _failure(
    status: str,
    reason: str,
    *,
    version: str | None = None,
    field_presence: dict[str, bool] | None = None,
    format_fingerprint: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "cache": None,
        "source_version": version,
        "field_presence": field_presence,
        "format_fingerprint": format_fingerprint,
    }


def _format_fingerprint(value: dict[str, Any]) -> str:
    quota = value.get("quota")
    quota_values = list(quota.values()) if isinstance(quota, dict) else []
    shape = {
        "top_level": sorted(key for key in ("version", "plan_tier", "quota") if key in value),
        "quota_is_map": isinstance(quota, dict),
        "bucket_count": len(quota) if isinstance(quota, dict) else None,
        "bucket_fields": sorted({
            key
            for item in quota_values
            if isinstance(item, dict)
            for key in item
            if key in {"remaining_fraction", "reset_time", "reset_in_seconds"}
        }),
    }
    return sha256(json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _safe_label(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_LABEL.fullmatch(value) else None


def _fraction(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def _seconds(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 31_536_000


def _reset_time(value: object) -> bool:
    if isinstance(value, str):
        if len(value) > 64:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _aware_timestamp(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)

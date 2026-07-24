"""Bounded, advisory-only capacity reads from the public Codex app-server RPC.

This module is intentionally not wired to CBR routing or the public provider-resource
contracts.  It sends one ``account/rateLimits/read`` request, never starts a model
turn, and projects only rate-limit window values that are safe to retain.
"""

from __future__ import annotations

import json
import math
import os
import re
import select
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


RPC_METHOD = "account/rateLimits/read"
CONTRACT = "codex-app-server-capacity-v1"
ADAPTER_ID = CONTRACT
MAX_TIMEOUT_SECONDS = 10
MAX_OUTPUT_BYTES = 65_536
_WINDOW_IDS = ("primary", "secondary")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
_INITIALIZE_ID = 1
_RATE_LIMITS_ID = 2


def acquire_capacity(
    argv: Sequence[str],
    *,
    timeout_seconds: float = 3.0,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    evaluated_at: datetime | None = None,
    fallback: Mapping[str, Any] | None = None,
    fallback_max_age_seconds: int | None = None,
) -> dict[str, Any]:
    """Issue one bounded stdio JSON-RPC read and return a sanitized projection.

    ``fallback`` is an earlier projection, never a credential-bearing raw response.
    It is retained only as explicitly labelled advisory context when the new read is
    unavailable; it can never make the new result observed.
    """
    baseline = _aware_utc(evaluated_at) or datetime.now(timezone.utc)
    if not _valid_argv(argv) or not _valid_timeout(timeout_seconds) or not _valid_output_limit(max_output_bytes):
        return _result("unknown", "invalid_request", baseline, fallback, fallback_max_age_seconds)

    messages = (
        {
            "id": _INITIALIZE_ID,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "codex-batch-runner-capacity", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized"},
        {"id": _RATE_LIMITS_ID, "method": RPC_METHOD, "params": None},
    )
    request = b"".join(
        json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        for message in messages
    )
    try:
        process = subprocess.Popen(
            list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError:
        return _result("unavailable", "app_server_unavailable", baseline, fallback, fallback_max_age_seconds)

    try:
        if process.stdin is None or process.stdout is None:
            return _result("unavailable", "app_server_unavailable", baseline, fallback, fallback_max_age_seconds)
        process.stdin.write(request)
        process.stdin.flush()
        response, read_reason = _read_bounded(process, timeout_seconds, max_output_bytes)
        if read_reason is not None:
            status = "unknown" if read_reason == "malformed_response" else "unavailable"
            return _result(status, read_reason, baseline, fallback, fallback_max_age_seconds)
    finally:
        _stop(process)

    if response is None:
        return _result("unknown", "malformed_response", baseline, fallback, fallback_max_age_seconds)
    error = response.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        return _result("unknown", "method_not_found" if code == -32601 else "rpc_error", baseline, fallback, fallback_max_age_seconds)
    projected = project_rate_limits_response(response.get("result"), evaluated_at=baseline)
    if projected is None:
        return _result("unknown", "malformed_response", baseline, fallback, fallback_max_age_seconds)
    return projected


def project_rate_limits_response(result: object, *, evaluated_at: datetime | None = None) -> dict[str, Any] | None:
    """Project a successful RPC result without retaining account or raw payload data."""
    baseline = _aware_utc(evaluated_at) or datetime.now(timezone.utc)
    if not isinstance(result, dict):
        return None
    resources: list[dict[str, Any]] = []
    seen_limit_ids: set[str] = set()
    by_limit_id = result.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict):
        for raw_limit_id in sorted(by_limit_id):
            projected = _project_resource(by_limit_id.get(raw_limit_id), fallback_limit_id=raw_limit_id)
            if projected is None:
                continue
            resources.append(projected)
            if projected["limit_id"] is not None:
                seen_limit_ids.add(projected["limit_id"])
    legacy = result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else result
    projected_legacy = _project_resource(legacy)
    if projected_legacy is not None and (
        projected_legacy["limit_id"] is None
        or projected_legacy["limit_id"] not in seen_limit_ids
    ):
        resources.append(projected_legacy)
    if not resources:
        return None
    return {
        "contract": CONTRACT,
        "adapter_id": ADAPTER_ID,
        "read_only": True,
        "method": RPC_METHOD,
        "status": (
            "observed"
            if any(window["status"] == "observed" for resource in resources for window in resource["windows"])
            else "unknown"
        ),
        "reason": None,
        "collected_at": baseline.isoformat(),
        "timestamp_provenance": "adapter_response_received_at",
        "freshness_authority": "advisory_only",
        "resources": resources,
        "advisory_fallback": {"status": "not_used", "windows": []},
    }


def _project_resource(value: object, *, fallback_limit_id: object = None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    windows = [_project_window(slot, value.get(slot)) for slot in _WINDOW_IDS]
    windows = [window for window in windows if window is not None]
    if not windows:
        return None
    return {
        "limit_id": _safe_label(value.get("limitId")) or _safe_label(fallback_limit_id),
        "plan_type": _safe_label(value.get("planType")),
        "windows": windows,
    }


def _project_window(window_id: str, raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    duration_minutes = _number(raw.get("windowDurationMins", raw.get("window_minutes")))
    used_percent = _number(raw.get("usedPercent", raw.get("used_percent")))
    resets_at = _timestamp(raw.get("resetsAt", raw.get("resets_at")))
    if duration_minutes is None or duration_minutes <= 0:
        return {"window_id": window_id, "status": "unknown", "window_duration_seconds": None, "used_ratio": None, "remaining_ratio": None, "resets_at": None}
    if used_percent is None or not 0 <= used_percent <= 100:
        return {"window_id": window_id, "status": "unknown", "window_duration_seconds": duration_minutes * 60, "used_ratio": None, "remaining_ratio": None, "resets_at": resets_at}
    used_ratio = used_percent / 100
    return {
        "window_id": window_id,
        "status": "observed",
        "window_duration_seconds": duration_minutes * 60,
        "used_ratio": used_ratio,
        "remaining_ratio": 1 - used_ratio,
        "resets_at": resets_at,
    }


def _read_bounded(
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
    max_output_bytes: int,
) -> tuple[dict[str, Any] | None, str | None]:
    assert process.stdout is not None
    fd = process.stdout.fileno()
    deadline = time.monotonic() + timeout_seconds
    pending = b""
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "timeout"
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            return None, "timeout"
        chunk = os.read(fd, min(4096, max_output_bytes + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > max_output_bytes:
            return None, "output_limit"
        pending += chunk
        while b"\n" in pending:
            raw_line, pending = pending.split(b"\n", 1)
            try:
                item = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None, "malformed_response"
            if isinstance(item, dict) and item.get("id") == _RATE_LIMITS_ID:
                return item, None
        if total >= max_output_bytes:
            return None, "output_limit"
    return None, None


def _result(status: str, reason: str, baseline: datetime, fallback: Mapping[str, Any] | None, max_age: int | None) -> dict[str, Any]:
    fallback_status, windows = _advisory_fallback(fallback, baseline, max_age)
    return {
        "contract": CONTRACT,
        "adapter_id": ADAPTER_ID,
        "read_only": True,
        "method": RPC_METHOD,
        "status": status,
        "reason": reason,
        "collected_at": baseline.isoformat(),
        "timestamp_provenance": "adapter_response_received_at",
        "freshness_authority": "advisory_only",
        "resources": [],
        "advisory_fallback": {"status": fallback_status, "windows": windows},
    }


def _advisory_fallback(fallback: Mapping[str, Any] | None, baseline: datetime, max_age: int | None) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(fallback, Mapping) or not isinstance(fallback.get("windows"), list):
        return "unavailable", []
    observed = _aware_utc(fallback.get("collected_at") or fallback.get("observed_at"))
    valid_age = isinstance(max_age, int) and not isinstance(max_age, bool) and max_age >= 0
    status = "fresh" if observed and valid_age and baseline <= observed + timedelta(seconds=max_age) else "stale"
    return status, _safe_fallback_windows(fallback["windows"])


def _safe_fallback_windows(value: list[Any]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    allowed = {"window_id", "status", "window_duration_seconds", "used_ratio", "remaining_ratio", "resets_at"}
    for item in value:
        if not isinstance(item, dict) or item.get("window_id") not in _WINDOW_IDS or set(item) - allowed:
            continue
        duration = _number(item.get("window_duration_seconds"))
        used = _number(item.get("used_ratio")) if item.get("used_ratio") is not None else None
        remaining = _number(item.get("remaining_ratio")) if item.get("remaining_ratio") is not None else None
        reset = _timestamp(item.get("resets_at")) if item.get("resets_at") is not None else None
        if item.get("status") not in {"observed", "unknown"} or (duration is not None and duration <= 0):
            continue
        if (item.get("used_ratio") is not None and used is None) or (item.get("remaining_ratio") is not None and remaining is None) or (item.get("resets_at") is not None and reset is None):
            continue
        if item.get("status") == "observed" and (duration is None or used is None or remaining is None):
            continue
        if (used is not None and not 0 <= used <= 1) or (remaining is not None and not 0 <= remaining <= 1):
            continue
        windows.append({"window_id": item["window_id"], "status": item["status"], "window_duration_seconds": duration, "used_ratio": used, "remaining_ratio": remaining, "resets_at": reset})
    return windows


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.2)
    if process.stdin is not None:
        process.stdin.close()
    if process.stdout is not None:
        process.stdout.close()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _timestamp(value: object) -> str | None:
    parsed = _aware_utc(value)
    return parsed.isoformat() if parsed else None


def _aware_utc(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _valid_argv(argv: Sequence[str]) -> bool:
    return (
        not isinstance(argv, (str, bytes))
        and bool(argv)
        and all(isinstance(part, str) and part for part in argv)
    )


def _valid_timeout(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= MAX_TIMEOUT_SECONDS


def _valid_output_limit(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_OUTPUT_BYTES


def _safe_label(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_LABEL.fullmatch(value) else None

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import Config
from .events import write_event_nonfatal
from .timeutil import parse_time, utc_now


@dataclass(frozen=True)
class UsageAdmissionDecision:
    status: str
    reason: str
    cooldown_until: datetime | None = None
    observed_at: datetime | None = None
    reset_at: datetime | None = None
    gate_windows: tuple[str, ...] = ()
    short_window_remaining_percent: float | None = None
    long_window_remaining_percent: float | None = None


def check_usage_admission(
    config: Config,
    *,
    now: datetime | None = None,
) -> UsageAdmissionDecision:
    if not config.usage_admission_enabled:
        return UsageAdmissionDecision("allowed", "disabled")

    snapshot, error = read_usage_snapshot(config)
    if error:
        return fail_open(config, error)
    if not snapshot or snapshot.get("available") is False:
        return fail_open(config, "snapshot_unavailable")
    if "available" in snapshot and snapshot.get("available") is not True:
        return fail_open(config, "snapshot_available_invalid")

    baseline = now or utc_now()
    observed_at = snapshot_time(snapshot, "observed_at", "generated_at")
    if not observed_at:
        return fail_open(config, "snapshot_observation_time_invalid")
    if observed_at > baseline + timedelta(seconds=60):
        return fail_open(config, "snapshot_observation_time_invalid")

    short_window, long_window = semantic_windows(snapshot)
    short_remaining = percentage(short_window.get("remaining_percent"))
    long_remaining = percentage(long_window.get("remaining_percent"))
    short_threshold = config.usage_admission_short_window_threshold_percent

    if short_remaining is None:
        return fail_open(config, "short_window_remaining_percent_invalid")
    short_low = bool(short_threshold is not None and short_remaining <= short_threshold)
    long_exhausted = long_remaining == 0
    if not short_low and not long_exhausted:
        age_seconds = max(0.0, (baseline - observed_at).total_seconds())
        if age_seconds > config.usage_admission_max_age_seconds:
            return fail_open(config, "snapshot_stale")
        return UsageAdmissionDecision(
            "allowed",
            "short_window_above_threshold_and_long_window_not_exhausted",
            observed_at=observed_at,
            short_window_remaining_percent=short_remaining,
            long_window_remaining_percent=long_remaining,
        )

    gate_windows: tuple[str, ...]
    if short_low:
        reset_at = snapshot_time(short_window, "resets_at", "resets_at_iso") or snapshot_time(
            snapshot,
            "resets_at",
            "resets_at_iso",
        )
        if not reset_at:
            return fail_open(config, "short_window_reset_time_invalid")
        gate_windows = ("short_window",)
    else:
        reset_at = snapshot_time(long_window, "resets_at", "resets_at_iso")
        if not reset_at:
            return fail_open(config, "long_window_reset_time_invalid")
        gate_windows = ("long_window",)

    if reset_at <= baseline:
        if observed_at <= reset_at:
            return UsageAdmissionDecision(
                "allowed",
                stale_after_reset_reason(gate_windows),
                observed_at=observed_at,
                reset_at=reset_at,
                gate_windows=gate_windows,
                short_window_remaining_percent=short_remaining,
                long_window_remaining_percent=long_remaining,
            )
        return fail_open(config, "snapshot_reset_time_inconsistent")

    age_seconds = max(0.0, (baseline - observed_at).total_seconds())
    if age_seconds > config.usage_admission_max_age_seconds:
        return fail_open(config, "snapshot_stale_before_reset")

    reason = gated_reason(gate_windows)
    cooldown_until = reset_at + timedelta(seconds=config.usage_admission_reset_grace_seconds)
    decision = UsageAdmissionDecision(
        "gated",
        reason,
        cooldown_until=cooldown_until,
        observed_at=observed_at,
        reset_at=reset_at,
        gate_windows=gate_windows,
        short_window_remaining_percent=short_remaining,
        long_window_remaining_percent=long_remaining,
    )
    write_event_nonfatal(
        config,
        "usage_admission_gated",
        summary="usage-aware admission gate deferred Codex execution",
        payload=decision_payload(decision),
    )
    return decision


def read_usage_snapshot(config: Config) -> tuple[dict[str, Any] | None, str | None]:
    try:
        result = subprocess.run(
            config.usage_admission_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=config.usage_admission_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "snapshot_command_timed_out"
    except OSError:
        return None, "snapshot_command_failed"
    if result.returncode != 0:
        return None, "snapshot_command_failed"
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None, "snapshot_json_invalid"
    if not isinstance(value, dict):
        return None, "snapshot_json_not_object"
    return value, None


def fail_open(config: Config, reason: str) -> UsageAdmissionDecision:
    message = f"usage admission snapshot {reason}; continuing with existing runner behavior"
    print(f"warning: {message}", file=sys.stderr)
    write_event_nonfatal(
        config,
        "usage_admission_warning",
        summary=message,
        payload={"status": "fail_open", "reason": reason},
    )
    return UsageAdmissionDecision("fail_open", reason)


def snapshot_section(snapshot: dict[str, Any], key: str) -> dict[str, Any]:
    value = snapshot.get(key)
    return value if isinstance(value, dict) else {}


def semantic_windows(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    windows = [snapshot_section(snapshot, key) for key in ("primary", "secondary")]
    ranked = sorted(
        (window for window in windows if positive_number(window.get("window_minutes")) is not None),
        key=lambda window: float(window["window_minutes"]),
    )
    if len(ranked) == 1:
        return ranked[0], {}
    if len(ranked) == 2 and float(ranked[0]["window_minutes"]) < float(ranked[1]["window_minutes"]):
        return ranked[0], ranked[1]
    return {}, {}


def positive_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def snapshot_time(snapshot: dict[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        value = snapshot.get(key)
        if isinstance(value, str):
            parsed = parse_time(value)
            if parsed:
                return parsed
    return None


def percentage(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= result <= 100:
        return None
    return result


def decision_payload(decision: UsageAdmissionDecision) -> dict[str, Any]:
    return {
        "status": decision.status,
        "reason": decision.reason,
        "observed_at": decision.observed_at.isoformat() if decision.observed_at else None,
        "reset_at": decision.reset_at.isoformat() if decision.reset_at else None,
        "gate_windows": list(decision.gate_windows),
        "cooldown_until": decision.cooldown_until.isoformat() if decision.cooldown_until else None,
        "short_window_remaining_percent": decision.short_window_remaining_percent,
        "long_window_remaining_percent": decision.long_window_remaining_percent,
    }


def gated_reason(gate_windows: tuple[str, ...]) -> str:
    if gate_windows == ("long_window",):
        return "long_window_exhausted"
    return "short_window_remaining_at_or_below_threshold"


def stale_after_reset_reason(gate_windows: tuple[str, ...]) -> str:
    return f"{'_and_'.join(gate_windows)}_stale_after_reset_bounded_attempt"

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
    primary_remaining_percent: float | None = None
    secondary_remaining_percent: float | None = None


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

    primary = snapshot_section(snapshot, "primary")
    secondary = snapshot_section(snapshot, "secondary")
    primary_remaining = percentage(primary.get("remaining_percent"))
    secondary_remaining = percentage(secondary.get("remaining_percent"))
    primary_threshold = config.usage_admission_primary_threshold_percent
    secondary_threshold = config.usage_admission_secondary_threshold_percent

    if primary_remaining is None:
        return fail_open(config, "primary_remaining_percent_invalid")
    primary_low = bool(primary_threshold is not None and primary_remaining <= primary_threshold)
    secondary_low = bool(
        secondary_threshold is not None
        and secondary_remaining is not None
        and secondary_remaining <= secondary_threshold
    )
    if secondary_threshold is not None and secondary_remaining is None:
        return fail_open(config, "secondary_remaining_percent_invalid")
    if not primary_low and not secondary_low:
        age_seconds = max(0.0, (baseline - observed_at).total_seconds())
        if age_seconds > config.usage_admission_max_age_seconds:
            return fail_open(config, "snapshot_stale")
        return UsageAdmissionDecision(
            "allowed",
            "remaining_above_thresholds",
            observed_at=observed_at,
            primary_remaining_percent=primary_remaining,
            secondary_remaining_percent=secondary_remaining,
        )

    gate_resets: list[tuple[str, datetime]] = []
    if primary_low:
        primary_reset = snapshot_time(primary, "resets_at", "resets_at_iso") or snapshot_time(
            snapshot,
            "resets_at",
            "resets_at_iso",
        )
        if not primary_reset:
            return fail_open(config, "primary_reset_time_invalid")
        gate_resets.append(("primary", primary_reset))
    if secondary_low:
        secondary_reset = snapshot_time(secondary, "resets_at", "resets_at_iso")
        if not secondary_reset:
            return fail_open(config, "secondary_reset_time_invalid")
        gate_resets.append(("secondary", secondary_reset))

    gate_windows = tuple(window for window, _reset in gate_resets)
    reset_at = max(reset for _window, reset in gate_resets)

    if reset_at <= baseline:
        if observed_at <= reset_at:
            return UsageAdmissionDecision(
                "allowed",
                stale_after_reset_reason(gate_windows),
                observed_at=observed_at,
                reset_at=reset_at,
                gate_windows=gate_windows,
                primary_remaining_percent=primary_remaining,
                secondary_remaining_percent=secondary_remaining,
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
        primary_remaining_percent=primary_remaining,
        secondary_remaining_percent=secondary_remaining,
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
        "primary_remaining_percent": decision.primary_remaining_percent,
        "secondary_remaining_percent": decision.secondary_remaining_percent,
    }


def gated_reason(gate_windows: tuple[str, ...]) -> str:
    if gate_windows == ("primary", "secondary"):
        return "primary_and_secondary_remaining_at_or_below_threshold"
    return f"{gate_windows[0]}_remaining_at_or_below_threshold"


def stale_after_reset_reason(gate_windows: tuple[str, ...]) -> str:
    return f"{'_and_'.join(gate_windows)}_stale_after_reset_bounded_attempt"

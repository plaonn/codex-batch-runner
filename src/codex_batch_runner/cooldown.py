from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .timeutil import parse_time

MANUAL_COOLDOWN_SAFETY_OFFSET_SECONDS = 60
MAX_MANUAL_COOLDOWN_SECONDS = 7 * 24 * 60 * 60

TIME_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{1,2})$")
MONTH_DAY_RE = re.compile(r"^(?P<month>\d{1,2})[/-](?P<day>\d{1,2})\s+(?P<hour>\d{1,2}):(?P<minute>\d{1,2})$")
YEAR_DATE_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\s+(?P<hour>\d{1,2}):(?P<minute>\d{1,2})$")
RELATIVE_TOKEN_RE = re.compile(r"(?P<amount>\d+)(?P<unit>[dhm])")


@dataclass(frozen=True)
class CooldownSchedule:
    input_value: str
    interpreted_reset_at: datetime
    effective_cooldown_until: datetime
    duration_seconds: int


def local_now() -> datetime:
    return datetime.now().astimezone()


def parse_manual_cooldown(value: str, *, now: datetime | None = None) -> CooldownSchedule:
    input_value = value.strip()
    if not input_value:
        raise ValueError("cooldown value is required")
    baseline = now or local_now()
    if baseline.tzinfo is None:
        baseline = baseline.astimezone()
    local_baseline = baseline
    reset_at = parse_reset_at(input_value, local_baseline)
    if reset_at <= local_baseline:
        raise ValueError("cooldown reset time is in the past")
    seconds_until_reset = (reset_at - local_baseline).total_seconds()
    if seconds_until_reset > MAX_MANUAL_COOLDOWN_SECONDS:
        raise ValueError("cooldown reset time must be within 7 days")
    effective = reset_at + timedelta(seconds=MANUAL_COOLDOWN_SAFETY_OFFSET_SECONDS)
    return CooldownSchedule(
        input_value=input_value,
        interpreted_reset_at=reset_at,
        effective_cooldown_until=effective,
        duration_seconds=max(0, int((reset_at - local_baseline).total_seconds())),
    )


def parse_reset_at(value: str, now: datetime) -> datetime:
    if value.startswith("+"):
        return now + parse_relative_duration(value)

    parsed = parse_time(value) if is_timezone_datetime(value) else None
    if parsed:
        return parsed.astimezone(now.tzinfo)

    match = TIME_RE.fullmatch(value)
    if match:
        hour, minute = parse_hour_minute(match)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    match = YEAR_DATE_RE.fullmatch(value)
    if match:
        hour, minute = parse_hour_minute(match)
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            hour,
            minute,
            tzinfo=now.tzinfo,
        )

    match = MONTH_DAY_RE.fullmatch(value)
    if match:
        hour, minute = parse_hour_minute(match)
        return datetime(
            now.year,
            int(match.group("month")),
            int(match.group("day")),
            hour,
            minute,
            tzinfo=now.tzinfo,
        )

    raise ValueError("unsupported cooldown value format")


def parse_hour_minute(match: re.Match[str]) -> tuple[int, int]:
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if not 0 <= hour <= 23:
        raise ValueError("hour must be 0..23")
    if not 0 <= minute <= 59:
        raise ValueError("minute must be 0..59")
    return hour, minute


def parse_relative_duration(value: str) -> timedelta:
    text = value[1:]
    if not text:
        raise ValueError("relative cooldown duration is required")
    position = 0
    days = hours = minutes = 0
    for match in RELATIVE_TOKEN_RE.finditer(text):
        if match.start() != position:
            raise ValueError("unsupported relative cooldown duration")
        amount = int(match.group("amount"))
        unit = match.group("unit")
        if unit == "d":
            days += amount
        elif unit == "h":
            hours += amount
        elif unit == "m":
            minutes += amount
        position = match.end()
    if position != len(text):
        raise ValueError("unsupported relative cooldown duration")
    duration = timedelta(days=days, hours=hours, minutes=minutes)
    if duration.total_seconds() <= 0:
        raise ValueError("relative cooldown duration must be positive")
    return duration


def is_timezone_datetime(value: str) -> bool:
    if "T" not in value:
        return False
    text = value.strip()
    if text.endswith("Z"):
        return True
    return bool(re.search(r"[+-]\d{2}:\d{2}$", text))


def cooldown_status(cooldown_until: str | None, *, now: datetime | None = None) -> dict[str, object]:
    baseline = now or local_now()
    if baseline.tzinfo is None:
        baseline = baseline.astimezone()
    parsed = parse_time(cooldown_until)
    local_baseline = baseline
    local_until = parsed.astimezone(local_baseline.tzinfo) if parsed else None
    remaining_seconds = max(0, int((local_until - local_baseline).total_seconds())) if local_until else 0
    return {
        "global_cooldown_until": local_until.isoformat() if local_until else None,
        "active": bool(local_until and local_until > local_baseline),
        "remaining_seconds": remaining_seconds,
        "remaining": format_duration(remaining_seconds),
    }


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)

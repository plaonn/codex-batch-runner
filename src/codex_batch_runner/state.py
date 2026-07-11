from __future__ import annotations

from .config import Config
from .events import sanitize_scalar
from .fs import read_json, write_json_atomic
from .timeutil import iso_now, parse_time, utc_now


DEFAULT_RUNNER_PAUSE = {
    "active": False,
    "reason": None,
    "paused_at": None,
    "paused_by": None,
}

DEFAULT_STATE = {
    "global_cooldown_until": None,
    "last_rate_limit_at": None,
    "last_run_at": None,
    "last_success_at": None,
    "last_task_id": None,
    "reviewer_codex_cooldown_until": None,
    "last_reviewer_codex_rate_limit_at": None,
    "usage_admission_stale_attempt": None,
    "runner_pause": dict(DEFAULT_RUNNER_PAUSE),
}


def load_state(config: Config) -> dict:
    data = read_json(config.state_file, None)
    state = dict(DEFAULT_STATE)
    if isinstance(data, dict):
        state.update(data)
    state["runner_pause"] = normalize_runner_pause(state.get("runner_pause"))
    return state


def save_state(config: Config, state: dict) -> None:
    write_json_atomic(config.state_file, state)


def in_global_cooldown(config: Config) -> bool:
    until = parse_time(load_state(config).get("global_cooldown_until"))
    return bool(until and until > utc_now())


def in_reviewer_codex_cooldown(config: Config) -> bool:
    until = parse_time(load_state(config).get("reviewer_codex_cooldown_until"))
    return bool(until and until > utc_now())


def normalize_runner_pause(value: object) -> dict:
    if not isinstance(value, dict):
        return dict(DEFAULT_RUNNER_PAUSE)
    active = bool(value.get("active"))
    if not active:
        return dict(DEFAULT_RUNNER_PAUSE)
    return {
        "active": True,
        "reason": sanitized_metadata_text(value.get("reason")),
        "paused_at": sanitized_metadata_text(value.get("paused_at")),
        "paused_by": sanitized_metadata_text(value.get("paused_by")),
    }


def get_runner_pause(config: Config) -> dict:
    return normalize_runner_pause(load_state(config).get("runner_pause"))


def is_runner_paused(config: Config) -> bool:
    return bool(get_runner_pause(config).get("active"))


def mark_run(config: Config, task_id: str | None = None) -> None:
    state = load_state(config)
    state["last_run_at"] = iso_now()
    state["last_task_id"] = task_id
    save_state(config, state)


def mark_success(config: Config, task_id: str) -> None:
    state = load_state(config)
    state["last_success_at"] = iso_now()
    state["last_task_id"] = task_id
    if parse_time(state.get("global_cooldown_until")) and parse_time(state.get("global_cooldown_until")) <= utc_now():
        state["global_cooldown_until"] = None
    save_state(config, state)


def mark_rate_limit(config: Config, cooldown_until: str, task_id: str) -> None:
    state = load_state(config)
    state["global_cooldown_until"] = cooldown_until
    state["last_rate_limit_at"] = iso_now()
    state["last_task_id"] = task_id
    save_state(config, state)


def mark_reviewer_codex_rate_limit(config: Config, cooldown_until: str, task_id: str) -> None:
    state = load_state(config)
    state["reviewer_codex_cooldown_until"] = cooldown_until
    state["last_reviewer_codex_rate_limit_at"] = iso_now()
    state["last_task_id"] = task_id
    save_state(config, state)


def reserve_usage_stale_attempt(config: Config, reset_at: str, task_id: str) -> str:
    state = load_state(config)
    current = state.get("usage_admission_stale_attempt")
    if isinstance(current, dict) and current.get("reset_at") == reset_at:
        if current.get("status") == "passed":
            return "passed"
        if current.get("status") == "in_flight" and usage_attempt_task_is_running(config, current.get("task_id")):
            return "in_flight"
    state["usage_admission_stale_attempt"] = {
        "reset_at": reset_at,
        "task_id": task_id,
        "status": "in_flight",
        "started_at": iso_now(),
    }
    save_state(config, state)
    return "reserved"


def finish_usage_stale_attempt(config: Config, reset_at: str, task_id: str) -> None:
    state = load_state(config)
    current = state.get("usage_admission_stale_attempt")
    if not isinstance(current, dict):
        return
    if current.get("reset_at") != reset_at or current.get("task_id") != task_id:
        return
    current["status"] = "passed"
    current["finished_at"] = iso_now()
    state["usage_admission_stale_attempt"] = current
    save_state(config, state)


def usage_attempt_task_is_running(config: Config, task_id: object) -> bool:
    if not isinstance(task_id, str) or not task_id:
        return False
    try:
        from .queue import load_task

        task = load_task(config, task_id)
    except (OSError, ValueError):
        return False
    return task.get("status") == "running"


def set_global_cooldown(config: Config, cooldown_until: str) -> dict:
    state = load_state(config)
    state["global_cooldown_until"] = cooldown_until
    save_state(config, state)
    return state


def clear_global_cooldown(config: Config) -> dict:
    state = load_state(config)
    state["global_cooldown_until"] = None
    save_state(config, state)
    return state


def clear_reviewer_codex_cooldown(config: Config) -> dict:
    state = load_state(config)
    state["reviewer_codex_cooldown_until"] = None
    save_state(config, state)
    return state


def set_runner_pause(config: Config, reason: object, paused_by: object = None) -> dict:
    normalized_reason = sanitized_metadata_text(reason)
    if not normalized_reason:
        raise ValueError("runner pause reason is required")
    state = load_state(config)
    state["runner_pause"] = {
        "active": True,
        "reason": normalized_reason,
        "paused_at": iso_now(),
        "paused_by": sanitized_metadata_text(paused_by),
    }
    save_state(config, state)
    return dict(state["runner_pause"])


def clear_runner_pause(config: Config) -> dict:
    state = load_state(config)
    previous = normalize_runner_pause(state.get("runner_pause"))
    state["runner_pause"] = dict(DEFAULT_RUNNER_PAUSE)
    save_state(config, state)
    return previous


def sanitized_metadata_text(value: object) -> str | None:
    if value is None:
        return None
    text = sanitize_scalar(str(value).strip())
    if not isinstance(text, str):
        return None
    return text or None

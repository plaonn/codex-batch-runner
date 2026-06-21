from __future__ import annotations

from .config import Config
from .fs import read_json, write_json_atomic
from .timeutil import iso_now, parse_time, utc_now


DEFAULT_STATE = {
    "global_cooldown_until": None,
    "last_rate_limit_at": None,
    "last_run_at": None,
    "last_success_at": None,
    "last_task_id": None,
    "reviewer_codex_cooldown_until": None,
    "last_reviewer_codex_rate_limit_at": None,
}


def load_state(config: Config) -> dict:
    data = read_json(config.state_file, None)
    state = dict(DEFAULT_STATE)
    if isinstance(data, dict):
        state.update(data)
    return state


def save_state(config: Config, state: dict) -> None:
    write_json_atomic(config.state_file, state)


def in_global_cooldown(config: Config) -> bool:
    until = parse_time(load_state(config).get("global_cooldown_until"))
    return bool(until and until > utc_now())


def in_reviewer_codex_cooldown(config: Config) -> bool:
    until = parse_time(load_state(config).get("reviewer_codex_cooldown_until"))
    return bool(until and until > utc_now())


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

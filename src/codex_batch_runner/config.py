from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .execution_profiles import execution_profiles_value, optional_profile_name
from .fs import read_json


@dataclass(frozen=True)
class Config:
    root: Path
    queue_dir: Path
    log_dir: Path
    event_dir: Path
    notifier_cursor_state_paths: list[Path]
    lock_file: Path
    state_file: Path
    codex_command: list[str]
    codex_resume_command: list[str]
    post_mutation_trigger_command: list[str]
    stale_lock_seconds: int
    rate_limit_cooldown_seconds: int
    default_max_attempts: int
    manual_cooldown_wake_scheduler: str = "disabled"
    manual_cooldown_wake_command: list[str] = field(default_factory=list)
    codex_cli_update_command: list[str] = field(default_factory=list)
    codex_cli_smoke_command: list[str] = field(default_factory=list)
    codex_cli_rollback_command: list[str] = field(default_factory=list)
    codex_cli_maintenance_on_empty: bool = False
    dependency_requires_accepted_review: bool = False
    auto_review_mechanical_accept: bool = False
    auto_review_codex_enabled: bool = False
    auto_review_codex_max_calls_per_run: int = 0
    auto_review_codex_max_fix_loops_per_task: int = 0
    auto_review_codex_cooldown_seconds: int = 1800
    auto_review_codex_max_bundle_chars: int = 120000
    auto_review_codex_max_diff_chars: int = 60000
    worktree_mode: str = "disabled"
    worktree_root: Path | None = None
    max_total_running: int = 1
    max_running_per_project: int = 1
    capacity_pools: dict[str, dict[str, int]] = field(default_factory=lambda: {"codex": {"max_running": 1}})
    project_priorities: dict[str, int] = field(default_factory=dict)
    default_project_priority: int = 100
    project_priority_aging_hours: int = 24
    codex_startup_stall_seconds: int = 240
    codex_first_meaningful_timeout_seconds: int = 420
    codex_mid_run_idle_seconds: int = 1800
    codex_mid_run_idle_kill_enabled: bool = False
    codex_total_runtime_timeout_seconds: int | None = None
    codex_watchdog_grace_seconds: int = 5
    codex_startup_stall_cooldown_seconds: int = 60
    shell_task_timeout_seconds: int = 900
    default_execution_profile: str | None = None
    review_execution_profile: str | None = None
    execution_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, config_path: str | None = None, root: Path | None = None) -> "Config":
        resolved_config_path = resolve_config_path(config_path, include_user_config=root is None)
        data: dict[str, Any] = {}
        if resolved_config_path:
            data = read_json(resolved_config_path, {}) or {}
        fallback_root = (root or Path.cwd()).resolve()
        base = fallback_root if root is not None else root_value(data.get("root"), fallback_root, resolved_config_path)

        def path_value(key: str, default: str) -> Path:
            raw = Path(data.get(key, default)).expanduser()
            return raw if raw.is_absolute() else base / raw

        queue_dir = path_value("queue_dir", ".codex-batch-runner/tasks")
        log_dir = path_value("log_dir", ".codex-batch-runner/logs")
        event_dir = path_value("event_dir", str(log_dir.parent / "events"))
        notifier_cursor_state_paths = path_list_value("notifier_cursor_state_paths", data, base)
        execution_profiles = execution_profiles_value(data.get("execution_profiles", {}))

        return cls(
            root=base,
            queue_dir=queue_dir,
            log_dir=log_dir,
            event_dir=event_dir,
            notifier_cursor_state_paths=notifier_cursor_state_paths,
            lock_file=path_value("lock_file", ".codex-batch-runner/runner.lock"),
            state_file=path_value("state_file", ".codex-batch-runner/state.json"),
            codex_command=list(data.get("codex_command", ["codex", "exec", "--sandbox", "workspace-write", "--json"])),
            codex_resume_command=list(
                data.get(
                    "codex_resume_command",
                    ["codex", "exec", "--sandbox", "workspace-write", "resume", "{session_id}", "--json"],
                )
            ),
            post_mutation_trigger_command=argv_list(
                "post_mutation_trigger_command",
                data.get("post_mutation_trigger_command", []),
            ),
            manual_cooldown_wake_scheduler=manual_cooldown_wake_scheduler_value(
                data.get("manual_cooldown_wake_scheduler", "disabled")
            ),
            manual_cooldown_wake_command=argv_list(
                "manual_cooldown_wake_command",
                data.get("manual_cooldown_wake_command", []),
            ),
            codex_cli_update_command=argv_list(
                "codex_cli_update_command",
                data.get("codex_cli_update_command", []),
            ),
            codex_cli_smoke_command=argv_list(
                "codex_cli_smoke_command",
                data.get("codex_cli_smoke_command", []),
            ),
            codex_cli_rollback_command=argv_list(
                "codex_cli_rollback_command",
                data.get("codex_cli_rollback_command", []),
            ),
            codex_cli_maintenance_on_empty=bool_value(
                "codex_cli_maintenance_on_empty",
                data.get("codex_cli_maintenance_on_empty", False),
            ),
            stale_lock_seconds=int(data.get("stale_lock_seconds", 21600)),
            rate_limit_cooldown_seconds=int(data.get("rate_limit_cooldown_seconds", 1800)),
            default_max_attempts=int(data.get("default_max_attempts", 5)),
            dependency_requires_accepted_review=bool_value(
                "dependency_requires_accepted_review",
                data.get("dependency_requires_accepted_review", False),
            ),
            auto_review_mechanical_accept=bool_value(
                "auto_review_mechanical_accept",
                data.get("auto_review_mechanical_accept", False),
            ),
            auto_review_codex_enabled=bool_value(
                "auto_review_codex_enabled",
                data.get("auto_review_codex_enabled", False),
            ),
            auto_review_codex_max_calls_per_run=non_negative_int_value(
                "auto_review_codex_max_calls_per_run",
                data.get("auto_review_codex_max_calls_per_run", 0),
            ),
            auto_review_codex_max_fix_loops_per_task=non_negative_int_value(
                "auto_review_codex_max_fix_loops_per_task",
                data.get("auto_review_codex_max_fix_loops_per_task", 0),
            ),
            auto_review_codex_cooldown_seconds=non_negative_int_value(
                "auto_review_codex_cooldown_seconds",
                data.get("auto_review_codex_cooldown_seconds", 1800),
            ),
            auto_review_codex_max_bundle_chars=non_negative_int_value(
                "auto_review_codex_max_bundle_chars",
                data.get("auto_review_codex_max_bundle_chars", 120000),
            ),
            auto_review_codex_max_diff_chars=non_negative_int_value(
                "auto_review_codex_max_diff_chars",
                data.get("auto_review_codex_max_diff_chars", 60000),
            ),
            worktree_mode=worktree_mode_value(data.get("worktree_mode", "disabled")),
            worktree_root=path_value("worktree_root", ".codex-batch-runner/worktrees"),
            max_total_running=positive_int_value("max_total_running", data.get("max_total_running", 1)),
            max_running_per_project=positive_int_value(
                "max_running_per_project",
                data.get("max_running_per_project", 1),
            ),
            capacity_pools=capacity_pools_value(data.get("capacity_pools")),
            project_priorities=project_priorities_value(data.get("project_priorities", {})),
            default_project_priority=int_value(
                "default_project_priority",
                data.get("default_project_priority", 100),
            ),
            project_priority_aging_hours=non_negative_int_value(
                "project_priority_aging_hours",
                data.get("project_priority_aging_hours", 24),
            ),
            codex_startup_stall_seconds=int(data.get("codex_startup_stall_seconds", 240)),
            codex_first_meaningful_timeout_seconds=int(data.get("codex_first_meaningful_timeout_seconds", 420)),
            codex_mid_run_idle_seconds=int(data.get("codex_mid_run_idle_seconds", 1800)),
            codex_mid_run_idle_kill_enabled=bool_value(
                "codex_mid_run_idle_kill_enabled",
                data.get("codex_mid_run_idle_kill_enabled", False),
            ),
            codex_total_runtime_timeout_seconds=optional_int_value(data.get("codex_total_runtime_timeout_seconds")),
            codex_watchdog_grace_seconds=int(data.get("codex_watchdog_grace_seconds", 5)),
            codex_startup_stall_cooldown_seconds=int(data.get("codex_startup_stall_cooldown_seconds", 60)),
            shell_task_timeout_seconds=positive_int_value(
                "shell_task_timeout_seconds",
                data.get("shell_task_timeout_seconds", 900),
            ),
            default_execution_profile=optional_profile_name(
                "default_execution_profile",
                data.get("default_execution_profile"),
                execution_profiles,
            ),
            review_execution_profile=optional_profile_name(
                "review_execution_profile",
                data.get("review_execution_profile"),
                execution_profiles,
            ),
            execution_profiles=execution_profiles,
        )


def argv_list(key: str, value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return value


def path_list_value(key: str, data: dict[str, Any], base: Path) -> list[Path]:
    value = data.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of path strings")
    paths = []
    for item in value:
        path = Path(item).expanduser()
        paths.append(path if path.is_absolute() else base / path)
    return paths


def root_value(value: object, fallback: Path, config_path: Path | None) -> Path:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise ValueError("root must be a path string")
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    base = config_path.parent if config_path else fallback
    return (base / path).resolve()


def bool_value(key: str, value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be a boolean")


def worktree_mode_value(value: object) -> str:
    if value in {"disabled", "task"}:
        return str(value)
    raise ValueError("worktree_mode must be one of: disabled, task")


def manual_cooldown_wake_scheduler_value(value: object) -> str:
    if value in {"disabled", "macos_launchd"}:
        return str(value)
    raise ValueError("manual_cooldown_wake_scheduler must be one of: disabled, macos_launchd")


def optional_int_value(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def non_negative_int_value(key: str, value: object) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a non-negative integer")
    result = int(value)
    if result < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return result


def positive_int_value(key: str, value: object) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{key} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if result < 1:
        raise ValueError(f"{key} must be a positive integer")
    return result


def int_value(key: str, value: object) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc


def capacity_pools_value(value: object) -> dict[str, dict[str, int]]:
    if value is None:
        return {"codex": {"max_running": 1}}
    if not isinstance(value, dict):
        raise ValueError("capacity_pools must be an object")
    pools: dict[str, dict[str, int]] = {}
    for name, pool in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("capacity_pools names must be non-empty strings")
        if not isinstance(pool, dict):
            raise ValueError(f"capacity_pools.{name} must be an object")
        pools[name] = {
            "max_running": positive_int_value(
                f"capacity_pools.{name}.max_running",
                pool.get("max_running"),
            )
        }
    if "codex" not in pools:
        raise ValueError("capacity_pools must define codex")
    return pools


def project_priorities_value(value: object) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("project_priorities must be an object")
    priorities: dict[str, int] = {}
    for key, raw_priority in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("project_priorities keys must be non-empty strings")
        priorities[normalize_project_priority_key(key)] = int_value(f"project_priorities.{key}", raw_priority)
    return priorities


def normalize_project_priority_key(value: str) -> str:
    text = value.strip()
    if not text:
        return text
    if text.startswith("~") or "/" in text:
        return str(Path(text).expanduser().resolve())
    return text


def resolve_config_path(config_path: str | None = None, include_user_config: bool = True) -> Path | None:
    if config_path:
        return Path(config_path).expanduser().resolve()
    env_path = os.environ.get("CBR_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()
    if not include_user_config:
        return None
    user_config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "codex-batch-runner" / "config.json"
    if user_config.exists():
        return user_config.resolve()
    return None

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fs import read_json
from .model_requirements import (
    REQUIREMENT_DIMENSIONS,
    execution_config_value,
    execution_targets_value,
    level_match_value,
    model_requirement_vector_value,
    model_selection_rules_value,
    optional_string_value,
    string_value,
)


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
    parent_attention_outbox_dir: Path | None = None
    parent_attention_delivery_command: list[str] = field(default_factory=list)
    parent_attention_delivery_timeout_seconds: int = 15
    parent_attention_max_attempts: int = 5
    parent_attention_retry_base_seconds: int = 30
    usage_admission_enabled: bool = False
    usage_admission_command: list[str] = field(default_factory=list)
    usage_admission_timeout_seconds: int = 5
    usage_admission_max_age_seconds: int = 300
    usage_admission_short_window_threshold_percent: float | None = None
    usage_admission_reset_grace_seconds: int = 60
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
    external_json_command_timeout_seconds: int = 900
    default_model_requirement_vector: dict[str, Any] = field(default_factory=dict)
    review_model_requirement_vector: dict[str, Any] = field(default_factory=dict)
    default_execution_config: dict[str, Any] = field(default_factory=dict)
    execution_targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    model_selection_rules: list[dict[str, Any]] = field(default_factory=list)
    worker_targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    worker_selection_rules: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, config_path: str | None = None, root: Path | None = None) -> "Config":
        resolved_config_path = resolve_config_path(config_path)
        if root is None and resolved_config_path is None:
            raise ValueError("config required: pass --config /path/to/config.json or set CBR_CONFIG")
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
        parent_attention_outbox_dir = path_value(
            "parent_attention_outbox_dir", str(log_dir.parent / "parent-attention-outbox")
        )
        notifier_cursor_state_paths = path_list_value("notifier_cursor_state_paths", data, base)
        reject_removed_config_keys(data)

        default_execution_config = execution_config_value(
            "default_execution_config",
            data.get("default_execution_config"),
        )
        execution_targets = execution_targets_value(data.get("execution_targets"))
        model_selection_rules = model_selection_rules_value(data.get("model_selection_rules"))
        validate_execution_target_references(default_execution_config, model_selection_rules, execution_targets)
        capacity_pools = capacity_pools_value(data.get("capacity_pools"))
        worker_targets = worker_targets_value(data.get("worker_targets"))
        worker_selection_rules = worker_selection_rules_value(data.get("worker_selection_rules"))
        validate_worker_target_references(worker_targets, worker_selection_rules, capacity_pools)
        usage_admission_enabled = bool_value(
            "usage_admission_enabled",
            data.get("usage_admission_enabled", False),
        )
        usage_admission_command = argv_list(
            "usage_admission_command",
            data.get("usage_admission_command", []),
        )
        usage_admission_short_window_threshold_percent = optional_percentage_value(
            "usage_admission_short_window_threshold_percent",
            data.get("usage_admission_short_window_threshold_percent"),
        )
        validate_usage_admission_config(
            usage_admission_enabled,
            usage_admission_command,
            usage_admission_short_window_threshold_percent,
        )

        return cls(
            root=base,
            queue_dir=queue_dir,
            log_dir=log_dir,
            event_dir=event_dir,
            parent_attention_outbox_dir=parent_attention_outbox_dir,
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
            parent_attention_delivery_command=argv_list(
                "parent_attention_delivery_command",
                data.get("parent_attention_delivery_command", []),
            ),
            parent_attention_delivery_timeout_seconds=positive_int_value(
                "parent_attention_delivery_timeout_seconds",
                data.get("parent_attention_delivery_timeout_seconds", 15),
            ),
            parent_attention_max_attempts=positive_int_value(
                "parent_attention_max_attempts", data.get("parent_attention_max_attempts", 5)
            ),
            parent_attention_retry_base_seconds=positive_int_value(
                "parent_attention_retry_base_seconds",
                data.get("parent_attention_retry_base_seconds", 30),
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
            usage_admission_enabled=usage_admission_enabled,
            usage_admission_command=usage_admission_command,
            usage_admission_timeout_seconds=positive_int_value(
                "usage_admission_timeout_seconds",
                data.get("usage_admission_timeout_seconds", 5),
            ),
            usage_admission_max_age_seconds=positive_int_value(
                "usage_admission_max_age_seconds",
                data.get("usage_admission_max_age_seconds", 300),
            ),
            usage_admission_short_window_threshold_percent=usage_admission_short_window_threshold_percent,
            usage_admission_reset_grace_seconds=non_negative_int_value(
                "usage_admission_reset_grace_seconds",
                data.get("usage_admission_reset_grace_seconds", 60),
            ),
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
            capacity_pools=capacity_pools,
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
            external_json_command_timeout_seconds=positive_int_value(
                "external_json_command_timeout_seconds",
                data.get("external_json_command_timeout_seconds", 900),
            ),
            default_model_requirement_vector=model_requirement_vector_value(
                "default_model_requirement_vector",
                data.get("default_model_requirement_vector"),
            ),
            review_model_requirement_vector=model_requirement_vector_value(
                "review_model_requirement_vector",
                data.get("review_model_requirement_vector"),
            ),
            default_execution_config=default_execution_config,
            execution_targets=execution_targets,
            model_selection_rules=model_selection_rules,
            worker_targets=worker_targets,
            worker_selection_rules=worker_selection_rules,
        )


def argv_list(key: str, value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return value


def reject_removed_config_keys(data: dict[str, Any]) -> None:
    removed = sorted(
        key
        for key in ("default_execution_profile", "review_execution_profile", "execution_profiles")
        if key in data
    )
    if removed:
        raise ValueError(
            "removed config field(s): "
            + ", ".join(removed)
            + "; use model_requirement_vector and model_selection_rules instead"
        )


def validate_execution_target_references(
    default_execution_config: dict[str, Any],
    model_selection_rules: list[dict[str, Any]],
    execution_targets: dict[str, dict[str, Any]],
) -> None:
    references: list[tuple[str, str]] = []
    if default_execution_config.get("execution_target"):
        references.append(("default_execution_config.execution_target", str(default_execution_config["execution_target"])))
    for rule in model_selection_rules:
        if rule.get("execution_target"):
            references.append(
                (
                    f"model_selection_rules.{rule.get('name')}.execution_target",
                    str(rule["execution_target"]),
                )
            )
    missing = [(key, alias) for key, alias in references if alias not in execution_targets]
    if missing:
        key, alias = missing[0]
        raise ValueError(f"{key} references unknown execution_target: {alias}")


def worker_targets_value(value: object) -> dict[str, dict[str, Any]]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("worker_targets must be an object")
    targets: dict[str, dict[str, Any]] = {}
    for raw_name, raw_target in value.items():
        name = string_value("worker_targets key", raw_name)
        targets[name] = worker_target_value(f"worker_targets.{name}", raw_target)
    return targets


def worker_target_value(key: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    backend = (
        optional_string_value(f"{key}.execution_backend", value.get("execution_backend"))
        or "external-json-command"
    )
    if backend not in {"shell", "external-json-command"}:
        raise ValueError(f"{key}.execution_backend must be one of: external-json-command, shell")
    target: dict[str, Any] = {"execution_backend": backend}
    if "capacity_pool" in value:
        target["capacity_pool"] = optional_string_value(f"{key}.capacity_pool", value.get("capacity_pool")) or "codex"
    if backend == "external-json-command":
        command = argv_list(f"{key}.external_command", value.get("external_command"))
        if not command:
            raise ValueError(f"{key}.external_command must be a non-empty list of strings")
        target["external_command"] = command
        if "external_timeout_seconds" in value:
            target["external_timeout_seconds"] = positive_int_value(
                f"{key}.external_timeout_seconds",
                value.get("external_timeout_seconds"),
            )
    if backend == "shell":
        command = argv_list(f"{key}.shell_command", value.get("shell_command"))
        if not command:
            raise ValueError(f"{key}.shell_command must be a non-empty list of strings")
        target["shell_command"] = command
        if "shell_timeout_seconds" in value:
            target["shell_timeout_seconds"] = positive_int_value(
                f"{key}.shell_timeout_seconds",
                value.get("shell_timeout_seconds"),
            )
    if "worker_family" in value:
        target["worker_family"] = optional_string_value(f"{key}.worker_family", value.get("worker_family"))
    if "model_group" in value:
        target["model_group"] = optional_string_value(f"{key}.model_group", value.get("model_group"))
    if "budget_hint" in value:
        target["budget_hint"] = optional_string_value(f"{key}.budget_hint", value.get("budget_hint"))
    return {item_key: item for item_key, item in target.items() if item not in (None, "", {})}


def worker_selection_rules_value(value: object) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("worker_selection_rules must be a list")
    rules: list[dict[str, Any]] = []
    for index, raw_rule in enumerate(value):
        key = f"worker_selection_rules[{index}]"
        if not isinstance(raw_rule, dict):
            raise ValueError(f"{key} must be an object")
        name = optional_string_value(f"{key}.name", raw_rule.get("name")) or f"rule-{index}"
        raw_when = raw_rule.get("when", {})
        if not isinstance(raw_when, dict):
            raise ValueError(f"{key}.when must be an object")
        when = {dimension: level_match_value(f"{key}.when.{dimension}", raw_when[dimension]) for dimension in raw_when}
        unknown = sorted(str(name) for name in when if name not in REQUIREMENT_DIMENSIONS)
        if unknown:
            raise ValueError(f"{key}.when contains unknown dimensions: {', '.join(unknown)}")
        worker_target = optional_string_value(f"{key}.worker_target", raw_rule.get("worker_target"))
        if not worker_target:
            raise ValueError(f"{key}.worker_target must be a non-empty string")
        rules.append({"name": name, "when": when, "worker_target": worker_target})
    return rules


def validate_worker_target_references(
    worker_targets: dict[str, dict[str, Any]],
    worker_selection_rules: list[dict[str, Any]],
    capacity_pools: dict[str, dict[str, int]],
) -> None:
    for name, target in worker_targets.items():
        pool = str(target.get("capacity_pool") or "codex")
        if pool not in capacity_pools:
            raise ValueError(f"worker_targets.{name}.capacity_pool references unknown capacity_pool: {pool}")
    for rule in worker_selection_rules:
        alias = str(rule.get("worker_target") or "")
        if alias not in worker_targets:
            raise ValueError(
                f"worker_selection_rules.{rule.get('name')}.worker_target references unknown worker_target: {alias}"
            )


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


def optional_percentage_value(key: str, value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a number from 0 to 100")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number from 0 to 100") from exc
    if not 0 <= result <= 100:
        raise ValueError(f"{key} must be a number from 0 to 100")
    return result


def validate_usage_admission_config(
    enabled: bool,
    command: list[str],
    short_window_threshold_percent: float | None,
) -> None:
    if not enabled:
        return
    if not command:
        raise ValueError("usage_admission_command must be configured when usage_admission_enabled is true")
    if short_window_threshold_percent is None:
        raise ValueError(
            "usage_admission_short_window_threshold_percent must be configured when usage_admission_enabled is true"
        )


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


def resolve_config_path(config_path: str | None = None) -> Path | None:
    if config_path:
        return Path(config_path).expanduser().resolve()
    env_path = os.environ.get("CBR_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return None

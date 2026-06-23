from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SAFE_CONFIG_OVERRIDE_KEYS = {
    "model_reasoning_effort",
    "model_reasoning_summary",
    "model_verbosity",
}
HIGH_RISK_PROFILE_TERMS = {
    "lock",
    "queue-mutation",
    "rebase",
    "resume",
    "reviewer-codex",
    "reviewer-safety",
    "runner",
    "runner-state",
    "stale-base",
    "worktree-apply",
    "worktree-critical",
    "worktree-recovery",
}


@dataclass(frozen=True)
class ExecutionSettings:
    profile_name: str | None = None
    profile_source: str | None = None
    profile_reason: str | None = None
    model: str | None = None
    codex_profile: str | None = None
    config_overrides: dict[str, str] | None = None
    token_budget_hint: str | None = None


def execution_profiles_value(value: object) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("execution_profiles must be an object")
    profiles: dict[str, dict[str, Any]] = {}
    for name, raw_profile in value.items():
        profile_name = string_value("execution profile name", name)
        if not isinstance(raw_profile, dict):
            raise ValueError(f"execution_profiles.{profile_name} must be an object")
        profiles[profile_name] = normalize_profile(raw_profile, prefix=f"execution_profiles.{profile_name}")
    return profiles


def normalize_profile(value: dict[str, Any], *, prefix: str) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    if "model" in value:
        profile["model"] = optional_string_value(f"{prefix}.model", value.get("model"))
    if "codex_profile" in value:
        profile["codex_profile"] = optional_string_value(f"{prefix}.codex_profile", value.get("codex_profile"))
    raw_overrides = value.get("config_overrides", value.get("codex_config_overrides", {}))
    overrides = config_overrides_value(f"{prefix}.config_overrides", raw_overrides)
    if overrides:
        profile["config_overrides"] = overrides
    if "token_budget_hint" in value:
        profile["token_budget_hint"] = optional_string_value(f"{prefix}.token_budget_hint", value.get("token_budget_hint"))
    return {key: item for key, item in profile.items() if item not in (None, {}, "")}


def optional_profile_name(key: str, value: object, profiles: dict[str, dict[str, Any]]) -> str | None:
    if value in (None, ""):
        return None
    name = string_value(key, value)
    if name not in profiles:
        raise ValueError(f"{key} references unknown execution profile: {name}")
    return name


def config_overrides_value(key: str, value: object) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    overrides: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = string_value(f"{key} key", raw_name)
        if name not in SAFE_CONFIG_OVERRIDE_KEYS:
            allowed = ", ".join(sorted(SAFE_CONFIG_OVERRIDE_KEYS))
            raise ValueError(f"{key}.{name} is not allowlisted; allowed keys: {allowed}")
        overrides[name] = string_value(f"{key}.{name}", raw_value)
    return overrides


def task_execution_metadata(
    *,
    execution_profile: object = None,
    model: object = None,
    codex_profile: object = None,
    config_overrides: object = None,
    token_budget_hint: object = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if execution_profile not in (None, ""):
        metadata["execution_profile"] = string_value("execution_profile", execution_profile)
    if model not in (None, ""):
        metadata["model"] = string_value("model", model)
    if codex_profile not in (None, ""):
        metadata["codex_profile"] = string_value("codex_profile", codex_profile)
    overrides = config_overrides_value("codex_config_overrides", config_overrides)
    if overrides:
        metadata["codex_config_overrides"] = overrides
    if token_budget_hint not in (None, ""):
        metadata["token_budget_hint"] = string_value("token_budget_hint", token_budget_hint)
    return metadata


def resolve_execution_settings(config: Any, task: dict[str, Any], *, reviewer: bool = False) -> ExecutionSettings:
    explicit_profile = task.get("execution_profile")
    profile_name = str(explicit_profile) if explicit_profile not in (None, "") else None
    profile_source = "task" if profile_name else None
    profile_reason = "explicit task execution_profile" if profile_name else None
    if profile_name is None:
        if reviewer:
            profile_name = config.review_execution_profile
            if profile_name:
                profile_source = "review_default"
                profile_reason = "reviewer Codex default profile"
        else:
            profile_name, profile_source, profile_reason = default_profile_resolution(config, task)
    profile: dict[str, Any] = {}
    if profile_name:
        profile = config.execution_profiles.get(profile_name, {})
        if not profile:
            raise ValueError(f"unknown execution profile: {profile_name}")

    overrides = dict(profile.get("config_overrides") or {})
    overrides.update(config_overrides_value("codex_config_overrides", task.get("codex_config_overrides", {})))
    return ExecutionSettings(
        profile_name=profile_name,
        profile_source=profile_source,
        profile_reason=profile_reason,
        model=task_value_or_profile(task, "model", profile),
        codex_profile=task_value_or_profile(task, "codex_profile", profile),
        config_overrides=overrides or None,
        token_budget_hint=task_value_or_profile(task, "token_budget_hint", profile),
    )


def default_profile_for_task(config: Any, task: dict[str, Any]) -> str | None:
    profile_name, _, _ = default_profile_resolution(config, task)
    return profile_name


def default_profile_resolution(config: Any, task: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    default = config.default_execution_profile
    terms = high_risk_profile_terms(task)
    if default and "deep" in config.execution_profiles and terms:
        return "deep", "high_risk_fallback", "matched category/label: " + ", ".join(terms)
    if default and "small" in config.execution_profiles and small_profile_routing_candidate(task):
        return "small", "low_risk_downshift", small_profile_routing_reason(task)
    if default:
        return default, "config_default", "default_execution_profile"
    return None, None, None


def high_risk_task(task: dict[str, Any]) -> bool:
    return bool(high_risk_profile_terms(task))


def high_risk_profile_terms(task: dict[str, Any]) -> list[str]:
    values = [task.get("category")]
    labels = task.get("labels")
    if isinstance(labels, list):
        values.extend(labels)
    normalized = {str(value).strip().lower() for value in values if value not in (None, "")}
    return sorted(normalized & HIGH_RISK_PROFILE_TERMS)


def small_profile_routing_candidate(task: dict[str, Any]) -> bool:
    return (
        normalized_task_value(task.get("routing_size")) in {"tiny", "small"}
        and normalized_task_value(task.get("routing_risk")) == "low"
        and set(normalized_task_list(task.get("verification_scope")) or ["none"]).issubset({"docs", "none"})
    )


def small_profile_routing_reason(task: dict[str, Any]) -> str:
    size = normalized_task_value(task.get("routing_size")) or "unspecified"
    risk = normalized_task_value(task.get("routing_risk")) or "unspecified"
    scopes = normalized_task_list(task.get("verification_scope")) or ["none"]
    return f"matched routing metadata: size={size} risk={risk} verify={'+'.join(sorted(scopes))}"


def normalized_task_value(value: object) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip().lower()


def normalized_task_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [normalized for item in value if (normalized := normalized_task_value(item))]


def task_value_or_profile(task: dict[str, Any], key: str, profile: dict[str, Any]) -> str | None:
    value = task.get(key)
    if value not in (None, ""):
        return string_value(key, value)
    profile_value = profile.get(key)
    if profile_value not in (None, ""):
        return string_value(key, profile_value)
    return None


def command_options(settings: ExecutionSettings) -> list[str]:
    options: list[str] = []
    if settings.model:
        options.extend(["--model", settings.model])
    if settings.codex_profile:
        options.extend(["--profile", settings.codex_profile])
    for key, value in sorted((settings.config_overrides or {}).items()):
        if key not in SAFE_CONFIG_OVERRIDE_KEYS:
            allowed = ", ".join(sorted(SAFE_CONFIG_OVERRIDE_KEYS))
            raise ValueError(f"codex_config_overrides.{key} is not allowlisted; allowed keys: {allowed}")
        options.extend(["-c", f"{key}={value}"])
    return options


def insert_command_options(command: list[str], options: list[str]) -> list[str]:
    if not options:
        return command
    try:
        exec_index = command.index("exec")
    except ValueError:
        exec_index = -1
    if exec_index >= 0:
        return [*command[: exec_index + 1], *options, *command[exec_index + 1 :]]
    try:
        resume_index = command.index("resume")
    except ValueError:
        return [*command, *options]
    return [*command[:resume_index], *options, *command[resume_index:]]


def string_value(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if not value.strip():
        raise ValueError(f"{key} must not be empty")
    return value


def optional_string_value(key: str, value: object) -> str | None:
    if value in (None, ""):
        return None
    return string_value(key, value)

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SAFE_CONFIG_OVERRIDE_KEYS = {
    "model_reasoning_effort",
    "model_reasoning_summary",
    "model_verbosity",
}
HIGH_RISK_TERMS = {
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
DERIVED_REQUIREMENT_SOURCE = "derived_from_task_vector"
REQUIREMENT_DIMENSIONS = (
    "reasoning_depth",
    "context_need",
    "tool_reliability",
    "latency_priority",
    "cost_sensitivity",
    "review_strictness",
)
REQUIREMENT_LEVELS = {"unknown", "low", "medium", "high"}


@dataclass(frozen=True)
class ResolvedExecutionConfig:
    requirement_vector: dict[str, Any]
    selection_rule: str | None = None
    selection_reason: str | None = None
    model: str | None = None
    model_source: str = "unknown"
    execution_target: str | None = None
    codex_profile: str | None = None
    config_overrides: dict[str, str] | None = None
    budget_hint: str | None = None
    worker_role: str = "implementer"


def model_requirement_vector_value(key: str, value: object) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    raw_dimensions = value.get("dimensions", value)
    if not isinstance(raw_dimensions, dict):
        raise ValueError(f"{key}.dimensions must be an object")
    dimensions = normalize_requirement_dimensions(f"{key}.dimensions", raw_dimensions)
    vector: dict[str, Any] = {
        "schema_version": 1,
        "source": optional_string_value(f"{key}.source", value.get("source")) or "explicit",
        "confidence": normalized_level(f"{key}.confidence", value.get("confidence") or "medium"),
        "dimensions": dimensions,
    }
    derived_from = value.get("derived_from")
    if derived_from not in (None, ""):
        vector["derived_from"] = string_list_value(f"{key}.derived_from", derived_from)
    rationale = optional_string_value(f"{key}.rationale", value.get("rationale"))
    if rationale:
        vector["rationale"] = rationale
    return vector


def normalize_requirement_dimensions(key: str, value: dict[str, Any]) -> dict[str, str]:
    dimensions: dict[str, str] = {}
    for dimension in REQUIREMENT_DIMENSIONS:
        dimensions[dimension] = normalized_level(f"{key}.{dimension}", value.get(dimension) or "unknown")
    unknown = sorted(str(name) for name in value if name not in REQUIREMENT_DIMENSIONS)
    if unknown:
        raise ValueError(f"{key} contains unknown dimensions: {', '.join(unknown)}")
    return dimensions


def model_selection_rules_value(value: object) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("model_selection_rules must be a list")
    return [model_selection_rule_value(index, item) for index, item in enumerate(value)]


def model_selection_rule_value(index: int, value: object) -> dict[str, Any]:
    key = f"model_selection_rules[{index}]"
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    name = optional_string_value(f"{key}.name", value.get("name")) or f"rule-{index}"
    raw_when = value.get("when", {})
    if not isinstance(raw_when, dict):
        raise ValueError(f"{key}.when must be an object")
    when = {dimension: level_match_value(f"{key}.when.{dimension}", raw_when[dimension]) for dimension in raw_when}
    unknown = sorted(str(name) for name in when if name not in REQUIREMENT_DIMENSIONS)
    if unknown:
        raise ValueError(f"{key}.when contains unknown dimensions: {', '.join(unknown)}")
    selection = execution_config_value(key, value)
    return {"name": name, "when": when, **selection}


def execution_config_value(key: str, value: object) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    config: dict[str, Any] = {}
    if "execution_target" in value:
        config["execution_target"] = optional_string_value(f"{key}.execution_target", value.get("execution_target"))
    if "model" in value:
        config["model"] = optional_string_value(f"{key}.model", value.get("model"))
    if "codex_profile" in value:
        config["codex_profile"] = optional_string_value(f"{key}.codex_profile", value.get("codex_profile"))
    raw_overrides = value.get("config_overrides", {})
    overrides = config_overrides_value(f"{key}.config_overrides", raw_overrides)
    if overrides:
        config["config_overrides"] = overrides
    if "budget_hint" in value:
        config["budget_hint"] = optional_string_value(f"{key}.budget_hint", value.get("budget_hint"))
    if config.get("execution_target"):
        direct = [name for name in ("model", "codex_profile", "config_overrides", "budget_hint") if config.get(name)]
        if direct:
            raise ValueError(f"{key}.execution_target cannot be combined with direct execution config: {', '.join(direct)}")
    return {item_key: item for item_key, item in config.items() if item not in (None, {}, "")}


def execution_targets_value(value: object) -> dict[str, dict[str, Any]]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("execution_targets must be an object")
    targets: dict[str, dict[str, Any]] = {}
    for raw_name, raw_config in value.items():
        name = string_value("execution_targets key", raw_name)
        targets[name] = execution_target_definition_value(f"execution_targets.{name}", raw_config)
    return targets


def execution_target_definition_value(key: str, value: object) -> dict[str, Any]:
    config = execution_config_value(key, value)
    if config.get("execution_target"):
        raise ValueError(f"{key}.execution_target is not allowed inside an execution target definition")
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    if "model_source" in value:
        model_source = optional_string_value(f"{key}.model_source", value.get("model_source"))
        if model_source != "cli_default":
            raise ValueError(f"{key}.model_source must be cli_default")
        if config.get("model"):
            raise ValueError(f"{key}.model_source cannot be combined with model")
        if model_source:
            config["model_source"] = model_source
    if "freshness" in value:
        freshness = freshness_metadata_value(f"{key}.freshness", value.get("freshness"))
        if freshness:
            config["freshness"] = freshness
    return config


def freshness_metadata_value(key: str, value: object) -> dict[str, Any]:
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    metadata: dict[str, Any] = {}
    if "owner" in value:
        metadata["owner"] = optional_string_value(f"{key}.owner", value.get("owner"))
    if "last_reviewed_at" in value:
        metadata["last_reviewed_at"] = optional_string_value(f"{key}.last_reviewed_at", value.get("last_reviewed_at"))
    if "review_after_days" in value:
        metadata["review_after_days"] = positive_int_value(f"{key}.review_after_days", value.get("review_after_days"))
    return {item_key: item for item_key, item in metadata.items() if item not in (None, "", {})}


def positive_int_value(key: str, value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return parsed


def task_requirement_metadata(*, model_requirement_vector: object = None) -> dict[str, Any]:
    vector = model_requirement_vector_value("model_requirement_vector", model_requirement_vector)
    return {"model_requirement_vector": vector} if vector else {}


def resolve_execution_config(config: Any, task: dict[str, Any], *, reviewer: bool = False) -> ResolvedExecutionConfig:
    worker_role = "reviewer" if reviewer else "implementer"
    vector = resolve_model_requirement_vector(config, task, reviewer=reviewer)
    selection = select_execution_config(config, vector)
    resolved_selection = resolve_execution_target(config, selection)
    return ResolvedExecutionConfig(
        requirement_vector=vector,
        selection_rule=resolved_selection.get("selection_rule"),
        selection_reason=resolved_selection.get("selection_reason"),
        model=resolved_selection.get("model"),
        model_source=model_source_for_selection(resolved_selection),
        execution_target=resolved_selection.get("execution_target"),
        codex_profile=resolved_selection.get("codex_profile"),
        config_overrides=resolved_selection.get("config_overrides") or None,
        budget_hint=resolved_selection.get("budget_hint"),
        worker_role=worker_role,
    )


def resolve_model_requirement_vector(config: Any, task: dict[str, Any], *, reviewer: bool = False) -> dict[str, Any]:
    explicit = task.get("model_requirement_vector")
    if explicit not in (None, "", {}):
        vector = model_requirement_vector_value("model_requirement_vector", explicit)
        if vector.get("source") == DERIVED_REQUIREMENT_SOURCE:
            return derive_model_requirement_vector(task, reviewer=reviewer)
        return vector
    default = config.review_model_requirement_vector if reviewer else config.default_model_requirement_vector
    if default:
        return model_requirement_vector_value(
            "review_model_requirement_vector" if reviewer else "default_model_requirement_vector",
            default,
        )
    return derive_model_requirement_vector(task, reviewer=reviewer)


def derive_model_requirement_vector(task: dict[str, Any], *, reviewer: bool = False) -> dict[str, Any]:
    dimensions = {dimension: "medium" for dimension in REQUIREMENT_DIMENSIONS}
    source_fields = ["routing_size", "routing_risk", "verification_scope", "category", "labels"]
    if low_cost_candidate(task):
        dimensions.update(
            reasoning_depth="low",
            context_need="low",
            latency_priority="high",
            cost_sensitivity="high",
            tool_reliability="medium",
        )
    if high_risk_terms(task) or normalized_task_value(task.get("routing_risk")) == "high":
        dimensions.update(reasoning_depth="high", context_need="high", tool_reliability="high", cost_sensitivity="low")
    if reviewer:
        dimensions.update(reasoning_depth="high", tool_reliability="high", review_strictness="high")
        source_fields.append("worker_role")
    return {
        "schema_version": 1,
        "source": DERIVED_REQUIREMENT_SOURCE,
        "confidence": "medium",
        "derived_from": source_fields,
        "dimensions": dimensions,
    }


def select_execution_config(config: Any, vector: dict[str, Any]) -> dict[str, Any]:
    dimensions = vector.get("dimensions") if isinstance(vector.get("dimensions"), dict) else {}
    for rule in config.model_selection_rules:
        if rule_matches(rule.get("when", {}), dimensions):
            selection = {key: value for key, value in rule.items() if key not in {"name", "when"}}
            selection["selection_rule"] = rule.get("name")
            selection["selection_reason"] = "matched model_requirement_vector"
            return selection
    default = dict(config.default_execution_config)
    if default:
        default["selection_rule"] = "default_execution_config"
        default["selection_reason"] = "no model_selection_rule matched"
    return default


def resolve_execution_target(config: Any, selection: dict[str, Any]) -> dict[str, Any]:
    alias = selection.get("execution_target")
    if not alias:
        return selection
    targets = getattr(config, "execution_targets", {}) or {}
    target = targets.get(alias)
    if target is None:
        raise ValueError(f"execution_target {alias!r} is not configured")
    resolved = {
        key: value
        for key, value in target.items()
        if key in {"model", "codex_profile", "config_overrides", "budget_hint"}
    }
    resolved["execution_target"] = alias
    if selection.get("selection_rule"):
        resolved["selection_rule"] = selection.get("selection_rule")
    if selection.get("selection_reason"):
        resolved["selection_reason"] = selection.get("selection_reason")
    return resolved


def model_source_for_selection(selection: dict[str, Any]) -> str:
    if selection.get("execution_target"):
        return "target_alias"
    if selection.get("model"):
        return "explicit_model"
    return "cli_default"


def rule_matches(when: object, dimensions: dict[str, Any]) -> bool:
    if not isinstance(when, dict):
        return False
    for dimension, allowed in when.items():
        value = str(dimensions.get(dimension) or "unknown")
        if isinstance(allowed, list):
            if value not in allowed:
                return False
        elif value != allowed:
            return False
    return True


def low_cost_candidate(task: dict[str, Any]) -> bool:
    return (
        normalized_task_value(task.get("routing_size")) in {"tiny", "small"}
        and normalized_task_value(task.get("routing_risk")) == "low"
        and set(normalized_task_list(task.get("verification_scope")) or ["none"]).issubset({"docs", "none"})
    )


def high_risk_terms(task: dict[str, Any]) -> list[str]:
    values = [task.get("category")]
    labels = task.get("labels")
    if isinstance(labels, list):
        values.extend(labels)
    normalized = {str(value).strip().lower() for value in values if value not in (None, "")}
    return sorted(normalized & HIGH_RISK_TERMS)


def command_options(settings: ResolvedExecutionConfig) -> list[str]:
    options: list[str] = []
    if settings.model:
        options.extend(["--model", settings.model])
    if settings.codex_profile:
        options.extend(["--profile", settings.codex_profile])
    for key, value in sorted((settings.config_overrides or {}).items()):
        if key not in SAFE_CONFIG_OVERRIDE_KEYS:
            allowed = ", ".join(sorted(SAFE_CONFIG_OVERRIDE_KEYS))
            raise ValueError(f"config_overrides.{key} is not allowlisted; allowed keys: {allowed}")
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


def level_match_value(key: str, value: object) -> str | list[str]:
    if isinstance(value, list):
        if not value:
            raise ValueError(f"{key} must not be empty")
        return [normalized_level(f"{key}[]", item) for item in value]
    return normalized_level(key, value)


def normalized_level(key: str, value: object) -> str:
    level = string_value(key, value).strip().lower()
    if level not in REQUIREMENT_LEVELS:
        raise ValueError(f"{key} must be one of: {', '.join(sorted(REQUIREMENT_LEVELS))}")
    return level


def normalized_task_value(value: object) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip().lower()


def normalized_task_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [normalized for item in value if (normalized := normalized_task_value(item))]


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


def string_list_value(key: str, value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return value

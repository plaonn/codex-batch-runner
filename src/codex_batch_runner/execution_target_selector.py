from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


SELECTION_POLICY_VERSION = "execution-target-selector-v1"
FAILURE_TAXONOMY = (
    "invalid_requirement_vector",
    "stale_model_inventory",
    "no_eligible_model",
    "insufficient_quality_evidence",
    "ambiguous_candidates",
    "below_quality_floor",
    "selected_model_unavailable",
    "selected_command_mismatch",
    "provider_model_mismatch",
    "explicit_fallback_exhausted",
    "exploration_not_admissible",
    "manual_pin_unavailable",
)
TRUST_STATES = {"trusted", "probe_only", "degraded", "unavailable", "cooldown", "unknown"}
ELIGIBLE_TRUST_STATES = {"trusted"}
CONSTRAINT_SOURCES = {"provider_declared", "surface_reported", "operator_verified", "empirically_observed", "unknown"}
UNKNOWN_POLICIES = {"reject", "probe_only", "soft_penalty", "ignore"}
QUALITY_AXES = {
    "semantic_reasoning", "context_integration", "planning_depth", "instruction_fidelity",
    "tool_execution_reliability", "adversarial_detection",
}
HARD_CONSTRAINTS = {
    "required_execution_surfaces", "required_tools", "minimum_context_tokens", "allowed_reasoning_efforts",
    "forbidden_provider_families", "interactive_input_required", "independent_provider_required",
}


class TargetSelectionError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SelectedExecutionTarget:
    target_id: str
    target: dict[str, Any]
    selection_reason: str


def execution_target_inventory_value(value: object) -> dict[str, Any]:
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("execution_target_inventory must be an object")
    allowed = {"schema_version", "snapshot_id", "status", "constraint_registry_version", "targets"}
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValueError(f"execution_target_inventory contains unknown keys: {', '.join(unknown)}")
    if value.get("schema_version") != 1:
        raise ValueError("execution_target_inventory.schema_version must be 1")
    snapshot_id = _string("execution_target_inventory.snapshot_id", value.get("snapshot_id"))
    status = value.get("status")
    if status not in {"current", "stale"}:
        raise ValueError("execution_target_inventory.status must be current or stale")
    registry_version = _string(
        "execution_target_inventory.constraint_registry_version", value.get("constraint_registry_version")
    )
    raw_targets = value.get("targets")
    if not isinstance(raw_targets, dict) or not raw_targets:
        raise ValueError("execution_target_inventory.targets must be a non-empty object")
    targets = {
        _string("execution_target_inventory.targets key", target_id): target_value(
            f"execution_target_inventory.targets.{target_id}", target_id, raw
        )
        for target_id, raw in raw_targets.items()
    }
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "status": status,
        "constraint_registry_version": registry_version,
        "targets": targets,
    }


def constraint_registry_value(value: object) -> dict[str, Any]:
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("constraint_registry must be an object")
    if value.get("schema_version") != 1:
        raise ValueError("constraint_registry.schema_version must be 1")
    version = _string("constraint_registry.version", value.get("version"))
    raw_constraints = value.get("constraints")
    if not isinstance(raw_constraints, dict):
        raise ValueError("constraint_registry.constraints must be an object")
    constraints: dict[str, Any] = {}
    for name, raw in raw_constraints.items():
        key = f"constraint_registry.constraints.{name}"
        if name not in HARD_CONSTRAINTS:
            raise ValueError(f"{key} is not a known hard constraint")
        if not isinstance(raw, dict):
            raise ValueError(f"{key} must be an object")
        policy = raw.get("unknown_policy")
        if policy not in UNKNOWN_POLICIES:
            raise ValueError(f"{key}.unknown_policy must be one of: {', '.join(sorted(UNKNOWN_POLICIES))}")
        constraints[str(name)] = {"unknown_policy": policy}
    return {"schema_version": 1, "version": version, "constraints": constraints}


def target_value(key: str, target_id: object, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    target_id = _string(f"{key}.target_id", value.get("target_id", target_id))
    if target_id != str(key).rsplit(".", 1)[-1]:
        raise ValueError(f"{key}.target_id must match inventory key")
    surface = _string(f"{key}.execution_surface", value.get("execution_surface"))
    trust_state = value.get("trust_state")
    if trust_state not in TRUST_STATES:
        raise ValueError(f"{key}.trust_state must be one of: {', '.join(sorted(TRUST_STATES))}")
    target = dict(value)
    target["target_id"] = target_id
    target["execution_surface"] = surface
    target["trust_state"] = trust_state
    if surface == "codex":
        if value.get("model_source") == "cli_default" or not value.get("model") or not value.get("reasoning_effort"):
            raise ValueError(f"{key} automatic codex target requires exact model and reasoning_effort")
        target["model"] = _string(f"{key}.model", value.get("model"))
        target["reasoning_effort"] = _string(f"{key}.reasoning_effort", value.get("reasoning_effort"))
    else:
        backend = value.get("execution_backend")
        if backend not in {"external-json-command", "shell"}:
            raise ValueError(f"{key}.execution_backend must be external-json-command or shell")
        command_key = "external_command" if backend == "external-json-command" else "shell_command"
        command = value.get(command_key)
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError(f"{key}.{command_key} must be a non-empty list of strings")
    for numeric in ("latency_score", "cost_score"):
        raw = value.get(numeric, 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 1000:
            raise ValueError(f"{key}.{numeric} must be an integer from 0 to 1000")
        target[numeric] = raw
    quality_status = value.get("quality_evidence_status", "static_non_learned")
    if quality_status not in {"static_non_learned", "insufficient"}:
        raise ValueError(f"{key}.quality_evidence_status must be static_non_learned or insufficient")
    target["quality_evidence_status"] = quality_status
    raw_fitness = value.get("static_fitness", {})
    if not isinstance(raw_fitness, dict) or (quality_status == "static_non_learned" and set(raw_fitness) != QUALITY_AXES):
        raise ValueError(f"{key}.static_fitness must define every quality axis for static_non_learned targets")
    static_fitness: dict[str, int] = {}
    for axis, raw in raw_fitness.items():
        if axis not in QUALITY_AXES or isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 1000:
            raise ValueError(f"{key}.static_fitness.{axis} must be a known quality axis scored from 0 to 1000")
        static_fitness[axis] = raw
    target["static_fitness"] = static_fitness
    capabilities = value.get("capabilities", {})
    evidence = value.get("capability_evidence", {})
    if not isinstance(capabilities, dict) or not isinstance(evidence, dict):
        raise ValueError(f"{key}.capabilities and capability_evidence must be objects")
    for constraint, item in evidence.items():
        if not isinstance(item, dict) or item.get("source", "unknown") not in CONSTRAINT_SOURCES:
            raise ValueError(f"{key}.capability_evidence.{constraint}.source is invalid")
    target["capabilities"] = capabilities
    target["capability_evidence"] = evidence
    target["fitness_source"] = "static_non_learned" if quality_status == "static_non_learned" else "none"
    return target


def select_execution_target(config: Any, task: dict[str, Any], requirement: dict[str, Any]) -> SelectedExecutionTarget | None:
    inventory = getattr(config, "execution_target_inventory", {}) or {}
    if not inventory:
        return None
    if requirement.get("schema_version") != 2:
        raise TargetSelectionError("invalid_requirement_vector", "automatic target selection requires requirement v2")
    if requirement.get("derivation_identity", {}).get("kind") == "legacy-derived":
        return None
    registry = getattr(config, "constraint_registry", {}) or {}
    if inventory.get("status") != "current":
        raise TargetSelectionError("stale_model_inventory", "inventory snapshot is marked stale")
    if registry.get("version") != inventory.get("constraint_registry_version"):
        raise TargetSelectionError("stale_model_inventory", "inventory and constraint registry versions differ")
    candidates: list[tuple[str, dict[str, Any]]] = []
    for target_id, target in inventory["targets"].items():
        if target.get("trust_state") not in ELIGIBLE_TRUST_STATES:
            continue
        if _hard_constraints_pass(requirement.get("hard_constraints", {}), target, registry):
            candidates.append((target_id, target))
    if not candidates:
        raise TargetSelectionError("no_eligible_model", "no trusted target satisfies all hard constraints")
    quality_requirements = requirement.get("quality_requirements", {})
    evidenced_candidates = [
        (name, target) for name, target in candidates if target["quality_evidence_status"] == "static_non_learned"
    ]
    if not evidenced_candidates:
        raise TargetSelectionError("insufficient_quality_evidence", "eligible targets have no D2 cold-start static fitness")
    quality_candidates = [
        (name, target) for name, target in evidenced_candidates
        if all(
            target["static_fitness"].get(axis, -1) >= axis_requirement.get("score", 0)
            for axis, axis_requirement in quality_requirements.items() if isinstance(axis_requirement, dict)
        )
    ]
    override = task.get("routing_override") if isinstance(task.get("routing_override"), dict) else None
    if override and override.get("mode") == "pin" and str(override.get("target_id") or "") not in dict(quality_candidates):
        raise TargetSelectionError("manual_pin_unavailable", "pinned target is missing, unavailable, ineligible, or below quality floor")
    if not quality_candidates:
        raise TargetSelectionError("below_quality_floor", "no eligible target meets cold-start static quality floor")
    preferences = requirement.get("utility_preferences", {})
    latency_weight = int(preferences.get("latency_weight", 0))
    cost_weight = int(preferences.get("cost_weight", 0))
    ranked = sorted(
        quality_candidates,
        key=lambda item: (
            -(item[1]["latency_score"] * latency_weight + item[1]["cost_score"] * cost_weight),
            -min(item[1]["static_fitness"].values(), default=0),
            item[0],
        ),
    )
    if override:
        preferred = str(override.get("target_id") or "")
        eligible = dict(ranked)
        if preferred in eligible:
            return SelectedExecutionTarget(preferred, eligible[preferred], f"operator_{override['mode']}")
        if not override.get("allow_fallback"):
            raise TargetSelectionError("explicit_fallback_exhausted", "preferred target is unavailable and fallback is disabled")
        return SelectedExecutionTarget(ranked[0][0], ranked[0][1], "operator_preference_fallback")
    return SelectedExecutionTarget(ranked[0][0], ranked[0][1], "automatic_static_non_learned")


def _hard_constraints_pass(requirements: object, target: dict[str, Any], registry: dict[str, Any]) -> bool:
    if not isinstance(requirements, dict):
        return False
    capabilities = target.get("capabilities", {})
    evidence = target.get("capability_evidence", {})
    for name, required in requirements.items():
        rule = registry.get("constraints", {}).get(name)
        if not rule:
            return False
        actual = _constraint_actual(name, target, capabilities)
        source = evidence.get(name, {}).get("source") if isinstance(evidence.get(name), dict) else "unknown"
        trusted = source in {"provider_declared", "surface_reported"} or (
            source == "operator_verified" and _operator_evidence_is_fresh(evidence.get(name))
        )
        if not trusted:
            policy = rule["unknown_policy"]
            if policy in {"reject", "probe_only", "soft_penalty"}:
                return False
            if policy == "ignore":
                continue
        if not _constraint_matches(name, required, actual):
            return False
    return True


def _constraint_actual(name: str, target: dict[str, Any], capabilities: dict[str, Any]) -> Any:
    if name == "required_execution_surfaces":
        return target.get("execution_surface")
    if name == "allowed_reasoning_efforts":
        return target.get("reasoning_effort")
    if name == "forbidden_provider_families":
        return target.get("provider_family")
    return capabilities.get(name)


def _constraint_matches(name: str, required: Any, actual: Any) -> bool:
    if name in {"required_execution_surfaces", "allowed_reasoning_efforts"}:
        return isinstance(required, list) and actual in required
    if name == "required_tools":
        return isinstance(required, list) and isinstance(actual, list) and set(required).issubset(set(actual))
    if name == "forbidden_provider_families":
        return actual not in set(required or [])
    if name == "minimum_context_tokens":
        return isinstance(actual, int) and actual >= required
    return actual == required


def _operator_evidence_is_fresh(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    expires_at = value.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return parsed.astimezone(timezone.utc) > datetime.now(timezone.utc)


def _string(key: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()

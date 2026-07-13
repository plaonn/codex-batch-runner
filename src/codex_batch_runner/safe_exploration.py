from __future__ import annotations

import hashlib
import json
from typing import Any

EXPLORATION_DECISION_SCHEMA_VERSION = 1
EXPLORATION_CONTRACT_VERSION = "safe-exploration-v1"
PROBE_KINDS = {"downshift_probe", "availability_probe", "uncertainty_probe", "upshift_guard"}
ALLOWED_TARGET_STATES = {"probe_only", "trusted"}
PROHIBITED_BOUNDARIES = {
    "credentials",
    "deployment",
    "destructive",
    "financial",
    "privacy",
    "public_private",
    "security",
}


class ExplorationError(ValueError):
    pass


def exploration_admission(context: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Evaluate bounded exploration; policy values are explicit reviewed inputs."""
    _validate_policy(policy)
    budget_remaining = _non_negative_integer(context, "budget_remaining")
    active_project_probes = _non_negative_integer(context, "active_project_probes")
    reasons: list[str] = []
    if context.get("probe_kind") not in PROBE_KINDS:
        reasons.append("invalid_probe_kind")
    if not (context.get("hard_constraints_pass") or context.get("unknown_policy") == "probe_only"):
        reasons.append("hard_constraints_not_eligible")
    if context.get("failure_cost") not in {"low", "medium"}:
        reasons.append("failure_cost_not_eligible")
    raw_boundaries = context.get("boundaries")
    if not isinstance(raw_boundaries, list) or any(
        not isinstance(item, str) or not item for item in raw_boundaries
    ):
        raise ExplorationError("boundaries must be a list of non-empty strings")
    boundaries = set(raw_boundaries)
    if boundaries & PROHIBITED_BOUNDARIES:
        reasons.append("prohibited_boundary")
    if context.get("objective_verification") != "strong":
        reasons.append("objective_verification_not_strong")
    if not context.get("rollback_available") or not context.get("baseline_fallback_available"):
        reasons.append("recovery_guard_missing")
    if budget_remaining <= 0:
        reasons.append("budget_exhausted")
    if context.get("target_state") not in ALLOWED_TARGET_STATES:
        reasons.append("target_state_not_eligible")
    if active_project_probes >= 1:
        reasons.append("project_probe_concurrency_limit")
    if context.get("same_target_region_adverse"):
        reasons.append("same_target_region_cooldown")
    candidates = context.get("eligible_candidates")
    if not isinstance(candidates, list) or not candidates or any(not isinstance(item, str) or not item for item in candidates):
        reasons.append("eligible_candidates_missing")
    if context.get("chosen_target") not in (candidates or []):
        reasons.append("chosen_target_not_eligible")
    if not isinstance(context.get("baseline_target"), str) or not context["baseline_target"]:
        reasons.append("baseline_target_missing")
    return {
        "schema_version": EXPLORATION_DECISION_SCHEMA_VERSION,
        "contract_version": EXPLORATION_CONTRACT_VERSION,
        "kind": "exploration_admission",
        "admitted": not reasons,
        "reasons": sorted(set(reasons)),
        "exploration_policy_version": policy["exploration_policy_version"],
        "selection_probability": policy["selection_probability"] if not reasons else None,
        "eligible_candidates": list(candidates or []),
        "chosen_target": context.get("chosen_target"),
        "baseline_target": context.get("baseline_target"),
        "probe_kind": context.get("probe_kind"),
        "cooldown_required": bool(context.get("same_target_region_adverse")),
        "mutation": {"routing_policy": False, "active_config": False},
    }


def build_probe_record(admission: dict[str, Any], *, project_key: str, requirement_region_id: str) -> dict[str, Any]:
    if not admission.get("admitted"):
        raise ExplorationError("cannot record a probe that was not admitted")
    probability = admission.get("selection_probability")
    if not isinstance(probability, (int, float)) or not 0 < probability <= 1:
        raise ExplorationError("selection probability is required for comparable probe evidence")
    payload = {
        "schema_version": 1,
        "contract_version": "exploration-evidence-v1",
        "kind": "exploration_probe",
        "project_key": project_key,
        "requirement_region_id": requirement_region_id,
        "probe_kind": admission["probe_kind"],
        "eligible_candidates": admission["eligible_candidates"],
        "chosen_target": admission["chosen_target"],
        "baseline_target": admission["baseline_target"],
        "selection_probability": probability,
        "exploration_policy_version": admission["exploration_policy_version"],
        "causally_comparable": True,
    }
    payload["probe_id"] = _stable_id(payload)
    return payload


def _validate_policy(policy: dict[str, Any]) -> None:
    if not isinstance(policy.get("exploration_policy_version"), str) or not policy["exploration_policy_version"]:
        raise ExplorationError("versioned exploration policy is required")
    probability = policy.get("selection_probability")
    if not isinstance(probability, (int, float)) or isinstance(probability, bool) or not 0 < probability <= 1:
        raise ExplorationError("explicit reviewed selection probability is required")


def _non_negative_integer(context: dict[str, Any], field: str) -> int:
    value = context.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExplorationError(f"{field} must be a non-negative integer")
    return value


def _stable_id(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:16]

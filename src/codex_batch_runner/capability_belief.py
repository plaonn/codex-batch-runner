from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable

OUTCOME_PROJECTION_SCHEMA_VERSION = 1
OUTCOME_PROJECTION_CONTRACT_VERSION = "outcome-projection-v1"
POSTERIOR_MODEL_VERSION = "capability-posterior-v1"
COHORT_DEFINITION_VERSION = "capability-cohort-v1"
REGION_DEFINITION_VERSION = "requirement-anchor-region-v1"
TOKEN_LATENCY_SUMMARY_VERSION = "token-latency-summary-v1"
DRIFT_CONTRACT_VERSION = "capability-drift-v1"
AVAILABILITY_CONTRACT_VERSION = "availability-belief-v1"

ANCHORS = {0, 250, 500, 750, 1000}
QUALITY_OUTCOMES = {"first_pass_pass", "minor_fix", "major_fix", "reject", "indeterminate"}
AVAILABILITY_OUTCOMES = {"available", "auth_failure", "quota_failure", "timeout", "provider_outage", "cancelled"}
QUALITY_AVAILABILITY_EXCLUSIONS = AVAILABILITY_OUTCOMES - {"available"}
TRUST_TRANSITIONS = {
    "unknown": {"probe_only"},
    "probe_only": {"trusted", "unavailable"},
    "trusted": {"degraded"},
    "degraded": {"probe_only"},
    "unavailable": {"cooldown"},
    "cooldown": {"probe_only"},
}
TOKEN_FIELDS = ("cached_input", "uncached_input", "output", "reasoning")
REQUIRED_VERSION_FIELDS = {
    "execution_evidence", "requirement", "rubric", "constraint_registry", "target_contract",
    "quality_outcome", "review_policy", "review_rubric", "posterior", "decay", "drift",
    "exploration", "cohort",
}
FORBIDDEN_KEYS = {"prompt", "transcript", "stdout", "stderr", "session_id", "thread_id", "cwd", "log_path"}


class CapabilityBeliefError(ValueError):
    pass


def build_outcome_projection(
    *,
    root_lineage_id: str,
    attempt: int,
    captured_at: str,
    target_id: str,
    requirement_region: dict[str, int],
    versions: dict[str, str],
    epoch: dict[str, str],
    first_pass_outcome: str,
    recovery_outcome: str,
    availability_outcome: str = "available",
    token_usage: dict[str, int | None] | None = None,
    latency_seconds: float | None = None,
    latency_censored: bool = False,
    first_pass_captured_at: str | None = None,
) -> dict[str, Any]:
    """Build one public-safe append-only projection from canonical raw evidence."""
    region = _validate_region(requirement_region)
    if not REQUIRED_VERSION_FIELDS.issubset(versions) or any(not isinstance(versions[key], str) or not versions[key] for key in REQUIRED_VERSION_FIELDS):
        raise CapabilityBeliefError("outcome projection requires all versioned contract boundaries")
    if versions["execution_evidence"] != "execution-evidence-v3":
        raise CapabilityBeliefError("only exact execution evidence v3 may enter capability cohorts")
    if first_pass_outcome not in QUALITY_OUTCOMES or recovery_outcome not in QUALITY_OUTCOMES:
        raise CapabilityBeliefError("invalid quality outcome")
    if availability_outcome not in AVAILABILITY_OUTCOMES:
        raise CapabilityBeliefError("invalid availability outcome")
    if attempt < 0 or not root_lineage_id or not target_id:
        raise CapabilityBeliefError("root lineage, non-negative attempt, and target are required")
    _parse_time(captured_at)
    first_pass_at = first_pass_captured_at or captured_at
    if _parse_time(first_pass_at) > _parse_time(captured_at):
        raise CapabilityBeliefError("first-pass timestamp cannot follow projection timestamp")
    epoch_required = {"epoch_id", "model_alias", "cli_major", "provider_behavior", "target_contract", "review_outcome_contract"}
    if not epoch_required.issubset(epoch) or any(not isinstance(epoch[key], str) or not epoch[key] for key in epoch_required):
        raise CapabilityBeliefError("epoch requires all drift boundary components")
    tokens = _validate_tokens(token_usage or {})
    if latency_seconds is not None and (not isinstance(latency_seconds, (int, float)) or latency_seconds < 0):
        raise CapabilityBeliefError("latency must be non-negative")
    if availability_outcome in {"timeout", "cancelled"} and not latency_censored:
        raise CapabilityBeliefError("timeout and cancelled latency must be censored")
    comparable_quality = availability_outcome not in QUALITY_AVAILABILITY_EXCLUSIONS
    record = {
        "schema_version": OUTCOME_PROJECTION_SCHEMA_VERSION,
        "contract_version": OUTCOME_PROJECTION_CONTRACT_VERSION,
        "kind": "outcome_projection",
        "projection_id": _stable_id({"root": root_lineage_id, "attempt": attempt, "captured_at": captured_at, "target": target_id}),
        "root_lineage_id": root_lineage_id,
        "attempt": attempt,
        "captured_at": captured_at,
        "target_id": target_id,
        "requirement_region": region,
        "versions": dict(sorted(versions.items())),
        "epoch": dict(sorted(epoch.items())),
        "quality": {
            "first_pass": first_pass_outcome,
            "first_pass_captured_at": first_pass_at,
            "recovery_inclusive": recovery_outcome,
            "beta_eligible": comparable_quality and first_pass_outcome != "indeterminate",
            "availability_excluded": not comparable_quality,
        },
        "availability": {"outcome": availability_outcome},
        "token_usage": tokens,
        "latency": {"seconds": float(latency_seconds) if latency_seconds is not None else None, "censored": bool(latency_censored)},
        "privacy": {"public_safe": True, "raw_evidence_included": False},
    }
    record["cohort"] = _cohort(record)
    validate_outcome_projection(record)
    return record


def validate_outcome_projection(record: object) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("schema_version") != OUTCOME_PROJECTION_SCHEMA_VERSION or record.get("contract_version") != OUTCOME_PROJECTION_CONTRACT_VERSION:
        raise CapabilityBeliefError("invalid outcome projection contract")
    for key in FORBIDDEN_KEYS:
        if _contains_key(record, key):
            raise CapabilityBeliefError(f"outcome projection contains forbidden key: {key}")
    required = {"projection_id", "root_lineage_id", "attempt", "captured_at", "target_id", "requirement_region", "versions", "epoch", "quality", "availability", "token_usage", "latency", "privacy", "cohort"}
    if not required.issubset(record):
        raise CapabilityBeliefError("outcome projection is missing required fields")
    if not isinstance(record["root_lineage_id"], str) or not record["root_lineage_id"] or not isinstance(record["target_id"], str) or not record["target_id"]:
        raise CapabilityBeliefError("invalid projection lineage or target")
    if not isinstance(record["attempt"], int) or isinstance(record["attempt"], bool) or record["attempt"] < 0:
        raise CapabilityBeliefError("invalid projection attempt")
    _parse_time(record["captured_at"])
    _validate_region(record.get("requirement_region"))
    versions = record.get("versions")
    if not isinstance(versions, dict) or not REQUIRED_VERSION_FIELDS.issubset(versions) or any(not isinstance(versions[key], str) or not versions[key] for key in REQUIRED_VERSION_FIELDS):
        raise CapabilityBeliefError("outcome projection requires all versioned contract boundaries")
    if versions.get("execution_evidence") != "execution-evidence-v3":
        raise CapabilityBeliefError("only exact execution evidence v3 may enter capability cohorts")
    epoch = record.get("epoch")
    epoch_required = {"epoch_id", "model_alias", "cli_major", "provider_behavior", "target_contract", "review_outcome_contract"}
    if not isinstance(epoch, dict) or not epoch_required.issubset(epoch) or any(not isinstance(epoch[key], str) or not epoch[key] for key in epoch_required):
        raise CapabilityBeliefError("invalid projection epoch")
    quality = record.get("quality")
    if not isinstance(quality, dict) or quality.get("first_pass") not in QUALITY_OUTCOMES or quality.get("recovery_inclusive") not in QUALITY_OUTCOMES:
        raise CapabilityBeliefError("invalid projection quality")
    if _parse_time(quality.get("first_pass_captured_at")) > _parse_time(record["captured_at"]):
        raise CapabilityBeliefError("first-pass timestamp cannot follow projection timestamp")
    availability = record.get("availability")
    if not isinstance(availability, dict) or availability.get("outcome") not in AVAILABILITY_OUTCOMES:
        raise CapabilityBeliefError("invalid projection availability")
    excluded = availability["outcome"] in QUALITY_AVAILABILITY_EXCLUSIONS
    if quality.get("availability_excluded") is not excluded or quality.get("beta_eligible") is not (not excluded and quality["first_pass"] != "indeterminate"):
        raise CapabilityBeliefError("derived quality eligibility is inconsistent")
    if _validate_tokens(record.get("token_usage") if isinstance(record.get("token_usage"), dict) else {}) != record.get("token_usage"):
        raise CapabilityBeliefError("invalid projection token usage")
    latency = record.get("latency")
    if not isinstance(latency, dict) or set(latency) != {"seconds", "censored"} or not isinstance(latency["censored"], bool):
        raise CapabilityBeliefError("invalid projection latency")
    if latency["seconds"] is not None and (not isinstance(latency["seconds"], (int, float)) or isinstance(latency["seconds"], bool) or latency["seconds"] < 0):
        raise CapabilityBeliefError("invalid projection latency")
    if availability["outcome"] in {"timeout", "cancelled"} and not latency["censored"]:
        raise CapabilityBeliefError("timeout and cancelled latency must be censored")
    expected_projection_id = _stable_id({"root": record["root_lineage_id"], "attempt": record["attempt"], "captured_at": record["captured_at"], "target": record["target_id"]})
    if record["projection_id"] != expected_projection_id:
        raise CapabilityBeliefError("projection id does not match content")
    expected_cohort = _cohort(record)
    if record.get("cohort") != expected_cohort:
        raise CapabilityBeliefError("invalid capability cohort")
    return record


def attach_outcome_projection(history: list[dict[str, Any]], record: dict[str, Any]) -> None:
    validate_outcome_projection(record)
    if not any(item.get("projection_id") == record["projection_id"] for item in history if isinstance(item, dict)):
        history.append(record)


def rebuild_capability_report(
    records: Iterable[dict[str, Any]], *, policy: dict[str, Any], as_of: str
) -> dict[str, Any]:
    """Rebuild derived posterior snapshots without mutating evidence or routing policy."""
    now = _parse_time(as_of)
    _validate_policy(policy)
    valid = [validate_outcome_projection(record) for record in records]
    deduped = _deduplicate_root_lineage(valid)
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in deduped:
        groups.setdefault(record["cohort"]["cohort_id"], []).append(record)
    cohorts = [_posterior(group, policy, now) for _, group in sorted(groups.items())]
    return {
        "schema_version": 1,
        "kind": "capability_belief_report",
        "posterior_model_version": POSTERIOR_MODEL_VERSION,
        "decay_policy_version": policy["decay_policy_version"],
        "drift_contract_version": DRIFT_CONTRACT_VERSION,
        "availability_contract_version": AVAILABILITY_CONTRACT_VERSION,
        "drift_policy_version": policy["drift_policy"]["version"],
        "generated_at": as_of,
        "mode": "read_only",
        "mutation": {"allowed": False, "applied": False},
        "input_record_count": len(valid),
        "independent_root_count": len(deduped),
        "deduplicated_attempt_count": len(valid) - len(deduped),
        "cohorts": cohorts,
    }


def transition_trust_state(current: str, target: str) -> str:
    if target not in TRUST_TRANSITIONS.get(current, set()):
        raise CapabilityBeliefError(f"invalid trust transition: {current} -> {target}")
    return target


def _posterior(records: list[dict[str, Any]], policy: dict[str, Any], now: datetime) -> dict[str, Any]:
    terminal_weights = [_weight_at(record["captured_at"], policy, now) for record in records]
    quality_weights = [_weight_at(record["quality"]["first_pass_captured_at"], policy, now) for record in records]
    alpha = float(policy["beta_prior"]["alpha"])
    beta = float(policy["beta_prior"]["beta"])
    dirichlet = {key: float(policy["dirichlet_prior"][key]) for key in QUALITY_OUTCOMES}
    first_counts = {key: 0.0 for key in QUALITY_OUTCOMES}
    recovery_counts = {key: 0.0 for key in QUALITY_OUTCOMES}
    availability = {key: 0.0 for key in AVAILABILITY_OUTCOMES}
    for record, quality_weight, terminal_weight in zip(records, quality_weights, terminal_weights):
        quality = record["quality"]
        first = quality["first_pass"]
        recovery = quality["recovery_inclusive"]
        availability[record["availability"]["outcome"]] += terminal_weight
        if not quality["availability_excluded"]:
            dirichlet[first] += quality_weight
            first_counts[first] += quality_weight
            recovery_counts[recovery] += terminal_weight
            if quality["beta_eligible"]:
                if first == "first_pass_pass":
                    alpha += quality_weight
                else:
                    beta += quality_weight
    return {
        "cohort": records[0]["cohort"],
        "epoch": records[0]["epoch"],
        "sample": {"root_count": len(records), "first_pass_effective_size": _effective_size(quality_weights), "terminal_effective_size": _effective_size(terminal_weights)},
        "quality": {
            "beta_floor_pass": {"alpha": alpha, "beta": beta, "mean": alpha / (alpha + beta)},
            "dirichlet_first_pass": dirichlet,
            "weighted_first_pass_outcomes": first_counts,
            "weighted_recovery_inclusive_outcomes": recovery_counts,
        },
        "availability": {"weighted_outcomes": availability},
        "tokens": {field: _summary([(record["token_usage"][field], weight) for record, weight in zip(records, terminal_weights)]) for field in TOKEN_FIELDS},
        "latency": {
            "completed": _summary([(record["latency"]["seconds"], weight) for record, weight in zip(records, terminal_weights) if not record["latency"]["censored"]]),
            "censored": _summary([(record["latency"]["seconds"], weight) for record, weight in zip(records, terminal_weights) if record["latency"]["censored"]]),
        },
        "freshness_and_drift": _drift(records, quality_weights, policy["drift_policy"], now),
    }


def _deduplicate_root_lineage(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record["root_lineage_id"]
        existing = latest.get(key)
        if existing is not None and (
            existing["cohort"]["cohort_id"] != record["cohort"]["cohort_id"]
            or existing["quality"]["first_pass"] != record["quality"]["first_pass"]
            or existing["quality"]["first_pass_captured_at"] != record["quality"]["first_pass_captured_at"]
        ):
            raise CapabilityBeliefError("root lineage changed first-pass outcome or cohort boundary")
        if existing is None or (_parse_time(record["captured_at"]), record["attempt"], record["projection_id"]) > (_parse_time(existing["captured_at"]), existing["attempt"], existing["projection_id"]):
            latest[key] = record
    return [latest[key] for key in sorted(latest)]


def _summary(samples: list[tuple[int | float | None, float]]) -> dict[str, Any]:
    observed = [(float(value), weight) for value, weight in samples if value is not None]
    if not observed:
        return {"version": TOKEN_LATENCY_SUMMARY_VERSION, "count": 0, "weighted_count": 0.0, "effective_size": 0.0, "log1p_weighted_mean": None, "log1p_weighted_variance": None, "median": None, "p80": None, "p95": None}
    total = sum(weight for _, weight in observed)
    logs = [(math.log1p(value), weight) for value, weight in observed]
    mean = sum(value * weight for value, weight in logs) / total
    variance = sum(weight * (value - mean) ** 2 for value, weight in logs) / total
    values = sorted(value for value, _ in observed)
    return {"version": TOKEN_LATENCY_SUMMARY_VERSION, "count": len(values), "weighted_count": total, "effective_size": _effective_size([weight for _, weight in observed]), "log1p_weighted_mean": mean, "log1p_weighted_variance": variance, "median": median(values), "p80": _percentile(values, .80), "p95": _percentile(values, .95)}


def _drift(records: list[dict[str, Any]], weights: list[float], policy: dict[str, Any], now: datetime) -> dict[str, Any]:
    recent_days = float(policy["recent_window_days"])
    baseline_days = float(policy["baseline_window_days"])
    recent: list[tuple[float, float]] = []
    baseline: list[tuple[float, float]] = []
    for record, weight in zip(records, weights):
        if record["quality"]["availability_excluded"] or not record["quality"]["beta_eligible"]:
            continue
        age = (now - _parse_time(record["quality"]["first_pass_captured_at"])).total_seconds() / 86400
        success = 1.0 if record["quality"]["first_pass"] == "first_pass_pass" else 0.0
        if age <= recent_days:
            recent.append((success, weight))
        elif age <= recent_days + baseline_days:
            baseline.append((success, weight))
    recent_eff = _effective_size([weight for _, weight in recent])
    baseline_eff = _effective_size([weight for _, weight in baseline])
    recent_rate = _weighted_rate(recent)
    baseline_rate = _weighted_rate(baseline)
    enough = recent_eff >= policy["minimum_effective_samples"] and baseline_eff >= policy["minimum_effective_samples"]
    delta = (recent_rate - baseline_rate) if enough else None
    adverse = bool(enough and delta is not None and delta <= -float(policy["adverse_floor_pass_delta"]))
    latest_age = min((now - _parse_time(record["captured_at"])).total_seconds() / 86400 for record in records)
    return {
        "contract_version": DRIFT_CONTRACT_VERSION,
        "policy_version": policy["version"],
        "latest_evidence_age_days": max(0.0, latest_age),
        "fresh": latest_age <= float(policy["freshness_days"]),
        "recent_floor_pass_rate": recent_rate,
        "baseline_floor_pass_rate": baseline_rate,
        "floor_pass_delta": delta,
        "effective_samples": {"recent": recent_eff, "baseline": baseline_eff},
        "status": "adverse" if adverse else "stable" if enough else "insufficient",
        "proposed_transition": {"from": "trusted", "to": "degraded"} if adverse else None,
        "mutation": {"allowed": False, "applied": False},
    }


def _weighted_rate(samples: list[tuple[float, float]]) -> float | None:
    total = sum(weight for _, weight in samples)
    return sum(value * weight for value, weight in samples) / total if total else None


def _percentile(values: list[float], q: float) -> float:
    index = max(0, math.ceil(q * len(values)) - 1)
    return values[index]


def _weight_at(captured_at: str, policy: dict[str, Any], now: datetime) -> float:
    age_days = max(0.0, (now - _parse_time(captured_at)).total_seconds() / 86400)
    return 2 ** (-age_days / float(policy["half_life_days"]))


def _effective_size(weights: list[float]) -> float:
    return (sum(weights) ** 2 / sum(weight * weight for weight in weights)) if weights else 0.0


def _validate_policy(policy: dict[str, Any]) -> None:
    if not isinstance(policy.get("decay_policy_version"), str) or not policy["decay_policy_version"]:
        raise CapabilityBeliefError("versioned decay policy is required")
    if not isinstance(policy.get("half_life_days"), (int, float)) or policy["half_life_days"] <= 0:
        raise CapabilityBeliefError("positive reviewed half-life is required")
    beta = policy.get("beta_prior")
    prior = policy.get("dirichlet_prior")
    if not isinstance(beta, dict) or any(not isinstance(beta.get(key), (int, float)) or beta[key] <= 0 for key in ("alpha", "beta")):
        raise CapabilityBeliefError("positive reviewed Beta prior is required")
    if not isinstance(prior, dict) or set(prior) != QUALITY_OUTCOMES or any(not isinstance(value, (int, float)) or value <= 0 for value in prior.values()):
        raise CapabilityBeliefError("positive reviewed Dirichlet prior is required")
    drift = policy.get("drift_policy")
    required = {"version", "freshness_days", "recent_window_days", "baseline_window_days", "minimum_effective_samples", "adverse_floor_pass_delta"}
    if not isinstance(drift, dict) or not required.issubset(drift) or not isinstance(drift["version"], str) or not drift["version"]:
        raise CapabilityBeliefError("versioned reviewed drift policy is required")
    for key in required - {"version"}:
        if not isinstance(drift[key], (int, float)) or isinstance(drift[key], bool) or drift[key] <= 0:
            raise CapabilityBeliefError("positive reviewed drift policy values are required")


def _validate_region(region: object) -> dict[str, int]:
    if not isinstance(region, dict) or not region or any(not isinstance(key, str) or value not in ANCHORS for key, value in region.items()):
        raise CapabilityBeliefError("requirement region must use only five accepted anchor bins")
    return dict(sorted(region.items()))


def _validate_tokens(tokens: dict[str, int | None]) -> dict[str, int | None]:
    unknown = set(tokens) - set(TOKEN_FIELDS)
    if unknown:
        raise CapabilityBeliefError(f"unknown token fields: {sorted(unknown)}")
    result = {}
    for key in TOKEN_FIELDS:
        value = tokens.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise CapabilityBeliefError("token values must be non-negative integers")
        result[key] = value
    return result


def _cohort(record: dict[str, Any]) -> dict[str, Any]:
    components = {"target_id": record["target_id"], "region": record["requirement_region"], "versions": record["versions"], "epoch_id": record["epoch"]["epoch_id"]}
    return {"definition_version": COHORT_DEFINITION_VERSION, "cohort_id": _stable_id(components), "components": components, "legacy_included": False}


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CapabilityBeliefError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise CapabilityBeliefError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _stable_id(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False

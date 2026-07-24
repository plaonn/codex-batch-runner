from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any


CERTIFICATION_CONTRACT = "worker-certification-matrix-v1"
CANARY_CONTRACT = "worker-canary-simulation-v1"
POLICY_REVISION = "worker-certification-policy-v1"

CERTIFICATION_STATES = {
    "experimental-private",
    "eligible-readonly",
    "eligible-bounded-write",
    "default-candidate",
    "disabled",
}
TASK_CLASSES = {"readonly-objective", "bounded-write-isolated"}
EVIDENCE_CLASSES = {"synthetic", "natural"}
EVIDENCE_OUTCOMES = {"pass", "fail", "unknown"}
MUTATION_PROVENANCE = {"no_mutation", "mutation_possible", "mutation_observed", "unknown"}
BOUNDARY_SCENARIOS = {
    "malformed_inventory",
    "malformed_command",
    "timeout",
    "invalid_final_json",
    "mismatched_task_id",
    "nonzero_failure",
    "resume_unavailable",
    "unsafe_changed_files",
    "worker_created_commit",
    "optional_model_token_attestation",
    "auth_failure",
    "quota_failure",
}
EVIDENCE_SCENARIOS = BOUNDARY_SCENARIOS | {"objective_outcome"}
READONLY_BOUNDARIES = {
    "malformed_inventory",
    "malformed_command",
    "timeout",
    "invalid_final_json",
    "mismatched_task_id",
    "nonzero_failure",
    "resume_unavailable",
    "optional_model_token_attestation",
    "auth_failure",
    "quota_failure",
}
BOUNDED_WRITE_BOUNDARIES = READONLY_BOUNDARIES | {
    "unsafe_changed_files",
    "worker_created_commit",
}

INITIAL_CANARY_BASIS_POINTS = 500
MAX_CANARY_BASIS_POINTS = 1_000
MIN_NATURAL_SAMPLES = 20
MIN_OBJECTIVE_PASS_RATIO = 0.95
DEFAULT_CANDIDATE_MIN_NATURAL_SAMPLES = 100
DEFAULT_CANDIDATE_MIN_PASS_RATIO = 0.98
MAX_ADVERSE_SIGNALS = 0
REVALIDATION_DAYS = 30

_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-"
)
_PROHIBITED_MUTATIONS = (
    "active_config",
    "cooldown",
    "execution_target",
    "provider_or_worker_promotion",
    "queue",
    "routing_policy",
    "task_backend",
    "trust_state",
    "wake",
)


class WorkerCertificationError(ValueError):
    pass


def certify_worker(
    candidate: object,
    evidence: object,
    *,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Build one advisory certification record from explicit sanitized inputs."""
    candidate_value = _candidate(candidate)
    evidence_envelope, evidence_values = _evidence(evidence, candidate_value)
    evaluated = _aware_utc(evaluated_at, "evaluated_at")
    task_class = candidate_value["task_class"]
    required = (
        READONLY_BOUNDARIES
        if task_class == "readonly-objective"
        else BOUNDED_WRITE_BOUNDARIES
    )

    coverage = []
    adverse_reasons: set[str] = set()
    for scenario in sorted(BOUNDARY_SCENARIOS):
        records = [item for item in evidence_values if item["scenario"] == scenario]
        outcomes = {item["outcome"] for item in records}
        mutation_values = {item["mutation_provenance"] for item in records}
        evidence_classes = sorted({item["evidence_class"] for item in records})
        if "fail" in outcomes:
            status = "failed"
            adverse_reasons.add(f"{scenario}_failed")
        elif "pass" in outcomes:
            status = "passed"
        else:
            status = "unknown"
        if "mutation_observed" in mutation_values:
            adverse_reasons.add("mutation_observed")
        coverage.append(
            {
                "scenario": scenario,
                "required": scenario in required,
                "status": status,
                "evidence_classes": evidence_classes,
                "status_by_evidence_class": {
                    evidence_class: _coverage_status(
                        item["outcome"]
                        for item in records
                        if item["evidence_class"] == evidence_class
                    )
                    for evidence_class in sorted(EVIDENCE_CLASSES)
                },
                "mutation_provenance": sorted(mutation_values),
            }
        )

    missing_required = sorted(
        item["scenario"]
        for item in coverage
        if item["required"] and item["status"] != "passed"
    )
    if missing_required:
        adverse_reasons.add("required_boundary_incomplete")
    if any(
        item["mutation_provenance"] == "mutation_observed"
        for item in evidence_values
    ):
        adverse_reasons.add("mutation_observed")

    natural_samples = [
        item for item in evidence_values
        if item["evidence_class"] == "natural"
        and item["scenario"] == "objective_outcome"
        and item["outcome"] == "pass"
    ]
    sample_count = sum(item["sample_count"] for item in natural_samples)
    passed_count = sum(item["passed_count"] for item in natural_samples)
    adverse_count = sum(item["adverse_count"] for item in natural_samples)
    pass_ratio = passed_count / sample_count if sample_count else None
    natural_ready = (
        sample_count >= MIN_NATURAL_SAMPLES
        and pass_ratio is not None
        and pass_ratio >= MIN_OBJECTIVE_PASS_RATIO
        and adverse_count <= MAX_ADVERSE_SIGNALS
    )

    mutation_free_boundaries = not missing_required and all(
        item["mutation_provenance"] == "no_mutation"
        for item in evidence_values
    )
    fallback_safe = bool(evidence_values) and mutation_free_boundaries

    if "mutation_observed" in adverse_reasons:
        state = "disabled"
    elif missing_required or any(item["outcome"] == "fail" for item in evidence_values):
        state = "experimental-private"
    elif not natural_ready:
        state = "experimental-private"
    elif (
        sample_count >= DEFAULT_CANDIDATE_MIN_NATURAL_SAMPLES
        and pass_ratio is not None
        and pass_ratio >= DEFAULT_CANDIDATE_MIN_PASS_RATIO
    ):
        state = "default-candidate"
    elif task_class == "readonly-objective":
        state = "eligible-readonly"
    else:
        state = "eligible-bounded-write"

    token_attested = any(
        item["evidence_class"] == "natural"
        and item["token_usage_attested"]
        and item["outcome"] == "pass"
        for item in evidence_values
    )
    record: dict[str, Any] = {
        "schema_version": 1,
        "contract": CERTIFICATION_CONTRACT,
        "policy_revision": POLICY_REVISION,
        "worker_id": candidate_value["worker_id"],
        "target_snapshot_id": candidate_value["target_snapshot_id"],
        "task_class": task_class,
        "state": state,
        "advisory_decision": "eligible" if state.startswith("eligible-") or state == "default-candidate" else (
            "disabled" if state == "disabled" else "unknown"
        ),
        "reasons": sorted(adverse_reasons),
        "coverage": coverage,
        "evidence_cohort_id": evidence_envelope["cohort_id"],
        "evidence_digest": _stable_id(evidence_envelope),
        "natural_evidence": {
            "sample_count": sample_count,
            "passed_count": passed_count,
            "objective_pass_ratio": pass_ratio,
            "adverse_count": adverse_count,
            "minimum_sample_count": MIN_NATURAL_SAMPLES,
            "minimum_objective_pass_ratio": MIN_OBJECTIVE_PASS_RATIO,
            "maximum_adverse_count": MAX_ADVERSE_SIGNALS,
            "default_candidate_minimum_sample_count": DEFAULT_CANDIDATE_MIN_NATURAL_SAMPLES,
            "default_candidate_minimum_pass_ratio": DEFAULT_CANDIDATE_MIN_PASS_RATIO,
        },
        "comparability": {
            "execution_quality": natural_ready,
            "token_cost": natural_ready and token_attested,
            "monetary_cost": False,
            "monetary_cost_reason": "not_attested_by_external_execution_contract",
        },
        "fallback": {
            "advisory_only": True,
            "safe_if_selected_execution_fails": fallback_safe,
            "reason": (
                "failure_evidence_proves_no_mutation"
                if fallback_safe
                else "mutation_free_failure_not_proven"
            ),
        },
        "evaluated_at": evaluated.isoformat(),
        "expires_at": (evaluated + timedelta(days=REVALIDATION_DAYS)).isoformat(),
        "revalidation_days": REVALIDATION_DAYS,
        **_no_mutation_proof(),
    }
    record["certification_id"] = _stable_id(record)
    return record


def simulate_report_only_canary(
    certification: object,
    *,
    cohort_key: str,
    candidate: object,
    evidence: object,
    evaluated_at: datetime,
    adverse_signals: int = 0,
) -> dict[str, Any]:
    """Return a deterministic advisory canary assignment without routing."""
    if not isinstance(certification, dict):
        raise WorkerCertificationError("certification must be an object")
    certification_time = _parse_timestamp(
        certification.get("evaluated_at"), "certification.evaluated_at"
    )
    value = certify_worker(
        candidate,
        evidence,
        evaluated_at=certification_time,
    )
    if value != certification:
        raise WorkerCertificationError(
            "certification does not match candidate and evidence"
        )
    evaluated = _aware_utc(evaluated_at, "evaluated_at")
    expires = _parse_timestamp(value.get("expires_at"), "certification.expires_at")
    adverse_count = _count(adverse_signals, "adverse_signals")
    key = _safe_id(cohort_key, "cohort_key")
    eligible = value["state"] in {
        "eligible-readonly",
        "eligible-bounded-write",
        "default-candidate",
    }
    raw = "\0".join(
        (
            POLICY_REVISION,
            value["worker_id"],
            value["target_snapshot_id"],
            value["task_class"],
            key,
        )
    ).encode("utf-8")
    bucket = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") % 10_000
    simulated_canary = eligible and bucket < INITIAL_CANARY_BASIS_POINTS
    reasons = []
    if not eligible:
        reasons.append("certification_not_eligible")
    if value["comparability"]["execution_quality"] is not True:
        reasons.append("natural_quality_evidence_not_comparable")
    if not value["fallback"]["safe_if_selected_execution_fails"]:
        reasons.append("mutation_free_fallback_not_proven")
    if evaluated >= expires:
        reasons.append("certification_expired")
    if adverse_count > MAX_ADVERSE_SIGNALS:
        reasons.append("adverse_signal_observed")
    if reasons:
        simulated_canary = False
    report: dict[str, Any] = {
        "schema_version": 1,
        "contract": CANARY_CONTRACT,
        "policy_revision": POLICY_REVISION,
        "certification_id": value["certification_id"],
        "worker_id": value["worker_id"],
        "target_snapshot_id": value["target_snapshot_id"],
        "task_class": value["task_class"],
        "cohort_key": key,
        "bucket_basis_points": bucket,
        "initial_canary_basis_points": INITIAL_CANARY_BASIS_POINTS,
        "maximum_canary_basis_points": MAX_CANARY_BASIS_POINTS,
        "report_only_lane": "canary" if simulated_canary else "baseline",
        "reasons": sorted(reasons),
        "rollback_recommendation": (
            "rollback_recommended"
            if adverse_count > MAX_ADVERSE_SIGNALS
            else "rollback_on_any_adverse_signal"
            if simulated_canary
            else "keep_baseline"
        ),
        "adverse_signals": adverse_count,
        "evaluated_at": evaluated.isoformat(),
        "fallback": dict(value["fallback"]),
        **_no_mutation_proof(),
    }
    report["simulation_id"] = _stable_id(report)
    return report


def _candidate(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "worker_id",
        "target_snapshot_id",
        "task_class",
    }:
        raise WorkerCertificationError(
            "candidate must contain only worker_id, target_snapshot_id, task_class"
        )
    task_class = value.get("task_class")
    if task_class not in TASK_CLASSES:
        raise WorkerCertificationError("candidate.task_class is unsupported")
    return {
        "worker_id": _safe_id(value.get("worker_id"), "candidate.worker_id"),
        "target_snapshot_id": _safe_id(
            value.get("target_snapshot_id"), "candidate.target_snapshot_id"
        ),
        "task_class": str(task_class),
    }


def _evidence(
    value: object,
    candidate: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expected = {
        "policy_revision",
        "worker_id",
        "target_snapshot_id",
        "task_class",
        "cohort_id",
        "records",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise WorkerCertificationError(
            "evidence must be a bound versioned envelope"
        )
    if value.get("policy_revision") != POLICY_REVISION:
        raise WorkerCertificationError("evidence policy revision is unsupported")
    for field in ("worker_id", "target_snapshot_id", "task_class"):
        if value.get(field) != candidate[field]:
            raise WorkerCertificationError(
                f"evidence.{field} does not match candidate"
            )
    cohort_id = _safe_id(value.get("cohort_id"), "evidence.cohort_id")
    raw_records = value.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise WorkerCertificationError(
            "evidence.records must be a non-empty list"
        )
    records = [
        _evidence_record(item, index)
        for index, item in enumerate(raw_records)
    ]
    ids = [item["evidence_id"] for item in records]
    if len(ids) != len(set(ids)):
        raise WorkerCertificationError("evidence_id values must be unique")
    natural_objective = [
        item
        for item in records
        if item["evidence_class"] == "natural"
        and item["scenario"] == "objective_outcome"
    ]
    if len(natural_objective) > 1:
        raise WorkerCertificationError(
            "evidence may contain only one bound natural objective aggregate"
        )
    envelope = {
        "policy_revision": POLICY_REVISION,
        "worker_id": candidate["worker_id"],
        "target_snapshot_id": candidate["target_snapshot_id"],
        "task_class": candidate["task_class"],
        "cohort_id": cohort_id,
        "records": records,
    }
    return envelope, records


def _evidence_record(value: object, index: int) -> dict[str, Any]:
    allowed = {
        "evidence_id",
        "evidence_class",
        "scenario",
        "outcome",
        "mutation_provenance",
        "sample_count",
        "passed_count",
        "adverse_count",
        "token_usage_attested",
    }
    if not isinstance(value, dict) or not set(value) <= allowed:
        raise WorkerCertificationError(
            f"evidence[{index}] contains unsupported fields"
        )
    required = {
        "evidence_id",
        "evidence_class",
        "scenario",
        "outcome",
        "mutation_provenance",
    }
    if not required <= set(value):
        raise WorkerCertificationError(
            f"evidence[{index}] is missing required fields"
        )
    evidence_class = value.get("evidence_class")
    scenario = value.get("scenario")
    outcome = value.get("outcome")
    mutation = value.get("mutation_provenance")
    if evidence_class not in EVIDENCE_CLASSES:
        raise WorkerCertificationError(f"evidence[{index}].evidence_class is invalid")
    if scenario not in EVIDENCE_SCENARIOS:
        raise WorkerCertificationError(f"evidence[{index}].scenario is invalid")
    if outcome not in EVIDENCE_OUTCOMES:
        raise WorkerCertificationError(f"evidence[{index}].outcome is invalid")
    if mutation not in MUTATION_PROVENANCE:
        raise WorkerCertificationError(
            f"evidence[{index}].mutation_provenance is invalid"
        )
    sample_count = _count(value.get("sample_count", 0), f"evidence[{index}].sample_count")
    passed_count = _count(value.get("passed_count", 0), f"evidence[{index}].passed_count")
    adverse_count = _count(value.get("adverse_count", 0), f"evidence[{index}].adverse_count")
    if passed_count > sample_count or adverse_count > sample_count:
        raise WorkerCertificationError(
            f"evidence[{index}] counts exceed sample_count"
        )
    if evidence_class == "synthetic" and any(
        (sample_count, passed_count, adverse_count)
    ):
        raise WorkerCertificationError(
            f"evidence[{index}] synthetic evidence cannot claim natural samples"
        )
    token_attested = value.get("token_usage_attested", False)
    if not isinstance(token_attested, bool):
        raise WorkerCertificationError(
            f"evidence[{index}].token_usage_attested must be boolean"
        )
    if evidence_class == "synthetic" and token_attested:
        raise WorkerCertificationError(
            f"evidence[{index}] synthetic evidence cannot attest token usage"
        )
    return {
        "evidence_id": _safe_id(value.get("evidence_id"), f"evidence[{index}].evidence_id"),
        "evidence_class": evidence_class,
        "scenario": scenario,
        "outcome": outcome,
        "mutation_provenance": mutation,
        "sample_count": sample_count,
        "passed_count": passed_count,
        "adverse_count": adverse_count,
        "token_usage_attested": token_attested,
    }


def _no_mutation_proof() -> dict[str, Any]:
    return {
        "read_only": True,
        "mutation_allowed": False,
        "live_routing": False,
        "routing_policy_mutation": False,
        "active_config_mutation": False,
        "prohibited_mutation_surfaces": list(_PROHIBITED_MUTATIONS),
    }


def _coverage_status(outcomes: Any) -> str:
    values = set(outcomes)
    if "fail" in values:
        return "failed"
    if "pass" in values:
        return "passed"
    return "unknown"


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkerCertificationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise WorkerCertificationError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkerCertificationError(
            f"{field} must be an ISO timestamp"
        ) from exc
    return _aware_utc(parsed, field)


def _count(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkerCertificationError(f"{field} must be a non-negative integer")
    return value


def _safe_id(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character not in _SAFE_ID_CHARS for character in value)
    ):
        raise WorkerCertificationError(f"{field} must be a public-safe identifier")
    return value


def _stable_id(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

from __future__ import annotations

import hashlib
import json
from typing import Any

from .timeutil import iso_now


SCHEMA_VERSION = 2
EVIDENCE_CONTRACT_VERSION = "execution-evidence-v2"
COHORT_DEFINITION_VERSION = "execution-cohort-v2"
TOKEN_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
OBSERVED_MODEL_EVENT_TYPES = {"turn.completed", "response.completed"}
FORBIDDEN_EVIDENCE_KEYS = {
    "prompt",
    "transcript",
    "stdout",
    "stderr",
    "log_path",
    "session_id",
    "thread_id",
    "command",
    "cwd",
}
PRIVACY_KEYS = {
    "raw_prompt_included",
    "raw_transcript_included",
    "session_or_thread_ids_included",
    "raw_paths_included",
    "credentials_included",
}


class ExecutionEvidenceV2Error(ValueError):
    pass


def build_codex_execution_evidence(task: dict[str, Any], result: Any) -> dict[str, Any]:
    usage = latest_usage(result.events)
    actual_model = observed_codex_model(result.events)
    return build_execution_evidence(
        task,
        capture_source="codex_jsonl",
        actual_model=actual_model,
        actual_model_source="codex_jsonl" if actual_model else None,
        actual_model_confidence="provider_observed" if actual_model else None,
        actual_model_unavailable_reason=None if actual_model else "model_not_exposed_by_provider_output",
        token_usage=usage,
        token_usage_source="codex_jsonl" if usage else None,
        token_usage_confidence="provider_observed" if usage else None,
        token_usage_unavailable_reason=None if usage else "usage_not_exposed_by_provider_output",
    )


def build_external_execution_evidence(
    task: dict[str, Any],
    attestation: object,
) -> dict[str, Any]:
    invalid_attestation = False
    try:
        normalized = validate_external_attestation(attestation) if attestation is not None else None
    except ExecutionEvidenceV2Error:
        normalized = None
        invalid_attestation = True
    actual_model = normalized.get("actual_model") if normalized else None
    usage = normalized.get("token_usage") if normalized else None
    return build_execution_evidence(
        task,
        capture_source=(
            "external_wrapper_attestation"
            if normalized
            else "external_wrapper_invalid_attestation"
            if invalid_attestation
            else "external_wrapper"
        ),
        actual_model=actual_model,
        actual_model_source="external_wrapper_attestation" if actual_model else None,
        actual_model_confidence="wrapper_attested" if actual_model else None,
        actual_model_unavailable_reason=(
            None
            if actual_model
            else "invalid_external_wrapper_attestation"
            if invalid_attestation
            else "external_wrapper_did_not_attest_model"
        ),
        token_usage=usage,
        token_usage_source="external_wrapper_attestation" if usage else None,
        token_usage_confidence="wrapper_attested" if usage else None,
        token_usage_unavailable_reason=(
            None
            if usage
            else "invalid_external_wrapper_attestation"
            if invalid_attestation
            else "external_wrapper_did_not_attest_usage"
        ),
    )


def build_shell_execution_evidence(task: dict[str, Any]) -> dict[str, Any]:
    return build_execution_evidence(
        task,
        capture_source="cbr_shell_backend",
        actual_model=None,
        actual_model_source=None,
        actual_model_confidence=None,
        actual_model_unavailable_reason="not_applicable_token_free_backend",
        token_usage={},
        token_usage_source="token_free",
        token_usage_confidence="cbr_backend_contract",
        token_usage_unavailable_reason=None,
        token_status="token_free",
    )


def build_execution_evidence(
    task: dict[str, Any],
    *,
    capture_source: str,
    actual_model: str | None,
    actual_model_source: str | None,
    actual_model_confidence: str | None,
    actual_model_unavailable_reason: str | None,
    token_usage: dict[str, int] | None,
    token_usage_source: str | None,
    token_usage_confidence: str | None,
    token_usage_unavailable_reason: str | None,
    token_status: str | None = None,
) -> dict[str, Any]:
    captured_at = iso_now()
    attempt = _int_value(task.get("attempts"))
    evidence_id = _stable_id(
        {
            "task_id": str(task.get("id") or "unknown"),
            "attempt": attempt,
            "backend": execution_backend(task),
            "captured_at": captured_at,
        }
    )
    actual_status = "observed" if actual_model else "unavailable"
    resolved_token_status = token_status or ("observed" if token_usage else "unavailable")
    record = {
        "schema_version": SCHEMA_VERSION,
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "kind": "execution_evidence",
        "evidence_id": evidence_id,
        "captured_at": captured_at,
        "attempt": attempt,
        "capture": {
            "source": capture_source,
            "raw_provider_output_included": False,
            "raw_wrapper_output_included": False,
        },
        "execution": {
            "backend": execution_backend(task),
            "worker_family": worker_family(task),
        },
        "actual_model": {
            "status": actual_status,
            "value": actual_model,
            "source": actual_model_source,
            "confidence": actual_model_confidence,
            "availability_reason": actual_model_unavailable_reason,
        },
        "token_usage": {
            "status": resolved_token_status,
            "values": token_usage_payload(token_usage or {}),
            "source": token_usage_source,
            "confidence": token_usage_confidence,
            "availability_reason": token_usage_unavailable_reason,
        },
        "monetary_cost": {
            "status": "unavailable",
            "amount": None,
            "currency": None,
            "source": None,
            "confidence": None,
            "availability_reason": "provider_billing_evidence_not_available",
        },
        "privacy": {
            "raw_prompt_included": False,
            "raw_transcript_included": False,
            "session_or_thread_ids_included": False,
            "raw_paths_included": False,
            "credentials_included": False,
        },
    }
    record["cohort"] = derive_cohort(task, record)
    validate_execution_evidence_v2(record)
    return record


def attach_execution_evidence(task: dict[str, Any], record: dict[str, Any]) -> None:
    validate_execution_evidence_v2(record)
    history = task.setdefault("execution_evidence_history", [])
    if not isinstance(history, list):
        history = []
        task["execution_evidence_history"] = history
    evidence_id = record["evidence_id"]
    if not any(isinstance(item, dict) and item.get("evidence_id") == evidence_id for item in history):
        history.append(record)
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else None
    if last_run is not None:
        last_run["execution_evidence_id"] = evidence_id


def latest_execution_evidence(task: dict[str, Any]) -> dict[str, Any] | None:
    history = task.get("execution_evidence_history")
    if not isinstance(history, list):
        return None
    evidence_id = None
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else {}
    if isinstance(last_run, dict):
        evidence_id = last_run.get("execution_evidence_id")
    if evidence_id:
        for record in reversed(history):
            if isinstance(record, dict) and record.get("evidence_id") == evidence_id:
                return record
    for record in reversed(history):
        if isinstance(record, dict) and record.get("schema_version") in {SCHEMA_VERSION, 3}:
            return record
    return None


def legacy_evidence_view(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "evidence_contract_version": "legacy-v1",
        "kind": "legacy_execution_evidence",
        "actual_model": {
            "status": "unavailable",
            "value": None,
            "source": None,
            "confidence": None,
            "availability_reason": "not_captured_by_legacy_contract",
        },
        "token_usage": {
            "status": "legacy_dynamic",
            "values": token_usage_payload({}),
            "source": None,
            "confidence": None,
            "availability_reason": "not_persisted_by_legacy_contract",
        },
        "monetary_cost": {
            "status": "unavailable",
            "amount": None,
            "currency": None,
            "source": None,
            "confidence": None,
            "availability_reason": "not_captured_by_legacy_contract",
        },
        "cohort": {
            "definition_version": COHORT_DEFINITION_VERSION,
            "cohort_id": "legacy-v1-non-comparable",
            "components": {},
            "comparability": {
                "model_quality": False,
                "token_cost": False,
                "monetary_cost": False,
            },
            "exclusion_reasons": ["legacy_evidence_contract"],
        },
        "privacy": {
            "raw_prompt_included": False,
            "raw_transcript_included": False,
            "session_or_thread_ids_included": False,
            "raw_paths_included": False,
            "credentials_included": False,
        },
    }


def evidence_view(task: dict[str, Any]) -> dict[str, Any]:
    record = latest_execution_evidence(task)
    if record is None:
        return legacy_evidence_view(task)
    if record.get("schema_version") == 3:
        from .execution_evidence_v3 import validate_execution_evidence_v3

        return validate_execution_evidence_v3(record)
    validate_execution_evidence_v2(record)
    return record


def reporting_evidence_view(task: dict[str, Any]) -> dict[str, Any]:
    """Return a version-preserving projection for read-only report consumers."""
    record = evidence_view(task)
    if record.get("schema_version") != 3:
        return record
    identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    provider = identity.get("provider_reported_model") if isinstance(identity.get("provider_reported_model"), dict) else {}
    selected = identity.get("selected_model")
    command = identity.get("command_model")
    integrity = record.get("integrity") if isinstance(record.get("integrity"), dict) else {}
    command_exact = bool(selected and command and selected == command)
    projected = dict(record)
    projected["actual_model"] = {
        "status": "observed" if command_exact else "unavailable",
        "value": command if command_exact else None,
        "source": "command_enforced" if command_exact else None,
        "confidence": identity.get("attestation") if command_exact else None,
        "availability_reason": None if command_exact else str(integrity.get("status") or "identity_not_exact"),
    }
    projected["identity"] = {
        "selected_model": selected,
        "command_model": command,
        "provider_reported_model": dict(provider),
        "reasoning_effort": identity.get("reasoning_effort"),
        "attestation": identity.get("attestation"),
        "integrity_status": integrity.get("status"),
        "adverse": bool(integrity.get("adverse")),
    }
    projected["monetary_cost"] = {
        "status": "unavailable", "amount": None, "currency": None, "source": None,
        "confidence": None, "availability_reason": "provider_billing_evidence_not_available",
    }
    return projected


def validate_execution_evidence_v2(record: object) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ExecutionEvidenceV2Error("execution evidence v2 must be a JSON object")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ExecutionEvidenceV2Error("execution evidence v2 schema_version must be 2")
    if record.get("evidence_contract_version") != EVIDENCE_CONTRACT_VERSION:
        raise ExecutionEvidenceV2Error("invalid execution evidence contract version")
    if record.get("kind") != "execution_evidence":
        raise ExecutionEvidenceV2Error("execution evidence v2 kind must be execution_evidence")
    if not isinstance(record.get("evidence_id"), str) or not str(record.get("evidence_id")).strip():
        raise ExecutionEvidenceV2Error("execution evidence v2 requires evidence_id")
    capture = record.get("capture")
    if not isinstance(capture, dict) or not isinstance(capture.get("source"), str) or not capture.get("source"):
        raise ExecutionEvidenceV2Error("execution evidence v2 requires capture source")
    for key in FORBIDDEN_EVIDENCE_KEYS:
        if _contains_key(record, key):
            raise ExecutionEvidenceV2Error(f"execution evidence v2 contains forbidden key: {key}")
    _validate_observation(record.get("actual_model"), "actual_model", allow_token_free=False)
    _validate_observation(record.get("token_usage"), "token_usage", allow_token_free=True)
    monetary = record.get("monetary_cost")
    if not isinstance(monetary, dict) or monetary.get("status") not in {"observed", "unavailable"}:
        raise ExecutionEvidenceV2Error("invalid monetary_cost observation")
    if monetary.get("status") == "observed" and (
        not isinstance(monetary.get("amount"), (int, float))
        or isinstance(monetary.get("amount"), bool)
        or not monetary.get("currency")
        or not monetary.get("source")
    ):
        raise ExecutionEvidenceV2Error("observed monetary_cost requires amount, currency, and source")
    if monetary.get("status") == "unavailable" and not monetary.get("availability_reason"):
        raise ExecutionEvidenceV2Error("unavailable monetary_cost requires availability_reason")
    cohort = record.get("cohort")
    if not isinstance(cohort, dict) or cohort.get("definition_version") != COHORT_DEFINITION_VERSION:
        raise ExecutionEvidenceV2Error("invalid execution evidence cohort")
    comparability = cohort.get("comparability")
    if not isinstance(comparability, dict) or set(comparability) != {
        "model_quality",
        "token_cost",
        "monetary_cost",
    }:
        raise ExecutionEvidenceV2Error("invalid execution evidence cohort comparability")
    privacy = record.get("privacy")
    if (
        not isinstance(privacy, dict)
        or set(privacy) != PRIVACY_KEYS
        or any(value is not False for value in privacy.values())
    ):
        raise ExecutionEvidenceV2Error("execution evidence privacy flags must all be false")
    return record


def validate_external_attestation(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionEvidenceV2Error("external execution_evidence must be a JSON object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ExecutionEvidenceV2Error("external execution_evidence schema_version must be 2")
    allowed = {"schema_version", "capability", "actual_model", "token_usage"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ExecutionEvidenceV2Error("external execution_evidence contains unsupported key(s): " + ", ".join(unknown))
    if value.get("capability") != "actual-model+usage-attestation":
        raise ExecutionEvidenceV2Error("invalid external execution_evidence capability")
    actual_model = value.get("actual_model")
    if actual_model is not None and (not isinstance(actual_model, str) or not actual_model.strip()):
        raise ExecutionEvidenceV2Error("external actual_model must be a non-empty string")
    usage = normalized_token_usage(value.get("token_usage"))
    if actual_model is None and not usage:
        raise ExecutionEvidenceV2Error("external execution_evidence must attest actual_model or token_usage")
    return {"actual_model": actual_model.strip() if isinstance(actual_model, str) else None, "token_usage": usage}


def latest_usage(events: list[dict[str, Any]]) -> dict[str, int] | None:
    latest = None
    for event in events:
        if not isinstance(event, dict):
            continue
        usage = normalized_token_usage(event.get("usage"))
        if usage:
            latest = usage
    return latest


def observed_codex_model(events: list[dict[str, Any]]) -> str | None:
    observed: str | None = None
    for event in events:
        if not isinstance(event, dict) or str(event.get("type") or "") not in OBSERVED_MODEL_EVENT_TYPES:
            continue
        candidates = [event.get("model")]
        response = event.get("response")
        if isinstance(response, dict):
            candidates.append(response.get("model"))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                observed = candidate.strip()
    return observed


def normalized_token_usage(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage: dict[str, int] = {}
    for key in TOKEN_USAGE_KEYS:
        parsed = _optional_int(value.get(key))
        if parsed is not None:
            if parsed < 0:
                raise ExecutionEvidenceV2Error(f"token usage {key} must be non-negative")
            usage[key] = parsed
    return usage or None


def token_usage_payload(usage: dict[str, int]) -> dict[str, int | None]:
    payload: dict[str, int | None] = {key: usage.get(key) for key in TOKEN_USAGE_KEYS}
    input_tokens = usage.get("input_tokens")
    cached_input_tokens = usage.get("cached_input_tokens")
    payload["uncached_input_tokens"] = (
        max(0, input_tokens - cached_input_tokens)
        if input_tokens is not None and cached_input_tokens is not None
        else None
    )
    output_tokens = usage.get("output_tokens")
    payload["known_total_tokens"] = (
        int(input_tokens or 0) + int(output_tokens or 0)
        if input_tokens is not None or output_tokens is not None
        else None
    )
    return payload


def derive_cohort(task: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else {}
    resolved = last_run.get("resolved_execution_config") if isinstance(last_run.get("resolved_execution_config"), dict) else {}
    worker_target = last_run.get("resolved_worker_target") if isinstance(last_run.get("resolved_worker_target"), dict) else {}
    actual_model = record.get("actual_model") if isinstance(record.get("actual_model"), dict) else {}
    token_usage = record.get("token_usage") if isinstance(record.get("token_usage"), dict) else {}
    monetary = record.get("monetary_cost") if isinstance(record.get("monetary_cost"), dict) else {}
    components = {
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "execution_backend": execution_backend(task),
        "actual_model": actual_model.get("value") if actual_model.get("status") == "observed" else "unknown",
        "model_selection_rule": resolved.get("selection_rule") or worker_target.get("selection_rule") or "unknown",
        "execution_target": resolved.get("execution_target") or worker_target.get("worker_target") or "unknown",
        "routing_experiment": task.get("routing_experiment") or "unspecified",
        "review_policy_version": task.get("review_policy_version") or "legacy",
        "requirement_derivation_version": _requirement_derivation_version(task, resolved),
    }
    model_comparable = actual_model.get("status") == "observed"
    token_comparable = token_usage.get("status") in {"observed", "token_free"}
    monetary_comparable = monetary.get("status") == "observed"
    exclusion_reasons = []
    if not model_comparable:
        exclusion_reasons.append("actual_model_unavailable")
    if not token_comparable:
        exclusion_reasons.append("token_usage_unavailable")
    if not monetary_comparable:
        exclusion_reasons.append("monetary_cost_unavailable")
    return {
        "definition_version": COHORT_DEFINITION_VERSION,
        "cohort_id": "sha256:" + _stable_hash(components)[:16],
        "components": components,
        "comparability": {
            "model_quality": model_comparable,
            "token_cost": token_comparable,
            "monetary_cost": monetary_comparable,
        },
        "exclusion_reasons": exclusion_reasons,
    }


def execution_backend(task: dict[str, Any]) -> str:
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else {}
    return str(last_run.get("execution_backend") or task.get("execution_backend") or "codex")


def worker_family(task: dict[str, Any]) -> str:
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else {}
    target = last_run.get("resolved_worker_target") if isinstance(last_run.get("resolved_worker_target"), dict) else {}
    return str(target.get("worker_family") or task.get("worker_family") or execution_backend(task))


def _requirement_derivation_version(task: dict[str, Any], resolved: dict[str, Any]) -> str:
    vector = resolved.get("model_requirement_vector") or task.get("model_requirement_vector")
    if isinstance(vector, dict):
        return str(vector.get("derivation_version") or vector.get("source") or "legacy")
    return "legacy"


def _validate_observation(value: object, name: str, *, allow_token_free: bool) -> None:
    statuses = {"observed", "unavailable"}
    if allow_token_free:
        statuses.add("token_free")
    if not isinstance(value, dict) or value.get("status") not in statuses:
        raise ExecutionEvidenceV2Error(f"invalid {name} observation")
    if value.get("status") == "observed" and not value.get("source"):
        raise ExecutionEvidenceV2Error(f"observed {name} requires source")
    if name == "actual_model" and value.get("status") == "observed" and (
        not isinstance(value.get("value"), str) or not value.get("value").strip()
    ):
        raise ExecutionEvidenceV2Error("observed actual_model requires non-empty value")
    if name == "actual_model" and value.get("status") == "unavailable" and value.get("value") is not None:
        raise ExecutionEvidenceV2Error("unavailable actual_model value must be null")
    if name == "token_usage" and value.get("status") == "observed":
        values = value.get("values")
        if not isinstance(values, dict) or not any(values.get(key) is not None for key in TOKEN_USAGE_KEYS):
            raise ExecutionEvidenceV2Error("observed token_usage requires token values")
    if value.get("status") == "unavailable" and not value.get("availability_reason"):
        raise ExecutionEvidenceV2Error(f"unavailable {name} requires availability_reason")


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _stable_id(value: object) -> str:
    return "evidence-sha256:" + _stable_hash(value)[:24]


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_value(value: object) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None else 0

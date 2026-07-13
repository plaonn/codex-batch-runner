from __future__ import annotations

import hashlib
import json
from typing import Any

from .execution_evidence_v2 import (
    FORBIDDEN_EVIDENCE_KEYS,
    PRIVACY_KEYS,
    latest_usage,
    normalized_token_usage,
    observed_codex_model,
    token_usage_payload,
)
from .timeutil import iso_now


SCHEMA_VERSION = 3
EVIDENCE_CONTRACT_VERSION = "execution-evidence-v3"
COHORT_DEFINITION_VERSION = "execution-cohort-v3"
TARGET_CONTRACT_VERSION = "execution-target-v1"
QUALITY_OUTCOME_VERSION = "quality-outcome-v1"


class ExecutionEvidenceV3Error(ValueError):
    pass


class CommandIdentityError(ExecutionEvidenceV3Error):
    def __init__(self, record: dict[str, Any]):
        self.record = record
        super().__init__("selected_command_mismatch: selected_model must equal command_model before execution")


def command_model(command: list[str]) -> str | None:
    models: list[str] = []
    for index, value in enumerate(command[:-1]):
        if value == "--model":
            candidate = command[index + 1]
            if candidate:
                models.append(candidate)
    if not models:
        return None
    return models[0] if len(models) == 1 else None


def command_reasoning_effort(command: list[str]) -> str | None:
    values: list[str] = []
    for index, value in enumerate(command[:-1]):
        if value == "-c" and command[index + 1].startswith("model_reasoning_effort="):
            candidate = command[index + 1].split("=", 1)[1]
            if candidate:
                values.append(candidate)
    if not values:
        return None
    return values[0] if len(values) == 1 else None


def enforce_codex_command_identity(
    task: dict[str, Any], settings: Any, command: list[str], config: Any
) -> None:
    if not exact_v3_settings(settings):
        return
    selected = str(settings.model)
    commanded = command_model(command)
    selected_reasoning = reasoning_effort(settings)
    commanded_reasoning = command_reasoning_effort(command)
    if selected != commanded or selected_reasoning != commanded_reasoning:
        raise CommandIdentityError(
            build_integrity_evidence(
                task, settings, config, command_model_value=commanded,
                integrity_status="selected_command_mismatch",
            )
        )


def enforce_external_command_identity(task: dict[str, Any], settings: Any, config: Any) -> None:
    if not exact_v3_settings(settings):
        return
    selected = str(settings.model)
    commanded = task.get("worker_command_model")
    selected_reasoning = reasoning_effort(settings)
    commanded_reasoning = task.get("worker_reasoning_effort")
    if selected != commanded or selected_reasoning != commanded_reasoning:
        raise CommandIdentityError(
            build_integrity_evidence(
                task, settings, config, command_model_value=str(commanded) if commanded else None,
                integrity_status="selected_command_mismatch",
            )
        )


def exact_v3_settings(settings: Any) -> bool:
    vector = getattr(settings, "requirement_vector", {}) or {}
    return bool(
        vector.get("schema_version") == 2
        and getattr(settings, "execution_target", None)
        and getattr(settings, "selection_rule", None) == "execution-target-selector-v1"
        and getattr(settings, "model", None)
        and reasoning_effort(settings)
    )


def reasoning_effort(settings: Any) -> str | None:
    overrides = getattr(settings, "config_overrides", None) or {}
    value = overrides.get("model_reasoning_effort")
    return str(value) if value not in (None, "") else None


def build_codex_execution_evidence_v3(
    task: dict[str, Any], result: Any, settings: Any, config: Any
) -> dict[str, Any]:
    provider_model = observed_codex_model(result.events)
    selected = str(settings.model)
    integrity_status = "provider_model_mismatch" if provider_model and provider_model != selected else "compliant"
    return _build_record(
        task,
        settings,
        config,
        capture_source="codex_jsonl",
        command_model_value=selected,
        provider_model=provider_model,
        provider_source="codex_jsonl" if provider_model else None,
        provider_confidence="provider_observed" if provider_model else None,
        provider_reason=None if provider_model else "model_not_exposed_by_provider_output",
        token_usage=latest_usage(result.events),
        integrity_status=integrity_status,
    )


def build_external_execution_evidence_v3(
    task: dict[str, Any], attestation: object, settings: Any, config: Any
) -> dict[str, Any]:
    invalid_attestation = False
    try:
        normalized = validate_external_attestation_v3(attestation) if attestation is not None else None
    except ExecutionEvidenceV3Error:
        normalized = None
        invalid_attestation = True
    provider_model = normalized.get("provider_reported_model") if normalized else None
    selected = str(settings.model)
    integrity_status = "provider_model_mismatch" if provider_model and provider_model != selected else "compliant"
    return _build_record(
        task,
        settings,
        config,
        capture_source=(
            "external_wrapper_attestation" if normalized
            else "external_wrapper_invalid_attestation" if invalid_attestation
            else "external_wrapper"
        ),
        command_model_value=(
            str(task.get("worker_command_model")) if task.get("worker_command_model") else None
        ),
        provider_model=provider_model,
        provider_source="external_wrapper_attestation" if provider_model else None,
        provider_confidence="wrapper_attested" if provider_model else None,
        provider_reason=(
            None if provider_model else "invalid_external_wrapper_attestation" if invalid_attestation
            else "external_wrapper_did_not_attest_model"
        ),
        token_usage=normalized.get("token_usage") if normalized else None,
        integrity_status=integrity_status,
    )


def build_integrity_evidence(
    task: dict[str, Any], settings: Any, config: Any, *, command_model_value: str | None,
    integrity_status: str,
) -> dict[str, Any]:
    return _build_record(
        task, settings, config, capture_source="pre_execution_command_validation",
        command_model_value=command_model_value, provider_model=None, provider_source=None,
        provider_confidence=None, provider_reason="provider_not_invoked", token_usage=None,
        integrity_status=integrity_status,
    )


def _build_record(
    task: dict[str, Any], settings: Any, config: Any, *, capture_source: str,
    command_model_value: str | None, provider_model: str | None, provider_source: str | None,
    provider_confidence: str | None, provider_reason: str, token_usage: dict[str, int] | None,
    integrity_status: str,
) -> dict[str, Any]:
    captured_at = iso_now()
    versions = contract_versions(task, settings, config)
    selected = str(settings.model)
    attestation = "verified" if provider_model == selected else "mismatch" if provider_model else "command_attributed"
    record = {
        "schema_version": SCHEMA_VERSION,
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "kind": "execution_evidence",
        "evidence_id": _stable_id({
            "task_id": task.get("id"), "attempt": task.get("attempts"),
            "captured_at": captured_at, "integrity": integrity_status,
        }),
        "captured_at": captured_at,
        "attempt": _int_value(task.get("attempts")),
        "capture": {
            "source": capture_source, "raw_provider_output_included": False,
            "raw_wrapper_output_included": False,
        },
        "identity": {
            "selected_model": selected,
            "command_model": command_model_value,
            "provider_reported_model": {
                "status": "observed" if provider_model else "unavailable", "value": provider_model,
                "source": provider_source, "confidence": provider_confidence, "availability_reason": provider_reason,
            },
            "reasoning_effort": reasoning_effort(settings),
            "attestation": attestation,
        },
        "routing": {"target_id": settings.execution_target, "selection_reason": settings.selection_reason},
        "versions": versions,
        "integrity": {"status": integrity_status, "adverse": integrity_status != "compliant"},
        "token_usage": {
            "status": "observed" if token_usage else "unavailable", "values": token_usage_payload(token_usage or {}),
            "source": capture_source if token_usage else None,
            "confidence": "provider_observed" if capture_source == "codex_jsonl" and token_usage else "wrapper_attested" if token_usage else None,
            "availability_reason": None if token_usage else "usage_not_exposed_by_provider_output",
        },
        "privacy": {key: False for key in PRIVACY_KEYS},
    }
    exact = integrity_status == "compliant" and command_model_value == selected
    record["cohort"] = {
        "definition_version": COHORT_DEFINITION_VERSION,
        "cohort_id": _stable_id({
            "identity": selected, "reasoning": reasoning_effort(settings), "versions": versions,
            "selection": selection_cohort(task, settings),
        }),
        "components": {"selection_cohort": selection_cohort(task, settings), **versions},
        "comparability": {"model_quality": exact, "token_cost": exact and bool(token_usage), "monetary_cost": False},
        "exclusion_reasons": [] if exact else [integrity_status],
    }
    return validate_execution_evidence_v3(record)


def contract_versions(task: dict[str, Any], settings: Any, config: Any) -> dict[str, str]:
    vector = settings.requirement_vector
    inventory = getattr(config, "execution_target_inventory", {}) or {}
    registry = getattr(config, "constraint_registry", {}) or {}
    return {
        "inventory_schema_version": str(inventory.get("schema_version")),
        "inventory_snapshot_id": str(inventory.get("snapshot_id")),
        "selection_policy_version": str(settings.selection_rule),
        "requirement_schema_version": str(vector.get("schema_version")),
        "requirement_revision_id": str(vector.get("revision_id")),
        "rubric_version": str(vector.get("derivation_version")),
        "constraint_registry_version": str(registry.get("version")),
        "target_contract_version": TARGET_CONTRACT_VERSION,
        "review_policy_version": str(task.get("review_policy_version") or "legacy"),
        "review_rubric_version": str(task.get("review_rubric_version") or "legacy"),
        "outcome_contract_version": QUALITY_OUTCOME_VERSION,
    }


def selection_cohort(task: dict[str, Any], settings: Any) -> str:
    if isinstance(task.get("routing_override"), dict):
        return "override"
    return "automatic" if settings.selection_reason == "automatic_static_non_learned" else "v2-other"


def attach_execution_evidence_v3(task: dict[str, Any], record: dict[str, Any]) -> None:
    validate_execution_evidence_v3(record)
    history = task.setdefault("execution_evidence_history", [])
    if not isinstance(history, list):
        raise ExecutionEvidenceV3Error("execution evidence history must be a list")
    if not any(isinstance(item, dict) and item.get("evidence_id") == record["evidence_id"] for item in history):
        history.append(record)
    if isinstance(task.get("last_run"), dict):
        task["last_run"]["execution_evidence_id"] = record["evidence_id"]


def validate_execution_evidence_v3(record: object) -> dict[str, Any]:
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != 3
        or record.get("evidence_contract_version") != EVIDENCE_CONTRACT_VERSION
    ):
        raise ExecutionEvidenceV3Error("invalid execution evidence v3 contract")
    for key in FORBIDDEN_EVIDENCE_KEYS:
        if _contains_key(record, key):
            raise ExecutionEvidenceV3Error(f"execution evidence v3 contains forbidden key: {key}")
    identity = record.get("identity")
    if not isinstance(identity, dict) or not identity.get("selected_model") or not identity.get("reasoning_effort"):
        raise ExecutionEvidenceV3Error("execution evidence v3 requires exact selected model and reasoning")
    if identity.get("attestation") not in {"verified", "mismatch", "command_attributed"}:
        raise ExecutionEvidenceV3Error("invalid provider attestation status")
    versions = record.get("versions")
    if not isinstance(versions, dict) or any(
        not isinstance(value, str) or not value or value == "None" for value in versions.values()
    ):
        raise ExecutionEvidenceV3Error("execution evidence v3 requires every contract version")
    privacy = record.get("privacy")
    if (
        not isinstance(privacy, dict)
        or set(privacy) != PRIVACY_KEYS
        or any(value is not False for value in privacy.values())
    ):
        raise ExecutionEvidenceV3Error("execution evidence privacy flags must all be false")
    return record


def validate_external_attestation_v3(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 3:
        raise ExecutionEvidenceV3Error("external execution_evidence schema_version must be 3")
    allowed = {"schema_version", "capability", "provider_reported_model", "token_usage"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ExecutionEvidenceV3Error("external execution_evidence contains unsupported key(s): " + ", ".join(unknown))
    if value.get("capability") != "provider-model+usage-attestation":
        raise ExecutionEvidenceV3Error("invalid external execution_evidence capability")
    model = value.get("provider_reported_model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ExecutionEvidenceV3Error("external provider_reported_model must be a non-empty string")
    usage = normalized_token_usage(value.get("token_usage"))
    if model is None and not usage:
        raise ExecutionEvidenceV3Error("external execution_evidence must attest model or token usage")
    return {"provider_reported_model": model.strip() if isinstance(model, str) else None, "token_usage": usage}


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _stable_id(value: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

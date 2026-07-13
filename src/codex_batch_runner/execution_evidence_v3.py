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
                command_reasoning_value=commanded_reasoning,
                integrity_status="selected_command_mismatch",
            )
        )


def enforce_external_command_identity(
    task: dict[str, Any], settings: Any, command: list[str], config: Any
) -> None:
    snapshot = getattr(settings, "selected_target_snapshot", None)
    selector_selected = bool(
        getattr(settings, "selection_rule", None) == "execution-target-selector-v1"
        and isinstance(snapshot, dict)
        and isinstance(snapshot.get("target"), dict)
        and snapshot["target"].get("execution_backend") == "external-json-command"
    )
    if not selector_selected:
        return
    selected = str(getattr(settings, "model", None) or "")
    target = _exact_external_target(settings)
    commanded = external_command_identity(command, settings)
    selected_reasoning = reasoning_effort(settings)
    commanded_reasoning = external_command_reasoning(command, settings)
    template = target.get("external_command") if isinstance(target.get("external_command"), list) else []
    snapshot_exact = bool(
        target.get("model") == selected
        and target.get("command_model") == selected
        and target.get("reasoning_effort") == selected_reasoning
        and template.count("{model}") == 1
        and template.count("{reasoning_effort}") == 1
    )
    if not snapshot_exact or selected != commanded or selected_reasoning != commanded_reasoning:
        if not snapshot_exact:
            commanded = None
            commanded_reasoning = None
        raise CommandIdentityError(
            build_integrity_evidence(
                task, settings, config, command_model_value=str(commanded) if commanded else None,
                command_reasoning_value=str(commanded_reasoning) if commanded_reasoning else None,
                integrity_status="selected_command_mismatch",
            )
        )


def _exact_external_target(settings: Any) -> dict[str, Any]:
    snapshot = getattr(settings, "selected_target_snapshot", None) or {}
    target = snapshot.get("target") if isinstance(snapshot, dict) else None
    if not isinstance(target, dict) or target.get("execution_backend") != "external-json-command":
        return {}
    return target


def _unique_argv_value(command: list[str], expected: str) -> str | None:
    matches = [value for value in command if value == expected]
    return expected if len(matches) == 1 else None


def external_command_identity(command: list[str], settings: Any) -> str | None:
    target = _exact_external_target(settings)
    expected = target.get("command_model")
    return _unique_argv_value(command, str(expected)) if expected else None


def external_command_reasoning(command: list[str], settings: Any) -> str | None:
    target = _exact_external_target(settings)
    expected = target.get("reasoning_effort")
    return _unique_argv_value(command, str(expected)) if expected else None


def exact_v3_settings(settings: Any) -> bool:
    vector = getattr(settings, "requirement_vector", {}) or {}
    return bool(
        vector.get("schema_version") == 2
        and getattr(settings, "execution_target", None)
        and getattr(settings, "selection_rule", None) == "execution-target-selector-v1"
        and getattr(settings, "model", None)
        and reasoning_effort(settings)
        and isinstance(getattr(settings, "selected_target_snapshot", None), dict)
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
        command_reasoning_value=reasoning_effort(settings),
        provider_model=provider_model,
        provider_source="codex_jsonl" if provider_model else None,
        provider_confidence="provider_observed" if provider_model else None,
        provider_reason=None if provider_model else "model_not_exposed_by_provider_output",
        token_usage=latest_usage(result.events),
        integrity_status=integrity_status,
    )


def build_external_execution_evidence_v3(
    task: dict[str, Any], attestation: object, settings: Any, config: Any,
    *, command: list[str] | None = None,
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
        command_model_value=(external_command_identity(command, settings) if command is not None else None),
        command_reasoning_value=(external_command_reasoning(command, settings) if command is not None else None),
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
    command_reasoning_value: str | None,
    integrity_status: str,
) -> dict[str, Any]:
    return _build_record(
        task, settings, config, capture_source="pre_execution_command_validation",
        command_model_value=command_model_value, command_reasoning_value=command_reasoning_value,
        provider_model=None, provider_source=None,
        provider_confidence=None, provider_reason="provider_not_invoked", token_usage=None,
        integrity_status=integrity_status,
    )


def _build_record(
    task: dict[str, Any], settings: Any, config: Any, *, capture_source: str,
    command_model_value: str | None, command_reasoning_value: str | None,
    provider_model: str | None, provider_source: str | None,
    provider_confidence: str | None, provider_reason: str, token_usage: dict[str, int] | None,
    integrity_status: str,
) -> dict[str, Any]:
    captured_at = iso_now()
    versions = contract_versions(task, settings, config)
    selected = str(settings.model)
    command_exact = (
        command_model_value == selected
        and command_reasoning_value == reasoning_effort(settings)
    )
    attestation = (
        "verified" if provider_model == selected
        else "mismatch" if provider_model
        else "command_attributed" if command_exact
        else "unattributed"
    )
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
            "command_reasoning_effort": command_reasoning_value,
            "attestation": attestation,
        },
        "routing": {
            "target_id": settings.execution_target,
            "selection_reason": settings.selection_reason,
            "selection_cohort": selection_cohort(task, settings),
        },
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
    exact = integrity_status == "compliant" and command_exact
    cohort_components = {
        "selection_cohort": selection_cohort(task, settings),
        "target_id": str(settings.execution_target),
        "selected_model": selected,
        "reasoning_effort": reasoning_effort(settings),
        **versions,
    }
    record["cohort"] = {
        "definition_version": COHORT_DEFINITION_VERSION,
        "cohort_id": _stable_id(cohort_components),
        "components": cohort_components,
        "comparability": {"model_quality": exact, "token_cost": exact and bool(token_usage), "monetary_cost": False},
        "exclusion_reasons": [] if exact else [integrity_status],
    }
    return validate_execution_evidence_v3(record)


def contract_versions(task: dict[str, Any], settings: Any, config: Any) -> dict[str, str]:
    vector = settings.requirement_vector
    snapshot = getattr(settings, "selected_target_snapshot", None) or {}
    return {
        "inventory_schema_version": str(snapshot.get("inventory_schema_version")),
        "inventory_snapshot_id": str(snapshot.get("inventory_snapshot_id")),
        "selection_policy_version": str(snapshot.get("selection_policy_version")),
        "requirement_schema_version": str(vector.get("schema_version")),
        "requirement_revision_id": str(vector.get("revision_id")),
        "rubric_version": str(vector.get("derivation_version")),
        "constraint_registry_version": str(snapshot.get("constraint_registry_version")),
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
    required_record_keys = {
        "schema_version", "evidence_contract_version", "kind", "evidence_id", "captured_at",
        "attempt", "capture", "identity", "routing", "versions", "integrity", "token_usage",
        "privacy", "cohort",
    }
    if set(record) != required_record_keys or record.get("kind") != "execution_evidence":
        raise ExecutionEvidenceV3Error("execution evidence v3 fields are not canonical")
    if (
        not isinstance(record.get("evidence_id"), str)
        or not record["evidence_id"].startswith("sha256:")
        or not isinstance(record.get("captured_at"), str)
        or not isinstance(record.get("attempt"), int)
    ):
        raise ExecutionEvidenceV3Error("execution evidence v3 metadata is invalid")
    for key in FORBIDDEN_EVIDENCE_KEYS:
        if _contains_key(record, key):
            raise ExecutionEvidenceV3Error(f"execution evidence v3 contains forbidden key: {key}")
    identity = record.get("identity")
    if (
        not isinstance(identity, dict)
        or set(identity) != {
            "selected_model", "command_model", "provider_reported_model", "reasoning_effort",
            "command_reasoning_effort", "attestation",
        }
        or not identity.get("selected_model")
        or not identity.get("reasoning_effort")
    ):
        raise ExecutionEvidenceV3Error("execution evidence v3 requires exact selected model and reasoning")
    selected = identity.get("selected_model")
    commanded = identity.get("command_model")
    provider = identity.get("provider_reported_model")
    if not isinstance(commanded, str) and commanded is not None:
        raise ExecutionEvidenceV3Error("invalid command model")
    if not isinstance(provider, dict) or set(provider) != {
        "status", "value", "source", "confidence", "availability_reason",
    }:
        raise ExecutionEvidenceV3Error("invalid provider model evidence")
    provider_status = provider.get("status")
    provider_value = provider.get("value")
    if provider_status not in {"observed", "unavailable"}:
        raise ExecutionEvidenceV3Error("invalid provider model status")
    if provider_status == "observed":
        if not isinstance(provider_value, str) or not provider_value:
            raise ExecutionEvidenceV3Error("observed provider model requires a value")
    elif provider_value is not None:
        raise ExecutionEvidenceV3Error("unavailable provider model must not have a value")
    capture = record.get("capture")
    if (
        not isinstance(capture, dict)
        or set(capture) != {"source", "raw_provider_output_included", "raw_wrapper_output_included"}
        or capture.get("raw_provider_output_included") is not False
        or capture.get("raw_wrapper_output_included") is not False
        or not isinstance(capture.get("source"), str)
    ):
        raise ExecutionEvidenceV3Error("invalid execution evidence v3 capture")
    capture_source = capture["source"]
    if provider_status == "observed":
        expected_provider_metadata = {
            "codex_jsonl": ("codex_jsonl", "provider_observed"),
            "external_wrapper_attestation": ("external_wrapper_attestation", "wrapper_attested"),
        }.get(capture_source)
        if expected_provider_metadata is None or (
            provider.get("source"), provider.get("confidence")
        ) != expected_provider_metadata or provider.get("availability_reason") is not None:
            raise ExecutionEvidenceV3Error("provider model attribution is not canonical")
    elif (
        provider.get("source") is not None
        or provider.get("confidence") is not None
        or not isinstance(provider.get("availability_reason"), str)
        or not provider.get("availability_reason")
    ):
        raise ExecutionEvidenceV3Error("unavailable provider model metadata is not canonical")
    command_exact = commanded == selected and identity.get("command_reasoning_effort") == identity.get("reasoning_effort")
    expected_integrity = (
        "selected_command_mismatch" if not command_exact
        else "provider_model_mismatch" if provider_value and provider_value != selected
        else "compliant"
    )
    expected_attestation = (
        "verified" if provider_value == selected
        else "mismatch" if provider_value
        else "command_attributed" if command_exact
        else "unattributed"
    )
    if identity.get("attestation") != expected_attestation:
        raise ExecutionEvidenceV3Error("execution evidence v3 attestation is not canonical")
    versions = record.get("versions")
    if not isinstance(versions, dict) or any(
        not isinstance(value, str) or not value or value == "None" for value in versions.values()
    ):
        raise ExecutionEvidenceV3Error("execution evidence v3 requires every contract version")
    required_versions = {
        "inventory_schema_version", "inventory_snapshot_id", "selection_policy_version",
        "requirement_schema_version", "requirement_revision_id", "rubric_version",
        "constraint_registry_version", "target_contract_version", "review_policy_version",
        "review_rubric_version", "outcome_contract_version",
    }
    if set(versions) != required_versions:
        raise ExecutionEvidenceV3Error("execution evidence v3 version components are not canonical")
    if (
        versions["inventory_schema_version"] != "1"
        or versions["selection_policy_version"] != "execution-target-selector-v1"
        or versions["requirement_schema_version"] != "2"
        or versions["target_contract_version"] != TARGET_CONTRACT_VERSION
        or versions["outcome_contract_version"] != QUALITY_OUTCOME_VERSION
    ):
        raise ExecutionEvidenceV3Error("execution evidence v3 routing/version contract is inconsistent")
    routing = record.get("routing")
    if not isinstance(routing, dict) or set(routing) != {"target_id", "selection_reason", "selection_cohort"}:
        raise ExecutionEvidenceV3Error("invalid execution evidence v3 routing")
    if not all(isinstance(routing.get(key), str) and routing[key] for key in routing):
        raise ExecutionEvidenceV3Error("execution evidence v3 requires exact routing identity")
    expected_selection = (
        "automatic" if routing["selection_reason"] == "automatic_static_non_learned"
        else "override" if routing["selection_reason"] in {"operator_pin", "operator_preference"}
        else "v2-other"
    )
    if routing["selection_cohort"] != expected_selection:
        raise ExecutionEvidenceV3Error("execution evidence v3 selection cohort is not canonical")
    integrity = record.get("integrity")
    if integrity != {"status": expected_integrity, "adverse": expected_integrity != "compliant"}:
        raise ExecutionEvidenceV3Error("execution evidence v3 integrity is not canonical")
    token_usage = record.get("token_usage")
    if (
        not isinstance(token_usage, dict)
        or set(token_usage) != {"status", "values", "source", "confidence", "availability_reason"}
        or token_usage.get("status") not in {"observed", "unavailable"}
    ):
        raise ExecutionEvidenceV3Error("invalid execution evidence v3 token usage")
    token_observed = token_usage.get("status") == "observed"
    token_values = token_usage.get("values")
    if not isinstance(token_values, dict):
        raise ExecutionEvidenceV3Error("invalid execution evidence v3 token values")
    normalized_usage = {
        key: value for key, value in token_values.items()
        if key not in {"uncached_input_tokens", "known_total_tokens"} and value is not None
    }
    try:
        canonical_values = token_usage_payload(normalized_token_usage(normalized_usage) or {})
    except ValueError as exc:
        raise ExecutionEvidenceV3Error(str(exc)) from exc
    if token_values != canonical_values:
        raise ExecutionEvidenceV3Error("execution evidence v3 token values are not canonical")
    if token_observed != bool(normalized_usage):
        raise ExecutionEvidenceV3Error("execution evidence v3 token status is not canonical")
    expected_token_metadata = (
        (
            capture_source,
            "provider_observed" if capture_source == "codex_jsonl" else "wrapper_attested",
            None,
        )
        if token_observed
        else (None, None, "usage_not_exposed_by_provider_output")
    )
    if (
        token_usage.get("source"), token_usage.get("confidence"), token_usage.get("availability_reason")
    ) != expected_token_metadata:
        raise ExecutionEvidenceV3Error("execution evidence v3 token attribution is not canonical")
    cohort = record.get("cohort")
    expected_components = {
        "selection_cohort": expected_selection,
        "target_id": routing["target_id"],
        "selected_model": selected,
        "reasoning_effort": identity["reasoning_effort"],
        **versions,
    }
    expected_cohort_id = _stable_id(expected_components)
    expected_comparability = {
        "model_quality": expected_integrity == "compliant" and command_exact,
        "token_cost": expected_integrity == "compliant" and command_exact and token_observed,
        "monetary_cost": False,
    }
    expected_exclusions = [] if expected_integrity == "compliant" and command_exact else [expected_integrity]
    if (
        not isinstance(cohort, dict)
        or set(cohort) != {
            "definition_version", "cohort_id", "components", "comparability", "exclusion_reasons",
        }
        or cohort.get("definition_version") != COHORT_DEFINITION_VERSION
    ):
        raise ExecutionEvidenceV3Error("invalid execution evidence v3 cohort contract")
    if cohort.get("components") != expected_components:
        raise ExecutionEvidenceV3Error("execution evidence v3 cohort components are not canonical")
    if cohort.get("cohort_id") != expected_cohort_id:
        raise ExecutionEvidenceV3Error("execution evidence v3 cohort id is not canonical")
    if cohort.get("comparability") != expected_comparability:
        raise ExecutionEvidenceV3Error("execution evidence v3 comparability is not canonical")
    if cohort.get("exclusion_reasons") != expected_exclusions:
        raise ExecutionEvidenceV3Error("execution evidence v3 exclusion reasons are not canonical")
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

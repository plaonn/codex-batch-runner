from __future__ import annotations

import hashlib
import json
from typing import Any

from .execution_evidence_v2 import reporting_evidence_view, execution_backend
from .review_outcome_evidence import review_outcome_view
from .timeutil import iso_now


SCHEMA_VERSION = 1
EVIDENCE_CONTRACT_VERSION = "routing-cost-evidence-v1"
COHORT_DEFINITION_VERSION = "routing-cost-cohort-v1"
EXACT_EVIDENCE_CONTRACT_VERSION = "routing-cost-evidence-v2"
EXACT_COHORT_DEFINITION_VERSION = "routing-cost-cohort-v2"
ATTRIBUTION_CLASSES = {
    "provider_attributed",
    "window_estimated",
    "concurrent_confounded",
    "unavailable",
}
USAGE_KEYS = (
    "uncached_input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
FORBIDDEN_KEYS = {
    "prompt",
    "context",
    "transcript",
    "stdout",
    "stderr",
    "log_path",
    "session_id",
    "thread_id",
    "command",
    "cwd",
}


class RoutingCostEvidenceError(ValueError):
    pass


def build_routing_cost_evidence(
    task: dict[str, Any],
    *,
    attribution_class: str | None = None,
    usage: dict[str, int | None] | None = None,
    attribution_source: str | None = None,
    window_before: float | None = None,
    window_after: float | None = None,
) -> dict[str, Any]:
    execution = reporting_evidence_view(task)
    review = review_outcome_view(task)
    resolved_usage = _usage_from_execution(execution) if usage is None else _normalize_usage(usage)
    resolved_class = attribution_class or _default_attribution_class(execution, resolved_usage)
    if resolved_class not in ATTRIBUTION_CLASSES:
        raise RoutingCostEvidenceError("invalid routing cost attribution class")
    if resolved_class == "window_estimated":
        if window_before is None or window_after is None:
            raise RoutingCostEvidenceError("window_estimated attribution requires before and after values")
        if window_before < 0 or window_after < 0 or window_after < window_before:
            raise RoutingCostEvidenceError("invalid usage window values")
    elif window_before is not None or window_after is not None:
        raise RoutingCostEvidenceError("usage window values are only valid for window_estimated attribution")
    if resolved_class == "unavailable" and any(value is not None for value in resolved_usage.values()):
        raise RoutingCostEvidenceError("unavailable attribution cannot include usage values")

    captured_at = iso_now()
    exact = execution.get("schema_version") == 3
    record = {
        "schema_version": 2 if exact else SCHEMA_VERSION,
        "evidence_contract_version": EXACT_EVIDENCE_CONTRACT_VERSION if exact else EVIDENCE_CONTRACT_VERSION,
        "kind": "routing_cost_evidence",
        "evidence_id": _stable_id({"task": str(task.get("id") or "unknown"), "attempt": _int(task.get("attempts")), "captured_at": captured_at}),
        "captured_at": captured_at,
        "selection": _selection(task),
        "execution": {
            "surface": _execution_surface(task),
            "backend": execution_backend(task),
            "task_bucket": _task_bucket(task),
            "prompt_contract_version": _version(task.get("prompt_contract_version")),
            "context_contract_version": _version(task.get("context_contract_version")),
            "execution_evidence_contract_version": str(execution.get("evidence_contract_version") or "legacy-v1"),
        },
        "actual_model": dict(execution.get("actual_model") or {}),
        "usage": {
            "values": resolved_usage,
            "attribution": {
                "class": resolved_class,
                "source": attribution_source or _default_attribution_source(execution, resolved_class),
                "window_before": window_before,
                "window_after": window_after,
            },
        },
        "quality": _quality(task, review),
        "privacy": {
            "public_safe_export": True,
            "raw_prompt_included": False,
            "raw_context_included": False,
            "raw_transcript_included": False,
            "session_or_thread_ids_included": False,
            "raw_paths_included": False,
            "personal_usage_messages_included": False,
        },
    }
    if exact:
        record["identity"] = dict(execution.get("identity") or {})
        record["versions"] = dict(execution.get("versions") or {})
        record["target"] = dict(execution.get("routing") or {})
        execution_cohort = execution.get("cohort") if isinstance(execution.get("cohort"), dict) else {}
        execution_components = execution_cohort.get("components") if isinstance(execution_cohort.get("components"), dict) else {}
        record["target"]["selection_cohort"] = execution_components.get("selection_cohort")
    record["cohort"] = derive_routing_cost_cohort(record)
    validate_routing_cost_evidence(record)
    return record


def attach_routing_cost_evidence(task: dict[str, Any], record: dict[str, Any]) -> None:
    validate_routing_cost_evidence(record)
    history = task.setdefault("routing_cost_evidence_history", [])
    if not isinstance(history, list):
        raise RoutingCostEvidenceError("routing cost evidence history must be a list")
    if not any(isinstance(item, dict) and item.get("evidence_id") == record["evidence_id"] for item in history):
        history.append(record)


def latest_routing_cost_evidence(task: dict[str, Any]) -> dict[str, Any] | None:
    history = task.get("routing_cost_evidence_history")
    if not isinstance(history, list):
        return None
    for record in reversed(history):
        if isinstance(record, dict) and record.get("evidence_contract_version") in {EVIDENCE_CONTRACT_VERSION, EXACT_EVIDENCE_CONTRACT_VERSION}:
            return validate_routing_cost_evidence(record)
    return None


def legacy_routing_cost_view(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 0,
        "evidence_contract_version": "legacy-routing-cost-unknown",
        "kind": "legacy_routing_cost_evidence",
        "selection": _selection(task),
        "execution": {"surface": _execution_surface(task), "task_bucket": _task_bucket(task)},
        "usage": {"values": {key: None for key in USAGE_KEYS}, "attribution": {"class": "unavailable", "source": None, "window_before": None, "window_after": None}},
        "quality": {"comparable": False},
        "cohort": {
            "definition_version": COHORT_DEFINITION_VERSION,
            "cohort_id": "legacy-routing-cost-non-comparable",
            "components": {},
            "comparability": {"quality": False, "usage_cost": False, "joint_quality_cost": False},
            "exclusion_reasons": ["legacy_routing_cost_evidence"],
        },
        "privacy": {"public_safe_export": True},
    }


def routing_cost_evidence_view(task: dict[str, Any]) -> dict[str, Any]:
    return latest_routing_cost_evidence(task) or legacy_routing_cost_view(task)


def derive_routing_cost_cohort(record: dict[str, Any]) -> dict[str, Any]:
    execution = record["execution"]
    selection = record["selection"]
    actual_model = record["actual_model"]
    attribution = record["usage"]["attribution"]
    quality = record["quality"]
    components = {
        "evidence_contract_version": record["evidence_contract_version"],
        "execution_surface": execution["surface"],
        "execution_backend": execution["backend"],
        "task_bucket": execution["task_bucket"],
        "prompt_contract_version": execution["prompt_contract_version"],
        "context_contract_version": execution["context_contract_version"],
        "planned_model": selection["planned_model"],
        "planned_reasoning": selection["planned_reasoning"],
        "actual_model": actual_model.get("value") if actual_model.get("status") == "observed" else "unknown",
        "attribution_class": attribution["class"],
        "review_outcome_cohort_id": quality["review_outcome_cohort_id"],
    }
    exact = record.get("evidence_contract_version") == EXACT_EVIDENCE_CONTRACT_VERSION
    if exact:
        identity = record["identity"]
        versions = record["versions"]
        target = record["target"]
        components.update({
            "selection_cohort": target.get("selection_cohort") or "unknown",
            "target_id": target.get("target_id") or "unknown",
            "selected_model": identity.get("selected_model") or "unknown",
            "command_model": identity.get("command_model") or "unknown",
            "reasoning_effort": identity.get("reasoning_effort") or "unknown",
            **versions,
        })
    exclusions: list[str] = []
    for key in (
        "execution_surface",
        "task_bucket",
        "prompt_contract_version",
        "context_contract_version",
        "planned_model",
        "planned_reasoning",
    ):
        if _unversioned_component(components[key]):
            exclusions.append(f"{key}_unversioned")
    if actual_model.get("status") != "observed":
        exclusions.append("actual_model_unavailable")
    if exact:
        identity = record["identity"]
        if identity.get("adverse"):
            exclusions.append(str(identity.get("integrity_status") or "adverse_identity_integrity"))
        if identity.get("selected_model") != identity.get("command_model"):
            exclusions.append("selected_command_mismatch")
    usage_comparable = attribution["class"] in {"provider_attributed", "window_estimated"}
    if not usage_comparable:
        exclusions.append(f"usage_attribution_{attribution['class']}")
    quality_comparable = bool(quality["comparable"])
    if not quality_comparable:
        exclusions.append("quality_evidence_non_comparable")
    axes_complete = not any(reason.endswith("_unversioned") for reason in exclusions)
    identity_comparable = not exact or not bool(record["identity"].get("adverse"))
    quality_comparable = quality_comparable and axes_complete and actual_model.get("status") == "observed" and identity_comparable
    usage_comparable = usage_comparable and axes_complete and actual_model.get("status") == "observed" and identity_comparable
    return {
        "definition_version": EXACT_COHORT_DEFINITION_VERSION if exact else COHORT_DEFINITION_VERSION,
        "cohort_id": "sha256:" + _stable_hash(components)[:16],
        "components": components,
        "comparability": {"quality": quality_comparable, "usage_cost": usage_comparable, "joint_quality_cost": quality_comparable and usage_comparable},
        "exclusion_reasons": sorted(set(exclusions)),
    }


def validate_routing_cost_evidence(record: object) -> dict[str, Any]:
    if not isinstance(record, dict) or (record.get("schema_version"), record.get("evidence_contract_version")) not in {
        (SCHEMA_VERSION, EVIDENCE_CONTRACT_VERSION), (2, EXACT_EVIDENCE_CONTRACT_VERSION)
    }:
        raise RoutingCostEvidenceError("invalid routing cost evidence contract")
    for key in FORBIDDEN_KEYS:
        if _contains_key(record, key):
            raise RoutingCostEvidenceError(f"routing cost evidence contains forbidden key: {key}")
    selection = record.get("selection")
    if not isinstance(selection, dict) or set(selection) != {"planned_model", "planned_reasoning"}:
        raise RoutingCostEvidenceError("invalid planned selection")
    exact = record.get("evidence_contract_version") == EXACT_EVIDENCE_CONTRACT_VERSION
    if exact:
        identity = record.get("identity")
        versions = record.get("versions")
        target = record.get("target")
        if not isinstance(identity, dict) or not identity.get("selected_model") or not identity.get("command_model") or not identity.get("reasoning_effort"):
            raise RoutingCostEvidenceError("exact routing cost evidence requires selected, command, and reasoning identity")
        if not isinstance(versions, dict) or not versions or any(not isinstance(value, str) or not value or value == "None" for value in versions.values()):
            raise RoutingCostEvidenceError("exact routing cost evidence requires contract versions")
        if not isinstance(target, dict) or not target.get("target_id"):
            raise RoutingCostEvidenceError("exact routing cost evidence requires target id")
        if target.get("selection_cohort") not in {"automatic", "override"}:
            raise RoutingCostEvidenceError("exact routing cost evidence requires automatic or override selection cohort")
    execution = record.get("execution")
    required_execution = {"surface", "backend", "task_bucket", "prompt_contract_version", "context_contract_version", "execution_evidence_contract_version"}
    if not isinstance(execution, dict) or set(execution) != required_execution:
        raise RoutingCostEvidenceError("invalid routing execution axes")
    actual = record.get("actual_model")
    if not isinstance(actual, dict) or actual.get("status") not in {"observed", "unavailable"}:
        raise RoutingCostEvidenceError("invalid actual model observation")
    if actual.get("status") == "observed" and (
        not isinstance(actual.get("value"), str)
        or not actual["value"].strip()
        or not actual.get("source")
        or not actual.get("confidence")
    ):
        raise RoutingCostEvidenceError("observed actual model requires value, source, and confidence")
    if actual.get("status") == "unavailable" and (
        actual.get("value") is not None or not actual.get("availability_reason")
    ):
        raise RoutingCostEvidenceError("unavailable actual model requires an availability reason")
    usage = record.get("usage")
    values = usage.get("values") if isinstance(usage, dict) else None
    attribution = usage.get("attribution") if isinstance(usage, dict) else None
    if not isinstance(values, dict) or set(values) != set(USAGE_KEYS) or any(value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0) for value in values.values()):
        raise RoutingCostEvidenceError("invalid routing usage values")
    if not isinstance(attribution, dict) or attribution.get("class") not in ATTRIBUTION_CLASSES:
        raise RoutingCostEvidenceError("invalid routing usage attribution")
    attribution_class = attribution["class"]
    if attribution_class != "unavailable" and not attribution.get("source"):
        raise RoutingCostEvidenceError("available routing usage attribution requires source")
    before = attribution.get("window_before")
    after = attribution.get("window_after")
    if attribution_class == "window_estimated":
        if (
            not isinstance(before, (int, float))
            or isinstance(before, bool)
            or not isinstance(after, (int, float))
            or isinstance(after, bool)
            or before < 0
            or after < before
        ):
            raise RoutingCostEvidenceError("window_estimated attribution requires valid before and after values")
    elif before is not None or after is not None:
        raise RoutingCostEvidenceError("usage window values require window_estimated attribution")
    if attribution_class == "unavailable" and any(value is not None for value in values.values()):
        raise RoutingCostEvidenceError("unavailable attribution cannot include usage values")
    quality = record.get("quality")
    required_quality = {
        "review_outcome_evidence_contract_version",
        "review_outcome_cohort_id",
        "objective_verification",
        "semantic_review",
        "human_acceptance",
        "accepted",
        "rejected",
        "follow_up_count",
        "rework_count",
        "comparable",
    }
    if (
        not isinstance(quality, dict)
        or set(quality) != required_quality
        or any(not isinstance(quality[key], bool) for key in ("human_acceptance", "accepted", "rejected", "comparable"))
        or any(not isinstance(quality[key], int) or isinstance(quality[key], bool) or quality[key] < 0 for key in ("follow_up_count", "rework_count"))
    ):
        raise RoutingCostEvidenceError("invalid routing quality evidence")
    cohort = record.get("cohort")
    expected_cohort = EXACT_COHORT_DEFINITION_VERSION if exact else COHORT_DEFINITION_VERSION
    if not isinstance(cohort, dict) or cohort.get("definition_version") != expected_cohort:
        raise RoutingCostEvidenceError("invalid routing cost cohort")
    canonical_cohort = derive_routing_cost_cohort(record)
    if cohort != canonical_cohort:
        raise RoutingCostEvidenceError("routing cost cohort does not match canonical record axes")
    privacy = record.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("public_safe_export") is not True or any(value is not False for key, value in privacy.items() if key != "public_safe_export"):
        raise RoutingCostEvidenceError("routing cost evidence is not public-safe")
    return record


def _selection(task: dict[str, Any]) -> dict[str, str]:
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else {}
    resolved = last_run.get("resolved_execution_config") if isinstance(last_run.get("resolved_execution_config"), dict) else {}
    return {"planned_model": str(resolved.get("model") or "unknown"), "planned_reasoning": str(resolved.get("reasoning_effort") or resolved.get("reasoning") or "unknown")}


def _quality(task: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    acceptance = review.get("acceptance") if isinstance(review.get("acceptance"), dict) else {}
    objective = review.get("objective_verification") if isinstance(review.get("objective_verification"), dict) else {}
    semantic = review.get("semantic_review") if isinstance(review.get("semantic_review"), dict) else {}
    cohort = review.get("cohort") if isinstance(review.get("cohort"), dict) else {}
    comparability = cohort.get("comparability") if isinstance(cohort.get("comparability"), dict) else {}
    accepted = bool(acceptance.get("accepted"))
    return {
        "review_outcome_evidence_contract_version": str(review.get("evidence_contract_version") or "legacy-review-unknown"),
        "review_outcome_cohort_id": str(cohort.get("cohort_id") or "legacy-review-unknown"),
        "objective_verification": str(objective.get("status") or "unavailable"),
        "semantic_review": str(semantic.get("status") or "not_performed"),
        "human_acceptance": acceptance.get("method") == "human_accept" and accepted,
        "accepted": accepted,
        "rejected": not accepted and acceptance.get("method") != "none",
        "follow_up_count": _int(task.get("review_attempts")),
        "rework_count": _int(task.get("fix_attempts")),
        "comparable": bool(comparability.get("quality")),
    }


def _usage_from_execution(execution: dict[str, Any]) -> dict[str, int | None]:
    token_usage = execution.get("token_usage") if isinstance(execution.get("token_usage"), dict) else {}
    values = token_usage.get("values") if isinstance(token_usage.get("values"), dict) else {}
    return _normalize_usage({
        "uncached_input_tokens": values.get("uncached_input_tokens"),
        "cached_input_tokens": values.get("cached_input_tokens"),
        "cache_write_tokens": values.get("cache_write_tokens"),
        "output_tokens": values.get("output_tokens"),
        "reasoning_output_tokens": values.get("reasoning_output_tokens"),
    })


def _normalize_usage(usage: dict[str, int | None]) -> dict[str, int | None]:
    unknown = set(usage) - set(USAGE_KEYS)
    if unknown:
        raise RoutingCostEvidenceError("unsupported routing usage key(s): " + ", ".join(sorted(unknown)))
    result: dict[str, int | None] = {}
    for key in USAGE_KEYS:
        value = usage.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise RoutingCostEvidenceError(f"routing usage {key} must be a non-negative integer")
        result[key] = value
    return result


def _default_attribution_class(execution: dict[str, Any], usage: dict[str, int | None]) -> str:
    token_usage = execution.get("token_usage") if isinstance(execution.get("token_usage"), dict) else {}
    if token_usage.get("status") == "observed" and any(value is not None for value in usage.values()):
        return "provider_attributed"
    return "unavailable"


def _default_attribution_source(execution: dict[str, Any], attribution_class: str) -> str | None:
    if attribution_class == "provider_attributed":
        token_usage = execution.get("token_usage") if isinstance(execution.get("token_usage"), dict) else {}
        return str(token_usage.get("source") or "provider_observation")
    return None


def _execution_surface(task: dict[str, Any]) -> str:
    return str(task.get("execution_surface") or ("supplemental" if task.get("queue_task") is False else "cbr_batch"))


def _task_bucket(task: dict[str, Any]) -> str:
    scopes = task.get("verification_scope") if isinstance(task.get("verification_scope"), list) else []
    return "size={size} risk={risk} verify={verify}".format(size=task.get("routing_size") or "unknown", risk=task.get("routing_risk") or "unknown", verify=",".join(sorted(str(item) for item in scopes)) or "none")


def _version(value: object) -> str:
    return str(value).strip() if isinstance(value, str) and value.strip() else "legacy"


def _unversioned_component(value: object) -> bool:
    text = str(value or "").lower()
    return not text or text == "legacy" or "unknown" in text


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _stable_id(value: object) -> str:
    return "routing-cost-sha256:" + _stable_hash(value)[:24]


def _stable_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0

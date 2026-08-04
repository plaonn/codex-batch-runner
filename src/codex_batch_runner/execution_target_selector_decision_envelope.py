"""Strict, report-only execution-target selector decision envelopes."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any

from .capacity_target_ordering_simulation import (
    CapacityTargetOrderingSimulationError,
    validate_capacity_target_ordering_simulation_report,
)
from .model_requirements import routing_override_value

REQUEST_CONTRACT = "execution-target-selector-decision-envelope-request-v1"
ENVELOPE_CONTRACT = "execution-target-selector-decision-envelope-v1"
PRODUCER_ID = "execution-target-selector"
PRODUCER_REVISION = "execution-target-selector-decision-envelope-producer-v1"
DISPOSITIONS = {
    "authoritative_absence",
    "unattested",
    "operator_preference",
    "operator_pin",
    "operator_preference_fallback",
    "fail_closed",
}
MUTATION_FIELDS = (
    "reservation_mutations",
    "retry_mutations",
    "queue_mutations",
    "config_mutations",
    "cooldown_mutations",
    "wake_mutations",
    "selection_mutations",
    "dispatch_mutations",
    "routing_mutations",
)
SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-")


class ExecutionTargetSelectorDecisionEnvelopeError(ValueError):
    pass


def stable_digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            "value is not stable-digest serializable"
        ) from exc
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def selector_input_digest(
    *,
    task: object,
    scope: object,
    requirement_revision: object,
    inventory_snapshot_id: object,
    selector_policy_revision: object,
) -> str:
    """Return the canonical digest used by request.selector_inputs."""
    return stable_digest(
        {
            "task": deepcopy(task),
            "scope": deepcopy(scope),
            "requirement_revision": requirement_revision,
            "inventory_snapshot_id": inventory_snapshot_id,
            "selector_policy_revision": selector_policy_revision,
        }
    )


def validate_execution_target_selector_decision_envelope_request(
    value: object,
) -> dict[str, Any]:
    request = _obj("request", value)
    _keys(
        "request",
        request,
        {
            "schema_version",
            "contract",
            "evaluated_at",
            "task",
            "scope",
            "selector_inputs",
            "manual_override_source",
            "currentness",
            "baseline_report",
        },
    )
    _int_literal("request.schema_version", request.get("schema_version"), 1)
    _lit("request.contract", request.get("contract"), REQUEST_CONTRACT)
    evaluated = _at("request.evaluated_at", request.get("evaluated_at"))
    task = _task(request.get("task"))
    scope = _scope(request.get("scope"))
    inputs = _selector_inputs(request.get("selector_inputs"), task, scope)
    source = _manual_override_source(request.get("manual_override_source"), task)
    currentness = _currentness(request.get("currentness"), source, evaluated)
    try:
        baseline = validate_capacity_target_ordering_simulation_report(
            request.get("baseline_report")
        )
    except CapacityTargetOrderingSimulationError as exc:
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            "request.baseline_report is invalid"
        ) from exc
    for field in ("project_id", "repository_id", "task_class", "opt_in_scope_id"):
        _lit(
            "request baseline scope " + field,
            baseline["scope"][field],
            scope[field],
        )
    for field in (
        "requirement_revision",
        "inventory_snapshot_id",
        "selector_policy_revision",
    ):
        _lit(
            "request baseline selector input " + field,
            baseline["revisions"][field],
            inputs[field],
        )
    return {
        "schema_version": 1,
        "contract": REQUEST_CONTRACT,
        "evaluated_at": request["evaluated_at"],
        "task": task,
        "scope": scope,
        "selector_inputs": inputs,
        "manual_override_source": source,
        "currentness": currentness,
        "baseline_report": deepcopy(baseline),
    }


def build_execution_target_selector_decision_envelope(value: object) -> dict[str, Any]:
    request = validate_execution_target_selector_decision_envelope_request(value)
    return validate_execution_target_selector_decision_envelope(_build(request))


def validate_execution_target_selector_decision_envelope(
    value: object,
) -> dict[str, Any]:
    envelope = _obj("envelope", value)
    _keys(
        "envelope",
        envelope,
        {
            "schema_version",
            "contract",
            "evaluated_at",
            "task",
            "scope",
            "selector_inputs",
            "manual_override_binding",
            "currentness_binding",
            "baseline_binding",
            "disposition",
            "reason_codes",
            "selected_target_id",
            "producer_request",
            "input_digest",
            "artifact_digest",
            "report_only",
            "simulation_only",
            "activation_authority",
            "selection_authority",
            "dispatch_authority",
            "runtime_reservation",
            "runtime_feedback_mutation",
            "automatic_half_open",
            "automatic_retry",
            "queue_mutation",
            "config_mutation",
            "cooldown_mutation",
            "wake_mutation",
            "provider_call",
            "promotion_authority",
            *MUTATION_FIELDS,
        },
    )
    _int_literal("envelope.schema_version", envelope.get("schema_version"), 1)
    _lit("envelope.contract", envelope.get("contract"), ENVELOPE_CONTRACT)
    _digest("envelope.input_digest", envelope.get("input_digest"))
    _digest("envelope.artifact_digest", envelope.get("artifact_digest"))
    _lit("envelope.report_only", envelope.get("report_only"), True)
    _lit("envelope.simulation_only", envelope.get("simulation_only"), True)
    for field in (
        "activation_authority",
        "selection_authority",
        "dispatch_authority",
        "runtime_reservation",
        "runtime_feedback_mutation",
        "automatic_half_open",
        "automatic_retry",
        "queue_mutation",
        "config_mutation",
        "cooldown_mutation",
        "wake_mutation",
        "provider_call",
        "promotion_authority",
    ):
        _lit("envelope." + field, envelope.get(field), False)
    for field in MUTATION_FIELDS:
        _lit("envelope." + field, envelope.get(field), [])
    request = validate_execution_target_selector_decision_envelope_request(
        envelope.get("producer_request")
    )
    expected = _build(request)
    if not _type_exact_equal(envelope, expected):
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            "envelope must exactly match deterministic producer request replay"
        )
    return deepcopy(envelope)


def _build(request: dict[str, Any]) -> dict[str, Any]:
    report = request["baseline_report"]
    source = request["manual_override_source"]
    override = source["source_projection"]["routing_override"]
    baseline_order = deepcopy(report["baseline_order"])
    baseline_target = report["baseline"]["selected_target_id"]
    disposition = "fail_closed"
    selected: str | None = None
    reasons: list[str]

    # This standalone request contains only caller-provided source labels,
    # projection, timestamps, and digests. Those values can prove internal
    # consistency, but cannot attest that the canonical task source was read.
    # Explicit override content retains its existing validation semantics; only
    # an asserted absence would need source authority that this contract lacks.
    if source["status"] == "authoritative_absence":
        disposition = "unattested"
        selected = None
        reasons = ["manual_override_source_not_trusted"]
    elif override["target_id"] in baseline_order:
        disposition = "operator_" + override["mode"]
        selected = override["target_id"]
        reasons = ["validated_override_target_precedes_capacity"]
    elif override["mode"] == "preference" and override["allow_fallback"]:
        disposition = "operator_preference_fallback"
        selected = baseline_target
        reasons = ["validated_override_fallback_uses_immutable_baseline"]
    elif override["mode"] == "pin":
        reasons = ["manual_pin_unavailable"]
    else:
        reasons = ["explicit_fallback_exhausted"]

    if selected is not None and selected not in baseline_order:
        disposition = "fail_closed"
        selected = None
        reasons = ["selected_target_not_in_exact_baseline"]

    baseline_binding = {
        "contract": report["contract"],
        "report_digest": stable_digest(report),
        "baseline_decision_digest": report["baseline"]["decision_digest"],
        "selected_baseline_target": baseline_target,
        "ordered_eligible_target_ids": baseline_order,
    }
    body: dict[str, Any] = {
        "schema_version": 1,
        "contract": ENVELOPE_CONTRACT,
        "evaluated_at": request["evaluated_at"],
        "task": deepcopy(request["task"]),
        "scope": deepcopy(request["scope"]),
        "selector_inputs": deepcopy(request["selector_inputs"]),
        "manual_override_binding": deepcopy(source),
        "currentness_binding": deepcopy(request["currentness"]),
        "baseline_binding": baseline_binding,
        "disposition": disposition,
        "reason_codes": reasons,
        "selected_target_id": selected,
        "producer_request": deepcopy(request),
        "input_digest": stable_digest(request),
        "report_only": True,
        "simulation_only": True,
        "activation_authority": False,
        "selection_authority": False,
        "dispatch_authority": False,
        "runtime_reservation": False,
        "runtime_feedback_mutation": False,
        "automatic_half_open": False,
        "automatic_retry": False,
        "queue_mutation": False,
        "config_mutation": False,
        "cooldown_mutation": False,
        "wake_mutation": False,
        "provider_call": False,
        "promotion_authority": False,
        **{field: [] for field in MUTATION_FIELDS},
    }
    body["artifact_digest"] = stable_digest(body)
    return body


def _task(value: object) -> dict[str, Any]:
    task = _obj("request.task", value)
    _keys(
        "request.task",
        task,
        {
            "task_id",
            "canonical_task_source_revision",
            "task_attempts_before_claim",
            "attempt",
        },
    )
    _id("request.task.task_id", task["task_id"])
    _id(
        "request.task.canonical_task_source_revision",
        task["canonical_task_source_revision"],
    )
    before = _non_negative_int(
        "request.task.task_attempts_before_claim",
        task["task_attempts_before_claim"],
    )
    attempt = _non_negative_int("request.task.attempt", task["attempt"])
    if attempt != before + 1:
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            "request.task.attempt must equal task_attempts_before_claim + 1"
        )
    return deepcopy(task)


def _scope(value: object) -> dict[str, str]:
    scope = _obj("request.scope", value)
    _keys(
        "request.scope",
        scope,
        {"project_id", "repository_id", "task_class", "opt_in_scope_id"},
    )
    for field, item in scope.items():
        _id("request.scope." + field, item)
    return deepcopy(scope)


def _selector_inputs(
    value: object, task: dict[str, Any], scope: dict[str, str]
) -> dict[str, str]:
    inputs = _obj("request.selector_inputs", value)
    _keys(
        "request.selector_inputs",
        inputs,
        {
            "requirement_revision",
            "inventory_snapshot_id",
            "selector_policy_revision",
            "selector_input_digest",
        },
    )
    for field in (
        "requirement_revision",
        "inventory_snapshot_id",
        "selector_policy_revision",
    ):
        _id("request.selector_inputs." + field, inputs[field])
    expected = selector_input_digest(
        task=task,
        scope=scope,
        requirement_revision=inputs["requirement_revision"],
        inventory_snapshot_id=inputs["inventory_snapshot_id"],
        selector_policy_revision=inputs["selector_policy_revision"],
    )
    _lit(
        "request.selector_inputs.selector_input_digest",
        inputs["selector_input_digest"],
        expected,
    )
    return deepcopy(inputs)


def _manual_override_source(value: object, task: dict[str, Any]) -> dict[str, Any]:
    source = _obj("request.manual_override_source", value)
    _keys(
        "request.manual_override_source",
        source,
        {
            "status",
            "producer_id",
            "producer_revision",
            "source_revision",
            "source_projection",
            "source_projection_digest",
        },
    )
    status = source.get("status")
    if status not in {"present", "authoritative_absence"}:
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            "request.manual_override_source.status is invalid"
        )
    _lit(
        "request.manual_override_source.producer_id",
        source.get("producer_id"),
        PRODUCER_ID,
    )
    _lit(
        "request.manual_override_source.producer_revision",
        source.get("producer_revision"),
        PRODUCER_REVISION,
    )
    _id("request.manual_override_source.source_revision", source.get("source_revision"))
    _lit(
        "request.manual_override_source.source_revision",
        source.get("source_revision"),
        task["canonical_task_source_revision"],
    )
    projection = _obj(
        "request.manual_override_source.source_projection",
        source.get("source_projection"),
    )
    _keys(
        "request.manual_override_source.source_projection",
        projection,
        {"task_id", "canonical_task_source_revision", "routing_override"},
    )
    _lit("manual override source task id", projection["task_id"], task["task_id"])
    _lit(
        "manual override source task revision",
        projection["canonical_task_source_revision"],
        task["canonical_task_source_revision"],
    )
    raw_override = projection["routing_override"]
    if status == "authoritative_absence":
        if raw_override is not None:
            raise ExecutionTargetSelectorDecisionEnvelopeError(
                "authoritative absence requires explicit null routing_override"
            )
        normalized_override = None
    else:
        if raw_override in (None, "", {}):
            raise ExecutionTargetSelectorDecisionEnvelopeError(
                "present override source requires a non-empty canonical routing_override"
            )
        try:
            normalized_override = routing_override_value(
                "request.manual_override_source.source_projection.routing_override",
                raw_override,
            )
        except ValueError as exc:
            raise ExecutionTargetSelectorDecisionEnvelopeError(
                "manual override source does not satisfy canonical routing_override"
            ) from exc
        if normalized_override != raw_override:
            raise ExecutionTargetSelectorDecisionEnvelopeError(
                "manual override source must use the canonical routing_override projection"
            )
    normalized_projection = {
        "task_id": projection["task_id"],
        "canonical_task_source_revision": projection["canonical_task_source_revision"],
        "routing_override": deepcopy(normalized_override),
    }
    _lit(
        "request.manual_override_source.source_projection_digest",
        source.get("source_projection_digest"),
        stable_digest(normalized_projection),
    )
    result = deepcopy(source)
    result["source_projection"] = normalized_projection
    return result


def _currentness(
    value: object, source: dict[str, Any], evaluated: datetime
) -> dict[str, Any]:
    currentness = _obj("request.currentness", value)
    _keys(
        "request.currentness",
        currentness,
        {
            "producer_id",
            "producer_revision",
            "source_revision",
            "identity_authority",
            "observed_at",
            "expires_at",
            "source_projection_digest",
            "currentness_digest",
        },
    )
    expected = {
        "producer_id": PRODUCER_ID,
        "producer_revision": PRODUCER_REVISION,
        "source_revision": source["source_revision"],
        "identity_authority": "source_attested",
        "observed_at": currentness.get("observed_at"),
        "expires_at": currentness.get("expires_at"),
        "source_projection_digest": source["source_projection_digest"],
    }
    for field in (
        "producer_id",
        "producer_revision",
        "source_revision",
        "identity_authority",
        "source_projection_digest",
    ):
        _lit("request.currentness." + field, currentness.get(field), expected[field])
    observed = _at("request.currentness.observed_at", currentness.get("observed_at"))
    expires = _at("request.currentness.expires_at", currentness.get("expires_at"))
    if not observed <= evaluated < expires:
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            "request.currentness is not current at evaluation time"
        )
    _lit(
        "request.currentness.currentness_digest",
        currentness.get("currentness_digest"),
        stable_digest(expected),
    )
    return deepcopy(currentness)


def _obj(name: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionTargetSelectorDecisionEnvelopeError(f"{name} must be an object")
    return value


def _keys(name: str, value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            f"{name} must contain exactly: {', '.join(sorted(expected))}"
        )


def _lit(name: str, actual: object, expected: object) -> None:
    if not _type_exact_equal(actual, expected):
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            f"{name} must equal {expected!r}"
        )


def _int_literal(name: str, value: object, expected: int) -> None:
    if type(value) is not int or value != expected:
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            f"{name} must be integer {expected}"
        )


def _non_negative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            f"{name} must be a non-negative integer"
        )
    return value


def _id(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(char not in SAFE for char in value)
    ):
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            f"{name} must be a public-safe identifier"
        )
    return value


def _at(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            f"{name} must be a timezone-aware timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            f"{name} must be a timezone-aware timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            f"{name} must be a timezone-aware timestamp"
        )
    return parsed


def _digest(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise ExecutionTargetSelectorDecisionEnvelopeError(
            f"{name} must be a lowercase sha256 digest"
        )
    return value


def _type_exact_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _type_exact_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _type_exact_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right

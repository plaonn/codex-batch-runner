"""Deterministic, non-mutating capacity reservation/feedback previews."""

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
from .provider_resource_authority import (
    resource_gate_key,
    validate_admission_policy,
    validate_gate_decision,
    validate_gate_state,
    validate_mapping_v2,
)
from .provider_resource_report import ProviderResourceValidationError

REQUEST_CONTRACT = "capacity-reservation-feedback-simulation-request-v1"
REPORT_CONTRACT = "capacity-reservation-feedback-simulation-v1"
POLICY_REVISION = "capacity-reservation-feedback-simulation-policy-v1"
RETRY_POLICY_REVISION = "capacity-reservation-feedback-retry-policy-v1"
MANUAL_OVERRIDE_BINDING_GAP = "manual_override_binding_not_expressed_by_selector_report"
MUTATION_FIELDS = (
    "queue_mutations",
    "config_mutations",
    "cooldown_mutations",
    "wake_mutations",
    "selection_mutations",
    "dispatch_mutations",
    "routing_mutations",
    "reservation_mutations",
    "feedback_mutations",
    "retry_mutations",
)
SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-")
GATE_STATUSES = {"allowed", "gated", "unknown"}
OUTCOMES = {"success", "failure", "unknown", "recovery"}


class CapacityReservationFeedbackSimulationError(ValueError):
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
        raise CapacityReservationFeedbackSimulationError(
            "value is not stable-digest serializable"
        ) from exc
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_capacity_reservation_feedback_simulation_request(
    value: object,
) -> dict[str, Any]:
    request = _obj("request", value)
    _keys(
        "request",
        request,
        {
            "schema_version",
            "contract",
            "scope",
            "revisions",
            "mapping",
            "admission_policy",
            "currentness_evidence",
            "selector_binding",
            "resource",
            "gates",
            "replay",
            "predecessor_events",
            "reservation",
            "feedback",
            "retry_budget",
        },
    )
    _int_literal("request.schema_version", request.get("schema_version"), 1)
    _lit("request.contract", request.get("contract"), REQUEST_CONTRACT)
    scope = _scope(request.get("scope"))
    revisions = _revisions(request.get("revisions"))
    replay = _replay(request.get("replay"))
    try:
        mapping = validate_mapping_v2(request.get("mapping"))
        policy = validate_admission_policy(request.get("admission_policy"))
    except ProviderResourceValidationError as exc:
        raise CapacityReservationFeedbackSimulationError(
            "request mapping or admission policy is invalid"
        ) from exc
    _lit(
        "request.revisions.mapping_revision",
        revisions["mapping_revision"],
        mapping["mapping_revision"],
    )
    _lit(
        "request.revisions.policy_revision",
        revisions["policy_revision"],
        policy["policy_revision"],
    )
    resource, binding, rule = _resource(
        request.get("resource"), scope, revisions, mapping, policy, replay
    )
    currentness = _currentness(
        request.get("currentness_evidence"),
        scope,
        revisions,
        mapping,
        policy,
        resource,
        replay,
    )
    selector = _selector(
        request.get("selector_binding"), scope, revisions, mapping, resource
    )
    gates = _gates(request.get("gates"), scope, resource, revisions, replay)
    events = _events(request.get("predecessor_events"), replay["evaluated_at"])
    reservation = _reservation(
        request.get("reservation"),
        scope,
        revisions,
        mapping,
        policy,
        currentness,
        selector,
        resource,
        binding,
        gates,
        replay,
    )
    feedback = _feedback(
        request.get("feedback"),
        scope,
        revisions,
        currentness,
        resource,
        gates,
        events,
        replay,
    )
    retry = _retry(request.get("retry_budget"), scope, revisions, selector)
    # Retain the selected applicable source rule in validation, but do not add a
    # second caller-authored authority copy to the normalized request.
    if rule["target_id"] != scope["target_id"]:
        raise CapacityReservationFeedbackSimulationError(
            "applicable policy target rule mismatch"
        )
    return {
        "schema_version": 1,
        "contract": REQUEST_CONTRACT,
        "scope": scope,
        "revisions": revisions,
        "mapping": deepcopy(mapping),
        "admission_policy": deepcopy(policy),
        "currentness_evidence": currentness,
        "selector_binding": selector,
        "resource": resource,
        "gates": gates,
        "replay": replay,
        "predecessor_events": events,
        "reservation": reservation,
        "feedback": feedback,
        "retry_budget": retry,
    }


def simulate_capacity_reservation_feedback(value: object) -> dict[str, Any]:
    request = validate_capacity_reservation_feedback_simulation_request(value)
    return validate_capacity_reservation_feedback_simulation_report(_build(request))


def validate_capacity_reservation_feedback_simulation_report(
    value: object,
) -> dict[str, Any]:
    report = _obj("report", value)
    required = {
        "schema_version",
        "contract",
        "evaluated_at",
        "scope",
        "preview",
        "reason_codes",
        "reservation_preview",
        "feedback_preview",
        "half_open_preview",
        "retry_preview",
        "event_results",
        "simulation_request",
        "input_digest",
        "replay_digest",
        "simulation_only",
        "activation_authority",
        "runtime_reservation",
        "runtime_feedback_mutation",
        "automatic_half_open",
        "automatic_retry",
        "queue_mutation",
        "config_mutation",
        "cooldown_mutation",
        "wake_mutation",
        "selection_mutation",
        "dispatch_authority",
        "provider_call",
        "promotion_authority",
        "live_routing",
        "default_routing",
        "worker_promotion",
        "provider_promotion",
        "manual_override_binding_resolved",
        *MUTATION_FIELDS,
    }
    _keys("report", report, required)
    _int_literal("report.schema_version", report.get("schema_version"), 1)
    _lit("report.contract", report.get("contract"), REPORT_CONTRACT)
    request = validate_capacity_reservation_feedback_simulation_request(
        report.get("simulation_request")
    )
    _digest("report.input_digest", report.get("input_digest"))
    _digest("report.replay_digest", report.get("replay_digest"))
    for field in (
        "simulation_only",
        "activation_authority",
        "runtime_reservation",
        "runtime_feedback_mutation",
        "automatic_half_open",
        "automatic_retry",
        "queue_mutation",
        "config_mutation",
        "cooldown_mutation",
        "wake_mutation",
        "selection_mutation",
        "dispatch_authority",
        "provider_call",
        "promotion_authority",
        "live_routing",
        "default_routing",
        "worker_promotion",
        "provider_promotion",
    ):
        _lit("report." + field, report.get(field), field == "simulation_only")
    for field in MUTATION_FIELDS:
        _lit("report." + field, report.get(field), [])
    _lit(
        "report.manual_override_binding_resolved",
        report.get("manual_override_binding_resolved"),
        False,
    )
    expected = _build(request)
    if not _type_exact_equal(report, expected):
        raise CapacityReservationFeedbackSimulationError(
            "report must exactly match deterministic simulation request replay"
        )
    return deepcopy(report)


def _build(request: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    gates = request["gates"]
    selector = request["selector_binding"]
    retry = request["retry_budget"]["body"]

    # Canonical global-first precedence. Target state is intentionally ignored
    # while the global tuple is not allowed.
    if gates["global"]["status"] != "allowed":
        reasons.append("global_gate_" + gates["global"]["status"])
    elif gates["target"]["status"] != "allowed":
        reasons.append("target_gate_" + gates["target"]["status"])
    elif selector["hard_constraints"] != "pass":
        reasons.append("selector_hard_constraints_not_pass")
    elif selector["exact_target_eligibility"] != "pass":
        reasons.append("selector_exact_target_eligibility_not_pass")
    elif selector["quality_floor"] != "pass":
        reasons.append("selector_quality_floor_not_pass")
    elif selector["activation_report"]["decision"] == "fail_closed":
        reasons.append("selector_activation_report_fail_closed")
    elif not retry["cooldown_inactive"]:
        reasons.append("retry_cooldown_active")
    elif not retry["dependencies_satisfied"]:
        reasons.append("retry_dependencies_unsatisfied")
    elif not retry["resume_stop_inactive"]:
        reasons.append("retry_resume_stop_active")
    elif not retry["operator_stop_inactive"]:
        reasons.append("retry_operator_stop_active")
    elif not retry["task_attempt_boundary_preserved"]:
        reasons.append("retry_task_attempt_boundary_not_preserved")
    reasons.append(MANUAL_OVERRIDE_BINDING_GAP)

    feedback_body = deepcopy(request["feedback"]["body"])
    recovery = request["feedback"]["recovery_evidence"]
    half_open_reason = (
        MANUAL_OVERRIDE_BINDING_GAP
        if recovery is not None
        else "no_source_attested_recovery_evidence"
    )
    event_results = [
        {
            "event_id": event["body"]["event_id"],
            "outcome": event["body"]["outcome"],
            "status": "retained_observation",
        }
        for event in request["predecessor_events"]
    ]
    event_results.append(
        {
            "event_id": feedback_body["event_id"],
            "outcome": feedback_body["outcome"],
            "status": "retained_observation",
        }
    )
    body: dict[str, Any] = {
        "schema_version": 1,
        "contract": REPORT_CONTRACT,
        "evaluated_at": request["replay"]["evaluated_at"],
        "scope": deepcopy(request["scope"]),
        "preview": "fail_closed",
        "reason_codes": reasons,
        "reservation_preview": {
            "status": "not_reserved",
            "reason": reasons[0],
            "expires_at": request["reservation"]["body"]["expires_at"],
        },
        "feedback_preview": {
            "status": "retained_observation",
            "event_id": feedback_body["event_id"],
            "outcome": feedback_body["outcome"],
            "evidence_digest": request["feedback"]["evidence_digest"],
        },
        "half_open_preview": {
            "status": "not_eligible",
            "reason": half_open_reason,
            "candidate_resource_keys": [],
        },
        "retry_preview": {
            "status": "would_not_retry",
            "safety_status": "safe" if _retry_safe(retry) else "unsafe",
            "remaining_preview": retry["remaining"],
            "separate_from_provider_quota": True,
            "separate_from_task_attempt_limit": True,
        },
        "event_results": event_results,
        "simulation_request": deepcopy(request),
        "input_digest": stable_digest(request),
        "simulation_only": True,
        "activation_authority": False,
        "runtime_reservation": False,
        "runtime_feedback_mutation": False,
        "automatic_half_open": False,
        "automatic_retry": False,
        "queue_mutation": False,
        "config_mutation": False,
        "cooldown_mutation": False,
        "wake_mutation": False,
        "selection_mutation": False,
        "dispatch_authority": False,
        "provider_call": False,
        "promotion_authority": False,
        "live_routing": False,
        "default_routing": False,
        "worker_promotion": False,
        "provider_promotion": False,
        "manual_override_binding_resolved": False,
        **{field: [] for field in MUTATION_FIELDS},
    }
    body["replay_digest"] = stable_digest(body)
    return body


def _scope(value: object) -> dict[str, Any]:
    scope = _obj("request.scope", value)
    _keys(
        "request.scope",
        scope,
        {
            "project_id",
            "repository_id",
            "task_class",
            "opt_in_scope_id",
            "task_id",
            "attempt_id",
            "target_id",
            "opted_in",
        },
    )
    for field in scope:
        if field != "opted_in":
            _id("request.scope." + field, scope[field])
    _lit("request.scope.opted_in", scope["opted_in"], True)
    return deepcopy(scope)


def _revisions(value: object) -> dict[str, Any]:
    revisions = _obj("request.revisions", value)
    _keys(
        "request.revisions",
        revisions,
        {
            "mapping_revision",
            "currentness_revision",
            "policy_revision",
            "selector_revision",
            "resume_revision",
            "retry_policy_revision",
            "simulation_policy_revision",
        },
    )
    for field, item in revisions.items():
        _id("request.revisions." + field, item)
    _lit(
        "request.revisions.retry_policy_revision",
        revisions["retry_policy_revision"],
        RETRY_POLICY_REVISION,
    )
    _lit(
        "request.revisions.simulation_policy_revision",
        revisions["simulation_policy_revision"],
        POLICY_REVISION,
    )
    return deepcopy(revisions)


def _replay(value: object) -> dict[str, str]:
    replay = _obj("request.replay", value)
    _keys("request.replay", replay, {"evaluated_at"})
    _at(replay["evaluated_at"])
    return deepcopy(replay)


def _resource(
    value: object,
    scope: dict[str, Any],
    revisions: dict[str, Any],
    mapping: dict[str, Any],
    policy: dict[str, Any],
    replay: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    resource = _obj("request.resource", value)
    _keys(
        "request.resource",
        resource,
        {
            "target_id",
            "binding_id",
            "provider_id",
            "quota_identity_id",
            "scope_id",
            "window_id",
            "canonical_key",
            "mapping_revision",
            "policy_revision",
            "identity_authority",
            "verified_at",
            "expires_at",
        },
    )
    for field in resource:
        if field not in {"verified_at", "expires_at"}:
            _id("request.resource." + field, resource[field])
    verified = _at(resource["verified_at"])
    expires = _at(resource["expires_at"])
    evaluated = _at(replay["evaluated_at"])
    if not verified <= evaluated < expires:
        raise CapacityReservationFeedbackSimulationError(
            "resource mapping binding is not current at replay time"
        )
    if mapping["status"] != "current" or policy["status"] != "current":
        raise CapacityReservationFeedbackSimulationError(
            "mapping and admission policy must be current"
        )
    if not policy["enabled"]:
        raise CapacityReservationFeedbackSimulationError(
            "admission policy must be enabled"
        )
    if mapping["mapping_revision"] not in policy["allowed_mapping_revisions"]:
        raise CapacityReservationFeedbackSimulationError(
            "admission policy rejects mapping revision"
        )
    bindings = [
        binding
        for binding in mapping["bindings"]
        if binding["target_id"] == scope["target_id"]
        and binding["status"] == "current"
        and binding["identity_authority"] == "source_attested"
        and _at(binding["verified_at"]) <= evaluated < _at(binding["expires_at"])
    ]
    if len(bindings) != 1:
        raise CapacityReservationFeedbackSimulationError(
            "exactly one current source-attested target binding is required"
        )
    binding = bindings[0]
    rules = [
        rule
        for rule in policy["target_rules"]
        if rule["target_id"] == scope["target_id"]
        and rule["provider_id"] == binding["provider_id"]
    ]
    if len(rules) != 1:
        raise CapacityReservationFeedbackSimulationError(
            "exactly one applicable enabled target policy rule is required"
        )
    rule = rules[0]
    windows = {window["window_id"]: window for window in rule["window_rules"]}
    if resource["window_id"] not in windows:
        raise CapacityReservationFeedbackSimulationError(
            "resource window is not admitted by target policy rule"
        )
    expected = {
        "target_id": scope["target_id"],
        "binding_id": binding["binding_id"],
        "provider_id": binding["provider_id"],
        "quota_identity_id": binding["quota_identity_id"],
        "scope_id": binding["observation_scope"]["scope_id"],
        "window_id": resource["window_id"],
        "canonical_key": resource_gate_key(
            binding["provider_id"],
            binding["quota_identity_id"],
            binding["observation_scope"]["scope_id"],
            resource["window_id"],
        ),
        "mapping_revision": revisions["mapping_revision"],
        "policy_revision": revisions["policy_revision"],
        "identity_authority": "source_attested",
        "verified_at": binding["verified_at"],
        "expires_at": binding["expires_at"],
    }
    if resource != expected:
        raise CapacityReservationFeedbackSimulationError(
            "resource must exact-bind the current source artifact"
        )
    return deepcopy(resource), deepcopy(binding), deepcopy(rule)


def _currentness(
    value: object,
    scope: dict[str, Any],
    revisions: dict[str, Any],
    mapping: dict[str, Any],
    policy: dict[str, Any],
    resource: dict[str, Any],
    replay: dict[str, str],
) -> dict[str, Any]:
    evidence = _evidence("request.currentness_evidence", value)
    body = evidence["body"]
    _keys(
        "request.currentness_evidence.body",
        body,
        {
            "target_id",
            "resource_key",
            "scope",
            "mapping_revision",
            "policy_revision",
            "currentness_revision",
            "mapping_artifact_digest",
            "policy_artifact_digest",
            "observed_at",
            "expires_at",
            "identity_authority",
        },
    )
    expected_scope = _source_scope(scope)
    _lit("currentness target", body["target_id"], scope["target_id"])
    _lit("currentness resource", body["resource_key"], resource["canonical_key"])
    _lit("currentness scope", body["scope"], expected_scope)
    _lit(
        "currentness mapping revision",
        body["mapping_revision"],
        revisions["mapping_revision"],
    )
    _lit(
        "currentness policy revision",
        body["policy_revision"],
        revisions["policy_revision"],
    )
    _lit(
        "currentness revision",
        body["currentness_revision"],
        revisions["currentness_revision"],
    )
    _lit(
        "currentness mapping artifact digest",
        body["mapping_artifact_digest"],
        stable_digest(mapping),
    )
    _lit(
        "currentness policy artifact digest",
        body["policy_artifact_digest"],
        stable_digest(policy),
    )
    _lit(
        "currentness identity authority", body["identity_authority"], "source_attested"
    )
    _digest("currentness mapping artifact digest", body["mapping_artifact_digest"])
    _digest("currentness policy artifact digest", body["policy_artifact_digest"])
    observed = _at(body["observed_at"])
    expires = _at(body["expires_at"])
    evaluated = _at(replay["evaluated_at"])
    if not observed <= evaluated < expires:
        raise CapacityReservationFeedbackSimulationError(
            "source-attested currentness is not current at replay time"
        )
    return evidence


def _selector(
    value: object,
    scope: dict[str, Any],
    revisions: dict[str, Any],
    mapping: dict[str, Any],
    resource: dict[str, Any],
) -> dict[str, Any]:
    selector = _obj("request.selector_binding", value)
    _keys(
        "request.selector_binding",
        selector,
        {
            "activation_report",
            "activation_report_digest",
            "hard_constraints",
            "exact_target_eligibility",
            "quality_floor",
            "eligible_target_ids",
            "immutable_baseline_digest",
            "immutable_baseline_order",
            "selected_target_id",
            "selector_revision",
            "resume_target_id",
            "resume_revision",
            "manual_override_binding_resolved",
        },
    )
    try:
        report = validate_capacity_target_ordering_simulation_report(
            selector["activation_report"]
        )
    except CapacityTargetOrderingSimulationError as exc:
        raise CapacityReservationFeedbackSimulationError(
            "selector activation report is invalid"
        ) from exc
    _validate_digest_fields("selector activation report", report)
    _lit(
        "selector activation report digest",
        selector["activation_report_digest"],
        stable_digest(report),
    )
    _digest("selector activation report digest", selector["activation_report_digest"])
    report_scope = report["scope"]
    for field in ("project_id", "repository_id", "task_class", "opt_in_scope_id"):
        _lit("selector scope " + field, report_scope[field], scope[field])
    report_gates = report["simulation_request"]["global_gate"]
    for field in ("hard_constraints", "exact_target_eligibility", "quality_floor"):
        _lit("selector " + field, selector[field], report_gates[field])
    _lit(
        "selector eligible target ids",
        selector["eligible_target_ids"],
        report["baseline_order"],
    )
    _lit(
        "selector immutable baseline order",
        selector["immutable_baseline_order"],
        report["baseline_order"],
    )
    _lit(
        "selector immutable baseline digest",
        selector["immutable_baseline_digest"],
        stable_digest(
            {"baseline": report["baseline"], "baseline_order": report["baseline_order"]}
        ),
    )
    _digest("selector immutable baseline digest", selector["immutable_baseline_digest"])
    _lit(
        "selector selected target",
        selector["selected_target_id"],
        report["counterfactual_target_id"],
    )
    _lit("selector target scope", selector["selected_target_id"], scope["target_id"])
    if selector["selected_target_id"] not in selector["eligible_target_ids"]:
        raise CapacityReservationFeedbackSimulationError(
            "selector target must be exactly eligible"
        )
    _lit(
        "selector revision",
        selector["selector_revision"],
        report["revisions"]["selector_policy_revision"],
    )
    _lit(
        "selector request revision",
        selector["selector_revision"],
        revisions["selector_revision"],
    )
    _lit(
        "selector mapping revision",
        report["revisions"]["mapping_revision"],
        mapping["mapping_revision"],
    )
    _lit(
        "selector resume target",
        selector["resume_target_id"],
        report["resume_target_id"],
    )
    if selector["resume_target_id"] is not None:
        _lit(
            "selector resume target scope",
            selector["resume_target_id"],
            scope["target_id"],
        )
    _lit(
        "selector resume revision",
        selector["resume_revision"],
        revisions["resume_revision"],
    )
    _lit(
        "selector manual override binding",
        selector["manual_override_binding_resolved"],
        False,
    )
    # Exact resource target binding is explicit even though the upstream report
    # does not yet express the independent manual-override policy axis.
    _lit(
        "selector resource target",
        resource["target_id"],
        selector["selected_target_id"],
    )
    result = deepcopy(selector)
    result["activation_report"] = report
    return result


def _gates(
    value: object,
    scope: dict[str, Any],
    resource: dict[str, Any],
    revisions: dict[str, Any],
    replay: dict[str, str],
) -> dict[str, Any]:
    gates = _obj("request.gates", value)
    _keys("request.gates", gates, {"state", "decisions", "global", "target"})
    try:
        state = validate_gate_state(gates["state"])
        raw_decisions = _list("request.gates.decisions", gates["decisions"])
        decisions = [validate_gate_decision(item) for item in raw_decisions]
    except ProviderResourceValidationError as exc:
        raise CapacityReservationFeedbackSimulationError(
            "canonical gate state or decision evidence is invalid"
        ) from exc
    if len(decisions) != 2 or len({item["decision_key"] for item in decisions}) != 2:
        raise CapacityReservationFeedbackSimulationError(
            "exactly two unique canonical gate decisions are required"
        )
    by_key = {item["decision_key"]: item for item in decisions}
    normalized: dict[str, Any] = {
        "state": state,
        "decisions": decisions,
    }
    evaluated_at = _at(replay["evaluated_at"])
    for name in ("global", "target"):
        item = _gate_tuple("request.gates." + name, gates[name])
        decision = by_key.get(item["decision_key"])
        if decision is None:
            raise CapacityReservationFeedbackSimulationError(
                name + " gate tuple lacks exact canonical decision evidence"
            )
        expected_status = _decision_status(decision["action"])
        expected = {
            "resource_key": decision["resource_key"],
            "decision_key": decision["decision_key"],
            "wake_key": decision["wake_key"],
            "status": expected_status,
        }
        if item != expected:
            raise CapacityReservationFeedbackSimulationError(
                name + " gate tuple does not match canonical evidence"
            )
        if (
            decision["policy_revision"] != revisions["policy_revision"]
            or decision["mapping_revision"] != revisions["mapping_revision"]
        ):
            raise CapacityReservationFeedbackSimulationError(
                name + " gate decision revision mismatch"
            )
        if _at(decision["observed_at"]) > evaluated_at:
            raise CapacityReservationFeedbackSimulationError(
                name + " gate decision is future-dated"
            )
        if _at(decision["reset_at"]) <= evaluated_at:
            raise CapacityReservationFeedbackSimulationError(
                name + " gate reset and wake boundary must be future-current"
            )
        normalized[name] = item
    if normalized["global"]["decision_key"] == normalized["target"]["decision_key"]:
        raise CapacityReservationFeedbackSimulationError(
            "global and target gates must be distinct"
        )
    if normalized["target"]["resource_key"] != resource["canonical_key"]:
        raise CapacityReservationFeedbackSimulationError(
            "target gate resource mismatch"
        )
    global_decision = by_key[normalized["global"]["decision_key"]]
    expected_global = {
        "provider_id": "global",
        "quota_identity_id": "global",
        "scope_id": scope["opt_in_scope_id"],
        "window_id": "admission",
        "resource_key": resource_gate_key(
            "global", "global", scope["opt_in_scope_id"], "admission"
        ),
    }
    if any(
        global_decision[field] != expected_value
        for field, expected_value in expected_global.items()
    ):
        raise CapacityReservationFeedbackSimulationError(
            "global gate decision does not bind the opted-in global scope"
        )
    target_decision = by_key[normalized["target"]["decision_key"]]
    expected_target = {
        "provider_id": resource["provider_id"],
        "quota_identity_id": resource["quota_identity_id"],
        "scope_id": resource["scope_id"],
        "window_id": resource["window_id"],
        "resource_key": resource["canonical_key"],
    }
    if any(
        target_decision[field] != expected_value
        for field, expected_value in expected_target.items()
    ):
        raise CapacityReservationFeedbackSimulationError(
            "target gate decision does not bind the selected target resource"
        )
    active_expected = sorted(
        [
            {
                "resource_key": decision["resource_key"],
                "decision_key": decision["decision_key"],
                "wake_key": decision["wake_key"],
                "reset_at": decision["reset_at"],
                "status": "active",
            }
            for decision in decisions
            if _decision_status(decision["action"]) == "gated"
        ],
        key=lambda item: item["resource_key"],
    )
    actual_active = sorted(state["active_gates"], key=lambda item: item["resource_key"])
    if actual_active != active_expected:
        raise CapacityReservationFeedbackSimulationError(
            "gate state must exact-bind all and only gated canonical tuples"
        )
    return normalized


def _gate_tuple(name: str, value: object) -> dict[str, str]:
    item = _obj(name, value)
    _keys(name, item, {"resource_key", "decision_key", "wake_key", "status"})
    for field in ("resource_key", "decision_key", "wake_key"):
        _id(name + "." + field, item[field])
    if item["status"] not in GATE_STATUSES:
        raise CapacityReservationFeedbackSimulationError(name + ".status invalid")
    return deepcopy(item)


def _decision_status(action: str) -> str:
    if action == "allow":
        return "allowed"
    if action in {"defer", "covered_by_global"}:
        return "gated"
    return "unknown"


def _events(value: object, evaluated_at: str) -> list[dict[str, Any]]:
    raw_events = _list("request.predecessor_events", value)
    events: list[dict[str, Any]] = []
    previous_id: str | None = None
    previous_at: datetime | None = None
    seen_ids: set[str] = set()
    seen_digests: set[str] = set()
    for index, raw in enumerate(raw_events):
        event = _evidence(f"request.predecessor_events[{index}]", raw)
        body = event["body"]
        _keys(
            f"request.predecessor_events[{index}].body",
            body,
            {"event_id", "predecessor_event_id", "observed_at", "outcome"},
        )
        event_id = _id("predecessor event id", body["event_id"])
        if body["outcome"] not in OUTCOMES:
            raise CapacityReservationFeedbackSimulationError(
                "predecessor event outcome invalid"
            )
        observed = _at(body["observed_at"])
        if (
            body["predecessor_event_id"] != previous_id
            or (previous_at is not None and observed <= previous_at)
            or observed > _at(evaluated_at)
        ):
            raise CapacityReservationFeedbackSimulationError(
                "predecessor lineage is broken, unordered, or future-dated"
            )
        if event_id in seen_ids or event["evidence_digest"] in seen_digests:
            raise CapacityReservationFeedbackSimulationError(
                "predecessor event ids and digests must be unique"
            )
        seen_ids.add(event_id)
        seen_digests.add(event["evidence_digest"])
        previous_id = event_id
        previous_at = observed
        events.append(event)
    return events


def _reservation(
    value: object,
    scope: dict[str, Any],
    revisions: dict[str, Any],
    mapping: dict[str, Any],
    policy: dict[str, Any],
    currentness: dict[str, Any],
    selector: dict[str, Any],
    resource: dict[str, Any],
    binding: dict[str, Any],
    gates: dict[str, Any],
    replay: dict[str, str],
) -> dict[str, Any]:
    evidence = _evidence("request.reservation", value)
    body = evidence["body"]
    _keys(
        "request.reservation.body",
        body,
        {
            "task_id",
            "attempt_id",
            "target_id",
            "resource_key",
            "policy_revision",
            "mapping_digest",
            "policy_digest",
            "currentness_digest",
            "selector_digest",
            "gate_digest",
            "authoritative_reset_at",
            "authoritative_wake_at",
            "expires_at",
        },
    )
    expected = {
        "task_id": scope["task_id"],
        "attempt_id": scope["attempt_id"],
        "target_id": scope["target_id"],
        "resource_key": resource["canonical_key"],
        "policy_revision": revisions["policy_revision"],
        "mapping_digest": stable_digest(mapping),
        "policy_digest": stable_digest(policy),
        "currentness_digest": currentness["evidence_digest"],
        "selector_digest": selector["activation_report_digest"],
        "gate_digest": stable_digest(gates),
        "authoritative_reset_at": _gate_reset(gates, "target"),
        "authoritative_wake_at": _authoritative_wake(gates),
        "expires_at": body["expires_at"],
    }
    for field, expected_value in expected.items():
        _lit("reservation " + field, body[field], expected_value)
    for field in (
        "mapping_digest",
        "policy_digest",
        "currentness_digest",
        "selector_digest",
        "gate_digest",
    ):
        _digest("reservation " + field, body[field])
    if _at(body["expires_at"]) <= _at(replay["evaluated_at"]):
        raise CapacityReservationFeedbackSimulationError(
            "reservation earliest boundary must be future-current"
        )
    earliest = _earliest(
        body["authoritative_reset_at"],
        body["authoritative_wake_at"],
        currentness["body"]["expires_at"],
        binding["expires_at"],
        resource["expires_at"],
    )
    _lit("reservation expires_at", body["expires_at"], earliest)
    return evidence


def _gate_reset(gates: dict[str, Any], name: str) -> str:
    decision_key = gates[name]["decision_key"]
    return next(
        decision["reset_at"]
        for decision in gates["decisions"]
        if decision["decision_key"] == decision_key
    )


def _authoritative_wake(gates: dict[str, Any]) -> str:
    return _earliest(*[decision["reset_at"] for decision in gates["decisions"]])


def _feedback(
    value: object,
    scope: dict[str, Any],
    revisions: dict[str, Any],
    currentness: dict[str, Any],
    resource: dict[str, Any],
    gates: dict[str, Any],
    events: list[dict[str, Any]],
    replay: dict[str, str],
) -> dict[str, Any]:
    feedback = _obj("request.feedback", value)
    _keys(
        "request.feedback",
        feedback,
        {"body", "evidence_digest", "recovery_evidence"},
    )
    evidence = _evidence(
        "request.feedback",
        {"body": feedback["body"], "evidence_digest": feedback["evidence_digest"]},
    )
    body = evidence["body"]
    _keys(
        "request.feedback.body",
        body,
        {
            "event_id",
            "task_id",
            "attempt_id",
            "target_id",
            "resource_key",
            "outcome",
            "predecessor_event_id",
            "observed_at",
        },
    )
    for field in ("event_id", "task_id", "attempt_id", "target_id", "resource_key"):
        _id("feedback." + field, body[field])
    if body["outcome"] not in OUTCOMES:
        raise CapacityReservationFeedbackSimulationError("feedback outcome invalid")
    expected_predecessor = events[-1]["body"]["event_id"] if events else None
    feedback_observed_at = _at(body["observed_at"])
    predecessor_observed_at = _at(events[-1]["body"]["observed_at"]) if events else None
    if (
        body["task_id"] != scope["task_id"]
        or body["attempt_id"] != scope["attempt_id"]
        or body["target_id"] != scope["target_id"]
        or body["resource_key"] != resource["canonical_key"]
        or body["predecessor_event_id"] != expected_predecessor
        or feedback_observed_at > _at(replay["evaluated_at"])
        or (
            predecessor_observed_at is not None
            and feedback_observed_at <= predecessor_observed_at
        )
    ):
        raise CapacityReservationFeedbackSimulationError(
            "feedback exact binding or lineage mismatch"
        )
    predecessor_ids = {event["body"]["event_id"] for event in events}
    predecessor_digests = {event["evidence_digest"] for event in events}
    if (
        body["event_id"] in predecessor_ids
        or evidence["evidence_digest"] in predecessor_digests
    ):
        raise CapacityReservationFeedbackSimulationError(
            "feedback may not reuse predecessor ids or digests"
        )
    recovery_raw = feedback["recovery_evidence"]
    if body["outcome"] == "recovery":
        recovery = _recovery(
            recovery_raw,
            body,
            scope,
            revisions,
            currentness,
            resource,
            gates,
            replay,
        )
        if recovery["evidence_digest"] in predecessor_digests | {
            evidence["evidence_digest"]
        }:
            raise CapacityReservationFeedbackSimulationError(
                "recovery evidence digest must be unique"
            )
    elif recovery_raw is not None:
        raise CapacityReservationFeedbackSimulationError(
            "non-recovery feedback cannot include recovery evidence"
        )
    else:
        recovery = None
    return {
        "body": body,
        "evidence_digest": evidence["evidence_digest"],
        "recovery_evidence": recovery,
    }


def _recovery(
    value: object,
    feedback_body: dict[str, Any],
    scope: dict[str, Any],
    revisions: dict[str, Any],
    currentness: dict[str, Any],
    resource: dict[str, Any],
    gates: dict[str, Any],
    replay: dict[str, str],
) -> dict[str, Any]:
    evidence = _evidence("request.feedback.recovery_evidence", value)
    body = evidence["body"]
    _keys(
        "request.feedback.recovery_evidence.body",
        body,
        {
            "observed_at",
            "expires_at",
            "currentness_revision",
            "currentness_digest",
            "target_id",
            "resource_key",
            "scope",
            "global_decision_key",
            "global_wake_key",
            "target_decision_key",
            "target_wake_key",
            "predecessor_event_id",
            "identity_authority",
        },
    )
    expected = {
        "observed_at": feedback_body["observed_at"],
        "expires_at": body["expires_at"],
        "currentness_revision": revisions["currentness_revision"],
        "currentness_digest": currentness["evidence_digest"],
        "target_id": scope["target_id"],
        "resource_key": resource["canonical_key"],
        "scope": _source_scope(scope),
        "global_decision_key": gates["global"]["decision_key"],
        "global_wake_key": gates["global"]["wake_key"],
        "target_decision_key": gates["target"]["decision_key"],
        "target_wake_key": gates["target"]["wake_key"],
        "predecessor_event_id": feedback_body["predecessor_event_id"],
        "identity_authority": "source_attested",
    }
    if body != expected:
        raise CapacityReservationFeedbackSimulationError(
            "recovery evidence exact binding mismatch"
        )
    _digest("recovery currentness digest", body["currentness_digest"])
    observed = _at(body["observed_at"])
    evaluated = _at(replay["evaluated_at"])
    expires = _at(body["expires_at"])
    current_observed = _at(currentness["body"]["observed_at"])
    current_expires = _at(currentness["body"]["expires_at"])
    if not current_observed <= observed <= evaluated < expires <= current_expires:
        raise CapacityReservationFeedbackSimulationError(
            "recovery evidence is stale or outside currentness window"
        )
    return evidence


def _retry(
    value: object,
    scope: dict[str, Any],
    revisions: dict[str, Any],
    selector: dict[str, Any],
) -> dict[str, Any]:
    evidence = _evidence("request.retry_budget", value)
    body = evidence["body"]
    _keys(
        "request.retry_budget.body",
        body,
        {
            "task_id",
            "attempt_id",
            "resume_target_id",
            "resume_revision",
            "retry_policy_revision",
            "remaining",
            "automatic_retries",
            "cooldown_inactive",
            "dependencies_satisfied",
            "resume_stop_inactive",
            "operator_stop_inactive",
            "task_attempt_boundary_preserved",
            "provider_quota_bound",
            "task_attempt_limit_bound",
        },
    )
    for field in (
        "cooldown_inactive",
        "dependencies_satisfied",
        "resume_stop_inactive",
        "operator_stop_inactive",
        "task_attempt_boundary_preserved",
        "provider_quota_bound",
        "task_attempt_limit_bound",
    ):
        _bool("retry." + field, body[field])
    _int("retry.remaining", body["remaining"], minimum=0)
    _int_literal("retry.automatic_retries", body["automatic_retries"], 0)
    expected = {
        "task_id": scope["task_id"],
        "attempt_id": scope["attempt_id"],
        "resume_target_id": selector["resume_target_id"],
        "resume_revision": revisions["resume_revision"],
        "retry_policy_revision": revisions["retry_policy_revision"],
    }
    for field, expected_value in expected.items():
        _lit("retry " + field, body[field], expected_value)
    _lit("retry provider quota separation", body["provider_quota_bound"], False)
    _lit("retry task attempt separation", body["task_attempt_limit_bound"], False)
    return evidence


def _retry_safe(body: dict[str, Any]) -> bool:
    return all(
        body[field]
        for field in (
            "cooldown_inactive",
            "dependencies_satisfied",
            "resume_stop_inactive",
            "operator_stop_inactive",
            "task_attempt_boundary_preserved",
        )
    )


def _evidence(name: str, value: object) -> dict[str, Any]:
    evidence = _obj(name, value)
    _keys(name, evidence, {"body", "evidence_digest"})
    body = _obj(name + ".body", evidence["body"])
    claimed = _digest(name + ".evidence_digest", evidence["evidence_digest"])
    _lit(name + ".evidence_digest", claimed, stable_digest(body))
    return {"body": deepcopy(body), "evidence_digest": claimed}


def _source_scope(scope: dict[str, Any]) -> dict[str, str]:
    return {
        "project_id": scope["project_id"],
        "repository_id": scope["repository_id"],
        "task_class": scope["task_class"],
        "opt_in_scope_id": scope["opt_in_scope_id"],
    }


def _earliest(*values: str) -> str:
    if not values:
        raise CapacityReservationFeedbackSimulationError(
            "earliest boundary requires candidates"
        )
    return min(values, key=_at)


def _obj(name: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapacityReservationFeedbackSimulationError(name + " must be an object")
    return value


def _list(name: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise CapacityReservationFeedbackSimulationError(name + " must be a list")
    return value


def _keys(name: str, value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise CapacityReservationFeedbackSimulationError(
            name + " has unknown, missing, or malformed fields"
        )


def _lit(name: str, value: object, expected: object) -> None:
    if type(value) is not type(expected) or value != expected:
        raise CapacityReservationFeedbackSimulationError(name + " mismatch")


def _int_literal(name: str, value: object, expected: int) -> None:
    if type(value) is not int or value != expected:
        raise CapacityReservationFeedbackSimulationError(name + " mismatch")


def _int(name: str, value: object, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise CapacityReservationFeedbackSimulationError(name + " invalid")
    return value


def _bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise CapacityReservationFeedbackSimulationError(name + " must be boolean")
    return value


def _id(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or any(character not in SAFE for character in value)
    ):
        raise CapacityReservationFeedbackSimulationError(
            name + " must be a safe identifier"
        )
    return value


def _digest(name: str, value: object) -> str:
    digest = _id(name, value)
    if (
        not digest.startswith("sha256:")
        or len(digest) != 71
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise CapacityReservationFeedbackSimulationError(
            name + " must be an exact lowercase sha256 digest"
        )
    return digest


def _validate_digest_fields(name: str, value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child = name + "." + str(key)
            if str(key).endswith("_digest"):
                _digest(child, item)
            else:
                _validate_digest_fields(child, item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_digest_fields(f"{name}[{index}]", item)


def _type_exact_equal(value: object, expected: object) -> bool:
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(
            _type_exact_equal(value[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _type_exact_equal(item, expected_item)
            for item, expected_item in zip(value, expected, strict=True)
        )
    return value == expected


def _at(value: object) -> datetime:
    if not isinstance(value, str):
        raise CapacityReservationFeedbackSimulationError("timestamp invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapacityReservationFeedbackSimulationError("timestamp invalid") from exc
    if parsed.tzinfo is None:
        raise CapacityReservationFeedbackSimulationError("timestamp timezone required")
    return parsed

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any

from .provider_capacity_shadow import (
    CapacityShadowValidationError,
    evaluate_capacity_shadow,
    validate_shadow_evaluation_request,
)


REQUEST_CONTRACT = "capacity-target-ordering-activation-simulation-request-v1"
REPORT_CONTRACT = "capacity-target-ordering-activation-simulation-v1"
SIMULATION_POLICY_REVISION = "capacity-target-ordering-simulation-policy-v1"
ROLLBACK_RULE_ID = "keep-immutable-baseline-on-any-ineligible-input-v1"
DECISIONS = {
    "keep_baseline",
    "would_select_alternative",
    "fail_closed",
}
GLOBAL_GATE_STATES = {"pass", "fail", "unknown"}
MUTATION_FIELDS = (
    "queue_mutations",
    "config_mutations",
    "reservation_mutations",
    "cooldown_mutations",
    "wake_mutations",
    "defer_mutations",
    "hard_exclusion_mutations",
    "retry_mutations",
    "selection_mutations",
    "dispatch_mutations",
    "routing_mutations",
)
REVISION_FIELDS = (
    "requirement_revision",
    "inventory_snapshot_id",
    "selector_policy_revision",
    "mapping_revision",
    "authority_revision",
    "capacity_bundle_revision",
    "currentness_digest",
    "simulation_policy_revision",
)
SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-"
)


class CapacityTargetOrderingSimulationError(ValueError):
    pass


def validate_capacity_target_ordering_simulation_request(
    value: object,
) -> dict[str, Any]:
    request = _object("request", value)
    _exact_keys(
        "request",
        request,
        {
            "schema_version",
            "contract",
            "evaluated_at",
            "scope",
            "revisions",
            "global_gate",
            "resume_target_id",
            "baseline_binding",
            "shadow_binding",
            "rollback_rule",
            "shadow_request",
            "shadow_report",
        },
    )
    _literal("request.schema_version", request.get("schema_version"), 1)
    _literal("request.contract", request.get("contract"), REQUEST_CONTRACT)
    scope = _scope(request.get("scope"))
    global_gate = _global_gate(request.get("global_gate"))
    rollback_rule = _rollback_rule(request.get("rollback_rule"))
    raw_shadow_request = request.get("shadow_request")
    try:
        expected_shadow_report = evaluate_capacity_shadow(raw_shadow_request)
        validated_shadow_request = validate_shadow_evaluation_request(
            raw_shadow_request
        )
    except CapacityShadowValidationError as exc:
        raise CapacityTargetOrderingSimulationError(
            "request.shadow_request is invalid"
        ) from exc
    if request.get("shadow_report") != expected_shadow_report:
        raise CapacityTargetOrderingSimulationError(
            "request.shadow_report must exactly match deterministic shadow evaluation"
        )
    shadow_report = deepcopy(expected_shadow_report)
    evaluated_at = request.get("evaluated_at")
    if evaluated_at != validated_shadow_request["evaluated_at"]:
        raise CapacityTargetOrderingSimulationError(
            "request.evaluated_at must match shadow evaluation time"
        )

    revisions = _revisions(request.get("revisions"))
    expected_revisions = {
        "requirement_revision": validated_shadow_request["revisions"][
            "requirement_revision"
        ],
        "inventory_snapshot_id": validated_shadow_request["revisions"][
            "inventory_snapshot_id"
        ],
        "selector_policy_revision": validated_shadow_request["revisions"][
            "selector_policy_revision"
        ],
        "mapping_revision": validated_shadow_request["revisions"]["mapping_revision"],
        "authority_revision": validated_shadow_request["revisions"][
            "authority_revision"
        ],
        "capacity_bundle_revision": validated_shadow_request["revisions"][
            "capacity_bundle_revision"
        ],
        "currentness_digest": stable_digest(raw_shadow_request["revision_currentness"]),
        "simulation_policy_revision": SIMULATION_POLICY_REVISION,
    }
    if revisions != expected_revisions:
        raise CapacityTargetOrderingSimulationError(
            "request.revisions must exact-bind the shadow request"
        )

    baseline_binding = _baseline_binding(request.get("baseline_binding"))
    baseline = shadow_report["baseline"]
    expected_baseline_binding = {
        "decision_digest": baseline["decision_digest"],
        "selected_target_id": baseline["selected_target_id"],
        "ordered_eligible_target_ids": shadow_report["preeligible_target_ids"],
    }
    if baseline_binding != expected_baseline_binding:
        raise CapacityTargetOrderingSimulationError(
            "request.baseline_binding must exact-bind immutable shadow baseline"
        )

    shadow_binding = _shadow_binding(request.get("shadow_binding"))
    expected_shadow_binding = {
        "request_digest": stable_digest(raw_shadow_request),
        "report_digest": shadow_report["report_hash"],
    }
    if shadow_binding != expected_shadow_binding:
        raise CapacityTargetOrderingSimulationError(
            "request.shadow_binding must exact-bind immutable shadow artifacts"
        )

    resume_target_id = request.get("resume_target_id")
    if resume_target_id is not None:
        resume_target_id = _safe_id("request.resume_target_id", resume_target_id)

    return {
        "schema_version": 1,
        "contract": REQUEST_CONTRACT,
        "evaluated_at": evaluated_at,
        "scope": scope,
        "revisions": revisions,
        "global_gate": global_gate,
        "resume_target_id": resume_target_id,
        "baseline_binding": baseline_binding,
        "shadow_binding": shadow_binding,
        "rollback_rule": rollback_rule,
        "shadow_request": deepcopy(raw_shadow_request),
        "shadow_report": shadow_report,
    }


def simulate_capacity_target_ordering_activation(
    value: object,
) -> dict[str, Any]:
    request = validate_capacity_target_ordering_simulation_request(value)
    return validate_capacity_target_ordering_simulation_report(
        _build_simulation_report(request)
    )


def _build_simulation_report(request: dict[str, Any]) -> dict[str, Any]:
    shadow_report = request["shadow_report"]
    baseline = deepcopy(shadow_report["baseline"])
    baseline_order = deepcopy(
        request["baseline_binding"]["ordered_eligible_target_ids"]
    )
    baseline_target = baseline["selected_target_id"]
    recommendation = shadow_report["shadow_recommendation"]
    reasons: list[str] = []
    decision = "fail_closed"
    counterfactual_target = baseline_target
    counterfactual_order = deepcopy(baseline_order)

    failed_gates = [
        gate for gate, status in request["global_gate"].items() if status != "pass"
    ]
    if failed_gates:
        reasons.extend(
            f"global_{gate}_{request['global_gate'][gate]}" for gate in failed_gates
        )
    elif request["resume_target_id"] is not None:
        if request["resume_target_id"] != baseline_target:
            reasons.append("resume_target_baseline_mismatch")
        else:
            decision = "keep_baseline"
            reasons.append("resume_target_pinned")
    elif recommendation["status"] != "capacity_aware_shadow":
        reasons.extend(f"shadow_{reason}" for reason in recommendation["reason_codes"])
    else:
        recommended = recommendation["recommended_target_id"]
        if recommended not in baseline_order:
            reasons.append("shadow_target_not_baseline_eligible")
        elif recommended == baseline_target:
            decision = "keep_baseline"
            reasons.append("capacity_order_matches_baseline")
        else:
            decision = "would_select_alternative"
            counterfactual_target = recommended
            counterfactual_order = [
                recommended,
                *[target for target in baseline_order if target != recommended],
            ]
            reasons.append("capacity_reorders_already_eligible_targets")

    if decision == "fail_closed" and not reasons:
        reasons.append("simulation_input_ineligible")

    body: dict[str, Any] = {
        "schema_version": 1,
        "contract": REPORT_CONTRACT,
        "evaluated_at": request["evaluated_at"],
        "scope": deepcopy(request["scope"]),
        "revisions": deepcopy(request["revisions"]),
        "decision": decision,
        "reason_codes": sorted(set(reasons)),
        "baseline": baseline,
        "baseline_order": baseline_order,
        "counterfactual_target_id": counterfactual_target,
        "counterfactual_order": counterfactual_order,
        "resume_target_id": request["resume_target_id"],
        "shadow_binding": deepcopy(request["shadow_binding"]),
        "rollback_rule": deepcopy(request["rollback_rule"]),
        "input_digest": stable_digest(request),
        "simulation_request": deepcopy(request),
        "simulation_only": True,
        "activation_authority": False,
        "live_routing": False,
        "default_routing": False,
        "automatic_substitution": False,
        "selection_or_dispatch_authority": False,
        "worker_promotion": False,
        "provider_promotion": False,
        "actual_canary": False,
        "synthetic_evidence_authority": False,
        **{field: [] for field in MUTATION_FIELDS},
    }
    body["simulation_digest"] = stable_digest(body)
    return body


def validate_capacity_target_ordering_simulation_report(
    value: object,
) -> dict[str, Any]:
    report = _object("report", value)
    expected = {
        "schema_version",
        "contract",
        "evaluated_at",
        "scope",
        "revisions",
        "decision",
        "reason_codes",
        "baseline",
        "baseline_order",
        "counterfactual_target_id",
        "counterfactual_order",
        "resume_target_id",
        "shadow_binding",
        "rollback_rule",
        "input_digest",
        "simulation_request",
        "simulation_only",
        "activation_authority",
        "live_routing",
        "default_routing",
        "automatic_substitution",
        "selection_or_dispatch_authority",
        "worker_promotion",
        "provider_promotion",
        "actual_canary",
        "synthetic_evidence_authority",
        *MUTATION_FIELDS,
        "simulation_digest",
    }
    _exact_keys("report", report, expected)
    _literal("report.schema_version", report.get("schema_version"), 1)
    _literal("report.contract", report.get("contract"), REPORT_CONTRACT)
    _timestamp("report.evaluated_at", report.get("evaluated_at"))
    _scope(report.get("scope"))
    _revisions(report.get("revisions"))
    _shadow_binding(report.get("shadow_binding"))
    _rollback_rule(report.get("rollback_rule"))
    simulation_request = validate_capacity_target_ordering_simulation_request(
        report.get("simulation_request")
    )
    decision = report.get("decision")
    if decision not in DECISIONS:
        raise CapacityTargetOrderingSimulationError("report.decision is invalid")
    reason_codes = report.get("reason_codes")
    if (
        not isinstance(reason_codes, list)
        or not reason_codes
        or reason_codes != sorted(set(reason_codes))
    ):
        raise CapacityTargetOrderingSimulationError(
            "report.reason_codes must be a sorted unique non-empty list"
        )
    for index, reason in enumerate(reason_codes):
        _safe_id(f"report.reason_codes[{index}]", reason)
    baseline = _report_baseline(report.get("baseline"))
    baseline_order = _safe_id_list(
        "report.baseline_order", report.get("baseline_order")
    )
    counterfactual_order = _safe_id_list(
        "report.counterfactual_order",
        report.get("counterfactual_order"),
    )
    if set(counterfactual_order) != set(baseline_order):
        raise CapacityTargetOrderingSimulationError(
            "counterfactual order may only reorder baseline eligible targets"
        )
    baseline_target = _safe_id(
        "report.baseline.selected_target_id",
        baseline.get("selected_target_id"),
    )
    counterfactual_target = _safe_id(
        "report.counterfactual_target_id",
        report.get("counterfactual_target_id"),
    )
    if (
        baseline_target != baseline_order[0]
        or baseline["selector_order"] != baseline_order
        or counterfactual_target != counterfactual_order[0]
    ):
        raise CapacityTargetOrderingSimulationError(
            "selected targets must be first in their exact orders"
        )
    if decision != "would_select_alternative" and (
        counterfactual_target != baseline_target
        or counterfactual_order != baseline_order
    ):
        raise CapacityTargetOrderingSimulationError(
            "non-alternative decisions must preserve baseline target and order"
        )
    if decision == "would_select_alternative" and (
        counterfactual_target == baseline_target
        or counterfactual_order == baseline_order
    ):
        raise CapacityTargetOrderingSimulationError(
            "alternative decision must be a non-trivial eligible reordering"
        )
    if decision == "would_select_alternative" and counterfactual_order != [
        counterfactual_target,
        *[target for target in baseline_order if target != counterfactual_target],
    ]:
        raise CapacityTargetOrderingSimulationError(
            "alternative decision may only move one eligible target to the front"
        )
    for field in (
        "simulation_only",
        "activation_authority",
        "live_routing",
        "default_routing",
        "automatic_substitution",
        "selection_or_dispatch_authority",
        "worker_promotion",
        "provider_promotion",
        "actual_canary",
        "synthetic_evidence_authority",
    ):
        expected_value = field == "simulation_only"
        _literal(f"report.{field}", report.get(field), expected_value)
    for field in MUTATION_FIELDS:
        _literal(f"report.{field}", report.get(field), [])
    for field in ("input_digest", "simulation_digest"):
        _digest_value(f"report.{field}", report.get(field))
    resume_target_id = report.get("resume_target_id")
    if resume_target_id is not None:
        _safe_id("report.resume_target_id", resume_target_id)
    claimed = report["simulation_digest"]
    body = deepcopy(report)
    body.pop("simulation_digest")
    _literal("report.simulation_digest", claimed, stable_digest(body))
    expected = _build_simulation_report(simulation_request)
    if report != expected:
        raise CapacityTargetOrderingSimulationError(
            "report must exactly match deterministic simulation request replay"
        )
    return deepcopy(report)


def stable_digest(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scope(value: object) -> dict[str, Any]:
    scope = _object("scope", value)
    _exact_keys(
        "scope",
        scope,
        {
            "task_class",
            "project_id",
            "repository_id",
            "opt_in_scope_id",
            "opted_in",
        },
    )
    _literal("scope.opted_in", scope.get("opted_in"), True)
    return {
        "task_class": _safe_id("scope.task_class", scope.get("task_class")),
        "project_id": _safe_id("scope.project_id", scope.get("project_id")),
        "repository_id": _safe_id("scope.repository_id", scope.get("repository_id")),
        "opt_in_scope_id": _safe_id(
            "scope.opt_in_scope_id", scope.get("opt_in_scope_id")
        ),
        "opted_in": True,
    }


def _revisions(value: object) -> dict[str, str]:
    revisions = _object("revisions", value)
    _exact_keys("revisions", revisions, set(REVISION_FIELDS))
    result = {
        field: _safe_id(f"revisions.{field}", revisions.get(field))
        for field in REVISION_FIELDS
    }
    _literal(
        "revisions.simulation_policy_revision",
        result["simulation_policy_revision"],
        SIMULATION_POLICY_REVISION,
    )
    _digest_value("revisions.currentness_digest", result["currentness_digest"])
    return result


def _global_gate(value: object) -> dict[str, str]:
    gate = _object("global_gate", value)
    expected = {
        "hard_constraints",
        "exact_target_eligibility",
        "quality_floor",
    }
    _exact_keys("global_gate", gate, expected)
    result: dict[str, str] = {}
    for field in sorted(expected):
        state = gate.get(field)
        if state not in GLOBAL_GATE_STATES:
            raise CapacityTargetOrderingSimulationError(
                f"global_gate.{field} is invalid"
            )
        result[field] = state
    return result


def _baseline_binding(value: object) -> dict[str, Any]:
    binding = _object("baseline_binding", value)
    _exact_keys(
        "baseline_binding",
        binding,
        {
            "decision_digest",
            "selected_target_id",
            "ordered_eligible_target_ids",
        },
    )
    return {
        "decision_digest": _digest_value(
            "baseline_binding.decision_digest",
            binding.get("decision_digest"),
        ),
        "selected_target_id": _safe_id(
            "baseline_binding.selected_target_id",
            binding.get("selected_target_id"),
        ),
        "ordered_eligible_target_ids": _safe_id_list(
            "baseline_binding.ordered_eligible_target_ids",
            binding.get("ordered_eligible_target_ids"),
        ),
    }


def _shadow_binding(value: object) -> dict[str, str]:
    binding = _object("shadow_binding", value)
    _exact_keys("shadow_binding", binding, {"request_digest", "report_digest"})
    return {
        "request_digest": _digest_value(
            "shadow_binding.request_digest",
            binding.get("request_digest"),
        ),
        "report_digest": _digest_value(
            "shadow_binding.report_digest",
            binding.get("report_digest"),
        ),
    }


def _rollback_rule(value: object) -> dict[str, Any]:
    rule = _object("rollback_rule", value)
    expected = {
        "rule_id",
        "on_any_ineligible_input",
        "baseline_source",
        "mutation_allowed",
    }
    _exact_keys("rollback_rule", rule, expected)
    _literal("rollback_rule.rule_id", rule.get("rule_id"), ROLLBACK_RULE_ID)
    _literal(
        "rollback_rule.on_any_ineligible_input",
        rule.get("on_any_ineligible_input"),
        "keep_baseline",
    )
    _literal(
        "rollback_rule.baseline_source",
        rule.get("baseline_source"),
        "immutable_shadow_baseline",
    )
    _literal(
        "rollback_rule.mutation_allowed",
        rule.get("mutation_allowed"),
        False,
    )
    return {
        "rule_id": ROLLBACK_RULE_ID,
        "on_any_ineligible_input": "keep_baseline",
        "baseline_source": "immutable_shadow_baseline",
        "mutation_allowed": False,
    }


def _report_baseline(value: object) -> dict[str, Any]:
    baseline = _object("report.baseline", value)
    _exact_keys(
        "report.baseline",
        baseline,
        {
            "decision",
            "decision_digest",
            "selected_target_id",
            "selector_order",
        },
    )
    decision = baseline.get("decision")
    if not isinstance(decision, dict) or not decision:
        raise CapacityTargetOrderingSimulationError(
            "report.baseline.decision must be a non-empty object"
        )
    digest = _digest_value(
        "report.baseline.decision_digest",
        baseline.get("decision_digest"),
    )
    if digest != stable_digest(decision):
        raise CapacityTargetOrderingSimulationError(
            "report baseline decision digest is invalid"
        )
    selected = _safe_id(
        "report.baseline.selected_target_id",
        baseline.get("selected_target_id"),
    )
    order = _safe_id_list(
        "report.baseline.selector_order",
        baseline.get("selector_order"),
    )
    if (
        selected != order[0]
        or decision.get("selected_target_id") != selected
        or decision.get("ranked_target_ids") != order
    ):
        raise CapacityTargetOrderingSimulationError(
            "report baseline decision and order are inconsistent"
        )
    return deepcopy(baseline)


def _object(key: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapacityTargetOrderingSimulationError(f"{key} must be an object")
    return value


def _exact_keys(key: str, value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise CapacityTargetOrderingSimulationError(
            f"{key} must contain exactly: {', '.join(sorted(expected))}"
        )


def _literal(key: str, value: object, expected: object) -> None:
    if type(value) is not type(expected) or value != expected:
        raise CapacityTargetOrderingSimulationError(f"{key} must be {expected!r}")


def _safe_id(key: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or any(character not in SAFE_ID_CHARS for character in value)
    ):
        raise CapacityTargetOrderingSimulationError(
            f"{key} must be a public-safe identifier"
        )
    return value


def _safe_id_list(key: str, value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise CapacityTargetOrderingSimulationError(f"{key} must be a non-empty list")
    values = [_safe_id(f"{key}[{index}]", item) for index, item in enumerate(value)]
    if len(values) != len(set(values)):
        raise CapacityTargetOrderingSimulationError(f"{key} values must be unique")
    return values


def _digest_value(key: str, value: object) -> str:
    digest = _safe_id(key, value)
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise CapacityTargetOrderingSimulationError(f"{key} must be a sha256 digest")
    return digest


def _timestamp(key: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise CapacityTargetOrderingSimulationError(f"{key} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapacityTargetOrderingSimulationError(
            f"{key} must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CapacityTargetOrderingSimulationError(f"{key} must include a timezone")
    return parsed

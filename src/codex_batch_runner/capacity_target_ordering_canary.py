from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .capacity_target_ordering_simulation import (
    CapacityTargetOrderingSimulationError,
    validate_capacity_target_ordering_simulation_report,
)


POLICY_CONTRACT = "capacity-target-ordering-canary-policy-v1"
REQUEST_CONTRACT = "capacity-target-ordering-canary-request-v1"
DECISION_CONTRACT = "capacity-target-ordering-canary-decision-v1"
OUTCOME_CONTRACT = "capacity-target-ordering-canary-outcome-v1"
POLICY_REVISION = "capacity-target-ordering-canary-policy-v1"
DEFAULT_ASSIGNMENT_PERCENT = 5
HARD_CEILING_PERCENT = 10
DECISIONS = {"keep_baseline", "apply_canary", "stop_new_canary"}
OUTCOMES = {"success", "failure", "unknown"}
SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-"
)


class CapacityTargetOrderingCanaryError(ValueError):
    pass


def default_capacity_target_ordering_canary_policy() -> dict[str, Any]:
    """Return the fail-closed policy used when config omits the canary."""
    return {
        "schema_version": 1,
        "contract": POLICY_CONTRACT,
        "revision": POLICY_REVISION,
        "enabled": False,
        "assignment_percent": DEFAULT_ASSIGNMENT_PERCENT,
        "hard_ceiling_percent": HARD_CEILING_PERCENT,
        "kill_switch_active": True,
        "max_evidence_age_seconds": 300,
        "allowed_scopes": [],
    }


def capacity_target_ordering_canary_policy_value(
    value: object,
) -> dict[str, Any]:
    if value in (None, "", {}):
        return default_capacity_target_ordering_canary_policy()
    policy = _object("capacity_target_ordering_canary_policy", value)
    _exact_keys(
        "capacity_target_ordering_canary_policy",
        policy,
        {
            "schema_version",
            "contract",
            "revision",
            "enabled",
            "assignment_percent",
            "hard_ceiling_percent",
            "kill_switch_active",
            "max_evidence_age_seconds",
            "allowed_scopes",
        },
    )
    _literal("policy.schema_version", policy.get("schema_version"), 1)
    _literal("policy.contract", policy.get("contract"), POLICY_CONTRACT)
    _literal("policy.revision", policy.get("revision"), POLICY_REVISION)
    enabled = _boolean("policy.enabled", policy.get("enabled"))
    assignment = _percentage(
        "policy.assignment_percent",
        policy.get("assignment_percent"),
    )
    ceiling = _percentage(
        "policy.hard_ceiling_percent",
        policy.get("hard_ceiling_percent"),
    )
    _literal("policy.hard_ceiling_percent", ceiling, HARD_CEILING_PERCENT)
    if assignment > ceiling:
        raise CapacityTargetOrderingCanaryError(
            "canary assignment percent exceeds the hard ceiling"
        )
    if enabled and assignment != DEFAULT_ASSIGNMENT_PERCENT:
        raise CapacityTargetOrderingCanaryError(
            "initial enabled canary assignment must be exactly 5 percent"
        )
    kill_switch = _boolean(
        "policy.kill_switch_active",
        policy.get("kill_switch_active"),
    )
    max_age = _positive_int(
        "policy.max_evidence_age_seconds",
        policy.get("max_evidence_age_seconds"),
    )
    scopes = [
        _scope(item, key=f"policy.allowed_scopes[{index}]")
        for index, item in enumerate(
            _list("policy.allowed_scopes", policy.get("allowed_scopes"))
        )
    ]
    if len({_scope_key(scope) for scope in scopes}) != len(scopes):
        raise CapacityTargetOrderingCanaryError(
            "policy.allowed_scopes must be unique"
        )
    if enabled and not scopes:
        raise CapacityTargetOrderingCanaryError(
            "enabled canary policy requires an explicit allowed scope"
        )
    return {
        "schema_version": 1,
        "contract": POLICY_CONTRACT,
        "revision": POLICY_REVISION,
        "enabled": enabled,
        "assignment_percent": assignment,
        "hard_ceiling_percent": ceiling,
        "kill_switch_active": kill_switch,
        "max_evidence_age_seconds": max_age,
        "allowed_scopes": scopes,
    }


def apply_capacity_target_ordering_canary(
    *,
    policy: object,
    task: dict[str, Any],
    requirement: dict[str, Any],
    assessment: dict[str, Any],
    dispatch_evaluated_at: str,
) -> str:
    """Return the exact dispatch target and append one sanitized claim decision.

    This is a runner-owned claim-boundary operation, not a selector hook. The
    selector remains read-only and consumes only the validated decision.
    Malformed input preserves the immutable baseline and does not become
    trusted evidence.
    """
    baseline_order = _eligible_order(assessment)
    if not baseline_order:
        raise CapacityTargetOrderingCanaryError(
            "canary requires a non-empty already-eligible baseline order"
        )
    baseline_target = baseline_order[0]
    if task.get("routing_override"):
        return baseline_target
    request_value = task.get("capacity_target_ordering_canary_request")
    if request_value is None:
        return baseline_target
    validated_policy = capacity_target_ordering_canary_policy_value(policy)
    try:
        request = validate_capacity_target_ordering_canary_request(
            request_value,
            policy=validated_policy,
            task=task,
            requirement=requirement,
            assessment=assessment,
            dispatch_evaluated_at=dispatch_evaluated_at,
        )
    except CapacityTargetOrderingCanaryError:
        task["capacity_target_ordering_canary_status"] = "fail_closed"
        task["capacity_target_ordering_canary_reason"] = (
            "invalid_or_mismatched_canary_request"
        )
        return baseline_target

    decision, reasons = _decision(
        validated_policy,
        task,
        request,
        baseline_order,
    )
    canary_order = request["activation_report"]["counterfactual_order"]
    canary_target = canary_order[0]
    dispatch_target = (
        canary_target if decision == "apply_canary" else baseline_target
    )
    record = _build_decision_record(
        policy=validated_policy,
        task=task,
        requirement=requirement,
        assessment=assessment,
        request=request,
        baseline_order=baseline_order,
        canary_order=canary_order,
        decision=decision,
        reasons=reasons,
        dispatch_target=dispatch_target,
    )
    attach_capacity_target_ordering_canary_decision(task, record)
    task["capacity_target_ordering_canary_status"] = decision
    task["capacity_target_ordering_canary_reason"] = reasons[0]
    return dispatch_target


def selected_capacity_target_ordering_canary_target(
    *,
    task: dict[str, Any],
    requirement: dict[str, Any],
    assessment: dict[str, Any],
) -> str | None:
    """Read a current exact-bound claim decision without mutating task state."""
    history = task.get("capacity_target_ordering_canary_decision_history")
    if not isinstance(history, list) or not history:
        return None
    expected_attempt = int(task.get("attempts") or 0) + 1
    matching = []
    for item in history:
        decision = validate_capacity_target_ordering_canary_decision(item)
        binding = decision["binding"]
        if binding["attempt"] != expected_attempt:
            continue
        matching.append(decision)
    if len(matching) != 1:
        if matching:
            raise CapacityTargetOrderingCanaryError(
                "canary claim contains divergent decisions for one attempt"
            )
        return None
    decision = matching[0]
    baseline_order = _eligible_order(assessment)
    binding = decision["binding"]
    if (
        binding["task_id"] != str(task.get("id"))
        or binding["requirement_revision"] != requirement.get("revision_id")
        or binding["inventory_snapshot_id"]
        != assessment.get("inventory_snapshot_id")
        or binding["selector_policy_revision"]
        != assessment.get("selection_policy_version")
        or decision["baseline"]["order"] != baseline_order
        or decision["baseline"]["target_id"] != baseline_order[0]
    ):
        raise CapacityTargetOrderingCanaryError(
            "canary claim decision drifted from dispatch-time selector inputs"
        )
    if decision["decision"] != "apply_canary":
        return None
    target_id = decision["dispatch"]["target_id"]
    if target_id not in baseline_order:
        raise CapacityTargetOrderingCanaryError(
            "canary dispatch target is no longer eligible"
        )
    return target_id


def validate_capacity_target_ordering_canary_request(
    value: object,
    *,
    policy: dict[str, Any],
    task: dict[str, Any],
    requirement: dict[str, Any],
    assessment: dict[str, Any],
    dispatch_evaluated_at: str,
) -> dict[str, Any]:
    request = _object("canary_request", value)
    _exact_keys(
        "canary_request",
        request,
        {
            "schema_version",
            "contract",
            "scope",
            "policy_revision",
            "task_id",
            "evidence_revision",
            "activation_report",
            "activation_report_digest",
        },
    )
    _literal("canary_request.schema_version", request.get("schema_version"), 1)
    _literal("canary_request.contract", request.get("contract"), REQUEST_CONTRACT)
    scope = _scope(request.get("scope"), key="canary_request.scope")
    _literal(
        "canary_request.policy_revision",
        request.get("policy_revision"),
        policy["revision"],
    )
    task_id = _safe_id("canary_request.task_id", request.get("task_id"))
    if task_id != _safe_id("task.id", task.get("id")):
        raise CapacityTargetOrderingCanaryError(
            "canary request task id does not match destination task"
        )
    _validate_task_scope(task, scope)
    assignment_identity = _safe_id(
        "task.capacity_target_ordering_assignment_id",
        task.get("capacity_target_ordering_assignment_id"),
    )
    if _scope_key(scope) not in {
        _scope_key(item) for item in policy["allowed_scopes"]
    }:
        raise CapacityTargetOrderingCanaryError(
            "canary request scope is not explicitly allowed"
        )
    evidence_revision = _safe_id(
        "canary_request.evidence_revision",
        request.get("evidence_revision"),
    )
    dispatch_at = _timestamp(
        "runner.dispatch_evaluated_at",
        dispatch_evaluated_at,
    )
    try:
        report = validate_capacity_target_ordering_simulation_report(
            request.get("activation_report")
        )
    except CapacityTargetOrderingSimulationError as exc:
        raise CapacityTargetOrderingCanaryError(
            "canary activation report is invalid"
        ) from exc
    _literal(
        "canary_request.activation_report_digest",
        request.get("activation_report_digest"),
        stable_digest(report),
    )
    _validate_report_binding(
        report,
        scope=scope,
        requirement=requirement,
        assessment=assessment,
        dispatch_at=dispatch_at,
        max_age_seconds=policy["max_evidence_age_seconds"],
    )
    return {
        "schema_version": 1,
        "contract": REQUEST_CONTRACT,
        "scope": scope,
        "policy_revision": policy["revision"],
        "task_id": task_id,
        "assignment_identity": assignment_identity,
        "dispatch_evaluated_at": dispatch_at.isoformat(),
        "evidence_revision": evidence_revision,
        "activation_report": deepcopy(report),
        "activation_report_digest": stable_digest(report),
    }


def validate_capacity_target_ordering_canary_decision(
    value: object,
) -> dict[str, Any]:
    record = _object("canary_decision", value)
    _exact_keys(
        "canary_decision",
        record,
        {
            "schema_version",
            "contract",
            "recorded_at",
            "binding",
            "assignment",
            "baseline",
            "canary",
            "dispatch",
            "decision",
            "reason_codes",
            "rollback_reason",
            "activation_binding",
            "default_routing",
            "global_activation",
            "provider_priority_mutation",
            "queue_mutation",
            "promotion_authority",
            "decision_id",
        },
    )
    _literal("canary_decision.schema_version", record.get("schema_version"), 1)
    _literal("canary_decision.contract", record.get("contract"), DECISION_CONTRACT)
    _timestamp("canary_decision.recorded_at", record.get("recorded_at"))
    binding = _decision_binding(record.get("binding"))
    assignment = _assignment(record.get("assignment"))
    baseline = _order_binding("canary_decision.baseline", record.get("baseline"))
    canary = _order_binding("canary_decision.canary", record.get("canary"))
    dispatch = _object("canary_decision.dispatch", record.get("dispatch"))
    _exact_keys(
        "canary_decision.dispatch",
        dispatch,
        {"target_id", "exact_bound"},
    )
    dispatch_target = _safe_id(
        "canary_decision.dispatch.target_id",
        dispatch.get("target_id"),
    )
    _literal("canary_decision.dispatch.exact_bound", dispatch.get("exact_bound"), True)
    if set(canary["order"]) != set(baseline["order"]):
        raise CapacityTargetOrderingCanaryError(
            "canary order must contain exactly the baseline eligible targets"
        )
    decision = record.get("decision")
    if decision not in DECISIONS:
        raise CapacityTargetOrderingCanaryError("canary decision is invalid")
    reasons = _reason_codes(record.get("reason_codes"))
    rollback_reason = record.get("rollback_reason")
    if rollback_reason is not None:
        rollback_reason = _safe_id(
            "canary_decision.rollback_reason",
            rollback_reason,
        )
    if decision == "apply_canary":
        if (
            not assignment["assigned"]
            or dispatch_target != canary["target_id"]
            or canary["order"] == baseline["order"]
        ):
            raise CapacityTargetOrderingCanaryError(
                "apply_canary must dispatch an assigned non-baseline ordering"
            )
    elif dispatch_target != baseline["target_id"]:
        raise CapacityTargetOrderingCanaryError(
            "non-canary decisions must preserve the baseline dispatch target"
        )
    activation = _object(
        "canary_decision.activation_binding",
        record.get("activation_binding"),
    )
    _exact_keys(
        "canary_decision.activation_binding",
        activation,
        {"report_digest", "evidence_revision"},
    )
    _digest("activation_binding.report_digest", activation.get("report_digest"))
    _safe_id(
        "activation_binding.evidence_revision",
        activation.get("evidence_revision"),
    )
    for field in (
        "default_routing",
        "global_activation",
        "provider_priority_mutation",
        "queue_mutation",
        "promotion_authority",
    ):
        _literal(f"canary_decision.{field}", record.get(field), False)
    canonical = {
        "schema_version": 1,
        "contract": DECISION_CONTRACT,
        "recorded_at": record["recorded_at"],
        "binding": binding,
        "assignment": assignment,
        "baseline": baseline,
        "canary": canary,
        "dispatch": {"target_id": dispatch_target, "exact_bound": True},
        "decision": decision,
        "reason_codes": reasons,
        "rollback_reason": rollback_reason,
        "activation_binding": {
            "report_digest": activation["report_digest"],
            "evidence_revision": activation["evidence_revision"],
        },
        "default_routing": False,
        "global_activation": False,
        "provider_priority_mutation": False,
        "queue_mutation": False,
        "promotion_authority": False,
    }
    _literal(
        "canary_decision.decision_id",
        record.get("decision_id"),
        stable_digest(canonical),
    )
    canonical["decision_id"] = record["decision_id"]
    return canonical


def attach_capacity_target_ordering_canary_decision(
    task: dict[str, Any],
    record: dict[str, Any],
) -> None:
    validated = validate_capacity_target_ordering_canary_decision(record)
    if validated["binding"]["task_id"] != str(task.get("id")):
        raise CapacityTargetOrderingCanaryError(
            "canary decision does not bind destination task"
        )
    history = task.setdefault("capacity_target_ordering_canary_decision_history", [])
    if not isinstance(history, list):
        raise CapacityTargetOrderingCanaryError(
            "canary decision history must be a list"
        )
    existing = [
        item
        for item in history
        if isinstance(item, dict)
        and item.get("decision_id") == validated["decision_id"]
    ]
    if existing and any(item != validated for item in existing):
        raise CapacityTargetOrderingCanaryError(
            "conflicting canary decision duplicate"
        )
    if not existing:
        history.append(validated)


def record_capacity_target_ordering_canary_outcome(
    task: dict[str, Any],
    *,
    recorded_at: str,
) -> dict[str, Any] | None:
    decisions = task.get("capacity_target_ordering_canary_decision_history")
    if not isinstance(decisions, list) or not decisions:
        return None
    attempt = int(task.get("attempts") or 0)
    matching = [
        validate_capacity_target_ordering_canary_decision(item)
        for item in decisions
        if isinstance(item, dict)
        and isinstance(item.get("binding"), dict)
        and item["binding"].get("attempt") == attempt
    ]
    if not matching and task.get("status") == "failed":
        attempt += 1
        matching = [
            validate_capacity_target_ordering_canary_decision(item)
            for item in decisions
            if isinstance(item, dict)
            and isinstance(item.get("binding"), dict)
            and item["binding"].get("attempt") == attempt
        ]
    if not matching:
        return None
    decision = matching[-1]
    history = task.setdefault("capacity_target_ordering_canary_outcome_history", [])
    if not isinstance(history, list):
        raise CapacityTargetOrderingCanaryError(
            "canary outcome history must be a list"
        )
    if any(
        isinstance(item, dict)
        and item.get("binding", {}).get("decision_id") == decision["decision_id"]
        for item in history
    ):
        return None
    status = str(task.get("status") or "")
    if status == "completed":
        outcome = "success"
        adverse = False
        rollback_applied = False
        rollback_reason = None
    elif status in {"failed", "needs_resume", "blocked"}:
        outcome = "failure" if status == "failed" else "unknown"
        adverse = True
        rollback_applied = True
        rollback_reason = (
            "execution_failed" if status == "failed" else "execution_incomplete"
        )
    else:
        outcome = "unknown"
        adverse = True
        rollback_applied = True
        rollback_reason = "terminal_status_unknown"
    body = {
        "schema_version": 1,
        "contract": OUTCOME_CONTRACT,
        "recorded_at": _timestamp("outcome.recorded_at", recorded_at).isoformat(),
        "binding": {
            "task_id": decision["binding"]["task_id"],
            "attempt": decision["binding"]["attempt"],
            "decision_id": decision["decision_id"],
            "dispatch_target_id": decision["dispatch"]["target_id"],
        },
        "outcome": outcome,
        "adverse": adverse,
        "rollback_applied": rollback_applied,
        "rollback_reason": rollback_reason,
        "baseline_order": deepcopy(decision["baseline"]["order"]),
        "canary_order": deepcopy(decision["canary"]["order"]),
        "default_routing": False,
        "global_activation": False,
        "promotion_authority": False,
    }
    body["outcome_id"] = stable_digest(body)
    validated = validate_capacity_target_ordering_canary_outcome(body)
    history.append(validated)
    return validated


def validate_capacity_target_ordering_canary_outcome(
    value: object,
) -> dict[str, Any]:
    record = _object("canary_outcome", value)
    _exact_keys(
        "canary_outcome",
        record,
        {
            "schema_version",
            "contract",
            "recorded_at",
            "binding",
            "outcome",
            "adverse",
            "rollback_applied",
            "rollback_reason",
            "baseline_order",
            "canary_order",
            "default_routing",
            "global_activation",
            "promotion_authority",
            "outcome_id",
        },
    )
    _literal("canary_outcome.schema_version", record.get("schema_version"), 1)
    _literal("canary_outcome.contract", record.get("contract"), OUTCOME_CONTRACT)
    recorded_at = _timestamp("canary_outcome.recorded_at", record.get("recorded_at"))
    binding = _object("canary_outcome.binding", record.get("binding"))
    _exact_keys(
        "canary_outcome.binding",
        binding,
        {"task_id", "attempt", "decision_id", "dispatch_target_id"},
    )
    canonical_binding = {
        "task_id": _safe_id("outcome.binding.task_id", binding.get("task_id")),
        "attempt": _positive_int("outcome.binding.attempt", binding.get("attempt")),
        "decision_id": _digest(
            "outcome.binding.decision_id",
            binding.get("decision_id"),
        ),
        "dispatch_target_id": _safe_id(
            "outcome.binding.dispatch_target_id",
            binding.get("dispatch_target_id"),
        ),
    }
    outcome = record.get("outcome")
    if outcome not in OUTCOMES:
        raise CapacityTargetOrderingCanaryError("canary outcome is invalid")
    adverse = _boolean("canary_outcome.adverse", record.get("adverse"))
    rollback_applied = _boolean(
        "canary_outcome.rollback_applied",
        record.get("rollback_applied"),
    )
    rollback_reason = record.get("rollback_reason")
    if rollback_reason is not None:
        rollback_reason = _safe_id(
            "canary_outcome.rollback_reason",
            rollback_reason,
        )
    if adverse and (not rollback_applied or rollback_reason is None):
        raise CapacityTargetOrderingCanaryError(
            "adverse canary outcome must exact-bind deterministic rollback"
        )
    if not adverse and (rollback_applied or rollback_reason is not None):
        raise CapacityTargetOrderingCanaryError(
            "successful canary outcome must not claim rollback"
        )
    baseline_order = _safe_id_list(
        "canary_outcome.baseline_order",
        record.get("baseline_order"),
    )
    canary_order = _safe_id_list(
        "canary_outcome.canary_order",
        record.get("canary_order"),
    )
    if set(baseline_order) != set(canary_order):
        raise CapacityTargetOrderingCanaryError(
            "outcome orders must contain the same exact targets"
        )
    for field in ("default_routing", "global_activation", "promotion_authority"):
        _literal(f"canary_outcome.{field}", record.get(field), False)
    canonical = {
        "schema_version": 1,
        "contract": OUTCOME_CONTRACT,
        "recorded_at": recorded_at.isoformat(),
        "binding": canonical_binding,
        "outcome": outcome,
        "adverse": adverse,
        "rollback_applied": rollback_applied,
        "rollback_reason": rollback_reason,
        "baseline_order": baseline_order,
        "canary_order": canary_order,
        "default_routing": False,
        "global_activation": False,
        "promotion_authority": False,
    }
    _literal(
        "canary_outcome.outcome_id",
        record.get("outcome_id"),
        stable_digest(canonical),
    )
    canonical["outcome_id"] = record["outcome_id"]
    return canonical


def reconstruct_capacity_target_ordering_canary(
    task: dict[str, Any],
) -> dict[str, Any]:
    decisions = [
        validate_capacity_target_ordering_canary_decision(item)
        for item in task.get("capacity_target_ordering_canary_decision_history", [])
    ]
    outcomes = [
        validate_capacity_target_ordering_canary_outcome(item)
        for item in task.get("capacity_target_ordering_canary_outcome_history", [])
    ]
    decision_ids = {item["decision_id"] for item in decisions}
    if len(decision_ids) != len(decisions):
        raise CapacityTargetOrderingCanaryError(
            "canary decision history contains duplicates"
        )
    if any(item["binding"]["decision_id"] not in decision_ids for item in outcomes):
        raise CapacityTargetOrderingCanaryError(
            "canary outcome references an unknown decision"
        )
    if len(
        {item["binding"]["decision_id"] for item in outcomes}
    ) != len(outcomes):
        raise CapacityTargetOrderingCanaryError(
            "canary outcome history contains duplicate closure"
        )
    unresolved = [
        item["decision_id"]
        for item in decisions
        if item["decision"] == "apply_canary"
        and not any(
            outcome["binding"]["decision_id"] == item["decision_id"]
            for outcome in outcomes
        )
    ]
    adverse = [
        item["outcome_id"] for item in outcomes if item["adverse"]
    ]
    return {
        "decision_count": len(decisions),
        "outcome_count": len(outcomes),
        "unresolved_decision_ids": unresolved,
        "adverse_outcome_ids": adverse,
        "stop_new_canary": bool(unresolved or adverse),
        "baseline_reconstruction_only": bool(unresolved or adverse),
    }


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
        raise CapacityTargetOrderingCanaryError(
            "value is not stable-digest serializable"
        ) from exc
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _decision(
    policy: dict[str, Any],
    task: dict[str, Any],
    request: dict[str, Any],
    baseline_order: list[str],
) -> tuple[str, list[str]]:
    if not policy["enabled"]:
        return "stop_new_canary", ["canary_policy_disabled"]
    if policy["kill_switch_active"]:
        return "stop_new_canary", ["canary_kill_switch_active"]
    reconstructed = reconstruct_capacity_target_ordering_canary(task)
    if reconstructed["stop_new_canary"]:
        return "stop_new_canary", ["canary_prior_closure_requires_rollback"]
    if task.get("status") == "needs_resume" or task.get("resume_requested"):
        return "keep_baseline", ["resume_target_pinned_before_capacity"]
    report = request["activation_report"]
    if report["decision"] == "fail_closed":
        return "stop_new_canary", ["activation_report_fail_closed"]
    bucket = _assignment_bucket(request, policy)
    if bucket >= policy["assignment_percent"] * 100:
        return "keep_baseline", ["task_outside_deterministic_canary_cohort"]
    if report["decision"] != "would_select_alternative":
        return "keep_baseline", ["capacity_order_matches_baseline"]
    if report["counterfactual_order"] == baseline_order:
        return "keep_baseline", ["capacity_order_matches_baseline"]
    return "apply_canary", ["deterministic_scoped_canary_assignment"]


def _build_decision_record(
    *,
    policy: dict[str, Any],
    task: dict[str, Any],
    requirement: dict[str, Any],
    assessment: dict[str, Any],
    request: dict[str, Any],
    baseline_order: list[str],
    canary_order: list[str],
    decision: str,
    reasons: list[str],
    dispatch_target: str,
) -> dict[str, Any]:
    assignment_bucket = _assignment_bucket(request, policy)
    rollback_reason = reasons[0] if decision == "stop_new_canary" else None
    body = {
        "schema_version": 1,
        "contract": DECISION_CONTRACT,
        "recorded_at": request["dispatch_evaluated_at"],
        "binding": {
            "task_id": request["task_id"],
            "attempt": int(task.get("attempts") or 0) + 1,
            "scope": deepcopy(request["scope"]),
            "requirement_revision": _safe_id(
                "requirement.revision_id",
                requirement.get("revision_id"),
            ),
            "inventory_snapshot_id": _safe_id(
                "assessment.inventory_snapshot_id",
                assessment.get("inventory_snapshot_id"),
            ),
            "selector_policy_revision": _safe_id(
                "assessment.selection_policy_version",
                assessment.get("selection_policy_version"),
            ),
            "evidence_revision": request["evidence_revision"],
        },
        "assignment": {
            "key": stable_digest(request["assignment_identity"]),
            "bucket_basis_points": assignment_bucket,
            "percentage": policy["assignment_percent"],
            "hard_ceiling_percent": policy["hard_ceiling_percent"],
            "assigned": assignment_bucket < policy["assignment_percent"] * 100,
        },
        "baseline": {
            "target_id": baseline_order[0],
            "order": deepcopy(baseline_order),
        },
        "canary": {
            "target_id": canary_order[0],
            "order": deepcopy(canary_order),
        },
        "dispatch": {
            "target_id": dispatch_target,
            "exact_bound": True,
        },
        "decision": decision,
        "reason_codes": sorted(set(reasons)),
        "rollback_reason": rollback_reason,
        "activation_binding": {
            "report_digest": request["activation_report_digest"],
            "evidence_revision": request["evidence_revision"],
        },
        "default_routing": False,
        "global_activation": False,
        "provider_priority_mutation": False,
        "queue_mutation": False,
        "promotion_authority": False,
    }
    body["decision_id"] = stable_digest(body)
    return validate_capacity_target_ordering_canary_decision(body)


def _validate_report_binding(
    report: dict[str, Any],
    *,
    scope: dict[str, str],
    requirement: dict[str, Any],
    assessment: dict[str, Any],
    dispatch_at: datetime,
    max_age_seconds: int,
) -> None:
    report_scope = report["scope"]
    for field in ("project_id", "repository_id", "task_class"):
        if report_scope[field] != scope[field]:
            raise CapacityTargetOrderingCanaryError(
                "activation report scope does not match canary scope"
            )
    baseline_order = _eligible_order(assessment)
    if (
        report["baseline_order"] != baseline_order
        or report["baseline"]["selected_target_id"] != baseline_order[0]
    ):
        raise CapacityTargetOrderingCanaryError(
            "activation report does not bind immutable selector baseline"
        )
    if set(report["counterfactual_order"]) != set(baseline_order):
        raise CapacityTargetOrderingCanaryError(
            "activation report injects a non-baseline target"
        )
    revisions = report["revisions"]
    if (
        revisions["requirement_revision"] != requirement.get("revision_id")
        or revisions["inventory_snapshot_id"]
        != assessment.get("inventory_snapshot_id")
        or revisions["selector_policy_revision"]
        != assessment.get("selection_policy_version")
    ):
        raise CapacityTargetOrderingCanaryError(
            "activation report selector revisions drifted"
        )
    report_at = _timestamp("activation_report.evaluated_at", report["evaluated_at"])
    age = (dispatch_at - report_at).total_seconds()
    if age < 0 or age > max_age_seconds:
        raise CapacityTargetOrderingCanaryError(
            "activation report is outside dispatch freshness window"
        )
    simulation_request = report["simulation_request"]
    shadow_request = simulation_request["shadow_request"]
    shadow_report = simulation_request["shadow_report"]
    currentness = shadow_report["revision_currentness"]
    if not currentness["all_current"]:
        raise CapacityTargetOrderingCanaryError(
            "activation report revision currentness is invalid"
        )
    mapping = shadow_request["provider_resource_mapping"]
    if mapping["status"] != "current":
        raise CapacityTargetOrderingCanaryError(
            "activation report mapping is not current"
        )
    for binding in mapping["bindings"]:
        verified = _timestamp("mapping.binding.verified_at", binding["verified_at"])
        expires = _timestamp("mapping.binding.expires_at", binding["expires_at"])
        if not verified <= dispatch_at < expires:
            raise CapacityTargetOrderingCanaryError(
                "source-attested mapping is not current at dispatch"
            )


def _validate_task_scope(task: dict[str, Any], scope: dict[str, str]) -> None:
    project_root = str(task.get("project_root") or "")
    expected = {
        "project_id": task.get("project_id"),
        "repository_id": project_root.rstrip("/").rsplit("/", 1)[-1],
        "task_class": task.get("category"),
    }
    if expected != scope:
        raise CapacityTargetOrderingCanaryError(
            "canary scope does not exact-bind task project/repository/class"
        )


def _assignment_bucket(
    request: dict[str, Any],
    policy: dict[str, Any],
) -> int:
    digest = hashlib.sha256(
        json.dumps(
            {
                "assignment_identity": request["assignment_identity"],
                "policy_revision": policy["revision"],
                "scope": request["scope"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def _eligible_order(assessment: dict[str, Any]) -> list[str]:
    order = assessment.get("ranked_eligible_target_ids")
    return _safe_id_list("assessment.ranked_eligible_target_ids", order)


def _scope(value: object, *, key: str) -> dict[str, str]:
    scope = _object(key, value)
    _exact_keys(key, scope, {"project_id", "repository_id", "task_class"})
    return {
        field: _safe_id(f"{key}.{field}", scope.get(field))
        for field in ("project_id", "repository_id", "task_class")
    }


def _scope_key(scope: dict[str, str]) -> tuple[str, str, str]:
    return (
        scope["project_id"],
        scope["repository_id"],
        scope["task_class"],
    )


def _decision_binding(value: object) -> dict[str, Any]:
    binding = _object("canary_decision.binding", value)
    _exact_keys(
        "canary_decision.binding",
        binding,
        {
            "task_id",
            "attempt",
            "scope",
            "requirement_revision",
            "inventory_snapshot_id",
            "selector_policy_revision",
            "evidence_revision",
        },
    )
    return {
        "task_id": _safe_id("binding.task_id", binding.get("task_id")),
        "attempt": _positive_int("binding.attempt", binding.get("attempt")),
        "scope": _scope(binding.get("scope"), key="binding.scope"),
        "requirement_revision": _safe_id(
            "binding.requirement_revision",
            binding.get("requirement_revision"),
        ),
        "inventory_snapshot_id": _safe_id(
            "binding.inventory_snapshot_id",
            binding.get("inventory_snapshot_id"),
        ),
        "selector_policy_revision": _safe_id(
            "binding.selector_policy_revision",
            binding.get("selector_policy_revision"),
        ),
        "evidence_revision": _safe_id(
            "binding.evidence_revision",
            binding.get("evidence_revision"),
        ),
    }


def _assignment(value: object) -> dict[str, Any]:
    assignment = _object("canary_decision.assignment", value)
    _exact_keys(
        "canary_decision.assignment",
        assignment,
        {
            "key",
            "bucket_basis_points",
            "percentage",
            "hard_ceiling_percent",
            "assigned",
        },
    )
    bucket = assignment.get("bucket_basis_points")
    if (
        isinstance(bucket, bool)
        or not isinstance(bucket, int)
        or not 0 <= bucket < 10_000
    ):
        raise CapacityTargetOrderingCanaryError(
            "assignment bucket must be an integer from 0 to 9999"
        )
    percentage = _percentage("assignment.percentage", assignment.get("percentage"))
    ceiling = _percentage(
        "assignment.hard_ceiling_percent",
        assignment.get("hard_ceiling_percent"),
    )
    _literal("assignment.hard_ceiling_percent", ceiling, HARD_CEILING_PERCENT)
    if percentage > ceiling:
        raise CapacityTargetOrderingCanaryError(
            "assignment percentage exceeds hard ceiling"
        )
    assigned = _boolean("assignment.assigned", assignment.get("assigned"))
    if assigned != (bucket < percentage * 100):
        raise CapacityTargetOrderingCanaryError(
            "assignment flag does not match deterministic bucket"
        )
    return {
        "key": _safe_id("assignment.key", assignment.get("key")),
        "bucket_basis_points": bucket,
        "percentage": percentage,
        "hard_ceiling_percent": ceiling,
        "assigned": assigned,
    }


def _order_binding(key: str, value: object) -> dict[str, Any]:
    binding = _object(key, value)
    _exact_keys(key, binding, {"target_id", "order"})
    order = _safe_id_list(f"{key}.order", binding.get("order"))
    target = _safe_id(f"{key}.target_id", binding.get("target_id"))
    if target != order[0]:
        raise CapacityTargetOrderingCanaryError(
            f"{key}.target_id must be first in order"
        )
    return {"target_id": target, "order": order}


def _reason_codes(value: object) -> list[str]:
    reasons = _safe_id_list("reason_codes", value)
    if reasons != sorted(set(reasons)):
        raise CapacityTargetOrderingCanaryError(
            "reason codes must be sorted and unique"
        )
    return reasons


def _object(key: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapacityTargetOrderingCanaryError(f"{key} must be an object")
    return value


def _list(key: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise CapacityTargetOrderingCanaryError(f"{key} must be a list")
    return value


def _exact_keys(key: str, value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise CapacityTargetOrderingCanaryError(
            f"{key} fields are not canonical"
        )


def _literal(key: str, value: object, expected: object) -> None:
    if isinstance(expected, bool):
        if type(value) is not bool or value is not expected:
            raise CapacityTargetOrderingCanaryError(
                f"{key} must be {expected!r}"
            )
    elif value != expected or type(value) is not type(expected):
        raise CapacityTargetOrderingCanaryError(f"{key} must be {expected!r}")


def _safe_id(key: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(char not in SAFE_ID_CHARS for char in value)
    ):
        raise CapacityTargetOrderingCanaryError(
            f"{key} must be a public-safe identifier"
        )
    return value


def _safe_id_list(key: str, value: object) -> list[str]:
    items = _list(key, value)
    if not items:
        raise CapacityTargetOrderingCanaryError(f"{key} must not be empty")
    result = [_safe_id(f"{key}[{index}]", item) for index, item in enumerate(items)]
    if len(set(result)) != len(result):
        raise CapacityTargetOrderingCanaryError(f"{key} must be unique")
    return result


def _boolean(key: str, value: object) -> bool:
    if type(value) is not bool:
        raise CapacityTargetOrderingCanaryError(f"{key} must be a boolean")
    return value


def _percentage(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise CapacityTargetOrderingCanaryError(
            f"{key} must be an integer percentage"
        )
    return value


def _positive_int(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CapacityTargetOrderingCanaryError(
            f"{key} must be a positive integer"
        )
    return value


def _timestamp(key: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise CapacityTargetOrderingCanaryError(f"{key} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapacityTargetOrderingCanaryError(
            f"{key} must be a timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CapacityTargetOrderingCanaryError(
            f"{key} must include timezone"
        )
    return parsed.astimezone(timezone.utc)


def _digest(key: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
    ):
        raise CapacityTargetOrderingCanaryError(f"{key} must be a sha256 digest")
    return value

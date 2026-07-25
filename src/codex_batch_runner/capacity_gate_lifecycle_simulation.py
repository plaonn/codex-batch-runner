from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .capacity_target_ordering_simulation import (
    CapacityTargetOrderingSimulationError,
    validate_capacity_target_ordering_simulation_report,
)
from .provider_resource_authority import (
    deduplicate_gate_decisions,
    resource_gate_key,
    validate_admission_policy,
    validate_gate_decision,
    validate_gate_state,
    validate_mapping_v2,
)
from .provider_resource_report import (
    ProviderResourceValidationError,
    parse_resource_timestamp,
)


REQUEST_CONTRACT = "capacity-gate-lifecycle-activation-simulation-request-v1"
REPORT_CONTRACT = "capacity-gate-lifecycle-activation-simulation-v1"
LIFECYCLE_POLICY_REVISION = "capacity-gate-lifecycle-simulation-policy-v1"
ROLLBACK_RULE_ID = "preserve-baseline-and-stop-new-typed-evaluation-v1"
PREVIEWS = {
    "no_change",
    "would_defer",
    "covered_by_global",
    "would_supersede_gate",
    "would_revalidate_wake",
    "would_release",
    "would_hard_exclude",
    "fail_closed",
}
EVENT_TYPES = {"decision", "wake_revalidation"}
EVIDENCE_KINDS = {
    "threshold",
    "recovery",
    "confirmed_exhaustion",
}
CURRENTNESS_STATES = {
    "current",
    "stale",
    "unknown",
    "missing",
    "ambiguous",
}
GLOBAL_GATE_STATES = {"allowed", "gated", "unknown"}
MUTATION_FIELDS = (
    "queue_mutations",
    "config_mutations",
    "cooldown_mutations",
    "wake_mutations",
    "defer_mutations",
    "hard_exclusion_mutations",
    "selection_mutations",
    "dispatch_mutations",
    "routing_mutations",
    "reservation_mutations",
    "retry_mutations",
)
SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-"
)


class CapacityGateLifecycleSimulationError(ValueError):
    pass


def validate_capacity_gate_lifecycle_simulation_request(
    value: object,
) -> dict[str, Any]:
    request = _object("request", value)
    _exact_keys(
        "request",
        request,
        {
            "schema_version",
            "contract",
            "scope",
            "baseline",
            "mapping",
            "admission_policy",
            "revisions",
            "currentness",
            "selector_binding",
            "global_gate_observations",
            "replay",
            "rollback_rule",
            "events",
        },
    )
    _literal("request.schema_version", request.get("schema_version"), 1)
    _literal("request.contract", request.get("contract"), REQUEST_CONTRACT)
    scope = _scope(request.get("scope"))
    baseline = _baseline(request.get("baseline"))

    raw_mapping = request.get("mapping")
    raw_policy = request.get("admission_policy")
    try:
        mapping = validate_mapping_v2(raw_mapping)
        policy = validate_admission_policy(raw_policy)
    except ProviderResourceValidationError as exc:
        raise CapacityGateLifecycleSimulationError(
            "request mapping or admission policy is invalid"
        ) from exc
    revisions = _revisions(request.get("revisions"))
    expected_revisions = {
        "mapping_revision": mapping["mapping_revision"],
        "admission_policy_revision": policy["policy_revision"],
        "currentness_revision": _safe_id(
            "request.currentness.revision",
            _object("request.currentness", request.get("currentness")).get("revision"),
        ),
        "lifecycle_policy_revision": LIFECYCLE_POLICY_REVISION,
    }
    if revisions != expected_revisions:
        raise CapacityGateLifecycleSimulationError(
            "request.revisions must exact-bind mapping, policy, and currentness"
        )
    currentness = _currentness(request.get("currentness"), revisions)
    selector_binding = _selector_binding(request.get("selector_binding"), scope)
    observations = _global_gate_observations(request.get("global_gate_observations"))
    replay = _replay(request.get("replay"), policy)
    rollback_rule = _rollback_rule(request.get("rollback_rule"), replay)
    events = _events(request.get("events"))

    _validate_baseline_bindings(baseline)
    _validate_event_duplicates(events)

    return {
        "schema_version": 1,
        "contract": REQUEST_CONTRACT,
        "scope": scope,
        "baseline": baseline,
        "mapping": deepcopy(mapping),
        "admission_policy": deepcopy(policy),
        "revisions": revisions,
        "currentness": currentness,
        "selector_binding": selector_binding,
        "global_gate_observations": observations,
        "replay": replay,
        "rollback_rule": rollback_rule,
        "events": events,
    }


def simulate_capacity_gate_lifecycle_activation(
    value: object,
) -> dict[str, Any]:
    request = validate_capacity_gate_lifecycle_simulation_request(value)
    return validate_capacity_gate_lifecycle_simulation_report(_build_report(request))


def validate_capacity_gate_lifecycle_simulation_report(
    value: object,
) -> dict[str, Any]:
    report = _object("report", value)
    _exact_keys(
        "report",
        report,
        {
            "schema_version",
            "contract",
            "evaluated_at",
            "scope",
            "preview",
            "reason_codes",
            "baseline",
            "counterfactual_gate_state",
            "counterfactual_evidence_history",
            "task_disposition_preview",
            "wake_registry_preview",
            "event_results",
            "simulation_request",
            "input_digest",
            "replay_digest",
            "simulation_only",
            "activation_authority",
            "runtime_gate_mutation",
            "automatic_defer",
            "automatic_wake",
            "hard_exclusion_authority",
            "natural_evidence_authority",
            "live_routing",
            "default_routing",
            "worker_promotion",
            "provider_promotion",
            *MUTATION_FIELDS,
        },
    )
    _literal("report.schema_version", report.get("schema_version"), 1)
    _literal("report.contract", report.get("contract"), REPORT_CONTRACT)
    request = validate_capacity_gate_lifecycle_simulation_request(
        report.get("simulation_request")
    )
    if report.get("preview") not in PREVIEWS:
        raise CapacityGateLifecycleSimulationError("report.preview is invalid")
    reason_codes = report.get("reason_codes")
    if (
        not isinstance(reason_codes, list)
        or not reason_codes
        or reason_codes != sorted(set(reason_codes))
    ):
        raise CapacityGateLifecycleSimulationError(
            "report.reason_codes must be a sorted unique non-empty list"
        )
    for index, reason in enumerate(reason_codes):
        _safe_id(f"report.reason_codes[{index}]", reason)
    _timestamp("report.evaluated_at", report.get("evaluated_at"))
    _scope(report.get("scope"))
    _baseline(report.get("baseline"))
    try:
        validate_gate_state(report.get("counterfactual_gate_state"))
        for decision in _list(
            "report.counterfactual_evidence_history",
            report.get("counterfactual_evidence_history"),
        ):
            validate_gate_decision(decision)
    except ProviderResourceValidationError as exc:
        raise CapacityGateLifecycleSimulationError(
            "report counterfactual gate evidence is invalid"
        ) from exc
    _task_disposition_preview(report.get("task_disposition_preview"))
    _wake_registry_preview(report.get("wake_registry_preview"))
    _event_results(report.get("event_results"))
    _digest_value("report.input_digest", report.get("input_digest"))
    _digest_value("report.replay_digest", report.get("replay_digest"))
    for field in (
        "simulation_only",
        "activation_authority",
        "runtime_gate_mutation",
        "automatic_defer",
        "automatic_wake",
        "hard_exclusion_authority",
        "natural_evidence_authority",
        "live_routing",
        "default_routing",
        "worker_promotion",
        "provider_promotion",
    ):
        _literal(
            f"report.{field}",
            report.get(field),
            field == "simulation_only",
        )
    for field in MUTATION_FIELDS:
        _literal(f"report.{field}", report.get(field), [])
    expected = _build_report(request)
    if report != expected:
        raise CapacityGateLifecycleSimulationError(
            "report must exactly match deterministic simulation request replay"
        )
    return deepcopy(report)


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
        raise CapacityGateLifecycleSimulationError(
            "value is not stable-digest serializable"
        ) from exc
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_report(request: dict[str, Any]) -> dict[str, Any]:
    baseline = deepcopy(request["baseline"])
    gate_state = deepcopy(baseline["gate_state"])
    evidence_history = deepcopy(baseline["evidence_history"])
    event_results: list[dict[str, Any]] = []
    wake_preview = {
        "would_register": [],
        "would_replace": [],
        "would_remove": [],
    }
    disposition = {
        "baseline": "unchanged",
        "counterfactual": "unchanged",
        "applied": False,
    }
    preview = "no_change"
    reasons = ["no_lifecycle_change"]

    if not request["replay"]["typed_evaluation_enabled"]:
        reasons = ["rollback_stops_new_typed_evaluation"]
    else:
        failure = _preflight_failure(request)
        if failure is not None:
            preview = "fail_closed"
            reasons = [failure]
        else:
            for index, event in enumerate(request["events"]):
                result = _apply_event(
                    request=request,
                    event=event,
                    event_index=index,
                    gate_state=gate_state,
                    evidence_history=evidence_history,
                    wake_preview=wake_preview,
                    disposition=disposition,
                )
                event_results.append(result)
                preview = result["preview"]
                reasons = result["reason_codes"]
                if preview == "fail_closed":
                    gate_state = deepcopy(baseline["gate_state"])
                    evidence_history = deepcopy(baseline["evidence_history"])
                    wake_preview = {
                        "would_register": [],
                        "would_replace": [],
                        "would_remove": [],
                    }
                    disposition = {
                        "baseline": "unchanged",
                        "counterfactual": "unchanged",
                        "applied": False,
                    }
                    break

    body: dict[str, Any] = {
        "schema_version": 1,
        "contract": REPORT_CONTRACT,
        "evaluated_at": request["replay"]["evaluated_at"],
        "scope": deepcopy(request["scope"]),
        "preview": preview,
        "reason_codes": sorted(set(reasons)),
        "baseline": baseline,
        "counterfactual_gate_state": gate_state,
        "counterfactual_evidence_history": evidence_history,
        "task_disposition_preview": disposition,
        "wake_registry_preview": wake_preview,
        "event_results": event_results,
        "simulation_request": deepcopy(request),
        "input_digest": stable_digest(request),
        "simulation_only": True,
        "activation_authority": False,
        "runtime_gate_mutation": False,
        "automatic_defer": False,
        "automatic_wake": False,
        "hard_exclusion_authority": False,
        "natural_evidence_authority": False,
        "live_routing": False,
        "default_routing": False,
        "worker_promotion": False,
        "provider_promotion": False,
        **{field: [] for field in MUTATION_FIELDS},
    }
    body["replay_digest"] = stable_digest(body)
    return body


def _preflight_failure(request: dict[str, Any]) -> str | None:
    return _sequence_failure(request)


def _authority_failure(request: dict[str, Any]) -> str | None:
    currentness = request["currentness"]
    revisions = request["revisions"]
    if currentness["status"] != "current":
        return f"currentness_{currentness['status']}"
    replay_at = parse_resource_timestamp(request["replay"]["evaluated_at"])
    if not (
        parse_resource_timestamp(currentness["observed_at"])
        <= replay_at
        < parse_resource_timestamp(currentness["expires_at"])
    ):
        return "currentness_outside_validity_window"
    mapping = request["mapping"]
    policy = request["admission_policy"]
    if mapping["status"] != "current":
        return "mapping_not_current"
    if policy["status"] != "current" or not policy["enabled"]:
        return "admission_policy_not_active"
    if mapping["mapping_revision"] not in policy["allowed_mapping_revisions"]:
        return "mapping_revision_not_admitted"

    scope = request["scope"]
    bindings = [
        binding
        for binding in mapping["bindings"]
        if binding["status"] == "current" and binding["target_id"] == scope["target_id"]
    ]
    if not bindings:
        return "mapping_binding_missing"
    if len(bindings) != 1:
        return "mapping_binding_ambiguous"
    binding = bindings[0]
    expected_resource = {
        "provider_id": binding["provider_id"],
        "quota_identity_id": binding["quota_identity_id"],
        "scope_id": binding["observation_scope"]["scope_id"],
        "window_id": scope["resource"]["window_id"],
    }
    if scope["resource"] != expected_resource:
        return "scope_resource_binding_mismatch"
    if binding["identity_authority"] != "source_attested":
        return "identity_not_source_attested"
    if not (
        parse_resource_timestamp(binding["verified_at"])
        <= replay_at
        < parse_resource_timestamp(binding["expires_at"])
    ):
        return "mapping_binding_outside_validity_window"

    rules = [
        rule
        for rule in policy["target_rules"]
        if rule["target_id"] == scope["target_id"]
        and rule["provider_id"] == scope["resource"]["provider_id"]
    ]
    if len(rules) != 1:
        return "admission_target_rule_missing_or_ambiguous"
    windows = [
        window
        for window in rules[0]["window_rules"]
        if window["window_id"] == scope["resource"]["window_id"]
    ]
    if len(windows) != 1:
        return "admission_window_rule_missing_or_ambiguous"
    if windows[0]["remaining_unit"] != scope["remaining_unit"]:
        return "remaining_unit_mismatch"

    selector_report = request["selector_binding"]["report"]
    if selector_report["decision"] == "fail_closed":
        return "selector_report_fail_closed"
    if scope["target_id"] not in selector_report["baseline_order"]:
        return "selector_exact_target_ineligible"
    resume_target = selector_report["resume_target_id"]
    if resume_target is not None and resume_target != scope["target_id"]:
        return "resume_target_scope_mismatch"
    relevant_active = _active_gate(
        request["baseline"]["gate_state"],
        resource_gate_key(
            scope["resource"]["provider_id"],
            scope["resource"]["quota_identity_id"],
            scope["resource"]["scope_id"],
            scope["resource"]["window_id"],
        ),
    )
    if relevant_active is not None:
        decision = next(
            item
            for item in request["baseline"]["evidence_history"]
            if item["decision_key"] == relevant_active["decision_key"]
        )
        if (
            decision["mapping_revision"] != revisions["mapping_revision"]
            or decision["policy_revision"] != revisions["admission_policy_revision"]
        ):
            return "baseline_active_gate_revision_mismatch"
    return None


def _sequence_failure(request: dict[str, Any]) -> str | None:
    previous_id: str | None = None
    previous_at: datetime | None = None
    observation_ids = {
        observation["observation_id"]
        for observation in request["global_gate_observations"]
    }
    for event in request["events"]:
        observed_at = parse_resource_timestamp(event["observed_at"])
        if previous_at is not None and observed_at < previous_at:
            return "event_sequence_out_of_order"
        if event["predecessor_event_id"] != previous_id:
            return "event_predecessor_broken"
        if event["global_gate_observation_id"] not in observation_ids:
            return "global_gate_observation_missing"
        if observed_at > parse_resource_timestamp(request["replay"]["evaluated_at"]):
            return "event_after_replay_clock"
        previous_id = event["event_id"]
        previous_at = observed_at
    return None


def _apply_event(
    *,
    request: dict[str, Any],
    event: dict[str, Any],
    event_index: int,
    gate_state: dict[str, Any],
    evidence_history: list[dict[str, Any]],
    wake_preview: dict[str, list[dict[str, Any]]],
    disposition: dict[str, Any],
) -> dict[str, Any]:
    decision = event["decision"]
    observation = next(
        item
        for item in request["global_gate_observations"]
        if item["observation_id"] == event["global_gate_observation_id"]
    )
    latest_observation = _latest_global_observation(
        request["global_gate_observations"],
        event["observed_at"],
    )
    if latest_observation is None:
        return _event_result(
            event,
            event_index,
            "fail_closed",
            ["global_gate_observation_missing"],
        )
    if observation != latest_observation:
        return _event_result(
            event,
            event_index,
            "fail_closed",
            ["global_gate_observation_not_latest"],
        )
    if observation["status"] == "unknown":
        return _event_result(
            event,
            event_index,
            "fail_closed",
            ["global_gate_unknown"],
        )
    if observation["status"] == "gated":
        global_reset = parse_resource_timestamp(observation["reset_at"])
        target_reset = parse_resource_timestamp(decision["reset_at"])
        coverage = decision["global_coverage"]
        if global_reset < target_reset:
            return _event_result(
                event,
                event_index,
                "fail_closed",
                ["global_gate_terminal_noncovering_reset"],
            )
        binding_failure = _event_binding_failure(request, event)
        if binding_failure is not None:
            return _event_result(
                event,
                event_index,
                "fail_closed",
                [binding_failure],
            )
        if (
            decision["action"] == "covered_by_global"
            and coverage["status"] == "covered"
            and parse_resource_timestamp(coverage["global_reset_at"]) == global_reset
        ):
            return _event_result(
                event,
                event_index,
                "covered_by_global",
                ["covering_global_reset_no_target_wake"],
            )
        return _event_result(
            event,
            event_index,
            "fail_closed",
            ["covering_global_reset_contract_mismatch"],
        )
    if decision["action"] == "covered_by_global":
        return _event_result(
            event,
            event_index,
            "fail_closed",
            ["covered_decision_without_global_gate"],
        )

    failure = _authority_failure(request)
    if failure is not None:
        return _event_result(event, event_index, "fail_closed", [failure])
    failure = _event_failure(request, event)
    if failure is not None:
        return _event_result(event, event_index, "fail_closed", [failure])

    active = _active_gate(gate_state, decision["resource_key"])
    if event["event_type"] == "wake_revalidation":
        return _apply_wake_revalidation(
            request=request,
            event=event,
            event_index=event_index,
            active=active,
            gate_state=gate_state,
            evidence_history=evidence_history,
            wake_preview=wake_preview,
            disposition=disposition,
        )

    existing = next(
        (
            value
            for value in evidence_history
            if value["decision_key"] == decision["decision_key"]
        ),
        None,
    )
    if existing is not None:
        if existing != decision:
            return _event_result(
                event,
                event_index,
                "fail_closed",
                ["conflicting_decision_duplicate"],
            )
        return _event_result(
            event,
            event_index,
            "no_change",
            ["idempotent_decision_duplicate"],
        )

    if event["evidence"]["kind"] == "confirmed_exhaustion":
        if active is not None:
            old_reset = parse_resource_timestamp(active["reset_at"])
            new_reset = parse_resource_timestamp(decision["reset_at"])
            if new_reset <= old_reset:
                return _event_result(
                    event,
                    event_index,
                    "fail_closed",
                    ["hard_exclusion_reset_not_later"],
                )
            if decision["supersedes_decision_key"] != active["decision_key"]:
                return _event_result(
                    event,
                    event_index,
                    "fail_closed",
                    ["hard_exclusion_predecessor_mismatch"],
                )
            wake_preview["would_replace"].append(
                {
                    "resource_key": decision["resource_key"],
                    "previous_decision_key": active["decision_key"],
                    "previous_wake_key": active["wake_key"],
                    "decision_key": decision["decision_key"],
                    "wake_key": decision["wake_key"],
                }
            )
        elif decision["supersedes_decision_key"] is not None:
            return _event_result(
                event,
                event_index,
                "fail_closed",
                ["hard_exclusion_predecessor_without_active_gate"],
            )
        _append_decision(evidence_history, decision)
        _set_active_gate(gate_state, decision)
        disposition["counterfactual"] = "hard_excluded"
        return _event_result(
            event,
            event_index,
            "would_hard_exclude",
            ["synthetic_confirmed_exhaustion_mechanics_only"],
        )
    if decision["action"] == "allow":
        _append_decision(evidence_history, decision)
        return _event_result(
            event,
            event_index,
            "no_change",
            ["resource_above_threshold"],
        )
    if decision["action"] != "defer":
        return _event_result(
            event,
            event_index,
            "fail_closed",
            ["unsupported_lifecycle_decision_action"],
        )
    if active is None:
        if decision["supersedes_decision_key"] is not None:
            return _event_result(
                event,
                event_index,
                "fail_closed",
                ["new_gate_predecessor_without_active_gate"],
            )
        _append_decision(evidence_history, decision)
        _set_active_gate(gate_state, decision)
        wake_preview["would_register"].append(_wake_entry(decision))
        disposition["counterfactual"] = "deferred"
        return _event_result(
            event,
            event_index,
            "would_defer",
            ["threshold_low_resource_would_defer"],
        )
    return _supersede_active_gate(
        event=event,
        event_index=event_index,
        active=active,
        gate_state=gate_state,
        evidence_history=evidence_history,
        wake_preview=wake_preview,
        disposition=disposition,
    )


def _apply_wake_revalidation(
    *,
    request: dict[str, Any],
    event: dict[str, Any],
    event_index: int,
    active: dict[str, Any] | None,
    gate_state: dict[str, Any],
    evidence_history: list[dict[str, Any]],
    wake_preview: dict[str, list[dict[str, Any]]],
    disposition: dict[str, Any],
) -> dict[str, Any]:
    if active is None or event["revalidates_decision_key"] != active["decision_key"]:
        return _event_result(
            event,
            event_index,
            "fail_closed",
            ["wake_revalidation_active_gate_mismatch"],
        )
    decision = event["decision"]
    wake_boundary = parse_resource_timestamp(active["reset_at"]) + timedelta(
        seconds=request["replay"]["reset_grace_seconds"]
    )
    if parse_resource_timestamp(event["observed_at"]) < wake_boundary:
        return _event_result(
            event,
            event_index,
            "would_revalidate_wake",
            ["wake_before_reset_grace_remains_gated"],
        )
    if event["evidence"]["kind"] == "recovery":
        if decision["action"] != "allow":
            return _event_result(
                event,
                event_index,
                "fail_closed",
                ["recovery_requires_allow_decision"],
            )
        _append_decision(evidence_history, decision)
        gate_state["active_gates"].remove(active)
        wake_preview["would_remove"].append(
            {
                "resource_key": active["resource_key"],
                "decision_key": active["decision_key"],
                "wake_key": active["wake_key"],
            }
        )
        disposition["counterfactual"] = "released"
        return _event_result(
            event,
            event_index,
            "would_release",
            ["fresh_post_grace_recovery_would_release"],
        )
    if event["evidence"]["kind"] == "threshold":
        return _supersede_active_gate(
            event=event,
            event_index=event_index,
            active=active,
            gate_state=gate_state,
            evidence_history=evidence_history,
            wake_preview=wake_preview,
            disposition=disposition,
        )
    return _event_result(
        event,
        event_index,
        "fail_closed",
        ["wake_revalidation_evidence_kind_invalid"],
    )


def _supersede_active_gate(
    *,
    event: dict[str, Any],
    event_index: int,
    active: dict[str, Any],
    gate_state: dict[str, Any],
    evidence_history: list[dict[str, Any]],
    wake_preview: dict[str, list[dict[str, Any]]],
    disposition: dict[str, Any],
) -> dict[str, Any]:
    decision = event["decision"]
    old_reset = parse_resource_timestamp(active["reset_at"])
    new_reset = parse_resource_timestamp(decision["reset_at"])
    if new_reset <= old_reset:
        return _event_result(
            event,
            event_index,
            "fail_closed",
            ["superseding_reset_not_later"],
        )
    if decision["supersedes_decision_key"] != active["decision_key"]:
        return _event_result(
            event,
            event_index,
            "fail_closed",
            ["superseding_predecessor_mismatch"],
        )
    if decision["action"] != "defer":
        return _event_result(
            event,
            event_index,
            "fail_closed",
            ["superseding_gate_requires_defer"],
        )
    _append_decision(evidence_history, decision)
    _set_active_gate(gate_state, decision)
    wake_preview["would_replace"].append(
        {
            "resource_key": decision["resource_key"],
            "previous_decision_key": active["decision_key"],
            "previous_wake_key": active["wake_key"],
            "decision_key": decision["decision_key"],
            "wake_key": decision["wake_key"],
        }
    )
    disposition["counterfactual"] = "deferred"
    return _event_result(
        event,
        event_index,
        "would_supersede_gate",
        ["later_reset_would_supersede_active_gate"],
    )


def _event_failure(
    request: dict[str, Any],
    event: dict[str, Any],
) -> str | None:
    decision = event["decision"]
    binding_failure = _event_binding_failure(request, event)
    if binding_failure is not None:
        return binding_failure
    evidence = event["evidence"]
    if evidence["freshness"] != "current":
        return f"event_freshness_{evidence['freshness']}"
    if evidence["mapping_status"] != "exact":
        return f"event_mapping_{evidence['mapping_status']}"
    remaining = evidence["remaining"]["value"]
    threshold = evidence["threshold"]["value"]
    if evidence["kind"] in {"threshold", "confirmed_exhaustion"}:
        if remaining > threshold:
            return "low_resource_evidence_above_threshold"
        allowed_actions = (
            {"defer"}
            if evidence["kind"] == "confirmed_exhaustion"
            else {"defer", "covered_by_global"}
        )
        if decision["action"] not in allowed_actions:
            return "low_resource_evidence_requires_defer"
    elif evidence["kind"] == "recovery":
        if remaining <= threshold:
            return "recovery_not_above_threshold"
    return None


def _event_binding_failure(
    request: dict[str, Any],
    event: dict[str, Any],
) -> str | None:
    decision = event["decision"]
    scope = request["scope"]
    revisions = request["revisions"]
    if decision["resource_key"] != resource_gate_key(
        scope["resource"]["provider_id"],
        scope["resource"]["quota_identity_id"],
        scope["resource"]["scope_id"],
        scope["resource"]["window_id"],
    ):
        return "decision_resource_scope_mismatch"
    if (
        decision["policy_revision"] != revisions["admission_policy_revision"]
        or decision["mapping_revision"] != revisions["mapping_revision"]
    ):
        return "decision_revision_mismatch"
    if decision["observed_at"] != event["observed_at"]:
        return "event_decision_timestamp_mismatch"
    evidence = event["evidence"]
    if evidence["currentness_revision"] != revisions["currentness_revision"]:
        return "event_currentness_revision_mismatch"
    if (
        evidence["remaining"]["unit"] != scope["remaining_unit"]
        or evidence["threshold"]["unit"] != scope["remaining_unit"]
    ):
        return "event_remaining_unit_mismatch"
    return None


def _latest_global_observation(
    observations: list[dict[str, Any]],
    event_time: str,
) -> dict[str, Any] | None:
    event_at = parse_resource_timestamp(event_time)
    applicable = [
        observation
        for observation in observations
        if parse_resource_timestamp(observation["observed_at"]) <= event_at
    ]
    return applicable[-1] if applicable else None


def _scope(value: object) -> dict[str, Any]:
    scope = _object("scope", value)
    _exact_keys(
        "scope",
        scope,
        {
            "project_id",
            "repository_id",
            "task_class",
            "opt_in_scope_id",
            "opted_in",
            "target_id",
            "remaining_unit",
            "resource",
        },
    )
    _literal("scope.opted_in", scope.get("opted_in"), True)
    resource = _object("scope.resource", scope.get("resource"))
    _exact_keys(
        "scope.resource",
        resource,
        {
            "provider_id",
            "quota_identity_id",
            "scope_id",
            "window_id",
        },
    )
    return {
        "project_id": _safe_id("scope.project_id", scope.get("project_id")),
        "repository_id": _safe_id("scope.repository_id", scope.get("repository_id")),
        "task_class": _safe_id("scope.task_class", scope.get("task_class")),
        "opt_in_scope_id": _safe_id(
            "scope.opt_in_scope_id", scope.get("opt_in_scope_id")
        ),
        "opted_in": True,
        "target_id": _safe_id("scope.target_id", scope.get("target_id")),
        "remaining_unit": _enum(
            "scope.remaining_unit",
            scope.get("remaining_unit"),
            {"percent", "tokens", "credits", "requests"},
        ),
        "resource": {
            field: _safe_id(f"scope.resource.{field}", resource.get(field))
            for field in (
                "provider_id",
                "quota_identity_id",
                "scope_id",
                "window_id",
            )
        },
    }


def _baseline(value: object) -> dict[str, Any]:
    baseline = _object("baseline", value)
    _exact_keys(
        "baseline",
        baseline,
        {
            "gate_state",
            "gate_state_digest",
            "evidence_history",
            "evidence_history_digest",
            "legacy_scalar",
        },
    )
    try:
        gate_state = validate_gate_state(baseline.get("gate_state"))
        history = [
            validate_gate_decision(item)
            for item in _list(
                "baseline.evidence_history",
                baseline.get("evidence_history"),
            )
        ]
        deduplicate_gate_decisions(history)
    except ProviderResourceValidationError as exc:
        raise CapacityGateLifecycleSimulationError(
            "baseline gate state or evidence history is invalid"
        ) from exc
    _literal(
        "baseline.gate_state_digest",
        baseline.get("gate_state_digest"),
        stable_digest(gate_state),
    )
    _literal(
        "baseline.evidence_history_digest",
        baseline.get("evidence_history_digest"),
        stable_digest(history),
    )
    legacy = _object("baseline.legacy_scalar", baseline.get("legacy_scalar"))
    _exact_keys(
        "baseline.legacy_scalar",
        legacy,
        {"role", "target_gate_projected"},
    )
    _literal(
        "baseline.legacy_scalar.role",
        legacy.get("role"),
        "global_gate_only",
    )
    _literal(
        "baseline.legacy_scalar.target_gate_projected",
        legacy.get("target_gate_projected"),
        False,
    )
    return {
        "gate_state": deepcopy(gate_state),
        "gate_state_digest": baseline["gate_state_digest"],
        "evidence_history": deepcopy(history),
        "evidence_history_digest": baseline["evidence_history_digest"],
        "legacy_scalar": {
            "role": "global_gate_only",
            "target_gate_projected": False,
        },
    }


def _validate_baseline_bindings(baseline: dict[str, Any]) -> None:
    evidence_history = baseline["evidence_history"]
    history = {
        decision["decision_key"]: (index, decision)
        for index, decision in enumerate(evidence_history)
    }
    latest_defer_index: dict[str, int] = {}
    previous_defer: dict[str, dict[str, Any]] = {}
    for index, decision in enumerate(evidence_history):
        if decision["action"] != "defer":
            continue
        resource_key = decision["resource_key"]
        predecessor = previous_defer.get(resource_key)
        predecessor_key = decision["supersedes_decision_key"]
        if predecessor is None:
            if predecessor_key is not None:
                raise CapacityGateLifecycleSimulationError(
                    "baseline first defer must not name a predecessor"
                )
        elif predecessor_key != predecessor["decision_key"]:
            raise CapacityGateLifecycleSimulationError(
                "baseline defer must bind the immediately previous defer"
            )
        elif parse_resource_timestamp(
            predecessor["reset_at"]
        ) >= parse_resource_timestamp(decision["reset_at"]):
            raise CapacityGateLifecycleSimulationError(
                "baseline defer predecessor lineage is invalid"
            )
        latest_defer_index[resource_key] = index
        previous_defer[resource_key] = decision
    for gate in baseline["gate_state"]["active_gates"]:
        entry = history.get(gate["decision_key"])
        if entry is None:
            raise CapacityGateLifecycleSimulationError(
                "baseline active gate must bind an evidence-history decision"
            )
        index, decision = entry
        if decision["action"] != "defer":
            raise CapacityGateLifecycleSimulationError(
                "baseline active gate must bind a defer decision"
            )
        if latest_defer_index.get(decision["resource_key"]) != index:
            raise CapacityGateLifecycleSimulationError(
                "baseline active gate must bind the latest defer decision"
            )
        expected = _active_gate_from_decision(decision)
        if gate != expected:
            raise CapacityGateLifecycleSimulationError(
                "baseline active gate must exact-bind its decision"
            )


def _revisions(value: object) -> dict[str, str]:
    revisions = _object("revisions", value)
    fields = {
        "mapping_revision",
        "admission_policy_revision",
        "currentness_revision",
        "lifecycle_policy_revision",
    }
    _exact_keys("revisions", revisions, fields)
    result = {
        field: _safe_id(f"revisions.{field}", revisions.get(field))
        for field in sorted(fields)
    }
    _literal(
        "revisions.lifecycle_policy_revision",
        result["lifecycle_policy_revision"],
        LIFECYCLE_POLICY_REVISION,
    )
    return result


def _currentness(
    value: object,
    revisions: dict[str, str],
) -> dict[str, Any]:
    currentness = _object("currentness", value)
    _exact_keys(
        "currentness",
        currentness,
        {
            "revision",
            "status",
            "mapping_revision",
            "admission_policy_revision",
            "observed_at",
            "expires_at",
            "binding_digest",
        },
    )
    status = _enum(
        "currentness.status",
        currentness.get("status"),
        CURRENTNESS_STATES,
    )
    observed_at = _timestamp("currentness.observed_at", currentness.get("observed_at"))
    expires_at = _timestamp("currentness.expires_at", currentness.get("expires_at"))
    if parse_resource_timestamp(expires_at) <= parse_resource_timestamp(observed_at):
        raise CapacityGateLifecycleSimulationError(
            "currentness expiry must be after observation"
        )
    body = {
        "revision": revisions["currentness_revision"],
        "status": status,
        "mapping_revision": _safe_id(
            "currentness.mapping_revision",
            currentness.get("mapping_revision"),
        ),
        "admission_policy_revision": _safe_id(
            "currentness.admission_policy_revision",
            currentness.get("admission_policy_revision"),
        ),
        "observed_at": observed_at,
        "expires_at": expires_at,
    }
    if (
        body["mapping_revision"] != revisions["mapping_revision"]
        or body["admission_policy_revision"] != revisions["admission_policy_revision"]
    ):
        raise CapacityGateLifecycleSimulationError(
            "currentness must exact-bind request revisions"
        )
    _literal(
        "currentness.binding_digest",
        currentness.get("binding_digest"),
        stable_digest(body),
    )
    return {**body, "binding_digest": currentness["binding_digest"]}


def _selector_binding(
    value: object,
    scope: dict[str, Any],
) -> dict[str, Any]:
    binding = _object("selector_binding", value)
    _exact_keys(
        "selector_binding",
        binding,
        {
            "report",
            "report_digest",
        },
    )
    try:
        report = validate_capacity_target_ordering_simulation_report(
            binding.get("report")
        )
    except CapacityTargetOrderingSimulationError as exc:
        raise CapacityGateLifecycleSimulationError(
            "selector_binding.report is invalid"
        ) from exc
    _literal(
        "selector_binding.report_digest",
        binding.get("report_digest"),
        stable_digest(report),
    )
    report_scope = report["scope"]
    if (
        report_scope["project_id"] != scope["project_id"]
        or report_scope["repository_id"] != scope["repository_id"]
    ):
        raise CapacityGateLifecycleSimulationError(
            "selector report project and repository must exact-bind scope"
        )
    if (
        report["baseline"]["selected_target_id"] != scope["target_id"]
        or report["baseline_order"][0] != scope["target_id"]
    ):
        raise CapacityGateLifecycleSimulationError(
            "selector report must exact-bind the immutable baseline target"
        )
    return {
        "report": deepcopy(report),
        "report_digest": binding["report_digest"],
    }


def _global_gate_observations(value: object) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    ids: set[str] = set()
    previous_at: datetime | None = None
    for index, raw in enumerate(_list("global_gate_observations", value)):
        key = f"global_gate_observations[{index}]"
        observation = _object(key, raw)
        _exact_keys(
            key,
            observation,
            {
                "observation_id",
                "status",
                "observed_at",
                "reset_at",
            },
        )
        observation_id = _safe_id(
            f"{key}.observation_id", observation.get("observation_id")
        )
        if observation_id in ids:
            raise CapacityGateLifecycleSimulationError(
                "global gate observation ids must be unique"
            )
        ids.add(observation_id)
        status = _enum(
            f"{key}.status",
            observation.get("status"),
            GLOBAL_GATE_STATES,
        )
        observed_at = _timestamp(f"{key}.observed_at", observation.get("observed_at"))
        observed_dt = parse_resource_timestamp(observed_at)
        if previous_at is not None and observed_dt <= previous_at:
            raise CapacityGateLifecycleSimulationError(
                "global gate observations must be strictly ordered"
            )
        reset_at = observation.get("reset_at")
        if status == "gated":
            reset_at = _timestamp(f"{key}.reset_at", reset_at)
            if parse_resource_timestamp(reset_at) <= observed_dt:
                raise CapacityGateLifecycleSimulationError(
                    "global gate reset must follow its observation"
                )
        elif reset_at is not None:
            raise CapacityGateLifecycleSimulationError(
                "non-gated global observation must not include reset"
            )
        observations.append(
            {
                "observation_id": observation_id,
                "status": status,
                "observed_at": observed_at,
                "reset_at": reset_at,
            }
        )
        previous_at = observed_dt
    if not observations:
        raise CapacityGateLifecycleSimulationError(
            "at least one global gate observation is required"
        )
    return observations


def _replay(value: object, policy: dict[str, Any]) -> dict[str, Any]:
    replay = _object("replay", value)
    _exact_keys(
        "replay",
        replay,
        {
            "evaluated_at",
            "reset_grace_seconds",
            "typed_evaluation_enabled",
        },
    )
    reset_grace = _nonnegative_integer(
        "replay.reset_grace_seconds",
        replay.get("reset_grace_seconds"),
    )
    if reset_grace != policy["timing"]["reset_grace_seconds"]:
        raise CapacityGateLifecycleSimulationError(
            "replay reset grace must exact-bind admission policy"
        )
    return {
        "evaluated_at": _timestamp("replay.evaluated_at", replay.get("evaluated_at")),
        "reset_grace_seconds": reset_grace,
        "typed_evaluation_enabled": _boolean(
            "replay.typed_evaluation_enabled",
            replay.get("typed_evaluation_enabled"),
        ),
    }


def _rollback_rule(
    value: object,
    replay: dict[str, Any],
) -> dict[str, Any]:
    rollback = _object("rollback_rule", value)
    _exact_keys(
        "rollback_rule",
        rollback,
        {
            "rule_id",
            "disable_behavior",
            "typed_state_behavior",
            "legacy_scalar_behavior",
            "rollback_active",
        },
    )
    _literal(
        "rollback_rule.rule_id",
        rollback.get("rule_id"),
        ROLLBACK_RULE_ID,
    )
    _literal(
        "rollback_rule.disable_behavior",
        rollback.get("disable_behavior"),
        "stop_new_target_decisions",
    )
    _literal(
        "rollback_rule.typed_state_behavior",
        rollback.get("typed_state_behavior"),
        "preserve_append_only_evidence",
    )
    _literal(
        "rollback_rule.legacy_scalar_behavior",
        rollback.get("legacy_scalar_behavior"),
        "remain_global_only",
    )
    active = _boolean(
        "rollback_rule.rollback_active",
        rollback.get("rollback_active"),
    )
    if active == replay["typed_evaluation_enabled"]:
        raise CapacityGateLifecycleSimulationError(
            "rollback state and typed evaluation flag must be inverse"
        )
    return {
        "rule_id": ROLLBACK_RULE_ID,
        "disable_behavior": "stop_new_target_decisions",
        "typed_state_behavior": "preserve_append_only_evidence",
        "legacy_scalar_behavior": "remain_global_only",
        "rollback_active": active,
    }


def _events(value: object) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(_list("events", value)):
        key = f"events[{index}]"
        event = _object(key, raw)
        _exact_keys(
            key,
            event,
            {
                "event_id",
                "event_type",
                "predecessor_event_id",
                "observed_at",
                "global_gate_observation_id",
                "revalidates_decision_key",
                "decision",
                "evidence",
            },
        )
        event_id = _safe_id(f"{key}.event_id", event.get("event_id"))
        if event_id in ids:
            raise CapacityGateLifecycleSimulationError("event ids must be unique")
        ids.add(event_id)
        event_type = _enum(
            f"{key}.event_type",
            event.get("event_type"),
            EVENT_TYPES,
        )
        predecessor = event.get("predecessor_event_id")
        if predecessor is not None:
            predecessor = _safe_id(f"{key}.predecessor_event_id", predecessor)
        observed_at = _timestamp(f"{key}.observed_at", event.get("observed_at"))
        global_id = _safe_id(
            f"{key}.global_gate_observation_id",
            event.get("global_gate_observation_id"),
        )
        revalidates = event.get("revalidates_decision_key")
        if event_type == "wake_revalidation":
            revalidates = _safe_id(f"{key}.revalidates_decision_key", revalidates)
        elif revalidates is not None:
            raise CapacityGateLifecycleSimulationError(
                "decision event must not set revalidates_decision_key"
            )
        try:
            decision = validate_gate_decision(event.get("decision"))
        except ProviderResourceValidationError as exc:
            raise CapacityGateLifecycleSimulationError(
                f"{key}.decision is invalid"
            ) from exc
        evidence = _evidence(f"{key}.evidence", event.get("evidence"))
        events.append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "predecessor_event_id": predecessor,
                "observed_at": observed_at,
                "global_gate_observation_id": global_id,
                "revalidates_decision_key": revalidates,
                "decision": decision,
                "evidence": evidence,
            }
        )
    return events


def _evidence(key: str, value: object) -> dict[str, Any]:
    evidence = _object(key, value)
    _exact_keys(
        key,
        evidence,
        {
            "kind",
            "synthetic",
            "freshness",
            "mapping_status",
            "currentness_revision",
            "remaining",
            "threshold",
        },
    )
    kind = _enum(f"{key}.kind", evidence.get("kind"), EVIDENCE_KINDS)
    synthetic = _boolean(f"{key}.synthetic", evidence.get("synthetic"))
    if kind == "confirmed_exhaustion" and not synthetic:
        raise CapacityGateLifecycleSimulationError(
            "non-synthetic confirmed exhaustion has no trusted v1 authority"
        )
    freshness = _enum(
        f"{key}.freshness",
        evidence.get("freshness"),
        {"current", "stale", "unknown", "missing"},
    )
    mapping_status = _enum(
        f"{key}.mapping_status",
        evidence.get("mapping_status"),
        {"exact", "ambiguous", "missing"},
    )
    remaining = _quantity(f"{key}.remaining", evidence.get("remaining"))
    threshold = _quantity(f"{key}.threshold", evidence.get("threshold"))
    return {
        "kind": kind,
        "synthetic": synthetic,
        "freshness": freshness,
        "mapping_status": mapping_status,
        "currentness_revision": _safe_id(
            f"{key}.currentness_revision",
            evidence.get("currentness_revision"),
        ),
        "remaining": remaining,
        "threshold": threshold,
    }


def _quantity(key: str, value: object) -> dict[str, Any]:
    quantity = _object(key, value)
    _exact_keys(key, quantity, {"value", "unit"})
    number = quantity.get("value")
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or number < 0
        or (isinstance(number, float) and not math.isfinite(number))
    ):
        raise CapacityGateLifecycleSimulationError(
            f"{key}.value must be a non-negative number"
        )
    return {
        "value": number,
        "unit": _enum(
            f"{key}.unit",
            quantity.get("unit"),
            {"percent", "tokens", "credits", "requests"},
        ),
    }


def _validate_event_duplicates(events: list[dict[str, Any]]) -> None:
    try:
        deduplicate_gate_decisions([event["decision"] for event in events])
    except ProviderResourceValidationError as exc:
        raise CapacityGateLifecycleSimulationError(
            "event sequence contains a conflicting decision duplicate"
        ) from exc


def _active_gate(
    gate_state: dict[str, Any],
    resource_key: str,
) -> dict[str, Any] | None:
    return next(
        (
            gate
            for gate in gate_state["active_gates"]
            if gate["resource_key"] == resource_key
        ),
        None,
    )


def _set_active_gate(
    gate_state: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    active = _active_gate(gate_state, decision["resource_key"])
    replacement = _active_gate_from_decision(decision)
    if active is None:
        gate_state["active_gates"].append(replacement)
    else:
        gate_state["active_gates"][gate_state["active_gates"].index(active)] = (
            replacement
        )
    gate_state["active_gates"].sort(key=lambda item: item["resource_key"])


def _active_gate_from_decision(
    decision: dict[str, Any],
) -> dict[str, Any]:
    return {
        "resource_key": decision["resource_key"],
        "decision_key": decision["decision_key"],
        "wake_key": decision["wake_key"],
        "reset_at": decision["reset_at"],
        "status": "active",
    }


def _append_decision(
    history: list[dict[str, Any]],
    decision: dict[str, Any],
) -> None:
    history.append(deepcopy(decision))


def _wake_entry(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource_key": decision["resource_key"],
        "decision_key": decision["decision_key"],
        "wake_key": decision["wake_key"],
        "reset_at": decision["reset_at"],
    }


def _event_result(
    event: dict[str, Any],
    event_index: int,
    preview: str,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "event_index": event_index,
        "event_id": event["event_id"],
        "decision_key": event["decision"]["decision_key"],
        "preview": preview,
        "reason_codes": sorted(set(reasons)),
    }


def _task_disposition_preview(value: object) -> dict[str, Any]:
    preview = _object("task_disposition_preview", value)
    _exact_keys(
        "task_disposition_preview",
        preview,
        {"baseline", "counterfactual", "applied"},
    )
    _literal(
        "task_disposition_preview.baseline",
        preview.get("baseline"),
        "unchanged",
    )
    _enum(
        "task_disposition_preview.counterfactual",
        preview.get("counterfactual"),
        {"unchanged", "deferred", "released", "hard_excluded"},
    )
    _literal(
        "task_disposition_preview.applied",
        preview.get("applied"),
        False,
    )
    return deepcopy(preview)


def _wake_registry_preview(value: object) -> dict[str, Any]:
    preview = _object("wake_registry_preview", value)
    _exact_keys(
        "wake_registry_preview",
        preview,
        {"would_register", "would_replace", "would_remove"},
    )
    for field in ("would_register", "would_replace", "would_remove"):
        items = _list(f"wake_registry_preview.{field}", preview.get(field))
        for index, item in enumerate(items):
            obj = _object(f"wake_registry_preview.{field}[{index}]", item)
            for key, raw in obj.items():
                _safe_id(
                    f"wake_registry_preview.{field}[{index}].{key}",
                    raw,
                )
    return deepcopy(preview)


def _event_results(value: object) -> list[dict[str, Any]]:
    results = _list("event_results", value)
    for index, raw in enumerate(results):
        key = f"event_results[{index}]"
        result = _object(key, raw)
        _exact_keys(
            key,
            result,
            {
                "event_index",
                "event_id",
                "decision_key",
                "preview",
                "reason_codes",
            },
        )
        _literal(f"{key}.event_index", result.get("event_index"), index)
        _safe_id(f"{key}.event_id", result.get("event_id"))
        _safe_id(f"{key}.decision_key", result.get("decision_key"))
        _enum(f"{key}.preview", result.get("preview"), PREVIEWS)
        reasons = result.get("reason_codes")
        if (
            not isinstance(reasons, list)
            or not reasons
            or reasons != sorted(set(reasons))
        ):
            raise CapacityGateLifecycleSimulationError(
                f"{key}.reason_codes must be sorted, unique, and non-empty"
            )
        for reason_index, reason in enumerate(reasons):
            _safe_id(f"{key}.reason_codes[{reason_index}]", reason)
    return deepcopy(results)


def _object(key: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapacityGateLifecycleSimulationError(f"{key} must be an object")
    return value


def _list(key: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise CapacityGateLifecycleSimulationError(f"{key} must be a list")
    return value


def _exact_keys(
    key: str,
    value: dict[str, Any],
    expected: set[str],
) -> None:
    if set(value) != expected:
        raise CapacityGateLifecycleSimulationError(
            f"{key} keys must be exactly {sorted(expected)}"
        )


def _literal(key: str, value: object, expected: object) -> None:
    if type(value) is not type(expected) or value != expected:
        raise CapacityGateLifecycleSimulationError(f"{key} must be {expected!r}")


def _boolean(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise CapacityGateLifecycleSimulationError(f"{key} must be a boolean")
    return value


def _enum(key: str, value: object, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise CapacityGateLifecycleSimulationError(
            f"{key} must be one of {sorted(allowed)}"
        )
    return value


def _safe_id(key: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(character not in SAFE_ID_CHARS for character in value)
    ):
        raise CapacityGateLifecycleSimulationError(
            f"{key} must be a public-safe identifier"
        )
    return value


def _timestamp(key: str, value: object) -> str:
    try:
        parsed = parse_resource_timestamp(value)
    except ProviderResourceValidationError as exc:
        raise CapacityGateLifecycleSimulationError(
            f"{key} must be a valid timestamp"
        ) from exc
    return parsed.isoformat()


def _nonnegative_integer(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapacityGateLifecycleSimulationError(
            f"{key} must be a non-negative integer"
        )
    return value


def _digest_value(key: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise CapacityGateLifecycleSimulationError(f"{key} must be a sha256 digest")
    return value

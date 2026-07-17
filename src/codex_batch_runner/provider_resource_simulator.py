from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .execution_target_selector import (
    TargetSelectionError,
    assess_execution_target_candidates,
)
from .model_requirements import model_requirement_vector_value
from .provider_resource_authority import (
    build_authority_preview,
    global_gate_coverage,
    resource_gate_decision_key,
    resource_gate_key,
    resource_gate_wake_key,
    validate_admission_policy,
    validate_gate_decision,
    validate_mapping_v2,
)
from .provider_resource_report import (
    SAFE_ID,
    ProviderResourceValidationError,
    parse_resource_timestamp,
    validate_snapshot,
)

SIMULATION_REQUEST_CONTRACT = "provider-resource-simulation-request-v1"
SIMULATION_REPORT_CONTRACT = "provider-resource-simulation-report-v1"
SIMULATION_ACTIONS = {"allow", "defer", "covered_by_global", "evidence_only"}
GLOBAL_GATE_STATUSES = {"allowed", "fail_open", "gated", "unknown"}


def validate_simulation_request(value: object) -> dict[str, Any]:
    item = _object("simulation_request", value)
    _exact_keys(
        "simulation_request",
        item,
        {
            "schema_version",
            "contract",
            "selected_target_id",
            "requirement",
            "global_gate",
        },
    )
    _literal("simulation_request.schema_version", item.get("schema_version"), 1)
    _literal(
        "simulation_request.contract",
        item.get("contract"),
        SIMULATION_REQUEST_CONTRACT,
    )
    selected_target_id = _safe_id(
        "simulation_request.selected_target_id",
        item.get("selected_target_id"),
    )
    requirement = model_requirement_vector_value(
        "simulation_request.requirement",
        item.get("requirement"),
    )
    if requirement.get("schema_version") != 2:
        raise ProviderResourceValidationError(
            "simulation_request.requirement must be an exact requirement v2 revision"
        )
    if requirement.get("derivation_identity", {}).get("kind") == "legacy-derived":
        raise ProviderResourceValidationError(
            "simulation_request.requirement must not be a legacy-derived projection"
        )
    _safe_id(
        "simulation_request.requirement.revision_id",
        requirement.get("revision_id"),
    )

    global_gate = _object(
        "simulation_request.global_gate",
        item.get("global_gate"),
    )
    _exact_keys(
        "simulation_request.global_gate",
        global_gate,
        {"status", "reason", "reset_at"},
    )
    status = _enum(
        "simulation_request.global_gate.status",
        global_gate.get("status"),
        GLOBAL_GATE_STATUSES,
    )
    reason = _safe_id(
        "simulation_request.global_gate.reason",
        global_gate.get("reason"),
    )
    reset_at = global_gate.get("reset_at")
    if status == "gated":
        if reset_at is None:
            raise ProviderResourceValidationError(
                "gated global input requires reset_at"
            )
        reset_at = parse_resource_timestamp(reset_at).isoformat()
    elif reset_at is not None:
        raise ProviderResourceValidationError(
            "non-gated global input must not include reset_at"
        )
    return {
        "schema_version": 1,
        "contract": SIMULATION_REQUEST_CONTRACT,
        "selected_target_id": selected_target_id,
        "requirement": requirement,
        "global_gate": {
            "status": status,
            "reason": reason,
            "reset_at": reset_at,
        },
    }


def build_provider_resource_simulation(
    config: Any,
    *,
    request: dict[str, Any],
    snapshots: list[dict[str, Any]],
    mapping: dict[str, Any],
    policy: dict[str, Any],
    evaluated_at: datetime,
) -> dict[str, Any]:
    validated_request = validate_simulation_request(request)
    validated_mapping = validate_mapping_v2(mapping)
    validated_policy = validate_admission_policy(policy)
    validated_snapshots = [validate_snapshot(snapshot) for snapshot in snapshots]
    snapshot_ids = [snapshot["snapshot_id"] for snapshot in validated_snapshots]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        raise ProviderResourceValidationError("snapshot ids must be unique")
    usable_snapshots = [
        snapshot
        for snapshot in validated_snapshots
        if parse_resource_timestamp(snapshot["generated_at"])
        <= evaluated_at + timedelta(seconds=60)
    ]
    invalid_snapshot_resources = {
        (
            snapshot["resource"]["provider_id"],
            snapshot["resource"]["quota_identity"].get("id"),
        )
        for snapshot in validated_snapshots
        if snapshot not in usable_snapshots
    }

    requirement = validated_request["requirement"]
    inventory = getattr(config, "execution_target_inventory", {}) or {}
    _safe_id(
        "execution_target_inventory.snapshot_id",
        inventory.get("snapshot_id"),
    )
    _safe_id(
        "execution_target_inventory.constraint_registry_version",
        inventory.get("constraint_registry_version"),
    )
    inventory_targets = (
        inventory.get("targets", {})
        if isinstance(inventory.get("targets"), dict)
        else {}
    )
    for target_id in inventory_targets:
        _safe_id("execution_target_inventory.target_id", target_id)
    assessment_error: str | None = None
    try:
        assessment = assess_execution_target_candidates(config, requirement)
    except TargetSelectionError as exc:
        assessment_error = exc.code
        assessment = {
            "selection_policy_version": "execution-target-selector-v1",
            "inventory_snapshot_id": inventory.get("snapshot_id"),
            "legacy_derived": False,
            "targets": [],
            "ranked_eligible_target_ids": [],
        }

    authority = build_authority_preview(
        snapshots=usable_snapshots,
        mapping=validated_mapping,
        policy=validated_policy,
        inventory=inventory,
        evaluated_at=evaluated_at,
    )
    assessment_by_id = {
        row["target_id"]: row
        for row in assessment["targets"]
    }
    authority_by_id = {
        row["target_id"]: row
        for row in authority["targets"]
    }
    mapping_by_target = _active_mapping_by_target(
        validated_mapping,
        evaluated_at=evaluated_at,
    )
    snapshot_by_resource = _snapshot_by_resource(usable_snapshots)
    policy_by_target = {
        rule["target_id"]: rule
        for rule in validated_policy["target_rules"]
    }

    selected_target_id = validated_request["selected_target_id"]
    inventory_target_ids = sorted(
        str(target_id)
        for target_id in inventory_targets
    )
    ranked_target_ids = list(assessment["ranked_eligible_target_ids"])
    target_ids = list(dict.fromkeys([selected_target_id, *ranked_target_ids, *inventory_target_ids]))
    rows: list[dict[str, Any]] = []
    for target_id in target_ids:
        selector_row = assessment_by_id.get(target_id)
        authority_row = authority_by_id.get(target_id)
        selector_reasons: list[str] = []
        if assessment_error:
            selector_reasons.append(assessment_error)
        elif selector_row is None:
            selector_reasons.append("target_not_in_inventory")
        else:
            selector_reasons.extend(selector_row["reasons"])
        selector_eligible = bool(
            selector_row is not None
            and selector_row["eligible"]
            and not assessment_error
        )
        resource_reasons = (
            list(authority_row["reasons"])
            if authority_row is not None
            else ["provider_resource_evidence_missing"]
        )
        binding = mapping_by_target.get(target_id)
        if (
            "snapshot_missing" in resource_reasons
            and binding is not None
            and (
                binding["provider_id"],
                binding["quota_identity_id"],
            )
            in invalid_snapshot_resources
        ):
            resource_reasons.append("snapshot_time_invalid")
        if "snapshot_missing" in resource_reasons and binding is not None:
            provider_identity_statuses = {
                snapshot["resource"]["quota_identity"]["status"]
                for snapshot in usable_snapshots
                if snapshot["resource"]["provider_id"] == binding["provider_id"]
            }
            if "unknown" in provider_identity_statuses:
                resource_reasons.append("snapshot_identity_unknown")
            if "unavailable" in provider_identity_statuses:
                resource_reasons.append("snapshot_identity_unavailable")
        resource_eligible = bool(
            authority_row is not None and authority_row["eligible"]
        )
        recommendation = _evidence_only_recommendation(
            [*selector_reasons, *resource_reasons]
        )
        if selector_eligible and resource_eligible:
            recommendation = _target_recommendation(
                binding=mapping_by_target[target_id],
                snapshot=snapshot_by_resource[
                    (
                        mapping_by_target[target_id]["provider_id"],
                        mapping_by_target[target_id]["quota_identity_id"],
                    )
                ],
                target_rule=policy_by_target[target_id],
                policy=validated_policy,
                mapping=validated_mapping,
                global_gate=validated_request["global_gate"],
                evaluated_at=evaluated_at,
            )
            if not recommendation["evidence_complete"]:
                resource_eligible = False
                resource_reasons.extend(recommendation["reason_codes"])
        rows.append(
            {
                "target_id": target_id,
                "role": "selected" if target_id == selected_target_id else "alternative",
                "exact_target": target_id in inventory_target_ids,
                "selector_eligible": selector_eligible,
                "hard_constraints_pass": bool(
                    selector_row and selector_row["hard_constraints_pass"]
                ),
                "quality_floor_pass": bool(
                    selector_row and selector_row["quality_floor_pass"]
                ),
                "provider_resource_eligible": resource_eligible,
                "included_reasons": _included_reasons(
                    selector_eligible=selector_eligible,
                    resource_eligible=resource_eligible,
                ),
                "excluded_reasons": sorted(
                    set([*selector_reasons, *resource_reasons])
                ),
                "recommendation": recommendation,
            }
        )

    by_id = {row["target_id"]: row for row in rows}
    selected = by_id[selected_target_id]
    alternatives = [
        by_id[target_id]
        for target_id in ranked_target_ids
        if target_id != selected_target_id
        and by_id[target_id]["selector_eligible"]
        and by_id[target_id]["provider_resource_eligible"]
    ]
    excluded = [
        row
        for row in rows
        if row["target_id"] != selected_target_id
        and not (
            row["selector_eligible"]
            and row["provider_resource_eligible"]
        )
    ]
    global_gate = deepcopy(validated_request["global_gate"])
    global_gate["terminal"] = global_gate["status"] == "gated"
    global_gate["evaluation_precedence"] = "global_first"
    return {
        "schema_version": 1,
        "contract": SIMULATION_REPORT_CONTRACT,
        "generated_at": evaluated_at.isoformat(),
        "read_only": True,
        "mutation_allowed": False,
        "scheduling_authoritative": False,
        "automatic_substitution": False,
        "d2b_activation": False,
        "input_revisions": {
            "requirement_revision": requirement["revision_id"],
            "selection_policy_version": assessment["selection_policy_version"],
            "target_inventory_snapshot_id": inventory.get("snapshot_id"),
            "constraint_registry_version": inventory.get(
                "constraint_registry_version"
            ),
            "mapping_revision": validated_mapping["mapping_revision"],
            "policy_revision": validated_policy["policy_revision"],
        },
        "policy_timing": deepcopy(validated_policy["timing"]),
        "global_gate": global_gate,
        "selected_target": selected,
        "alternative_recommendations": alternatives,
        "excluded_targets": excluded,
        "decision_impact": {
            "preserve_existing_execution": True,
            "runtime_mutations": [],
            "queue_changed": False,
            "cooldown_changed": False,
            "wake_state_changed": False,
            "routing_policy_changed": False,
        },
        "summary": {
            "selected_action": selected["recommendation"]["action"],
            "alternative_count": len(alternatives),
            "excluded_target_count": len(excluded),
            "defer_preview_count": sum(
                1
                for row in [selected, *alternatives]
                if row["recommendation"]["action"] == "defer"
            ),
            "covered_by_global_count": sum(
                1
                for row in [selected, *alternatives]
                if row["recommendation"]["action"] == "covered_by_global"
            ),
            "runtime_mutation_count": 0,
        },
    }


def render_provider_resource_simulation(report: dict[str, Any]) -> str:
    selected = report["selected_target"]
    recommendation = selected["recommendation"]
    lines = [
        "# provider resource D2-A simulation",
        "",
        "read_only: yes",
        "scheduling_authoritative: no",
        "automatic_substitution: no",
        "D2-B activation: no",
        "",
        (
            "global gate: "
            f"{report['global_gate']['status']} "
            f"terminal={'yes' if report['global_gate']['terminal'] else 'no'} "
            f"reset={report['global_gate']['reset_at'] or '-'}"
        ),
        "",
        "selected exact target",
        _render_target_row(selected),
    ]
    lines.extend(_render_decision_previews(recommendation))
    lines.extend(["", "alternative exact targets"])
    alternatives = report["alternative_recommendations"]
    if alternatives:
        for row in alternatives:
            lines.append(_render_target_row(row))
            lines.extend(_render_decision_previews(row["recommendation"]))
    else:
        lines.append("  none")
    lines.extend(["", "excluded targets"])
    excluded = report["excluded_targets"]
    if excluded:
        lines.extend(_render_target_row(row) for row in excluded)
    else:
        lines.append("  none")
    summary = report["summary"]
    lines.extend(
        [
            "",
            (
                "summary: "
                f"selected_action={summary['selected_action']} "
                f"alternatives={summary['alternative_count']} "
                f"excluded={summary['excluded_target_count']} "
                f"defer_previews={summary['defer_preview_count']} "
                f"covered_by_global={summary['covered_by_global_count']} "
                "runtime_mutations=0"
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def _target_recommendation(
    *,
    binding: dict[str, Any],
    snapshot: dict[str, Any],
    target_rule: dict[str, Any],
    policy: dict[str, Any],
    mapping: dict[str, Any],
    global_gate: dict[str, Any],
    evaluated_at: datetime,
) -> dict[str, Any]:
    windows = {
        window["window_id"]: window
        for window in snapshot["windows"]
    }
    invalid_reasons: list[str] = []
    inputs: list[tuple[dict[str, Any], dict[str, Any], datetime, datetime]] = []
    for window_rule in target_rule["window_rules"]:
        window = windows.get(window_rule["window_id"])
        if window is None:
            invalid_reasons.append("window_missing")
            continue
        if window.get("availability") != "observed":
            invalid_reasons.append(f"window_unavailable:{window_rule['window_id']}")
            continue
        remaining = window.get("remaining", {})
        if remaining.get("status") != "observed":
            invalid_reasons.append(f"remaining_unknown:{window_rule['window_id']}")
            continue
        if remaining.get("unit") != window_rule["remaining_unit"]:
            invalid_reasons.append(
                f"remaining_unit_mismatch:{window_rule['window_id']}"
            )
            continue
        reset_value = window.get("resets_at", {}).get("value")
        observed_value = window.get("observed_at")
        if (
            window.get("resets_at", {}).get("status") != "observed"
            or reset_value is None
        ):
            invalid_reasons.append(f"reset_unknown:{window_rule['window_id']}")
            continue
        if observed_value is None:
            invalid_reasons.append(
                f"observation_time_invalid:{window_rule['window_id']}"
            )
            continue
        observed_at = parse_resource_timestamp(observed_value)
        reset_at = parse_resource_timestamp(reset_value)
        if reset_at <= evaluated_at or reset_at <= observed_at:
            invalid_reasons.append(
                f"reset_not_future:{window_rule['window_id']}"
            )
            continue
        inputs.append((window_rule, window, observed_at, reset_at))
    if invalid_reasons:
        return _evidence_only_recommendation(invalid_reasons)

    global_terminal = global_gate["status"] == "gated"
    global_fail_open = global_gate["status"] in {"fail_open", "unknown"}
    decision_previews: list[dict[str, Any]] = []
    low_window_count = 0
    covered_window_count = 0
    for window_rule, window, observed_at, reset_at in inputs:
        remaining_value = float(window["remaining"]["value"])
        threshold = float(window_rule["gate_at_or_below"])
        at_or_below = remaining_value <= threshold
        if at_or_below:
            low_window_count += 1
        coverage = (
            {
                "status": "not_evaluated",
                "global_reset_at": None,
            }
            if global_fail_open
            else global_gate_coverage(
                global_gated=global_terminal,
                global_reset_at=global_gate["reset_at"],
                target_reset_at=reset_at.isoformat(),
            )
        )
        if global_terminal and not at_or_below:
            coverage = {
                "status": "not_evaluated",
                "global_reset_at": global_gate["reset_at"],
            }
        if global_fail_open:
            action = "evidence_only"
            reason = f"global_gate_{global_gate['status']}"
        elif global_terminal:
            if at_or_below and coverage["status"] == "covered":
                action = "covered_by_global"
                covered_window_count += 1
                reason = "covered_by_global"
            else:
                action = "evidence_only"
                reason = "global_gate_terminal"
        else:
            action = "defer" if at_or_below else "allow"
            reason = (
                "remaining_at_or_below_threshold"
                if at_or_below
                else "remaining_above_threshold"
            )
        decision = _gate_decision(
            binding=binding,
            window_id=window_rule["window_id"],
            observed_at=observed_at,
            reset_at=reset_at,
            action=action,
            coverage=coverage,
            policy=policy,
            mapping=mapping,
        )
        wake_at = (
            reset_at
            + timedelta(seconds=policy["timing"]["reset_grace_seconds"])
            if action == "defer"
            else None
        )
        decision_previews.append(
            {
                **decision,
                "reason": reason,
                "remaining": {
                    "value": window["remaining"]["value"],
                    "unit": window["remaining"]["unit"],
                    "gate_at_or_below": window_rule["gate_at_or_below"],
                },
                "wake_at": wake_at.isoformat() if wake_at else None,
                "wake_scheduled": False,
                "deduplicated_by_global": action == "covered_by_global",
            }
        )

    if global_fail_open:
        action = "evidence_only"
        reasons = [f"global_gate_{global_gate['status']}"]
    elif global_terminal:
        action = (
            "covered_by_global"
            if low_window_count > 0
            and low_window_count == covered_window_count
            else "evidence_only"
        )
        reasons = (
            ["covered_by_global"]
            if action == "covered_by_global"
            else ["global_gate_terminal"]
        )
    else:
        action = "defer" if low_window_count else "allow"
        reasons = (
            ["remaining_at_or_below_threshold"]
            if action == "defer"
            else ["remaining_above_threshold"]
        )
    return {
        "action": action,
        "reason_codes": reasons,
        "evidence_complete": True,
        "preserve_existing_execution": True,
        "runtime_effect": "none",
        "decision_previews": decision_previews,
    }


def _gate_decision(
    *,
    binding: dict[str, Any],
    window_id: str,
    observed_at: datetime,
    reset_at: datetime,
    action: str,
    coverage: dict[str, Any],
    policy: dict[str, Any],
    mapping: dict[str, Any],
) -> dict[str, Any]:
    if action not in SIMULATION_ACTIONS:
        raise ProviderResourceValidationError("simulation action is invalid")
    value = {
        "schema_version": 1,
        "contract": "provider-resource-gate-decision-v1",
        "decision_key": "placeholder",
        "resource_key": resource_gate_key(
            binding["provider_id"],
            binding["quota_identity_id"],
            binding["observation_scope"]["scope_id"],
            window_id,
        ),
        "wake_key": resource_gate_wake_key(
            binding["provider_id"],
            binding["quota_identity_id"],
            binding["observation_scope"]["scope_id"],
            window_id,
            reset_at.isoformat(),
        ),
        "policy_revision": policy["policy_revision"],
        "mapping_revision": mapping["mapping_revision"],
        "provider_id": binding["provider_id"],
        "quota_identity_id": binding["quota_identity_id"],
        "scope_id": binding["observation_scope"]["scope_id"],
        "window_id": window_id,
        "observed_at": observed_at.isoformat(),
        "reset_at": reset_at.isoformat(),
        "action": action,
        "global_coverage": coverage,
        "supersedes_decision_key": None,
    }
    value["decision_key"] = resource_gate_decision_key(value)
    return validate_gate_decision(value)


def _active_mapping_by_target(
    mapping: dict[str, Any],
    *,
    evaluated_at: datetime,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for binding in mapping["bindings"]:
        if (
            binding["status"] == "current"
            and parse_resource_timestamp(binding["verified_at"])
            <= evaluated_at
            < parse_resource_timestamp(binding["expires_at"])
        ):
            if binding["target_id"] in result:
                continue
            result[binding["target_id"]] = binding
    return result


def _snapshot_by_resource(
    snapshots: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    duplicates: set[tuple[str, str]] = set()
    for snapshot in snapshots:
        identity = snapshot["resource"]["quota_identity"]
        if identity["status"] != "verified":
            continue
        key = (snapshot["resource"]["provider_id"], identity["id"])
        if key in result:
            duplicates.add(key)
        else:
            result[key] = snapshot
    for key in duplicates:
        result.pop(key, None)
    return result


def _evidence_only_recommendation(reasons: list[str]) -> dict[str, Any]:
    return {
        "action": "evidence_only",
        "reason_codes": sorted(set(reasons or ["provider_resource_evidence_missing"])),
        "evidence_complete": False,
        "preserve_existing_execution": True,
        "runtime_effect": "none",
        "decision_previews": [],
    }


def _included_reasons(
    *,
    selector_eligible: bool,
    resource_eligible: bool,
) -> list[str]:
    reasons: list[str] = []
    if selector_eligible:
        reasons.extend(["hard_constraints_pass", "quality_floor_pass"])
    if resource_eligible:
        reasons.append("provider_resource_authority_eligible")
    return reasons


def _render_target_row(row: dict[str, Any]) -> str:
    reasons = row["recommendation"]["reason_codes"]
    return (
        f"  {row['target_id']}: action={row['recommendation']['action']} "
        f"selector={'included' if row['selector_eligible'] else 'excluded'} "
        f"resource={'included' if row['provider_resource_eligible'] else 'excluded'} "
        f"reasons={','.join(reasons) if reasons else '-'}"
    )


def _render_decision_previews(recommendation: dict[str, Any]) -> list[str]:
    decisions = recommendation["decision_previews"]
    if not decisions:
        return []
    lines = ["  decision previews"]
    for decision in decisions:
        lines.append(
            "    "
            f"{decision['window_id']}: action={decision['action']} "
            f"resource_key={decision['resource_key']} "
            f"decision_key={decision['decision_key']} "
            f"wake_key={decision['wake_key']} "
            f"wake_at={decision['wake_at'] or '-'} "
            f"wake_scheduled=no"
        )
    return lines


def _object(key: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderResourceValidationError(f"{key} must be an object")
    return value


def _exact_keys(key: str, value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ProviderResourceValidationError(f"{key} fields are invalid")


def _literal(key: str, value: object, expected: object) -> None:
    if value != expected or type(value) is not type(expected):
        raise ProviderResourceValidationError(f"{key} must be {expected!r}")


def _enum(key: str, value: object, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ProviderResourceValidationError(f"{key} is invalid")
    return value


def _safe_id(key: str, value: object) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ProviderResourceValidationError(
            f"{key} must be a public-safe opaque identifier"
        )
    return value

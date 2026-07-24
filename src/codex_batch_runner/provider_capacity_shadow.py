from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .provider_capacity_contract import validate_capacity_bundle
from .provider_resource_authority import validate_mapping_v2
from .provider_resource_report import ProviderResourceValidationError
from .worker_certification import (
    WorkerCertificationError,
    certify_worker,
    simulate_report_only_canary,
)


SHADOW_REQUEST_CONTRACT = "capacity-shadow-evaluation-request-v1"
SHADOW_REPORT_CONTRACT = "capacity-shadow-evaluation-report-v1"
CURRENTNESS_CONTRACT = "capacity-shadow-revision-currentness-v1"
CAPACITY_COMPARISON_POLICY_REVISION = "capacity-shadow-comparison-policy-v1"

_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-"
)
_REVISION_FIELDS = (
    "requirement_revision",
    "inventory_snapshot_id",
    "selector_policy_revision",
    "mapping_revision",
    "authority_revision",
    "simulator_revision",
    "capacity_policy_revision",
    "capacity_bundle_revision",
    "certification_policy_revision",
    "canary_policy_revision",
)
_ELIGIBLE_CERTIFICATION_STATES = {
    "eligible-readonly",
    "eligible-bounded-write",
    "default-candidate",
}


class CapacityShadowValidationError(ValueError):
    pass


def validate_shadow_evaluation_request(value: object) -> dict[str, Any]:
    """Validate and freeze one immutable report-only shadow request."""
    item = _object("request", value)
    _exact_keys(
        "request",
        item,
        {
            "schema_version",
            "contract",
            "evaluated_at",
            "revisions",
            "revision_currentness",
            "provider_resource_lineage",
            "provider_resource_mapping",
            "baseline",
            "preeligible_targets",
            "capacity_bundle",
        },
    )
    _literal("request.schema_version", item.get("schema_version"), 1)
    _literal("request.contract", item.get("contract"), SHADOW_REQUEST_CONTRACT)
    evaluated_at = _timestamp("request.evaluated_at", item.get("evaluated_at"))
    revisions = _revisions("request.revisions", item.get("revisions"))
    currentness = _revision_currentness(
        item.get("revision_currentness"),
        expected=revisions,
    )
    lineage = _provider_resource_lineage(item.get("provider_resource_lineage"))
    try:
        mapping = validate_mapping_v2(item.get("provider_resource_mapping"))
    except ProviderResourceValidationError as exc:
        raise CapacityShadowValidationError(
            "request.provider_resource_mapping is invalid"
        ) from exc
    baseline = _baseline(item.get("baseline"))
    targets = _preeligible_targets(
        item.get("preeligible_targets"),
        selector_order=baseline["selector_order"],
        evaluated_at=evaluated_at,
        revisions=revisions,
    )
    bundle = validate_capacity_bundle(item.get("capacity_bundle"))
    return {
        "schema_version": 1,
        "contract": SHADOW_REQUEST_CONTRACT,
        "evaluated_at": evaluated_at.isoformat(),
        "revisions": revisions,
        "revision_currentness": currentness,
        "provider_resource_lineage": lineage,
        "provider_resource_mapping": mapping,
        "baseline": baseline,
        "preeligible_targets": targets,
        "capacity_bundle": bundle,
    }


def evaluate_capacity_shadow(value: object) -> dict[str, Any]:
    """Return a deterministic advisory while preserving baseline byte-semantics."""
    request = validate_shadow_evaluation_request(value)
    baseline = deepcopy(request["baseline"])
    fallback_reasons = _request_fallback_reasons(request)
    evidence_by_target, evidence_reasons = _capacity_evidence_by_target(request)
    fallback_reasons.extend(evidence_reasons)

    recommendation: dict[str, Any]
    if fallback_reasons:
        recommendation = _baseline_fallback(
            baseline,
            fallback_reasons,
            evidence_by_target=evidence_by_target,
        )
    else:
        recommendation = _shadow_recommendation(
            baseline,
            request["preeligible_targets"],
            evidence_by_target,
        )

    report: dict[str, Any] = {
        "schema_version": 1,
        "contract": SHADOW_REPORT_CONTRACT,
        "evaluated_at": request["evaluated_at"],
        "read_only": True,
        "mutation_allowed": False,
        "scheduling_authoritative": False,
        "automatic_substitution": False,
        "live_routing": False,
        "default_routing": False,
        "provider_promotion": False,
        "synthetic_fixture_promotion": False,
        "queue_mutations": [],
        "config_mutations": [],
        "cooldown_mutations": [],
        "wake_mutations": [],
        "reservation_mutations": [],
        "routing_mutations": [],
        "revisions": deepcopy(request["revisions"]),
        "revision_currentness": deepcopy(request["revision_currentness"]),
        "provider_resource_lineage": deepcopy(
            request["provider_resource_lineage"]
        ),
        "provider_resource_mapping": deepcopy(
            request["provider_resource_mapping"]
        ),
        "baseline": baseline,
        "preeligible_target_ids": [
            target["target_id"]
            for target in request["preeligible_targets"]
        ],
        "preeligible_targets": deepcopy(request["preeligible_targets"]),
        "shadow_recommendation": recommendation,
    }
    report["report_hash"] = _digest(report)
    return report


def _revisions(key: str, value: object) -> dict[str, str]:
    item = _object(key, value)
    _exact_keys(key, item, set(_REVISION_FIELDS))
    revisions = {
        field: _safe_id(f"{key}.{field}", item.get(field))
        for field in _REVISION_FIELDS
    }
    _literal(
        f"{key}.capacity_policy_revision",
        revisions["capacity_policy_revision"],
        CAPACITY_COMPARISON_POLICY_REVISION,
    )
    return revisions


def _revision_currentness(
    value: object,
    *,
    expected: dict[str, str],
) -> dict[str, Any]:
    item = _object("request.revision_currentness", value)
    _exact_keys(
        "request.revision_currentness",
        item,
        {"contract", "current_revisions"},
    )
    _literal(
        "request.revision_currentness.contract",
        item.get("contract"),
        CURRENTNESS_CONTRACT,
    )
    current = _revisions(
        "request.revision_currentness.current_revisions",
        item.get("current_revisions"),
    )
    mismatches = [
        field
        for field in _REVISION_FIELDS
        if current[field] != expected[field]
    ]
    return {
        "contract": CURRENTNESS_CONTRACT,
        "current_revisions": current,
        "all_current": not mismatches,
        "mismatched_fields": mismatches,
    }


def _provider_resource_lineage(value: object) -> dict[str, Any]:
    key = "request.provider_resource_lineage"
    item = _object(key, value)
    _exact_keys(
        key,
        item,
        {
            "snapshot_ids",
            "mapping_revision",
            "authority_revision",
            "simulator_revision",
        },
    )
    snapshot_ids = _safe_id_list(
        f"{key}.snapshot_ids",
        item.get("snapshot_ids"),
        nonempty=True,
    )
    return {
        "snapshot_ids": snapshot_ids,
        "mapping_revision": _safe_id(
            f"{key}.mapping_revision",
            item.get("mapping_revision"),
        ),
        "authority_revision": _safe_id(
            f"{key}.authority_revision",
            item.get("authority_revision"),
        ),
        "simulator_revision": _safe_id(
            f"{key}.simulator_revision",
            item.get("simulator_revision"),
        ),
    }


def _baseline(value: object) -> dict[str, Any]:
    key = "request.baseline"
    item = _object(key, value)
    _exact_keys(
        key,
        item,
        {
            "decision",
            "decision_digest",
            "selected_target_id",
            "selector_order",
        },
    )
    decision = _json_value(f"{key}.decision", item.get("decision"))
    if not isinstance(decision, dict) or not decision:
        raise CapacityShadowValidationError(
            "request.baseline.decision must be a non-empty object"
        )
    digest = _digest(decision)
    _literal(f"{key}.decision_digest", item.get("decision_digest"), digest)
    selector_order = _safe_id_list(
        f"{key}.selector_order",
        item.get("selector_order"),
        nonempty=True,
    )
    selected = _safe_id(
        f"{key}.selected_target_id",
        item.get("selected_target_id"),
    )
    if selected != selector_order[0]:
        raise CapacityShadowValidationError(
            "baseline selected target must be first in selector order"
        )
    if (
        decision.get("selected_target_id") != selected
        or decision.get("ranked_target_ids") != selector_order
    ):
        raise CapacityShadowValidationError(
            "baseline envelope must match the digested decision"
        )
    return {
        "decision": decision,
        "decision_digest": digest,
        "selected_target_id": selected,
        "selector_order": selector_order,
    }


def _preeligible_targets(
    value: object,
    *,
    selector_order: list[str],
    evaluated_at: datetime,
    revisions: dict[str, str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise CapacityShadowValidationError(
            "request.preeligible_targets must be a non-empty list"
        )
    targets = [
        _preeligible_target(
            item,
            index=index,
            evaluated_at=evaluated_at,
            revisions=revisions,
        )
        for index, item in enumerate(value)
    ]
    ids = [target["target_id"] for target in targets]
    if ids != selector_order:
        raise CapacityShadowValidationError(
            "preeligible targets must exactly match baseline selector order"
        )
    return targets


def _preeligible_target(
    value: object,
    *,
    index: int,
    evaluated_at: datetime,
    revisions: dict[str, str],
) -> dict[str, Any]:
    key = f"request.preeligible_targets[{index}]"
    item = _object(key, value)
    _exact_keys(
        key,
        item,
        {
            "target_id",
            "selector_rank",
            "capability_pass",
            "safety_pass",
            "hard_constraints_pass",
            "quality_floor_pass",
            "binding",
            "worker_certification",
            "canary_gate",
        },
    )
    target_id = _safe_id(f"{key}.target_id", item.get("target_id"))
    rank = item.get("selector_rank")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank != index:
        raise CapacityShadowValidationError(
            f"{key}.selector_rank must match list position"
        )
    gates = (
        "capability_pass",
        "safety_pass",
        "hard_constraints_pass",
        "quality_floor_pass",
    )
    for gate in gates:
        _literal(f"{key}.{gate}", item.get(gate), True)
    binding = _target_binding(item.get("binding"), key=key, target_id=target_id)
    certification = _worker_certification(
        item.get("worker_certification"),
        key=key,
        evaluated_at=evaluated_at,
        expected_policy_revision=revisions["certification_policy_revision"],
    )
    canary = _canary_gate(
        item.get("canary_gate"),
        key=key,
        certification=certification,
        expected_policy_revision=revisions["canary_policy_revision"],
        request_evaluated_at=evaluated_at,
    )
    return {
        "target_id": target_id,
        "selector_rank": rank,
        **{gate: True for gate in gates},
        "binding": binding,
        "worker_certification": certification,
        "canary_gate": canary,
    }


def _target_binding(
    value: object,
    *,
    key: str,
    target_id: str,
) -> dict[str, str]:
    binding_key = f"{key}.binding"
    item = _object(binding_key, value)
    fields = {
        "binding_id",
        "target_id",
            "provider_id",
            "resource_id",
            "observation_id",
            "quota_identity_id",
            "model_id",
            "capacity_pool",
        "constraint_id",
        "remaining_unit",
    }
    _exact_keys(binding_key, item, fields)
    result = {
        field: _safe_id(f"{binding_key}.{field}", item.get(field))
        for field in fields
    }
    if result["target_id"] != target_id:
        raise CapacityShadowValidationError(
            f"{binding_key}.target_id must match target"
        )
    return result


def _worker_certification(
    value: object,
    *,
    key: str,
    evaluated_at: datetime,
    expected_policy_revision: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    cert_key = f"{key}.worker_certification"
    item = _object(cert_key, value)
    _exact_keys(
        cert_key,
        item,
        {
            "record",
            "candidate",
            "evidence",
        },
    )
    record = _object(f"{cert_key}.record", item.get("record"))
    try:
        certification_time = _timestamp(
            f"{cert_key}.record.evaluated_at",
            record.get("evaluated_at"),
        )
        derived = certify_worker(
            item.get("candidate"),
            item.get("evidence"),
            evaluated_at=certification_time,
        )
    except (WorkerCertificationError, CapacityShadowValidationError) as exc:
        raise CapacityShadowValidationError(
            f"{cert_key} does not match candidate and evidence"
        ) from exc
    if derived != record:
        raise CapacityShadowValidationError(
            f"{cert_key} does not match candidate and evidence"
        )
    expires_at = _timestamp(
        f"{cert_key}.record.expires_at", record.get("expires_at")
    )
    return {
        "record": deepcopy(record),
        "candidate": deepcopy(item["candidate"]),
        "evidence": deepcopy(item["evidence"]),
        "current_at_evaluation": (
            certification_time <= evaluated_at < expires_at
            and record.get("policy_revision") == expected_policy_revision
        ),
        "external_natural_attestation": False,
    }


def _canary_gate(
    value: object,
    *,
    key: str,
    certification: dict[str, Any] | None,
    expected_policy_revision: str,
    request_evaluated_at: datetime,
) -> dict[str, Any] | None:
    if value is None:
        return None
    canary_key = f"{key}.canary_gate"
    item = _object(canary_key, value)
    _exact_keys(
        canary_key,
        item,
        {
            "record",
            "cohort_key",
            "adverse_signals",
        },
    )
    if certification is None:
        raise CapacityShadowValidationError(
            f"{canary_key} must bind the target certification"
        )
    record = _object(f"{canary_key}.record", item.get("record"))
    adverse_signals = item.get("adverse_signals")
    if (
        isinstance(adverse_signals, bool)
        or not isinstance(adverse_signals, int)
        or adverse_signals < 0
    ):
        raise CapacityShadowValidationError(
            f"{canary_key}.adverse_signals must be non-negative"
        )
    cohort_key = _safe_id(
        f"{canary_key}.cohort_key", item.get("cohort_key")
    )
    try:
        canary_time = _timestamp(
            f"{canary_key}.record.evaluated_at",
            record.get("evaluated_at"),
        )
        derived = simulate_report_only_canary(
            certification["record"],
            cohort_key=cohort_key,
            candidate=certification["candidate"],
            evidence=certification["evidence"],
            evaluated_at=canary_time,
            adverse_signals=adverse_signals,
        )
    except (WorkerCertificationError, CapacityShadowValidationError) as exc:
        raise CapacityShadowValidationError(
            f"{canary_key} does not match certification inputs"
        ) from exc
    if derived != record:
        raise CapacityShadowValidationError(
            f"{canary_key} does not match certification inputs"
        )
    return {
        "record": deepcopy(record),
        "cohort_key": cohort_key,
        "adverse_signals": adverse_signals,
        "policy_current": record["policy_revision"]
        == expected_policy_revision,
        "chronology_current": (
            _timestamp(
                f"{canary_key}.record.evaluated_at",
                record["evaluated_at"],
            )
            >= _timestamp(
                f"{canary_key}.certification.evaluated_at",
                certification["record"]["evaluated_at"],
            )
            and _timestamp(
                f"{canary_key}.record.evaluated_at",
                record["evaluated_at"],
            )
            <= request_evaluated_at
        ),
    }


def _request_fallback_reasons(request: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not request["revision_currentness"]["all_current"]:
        reasons.append("revision_mismatch")
    if (
        request["provider_resource_lineage"]["mapping_revision"]
        != request["revisions"]["mapping_revision"]
        or request["provider_resource_lineage"]["authority_revision"]
        != request["revisions"]["authority_revision"]
        or request["provider_resource_lineage"]["simulator_revision"]
        != request["revisions"]["simulator_revision"]
    ):
        reasons.append("provider_resource_lineage_revision_mismatch")
    bundle = request["capacity_bundle"]
    if bundle.get("bundle_id") != request["revisions"]["capacity_bundle_revision"]:
        reasons.append("capacity_bundle_revision_mismatch")
    lineage = bundle.get("lineage")
    if (
        not isinstance(lineage, dict)
        or lineage.get("canonical_contract") != "provider-resource-snapshot-v1"
        or lineage.get("canonical_contract_unchanged") is not True
        or lineage.get("extension_role") != "advisory_evidence_input"
        or lineage.get("canonical_runtime_state") is not False
    ):
        reasons.append("capacity_bundle_source_lineage_invalid")
    canonical_snapshot_ids = sorted(
        {
            observation["canonical_snapshot"]["snapshot_id"]
            for observation in bundle.get("observations", [])
            if isinstance(observation, dict)
            and observation.get("source_contract")
            == "provider-resource-snapshot-v1"
            and isinstance(observation.get("canonical_snapshot"), dict)
            and isinstance(
                observation["canonical_snapshot"].get("snapshot_id"), str
            )
        }
    )
    if (
        canonical_snapshot_ids
        != request["provider_resource_lineage"]["snapshot_ids"]
    ):
        reasons.append("provider_resource_lineage_revision_mismatch")
    for target in request["preeligible_targets"]:
        certification = target["worker_certification"]
        canary = target["canary_gate"]
        if certification is None and canary is not None:
            reasons.append("canary_without_certification")
            continue
        if certification is None:
            continue
        record = certification["record"]
        if record["state"] not in _ELIGIBLE_CERTIFICATION_STATES:
            reasons.append("worker_certification_ineligible")
        if (
            not certification["current_at_evaluation"]
            or not certification["external_natural_attestation"]
            or record["comparability"]["execution_quality"] is not True
            or record["natural_evidence"]["sample_count"] < 20
            or record["fallback"]["safe_if_selected_execution_fails"] is not True
            or record["target_snapshot_id"]
            != request["revisions"]["inventory_snapshot_id"]
        ):
            reasons.append("worker_certification_ineligible")
        if canary is None:
            reasons.append("worker_canary_gate_missing")
        elif (
            not canary["policy_current"]
            or not canary["chronology_current"]
            or canary["record"]["report_only_lane"] != "canary"
            or canary["record"]["live_routing"] is not False
            or canary["record"]["mutation_allowed"] is not False
        ):
            reasons.append("worker_canary_gate_ineligible")
    return sorted(set(reasons))


def _capacity_evidence_by_target(
    request: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    bundle = request["capacity_bundle"]
    mapping = request["provider_resource_mapping"]
    observations = bundle.get("observations")
    if not isinstance(observations, list):
        return {}, ["capacity_bundle_observations_missing"]
    observations_by_id: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        if not isinstance(observation, dict):
            return {}, ["capacity_bundle_observation_invalid"]
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str):
            return {}, ["capacity_bundle_observation_invalid"]
        observations_by_id.setdefault(observation_id, []).append(observation)

    by_target: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    if bundle.get("conflict_evidence"):
        reasons.append("capacity_evidence_conflict")
    semantics: set[tuple[object, ...]] = set()
    for target in request["preeligible_targets"]:
        target_id = target["target_id"]
        binding = target["binding"]
        mapping_matches = [
            item
            for item in mapping["bindings"]
            if item["binding_id"] == binding["binding_id"]
            and item["status"] == "current"
        ]
        if (
            mapping["status"] != "current"
            or mapping["mapping_revision"]
            != request["revisions"]["mapping_revision"]
            or mapping["target_inventory_snapshot_id"]
            != request["revisions"]["inventory_snapshot_id"]
            or len(mapping_matches) != 1
        ):
            reasons.append("capacity_mapping_binding_invalid")
            continue
        mapping_binding = mapping_matches[0]
        if (
            mapping_binding["target_id"] != target_id
            or mapping_binding["provider_id"] != binding["provider_id"]
            or mapping_binding["quota_identity_id"]
            != binding["quota_identity_id"]
            or mapping_binding["capacity_pool"] != binding["capacity_pool"]
            or mapping_binding["identity_authority"] != "source_attested"
        ):
            reasons.append("capacity_mapping_binding_invalid")
            continue
        evaluated_at = _timestamp(
            "request.evaluated_at", request["evaluated_at"]
        )
        if not (
            _timestamp(
                "mapping.binding.verified_at",
                mapping_binding["verified_at"],
            )
            <= evaluated_at
            < _timestamp(
                "mapping.binding.expires_at",
                mapping_binding["expires_at"],
            )
        ):
            reasons.append("capacity_mapping_binding_invalid")
            continue
        candidates = observations_by_id.get(binding["observation_id"], [])
        if len(candidates) != 1:
            reasons.append(
                "capacity_evidence_missing"
                if not candidates
                else "capacity_evidence_ambiguous"
            )
            continue
        observation = candidates[0]
        canonical_snapshot = observation.get("canonical_snapshot")
        canonical_resource = (
            canonical_snapshot.get("resource")
            if isinstance(canonical_snapshot, dict)
            else None
        )
        canonical_producer = (
            canonical_snapshot.get("producer")
            if isinstance(canonical_snapshot, dict)
            else None
        )
        if (
            observation.get("provider_id") != binding["provider_id"]
            or observation.get("source_contract")
            != "provider-resource-snapshot-v1"
            or observation.get("acquisition_health") != "healthy"
            or observation.get("model_scope_revision")
            is not None
            or not isinstance(canonical_resource, dict)
            or canonical_resource.get("observation_scope")
            != mapping_binding["observation_scope"]
            or not isinstance(canonical_producer, dict)
            or canonical_producer.get("adapter_id")
            != mapping_binding["producer"]["adapter_id"]
            or canonical_producer.get("adapter_version")
            != mapping_binding["producer"]["adapter_revision"]
            or not isinstance(observation.get("freshness"), dict)
            or observation["freshness"].get("status") != "fresh"
        ):
            reasons.append("capacity_evidence_not_usable")
        observed_at_value = observation.get("observed_at")
        freshness = observation.get("freshness", {})
        max_age_seconds = freshness.get("max_age_seconds")
        if (
            not isinstance(observed_at_value, str)
            or isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
        ):
            reasons.append("capacity_evidence_not_usable")
        else:
            observed_at = _timestamp(
                "capacity_bundle.observation.observed_at",
                observed_at_value,
            )
            evaluated_at = _timestamp(
                "request.evaluated_at",
                request["evaluated_at"],
            )
            age_seconds = (evaluated_at - observed_at).total_seconds()
            if age_seconds < 0 or age_seconds > max_age_seconds:
                reasons.append("capacity_evidence_stale")
        scope = observation.get("observation_scope")
        if (
            not isinstance(scope, dict)
            or scope.get("scope_id_status") != "identified"
            or not isinstance(scope.get("scope_id"), str)
        ):
            reasons.append("capacity_observation_scope_unknown")
        resource_matches = [
            resource
            for resource in observation.get("resources", [])
            if isinstance(resource, dict)
            and resource.get("resource_id") == binding["resource_id"]
        ]
        if len(resource_matches) != 1:
            reasons.append(
                "capacity_evidence_missing"
                if not resource_matches
                else "capacity_evidence_ambiguous"
            )
            continue
        resource = resource_matches[0]
        quota_identity = resource.get("quota_identity")
        if (
            not isinstance(quota_identity, dict)
            or quota_identity.get("status") != "verified"
            or quota_identity.get("id") != binding["quota_identity_id"]
        ):
            reasons.append("capacity_identity_unknown")
        model_scope = resource.get("model_scope")
        if (
            not isinstance(model_scope, dict)
            or model_scope.get("status") != "unknown"
            or model_scope.get("model_ids") != []
        ):
            reasons.append("capacity_model_scope_inferred")
        pool_matches = [
            pool
            for pool in bundle.get("pools", [])
            if isinstance(pool, dict)
            and pool.get("pool_id") == binding["capacity_pool"]
            and pool.get("provider_id") == binding["provider_id"]
            and binding["resource_id"] in pool.get("resource_ids", [])
            and pool.get("binding_status") == "explicit"
            and pool.get("source_revision")
            == request["revisions"]["mapping_revision"]
        ]
        if len(pool_matches) != 1:
            reasons.append(
                "capacity_pool_binding_missing"
                if not pool_matches
                else "capacity_pool_binding_ambiguous"
            )
        constraint_matches = [
            constraint
            for constraint in resource.get("constraints", [])
            if isinstance(constraint, dict)
            and constraint.get("constraint_id") == binding["constraint_id"]
        ]
        if len(constraint_matches) != 1:
            reasons.append(
                "capacity_evidence_missing"
                if not constraint_matches
                else "capacity_evidence_ambiguous"
            )
            continue
        constraint = constraint_matches[0]
        remaining = constraint.get("remaining")
        if (
            not isinstance(remaining, dict)
            or remaining.get("status") != "observed"
            or remaining.get("unit") != binding["remaining_unit"]
            or isinstance(remaining.get("value"), bool)
            or not isinstance(remaining.get("value"), (int, float))
            or remaining["value"] < 0
        ):
            reasons.append("capacity_evidence_remaining_invalid")
            continue
        if constraint.get("availability") not in {
            "available",
            "constrained",
            "exhausted",
        }:
            reasons.append("capacity_evidence_not_usable")
        resets_at = constraint.get("resets_at")
        if (
            not isinstance(resets_at, dict)
            or resets_at.get("status") != "observed"
            or not isinstance(resets_at.get("value"), str)
            or constraint.get("window_duration_seconds") is None
        ):
            reasons.append("capacity_evidence_window_unknown")
        evidence = {
            "target_id": target_id,
            "binding_id": binding["binding_id"],
            "observation_id": binding["observation_id"],
            "provider_id": binding["provider_id"],
            "resource_id": binding["resource_id"],
            "quota_identity_id": binding["quota_identity_id"],
            "model_id": binding["model_id"],
            "capacity_pool": binding["capacity_pool"],
            "constraint_id": binding["constraint_id"],
            "window_id": constraint.get("window_id"),
            "window_duration_seconds": constraint.get(
                "window_duration_seconds"
            ),
            "resets_at": resets_at["value"],
            "remaining": remaining["value"],
            "remaining_unit": remaining["unit"],
            "remaining_provenance": remaining.get("provenance"),
            "source_field": constraint.get("source_field"),
        }
        by_target[target_id] = evidence
        semantics.add(
            (
                evidence["window_duration_seconds"],
                evidence["remaining_unit"],
            )
        )
    if len(by_target) != len(request["preeligible_targets"]):
        reasons.append("capacity_evidence_incomplete")
    if len(semantics) != 1:
        reasons.append("capacity_evidence_not_comparable")
    return by_target, sorted(set(reasons))


def _shadow_recommendation(
    baseline: dict[str, Any],
    targets: list[dict[str, Any]],
    evidence_by_target: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ranked = sorted(
        targets,
        key=lambda target: (
            -float(evidence_by_target[target["target_id"]]["remaining"]),
            target["selector_rank"],
        ),
    )
    recommended = ranked[0]["target_id"]
    return {
        "status": "capacity_aware_shadow",
        "recommended_target_id": recommended,
        "baseline_target_id": baseline["selected_target_id"],
        "baseline_preserved": True,
        "reason_codes": ["complete_comparable_capacity_evidence"],
        "evidence_by_target": deepcopy(evidence_by_target),
        "runtime_effect": "none",
    }


def _baseline_fallback(
    baseline: dict[str, Any],
    reasons: list[str],
    *,
    evidence_by_target: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "capacity_unaware_baseline_fallback",
        "recommended_target_id": baseline["selected_target_id"],
        "baseline_target_id": baseline["selected_target_id"],
        "baseline_preserved": True,
        "reason_codes": sorted(set(reasons)),
        "evidence_by_target": deepcopy(evidence_by_target),
        "runtime_effect": "none",
    }


def _object(key: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapacityShadowValidationError(f"{key} must be an object")
    return value


def _exact_keys(
    key: str,
    value: dict[str, Any],
    expected: set[str],
) -> None:
    if set(value) != expected:
        raise CapacityShadowValidationError(
            f"{key} must contain exactly: {', '.join(sorted(expected))}"
        )


def _literal(key: str, value: object, expected: object) -> None:
    if value != expected:
        raise CapacityShadowValidationError(f"{key} must be {expected!r}")


def _safe_id(key: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or any(character not in _SAFE_ID_CHARS for character in value)
    ):
        raise CapacityShadowValidationError(
            f"{key} must be a public-safe identifier"
        )
    return value


def _safe_id_list(
    key: str,
    value: object,
    *,
    nonempty: bool,
) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise CapacityShadowValidationError(f"{key} must be a list")
    values = [
        _safe_id(f"{key}[{index}]", item)
        for index, item in enumerate(value)
    ]
    if len(values) != len(set(values)):
        raise CapacityShadowValidationError(f"{key} values must be unique")
    return values


def _boolean(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise CapacityShadowValidationError(f"{key} must be boolean")
    return value


def _timestamp(key: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise CapacityShadowValidationError(
            f"{key} must be an ISO timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapacityShadowValidationError(
            f"{key} must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CapacityShadowValidationError(
            f"{key} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _json_value(key: str, value: object) -> Any:
    try:
        serialized = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CapacityShadowValidationError(
            f"{key} must be canonical JSON"
        ) from exc
    parsed = json.loads(serialized)
    if _contains_sensitive_key(parsed):
        raise CapacityShadowValidationError(
            f"{key} contains a prohibited sensitive key"
        )
    return parsed


def _contains_sensitive_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(
                token in lowered
                for token in (
                    "credential",
                    "email",
                    "prompt",
                    "session",
                    "thread",
                )
            ):
                return True
            if _contains_sensitive_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _digest(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

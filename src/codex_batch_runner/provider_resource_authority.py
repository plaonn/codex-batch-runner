from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from .provider_resource_report import ProviderResourceValidationError, parse_resource_timestamp

MAPPING_V2_CONTRACT = "provider-resource-mapping-v2"
POLICY_CONTRACT = "provider-resource-admission-policy-v1"
GATE_DECISION_CONTRACT = "provider-resource-gate-decision-v1"
GATE_STATE_CONTRACT = "provider-resource-gate-state-v1"

SAFE_TIMESTAMP_PROVENANCE = {"provider_observed_at", "client_event_at"}
IDENTITY_AUTHORITIES = {"source_attested", "operator_attested_single_context"}


def validate_mapping_v2(value: object) -> dict[str, Any]:
    item = _object("mapping", value)
    _exact_keys(
        "mapping",
        item,
        {
            "schema_version",
            "contract",
            "mapping_revision",
            "target_inventory_snapshot_id",
            "status",
            "bindings",
        },
    )
    _literal("mapping.schema_version", item.get("schema_version"), 2)
    _literal("mapping.contract", item.get("contract"), MAPPING_V2_CONTRACT)
    _safe_id("mapping.mapping_revision", item.get("mapping_revision"))
    _safe_id("mapping.target_inventory_snapshot_id", item.get("target_inventory_snapshot_id"))
    _enum("mapping.status", item.get("status"), {"current", "stale", "superseded"})
    bindings = _list("mapping.bindings", item.get("bindings"))
    binding_ids: set[str] = set()
    for index, raw_binding in enumerate(bindings):
        key = f"mapping.bindings[{index}]"
        binding = _object(key, raw_binding)
        _exact_keys(
            key,
            binding,
            {
                "binding_id",
                "target_id",
                "capacity_pool",
                "provider_id",
                "quota_identity_id",
                "identity_authority",
                "observation_scope",
                "producer",
                "verified_at",
                "expires_at",
                "status",
                "invalidation_reason",
                "supersedes_binding_id",
            },
        )
        binding_id = _safe_id(f"{key}.binding_id", binding.get("binding_id"))
        if binding_id in binding_ids:
            raise ProviderResourceValidationError("mapping binding ids must be unique")
        binding_ids.add(binding_id)
        for field in ("target_id", "capacity_pool", "provider_id", "quota_identity_id"):
            _safe_id(f"{key}.{field}", binding.get(field))
        _enum(f"{key}.identity_authority", binding.get("identity_authority"), IDENTITY_AUTHORITIES)
        scope = _object(f"{key}.observation_scope", binding.get("observation_scope"))
        _exact_keys(
            f"{key}.observation_scope",
            scope,
            {
                "scope_id",
                "scope_revision",
                "host_instance_id",
                "codex_home_instance_id",
                "source_surface",
                "credential_context_id",
            },
        )
        for field in scope:
            _safe_id(f"{key}.observation_scope.{field}", scope.get(field))
        producer = _object(f"{key}.producer", binding.get("producer"))
        _exact_keys(f"{key}.producer", producer, {"adapter_id", "adapter_revision"})
        _safe_id(f"{key}.producer.adapter_id", producer.get("adapter_id"))
        _safe_id(f"{key}.producer.adapter_revision", producer.get("adapter_revision"))
        verified_at = parse_resource_timestamp(binding.get("verified_at"))
        expires_at = parse_resource_timestamp(binding.get("expires_at"))
        if expires_at <= verified_at:
            raise ProviderResourceValidationError("mapping expiry must be after verification")
        status = _enum(f"{key}.status", binding.get("status"), {"current", "invalidated", "superseded"})
        reason = binding.get("invalidation_reason")
        supersedes = binding.get("supersedes_binding_id")
        if reason is not None:
            _safe_id(f"{key}.invalidation_reason", reason)
        if supersedes is not None:
            _safe_id(f"{key}.supersedes_binding_id", supersedes)
            if supersedes == binding_id:
                raise ProviderResourceValidationError("mapping binding cannot supersede itself")
        if status == "current" and reason is not None:
            raise ProviderResourceValidationError("current mapping binding must not have invalidation reason")
        if status != "current" and reason is None:
            raise ProviderResourceValidationError("inactive mapping binding requires invalidation reason")
    _reject_sensitive_keys(item)
    return deepcopy(item)


def validate_admission_policy(value: object) -> dict[str, Any]:
    item = _object("policy", value)
    _exact_keys(
        "policy",
        item,
        {
            "schema_version",
            "contract",
            "policy_revision",
            "status",
            "enabled",
            "identity_authority",
            "allowed_mapping_revisions",
            "target_rules",
            "accepted_timestamp_provenance",
            "timing",
            "unknown_behavior",
            "global_gate_interaction",
            "rollback",
        },
    )
    _literal("policy.schema_version", item.get("schema_version"), 1)
    _literal("policy.contract", item.get("contract"), POLICY_CONTRACT)
    _safe_id("policy.policy_revision", item.get("policy_revision"))
    status = _enum("policy.status", item.get("status"), {"current", "stale", "superseded"})
    enabled = _boolean("policy.enabled", item.get("enabled"))
    authority = _enum("policy.identity_authority", item.get("identity_authority"), IDENTITY_AUTHORITIES)
    if authority != "source_attested":
        raise ProviderResourceValidationError(
            "operator_attested_single_context is defined but not enabled by the strict authority policy"
        )
    if status != "current" and enabled:
        raise ProviderResourceValidationError("only a current admission policy may be enabled")

    revisions = _nonempty_safe_id_list("policy.allowed_mapping_revisions", item.get("allowed_mapping_revisions"))
    if len(revisions) != len(set(revisions)):
        raise ProviderResourceValidationError("policy mapping revisions must be unique")
    provenance = _nonempty_safe_id_list(
        "policy.accepted_timestamp_provenance",
        item.get("accepted_timestamp_provenance"),
    )
    if not set(provenance).issubset(SAFE_TIMESTAMP_PROVENANCE):
        raise ProviderResourceValidationError("policy accepted timestamp provenance is invalid")
    target_rules = _list("policy.target_rules", item.get("target_rules"))
    target_ids: set[str] = set()
    for index, raw_rule in enumerate(target_rules):
        key = f"policy.target_rules[{index}]"
        rule = _object(key, raw_rule)
        _exact_keys(key, rule, {"target_id", "provider_id", "window_rules"})
        target_id = _safe_id(f"{key}.target_id", rule.get("target_id"))
        if target_id in target_ids:
            raise ProviderResourceValidationError("policy target ids must be unique")
        target_ids.add(target_id)
        _safe_id(f"{key}.provider_id", rule.get("provider_id"))
        window_rules = _list(f"{key}.window_rules", rule.get("window_rules"))
        if not window_rules:
            raise ProviderResourceValidationError("policy target rule requires at least one window rule")
        window_ids: set[str] = set()
        for window_index, raw_window_rule in enumerate(window_rules):
            window_key = f"{key}.window_rules[{window_index}]"
            window_rule = _object(window_key, raw_window_rule)
            _exact_keys(window_key, window_rule, {"window_id", "remaining_unit", "gate_at_or_below"})
            window_id = _safe_id(f"{window_key}.window_id", window_rule.get("window_id"))
            if window_id in window_ids:
                raise ProviderResourceValidationError("policy window ids must be unique per target")
            window_ids.add(window_id)
            unit = _enum(
                f"{window_key}.remaining_unit",
                window_rule.get("remaining_unit"),
                {"percent", "tokens", "credits", "requests"},
            )
            threshold = _nonnegative_number(f"{window_key}.gate_at_or_below", window_rule.get("gate_at_or_below"))
            if unit == "percent" and threshold > 100:
                raise ProviderResourceValidationError("percent threshold must be within 0..100")

    timing = _object("policy.timing", item.get("timing"))
    _exact_keys(
        "policy.timing",
        timing,
        {"max_age_seconds", "allowed_clock_skew_seconds", "reset_grace_seconds"},
    )
    for field in timing:
        _nonnegative_integer(f"policy.timing.{field}", timing.get(field))
    unknown = _object("policy.unknown_behavior", item.get("unknown_behavior"))
    _exact_keys("policy.unknown_behavior", unknown, {"missing", "stale", "invalid"})
    for field in unknown:
        _literal(f"policy.unknown_behavior.{field}", unknown.get(field), "allow_existing_execution")
    global_gate = _object("policy.global_gate_interaction", item.get("global_gate_interaction"))
    _exact_keys(
        "policy.global_gate_interaction",
        global_gate,
        {"evaluation_order", "when_global_gated", "same_reset"},
    )
    _literal("policy.global_gate_interaction.evaluation_order", global_gate.get("evaluation_order"), "global_first")
    _literal(
        "policy.global_gate_interaction.when_global_gated",
        global_gate.get("when_global_gated"),
        "skip_target_evaluation",
    )
    _literal(
        "policy.global_gate_interaction.same_reset",
        global_gate.get("same_reset"),
        "covered_by_global_no_duplicate_wake",
    )
    rollback = _object("policy.rollback", item.get("rollback"))
    _exact_keys("policy.rollback", rollback, {"disable_behavior", "typed_state_behavior", "legacy_scalar_behavior"})
    _literal(
        "policy.rollback.disable_behavior",
        rollback.get("disable_behavior"),
        "stop_new_target_decisions",
    )
    _literal(
        "policy.rollback.typed_state_behavior",
        rollback.get("typed_state_behavior"),
        "preserve_append_only_evidence",
    )
    _literal(
        "policy.rollback.legacy_scalar_behavior",
        rollback.get("legacy_scalar_behavior"),
        "remain_global_only",
    )
    _reject_sensitive_keys(item)
    return deepcopy(item)


def build_authority_preview(
    *,
    snapshots: list[dict[str, Any]],
    mapping: dict[str, Any] | None,
    policy: dict[str, Any] | None,
    inventory: dict[str, Any],
    evaluated_at: datetime,
) -> dict[str, Any]:
    validated_mapping = validate_mapping_v2(mapping) if mapping is not None else None
    validated_policy = validate_admission_policy(policy) if policy is not None else None
    targets = inventory.get("targets") if isinstance(inventory, dict) and isinstance(inventory.get("targets"), dict) else {}
    inventory_current = isinstance(inventory, dict) and inventory.get("status") == "current"
    binding_index: dict[str, list[dict[str, Any]]] = {}
    if validated_mapping is not None:
        for binding in validated_mapping["bindings"]:
            binding_index.setdefault(binding["target_id"], []).append(binding)
    policy_rules = {
        rule["target_id"]: rule
        for rule in (validated_policy["target_rules"] if validated_policy is not None else [])
    }
    snapshot_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        identity = snapshot.get("resource", {}).get("quota_identity", {})
        if identity.get("status") == "verified" and identity.get("id"):
            snapshot_index.setdefault(
                (str(snapshot["resource"].get("provider_id")), str(identity.get("id"))),
                [],
            ).append(snapshot)

    rows: list[dict[str, Any]] = []
    for target_id in sorted(set(targets) | set(binding_index) | set(policy_rules)):
        reasons: list[str] = []
        if target_id not in targets:
            reasons.append("mapping_target_unknown")
        elif not inventory_current:
            reasons.append("mapping_stale")
        if validated_mapping is None:
            reasons.append("mapping_missing")
            active: list[dict[str, Any]] = []
        else:
            if validated_mapping["status"] != "current":
                reasons.append("mapping_stale")
            if validated_mapping["target_inventory_snapshot_id"] != inventory.get("snapshot_id"):
                reasons.append("mapping_stale")
            bindings = binding_index.get(target_id, [])
            active = [
                binding
                for binding in bindings
                if binding["status"] == "current"
                and parse_resource_timestamp(binding["verified_at"]) <= evaluated_at
                < parse_resource_timestamp(binding["expires_at"])
            ]
            if not bindings:
                reasons.append("mapping_missing")
            elif not active:
                if any(parse_resource_timestamp(binding["verified_at"]) > evaluated_at for binding in bindings):
                    reasons.append("mapping_not_yet_valid")
                elif any(parse_resource_timestamp(binding["expires_at"]) <= evaluated_at for binding in bindings):
                    reasons.append("mapping_expired")
                else:
                    reasons.append("mapping_disabled")
            elif len(active) != 1:
                reasons.append("mapping_ambiguous")
        binding = active[0] if len(active) == 1 else None

        if validated_policy is None:
            reasons.append("policy_missing")
        else:
            if validated_policy["status"] != "current":
                reasons.append("policy_stale")
            if not validated_policy["enabled"]:
                reasons.append("policy_disabled")
            if validated_mapping is not None and validated_mapping["mapping_revision"] not in validated_policy["allowed_mapping_revisions"]:
                reasons.append("policy_mapping_revision_rejected")
            if target_id not in policy_rules:
                reasons.append("policy_target_rejected")
            elif binding is not None and policy_rules[target_id]["provider_id"] != binding["provider_id"]:
                reasons.append("policy_target_rejected")

        snapshot = None
        if binding is not None:
            matches = snapshot_index.get((binding["provider_id"], binding["quota_identity_id"]), [])
            if not matches:
                reasons.append("snapshot_missing")
            elif len(matches) != 1:
                reasons.append("snapshot_ambiguous")
            else:
                snapshot = matches[0]
                identity = snapshot["resource"]["quota_identity"]
                if (
                    binding["identity_authority"] != "source_attested"
                    or identity.get("source") != "source_attested"
                    or identity.get("confidence") != "verified"
                ):
                    reasons.append("snapshot_identity_unverified")
                scope = snapshot["resource"].get("observation_scope")
                if not isinstance(scope, dict):
                    reasons.append("snapshot_scope_missing")
                elif any(
                    scope.get(field) != binding["observation_scope"].get(field)
                    for field in binding["observation_scope"]
                ):
                    reasons.append("snapshot_scope_mismatch")
                producer = snapshot.get("producer", {})
                if (
                    producer.get("adapter_id") != binding["producer"]["adapter_id"]
                    or producer.get("adapter_version") != binding["producer"]["adapter_revision"]
                ):
                    reasons.append("snapshot_producer_mismatch")
                rule = policy_rules.get(target_id)
                if rule is not None:
                    windows = {window["window_id"]: window for window in snapshot.get("windows", [])}
                    accepted_provenance = set(validated_policy["accepted_timestamp_provenance"])
                    timing = validated_policy["timing"]
                    for window_rule in rule["window_rules"]:
                        window = windows.get(window_rule["window_id"])
                        if window is None:
                            reasons.append("window_missing")
                            continue
                        provenance = window.get("source", {}).get("timestamp_provenance")
                        confidence = window.get("source", {}).get("confidence")
                        provenance_confident = (
                            provenance == "provider_observed_at"
                            and confidence == "verified_source_timestamp"
                        ) or (
                            provenance == "client_event_at"
                            and confidence
                            in {"experimental_observed_shape", "verified_source_timestamp"}
                        )
                        if provenance not in accepted_provenance or not provenance_confident:
                            reasons.append("timestamp_provenance_rejected")
                        observed_raw = window.get("observed_at")
                        observed = (
                            parse_resource_timestamp(observed_raw)
                            if observed_raw is not None
                            else None
                        )
                        reset_raw = window.get("resets_at", {}).get("value")
                        reset = (
                            parse_resource_timestamp(reset_raw)
                            if reset_raw is not None
                            else None
                        )
                        if (
                            observed is None
                            or observed
                            > evaluated_at
                            + timedelta(seconds=timing["allowed_clock_skew_seconds"])
                            or evaluated_at
                            > observed + timedelta(seconds=timing["max_age_seconds"])
                            or (
                                reset is not None
                                and reset <= evaluated_at
                                and observed <= reset
                            )
                        ):
                            reasons.append("window_freshness_rejected")
        rows.append(
            {
                "target_id": target_id,
                "eligible": not reasons,
                "reasons": sorted(set(reasons)),
                "mapping_revision": validated_mapping.get("mapping_revision") if validated_mapping else None,
                "policy_revision": validated_policy.get("policy_revision") if validated_policy else None,
                "provider_id": binding.get("provider_id") if binding else None,
                "quota_identity_id": binding.get("quota_identity_id") if binding else None,
                "scope_id": binding.get("observation_scope", {}).get("scope_id") if binding else None,
                "snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
            }
        )
    return {
        "contract": "provider-resource-authority-preview-v1",
        "read_only": True,
        "scheduling_authoritative": False,
        "strict_identity_authority": "source_attested",
        "mapping_revision": validated_mapping.get("mapping_revision") if validated_mapping else None,
        "policy_revision": validated_policy.get("policy_revision") if validated_policy else None,
        "targets": rows,
        "eligible_target_count": sum(1 for row in rows if row["eligible"]),
    }


def resource_gate_key(provider_id: str, quota_identity_id: str, scope_id: str, window_id: str) -> str:
    for key, value in (
        ("provider_id", provider_id),
        ("quota_identity_id", quota_identity_id),
        ("scope_id", scope_id),
        ("window_id", window_id),
    ):
        _safe_id(key, value)
    return _digest("resource", provider_id, quota_identity_id, scope_id, window_id)


def resource_gate_decision_key(value: dict[str, Any]) -> str:
    return _digest(
        "decision",
        str(value["policy_revision"]),
        str(value["mapping_revision"]),
        str(value["provider_id"]),
        str(value["quota_identity_id"]),
        str(value["scope_id"]),
        str(value["window_id"]),
        _canonical_timestamp(value["observed_at"]),
        _canonical_timestamp(value["reset_at"]),
        str(value["action"]),
    )


def resource_gate_wake_key(provider_id: str, quota_identity_id: str, scope_id: str, window_id: str, reset_at: str) -> str:
    return _digest(
        "wake",
        provider_id,
        quota_identity_id,
        scope_id,
        window_id,
        _canonical_timestamp(reset_at),
    )


def validate_gate_decision(value: object) -> dict[str, Any]:
    item = _object("gate_decision", value)
    _exact_keys(
        "gate_decision",
        item,
        {
            "schema_version",
            "contract",
            "decision_key",
            "resource_key",
            "wake_key",
            "policy_revision",
            "mapping_revision",
            "provider_id",
            "quota_identity_id",
            "scope_id",
            "window_id",
            "observed_at",
            "reset_at",
            "action",
            "global_coverage",
            "supersedes_decision_key",
        },
    )
    _literal("gate_decision.schema_version", item.get("schema_version"), 1)
    _literal("gate_decision.contract", item.get("contract"), GATE_DECISION_CONTRACT)
    for field in (
        "decision_key",
        "resource_key",
        "wake_key",
        "policy_revision",
        "mapping_revision",
        "provider_id",
        "quota_identity_id",
        "scope_id",
        "window_id",
    ):
        _safe_id(f"gate_decision.{field}", item.get(field))
    observed = parse_resource_timestamp(item.get("observed_at"))
    reset = parse_resource_timestamp(item.get("reset_at"))
    if reset <= observed:
        raise ProviderResourceValidationError("gate reset must be after observation")
    action = _enum(
        "gate_decision.action",
        item.get("action"),
        {"defer", "allow", "covered_by_global", "evidence_only"},
    )
    coverage = _object("gate_decision.global_coverage", item.get("global_coverage"))
    _exact_keys(coverage_key := "gate_decision.global_coverage", coverage, {"status", "global_reset_at"})
    coverage_status = _enum(
        f"{coverage_key}.status",
        coverage.get("status"),
        {"not_evaluated", "not_covered", "covered"},
    )
    global_reset = coverage.get("global_reset_at")
    if global_reset is not None:
        global_reset_dt = parse_resource_timestamp(global_reset)
    else:
        global_reset_dt = None
    if coverage_status == "covered":
        if global_reset_dt is None or global_reset_dt < reset or action != "covered_by_global":
            raise ProviderResourceValidationError("covered target gate requires a covering global reset and action")
    elif action == "covered_by_global":
        raise ProviderResourceValidationError("covered_by_global action requires covered global status")
    supersedes = item.get("supersedes_decision_key")
    if supersedes is not None:
        _safe_id("gate_decision.supersedes_decision_key", supersedes)
        if supersedes == item["decision_key"]:
            raise ProviderResourceValidationError("gate decision cannot supersede itself")
    expected_resource = resource_gate_key(
        item["provider_id"],
        item["quota_identity_id"],
        item["scope_id"],
        item["window_id"],
    )
    expected_decision = resource_gate_decision_key(item)
    expected_wake = resource_gate_wake_key(
        item["provider_id"],
        item["quota_identity_id"],
        item["scope_id"],
        item["window_id"],
        item["reset_at"],
    )
    if item["resource_key"] != expected_resource:
        raise ProviderResourceValidationError("gate resource_key does not match canonical fields")
    if item["decision_key"] != expected_decision:
        raise ProviderResourceValidationError("gate decision_key does not match canonical fields")
    if item["wake_key"] != expected_wake:
        raise ProviderResourceValidationError("gate wake_key does not match canonical fields")
    return deepcopy(item)


def validate_gate_state(value: object) -> dict[str, Any]:
    item = _object("gate_state", value)
    _exact_keys(
        "gate_state",
        item,
        {"schema_version", "contract", "migration", "active_gates"},
    )
    _literal("gate_state.schema_version", item.get("schema_version"), 1)
    _literal("gate_state.contract", item.get("contract"), GATE_STATE_CONTRACT)
    migration = _object("gate_state.migration", item.get("migration"))
    _exact_keys(
        "gate_state.migration",
        migration,
        {"mode", "legacy_scalar_role", "rollback_mode", "evidence_history"},
    )
    _literal("gate_state.migration.mode", migration.get("mode"), "typed_primary_scalar_compatibility")
    _literal(
        "gate_state.migration.legacy_scalar_role",
        migration.get("legacy_scalar_role"),
        "global_gate_only",
    )
    _literal(
        "gate_state.migration.rollback_mode",
        migration.get("rollback_mode"),
        "disable_typed_evaluation_preserve_records",
    )
    _literal("gate_state.migration.evidence_history", migration.get("evidence_history"), "append_only")
    gates = _list("gate_state.active_gates", item.get("active_gates"))
    resources: set[str] = set()
    wakes: set[str] = set()
    for index, raw_gate in enumerate(gates):
        key = f"gate_state.active_gates[{index}]"
        gate = _object(key, raw_gate)
        _exact_keys(key, gate, {"resource_key", "decision_key", "wake_key", "reset_at", "status"})
        for field in ("resource_key", "decision_key", "wake_key"):
            _safe_id(f"{key}.{field}", gate.get(field))
        parse_resource_timestamp(gate.get("reset_at"))
        _literal(f"{key}.status", gate.get("status"), "active")
        if gate["resource_key"] in resources:
            raise ProviderResourceValidationError("gate state may contain only one active gate per resource")
        if gate["wake_key"] in wakes:
            raise ProviderResourceValidationError("gate state wake keys must be unique")
        resources.add(gate["resource_key"])
        wakes.add(gate["wake_key"])
    return deepcopy(item)


def deduplicate_gate_decisions(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for value in values:
        validated = validate_gate_decision(value)
        previous = by_key.get(validated["decision_key"])
        if previous is not None and previous != validated:
            raise ProviderResourceValidationError("duplicate decision key has conflicting evidence")
        by_key[validated["decision_key"]] = validated
    return [by_key[key] for key in sorted(by_key)]


def global_gate_coverage(*, global_gated: bool, global_reset_at: str | None, target_reset_at: str) -> dict[str, Any]:
    target_reset = parse_resource_timestamp(target_reset_at)
    if not global_gated:
        return {"status": "not_covered", "global_reset_at": global_reset_at}
    if global_reset_at is None:
        return {"status": "not_evaluated", "global_reset_at": None}
    global_reset = parse_resource_timestamp(global_reset_at)
    if global_reset >= target_reset:
        return {"status": "covered", "global_reset_at": global_reset.isoformat()}
    return {"status": "not_covered", "global_reset_at": global_reset.isoformat()}


def _digest(namespace: str, *values: str) -> str:
    payload = json.dumps([namespace, *values], ensure_ascii=True, separators=(",", ":"))
    return f"{namespace}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _canonical_timestamp(value: object) -> str:
    return parse_resource_timestamp(value).astimezone(timezone.utc).isoformat()


def _object(key: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderResourceValidationError(f"{key} must be an object")
    return value


def _list(key: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ProviderResourceValidationError(f"{key} must be a list")
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


def _boolean(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ProviderResourceValidationError(f"{key} must be a boolean")
    return value


def _safe_id(key: str, value: object) -> str:
    from .provider_resource_report import SAFE_ID

    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ProviderResourceValidationError(f"{key} must be a public-safe opaque identifier")
    return value


def _nonempty_safe_id_list(key: str, value: object) -> list[str]:
    items = _list(key, value)
    if not items:
        raise ProviderResourceValidationError(f"{key} must not be empty")
    return [_safe_id(f"{key}[{index}]", item) for index, item in enumerate(items)]


def _nonnegative_integer(key: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderResourceValidationError(f"{key} must be a non-negative integer")
    return value


def _nonnegative_number(key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ProviderResourceValidationError(f"{key} must be a non-negative number")
    result = float(value)
    if result == float("inf") or result != result:
        raise ProviderResourceValidationError(f"{key} must be a finite non-negative number")
    return result


def _reject_sensitive_keys(value: object) -> None:
    from .provider_resource_report import FORBIDDEN_KEYS

    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ProviderResourceValidationError("sensitive or raw fields are forbidden")
            _reject_sensitive_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_keys(item)

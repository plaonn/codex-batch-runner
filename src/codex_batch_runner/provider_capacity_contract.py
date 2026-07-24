"""Provider-neutral, advisory capacity evidence extensions.

``provider-resource-capacity-bundle-v1`` is an additive evidence bundle.  It
does not replace ``provider-resource-snapshot-v1``, create runtime state, or
authorize scheduling.  The projections in this module accept only the strict,
sanitized outputs of the inactive Codex and Antigravity acquisition adapters.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Iterable, Mapping


CAPACITY_BUNDLE_CONTRACT = "provider-resource-capacity-bundle-v1"
CANONICAL_SNAPSHOT_CONTRACT = "provider-resource-snapshot-v1"
CODEX_SOURCE_CONTRACT = "codex-app-server-capacity-v1"
ANTIGRAVITY_SOURCE_CONTRACT = "antigravity-statusline-cache-v1"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_KEYS = {
    "account",
    "account_label",
    "argv",
    "command",
    "credential",
    "credentials",
    "email",
    "path",
    "prompt",
    "raw",
    "raw_output",
    "rollout_path",
    "session_id",
    "stderr",
    "stdout",
    "thread_id",
    "token",
    "transcript",
}


class ProviderCapacityValidationError(ValueError):
    """Raised when capacity evidence does not match the strict public contract."""


def capacity_content_id(value: object) -> str:
    """Return the contract's deterministic SHA-256 content identifier."""
    return "sha256:" + sha256(_canonical(value).encode("utf-8")).hexdigest()


def project_codex_app_server_capacity(
    value: object,
    *,
    evaluated_at: datetime | str | None = None,
    max_age_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Project one sanitized Codex app-server read into an advisory observation."""
    source = _validate_codex_source(value)
    evaluated, max_age = _freshness_inputs(evaluated_at, max_age_seconds)
    observed_at = _normalize_timestamp(source["collected_at"])
    resources: list[dict[str, Any]] = []
    for source_resource in source["resources"]:
        identity = _quota_identity(source_resource["limit_id"])
        resource_seed = {
            "provider_id": "codex",
            "quota_identity_key": _quota_identity_key(identity),
        }
        resource_id = _digest_id(resource_seed)
        constraints = [
            _codex_constraint(resource_id, window)
            for window in source_resource["windows"]
        ]
        resources.append(
            {
                "resource_id": resource_id,
                "quota_identity": identity,
                "model_scope": {"status": "unknown", "model_ids": []},
                "constraints": constraints,
            }
        )
    for resource in resources:
        resource["constraints"].sort(key=lambda item: item["constraint_id"])
    resources.sort(key=lambda item: item["resource_id"])
    body = {
        "provider_id": "codex",
        "source_contract": CODEX_SOURCE_CONTRACT,
        "source_revision": source["adapter_id"],
        "producer": {
            "id": "codex-app-server-capacity-adapter",
            "revision": source["adapter_id"],
        },
        "observation_scope": {
            "kind": "provider_control_plane",
            "scope_id_status": "unknown",
            "scope_id": None,
        },
        "observed_at": observed_at,
        "timestamp_provenance": source["timestamp_provenance"],
        "acquisition_health": {
            "observed": "healthy",
            "unknown": "degraded",
            "unavailable": "unavailable",
        }[source["status"]],
        "freshness": _freshness(observed_at, evaluated, max_age),
        "model_scope_revision": None,
        "canonical_snapshot": None,
        "resources": resources,
    }
    observation = {"observation_id": _digest_id(body), **body}
    return validate_capacity_observation(observation)


def project_antigravity_statusline_capacity(
    value: object,
    *,
    evaluated_at: datetime | str | None = None,
    max_age_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Project one strict Antigravity statusLine cache into an observation."""
    source = _validate_antigravity_source(value)
    evaluated, max_age = _freshness_inputs(evaluated_at, max_age_seconds)
    observed_at = _normalize_timestamp(source["collected_at"])
    resources: list[dict[str, Any]] = []
    for bucket in source["buckets"]:
        identity = _quota_identity(bucket["bucket_id"])
        resource_seed = {
            "provider_id": "antigravity",
            "quota_identity_key": _quota_identity_key(identity),
        }
        resource_id = _digest_id(resource_seed)
        constraint = _antigravity_constraint(resource_id, bucket)
        resources.append(
            {
                "resource_id": resource_id,
                "quota_identity": identity,
                # A bucket ID can resemble a model name, but the source contract
                # does not attest that relationship.
                "model_scope": {"status": "unknown", "model_ids": []},
                "constraints": [constraint],
            }
        )
    resources.sort(key=lambda item: item["resource_id"])
    revision = source["source_version"] or "unknown"
    body = {
        "provider_id": "antigravity",
        "source_contract": ANTIGRAVITY_SOURCE_CONTRACT,
        "source_revision": revision,
        "producer": {
            "id": "antigravity-statusline-capacity-adapter",
            "revision": revision,
        },
        "observation_scope": {
            "kind": "provider_control_plane",
            "scope_id_status": "unknown",
            "scope_id": None,
        },
        "observed_at": observed_at,
        "timestamp_provenance": source["timestamp_provenance"],
        "acquisition_health": "healthy",
        "freshness": _freshness(observed_at, evaluated, max_age),
        "model_scope_revision": None,
        "canonical_snapshot": None,
        "resources": resources,
    }
    observation = {"observation_id": _digest_id(body), **body}
    return validate_capacity_observation(observation)


def project_provider_resource_snapshot_capacity(
    value: object,
    *,
    evaluated_at: datetime | str | None = None,
    max_age_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Project a validated canonical snapshot without laundering its authority."""
    try:
        from .provider_resource_report import validate_snapshot

        snapshot = validate_snapshot(value)
    except (TypeError, ValueError) as exc:
        raise ProviderCapacityValidationError(
            "canonical snapshot validation failed"
        ) from exc
    evaluated, max_age = _freshness_inputs(evaluated_at, max_age_seconds)
    body = _canonical_observation_body(
        snapshot,
        evaluated_at=evaluated,
        max_age_seconds=max_age,
    )
    observation = {"observation_id": _digest_id(body), **body}
    return validate_capacity_observation(observation)


def build_capacity_bundle(
    observations: Iterable[Mapping[str, Any]],
    *,
    pools: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build deterministic, non-authoritative evidence from strict observations."""
    validated_observations = [
        validate_capacity_observation(item) for item in observations
    ]
    validated_observations.sort(key=lambda item: item["observation_id"])
    if len({item["observation_id"] for item in validated_observations}) != len(
        validated_observations
    ):
        raise ProviderCapacityValidationError("observation ids must be unique")

    resource_providers = {
        resource["resource_id"]: observation["provider_id"]
        for observation in validated_observations
        for resource in observation["resources"]
    }
    validated_pools = [
        _validate_pool(item, resource_providers) for item in pools
    ]
    validated_pools.sort(key=lambda item: item["pool_id"])
    if len({item["pool_id"] for item in validated_pools}) != len(validated_pools):
        raise ProviderCapacityValidationError("pool ids must be unique")

    producer_revisions = sorted(
        {
            (item["producer"]["id"], item["producer"]["revision"])
            for item in validated_observations
        }
    )
    producer_rows = [
        {"producer_id": producer_id, "revision": revision}
        for producer_id, revision in producer_revisions
    ]
    conflicts = _conflict_evidence(validated_observations)
    body = {
        "schema_version": 1,
        "contract": CAPACITY_BUNDLE_CONTRACT,
        "lineage": {
            "canonical_contract": CANONICAL_SNAPSHOT_CONTRACT,
            "canonical_contract_unchanged": True,
            "extension_role": "advisory_evidence_input",
            "canonical_runtime_state": False,
        },
        "read_only": True,
        "mutation_allowed": False,
        "scheduling_authoritative": False,
        "producer_revisions": producer_rows,
        "observations": validated_observations,
        "pools": validated_pools,
        "conflict_evidence": conflicts,
    }
    bundle = {
        "bundle_id": _digest_id(body),
        **body,
    }
    return validate_capacity_bundle(bundle)


def validate_capacity_bundle(value: object) -> dict[str, Any]:
    """Return a defensive copy of a valid bundle or raise."""
    item = _object("bundle", value)
    _exact_keys(
        "bundle",
        item,
        {
            "schema_version",
            "contract",
            "bundle_id",
            "lineage",
            "read_only",
            "mutation_allowed",
            "scheduling_authoritative",
            "producer_revisions",
            "observations",
            "pools",
            "conflict_evidence",
        },
    )
    _literal("bundle.schema_version", item["schema_version"], 1)
    _literal("bundle.contract", item["contract"], CAPACITY_BUNDLE_CONTRACT)
    _digest("bundle.bundle_id", item["bundle_id"])
    lineage = _object("bundle.lineage", item["lineage"])
    _exact_keys(
        "bundle.lineage",
        lineage,
        {
            "canonical_contract",
            "canonical_contract_unchanged",
            "extension_role",
            "canonical_runtime_state",
        },
    )
    _literal(
        "bundle.lineage.canonical_contract",
        lineage["canonical_contract"],
        CANONICAL_SNAPSHOT_CONTRACT,
    )
    _literal(
        "bundle.lineage.canonical_contract_unchanged",
        lineage["canonical_contract_unchanged"],
        True,
    )
    _literal(
        "bundle.lineage.extension_role",
        lineage["extension_role"],
        "advisory_evidence_input",
    )
    _literal(
        "bundle.lineage.canonical_runtime_state",
        lineage["canonical_runtime_state"],
        False,
    )
    _literal("bundle.read_only", item["read_only"], True)
    _literal("bundle.mutation_allowed", item["mutation_allowed"], False)
    _literal(
        "bundle.scheduling_authoritative",
        item["scheduling_authoritative"],
        False,
    )

    producers = _list("bundle.producer_revisions", item["producer_revisions"])
    producer_pairs: list[tuple[str, str]] = []
    for index, raw in enumerate(producers):
        producer = _object(f"bundle.producer_revisions[{index}]", raw)
        _exact_keys(
            f"bundle.producer_revisions[{index}]",
            producer,
            {"producer_id", "revision"},
        )
        producer_pairs.append(
            (
                _safe_id(
                    f"bundle.producer_revisions[{index}].producer_id",
                    producer["producer_id"],
                ),
                _safe_id(
                    f"bundle.producer_revisions[{index}].revision",
                    producer["revision"],
                ),
            )
        )
    if producer_pairs != sorted(set(producer_pairs)):
        raise ProviderCapacityValidationError(
            "bundle producer revisions must be sorted and unique"
        )

    observations = _list("bundle.observations", item["observations"])
    validated_observations = [
        validate_capacity_observation(raw) for raw in observations
    ]
    observation_ids = [entry["observation_id"] for entry in validated_observations]
    if observation_ids != sorted(set(observation_ids)):
        raise ProviderCapacityValidationError(
            "bundle observations must be sorted and unique"
        )
    expected_pairs = sorted(
        {
            (entry["producer"]["id"], entry["producer"]["revision"])
            for entry in validated_observations
        }
    )
    if producer_pairs != expected_pairs:
        raise ProviderCapacityValidationError(
            "bundle producer revisions do not match observations"
        )

    resource_providers = {
        resource["resource_id"]: observation["provider_id"]
        for observation in validated_observations
        for resource in observation["resources"]
    }
    pools = _list("bundle.pools", item["pools"])
    validated_pools = [
        _validate_pool(raw, resource_providers) for raw in pools
    ]
    pool_ids = [entry["pool_id"] for entry in validated_pools]
    if pool_ids != sorted(set(pool_ids)):
        raise ProviderCapacityValidationError(
            "bundle pools must be sorted and unique"
        )

    conflicts = _list("bundle.conflict_evidence", item["conflict_evidence"])
    validated_conflicts = [
        _validate_conflict(raw, observation_ids) for raw in conflicts
    ]
    conflict_ids = [entry["conflict_id"] for entry in validated_conflicts]
    if conflict_ids != sorted(set(conflict_ids)):
        raise ProviderCapacityValidationError(
            "bundle conflicts must be sorted and unique"
        )
    expected_conflicts = _conflict_evidence(validated_observations)
    if validated_conflicts != expected_conflicts:
        raise ProviderCapacityValidationError(
            "bundle conflict evidence does not match observations"
        )

    body = {key: item[key] for key in item if key != "bundle_id"}
    if item["bundle_id"] != _digest_id(body):
        raise ProviderCapacityValidationError("bundle id does not match content")
    _reject_sensitive_keys(item)
    return deepcopy(item)


def validate_capacity_observation(value: object) -> dict[str, Any]:
    """Return a defensive copy of one valid provider capacity observation."""
    item = _object("observation", value)
    _exact_keys(
        "observation",
        item,
        {
            "observation_id",
            "provider_id",
            "source_contract",
            "source_revision",
            "producer",
            "observation_scope",
            "observed_at",
            "timestamp_provenance",
            "acquisition_health",
            "freshness",
            "model_scope_revision",
            "canonical_snapshot",
            "resources",
        },
    )
    _digest("observation.observation_id", item["observation_id"])
    _safe_id("observation.provider_id", item["provider_id"])
    _enum(
        "observation.source_contract",
        item["source_contract"],
        {
            CODEX_SOURCE_CONTRACT,
            ANTIGRAVITY_SOURCE_CONTRACT,
            CANONICAL_SNAPSHOT_CONTRACT,
        },
    )
    _safe_id("observation.source_revision", item["source_revision"])
    producer = _object("observation.producer", item["producer"])
    _exact_keys("observation.producer", producer, {"id", "revision"})
    _safe_id("observation.producer.id", producer["id"])
    _safe_id("observation.producer.revision", producer["revision"])
    scope = _object("observation.observation_scope", item["observation_scope"])
    _exact_keys(
        "observation.observation_scope",
        scope,
        {"kind", "scope_id_status", "scope_id"},
    )
    _literal(
        "observation.observation_scope.kind",
        scope["kind"],
        "provider_control_plane",
    )
    _enum(
        "observation.observation_scope.scope_id_status",
        scope["scope_id_status"],
        {"identified", "unknown"},
    )
    if scope["scope_id_status"] == "identified":
        _safe_id("observation.observation_scope.scope_id", scope["scope_id"])
    elif scope["scope_id"] is not None:
        raise ProviderCapacityValidationError(
            "unknown observation scope must not contain an id"
        )
    observed_at = item["observed_at"]
    if observed_at is not None:
        _timestamp("observation.observed_at", observed_at)
    _safe_id(
        "observation.timestamp_provenance",
        item["timestamp_provenance"],
    )
    _enum(
        "observation.acquisition_health",
        item["acquisition_health"],
        {"healthy", "degraded", "unavailable"},
    )
    _validate_freshness(item["freshness"], observed_at)
    if item["model_scope_revision"] is not None:
        _safe_id(
            "observation.model_scope_revision",
            item["model_scope_revision"],
        )
    resources = _list("observation.resources", item["resources"])
    resource_ids: list[str] = []
    for index, raw in enumerate(resources):
        resource = _validate_resource(
            raw,
            provider_id=item["provider_id"],
            label=f"observation.resources[{index}]",
        )
        resource_ids.append(resource["resource_id"])
    if resource_ids != sorted(set(resource_ids)):
        raise ProviderCapacityValidationError(
            "observation resources must be sorted and unique"
        )
    if item["acquisition_health"] == "unavailable" and resources:
        raise ProviderCapacityValidationError(
            "unavailable acquisition must not expose resources"
        )
    if item["source_contract"] == CANONICAL_SNAPSHOT_CONTRACT:
        canonical = _validate_canonical_source(item["canonical_snapshot"])
        expected_body = _canonical_observation_body(
            canonical,
            evaluated_at=(
                _parse_timestamp(item["freshness"]["evaluated_at"])
                if item["freshness"]["evaluated_at"] is not None
                else None
            ),
            max_age_seconds=item["freshness"]["max_age_seconds"],
        )
        actual_body = {
            key: item[key] for key in item if key != "observation_id"
        }
        if actual_body != expected_body:
            raise ProviderCapacityValidationError(
                "canonical observation does not match validated snapshot projection"
            )
    elif (
        item["canonical_snapshot"] is not None
        or item["model_scope_revision"] is not None
    ):
        raise ProviderCapacityValidationError(
            "adapter observations must not carry canonical projection fields"
        )
    body = {key: item[key] for key in item if key != "observation_id"}
    if item["observation_id"] != _digest_id(body):
        raise ProviderCapacityValidationError(
            "observation id does not match content"
        )
    _reject_sensitive_keys(item)
    return deepcopy(item)


def _validate_canonical_source(value: object) -> dict[str, Any]:
    try:
        from .provider_resource_report import validate_snapshot

        return validate_snapshot(value)
    except (TypeError, ValueError) as exc:
        raise ProviderCapacityValidationError(
            "canonical snapshot validation failed"
        ) from exc


def _canonical_observation_body(
    snapshot: Mapping[str, Any],
    *,
    evaluated_at: datetime | None,
    max_age_seconds: float | None,
) -> dict[str, Any]:
    identity = snapshot["resource"]["quota_identity"]
    scope = snapshot["resource"].get("observation_scope")
    if (
        identity["status"] != "verified"
        or identity["source"] != "source_attested"
        or scope is None
    ):
        raise ProviderCapacityValidationError(
            "canonical projection requires source-attested identity and scope"
        )
    observed_values = {window["observed_at"] for window in snapshot["windows"]}
    provenance_values = {
        window["source"].get("timestamp_provenance")
        for window in snapshot["windows"]
    }
    if (
        not snapshot["windows"]
        or None in observed_values
        or len(observed_values) != 1
        or len(provenance_values) != 1
        or not provenance_values
        or next(iter(provenance_values))
        not in {"provider_observed_at", "client_event_at"}
    ):
        raise ProviderCapacityValidationError(
            "canonical projection requires one accepted event-time provenance"
        )
    observed_at = _normalize_timestamp(next(iter(observed_values)))
    timestamp_provenance = next(iter(provenance_values))
    projected_identity = {
        "status": "verified",
        "id": identity["id"],
        "opaque_id": None,
        "source": "source_attested",
        "confidence": "verified",
    }
    provider_id = snapshot["resource"]["provider_id"]
    resource_id = _digest_id(
        {
            "provider_id": provider_id,
            "quota_identity_key": _quota_identity_key(projected_identity),
        }
    )
    constraints = []
    for window in snapshot["windows"]:
        remaining = window["remaining"]
        remaining_status = remaining["status"]
        remaining_value = (
            float(remaining["value"])
            if remaining_status == "observed"
            else None
        )
        availability = (
            "unknown"
            if remaining_status != "observed"
            else "exhausted"
            if remaining_value == 0
            else "available"
        )
        reset = window["resets_at"]
        constraints.append(
            {
                "constraint_id": _digest_id(
                    {
                        "resource_id": resource_id,
                        "window_id": window["window_id"],
                    }
                ),
                "window_id": window["window_id"],
                "window_duration_seconds": float(
                    window["window_duration_seconds"]
                ),
                "remaining": {
                    "status": remaining_status,
                    "value": remaining_value,
                    "unit": remaining["unit"],
                    "provenance": (
                        (
                            "derived_complement_of_used_ratio"
                            if remaining["derivation"]
                            == "provider_used_percent_complement"
                            else "provider_reported"
                        )
                        if remaining_status == "observed"
                        else remaining_status
                    ),
                },
                "resets_at": {
                    "status": reset["status"],
                    "value": (
                        _normalize_timestamp(reset["value"])
                        if reset["value"] is not None
                        else None
                    ),
                    "relative_seconds": None,
                },
                "availability": availability,
                "source_field": window["source"]["field"],
            }
        )
    constraints.sort(key=lambda item: item["constraint_id"])
    resource = {
        "resource_id": resource_id,
        "quota_identity": projected_identity,
        "model_scope": {
            "status": "unknown",
            "model_ids": [],
        },
        "constraints": constraints,
    }
    return {
        "provider_id": provider_id,
        "source_contract": CANONICAL_SNAPSHOT_CONTRACT,
        "source_revision": snapshot["snapshot_id"],
        "producer": {
            "id": snapshot["producer"]["adapter_id"],
            "revision": snapshot["producer"]["adapter_version"],
        },
        "observation_scope": {
            "kind": "provider_control_plane",
            "scope_id_status": "identified",
            "scope_id": scope["scope_id"],
        },
        "observed_at": observed_at,
        "timestamp_provenance": timestamp_provenance,
        "acquisition_health": (
            "healthy" if not snapshot["diagnostics"] else "degraded"
        ),
        "freshness": _freshness(
            observed_at,
            evaluated_at,
            max_age_seconds,
        ),
        "model_scope_revision": None,
        "canonical_snapshot": deepcopy(snapshot),
        "resources": [resource],
    }


def _validate_resource(
    value: object,
    *,
    provider_id: str,
    label: str,
) -> dict[str, Any]:
    item = _object(label, value)
    _exact_keys(
        label,
        item,
        {"resource_id", "quota_identity", "model_scope", "constraints"},
    )
    _digest(f"{label}.resource_id", item["resource_id"])
    identity = _validate_quota_identity(
        item["quota_identity"], f"{label}.quota_identity"
    )
    expected_resource_id = _digest_id(
        {
            "provider_id": provider_id,
            "quota_identity_key": _quota_identity_key(identity),
        }
    )
    if item["resource_id"] != expected_resource_id:
        raise ProviderCapacityValidationError(
            f"{label}.resource_id does not match identity"
        )
    model_scope = _object(f"{label}.model_scope", item["model_scope"])
    _exact_keys(
        f"{label}.model_scope", model_scope, {"status", "model_ids"}
    )
    status = _enum(
        f"{label}.model_scope.status",
        model_scope["status"],
        {"identified", "unknown"},
    )
    model_ids = _list(
        f"{label}.model_scope.model_ids", model_scope["model_ids"]
    )
    checked_model_ids = [
        _safe_id(f"{label}.model_scope.model_ids[{index}]", model_id)
        for index, model_id in enumerate(model_ids)
    ]
    if checked_model_ids != sorted(set(checked_model_ids)):
        raise ProviderCapacityValidationError(
            f"{label}.model_scope.model_ids must be sorted and unique"
        )
    if status == "identified" and not checked_model_ids:
        raise ProviderCapacityValidationError(
            f"{label}.model_scope identified requires model ids"
        )
    if status == "unknown" and checked_model_ids:
        raise ProviderCapacityValidationError(
            f"{label}.model_scope unknown must not infer model ids"
        )
    constraints = _list(f"{label}.constraints", item["constraints"])
    constraint_ids: list[str] = []
    window_ids: list[str] = []
    for index, raw in enumerate(constraints):
        constraint = _validate_constraint(
            raw,
            resource_id=item["resource_id"],
            label=f"{label}.constraints[{index}]",
        )
        constraint_ids.append(constraint["constraint_id"])
        window_ids.append(constraint["window_id"])
    if constraint_ids != sorted(set(constraint_ids)):
        raise ProviderCapacityValidationError(
            f"{label}.constraints must be sorted and unique"
        )
    if len(window_ids) != len(set(window_ids)):
        raise ProviderCapacityValidationError(
            f"{label}.window ids must be unique"
        )
    return deepcopy(item)


def _validate_quota_identity(value: object, label: str) -> dict[str, Any]:
    item = _object(label, value)
    _exact_keys(
        label,
        item,
        {"status", "id", "opaque_id", "source", "confidence"},
    )
    status = _enum(
        f"{label}.status",
        item["status"],
        {"verified", "unknown", "unavailable"},
    )
    if status == "verified":
        _safe_id(f"{label}.id", item["id"])
        _literal(f"{label}.opaque_id", item["opaque_id"], None)
        _enum(
            f"{label}.source",
            item["source"],
            {"operator_verified", "source_attested"},
        )
        _literal(f"{label}.confidence", item["confidence"], "verified")
    elif status == "unknown":
        if item["id"] is not None:
            raise ProviderCapacityValidationError(
                f"{label}.unknown identity must not contain an id"
            )
        _safe_id(f"{label}.opaque_id", item["opaque_id"])
        _literal(
            f"{label}.source", item["source"], "source_reported_opaque_id"
        )
        _literal(f"{label}.confidence", item["confidence"], "unverified")
    else:
        _literal(f"{label}.id", item["id"], None)
        _literal(f"{label}.opaque_id", item["opaque_id"], None)
        _literal(f"{label}.source", item["source"], "unavailable")
        _literal(f"{label}.confidence", item["confidence"], "unavailable")
    return deepcopy(item)


def _validate_constraint(
    value: object,
    *,
    resource_id: str,
    label: str,
) -> dict[str, Any]:
    item = _object(label, value)
    _exact_keys(
        label,
        item,
        {
            "constraint_id",
            "window_id",
            "window_duration_seconds",
            "remaining",
            "resets_at",
            "availability",
            "source_field",
        },
    )
    _digest(f"{label}.constraint_id", item["constraint_id"])
    window_id = _safe_id(f"{label}.window_id", item["window_id"])
    expected_constraint_id = _digest_id(
        {"resource_id": resource_id, "window_id": window_id}
    )
    if item["constraint_id"] != expected_constraint_id:
        raise ProviderCapacityValidationError(
            f"{label}.constraint_id does not match resource/window"
        )
    duration = item["window_duration_seconds"]
    if duration is not None:
        _nonnegative_number(f"{label}.window_duration_seconds", duration)
        if duration == 0:
            raise ProviderCapacityValidationError(
                f"{label}.window_duration_seconds must be positive"
            )
    remaining = _object(f"{label}.remaining", item["remaining"])
    _exact_keys(
        f"{label}.remaining",
        remaining,
        {"status", "value", "unit", "provenance"},
    )
    remaining_status = _enum(
        f"{label}.remaining.status",
        remaining["status"],
        {"observed", "unknown", "unavailable"},
    )
    remaining_unit = (
        _enum(
            f"{label}.remaining.unit",
            remaining["unit"],
            {"ratio", "percent", "tokens", "credits", "requests"},
        )
        if remaining["unit"] is not None
        else None
    )
    _enum(
        f"{label}.remaining.provenance",
        remaining["provenance"],
        {
            "derived_complement_of_used_ratio",
            "provider_reported",
            "unknown",
            "unavailable",
        },
    )
    availability = _enum(
        f"{label}.availability",
        item["availability"],
        {"available", "constrained", "exhausted", "unknown"},
    )
    if remaining_status == "observed":
        if remaining_unit is None:
            raise ProviderCapacityValidationError(
                f"{label}.remaining observed requires a unit"
            )
        remaining_value = _nonnegative_number(
            f"{label}.remaining.value", remaining["value"]
        )
        if remaining_unit == "ratio" and remaining_value > 1:
            raise ProviderCapacityValidationError(
                f"{label}.remaining ratio must be between zero and one"
            )
        if remaining_unit == "percent" and remaining_value > 100:
            raise ProviderCapacityValidationError(
                f"{label}.remaining percent must be between zero and one hundred"
            )
        if remaining["provenance"] not in {
            "derived_complement_of_used_ratio",
            "provider_reported",
        }:
            raise ProviderCapacityValidationError(
                f"{label}.remaining observed requires observed provenance"
            )
        if remaining_value == 0 and availability != "exhausted":
            raise ProviderCapacityValidationError(
                f"{label}.zero remaining must be exhausted"
            )
        if remaining_value > 0 and availability not in {
            "available",
            "constrained",
        }:
            raise ProviderCapacityValidationError(
                f"{label}.positive remaining must be available or explicitly constrained"
            )
    else:
        if remaining["value"] is not None or availability != "unknown":
            raise ProviderCapacityValidationError(
                f"{label}.unknown remaining must keep availability unknown"
            )
        _literal(
            f"{label}.remaining.provenance",
            remaining["provenance"],
            remaining_status,
        )
    reset = _object(f"{label}.resets_at", item["resets_at"])
    _exact_keys(
        f"{label}.resets_at",
        reset,
        {"status", "value", "relative_seconds"},
    )
    reset_status = _enum(
        f"{label}.resets_at.status",
        reset["status"],
        {"observed", "not_applicable", "unknown", "unavailable"},
    )
    if reset_status == "observed":
        _timestamp(f"{label}.resets_at.value", reset["value"])
    elif reset["value"] is not None:
        raise ProviderCapacityValidationError(
            f"{label}.resets_at unknown value must be null"
        )
    if reset["relative_seconds"] is not None:
        seconds = reset["relative_seconds"]
        if (
            not isinstance(seconds, int)
            or isinstance(seconds, bool)
            or not 0 <= seconds <= 31_536_000
        ):
            raise ProviderCapacityValidationError(
                f"{label}.resets_at.relative_seconds is invalid"
            )
    _safe_id(f"{label}.source_field", item["source_field"])
    return deepcopy(item)


def _validate_freshness(value: object, observed_at: object) -> dict[str, Any]:
    item = _object("observation.freshness", value)
    _exact_keys(
        "observation.freshness",
        item,
        {"status", "evaluated_at", "max_age_seconds", "reason"},
    )
    status = _enum(
        "observation.freshness.status",
        item["status"],
        {"fresh", "stale", "unknown"},
    )
    if item["evaluated_at"] is not None:
        _timestamp("observation.freshness.evaluated_at", item["evaluated_at"])
    if item["max_age_seconds"] is not None:
        _nonnegative_number(
            "observation.freshness.max_age_seconds", item["max_age_seconds"]
        )
    reason = _enum(
        "observation.freshness.reason",
        item["reason"],
        {
            "within_max_age",
            "exceeds_max_age",
            "freshness_policy_unset",
            "evaluation_time_unavailable",
            "observation_time_unavailable",
            "observation_after_evaluation",
        },
    )
    if status in {"fresh", "stale"}:
        if (
            observed_at is None
            or item["evaluated_at"] is None
            or item["max_age_seconds"] is None
        ):
            raise ProviderCapacityValidationError(
                "known freshness requires observation, evaluation, and max age"
            )
        expected = _freshness(
            _normalize_timestamp(observed_at),
            _parse_timestamp(item["evaluated_at"]),
            float(item["max_age_seconds"]),
        )
        if item != expected:
            raise ProviderCapacityValidationError(
                "freshness status does not match timestamps"
            )
    elif reason in {"within_max_age", "exceeds_max_age"}:
        raise ProviderCapacityValidationError(
            "unknown freshness cannot use a known-age reason"
        )
    return deepcopy(item)


def _validate_pool(
    value: object,
    resource_providers: Mapping[str, str],
) -> dict[str, Any]:
    item = _object("pool", value)
    _exact_keys(
        "pool",
        item,
        {
            "pool_id",
            "provider_id",
            "resource_ids",
            "binding_status",
            "source_revision",
        },
    )
    _safe_id("pool.pool_id", item["pool_id"])
    provider_id = _safe_id("pool.provider_id", item["provider_id"])
    _safe_id("pool.source_revision", item["source_revision"])
    _literal("pool.binding_status", item["binding_status"], "explicit")
    refs = _list("pool.resource_ids", item["resource_ids"])
    checked = [
        _digest(f"pool.resource_ids[{index}]", ref)
        for index, ref in enumerate(refs)
    ]
    if not checked or checked != sorted(set(checked)):
        raise ProviderCapacityValidationError(
            "pool resource ids must be non-empty, sorted, and unique"
        )
    if not set(checked) <= set(resource_providers):
        raise ProviderCapacityValidationError(
            "pool references an unknown resource"
        )
    if any(resource_providers[resource_id] != provider_id for resource_id in checked):
        raise ProviderCapacityValidationError(
            "pool provider does not match referenced resources"
        )
    return deepcopy(item)


def _validate_conflict(
    value: object,
    observation_ids: list[str],
) -> dict[str, Any]:
    item = _object("conflict", value)
    _exact_keys(
        "conflict",
        item,
        {
            "conflict_id",
            "kind",
            "provider_id",
            "quota_identity_key",
            "window_id",
            "observation_ids",
            "producer_id",
            "revisions",
            "differing_fields",
        },
    )
    _digest("conflict.conflict_id", item["conflict_id"])
    kind = _enum(
        "conflict.kind",
        item["kind"],
        {
            "inconsistent_constraint_evidence",
            "producer_revision_conflict",
        },
    )
    refs = _list("conflict.observation_ids", item["observation_ids"])
    checked_refs = [
        _digest(f"conflict.observation_ids[{index}]", ref)
        for index, ref in enumerate(refs)
    ]
    if len(checked_refs) < 2 or checked_refs != sorted(set(checked_refs)):
        raise ProviderCapacityValidationError(
            "conflict observation ids must be sorted, unique, and plural"
        )
    if not set(checked_refs) <= set(observation_ids):
        raise ProviderCapacityValidationError(
            "conflict references an unknown observation"
        )
    revisions = _safe_id_list("conflict.revisions", item["revisions"])
    differing = _safe_id_list(
        "conflict.differing_fields", item["differing_fields"]
    )
    if kind == "inconsistent_constraint_evidence":
        _safe_id("conflict.provider_id", item["provider_id"])
        _safe_id(
            "conflict.quota_identity_key", item["quota_identity_key"]
        )
        _safe_id("conflict.window_id", item["window_id"])
        if item["producer_id"] is not None or revisions:
            raise ProviderCapacityValidationError(
                "constraint conflict must not contain producer revision fields"
            )
        if not differing:
            raise ProviderCapacityValidationError(
                "constraint conflict requires differing fields"
            )
    else:
        _safe_id("conflict.producer_id", item["producer_id"])
        if (
            item["provider_id"] is not None
            or item["quota_identity_key"] is not None
            or item["window_id"] is not None
            or differing
            or len(revisions) < 2
        ):
            raise ProviderCapacityValidationError(
                "producer revision conflict fields are inconsistent"
            )
    body = {key: item[key] for key in item if key != "conflict_id"}
    if item["conflict_id"] != _digest_id(body):
        raise ProviderCapacityValidationError(
            "conflict id does not match content"
        )
    return deepcopy(item)


def _codex_constraint(
    resource_id: str,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    remaining_status = window["status"]
    remaining_value = (
        float(window["remaining_ratio"])
        if remaining_status == "observed"
        else None
    )
    provenance = (
        "derived_complement_of_used_ratio"
        if remaining_status == "observed"
        else "unknown"
    )
    reset_value = window["resets_at"]
    return _constraint(
        resource_id=resource_id,
        window_id=window["window_id"],
        duration=window["window_duration_seconds"],
        remaining_status=remaining_status,
        remaining_value=remaining_value,
        remaining_provenance=provenance,
        reset_value=reset_value,
        reset_relative_seconds=None,
        source_field=f"resources.windows.{window['window_id']}",
    )


def _antigravity_constraint(
    resource_id: str,
    bucket: Mapping[str, Any],
) -> dict[str, Any]:
    reset = bucket["reset_time"]
    reset_value = _normalize_provider_reset(reset)
    return _constraint(
        resource_id=resource_id,
        window_id="provider-quota",
        duration=None,
        remaining_status="observed",
        remaining_value=float(bucket["remaining_fraction"]),
        remaining_provenance="provider_reported",
        reset_value=reset_value,
        reset_relative_seconds=bucket["reset_in_seconds"],
        source_field="buckets.remaining_fraction",
    )


def _constraint(
    *,
    resource_id: str,
    window_id: str,
    duration: object,
    remaining_status: str,
    remaining_value: float | None,
    remaining_provenance: str,
    reset_value: str | None,
    reset_relative_seconds: int | None,
    source_field: str,
) -> dict[str, Any]:
    availability = (
        "unknown"
        if remaining_status != "observed" or remaining_value is None
        else "exhausted"
        if remaining_value == 0
        else "available"
    )
    reset_status = "observed" if reset_value is not None else "unknown"
    return {
        "constraint_id": _digest_id(
            {"resource_id": resource_id, "window_id": window_id}
        ),
        "window_id": window_id,
        "window_duration_seconds": (
            float(duration) if duration is not None else None
        ),
        "remaining": {
            "status": remaining_status,
            "value": remaining_value,
            "unit": "ratio",
            "provenance": remaining_provenance,
        },
        "resets_at": {
            "status": reset_status,
            "value": reset_value,
            "relative_seconds": reset_relative_seconds,
        },
        "availability": availability,
        "source_field": source_field,
    }


def _quota_identity(value: object) -> dict[str, Any]:
    if value is None:
        return {
            "status": "unavailable",
            "id": None,
            "opaque_id": None,
            "source": "unavailable",
            "confidence": "unavailable",
        }
    return {
        "status": "unknown",
        "id": None,
        "opaque_id": value,
        "source": "source_reported_opaque_id",
        "confidence": "unverified",
    }


def _quota_identity_key(identity: Mapping[str, Any]) -> str:
    status = identity["status"]
    if status == "verified":
        return f"verified:{identity['id']}"
    if status == "unknown":
        return f"opaque:{identity['opaque_id']}"
    return "unavailable"


def _freshness_inputs(
    evaluated_at: datetime | str | None,
    max_age_seconds: int | float | None,
) -> tuple[datetime | None, float | None]:
    evaluated = (
        _parse_timestamp(evaluated_at) if evaluated_at is not None else None
    )
    if max_age_seconds is None:
        return evaluated, None
    return (
        evaluated,
        _nonnegative_number("max_age_seconds", max_age_seconds),
    )


def _freshness(
    observed_at: str | None,
    evaluated_at: datetime | None,
    max_age_seconds: float | None,
) -> dict[str, Any]:
    evaluated_value = (
        evaluated_at.isoformat() if evaluated_at is not None else None
    )
    if observed_at is None:
        return {
            "status": "unknown",
            "evaluated_at": evaluated_value,
            "max_age_seconds": max_age_seconds,
            "reason": "observation_time_unavailable",
        }
    if evaluated_at is None:
        return {
            "status": "unknown",
            "evaluated_at": None,
            "max_age_seconds": max_age_seconds,
            "reason": "evaluation_time_unavailable",
        }
    if max_age_seconds is None:
        return {
            "status": "unknown",
            "evaluated_at": evaluated_value,
            "max_age_seconds": None,
            "reason": "freshness_policy_unset",
        }
    observed = _parse_timestamp(observed_at)
    if observed > evaluated_at:
        return {
            "status": "unknown",
            "evaluated_at": evaluated_value,
            "max_age_seconds": max_age_seconds,
            "reason": "observation_after_evaluation",
        }
    status = (
        "fresh"
        if evaluated_at <= observed + timedelta(seconds=max_age_seconds)
        else "stale"
    )
    return {
        "status": status,
        "evaluated_at": evaluated_value,
        "max_age_seconds": max_age_seconds,
        "reason": "within_max_age" if status == "fresh" else "exceeds_max_age",
    }


def _conflict_evidence(
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    producers: dict[str, dict[str, list[str]]] = {}
    constraints: dict[
        tuple[str, str, str], list[tuple[str, str, dict[str, Any]]]
    ] = {}
    for observation in observations:
        producer = observation["producer"]
        producers.setdefault(producer["id"], {}).setdefault(
            producer["revision"], []
        ).append(observation["observation_id"])
        for resource in observation["resources"]:
            identity = resource["quota_identity"]
            identity_key = _quota_identity_key(identity)
            for constraint in resource["constraints"]:
                key = (
                    observation["provider_id"],
                    identity_key,
                    constraint["window_id"],
                )
                constraints.setdefault(key, []).append(
                    (
                        observation["observation_id"],
                        constraint["constraint_id"],
                        constraint,
                    )
                )

    for producer_id, revisions in sorted(producers.items()):
        if len(revisions) > 1:
            body = {
                "kind": "producer_revision_conflict",
                "provider_id": None,
                "quota_identity_key": None,
                "window_id": None,
                "observation_ids": sorted(
                    observation_id
                    for ids in revisions.values()
                    for observation_id in ids
                ),
                "producer_id": producer_id,
                "revisions": sorted(revisions),
                "differing_fields": [],
            }
            conflicts.append({"conflict_id": _digest_id(body), **body})

    compared_fields = (
        "window_duration_seconds",
        "remaining",
        "resets_at",
        "availability",
    )
    for (provider_id, identity_key, window_id), rows in sorted(
        constraints.items()
    ):
        if len(rows) < 2:
            continue
        differing = [
            field
            for field in compared_fields
            if len({_canonical(row[2][field]) for row in rows}) > 1
        ]
        if not differing:
            continue
        body = {
            "kind": "inconsistent_constraint_evidence",
            "provider_id": provider_id,
            "quota_identity_key": identity_key,
            "window_id": window_id,
            "observation_ids": sorted({row[0] for row in rows}),
            "producer_id": None,
            "revisions": [],
            "differing_fields": sorted(differing),
        }
        conflicts.append({"conflict_id": _digest_id(body), **body})
    return sorted(conflicts, key=lambda item: item["conflict_id"])


def _validate_codex_source(value: object) -> dict[str, Any]:
    item = _object("codex source", value)
    _exact_keys(
        "codex source",
        item,
        {
            "contract",
            "adapter_id",
            "read_only",
            "method",
            "status",
            "reason",
            "collected_at",
            "timestamp_provenance",
            "freshness_authority",
            "resources",
            "advisory_fallback",
        },
    )
    _literal("codex source.contract", item["contract"], CODEX_SOURCE_CONTRACT)
    _safe_id("codex source.adapter_id", item["adapter_id"])
    _literal("codex source.read_only", item["read_only"], True)
    _literal(
        "codex source.method", item["method"], "account/rateLimits/read"
    )
    _enum(
        "codex source.status",
        item["status"],
        {"observed", "unknown", "unavailable"},
    )
    if item["reason"] is not None:
        _safe_id("codex source.reason", item["reason"])
    _timestamp("codex source.collected_at", item["collected_at"])
    _literal(
        "codex source.timestamp_provenance",
        item["timestamp_provenance"],
        "adapter_response_received_at",
    )
    _literal(
        "codex source.freshness_authority",
        item["freshness_authority"],
        "advisory_only",
    )
    resources = _list("codex source.resources", item["resources"])
    for index, raw_resource in enumerate(resources):
        resource = _object(f"codex source.resources[{index}]", raw_resource)
        _exact_keys(
            f"codex source.resources[{index}]",
            resource,
            {"limit_id", "plan_type", "windows"},
        )
        if resource["limit_id"] is not None:
            _safe_id(
                f"codex source.resources[{index}].limit_id",
                resource["limit_id"],
            )
        if resource["plan_type"] is not None:
            _safe_id(
                f"codex source.resources[{index}].plan_type",
                resource["plan_type"],
            )
        windows = _list(
            f"codex source.resources[{index}].windows",
            resource["windows"],
        )
        seen_windows: set[str] = set()
        for window_index, raw_window in enumerate(windows):
            label = (
                f"codex source.resources[{index}].windows[{window_index}]"
            )
            window = _validate_codex_window(raw_window, label)
            if window["window_id"] in seen_windows:
                raise ProviderCapacityValidationError(
                    "codex source window ids must be unique"
                )
            seen_windows.add(window["window_id"])
    fallback = _object("codex source.advisory_fallback", item["advisory_fallback"])
    _exact_keys(
        "codex source.advisory_fallback", fallback, {"status", "windows"}
    )
    _enum(
        "codex source.advisory_fallback.status",
        fallback["status"],
        {"not_used", "unavailable", "fresh", "stale"},
    )
    for index, raw_window in enumerate(
        _list(
            "codex source.advisory_fallback.windows", fallback["windows"]
        )
    ):
        _validate_codex_window(
            raw_window,
            f"codex source.advisory_fallback.windows[{index}]",
        )
    _reject_sensitive_keys(item)
    return deepcopy(item)


def _validate_codex_window(value: object, label: str) -> dict[str, Any]:
    item = _object(label, value)
    _exact_keys(
        label,
        item,
        {
            "window_id",
            "status",
            "window_duration_seconds",
            "used_ratio",
            "remaining_ratio",
            "resets_at",
        },
    )
    _safe_id(f"{label}.window_id", item["window_id"])
    status = _enum(
        f"{label}.status", item["status"], {"observed", "unknown"}
    )
    if item["window_duration_seconds"] is not None:
        duration = _nonnegative_number(
            f"{label}.window_duration_seconds",
            item["window_duration_seconds"],
        )
        if duration == 0:
            raise ProviderCapacityValidationError(
                f"{label}.window_duration_seconds must be positive"
            )
    if status == "observed":
        used = _fraction(f"{label}.used_ratio", item["used_ratio"])
        remaining = _fraction(
            f"{label}.remaining_ratio", item["remaining_ratio"]
        )
        if not math.isclose(used + remaining, 1.0, abs_tol=1e-9):
            raise ProviderCapacityValidationError(
                f"{label} used and remaining ratios must be complements"
            )
    else:
        if item["used_ratio"] is not None or item["remaining_ratio"] is not None:
            raise ProviderCapacityValidationError(
                f"{label} unknown ratio fields must be null"
            )
    if item["resets_at"] is not None:
        _timestamp(f"{label}.resets_at", item["resets_at"])
    return deepcopy(item)


def _validate_antigravity_source(value: object) -> dict[str, Any]:
    item = _object("antigravity source", value)
    _exact_keys(
        "antigravity source",
        item,
        {
            "schema_version",
            "contract",
            "source_version",
            "field_presence",
            "format_fingerprint",
            "collected_at",
            "timestamp_provenance",
            "freshness_authority",
            "plan_tier",
            "buckets",
        },
    )
    _literal("antigravity source.schema_version", item["schema_version"], 1)
    _literal(
        "antigravity source.contract",
        item["contract"],
        ANTIGRAVITY_SOURCE_CONTRACT,
    )
    if item["source_version"] is not None:
        _safe_id(
            "antigravity source.source_version", item["source_version"]
        )
    if item["plan_tier"] is not None:
        _safe_id("antigravity source.plan_tier", item["plan_tier"])
    presence = _object(
        "antigravity source.field_presence", item["field_presence"]
    )
    _exact_keys(
        "antigravity source.field_presence",
        presence,
        {"version", "plan_tier", "quota"},
    )
    if not all(isinstance(entry, bool) for entry in presence.values()):
        raise ProviderCapacityValidationError(
            "antigravity source field presence must be boolean"
        )
    fingerprint = item["format_fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    ):
        raise ProviderCapacityValidationError(
            "antigravity source fingerprint is invalid"
        )
    _timestamp("antigravity source.collected_at", item["collected_at"])
    _literal(
        "antigravity source.timestamp_provenance",
        item["timestamp_provenance"],
        "statusline_callback_received_at",
    )
    _literal(
        "antigravity source.freshness_authority",
        item["freshness_authority"],
        "advisory_only",
    )
    buckets = _list("antigravity source.buckets", item["buckets"])
    bucket_ids: list[str] = []
    for index, raw in enumerate(buckets):
        bucket = _object(f"antigravity source.buckets[{index}]", raw)
        _exact_keys(
            f"antigravity source.buckets[{index}]",
            bucket,
            {
                "bucket_id",
                "remaining_fraction",
                "reset_time",
                "reset_in_seconds",
            },
        )
        bucket_ids.append(
            _safe_id(
                f"antigravity source.buckets[{index}].bucket_id",
                bucket["bucket_id"],
            )
        )
        _fraction(
            f"antigravity source.buckets[{index}].remaining_fraction",
            bucket["remaining_fraction"],
        )
        if bucket["reset_time"] is not None:
            _normalize_provider_reset(bucket["reset_time"])
        if bucket["reset_in_seconds"] is not None:
            seconds = bucket["reset_in_seconds"]
            if (
                not isinstance(seconds, int)
                or isinstance(seconds, bool)
                or not 0 <= seconds <= 31_536_000
            ):
                raise ProviderCapacityValidationError(
                    "antigravity reset_in_seconds is invalid"
                )
    if bucket_ids != sorted(set(bucket_ids)):
        raise ProviderCapacityValidationError(
            "antigravity buckets must be sorted and unique"
        )
    _reject_sensitive_keys(item)
    return deepcopy(item)


def _normalize_provider_reset(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ProviderCapacityValidationError(
                "provider reset timestamp is invalid"
            )
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            raise ProviderCapacityValidationError(
                "provider reset timestamp is invalid"
            ) from exc
    return _normalize_timestamp(value)


def _safe_id_list(label: str, value: object) -> list[str]:
    items = _list(label, value)
    checked = [
        _safe_id(f"{label}[{index}]", entry)
        for index, entry in enumerate(items)
    ]
    if checked != sorted(set(checked)):
        raise ProviderCapacityValidationError(
            f"{label} must be sorted and unique"
        )
    return checked


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = (
                key.lower().replace("-", "_")
                if isinstance(key, str)
                else ""
            )
            if normalized in _FORBIDDEN_KEYS:
                raise ProviderCapacityValidationError(
                    f"sensitive key rejected: {normalized}"
                )
            _reject_sensitive_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_keys(nested)


def _object(label: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderCapacityValidationError(f"{label} must be an object")
    return value


def _list(label: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ProviderCapacityValidationError(f"{label} must be a list")
    return value


def _exact_keys(label: str, value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ProviderCapacityValidationError(
            f"{label} fields do not match contract"
        )


def _literal(label: str, value: object, expected: object) -> None:
    if value != expected or type(value) is not type(expected):
        raise ProviderCapacityValidationError(
            f"{label} must equal {expected!r}"
        )


def _enum(label: str, value: object, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ProviderCapacityValidationError(
            f"{label} must be one of {sorted(choices)}"
        )
    return value


def _safe_id(label: str, value: object) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ProviderCapacityValidationError(f"{label} is not a safe id")
    return value


def _digest(label: str, value: object) -> str:
    if not isinstance(value, str) or not _SHA256_ID.fullmatch(value):
        raise ProviderCapacityValidationError(f"{label} is not a sha256 id")
    return value


def _nonnegative_number(label: str, value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ProviderCapacityValidationError(
            f"{label} must be a nonnegative finite number"
        )
    return float(value)


def _fraction(label: str, value: object) -> float:
    number = _nonnegative_number(label, value)
    if number > 1:
        raise ProviderCapacityValidationError(f"{label} must be within [0, 1]")
    return number


def _timestamp(label: str, value: object) -> datetime:
    try:
        return _parse_timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ProviderCapacityValidationError(
            f"{label} must be a timezone-aware timestamp"
        ) from exc


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("timestamp is not timezone-aware")
    return value.astimezone(timezone.utc)


def _normalize_timestamp(value: object) -> str:
    return _timestamp("timestamp", value).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _digest_id(value: object) -> str:
    return capacity_content_id(value)

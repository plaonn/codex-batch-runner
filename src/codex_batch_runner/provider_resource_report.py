from __future__ import annotations

import json
import math
import re
import subprocess
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .status_report import build_status_report
from .timeutil import utc_now

SNAPSHOT_CONTRACT = "provider-resource-snapshot-v1"
MAPPING_CONTRACT = "provider-resource-mapping-v1"
REPORT_CONTRACT = "provider-resource-report-v1"
MAX_INPUT_BYTES = 1_048_576
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
FORBIDDEN_KEYS = {
    "account", "account_label", "argv", "command", "credential", "credentials", "path",
    "prompt", "raw", "raw_output", "rollout_path", "session_id", "stderr", "stdout",
    "thread_id", "transcript",
}
REMAINING_STATUSES = {"observed", "unknown", "unavailable"}
RESET_STATUSES = {"observed", "not_applicable", "unknown", "unavailable"}
FRESHNESS_STATUSES = {"fresh", "stale_age", "stale_after_reset", "unknown"}
REMAINING_UNITS = {"percent", "tokens", "credits", "requests"}
DIAGNOSTIC_CODES = {
    "adapter_unavailable", "mapping_ambiguous", "mapping_missing", "mapping_stale", "mapping_target_unknown",
    "observation_time_invalid", "quota_identity_unknown", "remaining_out_of_range",
    "remaining_unknown", "reset_time_inconsistent", "reset_unknown",
    "resource_capability_unavailable", "snapshot_command_failed", "snapshot_command_timed_out",
    "snapshot_json_invalid", "snapshot_stale_after_reset", "snapshot_stale_age",
}


class ProviderResourceValidationError(ValueError):
    pass


def load_json_object(path: str | Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
        if len(raw) > MAX_INPUT_BYTES:
            raise ProviderResourceValidationError("input JSON exceeds the public-safe size limit")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderResourceValidationError("input JSON could not be read as an object") from exc
    if not isinstance(value, dict):
        raise ProviderResourceValidationError("input JSON must be an object")
    return value


def parse_resource_timestamp(value: object) -> datetime:
    return _timestamp("timestamp", value)


def validate_snapshot(value: object) -> dict[str, Any]:
    item = _object("snapshot", value)
    _keys("snapshot", item, {"schema_version", "contract", "snapshot_id", "generated_at", "producer", "resource", "windows", "diagnostics"})
    _literal("snapshot.schema_version", item.get("schema_version"), 1)
    _literal("snapshot.contract", item.get("contract"), SNAPSHOT_CONTRACT)
    _safe_id("snapshot.snapshot_id", item.get("snapshot_id"))
    generated_at = _timestamp("snapshot.generated_at", item.get("generated_at"))

    producer = _object("snapshot.producer", item.get("producer"))
    _keys("snapshot.producer", producer, {"adapter_id", "adapter_version", "observation_mode", "read_only"})
    _safe_id("snapshot.producer.adapter_id", producer.get("adapter_id"))
    _safe_id("snapshot.producer.adapter_version", producer.get("adapter_version"))
    _enum("snapshot.producer.observation_mode", producer.get("observation_mode"), {"cached_local", "provided_snapshot", "capability_projection"})
    _literal("snapshot.producer.read_only", producer.get("read_only"), True)

    resource = _object("snapshot.resource", item.get("resource"))
    _keys("snapshot.resource", resource, {"provider_id", "quota_identity"})
    _safe_id("snapshot.resource.provider_id", resource.get("provider_id"))
    identity = _object("snapshot.resource.quota_identity", resource.get("quota_identity"))
    _keys("snapshot.resource.quota_identity", identity, {"status", "id", "source", "confidence"})
    identity_status = _enum("snapshot.resource.quota_identity.status", identity.get("status"), {"verified", "unknown", "unavailable"})
    if identity_status == "verified":
        _safe_id("snapshot.resource.quota_identity.id", identity.get("id"))
        if identity.get("source") != "operator_verified" or identity.get("confidence") != "verified":
            raise ProviderResourceValidationError("verified quota identity requires operator-verified provenance")
    elif identity.get("id") is not None:
        raise ProviderResourceValidationError("unverified quota identity must not contain an id")
    elif identity_status == "unknown" and (
        identity.get("source") != "source_reported_opaque_id" or identity.get("confidence") != "unverified"
    ):
        raise ProviderResourceValidationError("unknown quota identity requires unverified source provenance")
    elif identity_status == "unavailable" and (
        identity.get("source") != "unavailable" or identity.get("confidence") != "unavailable"
    ):
        raise ProviderResourceValidationError("unavailable quota identity requires unavailable provenance")
    _enum("snapshot.resource.quota_identity.source", identity.get("source"), {"operator_verified", "source_reported_opaque_id", "unavailable"})
    _enum("snapshot.resource.quota_identity.confidence", identity.get("confidence"), {"verified", "unverified", "unavailable"})

    windows = item.get("windows")
    if not isinstance(windows, list):
        raise ProviderResourceValidationError("snapshot.windows must be a list")
    seen: set[str] = set()
    for index, raw_window in enumerate(windows):
        window = _object(f"snapshot.windows[{index}]", raw_window)
        _keys(f"snapshot.windows[{index}]", window, {"window_id", "window_duration_seconds", "availability", "remaining", "resets_at", "observed_at", "freshness", "source"})
        window_id = _safe_id(f"snapshot.windows[{index}].window_id", window.get("window_id"))
        if window_id in seen:
            raise ProviderResourceValidationError("snapshot window ids must be unique")
        seen.add(window_id)
        _positive_number(f"snapshot.windows[{index}].window_duration_seconds", window.get("window_duration_seconds"))
        availability = _enum(f"snapshot.windows[{index}].availability", window.get("availability"), {"observed", "unknown", "unavailable"})
        _remaining(f"snapshot.windows[{index}].remaining", window.get("remaining"))
        reset = _reset(f"snapshot.windows[{index}].resets_at", window.get("resets_at"))
        if availability == "unavailable" and (
            window["remaining"]["status"] != "unavailable" or reset[0] != "unavailable"
        ):
            raise ProviderResourceValidationError("unavailable window fields must remain unavailable")
        if availability == "unknown" and (
            window["remaining"]["status"] != "unknown" or reset[0] != "unknown"
        ):
            raise ProviderResourceValidationError("unknown window fields must remain unknown")
        observed_at = window.get("observed_at")
        if observed_at is not None:
            observed_at = _timestamp(f"snapshot.windows[{index}].observed_at", observed_at)
            if observed_at > generated_at + timedelta(seconds=60):
                raise ProviderResourceValidationError("observation_time_invalid")
        freshness = _object(f"snapshot.windows[{index}].freshness", window.get("freshness"))
        _keys(f"snapshot.windows[{index}].freshness", freshness, {"status", "evaluated_at", "max_age_seconds", "expires_at", "reason"})
        _enum(f"snapshot.windows[{index}].freshness.status", freshness.get("status"), FRESHNESS_STATUSES)
        _timestamp(f"snapshot.windows[{index}].freshness.evaluated_at", freshness.get("evaluated_at"))
        if freshness.get("max_age_seconds") is not None:
            _positive_number(f"snapshot.windows[{index}].freshness.max_age_seconds", freshness.get("max_age_seconds"), allow_zero=True)
        if freshness.get("expires_at") is not None:
            _timestamp(f"snapshot.windows[{index}].freshness.expires_at", freshness.get("expires_at"))
        _safe_id(f"snapshot.windows[{index}].freshness.reason", freshness.get("reason"))
        source = _object(f"snapshot.windows[{index}].source", window.get("source"))
        _keys(f"snapshot.windows[{index}].source", source, {"kind", "field", "confidence"})
        _enum(f"snapshot.windows[{index}].source.kind", source.get("kind"), {"local_cached_event", "provided_snapshot", "capability_projection"})
        _safe_id(f"snapshot.windows[{index}].source.field", source.get("field"))
        _enum(f"snapshot.windows[{index}].source.confidence", source.get("confidence"), {"verified_source_timestamp", "experimental_observed_shape", "source_file_mtime", "unavailable"})
        if reset[0] == "observed" and observed_at and reset[1] and observed_at > reset[1]:
            raise ProviderResourceValidationError("reset_time_inconsistent")

    diagnostics = item.get("diagnostics")
    if not isinstance(diagnostics, list):
        raise ProviderResourceValidationError("snapshot.diagnostics must be a list")
    for index, diagnostic in enumerate(diagnostics):
        entry = _object(f"snapshot.diagnostics[{index}]", diagnostic)
        _keys(f"snapshot.diagnostics[{index}]", entry, {"code", "scope"})
        _enum(f"snapshot.diagnostics[{index}].code", entry.get("code"), DIAGNOSTIC_CODES)
        if entry.get("scope") is not None:
            _safe_id(f"snapshot.diagnostics[{index}].scope", entry.get("scope"))
    _reject_sensitive_keys(item)
    return deepcopy(item)


def validate_mapping(value: object) -> dict[str, Any]:
    item = _object("mapping", value)
    _keys("mapping", item, {"schema_version", "contract", "mapping_revision", "status", "bindings"})
    _literal("mapping.schema_version", item.get("schema_version"), 1)
    _literal("mapping.contract", item.get("contract"), MAPPING_CONTRACT)
    _safe_id("mapping.mapping_revision", item.get("mapping_revision"))
    _enum("mapping.status", item.get("status"), {"current", "stale"})
    bindings = item.get("bindings")
    if not isinstance(bindings, list):
        raise ProviderResourceValidationError("mapping.bindings must be a list")
    seen_binding_ids: set[str] = set()
    for index, raw_binding in enumerate(bindings):
        binding = _object(f"mapping.bindings[{index}]", raw_binding)
        _keys(f"mapping.bindings[{index}]", binding, {"binding_id", "target_id", "capacity_pool", "provider_id", "quota_identity_id", "source", "verified_at", "expires_at"})
        binding_id = _safe_id(f"mapping.bindings[{index}].binding_id", binding.get("binding_id"))
        if binding_id in seen_binding_ids:
            raise ProviderResourceValidationError("mapping binding ids must be unique")
        seen_binding_ids.add(binding_id)
        for key in ("target_id", "capacity_pool", "provider_id", "quota_identity_id"):
            _safe_id(f"mapping.bindings[{index}].{key}", binding.get(key))
        _literal(f"mapping.bindings[{index}].source", binding.get("source"), "operator_verified")
        verified_at = _timestamp(f"mapping.bindings[{index}].verified_at", binding.get("verified_at"))
        expires_at = _timestamp(f"mapping.bindings[{index}].expires_at", binding.get("expires_at"))
        if expires_at <= verified_at:
            raise ProviderResourceValidationError("mapping expiry must be after verification")
    _reject_sensitive_keys(item)
    return deepcopy(item)


def run_snapshot_adapter(argv: list[str], *, timeout_seconds: int) -> dict[str, Any]:
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
        return _adapter_result("invalid", "snapshot_command_failed")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0 or timeout_seconds > 60:
        return _adapter_result("invalid", "snapshot_command_failed")
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return _adapter_result("unavailable", "snapshot_command_timed_out")
    except OSError:
        return _adapter_result("unavailable", "snapshot_command_failed")
    if result.returncode != 0:
        return _adapter_result("unavailable", "snapshot_command_failed")
    if len(result.stdout) > MAX_INPUT_BYTES:
        return _adapter_result("invalid", "snapshot_json_invalid")
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return _adapter_result("invalid", "snapshot_json_invalid")
    if not isinstance(value, dict):
        return _adapter_result("invalid", "snapshot_json_invalid")
    return {"status": "observed", "reason": None, "value": value}


def project_native_codex_cached_rollout(value: object, *, snapshot_id: str = "native-codex-cached", generated_at: datetime | None = None) -> dict[str, Any]:
    source = _object("native_codex_cached", value)
    baseline = generated_at or utc_now()
    available = source.get("available") is True
    observed_raw = source.get("observed_at")
    observed = _strict_timestamp_or_none(observed_raw)
    windows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    for slot in ("primary", "secondary"):
        raw = source.get(slot)
        if not isinstance(raw, dict):
            continue
        duration = _finite(raw.get("window_minutes"))
        if duration is None or duration <= 0:
            diagnostics.append({"code": "snapshot_json_invalid", "scope": slot})
            continue
        remaining = _finite(raw.get("remaining_percent"))
        reset_raw = raw.get("resets_at_iso") or raw.get("resets_at")
        reset = _strict_timestamp_or_none(reset_raw)
        availability = "observed" if available else "unavailable"
        remaining_entry: dict[str, Any] = {"status": "unavailable", "value": None, "unit": None, "derivation": "unavailable"}
        if available and remaining is not None and 0 <= remaining <= 100:
            remaining_entry = {"status": "observed", "value": remaining, "unit": "percent", "derivation": "provider_used_percent_complement"}
        elif available:
            remaining_entry = {"status": "unknown", "value": None, "unit": None, "derivation": "unavailable"}
            diagnostics.append({"code": "remaining_out_of_range" if remaining is not None else "remaining_unknown", "scope": slot})
        if available and reset_raw is not None and reset is None:
            diagnostics.append({"code": "reset_unknown", "scope": slot})
        windows.append({
            "window_id": slot,
            "window_duration_seconds": duration * 60,
            "availability": availability,
            "remaining": remaining_entry,
            "resets_at": {"status": "observed", "value": reset.isoformat()} if reset else {"status": "unknown" if available else "unavailable", "value": None},
            "observed_at": observed.isoformat() if observed else None,
            "freshness": {"status": "unknown", "evaluated_at": baseline.isoformat(), "max_age_seconds": None, "expires_at": None, "reason": "freshness_policy_unset"},
            "source": {"kind": "local_cached_event", "field": f"rate_limits.{slot}", "confidence": "source_file_mtime"},
        })
    if not available:
        diagnostics.append({"code": "adapter_unavailable", "scope": "native-codex-rollout"})
    if available and observed is None:
        diagnostics.append({"code": "observation_time_invalid", "scope": "native-codex-rollout"})
    if available and not windows:
        diagnostics.append({"code": "remaining_unknown", "scope": "native-codex-rollout"})
    snapshot = {
        "schema_version": 1,
        "contract": SNAPSHOT_CONTRACT,
        "snapshot_id": snapshot_id,
        "generated_at": baseline.isoformat(),
        "producer": {"adapter_id": "native-codex-rollout", "adapter_version": "experimental-v1", "observation_mode": "cached_local", "read_only": True},
        "resource": {"provider_id": "codex", "quota_identity": {"status": "unknown", "id": None, "source": "source_reported_opaque_id", "confidence": "unverified"}},
        "windows": windows,
        "diagnostics": diagnostics,
    }
    return validate_snapshot(snapshot)


def antigravity_unavailable_snapshot(*, generated_at: datetime | None = None) -> dict[str, Any]:
    baseline = generated_at or utc_now()
    snapshot = {
        "schema_version": 1, "contract": SNAPSHOT_CONTRACT, "snapshot_id": "antigravity-unavailable",
        "generated_at": baseline.isoformat(),
        "producer": {"adapter_id": "antigravity", "adapter_version": "capability-v1", "observation_mode": "capability_projection", "read_only": True},
        "resource": {"provider_id": "antigravity", "quota_identity": {"status": "unavailable", "id": None, "source": "unavailable", "confidence": "unavailable"}},
        "windows": [],
        "diagnostics": [{"code": "resource_capability_unavailable", "scope": "antigravity"}],
    }
    return validate_snapshot(snapshot)


def evaluate_snapshot_freshness(snapshot: dict[str, Any], *, evaluated_at: datetime, max_age_seconds: int | None) -> dict[str, Any]:
    result = deepcopy(snapshot)
    for window in result["windows"]:
        observed = _strict_timestamp_or_none(window.get("observed_at"))
        reset = _strict_timestamp_or_none(window.get("resets_at", {}).get("value"))
        if observed is None or max_age_seconds is None:
            status, reason, expires = "unknown", "freshness_policy_unset" if max_age_seconds is None else "observation_time_invalid", None
        elif observed > evaluated_at + timedelta(seconds=60):
            status, reason, expires = "unknown", "observation_time_invalid", None
        elif reset is not None and reset <= evaluated_at and observed <= reset:
            status, reason, expires = "stale_after_reset", "snapshot_stale_after_reset", observed + timedelta(seconds=max_age_seconds)
        elif evaluated_at > observed + timedelta(seconds=max_age_seconds):
            status, reason, expires = "stale_age", "snapshot_stale_age", observed + timedelta(seconds=max_age_seconds)
        else:
            status, reason, expires = "fresh", "within_max_age", observed + timedelta(seconds=max_age_seconds)
        window["freshness"] = {
            "status": status, "evaluated_at": evaluated_at.isoformat(), "max_age_seconds": max_age_seconds,
            "expires_at": expires.isoformat() if expires else None, "reason": reason,
        }
    return result


def mapping_preview(mapping: dict[str, Any] | None, inventory: dict[str, Any], *, evaluated_at: datetime) -> dict[str, Any]:
    target_values = inventory.get("targets") if isinstance(inventory, dict) and isinstance(inventory.get("targets"), dict) else {}
    inventory_current = isinstance(inventory, dict) and inventory.get("status") == "current"
    target_ids = sorted(str(target_id) for target_id in target_values)
    by_target: dict[str, list[dict[str, Any]]] = {}
    if mapping:
        for binding in mapping["bindings"]:
            by_target.setdefault(binding["target_id"], []).append(binding)
    rows = []
    for target_id in sorted(set(target_ids) | set(by_target)):
        bindings = by_target.get(target_id, [])
        active = [
            binding for binding in bindings
            if _strict_timestamp_or_none(binding["verified_at"]) <= evaluated_at
            < _strict_timestamp_or_none(binding["expires_at"])
        ]
        identities = {(binding["provider_id"], binding["quota_identity_id"]) for binding in active}
        if target_id in target_values and not inventory_current:
            status, reason = "stale", "mapping_stale"
        elif bindings and target_id not in target_values:
            status, reason = "invalid", "mapping_target_unknown"
        elif not mapping or not bindings:
            status, reason = "missing", "mapping_missing"
        elif mapping["status"] != "current" or not active:
            status, reason = "stale", "mapping_stale"
        elif len(active) != 1 or len(identities) != 1:
            status, reason = "ambiguous", "mapping_ambiguous"
        else:
            status, reason = "mapped", None
        binding = active[0] if status == "mapped" else None
        rows.append({
            "target_id": target_id, "status": status, "reason": reason,
            "provider_id": binding["provider_id"] if binding else None,
            "quota_identity_id": binding["quota_identity_id"] if binding else None,
            "capacity_pool": binding["capacity_pool"] if binding else None,
            "resource_aware_candidate": False,
        })
    pool_groups: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        if row["status"] == "mapped":
            pool_groups.setdefault(row["capacity_pool"], set()).add((row["provider_id"], row["quota_identity_id"]))
    pool_projection = [
        {"capacity_pool": pool, "status": "shared_identity" if len(identities) == 1 else "multiple_quota_identities", "quota_identity_count": len(identities), "provider_resource_summary_allowed": len(identities) == 1}
        for pool, identities in sorted(pool_groups.items())
    ]
    return {
        "mapping_revision": mapping.get("mapping_revision") if mapping else None,
        "target_inventory_snapshot_id": inventory.get("snapshot_id") if isinstance(inventory, dict) else None,
        "target_inventory_status": inventory.get("status") if isinstance(inventory, dict) else None,
        "targets": rows,
        "pool_projection": pool_projection,
    }


def build_provider_resource_report(config: Config, *, snapshots: list[dict[str, Any]], mapping: dict[str, Any] | None = None, evaluated_at: datetime | None = None, max_age_seconds: int | None = None, adapter_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    baseline = evaluated_at or utc_now()
    if max_age_seconds is not None and (
        isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds < 0
    ):
        raise ProviderResourceValidationError("max_age_seconds must be a non-negative integer")
    evaluated = []
    snapshot_ids: set[str] = set()
    for item in snapshots:
        validated = validate_snapshot(item)
        if validated["snapshot_id"] in snapshot_ids:
            raise ProviderResourceValidationError("snapshot ids must be unique")
        snapshot_ids.add(validated["snapshot_id"])
        if _timestamp("snapshot.generated_at", validated["generated_at"]) > baseline + timedelta(seconds=60):
            raise ProviderResourceValidationError("snapshot generated_at is in the future")
        evaluated.append(evaluate_snapshot_freshness(
            validated,
            evaluated_at=baseline,
            max_age_seconds=max_age_seconds,
        ))
    validated_mapping = validate_mapping(mapping) if mapping is not None else None
    preview = mapping_preview(validated_mapping, config.execution_target_inventory, evaluated_at=baseline)
    snapshot_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for snapshot in evaluated:
        identity = snapshot["resource"]["quota_identity"]
        if identity.get("status") == "verified":
            snapshot_index.setdefault((snapshot["resource"]["provider_id"], identity["id"]), []).append(snapshot)
    for row in preview["targets"]:
        matching = snapshot_index.get((row["provider_id"], row["quota_identity_id"]), [])
        snapshot = matching[0] if len(matching) == 1 else None
        row["resource_aware_candidate"] = bool(
            row["status"] == "mapped" and snapshot and any(
                window["availability"] == "observed" and window["freshness"]["status"] == "fresh"
                and window["remaining"]["status"] == "observed"
                for window in snapshot["windows"]
            )
        )
    sanitized_adapter_results = [_validate_adapter_result(item) for item in list(adapter_results or [])]
    local_status = build_status_report(config)
    capacity = local_status.get("queue", {}).get("capacity", {})
    return {
        "schema_version": 1, "contract": REPORT_CONTRACT, "generated_at": baseline.isoformat(),
        "read_only": True, "mutation_allowed": False, "scheduling_authoritative": False,
        "local_capacity": {
            "kind": "local_scheduler_admission", "provider_quota": False,
            "max_total_running": capacity.get("max_total_running"),
            "max_running_per_project": capacity.get("max_running_per_project"),
            "capacity_pools": capacity.get("capacity_pools", {}),
        },
        "provider_resources": evaluated,
        "adapter_results": sanitized_adapter_results,
        "mapping_preview": preview,
        "summary": {
            "snapshot_count": len(evaluated),
            "fresh_window_count": sum(1 for snapshot in evaluated for window in snapshot["windows"] if window["freshness"]["status"] == "fresh"),
            "resource_aware_candidate_count": sum(1 for row in preview["targets"] if row["resource_aware_candidate"]),
            "unavailable_count": sum(1 for snapshot in evaluated if any(item["code"] in {"adapter_unavailable", "resource_capability_unavailable"} for item in snapshot["diagnostics"])),
            "ambiguous_resource_count": sum(1 for items in snapshot_index.values() if len(items) > 1),
        },
    }


def render_provider_resource_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# provider resource report", "", "read_only: yes", "scheduling_authoritative: no", "",
        "local capacity (scheduler admission; not provider quota)",
    ]
    pools = report["local_capacity"].get("capacity_pools", {})
    if pools:
        for name, pool in sorted(pools.items()):
            lines.append(f"  {name}: running={pool.get('running', 0)} remaining={pool.get('remaining', 0)} max={pool.get('max_running', 0)}")
    else:
        lines.append("  none")
    lines.extend(["", "provider resources"])
    if not report["provider_resources"]:
        lines.append("  none")
    for snapshot in report["provider_resources"]:
        resource = snapshot["resource"]
        lines.append(f"  {resource['provider_id']}: identity={resource['quota_identity']['status']} windows={len(snapshot['windows'])}")
        for window in snapshot["windows"]:
            remaining = window["remaining"]
            value = f"{remaining['value']} {remaining['unit']}" if remaining["status"] == "observed" else remaining["status"]
            lines.append(f"    {window['window_id']}: remaining={value} freshness={window['freshness']['status']} reset={window['resets_at']['status']}")
        for diagnostic in snapshot["diagnostics"]:
            lines.append(f"    diagnostic={diagnostic['code']}")
    if report["adapter_results"]:
        lines.extend(["", "adapter results"])
        for result in report["adapter_results"]:
            lines.append(f"  status={result['status']} reason={result['reason']}")
    lines.extend(["", "mapping preview"])
    targets = report["mapping_preview"]["targets"]
    if not targets:
        lines.append("  none")
    for row in targets:
        lines.append(f"  {row['target_id']}: {row['status']} candidate={'yes' if row['resource_aware_candidate'] else 'no'} reason={row['reason'] or '-'}")
    lines.extend(["", f"summary: snapshots={summary['snapshot_count']} fresh_windows={summary['fresh_window_count']} candidates={summary['resource_aware_candidate_count']} unavailable={summary['unavailable_count']} ambiguous_resources={summary['ambiguous_resource_count']}"])
    return "\n".join(lines) + "\n"


def _remaining(key: str, value: object) -> None:
    item = _object(key, value)
    _keys(key, item, {"status", "value", "unit", "derivation"})
    status = _enum(f"{key}.status", item.get("status"), REMAINING_STATUSES)
    if status == "observed":
        number = _finite(item.get("value"))
        if number is None or number < 0:
            raise ProviderResourceValidationError("remaining_out_of_range")
        unit = _enum(f"{key}.unit", item.get("unit"), REMAINING_UNITS)
        if unit == "percent" and number > 100:
            raise ProviderResourceValidationError("remaining_out_of_range")
        _enum(f"{key}.derivation", item.get("derivation"), {"provider_reported", "provider_used_percent_complement"})
    elif item.get("value") is not None or item.get("unit") is not None:
        raise ProviderResourceValidationError("unobserved remaining must not contain a value or unit")
    else:
        _literal(f"{key}.derivation", item.get("derivation"), "unavailable")


def _reset(key: str, value: object) -> tuple[str, datetime | None]:
    item = _object(key, value)
    _keys(key, item, {"status", "value"})
    status = _enum(f"{key}.status", item.get("status"), RESET_STATUSES)
    if status == "observed":
        return status, _timestamp(f"{key}.value", item.get("value"))
    if item.get("value") is not None:
        raise ProviderResourceValidationError("unobserved reset must not contain a value")
    return status, None


def _adapter_result(status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "reason": reason, "value": None}


def _validate_adapter_result(value: object) -> dict[str, Any]:
    item = _object("adapter_result", value)
    _keys("adapter_result", item, {"status", "reason", "value"})
    _enum("adapter_result.status", item.get("status"), {"unknown", "invalid", "unavailable"})
    _enum("adapter_result.reason", item.get("reason"), DIAGNOSTIC_CODES)
    if item.get("value") is not None:
        raise ProviderResourceValidationError("adapter failure result must not contain raw value")
    return deepcopy(item)


def _object(key: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderResourceValidationError(f"{key} must be an object")
    return value


def _keys(key: str, value: dict[str, Any], allowed: set[str]) -> None:
    missing = allowed - set(value)
    extra = set(value) - allowed
    if missing or extra:
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
        raise ProviderResourceValidationError(f"{key} must be a public-safe opaque identifier")
    return value


def _timestamp(key: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ProviderResourceValidationError(f"{key} must be a timezone-aware timestamp")
    parsed = _strict_timestamp_or_none(value)
    if parsed is None:
        raise ProviderResourceValidationError(f"{key} must be a timezone-aware timestamp")
    return parsed


def _strict_timestamp_or_none(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _positive_number(key: str, value: object, *, allow_zero: bool = False) -> float:
    result = _finite(value)
    if result is None or result < 0 or (not allow_zero and result == 0):
        raise ProviderResourceValidationError(f"{key} must be a finite positive number")
    return result


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ProviderResourceValidationError("sensitive or raw fields are forbidden")
            _reject_sensitive_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_keys(item)

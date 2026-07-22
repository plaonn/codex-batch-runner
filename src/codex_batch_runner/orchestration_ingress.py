"""CBR-owned local ingress and durable D3 shadow reconciliation state."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .fs import read_json, write_json_atomic, write_json_atomic_create
from .lock import FileLock
from .orchestration import orchestration_request_fingerprint, validate_manifest
from .orchestration_dispatch import identity_for, validate_execution_envelope
from .orchestration_guard import (
    build_reconciliation_shadow,
    guard_idempotency_key,
    policy_fingerprint,
    trigger_id_for,
    validate_guard_policy,
    validate_guard_trigger,
)


BUNDLE_CONTRACT = "orchestration-local-ingress-v1"
PUBLISH_CONTRACT = "orchestration-local-ingress-publish-v1"
RECONCILIATION_CONTRACT = "orchestration-local-ingress-reconciliation-v1"
STATE_CONTRACT = "orchestration-reconciliation-state-v1"
SOURCE_ID = "cbr-local-operator-ingress"
PRODUCER_REVISION = "cbr-local-ingress-v1"
MAX_INPUT_BYTES = 384 * 1024
MAX_TTL = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)
BUNDLE_KEYS = {
    "schema_version",
    "contract",
    "producer",
    "source_event_id",
    "explicit_opt_in",
    "created_at",
    "expires_at",
    "policy",
    "manifest",
    "execution_envelope",
}
PRODUCER_KEYS = {"source_id", "revision"}
STATE_KEYS = {
    "schema_version",
    "contract",
    "source_id",
    "producer_revision",
    "source_event_id",
    "trigger_id",
    "bundle_fingerprint",
    "phase",
    "decision_status",
    "reason_codes",
    "lifecycle",
    "first_observed_at",
    "last_observed_at",
    "observation_count",
}
LIFECYCLE_KEYS = {
    "queue_admission",
    "execution",
    "review",
    "apply",
    "attention_delivery",
    "attention_acknowledgement",
    "attention_state_health",
    "source_disposition",
}


class LocalIngressError(ValueError):
    pass


class LocalIngressLockBusy(RuntimeError):
    pass


def load_local_ingress_bundle(path: str | Path) -> dict[str, Any]:
    input_path = Path(path)
    try:
        info = input_path.lstat()
    except OSError as exc:
        raise LocalIngressError("ingress_input_unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LocalIngressError("ingress_input_identity_invalid")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise LocalIngressError("ingress_input_permissions_invalid")
    if info.st_size > MAX_INPUT_BYTES:
        raise LocalIngressError("ingress_input_too_large")
    try:
        raw = input_path.read_bytes()
    except OSError as exc:
        raise LocalIngressError("ingress_input_unreadable") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise LocalIngressError("ingress_input_not_utf8") from exc
    except json.JSONDecodeError as exc:
        raise LocalIngressError("ingress_input_json_invalid") from exc
    return validate_local_ingress_bundle(value)


def validate_local_ingress_bundle(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LocalIngressError("ingress_input_not_object")
    if (
        set(value) != BUNDLE_KEYS
        or value.get("schema_version") != 1
        or value.get("contract") != BUNDLE_CONTRACT
    ):
        raise LocalIngressError("ingress_fields_invalid")
    producer = value.get("producer")
    if not isinstance(producer, dict) or set(producer) != PRODUCER_KEYS:
        raise LocalIngressError("ingress_fields_invalid")
    if producer != {"source_id": SOURCE_ID, "revision": PRODUCER_REVISION}:
        raise LocalIngressError("ingress_producer_mismatch")
    source_event_id = value.get("source_event_id")
    if not isinstance(source_event_id, str):
        raise LocalIngressError("ingress_value_type_invalid")
    try:
        trigger_id = trigger_id_for(SOURCE_ID, source_event_id)
    except ValueError as exc:
        raise LocalIngressError("ingress_source_event_id_invalid") from exc
    if value.get("explicit_opt_in") is not True:
        raise LocalIngressError("ingress_explicit_opt_in_required")
    created_at = _timestamp(value.get("created_at"), "ingress_created_at_invalid")
    expires_at = _timestamp(value.get("expires_at"), "ingress_expires_at_invalid")
    if expires_at <= created_at:
        raise LocalIngressError("ingress_time_window_invalid")
    try:
        policy = validate_guard_policy(value.get("policy"))
        manifest = validate_manifest(value.get("manifest"))
        envelope = validate_execution_envelope(value.get("execution_envelope"))
    except ValueError as exc:
        raise LocalIngressError("ingress_nested_contract_invalid") from exc
    _validate_initial_lane(policy, manifest, envelope)
    identity = identity_for(manifest, envelope)
    expected_trigger = _trigger(
        policy,
        manifest,
        identity,
        source_event_id=source_event_id,
        trigger_id=trigger_id,
        created_at=created_at,
    )
    if manifest["idempotency_key"] != guard_idempotency_key(trigger_id):
        raise LocalIngressError("ingress_idempotency_binding_mismatch")
    # Exercise the exact trigger validator at the ingress boundary too.
    validate_guard_trigger(expected_trigger)
    return {
        "schema_version": 1,
        "contract": BUNDLE_CONTRACT,
        "producer": {"source_id": SOURCE_ID, "revision": PRODUCER_REVISION},
        "source_event_id": source_event_id,
        "explicit_opt_in": True,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "policy": policy,
        "manifest": manifest,
        "execution_envelope": envelope,
    }


def bundle_fingerprint(bundle: dict[str, Any]) -> str:
    normalized = validate_local_ingress_bundle(bundle)
    return "sha256:" + hashlib.sha256(_canonical_bytes(normalized)).hexdigest()


def build_publish_preview(
    config: Config,
    bundle: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    bundle = validate_local_ingress_bundle(bundle)
    current = _aware_utc(now)
    time_reasons = _time_reasons(bundle, current)
    trigger = trigger_for_bundle(bundle)
    identity = identity_for(bundle["manifest"], bundle["execution_envelope"])
    shadow = build_reconciliation_shadow(
        config,
        policy=bundle["policy"],
        trigger=trigger,
        manifest=bundle["manifest"],
        envelope=bundle["execution_envelope"],
    )
    existing_status = _existing_bundle_status(config, bundle)
    reasons = [*time_reasons]
    if existing_status == "conflict":
        reasons.append("ingress_identity_conflict")
    status = (
        "blocked"
        if reasons
        else ("already_published" if existing_status == "matching" else "ready")
    )
    return {
        "schema_version": 1,
        "contract": PUBLISH_CONTRACT,
        "status": status,
        "source_id": SOURCE_ID,
        "producer_revision": PRODUCER_REVISION,
        "source_event_id": bundle["source_event_id"],
        "trigger_id": trigger["trigger_id"],
        "bundle_fingerprint": bundle_fingerprint(bundle),
        "request_id": bundle["manifest"]["request_id"],
        "request_fingerprint": trigger["request_fingerprint"],
        "execution_fingerprint": identity["execution_fingerprint"],
        "reason_codes": reasons,
        "shadow_decision_status": shadow["decision_status"],
        "shadow_reason_codes": shadow["reason_codes"],
        "mutation": {"allowed": False, "applied": False},
    }


def apply_publish(
    config: Config,
    bundle: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    bundle = validate_local_ingress_bundle(bundle)
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire(task_id="orchestration-local-ingress"):
        raise LocalIngressLockBusy()
    try:
        preview = build_publish_preview(config, bundle, now=now)
        if preview["status"] == "blocked":
            return preview, False
        if preview["status"] == "already_published":
            return preview, True
        root = ingress_dir(config)
        _ensure_private_directory(root)
        try:
            write_json_atomic_create(
                ingress_path(config, bundle["source_event_id"]), bundle
            )
        except FileExistsError:
            raced = _existing_bundle_status(config, bundle)
            if raced != "matching":
                conflict = dict(preview)
                conflict.update(
                    status="blocked",
                    reason_codes=["ingress_identity_conflict"],
                    mutation={"allowed": False, "applied": False},
                )
                return conflict, False
            retry = dict(preview)
            retry["status"] = "already_published"
            return retry, True
        published = dict(preview)
        published.update(
            status="published",
            mutation={"allowed": True, "applied": True},
        )
        return published, True
    finally:
        lock.release()


def build_local_reconciliation(
    config: Config,
    source_event_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    bundle = load_published_bundle(config, source_event_id)
    current = _aware_utc(now)
    trigger = trigger_for_bundle(bundle)
    shadow = build_reconciliation_shadow(
        config,
        policy=bundle["policy"],
        trigger=trigger,
        manifest=bundle["manifest"],
        envelope=bundle["execution_envelope"],
    )
    reasons = _time_reasons(bundle, current)
    reasons.extend(shadow["reason_codes"])
    reasons = list(dict.fromkeys(reasons))
    return {
        "schema_version": 1,
        "contract": RECONCILIATION_CONTRACT,
        "source_id": SOURCE_ID,
        "producer_revision": PRODUCER_REVISION,
        "source_event_id": source_event_id,
        "trigger_id": trigger["trigger_id"],
        "bundle_fingerprint": bundle_fingerprint(bundle),
        "decision_status": "eligible_shadow" if not reasons else "blocked",
        "reason_codes": reasons,
        "shadow": shadow,
        "required_action": (
            "retain_shadow_observation" if not reasons else "resolve_without_dispatch"
        ),
        "mutation": {"allowed": False, "applied": False},
    }


def apply_local_reconciliation(
    config: Config,
    source_event_id: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire(task_id="orchestration-local-reconciliation"):
        raise LocalIngressLockBusy()
    try:
        report = build_local_reconciliation(config, source_event_id, now=now)
        path = reconciliation_state_path(config, source_event_id)
        _validate_private_directory_if_present(path.parent)
        existing = _load_private_json(path, missing=None)
        if existing is not None and not _valid_reconciliation_state(existing, report):
            blocked = dict(report)
            blocked.update(
                decision_status="blocked",
                reason_codes=[*report["reason_codes"], "reconciliation_state_conflict"],
            )
            return blocked, False
        current = _aware_utc(now)
        first_observed_at = (
            existing["first_observed_at"]
            if isinstance(existing, dict)
            else current.isoformat()
        )
        observation_count = (
            int(existing["observation_count"]) + 1 if isinstance(existing, dict) else 1
        )
        state = {
            "schema_version": 1,
            "contract": STATE_CONTRACT,
            "source_id": SOURCE_ID,
            "producer_revision": PRODUCER_REVISION,
            "source_event_id": source_event_id,
            "trigger_id": report["trigger_id"],
            "bundle_fingerprint": report["bundle_fingerprint"],
            "phase": "shadow",
            "decision_status": report["decision_status"],
            "reason_codes": report["reason_codes"],
            "lifecycle": report["shadow"]["state"],
            "first_observed_at": first_observed_at,
            "last_observed_at": current.isoformat(),
            "observation_count": observation_count,
        }
        root = reconciliation_state_dir(config)
        _ensure_private_directory(root)
        write_json_atomic(path, state)
        applied = dict(report)
        applied.update(
            reconciliation_state={
                "phase": "shadow",
                "observation_count": observation_count,
                "lifecycle": state["lifecycle"],
            },
            mutation={"allowed": True, "applied": True},
        )
        return applied, True
    finally:
        lock.release()


def trigger_for_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    bundle = validate_local_ingress_bundle(bundle)
    policy = bundle["policy"]
    manifest = bundle["manifest"]
    identity = identity_for(manifest, bundle["execution_envelope"])
    trigger_id = trigger_id_for(SOURCE_ID, bundle["source_event_id"])
    return _trigger(
        policy,
        manifest,
        identity,
        source_event_id=bundle["source_event_id"],
        trigger_id=trigger_id,
        created_at=_timestamp(bundle["created_at"], "ingress_created_at_invalid"),
    )


def load_published_bundle(config: Config, source_event_id: str) -> dict[str, Any]:
    trigger_id_for(SOURCE_ID, source_event_id)
    path = ingress_path(config, source_event_id)
    _validate_private_directory_if_present(path.parent)
    value = _load_private_json(path, missing=None)
    if value is None:
        raise LocalIngressError("ingress_record_not_found")
    return validate_local_ingress_bundle(value)


def load_reconciliation_state(config: Config, source_event_id: str) -> dict[str, Any]:
    report = build_local_reconciliation(config, source_event_id)
    path = reconciliation_state_path(config, source_event_id)
    _validate_private_directory_if_present(path.parent)
    value = _load_private_json(path, missing=None)
    if value is None:
        raise LocalIngressError("reconciliation_state_not_found")
    if not _valid_reconciliation_state(value, report):
        raise LocalIngressError("reconciliation_state_conflict")
    return value


def local_ingress_time_reasons(
    bundle: dict[str, Any], *, now: datetime | None = None
) -> list[str]:
    return _time_reasons(validate_local_ingress_bundle(bundle), _aware_utc(now))


def ingress_dir(config: Config) -> Path:
    return config.root / "orchestration-ingress"


def ingress_path(config: Config, source_event_id: str) -> Path:
    trigger_id_for(SOURCE_ID, source_event_id)
    return ingress_dir(config) / f"{source_event_id}.json"


def reconciliation_state_dir(config: Config) -> Path:
    return config.root / "orchestration-reconciliation"


def reconciliation_state_path(config: Config, source_event_id: str) -> Path:
    trigger_id_for(SOURCE_ID, source_event_id)
    return reconciliation_state_dir(config) / f"{source_event_id}.json"


def render_local_ingress(report: dict[str, Any]) -> str:
    lines = [str(report["contract"])]
    for key in (
        "status",
        "decision_status",
        "source_event_id",
        "trigger_id",
        "request_id",
        "shadow_decision_status",
    ):
        if key in report:
            lines.append(f"{key}: {report[key]}")
    lines.append(
        "reason_codes: " + (", ".join(report.get("reason_codes", [])) or "none")
    )
    lines.append(
        "mutation: allowed={} applied={}".format(
            str(bool(report["mutation"]["allowed"])).lower(),
            str(bool(report["mutation"]["applied"])).lower(),
        )
    )
    return "\n".join(lines) + "\n"


def _validate_initial_lane(
    policy: dict[str, Any], manifest: dict[str, Any], envelope: dict[str, Any]
) -> None:
    authority = manifest["authority"]
    work = manifest["work"]
    task = envelope["task"]
    expected_policy_scope = {
        "source_kinds": ["operator"],
        "project_ids": [task["project_id"]],
        "repository_roots": [envelope["cwd"]],
        "work_kinds": ["verification"],
        "decision_authorities": [authority["decision_authority"]],
        "impacts": ["low"],
        "allowed_mutations": ["read_only"],
        "required_prohibited_mutations": ["destructive", "external_state"],
        "isolations": [work["isolation"]],
        "work_verifications": ["objective"],
        "required_verification_scope": task["verification_scope"],
        "capacity_pools": [task["capacity_pool"]],
    }
    if policy["source"] != {
        "source_id": SOURCE_ID,
        "adapter_revision": PRODUCER_REVISION,
    }:
        raise LocalIngressError("ingress_policy_source_mismatch")
    if policy["scope"] != expected_policy_scope:
        raise LocalIngressError("ingress_policy_scope_not_exact")
    if manifest["source"] != {"kind": "operator", "collection_owner": "operator"}:
        raise LocalIngressError("ingress_manifest_source_not_operator")
    if manifest["surface_preferences"] != ["cbr_batch"]:
        raise LocalIngressError("ingress_surface_not_exact")
    if manifest["automation_boundary"] != "bounded_automatic":
        raise LocalIngressError("ingress_automation_boundary_not_bounded")
    if authority not in (
        {
            "decision_authority": "delegated_decision",
            "resolution": "resolved",
            "impact": "low",
            "approval_state": "not_required",
        },
        {
            "decision_authority": "bounded_experiment",
            "resolution": "resolved",
            "impact": "low",
            "approval_state": "granted",
        },
    ):
        raise LocalIngressError("ingress_authority_not_exact")
    if manifest["mutation"] != {
        "allowed": ["read_only"],
        "prohibited": ["runtime_state", "external_state", "destructive"],
    }:
        raise LocalIngressError("ingress_mutation_lane_not_read_only")
    if not (
        work["kind"] == "verification"
        and work["interaction"] == "none"
        and work["dependency"] == "none"
        and work["verification"] == "objective"
        and work["external_worker_boundary"] == "unavailable"
    ):
        raise LocalIngressError("ingress_work_lane_not_exact")
    if task["category"] != "verification" or task["depends_on"]:
        raise LocalIngressError("ingress_task_lane_not_exact")


def _trigger(
    policy: dict[str, Any],
    manifest: dict[str, Any],
    identity: dict[str, str],
    *,
    source_event_id: str,
    trigger_id: str,
    created_at: datetime,
) -> dict[str, Any]:
    return validate_guard_trigger(
        {
            "schema_version": 1,
            "contract": "orchestration-trigger-v1",
            "trigger_id": trigger_id,
            "source_id": SOURCE_ID,
            "source_adapter_revision": PRODUCER_REVISION,
            "source_event_id": source_event_id,
            "explicit_opt_in": True,
            "policy_id": policy["policy_id"],
            "policy_revision": policy["revision"],
            "policy_fingerprint": policy_fingerprint(policy),
            "request_id": manifest["request_id"],
            "request_fingerprint": orchestration_request_fingerprint(manifest),
            "execution_fingerprint": identity["execution_fingerprint"],
            "created_at": created_at.isoformat(),
        }
    )


def _time_reasons(bundle: dict[str, Any], now: datetime) -> list[str]:
    created_at = _timestamp(bundle["created_at"], "ingress_created_at_invalid")
    expires_at = _timestamp(bundle["expires_at"], "ingress_expires_at_invalid")
    reasons: list[str] = []
    if created_at > now + MAX_FUTURE_SKEW:
        reasons.append("ingress_created_in_future")
    if expires_at <= now:
        reasons.append("ingress_expired")
    if expires_at - created_at > MAX_TTL:
        reasons.append("ingress_ttl_exceeds_limit")
    return reasons


def _existing_bundle_status(config: Config, bundle: dict[str, Any]) -> str:
    path = ingress_path(config, bundle["source_event_id"])
    _validate_private_directory_if_present(path.parent)
    if not path.exists():
        return "absent"
    try:
        existing = _load_private_json(path, missing=None)
        existing = validate_local_ingress_bundle(existing)
    except (OSError, ValueError, json.JSONDecodeError):
        return "conflict"
    return (
        "matching"
        if _canonical_bytes(existing) == _canonical_bytes(bundle)
        else "conflict"
    )


def _load_private_json(path: Path, *, missing: Any) -> Any:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return missing
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LocalIngressError("ingress_record_identity_invalid")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise LocalIngressError("ingress_record_permissions_invalid")
    if info.st_size > MAX_INPUT_BYTES:
        raise LocalIngressError("ingress_record_too_large")
    return read_json(path, missing)


def _validate_private_directory_if_present(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LocalIngressError("ingress_directory_identity_invalid")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise LocalIngressError("ingress_directory_permissions_invalid")


def _ensure_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LocalIngressError("ingress_directory_identity_invalid")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise LocalIngressError("ingress_directory_permissions_invalid")


def _valid_reconciliation_state(value: object, report: dict[str, Any]) -> bool:
    if not isinstance(value, dict) or set(value) != STATE_KEYS:
        return False
    lifecycle = value.get("lifecycle")
    if not isinstance(lifecycle, dict) or set(lifecycle) != LIFECYCLE_KEYS:
        return False
    count = value.get("observation_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return False
    try:
        _timestamp(value.get("first_observed_at"), "invalid")
        _timestamp(value.get("last_observed_at"), "invalid")
    except LocalIngressError:
        return False
    return bool(
        value.get("schema_version") == 1
        and value.get("contract") == STATE_CONTRACT
        and value.get("source_id") == SOURCE_ID
        and value.get("producer_revision") == PRODUCER_REVISION
        and value.get("source_event_id") == report["source_event_id"]
        and value.get("trigger_id") == report["trigger_id"]
        and value.get("bundle_fingerprint") == report["bundle_fingerprint"]
        and value.get("phase") == "shadow"
        and value.get("decision_status") in {"eligible_shadow", "blocked"}
        and isinstance(value.get("reason_codes"), list)
        and all(isinstance(item, str) for item in value["reason_codes"])
        and all(isinstance(item, str) for item in lifecycle.values())
    )


def _timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str):
        raise LocalIngressError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalIngressError(code) from exc
    if parsed.tzinfo is None:
        raise LocalIngressError(code)
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise LocalIngressError("ingress_now_invalid")
    return current.astimezone(timezone.utc)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

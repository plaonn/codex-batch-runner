"""Explicit one-event D3 guarded consumer with durable lease and disposition."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .events import write_event_nonfatal
from .fs import read_json, write_json_atomic, write_json_atomic_create
from .lock import FileLock
from .orchestration_dispatch import (
    DispatchLockBusy,
    apply_dispatch,
    identity_for,
)
from .orchestration_guard import build_reconciliation_shadow
from .orchestration_ingress import (
    LocalIngressError,
    bundle_fingerprint,
    load_published_bundle,
    load_reconciliation_state,
    local_ingress_time_reasons,
    trigger_for_bundle,
)


PREVIEW_CONTRACT = "orchestration-local-consumer-preview-v1"
STATE_CONTRACT = "orchestration-consumer-state-v1"
DISPOSITION_CONTRACT = "orchestration-source-disposition-v1"
MAX_ATTEMPTS = 3
LEASE_SECONDS = 120
RETRY_DELAYS = (30, 120)
MAX_RECORD_BYTES = 128 * 1024
STATE_KEYS = {
    "schema_version",
    "contract",
    "source_event_id",
    "trigger_id",
    "bundle_fingerprint",
    "dispatch_id",
    "task_id",
    "phase",
    "attempt_count",
    "max_attempts",
    "lease",
    "next_attempt_at",
    "last_reason_codes",
    "disposition_id",
    "created_at",
    "updated_at",
}
LEASE_KEYS = {"lease_id", "acquired_at", "expires_at"}
DISPOSITION_KEYS = {
    "schema_version",
    "contract",
    "disposition_id",
    "source_event_id",
    "trigger_id",
    "bundle_fingerprint",
    "dispatch_id",
    "task_id",
    "result",
    "reason_codes",
    "created_at",
}
PHASES = {"pending", "leased", "retry_wait", "admitted", "blocked", "exhausted"}
DISPOSITION_RESULTS = {"admitted", "blocked_terminal", "retry_exhausted"}


class ConsumerError(ValueError):
    pass


class ConsumerLockBusy(RuntimeError):
    pass


def build_consumer_preview(
    config: Config,
    source_event_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _aware_utc(now)
    bundle = load_published_bundle(config, source_event_id)
    trigger = trigger_for_bundle(bundle)
    identity = identity_for(bundle["manifest"], bundle["execution_envelope"])
    fingerprint = bundle_fingerprint(bundle)
    reasons: list[str] = []

    if bundle["policy"]["activation_mode"] != "guarded":
        reasons.append("consumer_activation_mode_not_guarded")
    try:
        shadow_state = load_reconciliation_state(config, source_event_id)
    except LocalIngressError as exc:
        shadow_state = None
        reasons.append(str(exc))
    if shadow_state is not None and not _activation_observation_ready(shadow_state):
        reasons.append("consumer_shadow_observation_not_ready")

    activation = build_reconciliation_shadow(
        config,
        policy=bundle["policy"],
        trigger=trigger,
        manifest=bundle["manifest"],
        envelope=bundle["execution_envelope"],
        allow_guarded_activation=True,
    )
    reasons.extend(local_ingress_time_reasons(bundle, now=current))
    reasons.extend(activation["reason_codes"])
    reasons = list(dict.fromkeys(reasons))

    state = _load_consumer_state(config, source_event_id, missing=None)
    if state is not None:
        _validate_state(
            state, source_event_id, trigger["trigger_id"], fingerprint, identity
        )
    disposition = _load_disposition(config, trigger["trigger_id"], missing=None)
    if disposition is not None:
        _validate_disposition(
            disposition, source_event_id, trigger["trigger_id"], fingerprint, identity
        )

    status = _preview_status(state, disposition, reasons, current)
    if status == "leased" and not reasons:
        reasons.append("consumer_lease_active")
    elif status == "retry_wait" and not reasons:
        reasons.append("consumer_retry_backoff_active")
    elif status == "blocked" and not reasons and disposition is not None:
        reasons.extend(disposition["reason_codes"])
    elif status == "blocked" and not reasons and state is not None:
        reasons.extend(state["last_reason_codes"])
    return {
        "schema_version": 1,
        "contract": PREVIEW_CONTRACT,
        "status": status,
        "source_event_id": source_event_id,
        "trigger_id": trigger["trigger_id"],
        "bundle_fingerprint": fingerprint,
        "dispatch_id": identity["dispatch_id"],
        "task_id": identity["task_id"],
        "reason_codes": reasons,
        "activation": {
            "decision_status": activation["decision_status"],
            "reason_codes": activation["reason_codes"],
            "d2_preview": activation["d2_preview"],
        },
        "consumer": _consumer_projection(state, disposition, current),
        "mutation": {"allowed": False, "applied": False},
    }


def apply_consumer(
    config: Config,
    source_event_id: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    current = _aware_utc(now)
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire(task_id=f"orchestration-consumer-{source_event_id}"):
        raise ConsumerLockBusy()
    lease_id: str | None = None
    try:
        preview = build_consumer_preview(config, source_event_id, now=current)
        state = _load_consumer_state(config, source_event_id, missing=None)
        disposition = _load_disposition(config, preview["trigger_id"], missing=None)
        if disposition is not None:
            repaired = state
            applied = False
            if not _state_matches_disposition(state, disposition):
                repaired = _terminal_state_from_disposition(
                    preview, state, disposition, current
                )
                _write_state(config, source_event_id, repaired)
                applied = True
            result = _applied_report(
                preview,
                disposition["result"],
                disposition["result"] == "admitted",
                repaired,
                applied=applied,
            )
            return result, disposition["result"] == "admitted"
        if preview["status"] in {"leased", "retry_wait"}:
            return preview, False
        if preview["status"] == "blocked":
            if _prerequisite_blocked(preview):
                return preview, False
            if _retryable_preview(preview):
                retry_state, terminal = _retry_state(
                    preview, state, current, increment=True
                )
                if terminal:
                    disposition = _write_disposition(
                        config,
                        preview,
                        "retry_exhausted",
                        retry_state["last_reason_codes"],
                        current,
                    )
                    retry_state["disposition_id"] = disposition["disposition_id"]
                _write_state(config, source_event_id, retry_state)
                _emit(config, preview, retry_state["phase"])
                return _applied_report(
                    preview, retry_state["phase"], False, retry_state
                ), False
            blocked_state = _terminal_state(preview, state, "blocked", current)
            disposition = _write_disposition(
                config, preview, "blocked_terminal", preview["reason_codes"], current
            )
            blocked_state["disposition_id"] = disposition["disposition_id"]
            _write_state(config, source_event_id, blocked_state)
            _emit(config, preview, "blocked")
            return _applied_report(preview, "blocked", False, blocked_state), False

        previous_attempts = int(state["attempt_count"]) if state else 0
        recovering_existing = bool(preview["activation"]["d2_preview"]["task_present"])
        attempt_count = (
            previous_attempts
            if recovering_existing and previous_attempts >= MAX_ATTEMPTS
            else previous_attempts + 1
        )
        if attempt_count > MAX_ATTEMPTS:
            exhausted = _terminal_state(preview, state, "exhausted", current)
            exhausted["attempt_count"] = MAX_ATTEMPTS
            disposition = _write_disposition(
                config,
                preview,
                "retry_exhausted",
                ["consumer_attempts_exhausted"],
                current,
            )
            exhausted["disposition_id"] = disposition["disposition_id"]
            _write_state(config, source_event_id, exhausted)
            return _applied_report(preview, "exhausted", False, exhausted), False
        lease_id = "ocl-" + uuid.uuid4().hex
        claimed = _base_state(preview, state, current)
        claimed.update(
            phase="leased",
            attempt_count=attempt_count,
            lease={
                "lease_id": lease_id,
                "acquired_at": current.isoformat(),
                "expires_at": (current + timedelta(seconds=LEASE_SECONDS)).isoformat(),
            },
            next_attempt_at=None,
            last_reason_codes=[],
        )
        _write_state(config, source_event_id, claimed)
        _emit(config, preview, "claimed")
    finally:
        lock.release()

    try:
        bundle = load_published_bundle(config, source_event_id)
        dispatch_report, admitted = apply_dispatch(
            config, bundle["manifest"], bundle["execution_envelope"]
        )
    except DispatchLockBusy:
        return _finalize_retry(
            config, source_event_id, lease_id, ["lock_busy"], current
        )
    except Exception:
        return _finalize_terminal(
            config, source_event_id, lease_id, ["consumer_internal_error"], current
        )
    if not admitted:
        reasons = list(dispatch_report.get("reason_codes") or ["d2_dispatch_failed"])
        if reasons == ["runner_paused"]:
            return _finalize_retry(config, source_event_id, lease_id, reasons, current)
        return _finalize_terminal(config, source_event_id, lease_id, reasons, current)
    return _finalize_admitted(
        config, source_event_id, lease_id, dispatch_report, current
    )


def consumer_state_dir(config: Config) -> Path:
    return config.root / "orchestration-consumer"


def disposition_dir(config: Config) -> Path:
    return config.root / "orchestration-source-dispositions"


def consumer_state_path(config: Config, source_event_id: str) -> Path:
    _safe_event_id(source_event_id)
    return consumer_state_dir(config) / f"{source_event_id}.json"


def disposition_path(config: Config, trigger_id: str) -> Path:
    if not trigger_id.startswith("ot-") or not trigger_id[3:].isalnum():
        raise ConsumerError("consumer_trigger_id_invalid")
    return disposition_dir(config) / f"{trigger_id}.json"


def render_consumer(report: dict[str, Any]) -> str:
    lines = [str(report["contract"]), f"status: {report.get('status', '-')}"]
    for key in ("source_event_id", "trigger_id", "dispatch_id", "task_id"):
        if key in report:
            lines.append(f"{key}: {report[key]}")
    lines.append(
        "reason_codes: " + (", ".join(report.get("reason_codes", [])) or "none")
    )
    mutation = report["mutation"]
    lines.append(
        f"mutation: allowed={str(bool(mutation['allowed'])).lower()} "
        f"applied={str(bool(mutation['applied'])).lower()}"
    )
    return "\n".join(lines) + "\n"


def consumer_doctor_summary(
    config: Config, *, now: datetime | None = None
) -> dict[str, Any]:
    current = _aware_utc(now)
    phases: Counter[str] = Counter()
    invalid = 0
    expired_leases = 0
    state_root = consumer_state_dir(config)
    disposition_root = disposition_dir(config)
    try:
        _validate_private_directory_if_present(state_root)
        state_paths = sorted(state_root.glob("*.json")) if state_root.exists() else []
    except ConsumerError:
        invalid += 1
        state_paths = []
    for path in state_paths:
        try:
            value = _load_private_json(path, missing=None)
            _validate_state_shape(value)
            assert isinstance(value, dict)
            phases[value["phase"]] += 1
            if (
                value["phase"] == "leased"
                and _timestamp(value["lease"]["expires_at"]) <= current
            ):
                expired_leases += 1
        except (ConsumerError, OSError):
            invalid += 1
    dispositions = 0
    try:
        _validate_private_directory_if_present(disposition_root)
        disposition_paths = (
            sorted(disposition_root.glob("*.json")) if disposition_root.exists() else []
        )
    except ConsumerError:
        invalid += 1
        disposition_paths = []
    for path in disposition_paths:
        try:
            value = _load_private_json(path, missing=None)
            _validate_disposition_shape(value)
            dispositions += 1
        except (ConsumerError, OSError):
            invalid += 1
    return {
        "read_only": True,
        "state_count": sum(phases.values()),
        "states_by_phase": dict(sorted(phases.items())),
        "expired_lease_count": expired_leases,
        "disposition_count": dispositions,
        "invalid_record_count": invalid,
    }


def _finalize_admitted(
    config: Config,
    source_event_id: str,
    lease_id: str | None,
    receipt: dict[str, Any],
    current: datetime,
) -> tuple[dict[str, Any], bool]:
    lock, preview, state = _finalize_lock(config, source_event_id, lease_id, current)
    if lock is None:
        return preview, False
    try:
        if not _receipt_matches_preview(receipt, preview):
            return _finalize_terminal_locked(
                config, preview, state, ["receipt_identity_conflict"], current
            )
        preview["reason_codes"] = []
        disposition = _write_disposition(config, preview, "admitted", [], current)
        admitted_state = _terminal_state(preview, state, "admitted", current)
        admitted_state["disposition_id"] = disposition["disposition_id"]
        _write_state(config, source_event_id, admitted_state)
        _emit(config, preview, "admitted")
        return _applied_report(preview, "admitted", True, admitted_state), True
    finally:
        lock.release()


def _finalize_retry(
    config: Config,
    source_event_id: str,
    lease_id: str | None,
    reasons: list[str],
    current: datetime,
) -> tuple[dict[str, Any], bool]:
    lock, preview, state = _finalize_lock(config, source_event_id, lease_id, current)
    if lock is None:
        return preview, False
    try:
        preview["reason_codes"] = reasons
        retry_state, terminal = _retry_state(preview, state, current)
        if terminal:
            disposition = _write_disposition(
                config, preview, "retry_exhausted", reasons, current
            )
            retry_state["disposition_id"] = disposition["disposition_id"]
        _write_state(config, source_event_id, retry_state)
        _emit(config, preview, retry_state["phase"])
        return _applied_report(preview, retry_state["phase"], False, retry_state), False
    finally:
        lock.release()


def _finalize_terminal(
    config: Config,
    source_event_id: str,
    lease_id: str | None,
    reasons: list[str],
    current: datetime,
) -> tuple[dict[str, Any], bool]:
    lock, preview, state = _finalize_lock(config, source_event_id, lease_id, current)
    if lock is None:
        return preview, False
    try:
        return _finalize_terminal_locked(config, preview, state, reasons, current)
    finally:
        lock.release()


def _finalize_terminal_locked(
    config: Config,
    preview: dict[str, Any],
    state: dict[str, Any],
    reasons: list[str],
    current: datetime,
) -> tuple[dict[str, Any], bool]:
    preview["reason_codes"] = reasons
    blocked = _terminal_state(preview, state, "blocked", current)
    disposition = _write_disposition(
        config, preview, "blocked_terminal", reasons, current
    )
    blocked["disposition_id"] = disposition["disposition_id"]
    _write_state(config, preview["source_event_id"], blocked)
    _emit(config, preview, "blocked")
    return _applied_report(preview, "blocked", False, blocked), False


def _finalize_lock(
    config: Config,
    source_event_id: str,
    lease_id: str | None,
    current: datetime,
) -> tuple[FileLock | None, dict[str, Any], dict[str, Any]]:
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire(task_id=f"orchestration-consumer-{source_event_id}"):
        preview = build_consumer_preview(config, source_event_id, now=current)
        preview.update(status="lease_finalize_pending", reason_codes=["lock_busy"])
        return None, preview, {}
    preview = build_consumer_preview(config, source_event_id, now=current)
    state = _load_consumer_state(config, source_event_id, missing=None)
    if not isinstance(state, dict) or not isinstance(state.get("lease"), dict):
        lock.release()
        preview.update(status="blocked", reason_codes=["consumer_lease_conflict"])
        return None, preview, {}
    if state["lease"].get("lease_id") != lease_id:
        lock.release()
        preview.update(status="blocked", reason_codes=["consumer_lease_conflict"])
        return None, preview, {}
    return lock, preview, state


def _retry_state(
    preview: dict[str, Any],
    state: dict[str, Any] | None,
    current: datetime,
    *,
    increment: bool = False,
) -> tuple[dict[str, Any], bool]:
    result = _base_state(preview, state, current)
    attempts = int(state["attempt_count"]) if state else 0
    if increment:
        attempts += 1
    result["attempt_count"] = attempts
    result["lease"] = None
    result["last_reason_codes"] = list(preview["reason_codes"])
    if attempts >= MAX_ATTEMPTS:
        result.update(phase="exhausted", next_attempt_at=None)
        return result, True
    delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
    result.update(
        phase="retry_wait",
        next_attempt_at=(current + timedelta(seconds=delay)).isoformat(),
    )
    return result, False


def _terminal_state(
    preview: dict[str, Any],
    state: dict[str, Any] | None,
    phase: str,
    current: datetime,
) -> dict[str, Any]:
    result = _base_state(preview, state, current)
    result.update(
        phase=phase,
        lease=None,
        next_attempt_at=None,
        last_reason_codes=list(preview["reason_codes"]),
        updated_at=current.isoformat(),
    )
    return result


def _base_state(
    preview: dict[str, Any], state: dict[str, Any] | None, current: datetime
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract": STATE_CONTRACT,
        "source_event_id": preview["source_event_id"],
        "trigger_id": preview["trigger_id"],
        "bundle_fingerprint": preview["bundle_fingerprint"],
        "dispatch_id": preview["dispatch_id"],
        "task_id": preview["task_id"],
        "phase": "pending",
        "attempt_count": int(state["attempt_count"]) if state else 0,
        "max_attempts": MAX_ATTEMPTS,
        "lease": None,
        "next_attempt_at": None,
        "last_reason_codes": [],
        "disposition_id": state.get("disposition_id") if state else None,
        "created_at": state["created_at"] if state else current.isoformat(),
        "updated_at": current.isoformat(),
    }


def _terminal_state_from_disposition(
    preview: dict[str, Any],
    state: dict[str, Any] | None,
    disposition: dict[str, Any],
    current: datetime,
) -> dict[str, Any]:
    phase = {
        "admitted": "admitted",
        "blocked_terminal": "blocked",
        "retry_exhausted": "exhausted",
    }[disposition["result"]]
    result = _base_state(preview, state, current)
    result.update(
        phase=phase,
        lease=None,
        next_attempt_at=None,
        last_reason_codes=disposition["reason_codes"],
        disposition_id=disposition["disposition_id"],
    )
    return result


def _state_matches_disposition(
    state: dict[str, Any] | None, disposition: dict[str, Any]
) -> bool:
    if not isinstance(state, dict):
        return False
    phase = {
        "admitted": "admitted",
        "blocked_terminal": "blocked",
        "retry_exhausted": "exhausted",
    }[disposition["result"]]
    return bool(
        state["phase"] == phase
        and state["lease"] is None
        and state["next_attempt_at"] is None
        and state["last_reason_codes"] == disposition["reason_codes"]
        and state["disposition_id"] == disposition["disposition_id"]
    )


def _write_disposition(
    config: Config,
    preview: dict[str, Any],
    result: str,
    reasons: list[str],
    current: datetime,
) -> dict[str, Any]:
    disposition = {
        "schema_version": 1,
        "contract": DISPOSITION_CONTRACT,
        "disposition_id": "osd-" + preview["trigger_id"][3:],
        "source_event_id": preview["source_event_id"],
        "trigger_id": preview["trigger_id"],
        "bundle_fingerprint": preview["bundle_fingerprint"],
        "dispatch_id": preview["dispatch_id"],
        "task_id": preview["task_id"],
        "result": result,
        "reason_codes": list(reasons),
        "created_at": current.isoformat(),
    }
    root = disposition_dir(config)
    _ensure_private_directory(root)
    path = disposition_path(config, preview["trigger_id"])
    try:
        write_json_atomic_create(path, disposition)
    except FileExistsError:
        existing = _load_private_json(path, missing=None)
        _validate_disposition(
            existing,
            preview["source_event_id"],
            preview["trigger_id"],
            preview["bundle_fingerprint"],
            {"dispatch_id": preview["dispatch_id"], "task_id": preview["task_id"]},
        )
        if existing != disposition:
            # created_at may differ on a matching recovery; compare immutable meaning.
            comparable = dict(existing) if isinstance(existing, dict) else {}
            comparable["created_at"] = disposition["created_at"]
            if comparable != disposition:
                raise ConsumerError("consumer_disposition_conflict")
        return existing
    return disposition


def _write_state(config: Config, source_event_id: str, state: dict[str, Any]) -> None:
    _validate_state_shape(state)
    root = consumer_state_dir(config)
    _ensure_private_directory(root)
    write_json_atomic(consumer_state_path(config, source_event_id), state)


def _load_consumer_state(config: Config, source_event_id: str, *, missing: Any) -> Any:
    path = consumer_state_path(config, source_event_id)
    _validate_private_directory_if_present(path.parent)
    return _load_private_json(path, missing=missing)


def _load_disposition(config: Config, trigger_id: str, *, missing: Any) -> Any:
    path = disposition_path(config, trigger_id)
    _validate_private_directory_if_present(path.parent)
    return _load_private_json(path, missing=missing)


def _validate_state(
    value: object,
    source_event_id: str,
    trigger_id: str,
    fingerprint: str,
    identity: dict[str, str],
) -> None:
    _validate_state_shape(value)
    assert isinstance(value, dict)
    if not (
        value["source_event_id"] == source_event_id
        and value["trigger_id"] == trigger_id
        and value["bundle_fingerprint"] == fingerprint
        and value["dispatch_id"] == identity["dispatch_id"]
        and value["task_id"] == identity["task_id"]
    ):
        raise ConsumerError("consumer_state_identity_conflict")


def _validate_state_shape(value: object) -> None:
    if not isinstance(value, dict) or set(value) != STATE_KEYS:
        raise ConsumerError("consumer_state_invalid")
    if value.get("schema_version") != 1 or value.get("contract") != STATE_CONTRACT:
        raise ConsumerError("consumer_state_invalid")
    if value.get("phase") not in PHASES or value.get("max_attempts") != MAX_ATTEMPTS:
        raise ConsumerError("consumer_state_invalid")
    attempts = value.get("attempt_count")
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or not 0 <= attempts <= MAX_ATTEMPTS
    ):
        raise ConsumerError("consumer_state_invalid")
    lease = value.get("lease")
    if lease is not None:
        if not isinstance(lease, dict) or set(lease) != LEASE_KEYS:
            raise ConsumerError("consumer_state_invalid")
        _timestamp(lease.get("acquired_at"))
        _timestamp(lease.get("expires_at"))
    if value.get("phase") == "leased" and lease is None:
        raise ConsumerError("consumer_state_invalid")
    if value.get("phase") != "leased" and lease is not None:
        raise ConsumerError("consumer_state_invalid")
    if value.get("next_attempt_at") is not None:
        _timestamp(value["next_attempt_at"])
    if not isinstance(value.get("last_reason_codes"), list) or not all(
        isinstance(item, str) for item in value["last_reason_codes"]
    ):
        raise ConsumerError("consumer_state_invalid")
    _timestamp(value.get("created_at"))
    _timestamp(value.get("updated_at"))


def _validate_disposition(
    value: object,
    source_event_id: str,
    trigger_id: str,
    fingerprint: str,
    identity: dict[str, str],
) -> None:
    _validate_disposition_shape(value)
    assert isinstance(value, dict)
    if not (
        value.get("source_event_id") == source_event_id
        and value.get("trigger_id") == trigger_id
        and value.get("bundle_fingerprint") == fingerprint
        and value.get("dispatch_id") == identity["dispatch_id"]
        and value.get("task_id") == identity["task_id"]
        and value.get("result") in DISPOSITION_RESULTS
        and isinstance(value.get("reason_codes"), list)
        and all(isinstance(item, str) for item in value["reason_codes"])
    ):
        raise ConsumerError("consumer_disposition_identity_conflict")


def _validate_disposition_shape(value: object) -> None:
    if not isinstance(value, dict) or set(value) != DISPOSITION_KEYS:
        raise ConsumerError("consumer_disposition_invalid")
    if not (
        value.get("schema_version") == 1
        and value.get("contract") == DISPOSITION_CONTRACT
        and value.get("result") in DISPOSITION_RESULTS
        and isinstance(value.get("reason_codes"), list)
        and all(isinstance(item, str) for item in value["reason_codes"])
    ):
        raise ConsumerError("consumer_disposition_invalid")
    _timestamp(value.get("created_at"))


def _activation_observation_ready(state: dict[str, Any]) -> bool:
    return bool(
        state.get("phase") == "shadow"
        and state.get("observation_count", 0) >= 1
        and state.get("decision_status") == "blocked"
        and state.get("reason_codes") == ["activation_not_implemented"]
    )


def _preview_status(
    state: dict[str, Any] | None,
    disposition: dict[str, Any] | None,
    reasons: list[str],
    current: datetime,
) -> str:
    if disposition is not None:
        return "already_admitted" if disposition["result"] == "admitted" else "blocked"
    if state is not None:
        if (
            state["phase"] == "leased"
            and _timestamp(state["lease"]["expires_at"]) > current
        ):
            return "leased"
        if state["phase"] == "retry_wait":
            next_at = _timestamp(state["next_attempt_at"])
            if next_at > current:
                return "retry_wait"
        if state["phase"] in {"blocked", "exhausted"}:
            return "blocked"
    return "blocked" if reasons else "ready"


def _consumer_projection(
    state: dict[str, Any] | None,
    disposition: dict[str, Any] | None,
    current: datetime,
) -> dict[str, Any]:
    return {
        "phase": state["phase"] if state else "pending",
        "attempt_count": state["attempt_count"] if state else 0,
        "max_attempts": MAX_ATTEMPTS,
        "lease_active": bool(
            state
            and state["phase"] == "leased"
            and _timestamp(state["lease"]["expires_at"]) > current
        ),
        "disposition": disposition["result"] if disposition else "not_written",
    }


def _retryable_preview(preview: dict[str, Any]) -> bool:
    return bool(
        preview["reason_codes"] == ["d2_dispatch_blocked"]
        and preview["activation"]["d2_preview"]["reason_codes"] == ["runner_paused"]
    )


def _prerequisite_blocked(preview: dict[str, Any]) -> bool:
    return any(
        reason
        in {
            "reconciliation_state_not_found",
            "consumer_shadow_observation_not_ready",
        }
        for reason in preview["reason_codes"]
    )


def _receipt_matches_preview(receipt: dict[str, Any], preview: dict[str, Any]) -> bool:
    return bool(
        receipt.get("contract") == "orchestration-dispatch-receipt-v1"
        and receipt.get("dispatch_id") == preview["dispatch_id"]
        and receipt.get("task_id") == preview["task_id"]
        and receipt.get("queue_admission") == "queued"
    )


def _applied_report(
    preview: dict[str, Any],
    status: str,
    success: bool,
    state: dict[str, Any],
    *,
    applied: bool = True,
) -> dict[str, Any]:
    report = dict(preview)
    report.update(
        status=status,
        consumer={
            "phase": state["phase"],
            "attempt_count": state["attempt_count"],
            "max_attempts": MAX_ATTEMPTS,
            "lease_active": state["phase"] == "leased",
            "disposition": state.get("disposition_id") or "not_written",
        },
        mutation={"allowed": applied, "applied": applied},
    )
    if success:
        report["reason_codes"] = []
    return report


def _emit(config: Config, preview: dict[str, Any], phase: str) -> None:
    write_event_nonfatal(
        config,
        "orchestration_consumer_" + phase,
        source="orchestration-local-consumer",
        summary=f"orchestration consumer {phase}",
        payload={
            "source_event_id": preview["source_event_id"],
            "trigger_id": preview["trigger_id"],
            "dispatch_id": preview["dispatch_id"],
            "task_id": preview["task_id"],
            "reason_codes": preview.get("reason_codes", []),
        },
    )


def _load_private_json(path: Path, *, missing: Any) -> Any:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return missing
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConsumerError("consumer_record_identity_invalid")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ConsumerError("consumer_record_permissions_invalid")
    if info.st_size > MAX_RECORD_BYTES:
        raise ConsumerError("consumer_record_too_large")
    try:
        return read_json(path, missing)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsumerError("consumer_record_unreadable") from exc


def _validate_private_directory_if_present(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConsumerError("consumer_directory_identity_invalid")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ConsumerError("consumer_directory_permissions_invalid")


def _ensure_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConsumerError("consumer_directory_identity_invalid")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ConsumerError("consumer_directory_permissions_invalid")


def _safe_event_id(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ConsumerError("consumer_source_event_id_invalid")
    if not value[0].isalnum() or not all(
        char.isalnum() or char in "._:@+-" for char in value
    ):
        raise ConsumerError("consumer_source_event_id_invalid")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ConsumerError("consumer_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConsumerError("consumer_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ConsumerError("consumer_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ConsumerError("consumer_now_invalid")
    return current.astimezone(timezone.utc)

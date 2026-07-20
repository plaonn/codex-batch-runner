"""D3-1 Todoist guarded-admission pilot.

Todoist contributes only an explicit approval signal and an opaque request ID.
Execution material is loaded from an exact runtime-private local bundle.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .fs import read_json, write_json_atomic, write_json_atomic_create
from .lock import FileLock
from .orchestration import load_manifest
from .orchestration_dispatch import (
    DispatchLockBusy,
    apply_dispatch,
    load_execution_envelope,
)
from .orchestration_guard import (
    build_reconciliation_shadow,
    guard_idempotency_key,
    policy_fingerprint,
    trigger_id_for,
    validate_guard_policy,
)

SOURCE_CONTRACT = "orchestration-todoist-source-v1"
SNAPSHOT_CONTRACT = "orchestration-todoist-snapshot-v1"
BUNDLE_CONTRACT = "orchestration-local-request-bundle-v1"
RESULT_CONTRACT = "orchestration-todoist-reconciliation-v1"
ERROR_CONTRACT = "orchestration-todoist-error-v1"
DISPOSITION_CONTRACT = "orchestration-source-disposition-v1"
TRIGGER_CONTRACT = "orchestration-trigger-v1"
MAX_INPUT_BYTES = 64 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
OPT_IN_PREFIX = "CBR-GUARDED-V1 "

SOURCE_KEYS = {
    "schema_version",
    "contract",
    "active",
    "source_id",
    "adapter_revision",
    "account_id",
    "project_id",
    "parent_id",
    "required_label",
    "require_unshared",
}
SNAPSHOT_KEYS = {
    "schema_version",
    "contract",
    "task_id",
    "account_id",
    "project_id",
    "parent_id",
    "labels",
    "description",
    "checked",
    "shared",
    "observed_at",
}
BUNDLE_KEYS = {
    "schema_version",
    "contract",
    "request_id",
    "manifest_path",
    "execution_envelope_path",
}
OPT_IN_KEYS = {"request_id", "opt_in_token", "created_at"}

REASON_ORDER = (
    "source_inactive",
    "source_policy_mismatch",
    "source_account_mismatch",
    "source_container_mismatch",
    "shared_task_not_allowed",
    "required_label_missing",
    "task_already_completed",
    "opt_in_record_missing",
    "opt_in_record_ambiguous",
    "opt_in_record_invalid",
    "request_bundle_mismatch",
    "idempotency_binding_mismatch",
    "shadow_blocked",
    "runtime_activation_disabled",
    "explicit_confirmation_required",
    "trigger_state_conflict",
    "pilot_lock_busy",
    "dispatch_lock_busy",
    "dispatch_blocked",
)


class TodoistPilotContractError(ValueError):
    pass


def load_todoist_source(path: str | Path) -> dict[str, Any]:
    return validate_todoist_source(_load_json(path))


def load_todoist_snapshot(path: str | Path) -> dict[str, Any]:
    return validate_todoist_snapshot(_load_json(path))


def load_local_request_bundle(path: str | Path) -> dict[str, Any]:
    return validate_local_request_bundle(_load_json(path))


def validate_todoist_source(value: object) -> dict[str, Any]:
    obj = _exact_object(value, SOURCE_KEYS)
    _contract(obj, SOURCE_CONTRACT)
    active = obj["active"]
    if not isinstance(active, bool):
        raise TodoistPilotContractError("source active must be boolean")
    if obj["require_unshared"] is not True:
        raise TodoistPilotContractError("source must require unshared tasks")
    parent_id = obj["parent_id"]
    if parent_id is not None:
        parent_id = _safe_id(parent_id)
    return {
        "schema_version": 1,
        "contract": SOURCE_CONTRACT,
        "active": active,
        "source_id": _safe_id(obj["source_id"]),
        "adapter_revision": _safe_id(obj["adapter_revision"]),
        "account_id": _safe_id(obj["account_id"]),
        "project_id": _safe_id(obj["project_id"]),
        "parent_id": parent_id,
        "required_label": _safe_id(obj["required_label"]),
        "require_unshared": True,
    }


def validate_todoist_snapshot(value: object) -> dict[str, Any]:
    obj = _exact_object(value, SNAPSHOT_KEYS)
    _contract(obj, SNAPSHOT_CONTRACT)
    parent_id = obj["parent_id"]
    if parent_id is not None:
        parent_id = _safe_id(parent_id)
    labels = obj["labels"]
    if not isinstance(labels, list) or not all(
        isinstance(item, str) for item in labels
    ):
        raise TodoistPilotContractError("snapshot labels must be a string list")
    if len(labels) > 32 or len(labels) != len(set(labels)):
        raise TodoistPilotContractError("snapshot labels are invalid")
    description = obj["description"]
    if (
        not isinstance(description, str)
        or len(description.encode("utf-8")) > MAX_INPUT_BYTES
    ):
        raise TodoistPilotContractError("snapshot description is invalid")
    checked = obj["checked"]
    if not isinstance(checked, bool):
        raise TodoistPilotContractError("snapshot checked must be boolean")
    shared = obj["shared"]
    if not isinstance(shared, bool):
        raise TodoistPilotContractError("snapshot shared must be boolean")
    observed_at = obj["observed_at"]
    if not isinstance(observed_at, str):
        raise TodoistPilotContractError("snapshot observed_at is invalid")
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TodoistPilotContractError("snapshot observed_at is invalid") from exc
    if parsed.tzinfo is None:
        raise TodoistPilotContractError("snapshot observed_at requires timezone")
    return {
        "schema_version": 1,
        "contract": SNAPSHOT_CONTRACT,
        "task_id": _safe_id(obj["task_id"]),
        "account_id": _safe_id(obj["account_id"]),
        "project_id": _safe_id(obj["project_id"]),
        "parent_id": parent_id,
        "labels": sorted(_safe_id(item) for item in labels),
        "description": description,
        "checked": checked,
        "shared": shared,
        "observed_at": parsed.isoformat(),
    }


def validate_local_request_bundle(value: object) -> dict[str, Any]:
    obj = _exact_object(value, BUNDLE_KEYS)
    _contract(obj, BUNDLE_CONTRACT)
    return {
        "schema_version": 1,
        "contract": BUNDLE_CONTRACT,
        "request_id": _safe_id(obj["request_id"]),
        "manifest_path": _private_file(obj["manifest_path"]),
        "execution_envelope_path": _private_file(obj["execution_envelope_path"]),
    }


def parse_opt_in(description: str) -> tuple[dict[str, str] | None, str | None]:
    matches = [
        line[len(OPT_IN_PREFIX) :]
        for line in description.splitlines()
        if line.startswith(OPT_IN_PREFIX)
    ]
    if not matches:
        return None, "opt_in_record_missing"
    if len(matches) != 1:
        return None, "opt_in_record_ambiguous"
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError:
        return None, "opt_in_record_invalid"
    if not isinstance(value, dict) or set(value) != OPT_IN_KEYS:
        return None, "opt_in_record_invalid"
    try:
        created_at = value["created_at"]
        if not isinstance(created_at, str):
            raise TodoistPilotContractError("opt-in time invalid")
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise TodoistPilotContractError("opt-in time requires timezone")
        return {
            "request_id": _safe_id(value["request_id"]),
            "opt_in_token": _safe_id(value["opt_in_token"]),
            "created_at": parsed.isoformat(),
        }, None
    except (TodoistPilotContractError, ValueError):
        return None, "opt_in_record_invalid"


def build_trigger(
    *,
    source: dict[str, Any],
    snapshot: dict[str, Any],
    opt_in: dict[str, str],
    policy: dict[str, Any],
    manifest: dict[str, Any],
    envelope: dict[str, Any],
) -> dict[str, Any]:
    from .orchestration import build_orchestration_plan
    from .orchestration_dispatch import identity_for

    event_digest = hashlib.sha256(
        (
            SNAPSHOT_CONTRACT
            + "\0"
            + snapshot["task_id"]
            + "\0"
            + opt_in["opt_in_token"]
        ).encode("utf-8")
    ).hexdigest()[:32]
    source_event_id = "todoist-event-" + event_digest
    trigger_id = trigger_id_for(source["source_id"], source_event_id)
    plan = build_orchestration_plan(manifest)
    identity = identity_for(manifest, envelope)
    return {
        "schema_version": 1,
        "contract": TRIGGER_CONTRACT,
        "trigger_id": trigger_id,
        "source_id": source["source_id"],
        "source_adapter_revision": source["adapter_revision"],
        "source_event_id": source_event_id,
        "explicit_opt_in": True,
        "policy_id": policy["policy_id"],
        "policy_revision": policy["revision"],
        "policy_fingerprint": policy_fingerprint(policy),
        "request_id": manifest["request_id"],
        "request_fingerprint": plan["request_fingerprint"],
        "execution_fingerprint": identity["execution_fingerprint"],
        "created_at": opt_in["created_at"],
    }


def reconcile_todoist_pilot(
    config: Config,
    *,
    source: dict[str, Any],
    snapshot: dict[str, Any],
    bundle: dict[str, Any],
    policy: dict[str, Any],
    apply: bool,
    confirm_trigger_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    source = validate_todoist_source(source)
    snapshot = validate_todoist_snapshot(snapshot)
    bundle = validate_local_request_bundle(bundle)
    policy = validate_guard_policy(policy)
    reasons: set[str] = set()
    if not source["active"]:
        reasons.add("source_inactive")
    if (
        source["source_id"] != policy["source"]["source_id"]
        or source["adapter_revision"] != policy["source"]["adapter_revision"]
    ):
        reasons.add("source_policy_mismatch")
    if snapshot["account_id"] != source["account_id"]:
        reasons.add("source_account_mismatch")
    if (
        snapshot["project_id"] != source["project_id"]
        or snapshot["parent_id"] != source["parent_id"]
    ):
        reasons.add("source_container_mismatch")
    if source["require_unshared"] and snapshot["shared"]:
        reasons.add("shared_task_not_allowed")
    if source["required_label"] not in snapshot["labels"]:
        reasons.add("required_label_missing")
    if snapshot["checked"]:
        reasons.add("task_already_completed")
    opt_in, opt_in_error = parse_opt_in(snapshot["description"])
    if opt_in_error:
        reasons.add(opt_in_error)
    if reasons or opt_in is None:
        return _result(None, reasons, apply=apply), False
    if opt_in["request_id"] != bundle["request_id"]:
        reasons.add("request_bundle_mismatch")

    try:
        manifest = load_manifest(bundle["manifest_path"])
        envelope = load_execution_envelope(bundle["execution_envelope_path"])
    except (OSError, ValueError, json.JSONDecodeError):
        return _error("private_request_bundle_invalid"), False
    if (
        manifest["request_id"] != bundle["request_id"]
        or envelope["request_id"] != bundle["request_id"]
    ):
        reasons.add("request_bundle_mismatch")
    trigger = build_trigger(
        source=source,
        snapshot=snapshot,
        opt_in=opt_in,
        policy=policy,
        manifest=manifest,
        envelope=envelope,
    )
    if manifest["idempotency_key"] != guard_idempotency_key(trigger["trigger_id"]):
        reasons.add("idempotency_binding_mismatch")
    if not apply:
        return _reconcile_bound(
            config,
            trigger=trigger,
            manifest=manifest,
            envelope=envelope,
            policy=policy,
            reasons=reasons,
            apply=False,
            confirm_trigger_id=confirm_trigger_id,
        )
    if policy["activation_mode"] == "guarded" and (
        not config.guarded_orchestration_enabled
        or confirm_trigger_id != trigger["trigger_id"]
    ):
        return _reconcile_bound(
            config,
            trigger=trigger,
            manifest=manifest,
            envelope=envelope,
            policy=policy,
            reasons=reasons,
            apply=True,
            confirm_trigger_id=confirm_trigger_id,
        )
    pilot_lock = FileLock(
        config.log_dir.parent / "orchestration-todoist-pilot.lock",
        config.stale_lock_seconds,
    )
    if not pilot_lock.acquire(task_id=trigger["trigger_id"]):
        shadow = build_reconciliation_shadow(
            config,
            policy=policy,
            trigger=trigger,
            manifest=manifest,
            envelope=envelope,
            allow_guarded_activation=True,
        )
        return _result(
            trigger,
            reasons | {"pilot_lock_busy"},
            apply=True,
            shadow=shadow,
        ), False
    try:
        return _reconcile_bound(
            config,
            trigger=trigger,
            manifest=manifest,
            envelope=envelope,
            policy=policy,
            reasons=reasons,
            apply=True,
            confirm_trigger_id=confirm_trigger_id,
        )
    finally:
        pilot_lock.release()


def _reconcile_bound(
    config: Config,
    *,
    trigger: dict[str, Any],
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    policy: dict[str, Any],
    reasons: set[str],
    apply: bool,
    confirm_trigger_id: str | None,
) -> tuple[dict[str, Any], bool]:
    reasons = set(reasons)
    shadow = build_reconciliation_shadow(
        config,
        policy=policy,
        trigger=trigger,
        manifest=manifest,
        envelope=envelope,
        allow_guarded_activation=True,
    )
    if shadow["decision_status"] == "blocked":
        reasons.add("shadow_blocked")
    if policy["activation_mode"] == "guarded":
        if not config.guarded_orchestration_enabled:
            reasons.add("runtime_activation_disabled")
        if apply and confirm_trigger_id != trigger["trigger_id"]:
            reasons.add("explicit_confirmation_required")

    if reasons:
        report = _result(trigger, reasons, apply=apply, shadow=shadow)
        if apply and policy["activation_mode"] == "shadow":
            persisted = _persist_trigger(config, trigger)
            if not persisted:
                report = _result(
                    trigger,
                    reasons | {"trigger_state_conflict"},
                    apply=apply,
                    shadow=shadow,
                )
            else:
                _persist_reconciliation(config, report)
                report["mutation"]["durable_state"] = True
        return report, False

    if not apply:
        return _result(trigger, set(), apply=False, shadow=shadow), True

    if not _persist_trigger(config, trigger):
        return _result(
            trigger,
            {"trigger_state_conflict"},
            apply=True,
            shadow=shadow,
        ), False

    admission_preexisting = shadow["state"]["queue_admission"] == "admitted"
    admitted = False
    if policy["activation_mode"] == "guarded":
        try:
            _, admitted = apply_dispatch(config, manifest, envelope)
        except DispatchLockBusy:
            reasons.add("dispatch_lock_busy")
        if not admitted and not reasons:
            reasons.add("dispatch_blocked")
        shadow = build_reconciliation_shadow(
            config,
            policy=policy,
            trigger=trigger,
            manifest=manifest,
            envelope=envelope,
            allow_guarded_activation=True,
        )
    report = _result(trigger, reasons, apply=True, shadow=shadow)
    report["mutation"]["durable_state"] = True
    report["mutation"]["queue_admission"] = admitted and not admission_preexisting
    _persist_disposition_if_terminal(config, report)
    _persist_reconciliation(config, report)
    return report, not reasons


def render_todoist_reconciliation(report: dict[str, Any]) -> str:
    lines = [str(report["contract"])]
    for key in ("decision_status", "trigger_id", "request_id"):
        if report.get(key) is not None:
            lines.append(f"{key}: {report[key]}")
    lines.append(
        "reason_codes: " + (", ".join(report.get("reason_codes", [])) or "none")
    )
    state = report.get("state")
    if isinstance(state, dict):
        for key in (
            "queue_admission",
            "execution",
            "review",
            "apply",
            "attention_delivery",
            "attention_acknowledgement",
            "source_disposition",
        ):
            lines.append(f"{key}: {state.get(key)}")
    mutation = report.get("mutation", {})
    lines.append(
        "mutation: "
        f"durable_state={str(bool(mutation.get('durable_state'))).lower()} "
        f"queue_admission={str(bool(mutation.get('queue_admission'))).lower()} "
        "todoist=false external_delivery=false"
    )
    return "\n".join(lines) + "\n"


def _result(
    trigger: dict[str, Any] | None,
    reasons: set[str],
    *,
    apply: bool,
    shadow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ordered = [code for code in REASON_ORDER if code in reasons]
    return {
        "schema_version": 1,
        "contract": RESULT_CONTRACT,
        "trigger_id": trigger["trigger_id"] if trigger else None,
        "request_id": trigger["request_id"] if trigger else None,
        "decision_status": (
            "blocked"
            if ordered
            else (
                "eligible_guarded"
                if shadow and shadow["decision_status"] == "eligible_guarded"
                else "eligible_shadow"
            )
        ),
        "reason_codes": ordered,
        "state": shadow["state"] if shadow else _empty_state(),
        "source_disposition": "not_mutated",
        "required_action": (
            "resolve_blockers"
            if ordered
            else (
                "apply_guarded_admission"
                if not apply
                and shadow
                and shadow["decision_status"] == "eligible_guarded"
                else "observe"
            )
        ),
        "mutation": {
            "requested": apply,
            "durable_state": False,
            "queue_admission": False,
            "todoist": False,
            "external_delivery": False,
        },
    }


def _error(code: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract": ERROR_CONTRACT,
        "decision_status": "invalid",
        "reason_codes": [code],
        "mutation": {
            "requested": False,
            "durable_state": False,
            "queue_admission": False,
            "todoist": False,
            "external_delivery": False,
        },
    }


def _persist_trigger(config: Config, trigger: dict[str, Any]) -> bool:
    path = _trigger_dir(config) / f"{trigger['trigger_id']}.json"
    try:
        write_json_atomic_create(path, trigger)
        return True
    except FileExistsError:
        try:
            return read_json(path, None) == trigger
        except (OSError, json.JSONDecodeError):
            return False


def _persist_reconciliation(config: Config, report: dict[str, Any]) -> bool:
    trigger_id = report.get("trigger_id")
    if not isinstance(trigger_id, str):
        return False
    lock = FileLock(
        config.log_dir.parent / "orchestration-reconciliation.lock",
        config.stale_lock_seconds,
    )
    if not lock.acquire(task_id=trigger_id):
        return False
    try:
        path = _reconciliation_dir(config) / f"{trigger_id}.json"
        try:
            existing = read_json(path, None)
        except (OSError, json.JSONDecodeError) as exc:
            raise TodoistPilotContractError("reconciliation state unreadable") from exc
        if existing is not None and (
            not isinstance(existing, dict)
            or existing.get("contract") != RESULT_CONTRACT
            or existing.get("trigger_id") != trigger_id
        ):
            raise TodoistPilotContractError("reconciliation state conflict")
        if isinstance(existing, dict) and _report_rank(existing) > _report_rank(report):
            return True
        write_json_atomic(path, report)
        return True
    finally:
        lock.release()


def _persist_disposition_if_terminal(config: Config, report: dict[str, Any]) -> None:
    state = report.get("state")
    trigger_id = report.get("trigger_id")
    if not isinstance(state, dict) or not isinstance(trigger_id, str):
        return
    execution = state.get("execution")
    if execution not in {"completed", "failed", "blocked_user"}:
        return
    projection = {
        "trigger_id": trigger_id,
        "request_id": report.get("request_id"),
        "execution": execution,
        "review": state.get("review"),
        "apply": state.get("apply"),
        "attention_delivery": state.get("attention_delivery"),
        "attention_acknowledgement": state.get("attention_acknowledgement"),
    }
    disposition_id = (
        "sd-"
        + hashlib.sha256(
            json.dumps(projection, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:32]
    )
    record = {
        "schema_version": 1,
        "contract": DISPOSITION_CONTRACT,
        "disposition_id": disposition_id,
        **projection,
        "delivery": {
            "state": "withheld",
            "reason": "external_coordination_mutation_not_authorized",
        },
    }
    path = _disposition_dir(config) / f"{disposition_id}.json"
    try:
        write_json_atomic_create(path, record)
    except FileExistsError:
        try:
            if read_json(path, None) != record:
                raise TodoistPilotContractError("disposition state conflict")
        except (OSError, json.JSONDecodeError) as exc:
            raise TodoistPilotContractError("disposition state conflict") from exc
    report["state"]["source_disposition"] = "withheld"
    report["source_disposition"] = "withheld"


def _trigger_dir(config: Config) -> Path:
    return config.log_dir.parent / "orchestration-trigger-inbox"


def _reconciliation_dir(config: Config) -> Path:
    return config.log_dir.parent / "orchestration-reconciliation"


def _disposition_dir(config: Config) -> Path:
    return config.log_dir.parent / "orchestration-disposition-outbox"


def _empty_state() -> dict[str, str]:
    return {
        "queue_admission": "not_evaluated",
        "execution": "not_evaluated",
        "review": "not_evaluated",
        "apply": "not_evaluated",
        "attention_delivery": "not_evaluated",
        "attention_acknowledgement": "not_evaluated",
        "attention_state_health": "not_evaluated",
        "source_disposition": "not_observed",
    }


def _report_rank(report: dict[str, Any]) -> tuple[int, int, int, int]:
    state = report.get("state")
    if not isinstance(state, dict):
        state = {}
    admission = {
        "not_evaluated": 0,
        "not_admitted": 1,
        "receipt_recovery_required": 2,
        "admitted": 3,
        "conflict": 4,
    }.get(state.get("queue_admission"), 0)
    execution = {
        "not_evaluated": 0,
        "not_started": 1,
        "runnable": 2,
        "running": 3,
        "needs_resume": 3,
        "completed": 4,
        "failed": 4,
        "blocked_user": 4,
    }.get(state.get("execution"), 0)
    success = (
        1
        if report.get("decision_status")
        in {
            "eligible_shadow",
            "eligible_guarded",
        }
        else 0
    )
    acknowledgement = {
        "not_evaluated": 0,
        "not_acknowledged": 1,
        "pending": 2,
        "acknowledged": 3,
    }.get(state.get("attention_acknowledgement"), 0)
    return admission, execution, success, acknowledgement


def _load_json(path: str | Path) -> object:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise TodoistPilotContractError("input unreadable") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise TodoistPilotContractError("input too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TodoistPilotContractError("input invalid") from exc


def _exact_object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise TodoistPilotContractError("fields invalid")
    return value


def _contract(value: dict[str, Any], contract: str) -> None:
    if value["schema_version"] != 1 or value["contract"] != contract:
        raise TodoistPilotContractError("contract invalid")


def _safe_id(value: object) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise TodoistPilotContractError("identifier invalid")
    return value


def _private_file(value: object) -> str:
    if not isinstance(value, str):
        raise TodoistPilotContractError("private file invalid")
    path = Path(value)
    if not path.is_absolute() or "~" in path.parts:
        raise TodoistPilotContractError("private file invalid")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise TodoistPilotContractError("private file invalid") from exc
    if not resolved.is_file():
        raise TodoistPilotContractError("private file invalid")
    return str(resolved)

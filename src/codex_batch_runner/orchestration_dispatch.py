"""Explicit, idempotent dispatch of a validated orchestration plan to CBR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .fs import read_json, write_json_atomic
from .lock import FileLock
from .orchestration import build_orchestration_plan, validate_manifest
from .queue import (
    create_task,
    dependency_status,
    running_capacity,
    task_path,
)
from .state import is_runner_paused, normalize_runner_pause
from .timeutil import iso_now


ENVELOPE_CONTRACT = "orchestration-cbr-execution-v1"
PREVIEW_CONTRACT = "orchestration-dispatch-preview-v1"
RECEIPT_CONTRACT = "orchestration-dispatch-receipt-v1"
ERROR_CONTRACT = "orchestration-dispatch-error-v1"
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_PROMPT_BYTES = 128 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
VERIFICATION_ORDER = (
    "docs",
    "lint",
    "typecheck",
    "unit",
    "integration",
    "e2e",
    "smoke",
    "manual",
    "build",
)
PRIORITIES = {"asap", "high", "normal", "low", "background"}
ROOT_KEYS = {
    "schema_version",
    "contract",
    "request_id",
    "request_fingerprint",
    "prompt",
    "cwd",
    "origin_parent_ref",
    "task",
}
TASK_KEYS = {
    "title",
    "description",
    "project_id",
    "category",
    "labels",
    "depends_on",
    "verification_scope",
    "capacity_pool",
    "priority",
}
FORBIDDEN_FIELDS = {
    "command",
    "argv",
    "shell_command",
    "external_command",
    "credential",
    "credentials",
    "environment",
    "env",
    "session_id",
    "thread_id",
    "transcript",
    "log",
    "metadata",
}
VALIDATION_ORDER = (
    "envelope_unreadable",
    "envelope_too_large",
    "envelope_not_utf8",
    "envelope_json_invalid",
    "envelope_not_object",
    "envelope_fields_invalid",
    "envelope_value_type_invalid",
    "envelope_value_enum_invalid",
    "envelope_value_bounds_invalid",
    "envelope_sensitive_field_forbidden",
    "envelope_duplicate_item",
)
REASON_ORDER = (
    "plan_not_ready",
    "recommended_surface_not_cbr_batch",
    "decision_authority_incompatible",
    "automation_boundary_incompatible",
    "request_id_mismatch",
    "request_fingerprint_mismatch",
    "worktree_isolation_unavailable",
    "capacity_pool_unknown",
    "dependency_missing",
    "self_dependency",
    "runner_paused",
    "receipt_recovery_required",
    "receipt_without_task",
    "task_identity_conflict",
    "receipt_identity_conflict",
)
ADMISSION_ORDER = (
    "dependency_not_ready",
    "max_total_running",
    "max_running_per_project",
    "capacity_pool_full",
)
IMMUTABLE_RECEIPT_KEYS = {
    "schema_version",
    "contract",
    "dispatch_id",
    "request_id",
    "request_fingerprint",
    "execution_fingerprint",
    "surface",
    "task_id",
    "admission_result",
    "created_at",
    "origin_parent_linked",
    "queue_admission",
    "completion_state_at_dispatch",
    "parent_attention_state_at_dispatch",
    "mutation",
}


class ExecutionEnvelopeError(ValueError):
    def __init__(self, *codes: str):
        self.codes = tuple(code for code in VALIDATION_ORDER if code in set(codes))
        super().__init__(", ".join(self.codes))


class DispatchLockBusy(RuntimeError):
    pass


def load_execution_envelope(path: str | Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ExecutionEnvelopeError("envelope_unreadable") from exc
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise ExecutionEnvelopeError("envelope_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ExecutionEnvelopeError("envelope_not_utf8") from exc
    except json.JSONDecodeError as exc:
        raise ExecutionEnvelopeError("envelope_json_invalid") from exc
    return validate_execution_envelope(value)


def validate_execution_envelope(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionEnvelopeError("envelope_not_object")
    normalized = _nfc(value)
    if _contains_forbidden_key(normalized):
        raise ExecutionEnvelopeError("envelope_sensitive_field_forbidden")
    if set(normalized) != ROOT_KEYS:
        raise ExecutionEnvelopeError("envelope_fields_invalid")
    task = normalized.get("task")
    if not isinstance(task, dict):
        raise ExecutionEnvelopeError("envelope_value_type_invalid")
    if set(task) != TASK_KEYS:
        raise ExecutionEnvelopeError("envelope_fields_invalid")
    if (
        normalized.get("schema_version") != 1
        or normalized.get("contract") != ENVELOPE_CONTRACT
    ):
        raise ExecutionEnvelopeError("envelope_fields_invalid")

    request_id = _safe_id(normalized.get("request_id"))
    request_fingerprint = _fingerprint_value(normalized.get("request_fingerprint"))
    prompt = _bounded_string(normalized.get("prompt"), max_chars=None, preserve=True)
    if not prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid")
    cwd = _canonical_cwd(normalized.get("cwd"))
    origin_parent_ref = _bounded_string(
        normalized.get("origin_parent_ref"), max_chars=512, preserve=True
    )
    if not origin_parent_ref:
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid")

    title = _clean_text(task.get("title"), required=True, max_chars=80)
    description = (
        None
        if task.get("description") is None
        else _clean_text(task.get("description"), required=True, max_chars=2048)
    )
    project_id = _safe_id(task.get("project_id"))
    category = None if task.get("category") is None else _safe_id(task.get("category"))
    labels = _safe_id_list(task.get("labels"))
    dependencies = _safe_id_list(task.get("depends_on"))
    verification = _verification_list(task.get("verification_scope"))
    capacity_pool = _safe_id(task.get("capacity_pool"))
    priority = task.get("priority")
    if not isinstance(priority, str):
        raise ExecutionEnvelopeError("envelope_value_type_invalid")
    if priority not in PRIORITIES:
        raise ExecutionEnvelopeError("envelope_value_enum_invalid")
    return {
        "schema_version": 1,
        "contract": ENVELOPE_CONTRACT,
        "request_id": request_id,
        "request_fingerprint": request_fingerprint,
        "prompt": prompt,
        "cwd": cwd,
        "origin_parent_ref": origin_parent_ref,
        "task": {
            "title": title,
            "description": description,
            "project_id": project_id,
            "category": category,
            "labels": sorted(labels),
            "depends_on": sorted(dependencies),
            "verification_scope": [
                item for item in VERIFICATION_ORDER if item in verification
            ],
            "capacity_pool": capacity_pool,
            "priority": priority,
        },
    }


def identity_for(manifest: dict[str, Any], envelope: dict[str, Any]) -> dict[str, str]:
    projection = execution_projection(envelope)
    execution_fingerprint = (
        "sha256:" + hashlib.sha256(_canonical_bytes(projection)).hexdigest()
    )
    digest = hashlib.sha256(
        b"cbr-orchestration-dispatch-v1\0"
        + unicodedata.normalize("NFC", manifest["idempotency_key"]).encode("utf-8")
    ).hexdigest()
    identity = {
        "contract": "orchestration-dispatch-v1",
        "idempotency_digest": digest,
        "surface": "cbr_batch",
    }
    dispatch_suffix = hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:32]
    return {
        "dispatch_id": "od-" + dispatch_suffix,
        "task_id": "orch-" + dispatch_suffix,
        "execution_fingerprint": execution_fingerprint,
    }


def execution_projection(envelope: dict[str, Any]) -> dict[str, Any]:
    task = envelope["task"]
    return {
        "prompt": envelope["prompt"],
        "cwd": envelope["cwd"],
        "origin_parent_ref": envelope["origin_parent_ref"],
        "title": task["title"],
        "description": task["description"],
        "project_id": task["project_id"],
        "category": task["category"],
        "labels": task["labels"],
        "depends_on": task["depends_on"],
        "verification_scope": task["verification_scope"],
        "capacity_pool": task["capacity_pool"],
        "priority": task["priority"],
        "execution_backend": "codex",
    }


def build_dispatch_preview(
    config: Config,
    manifest: dict[str, Any],
    envelope: dict[str, Any],
) -> dict[str, Any]:
    manifest = validate_manifest(manifest)
    envelope = validate_execution_envelope(envelope)
    plan = build_orchestration_plan(manifest)
    identity = identity_for(manifest, envelope)
    return _evaluate(config, manifest, envelope, plan, identity, locked=False)


def apply_dispatch(
    config: Config,
    manifest: dict[str, Any],
    envelope: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    identity = identity_for(manifest, envelope)
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire(task_id=identity["task_id"]):
        raise DispatchLockBusy()
    try:
        manifest = validate_manifest(manifest)
        envelope = validate_execution_envelope(envelope)
        plan = build_orchestration_plan(manifest)
        identity = identity_for(manifest, envelope)
        preview = _evaluate(config, manifest, envelope, plan, identity, locked=True)
        if preview["status"] in {"blocked", "conflict"}:
            return preview, False
        task_present = preview["task_present"]
        receipt_present = preview["receipt_present"]
        if task_present and receipt_present:
            return _read_receipt(config, identity["dispatch_id"]), True
        if not task_present:
            _create_orchestrated_task(config, envelope, plan, identity)
            admission_result = "created"
        else:
            _emit_recovered_event(config, identity, plan["request_fingerprint"])
            admission_result = "recovered"
        receipt = _receipt(plan, identity, admission_result)
        write_json_atomic(_receipt_path(config, identity["dispatch_id"]), receipt)
        return receipt, True
    finally:
        lock.release()


def dispatch_error(
    *,
    decision_status: str,
    reason_codes: list[str],
    validation_errors: list[str] | None = None,
    request_id: str | None = None,
    dispatch_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract": ERROR_CONTRACT,
        "decision_status": decision_status,
        "request_id": request_id,
        "dispatch_id": dispatch_id,
        "reason_codes": reason_codes,
        "validation_errors": validation_errors or [],
        "mutation": {"allowed": False, "applied": False},
    }


def preview_as_error(preview: dict[str, Any]) -> dict[str, Any]:
    return dispatch_error(
        decision_status=preview["status"],
        reason_codes=preview["reason_codes"],
        request_id=preview["request_id"],
        dispatch_id=preview["dispatch_id"],
    )


def render_dispatch_result(report: dict[str, Any]) -> str:
    contract = report["contract"]
    lines = [contract]
    for key in (
        "decision_status",
        "status",
        "admission_result",
        "request_id",
        "dispatch_id",
        "task_id",
    ):
        if key in report:
            lines.append(f"{key}: {report.get(key) or '-'}")
    lines.append("reasons: " + (", ".join(report.get("reason_codes") or []) or "-"))
    mutation = report["mutation"]
    lines.append(
        f"mutation: allowed={str(bool(mutation['allowed'])).lower()} "
        f"applied={str(bool(mutation['applied'])).lower()}"
    )
    return "\n".join(lines) + "\n"


def _evaluate(
    config: Config,
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    plan: dict[str, Any],
    identity: dict[str, str],
    *,
    locked: bool,
) -> dict[str, Any]:
    reasons: set[str] = set()
    authority = manifest["authority"]
    work = manifest["work"]
    task_data = envelope["task"]
    if plan["decision_status"] != "ready":
        reasons.add("plan_not_ready")
    if plan["recommended_surface"] != "cbr_batch":
        reasons.add("recommended_surface_not_cbr_batch")
    if authority["decision_authority"] not in {
        "delegated_decision",
        "bounded_experiment",
    }:
        reasons.add("decision_authority_incompatible")
    if manifest["automation_boundary"] not in {"manual_only", "bounded_automatic"}:
        reasons.add("automation_boundary_incompatible")
    if envelope["request_id"] != manifest["request_id"]:
        reasons.add("request_id_mismatch")
    if envelope["request_fingerprint"] != plan["request_fingerprint"]:
        reasons.add("request_fingerprint_mismatch")
    if work["isolation"] in {"worktree", "required"} and config.worktree_mode != "task":
        reasons.add("worktree_isolation_unavailable")
    if task_data["capacity_pool"] not in config.capacity_pools:
        reasons.add("capacity_pool_unknown")
    if identity["task_id"] in task_data["depends_on"]:
        reasons.add("self_dependency")

    tasks = _read_tasks(config)
    by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
    if any(dep not in by_id for dep in task_data["depends_on"]):
        reasons.add("dependency_missing")
    if _runner_paused(config, locked=locked):
        reasons.add("runner_paused")

    task = _read_task(config, identity["task_id"])
    receipt_value = _read_receipt_value(config, identity["dispatch_id"])
    task_present = task is not None
    receipt_present = receipt_value is not None
    task_matches = bool(
        isinstance(task, dict) and _task_matches(task, envelope, plan, identity)
    )
    receipt_matches = bool(
        receipt_value and _receipt_matches(receipt_value, plan, identity)
    )
    if task_present and not task_matches:
        reasons.add("task_identity_conflict")
    if receipt_present and not receipt_matches:
        reasons.add("receipt_identity_conflict")
    elif receipt_present and not task_present:
        reasons.add("receipt_without_task")
    elif task_matches and not receipt_present:
        reasons.add("receipt_recovery_required")

    admission = _admission_blockers(config, envelope, tasks, by_id)
    ordered_reasons = [code for code in REASON_ORDER if code in reasons]
    conflict_codes = {
        "receipt_without_task",
        "task_identity_conflict",
        "receipt_identity_conflict",
    }
    blocking_codes = (
        set(ordered_reasons) - {"receipt_recovery_required"} - conflict_codes
    )
    if conflict_codes & set(ordered_reasons):
        status = "conflict"
    elif blocking_codes:
        status = "blocked"
    elif task_matches:
        status = "already_dispatched"
    else:
        status = "ready"
    return {
        "schema_version": 1,
        "contract": PREVIEW_CONTRACT,
        "request_id": manifest["request_id"],
        "dispatch_id": identity["dispatch_id"],
        "request_fingerprint": plan["request_fingerprint"],
        "execution_fingerprint": identity["execution_fingerprint"],
        "surface": "cbr_batch",
        "status": status,
        "task_id": identity["task_id"],
        "reason_codes": ordered_reasons,
        "admission_blockers": admission,
        "task_present": task_present,
        "receipt_present": receipt_present,
        "origin_parent_linked": True,
        "snapshot_consistency": "locked" if locked else "unlocked_read_only",
        "mutation": {"allowed": False, "applied": False},
    }


def _create_orchestrated_task(
    config: Config,
    envelope: dict[str, Any],
    plan: dict[str, Any],
    identity: dict[str, str],
) -> dict[str, Any]:
    task_data = envelope["task"]
    return create_task(
        config,
        envelope["prompt"],
        envelope["cwd"],
        task_id=identity["task_id"],
        depends_on=task_data["depends_on"],
        project_id=task_data["project_id"],
        category=task_data["category"],
        labels=task_data["labels"],
        created_by="orchestration-dispatch",
        title=task_data["title"],
        description=task_data["description"],
        verification_scope=task_data["verification_scope"],
        execution_backend="codex",
        capacity_pool=task_data["capacity_pool"],
        task_priority=task_data["priority"],
        origin_parent_ref=envelope["origin_parent_ref"],
        orchestration_dispatch_id=identity["dispatch_id"],
        orchestration_request_fingerprint=plan["request_fingerprint"],
        orchestration_execution_fingerprint=identity["execution_fingerprint"],
    )


def _emit_recovered_event(
    config: Config,
    identity: dict[str, str],
    request_fingerprint: str,
) -> None:
    from .events import emit_task_event, read_jsonl

    if config.event_dir.exists():
        for path in sorted(config.event_dir.glob("*.jsonl")):
            for event in read_jsonl(path):
                if (
                    event.get("event_type") == "orchestration_task_admitted"
                    and event.get("task_id") == identity["task_id"]
                    and (event.get("payload") or {}).get("dispatch_id")
                    == identity["dispatch_id"]
                ):
                    return

    emit_task_event(
        config,
        "orchestration_task_admitted",
        {"id": identity["task_id"]},
        source="orchestration-dispatch",
        summary="orchestration task admission recovered",
        payload={
            "dispatch_id": identity["dispatch_id"],
            "task_id": identity["task_id"],
            "surface": "cbr_batch",
            "request_fingerprint": request_fingerprint,
            "execution_fingerprint": identity["execution_fingerprint"],
        },
    )


def _receipt(
    plan: dict[str, Any],
    identity: dict[str, str],
    admission_result: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract": RECEIPT_CONTRACT,
        "dispatch_id": identity["dispatch_id"],
        "request_id": plan["request_id"],
        "request_fingerprint": plan["request_fingerprint"],
        "execution_fingerprint": identity["execution_fingerprint"],
        "surface": "cbr_batch",
        "task_id": identity["task_id"],
        "admission_result": admission_result,
        "created_at": iso_now(),
        "origin_parent_linked": True,
        "queue_admission": "queued",
        "completion_state_at_dispatch": "pending",
        "parent_attention_state_at_dispatch": "not_emitted",
        "mutation": {"allowed": True, "applied": True},
    }


def _receipt_matches(
    value: object,
    plan: dict[str, Any],
    identity: dict[str, str],
) -> bool:
    if not isinstance(value, dict) or set(value) != IMMUTABLE_RECEIPT_KEYS:
        return False
    expected = {
        "schema_version": 1,
        "contract": RECEIPT_CONTRACT,
        "dispatch_id": identity["dispatch_id"],
        "request_id": plan["request_id"],
        "request_fingerprint": plan["request_fingerprint"],
        "execution_fingerprint": identity["execution_fingerprint"],
        "surface": "cbr_batch",
        "task_id": identity["task_id"],
        "origin_parent_linked": True,
        "queue_admission": "queued",
        "completion_state_at_dispatch": "pending",
        "parent_attention_state_at_dispatch": "not_emitted",
        "mutation": {"allowed": True, "applied": True},
    }
    if any(value.get(key) != item for key, item in expected.items()):
        return False
    if value.get("admission_result") not in {"created", "recovered"}:
        return False
    created_at = value.get("created_at")
    if not isinstance(created_at, str):
        return False
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _task_matches(
    task: dict[str, Any],
    envelope: dict[str, Any],
    plan: dict[str, Any],
    identity: dict[str, str],
) -> bool:
    expected = execution_projection(envelope)
    actual = {
        "prompt": task.get("prompt"),
        "cwd": task.get("cwd"),
        "origin_parent_ref": task.get("origin_parent_ref"),
        "title": task.get("title"),
        "description": task.get("description"),
        "project_id": task.get("project_id"),
        "category": task.get("category"),
        "labels": task.get("labels"),
        "depends_on": task.get("depends_on"),
        "verification_scope": task.get("verification_scope"),
        "capacity_pool": task.get("capacity_pool"),
        "priority": task.get("task_priority"),
        "execution_backend": task.get("execution_backend"),
    }
    return (
        actual == expected
        and task.get("id") == identity["task_id"]
        and task.get("orchestration_dispatch_id") == identity["dispatch_id"]
        and task.get("orchestration_request_fingerprint") == plan["request_fingerprint"]
        and task.get("orchestration_execution_fingerprint")
        == identity["execution_fingerprint"]
    )


def _admission_blockers(
    config: Config,
    envelope: dict[str, Any],
    tasks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> list[str]:
    found: set[str] = set()
    task_data = envelope["task"]
    candidate = {
        "id": "preview",
        "project_id": task_data["project_id"],
        "project_root": envelope["cwd"],
        "capacity_pool": task_data["capacity_pool"],
        "depends_on": task_data["depends_on"],
    }
    dependencies_exist = all(dep in by_id for dep in task_data["depends_on"])
    if dependencies_exist:
        ready, _ = dependency_status(
            candidate,
            by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )
        if not ready:
            found.add("dependency_not_ready")
    running = running_capacity(tasks)
    if int(running["total"]) >= config.max_total_running:
        found.add("max_total_running")
    by_project = running["by_project"]
    if (
        isinstance(by_project, Counter)
        and by_project[task_data["project_id"]] >= config.max_running_per_project
    ):
        found.add("max_running_per_project")
    by_pool = running["by_pool"]
    pool = task_data["capacity_pool"]
    if (
        pool in config.capacity_pools
        and isinstance(by_pool, Counter)
        and by_pool[pool] >= int(config.capacity_pools[pool]["max_running"])
    ):
        found.add("capacity_pool_full")
    return [code for code in ADMISSION_ORDER if code in found]


def _read_tasks(config: Config) -> list[dict[str, Any]]:
    if not config.queue_dir.exists():
        return []
    tasks: list[dict[str, Any]] = []
    for path in sorted(config.queue_dir.glob("*.json")):
        try:
            value = read_json(path, None)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            tasks.append(value)
    return tasks


def _read_task(config: Config, task_id: str) -> object:
    path = task_path(config, task_id)
    if not path.exists():
        return None
    try:
        value = read_json(path, None)
    except (OSError, json.JSONDecodeError):
        return _MalformedIdentity()
    return value if isinstance(value, dict) else _MalformedIdentity()


def _runner_paused(config: Config, *, locked: bool) -> bool:
    if locked:
        return is_runner_paused(config)
    value = read_json(config.state_file, None)
    state = value if isinstance(value, dict) else {}
    return bool(normalize_runner_pause(state.get("runner_pause")).get("active"))


def _receipt_dir(config: Config) -> Path:
    return config.log_dir.parent / "orchestration-dispatch-receipts"


def _receipt_path(config: Config, dispatch_id: str) -> Path:
    return _receipt_dir(config) / f"{dispatch_id}.json"


def _read_receipt_value(config: Config, dispatch_id: str) -> object:
    path = _receipt_path(config, dispatch_id)
    if not path.exists():
        return None
    try:
        value = read_json(path, None)
    except (OSError, json.JSONDecodeError):
        return _MalformedIdentity()
    return value if isinstance(value, dict) else _MalformedIdentity()


def _read_receipt(config: Config, dispatch_id: str) -> dict[str, Any]:
    value = _read_receipt_value(config, dispatch_id)
    if not isinstance(value, dict):
        raise ValueError("receipt missing after validated identity check")
    return value


def _safe_id(value: object) -> str:
    if not isinstance(value, str):
        raise ExecutionEnvelopeError("envelope_value_type_invalid")
    if not SAFE_ID.fullmatch(value):
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid")
    return value


def _fingerprint_value(value: object) -> str:
    if not isinstance(value, str):
        raise ExecutionEnvelopeError("envelope_value_type_invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid")
    return value


def _bounded_string(
    value: object,
    *,
    max_chars: int | None,
    preserve: bool,
) -> str:
    if not isinstance(value, str):
        raise ExecutionEnvelopeError("envelope_value_type_invalid")
    result = value if preserve else " ".join(value.split())
    if max_chars is not None and len(result) > max_chars:
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid")
    return result


def _clean_text(value: object, *, required: bool, max_chars: int) -> str:
    result = _bounded_string(value, max_chars=max_chars, preserve=False)
    if required and not result:
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid")
    return result


def _canonical_cwd(value: object) -> str:
    if not isinstance(value, str):
        raise ExecutionEnvelopeError("envelope_value_type_invalid")
    if not value or "~" in value or "$" in value or not os.path.isabs(value):
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid")
    try:
        path = Path(value).resolve(strict=True)
    except OSError as exc:
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid") from exc
    if not path.is_dir():
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid")
    return str(path)


def _safe_id_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ExecutionEnvelopeError("envelope_value_type_invalid")
    if len(value) > 32:
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid")
    if any(not isinstance(item, str) for item in value):
        raise ExecutionEnvelopeError("envelope_value_type_invalid")
    if len(value) != len(set(value)):
        raise ExecutionEnvelopeError("envelope_duplicate_item")
    return [_safe_id(item) for item in value]


def _verification_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ExecutionEnvelopeError("envelope_value_type_invalid")
    if len(value) > len(VERIFICATION_ORDER):
        raise ExecutionEnvelopeError("envelope_value_bounds_invalid")
    if any(not isinstance(item, str) for item in value):
        raise ExecutionEnvelopeError("envelope_value_type_invalid")
    if len(value) != len(set(value)):
        raise ExecutionEnvelopeError("envelope_duplicate_item")
    if any(item not in VERIFICATION_ORDER for item in value):
        raise ExecutionEnvelopeError("envelope_value_enum_invalid")
    return list(value)


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_FIELDS:
                return True
            if _contains_forbidden_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc(item) for item in value]
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", key) if isinstance(key, str) else key: _nfc(
                item
            )
            for key, item in value.items()
        }
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class _MalformedIdentity:
    pass

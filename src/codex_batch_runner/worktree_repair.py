from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from .config import Config
from .lock import FileLock, lock_pid, read_lock_metadata
from .queue import load_task, save_task, task_path
from .timeutil import iso_now, parse_time
from .worktree_reconciliation import (
    HEX_DIGEST,
    _digest,
    build_worktree_reconciliation_item,
)


CONTRACT = "worktree-reconciliation-repair-v1"
AUDIT_SUBTYPE = "worktree_reconciliation_exact_repair_v1"
AUDIT_EVENT_TYPE = "task_mutated"
ALLOWLISTED_FIELD = "execution_worktree_status"
ALLOWLISTED_BEFORE = {"retained", "recovery_required"}
ALLOWLISTED_AFTER = "cleaned"
AUDIT_MAX_BYTES = 1024 * 1024
AUDIT_MAX_EVENTS = 64
AUDIT_MAX_LINE_BYTES = 16 * 1024
AUDIT_DIRECTORY = "worktree-reconciliation-repair-v1"


class WorktreeReconciliationRepairError(ValueError):
    pass


def repair_worktree_reconciliation(
    config: Config,
    task_id: str,
    *,
    approved_source_digest: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or apply one exact C1 reconciliation metadata repair."""
    _validate_approval(task_id, approved_source_digest)
    if not apply:
        task = _load_exact_task(config, task_id)
        item = build_worktree_reconciliation_item(config, task)
        existing = _existing_terminal_report(
            config,
            task_id,
            task,
            item,
            approved_source_digest,
            apply=False,
        )
        if existing is not None:
            return existing
        _require_exact_candidate(item, approved_source_digest)
        before = str(task.get(ALLOWLISTED_FIELD) or "")
        preimage, postimage, audit = _transition(task, approved_source_digest)
        return _report(
            task_id=task_id,
            approved_source_digest=approved_source_digest,
            live_source_digest=item["source_snapshot_digest"],
            mode="dry-run",
            action="planned",
            before=before,
            operation_id=audit["operation_id"],
            task_mutation_performed=False,
            audit_event_appended=False,
            recovery_pending=False,
            preimage=preimage,
            postimage=postimage,
        )

    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire(task_id=task_id):
        raise WorktreeReconciliationRepairError(
            "active queue lock blocks reconciliation repair apply"
        )
    try:
        return _apply_locked(config, task_id, approved_source_digest)
    finally:
        lock.release()


def render_worktree_reconciliation_repair(report: dict[str, Any]) -> str:
    return "\n".join(
        (
            f"schema_version: {report['schema_version']}",
            f"mode: {report['mode']}",
            f"task: {report['task_id']}",
            f"action: {report['action']}",
            f"approved_source_digest: {report['approved_source_digest']}",
            f"live_source_digest: {report['live_source_digest']}",
            f"operation_id: {report['operation_id']}",
            "field: execution_worktree_status",
            f"before: {report['metadata_delta']['before']}",
            "after: cleaned",
            "task_mutation_performed: "
            + str(report["mutation"]["task_performed"]).lower(),
            "audit_event_appended: "
            + str(report["mutation"]["audit_event_appended"]).lower(),
        )
    ) + "\n"


def _apply_locked(
    config: Config,
    task_id: str,
    approved_source_digest: str,
) -> dict[str, Any]:
    _require_lock_owner(config)
    task = _load_exact_task(config, task_id)
    item = build_worktree_reconciliation_item(config, task)
    existing = _existing_terminal_report(
        config,
        task_id,
        task,
        item,
        approved_source_digest,
        apply=True,
    )
    if existing is not None:
        return existing
    _require_exact_candidate(item, approved_source_digest)

    preimage, postimage, audit = _transition(task, approved_source_digest)
    matching = _matching_audit_records(
        config,
        task_id,
        approved_source_digest,
        expected=audit,
    )
    if matching["committed"]:
        raise WorktreeReconciliationRepairError(
            "committed repair audit conflicts with live pre-repair task state"
        )

    # Reconstruct and classify from the exact canonical document immediately
    # before the first write. The queue lock and document digest then act as CAS.
    _require_lock_owner(config)
    live_task = _load_exact_task(config, task_id)
    if live_task != preimage:
        raise WorktreeReconciliationRepairError(
            "canonical task drifted before reconciliation repair mutation"
        )
    live_item = build_worktree_reconciliation_item(config, live_task)
    _require_exact_candidate(live_item, approved_source_digest)

    if not matching["prepared"]:
        _append_audit(config, live_task, audit, phase="prepared")

    # The prepared event is durable before the final external-evidence read.
    # Rebuild the full C1 item again so Git registry/branch/pool drift after
    # preparation cannot be hidden by a task-document-only CAS.
    live_task = _load_exact_task(config, task_id)
    post_prepare_item = build_worktree_reconciliation_item(config, live_task)
    _require_exact_candidate(post_prepare_item, approved_source_digest)
    if live_task != preimage:
        raise WorktreeReconciliationRepairError(
            "canonical task CAS changed after repair preparation"
        )
    _require_lock_owner(config)
    _save_exact_task_durable(config, task_id, deepcopy(postimage))
    readback = _load_exact_task(config, task_id)
    if readback != postimage:
        raise WorktreeReconciliationRepairError(
            "reconciliation repair task readback verification failed"
        )

    _append_audit(config, readback, audit, phase="committed", recovered=False)
    if _load_exact_task(config, task_id) != postimage:
        raise WorktreeReconciliationRepairError(
            "reconciliation repair post-audit readback verification failed"
        )
    return _report(
        task_id=task_id,
        approved_source_digest=approved_source_digest,
        live_source_digest=approved_source_digest,
        mode="apply",
        action="applied",
        before=audit["before"],
        operation_id=audit["operation_id"],
        task_mutation_performed=True,
        audit_event_appended=True,
        recovery_pending=False,
        preimage=preimage,
        postimage=postimage,
    )


def _existing_terminal_report(
    config: Config,
    requested_task_id: str,
    task: dict[str, Any],
    item: dict[str, Any],
    approved_source_digest: str,
    *,
    apply: bool,
) -> dict[str, Any] | None:
    if task.get(ALLOWLISTED_FIELD) != ALLOWLISTED_AFTER:
        return None
    if not (
        item["action_class"] == "no_action"
        and item["reason_codes"] == ["terminal_cleanup_current"]
    ):
        raise WorktreeReconciliationRepairError(
            "already-cleaned task has stale or adverse live reconciliation evidence"
        )

    candidates = _audit_candidates(config, requested_task_id, approved_source_digest)
    if not candidates:
        raise WorktreeReconciliationRepairError(
            "approved source digest does not identify an existing exact repair"
        )
    valid: list[tuple[dict[str, Any], dict[str, bool]]] = []
    for audit in candidates:
        if audit["before"] not in ALLOWLISTED_BEFORE:
            raise WorktreeReconciliationRepairError("repair audit before state is invalid")
        preimage = deepcopy(task)
        preimage[ALLOWLISTED_FIELD] = audit["before"]
        postimage = deepcopy(task)
        expected = _audit_payload(preimage, postimage, approved_source_digest)
        if audit != expected:
            raise WorktreeReconciliationRepairError(
                "repair audit does not match the exact live task postimage"
            )
        phases = _matching_audit_records(
            config,
            requested_task_id,
            approved_source_digest,
            expected=expected,
        )
        valid.append((expected, phases))
    operation_ids = {audit["operation_id"] for audit, _ in valid}
    if len(operation_ids) != 1:
        raise WorktreeReconciliationRepairError(
            "multiple exact repair operations conflict for one task and source digest"
        )
    audit, phases = valid[0]
    if not phases["prepared"]:
        raise WorktreeReconciliationRepairError(
            "cleaned repair state is missing its prepared audit event"
        )
    if phases["committed"]:
        return _report(
            task_id=requested_task_id,
            approved_source_digest=approved_source_digest,
            live_source_digest=item["source_snapshot_digest"],
            mode="apply" if apply else "dry-run",
            action="noop",
            before=audit["before"],
            operation_id=audit["operation_id"],
            task_mutation_performed=False,
            audit_event_appended=False,
            recovery_pending=False,
            preimage={**task, ALLOWLISTED_FIELD: audit["before"]},
            postimage=task,
        )
    if not apply:
        return _report(
            task_id=requested_task_id,
            approved_source_digest=approved_source_digest,
            live_source_digest=item["source_snapshot_digest"],
            mode="dry-run",
            action="recovery_pending",
            before=audit["before"],
            operation_id=audit["operation_id"],
            task_mutation_performed=False,
            audit_event_appended=False,
            recovery_pending=True,
            preimage={**task, ALLOWLISTED_FIELD: audit["before"]},
            postimage=task,
        )

    _require_lock_owner(config)
    if _load_exact_task(config, requested_task_id) != task:
        raise WorktreeReconciliationRepairError(
            "canonical task drifted before repair audit recovery"
        )
    _append_audit(config, task, audit, phase="committed", recovered=True)
    if _load_exact_task(config, requested_task_id) != task:
        raise WorktreeReconciliationRepairError(
            "recovered repair audit task readback verification failed"
        )
    return _report(
        task_id=requested_task_id,
        approved_source_digest=approved_source_digest,
        live_source_digest=item["source_snapshot_digest"],
        mode="apply",
        action="recovered",
        before=audit["before"],
        operation_id=audit["operation_id"],
        task_mutation_performed=False,
        audit_event_appended=True,
        recovery_pending=False,
        preimage={**task, ALLOWLISTED_FIELD: audit["before"]},
        postimage=task,
    )


def _transition(
    task: dict[str, Any],
    approved_source_digest: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    before = task.get(ALLOWLISTED_FIELD)
    if before not in ALLOWLISTED_BEFORE:
        raise WorktreeReconciliationRepairError(
            "live task is outside the allowlisted repair transition"
        )
    preimage = deepcopy(task)
    postimage = deepcopy(task)
    postimage[ALLOWLISTED_FIELD] = ALLOWLISTED_AFTER
    return preimage, postimage, _audit_payload(
        preimage, postimage, approved_source_digest
    )


def _audit_payload(
    preimage: dict[str, Any],
    postimage: dict[str, Any],
    approved_source_digest: str,
) -> dict[str, Any]:
    task_id = str(preimage.get("id") or "")
    before = str(preimage.get(ALLOWLISTED_FIELD) or "")
    preimage_digest = _digest(preimage)
    postimage_digest = _digest(postimage)
    operation_id = _digest(
        {
            "schema_version": CONTRACT,
            "task_id": task_id,
            "approved_source_digest": approved_source_digest,
            "task_preimage_digest": preimage_digest,
            "task_postimage_digest": postimage_digest,
            "field": ALLOWLISTED_FIELD,
            "before": before,
            "after": ALLOWLISTED_AFTER,
        }
    )
    return {
        "repair_contract": CONTRACT,
        "subtype": AUDIT_SUBTYPE,
        "operation_id": operation_id,
        "approved_source_digest": approved_source_digest,
        "task_preimage_digest": preimage_digest,
        "task_postimage_digest": postimage_digest,
        "field": ALLOWLISTED_FIELD,
        "before": before,
        "after": ALLOWLISTED_AFTER,
    }


def _append_audit(
    config: Config,
    task: dict[str, Any],
    audit: dict[str, Any],
    *,
    phase: str,
    recovered: bool | None = None,
) -> None:
    existing = _matching_audit_records(
        config,
        str(task["id"]),
        str(audit["approved_source_digest"]),
        expected=audit,
    )
    if phase == "prepared" and (existing["prepared"] or existing["committed"]):
        raise WorktreeReconciliationRepairError(
            "prepared repair audit phase already exists"
        )
    if phase == "committed" and (
        not existing["prepared"] or existing["committed"]
    ):
        raise WorktreeReconciliationRepairError(
            "committed repair audit requires one prepared phase and no commit"
        )
    payload: dict[str, Any] = {**audit, "phase": phase}
    if phase == "committed":
        payload["recovered_from_partial"] = bool(recovered)
    event = _audit_event(str(task["id"]), payload)
    _append_jsonl_durable(_audit_path(config, str(task["id"])), event)


def _audit_candidates(
    config: Config,
    task_id: str,
    approved_source_digest: str,
) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for event in _read_audit_events(config, task_id):
        payload = _validate_audit_event(event, task_id)
        base = _base_audit_payload(payload)
        if base["approved_source_digest"] != approved_source_digest:
            raise WorktreeReconciliationRepairError(
                "repair audit conflicts with the approved source digest"
            )
        operation_id = str(base.get("operation_id") or "")
        if not operation_id:
            raise WorktreeReconciliationRepairError("repair audit operation id is missing")
        previous = found.get(operation_id)
        if previous is not None and previous != base:
            raise WorktreeReconciliationRepairError(
                "repair audit operation payload is inconsistent"
            )
        found[operation_id] = base
    return list(found.values())


def _matching_audit_records(
    config: Config,
    task_id: str,
    approved_source_digest: str,
    *,
    expected: dict[str, Any],
) -> dict[str, bool]:
    phases = {"prepared": False, "committed": False}
    for event in _read_audit_events(config, task_id):
        payload = _validate_audit_event(event, task_id)
        if payload.get("approved_source_digest") != approved_source_digest:
            raise WorktreeReconciliationRepairError(
                "repair audit conflicts with the approved source digest"
            )
        if _base_audit_payload(payload) != expected:
            raise WorktreeReconciliationRepairError(
                "repair audit conflicts with the approved exact operation"
            )
        phase = payload.get("phase")
        if phase not in phases:
            raise WorktreeReconciliationRepairError("repair audit phase is invalid")
        expected_keys = set(expected) | {"phase"}
        if phase == "committed":
            expected_keys.add("recovered_from_partial")
            if not isinstance(payload.get("recovered_from_partial"), bool):
                raise WorktreeReconciliationRepairError(
                    "committed repair audit recovery marker is invalid"
                )
        if set(payload) != expected_keys:
            raise WorktreeReconciliationRepairError(
                "repair audit fields are not canonical"
            )
        if phases[phase]:
            raise WorktreeReconciliationRepairError(
                f"duplicate repair audit phase is ambiguous: {phase}"
            )
        if phase == "committed" and not phases["prepared"]:
            raise WorktreeReconciliationRepairError(
                "committed repair audit precedes prepared phase"
            )
        phases[phase] = True
    return phases


def _base_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "repair_contract",
        "subtype",
        "operation_id",
        "approved_source_digest",
        "task_preimage_digest",
        "task_postimage_digest",
        "field",
        "before",
        "after",
    }
    base = {key: payload.get(key) for key in keys}
    if set(payload) - keys - {"phase", "recovered_from_partial"}:
        raise WorktreeReconciliationRepairError(
            "repair audit contains non-canonical fields"
        )
    if (
        base["repair_contract"] != CONTRACT
        or base["subtype"] != AUDIT_SUBTYPE
        or base["field"] != ALLOWLISTED_FIELD
        or base["before"] not in ALLOWLISTED_BEFORE
        or base["after"] != ALLOWLISTED_AFTER
    ):
        raise WorktreeReconciliationRepairError("repair audit contract is invalid")
    for key in (
        "operation_id",
        "approved_source_digest",
        "task_preimage_digest",
        "task_postimage_digest",
    ):
        if not isinstance(base[key], str) or not HEX_DIGEST.fullmatch(base[key]):
            raise WorktreeReconciliationRepairError(f"repair audit {key} is invalid")
    return base


def _audit_event(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    phase = str(payload.get("phase") or "")
    return {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "event_type": AUDIT_EVENT_TYPE,
        "occurred_at": iso_now(),
        "task_id": task_id,
        "project_id": None,
        "project_root": None,
        "actor": "cbr",
        "source": "worktree-reconciliation-repair",
        "summary": f"{phase} exact worktree metadata repair",
        "payload": payload,
    }


def _validate_audit_event(event: object, task_id: str) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "event_id",
        "event_type",
        "occurred_at",
        "task_id",
        "project_id",
        "project_root",
        "actor",
        "source",
        "summary",
        "payload",
    }
    if not isinstance(event, dict) or set(event) != expected_keys:
        raise WorktreeReconciliationRepairError(
            "repair audit event fields are not canonical"
        )
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise WorktreeReconciliationRepairError("repair audit payload is invalid")
    try:
        event_uuid = uuid.UUID(str(event.get("event_id") or ""))
    except ValueError as exc:
        raise WorktreeReconciliationRepairError(
            "repair audit event id is invalid"
        ) from exc
    phase = payload.get("phase")
    if (
        event["schema_version"] != 1
        or str(event_uuid) != event["event_id"]
        or event["event_type"] != AUDIT_EVENT_TYPE
        or parse_time(event.get("occurred_at")) is None
        or event["task_id"] != task_id
        or event["project_id"] is not None
        or event["project_root"] is not None
        or event["actor"] != "cbr"
        or event["source"] != "worktree-reconciliation-repair"
        or event["summary"] != f"{phase} exact worktree metadata repair"
    ):
        raise WorktreeReconciliationRepairError(
            "repair audit event envelope is invalid"
        )
    return payload


def _audit_path(config: Config, task_id: str) -> Path:
    task_ref = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
    return config.event_dir / AUDIT_DIRECTORY / f"{task_ref}.audit"


def _read_audit_events(config: Config, task_id: str) -> list[dict[str, Any]]:
    path = _audit_path(config, task_id)
    opened = _open_audit_namespace(config.event_dir, create=False)
    if opened is None:
        return []
    event_fd, namespace_fd = opened
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            fd = os.open(path.name, flags, dir_fd=namespace_fd)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise WorktreeReconciliationRepairError(
                "repair audit file is unreadable"
            ) from exc
        try:
            _assert_audit_namespace_binding(event_fd, namespace_fd)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise WorktreeReconciliationRepairError(
                    "repair audit path must be a regular non-symlink file"
                )
            _assert_audit_file_binding(namespace_fd, path.name, fd)
            if before.st_size > AUDIT_MAX_BYTES:
                raise WorktreeReconciliationRepairError(
                    "repair audit file exceeds safety bound"
                )
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
            _assert_audit_file_binding(namespace_fd, path.name, fd)
            _assert_audit_namespace_binding(event_fd, namespace_fd)
        except OSError as exc:
            raise WorktreeReconciliationRepairError(
                "repair audit file is unreadable"
            ) from exc
        finally:
            os.close(fd)
    finally:
        os.close(namespace_fd)
        os.close(event_fd)
    if (
        len(raw) != before.st_size
        or after.st_size != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise WorktreeReconciliationRepairError(
            "repair audit file changed during strict read"
        )
    if raw and not raw.endswith(b"\n"):
        raise WorktreeReconciliationRepairError(
            "repair audit has a torn tail; automatic truncation is forbidden"
        )
    lines = raw.splitlines()
    if len(lines) > AUDIT_MAX_EVENTS:
        raise WorktreeReconciliationRepairError("repair audit event count exceeds bound")
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line or len(line) > AUDIT_MAX_LINE_BYTES:
            raise WorktreeReconciliationRepairError(
                "repair audit line is empty or exceeds bound"
            )
        try:
            decoded = line.decode("utf-8", errors="strict")
            event = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorktreeReconciliationRepairError(
                "repair audit contains malformed JSON"
            ) from exc
        if not isinstance(event, dict):
            raise WorktreeReconciliationRepairError(
                "repair audit event must be an object"
            )
        events.append(event)
    return events


def _append_jsonl_durable(path: Path, event: dict[str, Any]) -> None:
    encoded = (
        json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(encoded) > AUDIT_MAX_LINE_BYTES:
        raise WorktreeReconciliationRepairError("repair audit event exceeds bound")
    event_dir = path.parent.parent
    opened = _open_audit_namespace(event_dir, create=True)
    if opened is None:
        raise WorktreeReconciliationRepairError(
            "repair audit namespace could not be created"
        )
    event_fd, namespace_fd = opened
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        # Revalidate after opening the namespace and immediately before the
        # final open. A replaced/renamed namespace fails before file creation.
        _assert_audit_namespace_binding(event_fd, namespace_fd)
        try:
            fd = os.open(path.name, flags, 0o600, dir_fd=namespace_fd)
        except OSError as exc:
            raise WorktreeReconciliationRepairError(
                "repair audit file cannot be opened for durable append"
            ) from exc
        try:
            _assert_audit_namespace_binding(event_fd, namespace_fd)
            opened_stat = os.fstat(fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise WorktreeReconciliationRepairError(
                    "repair audit append target is not a regular file"
                )
            _assert_audit_file_binding(namespace_fd, path.name, fd)
            if opened_stat.st_size + len(encoded) > AUDIT_MAX_BYTES:
                raise WorktreeReconciliationRepairError(
                    "repair audit append exceeds safety bound"
                )
            if opened_stat.st_size:
                os.lseek(fd, -1, os.SEEK_END)
                if os.read(fd, 1) != b"\n":
                    raise WorktreeReconciliationRepairError(
                        "repair audit has a torn tail; append is forbidden"
                    )
            _assert_audit_namespace_binding(event_fd, namespace_fd)
            written = os.write(fd, encoded)
            if written != len(encoded):
                raise WorktreeReconciliationRepairError(
                    "repair audit append was partial"
                )
            _fsync_file(fd)
            _assert_audit_file_binding(namespace_fd, path.name, fd)
            _assert_audit_namespace_binding(event_fd, namespace_fd)
            _fsync_directory_fd(namespace_fd)
        finally:
            os.close(fd)
    finally:
        os.close(namespace_fd)
        os.close(event_fd)


def _fsync_file(fd: int) -> None:
    os.fsync(fd)


def _ensure_directory_durable(path: Path) -> None:
    missing: list[Path] = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        parent = candidate.parent
        if parent == candidate:
            raise WorktreeReconciliationRepairError(
                "repair audit directory has no existing ancestor"
            )
        candidate = parent
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorktreeReconciliationRepairError(
            "repair audit directory cannot be created"
        ) from exc
    for created in reversed(missing):
        _fsync_directory(created.parent)


def _open_audit_namespace(
    event_dir: Path,
    *,
    create: bool,
) -> tuple[int, int] | None:
    _require_secure_directory_primitives()
    if create:
        _ensure_directory_durable(event_dir)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        event_fd = os.open(event_dir, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorktreeReconciliationRepairError(
            "configured event directory is not a secure directory"
        ) from exc
    try:
        event_stat = os.fstat(event_fd)
        if not stat.S_ISDIR(event_stat.st_mode):
            raise WorktreeReconciliationRepairError(
                "configured event directory is not a directory"
            )
        created = False
        if create:
            try:
                os.mkdir(AUDIT_DIRECTORY, mode=0o700, dir_fd=event_fd)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise WorktreeReconciliationRepairError(
                    "repair audit namespace cannot be created"
                ) from exc
        try:
            namespace_fd = os.open(AUDIT_DIRECTORY, flags, dir_fd=event_fd)
        except FileNotFoundError:
            if not create:
                os.close(event_fd)
                return None
            raise WorktreeReconciliationRepairError(
                "repair audit namespace disappeared during creation"
            )
        except OSError as exc:
            raise WorktreeReconciliationRepairError(
                "repair audit namespace must be a non-symlink directory"
            ) from exc
        try:
            _assert_audit_namespace_binding(event_fd, namespace_fd)
            if created:
                _fsync_directory_fd(event_fd)
            return event_fd, namespace_fd
        except Exception:
            os.close(namespace_fd)
            raise
    except Exception:
        os.close(event_fd)
        raise


def _assert_audit_namespace_binding(event_fd: int, namespace_fd: int) -> None:
    try:
        linked = os.stat(
            AUDIT_DIRECTORY,
            dir_fd=event_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(namespace_fd)
    except OSError as exc:
        raise WorktreeReconciliationRepairError(
            "repair audit namespace binding cannot be verified"
        ) from exc
    if (
        not stat.S_ISDIR(linked.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or linked.st_dev != opened.st_dev
        or linked.st_ino != opened.st_ino
    ):
        raise WorktreeReconciliationRepairError(
            "repair audit namespace binding changed"
        )


def _assert_audit_file_binding(
    namespace_fd: int,
    filename: str,
    audit_fd: int,
) -> None:
    try:
        linked = os.stat(filename, dir_fd=namespace_fd, follow_symlinks=False)
        opened = os.fstat(audit_fd)
    except OSError as exc:
        raise WorktreeReconciliationRepairError(
            "repair audit file binding cannot be verified"
        ) from exc
    if (
        not stat.S_ISREG(linked.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or linked.st_dev != opened.st_dev
        or linked.st_ino != opened.st_ino
        or opened.st_nlink != 1
    ):
        raise WorktreeReconciliationRepairError(
            "repair audit file binding is unsafe or changed"
        )


def _require_secure_directory_primitives() -> None:
    if (
        not getattr(os, "O_NOFOLLOW", 0)
        or not getattr(os, "O_DIRECTORY", 0)
        or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise WorktreeReconciliationRepairError(
            "secure repair audit directory primitives are unavailable"
        )


def _fsync_directory_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise WorktreeReconciliationRepairError(
            "repair durability directory fsync failed"
        ) from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise WorktreeReconciliationRepairError(
            "repair durability directory cannot be opened"
        ) from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise WorktreeReconciliationRepairError(
            "repair durability directory fsync failed"
        ) from exc
    finally:
        os.close(fd)


def _require_exact_candidate(
    item: dict[str, Any],
    approved_source_digest: str,
) -> None:
    if item["source_snapshot_digest"] != approved_source_digest:
        raise WorktreeReconciliationRepairError(
            "approved source digest does not match the live C1 source snapshot"
        )
    if item["action_class"] != "exact_repair_candidate":
        reasons = ",".join(item["reason_codes"])
        raise WorktreeReconciliationRepairError(
            f"live C1 action is {item['action_class']}: {reasons}"
        )
    expected_delta = [
        {
            "field": ALLOWLISTED_FIELD,
            "before": item["source_snapshot"]["canonical_state"]["worktree_status"],
            "after": ALLOWLISTED_AFTER,
        }
    ]
    if item["metadata_delta"] != expected_delta:
        raise WorktreeReconciliationRepairError(
            "live C1 metadata delta is outside the repair allowlist"
        )


def _require_lock_owner(config: Config) -> None:
    metadata = read_lock_metadata(config.lock_file)
    if (
        lock_pid(metadata.get("pid")) != os.getpid()
        or metadata.get("hostname") != socket.gethostname()
    ):
        raise WorktreeReconciliationRepairError(
            "current process does not own the canonical queue lock"
        )


def _load_exact_task(config: Config, requested_task_id: str) -> dict[str, Any]:
    task = load_task(config, requested_task_id)
    if task.get("id") != requested_task_id:
        raise WorktreeReconciliationRepairError(
            "canonical task document id does not match requested task id"
        )
    return task


def _save_exact_task_durable(
    config: Config,
    requested_task_id: str,
    task: dict[str, Any],
) -> None:
    if task.get("id") != requested_task_id:
        raise WorktreeReconciliationRepairError(
            "repair save document id does not match requested task id"
        )
    requested_path = task_path(config, requested_task_id)
    document_path = task_path(config, str(task["id"]))
    if requested_path != document_path:
        raise WorktreeReconciliationRepairError(
            "repair save target is not the requested task path"
        )
    save_task(config, task, touch_updated_at=False)
    _fsync_directory(requested_path.parent)


def _validate_approval(task_id: str, approved_source_digest: str) -> None:
    if (
        not task_id
        or len(task_id) > 256
        or any(character.isspace() for character in task_id)
        or "/" in task_id
        or "\\" in task_id
    ):
        raise WorktreeReconciliationRepairError("task id is invalid")
    if not HEX_DIGEST.fullmatch(approved_source_digest):
        raise WorktreeReconciliationRepairError(
            "--approved-source-digest must be an exact sha256 digest"
        )


def _report(
    *,
    task_id: str,
    approved_source_digest: str,
    live_source_digest: str,
    mode: str,
    action: str,
    before: str,
    operation_id: str,
    task_mutation_performed: bool,
    audit_event_appended: bool,
    recovery_pending: bool,
    preimage: dict[str, Any],
    postimage: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT,
        "mode": mode,
        "task_id": task_id,
        "approved_source_digest": approved_source_digest,
        "live_source_digest": live_source_digest,
        "operation_id": operation_id,
        "action": action,
        "metadata_delta": {
            "field": ALLOWLISTED_FIELD,
            "before": before,
            "after": ALLOWLISTED_AFTER,
        },
        "mutation": {
            "task_performed": task_mutation_performed,
            "audit_event_appended": audit_event_appended,
            "only_allowlisted_task_field_changed": _only_allowlisted_change(
                preimage, postimage
            ),
        },
        "recovery_pending": recovery_pending,
    }


def _only_allowlisted_change(
    preimage: dict[str, Any], postimage: dict[str, Any]
) -> bool:
    before = deepcopy(preimage)
    after = deepcopy(postimage)
    before_status = before.pop(ALLOWLISTED_FIELD, None)
    after_status = after.pop(ALLOWLISTED_FIELD, None)
    return bool(
        before == after
        and before_status in ALLOWLISTED_BEFORE
        and after_status == ALLOWLISTED_AFTER
    )

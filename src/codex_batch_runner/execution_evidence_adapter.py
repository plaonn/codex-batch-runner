from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any


MAX_SOURCE_VALUE_LENGTH = 256
SOURCE_KIND_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SOURCE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
ALLOWED_PRODUCER = "execution-controller"
SETTLEMENT_DISPOSITIONS = {"recorded", "idempotent"}


class AutomaticEvidenceError(ValueError):
    """A bounded, machine-safe automatic-evidence contract error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _bounded_source_value(value: object, field: str, *, kind: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AutomaticEvidenceError(f"invalid-{field}")
    if len(value) > MAX_SOURCE_VALUE_LENGTH or any(ord(char) < 0x20 for char in value):
        raise AutomaticEvidenceError(f"invalid-{field}")
    if kind and not SOURCE_KIND_PATTERN.fullmatch(value):
        raise AutomaticEvidenceError(f"invalid-{field}")
    if not kind and not SOURCE_VALUE_PATTERN.fullmatch(value):
        raise AutomaticEvidenceError(f"invalid-{field}")
    return value


def validate_evidence_source(value: object, *, field: str = "evidence_source") -> dict[str, str]:
    if not isinstance(value, dict):
        raise AutomaticEvidenceError(f"{field}-must-be-object")
    if set(value) != {"kind", "id", "revision"}:
        raise AutomaticEvidenceError(f"{field}-requires-kind-id-revision")
    return {
        "kind": _bounded_source_value(value["kind"], f"{field}-kind", kind=True),
        "id": _bounded_source_value(value["id"], f"{field}-id"),
        "revision": _bounded_source_value(value["revision"], f"{field}-revision"),
    }


def parse_evidence_source_file(path_text: str | Path) -> dict[str, str]:
    path = Path(path_text).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("--evidence-source-json must point to a readable JSON object") from exc
    try:
        return validate_evidence_source(value)
    except AutomaticEvidenceError as exc:
        raise ValueError(f"--evidence-source-json {exc.code}") from exc


def _argv_value(key: str, value: object, *, required: bool = False) -> list[str]:
    if value is None:
        values: list[str] = []
    elif isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        values = list(value)
    else:
        raise ValueError(f"{key} must be a list of non-empty strings")
    if required and not values:
        raise ValueError(f"{key} must be a non-empty list of strings")
    return values


def execution_evidence_adapter_value(value: object) -> dict[str, Any]:
    """Validate the provider-neutral argv adapter configuration.

    The configured command implements the existing external adapter boundary.
    CBR supplies the subcommand, exact source tuple, exact attempt identity and
    child command; it never parses prompt text or derives identity from paths.
    """

    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("execution_evidence_adapter must be an object")
    command = _argv_value("execution_evidence_adapter.command", value.get("command"), required=True)
    run_arguments = _argv_value(
        "execution_evidence_adapter.run_arguments", value.get("run_arguments", [])
    )
    settle_arguments = _argv_value(
        "execution_evidence_adapter.settle_arguments", value.get("settle_arguments", [])
    )
    timeout_raw = value.get("timeout_seconds", 30)
    if isinstance(timeout_raw, bool):
        raise ValueError("execution_evidence_adapter.timeout_seconds must be a positive integer")
    try:
        timeout_seconds = int(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("execution_evidence_adapter.timeout_seconds must be a positive integer") from exc
    if timeout_seconds < 1 or timeout_seconds > 300:
        raise ValueError("execution_evidence_adapter.timeout_seconds must be between 1 and 300")
    producer = value.get("producer", ALLOWED_PRODUCER)
    if producer != ALLOWED_PRODUCER:
        raise ValueError(
            "execution_evidence_adapter.producer must be execution-controller"
        )
    return {
        "command": command,
        "run_arguments": run_arguments,
        "settle_arguments": settle_arguments,
        "timeout_seconds": timeout_seconds,
        "producer": ALLOWED_PRODUCER,
    }


def validate_task_evidence_binding(config: Any, task: dict[str, Any]) -> str | None:
    source = task.get("evidence_source")
    if source is None:
        return None
    try:
        validate_evidence_source(source)
    except AutomaticEvidenceError as exc:
        return exc.code
    if task.get("execution_backend") not in (None, "codex"):
        return "automatic evidence source requires execution_backend=codex"
    if not isinstance(getattr(config, "execution_evidence_adapter", None), dict) or not getattr(
        config, "execution_evidence_adapter", {}
    ):
        return "execution evidence source requires configured execution_evidence_adapter"
    return None


def _adapter_config(config: Any) -> dict[str, Any]:
    adapter = getattr(config, "execution_evidence_adapter", None)
    if not isinstance(adapter, dict) or not adapter:
        raise AutomaticEvidenceError("adapter-not-configured")
    return adapter


def build_run_command(
    config: Any,
    source: dict[str, str],
    execution_id: str,
    result_file: Path,
    child_command: list[str],
) -> list[str]:
    normalized_source = validate_evidence_source(source)
    if not execution_id or any(ord(char) < 0x20 for char in execution_id):
        raise AutomaticEvidenceError("invalid-execution-id")
    if not child_command or not all(isinstance(item, str) and item for item in child_command):
        raise AutomaticEvidenceError("missing-child-command")
    adapter = _adapter_config(config)
    return [
        *adapter["command"],
        "run",
        *adapter.get("run_arguments", []),
        "--execution-id",
        execution_id,
        "--source-kind",
        normalized_source["kind"],
        "--source-id",
        normalized_source["id"],
        "--source-revision",
        normalized_source["revision"],
        "--result-file",
        str(result_file),
        "--",
        *child_command,
    ]


def _safe_optional_text(value: object, *, max_length: int = 128) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_length or any(ord(char) < 0x20 for char in value):
        return None
    return value


def _safe_run_result(
    value: object,
    source: dict[str, str],
    execution_id: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != "chamber-orca-evidence.v1":
        raise AutomaticEvidenceError("result-invalid-schema")
    if value.get("execution_id") != execution_id:
        raise AutomaticEvidenceError("result-execution-id-mismatch")
    try:
        returned_source = validate_evidence_source(value.get("source"), field="result-source")
    except AutomaticEvidenceError as exc:
        raise AutomaticEvidenceError("result-source-invalid") from exc
    if returned_source != source:
        raise AutomaticEvidenceError("result-source-mismatch")
    acceptance_state = value.get("acceptance_state")
    if acceptance_state not in {"pending-authority", "explicit-result", "invalid-input"}:
        raise AutomaticEvidenceError("result-acceptance-state-invalid")
    acceptance_status = value.get("acceptance_status")
    if acceptance_status not in {None, "unknown", "accepted", "rejected"}:
        raise AutomaticEvidenceError("result-acceptance-status-invalid")
    outcome_status = value.get("outcome_status")
    if outcome_status not in {None, "accepted", "rejected", "unknown"}:
        raise AutomaticEvidenceError("result-outcome-status-invalid")
    settlement_disposition = value.get("settlement_disposition")
    if settlement_disposition not in {"pending-authority", "not-applicable", "recorded", "idempotent", "failed"}:
        raise AutomaticEvidenceError("result-settlement-disposition-invalid")
    child_exit_code = value.get("child_exit_code")
    if child_exit_code is not None and (isinstance(child_exit_code, bool) or not isinstance(child_exit_code, int)):
        raise AutomaticEvidenceError("result-child-exit-invalid")
    for field in ("settlement_attempted", "settlement_recorded"):
        if not isinstance(value.get(field), bool):
            raise AutomaticEvidenceError(f"result-{field}-invalid")
    return {
        "schema": "chamber-orca-evidence.v1",
        "execution_id": execution_id,
        "source": returned_source,
        "child_exit_code": child_exit_code,
        "acceptance_status": acceptance_status,
        "acceptance_state": acceptance_state,
        "outcome_status": outcome_status,
        "outcome_producer": _safe_optional_text(value.get("outcome_producer")),
        "settlement_attempted": value["settlement_attempted"],
        "settlement_status": _safe_optional_text(value.get("settlement_status")),
        "settlement_recorded": value["settlement_recorded"],
        "settlement_disposition": settlement_disposition,
        "settlement_error": _safe_optional_text(value.get("settlement_error")),
        "outcome_input_error": _safe_optional_text(value.get("outcome_input_error")),
    }


def read_run_result(
    result_file: Path,
    source: dict[str, str],
    execution_id: str,
) -> dict[str, Any]:
    try:
        value = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AutomaticEvidenceError("result-unavailable") from exc
    return _safe_run_result(value, validate_evidence_source(source), execution_id)


def _json_stdout(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _callback_result(
    *,
    status: str,
    requested_status: str,
    source: dict[str, str] | None = None,
    execution_id: str | None = None,
    attempt: int | None = None,
    returncode: int | None = None,
    settlement_status: str | None = None,
    settlement_disposition: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "automatic-evidence-callback.v1",
        "status": status,
        "requested_status": requested_status,
        "reconciliation_required": status == "unknown-effect",
        "execution_id": execution_id,
        "attempt": attempt,
        "source": source,
        "returncode": returncode,
        "settlement_status": settlement_status,
        "settlement_disposition": settlement_disposition,
        "error_code": error_code,
    }
    return {key: value for key, value in result.items() if value is not None}


def invoke_review_callback(config: Any, task: dict[str, Any], review_status: str) -> dict[str, Any]:
    if review_status not in {"accepted", "rejected"}:
        raise ValueError(f"unsupported automatic-evidence review status: {review_status}")
    source_value = task.get("evidence_source")
    if source_value is None:
        return _callback_result(status="not-applicable", requested_status=review_status, error_code="no-source")
    try:
        source = validate_evidence_source(source_value)
    except AutomaticEvidenceError as exc:
        return _callback_result(status="unknown-effect", requested_status=review_status, error_code=exc.code)

    history = task.get("automatic_evidence_attempts")
    latest = history[-1] if isinstance(history, list) and history else None
    if not isinstance(latest, dict):
        return _callback_result(
            status="unknown-effect", requested_status=review_status, source=source, error_code="no-execution-attempt"
        )
    execution_id = latest.get("execution_id")
    attempt = latest.get("attempt")
    if not isinstance(execution_id, str) or not execution_id:
        return _callback_result(
            status="unknown-effect", requested_status=review_status, source=source, error_code="attempt-identity-missing"
        )
    if latest.get("source") != source:
        return _callback_result(
            status="unknown-effect",
            requested_status=review_status,
            source=source,
            execution_id=execution_id,
            attempt=attempt if isinstance(attempt, int) else None,
            error_code="attempt-source-mismatch",
        )
    try:
        adapter = _adapter_config(config)
    except AutomaticEvidenceError as exc:
        return _callback_result(
            status="unknown-effect",
            requested_status=review_status,
            source=source,
            execution_id=execution_id,
            attempt=attempt if isinstance(attempt, int) else None,
            error_code=exc.code,
        )
    command = [
        *adapter["command"],
        "settle",
        *adapter.get("settle_arguments", []),
        "--execution-id",
        execution_id,
        "--status",
        review_status,
        "--authoritative",
        "--producer",
        adapter.get("producer", ALLOWED_PRODUCER),
        "--source-kind",
        source["kind"],
        "--source-id",
        source["id"],
        "--source-revision",
        source["revision"],
    ]
    cwd_value = task.get("cwd")
    cwd = str(cwd_value) if isinstance(cwd_value, str) and Path(cwd_value).is_dir() else None
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(adapter["timeout_seconds"]),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _callback_result(
            status="unknown-effect",
            requested_status=review_status,
            source=source,
            execution_id=execution_id,
            attempt=attempt if isinstance(attempt, int) else None,
            error_code="callback-timeout",
        )
    except OSError as exc:
        return _callback_result(
            status="unknown-effect",
            requested_status=review_status,
            source=source,
            execution_id=execution_id,
            attempt=attempt if isinstance(attempt, int) else None,
            error_code=f"callback-launch-{exc.__class__.__name__}",
        )

    payload = _json_stdout(completed.stdout)
    disposition = payload.get("settlement_disposition") if isinstance(payload, dict) else None
    returned_execution_id = payload.get("execution_id") if isinstance(payload, dict) else None
    settlement_status = payload.get("settlement_status") if isinstance(payload, dict) else None
    if (
        completed.returncode == 0
        and isinstance(payload, dict)
        and payload.get("schema") == "chamber-orca-evidence-settle.v1"
        and returned_execution_id == execution_id
        and disposition in SETTLEMENT_DISPOSITIONS
        and payload.get("settlement_recorded") is True
    ):
        return _callback_result(
            status=str(disposition),
            requested_status=review_status,
            source=source,
            execution_id=execution_id,
            attempt=attempt if isinstance(attempt, int) else None,
            returncode=completed.returncode,
            settlement_status=_safe_optional_text(settlement_status),
            settlement_disposition=str(disposition),
        )
    return _callback_result(
        status="unknown-effect",
        requested_status=review_status,
        source=source,
        execution_id=execution_id,
        attempt=attempt if isinstance(attempt, int) else None,
        returncode=completed.returncode,
        settlement_status=_safe_optional_text(settlement_status),
        settlement_disposition=_safe_optional_text(disposition),
        error_code=(
            "callback-invalid-response"
            if completed.returncode == 0
            else "callback-nonzero"
        ),
    )


def record_review_callback(config: Any, task: dict[str, Any], review_status: str) -> dict[str, Any]:
    """Attempt exactly one authoritative callback and persist its disposition."""

    callback = invoke_review_callback(config, task, review_status)
    if callback.get("status") == "not-applicable":
        return callback
    history = task.setdefault("automatic_evidence_callback_history", [])
    if not isinstance(history, list):
        history = []
        task["automatic_evidence_callback_history"] = history
    history.append(callback)
    task["automatic_evidence_last_callback"] = callback
    from .events import emit_task_event, transition_payload
    from .queue import save_task

    save_task(config, task)
    emit_task_event(
        config,
        "automatic_evidence_callback",
        task,
        source="automatic-evidence",
        summary=f"automatic evidence callback {callback.get('status')} for {review_status}",
        payload=transition_payload(
            task,
            automatic_evidence_status=callback.get("status"),
            automatic_evidence_execution_id=callback.get("execution_id"),
            automatic_evidence_review_status=review_status,
            reconciliation_required=callback.get("reconciliation_required"),
        ),
    )
    return callback

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .evaluation import derive_evaluation_row
from .execution_evidence_v2 import derive_cohort, validate_execution_evidence_v2
from .request_fingerprint import _safe_id_hash, _safe_metadata_value

SUPPORTED_RECORD_KINDS = {"codex_subagent_execution", "execution_evidence_v2"}


class ExecutionEvidenceError(ValueError):
    pass


def load_execution_evidence_records(paths: list[str] | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path_text in paths or []:
        path = Path(path_text)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ExecutionEvidenceError(f"could not read execution evidence file: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ExecutionEvidenceError(f"invalid execution evidence JSON: {path}") from exc
        loaded = _records_from_json(data, str(path))
        for offset, record in enumerate(loaded, start=len(records)):
            execution_evidence_task_projection(record, index=offset)
        records.extend(loaded)
    return records


def derive_execution_evidence_rows(records: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = []
    for index, record in enumerate(records or []):
        task_projection = execution_evidence_task_projection(record, index=index)
        row = derive_evaluation_row(task_projection)
        row["supplemental_evidence"] = {
            "record_kind": _safe_metadata_value(record.get("record_kind")),
            "queue_task": False,
            "source": "supplemental_execution_evidence",
            "raw_record_included": False,
        }
        rows.append(row)
    return rows


def execution_evidence_task_projection(record: dict[str, Any], *, index: int = 0) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ExecutionEvidenceError("execution evidence record must be a JSON object")
    kind = _safe_metadata_value(record.get("record_kind"))
    if kind not in SUPPORTED_RECORD_KINDS:
        raise ExecutionEvidenceError(f"unsupported execution evidence record_kind: {kind}")

    work_hash = _safe_id_hash(record.get("work_id") or record.get("id") or f"record-{index}") or "sha256:unknown"
    task_id = "evidence-" + work_hash.split(":", 1)[-1][:16]
    surface = _safe_metadata_value(record.get("execution_surface") or "codex_subagent")
    if surface == "unknown":
        surface = "codex_subagent"

    task: dict[str, Any] = {
        "id": task_id,
        "queue_task": False,
        "execution_surface": surface,
        "project_id": record.get("project_id"),
        "category": record.get("category"),
        "labels": _list_value(record.get("labels")),
        "routing_size": record.get("routing_size"),
        "routing_risk": record.get("routing_risk"),
        "routing_experiment": record.get("routing_experiment"),
        "verification_scope": _list_value(record.get("verification_scope")),
        "routing_risk_factors": _list_value(record.get("routing_risk_factors")),
        "execution_backend": record.get("execution_backend") or "codex_app",
        "model_requirement_vector": _dict_value(record.get("model_requirement_vector")),
        "provider_resource": _dict_value(record.get("provider_resource")),
        "status": record.get("status") or "completed",
        "review_status": record.get("review_status"),
        "anchor_review": record.get("anchor_review"),
        "attempts": _int_value(record.get("attempts")),
        "run_count": _int_value(record.get("run_count")),
        "last_run": _last_run_projection(_dict_value(record.get("last_run"))),
        "last_result": _last_result_projection(_dict_value(record.get("last_result"))),
        "reviewer_codex": _reviewer_projection(_dict_value(record.get("reviewer_codex"))),
    }
    if kind == "execution_evidence_v2":
        evidence = deepcopy(validate_execution_evidence_v2(record.get("execution_evidence")))
        evidence["cohort"] = derive_cohort(task, evidence)
        validate_execution_evidence_v2(evidence)
        task["execution_evidence_history"] = [evidence]
        task["last_run"]["execution_evidence_id"] = evidence.get("evidence_id")
    if record.get("title"):
        task["title"] = _safe_metadata_value(record.get("title"))
    return task


def _records_from_json(data: Any, source: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict) and isinstance(data.get("records"), list):
        records = data["records"]
    elif isinstance(data, dict):
        records = [data]
    else:
        raise ExecutionEvidenceError(f"execution evidence JSON must be an object, records object, or list: {source}")
    if not all(isinstance(record, dict) for record in records):
        raise ExecutionEvidenceError(f"execution evidence records must be JSON objects: {source}")
    return list(records)


def _last_run_projection(last_run: dict[str, Any]) -> dict[str, Any]:
    if not last_run:
        return {}
    resolved = _dict_value(last_run.get("resolved_execution_config"))
    return {
        "command_kind": _safe_metadata_value(last_run.get("command_kind") or "subagent"),
        "execution_backend": _safe_metadata_value(last_run.get("execution_backend") or "codex_app"),
        "returncode": _int_value(last_run.get("returncode")),
        "duration_seconds": _number_value(last_run.get("duration_seconds")),
        "timed_out": bool(last_run.get("timed_out")),
        "resolved_execution_config": {
            "selection_rule": resolved.get("selection_rule"),
            "model_source": resolved.get("model_source") or "codex_app_default",
            "execution_target": resolved.get("execution_target"),
            "model_requirement_vector": _dict_value(resolved.get("model_requirement_vector")),
            "model": resolved.get("model"),
            "codex_profile": resolved.get("codex_profile"),
        },
    }


def _last_result_projection(last_result: dict[str, Any]) -> dict[str, Any]:
    if not last_result:
        return {}
    return {
        "status": _safe_metadata_value(last_result.get("status") or "completed"),
        "changed_files": _list_value(last_result.get("changed_files")),
        "verification": _list_value(last_result.get("verification")),
        "commits": _list_value(last_result.get("commits")),
    }


def _reviewer_projection(reviewer: dict[str, Any]) -> dict[str, Any]:
    if not reviewer:
        return {}
    findings = []
    for finding in _list_value(reviewer.get("findings")):
        if isinstance(finding, dict):
            findings.append({"severity": finding.get("severity")})
    return {
        "decision": reviewer.get("decision"),
        "confidence": reviewer.get("confidence"),
        "findings": findings,
        "required_human_checks": _list_value(reviewer.get("required_human_checks")),
    }


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _number_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

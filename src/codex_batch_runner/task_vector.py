from __future__ import annotations

from typing import Any

from .request_fingerprint import _cwd_class, _has_value, _normalized_list, _path_hash, _safe_id_hash, _safe_metadata_value

SCHEMA_VERSION = 1
PREPROCESSING_VERSION = "task-vector-v1"

SCALAR_DIMENSIONS = (
    "routing_size",
    "routing_risk",
    "category",
    "execution_backend",
)
LIST_DIMENSIONS = (
    "verification_scope",
    "routing_risk_factors",
    "labels",
)
PROJECT_METADATA_FIELDS = ("project_id", "project_root", "cwd")
TASK_METADATA_FIELDS = ("id", "task_id", "subtask_type")
SOURCE_FIELDS = (*SCALAR_DIMENSIONS, *LIST_DIMENSIONS, *PROJECT_METADATA_FIELDS, *TASK_METADATA_FIELDS)


def derive_normalized_task_vector(task: dict[str, Any]) -> dict[str, Any]:
    """Derive a public-safe task vector from already-structured task metadata.

    Missing scalar dimensions are represented as "unknown". Missing list dimensions
    are represented as empty lists. This helper does not read raw prompt content and
    does not include provider, model, profile, quota, capacity, reviewer, or outcome
    fields.
    """
    dimensions = {
        "routing_size": _safe_metadata_value(task.get("routing_size")),
        "routing_risk": _safe_metadata_value(task.get("routing_risk")),
        "category": _safe_metadata_value(task.get("category")),
        "execution_backend": _safe_metadata_value(task.get("execution_backend")),
        "verification_scope": _normalized_list(task.get("verification_scope")),
        "routing_risk_factors": _normalized_list(task.get("routing_risk_factors")),
        "labels": _normalized_list(task.get("labels")),
    }

    source_fields = _source_fields(task)
    field_provenance = {field: _field_provenance(task, field) for field in (*SCALAR_DIMENSIONS, *LIST_DIMENSIONS)}
    return {
        "schema_version": SCHEMA_VERSION,
        "preprocessing_version": PREPROCESSING_VERSION,
        "source": "existing_metadata",
        "derivation": "deterministic",
        "confidence": _confidence_bucket(dimensions),
        "source_fields": source_fields,
        "dimensions": dimensions,
        "project": _project_metadata(task),
        "task": _task_metadata(task),
        "provenance": {
            "policy_version": PREPROCESSING_VERSION,
            "raw_prompt_used": False,
            "persisted_to_task_json": False,
            "field_sources": field_provenance,
        },
    }


def _source_fields(task: dict[str, Any]) -> list[str]:
    return sorted(field for field in SOURCE_FIELDS if _has_value(task.get(field)))


def _field_provenance(task: dict[str, Any], field: str) -> dict[str, str]:
    if _has_value(task.get(field)):
        return {"source": "existing_metadata", "confidence": "high"}
    return {"source": "missing", "confidence": "low"}


def _project_metadata(task: dict[str, Any]) -> dict[str, Any]:
    project_root_hash = _path_hash(task.get("project_root") or task.get("cwd"))
    project: dict[str, Any] = {
        "project_id": _safe_metadata_value(task.get("project_id")),
        "cwd_class": _cwd_class(task),
    }
    if project_root_hash:
        project["project_root_hash"] = project_root_hash
    return project


def _task_metadata(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id_hash": _safe_id_hash(task.get("id") or task.get("task_id")),
        "subtask_type": _safe_metadata_value(task.get("subtask_type")),
    }


def _confidence_bucket(dimensions: dict[str, Any]) -> str:
    explicit_scalars = sum(1 for field in SCALAR_DIMENSIONS if dimensions.get(field) != "unknown")
    explicit_lists = sum(1 for field in LIST_DIMENSIONS if dimensions.get(field))
    if explicit_scalars == len(SCALAR_DIMENSIONS) and explicit_lists >= 1:
        return "high"
    if explicit_scalars >= 2 or explicit_lists >= 1:
        return "medium"
    return "low"

from __future__ import annotations

import hashlib
import json
from typing import Any

from .execution_evidence_v2 import evidence_view
from .timeutil import iso_now

SCHEMA_VERSION = 1
EVIDENCE_CONTRACT_VERSION = "review-outcome-evidence-v1"
COHORT_DEFINITION_VERSION = "review-outcome-cohort-v1"
ACCEPTANCE_METHODS = {"mechanical_safe", "reviewer_pass", "human_accept", "external_review", "none"}
OBJECTIVE_STATUSES = {"passed", "failed", "unavailable", "not_applicable"}
SEMANTIC_STATUSES = {"pass", "needs_fix", "needs_human", "failed_review", "not_performed"}
REVIEWER_KINDS = {"codex", "human", "external", "none", "unknown"}
IDENTITY_SOURCES = {
    "provider_observed": "provider_observed",
    "wrapper_observed": "wrapper_attested",
}
FORBIDDEN_KEYS = {
    "prompt",
    "transcript",
    "stdout",
    "stderr",
    "log_path",
    "session_id",
    "thread_id",
    "command",
    "cwd",
    "alias",
    "self_claim",
    "planned_model",
}


class ReviewOutcomeEvidenceError(ValueError):
    pass


def build_review_outcome_evidence(
    task: dict[str, Any],
    *,
    acceptance_method: str,
    accepted: bool,
    objective_status: str,
    semantic_status: str,
    reviewer_kind: str,
    reviewer_role: str | None = None,
    decision_confidence: str | None = None,
    anchor_semantic_review: bool = False,
    actual_identity: str | None = None,
    actual_identity_source: str | None = None,
    actual_identity_confidence: str | None = None,
    review_policy_version: str | None = None,
    rubric_version: str | None = None,
    reviewer_execution_cohort_id: str | None = None,
) -> dict[str, Any]:
    if acceptance_method not in ACCEPTANCE_METHODS:
        raise ReviewOutcomeEvidenceError("invalid acceptance method")
    if objective_status not in OBJECTIVE_STATUSES:
        raise ReviewOutcomeEvidenceError("invalid objective verification status")
    if semantic_status not in SEMANTIC_STATUSES:
        raise ReviewOutcomeEvidenceError("invalid semantic review status")
    if reviewer_kind not in REVIEWER_KINDS:
        raise ReviewOutcomeEvidenceError("invalid reviewer kind")
    identity = _identity_payload(actual_identity, actual_identity_source, actual_identity_confidence)
    captured_at = iso_now()
    record = {
        "schema_version": SCHEMA_VERSION,
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "kind": "review_outcome_evidence",
        "evidence_id": _stable_id(
            {
                "task_id": str(task.get("id") or "unknown"),
                "attempt": _int_value(task.get("attempts")),
                "captured_at": captured_at,
                "acceptance_method": acceptance_method,
            }
        ),
        "captured_at": captured_at,
        "acceptance": {"method": acceptance_method, "accepted": bool(accepted)},
        "objective_verification": {"status": objective_status},
        "semantic_review": {"status": semantic_status, "anchor": bool(anchor_semantic_review)},
        "reviewer": {
            "kind": reviewer_kind,
            "role": _safe_role(reviewer_role),
            "decision_confidence": _safe_confidence(decision_confidence),
            "actual_identity": identity,
            "provenance_class": _provenance_class(reviewer_kind, identity),
        },
        "privacy": {
            "raw_prompt_included": False,
            "raw_transcript_included": False,
            "session_or_thread_ids_included": False,
            "raw_paths_included": False,
            "credentials_included": False,
        },
    }
    record["cohort"] = derive_review_outcome_cohort(
        task,
        record,
        review_policy_version=review_policy_version,
        rubric_version=rubric_version,
        reviewer_execution_cohort_id=reviewer_execution_cohort_id,
    )
    validate_review_outcome_evidence(record)
    return record


def attach_review_outcome_evidence(task: dict[str, Any], record: dict[str, Any]) -> None:
    """Append a validated supplemental review outcome without rewriting task review metadata."""
    validate_review_outcome_evidence(record)
    history = task.get("review_outcome_evidence_history")
    if history is None:
        history = []
        task["review_outcome_evidence_history"] = history
    if not isinstance(history, list):
        raise ReviewOutcomeEvidenceError("review outcome evidence history must be a list")
    if not any(isinstance(item, dict) and item.get("evidence_id") == record["evidence_id"] for item in history):
        history.append(record)


def latest_review_outcome_evidence(task: dict[str, Any]) -> dict[str, Any] | None:
    history = task.get("review_outcome_evidence_history")
    if not isinstance(history, list):
        return None
    for record in reversed(history):
        if isinstance(record, dict) and record.get("schema_version") == SCHEMA_VERSION:
            validate_review_outcome_evidence(record)
            return record
    return None


def legacy_review_outcome_view(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 0,
        "evidence_contract_version": "legacy-review-unknown",
        "kind": "legacy_review_outcome_evidence",
        "acceptance": {"method": "none", "accepted": False},
        "objective_verification": {"status": "unavailable"},
        "semantic_review": {"status": "not_performed", "anchor": False},
        "reviewer": {
            "kind": "unknown",
            "role": "unknown",
            "decision_confidence": "unknown",
            "actual_identity": {
                "status": "unknown",
                "value": None,
                "source": None,
                "confidence": None,
            },
            "provenance_class": "legacy-review-unknown",
        },
        "cohort": {
            "definition_version": COHORT_DEFINITION_VERSION,
            "cohort_id": "legacy-review-unknown",
            "components": {},
            "comparability": {"quality": False, "anchor_semantic_review": False},
            "exclusion_reasons": ["legacy_review_outcome_evidence"],
        },
        "privacy": {
            "raw_prompt_included": False,
            "raw_transcript_included": False,
            "session_or_thread_ids_included": False,
            "raw_paths_included": False,
            "credentials_included": False,
        },
    }


def review_outcome_view(task: dict[str, Any]) -> dict[str, Any]:
    record = latest_review_outcome_evidence(task)
    return record if record is not None else legacy_review_outcome_view(task)


def derive_review_outcome_cohort(
    task: dict[str, Any],
    record: dict[str, Any],
    *,
    review_policy_version: str | None = None,
    rubric_version: str | None = None,
    reviewer_execution_cohort_id: str | None = None,
) -> dict[str, Any]:
    execution = evidence_view(task)
    execution_cohort = execution.get("cohort") if isinstance(execution.get("cohort"), dict) else {}
    acceptance = record.get("acceptance") if isinstance(record.get("acceptance"), dict) else {}
    reviewer = record.get("reviewer") if isinstance(record.get("reviewer"), dict) else {}
    components = {
        "task_bucket_key": _task_bucket_key(task),
        "execution_cohort_id": str(execution_cohort.get("cohort_id") or "legacy-execution-unknown"),
        "outcome_contract_version": EVIDENCE_CONTRACT_VERSION,
        "review_policy_version": str(review_policy_version or task.get("review_policy_version") or "legacy"),
        "rubric_version": str(rubric_version or task.get("review_rubric_version") or "legacy"),
        "acceptance_method": str(acceptance.get("method") or "none"),
        "reviewer_provenance_class": str(reviewer.get("provenance_class") or "unknown"),
        "reviewer_execution_cohort_id": str(reviewer_execution_cohort_id or "legacy-reviewer-execution-unknown"),
    }
    return {
        "definition_version": COHORT_DEFINITION_VERSION,
        "cohort_id": "sha256:" + _stable_hash(components)[:16],
        "components": components,
        "comparability": {"quality": True, "anchor_semantic_review": True},
        "exclusion_reasons": [],
    }


def validate_review_outcome_evidence(record: object) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ReviewOutcomeEvidenceError("review outcome evidence must be a JSON object")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ReviewOutcomeEvidenceError("review outcome evidence schema_version must be 1")
    if record.get("evidence_contract_version") != EVIDENCE_CONTRACT_VERSION:
        raise ReviewOutcomeEvidenceError("invalid review outcome evidence contract version")
    if record.get("kind") != "review_outcome_evidence":
        raise ReviewOutcomeEvidenceError("review outcome evidence kind must be review_outcome_evidence")
    if not isinstance(record.get("evidence_id"), str) or not record["evidence_id"].strip():
        raise ReviewOutcomeEvidenceError("review outcome evidence requires evidence_id")
    for key in FORBIDDEN_KEYS:
        if _contains_key(record, key):
            raise ReviewOutcomeEvidenceError(f"review outcome evidence contains forbidden key: {key}")
    acceptance = record.get("acceptance")
    if not isinstance(acceptance, dict) or acceptance.get("method") not in ACCEPTANCE_METHODS or not isinstance(acceptance.get("accepted"), bool):
        raise ReviewOutcomeEvidenceError("invalid acceptance")
    if acceptance["method"] == "none" and acceptance["accepted"]:
        raise ReviewOutcomeEvidenceError("acceptance method none cannot accept")
    objective = record.get("objective_verification")
    if not isinstance(objective, dict) or objective.get("status") not in OBJECTIVE_STATUSES:
        raise ReviewOutcomeEvidenceError("invalid objective verification")
    semantic = record.get("semantic_review")
    if not isinstance(semantic, dict) or semantic.get("status") not in SEMANTIC_STATUSES or not isinstance(semantic.get("anchor"), bool):
        raise ReviewOutcomeEvidenceError("invalid semantic review")
    reviewer = record.get("reviewer")
    if not isinstance(reviewer, dict) or reviewer.get("kind") not in REVIEWER_KINDS:
        raise ReviewOutcomeEvidenceError("invalid reviewer")
    identity = reviewer.get("actual_identity")
    if not isinstance(identity, dict):
        raise ReviewOutcomeEvidenceError("invalid reviewer actual identity")
    _validate_identity(identity)
    if reviewer.get("provenance_class") != _provenance_class(str(reviewer.get("kind")), identity):
        raise ReviewOutcomeEvidenceError("reviewer provenance class does not match reviewer identity")
    if acceptance["method"] == "reviewer_pass" and semantic["status"] != "pass":
        raise ReviewOutcomeEvidenceError("reviewer_pass requires semantic pass")
    cohort = record.get("cohort")
    components = cohort.get("components") if isinstance(cohort, dict) else None
    if (
        not isinstance(cohort, dict)
        or cohort.get("definition_version") != COHORT_DEFINITION_VERSION
        or not isinstance(components, dict)
        or set(components) not in ({
            "task_bucket_key",
            "execution_cohort_id",
            "outcome_contract_version",
            "review_policy_version",
            "rubric_version",
            "acceptance_method",
            "reviewer_provenance_class",
        }, {
            "task_bucket_key",
            "execution_cohort_id",
            "outcome_contract_version",
            "review_policy_version",
            "rubric_version",
            "acceptance_method",
            "reviewer_provenance_class",
            "reviewer_execution_cohort_id",
        })
        or components.get("outcome_contract_version") != EVIDENCE_CONTRACT_VERSION
        or components.get("acceptance_method") != acceptance["method"]
        or components.get("reviewer_provenance_class") != reviewer["provenance_class"]
    ):
        raise ReviewOutcomeEvidenceError("invalid review outcome cohort")
    privacy = record.get("privacy")
    if not isinstance(privacy, dict) or any(value is not False for value in privacy.values()):
        raise ReviewOutcomeEvidenceError("review outcome evidence privacy flags must all be false")
    return record


def _identity_payload(value: str | None, source: str | None, confidence: str | None) -> dict[str, Any]:
    if value is None and source is None and confidence is None:
        return {"status": "unknown", "value": None, "source": None, "confidence": None}
    if source not in IDENTITY_SOURCES or confidence != IDENTITY_SOURCES[source] or not isinstance(value, str) or not value.strip():
        raise ReviewOutcomeEvidenceError("actual reviewer identity requires provider/wrapper observed source and matching confidence")
    return {"status": "observed", "value": value.strip(), "source": source, "confidence": confidence}


def _validate_identity(identity: dict[str, Any]) -> None:
    status = identity.get("status")
    if status == "unknown":
        if any(identity.get(key) is not None for key in ("value", "source", "confidence")):
            raise ReviewOutcomeEvidenceError("unknown reviewer identity must not contain value, source, or confidence")
        return
    if status != "observed":
        raise ReviewOutcomeEvidenceError("invalid reviewer identity status")
    source = identity.get("source")
    if (
        source not in IDENTITY_SOURCES
        or identity.get("confidence") != IDENTITY_SOURCES[source]
        or not isinstance(identity.get("value"), str)
        or not identity["value"].strip()
    ):
        raise ReviewOutcomeEvidenceError("observed reviewer identity must be provider/wrapper observed")


def _provenance_class(kind: str, identity: dict[str, Any]) -> str:
    source = identity.get("source") if identity.get("status") == "observed" else "unknown"
    return f"kind={kind} identity={source}"


def _task_bucket_key(task: dict[str, Any]) -> str:
    scope = task.get("verification_scope")
    scopes = sorted(str(value) for value in scope) if isinstance(scope, list) else []
    return "size={size} risk={risk} verify={scope}".format(
        size=str(task.get("routing_size") or "unknown"),
        risk=str(task.get("routing_risk") or "unknown"),
        scope=",".join(scopes) if scopes else "none",
    )


def _safe_role(value: str | None) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else "unknown"


def _safe_confidence(value: str | None) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else "unknown"


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _stable_id(value: object) -> str:
    return "review-sha256:" + _stable_hash(value)[:24]


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _int_value(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0

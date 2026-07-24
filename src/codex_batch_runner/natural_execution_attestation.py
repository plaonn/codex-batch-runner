from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .execution_evidence_v3 import validate_execution_evidence_v3
from .review_outcome_evidence import validate_review_outcome_evidence
from .worker_certification import (
    BOUNDARY_SCENARIOS,
    POLICY_REVISION as WORKER_POLICY_REVISION,
    TASK_CLASSES,
)


CONTRACT_VERSION = "cbr-natural-execution-attestation-v1"
SCHEMA_VERSION = 1
EVIDENCE_CLASSES = {
    "natural-objective-run",
    "natural-boundary-event",
    "provider-observation",
    "synthetic-boundary",
}
OUTCOMES = {"pass", "fail", "unknown"}
MUTATION_PROVENANCE = {
    "no_mutation",
    "mutation_possible",
    "mutation_observed",
    "unknown",
}
FORBIDDEN_KEYS = {
    "prompt",
    "transcript",
    "argv",
    "command",
    "cwd",
    "path",
    "session_id",
    "thread_id",
    "account",
    "email",
    "credential",
    "token",
}
_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-"
)


class NaturalExecutionAttestationError(ValueError):
    pass


def build_natural_execution_attestation(
    task: dict[str, Any],
    *,
    mapping: object,
    evidence_class: str,
    scenario: str,
    outcome: str,
    mutation_provenance: str,
    attestor_revision: str,
    recorded_at: datetime,
    supersedes_attestation_id: str | None = None,
    boundary_event: object | None = None,
) -> dict[str, Any]:
    """Bind one already-finished execution/review closure to a report-only record."""
    recorded = _aware_utc(recorded_at, "recorded_at")
    execution = _linked_execution(task)
    review = _latest_review(task)
    mapping_value = _mapping(mapping, execution)
    _validate_closure(task, execution, review, recorded)
    _validate_observation(evidence_class, scenario, outcome, mutation_provenance, review)
    if evidence_class == "natural-boundary-event":
        validated_boundary = validate_natural_boundary_event(boundary_event)
        boundary_time = _timestamp(
            validated_boundary["observed_at"], "boundary.observed_at"
        )
        if boundary_time > recorded:
            raise NaturalExecutionAttestationError(
                "future natural boundary event is ineligible"
            )
        expected_boundary = build_natural_boundary_event(
            task, scenario=scenario, observed_at=boundary_time
        )
        if validated_boundary != expected_boundary:
            raise NaturalExecutionAttestationError(
                "natural boundary event does not match current canonical closure"
            )
    elif boundary_event is not None:
        raise NaturalExecutionAttestationError(
            "boundary_event is only valid for natural boundary evidence"
        )

    provider = execution["identity"]["provider_reported_model"]
    token_usage = execution["token_usage"]
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT_VERSION,
        "kind": "natural_execution_attestation",
        "recorded_at": recorded.isoformat(),
        "evidence": {
            "class": evidence_class,
            "scenario": scenario,
            "outcome": outcome,
            "mutation_provenance": mutation_provenance,
        },
        "binding": {
            "task_id": _safe_id(task.get("id"), "task.id"),
            "attempt": _count(task.get("attempts"), "task.attempts"),
            "execution_evidence_id": execution["evidence_id"],
            "execution_cohort_id": execution["cohort"]["cohort_id"],
            "worker_family": mapping_value["worker_family"],
            "worker_id": mapping_value["worker_id"],
            "target_id": mapping_value["target_id"],
            "target_snapshot_id": mapping_value["target_snapshot_id"],
            "resolved_config_digest": _stable_id(
                {
                    "routing": execution["routing"],
                    "versions": execution["versions"],
                    "cohort": execution["cohort"]["components"],
                }
            ),
            "task_class": mapping_value["task_class"],
            "mapping_revision": mapping_value["mapping_revision"],
        },
        "execution": {
            "captured_at": execution["captured_at"],
            "integrity_status": execution["integrity"]["status"],
            "identity_attestation": execution["identity"]["attestation"],
        },
        "review": {
            "evidence_id": review["evidence_id"],
            "captured_at": review["captured_at"],
            "policy_version": review["cohort"]["components"]["review_policy_version"],
            "rubric_version": review["cohort"]["components"]["rubric_version"],
            "acceptance_method": review["acceptance"]["method"],
            "accepted": review["acceptance"]["accepted"],
            "objective_status": review["objective_verification"]["status"],
            "semantic_status": review["semantic_review"]["status"],
            "reviewer_provenance_class": review["reviewer"]["provenance_class"],
        },
        "provider_observation": {
            "model_status": provider["status"],
            "model_value": provider["value"],
            "model_source": provider["source"],
            "model_confidence": provider["confidence"],
            "token_status": token_usage["status"],
            "token_source": token_usage["source"],
            "token_confidence": token_usage["confidence"],
            "quality_attested": False,
            "safety_attested": False,
            "natural_origin_attested": False,
            "fallback_attested": False,
        },
        "source_digests": {
            "execution": _stable_id(execution),
            "review": _stable_id(review),
            "boundary": (
                _stable_id(validated_boundary)
                if evidence_class == "natural-boundary-event"
                else None
            ),
        },
        "boundary_event": (
            validated_boundary
            if evidence_class == "natural-boundary-event"
            else None
        ),
        "attestor_revision": _safe_id(attestor_revision, "attestor_revision"),
        "supersedes_attestation_id": supersedes_attestation_id,
        "privacy": {
            "raw_prompt_included": False,
            "raw_transcript_included": False,
            "argv_included": False,
            "private_identity_included": False,
            "credentials_included": False,
            "raw_paths_included": False,
            "session_or_thread_ids_included": False,
        },
        "report_only": True,
        "routing_mutation_allowed": False,
        "promotion_authority": False,
    }
    body["attestation_id"] = _stable_id(body)
    return validate_natural_execution_attestation(body)


def build_natural_boundary_event(
    task: dict[str, Any],
    *,
    scenario: str,
    observed_at: datetime,
) -> dict[str, Any]:
    """Derive a narrow boundary event from canonical terminal task facts."""
    observed = _aware_utc(observed_at, "observed_at")
    execution = _linked_execution(task)
    review = _latest_review(task)
    _validate_closure(task, execution, review, observed)
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else {}
    triggers = {
        "timeout": last_run.get("timed_out") is True,
        "nonzero_failure": (
            isinstance(last_run.get("returncode"), int)
            and not isinstance(last_run.get("returncode"), bool)
            and last_run["returncode"] != 0
        ),
        "optional_model_token_attestation": (
            execution["identity"]["provider_reported_model"]["status"] == "observed"
            or execution["token_usage"]["status"] == "observed"
        ),
    }
    if scenario not in triggers:
        raise NaturalExecutionAttestationError(
            "canonical source is unavailable for this natural boundary scenario"
        )
    if not triggers[scenario]:
        raise NaturalExecutionAttestationError(
            "natural boundary scenario is not present in canonical closure"
        )
    outcome = _reviewed_outcome(review)
    mutation = "unknown"
    body = {
        "schema_version": 1,
        "contract": "cbr-natural-boundary-event-v1",
        "observed_at": observed.isoformat(),
        "binding": {
            "task_id": _safe_id(task.get("id"), "task.id"),
            "attempt": _count(task.get("attempts"), "task.attempts"),
            "execution_evidence_id": execution["evidence_id"],
        },
        "scenario": scenario,
        "outcome": outcome,
        "mutation_provenance": mutation,
        "source_digest": _stable_id(
            {
                "execution": execution,
                "review": review,
                "terminal": {
                    "status": task.get("status"),
                    "timed_out": last_run.get("timed_out"),
                    "returncode": last_run.get("returncode"),
                },
            }
        ),
        "report_only": True,
        "mutation_allowed": False,
    }
    body["event_id"] = _stable_id(body)
    return validate_natural_boundary_event(body)


def validate_natural_boundary_event(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "contract",
        "observed_at",
        "binding",
        "scenario",
        "outcome",
        "mutation_provenance",
        "source_digest",
        "report_only",
        "mutation_allowed",
        "event_id",
    }:
        raise NaturalExecutionAttestationError(
            "natural boundary event fields are not canonical"
        )
    if (
        value.get("schema_version") != 1
        or value.get("contract") != "cbr-natural-boundary-event-v1"
        or value.get("report_only") is not True
        or value.get("mutation_allowed") is not False
    ):
        raise NaturalExecutionAttestationError("invalid natural boundary event contract")
    _timestamp(value.get("observed_at"), "boundary.observed_at")
    binding = value.get("binding")
    if not isinstance(binding, dict) or set(binding) != {
        "task_id", "attempt", "execution_evidence_id"
    }:
        raise NaturalExecutionAttestationError("invalid natural boundary binding")
    _safe_id(binding.get("task_id"), "boundary.task_id")
    _count(binding.get("attempt"), "boundary.attempt")
    _safe_id(
        binding.get("execution_evidence_id"), "boundary.execution_evidence_id"
    )
    if value.get("scenario") not in {
        "timeout",
        "nonzero_failure",
        "unsafe_changed_files",
        "worker_created_commit",
        "optional_model_token_attestation",
    }:
        raise NaturalExecutionAttestationError("unsupported natural boundary scenario")
    if value.get("outcome") not in OUTCOMES:
        raise NaturalExecutionAttestationError("invalid natural boundary outcome")
    if value.get("mutation_provenance") not in MUTATION_PROVENANCE:
        raise NaturalExecutionAttestationError(
            "invalid natural boundary mutation provenance"
        )
    if not _is_digest(value.get("source_digest")):
        raise NaturalExecutionAttestationError("invalid natural boundary source digest")
    claimed = value.get("event_id")
    body = dict(value)
    body.pop("event_id")
    if claimed != _stable_id(body):
        raise NaturalExecutionAttestationError("natural boundary event digest mismatch")
    return value


def attach_natural_boundary_event(
    task: dict[str, Any], event: dict[str, Any]
) -> None:
    validated = validate_natural_boundary_event(event)
    expected = build_natural_boundary_event(
        task,
        scenario=validated["scenario"],
        observed_at=_timestamp(validated["observed_at"], "boundary.observed_at"),
    )
    if validated != expected:
        raise NaturalExecutionAttestationError(
            "natural boundary event does not match current canonical closure"
        )
    history = task.setdefault("natural_boundary_event_history", [])
    if not isinstance(history, list):
        raise NaturalExecutionAttestationError(
            "natural boundary event history must be a list"
        )
    if not any(
        isinstance(item, dict) and item.get("event_id") == validated["event_id"]
        for item in history
    ):
        history.append(validated)


def attach_natural_execution_attestation(
    task: dict[str, Any], record: dict[str, Any]
) -> None:
    validated = validate_natural_execution_attestation(record)
    execution = _linked_execution(task)
    review = _latest_review(task)
    _validate_closure(
        task,
        execution,
        review,
        _timestamp(validated["recorded_at"], "recorded_at"),
    )
    binding = validated["binding"]
    if (
        binding["task_id"] != str(task.get("id"))
        or binding["attempt"] != task.get("attempts")
        or binding["execution_evidence_id"] != execution["evidence_id"]
    ):
        raise NaturalExecutionAttestationError(
            "attestation binding does not match destination task"
        )
    if (
        validated["review"]["evidence_id"] != review["evidence_id"]
        or validated["source_digests"]["execution"] != _stable_id(execution)
        or validated["source_digests"]["review"] != _stable_id(review)
    ):
        raise NaturalExecutionAttestationError(
            "attestation sources do not match current destination closure"
        )
    if validated["evidence"]["class"] == "natural-boundary-event":
        boundary = validated["boundary_event"]
        expected_boundary = build_natural_boundary_event(
            task,
            scenario=boundary["scenario"],
            observed_at=_timestamp(
                boundary["observed_at"], "boundary.observed_at"
            ),
        )
        boundary_history = task.get("natural_boundary_event_history")
        if (
            boundary != expected_boundary
            or not isinstance(boundary_history, list)
            or not any(item == boundary for item in boundary_history)
        ):
            raise NaturalExecutionAttestationError(
                "attestation boundary source is not in verified task history"
            )
    history = task.setdefault("natural_execution_attestation_history", [])
    if not isinstance(history, list):
        raise NaturalExecutionAttestationError(
            "natural execution attestation history must be a list"
        )
    if any(
        isinstance(item, dict)
        and item.get("attestation_id") == validated["attestation_id"]
        for item in history
    ):
        return
    supersedes = validated["supersedes_attestation_id"]
    if supersedes is not None:
        prior = next(
            (
                item
                for item in history
                if isinstance(item, dict) and item.get("attestation_id") == supersedes
            ),
            None,
        )
        if prior is None:
            raise NaturalExecutionAttestationError(
                "superseded attestation must already exist in history"
            )
        if prior.get("binding") != validated["binding"]:
            raise NaturalExecutionAttestationError(
                "superseding correction must preserve exact binding"
            )
    history.append(validated)


def build_natural_execution_attestation_report(
    records: list[dict[str, Any]] | None,
    *,
    as_of: datetime,
    verified_boundary_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    observed_at = _aware_utc(as_of, "as_of")
    validated = [validate_natural_execution_attestation(item) for item in records or []]
    boundary_registry = {
        event["event_id"]: validate_natural_boundary_event(event)
        for event in verified_boundary_events or []
    }
    for item in validated:
        if item["evidence"]["class"] != "natural-boundary-event":
            continue
        boundary = item["boundary_event"]
        if boundary_registry.get(boundary["event_id"]) != boundary:
            raise NaturalExecutionAttestationError(
                "natural boundary source is not verified for report"
            )
    ids = [item["attestation_id"] for item in validated]
    if len(ids) != len(set(ids)):
        raise NaturalExecutionAttestationError(
            "duplicate attestation ids are ineligible"
        )
    if any(_timestamp(item["recorded_at"], "recorded_at") > observed_at for item in validated):
        raise NaturalExecutionAttestationError("future attestation is ineligible")
    known_ids = set(ids)
    superseding_ids = [
        item["supersedes_attestation_id"]
        for item in validated
        if item["supersedes_attestation_id"] is not None
    ]
    if any(item not in known_ids for item in superseding_ids):
        raise NaturalExecutionAttestationError(
            "superseded attestation is missing from report history"
        )
    if len(superseding_ids) != len(set(superseding_ids)):
        raise NaturalExecutionAttestationError(
            "multiple corrections for one attestation are ineligible"
        )
    by_id = {item["attestation_id"]: item for item in validated}
    for item in validated:
        predecessor_id = item["supersedes_attestation_id"]
        if predecessor_id is None:
            continue
        predecessor = by_id[predecessor_id]
        if predecessor["binding"] != item["binding"]:
            raise NaturalExecutionAttestationError(
                "superseding correction must preserve exact binding"
            )
        if _timestamp(predecessor["recorded_at"], "recorded_at") >= _timestamp(
            item["recorded_at"], "recorded_at"
        ):
            raise NaturalExecutionAttestationError(
                "superseding correction must be strictly later"
            )
    _validate_supersession_graph(by_id)
    superseded_ids = {
        item["supersedes_attestation_id"]
        for item in validated
        if item["supersedes_attestation_id"] is not None
    }
    effective = [
        item for item in validated if item["attestation_id"] not in superseded_ids
    ]
    observation_keys: set[tuple[object, ...]] = set()
    for item in effective:
        key = (
            item["binding"]["task_id"],
            item["binding"]["attempt"],
            item["binding"]["execution_evidence_id"],
            item["evidence"]["class"],
            item["evidence"]["scenario"],
        )
        if key in observation_keys:
            raise NaturalExecutionAttestationError(
                "conflicting effective attestations are ineligible"
            )
        observation_keys.add(key)
    classes = {
        name: [item for item in effective if item["evidence"]["class"] == name]
        for name in sorted(EVIDENCE_CLASSES)
    }
    natural_records = (
        classes["natural-objective-run"]
        + classes["natural-boundary-event"]
    )
    worker_eligible_natural = [
        item
        for item in natural_records
        if item["evidence"]["mutation_provenance"]
        in {"no_mutation", "mutation_observed"}
    ]
    return {
        "schema_version": 1,
        "contract": "cbr-natural-execution-attestation-report-v1",
        "as_of": observed_at.isoformat(),
        "record_count": len(validated),
        "effective_record_count": len(effective),
        "superseded_record_count": len(superseded_ids),
        "classes": classes,
        "eligibility": {
            "natural_record_count": len(natural_records),
            "natural_worker_evidence_count": len(worker_eligible_natural),
            "natural_policy_ineligible_count": (
                len(natural_records) - len(worker_eligible_natural)
            ),
            "natural_policy_ineligible_reasons": (
                ["unknown_or_unverified_mutation_provenance"]
                if len(worker_eligible_natural) != len(natural_records)
                else []
            ),
            "provider_observation_count": len(classes["provider-observation"]),
            "synthetic_boundary_count": len(classes["synthetic-boundary"]),
            "live_routing": False,
            "promotion_authority": False,
        },
        "mutation_allowed": False,
    }


def natural_execution_attestation_view(
    task: dict[str, Any],
    *,
    as_of: datetime,
) -> dict[str, Any]:
    history = task.get("natural_execution_attestation_history")
    if history is None:
        records: list[dict[str, Any]] = []
    elif isinstance(history, list):
        records = history
    else:
        raise NaturalExecutionAttestationError(
            "natural execution attestation history must be a list"
        )
    boundary_history = task.get("natural_boundary_event_history")
    if boundary_history is None:
        verified_boundaries: list[dict[str, Any]] = []
    elif isinstance(boundary_history, list):
        # Events were recomputed from the then-current closure before append.
        # Their stable event/source digests preserve older attempts after retry.
        verified_boundaries = [
            validate_natural_boundary_event(event) for event in boundary_history
        ]
    else:
        raise NaturalExecutionAttestationError(
            "natural boundary event history must be a list"
        )
    return build_natural_execution_attestation_report(
        records,
        as_of=as_of,
        verified_boundary_events=verified_boundaries,
    )


def build_worker_certification_evidence(
    records: list[dict[str, Any]],
    *,
    candidate: object,
    as_of: datetime,
    verified_boundary_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project effective attestations into the existing advisory-only worker envelope."""
    candidate_value = _candidate(candidate)
    report = build_natural_execution_attestation_report(
        records,
        as_of=as_of,
        verified_boundary_events=verified_boundary_events,
    )
    natural_records = (
        report["classes"]["natural-objective-run"]
        + report["classes"]["natural-boundary-event"]
    )
    natural_records = [
        item
        for item in natural_records
        if item["evidence"]["mutation_provenance"]
        in {"no_mutation", "mutation_observed"}
    ]
    if not natural_records:
        raise NaturalExecutionAttestationError(
            "no policy-eligible natural worker evidence"
        )
    for item in natural_records:
        binding = item["binding"]
        for field in ("worker_id", "target_snapshot_id", "task_class"):
            if binding[field] != candidate_value[field]:
                raise NaturalExecutionAttestationError(
                    f"attestation binding {field} does not match candidate"
                )

    cohort_components = {
        (
            item["binding"]["execution_cohort_id"],
            item["binding"]["resolved_config_digest"],
            item["binding"]["target_id"],
            item["binding"]["worker_family"],
            item["binding"]["mapping_revision"],
            item["review"]["policy_version"],
            item["review"]["rubric_version"],
            item["review"]["acceptance_method"],
            item["review"]["reviewer_provenance_class"],
            item["attestor_revision"],
        )
        for item in natural_records
    }
    if len(cohort_components) != 1:
        raise NaturalExecutionAttestationError(
            "mixed natural execution cohorts are ineligible"
        )
    objective = [
        item for item in natural_records
        if item["evidence"]["class"] == "natural-objective-run"
    ]
    projected: list[dict[str, Any]] = []
    if objective:
        passed = sum(item["evidence"]["outcome"] == "pass" for item in objective)
        adverse = sum(
            item["evidence"]["outcome"] != "pass"
            or item["evidence"]["mutation_provenance"] == "mutation_observed"
            for item in objective
        )
        projected.append(
            {
                "evidence_id": _stable_id(
                    [item["attestation_id"] for item in objective]
                ),
                "evidence_class": "natural",
                "scenario": "objective_outcome",
                "outcome": "fail" if adverse else "pass",
                "mutation_provenance": _aggregate_mutation(objective),
                "sample_count": len(objective),
                "passed_count": passed,
                "adverse_count": adverse,
                "token_usage_attested": any(
                    item["provider_observation"]["token_status"] == "observed"
                    for item in objective
                ),
            }
        )
    for item in natural_records:
        if item["evidence"]["class"] != "natural-boundary-event":
            continue
        projected.append(
            {
                "evidence_id": item["attestation_id"],
                "evidence_class": "natural",
                "scenario": item["evidence"]["scenario"],
                "outcome": item["evidence"]["outcome"],
                "mutation_provenance": item["evidence"]["mutation_provenance"],
                "sample_count": 0,
                "passed_count": 0,
                "adverse_count": 0,
                "token_usage_attested": False,
            }
        )
    if not projected:
        raise NaturalExecutionAttestationError(
            "no eligible natural worker evidence"
        )
    return {
        "policy_revision": WORKER_POLICY_REVISION,
        "worker_id": candidate_value["worker_id"],
        "target_snapshot_id": candidate_value["target_snapshot_id"],
        "task_class": candidate_value["task_class"],
        "cohort_id": _stable_id(
            {
                "candidate": candidate_value,
                "binding": list(next(iter(cohort_components))),
                "attestor_contract": CONTRACT_VERSION,
            }
        ),
        "records": projected,
    }


def validate_natural_execution_attestation(record: object) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise NaturalExecutionAttestationError("attestation must be a JSON object")
    expected = {
        "schema_version",
        "contract",
        "kind",
        "recorded_at",
        "evidence",
        "binding",
        "execution",
        "review",
        "provider_observation",
        "source_digests",
        "boundary_event",
        "attestor_revision",
        "supersedes_attestation_id",
        "privacy",
        "report_only",
        "routing_mutation_allowed",
        "promotion_authority",
        "attestation_id",
    }
    if set(record) != expected:
        raise NaturalExecutionAttestationError("attestation fields are not canonical")
    if (
        record.get("schema_version") != SCHEMA_VERSION
        or record.get("contract") != CONTRACT_VERSION
        or record.get("kind") != "natural_execution_attestation"
    ):
        raise NaturalExecutionAttestationError("invalid attestation contract")
    for key in FORBIDDEN_KEYS:
        if _contains_key(record, key):
            raise NaturalExecutionAttestationError(
                f"attestation contains forbidden key: {key}"
            )
    recorded = _timestamp(record.get("recorded_at"), "recorded_at")
    evidence = record.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {
        "class", "scenario", "outcome", "mutation_provenance"
    }:
        raise NaturalExecutionAttestationError("invalid evidence")
    _validate_observation(
        evidence.get("class"),
        evidence.get("scenario"),
        evidence.get("outcome"),
        evidence.get("mutation_provenance"),
        None,
    )
    binding = record.get("binding")
    if not isinstance(binding, dict) or set(binding) != {
        "task_id",
        "attempt",
        "execution_evidence_id",
        "execution_cohort_id",
        "worker_family",
        "worker_id",
        "target_id",
        "target_snapshot_id",
        "resolved_config_digest",
        "task_class",
        "mapping_revision",
    }:
        raise NaturalExecutionAttestationError("invalid binding")
    for field in (
        "task_id",
        "execution_evidence_id",
        "execution_cohort_id",
        "worker_family",
        "worker_id",
        "target_id",
        "target_snapshot_id",
        "resolved_config_digest",
        "mapping_revision",
    ):
        _safe_id(binding.get(field), f"binding.{field}")
    if binding.get("task_class") not in TASK_CLASSES:
        raise NaturalExecutionAttestationError("binding.task_class is unsupported")
    _count(binding.get("attempt"), "binding.attempt")
    execution = record.get("execution")
    review = record.get("review")
    if not isinstance(execution, dict) or set(execution) != {
        "captured_at", "integrity_status", "identity_attestation"
    }:
        raise NaturalExecutionAttestationError("invalid execution closure")
    if not isinstance(review, dict) or set(review) != {
        "evidence_id",
        "captured_at",
        "policy_version",
        "rubric_version",
        "acceptance_method",
        "accepted",
        "objective_status",
        "semantic_status",
        "reviewer_provenance_class",
    }:
        raise NaturalExecutionAttestationError("invalid review closure")
    if _timestamp(execution.get("captured_at"), "execution.captured_at") > recorded:
        raise NaturalExecutionAttestationError("future execution evidence is ineligible")
    if _timestamp(review.get("captured_at"), "review.captured_at") > recorded:
        raise NaturalExecutionAttestationError("future review evidence is ineligible")
    if (
        execution.get("integrity_status") != "compliant"
        or execution.get("identity_attestation")
        not in {"verified", "command_attributed"}
    ):
        raise NaturalExecutionAttestationError(
            "execution closure is not exact and compliant"
        )
    if (
        not isinstance(review.get("evidence_id"), str)
        or not review["evidence_id"]
        or not isinstance(review.get("accepted"), bool)
        or review.get("objective_status")
        not in {"passed", "failed", "unavailable", "not_applicable"}
        or review.get("semantic_status")
        not in {"pass", "needs_fix", "needs_human", "failed_review", "not_performed"}
        or not all(
            isinstance(review.get(field), str) and review[field]
            for field in (
                "policy_version",
                "rubric_version",
                "acceptance_method",
                "reviewer_provenance_class",
            )
        )
    ):
        raise NaturalExecutionAttestationError("invalid review closure values")
    if review["reviewer_provenance_class"].startswith(
        ("kind=none ", "kind=unknown ")
    ):
        raise NaturalExecutionAttestationError(
            "absent reviewer provenance is ineligible"
        )
    _validate_review_consistency(evidence, review)
    provider = record.get("provider_observation")
    if not isinstance(provider, dict) or set(provider) != {
        "model_status",
        "model_value",
        "model_source",
        "model_confidence",
        "token_status",
        "token_source",
        "token_confidence",
        "quality_attested",
        "safety_attested",
        "natural_origin_attested",
        "fallback_attested",
    }:
        raise NaturalExecutionAttestationError("invalid provider observation")
    if any(
        provider.get(field) is not False
        for field in (
            "quality_attested",
            "safety_attested",
            "natural_origin_attested",
            "fallback_attested",
        )
    ):
        raise NaturalExecutionAttestationError(
            "provider observation cannot attest quality, safety, origin, or fallback"
        )
    _validate_provider_observation(provider)
    digests = record.get("source_digests")
    if (
        not isinstance(digests, dict)
        or set(digests) != {"execution", "review", "boundary"}
        or not _is_digest(digests.get("execution"))
        or not _is_digest(digests.get("review"))
    ):
        raise NaturalExecutionAttestationError("invalid source digests")
    if evidence["class"] == "natural-boundary-event":
        if not _is_digest(digests.get("boundary")):
            raise NaturalExecutionAttestationError(
                "natural boundary evidence requires a source digest"
            )
        boundary = validate_natural_boundary_event(record.get("boundary_event"))
        if (
            digests["boundary"] != _stable_id(boundary)
            or boundary["binding"]["task_id"] != binding["task_id"]
            or boundary["binding"]["attempt"] != binding["attempt"]
            or boundary["binding"]["execution_evidence_id"]
            != binding["execution_evidence_id"]
            or boundary["scenario"] != evidence["scenario"]
            or boundary["outcome"] != evidence["outcome"]
            or boundary["mutation_provenance"]
            != evidence["mutation_provenance"]
            or _timestamp(boundary["observed_at"], "boundary.observed_at")
            > recorded
        ):
            raise NaturalExecutionAttestationError(
                "natural boundary source does not match attestation"
            )
    elif digests.get("boundary") is not None:
        raise NaturalExecutionAttestationError(
            "non-boundary evidence cannot claim a boundary source"
        )
    elif record.get("boundary_event") is not None:
        raise NaturalExecutionAttestationError(
            "non-boundary evidence cannot include a boundary event"
        )
    _safe_id(record.get("attestor_revision"), "attestor_revision")
    supersedes = record.get("supersedes_attestation_id")
    if supersedes is not None:
        _safe_id(supersedes, "supersedes_attestation_id")
        if supersedes == record.get("attestation_id"):
            raise NaturalExecutionAttestationError("attestation cannot supersede itself")
    privacy = record.get("privacy")
    expected_privacy = {
        "raw_prompt_included",
        "raw_transcript_included",
        "argv_included",
        "private_identity_included",
        "credentials_included",
        "raw_paths_included",
        "session_or_thread_ids_included",
    }
    if (
        not isinstance(privacy, dict)
        or set(privacy) != expected_privacy
        or any(value is not False for value in privacy.values())
    ):
        raise NaturalExecutionAttestationError("privacy flags must all be false")
    if (
        record.get("report_only") is not True
        or record.get("routing_mutation_allowed") is not False
        or record.get("promotion_authority") is not False
    ):
        raise NaturalExecutionAttestationError("attestation must remain report-only")
    claimed_id = record.get("attestation_id")
    body = dict(record)
    body.pop("attestation_id")
    if claimed_id != _stable_id(body):
        raise NaturalExecutionAttestationError("attestation digest mismatch")
    return record


def _linked_execution(task: dict[str, Any]) -> dict[str, Any]:
    last_run = task.get("last_run")
    history = task.get("execution_evidence_history")
    if not isinstance(last_run, dict) or not isinstance(history, list):
        raise NaturalExecutionAttestationError("linked execution evidence is required")
    evidence_id = last_run.get("execution_evidence_id")
    matches = [
        item
        for item in history
        if isinstance(item, dict) and item.get("evidence_id") == evidence_id
    ]
    if not evidence_id or len(matches) != 1:
        raise NaturalExecutionAttestationError(
            "exactly one linked execution evidence record is required"
        )
    try:
        return validate_execution_evidence_v3(matches[0])
    except ValueError as exc:
        raise NaturalExecutionAttestationError(str(exc)) from exc


def _latest_review(task: dict[str, Any]) -> dict[str, Any]:
    history = task.get("review_outcome_evidence_history")
    if not isinstance(history, list) or not history:
        raise NaturalExecutionAttestationError("final review outcome evidence is required")
    candidates = [item for item in history if isinstance(item, dict)]
    if not candidates:
        raise NaturalExecutionAttestationError("final review outcome evidence is required")
    try:
        return validate_review_outcome_evidence(candidates[-1])
    except ValueError as exc:
        raise NaturalExecutionAttestationError(str(exc)) from exc


def _validate_closure(
    task: dict[str, Any],
    execution: dict[str, Any],
    review: dict[str, Any],
    recorded: datetime,
) -> None:
    if task.get("status") not in {"completed", "failed"}:
        raise NaturalExecutionAttestationError("nonterminal execution is ineligible")
    if execution["attempt"] != _count(task.get("attempts"), "task.attempts"):
        raise NaturalExecutionAttestationError("execution attempt does not match task")
    expected_execution_id = _stable_id(
        {
            "task_id": task.get("id"),
            "attempt": task.get("attempts"),
            "captured_at": execution["captured_at"],
            "integrity": execution["integrity"]["status"],
        }
    )
    if execution["evidence_id"] != expected_execution_id:
        raise NaturalExecutionAttestationError(
            "execution evidence is not bound to task and attempt"
        )
    expected_review_id = _review_evidence_id(
        task_id=task.get("id"),
        attempt=task.get("attempts"),
        captured_at=review["captured_at"],
        acceptance_method=review["acceptance"]["method"],
    )
    if review["evidence_id"] != expected_review_id:
        raise NaturalExecutionAttestationError(
            "review evidence is not bound to task and attempt"
        )
    components = review["cohort"]["components"]
    if components["execution_cohort_id"] != execution["cohort"]["cohort_id"]:
        raise NaturalExecutionAttestationError(
            "review outcome is not bound to the execution cohort"
        )
    if execution["integrity"] != {"status": "compliant", "adverse": False}:
        raise NaturalExecutionAttestationError("adverse execution integrity is ineligible")
    if review["acceptance"]["method"] in {"mechanical_safe", "none"}:
        raise NaturalExecutionAttestationError(
            "mechanical or absent review closure is ineligible"
        )
    reviewer_kind = review["reviewer"]["kind"]
    method = review["acceptance"]["method"]
    if (
        reviewer_kind in {"none", "unknown"}
        or (method == "human_accept" and reviewer_kind != "human")
        or (method == "external_review" and reviewer_kind != "external")
        or (
            method == "reviewer_pass"
            and reviewer_kind not in {"codex", "human", "external"}
        )
    ):
        raise NaturalExecutionAttestationError(
            "reviewer authority does not match acceptance method"
        )
    if _timestamp(execution["captured_at"], "execution.captured_at") > recorded:
        raise NaturalExecutionAttestationError("future execution evidence is ineligible")
    if _timestamp(review["captured_at"], "review.captured_at") > recorded:
        raise NaturalExecutionAttestationError("future review evidence is ineligible")


def _mapping(value: object, execution: dict[str, Any]) -> dict[str, str]:
    expected = {
        "worker_family",
        "worker_id",
        "target_id",
        "target_snapshot_id",
        "task_class",
        "mapping_revision",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise NaturalExecutionAttestationError("mapping fields are not canonical")
    result = {
        field: _safe_id(value.get(field), f"mapping.{field}")
        for field in expected
        if field != "task_class"
    }
    if value.get("task_class") not in TASK_CLASSES:
        raise NaturalExecutionAttestationError("mapping.task_class is unsupported")
    result["task_class"] = str(value["task_class"])
    if result["target_id"] != execution["routing"]["target_id"]:
        raise NaturalExecutionAttestationError(
            "mapping.target_id does not match execution evidence"
        )
    return result


def _candidate(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "worker_id", "target_snapshot_id", "task_class"
    }:
        raise NaturalExecutionAttestationError("candidate fields are not canonical")
    if value.get("task_class") not in TASK_CLASSES:
        raise NaturalExecutionAttestationError("candidate.task_class is unsupported")
    return {
        "worker_id": _safe_id(value.get("worker_id"), "candidate.worker_id"),
        "target_snapshot_id": _safe_id(
            value.get("target_snapshot_id"), "candidate.target_snapshot_id"
        ),
        "task_class": str(value["task_class"]),
    }


def _validate_observation(
    evidence_class: object,
    scenario: object,
    outcome: object,
    mutation_provenance: object,
    review: dict[str, Any] | None,
) -> None:
    if evidence_class not in EVIDENCE_CLASSES:
        raise NaturalExecutionAttestationError("unsupported evidence class")
    if outcome not in OUTCOMES:
        raise NaturalExecutionAttestationError("unsupported evidence outcome")
    if mutation_provenance not in MUTATION_PROVENANCE:
        raise NaturalExecutionAttestationError("unsupported mutation provenance")
    if (
        evidence_class in {"natural-objective-run", "natural-boundary-event"}
        and mutation_provenance != "unknown"
    ):
        raise NaturalExecutionAttestationError(
            "natural mutation provenance is unknown without verified mutation evidence"
        )
    if evidence_class == "natural-objective-run" and scenario != "objective_outcome":
        raise NaturalExecutionAttestationError(
            "natural objective evidence requires objective_outcome"
        )
    if evidence_class in {"natural-boundary-event", "synthetic-boundary"}:
        if scenario not in BOUNDARY_SCENARIOS:
            raise NaturalExecutionAttestationError("unsupported boundary scenario")
    if evidence_class == "provider-observation" and scenario != "provider_observation":
        raise NaturalExecutionAttestationError(
            "provider observation requires provider_observation scenario"
        )
    if review is not None and evidence_class == "natural-objective-run":
        expected = _reviewed_outcome(review)
        if outcome != expected:
            raise NaturalExecutionAttestationError(
                "objective outcome does not match reviewer closure"
            )


def _aggregate_mutation(records: list[dict[str, Any]]) -> str:
    values = {
        item["evidence"]["mutation_provenance"]
        for item in records
    }
    for value in ("mutation_observed", "unknown", "mutation_possible", "no_mutation"):
        if value in values:
            return value
    return "unknown"


def _validate_review_consistency(
    evidence: dict[str, Any], review: dict[str, Any]
) -> None:
    if evidence["class"] != "natural-objective-run":
        return
    expected = _reviewed_outcome_projection(review)
    if evidence["outcome"] != expected:
        raise NaturalExecutionAttestationError(
            "objective outcome does not match reviewer closure"
        )


def _reviewed_outcome(review: dict[str, Any]) -> str:
    projection = {
        "accepted": review["acceptance"]["accepted"],
        "objective_status": review["objective_verification"]["status"],
        "semantic_status": review["semantic_review"]["status"],
        "acceptance_method": review["acceptance"]["method"],
    }
    return _reviewed_outcome_projection(projection)


def _reviewed_outcome_projection(review: dict[str, Any]) -> str:
    if review.get("acceptance_method") in {"mechanical_safe", "none"}:
        return "unknown"
    if (
        review.get("accepted")
        and review.get("objective_status") == "passed"
        and review.get("semantic_status") == "pass"
    ):
        return "pass"
    if (
        review.get("objective_status") == "failed"
        or review.get("semantic_status") in {"needs_fix", "failed_review"}
    ):
        return "fail"
    return "unknown"


def _validate_provider_observation(provider: dict[str, Any]) -> None:
    model_status = provider.get("model_status")
    if model_status == "observed":
        if (
            not isinstance(provider.get("model_value"), str)
            or not provider["model_value"]
            or provider.get("model_source")
            not in {"codex_jsonl", "external_wrapper_attestation"}
            or provider.get("model_confidence")
            not in {"provider_observed", "wrapper_attested"}
        ):
            raise NaturalExecutionAttestationError(
                "observed provider model metadata is invalid"
            )
    elif model_status == "unavailable":
        if any(
            provider.get(field) is not None
            for field in ("model_value", "model_source", "model_confidence")
        ):
            raise NaturalExecutionAttestationError(
                "unavailable provider model must not claim identity"
            )
    else:
        raise NaturalExecutionAttestationError("invalid provider model status")
    token_status = provider.get("token_status")
    if token_status == "observed":
        if (
            provider.get("token_source")
            not in {"codex_jsonl", "external_wrapper_attestation"}
            or provider.get("token_confidence")
            not in {"provider_observed", "wrapper_attested"}
        ):
            raise NaturalExecutionAttestationError(
                "observed provider token metadata is invalid"
            )
    elif token_status == "unavailable":
        if any(
            provider.get(field) is not None
            for field in ("token_source", "token_confidence")
        ):
            raise NaturalExecutionAttestationError(
                "unavailable provider tokens must not claim attribution"
            )
    else:
        raise NaturalExecutionAttestationError("invalid provider token status")


def _validate_supersession_graph(by_id: dict[str, dict[str, Any]]) -> None:
    for start in by_id:
        seen: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in seen:
                raise NaturalExecutionAttestationError(
                    "supersession cycle is ineligible"
                )
            seen.add(current)
            current = by_id[current]["supersedes_attestation_id"]


def _review_evidence_id(
    *,
    task_id: object,
    attempt: object,
    captured_at: object,
    acceptance_method: object,
) -> str:
    payload = {
        "task_id": str(task_id or "unknown"),
        "attempt": int(attempt or 0),
        "captured_at": captured_at,
        "acceptance_method": acceptance_method,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "review-sha256:" + digest[:24]


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.split(":", 1)[1]
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _safe_id(value: object, field: str) -> str:
    text = str(value or "")
    if (
        not text
        or len(text) > 200
        or any(char not in _SAFE_ID_CHARS for char in text)
    ):
        raise NaturalExecutionAttestationError(f"{field} must be a public-safe identifier")
    return text


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NaturalExecutionAttestationError(f"{field} must be a non-negative integer")
    return value


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise NaturalExecutionAttestationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise NaturalExecutionAttestationError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NaturalExecutionAttestationError(
            f"{field} must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise NaturalExecutionAttestationError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _stable_id(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()

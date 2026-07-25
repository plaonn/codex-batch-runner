from __future__ import annotations

import hashlib
import json
import copy
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .execution_mutation_provenance import (
    CONTRACT_VERSION as MUTATION_CONTRACT,
    SCOPE,
    attach_execution_mutation_provenance,
    validate_execution_mutation_provenance,
)
from .execution_delegation import (
    preexecution_delegation_view,
    validate_execution_delegation_contract,
    validate_preexecution_delegation_receipt,
)
from .natural_execution_attestation import (
    CONTRACT_VERSION as NATURAL_CONTRACT,
    attach_natural_execution_attestation,
    build_natural_execution_attestation_report,
    validate_natural_execution_attestation,
)
from .queue import load_task


POLICY_CONTRACT = "scoped-readonly-certification-policy-v1"
PROJECTION_CONTRACT = "scoped-readonly-certification-projection-v1"
REPORT_CONTRACT = "scoped-readonly-certification-report-v1"
REPORT_BUNDLE_CONTRACT = "scoped-readonly-certification-report-bundle-v1"
POLICY_REVISION = "scoped-readonly-certification-policy-v1"
SCHEMA_VERSION = 1
TASK_CLASS = "readonly-objective"
MIN_SAMPLE_COUNT = 20
MIN_PASS_RATIO = 0.95
MAX_ADVERSE_SIGNALS = 0
REVALIDATION_DAYS = 30
STATUSES = {"insufficient", "eligible-scoped-readonly", "disabled"}
FORBIDDEN_KEYS = {
    "prompt",
    "transcript",
    "stdout",
    "stderr",
    "command",
    "argv",
    "cwd",
    "path",
    "session_id",
    "thread_id",
    "credential",
    "account",
    "email",
}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")


class ScopedReadonlyCertificationError(ValueError):
    pass


def project_scoped_readonly_certification(
    *,
    config: Config,
    candidate: object,
    samples: object,
    evaluated_at: datetime,
) -> dict[str, Any]:
    evaluated = _aware_utc(evaluated_at, "evaluated_at")
    candidate_value = _candidate(candidate)
    if not isinstance(samples, list):
        raise ScopedReadonlyCertificationError("samples must be a list")
    raw_qualified: list[tuple[int, dict[str, Any]]] = []
    exclusions: list[dict[str, Any]] = []
    source_conflict = False
    for index, sample in enumerate(samples):
        try:
            value = _qualify_sample(
                config, sample, candidate_value, evaluated
            )
        except (ScopedReadonlyCertificationError, ValueError) as exc:
            reason = _safe_reason(str(exc))
            if reason == "conflicting_effective_attestations_are_ineligible":
                source_conflict = True
                reason = "conflicting_sample"
            exclusions.append({"index": index, "reason": reason})
            continue
        raw_qualified.append((index, value))

    by_execution: dict[tuple[str, int, str], list[tuple[int, dict[str, Any]]]] = {}
    for index, value in raw_qualified:
        key = (
            value["task_id"],
            value["attempt"],
            value["execution_evidence_id"],
        )
        by_execution.setdefault(key, []).append((index, value))
    qualified: list[dict[str, Any]] = []
    conflicting_sample = source_conflict
    for values in by_execution.values():
        by_id: dict[str, list[int]] = {}
        for index, value in values:
            by_id.setdefault(value["sample_id"], []).append(index)
        if len(by_id) > 1:
            conflicting_sample = True
            exclusions.extend(
                {"index": index, "reason": "conflicting_sample"}
                for index, _value in values
            )
            continue
        chosen_id = next(iter(by_id))
        chosen = next(value for _index, value in values if value["sample_id"] == chosen_id)
        qualified.append(chosen)
        duplicate_indexes = sorted(by_id[chosen_id])[1:]
        exclusions.extend(
            {"index": index, "reason": "duplicate_sample"}
            for index in duplicate_indexes
        )

    cohorts = {item["cohort_id"] for item in qualified}
    mixed_cohort = len(cohorts) > 1
    passed_count = sum(item["outcome"] == "pass" for item in qualified)
    adverse_count = sum(item["adverse"] for item in qualified)
    sample_count = len(qualified)
    pass_ratio = passed_count / sample_count if sample_count else None
    reasons: list[str] = []
    if mixed_cohort:
        reasons.append("mixed_cohort")
    if conflicting_sample:
        reasons.append("conflicting_sample")
    if sample_count < MIN_SAMPLE_COUNT:
        reasons.append("insufficient_samples")
    if pass_ratio is None or pass_ratio < MIN_PASS_RATIO:
        reasons.append("objective_pass_ratio_below_floor")
    if adverse_count > MAX_ADVERSE_SIGNALS:
        reasons.append("adverse_signal_observed")
    if exclusions:
        reasons.append("policy_ineligible_samples_excluded")

    if mixed_cohort or conflicting_sample or adverse_count > MAX_ADVERSE_SIGNALS:
        status = "disabled"
    elif (
        sample_count >= MIN_SAMPLE_COUNT
        and pass_ratio is not None
        and pass_ratio >= MIN_PASS_RATIO
    ):
        status = "eligible-scoped-readonly"
    else:
        status = "insufficient"
    body = {
        "schema_version": SCHEMA_VERSION,
        "contract": PROJECTION_CONTRACT,
        "policy_contract": POLICY_CONTRACT,
        "policy_revision": POLICY_REVISION,
        "kind": "scoped_readonly_certification_projection",
        "evaluated_at": evaluated.isoformat(),
        "expires_at": (evaluated + timedelta(days=REVALIDATION_DAYS)).isoformat(),
        "candidate": candidate_value,
        "scope": SCOPE,
        "cohort_id": next(iter(cohorts)) if len(cohorts) == 1 else None,
        "status": status,
        "sample_count": sample_count,
        "passed_count": passed_count,
        "objective_pass_ratio": pass_ratio,
        "adverse_count": adverse_count,
        "minimum_sample_count": MIN_SAMPLE_COUNT,
        "minimum_objective_pass_ratio": MIN_PASS_RATIO,
        "maximum_adverse_signals": MAX_ADVERSE_SIGNALS,
        "revalidation_days": REVALIDATION_DAYS,
        "sample_ids": [item["sample_id"] for item in qualified],
        "excluded_samples": exclusions,
        "reasons": sorted(set(reasons)),
        "global_provenance": "unknown",
        "existing_global_worker_certification_semantics_changed": False,
        "actual_canary": False,
        "promotion_authority": False,
        "routing_mutation_allowed": False,
        "worker_selection_or_dispatch_allowed": False,
        "queue_or_config_mutation_allowed": False,
        "report_only": True,
        "privacy": _privacy(),
    }
    body["projection_id"] = _stable_id(body)
    return validate_scoped_readonly_certification_projection(body)


def validate_scoped_readonly_certification_projection(
    value: object,
) -> dict[str, Any]:
    expected = {
        "schema_version",
        "contract",
        "policy_contract",
        "policy_revision",
        "kind",
        "evaluated_at",
        "expires_at",
        "candidate",
        "scope",
        "cohort_id",
        "status",
        "sample_count",
        "passed_count",
        "objective_pass_ratio",
        "adverse_count",
        "minimum_sample_count",
        "minimum_objective_pass_ratio",
        "maximum_adverse_signals",
        "revalidation_days",
        "sample_ids",
        "excluded_samples",
        "reasons",
        "global_provenance",
        "existing_global_worker_certification_semantics_changed",
        "actual_canary",
        "promotion_authority",
        "routing_mutation_allowed",
        "worker_selection_or_dispatch_allowed",
        "queue_or_config_mutation_allowed",
        "report_only",
        "privacy",
        "projection_id",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ScopedReadonlyCertificationError(
            "scoped readonly projection fields are not canonical"
        )
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["contract"] != PROJECTION_CONTRACT
        or value["policy_contract"] != POLICY_CONTRACT
        or value["policy_revision"] != POLICY_REVISION
        or value["kind"] != "scoped_readonly_certification_projection"
        or value["scope"] != SCOPE
        or value["status"] not in STATUSES
        or value["global_provenance"] != "unknown"
        or value["existing_global_worker_certification_semantics_changed"] is not False
        or value["actual_canary"] is not False
        or value["promotion_authority"] is not False
        or value["routing_mutation_allowed"] is not False
        or value["worker_selection_or_dispatch_allowed"] is not False
        or value["queue_or_config_mutation_allowed"] is not False
        or value["report_only"] is not True
    ):
        raise ScopedReadonlyCertificationError(
            "invalid scoped readonly projection contract"
        )
    evaluated = _timestamp(value["evaluated_at"], "evaluated_at")
    expires = _timestamp(value["expires_at"], "expires_at")
    if expires != evaluated + timedelta(days=REVALIDATION_DAYS):
        raise ScopedReadonlyCertificationError("projection expiry mismatch")
    if value["candidate"] != _candidate(value["candidate"]):
        raise ScopedReadonlyCertificationError("invalid projection candidate")
    for field in ("sample_count", "passed_count", "adverse_count"):
        _count(value[field], field)
    if (
        value["passed_count"] > value["sample_count"]
        or value["adverse_count"] > value["sample_count"]
        or value["minimum_sample_count"] != MIN_SAMPLE_COUNT
        or value["minimum_objective_pass_ratio"] != MIN_PASS_RATIO
        or value["maximum_adverse_signals"] != MAX_ADVERSE_SIGNALS
        or value["revalidation_days"] != REVALIDATION_DAYS
    ):
        raise ScopedReadonlyCertificationError("projection counts are invalid")
    expected_ratio = (
        value["passed_count"] / value["sample_count"]
        if value["sample_count"]
        else None
    )
    if value["objective_pass_ratio"] != expected_ratio:
        raise ScopedReadonlyCertificationError(
            "projection objective pass ratio mismatch"
        )
    if not isinstance(value["sample_ids"], list) or any(
        not _is_digest(item) for item in value["sample_ids"]
    ):
        raise ScopedReadonlyCertificationError("invalid projection sample ids")
    if len(value["sample_ids"]) != len(set(value["sample_ids"])):
        raise ScopedReadonlyCertificationError("duplicate projection sample ids")
    if len(value["sample_ids"]) != value["sample_count"]:
        raise ScopedReadonlyCertificationError(
            "projection sample count does not match ids"
        )
    if not isinstance(value["excluded_samples"], list) or not isinstance(
        value["reasons"], list
    ):
        raise ScopedReadonlyCertificationError("invalid projection reasons")
    if not all(
        isinstance(item, dict)
        and set(item) == {"index", "reason"}
        and isinstance(item["index"], int)
        and not isinstance(item["index"], bool)
        and item["index"] >= 0
        and isinstance(item["reason"], str)
        and bool(item["reason"])
        for item in value["excluded_samples"]
    ):
        raise ScopedReadonlyCertificationError(
            "invalid excluded sample records"
        )
    if value["reasons"] != sorted(set(value["reasons"])):
        raise ScopedReadonlyCertificationError(
            "projection reasons are not canonical"
        )
    mixed = "mixed_cohort" in value["reasons"]
    conflicting = "conflicting_sample" in value["reasons"]
    expected_status = (
        "disabled"
        if mixed or conflicting or value["adverse_count"] > MAX_ADVERSE_SIGNALS
        else "eligible-scoped-readonly"
        if (
            value["sample_count"] >= MIN_SAMPLE_COUNT
            and expected_ratio is not None
            and expected_ratio >= MIN_PASS_RATIO
        )
        else "insufficient"
    )
    if value["status"] != expected_status:
        raise ScopedReadonlyCertificationError(
            "projection status does not match advisory floor"
        )
    if value["cohort_id"] is not None and not _is_digest(value["cohort_id"]):
        raise ScopedReadonlyCertificationError("invalid projection cohort id")
    _validate_privacy(value)
    _validate_digest(value, "projection_id")
    return value


def build_scoped_readonly_certification_report(
    projection: object,
    *,
    config: Config,
    candidate: object,
    samples: object,
    as_of: datetime,
) -> dict[str, Any]:
    value = validate_scoped_readonly_certification_projection(projection)
    rebuilt = project_scoped_readonly_certification(
        config=config,
        candidate=candidate,
        samples=samples,
        evaluated_at=_timestamp(value["evaluated_at"], "evaluated_at"),
    )
    if rebuilt != value:
        raise ScopedReadonlyCertificationError(
            "projection does not match verified source samples"
        )
    observed = _aware_utc(as_of, "as_of")
    evaluated = _timestamp(value["evaluated_at"], "evaluated_at")
    expires = _timestamp(value["expires_at"], "expires_at")
    if evaluated > observed:
        raise ScopedReadonlyCertificationError(
            "future scoped readonly projection is ineligible"
        )
    expired = observed >= expires
    status = "disabled" if expired else value["status"]
    reasons = sorted(
        set(value["reasons"] + (["projection_expired"] if expired else []))
    )
    body = {
        "schema_version": SCHEMA_VERSION,
        "contract": REPORT_CONTRACT,
        "policy_revision": POLICY_REVISION,
        "as_of": observed.isoformat(),
        "projection_id": value["projection_id"],
        "candidate": value["candidate"],
        "scope": SCOPE,
        "cohort_id": value["cohort_id"],
        "status": status,
        "sample_count": value["sample_count"],
        "passed_count": value["passed_count"],
        "objective_pass_ratio": value["objective_pass_ratio"],
        "adverse_count": value["adverse_count"],
        "excluded_sample_count": len(value["excluded_samples"]),
        "reasons": reasons,
        "global_provenance": "unknown",
        "actual_canary": False,
        "promotion_authority": False,
        "routing_mutation_allowed": False,
        "worker_selection_or_dispatch_allowed": False,
        "queue_or_config_mutation_allowed": False,
        "report_only": True,
    }
    body["report_id"] = _stable_id(body)
    return validate_scoped_readonly_certification_report(body)


def validate_scoped_readonly_certification_report(
    value: object,
) -> dict[str, Any]:
    expected = {
        "schema_version",
        "contract",
        "policy_revision",
        "as_of",
        "projection_id",
        "candidate",
        "scope",
        "cohort_id",
        "status",
        "sample_count",
        "passed_count",
        "objective_pass_ratio",
        "adverse_count",
        "excluded_sample_count",
        "reasons",
        "global_provenance",
        "actual_canary",
        "promotion_authority",
        "routing_mutation_allowed",
        "worker_selection_or_dispatch_allowed",
        "queue_or_config_mutation_allowed",
        "report_only",
        "report_id",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ScopedReadonlyCertificationError(
            "scoped readonly report fields are not canonical"
        )
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["contract"] != REPORT_CONTRACT
        or value["policy_revision"] != POLICY_REVISION
        or value["scope"] != SCOPE
        or value["status"] not in STATUSES
        or value["global_provenance"] != "unknown"
        or value["actual_canary"] is not False
        or value["promotion_authority"] is not False
        or value["routing_mutation_allowed"] is not False
        or value["worker_selection_or_dispatch_allowed"] is not False
        or value["queue_or_config_mutation_allowed"] is not False
        or value["report_only"] is not True
        or not _is_digest(value["projection_id"])
        or _candidate(value["candidate"]) != value["candidate"]
        or (
            value["cohort_id"] is not None
            and not _is_digest(value["cohort_id"])
        )
    ):
        raise ScopedReadonlyCertificationError(
            "invalid scoped readonly report contract"
        )
    _timestamp(value["as_of"], "as_of")
    for field in (
        "sample_count",
        "passed_count",
        "adverse_count",
        "excluded_sample_count",
    ):
        _count(value[field], field)
    expected_ratio = (
        value["passed_count"] / value["sample_count"]
        if value["sample_count"]
        else None
    )
    if (
        value["passed_count"] > value["sample_count"]
        or value["adverse_count"] > value["sample_count"]
        or value["objective_pass_ratio"] != expected_ratio
        or not isinstance(value["reasons"], list)
        or value["reasons"] != sorted(set(value["reasons"]))
    ):
        raise ScopedReadonlyCertificationError(
            "invalid scoped readonly report values"
        )
    _validate_digest(value, "report_id")
    return value


def build_scoped_readonly_certification_report_bundle(
    config: Config, tasks: object, *, as_of: datetime
) -> dict[str, Any]:
    observed = _aware_utc(as_of, "as_of")
    if not isinstance(tasks, list):
        raise ScopedReadonlyCertificationError("tasks must be a list")
    groups: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        effective = _natural_records_for_bundle(task, observed)
        for natural in effective:
            binding = natural.get("binding")
            mutation_binding = natural.get("mutation_binding")
            if (
                not isinstance(binding, dict)
                or not isinstance(mutation_binding, dict)
                or binding.get("task_class") != TASK_CLASS
            ):
                continue
            candidate = {
                "worker_id": binding.get("worker_id"),
                "target_snapshot_id": binding.get("target_snapshot_id"),
                "task_class": TASK_CLASS,
            }
            try:
                candidate_value = _candidate(candidate)
            except ScopedReadonlyCertificationError:
                continue
            receipt_view = preexecution_delegation_view(task)
            receipt_id = receipt_view.get("receipt_id")
            if not isinstance(receipt_id, str):
                receipt_id = "missing-delegation-receipt"
            sample = {
                "task_id": task.get("id"),
                "natural_attestation_id": natural.get("attestation_id"),
                "mutation_provenance_id": mutation_binding.get(
                    "provenance_id"
                ),
                "delegation_receipt_id": receipt_id,
            }
            group_id = _stable_id(candidate_value)
            group = groups.setdefault(
                group_id,
                {"candidate": candidate_value, "samples": []},
            )
            group["samples"].append(sample)
    reports = []
    for group_id in sorted(groups):
        group = groups[group_id]
        projection = project_scoped_readonly_certification(
            config=config,
            candidate=group["candidate"],
            samples=group["samples"],
            evaluated_at=observed,
        )
        reports.append(
            build_scoped_readonly_certification_report(
                projection,
                config=config,
                candidate=group["candidate"],
                samples=group["samples"],
                as_of=observed,
            )
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": REPORT_BUNDLE_CONTRACT,
        "policy_revision": POLICY_REVISION,
        "as_of": observed.isoformat(),
        "scope": SCOPE,
        "cohort_count": len(reports),
        "reports": reports,
        "global_provenance": "unknown",
        "actual_canary": False,
        "promotion_authority": False,
        "routing_mutation_allowed": False,
        "worker_selection_or_dispatch_allowed": False,
        "queue_or_config_mutation_allowed": False,
        "report_only": True,
    }


def _effective_natural_objective_records(
    task: dict[str, Any], as_of: datetime
) -> list[dict[str, Any]]:
    history = task.get("natural_execution_attestation_history")
    if not isinstance(history, list):
        return []
    boundary_events = [
        item["boundary_event"]
        for item in history
        if isinstance(item, dict)
        and item.get("evidence", {}).get("class")
        == "natural-boundary-event"
        and isinstance(item.get("boundary_event"), dict)
    ]
    report = build_natural_execution_attestation_report(
        history,
        as_of=as_of,
        verified_boundary_events=boundary_events,
    )
    classes = report.get("classes")
    records = (
        classes.get("natural-objective-run")
        if isinstance(classes, dict)
        else None
    )
    return list(records) if isinstance(records, list) else []


def _natural_records_for_bundle(
    task: dict[str, Any], as_of: datetime
) -> list[dict[str, Any]]:
    try:
        return _effective_natural_objective_records(task, as_of)
    except ValueError as exc:
        if (
            _safe_reason(str(exc))
            != "conflicting_effective_attestations_are_ineligible"
        ):
            return []
    records: list[dict[str, Any]] = []
    history = task.get("natural_execution_attestation_history")
    for item in history if isinstance(history, list) else []:
        try:
            validated = validate_natural_execution_attestation(item)
        except ValueError:
            continue
        if validated["evidence"]["class"] == "natural-objective-run":
            records.append(validated)
    return records


def _qualify_sample(
    config: Config,
    sample: object,
    candidate: dict[str, str],
    evaluated: datetime,
) -> dict[str, Any]:
    if not isinstance(sample, dict) or set(sample) != {
        "task_id",
        "natural_attestation_id",
        "mutation_provenance_id",
        "delegation_receipt_id",
    }:
        raise ScopedReadonlyCertificationError("sample fields are not canonical")
    try:
        task = load_task(
            config, _safe_id(sample["task_id"], "sample.task_id")
        )
    except (FileNotFoundError, ValueError) as exc:
        raise ScopedReadonlyCertificationError(
            "canonical queue task is unavailable"
        ) from exc
    effective_ids = {
        item["attestation_id"]
        for item in _effective_natural_objective_records(task, evaluated)
    }
    if sample["natural_attestation_id"] not in effective_ids:
        raise ScopedReadonlyCertificationError(
            "natural attestation is not current effective evidence"
        )
    natural = _history_record(
        task,
        "natural_execution_attestation_history",
        "attestation_id",
        sample["natural_attestation_id"],
        "natural attestation",
    )
    mutation = _history_record(
        task,
        "execution_mutation_provenance_history",
        "provenance_id",
        sample["mutation_provenance_id"],
        "mutation provenance",
    )
    receipt = _history_record(
        task,
        "preexecution_delegation_receipt_history",
        "receipt_id",
        sample["delegation_receipt_id"],
        "preexecution delegation receipt",
    )
    task_copy = copy.deepcopy(task)
    try:
        attach_execution_mutation_provenance(task_copy, mutation)
        attach_natural_execution_attestation(task_copy, natural)
    except ValueError as exc:
        raise ScopedReadonlyCertificationError(str(exc)) from exc
    natural = validate_natural_execution_attestation(natural)
    mutation = validate_execution_mutation_provenance(mutation)
    receipt = validate_preexecution_delegation_receipt(receipt)
    contract = validate_execution_delegation_contract(
        task.get("execution_delegation_contract")
    )
    delegation = preexecution_delegation_view(task)
    if (
        delegation["status"] != "verified-local-preexecution-binding"
        or delegation.get("receipt_id") != receipt["receipt_id"]
    ):
        raise ScopedReadonlyCertificationError(
            "canonical preexecution delegation receipt is not verified"
        )
    natural_time = _timestamp(natural["recorded_at"], "natural.recorded_at")
    mutation_time = _timestamp(mutation["recorded_at"], "mutation.recorded_at")
    receipt_time = _timestamp(receipt["recorded_at"], "receipt.recorded_at")
    if any(item > natural_time for item in (mutation_time, receipt_time)):
        raise ScopedReadonlyCertificationError(
            "sample source chronology is invalid"
        )
    if natural_time > evaluated:
        raise ScopedReadonlyCertificationError("future natural sample")
    if natural_time + timedelta(days=REVALIDATION_DAYS) <= evaluated:
        raise ScopedReadonlyCertificationError("expired natural sample")

    binding = natural["binding"]
    mutation_binding = natural.get("mutation_binding")
    if (
        natural["contract"] != NATURAL_CONTRACT
        or natural["evidence"]["class"] != "natural-objective-run"
        or natural["evidence"]["scenario"] != "objective_outcome"
        or natural["boundary_event"] is not None
        or natural["evidence"]["mutation_provenance"] != "unknown"
        or natural["report_only"] is not True
        or natural["routing_mutation_allowed"] is not False
        or natural["promotion_authority"] is not False
        or not isinstance(mutation_binding, dict)
    ):
        raise ScopedReadonlyCertificationError(
            "natural sample is not a real scoped objective record"
        )
    if (
        mutation_binding["provenance_id"] != mutation["provenance_id"]
        or mutation_binding["source_digest"] != _stable_id(mutation)
        or mutation_binding["scope"] != SCOPE
        or mutation_binding["scoped_provenance"] != "no_mutation"
        or mutation_binding["global_provenance"] != "unknown"
        or mutation_binding["worker_certification_projection_allowed"] is not False
    ):
        raise ScopedReadonlyCertificationError(
            "natural and scoped mutation binding mismatch"
        )
    if (
        mutation["contract"] != MUTATION_CONTRACT
        or mutation["scope"]["name"] != SCOPE
        or mutation["provenance"] != "no_mutation"
        or mutation["global_provenance"] != "unknown"
        or mutation["fail_closed_reasons"]
        or any(item is None for item in mutation["snapshot_ids"].values())
        or mutation["attribution"]["pre_existing_dirt"]
        or mutation["attribution"]["unsafe_or_unreported_paths"]
        or mutation["attribution"]["worker_created_commit"]
        or mutation["worker_certification_projection_allowed"] is not False
        or mutation["routing_mutation_allowed"] is not False
        or mutation["promotion_authority"] is not False
    ):
        raise ScopedReadonlyCertificationError(
            "scoped mutation record is policy-ineligible"
        )
    receipt_binding = receipt["binding"]
    receipt_target = receipt["target"]
    side_effects = contract["side_effect_boundary"]
    if (
        contract["binding"]["task_id"] != binding["task_id"]
        or contract["binding"]["task_class"] != TASK_CLASS
        or receipt_binding["task_id"] != binding["task_id"]
        or receipt_binding["attempt"] != binding["attempt"]
        or receipt_binding["delegation_contract_digest"]
        != contract["contract_digest"]
        or receipt_target["worker_family"] != binding["worker_family"]
        or receipt_target["worker_identity_digest"]
        != _stable_id({"worker_id": binding["worker_id"]})
        or receipt_target["target_id"] != binding["target_id"]
        or receipt_target["target_snapshot_digest"]
        != binding["target_snapshot_id"]
        or side_effects["cbr_controlled_repository_write_allowed"]
        or side_effects["external_state_mutation_allowed"]
        or side_effects["credential_access_allowed"]
        or side_effects["deployment_or_publication_allowed"]
        or side_effects["destructive_action_allowed"]
    ):
        raise ScopedReadonlyCertificationError(
            "delegation receipt does not exact-bind scoped readonly sample"
        )
    if (
        binding["worker_id"] != candidate["worker_id"]
        or binding["target_snapshot_id"] != candidate["target_snapshot_id"]
        or binding["task_class"] != TASK_CLASS
        or candidate["task_class"] != TASK_CLASS
        or mutation["binding"]["task_id"] != binding["task_id"]
        or mutation["binding"]["attempt"] != binding["attempt"]
        or mutation["binding"]["execution_evidence_id"]
        != binding["execution_evidence_id"]
    ):
        raise ScopedReadonlyCertificationError(
            "sample does not match candidate or execution binding"
        )
    review = natural["review"]
    adverse = int(
        natural["evidence"]["outcome"] != "pass"
        or review["accepted"] is not True
        or review["objective_status"] != "passed"
        or review["semantic_status"] != "pass"
    )
    cohort_components = {
        "worker_family": binding["worker_family"],
        "worker_id": binding["worker_id"],
        "target_id": binding["target_id"],
        "target_snapshot_id": binding["target_snapshot_id"],
        "task_class": binding["task_class"],
        "mapping_revision": binding["mapping_revision"],
        "resolved_config_digest": binding["resolved_config_digest"],
        "delegation_resolved_config_digest": receipt_target[
            "resolved_config_digest"
        ],
        "delegation_target_snapshot_digest": receipt_target[
            "target_snapshot_digest"
        ],
        "delegation_worker_identity_digest": receipt_target[
            "worker_identity_digest"
        ],
        "delegation_command_contract_digest": receipt_target[
            "command_contract_digest"
        ],
        "execution_cohort_id": binding["execution_cohort_id"],
        "review_policy_version": review["policy_version"],
        "review_rubric_version": review["rubric_version"],
        "review_acceptance_method": review["acceptance_method"],
        "reviewer_provenance_class": review["reviewer_provenance_class"],
        "attestor_revision": natural["attestor_revision"],
        "mutation_contract": mutation["contract"],
        "mutation_producer_revision": mutation["producer_revision"],
        "delegation_contract_revision": contract["binding"]["task_revision"],
        "delegation_authority_revision": contract["issuer"][
            "authority_revision"
        ],
        "delegation_policy_revision": contract["revisions"][
            "policy_revision"
        ],
        "delegation_execution_revision": contract["revisions"][
            "execution_revision"
        ],
        "delegation_review_revision": contract["revisions"][
            "review_revision"
        ],
        "delegation_receipt_producer_revision": receipt["producer"]["revision"],
        "policy_revision": POLICY_REVISION,
    }
    sample_id = _stable_id(
        {
            "natural_attestation_id": natural["attestation_id"],
            "mutation_provenance_id": mutation["provenance_id"],
            "delegation_receipt_id": receipt["receipt_id"],
        }
    )
    return {
        "sample_id": sample_id,
        "task_id": binding["task_id"],
        "attempt": binding["attempt"],
        "execution_evidence_id": binding["execution_evidence_id"],
        "cohort_id": _stable_id(cohort_components),
        "outcome": natural["evidence"]["outcome"],
        "adverse": adverse,
    }


def _candidate(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "worker_id",
        "target_snapshot_id",
        "task_class",
    }:
        raise ScopedReadonlyCertificationError(
            "candidate fields are not canonical"
        )
    if value["task_class"] != TASK_CLASS:
        raise ScopedReadonlyCertificationError(
            "candidate task_class must be readonly-objective"
        )
    return {
        "worker_id": _safe_id(value["worker_id"], "candidate.worker_id"),
        "target_snapshot_id": _safe_id(
            value["target_snapshot_id"], "candidate.target_snapshot_id"
        ),
        "task_class": TASK_CLASS,
    }


def _history_record(
    task: dict[str, Any],
    history_field: str,
    id_field: str,
    expected_id: object,
    label: str,
) -> dict[str, Any]:
    record_id = _safe_id(expected_id, f"{label}.id")
    history = task.get(history_field)
    if not isinstance(history, list):
        raise ScopedReadonlyCertificationError(
            f"{label} task-owned history is unavailable"
        )
    matches = [
        item
        for item in history
        if isinstance(item, dict) and item.get(id_field) == record_id
    ]
    if len(matches) != 1:
        raise ScopedReadonlyCertificationError(
            f"exactly one task-owned {label} record is required"
        )
    return matches[0]


def _privacy() -> dict[str, bool]:
    return {
        "raw_paths_included": False,
        "raw_prompt_included": False,
        "raw_transcript_included": False,
        "session_or_thread_ids_included": False,
        "credentials_included": False,
        "private_identity_included": False,
    }


def _validate_privacy(value: dict[str, Any]) -> None:
    for key in FORBIDDEN_KEYS:
        if _contains_key(value, key):
            raise ScopedReadonlyCertificationError(
                f"scoped readonly evidence contains forbidden key: {key}"
            )
    if value.get("privacy") != _privacy():
        raise ScopedReadonlyCertificationError(
            "scoped readonly privacy flags must all be false"
        )


def _validate_digest(value: dict[str, Any], field: str) -> None:
    claimed = value.get(field)
    body = dict(value)
    body.pop(field)
    if claimed != _stable_id(body):
        raise ScopedReadonlyCertificationError(f"{field} digest mismatch")


def _stable_id(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _safe_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ScopedReadonlyCertificationError(
            f"{field} is not a safe identifier"
        )
    return value


def _safe_reason(value: str) -> str:
    normalized = "_".join(value.lower().split())
    allowed = "".join(
        char for char in normalized if char.isalnum() or char in "._:-"
    )
    return allowed[:160] or "invalid_sample"


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScopedReadonlyCertificationError(
            f"{field} must be a non-negative integer"
        )
    return value


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ScopedReadonlyCertificationError(
            f"{field} must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ScopedReadonlyCertificationError(
            f"{field} must be an ISO timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScopedReadonlyCertificationError(
            f"{field} must be an ISO timestamp"
        ) from exc
    return _aware_utc(parsed, field)


def _contains_key(value: object, needle: str) -> bool:
    if isinstance(value, dict):
        return any(
            key == needle or _contains_key(item, needle)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_key(item, needle) for item in value)
    return False

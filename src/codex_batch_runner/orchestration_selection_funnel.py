"""Read-only projection of trusted orchestration selection funnel evidence."""

from __future__ import annotations

import json
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .fs import read_json
from .orchestration import SURFACES, build_orchestration_plan, validate_manifest
from .orchestration_dispatch import (
    _receipt_matches,
    _task_matches,
    config_independent_gate_reason_codes,
    identity_for,
    request_binding_reason_codes,
    validate_execution_envelope,
)
from .orchestration_selection import (
    validate_source_bound_selection_receipt,
    stable_digest,
)
from .parent_attention import WAKE_REASONS, stable_event_id
from .queue import task_path
from .worktree import verify_applied_cleanup_target


CONTRACT = "orchestration-selection-funnel-projection-v1"
STAGE_ORDER = (
    "durable_eligible",
    "planned",
    "selected",
    "admitted",
    "completed",
    "accepted",
    "applied",
    "parent_attention_recorded",
)
STATUSES = frozenset({"observed", "not_observed", "unknown", "not_applicable"})
DOWNSTREAM_STAGES = STAGE_ORDER[3:]
MAX_EVIDENCE_BYTES = 512 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
PARENT_EVENT_ID = re.compile(r"^pa-[0-9a-f]{32}$")
DISPATCH_REASON_CODES = frozenset(
    {
        "trusted_dispatch_observed",
        "trusted_dispatch_not_observed",
        "trusted_dispatch_conflict",
        "dispatch_source_binding_invalid",
    }
)
STAGE_REASON_CODES = frozenset(
    {
        "eligibility_not_evaluated",
        "durable_eligible_observed",
        "durable_eligible_not_observed",
        "recommended_surface_match",
        "recommended_surface_not_match",
        "selection_invalid",
        "selection_not_recorded",
        "selected_surface_match",
        "selected_surface_not_match",
        "trusted_adapter_unavailable",
        "selected_cbr_prerequisite_not_observed",
        "trusted_dispatch_observed",
        "trusted_dispatch_not_observed",
        "trusted_dispatch_conflict",
        "dispatch_source_binding_invalid",
        "completed_prerequisite_not_observed",
        "task_not_completed",
        "task_completion_evidence_invalid",
        "task_completion_chronology_invalid",
        "task_completed",
        "accepted_prerequisite_not_observed",
        "task_not_accepted",
        "task_acceptance_evidence_invalid",
        "task_acceptance_chronology_invalid",
        "task_accepted",
        "worktree_apply_not_applicable",
        "worktree_not_applied",
        "worktree_apply_evidence_invalid",
        "worktree_apply_chronology_invalid",
        "worktree_apply_ancestry_unverified",
        "worktree_applied",
        "execution_mode_invalid",
        "apply_stage_not_applicable",
        "applied_prerequisite_not_observed",
        "parent_attention_recorded",
        "parent_attention_evidence_invalid",
        "parent_attention_not_observed",
    }
)
REASON_STATUSES = {
    "eligibility_not_evaluated": {"unknown"},
    "durable_eligible_observed": {"observed"},
    "durable_eligible_not_observed": {"not_observed"},
    "recommended_surface_match": {"observed"},
    "recommended_surface_not_match": {"not_observed"},
    "selection_invalid": {"unknown"},
    "selection_not_recorded": {"not_observed"},
    "selected_surface_match": {"observed"},
    "selected_surface_not_match": {"not_observed"},
    "trusted_adapter_unavailable": {"unknown"},
    "selected_cbr_prerequisite_not_observed": {"not_observed"},
    "trusted_dispatch_observed": {"observed"},
    "trusted_dispatch_not_observed": {"not_observed"},
    "trusted_dispatch_conflict": {"unknown"},
    "dispatch_source_binding_invalid": {"unknown"},
    "completed_prerequisite_not_observed": {"not_observed", "unknown"},
    "task_not_completed": {"not_observed"},
    "task_completion_evidence_invalid": {"unknown"},
    "task_completion_chronology_invalid": {"unknown"},
    "task_completed": {"observed"},
    "accepted_prerequisite_not_observed": {"not_observed", "unknown"},
    "task_not_accepted": {"not_observed"},
    "task_acceptance_evidence_invalid": {"unknown"},
    "task_acceptance_chronology_invalid": {"unknown"},
    "task_accepted": {"observed"},
    "worktree_apply_not_applicable": {"not_applicable"},
    "worktree_not_applied": {"not_observed"},
    "worktree_apply_evidence_invalid": {"unknown"},
    "worktree_apply_chronology_invalid": {"unknown"},
    "worktree_apply_ancestry_unverified": {"unknown"},
    "worktree_applied": {"observed"},
    "execution_mode_invalid": {"unknown"},
    "apply_stage_not_applicable": {"unknown"},
    "applied_prerequisite_not_observed": {"not_observed", "unknown"},
    "parent_attention_recorded": {"observed"},
    "parent_attention_evidence_invalid": {"unknown"},
    "parent_attention_not_observed": {"not_observed"},
}
TRUST_BOUNDARY_REASONS = {
    "trusted_adapter_unavailable",
    "selected_cbr_prerequisite_not_observed",
    "trusted_dispatch_not_observed",
    "trusted_dispatch_conflict",
    "dispatch_source_binding_invalid",
}
STAGE_REASONS = {
    "durable_eligible": {
        "eligibility_not_evaluated",
        "durable_eligible_observed",
        "durable_eligible_not_observed",
    },
    "planned": {
        "recommended_surface_match",
        "recommended_surface_not_match",
    },
    "selected": {
        "selection_invalid",
        "selection_not_recorded",
        "selected_surface_match",
        "selected_surface_not_match",
    },
    "admitted": TRUST_BOUNDARY_REASONS | {"trusted_dispatch_observed"},
    "completed": TRUST_BOUNDARY_REASONS
    | {
        "task_not_completed",
        "task_completion_evidence_invalid",
        "task_completion_chronology_invalid",
        "task_completed",
    },
    "accepted": TRUST_BOUNDARY_REASONS
    | {
        "completed_prerequisite_not_observed",
        "task_not_accepted",
        "task_acceptance_evidence_invalid",
        "task_acceptance_chronology_invalid",
        "task_accepted",
    },
    "applied": TRUST_BOUNDARY_REASONS
    | {
        "completed_prerequisite_not_observed",
        "accepted_prerequisite_not_observed",
        "worktree_apply_not_applicable",
        "worktree_not_applied",
        "worktree_apply_evidence_invalid",
        "worktree_apply_chronology_invalid",
        "worktree_apply_ancestry_unverified",
        "worktree_applied",
        "execution_mode_invalid",
    },
    "parent_attention_recorded": TRUST_BOUNDARY_REASONS
    | {
        "completed_prerequisite_not_observed",
        "accepted_prerequisite_not_observed",
        "apply_stage_not_applicable",
        "applied_prerequisite_not_observed",
        "parent_attention_recorded",
        "parent_attention_evidence_invalid",
        "parent_attention_not_observed",
    },
}


class SelectionFunnelError(ValueError):
    pass


def build_selection_funnel(
    config: Config,
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    selection_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Project current trusted durable state without locks or writes."""
    canonical_manifest = validate_manifest(manifest)
    canonical_envelope = validate_execution_envelope(envelope)
    receipt = validate_source_bound_selection_receipt(
        selection_receipt, canonical_manifest
    )
    decision = receipt["decision"]
    plan = build_orchestration_plan(canonical_manifest)
    if canonical_envelope["request_fingerprint"] != plan["request_fingerprint"]:
        raise SelectionFunnelError("execution envelope request binding mismatch")

    rows: list[dict[str, Any]] = []
    snapshot = {
        row["surface"]: row for row in decision["eligibility_snapshot"]
    }
    downstream_reason: list[str] = []
    join: tuple[dict[str, Any] | None, str] = (None, "not_observed")
    cbr_selected = (
        decision["decision_status"] == "recorded"
        and decision["selected_surface"] == "cbr_batch"
        and decision["would_warn"] is False
    )
    if cbr_selected:
        join = _trusted_cbr_join(
            config,
            canonical_manifest,
            canonical_envelope,
            plan,
        )
        downstream_reason.append(
            {
                "observed": "trusted_dispatch_observed",
                "not_observed": "trusted_dispatch_not_observed",
                "conflict": "trusted_dispatch_conflict",
                "invalid": "dispatch_source_binding_invalid",
            }[join[1]]
        )

    for surface in decision["candidates"]:
        eligibility = snapshot[surface]
        stages = {
            "durable_eligible": _eligibility_stage(eligibility),
            "planned": _binary_stage(
                surface == decision["recommended_surface"],
                "recommended_surface_match",
                "recommended_surface_not_match",
            ),
            "selected": _selection_stage(decision, surface),
        }
        if surface != "cbr_batch":
            stages.update(
                {
                    stage: _stage("unknown", "trusted_adapter_unavailable")
                    for stage in DOWNSTREAM_STAGES
                }
            )
        elif not cbr_selected:
            stages.update(
                {
                    stage: _stage(
                        "not_observed",
                        "selected_cbr_prerequisite_not_observed",
                    )
                    for stage in DOWNSTREAM_STAGES
                }
            )
        else:
            stages.update(
                _cbr_downstream_stages(config, join[0], join[1])
            )
        rows.append({"surface": surface, "stages": stages})

    body = {
        "schema_version": 1,
        "contract": CONTRACT,
        "selection_decision_id": decision["decision_id"],
        "source_contract_digest": decision["source_contract_digest"],
        "request_fingerprint": decision["request_fingerprint"],
        "policy_revision": decision["policy_revision"],
        "stage_order": list(STAGE_ORDER),
        "surface_rows": rows,
        "reason_codes": downstream_reason,
        "semantic_non_claims": {
            "parent_attention_recorded_is_parent_collected": False,
            "parent_attention_recorded_is_root_complete": False,
            "routing_authority": False,
        },
        "mutation": {"allowed": False, "applied": False},
    }
    body["report_digest"] = stable_digest(body)
    return validate_selection_funnel(body)


def validate_selection_funnel(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SelectionFunnelError("funnel report must be an object")
    expected = {
        "schema_version",
        "contract",
        "selection_decision_id",
        "source_contract_digest",
        "request_fingerprint",
        "policy_revision",
        "stage_order",
        "surface_rows",
        "reason_codes",
        "semantic_non_claims",
        "mutation",
        "report_digest",
    }
    if set(value) != expected:
        raise SelectionFunnelError("funnel report fields are invalid")
    if value["schema_version"] != 1 or value["contract"] != CONTRACT:
        raise SelectionFunnelError("funnel report contract is invalid")
    for key in (
        "selection_decision_id",
        "source_contract_digest",
        "request_fingerprint",
    ):
        _digest(key, value[key])
    _safe_id("policy_revision", value["policy_revision"])
    if value["stage_order"] != list(STAGE_ORDER):
        raise SelectionFunnelError("funnel stage order is invalid")
    rows = value["surface_rows"]
    if not isinstance(rows, list) or not rows:
        raise SelectionFunnelError("surface rows must be non-empty")
    surfaces: list[str] = []
    canonical_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"surface", "stages"}:
            raise SelectionFunnelError("surface row fields are invalid")
        surface = row["surface"]
        if surface not in SURFACES or surface in surfaces:
            raise SelectionFunnelError("surface rows are invalid")
        stages = row["stages"]
        if not isinstance(stages, dict) or list(stages) != list(STAGE_ORDER):
            raise SelectionFunnelError("surface stages are invalid")
        canonical_stages = {
            stage: _validate_stage(stage, stages[stage])
            for stage in STAGE_ORDER
        }
        surfaces.append(surface)
        canonical_rows.append(
            {"surface": surface, "stages": canonical_stages}
        )
        _validate_row_semantics(surface, canonical_stages)
    reasons = value["reason_codes"]
    if (
        not isinstance(reasons, list)
        or len(reasons) != len(set(reasons))
        or not set(reasons) <= DISPATCH_REASON_CODES
    ):
        raise SelectionFunnelError("funnel reason codes are invalid")
    selected_rows = [
        row
        for row in canonical_rows
        if row["stages"]["selected"]["status"] == "observed"
    ]
    planned_rows = [
        row
        for row in canonical_rows
        if row["stages"]["planned"]["status"] == "observed"
    ]
    if len(selected_rows) > 1 or len(planned_rows) > 1:
        raise SelectionFunnelError("funnel surface cardinality is invalid")
    cbr_selected = any(
        row["surface"] == "cbr_batch" for row in selected_rows
    )
    if cbr_selected:
        cbr_row = next(
            row for row in canonical_rows if row["surface"] == "cbr_batch"
        )
        admitted_reason = cbr_row["stages"]["admitted"]["reason_codes"][0]
        if reasons != [admitted_reason] or admitted_reason not in DISPATCH_REASON_CODES:
            raise SelectionFunnelError("funnel dispatch reason is inconsistent")
    elif reasons:
        raise SelectionFunnelError("funnel dispatch reason is unexpected")
    non_claims = value["semantic_non_claims"]
    expected_non_claims = {
        "parent_attention_recorded_is_parent_collected": False,
        "parent_attention_recorded_is_root_complete": False,
        "routing_authority": False,
    }
    if non_claims != expected_non_claims:
        raise SelectionFunnelError("semantic non-claims are invalid")
    mutation = value["mutation"]
    if mutation != {"allowed": False, "applied": False}:
        raise SelectionFunnelError("funnel mutation boundary is invalid")
    canonical = {
        "schema_version": 1,
        "contract": CONTRACT,
        "selection_decision_id": value["selection_decision_id"],
        "source_contract_digest": value["source_contract_digest"],
        "request_fingerprint": value["request_fingerprint"],
        "policy_revision": value["policy_revision"],
        "stage_order": list(STAGE_ORDER),
        "surface_rows": canonical_rows,
        "reason_codes": reasons,
        "semantic_non_claims": expected_non_claims,
        "mutation": {"allowed": False, "applied": False},
    }
    if value["report_digest"] != stable_digest(canonical):
        raise SelectionFunnelError("funnel report digest is invalid")
    canonical["report_digest"] = value["report_digest"]
    _validate_public_safe(canonical)
    return canonical


def _trusted_cbr_join(
    config: Config,
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    identity = identity_for(manifest, envelope)
    if request_binding_reason_codes(
        manifest, envelope, plan["request_fingerprint"]
    ) or config_independent_gate_reason_codes(manifest, plan):
        return None, "invalid"
    receipt_path = (
        config.log_dir.parent
        / "orchestration-dispatch-receipts"
        / f"{identity['dispatch_id']}.json"
    )
    task_file = task_path(config, identity["task_id"])
    receipt_value, receipt_state = _read_evidence_object(receipt_path)
    task_value, task_state = _read_evidence_object(task_file)
    if receipt_state == "missing" and task_state == "missing":
        return None, "not_observed"
    if receipt_state != "ok" or task_state != "ok":
        return None, "conflict"
    if not _receipt_matches(receipt_value, plan, identity):
        return None, "conflict"
    if not _task_matches(task_value, envelope, plan, identity):
        return None, "conflict"
    return task_value, "observed"


def _cbr_downstream_stages(
    config: Config,
    task: dict[str, Any] | None,
    join_status: str,
) -> dict[str, dict[str, Any]]:
    if join_status == "not_observed":
        return {
            stage: _stage("not_observed", "trusted_dispatch_not_observed")
            for stage in DOWNSTREAM_STAGES
        }
    if join_status in {"conflict", "invalid"} or task is None:
        reason = (
            "trusted_dispatch_conflict"
            if join_status == "conflict"
            else "dispatch_source_binding_invalid"
        )
        return {
            stage: _stage("unknown", reason) for stage in DOWNSTREAM_STAGES
        }
    stages: dict[str, dict[str, Any]] = {
        "admitted": _stage("observed", "trusted_dispatch_observed")
    }
    completed = _completed_stage(task)
    stages["completed"] = completed
    if completed["status"] != "observed":
        later_status = (
            "unknown" if completed["status"] == "unknown" else "not_observed"
        )
        for stage in STAGE_ORDER[5:]:
            stages[stage] = _stage(
                later_status, "completed_prerequisite_not_observed"
            )
        return stages
    accepted = _accepted_stage(task)
    stages["accepted"] = accepted
    if accepted["status"] != "observed":
        later_status = (
            "unknown" if accepted["status"] == "unknown" else "not_observed"
        )
        for stage in STAGE_ORDER[6:]:
            stages[stage] = _stage(
                later_status, "accepted_prerequisite_not_observed"
            )
        return stages
    applied = _applied_stage(task)
    stages["applied"] = applied
    if applied["status"] == "observed":
        stages["parent_attention_recorded"] = _parent_attention_stage(
            config, task
        )
    elif applied["status"] == "not_applicable":
        stages["parent_attention_recorded"] = _stage(
            "unknown", "apply_stage_not_applicable"
        )
    else:
        later_status = (
            "unknown" if applied["status"] == "unknown" else "not_observed"
        )
        stages["parent_attention_recorded"] = _stage(
            later_status, "applied_prerequisite_not_observed"
        )
    return stages


def _eligibility_stage(row: dict[str, Any]) -> dict[str, Any]:
    if row["evaluated"] is False:
        return _stage("unknown", "eligibility_not_evaluated")
    if row["eligible"] is True:
        return _stage("observed", "durable_eligible_observed")
    return _stage("not_observed", "durable_eligible_not_observed")


def _selection_stage(
    decision: dict[str, Any], surface: str
) -> dict[str, Any]:
    if decision["decision_status"] == "invalid":
        return _stage("unknown", "selection_invalid")
    if decision["decision_status"] != "recorded":
        return _stage("not_observed", "selection_not_recorded")
    return _binary_stage(
        decision["selected_surface"] == surface,
        "selected_surface_match",
        "selected_surface_not_match",
    )


def _completed_stage(task: dict[str, Any]) -> dict[str, Any]:
    status = task.get("status")
    if status == "archived":
        gate = task.get("archive_gate_result")
        if (
            task.get("previous_status") != "completed"
            or not isinstance(gate, dict)
            or set(gate)
            != {"status", "checked_at", "blockers", "warnings"}
            or gate.get("status") != "passed"
            or gate.get("blockers") != []
            or not isinstance(gate.get("warnings"), list)
            or not all(
                isinstance(item, str) for item in gate.get("warnings", [])
            )
            or not _valid_timestamp(gate.get("checked_at"))
            or not _valid_timestamp(task.get("archived_at"))
        ):
            return _stage("unknown", "task_completion_evidence_invalid")
    elif status != "completed":
        return _stage("not_observed", "task_not_completed")
    result = task.get("last_result")
    if (
        not isinstance(result, dict)
        or result.get("status") != "completed"
        or not _valid_timestamp(task.get("completed_at"))
    ):
        return _stage("unknown", "task_completion_evidence_invalid")
    if status == "archived" and not _times_monotonic(
        task.get("completed_at"), task.get("archived_at")
    ):
        return _stage("unknown", "task_completion_chronology_invalid")
    return _stage("observed", "task_completed")


def _accepted_stage(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("review_status") != "accepted":
        return _stage("not_observed", "task_not_accepted")
    if not _valid_timestamp(task.get("reviewed_at")):
        return _stage("unknown", "task_acceptance_evidence_invalid")
    if not _times_monotonic(
        task.get("completed_at"), task.get("reviewed_at")
    ):
        return _stage("unknown", "task_acceptance_chronology_invalid")
    if task.get("status") == "archived" and not _times_monotonic(
        task.get("reviewed_at"), task.get("archived_at")
    ):
        return _stage("unknown", "task_acceptance_chronology_invalid")
    return _stage("observed", "task_accepted")


def _applied_stage(task: dict[str, Any]) -> dict[str, Any]:
    mode = task.get("execution_mode")
    if mode is None or mode == "main_worktree":
        return _stage("not_applicable", "worktree_apply_not_applicable")
    if mode != "git_worktree":
        return _stage("unknown", "execution_mode_invalid")
    if task.get("execution_apply_status") != "applied":
        return _stage("not_observed", "worktree_not_applied")
    if (
        not _valid_timestamp(task.get("execution_applied_at"))
        or not isinstance(task.get("execution_applied_head"), str)
        or not isinstance(task.get("execution_apply_target"), str)
        or not isinstance(task.get("execution_repo_root"), str)
    ):
        return _stage("unknown", "worktree_apply_evidence_invalid")
    if not _times_monotonic(
        task.get("reviewed_at"), task.get("execution_applied_at")
    ):
        return _stage("unknown", "worktree_apply_chronology_invalid")
    if task.get("status") == "archived" and not _times_monotonic(
        task.get("execution_applied_at"), task.get("archived_at")
    ):
        return _stage("unknown", "worktree_apply_chronology_invalid")
    verification = verify_applied_cleanup_target(
        task, Path(task["execution_repo_root"])
    )
    if verification.get("status") != "current":
        return _stage("unknown", "worktree_apply_ancestry_unverified")
    return _stage("observed", "worktree_applied")


def _parent_attention_stage(
    config: Config, task: dict[str, Any]
) -> dict[str, Any]:
    outbox = config.parent_attention_outbox_dir or (
        config.log_dir.parent / "parent-attention-outbox"
    )
    if not outbox.exists():
        return _stage("not_observed", "parent_attention_not_observed")
    found = False
    malformed_match = False
    completion_id = task.get("completed_at")
    parent_ref = task.get("origin_parent_ref")
    task_id = task.get("id")
    if (
        not isinstance(parent_ref, str)
        or not isinstance(task_id, str)
        or not isinstance(completion_id, str)
    ):
        return _stage("unknown", "parent_attention_evidence_invalid")
    for wake_reason in sorted(WAKE_REASONS):
        event_id = stable_event_id(
            parent_ref, task_id, completion_id, wake_reason
        )
        value, state = _read_evidence_object(outbox / f"{event_id}.json")
        if state == "missing":
            continue
        if (
            state == "ok"
            and isinstance(value, dict)
            and _valid_parent_attention(
                value,
                task,
                expected_event_id=event_id,
                expected_completion_id=completion_id,
                expected_wake_reason=wake_reason,
            )
        ):
            found = True
        else:
            malformed_match = True
    if found:
        return _stage("observed", "parent_attention_recorded")
    if malformed_match:
        return _stage("unknown", "parent_attention_evidence_invalid")
    return _stage("not_observed", "parent_attention_not_observed")


def _valid_parent_attention(
    value: dict[str, Any],
    task: dict[str, Any],
    *,
    expected_event_id: str,
    expected_completion_id: str,
    expected_wake_reason: str,
) -> bool:
    expected = {
        "schema_version",
        "event_type",
        "event_id",
        "parent_ref",
        "work_item_ref",
        "completion_id",
        "wake_reason",
        "result",
        "delivery",
        "created_at",
        "updated_at",
    }
    if set(value) != expected:
        return False
    if (
        value.get("schema_version") != 1
        or value.get("event_type") != "parent_attention_required"
        or value.get("work_item_ref") != task.get("id")
        or value.get("parent_ref") != task.get("origin_parent_ref")
        or value.get("event_id") != expected_event_id
        or value.get("completion_id") != expected_completion_id
        or value.get("wake_reason") != expected_wake_reason
        or not _valid_attention_result(value.get("result"))
        or not _valid_attention_delivery(value.get("delivery"))
        or not _valid_timestamp(value.get("created_at"))
        or not _valid_timestamp(value.get("updated_at"))
        or not _times_monotonic(
            task.get("completed_at"), value.get("created_at")
        )
        or not _times_monotonic(
            value.get("created_at"), value.get("updated_at")
        )
    ):
        return False
    event_id = value.get("event_id")
    if not isinstance(event_id, str) or not PARENT_EVENT_ID.fullmatch(event_id):
        return False
    return event_id == expected_event_id == stable_event_id(
        str(value["parent_ref"]),
        str(value["work_item_ref"]),
        str(value["completion_id"]),
        str(value["wake_reason"]),
    )


def _valid_attention_result(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"summary", "evidence_refs"}
        and isinstance(value.get("summary"), str)
        and isinstance(value.get("evidence_refs"), list)
        and all(isinstance(item, str) for item in value["evidence_refs"])
    )


def _valid_attention_delivery(value: object) -> bool:
    expected = {
        "state",
        "attempts",
        "max_attempts",
        "next_attempt_at",
        "last_attempt_at",
        "last_error",
        "delivered_at",
        "acknowledged_at",
    }
    if not isinstance(value, dict) or set(value) != expected:
        return False
    if (
        value.get("state")
        not in {
            "pending",
            "retry_wait",
            "delivered",
            "acknowledged",
            "unavailable",
            "failed",
        }
        or type(value.get("attempts")) is not int
        or type(value.get("max_attempts")) is not int
        or value["attempts"] < 0
        or value["max_attempts"] < 1
        or value["attempts"] > value["max_attempts"]
    ):
        return False
    for key in (
        "next_attempt_at",
        "last_attempt_at",
        "delivered_at",
        "acknowledged_at",
    ):
        item = value.get(key)
        if item is not None and not _valid_timestamp(item):
            return False
    error = value.get("last_error")
    if error is not None and not isinstance(error, str):
        return False
    state = value["state"]
    attempts = value["attempts"]
    next_at = value["next_attempt_at"]
    last_at = value["last_attempt_at"]
    delivered_at = value["delivered_at"]
    acknowledged_at = value["acknowledged_at"]
    if state == "pending":
        return (
            attempts == 0
            and next_at is not None
            and last_at is None
            and error is None
            and delivered_at is None
            and acknowledged_at is None
        )
    if state == "retry_wait":
        return (
            0 < attempts < value["max_attempts"]
            and next_at is not None
            and last_at is not None
            and error is not None
            and delivered_at is None
            and acknowledged_at is None
        )
    if state == "delivered":
        return (
            attempts > 0
            and next_at is None
            and last_at is not None
            and error is None
            and delivered_at is not None
            and acknowledged_at is None
            and _times_monotonic(last_at, delivered_at)
        )
    if state == "acknowledged":
        return (
            attempts > 0
            and next_at is None
            and last_at is not None
            and error is None
            and delivered_at is not None
            and acknowledged_at is not None
            and _times_monotonic(last_at, delivered_at, acknowledged_at)
        )
    if state == "unavailable":
        return (
            next_at is None
            and error is not None
            and delivered_at is None
            and acknowledged_at is None
        )
    return (
        state == "failed"
        and attempts == value["max_attempts"]
        and next_at is None
        and last_at is not None
        and error is not None
        and delivered_at is None
        and acknowledged_at is None
    )


def _read_evidence_object(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, "missing"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, "invalid"
    if info.st_size > MAX_EVIDENCE_BYTES:
        return None, "invalid"
    try:
        value = read_json(path, None)
    except (OSError, json.JSONDecodeError):
        return None, "invalid"
    return (value, "ok") if isinstance(value, dict) else (None, "invalid")


def _binary_stage(
    matched: bool, observed_reason: str, missing_reason: str
) -> dict[str, Any]:
    return _stage(
        "observed" if matched else "not_observed",
        observed_reason if matched else missing_reason,
    )


def _stage(status: str, reason: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise SelectionFunnelError("stage status is invalid")
    return {"status": status, "reason_codes": [reason]}


def _validate_stage(stage: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "status",
        "reason_codes",
    }:
        raise SelectionFunnelError("stage fields are invalid")
    status = value["status"]
    reasons = value["reason_codes"]
    if (
        status not in STATUSES
        or not isinstance(reasons, list)
        or len(reasons) != 1
        or len(reasons) != len(set(reasons))
    ):
        raise SelectionFunnelError("stage value is invalid")
    for reason in reasons:
        _safe_id("stage.reason_code", reason)
    if not set(reasons) <= STAGE_REASON_CODES:
        raise SelectionFunnelError("stage reason code is invalid")
    if status not in REASON_STATUSES[reasons[0]]:
        raise SelectionFunnelError("stage status and reason are inconsistent")
    if reasons[0] not in STAGE_REASONS[stage]:
        raise SelectionFunnelError("stage reason is invalid for stage")
    return {"status": status, "reason_codes": reasons}


def _validate_row_semantics(
    surface: str, stages: dict[str, dict[str, Any]]
) -> None:
    if stages["selected"]["status"] == "observed" and stages[
        "durable_eligible"
    ]["status"] != "observed":
        raise SelectionFunnelError(
            "observed selection requires durable eligibility"
        )
    if surface != "cbr_batch":
        expected = _stage("unknown", "trusted_adapter_unavailable")
        if any(stages[name] != expected for name in DOWNSTREAM_STAGES):
            raise SelectionFunnelError(
                "non-CBR downstream evidence is not trusted"
            )
        return
    if stages["selected"]["status"] != "observed":
        expected = _stage(
            "not_observed", "selected_cbr_prerequisite_not_observed"
        )
        if any(stages[name] != expected for name in DOWNSTREAM_STAGES):
            raise SelectionFunnelError(
                "unselected CBR downstream evidence is invalid"
            )
        return
    admitted = stages["admitted"]
    if admitted["status"] != "observed":
        if any(stages[name] != admitted for name in STAGE_ORDER[4:]):
            raise SelectionFunnelError(
                "CBR dispatch prerequisite propagation is invalid"
            )
        return
    completed = stages["completed"]
    if completed["status"] != "observed":
        expected = _stage(
            "unknown" if completed["status"] == "unknown" else "not_observed",
            "completed_prerequisite_not_observed",
        )
        if any(stages[name] != expected for name in STAGE_ORDER[5:]):
            raise SelectionFunnelError(
                "CBR completion prerequisite propagation is invalid"
            )
        return
    accepted = stages["accepted"]
    if accepted["status"] != "observed":
        expected = _stage(
            "unknown" if accepted["status"] == "unknown" else "not_observed",
            "accepted_prerequisite_not_observed",
        )
        if any(stages[name] != expected for name in STAGE_ORDER[6:]):
            raise SelectionFunnelError(
                "CBR acceptance prerequisite propagation is invalid"
            )
        return
    applied = stages["applied"]
    parent = stages["parent_attention_recorded"]
    if applied["status"] == "not_applicable":
        expected = _stage("unknown", "apply_stage_not_applicable")
    elif applied["status"] == "observed":
        return
    else:
        expected = _stage(
            "unknown" if applied["status"] == "unknown" else "not_observed",
            "applied_prerequisite_not_observed",
        )
    if parent != expected:
        raise SelectionFunnelError(
            "CBR apply prerequisite propagation is invalid"
        )


def _safe_id(key: str, value: object) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise SelectionFunnelError(f"{key} is invalid")
    return value


def _digest(key: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
    ):
        raise SelectionFunnelError(f"{key} is invalid")
    return value


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _times_monotonic(*values: object) -> bool:
    if not all(_valid_timestamp(value) for value in values):
        return False
    parsed = [
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        for value in values
    ]
    return all(left <= right for left, right in zip(parsed, parsed[1:]))


def _validate_public_safe(value: object) -> None:
    forbidden = {
        "task_id",
        "dispatch_id",
        "request_id",
        "thread_id",
        "session_id",
        "user_id",
        "account_id",
        "parent_ref",
        "prompt",
        "transcript",
        "log",
        "path",
        "argv",
        "command",
        "credential",
        "credentials",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in forbidden:
                raise SelectionFunnelError(
                    "funnel report contains private identity or content"
                )
            _validate_public_safe(item)
    elif isinstance(value, list):
        for item in value:
            _validate_public_safe(item)
    elif isinstance(value, str) and (
        value.startswith("/") or value.startswith("~")
    ):
        raise SelectionFunnelError("funnel report contains a private path")

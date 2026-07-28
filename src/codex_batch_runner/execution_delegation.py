from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from .model_requirements import ResolvedExecutionConfig
from .timeutil import iso_now


DELEGATION_CONTRACT = "cbr-execution-delegation-contract-v1"
RECEIPT_CONTRACT = "cbr-preexecution-delegation-receipt-v1"
SCHEMA_VERSION = 1
CONTROL_PLANE = "local-cbr-queue-claim"
PRODUCER_REVISION = "cbr-runner-preexecution-delegation-v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
SIDE_EFFECT_FIELDS = {
    "cbr_controlled_repository_write_allowed",
    "external_state_mutation_allowed",
    "credential_access_allowed",
    "deployment_or_publication_allowed",
    "destructive_action_allowed",
}
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


class ExecutionDelegationError(ValueError):
    pass


def build_execution_delegation_contract(
    *,
    task_id: str,
    task_revision: str,
    task_class: str,
    issuer_source_kind: str,
    authority_revision: str,
    policy_revision: str,
    execution_revision: str,
    review_revision: str,
    side_effect_boundary: dict[str, bool],
) -> dict[str, Any]:
    record = {
        "schema_version": SCHEMA_VERSION,
        "contract": DELEGATION_CONTRACT,
        "kind": "execution_delegation_contract",
        "binding": {
            "task_id": _safe_id(task_id, "task_id"),
            "task_revision": _safe_id(task_revision, "task_revision"),
            "task_class": _safe_id(task_class, "task_class"),
        },
        "issuer": {
            "source_kind": _safe_id(issuer_source_kind, "issuer_source_kind"),
            "authority_revision": _safe_id(
                authority_revision, "authority_revision"
            ),
            "external_issuer_authenticated": False,
        },
        "revisions": {
            "policy_revision": _safe_id(policy_revision, "policy_revision"),
            "execution_revision": _safe_id(
                execution_revision, "execution_revision"
            ),
            "review_revision": _safe_id(review_revision, "review_revision"),
        },
        "side_effect_boundary": _side_effect_boundary(side_effect_boundary),
        "admission": {
            "control_plane": CONTROL_PLANE,
            "immutable_after_admission": True,
            "post_hoc_insertion_allowed": False,
            "historical_backfill_allowed": False,
        },
        "scope": {
            "local_preexecution_delegation_binding": True,
            "global_provenance": "unknown",
        },
        "report_only": True,
        "actual_canary": False,
        "promotion_authority": False,
        "routing_mutation_allowed": False,
    }
    record["contract_digest"] = _stable_id(record)
    return validate_execution_delegation_contract(record)


def validate_execution_delegation_contract(value: object) -> dict[str, Any]:
    expected = {
        "schema_version",
        "contract",
        "kind",
        "binding",
        "issuer",
        "revisions",
        "side_effect_boundary",
        "admission",
        "scope",
        "report_only",
        "actual_canary",
        "promotion_authority",
        "routing_mutation_allowed",
        "contract_digest",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ExecutionDelegationError("delegation contract fields are not canonical")
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["contract"] != DELEGATION_CONTRACT
        or value["kind"] != "execution_delegation_contract"
        or value["report_only"] is not True
        or value["actual_canary"] is not False
        or value["promotion_authority"] is not False
        or value["routing_mutation_allowed"] is not False
    ):
        raise ExecutionDelegationError("invalid delegation contract")
    binding = value["binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "task_id",
        "task_revision",
        "task_class",
    }:
        raise ExecutionDelegationError("invalid delegation binding")
    for key in ("task_id", "task_revision", "task_class"):
        _safe_id(binding.get(key), f"binding.{key}")
    issuer = value["issuer"]
    if not isinstance(issuer, dict) or set(issuer) != {
        "source_kind",
        "authority_revision",
        "external_issuer_authenticated",
    }:
        raise ExecutionDelegationError("invalid delegation issuer")
    _safe_id(issuer.get("source_kind"), "issuer.source_kind")
    _safe_id(issuer.get("authority_revision"), "issuer.authority_revision")
    if issuer["external_issuer_authenticated"] is not False:
        raise ExecutionDelegationError("external issuer authentication is not claimed")
    revisions = value["revisions"]
    if not isinstance(revisions, dict) or set(revisions) != {
        "policy_revision",
        "execution_revision",
        "review_revision",
    }:
        raise ExecutionDelegationError("invalid delegation revisions")
    for key in ("policy_revision", "execution_revision", "review_revision"):
        _safe_id(revisions.get(key), f"revisions.{key}")
    _side_effect_boundary(value["side_effect_boundary"])
    if value["admission"] != {
        "control_plane": CONTROL_PLANE,
        "immutable_after_admission": True,
        "post_hoc_insertion_allowed": False,
        "historical_backfill_allowed": False,
    }:
        raise ExecutionDelegationError("invalid delegation admission boundary")
    if value["scope"] != {
        "local_preexecution_delegation_binding": True,
        "global_provenance": "unknown",
    }:
        raise ExecutionDelegationError("invalid delegation scope")
    _validate_public_safe(value)
    _validate_digest(value, "contract_digest")
    return copy.deepcopy(value)


def admit_execution_delegation_contract(
    task_id: str, value: object | None
) -> dict[str, Any] | None:
    if value is None:
        return None
    validated = validate_execution_delegation_contract(value)
    if validated["binding"]["task_id"] != task_id:
        raise ExecutionDelegationError(
            "delegation contract task binding does not match task id"
        )
    return validated


def append_preexecution_delegation_receipt(
    task: dict[str, Any],
    *,
    execution_settings: ResolvedExecutionConfig | None,
    active_run_id: str,
) -> dict[str, Any] | None:
    raw_contract = task.get("execution_delegation_contract")
    if raw_contract is None:
        return None
    contract = validate_execution_delegation_contract(raw_contract)
    binding = contract["binding"]
    if binding["task_id"] != str(task.get("id")):
        raise ExecutionDelegationError(
            "delegation contract task binding does not match canonical task"
        )
    attempt = _positive_int(task.get("attempts"), "task.attempts")
    lease_sequence = _positive_int(task.get("run_count"), "task.run_count")
    _safe_id(active_run_id, "active_run_id")
    identity = resolved_execution_identity(task, execution_settings)
    snapshot = _target_snapshot(task, execution_settings)
    target = snapshot.get("target")
    if not isinstance(target, dict):
        raise ExecutionDelegationError("resolved target snapshot is missing target")
    target_id = identity["target_id"]
    worker_family = _safe_id(
        target.get("worker_family")
        or task.get("worker_family")
        or target.get("execution_surface"),
        "target.worker_family",
    )
    worker_id = _safe_id(
        target.get("worker_id") or task.get("worker_target") or target_id,
        "target.worker_id",
    )
    target_record = {
        "worker_family": worker_family,
        "worker_identity_digest": _stable_id({"worker_id": worker_id}),
        "target_id": target_id,
        "target_snapshot_digest": identity["resolved_target_digest"],
        "resolved_config_digest": identity["resolved_config_digest"],
        "command_contract_digest": identity["command_contract_digest"],
    }
    history = task.setdefault("preexecution_delegation_receipt_history", [])
    if not isinstance(history, list):
        raise ExecutionDelegationError("delegation receipt history must be a list")
    phases = task.setdefault("preexecution_delegation_phase_history", [])
    if not isinstance(phases, list):
        raise ExecutionDelegationError("delegation phase history must be a list")
    validated_history = [
        validate_preexecution_delegation_receipt(item) for item in history
    ]
    same_attempt = [
        item for item in validated_history if item["binding"]["attempt"] == attempt
    ]
    if same_attempt:
        if len(same_attempt) != 1:
            raise ExecutionDelegationError(
                "duplicate preexecution delegation receipts"
            )
        existing = same_attempt[0]
        if (
            existing["claim"]
            != {
                "claim_id": active_run_id,
                "lease_sequence": lease_sequence,
            }
            or existing["target"] != target_record
            or existing["binding"]["task_id"] != binding["task_id"]
            or existing["binding"]["task_revision"]
            != binding["task_revision"]
            or existing["binding"]["delegation_contract_digest"]
            != contract["contract_digest"]
            or existing["revisions"] != contract["revisions"]
        ):
            raise ExecutionDelegationError(
                "divergent preexecution delegation receipt"
            )
        return existing
    previous = validated_history[-1] if validated_history else None
    if previous is None:
        if attempt != 1:
            raise ExecutionDelegationError(
                "resume or retry requires prior delegation receipt"
            )
        predecessor = contract["contract_digest"]
    else:
        if previous["binding"]["attempt"] != attempt - 1:
            raise ExecutionDelegationError("delegation receipt attempt chain is broken")
        predecessor = previous["receipt_id"]
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "contract": RECEIPT_CONTRACT,
        "kind": "preexecution_delegation_receipt",
        "recorded_at": iso_now(),
        "binding": {
            "task_id": binding["task_id"],
            "task_revision": binding["task_revision"],
            "attempt": attempt,
            "delegation_contract_digest": contract["contract_digest"],
        },
        "target": target_record,
        "revisions": copy.deepcopy(contract["revisions"]),
        "claim": {
            "claim_id": active_run_id,
            "lease_sequence": lease_sequence,
        },
        "sequence": {
            "position": len(validated_history) + 1,
            "predecessor": predecessor,
        },
        "producer": {
            "kind": "cbr_runner",
            "revision": PRODUCER_REVISION,
        },
        "scope": {
            "control_plane": CONTROL_PLANE,
            "local_preexecution_delegation_binding": True,
            "external_issuer_authenticated": False,
            "global_provenance": "unknown",
        },
        "report_only": True,
        "actual_canary": False,
        "promotion_authority": False,
        "routing_mutation_allowed": False,
    }
    receipt["receipt_id"] = _receipt_id(receipt)
    validated = validate_preexecution_delegation_receipt(receipt)
    history.append(validated)
    _append_phase(
        phases,
        task_id=binding["task_id"],
        attempt=attempt,
        receipt_id=validated["receipt_id"],
        phase="preexecution_receipt_appended",
    )
    return validated


def resolved_execution_identity(
    task: dict[str, Any],
    execution_settings: ResolvedExecutionConfig | None,
) -> dict[str, Any]:
    """Return the receipt-owned launch identity without appending queue state."""

    snapshot = _target_snapshot(task, execution_settings)
    target = snapshot.get("target")
    if not isinstance(target, dict):
        raise ExecutionDelegationError("resolved target snapshot is missing target")
    target_id = _safe_id(snapshot.get("target_id"), "target_snapshot.target_id")
    nested_target_id = _safe_id(target.get("target_id"), "target.target_id")
    if nested_target_id != target_id:
        raise ExecutionDelegationError(
            "resolved target identifiers are divergent"
        )
    if (
        execution_settings is None
        or execution_settings.execution_target != target_id
    ):
        raise ExecutionDelegationError(
            "resolved execution target does not match target snapshot"
        )
    if task.get("worker_target") not in (None, target_id):
        raise ExecutionDelegationError(
            "resolved worker target does not match target snapshot"
        )
    snapshot_backend = target.get("execution_backend") or target.get(
        "execution_surface"
    )
    task_backend = str(task.get("execution_backend") or "codex")
    expected_backend = "codex" if snapshot_backend == "codex" else snapshot_backend
    if expected_backend not in (None, task_backend):
        raise ExecutionDelegationError(
            "resolved backend does not match execution target snapshot"
        )
    return {
        "target_id": target_id,
        "backend": task_backend,
        "resolved_target_digest": _stable_id(snapshot),
        "resolved_config_digest": _stable_id(
            _resolved_config(task, execution_settings)
        ),
        "command_contract_digest": _stable_id(_resolved_command(task)),
    }


def record_delegation_recovery(task: dict[str, Any]) -> None:
    if task.get("execution_delegation_contract") is None:
        return
    contract = validate_execution_delegation_contract(
        task["execution_delegation_contract"]
    )
    attempt = _positive_int(task.get("attempts"), "task.attempts")
    receipts = task.get("preexecution_delegation_receipt_history")
    phases = task.get("preexecution_delegation_phase_history")
    if not isinstance(receipts, list) or not isinstance(phases, list):
        raise ExecutionDelegationError("delegation recovery history is missing")
    current = [
        validate_preexecution_delegation_receipt(item)
        for item in receipts
        if isinstance(item, dict)
        and item.get("binding", {}).get("attempt") == attempt
    ]
    if len(current) != 1:
        raise ExecutionDelegationError(
            "stale delegated attempt requires exactly one receipt"
        )
    attempt_phases = [
        item
        for item in phases
        if isinstance(item, dict) and item.get("attempt") == attempt
    ]
    if len(attempt_phases) == 1 and attempt_phases[0].get(
        "phase"
    ) == "preexecution_receipt_appended":
        _append_phase(
            phases,
            task_id=contract["binding"]["task_id"],
            attempt=attempt,
            receipt_id=current[0]["receipt_id"],
            phase="attempt_recovered_before_pre_worker",
        )
    elif len(attempt_phases) != 2:
        raise ExecutionDelegationError("delegation recovery phase is divergent")


def require_preexecution_delegation_receipt(task: dict[str, Any]) -> None:
    if task.get("execution_delegation_contract") is None:
        return
    view = preexecution_delegation_view(task)
    if view["status"] != "verified-local-preexecution-binding":
        reasons = ", ".join(view.get("insufficiency_reasons") or ["unknown"])
        raise ExecutionDelegationError(
            f"worker invocation blocked by delegation receipt: {reasons}"
        )


def record_pre_worker_snapshot_phase(
    task: dict[str, Any], *, receipt_id: str, snapshot_id: str
) -> None:
    contract = validate_execution_delegation_contract(
        task.get("execution_delegation_contract")
    )
    phases = task.get("preexecution_delegation_phase_history")
    if not isinstance(phases, list):
        raise ExecutionDelegationError("delegation phase history must be a list")
    _append_phase(
        phases,
        task_id=contract["binding"]["task_id"],
        attempt=_positive_int(task.get("attempts"), "task.attempts"),
        receipt_id=_digest(receipt_id, "receipt_id"),
        phase="pre_worker_snapshot_recorded",
        snapshot_id=_digest(snapshot_id, "snapshot_id"),
    )


def validate_preexecution_delegation_receipt(value: object) -> dict[str, Any]:
    expected = {
        "schema_version",
        "contract",
        "kind",
        "recorded_at",
        "binding",
        "target",
        "revisions",
        "claim",
        "sequence",
        "producer",
        "scope",
        "report_only",
        "actual_canary",
        "promotion_authority",
        "routing_mutation_allowed",
        "receipt_id",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ExecutionDelegationError("delegation receipt fields are not canonical")
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["contract"] != RECEIPT_CONTRACT
        or value["kind"] != "preexecution_delegation_receipt"
        or value["report_only"] is not True
        or value["actual_canary"] is not False
        or value["promotion_authority"] is not False
        or value["routing_mutation_allowed"] is not False
    ):
        raise ExecutionDelegationError("invalid delegation receipt")
    recorded_at = _timestamp(value["recorded_at"], "recorded_at")
    if recorded_at > datetime.now(timezone.utc):
        raise ExecutionDelegationError("delegation receipt timestamp is in the future")
    binding = value["binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "task_id",
        "task_revision",
        "attempt",
        "delegation_contract_digest",
    }:
        raise ExecutionDelegationError("invalid receipt binding")
    _safe_id(binding.get("task_id"), "binding.task_id")
    _safe_id(binding.get("task_revision"), "binding.task_revision")
    _positive_int(binding.get("attempt"), "binding.attempt")
    _digest(
        binding.get("delegation_contract_digest"),
        "binding.delegation_contract_digest",
    )
    target = value["target"]
    if not isinstance(target, dict) or set(target) != {
        "worker_family",
        "worker_identity_digest",
        "target_id",
        "target_snapshot_digest",
        "resolved_config_digest",
        "command_contract_digest",
    }:
        raise ExecutionDelegationError("invalid receipt target")
    for key in ("worker_family", "target_id"):
        _safe_id(target.get(key), f"target.{key}")
    for key in (
        "worker_identity_digest",
        "target_snapshot_digest",
        "resolved_config_digest",
        "command_contract_digest",
    ):
        _digest(target.get(key), f"target.{key}")
    revisions = value["revisions"]
    if not isinstance(revisions, dict) or set(revisions) != {
        "policy_revision",
        "execution_revision",
        "review_revision",
    }:
        raise ExecutionDelegationError("invalid receipt revisions")
    for key in revisions:
        _safe_id(revisions.get(key), f"revisions.{key}")
    claim = value["claim"]
    if not isinstance(claim, dict) or set(claim) != {
        "claim_id",
        "lease_sequence",
    }:
        raise ExecutionDelegationError("invalid receipt claim")
    _safe_id(claim.get("claim_id"), "claim.claim_id")
    _positive_int(claim.get("lease_sequence"), "claim.lease_sequence")
    sequence = value["sequence"]
    if not isinstance(sequence, dict) or set(sequence) != {
        "position",
        "predecessor",
    }:
        raise ExecutionDelegationError("invalid receipt sequence")
    _positive_int(sequence.get("position"), "sequence.position")
    _digest(sequence.get("predecessor"), "sequence.predecessor")
    if value["producer"] != {
        "kind": "cbr_runner",
        "revision": PRODUCER_REVISION,
    }:
        raise ExecutionDelegationError("invalid receipt producer")
    if value["scope"] != {
        "control_plane": CONTROL_PLANE,
        "local_preexecution_delegation_binding": True,
        "external_issuer_authenticated": False,
        "global_provenance": "unknown",
    }:
        raise ExecutionDelegationError("invalid receipt scope")
    _validate_public_safe(value)
    if value["receipt_id"] != _receipt_id(value):
        raise ExecutionDelegationError("receipt_id does not match canonical receipt")
    return copy.deepcopy(value)


def preexecution_delegation_view(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("execution_delegation_contract") is None:
        return _unknown_view("missing_delegation_contract")
    try:
        contract = validate_execution_delegation_contract(
            task["execution_delegation_contract"]
        )
        receipts = _validate_canonical_history(task, contract)
        attempt = task.get("attempts")
        current = [
            item for item in receipts if item["binding"]["attempt"] == attempt
        ]
        if len(current) != 1:
            raise ExecutionDelegationError(
                "current attempt requires exactly one delegation receipt"
            )
    except (ExecutionDelegationError, ValueError) as exc:
        return _unknown_view(_reason(str(exc)))
    receipt = current[0]
    if task.get("active_run_id") is not None and receipt["claim"][
        "claim_id"
    ] != task.get("active_run_id"):
        return _unknown_view("current_claim_identity_mismatch")
    if task.get("active_run_id") is not None and receipt["claim"][
        "lease_sequence"
    ] != task.get("run_count"):
        return _unknown_view("current_lease_sequence_mismatch")
    if task.get("worker_target") not in (
        None,
        receipt["target"]["target_id"],
    ):
        return _unknown_view("current_worker_target_mismatch")
    active_snapshot = task.get("active_execution_target_snapshot")
    if (
        isinstance(active_snapshot, dict)
        and receipt["target"]["target_snapshot_digest"]
        != _stable_id(active_snapshot)
    ):
        return _unknown_view("current_target_snapshot_mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": RECEIPT_CONTRACT,
        "status": "verified-local-preexecution-binding",
        "task_class": contract["binding"]["task_class"],
        "task_revision": contract["binding"]["task_revision"],
        "attempt": receipt["binding"]["attempt"],
        "contract_digest": contract["contract_digest"],
        "receipt_id": receipt["receipt_id"],
        "target": copy.deepcopy(receipt["target"]),
        "revisions": copy.deepcopy(receipt["revisions"]),
        "scope": copy.deepcopy(receipt["scope"]),
        "insufficiency_reasons": [],
        "report_only": True,
        "actual_canary": False,
        "promotion_authority": False,
        "routing_mutation_allowed": False,
    }


def _validate_canonical_history(
    task: dict[str, Any], contract: dict[str, Any]
) -> list[dict[str, Any]]:
    if contract["binding"]["task_id"] != str(task.get("id")):
        raise ExecutionDelegationError("delegation contract task mismatch")
    history = task.get("preexecution_delegation_receipt_history")
    phases = task.get("preexecution_delegation_phase_history")
    if not isinstance(history, list) or not history:
        raise ExecutionDelegationError("missing delegation receipt")
    if not isinstance(phases, list):
        raise ExecutionDelegationError("missing delegation phase history")
    receipts = [validate_preexecution_delegation_receipt(item) for item in history]
    if len({item["receipt_id"] for item in receipts}) != len(receipts):
        raise ExecutionDelegationError("duplicate delegation receipt")
    if len(phases) != len(receipts) * 2:
        raise ExecutionDelegationError("delegation phase history is not canonical")
    if [
        item.get("sequence") if isinstance(item, dict) else None
        for item in phases
    ] != list(range(1, len(phases) + 1)):
        raise ExecutionDelegationError("delegation phase sequence is divergent")
    expected_predecessor = contract["contract_digest"]
    for position, receipt in enumerate(receipts, start=1):
        binding = receipt["binding"]
        if (
            binding["task_id"] != contract["binding"]["task_id"]
            or binding["task_revision"] != contract["binding"]["task_revision"]
            or binding["delegation_contract_digest"] != contract["contract_digest"]
            or binding["attempt"] != position
            or receipt["sequence"]["position"] != position
            or receipt["sequence"]["predecessor"] != expected_predecessor
            or receipt["revisions"] != contract["revisions"]
        ):
            raise ExecutionDelegationError("delegation receipt chain is divergent")
        expected_predecessor = receipt["receipt_id"]
        attempt_phases = [
            item
            for item in phases
            if isinstance(item, dict) and item.get("attempt") == position
        ]
        if len(attempt_phases) != 2:
            raise ExecutionDelegationError("delegation phase ordering is incomplete")
        first, second = attempt_phases
        common_invalid = (
            first.get("phase") != "preexecution_receipt_appended"
            or first.get("receipt_id") != receipt["receipt_id"]
            or second.get("receipt_id") != receipt["receipt_id"]
            or first.get("sequence") >= second.get("sequence")
        )
        if common_invalid:
            raise ExecutionDelegationError("receipt was not ordered before pre-worker snapshot")
        if second.get("phase") == "attempt_recovered_before_pre_worker":
            if position == task.get("attempts"):
                raise ExecutionDelegationError(
                    "current attempt ended before pre-worker snapshot"
                )
            continue
        if second.get("phase") != "pre_worker_snapshot_recorded":
            raise ExecutionDelegationError("invalid delegation phase transition")
        snapshots = [
            item
            for item in task.get("execution_mutation_snapshot_history") or []
            if isinstance(item, dict)
            and item.get("snapshot_id") == second.get("snapshot_id")
            and item.get("phase") == "pre_worker"
            and item.get("binding")
            == {"task_id": binding["task_id"], "attempt": binding["attempt"]}
        ]
        if len(snapshots) != 1:
            raise ExecutionDelegationError("bound pre-worker snapshot is missing")
        receipt_time = _timestamp(receipt["recorded_at"], "recorded_at")
        snapshot_time = _timestamp(
            snapshots[0]["captured_at"], "pre_worker.captured_at"
        )
        if receipt_time > snapshot_time:
            raise ExecutionDelegationError(
                "delegation receipt was recorded after pre-worker snapshot"
            )
    return receipts


def _append_phase(
    phases: list[dict[str, Any]],
    *,
    task_id: str,
    attempt: int,
    receipt_id: str,
    phase: str,
    snapshot_id: str | None = None,
) -> None:
    sequence = len(phases) + 1
    record = {
        "sequence": sequence,
        "task_id": task_id,
        "attempt": attempt,
        "receipt_id": receipt_id,
        "phase": phase,
    }
    if snapshot_id is not None:
        record["snapshot_id"] = snapshot_id
    phases.append(record)


def _target_snapshot(
    task: dict[str, Any],
    settings: ResolvedExecutionConfig | None,
) -> dict[str, Any]:
    snapshot = (
        settings.selected_target_snapshot
        if settings is not None
        else task.get("active_execution_target_snapshot")
    )
    if not isinstance(snapshot, dict):
        raise ExecutionDelegationError(
            "delegated execution requires exact resolved target snapshot"
        )
    return copy.deepcopy(snapshot)


def _resolved_config(
    task: dict[str, Any],
    settings: ResolvedExecutionConfig | None,
) -> dict[str, Any]:
    if settings is None:
        raise ExecutionDelegationError(
            "delegated execution requires resolved execution config"
        )
    return {
        "selection_rule": settings.selection_rule,
        "selection_reason": settings.selection_reason,
        "model": settings.model,
        "model_source": settings.model_source,
        "execution_target": settings.execution_target,
        "codex_profile": settings.codex_profile,
        "config_overrides": settings.config_overrides or {},
        "budget_hint": settings.budget_hint,
        "requirement_vector": settings.requirement_vector,
        "worker_role": settings.worker_role,
        "backend": task.get("execution_backend"),
    }


def _resolved_command(task: dict[str, Any]) -> dict[str, Any]:
    backend = str(task.get("execution_backend") or "codex")
    if backend == "shell":
        argv = task.get("shell_command")
    elif backend == "external-json-command":
        argv = task.get("external_command")
    else:
        argv = ["codex-exec-contract-v1"]
    if not isinstance(argv, list) or not argv or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise ExecutionDelegationError("resolved command contract is unavailable")
    return {"backend": backend, "argv": argv}


def _receipt_id(value: dict[str, Any]) -> str:
    body = copy.deepcopy(value)
    body.pop("receipt_id", None)
    body.pop("recorded_at", None)
    return _stable_id(body)


def _unknown_view(reason: str) -> dict[str, Any]:
    return {
        "schema_version": 0,
        "contract": "cbr-preexecution-delegation-receipt-unknown",
        "status": "insufficient",
        "scope": {
            "control_plane": CONTROL_PLANE,
            "local_preexecution_delegation_binding": False,
            "external_issuer_authenticated": False,
            "global_provenance": "unknown",
        },
        "insufficiency_reasons": [reason],
        "report_only": True,
        "actual_canary": False,
        "promotion_authority": False,
        "routing_mutation_allowed": False,
    }


def _side_effect_boundary(value: object) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) != SIDE_EFFECT_FIELDS:
        raise ExecutionDelegationError("side effect boundary fields are not canonical")
    if not all(isinstance(item, bool) for item in value.values()):
        raise ExecutionDelegationError("side effect boundary values must be boolean")
    return {key: value[key] for key in sorted(SIDE_EFFECT_FIELDS)}


def _safe_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ExecutionDelegationError(f"{name} must be a public-safe identifier")
    return value


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
    ):
        raise ExecutionDelegationError(f"{name} must be a sha256 digest")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExecutionDelegationError(f"{name} must be a positive integer")
    return value


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ExecutionDelegationError(f"{name} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionDelegationError(f"{name} must be a timestamp") from exc
    if parsed.tzinfo is None:
        raise ExecutionDelegationError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _stable_id(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_digest(value: dict[str, Any], field: str) -> None:
    expected = _stable_id({key: item for key, item in value.items() if key != field})
    if value.get(field) != expected:
        raise ExecutionDelegationError(f"{field} does not match canonical record")


def _validate_public_safe(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ExecutionDelegationError("private field is forbidden")
            _validate_public_safe(item)
    elif isinstance(value, list):
        for item in value:
            _validate_public_safe(item)


def _reason(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return text[:96] or "invalid_delegation_evidence"

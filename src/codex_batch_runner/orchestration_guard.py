"""Read-only D3 guarded-orchestration policy and reconciliation shadow."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .fs import read_json
from .orchestration import build_orchestration_plan, validate_manifest
from .orchestration_dispatch import (
    build_dispatch_preview,
    identity_for,
    validate_execution_envelope,
)
from .queue import load_task


POLICY_CONTRACT = "orchestration-guard-policy-v1"
TRIGGER_CONTRACT = "orchestration-trigger-v1"
SHADOW_CONTRACT = "orchestration-reconciliation-shadow-v1"
ERROR_CONTRACT = "orchestration-reconciliation-error-v1"
MINIMUM_EXPLICIT_SUCCESSES = 5
MAX_INPUT_BYTES = 64 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
TRIGGER_ID = re.compile(r"^ot-[0-9a-f]{32}$")

POLICY_ROOT_KEYS = {
    "schema_version",
    "contract",
    "policy_id",
    "revision",
    "active",
    "activation_mode",
    "source",
    "scope",
    "evidence",
    "rollout",
}
POLICY_SOURCE_KEYS = {"source_id", "adapter_revision"}
POLICY_SCOPE_KEYS = {
    "source_kinds",
    "project_ids",
    "repository_roots",
    "work_kinds",
    "decision_authorities",
    "impacts",
    "allowed_mutations",
    "required_prohibited_mutations",
    "isolations",
    "work_verifications",
    "required_verification_scope",
    "capacity_pools",
}
POLICY_EVIDENCE_KEYS = {
    "cohort_id",
    "provenance",
    "successful_explicit_dispatches",
    "identity_conflicts",
    "safety_violations",
}
POLICY_ROLLOUT_KEYS = {"max_new_admissions_per_run"}
TRIGGER_ROOT_KEYS = {
    "schema_version",
    "contract",
    "trigger_id",
    "source_id",
    "source_adapter_revision",
    "source_event_id",
    "explicit_opt_in",
    "policy_id",
    "policy_revision",
    "policy_fingerprint",
    "request_id",
    "request_fingerprint",
    "execution_fingerprint",
    "created_at",
}

VALIDATION_ORDER = (
    "input_unreadable",
    "input_too_large",
    "input_not_utf8",
    "input_json_invalid",
    "input_not_object",
    "fields_invalid",
    "value_type_invalid",
    "value_enum_invalid",
    "value_bounds_invalid",
    "unsafe_identifier",
    "duplicate_list_item",
    "cross_field_conflict",
)
REASON_ORDER = (
    "policy_inactive",
    "activation_not_implemented",
    "source_mismatch",
    "explicit_opt_in_required",
    "policy_binding_mismatch",
    "trigger_identity_mismatch",
    "request_binding_mismatch",
    "idempotency_binding_mismatch",
    "evidence_floor_not_met",
    "evidence_conflict_present",
    "source_kind_not_allowed",
    "project_not_allowed",
    "repository_not_allowed",
    "work_kind_not_allowed",
    "decision_authority_not_allowed",
    "impact_not_allowed",
    "automation_boundary_not_bounded",
    "mutation_not_allowed",
    "required_prohibition_missing",
    "isolation_not_allowed",
    "work_verification_not_allowed",
    "verification_scope_missing",
    "capacity_pool_not_allowed",
    "plan_not_ready",
    "recommended_surface_not_cbr_batch",
    "d2_state_unreadable",
    "d2_dispatch_blocked",
    "d2_identity_conflict",
    "attention_state_unreadable",
)

SOURCE_KINDS = {
    "codex_parent_thread",
    "codex_user_owned_thread",
    "todoist_task",
    "operator",
    "automation",
    "other",
}
WORK_KINDS = {
    "architecture_policy",
    "discussion",
    "implementation",
    "review",
    "verification",
    "operations",
}
DECISION_AUTHORITIES = {"delegated_decision", "bounded_experiment"}
IMPACTS = {"low", "medium"}
MUTATIONS = {"read_only", "local_files", "tracked_files"}
REQUIRED_PROHIBITIONS = {"external_state", "destructive"}
ISOLATIONS = {"none", "worktree", "required"}
WORK_VERIFICATIONS = {"objective", "mixed"}
VERIFICATION_SCOPE = {
    "docs",
    "lint",
    "typecheck",
    "unit",
    "integration",
    "e2e",
    "smoke",
    "manual",
    "build",
}
ATTENTION_DELIVERY_STATES = {
    "pending",
    "retry_wait",
    "delivered",
    "acknowledged",
    "unavailable",
    "failed",
}
ATTENTION_BLOCKING_PRECEDENCE = (
    "failed",
    "unavailable",
    "retry_wait",
    "pending",
)


class GuardContractError(ValueError):
    def __init__(self, *codes: str):
        found = set(codes)
        self.codes = tuple(code for code in VALIDATION_ORDER if code in found)
        super().__init__(", ".join(self.codes))


def load_guard_policy(path: str | Path) -> dict[str, Any]:
    return validate_guard_policy(_load_json(path))


def load_guard_trigger(path: str | Path) -> dict[str, Any]:
    return validate_guard_trigger(_load_json(path))


def validate_guard_policy(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GuardContractError("input_not_object")
    value = _nfc(value)
    if (
        set(value) != POLICY_ROOT_KEYS
        or value.get("schema_version") != 1
        or value.get("contract") != POLICY_CONTRACT
    ):
        raise GuardContractError("fields_invalid")
    source = _exact_object(value.get("source"), POLICY_SOURCE_KEYS)
    scope = _exact_object(value.get("scope"), POLICY_SCOPE_KEYS)
    evidence = _exact_object(value.get("evidence"), POLICY_EVIDENCE_KEYS)
    rollout = _exact_object(value.get("rollout"), POLICY_ROLLOUT_KEYS)
    active = value.get("active")
    if not isinstance(active, bool):
        raise GuardContractError("value_type_invalid")
    activation_mode = value.get("activation_mode")
    if not isinstance(activation_mode, str):
        raise GuardContractError("value_type_invalid")
    if activation_mode not in {"shadow", "guarded"}:
        raise GuardContractError("value_enum_invalid")
    repository_roots = _repository_roots(scope.get("repository_roots"))
    successful = _nonnegative_int(evidence.get("successful_explicit_dispatches"))
    identity_conflicts = _nonnegative_int(evidence.get("identity_conflicts"))
    safety_violations = _nonnegative_int(evidence.get("safety_violations"))
    max_admissions = _nonnegative_int(rollout.get("max_new_admissions_per_run"))
    if max_admissions != 1:
        raise GuardContractError("cross_field_conflict")
    normalized = {
        "schema_version": 1,
        "contract": POLICY_CONTRACT,
        "policy_id": _safe_id(value.get("policy_id")),
        "revision": _safe_id(value.get("revision")),
        "active": active,
        "activation_mode": activation_mode,
        "source": {
            "source_id": _safe_id(source.get("source_id")),
            "adapter_revision": _safe_id(source.get("adapter_revision")),
        },
        "scope": {
            "source_kinds": _enum_list(scope.get("source_kinds"), SOURCE_KINDS),
            "project_ids": _safe_id_list(scope.get("project_ids")),
            "repository_roots": repository_roots,
            "work_kinds": _enum_list(scope.get("work_kinds"), WORK_KINDS),
            "decision_authorities": _enum_list(
                scope.get("decision_authorities"), DECISION_AUTHORITIES
            ),
            "impacts": _enum_list(scope.get("impacts"), IMPACTS),
            "allowed_mutations": _enum_list(scope.get("allowed_mutations"), MUTATIONS),
            "required_prohibited_mutations": _enum_list(
                scope.get("required_prohibited_mutations"),
                REQUIRED_PROHIBITIONS,
            ),
            "isolations": _enum_list(scope.get("isolations"), ISOLATIONS),
            "work_verifications": _enum_list(
                scope.get("work_verifications"), WORK_VERIFICATIONS
            ),
            "required_verification_scope": _enum_list(
                scope.get("required_verification_scope"), VERIFICATION_SCOPE
            ),
            "capacity_pools": _safe_id_list(scope.get("capacity_pools")),
        },
        "evidence": {
            "cohort_id": _safe_id(evidence.get("cohort_id")),
            "provenance": _exact_value(
                evidence.get("provenance"), "operator_attested_explicit_d2"
            ),
            "successful_explicit_dispatches": successful,
            "identity_conflicts": identity_conflicts,
            "safety_violations": safety_violations,
        },
        "rollout": {"max_new_admissions_per_run": max_admissions},
    }
    for key, items in normalized["scope"].items():
        if key != "required_verification_scope" and not items:
            raise GuardContractError("value_bounds_invalid")
    if (
        set(normalized["scope"]["required_prohibited_mutations"])
        != REQUIRED_PROHIBITIONS
    ):
        raise GuardContractError("cross_field_conflict")
    return normalized


def validate_guard_trigger(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GuardContractError("input_not_object")
    value = _nfc(value)
    if (
        set(value) != TRIGGER_ROOT_KEYS
        or value.get("schema_version") != 1
        or value.get("contract") != TRIGGER_CONTRACT
    ):
        raise GuardContractError("fields_invalid")
    explicit_opt_in = value.get("explicit_opt_in")
    if not isinstance(explicit_opt_in, bool):
        raise GuardContractError("value_type_invalid")
    trigger_id = value.get("trigger_id")
    if not isinstance(trigger_id, str) or not TRIGGER_ID.fullmatch(trigger_id):
        raise GuardContractError("unsafe_identifier")
    created_at = value.get("created_at")
    if not isinstance(created_at, str):
        raise GuardContractError("value_type_invalid")
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GuardContractError("value_bounds_invalid") from exc
    if parsed.tzinfo is None:
        raise GuardContractError("value_bounds_invalid")
    return {
        "schema_version": 1,
        "contract": TRIGGER_CONTRACT,
        "trigger_id": trigger_id,
        "source_id": _safe_id(value.get("source_id")),
        "source_adapter_revision": _safe_id(value.get("source_adapter_revision")),
        "source_event_id": _safe_id(value.get("source_event_id")),
        "explicit_opt_in": explicit_opt_in,
        "policy_id": _safe_id(value.get("policy_id")),
        "policy_revision": _safe_id(value.get("policy_revision")),
        "policy_fingerprint": _fingerprint(value.get("policy_fingerprint")),
        "request_id": _safe_id(value.get("request_id")),
        "request_fingerprint": _fingerprint(value.get("request_fingerprint")),
        "execution_fingerprint": _fingerprint(value.get("execution_fingerprint")),
        "created_at": parsed.isoformat(),
    }


def policy_fingerprint(policy: dict[str, Any]) -> str:
    normalized = validate_guard_policy(policy)
    return "sha256:" + hashlib.sha256(_canonical_bytes(normalized)).hexdigest()


def trigger_id_for(source_id: str, source_event_id: str) -> str:
    source_id = _safe_id(source_id)
    source_event_id = _safe_id(source_event_id)
    value = {
        "contract": TRIGGER_CONTRACT,
        "source_id": source_id,
        "source_event_id": source_event_id,
    }
    return "ot-" + hashlib.sha256(_canonical_bytes(value)).hexdigest()[:32]


def guard_idempotency_key(trigger_id: str) -> str:
    if not isinstance(trigger_id, str) or not TRIGGER_ID.fullmatch(trigger_id):
        raise GuardContractError("unsafe_identifier")
    return "d3-" + trigger_id[3:]


def build_reconciliation_shadow(
    config: Config,
    *,
    policy: dict[str, Any],
    trigger: dict[str, Any],
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    allow_guarded_activation: bool = False,
) -> dict[str, Any]:
    policy = validate_guard_policy(policy)
    trigger = validate_guard_trigger(trigger)
    manifest = validate_manifest(manifest)
    envelope = validate_execution_envelope(envelope)
    plan = build_orchestration_plan(manifest)
    identity = identity_for(manifest, envelope)
    reasons: set[str] = set()
    try:
        preview = build_dispatch_preview(config, manifest, envelope)
    except (OSError, ValueError, json.JSONDecodeError):
        reasons.add("d2_state_unreadable")
        preview = {
            "status": "unreadable",
            "reason_codes": ["runtime_state_unreadable"],
            "admission_blockers": [],
            "task_present": False,
            "receipt_present": False,
            "task_id": identity["task_id"],
        }

    if not policy["active"]:
        reasons.add("policy_inactive")
    if policy["activation_mode"] != "shadow" and not allow_guarded_activation:
        reasons.add("activation_not_implemented")
    if (
        trigger["source_id"] != policy["source"]["source_id"]
        or trigger["source_adapter_revision"] != policy["source"]["adapter_revision"]
    ):
        reasons.add("source_mismatch")
    if not trigger["explicit_opt_in"]:
        reasons.add("explicit_opt_in_required")
    if (
        trigger["policy_id"] != policy["policy_id"]
        or trigger["policy_revision"] != policy["revision"]
        or trigger["policy_fingerprint"] != policy_fingerprint(policy)
    ):
        reasons.add("policy_binding_mismatch")
    if trigger["trigger_id"] != trigger_id_for(
        trigger["source_id"], trigger["source_event_id"]
    ):
        reasons.add("trigger_identity_mismatch")
    if (
        trigger["request_id"] != manifest["request_id"]
        or trigger["request_fingerprint"] != plan["request_fingerprint"]
        or trigger["execution_fingerprint"] != identity["execution_fingerprint"]
    ):
        reasons.add("request_binding_mismatch")
    if manifest["idempotency_key"] != guard_idempotency_key(trigger["trigger_id"]):
        reasons.add("idempotency_binding_mismatch")

    evidence = policy["evidence"]
    if evidence["successful_explicit_dispatches"] < MINIMUM_EXPLICIT_SUCCESSES:
        reasons.add("evidence_floor_not_met")
    if evidence["identity_conflicts"] or evidence["safety_violations"]:
        reasons.add("evidence_conflict_present")

    scope = policy["scope"]
    work = manifest["work"]
    authority = manifest["authority"]
    mutations = manifest["mutation"]
    task = envelope["task"]
    if manifest["source"]["kind"] not in scope["source_kinds"]:
        reasons.add("source_kind_not_allowed")
    if task["project_id"] not in scope["project_ids"]:
        reasons.add("project_not_allowed")
    if not _cwd_allowed(envelope["cwd"], scope["repository_roots"]):
        reasons.add("repository_not_allowed")
    if work["kind"] not in scope["work_kinds"]:
        reasons.add("work_kind_not_allowed")
    if authority["decision_authority"] not in scope["decision_authorities"]:
        reasons.add("decision_authority_not_allowed")
    if authority["impact"] not in scope["impacts"]:
        reasons.add("impact_not_allowed")
    if manifest["automation_boundary"] != "bounded_automatic":
        reasons.add("automation_boundary_not_bounded")
    if not set(mutations["allowed"]).issubset(scope["allowed_mutations"]):
        reasons.add("mutation_not_allowed")
    if not set(scope["required_prohibited_mutations"]).issubset(
        mutations["prohibited"]
    ):
        reasons.add("required_prohibition_missing")
    if work["isolation"] not in scope["isolations"]:
        reasons.add("isolation_not_allowed")
    if work["verification"] not in scope["work_verifications"]:
        reasons.add("work_verification_not_allowed")
    if not set(scope["required_verification_scope"]).issubset(
        task["verification_scope"]
    ):
        reasons.add("verification_scope_missing")
    if task["capacity_pool"] not in scope["capacity_pools"]:
        reasons.add("capacity_pool_not_allowed")
    if plan["decision_status"] != "ready":
        reasons.add("plan_not_ready")
    if plan["recommended_surface"] != "cbr_batch":
        reasons.add("recommended_surface_not_cbr_batch")
    if preview["status"] == "conflict":
        reasons.add("d2_identity_conflict")
    elif preview["status"] == "blocked":
        reasons.add("d2_dispatch_blocked")

    state = _reconciliation_state(config, preview)
    if state["attention_state_health"] != "readable":
        reasons.add("attention_state_unreadable")
    ordered_reasons = [code for code in REASON_ORDER if code in reasons]
    return {
        "schema_version": 1,
        "contract": SHADOW_CONTRACT,
        "trigger_id": trigger["trigger_id"],
        "policy_id": policy["policy_id"],
        "policy_revision": policy["revision"],
        "policy_fingerprint": policy_fingerprint(policy),
        "request_id": manifest["request_id"],
        "request_fingerprint": plan["request_fingerprint"],
        "execution_fingerprint": identity["execution_fingerprint"],
        "dispatch_id": identity["dispatch_id"],
        "task_id": identity["task_id"],
        "decision_status": (
            (
                "eligible_guarded"
                if policy["activation_mode"] == "guarded"
                else "eligible_shadow"
            )
            if not ordered_reasons
            else "blocked"
        ),
        "reason_codes": ordered_reasons,
        "evidence": {
            "minimum_successes": MINIMUM_EXPLICIT_SUCCESSES,
            "observed_successes": evidence["successful_explicit_dispatches"],
            "identity_conflicts": evidence["identity_conflicts"],
            "safety_violations": evidence["safety_violations"],
        },
        "d2_preview": {
            "status": preview["status"],
            "reason_codes": preview["reason_codes"],
            "admission_blockers": preview["admission_blockers"],
            "task_present": preview["task_present"],
            "receipt_present": preview["receipt_present"],
        },
        "state": state,
        "required_action": (
            "retain_shadow_observation"
            if not ordered_reasons
            else "resolve_blockers_without_dispatch"
        ),
        "mutation": {"allowed": False, "applied": False},
    }


def error_report(codes: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract": ERROR_CONTRACT,
        "decision_status": "invalid",
        "reason_codes": ["guard_contract_invalid"],
        "validation_errors": codes,
        "mutation": {"allowed": False, "applied": False},
    }


def render_reconciliation_shadow(report: dict[str, Any]) -> str:
    lines = [str(report["contract"])]
    for key in (
        "decision_status",
        "trigger_id",
        "policy_id",
        "policy_revision",
        "request_id",
        "dispatch_id",
        "task_id",
    ):
        if key in report:
            lines.append(f"{key}: {report[key]}")
    lines.append(
        "reason_codes: " + (", ".join(report.get("reason_codes", [])) or "none")
    )
    state = report.get("state")
    if isinstance(state, dict):
        for key in (
            "queue_admission",
            "execution",
            "review",
            "apply",
            "attention_delivery",
            "attention_acknowledgement",
            "attention_state_health",
            "source_disposition",
        ):
            lines.append(f"{key}: {state.get(key)}")
    lines.append("mutation: allowed=false applied=false")
    return "\n".join(lines) + "\n"


def _reconciliation_state(config: Config, preview: dict[str, Any]) -> dict[str, str]:
    if preview["status"] == "conflict":
        admission = "conflict"
    elif preview["task_present"] and preview["receipt_present"]:
        admission = "admitted"
    elif preview["task_present"]:
        admission = "receipt_recovery_required"
    else:
        admission = "not_admitted"
    task: dict[str, Any] | None = None
    if preview["task_present"]:
        try:
            loaded = load_task(config, preview["task_id"])
        except (OSError, ValueError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            task = loaded
    attention = _attention_state(config, preview["task_id"])
    return {
        "queue_admission": admission,
        "execution": str(task.get("status") or "unknown") if task else "not_started",
        "review": (
            str(task.get("review_status") or "not_started") if task else "not_started"
        ),
        "apply": (
            str(task.get("execution_apply_status") or "not_started")
            if task
            else "not_started"
        ),
        "attention_delivery": attention["delivery"],
        "attention_acknowledgement": attention["acknowledgement"],
        "attention_state_health": attention["health"],
        "source_disposition": "not_observed",
    }


def _attention_state(config: Config, task_id: str) -> dict[str, str]:
    root = config.parent_attention_outbox_dir or (
        config.log_dir.parent / "parent-attention-outbox"
    )
    if not root.exists():
        return {
            "delivery": "not_emitted",
            "acknowledgement": "not_acknowledged",
            "health": "readable",
        }
    delivery_states: list[str] = []
    unreadable = False
    for path in sorted(root.glob("pa-*.json")):
        try:
            record = read_json(path, None)
        except (OSError, json.JSONDecodeError):
            unreadable = True
            continue
        if not isinstance(record, dict):
            unreadable = True
            continue
        work_item_ref = record.get("work_item_ref")
        if not isinstance(work_item_ref, str):
            unreadable = True
            continue
        if work_item_ref != task_id:
            continue
        delivery = record.get("delivery")
        if not isinstance(delivery, dict):
            unreadable = True
            continue
        state = delivery.get("state")
        if not isinstance(state, str) or state not in ATTENTION_DELIVERY_STATES:
            unreadable = True
            continue
        delivery_states.append(state)
    if not delivery_states:
        return {
            "delivery": "unknown" if unreadable else "not_emitted",
            "acknowledgement": "unknown" if unreadable else "not_acknowledged",
            "health": "unreadable" if unreadable else "readable",
        }
    for state in ATTENTION_BLOCKING_PRECEDENCE:
        if state in delivery_states:
            return {
                "delivery": state,
                "acknowledgement": "not_acknowledged",
                "health": "unreadable" if unreadable else "readable",
            }
    if all(state == "acknowledged" for state in delivery_states):
        return {
            "delivery": "delivered",
            "acknowledgement": "acknowledged",
            "health": "unreadable" if unreadable else "readable",
        }
    if all(state in {"delivered", "acknowledged"} for state in delivery_states):
        return {
            "delivery": "delivered",
            "acknowledgement": "pending",
            "health": "unreadable" if unreadable else "readable",
        }
    return {
        "delivery": "unknown",
        "acknowledgement": "unknown",
        "health": "unreadable",
    }


def _load_json(path: str | Path) -> object:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise GuardContractError("input_unreadable") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise GuardContractError("input_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GuardContractError("input_not_utf8") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GuardContractError("input_json_invalid") from exc


def _exact_object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GuardContractError("value_type_invalid")
    if set(value) != keys:
        raise GuardContractError("fields_invalid")
    return value


def _safe_id(value: object) -> str:
    if not isinstance(value, str):
        raise GuardContractError("value_type_invalid")
    if not SAFE_ID.fullmatch(value):
        raise GuardContractError("unsafe_identifier")
    return value


def _safe_id_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise GuardContractError("value_type_invalid")
    values = [_safe_id(item) for item in value]
    if len(values) > 32:
        raise GuardContractError("value_bounds_invalid")
    if len(values) != len(set(values)):
        raise GuardContractError("duplicate_list_item")
    return sorted(values)


def _enum_list(value: object, allowed: set[str]) -> list[str]:
    if not isinstance(value, list):
        raise GuardContractError("value_type_invalid")
    if len(value) > 32:
        raise GuardContractError("value_bounds_invalid")
    if not all(isinstance(item, str) for item in value):
        raise GuardContractError("value_type_invalid")
    if len(value) != len(set(value)):
        raise GuardContractError("duplicate_list_item")
    if not set(value).issubset(allowed):
        raise GuardContractError("value_enum_invalid")
    return sorted(value)


def _repository_roots(value: object) -> list[str]:
    if not isinstance(value, list):
        raise GuardContractError("value_type_invalid")
    roots: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise GuardContractError("value_type_invalid")
        path = Path(item)
        if not path.is_absolute() or "~" in path.parts:
            raise GuardContractError("value_bounds_invalid")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise GuardContractError("value_bounds_invalid") from exc
        if not resolved.is_dir():
            raise GuardContractError("value_bounds_invalid")
        roots.append(str(resolved))
    if not roots:
        raise GuardContractError("value_bounds_invalid")
    if len(roots) != len(set(roots)):
        raise GuardContractError("duplicate_list_item")
    return sorted(roots)


def _nonnegative_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise GuardContractError("value_type_invalid")
    if value < 0:
        raise GuardContractError("value_bounds_invalid")
    return value


def _exact_value(value: object, expected: str) -> str:
    if not isinstance(value, str):
        raise GuardContractError("value_type_invalid")
    if value != expected:
        raise GuardContractError("value_enum_invalid")
    return value


def _fingerprint(value: object) -> str:
    if not isinstance(value, str):
        raise GuardContractError("value_type_invalid")
    if not FINGERPRINT.fullmatch(value):
        raise GuardContractError("value_bounds_invalid")
    return value


def _cwd_allowed(cwd: str, roots: list[str]) -> bool:
    try:
        path = Path(cwd).resolve(strict=True)
    except OSError:
        return False
    return any(path == Path(root) or path.is_relative_to(Path(root)) for root in roots)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc(item) for item in value]
    if isinstance(value, dict):
        return {_nfc(key): _nfc(item) for key, item in value.items()}
    return value

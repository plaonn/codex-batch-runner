"""Report-only orchestration surface selection shadow evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .events import read_jsonl, write_event
from .fs import read_json, write_json_atomic_create
from .lock import FileLock
from .orchestration import SURFACES, build_orchestration_plan, validate_manifest
from .timeutil import iso_now


DECISION_CONTRACT = "orchestration-selection-decision-v1"
PREVIEW_CONTRACT = "orchestration-selection-preview-v1"
RECEIPT_CONTRACT = "orchestration-selection-receipt-v1"
APPLY_PREVIEW_CONTRACT = "orchestration-selection-apply-preview-v1"
POLICY_REVISION = "orchestration-selection-shadow-policy-v1"
MAX_PREVIEW_BYTES = 128 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
CANONICAL_SURFACES = frozenset(SURFACES)
SELECTION_REASON_ORDER = (
    "selected_recommended_surface",
    "selected_authorized_override",
    "selection_missing",
    "selected_surface_not_candidate",
    "selected_surface_ineligible",
    "selected_without_valid_override",
    "override_authority_insufficient",
    "override_scope_mismatch",
    "override_expired",
    "source_binding_mismatch",
    "policy_revision_mismatch",
)
SELECTION_REASON_CODES = frozenset(SELECTION_REASON_ORDER)
OVERRIDE_ACTOR_KINDS = frozenset(
    {"coordinator", "operator", "source_owner", "automation"}
)
OVERRIDE_AUTHORITIES = frozenset({"delegated_decision", "bounded_experiment"})
FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "raw_prompt",
        "transcript",
        "log",
        "logs",
        "task_id",
        "thread_id",
        "session_id",
        "user_id",
        "account_id",
        "actor_name",
        "path",
        "argv",
        "command",
        "credential",
        "credentials",
        "todoist",
        "quota_identity",
    }
)


class OrchestrationSelectionError(ValueError):
    pass


class SelectionReceiptConflict(RuntimeError):
    pass


def build_selection_preview(
    manifest: dict[str, Any],
    *,
    selected_surface: str | None,
    source_contract_digest: str,
    policy_revision: str,
    evaluated_at: str,
    override: object | None = None,
) -> dict[str, Any]:
    """Build deterministic evidence without reading or mutating runtime state."""
    plan = build_orchestration_plan(manifest)
    evaluated = _timestamp("evaluated_at", evaluated_at)
    policy = _safe_id("policy_revision", policy_revision)
    source_digest = _digest("source_contract_digest", source_contract_digest)
    expected_source_digest = stable_digest(manifest)
    source_reason = (
        [] if source_digest == expected_source_digest else ["source_binding_mismatch"]
    )
    if policy != POLICY_REVISION:
        policy_reason = ["policy_revision_mismatch"]
    else:
        policy_reason = []
    selected = _optional_surface("selected_surface", selected_surface)
    candidates = list(manifest["surface_preferences"])
    eligible = [
        item
        for item in [
            plan.get("recommended_surface"),
            *plan.get("fallback_surfaces", []),
        ]
        if isinstance(item, str)
    ]
    excluded = {
        item["surface"]: list(item["reason_codes"])
        for item in plan.get("excluded_surfaces", [])
    }
    surfaces_evaluated = (
        plan.get("decision_status") == "ready"
        or bool(plan.get("excluded_surfaces"))
    )
    eligibility_snapshot = [
        {
            "surface": surface,
            "evaluated": surfaces_evaluated,
            "eligible": surface in eligible if surfaces_evaluated else None,
            "reason_codes": (
                [] if surface in eligible else excluded.get(surface, [])
            )
            if surfaces_evaluated
            else [],
        }
        for surface in candidates
    ]
    request_fingerprint = _digest(
        "plan.request_fingerprint", plan["request_fingerprint"]
    )
    canonical_override, override_reasons = _override(
        override,
        request_fingerprint=request_fingerprint,
        policy_revision=policy,
        selected_surface=selected,
        evaluated_at=evaluated,
        authority_context=manifest["authority"],
    )
    reasons = [*source_reason, *policy_reason]
    status: str
    would_warn: bool | None
    recommended = plan.get("recommended_surface")
    if plan.get("decision_status") != "ready":
        status = "blocked"
        would_warn = None
        reasons.append(
            "selection_missing" if selected is None else "selected_surface_ineligible"
        )
    elif selected is None:
        status = "missing"
        would_warn = None
        reasons.append("selection_missing")
    elif selected not in candidates:
        status = "mismatch"
        would_warn = True
        reasons.append("selected_surface_not_candidate")
    elif selected not in eligible:
        status = "mismatch"
        would_warn = True
        reasons.append("selected_surface_ineligible")
    elif selected == recommended:
        status = "recorded"
        would_warn = False
        reasons.append("selected_recommended_surface")
    elif override_reasons:
        status = "invalid"
        would_warn = None
        reasons.extend(override_reasons)
        reasons.append("selected_without_valid_override")
    elif canonical_override is None:
        status = "mismatch"
        would_warn = True
        reasons.append("selected_without_valid_override")
    else:
        status = "recorded"
        would_warn = False
        reasons.append("selected_authorized_override")
    if override_reasons and status not in {"blocked", "missing"}:
        status = "invalid"
        would_warn = None
        reasons.extend(override_reasons)
    if source_reason or policy_reason:
        status = "invalid"
        would_warn = None
    decision_body = {
        "schema_version": 1,
        "contract": DECISION_CONTRACT,
        "evaluated_at": evaluated,
        "source_contract_digest": source_digest,
        "request_fingerprint": request_fingerprint,
        "policy_revision": policy,
        "decision_status": status,
        "candidates": candidates,
        "eligible": eligible,
        "eligibility_snapshot": eligibility_snapshot,
        "recommended_surface": recommended,
        "selected_surface": selected,
        "recommendation_reason_codes": _safe_reason_codes(
            "recommendation_reason_codes", plan.get("reason_codes", [])
        ),
        "selection_reason_codes": _ordered_selection_reasons(reasons),
        "required_preflight": _safe_reason_codes(
            "required_preflight", plan.get("required_preflight", [])
        ),
        "collection_owner": _safe_id("collection_owner", plan["collection_owner"]),
        "override": canonical_override,
        "would_warn": would_warn,
        "mutation": False,
    }
    decision_body["decision_id"] = _decision_id(decision_body)
    decision = validate_selection_decision(decision_body)
    preview_body = {
        "schema_version": 1,
        "contract": PREVIEW_CONTRACT,
        "decision": decision,
        "mutation": False,
    }
    preview_body["preview_digest"] = stable_digest(preview_body)
    return validate_selection_preview(preview_body)


def validate_selection_decision(value: object) -> dict[str, Any]:
    decision = _object("decision", value)
    expected = {
        "schema_version",
        "contract",
        "decision_id",
        "evaluated_at",
        "source_contract_digest",
        "request_fingerprint",
        "policy_revision",
        "decision_status",
        "candidates",
        "eligible",
        "eligibility_snapshot",
        "recommended_surface",
        "selected_surface",
        "recommendation_reason_codes",
        "selection_reason_codes",
        "required_preflight",
        "collection_owner",
        "override",
        "would_warn",
        "mutation",
    }
    _exact_keys("decision", decision, expected)
    _literal("decision.schema_version", decision["schema_version"], 1)
    _literal("decision.contract", decision["contract"], DECISION_CONTRACT)
    evaluated_at = _timestamp("decision.evaluated_at", decision["evaluated_at"])
    candidates = _surface_list("decision.candidates", decision["candidates"])
    eligible = _surface_subset(
        "decision.eligible", decision["eligible"], candidates=candidates
    )
    snapshot = _eligibility_snapshot(
        decision["eligibility_snapshot"],
        candidates=candidates,
        eligible=eligible,
    )
    recommended = _optional_surface(
        "decision.recommended_surface", decision["recommended_surface"]
    )
    selected = _optional_surface(
        "decision.selected_surface", decision["selected_surface"]
    )
    if recommended is not None and recommended not in candidates:
        raise OrchestrationSelectionError("recommended surface must be a candidate")
    status = decision["decision_status"]
    if status not in {"recorded", "mismatch", "invalid", "blocked", "missing"}:
        raise OrchestrationSelectionError("decision status is invalid")
    override_value = decision["override"]
    override = None
    if override_value is not None:
        override, override_reasons = _override(
            override_value,
            request_fingerprint=_digest(
                "decision.request_fingerprint",
                decision["request_fingerprint"],
            ),
            policy_revision=_safe_id(
                "decision.policy_revision", decision["policy_revision"]
            ),
            selected_surface=selected,
            evaluated_at=evaluated_at,
        )
        if override_reasons:
            raise OrchestrationSelectionError("stored override is not exact-bound")
    would_warn = decision["would_warn"]
    if would_warn not in {True, False, None}:
        raise OrchestrationSelectionError("would_warn must be boolean or null")
    if status in {"invalid", "blocked", "missing"} and would_warn is not None:
        raise OrchestrationSelectionError(
            "invalid, blocked, or missing decisions require null would_warn"
        )
    if status == "recorded" and would_warn is not False:
        raise OrchestrationSelectionError("recorded decisions require would_warn=false")
    if status == "mismatch" and would_warn is not True:
        raise OrchestrationSelectionError("mismatch decisions require would_warn=true")
    _literal("decision.mutation", decision["mutation"], False)
    canonical = {
        "schema_version": 1,
        "contract": DECISION_CONTRACT,
        "evaluated_at": evaluated_at,
        "source_contract_digest": _digest(
            "decision.source_contract_digest",
            decision["source_contract_digest"],
        ),
        "request_fingerprint": _digest(
            "decision.request_fingerprint",
            decision["request_fingerprint"],
        ),
        "policy_revision": _safe_id(
            "decision.policy_revision", decision["policy_revision"]
        ),
        "decision_status": status,
        "candidates": candidates,
        "eligible": eligible,
        "eligibility_snapshot": snapshot,
        "recommended_surface": recommended,
        "selected_surface": selected,
        "recommendation_reason_codes": _safe_reason_codes(
            "decision.recommendation_reason_codes",
            decision["recommendation_reason_codes"],
        ),
        "selection_reason_codes": _selection_reason_codes(
            decision["selection_reason_codes"]
        ),
        "required_preflight": _safe_reason_codes(
            "decision.required_preflight", decision["required_preflight"]
        ),
        "collection_owner": _safe_id(
            "decision.collection_owner", decision["collection_owner"]
        ),
        "override": override,
        "would_warn": would_warn,
        "mutation": False,
    }
    _literal(
        "decision.decision_id",
        decision["decision_id"],
        _decision_id(canonical),
    )
    canonical["decision_id"] = decision["decision_id"]
    _validate_public_safe(canonical)
    return canonical


def validate_selection_preview(value: object) -> dict[str, Any]:
    preview = _object("preview", value)
    _exact_keys(
        "preview",
        preview,
        {
            "schema_version",
            "contract",
            "decision",
            "mutation",
            "preview_digest",
        },
    )
    _literal("preview.schema_version", preview["schema_version"], 1)
    _literal("preview.contract", preview["contract"], PREVIEW_CONTRACT)
    decision = validate_selection_decision(preview["decision"])
    _literal("preview.mutation", preview["mutation"], False)
    canonical = {
        "schema_version": 1,
        "contract": PREVIEW_CONTRACT,
        "decision": decision,
        "mutation": False,
    }
    _literal(
        "preview.preview_digest",
        preview["preview_digest"],
        stable_digest(canonical),
    )
    canonical["preview_digest"] = preview["preview_digest"]
    return canonical


def load_selection_preview(path: str | Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise OrchestrationSelectionError("preview is unreadable") from exc
    if len(raw) > MAX_PREVIEW_BYTES:
        raise OrchestrationSelectionError("preview is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OrchestrationSelectionError("preview is invalid JSON") from exc
    return validate_selection_preview(value)


def load_selection_receipt(path: str | Path) -> dict[str, Any]:
    value = _load_private_json(Path(path), missing=None)
    if value is None:
        raise OrchestrationSelectionError("selection receipt is missing")
    return validate_selection_receipt(value)


def validate_source_bound_selection_receipt(
    value: object,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    receipt = validate_selection_receipt(value)
    preview = {
        "schema_version": 1,
        "contract": PREVIEW_CONTRACT,
        "decision": receipt["decision"],
        "mutation": False,
        "preview_digest": receipt["preview_digest"],
    }
    _validate_source_bound_preview(preview, manifest)
    return receipt


def load_selection_override(path: str | Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise OrchestrationSelectionError("override is unreadable") from exc
    if len(raw) > MAX_PREVIEW_BYTES:
        raise OrchestrationSelectionError("override is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OrchestrationSelectionError("override is invalid JSON") from exc
    return _object("override", value)


def _validate_source_bound_preview(
    preview: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    validated = validate_selection_preview(preview)
    decision = validated["decision"]
    canonical_manifest = validate_manifest(manifest)
    if decision["source_contract_digest"] != stable_digest(canonical_manifest):
        raise OrchestrationSelectionError(
            "selection preview source binding mismatch"
        )
    if decision["policy_revision"] != POLICY_REVISION:
        raise OrchestrationSelectionError(
            "selection preview policy revision mismatch"
        )
    if decision["decision_status"] == "invalid":
        raise OrchestrationSelectionError(
            "invalid selection preview cannot be recorded"
        )
    expected = build_selection_preview(
        canonical_manifest,
        selected_surface=decision["selected_surface"],
        source_contract_digest=decision["source_contract_digest"],
        policy_revision=decision["policy_revision"],
        evaluated_at=decision["evaluated_at"],
        override=decision["override"],
    )
    if validated != expected:
        raise OrchestrationSelectionError(
            "selection preview does not match canonical D1 output"
        )
    return validated


def build_selection_apply_preview(
    config: Config,
    preview: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    validated = _validate_source_bound_preview(preview, manifest)
    decision_id = validated["decision"]["decision_id"]
    path = _receipt_path(config, decision_id)
    existing = _load_private_json(path, missing=None)
    if existing is None:
        status = "ready"
    elif _receipt_matches(existing, validated):
        status = "already_recorded"
    else:
        status = "conflict"
    return {
        "schema_version": 1,
        "contract": APPLY_PREVIEW_CONTRACT,
        "decision_id": decision_id,
        "status": status,
        "receipt_present": existing is not None,
        "mutation": {"allowed": False, "applied": False},
    }


def apply_selection_record(
    config: Config,
    preview: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    validated = _validate_source_bound_preview(preview, manifest)
    decision = validated["decision"]
    decision_id = decision["decision_id"]
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire():
        raise OrchestrationSelectionError("selection receipt lock is busy")
    try:
        path = _receipt_path(config, decision_id)
        existing = _load_private_json(path, missing=None)
        if existing is not None:
            if not _receipt_matches(existing, validated):
                raise SelectionReceiptConflict("selection receipt identity conflict")
            receipt = existing
        else:
            body = {
                "schema_version": 1,
                "contract": RECEIPT_CONTRACT,
                "recorded_at": iso_now(),
                "decision": deepcopy(decision),
                "preview_digest": validated["preview_digest"],
                "audit_event": {
                    "event_type": "orchestration_selection_recorded",
                    "required": True,
                },
                "mutation": {"allowed": True, "applied": True},
            }
            body["receipt_id"] = stable_digest(body)
            receipt = validate_selection_receipt(body)
            _ensure_private_directory(path.parent)
            try:
                write_json_atomic_create(path, receipt)
                os.chmod(path, 0o600)
            except FileExistsError:
                raced = _load_private_json(path, missing=None)
                if not _receipt_matches(raced, validated):
                    raise SelectionReceiptConflict(
                        "selection receipt identity conflict"
                    )
                receipt = validate_selection_receipt(raced)
            except OSError as exc:
                raise OrchestrationSelectionError(
                    "selection receipt write failed"
                ) from exc
        _ensure_selection_event(config, receipt)
        return receipt
    finally:
        lock.release()


def validate_selection_receipt(value: object) -> dict[str, Any]:
    receipt = _object("receipt", value)
    _exact_keys(
        "receipt",
        receipt,
        {
            "schema_version",
            "contract",
            "receipt_id",
            "recorded_at",
            "decision",
            "preview_digest",
            "audit_event",
            "mutation",
        },
    )
    _literal("receipt.schema_version", receipt["schema_version"], 1)
    _literal("receipt.contract", receipt["contract"], RECEIPT_CONTRACT)
    decision = validate_selection_decision(receipt["decision"])
    audit = _object("receipt.audit_event", receipt["audit_event"])
    _exact_keys("receipt.audit_event", audit, {"event_type", "required"})
    _literal(
        "receipt.audit_event.event_type",
        audit["event_type"],
        "orchestration_selection_recorded",
    )
    _literal("receipt.audit_event.required", audit["required"], True)
    mutation = _object("receipt.mutation", receipt["mutation"])
    _exact_keys("receipt.mutation", mutation, {"allowed", "applied"})
    _literal("receipt.mutation.allowed", mutation["allowed"], True)
    _literal("receipt.mutation.applied", mutation["applied"], True)
    canonical = {
        "schema_version": 1,
        "contract": RECEIPT_CONTRACT,
        "recorded_at": _timestamp("receipt.recorded_at", receipt["recorded_at"]),
        "decision": decision,
        "preview_digest": _digest("receipt.preview_digest", receipt["preview_digest"]),
        "audit_event": {
            "event_type": "orchestration_selection_recorded",
            "required": True,
        },
        "mutation": {"allowed": True, "applied": True},
    }
    _literal(
        "receipt.receipt_id",
        receipt["receipt_id"],
        stable_digest(canonical),
    )
    canonical["receipt_id"] = receipt["receipt_id"]
    _validate_public_safe(canonical)
    return canonical


def stable_digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise OrchestrationSelectionError(
            "value is not stable-digest serializable"
        ) from exc
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _decision_id(value: dict[str, Any]) -> str:
    identity = {
        key: item
        for key, item in value.items()
        if key not in {"decision_id", "evaluated_at"}
    }
    return stable_digest(identity)


def _override(
    value: object | None,
    *,
    request_fingerprint: str,
    policy_revision: str,
    selected_surface: str | None,
    evaluated_at: str,
    authority_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if value is None:
        return None, []
    reasons: list[str] = []
    if not isinstance(value, dict):
        return None, ["override_scope_mismatch"]
    expected = {"actor_kind", "authority", "reason_code", "scope", "expires_at"}
    if set(value) not in (expected, expected - {"expires_at"}):
        return None, ["override_scope_mismatch"]
    actor = value.get("actor_kind")
    authority = value.get("authority")
    reason_code = value.get("reason_code")
    if actor not in OVERRIDE_ACTOR_KINDS:
        reasons.append("override_authority_insufficient")
    if authority not in OVERRIDE_AUTHORITIES:
        reasons.append("override_authority_insufficient")
    if authority_context is not None and (
        authority_context.get("decision_authority") != authority
        or authority_context.get("resolution") != "resolved"
        or authority_context.get("approval_state")
        not in {"not_required", "granted"}
    ):
        reasons.append("override_authority_insufficient")
    try:
        reason = _safe_id("override.reason_code", reason_code)
    except OrchestrationSelectionError:
        reasons.append("override_scope_mismatch")
        reason = "invalid_override_reason"
    scope = value.get("scope")
    if not isinstance(scope, dict) or set(scope) != {
        "request_fingerprint",
        "policy_revision",
        "selected_surface",
    }:
        reasons.append("override_scope_mismatch")
        canonical_scope = {
            "request_fingerprint": request_fingerprint,
            "policy_revision": policy_revision,
            "selected_surface": selected_surface,
        }
    else:
        canonical_scope = {
            "request_fingerprint": scope.get("request_fingerprint"),
            "policy_revision": scope.get("policy_revision"),
            "selected_surface": scope.get("selected_surface"),
        }
        if canonical_scope != {
            "request_fingerprint": request_fingerprint,
            "policy_revision": policy_revision,
            "selected_surface": selected_surface,
        }:
            reasons.append("override_scope_mismatch")
    expires = value.get("expires_at")
    if expires is not None:
        try:
            expiry = _timestamp("override.expires_at", expires)
            if datetime.fromisoformat(expiry) <= datetime.fromisoformat(evaluated_at):
                reasons.append("override_expired")
        except OrchestrationSelectionError:
            reasons.append("override_expired")
            expiry = str(expires)
    else:
        expiry = None
    canonical = {
        "actor_kind": actor,
        "authority": authority,
        "reason_code": reason,
        "scope": canonical_scope,
        "expires_at": expiry,
    }
    ordered = _ordered_selection_reasons(reasons)
    return (None if ordered else canonical), ordered


def _eligibility_snapshot(
    value: object,
    *,
    candidates: list[str],
    eligible: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(candidates):
        raise OrchestrationSelectionError("eligibility snapshot must match candidates")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        row = _object(f"eligibility_snapshot[{index}]", item)
        _exact_keys(
            f"eligibility_snapshot[{index}]",
            row,
            {"surface", "evaluated", "eligible", "reason_codes"},
        )
        surface = _surface(f"eligibility_snapshot[{index}].surface", row["surface"])
        if surface != candidates[index]:
            raise OrchestrationSelectionError(
                "eligibility snapshot order must match candidates"
            )
        evaluated = row["evaluated"]
        if type(evaluated) is not bool:
            raise OrchestrationSelectionError("evaluated must be boolean")
        row_eligible = row["eligible"]
        if row_eligible not in {True, False, None}:
            raise OrchestrationSelectionError("eligible must be boolean or null")
        if not evaluated and row_eligible is not None:
            raise OrchestrationSelectionError(
                "unevaluated surface requires null eligibility"
            )
        if evaluated and type(row_eligible) is not bool:
            raise OrchestrationSelectionError(
                "evaluated surface requires boolean eligibility"
            )
        if (surface in eligible) is not (row_eligible is True):
            raise OrchestrationSelectionError(
                "eligibility snapshot must match eligible surfaces"
            )
        reasons = _safe_reason_codes(
            f"eligibility_snapshot[{index}].reason_codes",
            row["reason_codes"],
        )
        if row_eligible is True and reasons:
            raise OrchestrationSelectionError(
                "eligible surface cannot have exclusion reasons"
            )
        result.append(
            {
                "surface": surface,
                "evaluated": evaluated,
                "eligible": row_eligible,
                "reason_codes": reasons,
            }
        )
    return result


def _selection_reason_codes(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise OrchestrationSelectionError("selection reason codes must be non-empty")
    codes = [_safe_id("selection_reason_code", item) for item in value]
    if len(codes) != len(set(codes)):
        raise OrchestrationSelectionError("selection reason codes must be unique")
    if not set(codes) <= SELECTION_REASON_CODES:
        raise OrchestrationSelectionError("selection reason code is not canonical")
    return _ordered_selection_reasons(codes)


def _ordered_selection_reasons(values: list[str]) -> list[str]:
    return sorted(set(values), key=lambda item: SELECTION_REASON_ORDER.index(item))


def _safe_reason_codes(key: str, value: object) -> list[str]:
    if not isinstance(value, list):
        raise OrchestrationSelectionError(f"{key} must be a list")
    result = [_safe_id(f"{key}[]", item) for item in value]
    if len(result) != len(set(result)):
        raise OrchestrationSelectionError(f"{key} must be unique")
    return result


def _surface_list(key: str, value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise OrchestrationSelectionError(f"{key} must be a non-empty list")
    result = [_surface(f"{key}[]", item) for item in value]
    if len(result) != len(set(result)):
        raise OrchestrationSelectionError(f"{key} must be unique")
    return result


def _surface_subset(
    key: str, value: object, *, candidates: list[str]
) -> list[str]:
    if not isinstance(value, list):
        raise OrchestrationSelectionError(f"{key} must be a list")
    result = [_surface(f"{key}[]", item) for item in value]
    if len(result) != len(set(result)):
        raise OrchestrationSelectionError(f"{key} must be unique")
    if any(item not in candidates for item in result):
        raise OrchestrationSelectionError(f"{key} must be a candidate subset")
    expected_order = [item for item in candidates if item in result]
    if result != expected_order:
        raise OrchestrationSelectionError(
            f"{key} must preserve candidate order"
        )
    return result


def _optional_surface(key: str, value: object) -> str | None:
    if value is None:
        return None
    return _surface(key, value)


def _surface(key: str, value: object) -> str:
    if value not in CANONICAL_SURFACES:
        raise OrchestrationSelectionError(f"{key} is not a canonical surface")
    return str(value)


def _safe_id(key: str, value: object) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise OrchestrationSelectionError(f"{key} must be a safe identifier")
    return value


def _digest(key: str, value: object) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise OrchestrationSelectionError(f"{key} must be a sha256 digest")
    return value


def _timestamp(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise OrchestrationSelectionError(f"{key} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrchestrationSelectionError(
            f"{key} must be a timezone-aware timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise OrchestrationSelectionError(f"{key} must be a timezone-aware timestamp")
    return parsed.isoformat()


def _object(key: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OrchestrationSelectionError(f"{key} must be an object")
    return value


def _exact_keys(key: str, value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise OrchestrationSelectionError(f"{key} fields are invalid")


def _literal(key: str, value: object, expected: object) -> None:
    if type(value) is not type(expected) or value != expected:
        raise OrchestrationSelectionError(f"{key} is invalid")


def _validate_public_safe(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise OrchestrationSelectionError(
                    "selection evidence contains forbidden identity or content"
                )
            _validate_public_safe(item)
    elif isinstance(value, list):
        for item in value:
            _validate_public_safe(item)
    elif isinstance(value, str):
        if value.startswith("/") or value.startswith("~"):
            raise OrchestrationSelectionError(
                "selection evidence contains a personal path"
            )


def _receipt_matches(value: object, preview: dict[str, Any]) -> bool:
    try:
        receipt = validate_selection_receipt(value)
    except OrchestrationSelectionError:
        return False
    return (
        receipt["decision"] == preview["decision"]
        and receipt["preview_digest"] == preview["preview_digest"]
    )


def _receipt_dir(config: Config) -> Path:
    return config.log_dir.parent / "orchestration-selection-receipts"


def _receipt_path(config: Config, decision_id: str) -> Path:
    digest = _digest("decision_id", decision_id).split(":", 1)[1]
    return _receipt_dir(config) / f"{digest}.json"


def _ensure_selection_event(config: Config, receipt: dict[str, Any]) -> None:
    decision_id = receipt["decision"]["decision_id"]
    decision = receipt["decision"]
    expected_payload = {
        "receipt_id": receipt["receipt_id"],
        "decision_id": decision_id,
        "decision_status": decision["decision_status"],
        "recommended_surface": decision["recommended_surface"],
        "selected_surface": decision["selected_surface"],
        "policy_revision": decision["policy_revision"],
        "would_warn": decision["would_warn"],
        "mutation": False,
    }
    if config.event_dir.exists():
        for path in sorted(config.event_dir.glob("*.jsonl")):
            for event in read_jsonl(path):
                if (
                    event.get("event_type") == "orchestration_selection_recorded"
                    and event.get("source") == "orchestration-selection"
                    and event.get("summary")
                    == "orchestration selection shadow recorded"
                    and event.get("payload") == expected_payload
                ):
                    return
    try:
        write_event(
            config,
            "orchestration_selection_recorded",
            source="orchestration-selection",
            summary="orchestration selection shadow recorded",
            payload=expected_payload,
        )
    except OSError as exc:
        raise OrchestrationSelectionError(
            "selection audit event write failed"
        ) from exc


def _load_private_json(path: Path, *, missing: Any) -> Any:
    try:
        info = path.lstat()
    except FileNotFoundError:
        _validate_private_directory_if_present(path.parent)
        return missing
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OrchestrationSelectionError("selection receipt identity is invalid")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise OrchestrationSelectionError("selection receipt permissions are invalid")
    if info.st_size > MAX_PREVIEW_BYTES:
        raise OrchestrationSelectionError("selection receipt is too large")
    try:
        return read_json(path, missing)
    except (OSError, json.JSONDecodeError) as exc:
        raise OrchestrationSelectionError("selection receipt is unreadable") from exc


def _validate_private_directory_if_present(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OrchestrationSelectionError(
            "selection receipt directory identity is invalid"
        )
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise OrchestrationSelectionError(
            "selection receipt directory permissions are invalid"
        )


def _ensure_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OrchestrationSelectionError(
            "selection receipt directory identity is invalid"
        )
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise OrchestrationSelectionError(
            "selection receipt directory permissions are invalid"
        )

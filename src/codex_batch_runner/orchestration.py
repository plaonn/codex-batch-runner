"""Versioned, deterministic, manifest-only orchestration planning."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any


INTAKE_CONTRACT = "orchestration-intake-v1"
PLAN_CONTRACT = "orchestration-plan-v1"
ERROR_CONTRACT = "orchestration-plan-error-v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
MAX_MANIFEST_BYTES = 64 * 1024
SURFACES = ("codex_parent_thread", "codex_user_owned_thread", "codex_subagent", "cbr_batch", "external_worker")
EXCLUSION_ORDER = ("interaction_incompatible", "work_kind_incompatible", "duration_incompatible", "persistence_incompatible", "resume_incompatible", "dependency_incompatible", "collection_incompatible", "context_incompatible", "impact_incompatible", "mutation_boundary_incompatible", "verification_incompatible", "external_worker_boundary_unverified")
VALIDATION_ORDER = ("input_unreadable", "input_too_large", "input_not_utf8", "input_json_invalid", "input_not_object", "fields_invalid", "value_type_invalid", "value_enum_invalid", "value_bounds_invalid", "unsafe_identifier", "sensitive_field_forbidden", "duplicate_list_item", "empty_surface_preferences", "mutation_overlap", "cross_field_conflict")
MUTATION_ORDER = ("read_only", "local_files", "tracked_files", "runtime_state", "external_state", "destructive")

SCHEMA = {
    "source": {"kind", "collection_owner"},
    "summary": {"root_goal", "requirement", "stop_condition", "done_means"},
    "authority": {"decision_authority", "resolution", "impact", "approval_state"},
    "work": {"kind", "interaction", "duration", "persistence", "resume", "dependency", "collection", "context", "isolation", "verification", "external_worker_boundary", "repository_scope"},
    "mutation": {"allowed", "prohibited"},
}
ROOT_KEYS = {"schema_version", "contract", "request_id", "idempotency_key", "source", "summary", "authority", "work", "mutation", "automation_boundary", "surface_preferences"}
FORBIDDEN_FIELDS = {"source_reference", "repository_path", "task_id", "thread_id", "session_id", "raw_prompt", "prompt", "transcript", "log", "credential", "environment", "command", "argv"}
ENUMS = {
    "source.kind": {"codex_parent_thread", "codex_user_owned_thread", "todoist_task", "operator", "automation", "other"},
    "source.collection_owner": {"source_parent", "source_user", "operator", "external_owner"},
    "authority.decision_authority": {"proposal_only", "recommend_and_pause", "delegated_decision", "bounded_experiment"},
    "authority.resolution": {"resolved", "needs_user_decision", "blocked_external"},
    "authority.impact": {"low", "medium", "high"},
    "authority.approval_state": {"not_required", "granted", "required"},
    "work.kind": {"architecture_policy", "discussion", "implementation", "review", "verification", "operations"},
    "work.interaction": {"none", "user_required", "external_required"},
    "work.duration": {"short", "bounded", "long"},
    "work.persistence": {"turn_bound", "durable"},
    "work.resume": {"not_needed", "required"},
    "work.dependency": {"none", "soft", "hard"},
    "work.collection": {"immediate_parent", "durable_attention", "user_continuation", "external_callback"},
    "work.context": {"parent_context", "self_contained", "independent_context"},
    "work.isolation": {"none", "worktree", "required"},
    "work.verification": {"none", "objective", "semantic", "mixed"},
    "work.external_worker_boundary": {"unavailable", "verified_bounded"},
    "work.repository_scope": {"none", "present"},
    "automation_boundary": {"manual_only", "advisory_only", "bounded_automatic"},
}
MUTATIONS = {"read_only", "local_files", "tracked_files", "runtime_state", "external_state", "destructive"}


class OrchestrationManifestError(ValueError):
    def __init__(self, *codes: str):
        self.codes = tuple(sorted(set(codes), key=VALIDATION_ORDER.index))
        super().__init__(", ".join(self.codes))


def load_manifest(path: str | Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise OrchestrationManifestError("input_unreadable") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise OrchestrationManifestError("input_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise OrchestrationManifestError("input_not_utf8") from exc
    except json.JSONDecodeError as exc:
        raise OrchestrationManifestError("input_json_invalid") from exc
    return validate_manifest(value)


def validate_manifest(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OrchestrationManifestError("input_not_object")
    manifest = _nfc(value)
    if set(manifest) != ROOT_KEYS or manifest.get("schema_version") != 1 or manifest.get("contract") != INTAKE_CONTRACT:
        if (set(manifest) - ROOT_KEYS) & FORBIDDEN_FIELDS:
            raise OrchestrationManifestError("sensitive_field_forbidden")
        raise OrchestrationManifestError("fields_invalid")
    result: dict[str, Any] = {"schema_version": 1, "contract": INTAKE_CONTRACT}
    result["request_id"] = _identifier(manifest.get("request_id"))
    result["idempotency_key"] = _identifier(manifest.get("idempotency_key"))
    for section, keys in SCHEMA.items():
        value_section = _object(manifest.get(section), "value_type_invalid")
        if set(value_section) != keys:
            if (set(value_section) - keys) & FORBIDDEN_FIELDS:
                raise OrchestrationManifestError("sensitive_field_forbidden")
            raise OrchestrationManifestError("fields_invalid")
        result[section] = dict(value_section)
    for key in ("automation_boundary",):
        result[key] = manifest.get(key)
    preferences = manifest.get("surface_preferences")
    result["surface_preferences"] = _enum_list(preferences, set(SURFACES))
    if not result["surface_preferences"]:
        raise OrchestrationManifestError("empty_surface_preferences")
    for section in ("source", "authority", "work"):
        for key, item in result[section].items():
            _enum(f"{section}.{key}", item)
    _enum("automation_boundary", result["automation_boundary"])
    for key in SCHEMA["summary"]:
        _summary_text(result["summary"][key])
    allowed = _enum_list(result["mutation"]["allowed"], MUTATIONS)
    prohibited = _enum_list(result["mutation"]["prohibited"], MUTATIONS)
    if set(allowed) & set(prohibited):
        raise OrchestrationManifestError("mutation_overlap")
    if "read_only" in allowed and allowed != ["read_only"]:
        raise OrchestrationManifestError("cross_field_conflict")
    if "read_only" in prohibited:
        raise OrchestrationManifestError("cross_field_conflict")
    result["mutation"] = {"allowed": _ordered_mutations(allowed), "prohibited": _ordered_mutations(prohibited)}
    _cross_validate(result)
    return result


def build_orchestration_plan(manifest: dict[str, Any]) -> dict[str, Any]:
    """Build a pure plan from an already validated manifest."""
    fingerprint = orchestration_request_fingerprint(manifest)
    authority, work = manifest["authority"], manifest["work"]
    base = _plan_base(manifest["request_id"], fingerprint, manifest["source"]["collection_owner"], manifest)
    if authority["resolution"] == "needs_user_decision" or authority["approval_state"] == "required":
        codes = []
        if authority["resolution"] == "needs_user_decision":
            codes.append("authority_resolution_requires_user_decision")
        if authority["approval_state"] == "required":
            codes.append("approval_required")
        return _complete(base, "needs_user_decision", None, [], [], codes, ["user_decision_required"], ["obtain_user_decision"])
    if authority["resolution"] == "blocked_external" or work["interaction"] == "external_required":
        codes = []
        if authority["resolution"] == "blocked_external":
            codes.append("authority_blocked_external")
        if work["interaction"] == "external_required":
            codes.append("external_interaction_required")
        return _complete(base, "blocked", None, [], [], codes, ["external_blocker"], ["resolve_external_blocker"])
    eligible: list[str] = []
    excluded: list[dict[str, Any]] = []
    for surface in manifest["surface_preferences"]:
        reasons = _surface_reasons(surface, manifest)
        if reasons:
            excluded.append({"surface": surface, "reason_codes": reasons})
        else:
            eligible.append(surface)
    if not eligible:
        return _complete(base, "blocked", None, [], excluded, ["no_eligible_surface"], ["surface_constraints_unsatisfied"], [])
    selected = eligible[0]
    return _complete(base, "ready", selected, eligible[1:], excluded, ["selected_first_eligible_surface", _selection_reason(selected)], [], _preflight(selected))


def orchestration_request_fingerprint(manifest: dict[str, Any]) -> str:
    """Return the canonical D1 request fingerprint without evaluating routing."""
    return _fingerprint(manifest)


def error_plan(codes: tuple[str, ...]) -> dict[str, Any]:
    ordered_codes = sorted(set(codes), key=VALIDATION_ORDER.index)
    return {
        "schema_version": 1, "contract": ERROR_CONTRACT, "request_id": None, "decision_status": "invalid",
        "recommended_surface": None, "fallback_surfaces": [], "reason_codes": ["manifest_invalid"], "validation_errors": ordered_codes,
        "excluded_surfaces": [], "unresolved_constraints": ["valid_manifest_required"], "required_preflight": [],
        "collection_owner": None, "mutation": {"allowed": False, "applied": False},
    }


def render_orchestration_plan(plan: dict[str, Any]) -> str:
    excluded = "; ".join(item["surface"] + ": " + ", ".join(item["reason_codes"]) for item in plan["excluded_surfaces"]) or "-"
    return "\n".join((
        "Orchestration plan (read-only)", f"status: {plan['decision_status']}",
        f"recommendation: {plan['recommended_surface'] or '-'}",
        "fallbacks: " + (", ".join(plan["fallback_surfaces"]) or "-"),
        "blockers or unresolved: " + (", ".join(plan["unresolved_constraints"]) or "-"),
        "excluded: " + excluded, "preflight: " + (", ".join(plan["required_preflight"]) or "-"),
        "collection owner: " + (plan["collection_owner"] or "-"), "mutation: allowed=false applied=false", "",
    ))


def _cross_validate(manifest: dict[str, Any]) -> None:
    authority, work = manifest["authority"], manifest["work"]
    if authority["resolution"] == "resolved" and authority["approval_state"] not in {"not_required", "granted"}:
        raise OrchestrationManifestError("cross_field_conflict")
    if authority["approval_state"] == "required" and authority["resolution"] != "needs_user_decision":
        raise OrchestrationManifestError("cross_field_conflict")
    if authority["resolution"] == "blocked_external" and work["interaction"] != "external_required" and work["collection"] != "external_callback":
        raise OrchestrationManifestError("cross_field_conflict")
    pairs = {"codex_parent_thread": "source_parent", "codex_user_owned_thread": "source_user", "todoist_task": "operator", "operator": "operator", "automation": "operator", "other": "external_owner"}
    if manifest["source"]["collection_owner"] != pairs[manifest["source"]["kind"]]:
        raise OrchestrationManifestError("cross_field_conflict")
    authority, allowed = manifest["authority"], set(manifest["mutation"]["allowed"])
    boundary = manifest["automation_boundary"]
    if authority["decision_authority"] == "proposal_only" and (allowed != {"read_only"} or authority["approval_state"] != "not_required" or boundary not in {"manual_only", "advisory_only"}):
        raise OrchestrationManifestError("cross_field_conflict")
    if authority["decision_authority"] == "recommend_and_pause" and (not allowed <= {"read_only", "local_files"} or boundary not in {"manual_only", "advisory_only"}):
        raise OrchestrationManifestError("cross_field_conflict")
    if authority["decision_authority"] == "bounded_experiment" and not allowed <= {"read_only", "local_files", "tracked_files"}:
        raise OrchestrationManifestError("cross_field_conflict")
    if boundary == "bounded_automatic" and (authority["decision_authority"] not in {"delegated_decision", "bounded_experiment"} or authority["resolution"] != "resolved" or authority["approval_state"] not in {"not_required", "granted"}):
        raise OrchestrationManifestError("cross_field_conflict")


def _surface_reasons(surface: str, manifest: dict[str, Any]) -> list[str]:
    authority, work, mutation = manifest["authority"], manifest["work"], set(manifest["mutation"]["allowed"])
    fail: set[str] = set()
    if surface == "codex_parent_thread":
        if not (work["kind"] in {"architecture_policy", "discussion"} or work["context"] == "parent_context" or work["collection"] == "immediate_parent"):
            fail.add("context_incompatible")
    elif surface == "codex_user_owned_thread":
        if not (work["interaction"] == "user_required" or work["collection"] == "user_continuation"):
            fail.add("interaction_incompatible")
    elif surface == "codex_subagent":
        _require(fail, work["interaction"] == "none", "interaction_incompatible")
        _require(fail, work["duration"] in {"short", "bounded"}, "duration_incompatible")
        _require(fail, work["persistence"] == "turn_bound", "persistence_incompatible")
        _require(fail, work["resume"] == "not_needed", "resume_incompatible")
        _require(fail, work["dependency"] in {"none", "soft"}, "dependency_incompatible")
        _require(fail, work["collection"] == "immediate_parent", "collection_incompatible")
        _require(fail, authority["impact"] in {"low", "medium"}, "impact_incompatible")
        _require(fail, not ({"external_state", "destructive"} & mutation), "mutation_boundary_incompatible")
    elif surface == "cbr_batch":
        _require(fail, work["interaction"] == "none", "interaction_incompatible")
        _require(fail, work["kind"] not in {"architecture_policy", "discussion"}, "work_kind_incompatible")
        _require(fail, any((work["duration"] == "long", work["persistence"] == "durable", work["resume"] == "required", work["dependency"] == "hard", work["collection"] == "durable_attention")), "persistence_incompatible")
        _require(fail, authority["impact"] in {"low", "medium"}, "impact_incompatible")
        _require(fail, not ({"external_state", "destructive"} & mutation), "mutation_boundary_incompatible")
    elif surface == "external_worker":
        _require(fail, work["interaction"] == "none", "interaction_incompatible")
        _require(fail, work["external_worker_boundary"] == "verified_bounded", "external_worker_boundary_unverified")
        _require(fail, work["verification"] in {"objective", "mixed"}, "verification_incompatible")
        _require(fail, authority["impact"] in {"low", "medium"}, "impact_incompatible")
        _require(fail, not ({"external_state", "destructive"} & mutation), "mutation_boundary_incompatible")
    return [code for code in EXCLUSION_ORDER if code in fail]


def _plan_base(request_id: str, fingerprint: str, collection_owner: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    constraints = {"decision_authority": manifest["authority"]["decision_authority"], "allowed_mutation_classes": manifest["mutation"]["allowed"], "prohibited_mutation_classes": manifest["mutation"]["prohibited"], "automation_boundary": manifest["automation_boundary"]}
    return {"schema_version": 1, "contract": PLAN_CONTRACT, "request_id": request_id, "request_fingerprint": fingerprint, "collection_owner": collection_owner, "execution_constraints": constraints, "mutation": {"allowed": False, "applied": False}}


def _complete(base: dict[str, Any], status: str, recommended: str | None, fallbacks: list[str], excluded: list[dict[str, Any]], reasons: list[str], unresolved: list[str], preflight: list[str]) -> dict[str, Any]:
    return {**base, "decision_status": status, "recommended_surface": recommended, "fallback_surfaces": fallbacks, "reason_codes": reasons, "excluded_surfaces": excluded, "unresolved_constraints": unresolved, "required_preflight": preflight}


def _selection_reason(surface: str) -> str:
    return {"codex_parent_thread": "selected_parent_thread", "codex_user_owned_thread": "selected_user_owned_thread", "codex_subagent": "selected_subagent", "cbr_batch": "selected_cbr_batch", "external_worker": "selected_external_worker"}[surface]


def _preflight(surface: str) -> list[str]:
    return {"codex_parent_thread": [], "codex_user_owned_thread": ["confirm_user_continuation"], "codex_subagent": ["verify_immediate_parent_collection"], "cbr_batch": ["verify_cbr_admission"], "external_worker": ["verify_external_worker_contract"]}[surface]


def _fingerprint(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _object(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OrchestrationManifestError(code)
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str):
        raise OrchestrationManifestError("value_type_invalid")
    if not SAFE_ID.fullmatch(value):
        raise OrchestrationManifestError("unsafe_identifier")
    return value


def _enum(path: str, value: object) -> None:
    if not isinstance(value, str):
        raise OrchestrationManifestError("value_type_invalid")
    if value not in ENUMS[path]:
        raise OrchestrationManifestError("value_enum_invalid")


def _enum_list(value: object, allowed: set[str]) -> list[str]:
    if not isinstance(value, list):
        raise OrchestrationManifestError("value_type_invalid")
    if len(value) > 8:
        raise OrchestrationManifestError("value_bounds_invalid")
    if any(not isinstance(item, str) for item in value):
        raise OrchestrationManifestError("value_type_invalid")
    if any(item not in allowed for item in value):
        raise OrchestrationManifestError("value_enum_invalid")
    if len(value) != len(set(value)):
        raise OrchestrationManifestError("duplicate_list_item")
    return list(value)


def _summary_text(value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 512:
        if not isinstance(value, str):
            raise OrchestrationManifestError("value_type_invalid")
        raise OrchestrationManifestError("value_bounds_invalid")


def _require(failures: set[str], condition: bool, code: str) -> None:
    if not condition:
        failures.add(code)


def _ordered_mutations(values: list[str]) -> list[str]:
    return [item for item in MUTATION_ORDER if item in values]


def _nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc(item) for item in value]
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", key) if isinstance(key, str) else key: _nfc(item)
            for key, item in value.items()
        }
    return value

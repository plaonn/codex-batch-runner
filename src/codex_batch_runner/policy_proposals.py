from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from typing import Any

from .config import Config
from .doctor import execution_target_freshness_metadata
from .timeutil import utc_now
from .transcript import sanitize

SCHEMA_VERSION = 1
REPORT_KIND = "policy_proposal_report"
PREVIEW_KIND = "policy_proposal_preview"
APPROVAL_TEMPLATE_KIND = "policy_proposal_approval_template"
APPROVAL_VALIDATION_KIND = "policy_proposal_approval_validation"
EXECUTION_TARGET_FRESHNESS_CLASS = "execution_target_freshness"
READ_ONLY_MODE = "read_only"
PROHIBITED_STATE_CHANGES = [
    "apply",
    "config_rewrite",
    "task_mutation",
    "model_replacement",
    "rule_replacement",
]
PREVIEW_BLOCKED_REASON = "preview_only_no_apply_target"


def build_execution_target_freshness_proposal_report(config: Config) -> dict[str, Any]:
    items = execution_target_freshness_items(config)
    proposals = [proposal_from_item(item) for item in items if item.get("proposal_id")]
    counts = Counter(item["freshness_status"] for item in items)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "proposal_class": EXECUTION_TARGET_FRESHNESS_CLASS,
        "mode": READ_ONLY_MODE,
        "generated_at": utc_now().isoformat(),
        "mutation": {
            "allowed": False,
            "applied": False,
            "prohibited_state_changes": PROHIBITED_STATE_CHANGES,
        },
        "summary": {
            "targets_checked": len(items),
            "fresh": counts.get("fresh", 0),
            "stale": counts.get("stale", 0),
            "missing": counts.get("missing", 0),
            "proposal_count": len(proposals),
        },
        "items": items,
        "proposals": proposals,
        "warnings": [],
        "errors": [],
    }


def execution_target_freshness_items(config: Config) -> list[dict[str, Any]]:
    refs = selection_refs_by_target(config)
    items: list[dict[str, Any]] = []
    for alias in sorted(config.execution_targets):
        metadata = execution_target_freshness_metadata(config, alias)
        status = proposal_status(metadata)
        proposal_id = f"{EXECUTION_TARGET_FRESHNESS_CLASS}:{sanitize(alias)}" if status in {"missing", "stale"} else None
        item = {
            "target_alias": sanitize(alias),
            "selection_refs": refs.get(alias, []),
            "freshness_status": status,
            "freshness_reason": metadata.get("reason"),
            "last_reviewed_at": sanitize(metadata.get("last_reviewed_at")),
            "review_after_days": metadata.get("review_after_days") if isinstance(metadata.get("review_after_days"), int) else None,
            "review_due_at": metadata.get("review_due_at"),
            "checked_at": metadata.get("checked_at"),
            "proposal_id": proposal_id,
        }
        items.append(item)
    return items


def selection_refs_by_target(config: Config) -> dict[str, list[dict[str, str | None]]]:
    refs: dict[str, list[dict[str, str | None]]] = {}
    default_target = config.default_execution_config.get("execution_target")
    if isinstance(default_target, str) and default_target:
        refs.setdefault(default_target, []).append({"scope": "default_execution_config", "name": None})
    for rule in config.model_selection_rules:
        target = rule.get("execution_target")
        if not isinstance(target, str) or not target:
            continue
        refs.setdefault(target, []).append({"scope": "model_selection_rule", "name": sanitize(rule.get("name"))})
    return refs


def proposal_status(metadata: dict[str, Any]) -> str:
    status = metadata.get("status")
    if status == "stale":
        return "stale"
    if status == "fresh":
        return "fresh"
    return "missing"


def proposal_from_item(item: dict[str, Any]) -> dict[str, Any]:
    freshness_status = str(item["freshness_status"])
    action = "review_execution_target_freshness" if freshness_status == "stale" else "add_execution_target_freshness_metadata"
    severity = "warning" if freshness_status == "stale" else "info"
    return {
        "proposal_id": item["proposal_id"],
        "proposal_class": EXECUTION_TARGET_FRESHNESS_CLASS,
        "target_alias": item["target_alias"],
        "status": "open",
        "severity": severity,
        "reason": item.get("freshness_reason"),
        "recommended_action": action,
        "allowed_state_changes": ["none"],
        "prohibited_state_changes": PROHIBITED_STATE_CHANGES,
        "selection_refs": item.get("selection_refs") or [],
    }


def build_policy_proposal_preview(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return empty_policy_proposal_preview(errors=["proposal report must be a JSON object"])

    warnings: list[str] = []
    errors: list[str] = []
    source_schema_version = source.get("schema_version")
    source_kind = source.get("kind")
    proposal_class = source.get("proposal_class")
    if source_schema_version != SCHEMA_VERSION:
        errors.append("unsupported proposal report schema_version")
    if source_kind != REPORT_KIND:
        errors.append("unsupported proposal report kind")
    if proposal_class != EXECUTION_TARGET_FRESHNESS_CLASS:
        errors.append("unsupported proposal_class")

    raw_proposals = source.get("proposals")
    if not isinstance(raw_proposals, list):
        errors.append("proposal report proposals must be a list")
        raw_proposals = []

    items = []
    if not errors:
        for index, proposal in enumerate(raw_proposals):
            if not isinstance(proposal, dict):
                warnings.append(f"skipped non-object proposal at index {index}")
                continue
            items.append(preview_item_from_proposal(proposal))

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": PREVIEW_KIND,
        "source_schema_version": source_schema_version,
        "source_kind": source_kind,
        "proposal_class": proposal_class,
        "mode": READ_ONLY_MODE,
        "mutation": {
            "allowed": False,
            "applied": False,
            "prohibited_state_changes": PROHIBITED_STATE_CHANGES,
        },
        "summary": {
            "proposal_count": len(items),
            "apply_ready": 0,
            "blocked": len(items),
            "would_change": "none",
        },
        "items": items,
        "warnings": warnings,
        "errors": errors,
    }


def build_policy_proposal_approval_template(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return empty_policy_proposal_approval_template(errors=["policy proposal preview must be a JSON object"])

    warnings: list[str] = []
    errors: list[str] = []
    source_schema_version = source.get("schema_version")
    source_kind = source.get("kind")
    proposal_class = source.get("proposal_class")
    if source_schema_version != SCHEMA_VERSION:
        errors.append("unsupported policy proposal preview schema_version")
    if source_kind != PREVIEW_KIND:
        errors.append("unsupported policy proposal preview kind")
    if proposal_class != EXECUTION_TARGET_FRESHNESS_CLASS:
        errors.append("unsupported proposal_class")
    if source.get("errors"):
        errors.append("policy proposal preview contains errors")

    raw_items = source.get("items")
    if not isinstance(raw_items, list):
        errors.append("policy proposal preview items must be a list")
        raw_items = []

    approvals = []
    if not errors:
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                warnings.append(f"skipped non-object preview item at index {index}")
                continue
            approvals.append(approval_template_item_from_preview_item(item))

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": APPROVAL_TEMPLATE_KIND,
        "source_schema_version": source_schema_version,
        "source_kind": source_kind,
        "source_preview_sha256": canonical_json_sha256(source),
        "proposal_class": proposal_class,
        "mode": READ_ONLY_MODE,
        "created_at": utc_now().isoformat(),
        "mutation": {
            "allowed": False,
            "applied": False,
            "prohibited_state_changes": PROHIBITED_STATE_CHANGES,
        },
        "summary": {
            "proposal_count": len(approvals),
            "approved_count": 0,
            "pending_count": len(approvals),
        },
        "approvals": approvals,
        "warnings": warnings,
        "errors": errors,
    }


def build_policy_proposal_approval_validation(approval: Any, preview: Any) -> dict[str, Any]:
    if not isinstance(approval, dict):
        return empty_policy_proposal_approval_validation(errors=["policy proposal approval must be a JSON object"])
    if not isinstance(preview, dict):
        return empty_policy_proposal_approval_validation(errors=["policy proposal preview must be a JSON object"])

    warnings: list[str] = []
    errors: list[str] = []
    approval_schema_version = approval.get("schema_version")
    approval_kind = approval.get("kind")
    preview_schema_version = preview.get("schema_version")
    preview_kind = preview.get("kind")
    proposal_class = approval.get("proposal_class")
    if approval_schema_version != SCHEMA_VERSION:
        errors.append("unsupported approval schema_version")
    if approval_kind != APPROVAL_TEMPLATE_KIND:
        errors.append("unsupported approval kind")
    if preview_schema_version != SCHEMA_VERSION:
        errors.append("unsupported preview schema_version")
    if preview_kind != PREVIEW_KIND:
        errors.append("unsupported preview kind")
    if proposal_class != EXECUTION_TARGET_FRESHNESS_CLASS:
        errors.append("unsupported proposal_class")
    if preview.get("proposal_class") != proposal_class:
        errors.append("approval proposal_class does not match preview")
    if approval.get("source_preview_sha256") != canonical_json_sha256(preview):
        errors.append("source_preview_sha256 mismatch")
    if approval.get("errors"):
        errors.append("approval contains errors")
    if preview.get("errors"):
        errors.append("preview contains errors")

    preview_items = preview.get("items")
    if not isinstance(preview_items, list):
        errors.append("preview items must be a list")
        preview_items = []
    approvals = approval.get("approvals")
    if not isinstance(approvals, list):
        errors.append("approval approvals must be a list")
        approvals = []

    preview_by_proposal_id, duplicate_preview_ids = preview_items_by_proposal_id(preview_items)
    for proposal_id in duplicate_preview_ids:
        errors.append(f"duplicate preview proposal_id: {proposal_id}")

    items = []
    if not errors:
        for index, approval_item in enumerate(approvals):
            if not isinstance(approval_item, dict):
                warnings.append(f"skipped non-object approval at index {index}")
                continue
            items.append(validate_approval_item(approval_item, preview_by_proposal_id))

    item_error_count = sum(1 for item in items if item.get("errors"))
    approved_count = sum(1 for item in items if item.get("approved") is True)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": APPROVAL_VALIDATION_KIND,
        "approval_schema_version": approval_schema_version,
        "approval_kind": approval_kind,
        "preview_schema_version": preview_schema_version,
        "preview_kind": preview_kind,
        "proposal_class": proposal_class,
        "mode": READ_ONLY_MODE,
        "valid": not errors and item_error_count == 0,
        "mutation": {
            "allowed": False,
            "applied": False,
            "prohibited_state_changes": PROHIBITED_STATE_CHANGES,
        },
        "summary": {
            "approval_count": len(items),
            "approved_count": approved_count,
            "pending_count": len(items) - approved_count,
            "valid_approved_count": sum(
                1 for item in items if item.get("approved") is True and not item.get("errors")
            ),
            "invalid_count": item_error_count,
        },
        "items": items,
        "warnings": warnings,
        "errors": errors,
    }


def empty_policy_proposal_approval_validation(*, errors: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": APPROVAL_VALIDATION_KIND,
        "approval_schema_version": None,
        "approval_kind": None,
        "preview_schema_version": None,
        "preview_kind": None,
        "proposal_class": None,
        "mode": READ_ONLY_MODE,
        "valid": False,
        "mutation": {
            "allowed": False,
            "applied": False,
            "prohibited_state_changes": PROHIBITED_STATE_CHANGES,
        },
        "summary": {
            "approval_count": 0,
            "approved_count": 0,
            "pending_count": 0,
            "valid_approved_count": 0,
            "invalid_count": 0,
        },
        "items": [],
        "warnings": [],
        "errors": errors,
    }


def preview_items_by_proposal_id(items: list[Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    by_id: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        proposal_id = sanitize(item.get("proposal_id"))
        if not proposal_id:
            continue
        if proposal_id in by_id:
            duplicates.append(proposal_id)
            continue
        by_id[proposal_id] = item
    return by_id, duplicates


def validate_approval_item(
    approval_item: dict[str, Any], preview_by_proposal_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    proposal_id = sanitize(approval_item.get("proposal_id"))
    preview_item = preview_by_proposal_id.get(proposal_id)
    approved = approval_item.get("approved")
    item_errors: list[str] = []
    if not isinstance(approved, bool):
        item_errors.append("approved must be boolean")
        approved = False
    if preview_item is None:
        item_errors.append("proposal_id not found in preview")
    expected_item_hash = canonical_json_sha256(preview_item) if preview_item is not None else None
    source_item_hash = approval_item.get("source_item_sha256")
    if source_item_hash != expected_item_hash:
        item_errors.append("source_item_sha256 mismatch")
    if preview_item is not None:
        for field in ("proposal_class", "target_alias", "target", "recommended_action"):
            if sanitize(approval_item.get(field)) != sanitize(preview_item.get(field)):
                item_errors.append(f"{field} does not match preview")

    reviewer = approval_item.get("reviewer")
    reviewed_at = approval_item.get("reviewed_at")
    decision_note = approval_item.get("decision_note")
    reviewer_present = isinstance(reviewer, str) and bool(reviewer.strip())
    reviewed_at_valid = isinstance(reviewed_at, str) and iso_datetime_is_valid(reviewed_at)
    decision_note_present = isinstance(decision_note, str) and bool(decision_note.strip())
    if approved:
        if not reviewer_present:
            item_errors.append("approved item requires reviewer")
        if not reviewed_at_valid:
            item_errors.append("approved item requires reviewed_at ISO datetime")
        if not decision_note_present:
            item_errors.append("approved item requires decision_note")

    return {
        "proposal_id": proposal_id,
        "proposal_class": sanitize(approval_item.get("proposal_class")),
        "target_alias": sanitize(approval_item.get("target_alias")),
        "target": sanitize(approval_item.get("target")),
        "recommended_action": sanitize(approval_item.get("recommended_action")),
        "approved": approved,
        "validation_status": "invalid" if item_errors else ("approved" if approved else "pending"),
        "preview_item_found": preview_item is not None,
        "source_item_sha256_matches": source_item_hash == expected_item_hash,
        "reviewer_present": reviewer_present,
        "reviewed_at_valid": reviewed_at_valid,
        "decision_note_present": decision_note_present,
        "errors": item_errors,
    }


def iso_datetime_is_valid(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def empty_policy_proposal_approval_template(*, errors: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": APPROVAL_TEMPLATE_KIND,
        "source_schema_version": None,
        "source_kind": None,
        "source_preview_sha256": None,
        "proposal_class": None,
        "mode": READ_ONLY_MODE,
        "created_at": utc_now().isoformat(),
        "mutation": {
            "allowed": False,
            "applied": False,
            "prohibited_state_changes": PROHIBITED_STATE_CHANGES,
        },
        "summary": {
            "proposal_count": 0,
            "approved_count": 0,
            "pending_count": 0,
        },
        "approvals": [],
        "warnings": [],
        "errors": errors,
    }


def approval_template_item_from_preview_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": sanitize(item.get("proposal_id")),
        "proposal_class": sanitize(item.get("proposal_class")),
        "target_alias": sanitize(item.get("target_alias")),
        "target": sanitize(item.get("target")),
        "recommended_action": sanitize(item.get("recommended_action")),
        "source_item_sha256": canonical_json_sha256(item),
        "approved": False,
        "reviewer": None,
        "reviewed_at": None,
        "decision_note": None,
    }


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def empty_policy_proposal_preview(*, errors: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": PREVIEW_KIND,
        "source_schema_version": None,
        "source_kind": None,
        "proposal_class": None,
        "mode": READ_ONLY_MODE,
        "mutation": {
            "allowed": False,
            "applied": False,
            "prohibited_state_changes": PROHIBITED_STATE_CHANGES,
        },
        "summary": {
            "proposal_count": 0,
            "apply_ready": 0,
            "blocked": 0,
            "would_change": "none",
        },
        "items": [],
        "warnings": [],
        "errors": errors,
    }


def preview_item_from_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    target_alias = sanitize(proposal.get("target_alias"))
    selection_refs = proposal.get("selection_refs")
    return {
        "proposal_id": sanitize(proposal.get("proposal_id")),
        "proposal_class": sanitize(proposal.get("proposal_class")),
        "target_alias": target_alias,
        "status": sanitize(proposal.get("status")),
        "severity": sanitize(proposal.get("severity")),
        "reason": sanitize(proposal.get("reason")),
        "recommended_action": sanitize(proposal.get("recommended_action")),
        "target": f"execution_targets.{target_alias}.freshness",
        "would_change": "none",
        "apply_ready": False,
        "blocked_reason": PREVIEW_BLOCKED_REASON,
        "selection_refs": sanitize_selection_refs(selection_refs) if isinstance(selection_refs, list) else [],
    }


def sanitize_selection_refs(refs: list[Any]) -> list[dict[str, str | None]]:
    sanitized_refs: list[dict[str, str | None]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        name = ref.get("name")
        sanitized_refs.append(
            {
                "scope": sanitize(ref.get("scope")),
                "name": sanitize(name) if name is not None else None,
            }
        )
    return sanitized_refs


def render_execution_target_freshness_proposal_report(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "cbr policy-proposals execution-target-freshness",
        f"mode: {report.get('mode')}",
        f"proposal_class: {report.get('proposal_class')}",
        f"targets_checked: {summary.get('targets_checked')}",
        f"proposal_count: {summary.get('proposal_count')}",
        f"fresh: {summary.get('fresh')} stale: {summary.get('stale')} missing: {summary.get('missing')}",
        "mutation: allowed=false applied=false",
    ]
    items = report.get("items") or []
    if items:
        lines.append("items:")
        for item in items:
            refs = format_selection_refs(item.get("selection_refs") or [])
            lines.append(
                "  - "
                f"target={item.get('target_alias')} "
                f"freshness={item.get('freshness_status')}({item.get('freshness_reason')}) "
                f"due={item.get('review_due_at') or '-'} "
                f"refs={refs} "
                f"proposal={item.get('proposal_id') or '-'}"
            )
    proposals = report.get("proposals") or []
    if proposals:
        lines.append("proposals:")
        for proposal in proposals:
            lines.append(
                "  - "
                f"{proposal.get('proposal_id')} "
                f"action={proposal.get('recommended_action')} "
                f"state_changes=none"
            )
    return "\n".join(lines) + "\n"


def render_policy_proposal_preview(preview: dict[str, Any]) -> str:
    summary = preview.get("summary") or {}
    lines = [
        "cbr policy-proposals preview",
        f"schema_version: {preview.get('schema_version')}",
        f"source_schema_version: {preview.get('source_schema_version')}",
        f"proposal_class: {preview.get('proposal_class')}",
        f"mode: {preview.get('mode')}",
        f"proposal_count: {summary.get('proposal_count')}",
        "mutation: allowed=false applied=false",
    ]
    errors = preview.get("errors") or []
    if errors:
        lines.append("errors:")
        for error in errors:
            lines.append(f"  - {error}")
    items = preview.get("items") or []
    if items:
        lines.append("items:")
        for index, item in enumerate(items, start=1):
            lines.extend(
                [
                    f"  {index}. {item.get('proposal_id')}",
                    f"     action: {item.get('recommended_action')}",
                    f"     target: {item.get('target')}",
                    f"     would_change: {item.get('would_change')}",
                    f"     apply_ready: {str(item.get('apply_ready')).lower()}",
                    f"     reason: {item.get('reason')}",
                ]
            )
    return "\n".join(lines) + "\n"


def render_policy_proposal_approval_template(template: dict[str, Any]) -> str:
    summary = template.get("summary") or {}
    lines = [
        "cbr policy-proposals approval-template",
        f"schema_version: {template.get('schema_version')}",
        f"source_schema_version: {template.get('source_schema_version')}",
        f"source_preview_sha256: {template.get('source_preview_sha256')}",
        f"proposal_class: {template.get('proposal_class')}",
        f"mode: {template.get('mode')}",
        f"proposal_count: {summary.get('proposal_count')}",
        f"approved_count: {summary.get('approved_count')}",
        "mutation: allowed=false applied=false",
    ]
    errors = template.get("errors") or []
    if errors:
        lines.append("errors:")
        for error in errors:
            lines.append(f"  - {error}")
    approvals = template.get("approvals") or []
    if approvals:
        lines.append("approvals:")
        for index, approval in enumerate(approvals, start=1):
            lines.extend(
                [
                    f"  {index}. {approval.get('proposal_id')}",
                    f"     action: {approval.get('recommended_action')}",
                    f"     target: {approval.get('target')}",
                    f"     approved: {str(approval.get('approved')).lower()}",
                    f"     source_item_sha256: {approval.get('source_item_sha256')}",
                ]
            )
    return "\n".join(lines) + "\n"


def render_policy_proposal_approval_validation(validation: dict[str, Any]) -> str:
    summary = validation.get("summary") or {}
    lines = [
        "cbr policy-proposals validate-approval",
        f"schema_version: {validation.get('schema_version')}",
        f"proposal_class: {validation.get('proposal_class')}",
        f"mode: {validation.get('mode')}",
        f"valid: {str(validation.get('valid')).lower()}",
        f"approval_count: {summary.get('approval_count')}",
        f"approved_count: {summary.get('approved_count')}",
        f"valid_approved_count: {summary.get('valid_approved_count')}",
        f"invalid_count: {summary.get('invalid_count')}",
        "mutation: allowed=false applied=false",
    ]
    errors = validation.get("errors") or []
    if errors:
        lines.append("errors:")
        for error in errors:
            lines.append(f"  - {error}")
    items = validation.get("items") or []
    if items:
        lines.append("items:")
        for index, item in enumerate(items, start=1):
            lines.extend(
                [
                    f"  {index}. {item.get('proposal_id')}",
                    f"     status: {item.get('validation_status')}",
                    f"     approved: {str(item.get('approved')).lower()}",
                    f"     target: {item.get('target')}",
                    f"     source_item_sha256_matches: {str(item.get('source_item_sha256_matches')).lower()}",
                ]
            )
            for error in item.get("errors") or []:
                lines.append(f"     error: {error}")
    return "\n".join(lines) + "\n"


def format_selection_refs(refs: list[dict[str, Any]]) -> str:
    if not refs:
        return "-"
    parts = []
    for ref in refs:
        scope = ref.get("scope")
        name = ref.get("name")
        parts.append(str(scope) if not name else f"{scope}:{name}")
    return ",".join(parts)

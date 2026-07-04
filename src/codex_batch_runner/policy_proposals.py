from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .doctor import execution_target_freshness_metadata
from .fs import write_json_atomic
from .timeutil import utc_now
from .transcript import sanitize

SCHEMA_VERSION = 1
REPORT_KIND = "policy_proposal_report"
PREVIEW_KIND = "policy_proposal_preview"
APPROVAL_TEMPLATE_KIND = "policy_proposal_approval_template"
APPROVAL_VALIDATION_KIND = "policy_proposal_approval_validation"
APPLY_KIND = "policy_proposal_apply"
EXECUTION_TARGET_FRESHNESS_CLASS = "execution_target_freshness"
DIRECT_MODEL_PIN_MIGRATION_CLASS = "direct_model_pin_migration"
READ_ONLY_MODE = "read_only"
DRY_RUN_MODE = "dry_run"
APPLY_MODE = "apply"
DEFAULT_FRESHNESS_REVIEW_AFTER_DAYS = 14
PROHIBITED_STATE_CHANGES = [
    "apply",
    "config_rewrite",
    "task_mutation",
    "model_replacement",
    "rule_replacement",
]
PREVIEW_BLOCKED_REASON = "preview_only_no_apply_target"
SUPPORTED_APPLY_ACTIONS = {
    "review_execution_target_freshness": "stale",
    "add_execution_target_freshness_metadata": "missing",
}


def build_execution_target_freshness_proposal_report(config: Config) -> dict[str, Any]:
    items = execution_target_freshness_items(config)
    proposals = [proposal_from_item(item) for item in items if item.get("proposal_id")]
    decision_cards = [decision_card_from_policy_proposal(proposal) for proposal in proposals]
    counts = Counter(item["freshness_status"] for item in items)
    target_count = sum(1 for item in items if item.get("target_kind") == "execution_target")
    direct_pin_count = sum(1 for item in items if item.get("target_kind") == "direct_model_pin")
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
            "targets_checked": target_count,
            "execution_targets_checked": target_count,
            "direct_model_pins": direct_pin_count,
            "fresh": counts.get("fresh", 0),
            "stale": counts.get("stale", 0),
            "missing": counts.get("missing", 0),
            "proposal_count": len(proposals),
            "decision_card_count": len(decision_cards),
            "decision_required_count": sum(
                1 for card in decision_cards if card.get("user_decision_status") == "decision_required"
            ),
        },
        "items": items,
        "proposals": proposals,
        "decision_cards": decision_cards,
        "warnings": [],
        "errors": [],
    }


def build_direct_model_pin_migration_proposal_report(config: Config) -> dict[str, Any]:
    items = direct_model_pin_freshness_items(config)
    proposals = [direct_model_pin_migration_proposal_from_item(item) for item in items]
    decision_cards = [decision_card_from_policy_proposal(proposal) for proposal in proposals]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "proposal_class": DIRECT_MODEL_PIN_MIGRATION_CLASS,
        "mode": READ_ONLY_MODE,
        "generated_at": utc_now().isoformat(),
        "mutation": {
            "allowed": False,
            "applied": False,
            "prohibited_state_changes": PROHIBITED_STATE_CHANGES,
        },
        "summary": {
            "direct_model_pins": len(items),
            "proposal_count": len(proposals),
            "decision_card_count": len(decision_cards),
            "approval_blocked_count": sum(
                1 for card in decision_cards if card.get("user_decision_status") == "approval_blocked"
            ),
            "decision_required_count": sum(
                1 for card in decision_cards if card.get("user_decision_status") == "decision_required"
            ),
        },
        "items": items,
        "proposals": proposals,
        "decision_cards": decision_cards,
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
            "target_kind": "execution_target",
            "target_alias": sanitize(alias),
            "target": f"execution_targets.{sanitize(alias)}.freshness",
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
    items.extend(direct_model_pin_freshness_items(config))
    return items


def direct_model_pin_freshness_items(config: Config) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    default_item = direct_model_pin_item(
        config.default_execution_config,
        scope="default_execution_config",
        name=None,
        target="default_execution_config.model",
    )
    if default_item:
        items.append(default_item)
    for rule in config.model_selection_rules:
        name = sanitize(rule.get("name"))
        item = direct_model_pin_item(
            rule,
            scope="model_selection_rule",
            name=name,
            target=f"model_selection_rules.{name}.model",
        )
        if item:
            items.append(item)
    return items


def direct_model_pin_item(
    selection: dict[str, Any],
    *,
    scope: str,
    name: str | None,
    target: str,
) -> dict[str, Any] | None:
    if selection.get("execution_target") or not selection.get("model"):
        return None
    selection_id = scope if name is None else f"{scope}.{name}"
    return {
        "target_kind": "direct_model_pin",
        "target_alias": selection_id,
        "target": target,
        "selection_refs": [{"scope": scope, "name": name}],
        "freshness_status": "missing",
        "freshness_reason": "direct_model_pin_without_execution_target",
        "last_reviewed_at": None,
        "review_after_days": None,
        "review_due_at": None,
        "checked_at": None,
        "proposal_id": f"{EXECUTION_TARGET_FRESHNESS_CLASS}:direct_model_pin:{sanitize(selection_id)}",
    }


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
    if item.get("target_kind") == "direct_model_pin":
        action = "migrate_direct_model_pin_to_execution_target"
    else:
        action = "review_execution_target_freshness" if freshness_status == "stale" else "add_execution_target_freshness_metadata"
    severity = "warning" if freshness_status == "stale" else "info"
    return {
        "proposal_id": item["proposal_id"],
        "proposal_class": EXECUTION_TARGET_FRESHNESS_CLASS,
        "target_kind": item.get("target_kind") or "execution_target",
        "target_alias": item["target_alias"],
        "target": item.get("target") or f"execution_targets.{item['target_alias']}.freshness",
        "status": "open",
        "severity": severity,
        "reason": item.get("freshness_reason"),
        "recommended_action": action,
        "allowed_state_changes": ["none"],
        "prohibited_state_changes": PROHIBITED_STATE_CHANGES,
        "selection_refs": item.get("selection_refs") or [],
    }


def direct_model_pin_migration_proposal_from_item(item: dict[str, Any]) -> dict[str, Any]:
    target_alias = sanitize(item.get("target_alias"))
    return {
        "proposal_id": f"{DIRECT_MODEL_PIN_MIGRATION_CLASS}:{target_alias}",
        "proposal_class": DIRECT_MODEL_PIN_MIGRATION_CLASS,
        "target_kind": "direct_model_pin",
        "target_alias": target_alias,
        "target": sanitize(item.get("target")),
        "status": "open",
        "severity": "info",
        "reason": sanitize(item.get("freshness_reason")),
        "recommended_action": "draft_execution_target_migration_proposal",
        "approval_required": True,
        "apply_ready": False,
        "blocked_reason": "direct_model_pin_requires_separate_migration_approval",
        "would_change": "none",
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
    decision_cards = [decision_card_from_policy_preview_item(item) for item in items]

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
            "decision_card_count": len(decision_cards),
            "decision_required_count": sum(
                1 for card in decision_cards if card.get("user_decision_status") == "decision_required"
            ),
        },
        "items": items,
        "decision_cards": decision_cards,
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
    decision_cards = [decision_card_from_approval_template_item(item) for item in approvals]

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
            "decision_card_count": len(decision_cards),
            "decision_pending_count": len(decision_cards),
        },
        "approvals": approvals,
        "decision_cards": decision_cards,
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
    decision_cards = [decision_card_from_approval_validation_item(item) for item in items]
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
            "decision_card_count": len(decision_cards),
            "decision_approved_count": sum(
                1 for card in decision_cards if card.get("user_decision_status") == "approved"
            ),
            "decision_invalid_count": sum(
                1 for card in decision_cards if card.get("user_decision_status") == "invalid"
            ),
        },
        "items": items,
        "decision_cards": decision_cards,
        "warnings": warnings,
        "errors": errors,
    }


def build_policy_proposal_apply_report(
    approval: Any,
    preview: Any,
    config_target_path: str,
    *,
    apply: bool,
    approve: bool,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    mode = APPLY_MODE if apply else DRY_RUN_MODE
    errors: list[str] = []
    warnings: list[str] = []
    validation = build_policy_proposal_approval_validation(approval, preview)
    if not validation.get("valid"):
        errors.append("approval validation failed")
    if apply and not approve:
        errors.append("--apply requires --approve")

    target_path = Path(config_target_path).expanduser()
    resolved_target_path = target_path.resolve(strict=False)
    target_guard = config_target_guard(resolved_target_path, repo_root=repo_root)
    errors.extend(target_guard["errors"])

    raw_config: dict[str, Any] | None = None
    loaded_config: Config | None = None
    config_sha256_before: str | None = None
    if not target_guard["errors"]:
        raw_config, loaded_config, config_sha256_before, load_errors = load_config_target(resolved_target_path)
        errors.extend(load_errors)

    approval_items = approvals_by_proposal_id(approval.get("approvals") if isinstance(approval, dict) else None)
    raw_preview_items = preview.get("items") if isinstance(preview, dict) else []
    preview_items = preview_items_by_proposal_id(raw_preview_items if isinstance(raw_preview_items, list) else [])[0]
    planned_items: list[dict[str, Any]] = []
    mutated_config = deepcopy(raw_config) if raw_config is not None else None
    if not errors and raw_config is not None and loaded_config is not None and mutated_config is not None:
        for validation_item in validation.get("items") or []:
            if not isinstance(validation_item, dict) or validation_item.get("approved") is not True:
                continue
            proposal_id = sanitize(validation_item.get("proposal_id"))
            approval_item = approval_items.get(proposal_id, {})
            preview_item = preview_items.get(proposal_id, {})
            planned = apply_item_plan(validation_item, approval_item, preview_item, loaded_config, mutated_config)
            planned_items.append(planned)

    item_error_count = sum(1 for item in planned_items if item.get("errors"))
    eligible = not errors and item_error_count == 0
    applied = False
    config_sha256_after = config_sha256_before
    if apply and eligible and raw_config is not None and mutated_config is not None:
        if non_freshness_config_changed(raw_config, mutated_config):
            errors.append("internal safety check failed: non-freshness config change detected")
            eligible = False
        else:
            write_json_atomic(resolved_target_path, mutated_config)
            config_sha256_after = canonical_json_sha256(mutated_config)
            applied = True
            for item in planned_items:
                item["applied"] = True

    approved_count = sum(1 for item in validation.get("items") or [] if isinstance(item, dict) and item.get("approved") is True)
    eligible_count = sum(1 for item in planned_items if item.get("eligible") is True)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": APPLY_KIND,
        "proposal_class": validation.get("proposal_class"),
        "mode": mode,
        "valid": eligible,
        "mutation": {
            "allowed": eligible,
            "applied": applied,
            "requires_approve": apply,
            "approved": bool(approve),
            "allowed_state_changes": ["execution_target_freshness_metadata"] if eligible else [],
            "prohibited_state_changes": [
                "task_mutation",
                "model_replacement",
                "rule_replacement",
                "routing_rule_rewrite",
            ],
        },
        "summary": {
            "approval_count": validation.get("summary", {}).get("approval_count", 0),
            "approved_count": approved_count,
            "eligible_count": eligible_count,
            "applied_count": len(planned_items) if applied else 0,
            "rejected_count": len(planned_items) - eligible_count + len(errors),
        },
        "config_target": {
            "supported": not target_guard["errors"],
            "classification": target_guard["classification"],
            "sha256_before": config_sha256_before,
            "sha256_after": config_sha256_after,
        },
        "source_preview_sha256": approval.get("source_preview_sha256") if isinstance(approval, dict) else None,
        "validation": validation,
        "items": planned_items,
        "audit": apply_audit_payload(
            validation=validation,
            planned_items=planned_items,
            config_sha256_before=config_sha256_before,
            config_sha256_after=config_sha256_after,
            applied=applied,
        ),
        "warnings": warnings + validation.get("warnings", []),
        "errors": errors,
    }


def config_target_guard(path: Path, *, repo_root: Path | None) -> dict[str, Any]:
    errors: list[str] = []
    classification = "external_json"
    if path.suffix != ".json":
        errors.append("unsupported config target path: expected a JSON file")

    root = repo_root.resolve() if repo_root is not None else Path.cwd().resolve()
    if path_is_relative_to(path, root):
        relative = path.relative_to(root)
        parts = relative.parts
        if parts and parts[0] == ".private":
            classification = "repo_private_json"
        elif parts and parts[0] == ".codex-batch-runner":
            errors.append("unsupported config target path: repo runtime state is not a mutable config target")
            classification = "repo_runtime_json"
        elif path.name.endswith(".local.json"):
            classification = "repo_local_json"
        else:
            errors.append("unsupported config target path: repo public files are not mutable config targets")
            classification = "repo_public_json"

    return {"classification": classification, "errors": errors}


def path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def load_config_target(path: Path) -> tuple[dict[str, Any] | None, Config | None, str | None, list[str]]:
    if not path.exists():
        return None, None, None, ["config target does not exist"]
    if not path.is_file():
        return None, None, None, ["config target is not a file"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, None, None, [f"failed to read config target: {exc}"]
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, None, None, [f"failed to parse config target JSON: {exc}"]
    if not isinstance(raw, dict):
        return None, None, None, ["config target JSON must be an object"]
    try:
        loaded = Config.load(str(path))
    except Exception as exc:
        return raw, None, canonical_json_sha256(raw), [f"config target is invalid: {exc}"]
    return raw, loaded, canonical_json_sha256(raw), []


def approvals_by_proposal_id(approvals: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(approvals, list):
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    for approval in approvals:
        if not isinstance(approval, dict):
            continue
        proposal_id = sanitize(approval.get("proposal_id"))
        if proposal_id and proposal_id not in by_id:
            by_id[proposal_id] = approval
    return by_id


def apply_item_plan(
    validation_item: dict[str, Any],
    approval_item: dict[str, Any],
    preview_item: dict[str, Any],
    config: Config,
    mutable_config: dict[str, Any],
) -> dict[str, Any]:
    proposal_id = sanitize(validation_item.get("proposal_id"))
    target_alias = sanitize(validation_item.get("target_alias"))
    target_path = sanitize(validation_item.get("target"))
    action = sanitize(validation_item.get("recommended_action"))
    item_errors: list[str] = []

    if validation_item.get("proposal_class") != EXECUTION_TARGET_FRESHNESS_CLASS:
        item_errors.append("unsupported proposal_class")
    if proposal_id != f"{EXECUTION_TARGET_FRESHNESS_CLASS}:{target_alias}":
        item_errors.append("proposal_id does not match target_alias")
    if target_path != f"execution_targets.{target_alias}.freshness":
        item_errors.append("unsupported target path")
    expected_status = SUPPORTED_APPLY_ACTIONS.get(action)
    if expected_status is None:
        item_errors.append("unsupported recommended_action")

    execution_targets = mutable_config.get("execution_targets")
    target_config = execution_targets.get(target_alias) if isinstance(execution_targets, dict) else None
    if not isinstance(target_config, dict):
        item_errors.append("missing target")
        target_config = {}

    metadata = execution_target_freshness_metadata(config, target_alias)
    current_status = proposal_status(metadata)
    current_reason = sanitize(metadata.get("reason"))
    preview_reason = sanitize(preview_item.get("reason"))
    if expected_status is not None and current_status != expected_status:
        item_errors.append(f"config target is dirty: expected freshness status {expected_status}, found {current_status}")
    if preview_reason and current_reason != preview_reason:
        item_errors.append(f"config target is dirty: expected freshness reason {preview_reason}, found {current_reason}")

    reviewer = str(approval_item.get("reviewer") or "").strip()
    reviewed_at = str(approval_item.get("reviewed_at") or "").strip()
    reviewed_date = reviewed_at_date(reviewed_at)
    if reviewed_date is None:
        item_errors.append("approved item requires reviewed_at ISO datetime")

    before_freshness = deepcopy(target_config.get("freshness") if isinstance(target_config.get("freshness"), dict) else {})
    after_freshness = deepcopy(before_freshness)
    if reviewed_date is not None:
        after_freshness["owner"] = reviewer
        after_freshness["last_reviewed_at"] = reviewed_date
        if not isinstance(after_freshness.get("review_after_days"), int):
            after_freshness["review_after_days"] = DEFAULT_FRESHNESS_REVIEW_AFTER_DAYS

    if not item_errors and isinstance(execution_targets, dict):
        execution_targets[target_alias]["freshness"] = after_freshness

    changed_keys = sorted(
        key for key in set(before_freshness) | set(after_freshness) if before_freshness.get(key) != after_freshness.get(key)
    )
    return {
        "proposal_id": proposal_id,
        "proposal_class": validation_item.get("proposal_class"),
        "target_alias": target_alias,
        "target": target_path,
        "recommended_action": action,
        "approved": True,
        "eligible": not item_errors,
        "applied": False,
        "current_freshness_status": current_status,
        "current_freshness_reason": current_reason,
        "before": {"freshness": before_freshness},
        "after": {"freshness": after_freshness},
        "diff": {
            "changed_keys": changed_keys,
            "only_execution_target_freshness_metadata": True,
        },
        "approved_metadata": {
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "decision_note_sha256": canonical_json_sha256(approval_item.get("decision_note")),
        },
        "errors": item_errors,
    }


def reviewed_at_date(value: str) -> str | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def non_freshness_config_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    before_without_freshness = deepcopy(before)
    after_without_freshness = deepcopy(after)
    for config_doc in (before_without_freshness, after_without_freshness):
        execution_targets = config_doc.get("execution_targets")
        if not isinstance(execution_targets, dict):
            continue
        for target in execution_targets.values():
            if isinstance(target, dict):
                target.pop("freshness", None)
    return before_without_freshness != after_without_freshness


def apply_audit_payload(
    *,
    validation: dict[str, Any],
    planned_items: list[dict[str, Any]],
    config_sha256_before: str | None,
    config_sha256_after: str | None,
    applied: bool,
) -> dict[str, Any]:
    return {
        "event_type": "policy_proposal_apply",
        "source": "policy-proposals apply",
        "proposal_class": validation.get("proposal_class"),
        "applied": applied,
        "config_target": {
            "sha256_before": config_sha256_before,
            "sha256_after": config_sha256_after,
        },
        "items": [
            {
                "proposal_id": item.get("proposal_id"),
                "target_alias": item.get("target_alias"),
                "recommended_action": item.get("recommended_action"),
                "eligible": item.get("eligible"),
                "applied": item.get("applied"),
                "changed_keys": item.get("diff", {}).get("changed_keys"),
                "reviewer": item.get("approved_metadata", {}).get("reviewer"),
                "reviewed_at": item.get("approved_metadata", {}).get("reviewed_at"),
                "decision_note_sha256": item.get("approved_metadata", {}).get("decision_note_sha256"),
            }
            for item in planned_items
        ],
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
        "target_kind": sanitize(proposal.get("target_kind")) or "execution_target",
        "target_alias": target_alias,
        "status": sanitize(proposal.get("status")),
        "severity": sanitize(proposal.get("severity")),
        "reason": sanitize(proposal.get("reason")),
        "recommended_action": sanitize(proposal.get("recommended_action")),
        "target": sanitize(proposal.get("target")) or f"execution_targets.{target_alias}.freshness",
        "would_change": "none",
        "apply_ready": False,
        "blocked_reason": PREVIEW_BLOCKED_REASON,
        "selection_refs": sanitize_selection_refs(selection_refs) if isinstance(selection_refs, list) else [],
    }


def decision_card_from_policy_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    proposal_class = sanitize(proposal.get("proposal_class")) or EXECUTION_TARGET_FRESHNESS_CLASS
    decision_axis = (
        "direct_model_pin_migration"
        if proposal_class == DIRECT_MODEL_PIN_MIGRATION_CLASS
        else "execution_target_freshness"
    )
    target_kind = sanitize(proposal.get("target_kind")) or "execution_target"
    blocked_reason = None
    if target_kind == "direct_model_pin":
        user_decision_status = "approval_blocked"
        recommendation = "create_bounded_migration_proposal"
        question = "Decide whether to draft a separate bounded migration proposal for this direct model pin."
        explanation = (
            "The source uses a direct model pin, so freshness metadata cannot be applied safely to this target. "
            "A separate operator-approved migration proposal is required before any model or rule change."
        )
        allowed_decisions = ["draft_migration_proposal", "continue_observing", "reject_candidate"]
        blocked_reason = "direct_model_pin_requires_separate_migration_approval"
    else:
        user_decision_status = "decision_required"
        recommendation = "operator_review"
        question = "Review whether to approve this execution target freshness metadata update."
        explanation = (
            "The proposal is ready for human review of local/private freshness metadata only. "
            "Approval does not change models, routing rules, providers, task state, or central runner config."
        )
        allowed_decisions = ["approve_freshness_metadata_update", "continue_observing", "reject_candidate"]
    return {
        "card_id": "decision-card:" + sanitize(proposal.get("proposal_id")),
        "proposal_id": sanitize(proposal.get("proposal_id")),
        "proposal_class": proposal_class,
        "decision_axis": decision_axis,
        "execution_task_status": "proposal_reported",
        "user_decision_status": user_decision_status,
        "target_kind": target_kind,
        "target_alias": sanitize(proposal.get("target_alias")),
        "target": sanitize(proposal.get("target")),
        "question": question,
        "recommendation": recommendation,
        "explanation": explanation,
        "recommended_action": sanitize(proposal.get("recommended_action")),
        "reason": sanitize(proposal.get("reason")),
        "allowed_decisions": allowed_decisions,
        "prohibited_actions": [
            "auto_apply_policy",
            "change_model",
            "change_model_selection_rule",
            "change_provider",
            "change_central_runner_config",
            "mutate_task_state",
        ],
        "read_only": True,
        "mutation_allowed": False,
    } | ({"blocked_reason": blocked_reason} if blocked_reason else {})


def decision_card_from_policy_preview_item(item: dict[str, Any]) -> dict[str, Any]:
    card = decision_card_from_policy_proposal(item)
    if item.get("apply_ready") is False:
        card["preview_apply_ready"] = False
        card["preview_blocked_reason"] = sanitize(item.get("blocked_reason"))
    return card


def decision_card_from_approval_template_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "card_id": "decision-card:" + sanitize(item.get("proposal_id")),
        "proposal_id": sanitize(item.get("proposal_id")),
        "proposal_class": EXECUTION_TARGET_FRESHNESS_CLASS,
        "decision_axis": "execution_target_freshness",
        "execution_task_status": "approval_template_created",
        "user_decision_status": "decision_pending",
        "target_alias": sanitize(item.get("target_alias")),
        "target": sanitize(item.get("target")),
        "question": "Decide whether to approve this freshness metadata update in the approval template.",
        "recommendation": "operator_review",
        "explanation": (
            "This template records the operator decision fields only. "
            "It does not apply the proposal or change model, routing, provider, task, or runner config state."
        ),
        "recommended_action": sanitize(item.get("recommended_action")),
        "allowed_decisions": ["approve_with_reviewer_metadata", "leave_pending", "reject_candidate"],
        "prohibited_actions": [
            "auto_apply_policy",
            "change_model",
            "change_model_selection_rule",
            "change_provider",
            "change_central_runner_config",
            "mutate_task_state",
        ],
        "read_only": True,
        "mutation_allowed": False,
    }


def decision_card_from_approval_validation_item(item: dict[str, Any]) -> dict[str, Any]:
    errors = item.get("errors") if isinstance(item.get("errors"), list) else []
    if errors:
        user_decision_status = "invalid"
        recommendation = "fix_approval_metadata"
        explanation = (
            "The approval entry is not valid for guarded apply. "
            "Fix the approval metadata before using apply dry-run or apply."
        )
    elif item.get("approved") is True:
        user_decision_status = "approved"
        recommendation = "eligible_for_guarded_apply_dry_run"
        explanation = (
            "The approval entry is valid and may be used by the separate guarded apply dry-run/apply command. "
            "Validation itself remains read-only and does not change config or tasks."
        )
    else:
        user_decision_status = "not_approved"
        recommendation = "leave_unapplied"
        explanation = (
            "The approval entry is valid but not approved. "
            "No guarded apply item should be produced for this proposal."
        )
    return {
        "card_id": "decision-card:" + sanitize(item.get("proposal_id")),
        "proposal_id": sanitize(item.get("proposal_id")),
        "proposal_class": EXECUTION_TARGET_FRESHNESS_CLASS,
        "decision_axis": "execution_target_freshness",
        "execution_task_status": "approval_validated" if not errors else "approval_invalid",
        "user_decision_status": user_decision_status,
        "target_alias": sanitize(item.get("target_alias")),
        "target": sanitize(item.get("target")),
        "question": "Review the validated approval status for this freshness metadata proposal.",
        "recommendation": recommendation,
        "explanation": explanation,
        "recommended_action": sanitize(item.get("recommended_action")),
        "validation_status": sanitize(item.get("validation_status")),
        "validation_errors": [sanitize(error) for error in errors],
        "prohibited_actions": [
            "auto_apply_policy",
            "change_model",
            "change_model_selection_rule",
            "change_provider",
            "change_central_runner_config",
            "mutate_task_state",
        ],
        "read_only": True,
        "mutation_allowed": False,
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
                f"target={item.get('target')} "
                f"kind={item.get('target_kind') or 'execution_target'} "
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
    decision_cards = report.get("decision_cards") or []
    if decision_cards:
        lines.append("decision_cards:")
        lines.extend(render_policy_decision_cards(decision_cards))
    return "\n".join(lines) + "\n"


def render_direct_model_pin_migration_proposal_report(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "cbr policy-proposals direct-model-pin-migration",
        f"mode: {report.get('mode')}",
        f"proposal_class: {report.get('proposal_class')}",
        f"direct_model_pins: {summary.get('direct_model_pins')}",
        f"proposal_count: {summary.get('proposal_count')}",
        f"approval_blocked_count: {summary.get('approval_blocked_count')}",
        "mutation: allowed=false applied=false",
    ]
    items = report.get("items") or []
    if items:
        lines.append("items:")
        for item in items:
            refs = format_selection_refs(item.get("selection_refs") or [])
            lines.append(
                "  - "
                f"target={item.get('target')} "
                f"kind={item.get('target_kind')} "
                f"reason={item.get('freshness_reason')} "
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
                f"blocked_reason={proposal.get('blocked_reason')} "
                f"state_changes=none"
            )
    decision_cards = report.get("decision_cards") or []
    if decision_cards:
        lines.append("decision_cards:")
        lines.extend(render_policy_decision_cards(decision_cards))
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
    decision_cards = preview.get("decision_cards") or []
    if decision_cards:
        lines.append("decision_cards:")
        lines.extend(render_policy_decision_cards(decision_cards))
    return "\n".join(lines) + "\n"


def render_policy_decision_cards(cards: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for card in cards:
        lines.extend(
            [
                f"  - {card.get('card_id')}",
                f"     decision_axis: {card.get('decision_axis')}",
                f"     execution_task_status: {card.get('execution_task_status')}",
                f"     user_decision_status: {card.get('user_decision_status')}",
                f"     target: {card.get('target')}",
                f"     question: {card.get('question')}",
                f"     recommendation: {card.get('recommendation')}",
                f"     explanation: {card.get('explanation')}",
            ]
        )
        if card.get("blocked_reason"):
            lines.append(f"     blocked_reason: {card.get('blocked_reason')}")
        if card.get("preview_blocked_reason"):
            lines.append(f"     preview_blocked_reason: {card.get('preview_blocked_reason')}")
    return lines


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
    decision_cards = template.get("decision_cards") or []
    if decision_cards:
        lines.append("decision_cards:")
        lines.extend(render_policy_decision_cards(decision_cards))
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
    decision_cards = validation.get("decision_cards") or []
    if decision_cards:
        lines.append("decision_cards:")
        lines.extend(render_policy_decision_cards(decision_cards))
    return "\n".join(lines) + "\n"


def render_policy_proposal_apply_report(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    mutation = report.get("mutation") or {}
    config_target = report.get("config_target") or {}
    lines = [
        "cbr policy-proposals apply",
        f"schema_version: {report.get('schema_version')}",
        f"proposal_class: {report.get('proposal_class')}",
        f"mode: {report.get('mode')}",
        f"valid: {str(report.get('valid')).lower()}",
        f"approved_count: {summary.get('approved_count')}",
        f"eligible_count: {summary.get('eligible_count')}",
        f"applied_count: {summary.get('applied_count')}",
        f"mutation: allowed={str(mutation.get('allowed')).lower()} applied={str(mutation.get('applied')).lower()}",
        f"config_target_supported: {str(config_target.get('supported')).lower()}",
    ]
    errors = report.get("errors") or []
    if errors:
        lines.append("errors:")
        for error in errors:
            lines.append(f"  - {error}")
    items = report.get("items") or []
    if items:
        lines.append("items:")
        for index, item in enumerate(items, start=1):
            changed_keys = ",".join(item.get("diff", {}).get("changed_keys") or []) or "-"
            lines.extend(
                [
                    f"  {index}. {item.get('proposal_id')}",
                    f"     action: {item.get('recommended_action')}",
                    f"     target: {item.get('target')}",
                    f"     eligible: {str(item.get('eligible')).lower()}",
                    f"     applied: {str(item.get('applied')).lower()}",
                    f"     changed_keys: {changed_keys}",
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

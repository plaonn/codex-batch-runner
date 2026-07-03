from __future__ import annotations

from collections import Counter
from typing import Any

from .config import Config
from .doctor import execution_target_freshness_metadata
from .timeutil import utc_now
from .transcript import sanitize

SCHEMA_VERSION = 1
REPORT_KIND = "policy_proposal_report"
EXECUTION_TARGET_FRESHNESS_CLASS = "execution_target_freshness"
READ_ONLY_MODE = "read_only"
PROHIBITED_STATE_CHANGES = [
    "apply",
    "config_rewrite",
    "task_mutation",
    "model_replacement",
    "rule_replacement",
]


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


def format_selection_refs(refs: list[dict[str, Any]]) -> str:
    if not refs:
        return "-"
    parts = []
    for ref in refs:
        scope = ref.get("scope")
        name = ref.get("name")
        parts.append(str(scope) if not name else f"{scope}:{name}")
    return ",".join(parts)

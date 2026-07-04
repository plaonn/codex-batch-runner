from __future__ import annotations

from collections import Counter
from typing import Any

from .config import Config
from .policy_proposals import build_execution_target_freshness_proposal_report
from .routing_policy_candidates import build_routing_policy_candidate_report
from .routing_report import DEFAULT_ROUTING_REPORT_LIMIT, render_table


DEFAULT_DECISION_CARD_LIMIT = DEFAULT_ROUTING_REPORT_LIMIT


def build_decision_card_inventory(
    config: Config,
    *,
    project_id: str | None = None,
    project_root: str | None = None,
    category: str | None = None,
    label: str | None = None,
    limit: int = DEFAULT_DECISION_CARD_LIMIT,
    include_archived: bool = False,
    execution_evidence_records: list[dict[str, Any]] | None = None,
    include_observations: bool = False,
) -> dict[str, Any]:
    policy_report = build_execution_target_freshness_proposal_report(config)
    routing_report = build_routing_policy_candidate_report(
        config,
        project_id=project_id,
        project_root=project_root,
        category=category,
        label=label,
        limit=limit,
        include_archived=include_archived,
        execution_evidence_records=execution_evidence_records,
        include_non_reviewable=include_observations,
    )
    cards = _inventory_cards("policy-proposals execution-target-freshness", policy_report)
    cards.extend(_inventory_cards("routing-policy-candidates", routing_report))
    if not include_observations:
        cards = [
            card
            for card in cards
            if card.get("user_decision_status") in {"decision_required", "approval_blocked"}
        ]
    status_counts = Counter(str(card.get("user_decision_status") or "unknown") for card in cards)
    axis_counts = Counter(str(card.get("decision_axis") or "unknown") for card in cards)
    return {
        "kind": "decision_card_inventory",
        "read_only": True,
        "mutation_allowed": False,
        "filters": routing_report.get("filters"),
        "summary": {
            "card_count": len(cards),
            "decision_required": status_counts.get("decision_required", 0),
            "approval_blocked": status_counts.get("approval_blocked", 0),
            "decision_pending": status_counts.get("decision_pending", 0),
            "approved": status_counts.get("approved", 0),
            "not_ready": status_counts.get("not_ready", 0),
            "invalid": status_counts.get("invalid", 0),
            "include_observations": include_observations,
            "by_status": dict(sorted(status_counts.items())),
            "by_axis": dict(sorted(axis_counts.items())),
        },
        "source_reports": [
            {
                "source": "policy-proposals execution-target-freshness",
                "card_count": len(_list_value(policy_report.get("decision_cards"))),
                "mutation_allowed": False,
            },
            {
                "source": "routing-policy-candidates",
                "card_count": len(_list_value(routing_report.get("decision_cards"))),
                "mutation_allowed": False,
            },
        ],
        "decision_cards": cards,
    }


def _inventory_cards(source: str, report: dict[str, Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for card in _list_value(report.get("decision_cards")):
        if not isinstance(card, dict):
            continue
        copied = dict(card)
        copied["source"] = source
        copied["read_only"] = True
        copied["mutation_allowed"] = False
        cards.append(copied)
    return cards


def render_decision_card_inventory(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# decision cards",
        "",
        "read_only: yes",
        "mutation_allowed: no",
        (
            "summary: "
            f"cards={summary.get('card_count', 0)} "
            f"decision_required={summary.get('decision_required', 0)} "
            f"approval_blocked={summary.get('approval_blocked', 0)} "
            f"not_ready={summary.get('not_ready', 0)}"
        ),
        "",
        render_decision_card_table(_list_value(report.get("decision_cards"))),
    ]
    return "\n".join(lines) + "\n"


def render_decision_card_table(cards: list[dict[str, Any]]) -> str:
    header = ["SOURCE", "CARD_ID", "AXIS", "EXECUTION_STATUS", "USER_STATUS", "RECOMMENDATION", "TARGET"]
    rows = [
        [
            str(card.get("source") or "-"),
            str(card.get("card_id") or "-"),
            str(card.get("decision_axis") or "-"),
            str(card.get("execution_task_status") or "-"),
            str(card.get("user_decision_status") or "-"),
            str(card.get("recommendation") or "-"),
            str(card.get("target") or card.get("task_bucket_key") or "-"),
        ]
        for card in cards
    ]
    return render_table(header, rows)


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []

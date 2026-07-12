from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import Config
from .queue import list_tasks
from .routing_cost_evidence import (
    RoutingCostEvidenceError,
    latest_routing_cost_evidence,
    validate_routing_cost_evidence,
)
from .routing_report import TASK_BUCKET_ADVISORY_THRESHOLDS, render_table
from .timeutil import iso_now


STATIC_FALLBACK_MODEL = "operator_baseline"
STATIC_FALLBACK_REASON = "insufficient_comparable_evidence"
EXCLUDED_DEFAULT_MODEL = "xhigh"


def load_routing_cost_evidence_records(paths: list[str]) -> list[dict[str, Any]]:
    """Read public-safe supplemental records without turning them into queue tasks."""
    records: list[dict[str, Any]] = []
    for raw_path in paths:
        try:
            payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RoutingCostEvidenceError(f"cannot read routing cost evidence JSON: {raw_path}") from exc
        values = payload.get("records") if isinstance(payload, dict) and "records" in payload else [payload]
        if not isinstance(values, list):
            raise RoutingCostEvidenceError("routing cost evidence JSON must be a record or a records list")
        for value in values:
            records.append(validate_routing_cost_evidence(value))
    return records


def build_routing_recommendation(
    config: Config,
    *,
    task_bucket: str,
    execution_surface: str,
    semantic_complexity: str,
    failure_cost: str,
    objective_verification: str,
    expected_context: str,
    interaction_need: str,
    usage_pressure: str,
    available_models: list[str] | None = None,
    routing_cost_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a deterministic advisory only; no queue or configuration writes occur."""
    requested = {
        "task_bucket": task_bucket,
        "execution_surface": execution_surface,
        "semantic_complexity": semantic_complexity,
        "failure_cost": failure_cost,
        "objective_verification": objective_verification,
        "expected_context": expected_context,
        "interaction_need": interaction_need,
        "usage_pressure": usage_pressure,
        "available_models": sorted(set(available_models or [])),
    }
    records = _queue_records(config) + list(routing_cost_records or [])
    matching = [record for record in records if _matches_request(record, requested)]
    comparable = [record for record in matching if record["cohort"]["comparability"]["joint_quality_cost"]]
    cohort_keys = {_comparison_cohort_key(record) for record in comparable}
    if len(cohort_keys) != 1:
        return _insufficient_report(requested, matching, comparable, "non_comparable_or_sparse_cohort")

    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in comparable:
        model = _model(record)
        if model:
            by_model[model].append(record)
    candidates = [_candidate(model, rows) for model, rows in sorted(by_model.items())]
    if requested["available_models"]:
        candidates = [item for item in candidates if item["model"] in requested["available_models"]]
    candidates = [item for item in candidates if item["model"] != EXCLUDED_DEFAULT_MODEL]
    eligible = [item for item in candidates if item["quality_gate"]["passed"]]
    if not eligible:
        return _insufficient_report(requested, matching, comparable, "quality_gate_not_met", candidates=candidates)

    # All eligible candidates share an exact comparison cohort. Quality is gated
    # before this cost-only Pareto selection; lexical model order makes ties stable.
    recommended = min(eligible, key=lambda item: (item["cost_evidence"]["mean_token_cost"], item["model"]))
    return {
        "kind": "routing_recommendation",
        "generated_at": iso_now(),
        "read_only": True,
        "mutation_allowed": False,
        "status": "recommended",
        "request": requested,
        "recommendation": {
            "model": recommended["model"],
            "reasoning": "quality gate passed in an exact execution-surface cohort; lowest mean comparable token cost among Pareto candidates",
            "fallback": {"model": STATIC_FALLBACK_MODEL, "reason": STATIC_FALLBACK_REASON},
            "confidence": "medium" if len(eligible) > 1 else "low",
            "comparable_cohort_size": len(comparable),
            "quality_evidence": recommended["quality_gate"],
            "cost_evidence": recommended["cost_evidence"],
            "known_confounders": [],
            "escalation_trigger": "quality gate fails, objective verification changes, or a higher failure-cost task falls outside this exact cohort",
            "downgrade_trigger": "a lower-cost candidate accumulates an exact comparable cohort that passes the same quality gate",
        },
        "candidates": candidates,
        "privacy": {"raw_prompts_included": False, "raw_context_included": False, "session_or_thread_ids_included": False},
    }


def _queue_records(config: Config) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for task in list_tasks(config):
        record = latest_routing_cost_evidence(task)
        if record is not None:
            records.append(record)
    return records


def _matches_request(record: dict[str, Any], request: dict[str, Any]) -> bool:
    execution = record["execution"]
    return execution["surface"] == request["execution_surface"] and execution["task_bucket"] == request["task_bucket"]


def _comparison_cohort_key(record: dict[str, Any]) -> tuple[str, ...]:
    components = record["cohort"]["components"]
    return tuple(
        str(components[key])
        for key in (
            "execution_surface",
            "execution_backend",
            "task_bucket",
            "prompt_contract_version",
            "context_contract_version",
            "attribution_class",
            "review_outcome_cohort_id",
        )
    )


def _model(record: dict[str, Any]) -> str | None:
    actual = record["actual_model"]
    return str(actual["value"]) if actual.get("status") == "observed" else None


def _token_cost(record: dict[str, Any]) -> int:
    return sum(value for value in record["usage"]["values"].values() if isinstance(value, int))


def _candidate(model: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [row for row in rows if row["quality"]["accepted"] and not row["quality"]["rejected"]]
    first_pass = [row for row in accepted if row["quality"]["follow_up_count"] == 0 and row["quality"]["rework_count"] == 0]
    adverse = [row for row in rows if row["quality"]["rejected"] or not row["quality"]["accepted"]]
    quality = {
        "accepted_count": len(accepted),
        "first_pass_accept_rate": len(first_pass) / len(rows),
        "needs_fix_or_rejected_rate": len(adverse) / len(rows),
        "thresholds": TASK_BUCKET_ADVISORY_THRESHOLDS,
    }
    quality["passed"] = (
        quality["accepted_count"] >= TASK_BUCKET_ADVISORY_THRESHOLDS["min_accepted_count"]
        and quality["first_pass_accept_rate"] >= TASK_BUCKET_ADVISORY_THRESHOLDS["min_first_pass_accept_rate"]
        and quality["needs_fix_or_rejected_rate"] <= TASK_BUCKET_ADVISORY_THRESHOLDS["max_needs_fix_or_rejected_rate"]
    )
    costs = [_token_cost(row) for row in rows]
    return {
        "model": model,
        "quality_gate": quality,
        "cost_evidence": {
            "sample_count": len(costs),
            "mean_token_cost": sum(costs) / len(costs),
            "attribution_class": rows[0]["usage"]["attribution"]["class"],
        },
    }


def _insufficient_report(
    request: dict[str, Any], matching: list[dict[str, Any]], comparable: list[dict[str, Any]], reason: str, *, candidates: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "kind": "routing_recommendation",
        "generated_at": iso_now(),
        "read_only": True,
        "mutation_allowed": False,
        "status": "insufficient",
        "request": request,
        "recommendation": {
            "model": None,
            "reasoning": STATIC_FALLBACK_REASON,
            "fallback": {"model": STATIC_FALLBACK_MODEL, "reason": STATIC_FALLBACK_REASON},
            "confidence": "none",
            "comparable_cohort_size": len(comparable),
            "quality_evidence": {"matching_record_count": len(matching)},
            "cost_evidence": {"matching_record_count": len(matching)},
            "known_confounders": [reason],
            "escalation_trigger": "collect exact comparable evidence before changing routing",
            "downgrade_trigger": "not_applicable",
        },
        "candidates": candidates or [],
        "privacy": {"raw_prompts_included": False, "raw_context_included": False, "session_or_thread_ids_included": False},
    }


def render_routing_recommendation(report: dict[str, Any]) -> str:
    recommendation = report["recommendation"]
    rows = [
        {"FIELD": "status", "VALUE": report["status"]},
        {"FIELD": "model", "VALUE": recommendation["model"] or "-"},
        {"FIELD": "fallback", "VALUE": recommendation["fallback"]["model"]},
        {"FIELD": "confidence", "VALUE": recommendation["confidence"]},
        {"FIELD": "cohort", "VALUE": str(recommendation["comparable_cohort_size"])},
        {"FIELD": "reasoning", "VALUE": recommendation["reasoning"]},
        {"FIELD": "escalation", "VALUE": recommendation["escalation_trigger"]},
        {"FIELD": "downgrade", "VALUE": recommendation["downgrade_trigger"]},
    ]
    return "Routing recommendation (read-only)\n" + render_table(
        ["FIELD", "VALUE"], [[row["FIELD"], row["VALUE"]] for row in rows]
    ) + "\n"

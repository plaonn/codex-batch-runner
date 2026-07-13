from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import Config
from .queue import list_tasks
from .routing_cost_evidence import (
    EXACT_EVIDENCE_CONTRACT_VERSION,
    RoutingCostEvidenceError,
    USAGE_KEYS,
    latest_routing_cost_evidence,
    validate_routing_cost_evidence,
)
from .routing_report import TASK_BUCKET_ADVISORY_THRESHOLDS, render_table
from .timeutil import iso_now


STATIC_FALLBACK_MODEL = "operator_baseline"
STATIC_FALLBACK_REASON = "insufficient_comparable_evidence"
PROHIBITED_REASONING_EFFORT = "xhigh"


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
        records.extend(validate_routing_cost_evidence(value) for value in values)
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
    """Build a deterministic advisory only; queue/configuration state is never written."""
    request = {
        "task_bucket": task_bucket,
        "execution_surface": execution_surface,
        "semantic_complexity": semantic_complexity,
        "failure_cost": failure_cost,
        "objective_verification": objective_verification,
        "expected_context_contract_version": expected_context,
        "interaction_need": interaction_need,
        "usage_pressure": usage_pressure,
        "available_models": sorted(set(available_models or [])),
    }
    matching = [record for record in _queue_records(config) + list(routing_cost_records or []) if _matches_request(record, request)]
    exact_matching = [record for record in matching if record.get("evidence_contract_version") == EXACT_EVIDENCE_CONTRACT_VERSION]
    comparable = [record for record in exact_matching if record["cohort"]["comparability"]["joint_quality_cost"]]
    safety = _safety_gate(request, comparable)
    cohort_keys = {_comparison_cohort_key(record) for record in comparable}
    if not safety["passed"]:
        reason = safety["reason"] if exact_matching else "no_matching_exact_v3_cohort"
        return _insufficient_report(request, matching, comparable, reason, safety=safety)
    if len(cohort_keys) != 1:
        return _insufficient_report(request, matching, comparable, "non_comparable_or_sparse_cohort", safety=safety)

    candidates = _candidates(comparable, request["available_models"])
    if not candidates:
        return _insufficient_report(request, matching, comparable, "no_eligible_model_effort_candidates", safety=safety)
    # routing-cost-evidence-v1 records components and attribution, but deliberately
    # has no price table or normalized-cost field. It is unsafe to rank components
    # with an invented scalar or to treat cached and uncached tokens as equivalent.
    return _insufficient_report(
        request,
        matching,
        comparable,
        "normalized_cost_unavailable",
        candidates=candidates,
        safety=safety,
    )


def _queue_records(config: Config) -> list[dict[str, Any]]:
    return [record for task in list_tasks(config) if (record := latest_routing_cost_evidence(task)) is not None]


def _matches_request(record: dict[str, Any], request: dict[str, Any]) -> bool:
    execution = record["execution"]
    return (
        execution["surface"] == request["execution_surface"]
        and execution["task_bucket"] == request["task_bucket"]
        and execution["context_contract_version"] == request["expected_context_contract_version"]
    )


def _safety_gate(request: dict[str, Any], comparable: list[dict[str, Any]]) -> dict[str, Any]:
    if not comparable:
        return {"passed": False, "reason": "no_matching_verified_cohort", "usage_pressure_does_not_relax_quality": True}
    if request["interaction_need"] != "none":
        return {"passed": False, "reason": "interaction_need_not_covered_by_execution_evidence", "usage_pressure_does_not_relax_quality": True}
    verified = all(row["quality"]["objective_verification"] == "passed" and row["quality"]["semantic_review"] == "pass" for row in comparable)
    if request["failure_cost"] == "high" and not verified:
        return {"passed": False, "reason": "high_failure_cost_requires_matching_verified_cohort", "usage_pressure_does_not_relax_quality": True}
    if request["semantic_complexity"] == "high" and not verified:
        return {"passed": False, "reason": "high_semantic_complexity_requires_matching_verified_cohort", "usage_pressure_does_not_relax_quality": True}
    if request["objective_verification"] == "none":
        return {"passed": False, "reason": "objective_verification_required", "usage_pressure_does_not_relax_quality": True}
    if request["usage_pressure"] in {"high", "critical"} and any(row["usage"]["attribution"]["class"] != "provider_attributed" for row in comparable):
        return {"passed": False, "reason": "high_usage_pressure_requires_provider_attributed_usage", "usage_pressure_does_not_relax_quality": True}
    return {"passed": True, "reason": None, "usage_pressure_does_not_relax_quality": True, "verified_cohort": verified}


def _comparison_cohort_key(record: dict[str, Any]) -> tuple[str, ...]:
    components = record["cohort"]["components"]
    excluded = {
        "cohort_id", "target_id", "planned_model", "actual_model", "selected_model",
        "command_model", "reasoning_effort", "planned_reasoning", "review_outcome_cohort_id",
    }
    return tuple(f"{key}={components[key]}" for key in sorted(components) if key not in excluded)


def _candidates(records: list[dict[str, Any]], available_models: list[str]) -> list[dict[str, Any]]:
    by_selection: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
        model = str(identity.get("selected_model") or "unknown")
        effort = str(identity.get("reasoning_effort") or "unknown")
        if effort == PROHIBITED_REASONING_EFFORT or (available_models and model not in available_models):
            continue
        by_selection[(model, effort)].append(record)
    return [_candidate(model, effort, rows) for (model, effort), rows in sorted(by_selection.items())]


def _candidate(model: str, reasoning_effort: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [row for row in rows if row["quality"]["accepted"] and not row["quality"]["rejected"]]
    first_pass = [row for row in accepted if row["quality"]["follow_up_count"] == 0 and row["quality"]["rework_count"] == 0]
    adverse = [row for row in rows if row["quality"]["rejected"] or not row["quality"]["accepted"]]
    quality = {
        "accepted_count": len(accepted),
        "first_pass_accept_rate": len(first_pass) / len(rows),
        "needs_fix_or_rejected_rate": len(adverse) / len(rows),
        "thresholds": TASK_BUCKET_ADVISORY_THRESHOLDS,
    }
    quality["passed"] = quality["accepted_count"] >= TASK_BUCKET_ADVISORY_THRESHOLDS["min_accepted_count"] and quality["first_pass_accept_rate"] >= TASK_BUCKET_ADVISORY_THRESHOLDS["min_first_pass_accept_rate"] and quality["needs_fix_or_rejected_rate"] <= TASK_BUCKET_ADVISORY_THRESHOLDS["max_needs_fix_or_rejected_rate"]
    return {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "quality_gate": quality,
        "usage_evidence": {
            "sample_count": len(rows),
            "mean_components": {key: sum(row["usage"]["values"][key] or 0 for row in rows) / len(rows) for key in USAGE_KEYS},
            "attribution": {"class": rows[0]["usage"]["attribution"]["class"], "source": rows[0]["usage"]["attribution"]["source"], "confidence": "contract_attribution_class"},
            "normalized_cost_available": False,
        },
    }


def _insufficient_report(request: dict[str, Any], matching: list[dict[str, Any]], comparable: list[dict[str, Any]], reason: str, *, candidates: list[dict[str, Any]] | None = None, safety: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "kind": "routing_recommendation",
        "generated_at": iso_now(),
        "read_only": True,
        "mutation_allowed": False,
        "status": "insufficient",
        "request": request,
        "safety_gate": safety or {},
        "recommendation": {
            "model": None,
            "reasoning_effort": None,
            "rationale": STATIC_FALLBACK_REASON,
            "fallback": {"model": STATIC_FALLBACK_MODEL, "reasoning_effort": "operator_selected", "reason": STATIC_FALLBACK_REASON},
            "confidence": "none",
            "comparable_cohort_size": len(comparable),
            "quality_evidence": {"matching_record_count": len(matching)},
            "cost_evidence": {"normalized_cost_available": False, "reason": reason},
            "known_confounders": [reason],
            "escalation_trigger": "collect exact comparable evidence with normalized cost before changing routing",
            "downgrade_trigger": "not_applicable",
        },
        "candidates": candidates or [],
        "privacy": {"raw_prompts_included": False, "raw_context_included": False, "session_or_thread_ids_included": False},
    }


def render_routing_recommendation(report: dict[str, Any]) -> str:
    recommendation = report["recommendation"]
    rows = [
        ["status", report["status"]], ["model", recommendation["model"] or "-"], ["reasoning_effort", recommendation["reasoning_effort"] or "-"], ["fallback", f"{recommendation['fallback']['model']} / {recommendation['fallback']['reasoning_effort']}"], ["confidence", recommendation["confidence"]], ["cohort", str(recommendation["comparable_cohort_size"])], ["rationale", recommendation["rationale"]], ["escalation", recommendation["escalation_trigger"]], ["downgrade", recommendation["downgrade_trigger"]],
    ]
    return "Routing recommendation (read-only)\n" + render_table(["FIELD", "VALUE"], rows) + "\n"

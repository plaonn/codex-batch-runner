from __future__ import annotations

from typing import Any

from .provider_resource import derive_provider_resource_evidence, provider_resource_key
from .request_fingerprint import _has_value, _normalized_list, _safe_id_hash, _safe_metadata_value, derive_request_fingerprint
from .task_vector import derive_normalized_task_vector

SCHEMA_VERSION = 1
DERIVATION_VERSION = "evaluation-row-v1"

TERMINAL_STATUSES = {"completed", "needs_resume", "blocked_user", "failed"}
REVIEW_DECISIONS = {"pass", "needs_fix", "needs_human", "failed_review"}


def derive_evaluation_row(task: dict[str, Any]) -> dict[str, Any]:
    """Derive one public-safe read-only evaluation row from task metadata.

    The row deliberately keeps worker, reviewer, task-vector, fingerprint, and
    objective check evidence separate. It does not include prompts, summaries,
    logs, stdout/stderr, session/thread ids, or raw local paths.
    """
    fingerprint = derive_request_fingerprint(task)
    task_vector = derive_normalized_task_vector(task)
    worker = _worker_section(task)
    reviewer = _reviewer_section(task)
    provider_resource = derive_provider_resource_evidence(task)
    objective_checks = _objective_checks(task, reviewer)
    outcomes = _outcomes(task, reviewer)
    task_vector_evaluation = _task_vector_evaluation(task, task_vector, worker, objective_checks, outcomes)
    exclusion_reasons = _exclusion_reasons(task, task_vector, reviewer, objective_checks, outcomes)
    policy_usage = _policy_usage(task, task_vector, reviewer, objective_checks, outcomes, exclusion_reasons)

    row = {
        "schema_version": SCHEMA_VERSION,
        "derivation_version": DERIVATION_VERSION,
        "execution_surface": _execution_surface(task),
        "subject": _subject_section(task, fingerprint),
        "task_id": _task_id(task),
        "task_id_hash": fingerprint.get("task_id_hash"),
        "request_fingerprint": _fingerprint_section(fingerprint),
        "task_vector": _task_vector_section(task_vector),
        "task_bucket_key": _task_bucket_key(task_vector, fingerprint),
        "request_family_key": _request_family_key(fingerprint),
        "lineage": _lineage_section(fingerprint, task),
        "routing": _routing_section(task),
        "provider_resource": provider_resource,
        "worker": worker,
        "reviewer": reviewer,
        "objective_checks": objective_checks,
        "task_vector_evaluation": task_vector_evaluation,
        "outcomes": outcomes,
        "policy_usage": policy_usage,
        "exclusion_reasons": exclusion_reasons,
        "privacy": {
            "raw_prompt_included": False,
            "raw_result_summary_included": False,
            "raw_logs_included": False,
            "session_or_thread_ids_included": False,
            "raw_paths_included": False,
        },
    }
    row["experiment_cell_key"] = _experiment_cell_key(row)
    return row


def _subject_section(task: dict[str, Any], fingerprint: dict[str, Any]) -> dict[str, Any]:
    queue_task = task.get("queue_task") is not False
    return {
        "kind": "cbr_queue_task" if queue_task else "supplemental_execution_evidence",
        "queue_task": queue_task,
        "execution_surface": _execution_surface(task),
        "work_id_hash": fingerprint.get("task_id_hash"),
    }


def _execution_surface(task: dict[str, Any]) -> str:
    surface = _safe_metadata_value(task.get("execution_surface"))
    if surface != "unknown":
        return surface
    if task.get("queue_task") is False:
        return "unknown"
    return "cbr_batch"


def _fingerprint_section(fingerprint: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": fingerprint.get("schema_version"),
        "fingerprint_id": fingerprint.get("fingerprint_id"),
        "preprocessing_version": fingerprint.get("preprocessing_version"),
        "source_fields": list(fingerprint.get("source_fields") or []),
        "text_stats": dict(fingerprint.get("text_stats") or {}),
        "metadata_hints": dict(fingerprint.get("metadata_hints") or {}),
        "lineage_hints": dict(fingerprint.get("lineage_hints") or {}),
        "privacy": dict(fingerprint.get("privacy") or {}),
    }


def _task_vector_section(task_vector: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": task_vector.get("schema_version"),
        "preprocessing_version": task_vector.get("preprocessing_version"),
        "source": task_vector.get("source"),
        "derivation": task_vector.get("derivation"),
        "confidence": task_vector.get("confidence"),
        "dimensions": dict(task_vector.get("dimensions") or {}),
        "project": dict(task_vector.get("project") or {}),
        "task": dict(task_vector.get("task") or {}),
        "provenance": dict(task_vector.get("provenance") or {}),
    }


def _routing_section(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_backend": _safe_metadata_value(task.get("execution_backend")),
        "capacity_pool": _safe_metadata_value(task.get("capacity_pool")),
        "routing_experiment": _safe_metadata_value(task.get("routing_experiment")),
        "routing_size": _safe_metadata_value(task.get("routing_size")),
        "routing_risk": _safe_metadata_value(task.get("routing_risk")),
        "verification_scope": _normalized_list(task.get("verification_scope")),
        "routing_risk_factors": _normalized_list(task.get("routing_risk_factors")),
    }


def _worker_section(task: dict[str, Any]) -> dict[str, Any]:
    last_run = _dict_value(task.get("last_run"))
    resolved_config = _dict_value(last_run.get("resolved_execution_config"))
    requirement_vector = _dict_value(resolved_config.get("model_requirement_vector") or task.get("model_requirement_vector"))
    dimensions = _dict_value(requirement_vector.get("dimensions"))
    selection_rule = _safe_metadata_value(resolved_config.get("selection_rule"))
    requirement_key = _join_key(
        "requirement",
        {key: _safe_metadata_value(dimensions.get(key)) for key in sorted(dimensions)},
    )
    backend = _safe_metadata_value(last_run.get("execution_backend") or task.get("execution_backend"))
    model_present = _has_value(resolved_config.get("model"))
    codex_profile_present = _has_value(resolved_config.get("codex_profile"))
    model_source = _safe_metadata_value(resolved_config.get("model_source"))
    execution_target = _execution_target_value(resolved_config)
    duration = _number_value(last_run.get("duration_seconds"))
    attempts = _int_value(task.get("attempts"))
    run_count = _int_value(task.get("run_count"))
    terminal_status = _terminal_status(task.get("status"))

    return {
        "worker_id": f"{backend}.{selection_rule}" if backend != "unknown" and selection_rule != "unknown" else "unknown",
        "worker_cell_key": _join_key(
            "worker",
            {
                "backend": backend,
                "selection_rule": selection_rule,
                "model_present": _bool_key(model_present),
                "codex_profile_present": _bool_key(codex_profile_present),
            },
        ),
        "execution_backend": backend,
        "model_requirement_key": requirement_key,
        "model_selection_rule": selection_rule,
        "model_source": model_source,
        "execution_target": execution_target,
        "model_present": model_present,
        "codex_profile_present": codex_profile_present,
        "terminal_status": terminal_status,
        "attempts": attempts,
        "run_count": run_count,
        "duration_seconds_bucket": _duration_bucket(duration),
        "resume_requested": bool(task.get("resume_requested")),
        "resume_required": terminal_status == "needs_resume" or bool(task.get("next_prompt")),
        "rate_limited": _marker_present(task, ("rate_limit", "usage_limit")),
        "cooldown_applied": _has_value(task.get("cooldown_until")),
        "startup_stalled": _marker_present(task, ("startup_stall", "first_meaningful_stall", "no_progress")),
    }


def _reviewer_section(task: dict[str, Any]) -> dict[str, Any]:
    reviewer_codex = _dict_value(task.get("reviewer_codex"))
    findings = _list_value(reviewer_codex.get("findings") or task.get("review_findings"))
    required_human_checks = _list_value(reviewer_codex.get("required_human_checks"))
    decision = _review_decision(reviewer_codex.get("decision") or task.get("last_review_decision"))
    confidence = _safe_metadata_value(reviewer_codex.get("confidence"))
    review_status = _review_status(task)
    review_attempts = _int_value(task.get("review_attempts"))
    fix_attempts = _int_value(task.get("fix_attempts"))
    reviewer_present = bool(reviewer_codex)
    reviewer_role = _safe_metadata_value(task.get("reviewer_role") or "reviewer")
    policy_version = _safe_metadata_value(task.get("review_policy_version") or "legacy")
    anchor_review = _anchor_review(task, reviewer_role)

    return {
        "reviewer_id": f"codex.reviewer.{reviewer_role}" if reviewer_present and reviewer_role != "unknown" else "unknown",
        "reviewer_cell_key": _join_key(
            "reviewer",
            {
                "present": _bool_key(reviewer_present),
                "role": reviewer_role,
                "policy": policy_version,
                "anchor": _tri_key(anchor_review),
            },
        ),
        "review_status": review_status,
        "reviewer_codex_present": reviewer_present,
        "reviewer_role": reviewer_role,
        "review_policy_version": policy_version,
        "review_scope": _normalized_list(task.get("review_scope")) or ["unknown"],
        "anchor_review": anchor_review,
        "reviewer_decision": decision,
        "confidence": confidence,
        "finding_count": len(findings),
        "error_finding_count": _error_finding_count(findings),
        "required_human_check_count": len(required_human_checks),
        "schema_valid": reviewer_present and decision in REVIEW_DECISIONS,
        "review_attempts": review_attempts,
        "fix_attempts": fix_attempts,
        "last_auto_fix_task_id_hash": _safe_id_hash(task.get("last_auto_fix_task_id")),
        "last_conflict_fix_task_id_hash": _safe_id_hash(task.get("last_conflict_fix_task_id")),
        "human_override_present": _has_value(task.get("review_reason")) or _has_value(task.get("resolution_reason")),
    }


def _objective_checks(task: dict[str, Any], reviewer: dict[str, Any]) -> dict[str, Any]:
    last_result = _dict_value(task.get("last_result"))
    verification = _list_value(last_result.get("verification"))
    final_json_available = bool(last_result)
    final_status = _safe_metadata_value(last_result.get("status"))
    task_status = _terminal_status(task.get("status"))
    final_json_valid = final_json_available and final_status != "unknown" and _result_task_matches(task, last_result)
    verification_missing = task_status == "completed" and len(verification) == 0
    required_check_failed = final_json_available and final_status not in {"completed", "needs_resume", "blocked_user", "failed"}

    stale_marker = _marker_present(task, ("stale", "rebase"))
    conflict_marker = _marker_present(task, ("conflict",))
    recovery_marker = _marker_present(task, ("recovery", "recovered"))
    safety_flag = _marker_present(task, ("secret", "credential", "private_leak", "safety"))

    return {
        "final_json_available": final_json_available,
        "final_json_valid": final_json_valid,
        "final_result_status": final_status,
        "verification_count": len(verification),
        "verification_missing": verification_missing,
        "changed_file_count_bucket": _count_bucket(len(_list_value(last_result.get("changed_files"))), empty="0"),
        "commit_count_bucket": _count_bucket(len(_list_value(last_result.get("commits"))), empty="0"),
        "worktree_apply_status": _safe_metadata_value(task.get("execution_apply_status")),
        "worktree_cleanup_status": _safe_metadata_value(task.get("execution_cleanup_status")),
        "stale_base_marker_present": stale_marker,
        "conflict_marker_present": conflict_marker,
        "recovery_marker_present": recovery_marker,
        "rate_limit_marker_present": _marker_present(task, ("rate_limit", "usage_limit")),
        "cooldown_marker_present": _has_value(task.get("cooldown_until")),
        "public_private_safety_flag": safety_flag,
        "review_process_failed": reviewer.get("reviewer_decision") == "failed_review",
        "required_checks_passed": final_json_valid and final_status == "completed" and not required_check_failed and not safety_flag,
        "required_check_failed": required_check_failed or safety_flag,
        "git_state_unsafe_or_ambiguous": stale_marker or conflict_marker or recovery_marker,
    }


def _task_vector_evaluation(
    task: dict[str, Any],
    task_vector: dict[str, Any],
    worker: dict[str, Any],
    objective_checks: dict[str, Any],
    outcomes: dict[str, Any],
) -> dict[str, Any]:
    """Compare the derived vector with safe post-run metadata.

    This is an audit-only quality slice. It uses buckets, classes, and already
    structured outcome fields; it does not surface raw changed paths,
    verification commands, summaries, prompts, logs, or volatile ids.
    """
    dimensions = task_vector.get("dimensions") if isinstance(task_vector.get("dimensions"), dict) else {}
    observed = _observed_task_metadata(task, worker, objective_checks, outcomes)
    evaluations = {
        "routing_size": _evaluate_scalar_dimension(
            dimensions.get("routing_size"),
            observed.get("inferred_routing_size"),
            observed_signal="changed_file_count_bucket+verification_count_bucket",
        ),
        "routing_risk": _evaluate_scalar_dimension(
            dimensions.get("routing_risk"),
            observed.get("inferred_routing_risk"),
            observed_signal="path_classes+outcome_flags",
        ),
        "category": _evaluate_scalar_dimension(
            dimensions.get("category"),
            observed.get("inferred_category"),
            observed_signal="changed_file_classes",
        ),
        "execution_backend": _evaluate_scalar_dimension(
            dimensions.get("execution_backend"),
            observed.get("execution_backend"),
            observed_signal="worker_execution_backend",
        ),
        "verification_scope": _evaluate_list_dimension(
            dimensions.get("verification_scope"),
            observed.get("observed_verification_scope"),
            observed_signal="verification_command_classes",
        ),
        "routing_risk_factors": _evaluate_list_dimension(
            dimensions.get("routing_risk_factors"),
            observed.get("observed_risk_factors"),
            observed_signal="path_classes+outcome_flags",
        ),
        "labels": _evaluate_list_dimension(
            dimensions.get("labels"),
            [],
            observed_signal="not_observed_post_run",
        ),
    }
    uncertainty_reasons = sorted(
        {
            reason
            for evaluation in evaluations.values()
            for reason in evaluation.get("uncertainty_reasons", [])
            if str(reason).strip()
        }
    )
    mismatches = sorted(
        dimension
        for dimension, evaluation in evaluations.items()
        if evaluation.get("comparison") == "mismatch"
    )
    excluded_dimensions = sorted(
        dimension
        for dimension, evaluation in evaluations.items()
        if evaluation.get("comparison") in {"not_observed", "declared_missing", "observed_missing"}
    )
    return {
        "schema_version": 1,
        "derivation_version": "task-vector-evaluation-v1",
        "read_only": True,
        "vector_confidence": task_vector.get("confidence"),
        "observed": observed,
        "dimensions": evaluations,
        "summary": {
            "matched_dimensions": sum(1 for item in evaluations.values() if item.get("comparison") == "match"),
            "mismatched_dimensions": len(mismatches),
            "excluded_dimensions": excluded_dimensions,
            "mismatched_dimension_names": mismatches,
            "uncertainty_reasons": uncertainty_reasons,
        },
        "privacy": {
            "raw_changed_files_included": False,
            "raw_verification_included": False,
            "raw_prompts_included": False,
            "raw_logs_included": False,
            "raw_paths_included": False,
        },
    }


def _observed_task_metadata(
    task: dict[str, Any],
    worker: dict[str, Any],
    objective_checks: dict[str, Any],
    outcomes: dict[str, Any],
) -> dict[str, Any]:
    last_result = _dict_value(task.get("last_result"))
    changed_files = _list_value(last_result.get("changed_files"))
    verification = _list_value(last_result.get("verification"))
    changed_file_classes = _changed_file_classes(changed_files)
    verification_scope = _verification_scope_from_observed(verification)
    risk_factors = _observed_risk_factors(changed_file_classes, objective_checks, outcomes)
    changed_count = len(changed_files)
    verification_count = len(verification)
    return {
        "terminal_status": outcomes.get("worker_terminal_status"),
        "review_status": outcomes.get("review_status"),
        "review_decision": outcomes.get("review_decision"),
        "execution_backend": worker.get("execution_backend"),
        "changed_file_count_bucket": _count_bucket(changed_count),
        "verification_count_bucket": _count_bucket(verification_count),
        "changed_file_classes": changed_file_classes,
        "observed_verification_scope": verification_scope,
        "observed_risk_factors": risk_factors,
        "inferred_routing_size": _infer_routing_size(changed_count, verification_count),
        "inferred_routing_risk": _infer_routing_risk(changed_file_classes, objective_checks, outcomes),
        "inferred_category": _infer_category(changed_file_classes),
    }


def _changed_file_classes(changed_files: list[Any]) -> list[str]:
    classes = set()
    for item in changed_files:
        value = _safe_metadata_value(item)
        if not value or value == "unknown":
            continue
        if value.startswith("hash:"):
            classes.add("path_like")
            continue
        if value.startswith("<path:") and value.endswith(">"):
            classes.add(value.removeprefix("<path:").removesuffix(">"))
            continue
        if _path_contains(value, ".private"):
            classes.add("private_docs")
        elif _path_contains(value, "test") or _path_contains(value, "tests"):
            classes.add("tests")
        elif _path_contains(value, "doc") or value.endswith(".md") or _path_contains(value, "readme"):
            classes.add("public_docs")
        elif _path_contains(value, "src") or _path_contains(value, "lib"):
            classes.add("source")
        elif value.endswith((".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".plist")):
            classes.add("config")
        else:
            classes.add("other")
    return sorted(classes)


def _verification_scope_from_observed(verification: list[Any]) -> list[str]:
    scopes = set()
    for item in verification:
        value = _safe_metadata_value(item)
        if not value or value == "unknown":
            continue
        if any(token in value for token in ("unittest", "pytest", "test", "tests")):
            scopes.add("unit")
        if any(token in value for token in ("compileall", "python -m compileall", "tsc", "typecheck")):
            scopes.add("compile")
        if any(token in value for token in ("lint", "ruff", "eslint", "flake8")):
            scopes.add("lint")
        if "manual" in value:
            scopes.add("manual")
        if any(token in value for token in ("smoke", "start server", "curl", "healthcheck")):
            scopes.add("smoke")
    return sorted(scopes)


def _observed_risk_factors(
    changed_file_classes: list[str],
    objective_checks: dict[str, Any],
    outcomes: dict[str, Any],
) -> list[str]:
    factors = set()
    if "private_docs" in changed_file_classes:
        factors.add("private-docs")
    if "config" in changed_file_classes:
        factors.add("config")
    if "source" in changed_file_classes:
        factors.add("source")
    if objective_checks.get("public_private_safety_flag"):
        factors.add("safety-flag")
    if objective_checks.get("git_state_unsafe_or_ambiguous"):
        factors.add("git-state")
    if outcomes.get("needs_resume"):
        factors.add("needs-resume")
    if outcomes.get("blocked_user"):
        factors.add("blocked-user")
    if outcomes.get("failed"):
        factors.add("failed")
    return sorted(factors)


def _infer_routing_size(changed_file_count: int, verification_count: int) -> str:
    if changed_file_count == 0 and verification_count == 0:
        return "unknown"
    if changed_file_count <= 2 and verification_count <= 2:
        return "small"
    if changed_file_count <= 10 and verification_count <= 5:
        return "medium"
    return "large"


def _infer_routing_risk(
    changed_file_classes: list[str],
    objective_checks: dict[str, Any],
    outcomes: dict[str, Any],
) -> str:
    if not changed_file_classes and not any(outcomes.get(key) for key in ("failed", "needs_resume", "blocked_user")):
        return "unknown"
    if objective_checks.get("public_private_safety_flag") or objective_checks.get("git_state_unsafe_or_ambiguous"):
        return "high"
    if outcomes.get("failed") or outcomes.get("needs_resume") or outcomes.get("blocked_user"):
        return "medium"
    if "config" in changed_file_classes or "source" in changed_file_classes:
        return "medium"
    return "low"


def _infer_category(changed_file_classes: list[str]) -> str:
    if not changed_file_classes:
        return "unknown"
    if changed_file_classes == ["public_docs"]:
        return "docs"
    if set(changed_file_classes).issubset({"tests"}):
        return "tests"
    if "source" in changed_file_classes:
        return "implementation"
    if "config" in changed_file_classes:
        return "configuration"
    return "unknown"


def _evaluate_scalar_dimension(declared: Any, observed: Any, *, observed_signal: str) -> dict[str, Any]:
    declared_value = _safe_metadata_value(declared)
    observed_value = _safe_metadata_value(observed)
    reasons: list[str] = []
    if declared_value == "unknown" and observed_value == "unknown":
        comparison = "not_observed"
        reasons.append("declared_and_observed_missing")
    elif declared_value == "unknown":
        comparison = "declared_missing"
        reasons.append("declared_missing")
    elif observed_value == "unknown":
        comparison = "observed_missing"
        reasons.append("safe_observed_signal_missing")
    elif declared_value == observed_value:
        comparison = "match"
    else:
        comparison = "mismatch"
    return {
        "declared": declared_value,
        "observed": observed_value,
        "observed_signal": observed_signal,
        "comparison": comparison,
        "uncertainty_reasons": reasons,
    }


def _evaluate_list_dimension(declared: Any, observed: Any, *, observed_signal: str) -> dict[str, Any]:
    declared_values = _normalized_list(declared)
    observed_values = _normalized_list(observed)
    reasons: list[str] = []
    if not declared_values and not observed_values:
        comparison = "not_observed"
        reasons.append("declared_and_observed_missing")
    elif not declared_values:
        comparison = "declared_missing"
        reasons.append("declared_missing")
    elif not observed_values:
        comparison = "observed_missing"
        reasons.append("safe_observed_signal_missing")
    elif set(declared_values) & set(observed_values):
        comparison = "match"
    else:
        comparison = "mismatch"
    return {
        "declared": declared_values,
        "observed": observed_values,
        "observed_signal": observed_signal,
        "comparison": comparison,
        "uncertainty_reasons": reasons,
    }


def _path_contains(value: str, part: str) -> bool:
    return f"/{part}/" in f"/{value.strip('/')}/" or value.endswith(f"/{part}") or value.startswith(f"{part}/")


def _outcomes(task: dict[str, Any], reviewer: dict[str, Any]) -> dict[str, Any]:
    status = _terminal_status(task.get("status"))
    review = _review_status(task)
    resolution = _safe_metadata_value(task.get("resolution"))
    decision = _review_decision(reviewer.get("reviewer_decision") or task.get("last_review_decision"))
    applied = task.get("execution_apply_status") == "applied"

    return {
        "worker_terminal_status": status,
        "review_status": review,
        "review_decision": decision,
        "accepted": review == "accepted",
        "applied": applied,
        "rejected": review == "rejected",
        "needs_followup": review == "needs_followup",
        "unreviewed": review == "unreviewed",
        "resolved": resolution != "unknown",
        "resolution": resolution,
        "chain_status": _safe_metadata_value(task.get("chain_status")),
        "runnable": status == "runnable",
        "running": status == "running",
        "failed": status == "failed",
        "needs_resume": status == "needs_resume",
        "blocked_user": status == "blocked_user",
    }


def _policy_usage(
    task: dict[str, Any],
    task_vector: dict[str, Any],
    reviewer: dict[str, Any],
    objective_checks: dict[str, Any],
    outcomes: dict[str, Any],
    exclusion_reasons: list[str],
) -> dict[str, bool]:
    has_terminal_worker = outcomes["worker_terminal_status"] in TERMINAL_STATUSES
    review_decision = reviewer.get("reviewer_decision")
    review_usable = bool(reviewer.get("reviewer_codex_present")) and review_decision in REVIEW_DECISIONS
    objective_usable = bool(objective_checks.get("final_json_available"))
    vector_usable = task_vector.get("confidence") in {"high", "medium"}

    worker_blockers = {
        "review_process_failed",
        "reviewer_not_anchor",
        "human_override",
        "objective_checks_missing",
        "stale_base_or_conflict",
        "task_vector_uncertain",
    }
    return {
        "usable_for_worker_policy": has_terminal_worker
        and review_usable
        and objective_usable
        and not any(reason in worker_blockers for reason in exclusion_reasons),
        "usable_for_reviewer_calibration": has_terminal_worker and review_usable,
        "usable_for_task_vector_evaluation": has_terminal_worker and vector_usable,
        "usable_for_quota_debugging": bool(task.get("attempts") or task.get("run_count") or task.get("last_run")),
    }


def _exclusion_reasons(
    task: dict[str, Any],
    task_vector: dict[str, Any],
    reviewer: dict[str, Any],
    objective_checks: dict[str, Any],
    outcomes: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if task_vector.get("confidence") == "low":
        reasons.append("task_vector_uncertain")
    if outcomes["worker_terminal_status"] in {"running", "runnable", "unknown"}:
        reasons.append("worker_not_terminal")
    if objective_checks.get("verification_missing"):
        reasons.append("objective_checks_missing")
    if objective_checks.get("review_process_failed"):
        reasons.append("review_process_failed")
    if reviewer.get("reviewer_codex_present") and reviewer.get("anchor_review") is False:
        reasons.append("reviewer_not_anchor")
    if reviewer.get("human_override_present"):
        reasons.append("human_override")
    if objective_checks.get("stale_base_marker_present") or objective_checks.get("conflict_marker_present"):
        reasons.append("stale_base_or_conflict")
    if outcomes.get("unreviewed") and _has_value(task.get("private_only")):
        reasons.append("private_only_unreviewed")
    return reasons


def _task_bucket_key(task_vector: dict[str, Any], fingerprint: dict[str, Any]) -> str:
    hints = fingerprint.get("metadata_hints") if isinstance(fingerprint.get("metadata_hints"), dict) else {}
    hinted = hints.get("task_bucket_key")
    if hinted:
        return str(hinted)
    dimensions = task_vector.get("dimensions") if isinstance(task_vector.get("dimensions"), dict) else {}
    scopes = dimensions.get("verification_scope") if isinstance(dimensions.get("verification_scope"), list) else []
    scope_text = ",".join(str(item) for item in scopes) if scopes else "none"
    return f"size={dimensions.get('routing_size', 'unknown')} risk={dimensions.get('routing_risk', 'unknown')} verify={scope_text}"


def _request_family_key(fingerprint: dict[str, Any]) -> str:
    return str(fingerprint.get("fingerprint_id") or "unknown")


def _lineage_section(fingerprint: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    hints = fingerprint.get("lineage_hints") if isinstance(fingerprint.get("lineage_hints"), dict) else {}
    explicit = any(_has_value(task.get(field)) for field in ("root_task_id", "parent_task_id", "source_task_id", "subtask_type"))
    return {
        "context": _lineage_context(task),
        "explicit_lineage_present": explicit,
        "hints": dict(hints),
    }


def _lineage_context(task: dict[str, Any]) -> str:
    subtask_type = _safe_metadata_value(task.get("subtask_type"))
    if subtask_type in {"auto_review_fix", "review_fix"}:
        return "review_fix"
    if subtask_type == "worktree_conflict_fix":
        return "conflict_fix"
    if _has_value(task.get("source_task_id")):
        return "related"
    if _has_value(task.get("parent_task_id")) or _has_value(task.get("root_task_id")):
        return "retry"
    return "none"


def _experiment_cell_key(row: dict[str, Any]) -> str:
    return "|".join(
        (
            str(row.get("task_bucket_key") or "unknown"),
            provider_resource_key(_dict_value(row.get("provider_resource"))),
            str(row.get("worker", {}).get("worker_cell_key") or "unknown"),
            str(row.get("reviewer", {}).get("reviewer_cell_key") or "unknown"),
        )
    )


def _task_id(task: dict[str, Any]) -> str:
    return _safe_metadata_value(task.get("id") or task.get("task_id"))


def _review_status(task: dict[str, Any]) -> str:
    value = _safe_metadata_value(task.get("review_status"))
    if value == "unknown" and task.get("status") == "completed":
        return "unreviewed"
    return value


def _terminal_status(value: Any) -> str:
    status = _safe_metadata_value(value)
    if status in {"runnable", "running", "completed", "needs_resume", "blocked_user", "failed", "archived"}:
        return status
    return "unknown"


def _review_decision(value: Any) -> str | None:
    decision = _safe_metadata_value(value)
    if decision in REVIEW_DECISIONS:
        return decision
    return None


def _anchor_review(task: dict[str, Any], reviewer_role: str) -> bool | None:
    if isinstance(task.get("anchor_review"), bool):
        return bool(task.get("anchor_review"))
    if not task.get("reviewer_codex"):
        return None
    if reviewer_role in {"anchor"}:
        return True
    return None


def _result_task_matches(task: dict[str, Any], last_result: dict[str, Any]) -> bool:
    result_task_id = last_result.get("task_id")
    if not _has_value(result_task_id):
        return True
    return _safe_metadata_value(result_task_id) == _safe_metadata_value(task.get("id") or task.get("task_id"))


def _marker_present(value: Any, needles: tuple[str, ...]) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = _safe_metadata_value(key)
            if any(needle in key_text for needle in needles) and _has_value(child):
                return True
            if _marker_present(child, needles):
                return True
    elif isinstance(value, list):
        return any(_marker_present(item, needles) for item in value)
    elif isinstance(value, str):
        text = _safe_metadata_value(value)
        return any(needle in text for needle in needles)
    return False


def _error_finding_count(findings: list[Any]) -> int:
    return sum(1 for item in findings if isinstance(item, dict) and _safe_metadata_value(item.get("severity")) == "error")


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _execution_target_value(resolved_config: dict[str, Any]) -> str:
    if "execution_target" not in resolved_config:
        return "none"
    return _safe_metadata_value(resolved_config.get("execution_target"))


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int_value(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _number_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _duration_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 60:
        return "0-60s"
    if value < 300:
        return "1-5m"
    if value < 900:
        return "5-15m"
    if value < 1800:
        return "15-30m"
    return "30m+"


def _count_bucket(count: int, *, empty: str = "0") -> str:
    if count <= 0:
        return empty
    if count == 1:
        return "1"
    if count <= 5:
        return "2-5"
    if count <= 20:
        return "6-20"
    return "20+"


def _join_key(prefix: str, parts: dict[str, str]) -> str:
    return prefix + ":" + " ".join(f"{key}={value}" for key, value in sorted(parts.items()))


def _bool_key(value: bool) -> str:
    return "true" if value else "false"


def _tri_key(value: bool | None) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"

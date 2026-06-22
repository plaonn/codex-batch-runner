from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

from .config import Config
from .events import emit_task_event, transition_payload
from .fs import ensure_dir
from .lock import FileLock
from .queue import (
    chain_metadata,
    dependency_status,
    list_tasks,
    load_task,
    save_task,
    task_labels,
    task_project_id,
    task_project_root,
)
from .review_bundle import build_review_bundle, sanitize_value
from .reviewer_codex import reviewer_clear_pass, run_reviewer_codex
from .state import in_global_cooldown, in_reviewer_codex_cooldown, mark_reviewer_codex_rate_limit
from .summary import review_status
from .timeutil import add_seconds, iso_now
from .transcript import sanitize

REVIEW_NEEDED_STATUSES = {"unreviewed", "rejected", "needs_followup"}
AUTO_FIX_ALLOWED_CONFIDENCE = {"medium", "high"}
HIGH_RISK_TERMS = (
    "credential",
    "token",
    "secret",
    "auth",
    "permission",
    "encryption",
    "signing",
    "migration",
    "schema migration",
    "dependency",
    "upgrade",
    "lockfile",
    "reset",
    "prune",
    "cleanup",
    "delete",
    "remove",
    "history rewrite",
    "public api",
    "breaking",
)


def build_review_next_report(config: Config, filters: Namespace | None = None) -> dict[str, Any]:
    report = select_review_next_report(config, filters, mode="dry-run")
    report.pop("_fingerprint", None)
    return report


def build_review_next_apply_report(
    config: Config,
    filters: Namespace | None = None,
    *,
    mechanical_auto_accept: bool = False,
    reviewer_codex: bool = False,
) -> dict[str, Any]:
    ensure_dir(config.queue_dir)
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire():
        return no_selection_report(
            mode="apply",
            message="another runner is active",
            filters=filter_summary(filters),
            status="locked",
        )

    try:
        return build_review_next_apply_report_locked(
            config,
            filters,
            mechanical_auto_accept=mechanical_auto_accept,
            reviewer_codex=reviewer_codex,
        )
    finally:
        lock.release()


def build_review_next_apply_report_locked(
    config: Config,
    filters: Namespace | None = None,
    *,
    mechanical_auto_accept: bool = False,
    reviewer_codex: bool = False,
    auto_mode: bool = False,
) -> dict[str, Any]:
    report = select_review_next_report(config, filters, mode="apply", skip_reviewer_backoff=auto_mode)
    expected = report.pop("_fingerprint", None)
    mechanical_enabled = mechanical_auto_accept or config.auto_review_mechanical_accept
    reviewer_enabled = reviewer_codex or config.auto_review_codex_enabled
    report["auto_review"] = auto_review_summary(
        decision="none",
        reason="no completed task needs review" if not report["selected"] else "pending",
        enabled=mechanical_enabled,
        reviewer_codex_enabled=reviewer_enabled,
    )
    if not report["selected"]:
        return report

    if not mechanical_enabled and not reviewer_enabled:
        report["auto_review"] = auto_review_summary(
            decision="needs_human",
            reason="mechanical auto-accept and reviewer Codex are disabled",
            enabled=False,
            reviewer_codex_enabled=False,
        )
        return report

    failed = [gate for gate in report.get("gates", []) if not gate.get("ok")]
    if failed:
        report["auto_review"] = auto_review_summary(
            decision="needs_human",
            reason="mechanical gates failed",
            enabled=mechanical_enabled,
            reviewer_codex_enabled=reviewer_enabled,
            failing_gates=[gate.get("name") for gate in failed],
        )
        return report

    task_id = str(report["task_id"])
    if not isinstance(expected, dict):
        report["auto_review"] = auto_review_summary(
            decision="needs_human",
            reason="review fingerprint unavailable",
            enabled=mechanical_enabled,
            reviewer_codex_enabled=reviewer_enabled,
        )
        return report

    if reviewer_enabled:
        reviewer_report = run_reviewer_phase(config, task_id, expected, mechanical_enabled=mechanical_enabled)
        report["mutated"] = reviewer_report["mutated"]
        report["review_status"] = reviewer_report.get("review_status", report.get("review_status"))
        report["auto_review"] = reviewer_report["auto_review"]
        task = load_task(config, task_id)
        report["chain"] = chain_metadata(task)
        report["auto_fix_planner"] = build_auto_fix_planner_report(config, task, report.get("gates", []))
        return report

    if not mechanical_enabled:
        report["auto_review"] = auto_review_summary(
            decision="needs_human",
            reason="mechanical auto-accept is disabled",
            enabled=False,
            reviewer_codex_enabled=False,
        )
        return report

    applied = apply_mechanical_accept(config, task_id, expected)
    report["mutated"] = applied["mutated"]
    report["review_status"] = applied.get("review_status", report.get("review_status"))
    task = load_task(config, task_id)
    report["chain"] = chain_metadata(task)
    report["auto_fix_planner"] = build_auto_fix_planner_report(config, task, report.get("gates", []))
    report["auto_review"] = auto_review_summary(
        decision=applied["decision"],
        reason=applied["reason"],
        enabled=True,
        reviewer_codex_enabled=False,
    )
    return report


def has_actionable_auto_review_candidate(config: Config) -> bool:
    if not (config.auto_review_mechanical_accept or config.auto_review_codex_enabled):
        return False
    if in_global_cooldown(config):
        return False

    report = select_review_next_report(config, mode="apply", skip_reviewer_backoff=True)
    if not report.get("selected"):
        return False
    if not report.get("gates_ok"):
        return False

    if config.auto_review_codex_enabled:
        if config.auto_review_codex_max_calls_per_run < 1:
            return False
        if in_reviewer_codex_cooldown(config):
            return False
        task_id = str(report.get("task_id") or "")
        if not task_id:
            return False
        task = load_task(config, task_id)
        bundle = build_review_bundle(
            task,
            by_id={item.get("id"): item for item in list_tasks(config)},
            require_accepted_review=config.dependency_requires_accepted_review,
        )
        return bundle_limit_error(config, bundle) is None

    return bool(config.auto_review_mechanical_accept)


def select_review_next_report(
    config: Config,
    filters: Namespace | None = None,
    *,
    mode: str,
    skip_reviewer_backoff: bool = False,
) -> dict[str, Any]:
    tasks = list_tasks(config)
    by_id = {task.get("id"): task for task in tasks}
    candidates = [task for task in tasks if is_review_needed(task)]
    candidates = apply_filters(candidates, filters)
    candidates.sort(key=review_sort_key)
    skipped: list[dict[str, Any]] = []
    if not candidates:
        return no_selection_report(
            mode=mode,
            message="no completed task needs review",
            filters=filter_summary(filters),
        )

    for task in candidates:
        bundle = build_review_bundle(
            task,
            by_id=by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )
        gates = mechanical_gates(task, bundle)
        if skip_reviewer_backoff and reviewer_backoff_matches(config, task, bundle, gates):
            marker = task.get("reviewer_codex_backoff") if isinstance(task.get("reviewer_codex_backoff"), dict) else {}
            skipped.append(
                {
                    "task_id": sanitize(task.get("id")),
                    "decision": sanitize(marker.get("decision")),
                    "reason": sanitize(marker.get("reason")),
                    "recorded_at": sanitize(marker.get("recorded_at")),
                }
            )
            continue
        return review_report_for_task(
            config,
            task,
            bundle,
            by_id,
            candidates,
            mode=mode,
            filters=filters,
            skipped_review_candidates=skipped,
            gates=gates,
        )

    return no_selection_report(
        mode=mode,
        message="no completed task needs review outside reviewer backoff",
        filters=filter_summary(filters),
        status="backoff",
        candidate_count=len(candidates),
        skipped_review_candidates=skipped,
    )


def review_report_for_task(
    config: Config,
    task: dict[str, Any],
    bundle: dict[str, Any],
    by_id: dict[str, dict],
    candidates: list[dict],
    *,
    mode: str,
    filters: Namespace | None,
    skipped_review_candidates: list[dict[str, Any]],
    gates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    gates = gates if gates is not None else mechanical_gates(task, bundle)
    deps_ready, blocked_by = dependency_status(
        task,
        by_id,
        require_accepted_review=config.dependency_requires_accepted_review,
    )
    return {
        "mode": mode,
        "selected": True,
        "task_id": task.get("id"),
        "candidate_count": len(candidates),
        "skipped_review_candidates": skipped_review_candidates,
        "message": "selected oldest completed task needing review",
        "filters": filter_summary(filters),
        "gates_ok": all(gate["ok"] for gate in gates),
        "gates": gates,
        "dependencies": {
            "ready": deps_ready,
            "blocked_by": blocked_by,
            "blockers": bundle.get("dependencies", {}).get("blockers", []),
            "requires_accepted_review": config.dependency_requires_accepted_review,
            "items": bundle.get("dependencies", {}).get("items", []),
        },
        "worktree_report": bundle.get("task_worktree"),
        "review_status": review_status(task),
        "chain": chain_metadata(task),
        "auto_fix_planner": build_auto_fix_planner_report(config, task, gates),
        "bundle": concise_bundle(bundle),
        "_fingerprint": review_fingerprint(task, bundle),
        "mutated": False,
    }


def no_selection_report(
    *,
    mode: str,
    message: str,
    filters: dict[str, Any],
    status: str = "empty",
    candidate_count: int = 0,
    skipped_review_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "status": status,
        "selected": False,
        "task_id": None,
        "candidate_count": candidate_count,
        "message": message,
        "filters": filters,
        "gates_ok": False,
        "gates": [],
        "dependencies": None,
        "review_status": None,
        "chain": None,
        "bundle": None,
        "mutated": False,
        "skipped_review_candidates": skipped_review_candidates or [],
    }


def is_review_needed(task: dict) -> bool:
    return task.get("status") == "completed" and review_status(task) in REVIEW_NEEDED_STATUSES


def apply_filters(tasks: list[dict], filters: Namespace | None) -> list[dict]:
    if filters is None:
        return tasks
    filtered = tasks
    if getattr(filters, "project_id", None):
        filtered = [task for task in filtered if task_project_id(task) == filters.project_id]
    if getattr(filters, "project_root", None):
        project_root = normalized_path(filters.project_root)
        filtered = [task for task in filtered if task_project_root(task) == project_root]
    if getattr(filters, "category", None):
        filtered = [task for task in filtered if task.get("category") == filters.category]
    if getattr(filters, "label", None):
        filtered = [task for task in filtered if filters.label in task_labels(task)]
    return filtered


def normalized_path(value: object) -> str:
    return str(Path(str(value)).expanduser().resolve()) if value else ""


def filter_summary(filters: Namespace | None) -> dict[str, Any]:
    if filters is None:
        return {}
    summary = {}
    for attr, key in (
        ("project_id", "project"),
        ("project_root", "project_root"),
        ("category", "category"),
        ("label", "label"),
    ):
        value = getattr(filters, attr, None)
        if value:
            summary[key] = str(value)
    return summary


def review_sort_key(task: dict) -> tuple[str, str, str]:
    timestamp = task.get("completed_at") or task.get("updated_at") or task.get("created_at") or ""
    return (str(timestamp), str(task.get("created_at") or ""), str(task.get("id") or ""))


def mechanical_gates(task: dict, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    last_result = task.get("last_result") if isinstance(task.get("last_result"), dict) else {}
    git_status = task.get("git_status") if isinstance(task.get("git_status"), dict) else {}
    repo = review_gate_repository(bundle)
    commit_info = bundle.get("commit_information") if isinstance(bundle.get("commit_information"), dict) else {}
    changed_files = last_result.get("changed_files") if isinstance(last_result, dict) else None
    verification = last_result.get("verification") if isinstance(last_result, dict) else None
    deps = bundle.get("dependencies") if isinstance(bundle.get("dependencies"), dict) else {}

    return [
        gate(
            "final_status_completed",
            task.get("status") == "completed",
            f"task.status={task.get('status')}",
        ),
        gate(
            "final_result_completed",
            isinstance(last_result, dict) and last_result.get("status") == "completed",
            f"last_result.status={last_result.get('status') if isinstance(last_result, dict) else None}",
        ),
        gate(
            "no_last_error",
            not task.get("last_error"),
            "last_error is empty" if not task.get("last_error") else "last_error is present",
        ),
        gate(
            "verification_present",
            isinstance(verification, list) and bool(verification),
            f"verification_count={len(verification) if isinstance(verification, list) else 'missing'}",
        ),
        gate(
            "changed_files_reported",
            isinstance(changed_files, list),
            f"changed_files_count={len(changed_files) if isinstance(changed_files, list) else 'missing'}",
        ),
        gate("dependencies_ready", bool(deps.get("ready")), dependency_detail(deps)),
        gate("git_clean", repo.get("dirty") is False, git_clean_detail(repo)),
        gate("no_unpushed_commits", no_unpushed(repo, git_status), unpushed_detail(repo, git_status)),
        gate("commit_ancestry_acceptable", commit_ancestry_ok(commit_info), commit_ancestry_detail(commit_info)),
        gate("safety_metadata_clean", not detectable_safety_violation(task, bundle), safety_detail(task, bundle)),
    ]


def gate(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": sanitize(detail)}


def review_gate_repository(bundle: dict[str, Any]) -> dict[str, Any]:
    repo = bundle.get("current_git_repository") if isinstance(bundle.get("current_git_repository"), dict) else {}
    if repo.get("available") is False and repo.get("inspection_scope") == "task_worktree":
        main_repo = bundle.get("current_main_repository") if isinstance(bundle.get("current_main_repository"), dict) else {}
        if main_repo.get("available") is True:
            return main_repo
    return repo


def dependency_detail(deps: dict[str, Any]) -> str:
    blockers = deps.get("blockers") or []
    if blockers:
        return "blocked_by=" + ",".join(
            f"{item.get('id')}:{item.get('reason')}" if isinstance(item, dict) else str(item) for item in blockers
        )
    blocked_by = deps.get("blocked_by") or []
    if blocked_by:
        return "blocked_by=" + ",".join(str(item) for item in blocked_by)
    return "ready=true"


def git_clean_detail(repo: dict[str, Any]) -> str:
    if not repo:
        return "git repository unavailable"
    if repo.get("available") is False:
        return str(repo.get("reason") or "git repository unavailable")
    return f"dirty={repo.get('dirty')}"


def no_unpushed(repo: dict[str, Any], git_status: dict[str, Any]) -> bool:
    if repo and repo.get("has_unpushed") is not None:
        return repo.get("has_unpushed") is False
    if not git_status:
        return False
    if git_status.get("has_unpushed") is not None:
        return git_status.get("has_unpushed") is False
    ahead = git_status.get("ahead")
    return isinstance(ahead, int) and ahead == 0


def unpushed_detail(repo: dict[str, Any], git_status: dict[str, Any]) -> str:
    if not repo and not git_status:
        return "current_git_repository and task_git_status_snapshot unavailable"
    return (
        f"current_has_unpushed={repo.get('has_unpushed') if repo else None}; "
        f"current_ahead={repo.get('ahead') if repo else None}; "
        f"snapshot_has_unpushed={git_status.get('has_unpushed') if git_status else None}; "
        f"snapshot_ahead={git_status.get('ahead') if git_status else None}"
    )


def commit_ancestry_ok(commit_info: dict[str, Any]) -> bool:
    ancestry = commit_info.get("ancestry") if isinstance(commit_info.get("ancestry"), dict) else {}
    status = ancestry.get("status")
    if status in {"equal", "ancestor"}:
        return True
    if status in {"not_reachable", "ambiguous"}:
        return False
    return True


def commit_ancestry_detail(commit_info: dict[str, Any]) -> str:
    ancestry = commit_info.get("ancestry") if isinstance(commit_info.get("ancestry"), dict) else {}
    status = ancestry.get("status") or "unavailable"
    detail = ancestry.get("detail") or ""
    reported = ancestry.get("reported_commit")
    current = ancestry.get("current_head")
    base = ancestry.get("base_head")
    parts = [f"status={status}"]
    if base:
        parts.append(f"execution_base_head={short_sha(base)}")
    if reported:
        parts.append(f"reported={short_sha(reported)}")
    if current:
        parts.append(f"current_head={short_sha(current)}")
    if detail:
        parts.append(str(detail))
    return "; ".join(parts)


def short_sha(value: object) -> str:
    text = str(value or "")
    return text[:12] if len(text) >= 12 else text


def concise_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    git_diff = bundle.get("git_diff") if isinstance(bundle.get("git_diff"), dict) else {}
    return {
        "task": bundle.get("task"),
        "prompt_excerpt": bundle.get("prompt_excerpt"),
        "status": bundle.get("status"),
        "review_status": bundle.get("review_status"),
        "dependencies": bundle.get("dependencies"),
        "last_result": bundle.get("last_result"),
        "last_run": bundle.get("last_run"),
        "task_worktree": bundle.get("task_worktree"),
        "review_follow_up": bundle.get("review_follow_up"),
        "reviewer_codex": bundle.get("reviewer_codex"),
        "reviewer_codex_backoff": bundle.get("reviewer_codex_backoff"),
        "chain": bundle.get("chain"),
        "changed_files": bundle.get("changed_files"),
        "verification": bundle.get("verification"),
        "last_error": bundle.get("last_error"),
        "task_git_status_snapshot": bundle.get("task_git_status_snapshot"),
        "current_git_repository": bundle.get("current_git_repository"),
        "current_task_repository": bundle.get("current_task_repository"),
        "current_main_repository": bundle.get("current_main_repository"),
        "current_task_worktree_repository": bundle.get("current_task_worktree_repository"),
        "git_status": bundle.get("git_status"),
        "git_repository": bundle.get("git_repository"),
        "commit_information": bundle.get("commit_information"),
        "git_diff_summary": {
            "kind": git_diff.get("kind"),
            "ref": git_diff.get("ref"),
            "stat": git_diff.get("stat"),
            "warnings": git_diff.get("warnings", []),
        },
        "safety_policy": bundle.get("safety_policy"),
        "transcript_contents_included": bundle.get("transcript_contents_included"),
    }


def detectable_safety_violation(task: dict, bundle: dict[str, Any]) -> bool:
    values = [
        task.get("last_error"),
        task.get("last_result"),
        bundle.get("last_error"),
        bundle.get("last_result"),
        bundle.get("changed_files"),
        bundle.get("verification"),
    ]
    for value in values:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value or "")
        if sanitize(text) != " ".join(text.split()):
            return True
    return False


def safety_detail(task: dict, bundle: dict[str, Any]) -> str:
    return "no obvious secrets or local user paths detected" if not detectable_safety_violation(task, bundle) else "obvious secret or local user path detected"


def review_fingerprint(task: dict, bundle: dict[str, Any]) -> dict[str, Any]:
    repo = review_gate_repository(bundle)
    commit_info = bundle.get("commit_information") if isinstance(bundle.get("commit_information"), dict) else {}
    return {
        "updated_at": task.get("updated_at"),
        "status": task.get("status"),
        "review_status": review_status(task),
        "completed_at": task.get("completed_at"),
        "last_result": task.get("last_result"),
        "repo": {
            "available": repo.get("available"),
            "branch": repo.get("branch"),
            "head": repo.get("head"),
            "dirty": repo.get("dirty"),
            "comparison_ref": repo.get("comparison_ref"),
            "ahead": repo.get("ahead"),
            "behind": repo.get("behind"),
            "has_unpushed": repo.get("has_unpushed"),
        },
        "commit_information": {
            "status": commit_info.get("status"),
            "inferred_commits": commit_info.get("inferred_commits"),
            "ancestry": commit_info.get("ancestry"),
        },
    }


def reviewer_backoff_matches(
    config: Config,
    task: dict[str, Any],
    bundle: dict[str, Any],
    gates: list[dict[str, Any]],
) -> bool:
    marker = task.get("reviewer_codex_backoff") if isinstance(task.get("reviewer_codex_backoff"), dict) else {}
    if not marker:
        return False
    if marker.get("fingerprint") != reviewer_backoff_fingerprint(task, bundle):
        return False
    decision = marker.get("decision")
    if decision in {"needs_human", "failed_review"}:
        return True
    if decision == "needs_fix":
        planner = build_auto_fix_planner_report(config, task, gates)
        return not bool(planner and planner.get("allowed"))
    return False


def reviewer_backoff_marker(
    task: dict[str, Any],
    bundle: dict[str, Any],
    reviewer_result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "decision": sanitize(reviewer_result.get("decision")),
        "reason": sanitize(reviewer_result.get("reason")),
        "recorded_at": iso_now(),
        "fingerprint": reviewer_backoff_fingerprint(task, bundle),
    }


def reviewer_backoff_fingerprint(task: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    fingerprint = dict(review_fingerprint(task, bundle))
    fingerprint.pop("updated_at", None)
    return fingerprint


def build_auto_fix_planner_report(config: Config, task: dict[str, Any], gates: list[dict[str, Any]]) -> dict[str, Any] | None:
    reviewer = task.get("reviewer_codex") if isinstance(task.get("reviewer_codex"), dict) else {}
    if reviewer.get("decision") != "needs_fix":
        return None
    parent_task_id = str(task.get("id") or "")
    root_task_id = str(task.get("root_task_id") or parent_task_id)
    review_cycle = non_negative_int(task.get("review_cycle"))
    fix_attempts = non_negative_int(task.get("fix_attempts"))
    max_fix_attempts = config.auto_review_codex_max_fix_loops_per_task
    skip_reasons: list[dict[str, str]] = []

    if max_fix_attempts < 1:
        skip_reasons.append(skip_reason("disabled_config", "auto_review_codex_max_fix_loops_per_task is zero"))
    if not bool(reviewer.get("auto_fix_allowed", False)):
        skip_reasons.append(skip_reason("disabled_config", "reviewer did not set auto_fix_allowed=true"))

    confidence = str(reviewer.get("confidence") or "")
    risk = str(reviewer.get("auto_fix_risk") or "")
    if confidence not in AUTO_FIX_ALLOWED_CONFIDENCE or risk != "low":
        skip_reasons.append(skip_reason("confidence_risk_mismatch", f"confidence={confidence or 'missing'} risk={risk or 'missing'}"))

    suggested_fix_prompt = sanitize(reviewer.get("suggested_fix_prompt", ""))
    if not suggested_fix_prompt.strip():
        skip_reasons.append(skip_reason("missing_suggested_fix_prompt", "reviewer did not provide a bounded fix prompt"))

    if repeated_finding(task, reviewer):
        skip_reasons.append(skip_reason("repeated_finding", "finding fingerprint already appeared in this chain"))

    skip_reasons.extend(cooldown_limit_stale_skip_reasons(config, task, gates, fix_attempts, max_fix_attempts))

    high_risk = high_risk_blocker(reviewer)
    if high_risk:
        skip_reasons.append(skip_reason("high_risk_blocker", high_risk))

    report: dict[str, Any] = {
        "allowed": not skip_reasons,
        "skip_reasons": skip_reasons,
        "config": {
            "auto_review_codex_max_fix_loops_per_task": max_fix_attempts,
        },
        "chain": {
            "root_task_id": root_task_id,
            "parent_task_id": parent_task_id,
            "review_cycle": review_cycle,
            "fix_attempts": fix_attempts,
        },
        "fix_task_draft": None,
    }
    if not skip_reasons:
        report["fix_task_draft"] = build_fix_task_draft(task, reviewer, root_task_id, parent_task_id, review_cycle)
    return report


def skip_reason(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": sanitize(detail)}


def repeated_finding(task: dict[str, Any], reviewer: dict[str, Any]) -> bool:
    current = normalized_fingerprints(reviewer.get("finding_fingerprints"))
    if not current or non_negative_int(task.get("fix_attempts")) < 1:
        return False
    previous = normalized_fingerprints(task.get("finding_fingerprints"))
    return bool(current.intersection(previous))


def normalized_fingerprints(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {sanitize(item).strip().lower() for item in value if sanitize(item).strip()}


def cooldown_limit_stale_skip_reasons(
    config: Config,
    task: dict[str, Any],
    gates: list[dict[str, Any]],
    fix_attempts: int,
    max_fix_attempts: int,
) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    if in_global_cooldown(config):
        reasons.append(skip_reason("cooldown_limit_stale_gate", "global cooldown is active"))
    if in_reviewer_codex_cooldown(config):
        reasons.append(skip_reason("cooldown_limit_stale_gate", "reviewer Codex cooldown is active"))
    if max_fix_attempts >= 0 and fix_attempts >= max_fix_attempts:
        reasons.append(skip_reason("cooldown_limit_stale_gate", "fix loop limit is exhausted"))
    if task.get("last_auto_fix_task_id"):
        reasons.append(skip_reason("cooldown_limit_stale_gate", "last auto-fix task is already recorded"))
    failed_gates = [str(gate.get("name")) for gate in gates if isinstance(gate, dict) and not gate.get("ok")]
    if failed_gates:
        reasons.append(skip_reason("cooldown_limit_stale_gate", "mechanical gates failed: " + ",".join(failed_gates)))
    return reasons


def high_risk_blocker(reviewer: dict[str, Any]) -> str | None:
    if str(reviewer.get("auto_fix_risk") or "") == "high":
        return "reviewer marked auto_fix_risk=high"
    required = reviewer.get("required_human_checks") if isinstance(reviewer.get("required_human_checks"), list) else []
    if required:
        return "reviewer required human checks"
    findings = reviewer.get("findings") if isinstance(reviewer.get("findings"), list) else []
    for finding in findings:
        if isinstance(finding, dict) and finding.get("severity") == "error":
            return "reviewer finding severity=error"
    text = " ".join(
        [
            sanitize(reviewer.get("reason", "")),
            sanitize(reviewer.get("suggested_fix_prompt", "")),
            json.dumps(sanitize_value(findings), ensure_ascii=False, sort_keys=True),
        ]
    ).lower()
    for term in HIGH_RISK_TERMS:
        if term in text:
            return f"reviewer result mentions high-risk term: {term}"
    return None


def build_fix_task_draft(
    task: dict[str, Any],
    reviewer: dict[str, Any],
    root_task_id: str,
    parent_task_id: str,
    review_cycle: int,
) -> dict[str, Any]:
    return {
        "root_task_id": root_task_id,
        "parent_task_id": parent_task_id,
        "review_cycle": review_cycle + 1,
        "bounded_prompt_summary": bounded_prompt_summary(reviewer),
        "project_id": sanitize(task_project_id(task)),
        "category": sanitize(task.get("category")),
        "labels": [sanitize(label) for label in task_labels(task)],
        "cwd": sanitize(task.get("cwd")),
        "depends_on": [parent_task_id],
        "required_verification_summary": required_verification_summary(task),
    }


def bounded_prompt_summary(reviewer: dict[str, Any]) -> str:
    prompt = sanitize(reviewer.get("suggested_fix_prompt", ""))
    findings = reviewer.get("findings") if isinstance(reviewer.get("findings"), list) else []
    finding_summaries = []
    for finding in findings[:3]:
        if isinstance(finding, dict) and finding.get("summary"):
            finding_summaries.append(sanitize(finding.get("summary")))
    prefix = "Fix reviewer needs_fix finding"
    if finding_summaries:
        prefix += ": " + "; ".join(finding_summaries)
    return one_line(f"{prefix}. Bounded fix prompt: {prompt}", 500)


def required_verification_summary(task: dict[str, Any]) -> str:
    last_result = task.get("last_result") if isinstance(task.get("last_result"), dict) else {}
    verification = last_result.get("verification") if isinstance(last_result.get("verification"), list) else []
    if verification:
        return one_line("; ".join(sanitize(item) for item in verification[:5]), 500)
    return "Run focused verification covering the reviewer-requested fix and report commands/results."


def apply_mechanical_accept(config: Config, task_id: str, expected_fingerprint: dict[str, Any]) -> dict[str, Any]:
    task = load_task(config, task_id)
    bundle = build_review_bundle(
        task,
        by_id={item.get("id"): item for item in list_tasks(config)},
        require_accepted_review=config.dependency_requires_accepted_review,
    )
    current = review_fingerprint(task, bundle)
    if current != expected_fingerprint:
        return {
            "mutated": False,
            "decision": "needs_human",
            "reason": "stale review state; task or git metadata changed after gates were computed",
            "review_status": review_status(task),
        }
    gates = mechanical_gates(task, bundle)
    if not all(gate["ok"] for gate in gates):
        return {
            "mutated": False,
            "decision": "needs_human",
            "reason": "mechanical gates no longer pass",
            "review_status": review_status(task),
        }
    task["review_status"] = "accepted"
    task["reviewed_at"] = iso_now()
    task["review_reason"] = "auto-accepted by local mechanical review gates"
    task.pop("reviewer_codex_backoff", None)
    if task.get("chain_status"):
        task["chain_status"] = "accepted"
    save_task(config, task)
    emit_task_event(
        config,
        "task_reviewed",
        task,
        source="review-next",
        summary=f"mechanically auto-accepted task {task_id}",
        payload=transition_payload(task, review_status="accepted", reviewed_at=task.get("reviewed_at")),
    )
    return {
        "mutated": True,
        "decision": "accepted",
        "reason": "all local mechanical gates passed",
        "review_status": "accepted",
    }


def run_reviewer_phase(
    config: Config,
    task_id: str,
    expected_fingerprint: dict[str, Any],
    *,
    mechanical_enabled: bool,
) -> dict[str, Any]:
    if config.auto_review_codex_max_calls_per_run < 1:
        return reviewer_phase_report(
            decision="needs_human",
            reason="reviewer Codex call limit is zero",
            mechanical_enabled=mechanical_enabled,
            reviewer_codex_invoked=False,
        )
    if in_global_cooldown(config):
        return reviewer_phase_report(
            decision="needs_human",
            reason="global cooldown is active",
            mechanical_enabled=mechanical_enabled,
            reviewer_codex_invoked=False,
        )
    if in_reviewer_codex_cooldown(config):
        return reviewer_phase_report(
            decision="needs_human",
            reason="reviewer Codex cooldown is active",
            mechanical_enabled=mechanical_enabled,
            reviewer_codex_invoked=False,
        )

    task = load_task(config, task_id)
    bundle = build_review_bundle(
        task,
        by_id={item.get("id"): item for item in list_tasks(config)},
        require_accepted_review=config.dependency_requires_accepted_review,
    )
    limit_error = bundle_limit_error(config, bundle)
    if limit_error:
        record_reviewer_summary(config, task_id, {"decision": "needs_human", "reason": limit_error}, bundle=bundle)
        return reviewer_phase_report(
            decision="needs_human",
            reason=limit_error,
            mechanical_enabled=mechanical_enabled,
            reviewer_codex_invoked=False,
        )

    outcome = run_reviewer_codex(config, task, bundle, calls_used_this_run=1)
    if outcome.rate_limited:
        cooldown_until = add_seconds(config.auto_review_codex_cooldown_seconds)
        mark_reviewer_codex_rate_limit(config, cooldown_until, task_id)
        record_reviewer_summary(
            config,
            task_id,
            {
                "decision": "failed_review",
                "reason": outcome.reason,
                "rate_limit_markers": sorted(set(outcome.rate_limit_markers or [])),
                "cooldown_until": cooldown_until,
            },
            bundle=bundle,
        )
        return reviewer_phase_report(
            decision="failed_review",
            reason=outcome.reason,
            mechanical_enabled=mechanical_enabled,
            reviewer_codex_invoked=True,
            reviewer_result=outcome.result,
            rate_limited=True,
        )

    reviewer_result = outcome.result or {
        "decision": "failed_review",
        "confidence": "low",
        "reason": outcome.reason,
        "findings": [],
        "required_human_checks": [],
        "suggested_fix_prompt": "",
        "auto_fix_allowed": False,
        "auto_fix_risk": "",
        "finding_fingerprints": [],
        "reviewer_limits": {
            "calls_used_this_run": 1,
            "fix_loops_used_for_task": 0,
            "cooldown_recommended_seconds": 0,
        },
    }
    if reviewer_clear_pass(reviewer_result):
        applied = apply_reviewer_accept(config, task_id, expected_fingerprint, reviewer_result)
        return reviewer_phase_report(
            decision=applied["decision"],
            reason=applied["reason"],
            mechanical_enabled=mechanical_enabled,
            reviewer_codex_invoked=True,
            reviewer_result=reviewer_result,
            mutated=applied["mutated"],
            review_status=applied.get("review_status"),
        )

    record_reviewer_summary(config, task_id, reviewer_result, bundle=bundle)
    return reviewer_phase_report(
        decision=str(reviewer_result.get("decision") or "failed_review"),
        reason=str(reviewer_result.get("reason") or outcome.reason),
        mechanical_enabled=mechanical_enabled,
        reviewer_codex_invoked=True,
        reviewer_result=reviewer_result,
    )


def apply_reviewer_accept(
    config: Config,
    task_id: str,
    expected_fingerprint: dict[str, Any],
    reviewer_result: dict[str, Any],
) -> dict[str, Any]:
    task = load_task(config, task_id)
    bundle = build_review_bundle(
        task,
        by_id={item.get("id"): item for item in list_tasks(config)},
        require_accepted_review=config.dependency_requires_accepted_review,
    )
    current = review_fingerprint(task, bundle)
    if current != expected_fingerprint:
        record_reviewer_summary(
            config,
            task_id,
            {
                **reviewer_result,
                "decision": "needs_human",
                "reason": "stale review state after reviewer Codex pass",
            },
            bundle=bundle,
        )
        return {
            "mutated": False,
            "decision": "needs_human",
            "reason": "stale review state after reviewer Codex pass",
            "review_status": review_status(task),
        }
    gates = mechanical_gates(task, bundle)
    if not all(gate["ok"] for gate in gates):
        record_reviewer_summary(
            config,
            task_id,
            {
                **reviewer_result,
                "decision": "needs_human",
                "reason": "mechanical gates no longer pass after reviewer Codex pass",
            },
            bundle=bundle,
        )
        return {
            "mutated": False,
            "decision": "needs_human",
            "reason": "mechanical gates no longer pass after reviewer Codex pass",
            "review_status": review_status(task),
        }

    task["review_status"] = "accepted"
    task["reviewed_at"] = iso_now()
    task["review_reason"] = "auto-accepted by reviewer Codex clear pass and local mechanical gates"
    task["reviewer_codex"] = compact_reviewer_result(reviewer_result)
    task.pop("reviewer_codex_backoff", None)
    update_task_chain_from_reviewer(task, reviewer_result, accepted=True)
    save_task(config, task)
    emit_task_event(
        config,
        "task_reviewed",
        task,
        source="review-next",
        summary=f"reviewer Codex auto-accepted task {task_id}",
        payload=transition_payload(
            task,
            review_status="accepted",
            reviewed_at=task.get("reviewed_at"),
            reviewer_codex=compact_reviewer_result(reviewer_result),
        ),
    )
    return {
        "mutated": True,
        "decision": "accepted",
        "reason": "reviewer Codex returned high-confidence pass and mechanical gates passed",
        "review_status": "accepted",
    }


def record_reviewer_summary(
    config: Config,
    task_id: str,
    reviewer_result: dict[str, Any],
    *,
    bundle: dict[str, Any] | None = None,
) -> None:
    task = load_task(config, task_id)
    task["reviewer_codex"] = compact_reviewer_result(reviewer_result)
    update_task_chain_from_reviewer(task, reviewer_result, accepted=False)
    if bundle is None:
        bundle = build_review_bundle(
            task,
            by_id={item.get("id"): item for item in list_tasks(config)},
            require_accepted_review=config.dependency_requires_accepted_review,
        )
    task["reviewer_codex_backoff"] = reviewer_backoff_marker(task, bundle, reviewer_result)
    save_task(config, task)
    emit_task_event(
        config,
        "task_reviewer_codex_reviewed",
        task,
        source="review-next",
        summary=f"reviewer Codex decision for task {task_id}: {task['reviewer_codex'].get('decision')}",
        payload=transition_payload(task, reviewer_codex=task["reviewer_codex"]),
    )


def compact_reviewer_result(result: dict[str, Any]) -> dict[str, Any]:
    findings = result.get("findings") if isinstance(result.get("findings"), list) else []
    fingerprints = result.get("finding_fingerprints") if isinstance(result.get("finding_fingerprints"), list) else []
    return {
        "decision": sanitize(result.get("decision")),
        "confidence": sanitize(result.get("confidence")),
        "reason": sanitize(result.get("reason")),
        "findings": sanitize_value(findings[:10]),
        "required_human_checks": [sanitize(item) for item in result.get("required_human_checks", [])[:10]]
        if isinstance(result.get("required_human_checks"), list)
        else [],
        "auto_fix_allowed": bool(result.get("auto_fix_allowed", False)),
        "auto_fix_risk": sanitize(result.get("auto_fix_risk", "")),
        "suggested_fix_prompt": sanitize(result.get("suggested_fix_prompt", "")),
        "finding_fingerprints": [sanitize(item) for item in fingerprints[:20]],
        "reviewer_limits": sanitize_value(result.get("reviewer_limits")) if isinstance(result.get("reviewer_limits"), dict) else {},
        "reviewed_at": sanitize(result.get("reviewed_at") or iso_now()),
    }


def update_task_chain_from_reviewer(task: dict[str, Any], result: dict[str, Any], *, accepted: bool) -> None:
    task_id = str(task.get("id") or "")
    task["root_task_id"] = task.get("root_task_id") or task_id
    task["parent_task_id"] = task.get("parent_task_id") or None
    task["review_cycle"] = non_negative_int(task.get("review_cycle"))
    task["review_attempts"] = non_negative_int(task.get("review_attempts")) + 1
    task["fix_attempts"] = non_negative_int(task.get("fix_attempts"))
    task["last_review_decision"] = sanitize(result.get("decision"))
    task["review_findings"] = compact_reviewer_result(result).get("findings", [])
    task["auto_fix_allowed"] = bool(result.get("auto_fix_allowed", False))
    if isinstance(result.get("reviewer_limits"), dict):
        task["auto_fix_budget"] = sanitize_value(result.get("reviewer_limits"))
    fingerprints = result.get("finding_fingerprints") if isinstance(result.get("finding_fingerprints"), list) else []
    if fingerprints:
        task["finding_fingerprints"] = [sanitize(item) for item in fingerprints[:100]]
    elif "finding_fingerprints" not in task:
        task["finding_fingerprints"] = []
    if accepted:
        task["chain_status"] = "accepted"
    elif result.get("decision") == "needs_fix":
        task["chain_status"] = "needs_fix"
    elif result.get("decision") in {"needs_human", "failed_review"}:
        task["chain_status"] = "needs_human"
    elif result.get("decision") == "pass":
        task["chain_status"] = "needs_human"


def non_negative_int(value: object) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def bundle_limit_error(config: Config, bundle: dict[str, Any]) -> str | None:
    encoded = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
    if len(encoded) > config.auto_review_codex_max_bundle_chars:
        return "review bundle exceeds reviewer Codex bundle size limit"
    diff = bundle.get("git_diff")
    diff_encoded = json.dumps(diff, ensure_ascii=False, sort_keys=True) if isinstance(diff, dict) else ""
    if len(diff_encoded) > config.auto_review_codex_max_diff_chars:
        return "review bundle diff exceeds reviewer Codex diff size limit"
    return None


def reviewer_phase_report(
    *,
    decision: str,
    reason: str,
    mechanical_enabled: bool,
    reviewer_codex_invoked: bool,
    reviewer_result: dict[str, Any] | None = None,
    mutated: bool = False,
    review_status: str | None = None,
    rate_limited: bool = False,
) -> dict[str, Any]:
    summary = auto_review_summary(
        decision=decision if decision != "pass" else "needs_human",
        reason=reason,
        enabled=mechanical_enabled,
        reviewer_codex_enabled=True,
    )
    summary["reviewer_codex_invoked"] = reviewer_codex_invoked
    if reviewer_result is not None:
        summary["reviewer_codex_result"] = compact_reviewer_result(reviewer_result)
    if rate_limited:
        summary["rate_limited"] = True
    return {
        "mutated": mutated,
        "review_status": review_status,
        "auto_review": summary,
    }


def auto_review_summary(
    *,
    decision: str,
    reason: str,
    enabled: bool,
    reviewer_codex_enabled: bool,
    failing_gates: list[object] | None = None,
) -> dict[str, Any]:
    summary = {
        "decision": decision,
        "reason": sanitize(reason),
        "mechanical_auto_accept_enabled": bool(enabled),
        "reviewer_codex_enabled": bool(reviewer_codex_enabled),
        "reviewer_codex_invoked": False,
        "follow_up_enqueued": False,
    }
    if failing_gates:
        summary["failing_gates"] = [sanitize(item) for item in failing_gates]
    return summary


def render_review_next_report(report: dict[str, Any]) -> str:
    lines = [
        f"mode: {report['mode']}",
        f"selected: {str(report['selected']).lower()}",
        f"candidate_count: {report['candidate_count']}",
    ]
    if not report["selected"]:
        lines.append(f"message: {report['message']}")
        skipped = report.get("skipped_review_candidates") if isinstance(report.get("skipped_review_candidates"), list) else []
        if skipped:
            lines.append(f"skipped_review_candidates: {len(skipped)}")
        return "\n".join(lines) + "\n"

    bundle = report.get("bundle") or {}
    task = bundle.get("task") or {}
    lines.extend(
        [
            f"task_id: {report['task_id']}",
            f"review_status: {report['review_status']}",
            f"project_id: {task.get('project_id') or '-'}",
            f"project_root: {task.get('project_root') or '-'}",
            f"gates_ok: {str(report['gates_ok']).lower()}",
            "gates:",
        ]
    )
    for item in report.get("gates", []):
        status = "pass" if item["ok"] else "fail"
        lines.append(f"- {item['name']}: {status} ({item['detail']})")
    auto_review = report.get("auto_review") or {}
    if auto_review:
        lines.extend(
            [
                "auto_review:",
                f"- decision: {auto_review.get('decision')}",
                f"- reason: {auto_review.get('reason')}",
                f"- reviewer_codex_invoked: {str(bool(auto_review.get('reviewer_codex_invoked'))).lower()}",
            ]
        )
    skipped = report.get("skipped_review_candidates") if isinstance(report.get("skipped_review_candidates"), list) else []
    if skipped:
        lines.append(f"skipped_review_candidates: {len(skipped)}")
    chain = report.get("chain") if isinstance(report.get("chain"), dict) else {}
    if chain:
        parts = [
            f"status={chain.get('chain_status') or '-'}",
            f"decision={chain.get('last_review_decision') or '-'}",
            f"cycle={chain.get('review_cycle', '-')}",
            f"review_attempts={chain.get('review_attempts', '-')}",
            f"fix_attempts={chain.get('fix_attempts', '-')}",
        ]
        lines.append("chain: " + " ".join(parts))
    auto_fix_planner = report.get("auto_fix_planner") if isinstance(report.get("auto_fix_planner"), dict) else {}
    if auto_fix_planner:
        lines.append(f"auto_fix_planner: allowed={str(bool(auto_fix_planner.get('allowed'))).lower()}")
        skip_reasons = auto_fix_planner.get("skip_reasons") if isinstance(auto_fix_planner.get("skip_reasons"), list) else []
        for reason in skip_reasons[:5]:
            if isinstance(reason, dict):
                lines.append(f"- skip: {reason.get('code')} ({reason.get('detail')})")
        draft = auto_fix_planner.get("fix_task_draft") if isinstance(auto_fix_planner.get("fix_task_draft"), dict) else {}
        if draft:
            lines.append(
                "fix_task_draft: "
                f"root={draft.get('root_task_id')} "
                f"parent={draft.get('parent_task_id')} "
                f"cycle={draft.get('review_cycle')} "
                f"depends_on={','.join(draft.get('depends_on') or [])}"
            )
    deps = report.get("dependencies") or {}
    blockers = deps.get("blockers") or []
    if blockers:
        blocked_by = ",".join(
            f"{item.get('id')}:{item.get('reason')}" if isinstance(item, dict) else str(item) for item in blockers
        )
    else:
        blocked_by = ",".join(deps.get("blocked_by") or []) or "-"
    lines.append(
        "dependencies: "
        f"ready={str(bool(deps.get('ready'))).lower()} "
        f"requires_accepted_review={str(bool(deps.get('requires_accepted_review'))).lower()} "
        f"blocked_by={blocked_by}"
    )
    append_worktree_summary(lines, report.get("worktree_report"))
    append_result_summary(lines, bundle)
    lines.append("dry_run: no task state changed; reviewer Codex not invoked")
    return "\n".join(lines).rstrip() + "\n"


def append_result_summary(lines: list[str], bundle: dict[str, Any]) -> None:
    last_result = bundle.get("last_result") if isinstance(bundle.get("last_result"), dict) else {}
    if last_result.get("summary"):
        lines.append("summary: " + one_line(last_result.get("summary"), 240))
    changed_files = bundle.get("changed_files") if isinstance(bundle.get("changed_files"), dict) else {}
    reported = changed_files.get("reported") if isinstance(changed_files, dict) else []
    lines.append(f"changed_files_reported: {len(reported) if isinstance(reported, list) else 0}")
    verification = bundle.get("verification") if isinstance(bundle.get("verification"), list) else []
    lines.append(f"verification_count: {len(verification)}")
    git_repo = bundle.get("current_git_repository") if isinstance(bundle.get("current_git_repository"), dict) else {}
    if git_repo:
        lines.append(f"git: branch={git_repo.get('branch') or '-'} dirty={git_repo.get('dirty')}")
    commit_info = bundle.get("commit_information") if isinstance(bundle.get("commit_information"), dict) else {}
    if commit_info:
        reported_count = len(commit_info.get("reported") or [])
        lines.append(f"commit_information: status={commit_info.get('status')} reported={reported_count}")
        ancestry = commit_info.get("ancestry") if isinstance(commit_info.get("ancestry"), dict) else {}
        if ancestry:
            lines.append("commit_ancestry: " + commit_ancestry_detail(commit_info))
    diff = bundle.get("git_diff_summary") if isinstance(bundle.get("git_diff_summary"), dict) else {}
    if diff:
        lines.append(f"git_diff: kind={diff.get('kind')} ref={diff.get('ref') or '-'}")


def append_worktree_summary(lines: list[str], report: object) -> None:
    if not isinstance(report, dict):
        return
    metadata = report.get("metadata") if isinstance(report.get("metadata"), dict) else {}
    if metadata.get("execution_mode") == "main_worktree" and not report.get("warnings"):
        return
    missing = ",".join(report.get("missing_metadata") or []) or "-"
    stale = ",".join(report.get("stale_metadata") or []) or "-"
    warnings = len(report.get("warnings") or [])
    lines.append(
        "worktree: "
        f"mode={metadata.get('execution_mode') or '-'} "
        f"branch={metadata.get('branch') or '-'} "
        f"status={metadata.get('worktree_status') or '-'} "
        f"path_exists={report.get('path_exists')} "
        f"branch_exists={report.get('branch_exists')} "
        f"recovery_required={str(bool(report.get('recovery_required'))).lower()} "
        f"missing={missing} stale={stale} warnings={warnings}"
    )


def one_line(value: object, limit: int) -> str:
    text = " ".join(sanitize(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."

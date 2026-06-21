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
    dependency_status,
    list_tasks,
    load_task,
    save_task,
    task_labels,
    task_project_id,
    task_project_root,
)
from .review_bundle import build_review_bundle
from .reviewer_codex import reviewer_clear_pass, run_reviewer_codex
from .state import in_global_cooldown, in_reviewer_codex_cooldown, mark_reviewer_codex_rate_limit
from .summary import review_status
from .timeutil import add_seconds, iso_now
from .transcript import sanitize

REVIEW_NEEDED_STATUSES = {"unreviewed", "rejected", "needs_followup"}


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
) -> dict[str, Any]:
    report = select_review_next_report(config, filters, mode="apply")
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
    report["auto_review"] = auto_review_summary(
        decision=applied["decision"],
        reason=applied["reason"],
        enabled=True,
        reviewer_codex_enabled=False,
    )
    return report


def select_review_next_report(config: Config, filters: Namespace | None = None, *, mode: str) -> dict[str, Any]:
    tasks = list_tasks(config)
    by_id = {task.get("id"): task for task in tasks}
    candidates = [task for task in tasks if is_review_needed(task)]
    candidates = apply_filters(candidates, filters)
    candidates.sort(key=review_sort_key)
    if not candidates:
        return no_selection_report(
            mode=mode,
            message="no completed task needs review",
            filters=filter_summary(filters),
        )

    task = candidates[0]
    bundle = build_review_bundle(
        task,
        by_id=by_id,
        require_accepted_review=config.dependency_requires_accepted_review,
    )
    gates = mechanical_gates(task, bundle)
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
        "review_status": review_status(task),
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
) -> dict[str, Any]:
    return {
        "mode": mode,
        "status": status,
        "selected": False,
        "task_id": None,
        "candidate_count": 0,
        "message": message,
        "filters": filters,
        "gates_ok": False,
        "gates": [],
        "dependencies": None,
        "review_status": None,
        "bundle": None,
        "mutated": False,
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
    repo = bundle.get("current_git_repository") if isinstance(bundle.get("current_git_repository"), dict) else {}
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
        gate("safety_metadata_clean", not detectable_safety_violation(task, bundle), safety_detail(task, bundle)),
    ]


def gate(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": sanitize(detail)}


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
        "changed_files": bundle.get("changed_files"),
        "verification": bundle.get("verification"),
        "last_error": bundle.get("last_error"),
        "task_git_status_snapshot": bundle.get("task_git_status_snapshot"),
        "current_git_repository": bundle.get("current_git_repository"),
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
    repo = bundle.get("current_git_repository") if isinstance(bundle.get("current_git_repository"), dict) else {}
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
        },
    }


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
        record_reviewer_summary(config, task_id, {"decision": "needs_human", "reason": limit_error})
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

    record_reviewer_summary(config, task_id, reviewer_result)
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


def record_reviewer_summary(config: Config, task_id: str, reviewer_result: dict[str, Any]) -> None:
    task = load_task(config, task_id)
    task["reviewer_codex"] = compact_reviewer_result(reviewer_result)
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
    return {
        "decision": sanitize(result.get("decision")),
        "confidence": sanitize(result.get("confidence")),
        "reason": sanitize(result.get("reason")),
        "findings": findings[:10],
        "required_human_checks": result.get("required_human_checks", [])[:10]
        if isinstance(result.get("required_human_checks"), list)
        else [],
        "suggested_fix_prompt": sanitize(result.get("suggested_fix_prompt", "")),
        "reviewer_limits": result.get("reviewer_limits") if isinstance(result.get("reviewer_limits"), dict) else {},
        "reviewed_at": sanitize(result.get("reviewed_at") or iso_now()),
    }


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
    diff = bundle.get("git_diff_summary") if isinstance(bundle.get("git_diff_summary"), dict) else {}
    if diff:
        lines.append(f"git_diff: kind={diff.get('kind')} ref={diff.get('ref') or '-'}")


def one_line(value: object, limit: int) -> str:
    text = " ".join(sanitize(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."

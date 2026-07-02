from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .events import transition_payload, write_event_nonfatal
from .lock import FileLock
from .queue import create_task, load_task, save_task, task_labels, task_project_id, task_title, truncate_text
from .timeutil import iso_now


PREPARE_OK_STATUSES = {"runnable", "needs_resume"}
WORKTREE_RETAINED_STATUSES = {"prepared", "running", "retained", "cleanup_candidate"}
APPLY_OK_WORKTREE_STATUSES = {"prepared", "retained", "cleanup_candidate"}
CLEANUP_OK_WORKTREE_STATUSES = {"prepared", "retained", "cleanup_candidate"}
DISCARD_CLEANUP_RESOLUTIONS = {"duplicate", "manual", "superseded", "wont_fix"}


def sanitize_branch_name(task_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id.strip())
    slug = re.sub(r"[-_.]{2,}", "-", slug).strip("-._")
    slug = slug.replace("@{", "-")
    if not slug:
        slug = "task"
    if slug.endswith(".lock"):
        slug = slug[: -len(".lock")] or "task"
    branch = f"cbr/{slug[:180]}"
    validate_branch_name(branch)
    return branch


def build_prepare_report(config: Config, task_id: str, *, apply: bool = False) -> dict[str, Any]:
    if apply:
        return _with_lock(config, task_id, lambda: _build_prepare_report_locked(config, task_id, apply=True))
    return _build_prepare_report_locked(config, task_id, apply=False)


def build_cleanup_report(config: Config, task_id: str, *, apply: bool = False) -> dict[str, Any]:
    if apply:
        return _with_lock(config, task_id, lambda: _build_cleanup_report_locked(config, task_id, apply=True))
    return _build_cleanup_report_locked(config, task_id, apply=False)


def build_branch_prune_report(config: Config, task_id: str, *, apply: bool = False) -> dict[str, Any]:
    if apply:
        return _with_lock(config, task_id, lambda: _build_branch_prune_report_locked(config, task_id, apply=True))
    return _build_branch_prune_report_locked(config, task_id, apply=False)


def build_apply_report(config: Config, task_id: str, *, apply: bool = False) -> dict[str, Any]:
    if apply:
        return _with_lock(config, task_id, lambda: build_apply_report_locked(config, task_id, apply=True))
    return build_apply_report_locked(config, task_id, apply=False)


def build_apply_report_locked(config: Config, task_id: str, *, apply: bool = False) -> dict[str, Any]:
    return _build_apply_report_locked(config, task_id, apply=apply)


def prepare_task_worktree_for_run_locked(config: Config, task: dict[str, Any]) -> dict[str, Any]:
    task_id = str(task.get("id") or "")
    if task.get("status") == "needs_resume":
        validation_error = validate_resume_worktree(task)
        if validation_error:
            report = base_report("prepare", task, apply=True)
            report["errors"].append(validation_error)
            return {"task": task, "worktree_path": None, "report": report}

    report = _build_prepare_report_locked(config, task_id, apply=True)
    if report.get("errors") or not report.get("applied"):
        return {"task": task, "worktree_path": None, "report": report}
    prepared = load_task(config, task_id)
    return {"task": prepared, "worktree_path": Path(str(report.get("worktree_path"))), "report": report}


def validate_resume_worktree(task: dict[str, Any]) -> str | None:
    if task.get("execution_mode") != "git_worktree":
        return "needs_resume task requires an existing retained git worktree"
    if str(task.get("execution_worktree_status") or "") not in WORKTREE_RETAINED_STATUSES:
        return "needs_resume task worktree is not retained for recovery"
    if not task.get("execution_branch") or not task.get("execution_worktree_path"):
        return "needs_resume task has incomplete retained worktree metadata"
    return None


def _with_lock(config: Config, task_id: str, callback) -> dict[str, Any]:
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire(task_id=task_id):
        return {
            "task_id": task_id,
            "action": "locked",
            "applied": False,
            "errors": [f"another runner is active: {config.lock_file}"],
            "warnings": [],
        }
    try:
        return callback()
    finally:
        lock.release()


def _build_prepare_report_locked(config: Config, task_id: str, *, apply: bool) -> dict[str, Any]:
    task = load_task(config, task_id)
    report = base_report("prepare", task, apply)
    if config.worktree_mode != "task":
        report["errors"].append("worktree_mode is disabled; set worktree_mode=task to prepare task worktrees")
        return report
    if task.get("status") not in PREPARE_OK_STATUSES and task.get("execution_worktree_status") != "prepared":
        report["errors"].append(f"task status {task.get('status')} is not eligible for worktree prepare")
        return report

    try:
        repo = repo_context(task)
        branch = sanitize_branch_name(str(task.get("id") or task_id))
        worktree_path = guarded_worktree_path(config, branch)
        registry = worktree_registry(repo["repo_root"])
        branch_state = local_branch_state(repo["repo_root"], branch)
        classification = classify_prepare_state(task, branch, worktree_path, registry, branch_state)
        report.update(
            {
                "repo_root": str(repo["repo_root"]),
                "base_ref": repo["base_ref"],
                "base_head": repo["base_head"],
                "branch": branch,
                "worktree_path": str(worktree_path),
                "classification": classification,
            }
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        report["errors"].append(str(exc))
        return report

    if classification["status"] == "recovery_required":
        report["errors"].append(classification["reason"])
    elif classification["status"] == "prepared":
        report["warnings"].append(classification["reason"])
    elif branch_state["exists"]:
        report["errors"].append("existing branch is not linked to this task metadata")

    previous_base = task.get("execution_base_head")
    if previous_base and previous_base != repo["base_head"] and task.get("execution_worktree_status") != "prepared":
        report["errors"].append("stale base: task metadata records a different execution_base_head")

    if report["errors"] or not apply:
        return report

    if classification["status"] == "absent":
        git(repo["repo_root"], "worktree", "add", "-b", branch, str(worktree_path), repo["base_head"])

    task.update(
        {
            "execution_mode": "git_worktree",
            "execution_original_cwd": task.get("cwd"),
            "execution_repo_root": str(repo["repo_root"]),
            "execution_worktree_path": str(worktree_path),
            "execution_worktree_root": str(config.worktree_root),
            "execution_branch": branch,
            "execution_base_ref": repo["base_ref"],
            "execution_base_head": repo["base_head"],
            "execution_worktree_status": "prepared",
            "execution_prepared_at": iso_now(),
        }
    )
    save_task(config, task)
    report["applied"] = True
    report["classification"] = {**classification, "status": "prepared", "reason": "worktree prepared"}
    write_event_nonfatal(
        config,
        "task_worktree_prepared",
        task=task,
        source="worktree prepare",
        summary=f"prepared worktree for task {task_id}",
        payload=transition_payload(
            task,
            execution_mode="git_worktree",
            execution_branch=branch,
            execution_worktree_status="prepared",
        ),
    )
    return report


def _build_cleanup_report_locked(config: Config, task_id: str, *, apply: bool) -> dict[str, Any]:
    task = load_task(config, task_id)
    report = base_report("cleanup", task, apply)
    branch = str(task.get("execution_branch") or "")
    worktree_raw = task.get("execution_worktree_path")
    apply_status = str(task.get("execution_apply_status") or "")
    report["apply_status"] = apply_status or "-"
    missing = [
        name
        for name, value in (
            ("execution_branch", branch),
            ("execution_worktree_path", worktree_raw),
            ("execution_worktree_status", task.get("execution_worktree_status")),
            ("execution_repo_root", task.get("execution_repo_root")),
        )
        if not value
    ]
    if missing:
        report["classification"] = {"status": "missing", "reason": "task has no worktree metadata"}
        report["errors"].append("worktree cleanup requires retained worktree metadata; missing: " + ", ".join(missing))
        return report
    eligibility = cleanup_eligibility(task)
    if eligibility.get("cleanup_kind"):
        report["cleanup_kind"] = eligibility["cleanup_kind"]
        report["cleanup_reason"] = eligibility.get("cleanup_reason")
    if eligibility.get("error"):
        report["errors"].append(str(eligibility["error"]))
        return report
    try:
        validate_branch_name(branch)
        worktree_path = guarded_existing_worktree_path(config, Path(str(worktree_raw)))
        repo_root = Path(str(task.get("execution_repo_root") or task.get("project_root") or task.get("cwd"))).expanduser().resolve()
        registry = worktree_registry(repo_root)
        classification = classify_cleanup_state(task, branch, worktree_path, registry)
        if classification["status"] == "cleanup_candidate" and eligibility.get("cleanup_kind") == "applied":
            applied_check = verify_applied_cleanup_target(task, repo_root)
            report["applied_metadata"] = applied_check
            if applied_check["status"] != "current":
                classification = {
                    "status": "recovery_required",
                    "reason": applied_check["reason"],
                }
        report.update(
            {
                "repo_root": str(repo_root),
                "branch": branch,
                "worktree_path": str(worktree_path),
                "classification": classification,
            }
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        report["errors"].append(str(exc))
        return report

    if classification["status"] in {"missing", "recovery_required"}:
        report["errors"].append(classification["reason"])
    if report["errors"] or not apply:
        return report

    if classification["status"] == "cleanup_candidate":
        git(repo_root, "worktree", "remove", str(worktree_path))
    task["execution_worktree_status"] = "cleaned"
    task["execution_cleaned_at"] = iso_now()
    task["execution_cleanup_kind"] = eligibility.get("cleanup_kind")
    task["execution_cleanup_reason"] = eligibility.get("cleanup_reason")
    task["execution_cleanup_branch_retained"] = True
    task["execution_cleanup_result_applied"] = eligibility.get("cleanup_kind") == "applied"
    save_task(config, task)
    report["applied"] = True
    if eligibility.get("cleanup_kind") == "discard":
        reason = "worktree cleaned after explicit discard; branch retained; task result was not applied"
    else:
        reason = "worktree cleaned; branch retained"
    report["classification"] = {**classification, "status": "cleaned", "reason": reason}
    write_event_nonfatal(
        config,
        "task_worktree_cleaned",
        task=task,
        source="worktree cleanup",
        summary=f"cleaned worktree for task {task_id}",
        payload=transition_payload(
            task,
            execution_mode=task.get("execution_mode"),
            execution_branch=branch,
            execution_worktree_status="cleaned",
            execution_apply_status=task.get("execution_apply_status"),
            execution_applied_head=task.get("execution_applied_head"),
            execution_cleanup_kind=task.get("execution_cleanup_kind"),
            execution_cleanup_reason=task.get("execution_cleanup_reason"),
            execution_cleanup_branch_retained=task.get("execution_cleanup_branch_retained"),
            execution_cleanup_result_applied=task.get("execution_cleanup_result_applied"),
        ),
    )
    return report


def _build_branch_prune_report_locked(config: Config, task_id: str, *, apply: bool) -> dict[str, Any]:
    task = load_task(config, task_id)
    report = base_report("branch-prune", task, apply)
    report["planned_action"] = "git branch -d <execution_branch>"
    validate_branch_prune_report(task, report)
    if report["errors"] or not apply:
        return report
    if report.get("branch_exists") is False:
        return report

    repo_root = Path(str(report["repo_root"]))
    branch = str(report["branch"])
    head = str(report.get("branch_head") or "")
    git(repo_root, "branch", "-d", branch)
    now = iso_now()
    task["execution_branch_prune_status"] = "pruned"
    task["execution_branch_pruned_at"] = now
    task["execution_branch_prune_reason"] = report.get("prune_reason")
    task["execution_branch_pruned_head"] = head
    task["execution_cleanup_branch_retained"] = False
    save_task(config, task)
    report["applied"] = True
    report["classification"] = {"status": "pruned", "reason": "local task branch deleted with git branch -d"}
    write_event_nonfatal(
        config,
        "task_worktree_branch_pruned",
        task=task,
        source="worktree branch-prune",
        summary=f"pruned worktree branch for task {task_id}",
        payload=transition_payload(
            task,
            execution_mode=task.get("execution_mode"),
            execution_branch=branch,
            execution_worktree_status=task.get("execution_worktree_status"),
            execution_apply_status=task.get("execution_apply_status"),
            execution_applied_head=task.get("execution_applied_head"),
            execution_branch_prune_status=task.get("execution_branch_prune_status"),
            execution_branch_prune_reason=task.get("execution_branch_prune_reason"),
            execution_branch_pruned_head=task.get("execution_branch_pruned_head"),
        ),
    )
    return report


def validate_branch_prune_report(task: dict[str, Any], report: dict[str, Any]) -> None:
    gates: list[dict[str, Any]] = []
    report["gates"] = gates

    def gate(name: str, ok: bool, detail: str, error_detail: str | None = None) -> None:
        gates.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            report["errors"].append(error_detail or detail)

    gate(
        "execution_mode_git_worktree",
        task.get("execution_mode") == "git_worktree",
        f"execution_mode={task.get('execution_mode') or '-'}",
        "branch prune requires execution_mode=git_worktree",
    )

    branch = str(task.get("execution_branch") or "").strip()
    repo_raw = task.get("execution_repo_root") or task.get("project_root") or task.get("cwd")
    missing = [
        name
        for name, value in (
            ("execution_branch", branch),
            ("execution_repo_root", repo_raw),
            ("execution_worktree_status", task.get("execution_worktree_status")),
        )
        if not value
    ]
    gate(
        "required_branch_metadata",
        not missing,
        "required branch metadata present" if not missing else "missing: " + ", ".join(missing),
        "branch prune requires worktree branch metadata; missing: " + ", ".join(missing),
    )
    gate(
        "worktree_status_cleaned",
        task.get("execution_worktree_status") == "cleaned",
        f"execution_worktree_status={task.get('execution_worktree_status') or '-'}",
        "branch prune requires execution_worktree_status=cleaned",
    )
    gate(
        "cleanup_was_applied",
        task.get("execution_cleanup_kind") == "applied" and task.get("execution_cleanup_result_applied") is True,
        (
            f"execution_cleanup_kind={task.get('execution_cleanup_kind') or '-'} "
            f"execution_cleanup_result_applied={task.get('execution_cleanup_result_applied')}"
        ),
        "branch prune is currently limited to applied worktree cleanup; discard-cleaned branches are retained",
    )
    gate(
        "execution_apply_status_applied",
        task.get("execution_apply_status") == "applied",
        f"execution_apply_status={task.get('execution_apply_status') or '-'}",
        "branch prune requires execution_apply_status=applied",
    )
    gate(
        "task_accepted_or_archived",
        (task.get("status") == "completed" and task.get("review_status") == "accepted") or task.get("status") == "archived",
        f"status={task.get('status') or '-'} review_status={task.get('review_status') or '-'}",
        "branch prune requires an applied completed+accepted or archived task",
    )
    if missing or not branch or not repo_raw:
        report["gates_ok"] = False
        return

    branch_valid = True
    try:
        validate_branch_name(branch)
    except ValueError as exc:
        branch_valid = False
        gate("branch_ref_valid", False, str(exc))
    if branch_valid:
        gate("branch_ref_valid", True, "branch name passes git ref validation")

    namespace_ok = branch.startswith("cbr/") and not branch.startswith("origin/")
    protected = is_protected_branch_name(branch, task)
    gate(
        "branch_namespace_cbr",
        namespace_ok and not protected,
        f"branch={branch}",
        "branch prune only deletes local cbr/* task branches; protected, remote, and non-cbr branches are rejected",
    )
    expected_branch = sanitize_branch_name(str(task.get("id") or ""))
    report["expected_branch"] = expected_branch
    gate(
        "branch_matches_task_id",
        branch == expected_branch,
        f"branch={branch} expected_branch={expected_branch}",
        "branch prune requires execution_branch to match the sanitized task id branch",
    )
    if not branch_valid:
        report["gates_ok"] = False
        return

    try:
        repo_root = Path(str(repo_raw)).expanduser().resolve()
        repo_top = git(repo_root, "rev-parse", "--show-toplevel")
        registry = worktree_registry(repo_root)
        branch_state = local_branch_state(repo_root, branch)
        current_branch = git_optional(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD") or "HEAD"
    except (OSError, subprocess.CalledProcessError) as exc:
        gate("git_state_available", False, clean_git_exception(exc) if isinstance(exc, subprocess.CalledProcessError) else str(exc))
        report["gates_ok"] = False
        return

    report.update(
        {
            "repo_root": repo_top,
            "branch": branch,
            "branch_exists": branch_state.get("exists"),
            "current_branch": current_branch,
            "prune_reason": "execution_apply_status=applied",
        }
    )
    if branch_state.get("head"):
        report["branch_head"] = branch_state.get("head")
    if not branch_state.get("exists"):
        report["classification"] = {"status": "missing", "reason": "local task branch is already absent; nothing to prune"}
        report["gates_ok"] = not report["errors"]
        return

    gate("git_state_available", True, "git state is available")
    checked_out = registry_entry_for_branch(registry, branch) is not None
    gate(
        "branch_not_checked_out",
        not checked_out,
        "branch is not checked out in any git worktree" if not checked_out else "branch is checked out in a git worktree",
        "branch prune refuses a branch checked out in the git worktree registry",
    )
    gate(
        "branch_not_current",
        current_branch != branch,
        f"current_branch={current_branch}",
        "branch prune refuses the current checked-out branch",
    )
    expected_head = branch_prune_expected_head(task)
    report["expected_head"] = expected_head
    gate(
        "expected_head_available",
        bool(expected_head),
        f"expected_head={expected_head or '-'}",
        "branch prune requires reliable expected head metadata such as execution_applied_head",
    )
    if expected_head:
        gate(
            "branch_head_matches_expected",
            branch_state.get("head") == expected_head,
            f"branch_head={branch_state.get('head') or '-'} expected_head={expected_head}",
            "branch HEAD does not match recorded expected head metadata",
        )
    if not report["errors"]:
        report["classification"] = {"status": "eligible", "reason": "local applied cbr task branch can be pruned"}
    report["gates_ok"] = not report["errors"]


def branch_prune_expected_head(task: dict[str, Any]) -> str | None:
    for key in ("execution_applied_head", "execution_branch_head", "execution_rebased_head"):
        value = str(task.get(key) or "").strip()
        if value:
            return value
    return None


def is_protected_branch_name(branch: str, task: dict[str, Any]) -> bool:
    if branch in {"main", "master", "develop"} or branch.startswith("release/") or branch.startswith("origin/"):
        return True
    for key in ("execution_apply_target", "execution_base_ref", "execution_merge_target"):
        target = str(task.get(key) or "").strip()
        if target and target != "HEAD" and branch == target:
            return True
    return False


def cleanup_eligibility_error(task: dict[str, Any]) -> str | None:
    eligibility = cleanup_eligibility(task)
    error = eligibility.get("error")
    return str(error) if error else None


def cleanup_eligibility(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("execution_mode") != "git_worktree":
        return {"error": "worktree cleanup requires execution_mode=git_worktree"}

    status = str(task.get("status") or "")
    review = str(task.get("review_status") or "")
    resolution = str(task.get("resolution") or "")
    applied = has_applied_worktree_metadata(task)

    if applied:
        if (status == "completed" and review == "accepted") or status == "archived":
            return {"cleanup_kind": "applied", "cleanup_reason": "execution_apply_status=applied"}
        return {
            "error": (
                "worktree cleanup found applied metadata, but applied cleanup is only allowed for "
                "archived or completed accepted tasks"
            )
        }

    if status in {"completed", "archived"} and review == "accepted":
        return {
            "error": (
                "worktree cleanup requires execution_apply_status=applied before removing retained worktree; "
                f"found execution_apply_status={task.get('execution_apply_status') or '-'}"
            )
        }

    if resolution in DISCARD_CLEANUP_RESOLUTIONS and status in {"archived", "blocked_user", "completed", "failed"} and review != "accepted":
        return {"cleanup_kind": "discard", "cleanup_reason": f"resolution={resolution}"}

    if status not in {"completed", "archived"}:
        return {"error": "worktree cleanup is only allowed for completed, archived, or resolved terminal worktree tasks"}

    if status in {"completed", "archived"} and review == "rejected":
        return {"cleanup_kind": "discard", "cleanup_reason": "review_status=rejected"}

    if review == "needs_followup":
        return {
            "error": (
                "worktree cleanup requires execution_apply_status=applied or an explicit terminal discard "
                "resolution before removing a needs_followup retained worktree"
            )
        }

    return {
        "error": (
            "worktree cleanup requires execution_apply_status=applied, review_status=rejected, "
            "or terminal resolution=duplicate|manual|superseded|wont_fix"
        )
    }


def has_applied_worktree_metadata(task: dict[str, Any]) -> bool:
    if task.get("execution_apply_status") == "applied":
        return True
    return bool(task.get("execution_applied_at") and task.get("execution_applied_head"))


def verify_applied_cleanup_target(task: dict[str, Any], repo_root: Path) -> dict[str, str]:
    applied_head = str(task.get("execution_applied_head") or "").strip()
    target = str(task.get("execution_apply_target") or "").strip()
    if not applied_head:
        return {
            "status": "stale_applied_metadata",
            "reason": "stale applied metadata: missing execution_applied_head",
        }
    if not target:
        target = git_optional(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD") or "HEAD"
    try:
        verified_applied_head = git(repo_root, "rev-parse", "--verify", f"{applied_head}^{{commit}}")
        target_head = git(repo_root, "rev-parse", "--verify", f"{target}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        detail = clean_git_exception(exc)
        return {
            "status": "stale_applied_metadata",
            "reason": f"stale applied metadata: cannot verify execution_applied_head against apply target {target}: {detail}",
            "apply_target": target,
        }
    if not is_ancestor(repo_root, verified_applied_head, target_head):
        return {
            "status": "stale_applied_metadata",
            "reason": (
                "stale applied metadata: execution_applied_head is not contained in "
                f"current apply target {target}"
            ),
            "apply_target": target,
            "execution_applied_head": verified_applied_head,
            "target_head": target_head,
        }
    return {
        "status": "current",
        "reason": "execution_applied_head is contained in current apply target",
        "apply_target": target,
        "execution_applied_head": verified_applied_head,
        "target_head": target_head,
    }


def _build_apply_report_locked(config: Config, task_id: str, *, apply: bool) -> dict[str, Any]:
    task = load_task(config, task_id)
    report = base_report("apply", task, apply)
    report["planned_action"] = "git merge --ff-only <execution_branch>"
    validate_apply_report(config, task, report)
    if report["errors"]:
        if apply and report.get("apply_strategy") == "stale_base_rebase" and report.get("rebase", {}).get("status") == "blocked":
            record_rebase_conflict_fix(config, task, report)
        return report
    if apply and report.get("apply_strategy") == "stale_base_rebase":
        return apply_stale_base_rebase(config, task, report)
    if report["errors"] or not apply:
        return report

    repo_root = Path(str(report["repo_root"]))
    branch = str(report["branch"])
    git(repo_root, "merge", "--ff-only", branch)
    applied_head = git(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    task["execution_apply_status"] = "applied"
    task["execution_applied_at"] = iso_now()
    task["execution_applied_head"] = applied_head
    task["execution_apply_target"] = report.get("apply_target")
    save_task(config, task)
    report["applied"] = True
    report["main_head"] = applied_head
    report["execution_applied_head"] = applied_head
    write_event_nonfatal(
        config,
        "task_worktree_applied",
        task=task,
        source="worktree apply",
        summary=f"applied worktree branch for task {task_id}",
        payload=transition_payload(
            task,
            execution_branch=branch,
            execution_base_head=report.get("base_head"),
            execution_branch_head=report.get("branch_head"),
            execution_applied_head=applied_head,
            execution_apply_target=report.get("apply_target"),
        ),
    )
    mark_parent_applied_by_conflict_fix(config, task, applied_head, report)
    return report


def apply_stale_base_rebase(config: Config, task: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    repo_root = Path(str(report["repo_root"]))
    worktree_path = Path(str(report["worktree_path"]))
    branch = str(report["branch"])
    old_base = str(report["base_head"])
    old_branch_head = str(report["branch_head"])
    new_base = str(report["main_head"])
    result = run_git(worktree_path, "rebase", new_base)
    if result.returncode != 0:
        detail = sanitize_git_detail(clean_git_result(result))
        abort = run_git(worktree_path, "rebase", "--abort")
        if abort.returncode != 0:
            detail = sanitize_git_detail(f"{detail}; rebase --abort failed: {clean_git_result(abort)}")
            task["execution_worktree_status"] = "recovery_required"
        report["errors"].append("stale-base rebase failed; conflict-fix subtask required: " + detail)
        report["rebase"] = {
            **report.get("rebase", {}),
            "status": "blocked",
            "reason": detail,
            "abort_status": "ok" if abort.returncode == 0 else "failed",
        }
        record_rebase_conflict_fix(config, task, report)
        return report

    new_branch_head = git(worktree_path, "rev-parse", "--verify", "HEAD^{commit}")
    commit_lines = git_optional(repo_root, "log", "--reverse", "--format=%H %s", f"{new_base}..{new_branch_head}")
    commits = rev_list(repo_root, f"{new_base}..{new_branch_head}")
    now = iso_now()
    task["execution_base_ref"] = report.get("apply_target") or task.get("execution_base_ref")
    task["execution_base_head"] = new_base
    task["execution_rebase_status"] = "rebased"
    task["execution_rebased_at"] = now
    task["execution_rebased_from_base"] = old_base
    task["execution_rebased_onto"] = new_base
    task["execution_rebased_from_head"] = old_branch_head
    task["execution_rebased_head"] = new_branch_head
    task["review_status"] = "unreviewed"
    task["reviewed_at"] = None
    task["review_reason"] = "stale-base rebase invalidated prior acceptance; re-review required"
    if task.get("root_task_id") or task.get("parent_task_id") or task.get("chain_status"):
        task["chain_status"] = "awaiting_review"
    for key in ("execution_rebase_blocker", "execution_rebase_blocked_at"):
        task.pop(key, None)
    save_task(config, task)

    report["rebased"] = True
    report["applied"] = False
    report["base_head"] = new_base
    report["branch_head"] = new_branch_head
    report["commit_summary"] = {
        "range": f"{new_base}..{new_branch_head}",
        "count": len(commits),
        "commits": commit_lines.splitlines() if commit_lines else commits,
    }
    report["rebase"] = {
        **report.get("rebase", {}),
        "status": "rebased",
        "from_base": old_base,
        "onto": new_base,
        "from_head": old_branch_head,
        "head": new_branch_head,
        "review_status": "unreviewed",
        "chain_status": task.get("chain_status"),
        "reason": "task branch rebased onto current main; re-review required before main apply",
    }
    write_event_nonfatal(
        config,
        "task_worktree_rebased",
        task=task,
        source="worktree apply",
        summary=f"rebased stale worktree branch for task {task.get('id')}",
        payload=transition_payload(
            task,
            execution_branch=branch,
            execution_base_head=new_base,
            execution_branch_head=new_branch_head,
            execution_rebased_from_base=old_base,
            execution_rebased_onto=new_base,
            review_status="unreviewed",
        ),
    )
    return report


def record_rebase_conflict_fix(config: Config, task: dict[str, Any], report: dict[str, Any]) -> None:
    reason = sanitize_git_detail(str(report.get("rebase", {}).get("reason") or "stale-base rebase is blocked"))
    task["execution_rebase_status"] = "blocked"
    task["execution_rebase_blocker"] = reason
    task["execution_rebase_blocked_at"] = iso_now()
    task["execution_conflict_fix_status"] = "queued"
    task["root_task_id"] = task.get("root_task_id") or task.get("id")
    task["parent_task_id"] = task.get("parent_task_id") or None
    task["chain_status"] = "fixing"
    fix_task = enqueue_conflict_fix_subtask(config, task, report, reason)
    task["last_conflict_fix_task_id"] = fix_task["id"]
    task["execution_conflict_fix_task_id"] = fix_task["id"]
    task["execution_conflict_fix_queued_at"] = iso_now()
    task["fix_attempts"] = max(non_negative_int(task.get("fix_attempts")), 1)
    existing = task.get("blocking_subtask_ids") if isinstance(task.get("blocking_subtask_ids"), list) else []
    task["blocking_subtask_ids"] = [*dict.fromkeys([*[str(item) for item in existing if str(item)], fix_task["id"]])]
    save_task(config, task)
    report["conflict_fix"] = {
        "status": "queued",
        "task_id": fix_task["id"],
        "title": task_title(fix_task),
        "reason": reason,
    }
    write_event_nonfatal(
        config,
        "task_worktree_conflict_fix_enqueued",
        task=task,
        source="worktree apply",
        summary=f"queued conflict-fix task for {task_title(task)} ({task.get('id')})",
        payload=transition_payload(
            task,
            execution_branch=task.get("execution_branch"),
            execution_rebase_status="blocked",
            execution_rebase_blocker=reason,
            conflict_fix_task_id=fix_task["id"],
            conflict_fix_task_title=task_title(fix_task),
        ),
    )


def enqueue_conflict_fix_subtask(
    config: Config,
    parent_task: dict[str, Any],
    report: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    existing_id = str(parent_task.get("execution_conflict_fix_task_id") or parent_task.get("last_conflict_fix_task_id") or "")
    if existing_id:
        try:
            return load_task(config, existing_id)
        except FileNotFoundError:
            pass

    parent_task_id = str(parent_task.get("id") or "")
    root_task_id = str(parent_task.get("root_task_id") or parent_task_id)
    review_cycle = non_negative_int(parent_task.get("review_cycle")) + 1
    title = truncate_text(f"Resolve stale-base conflict for {task_title(parent_task)}", 80)
    prompt = build_conflict_fix_prompt(parent_task, report, root_task_id, review_cycle, reason)
    fix_task = create_task(
        config,
        prompt,
        str(parent_task.get("cwd") or config.root),
        depends_on=[],
        project_id=task_project_id(parent_task) or None,
        category=parent_task.get("category"),
        labels=[*task_labels(parent_task), "worktree-conflict-fix"],
        created_by="worktree-conflict-fix",
        title=title,
        description=f"Bounded stale-base conflict fix for {parent_task_id}.",
        model_requirement_vector=parent_task.get("model_requirement_vector")
        if isinstance(parent_task.get("model_requirement_vector"), dict)
        else None,
        subtask_type="worktree_conflict_fix",
        subtask_for=parent_task_id,
        blocks_root_completion=True,
    )
    fix_task["root_task_id"] = root_task_id
    fix_task["parent_task_id"] = parent_task_id
    fix_task["review_cycle"] = review_cycle
    fix_task["fix_attempts"] = non_negative_int(parent_task.get("fix_attempts")) + 1
    fix_task["chain_status"] = "fixing"
    fix_task["last_review_decision"] = "stale_base_conflict"
    fix_task["auto_fix_allowed"] = False
    fix_task["auto_fix_budget"] = {"max_conflict_fix_tasks": 1}
    fix_task["execution_parent_task_id"] = parent_task_id
    save_task(config, fix_task)
    return fix_task


def build_conflict_fix_prompt(
    parent_task: dict[str, Any],
    report: dict[str, Any],
    root_task_id: str,
    review_cycle: int,
    reason: str,
) -> str:
    return "\n".join(
        [
            "Implement a bounded stale-base conflict-fix task.",
            "",
            "Scope constraints:",
            f"- Root task: {root_task_id}",
            f"- Parent task: {parent_task.get('id')}",
            f"- Review cycle: {review_cycle}",
            f"- Parent task branch: {parent_task.get('execution_branch') or '-'}",
            f"- Parent execution base: {report.get('base_head') or parent_task.get('execution_base_head') or '-'}",
            f"- Current main HEAD: {report.get('main_head') or '-'}",
            "- Port the parent task branch changes onto current main in this task's own worktree.",
            "- Resolve conflicts in this conflict-fix worktree only.",
            "- Do not edit conflict markers inside `cbr worktree apply`.",
            "- Do not apply worktree branches or push remotes.",
            "- Do not create or enqueue new tasks.",
            "- Preserve cbr final JSON schema requirements in the final response.",
            "",
            "Conflict reason:",
            reason,
            "",
            "Expected verification:",
            "Run focused tests or checks relevant to the ported changes and report the commands/results.",
        ]
    )


def mark_parent_applied_by_conflict_fix(
    config: Config,
    task: dict[str, Any],
    applied_head: str,
    report: dict[str, Any],
) -> None:
    if task.get("subtask_type") != "worktree_conflict_fix" or not task.get("parent_task_id"):
        return
    try:
        parent = load_task(config, str(task.get("parent_task_id")))
    except FileNotFoundError:
        return
    if parent.get("execution_conflict_fix_task_id") != task.get("id") and parent.get("last_conflict_fix_task_id") != task.get("id"):
        return
    parent["execution_apply_status"] = "applied"
    parent["execution_applied_at"] = iso_now()
    parent["execution_applied_head"] = applied_head
    parent["execution_apply_target"] = report.get("apply_target")
    parent["execution_apply_via_task_id"] = task.get("id")
    parent["execution_conflict_fix_status"] = "applied"
    parent["chain_status"] = "accepted"
    save_task(config, parent)
    write_event_nonfatal(
        config,
        "task_worktree_applied_via_conflict_fix",
        task=parent,
        source="worktree apply",
        summary=f"marked {task_title(parent)} ({parent.get('id')}) applied via conflict-fix task",
        payload=transition_payload(
            parent,
            conflict_fix_task_id=task.get("id"),
            execution_applied_head=applied_head,
            execution_apply_target=report.get("apply_target"),
        ),
    )


def non_negative_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def validate_apply_report(config: Config, task: dict[str, Any], report: dict[str, Any]) -> None:
    gates: list[dict[str, Any]] = []
    report["gates"] = gates

    def gate(name: str, ok: bool, detail: str, error_detail: str | None = None) -> None:
        gates.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            report["errors"].append(error_detail or detail)

    gate(
        "task_completed_accepted",
        task.get("status") == "completed" and task.get("review_status") == "accepted",
        f"status={task.get('status') or '-'} review_status={task.get('review_status') or '-'}",
        "worktree apply requires task status=completed and review_status=accepted",
    )
    gate(
        "execution_mode_git_worktree",
        task.get("execution_mode") == "git_worktree",
        f"execution_mode={task.get('execution_mode') or '-'}",
        "worktree apply requires execution_mode=git_worktree",
    )
    gate(
        "not_already_applied",
        task.get("execution_apply_status") != "applied",
        f"execution_apply_status={task.get('execution_apply_status') or '-'}",
        "worktree apply requires execution_apply_status not already applied",
    )

    branch = str(task.get("execution_branch") or "").strip()
    base = str(task.get("execution_base_head") or "").strip()
    worktree_raw = task.get("execution_worktree_path")
    repo_raw = task.get("execution_repo_root")
    worktree_status = str(task.get("execution_worktree_status") or "")
    missing = [
        name
        for name, value in (
            ("execution_branch", branch),
            ("execution_base_head", base),
            ("execution_repo_root", repo_raw),
            ("execution_worktree_path", worktree_raw),
            ("execution_worktree_status", worktree_status),
        )
        if not value
    ]
    gate(
        "required_worktree_metadata",
        not missing,
        "required worktree metadata present" if not missing else "missing: " + ", ".join(missing),
        "worktree apply requires retained worktree metadata; missing: " + ", ".join(missing),
    )
    gate(
        "worktree_status_retained",
        bool(worktree_status) and worktree_status in APPLY_OK_WORKTREE_STATUSES,
        f"execution_worktree_status={worktree_status or '-'}",
        f"worktree apply requires retained worktree metadata, found execution_worktree_status={worktree_status or '-'}",
    )
    if missing or not branch or not base or not repo_raw or not worktree_raw:
        report["gates_ok"] = False
        return

    try:
        validate_branch_name(branch)
        repo_root = Path(str(repo_raw)).expanduser().resolve()
        worktree_path = guarded_existing_worktree_path(config, Path(str(worktree_raw)))
        registry = worktree_registry(repo_root)
        classification = classify_apply_state(task, branch, worktree_path, registry)
        report.update(
            {
                "repo_root": str(repo_root),
                "branch": branch,
                "worktree_path": str(worktree_path),
                "worktree_status": worktree_status,
                "classification": classification,
            }
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        gate("worktree_metadata_recoverable", False, str(exc))
        report["gates_ok"] = False
        return

    gate(
        "worktree_metadata_recoverable",
        classification["status"] == "retained",
        classification["reason"],
    )

    try:
        repo_top = git(repo_root, "rev-parse", "--show-toplevel")
        main_branch = git_optional(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD") or "HEAD"
        main_head = git(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
        base_head = git(repo_root, "rev-parse", "--verify", f"{base}^{{commit}}")
        branch_head = git(repo_root, "rev-parse", "--verify", f"{branch}^{{commit}}")
        status = git(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
        commits = rev_list(repo_root, f"{base_head}..{branch_head}")
        commit_lines = git_optional(repo_root, "log", "--reverse", "--format=%H %s", f"{base_head}..{branch_head}")
        task_status = git_optional(worktree_path, "status", "--porcelain=v1", "--untracked-files=all") or ""
    except subprocess.CalledProcessError as exc:
        gate("git_state_available", False, clean_git_exception(exc))
        report["gates_ok"] = False
        return

    report.update(
        {
            "repo_root": repo_top,
            "apply_target": main_branch,
            "base_head": base_head,
            "branch_head": branch_head,
            "main_head": main_head,
            "commit_summary": {
                "range": f"{base_head}..{branch_head}",
                "count": len(commits),
                "commits": commit_lines.splitlines() if commit_lines else commits,
            },
        }
    )
    gate("git_state_available", True, "git state is available")
    gate(
        "main_worktree_clean",
        not status.strip(),
        "main worktree is clean" if not status.strip() else "main worktree has uncommitted changes",
        "main worktree must be clean before applying",
    )
    gate(
        "branch_based_on_execution_base",
        is_ancestor(repo_root, base_head, branch_head),
        "execution_base_head is an ancestor of the task branch",
        "task branch must be based on execution_base_head",
    )
    gate(
        "branch_has_commits",
        bool(commits),
        f"commits_after_base={len(commits)}",
        "task branch has no commits after execution_base_head; nothing to apply",
    )
    stale_base = main_head != base_head
    if stale_base:
        report["apply_strategy"] = "stale_base_rebase"
        report["planned_action"] = f"git -C <task_worktree> rebase {main_head}"
        report["rebase"] = {
            "status": "pending",
            "from_base": base_head,
            "onto": main_head,
            "from_head": branch_head,
            "review_status_after_clean_rebase": "unreviewed",
        }
        gate(
            "main_contains_execution_base",
            is_ancestor(repo_root, base_head, main_head),
            "main HEAD contains execution_base_head",
            "main HEAD must contain execution_base_head before stale-base rebase",
        )
        gate(
            "task_worktree_clean_for_rebase",
            not task_status.strip(),
            "task worktree is clean" if not task_status.strip() else "task worktree has uncommitted changes",
            "task worktree must be clean before stale-base rebase",
        )
        rebase_can_run = not any(
            not gate_result.get("ok")
            for gate_result in gates
            if gate_result.get("name")
            in {
                "task_completed_accepted",
                "execution_mode_git_worktree",
                "not_already_applied",
                "required_worktree_metadata",
                "worktree_status_retained",
                "worktree_metadata_recoverable",
                "git_state_available",
                "main_worktree_clean",
                "branch_based_on_execution_base",
                "branch_has_commits",
                "main_contains_execution_base",
                "task_worktree_clean_for_rebase",
            }
        )
        if rebase_can_run:
            rebase_check = check_clean_rebase(config, repo_root, branch_head, main_head)
            report["rebase"].update(rebase_check)
            gate(
                "stale_base_rebase_clean",
                rebase_check.get("status") == "clean",
                str(rebase_check.get("reason") or "stale-base rebase preflight completed"),
                "stale-base rebase is not clean; conflict-fix subtask required: "
                + str(rebase_check.get("reason") or "unknown"),
            )
    else:
        report["apply_strategy"] = "fast_forward"
        gate(
            "main_head_matches_execution_base",
            True,
            "main HEAD equals execution_base_head",
        )
    if task_status.strip() and not stale_base:
        report["warnings"].append("task worktree has uncommitted changes; apply only merges committed branch changes")
    report["gates_ok"] = not report["errors"]


def check_clean_rebase(config: Config, repo_root: Path, branch_head: str, main_head: str) -> dict[str, Any]:
    try:
        temp_path = Path(tempfile.mkdtemp(prefix="cbr-rebase-check-"))
        shutil.rmtree(temp_path)
    except OSError as exc:
        return {"status": "blocked", "reason": f"cannot create temporary rebase worktree: {exc}"}

    try:
        add = run_git(repo_root, "worktree", "add", "--detach", str(temp_path), branch_head)
        if add.returncode != 0:
            return {"status": "blocked", "reason": sanitize_git_detail("cannot create temporary rebase worktree: " + clean_git_result(add))}
        rebase = run_git(temp_path, "rebase", main_head)
        if rebase.returncode == 0:
            return {"status": "clean", "reason": "task branch can be cleanly rebased onto current main HEAD"}
        return {"status": "blocked", "reason": sanitize_git_detail(clean_git_result(rebase) or "rebase reported a conflict")}
    finally:
        remove = run_git(repo_root, "worktree", "remove", "--force", str(temp_path))
        if remove.returncode != 0:
            run_git(repo_root, "worktree", "prune")
        shutil.rmtree(temp_path, ignore_errors=True)


def base_report(action: str, task: dict[str, Any], apply: bool) -> dict[str, Any]:
    return {
        "task_id": task.get("id"),
        "action": action,
        "mode": "apply" if apply else "dry-run",
        "applied": False,
        "errors": [],
        "warnings": [],
    }


def repo_context(task: dict[str, Any]) -> dict[str, Any]:
    cwd = Path(str(task.get("cwd") or "")).expanduser()
    if not cwd:
        raise ValueError("task cwd is missing")
    repo_root = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
    base_head = git(repo_root, "rev-parse", "HEAD")
    return {"repo_root": repo_root, "base_ref": "HEAD", "base_head": base_head}


def guarded_worktree_path(config: Config, branch: str) -> Path:
    if config.worktree_root is None:
        raise ValueError("worktree_root is not configured")
    slug = branch.split("/", 1)[1]
    return guard_path_under_root(config.worktree_root, config.worktree_root / slug)


def guarded_existing_worktree_path(config: Config, path: Path) -> Path:
    if config.worktree_root is None:
        raise ValueError("worktree_root is not configured")
    return guard_path_under_root(config.worktree_root, path)


def guard_path_under_root(root: Path, path: Path) -> Path:
    root_resolved = root.expanduser().resolve()
    target = path.expanduser().resolve()
    if target == root_resolved or root_resolved not in target.parents:
        raise ValueError("worktree path must be inside configured worktree_root")
    if str(target) in {"/", ""}:
        raise ValueError("refusing unsafe worktree path")
    return target


def classify_prepare_state(
    task: dict[str, Any],
    branch: str,
    worktree_path: Path,
    registry: list[dict[str, str]],
    branch_state: dict[str, Any],
) -> dict[str, str]:
    registered = registry_entry_for_path(registry, worktree_path)
    registered_by_branch = registry_entry_for_branch(registry, branch)
    path_exists = worktree_path.exists()
    metadata_matches = task.get("execution_branch") == branch and Path(str(task.get("execution_worktree_path") or worktree_path)).expanduser().resolve() == worktree_path
    if registered and not path_exists:
        return {"status": "recovery_required", "reason": "git worktree registry points to a missing path"}
    if path_exists and not registered:
        return {"status": "recovery_required", "reason": "worktree path exists but is not registered by git"}
    if registered_by_branch and registered_by_branch is not registered:
        return {"status": "recovery_required", "reason": "branch is already checked out in a different worktree"}
    if registered and registered.get("branch") != f"refs/heads/{branch}":
        return {"status": "recovery_required", "reason": "registered worktree branch does not match task branch"}
    if registered and metadata_matches:
        return {"status": "prepared", "reason": "matching worktree already exists"}
    if registered:
        return {"status": "recovery_required", "reason": "existing worktree is not linked to this task metadata"}
    if branch_state["exists"] and not metadata_matches:
        return {"status": "existing_branch", "reason": "branch exists without matching task metadata"}
    return {"status": "absent", "reason": "worktree and branch are absent"}


def classify_cleanup_state(
    task: dict[str, Any],
    branch: str,
    worktree_path: Path,
    registry: list[dict[str, str]],
) -> dict[str, str]:
    metadata_status = str(task.get("execution_worktree_status") or "")
    if metadata_status == "recovery_required":
        return {"status": "recovery_required", "reason": "task worktree metadata is marked recovery_required"}
    if metadata_status not in CLEANUP_OK_WORKTREE_STATUSES:
        return {
            "status": "recovery_required",
            "reason": f"execution_worktree_status={metadata_status or '-'} is not a retained cleanup candidate",
        }
    registered = registry_entry_for_path(registry, worktree_path)
    if not worktree_path.exists() and not registered:
        return {"status": "missing", "reason": "worktree path and registry entry are already absent"}
    if worktree_path.exists() and not registered:
        return {"status": "recovery_required", "reason": "worktree path exists but is not registered by git"}
    if registered and not worktree_path.exists():
        return {"status": "recovery_required", "reason": "git worktree registry points to a missing path"}
    if registered and registered.get("branch") != f"refs/heads/{branch}":
        return {"status": "recovery_required", "reason": "registered worktree branch does not match task metadata"}
    if not any(worktree_path.iterdir()):
        return {"status": "recovery_required", "reason": "refusing to cleanup an empty worktree path"}
    return {"status": "cleanup_candidate", "reason": "worktree can be removed; branch will be retained"}


def classify_apply_state(
    task: dict[str, Any],
    branch: str,
    worktree_path: Path,
    registry: list[dict[str, str]],
) -> dict[str, str]:
    registered = registry_entry_for_path(registry, worktree_path)
    if not worktree_path.exists() and not registered:
        return {"status": "missing", "reason": "worktree path and registry entry are absent"}
    if worktree_path.exists() and not registered:
        return {"status": "recovery_required", "reason": "worktree path exists but is not registered by git"}
    if registered and not worktree_path.exists():
        return {"status": "recovery_required", "reason": "git worktree registry points to a missing path"}
    if registered and registered.get("branch") != f"refs/heads/{branch}":
        return {"status": "recovery_required", "reason": "registered worktree branch does not match task branch"}
    return {"status": "retained", "reason": "retained worktree metadata is valid"}


def local_branch_state(repo_root: Path, branch: str) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show-ref", "--verify", f"refs/heads/{branch}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        return {"exists": False, "head": None}
    return {"exists": True, "head": result.stdout.split()[0] if result.stdout.split() else None}


def worktree_registry(repo_root: Path) -> list[dict[str, str]]:
    output = git(repo_root, "worktree", "list", "--porcelain")
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                entries.append(current)
            current = {"path": value}
        else:
            current[key] = value
    if current:
        entries.append(current)
    return entries


def registry_entry_for_path(registry: list[dict[str, str]], path: Path) -> dict[str, str] | None:
    target = str(path)
    for entry in registry:
        if str(Path(entry.get("path", "")).expanduser().resolve()) == target:
            return entry
    return None


def registry_entry_for_branch(registry: list[dict[str, str]], branch: str) -> dict[str, str] | None:
    ref = f"refs/heads/{branch}"
    for entry in registry:
        if entry.get("branch") == ref:
            return entry
    return None


def validate_branch_name(branch: str) -> None:
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"invalid worktree branch name: {branch}")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def git_optional(cwd: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args=["git", *args], returncode=1, stdout="", stderr=str(exc))


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", ancestor, descendant],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.returncode == 0


def rev_list(repo_root: Path, ref_range: str) -> list[str]:
    output = git(repo_root, "rev-list", "--reverse", ref_range)
    return [line.strip() for line in output.splitlines() if line.strip()]


def clean_git_exception(exc: subprocess.CalledProcessError) -> str:
    stderr = " ".join(str(exc.stderr or "").split())
    stdout = " ".join(str(exc.stdout or "").split())
    detail = stderr or stdout or str(exc)
    return detail


def clean_git_result(result: subprocess.CompletedProcess[str]) -> str:
    return " ".join(str(result.stderr or result.stdout or "").split())


def sanitize_git_detail(value: object) -> str:
    return str(sanitize_report_value(value))


def task_worktree_metadata(task: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"execution_mode": task.get("execution_mode") or "main_worktree"}
    for source, target in (
        ("execution_branch", "branch"),
        ("execution_base_ref", "base_ref"),
        ("execution_base_head", "base_head"),
        ("execution_worktree_status", "worktree_status"),
        ("execution_worktree_path", "worktree_path"),
        ("execution_worktree_root", "worktree_root"),
        ("execution_repo_root", "repo_root"),
        ("execution_original_cwd", "original_cwd"),
        ("execution_parent_task_id", "parent_task_id"),
        ("execution_merge_target", "merge_target"),
        ("execution_apply_status", "apply_status"),
        ("execution_applied_at", "applied_at"),
        ("execution_applied_head", "applied_head"),
        ("execution_apply_target", "apply_target"),
        ("execution_apply_via_task_id", "apply_via_task_id"),
        ("execution_rebase_status", "rebase_status"),
        ("execution_rebased_at", "rebased_at"),
        ("execution_rebased_from_base", "rebased_from_base"),
        ("execution_rebased_onto", "rebased_onto"),
        ("execution_rebased_from_head", "rebased_from_head"),
        ("execution_rebased_head", "rebased_head"),
        ("execution_rebase_blocker", "rebase_blocker"),
        ("execution_rebase_blocked_at", "rebase_blocked_at"),
        ("execution_conflict_fix_status", "conflict_fix_status"),
        ("execution_conflict_fix_task_id", "conflict_fix_task_id"),
        ("execution_conflict_fix_queued_at", "conflict_fix_queued_at"),
        ("execution_cleaned_at", "cleaned_at"),
        ("execution_cleanup_kind", "cleanup_kind"),
        ("execution_cleanup_reason", "cleanup_reason"),
        ("execution_cleanup_branch_retained", "cleanup_branch_retained"),
        ("execution_cleanup_result_applied", "cleanup_result_applied"),
        ("execution_branch_prune_status", "branch_prune_status"),
        ("execution_branch_pruned_at", "branch_pruned_at"),
        ("execution_branch_prune_reason", "branch_prune_reason"),
        ("execution_branch_pruned_head", "branch_pruned_head"),
    ):
        value = task.get(source)
        if value not in (None, ""):
            metadata[target] = sanitize_report_value(value)
    return metadata


def task_worktree_report(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task_worktree_metadata(task)
    report: dict[str, Any] = {
        "metadata": metadata,
        "warnings": [],
        "missing_metadata": [],
        "stale_metadata": [],
        "recovery_required": False,
        "path_exists": None,
        "branch_exists": None,
    }
    if metadata.get("execution_mode") != "git_worktree":
        return report

    required = {
        "execution_branch": "branch",
        "execution_base_ref": "base_ref",
        "execution_base_head": "base_head",
        "execution_worktree_status": "worktree_status",
        "execution_worktree_path": "worktree_path",
    }
    for source, public_name in required.items():
        if not task.get(source):
            report["missing_metadata"].append(public_name)
    if report["missing_metadata"]:
        report["warnings"].append("git_worktree task has incomplete worktree metadata")

    status = str(task.get("execution_worktree_status") or "")
    if status == "recovery_required":
        report["recovery_required"] = True
        report["warnings"].append("task worktree metadata is marked recovery_required")

    path_value = task.get("execution_worktree_path")
    if path_value:
        try:
            worktree_path = Path(str(path_value)).expanduser()
            report["path_exists"] = worktree_path.exists()
            if not report["path_exists"] and status in WORKTREE_RETAINED_STATUSES:
                report["recovery_required"] = True
                report["stale_metadata"].append("worktree_path")
                report["warnings"].append("retained worktree metadata points to a missing path")
        except OSError as exc:
            report["recovery_required"] = True
            report["stale_metadata"].append("worktree_path")
            report["warnings"].append("cannot inspect worktree path: " + sanitize_report_value(exc))

    repo_value = task.get("execution_repo_root") or task.get("project_root") or task.get("cwd")
    branch = str(task.get("execution_branch") or "")
    if repo_value and branch:
        try:
            repo_root = Path(str(repo_value)).expanduser()
            branch_state = local_branch_state(repo_root, branch)
            report["branch_exists"] = branch_state.get("exists")
            if not branch_state.get("exists") and status in WORKTREE_RETAINED_STATUSES:
                report["recovery_required"] = True
                report["stale_metadata"].append("branch")
                report["warnings"].append("retained worktree metadata points to a missing branch")
            if branch_state.get("head"):
                report["branch_head"] = sanitize_report_value(branch_state.get("head"))
        except (OSError, subprocess.SubprocessError) as exc:
            report["warnings"].append("cannot inspect worktree branch: " + sanitize_report_value(exc))

    if status in WORKTREE_RETAINED_STATUSES and has_applied_worktree_metadata(task):
        if repo_value:
            try:
                repo_root = Path(str(repo_value)).expanduser()
                applied_metadata = verify_applied_cleanup_target(task, repo_root)
                report["applied_metadata"] = {key: sanitize_report_value(value) for key, value in applied_metadata.items()}
                if applied_metadata.get("status") == "stale_applied_metadata":
                    report["recovery_required"] = True
                    report["stale_metadata"].append("applied_metadata")
                    report["warnings"].append(str(applied_metadata.get("reason") or "stale applied metadata"))
            except (OSError, subprocess.SubprocessError) as exc:
                report["warnings"].append("cannot inspect applied metadata: " + sanitize_report_value(exc))
        else:
            report["warnings"].append("cannot inspect applied metadata: missing repository metadata")

    report["missing_metadata"] = sorted(set(report["missing_metadata"]))
    report["stale_metadata"] = sorted(set(report["stale_metadata"]))
    report["warnings"] = sorted(set(report["warnings"]))
    return report


def worktree_task_counts(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    retained = 0
    recovery_required = 0
    missing_metadata = 0
    for task in tasks:
        status = str(task.get("execution_worktree_status") or "")
        if status:
            by_status[status] = by_status.get(status, 0) + 1
        if status in WORKTREE_RETAINED_STATUSES:
            retained += 1
        report = task_worktree_report(task)
        if report.get("missing_metadata"):
            missing_metadata += 1
        if report.get("recovery_required"):
            recovery_required += 1
    return {
        "by_status": dict(sorted(by_status.items())),
        "retained": retained,
        "recovery_required": recovery_required,
        "missing_metadata": missing_metadata,
    }


def sanitize_report_value(value: object) -> Any:
    from .transcript import sanitize

    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize(value)


def render_worktree_report(report: dict[str, Any]) -> str:
    lines = [
        f"action: {report.get('action')}",
        f"mode: {report.get('mode')}",
        f"task_id: {report.get('task_id')}",
        f"applied: {str(bool(report.get('applied'))).lower()}",
    ]
    for key in (
        "branch",
        "worktree_path",
        "base_ref",
        "base_head",
        "apply_status",
        "cleanup_kind",
        "cleanup_reason",
        "prune_reason",
    ):
        if report.get(key):
            lines.append(f"{key}: {report.get(key)}")
    for key in ("branch_head", "expected_head", "main_head", "apply_target", "apply_strategy", "planned_action"):
        if report.get(key):
            lines.append(f"{key}: {report.get(key)}")
    rebase = report.get("rebase")
    if isinstance(rebase, dict):
        detail = rebase.get("reason") or rebase.get("status")
        lines.append(f"rebase: {rebase.get('status')} ({detail})")
    conflict_fix = report.get("conflict_fix")
    if isinstance(conflict_fix, dict):
        title = conflict_fix.get("title") or "conflict-fix subtask"
        lines.append(f"conflict_fix: {conflict_fix.get('status')} {title} ({conflict_fix.get('task_id')})")
    commit_summary = report.get("commit_summary")
    if isinstance(commit_summary, dict):
        lines.append(
            f"commit_summary: count={commit_summary.get('count')} range={commit_summary.get('range')}"
        )
    if report.get("gates"):
        for gate in report.get("gates") or []:
            if isinstance(gate, dict):
                status = "ok" if gate.get("ok") else "blocked"
                lines.append(f"gate: {gate.get('name')} {status} ({gate.get('detail')})")
    classification = report.get("classification")
    if isinstance(classification, dict):
        lines.append(f"classification: {classification.get('status')} ({classification.get('reason')})")
    for warning in report.get("warnings") or []:
        lines.append(f"warning: {warning}")
    for error in report.get("errors") or []:
        lines.append(f"error: {error}")
    return "\n".join(lines) + "\n"

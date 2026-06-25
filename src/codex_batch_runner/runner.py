from __future__ import annotations

import shutil
import os
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from .codex import FIRST_MEANINGFUL_STALL_REASON, STARTUP_STALL_REASON, CodexResult, run_codex
from .config import Config
from .events import emit_task_event, result_summary_payload, transition_payload
from .execution_profiles import ExecutionSettings, command_options, resolve_execution_settings
from .evidence import capture_rate_limit_evidence
from .fs import ensure_dir
from .lock import FileLock
from .maintenance import build_codex_cli_maintenance_report, run_codex_cli_maintenance
from .prompts import build_prompt
from .queue import is_in_cooldown, recover_stale_running_tasks, save_task, select_next_task
from .review_next import build_review_next_apply_report_locked, has_actionable_auto_review_candidate
from .shell import ShellResult, run_shell_task
from .state import get_runner_pause, in_global_cooldown, is_runner_paused, mark_rate_limit, mark_run, mark_success
from .timeutil import add_seconds, iso_now, parse_time
from .triggers import run_post_run_trigger
from .worktree import prepare_task_worktree_for_run_locked


ACTIVE_RUN_FIELDS = (
    "active_run_id",
    "active_runner_hostname",
    "active_runner_pid",
    "active_run_attempt",
    "active_run_started_at",
)


@dataclass
class RunOutcome:
    status: str
    message: str
    task_id: str | None = None
    review: dict[str, Any] | None = None
    maintenance: dict[str, Any] | None = None


@dataclass
class ClaimedRun:
    task: dict[str, Any]
    run_task: dict[str, Any]
    prompt: str
    attempt: int
    active_run_id: str
    execution_backend: str
    execution_cwd: Path | None
    execution_settings: ExecutionSettings | None


def run_next(config: Config, *, suppress_wake_hooks: bool = False) -> RunOutcome:
    ensure_dir(config.queue_dir)
    ensure_dir(config.log_dir)
    if in_global_cooldown(config):
        return RunOutcome(status="cooldown", message="global cooldown is active")

    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire():
        return RunOutcome(status="locked", message="another runner is active")

    task: dict[str, Any] | None = None
    outcome: RunOutcome | None = None
    claimed: ClaimedRun | None = None
    try:
        recover_stale_running_tasks(config)
        if is_runner_paused(config):
            pause = get_runner_pause(config)
            mark_run(config, None)
            outcome = RunOutcome(status="paused", message=runner_pause_message(pause))
        review_report: dict[str, Any] | None = None
        if outcome is None and (config.auto_review_mechanical_accept or config.auto_review_codex_enabled):
            review_report = build_review_next_apply_report_locked(config, auto_mode=True)
            if review_report.get("mutated"):
                auto_review = review_report.get("auto_review") if isinstance(review_report.get("auto_review"), dict) else {}
                if auto_review.get("follow_up_enqueued"):
                    mark_run(config, None)
                    outcome = RunOutcome(
                        status="review_fix_enqueued",
                        message="auto-review enqueued one follow-up fix task",
                        task_id=str(review_report.get("task_id") or "") or None,
                        review=review_report,
                    )
                elif auto_review.get("decision") != "accepted":
                    mark_run(config, None)
                    outcome = RunOutcome(
                        status="review_needed",
                        message="completed task needs human review",
                        task_id=str(review_report.get("task_id") or "") or None,
                        review=review_report,
                    )
                else:
                    mark_run(config, None)
                    outcome = RunOutcome(
                        status="review_accepted",
                        message="auto-review accepted one completed task",
                        task_id=str(review_report.get("task_id") or "") or None,
                        review=review_report,
                    )
            elif review_report_consumed_work(review_report):
                mark_run(config, None)
                outcome = RunOutcome(
                    status="review_needed",
                    message="completed task needs human review",
                    task_id=str(review_report.get("task_id") or "") or None,
                    review=review_report,
                )

        if outcome is None:
            claimed, outcome = claim_next_implementation_task_locked(config, review_report)
        if outcome is None and not claimed:
            mark_run(config, None)
            outcome = RunOutcome(status="empty", message="no runnable task")
    finally:
        lock.release()

    if not claimed:
        should_wake = bool(
            outcome
            and outcome.status in {"review_accepted", "review_fix_enqueued"}
            and should_trigger_post_review_wake(config)
        )
        if should_wake and not suppress_wake_hooks:
            run_post_run_trigger(config)
        elif not should_wake and outcome and outcome.status == "review_accepted":
            outcome.maintenance = maybe_run_empty_codex_cli_maintenance(config)
        return outcome or RunOutcome(status="empty", message="no runnable task")

    task = claimed.task
    if not suppress_wake_hooks and should_trigger_post_claim_wake(config, task):
        run_post_run_trigger(config)

    if claimed.execution_backend == "shell":
        result = run_shell_task(config, claimed.run_task, claimed.attempt)
        outcome = finalize_shell_run(config, claimed, result)
    else:
        result = run_codex(config, claimed.run_task, claimed.prompt, claimed.attempt)
        outcome = finalize_codex_run(config, claimed, result)

    should_wake = bool(outcome and outcome.task_id and should_trigger_post_run_wake(config, task))
    if should_wake and not suppress_wake_hooks:
        run_post_run_trigger(config)
    elif not should_wake and outcome and outcome.task_id and outcome.status != "stale_finalization":
        outcome.maintenance = maybe_run_empty_codex_cli_maintenance(config)
    return outcome


def claim_next_implementation_task_locked(
    config: Config,
    review_report: dict[str, Any] | None = None,
) -> tuple[ClaimedRun | None, RunOutcome | None]:
    if is_runner_paused(config):
        pause = get_runner_pause(config)
        mark_run(config, None)
        return None, RunOutcome(status="paused", message=runner_pause_message(pause))

    task = select_next_task(config)
    if not task:
        if review_report and review_report.get("selected"):
            mark_run(config, None)
            return None, RunOutcome(
                status="review_needed",
                message="completed task needs human review",
                task_id=str(review_report.get("task_id") or "") or None,
                review=review_report,
            )
        mark_run(config, None)
        return None, RunOutcome(status="empty", message="no runnable task")

    started_at = iso_now()
    execution_backend = task_execution_backend(task)
    resume_requested = execution_backend == "codex" and task.get("status") == "needs_resume"
    execution_cwd: Path | None = None
    if config.worktree_mode == "task":
        worktree_result = prepare_task_worktree_for_run_locked(config, task)
        task = worktree_result["task"]
        if worktree_result["report"].get("errors"):
            mark_worktree_prepare_failure(config, task, worktree_result["report"])
            mark_run(config, task["id"])
            return None, RunOutcome(status=task["status"], message="worktree preparation failed", task_id=task["id"])
        execution_cwd = worktree_result["worktree_path"]

    backend_error = validate_task_backend(task)
    if backend_error:
        mark_backend_failure(config, task, backend_error)
        mark_run(config, task["id"])
        return None, RunOutcome(status=task["status"], message=backend_error, task_id=task["id"])
    prompt = ""
    profile_settings: ExecutionSettings | None = None
    resume_unavailable = False
    if execution_backend == "codex":
        resume_unavailable = bool(resume_requested and task.get("next_prompt") and not resume_id(task))
        prompt = build_prompt(
            task,
            resume_unavailable=resume_unavailable,
            execution_cwd=str(execution_cwd) if execution_cwd else None,
        )
        profile_settings, profile_error = validate_execution_profile(config, task)
        if profile_error:
            mark_profile_failure(config, task, profile_error)
            mark_run(config, task["id"])
            return None, RunOutcome(status=task["status"], message=profile_error, task_id=task["id"])

    active_run_id = uuid.uuid4().hex
    task["status"] = "running"
    task["started_at"] = started_at
    task["execution_backend"] = execution_backend
    task["resume_requested"] = resume_requested
    task["resume_unavailable"] = resume_unavailable
    task["resume_unavailable_at"] = started_at if resume_unavailable else None
    if resume_unavailable:
        task["resume_unavailable_attempts"] = int(task.get("resume_unavailable_attempts", 0)) + 1
    task["attempts"] = int(task.get("attempts", 0)) + 1
    task["run_count"] = int(task.get("run_count", 0)) + 1
    task["active_run_id"] = active_run_id
    task["active_runner_hostname"] = socket.gethostname()
    task["active_runner_pid"] = os.getpid()
    task["active_run_attempt"] = task["attempts"]
    task["active_run_started_at"] = started_at
    if execution_cwd:
        task["execution_worktree_status"] = "running"
        task["execution_started_at"] = started_at
    save_task(config, task)
    emit_task_event(
        config,
        "task_started",
        task,
        source="run-next",
        summary=f"started task {task['id']}",
        payload=transition_payload(
            task,
            started_at=started_at,
            resume_requested=resume_requested,
            resume_unavailable=resume_unavailable,
            active_run_id=active_run_id,
        ),
    )
    mark_run(config, task["id"])

    run_task = dict(task)
    if execution_cwd:
        run_task["cwd"] = str(execution_cwd)
    return ClaimedRun(
        task=task,
        run_task=run_task,
        prompt=prompt,
        attempt=task["attempts"],
        active_run_id=active_run_id,
        execution_backend=execution_backend,
        execution_cwd=execution_cwd,
        execution_settings=profile_settings,
    ), None


def finalize_codex_run(config: Config, claimed: ClaimedRun, result: CodexResult) -> RunOutcome:
    lock = acquire_finalize_lock(config)
    try:
        task = load_claimed_task_for_finalize(config, claimed)
        if not task:
            return RunOutcome(status="stale_finalization", message="active run id no longer matches", task_id=claimed.task["id"])
        if claimed.execution_cwd:
            task["execution_worktree_status"] = "retained"
            task["execution_retained_at"] = iso_now()
            auto_commit_worktree_result(config, task, result, claimed.execution_cwd)
        apply_codex_result(config, task, result, git_status_cwd=claimed.execution_cwd, execution_settings=claimed.execution_settings)
        claimed.task = task
        return RunOutcome(status=task["status"], message="task processed", task_id=task["id"])
    finally:
        lock.release()


def finalize_shell_run(config: Config, claimed: ClaimedRun, result: ShellResult) -> RunOutcome:
    lock = acquire_finalize_lock(config)
    try:
        task = load_claimed_task_for_finalize(config, claimed)
        if not task:
            return RunOutcome(status="stale_finalization", message="active run id no longer matches", task_id=claimed.task["id"])
        if claimed.execution_cwd:
            task["execution_worktree_status"] = "retained"
            task["execution_retained_at"] = iso_now()
        apply_shell_result(config, task, result, git_status_cwd=claimed.execution_cwd)
        claimed.task = task
        return RunOutcome(status=task["status"], message="task processed", task_id=task["id"])
    finally:
        lock.release()


def load_claimed_task_for_finalize(config: Config, claimed: ClaimedRun) -> dict[str, Any] | None:
    from .queue import load_task

    task = load_task(config, str(claimed.task["id"]))
    if task.get("active_run_id") != claimed.active_run_id:
        return None
    return task


def acquire_finalize_lock(config: Config) -> FileLock:
    deadline = time.monotonic() + 30.0
    while True:
        lock = FileLock(config.lock_file, config.stale_lock_seconds)
        if lock.acquire():
            return lock
        if time.monotonic() >= deadline:
            raise RuntimeError("could not acquire queue lock to finalize task result")
        time.sleep(0.1)


def should_trigger_post_claim_wake(config: Config, processed_task: dict[str, Any] | None) -> bool:
    if not processed_task:
        return False
    if in_global_cooldown(config):
        return False
    if select_next_task(config) is None:
        return False
    if is_runner_paused(config):
        return False
    return True


def review_report_consumed_work(report: dict[str, Any]) -> bool:
    auto_review = report.get("auto_review") if isinstance(report.get("auto_review"), dict) else {}
    return bool(auto_review.get("reviewer_codex_invoked"))


def runner_pause_message(pause: dict[str, Any]) -> str:
    reason = str(pause.get("reason") or "no reason recorded")
    paused_at = pause.get("paused_at")
    if paused_at:
        return f"runner pause is active: {reason} (paused_at={paused_at})"
    return f"runner pause is active: {reason}"


def validate_execution_profile(config: Config, task: dict[str, Any]) -> tuple[ExecutionSettings | None, str | None]:
    try:
        settings = resolve_execution_settings(config, task)
        command_options(settings)
    except ValueError as exc:
        return None, str(exc)
    return settings, None


def task_execution_backend(task: dict[str, Any]) -> str:
    backend = task.get("execution_backend") or "codex"
    return str(backend)


def validate_task_backend(task: dict[str, Any]) -> str | None:
    backend = task_execution_backend(task)
    if backend == "codex":
        return None
    if backend != "shell":
        return f"invalid execution backend: {backend}"
    command = task.get("shell_command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        return "shell task requires non-empty shell_command argv list"
    timeout = task.get("shell_timeout_seconds")
    if timeout is not None:
        try:
            if int(timeout) < 1:
                return "shell_timeout_seconds must be a positive integer"
        except (TypeError, ValueError):
            return "shell_timeout_seconds must be a positive integer"
    return None


def mark_backend_failure(config: Config, task: dict, error_message: str) -> None:
    task["status"] = "failed"
    task["last_error"] = error_message
    task["failure_count"] = int(task.get("failure_count", 0)) + 1
    save_task(config, task)
    emit_task_event(
        config,
        "task_failed",
        task,
        source="run-next",
        summary=error_message,
        payload=transition_payload(task, failure_count=task.get("failure_count")),
    )


def mark_profile_failure(config: Config, task: dict[str, Any], error_message: str) -> None:
    task["status"] = "failed"
    task["last_error"] = f"invalid execution profile: {error_message}"
    task["failure_count"] = int(task.get("failure_count", 0)) + 1
    save_task(config, task)
    emit_task_event(
        config,
        "task_failed",
        task,
        source="run-next",
        summary=task["last_error"],
        payload=transition_payload(task, failure_count=task.get("failure_count")),
    )


def should_trigger_post_run_wake(config: Config, processed_task: dict[str, Any] | None) -> bool:
    if not processed_task:
        return False
    if in_global_cooldown(config):
        return False
    if is_runner_paused(config):
        return False
    if is_in_cooldown(processed_task):
        return False
    return select_next_task(config) is not None or has_actionable_auto_review_candidate(config)


def should_trigger_post_review_wake(config: Config) -> bool:
    if in_global_cooldown(config):
        return False
    if is_runner_paused(config):
        return False
    return select_next_task(config) is not None or has_actionable_auto_review_candidate(config)


def maybe_run_empty_codex_cli_maintenance(config: Config) -> dict[str, Any] | None:
    if not config.codex_cli_maintenance_on_empty:
        return None
    if in_global_cooldown(config):
        return None
    if is_runner_paused(config):
        return None
    if select_next_task(config) is not None:
        return None
    if has_actionable_auto_review_candidate(config):
        return None
    report = build_codex_cli_maintenance_report(config)
    if report.get("status") != "ready":
        return None
    return run_codex_cli_maintenance(config)


def apply_codex_result(
    config: Config,
    task: dict,
    result: CodexResult,
    *,
    git_status_cwd: Path | None = None,
    execution_settings: ExecutionSettings | None = None,
) -> None:
    clear_active_run_metadata(task)
    task.setdefault("log_paths", []).append(str(result.log_path))
    record_last_run(task, result, execution_settings=execution_settings)
    if result.command_kind == "resume":
        task["resume_count"] = int(task.get("resume_count", 0)) + 1
    if result.session_id:
        task["session_id"] = result.session_id
    if result.thread_id:
        task["thread_id"] = result.thread_id

    previous_runnable_status = resumable_status(task)

    if result.watchdog_reason in {STARTUP_STALL_REASON, FIRST_MEANINGFUL_STALL_REASON}:
        mark_startup_stall(config, task, result, previous_runnable_status)
        return

    final_response = result.final_response
    if not final_response:
        if result.rate_limited:
            cooldown_until = add_seconds(config.rate_limit_cooldown_seconds)
            task["status"] = previous_runnable_status
            task["cooldown_until"] = cooldown_until
            task["last_error"] = compact_error(result.stderr, "rate-limit or usage-limit detected")
            task["rate_limit_count"] = int(task.get("rate_limit_count", 0)) + 1
            save_task(config, task)
            mark_rate_limit(config, cooldown_until, task["id"])
            capture_rate_limit_evidence(config, task, result, cooldown_until)
            emit_task_event(
                config,
                "rate_limit_detected",
                task,
                source="run-next",
                summary=f"rate limit detected for task {task.get('id')}",
                payload=transition_payload(
                    task,
                    cooldown_until=cooldown_until,
                    matched_markers=sorted(set(result.rate_limit_markers or [])),
                ),
            )
            return
        mark_non_rate_failure(config, task, result, "missing final JSON response")
        return

    if final_response.get("task_id") and final_response.get("task_id") != task.get("id"):
        mark_non_rate_failure(config, task, result, "final JSON task_id mismatch")
        return

    status = final_response.get("status")
    if status not in {"completed", "needs_resume", "blocked_user", "failed"}:
        mark_non_rate_failure(config, task, result, "invalid final JSON status")
        return

    task["last_result"] = final_response
    git_status = inspect_task_git_status(str(git_status_cwd) if git_status_cwd else task.get("cwd"))
    if git_status:
        task["git_status"] = git_status
    else:
        task.pop("git_status", None)
    task["last_error"] = None
    task["cooldown_until"] = None

    if status == "completed":
        task["status"] = "completed"
        task["review_status"] = "unreviewed"
        task["reviewed_at"] = None
        task["review_reason"] = None
        task.pop("reviewer_codex_backoff", None)
        if task.get("root_task_id") or task.get("chain_status"):
            task["chain_status"] = "awaiting_review"
        task["next_prompt"] = None
        task["completed_at"] = iso_now()
        save_task(config, task)
        mark_success(config, task["id"])
        payload = transition_payload(task, completed_at=task.get("completed_at"))
        payload.update(result_summary_payload(task))
        emit_task_event(
            config,
            "task_completed",
            task,
            source="run-next",
            summary=payload.get("summary_excerpt") or f"completed task {task.get('id')}",
            payload=payload,
        )
        return

    if status == "needs_resume":
        task["status"] = "needs_resume"
        task["next_prompt"] = final_response.get("next_prompt") or ""
        save_task(config, task)
        payload = transition_payload(task)
        payload.update(result_summary_payload(task))
        emit_task_event(
            config,
            "task_needs_resume",
            task,
            source="run-next",
            summary=payload.get("summary_excerpt") or f"task {task.get('id')} needs resume",
            payload=payload,
        )
        return

    if status == "blocked_user":
        task["status"] = "blocked_user"
        task["next_prompt"] = final_response.get("next_prompt") or None
        save_task(config, task)
        payload = transition_payload(task)
        payload.update(result_summary_payload(task))
        emit_task_event(
            config,
            "task_blocked_user",
            task,
            source="run-next",
            summary=payload.get("summary_excerpt") or f"task {task.get('id')} blocked on user input",
            payload=payload,
        )
        return

    task["status"] = "failed"
    task["last_error"] = final_response.get("summary") or "Codex reported failed"
    task["failure_count"] = int(task.get("failure_count", 0)) + 1
    save_task(config, task)
    payload = transition_payload(task, failure_count=task.get("failure_count"))
    payload.update(result_summary_payload(task))
    emit_task_event(
        config,
        "task_failed",
        task,
        source="run-next",
        summary=payload.get("summary_excerpt") or str(task.get("last_error") or f"failed task {task.get('id')}"),
        payload=payload,
    )


def apply_shell_result(
    config: Config,
    task: dict,
    result: ShellResult,
    *,
    git_status_cwd: Path | None = None,
) -> None:
    clear_active_run_metadata(task)
    task.setdefault("log_paths", []).append(str(result.log_path))
    record_shell_last_run(task, result)
    task["last_result"] = shell_last_result(task, result)
    task["last_error"] = None if result.returncode == 0 and not result.timed_out else shell_error_summary(result)
    task["cooldown_until"] = None
    git_status = inspect_task_git_status(str(git_status_cwd) if git_status_cwd else task.get("cwd"))
    if git_status:
        task["git_status"] = git_status
    else:
        task.pop("git_status", None)

    if result.returncode == 0 and not result.timed_out:
        task["status"] = "completed"
        task["review_status"] = "unreviewed"
        task["reviewed_at"] = None
        task["review_reason"] = None
        task["next_prompt"] = None
        task["completed_at"] = iso_now()
        save_task(config, task)
        mark_success(config, task["id"])
        payload = transition_payload(task, completed_at=task.get("completed_at"))
        payload.update(result_summary_payload(task))
        emit_task_event(
            config,
            "task_completed",
            task,
            source="run-next",
            summary=payload.get("summary_excerpt") or f"completed task {task.get('id')}",
            payload=payload,
        )
        return

    task["status"] = "failed"
    task["failure_count"] = int(task.get("failure_count", 0)) + 1
    save_task(config, task)
    payload = transition_payload(
        task,
        failure_count=task.get("failure_count"),
        timed_out=result.timed_out,
        returncode=result.returncode,
    )
    payload.update(result_summary_payload(task))
    emit_task_event(
        config,
        "task_failed",
        task,
        source="run-next",
        summary=payload.get("summary_excerpt") or str(task.get("last_error") or f"failed task {task.get('id')}"),
        payload=payload,
    )


def record_shell_last_run(task: dict, result: ShellResult) -> None:
    task["last_run"] = {
        "execution_backend": "shell",
        "command_kind": "shell",
        "command": result.command,
        "returncode": result.returncode,
        "started_at": task.get("started_at") or result.started_at,
        "finished_at": result.finished_at,
        "duration_seconds": result.duration_seconds,
        "timeout_seconds": result.timeout_seconds,
        "timed_out": result.timed_out,
        "log_path": str(result.log_path),
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
    }


def clear_active_run_metadata(task: dict[str, Any]) -> None:
    for key in ACTIVE_RUN_FIELDS:
        task.pop(key, None)


def shell_last_result(task: dict, result: ShellResult) -> dict[str, Any]:
    status = "completed" if result.returncode == 0 and not result.timed_out else "failed"
    summary = "shell command completed" if status == "completed" else shell_error_summary(result)
    return {
        "task_id": task.get("id"),
        "status": status,
        "summary": summary,
        "changed_files": [],
        "verification": [
            f"shell command exited with {result.returncode}"
            if result.returncode is not None
            else f"shell command timed out after {result.timeout_seconds}s"
        ],
    }


def shell_error_summary(result: ShellResult) -> str:
    if result.timed_out:
        return f"shell command timed out after {result.timeout_seconds}s"
    if result.error:
        return result.error
    return f"shell command exited with {result.returncode}"


def mark_worktree_prepare_failure(config: Config, task: dict, report: dict[str, Any]) -> None:
    errors = [str(error) for error in report.get("errors") or []]
    reason = "; ".join(errors) if errors else "worktree preparation failed"
    task["status"] = "failed"
    task["last_error"] = reason
    task["failure_count"] = int(task.get("failure_count", 0)) + 1
    classification = report.get("classification")
    if task.get("execution_mode") == "git_worktree" or isinstance(classification, dict):
        task["execution_worktree_status"] = "recovery_required"
        task["execution_recovery_required_at"] = iso_now()
    save_task(config, task)
    payload = transition_payload(
        task,
        failure_count=task.get("failure_count"),
        reason=reason,
        worktree_prepare_report={
            "classification": classification,
            "errors": errors,
            "warnings": report.get("warnings") or [],
        },
    )
    emit_task_event(
        config,
        "task_failed",
        task,
        source="run-next",
        summary=reason,
        payload=payload,
    )


def auto_commit_worktree_result(config: Config, task: dict[str, Any], result: CodexResult, worktree_path: Path) -> None:
    final_response = result.final_response if isinstance(result.final_response, dict) else None
    if not final_response or final_response.get("status") != "completed":
        return
    if task.get("execution_mode") != "git_worktree":
        return
    if not worktree_has_changes(worktree_path):
        return

    paths, skipped = safe_changed_file_paths(final_response.get("changed_files"))
    if not paths:
        task["execution_commit_warning"] = "worktree dirty but no safe changed_files paths were reported"
        return

    add_result = run_git(worktree_path, ["add", "--", *paths])
    if add_result.returncode != 0:
        task["execution_commit_warning"] = "cannot stage reported changed_files: " + clean_git_error(add_result)
        return
    if not worktree_has_staged_changes(worktree_path):
        task["execution_commit_warning"] = "worktree dirty but reported changed_files produced no staged changes"
        return

    subject = f"Complete cbr task {task.get('id')}"
    body = "Created automatically by codex-batch-runner from reported changed_files in the task worktree."
    commit_result = run_git(worktree_path, ["commit", "-m", subject, "-m", body])
    if commit_result.returncode != 0:
        task["execution_commit_warning"] = "cannot commit task worktree changes: " + clean_git_error(commit_result)
        return

    head = run_git(worktree_path, ["rev-parse", "--verify", "HEAD^{commit}"])
    if head.returncode != 0 or not head.stdout.strip():
        task["execution_commit_warning"] = "committed task worktree changes but could not read HEAD"
        return

    commit = head.stdout.strip()
    task["execution_commit"] = commit
    task["execution_committed_at"] = iso_now()
    if skipped:
        task["execution_commit_warning"] = "skipped unsafe changed_files paths: " + ", ".join(skipped)
    else:
        task.pop("execution_commit_warning", None)

    final_response["commits"] = append_commit(final_response.get("commits"), commit)
    final_response["push_status"] = {
        "status": "not_pushed",
        "branch": task.get("execution_branch"),
        "reason": "runner created a local task worktree commit; push/apply remains explicit",
    }
    emit_task_event(
        config,
        "task_worktree_committed",
        task,
        source="run-next",
        summary=f"committed worktree changes for task {task.get('id')}",
        payload=transition_payload(
            task,
            execution_branch=task.get("execution_branch"),
            execution_commit=commit,
            changed_files=paths,
        ),
    )


def worktree_has_changes(worktree_path: Path) -> bool:
    status = run_git(worktree_path, ["status", "--porcelain=v1", "--untracked-files=all"])
    return status.returncode == 0 and bool(status.stdout.strip())


def worktree_has_staged_changes(worktree_path: Path) -> bool:
    diff = run_git(worktree_path, ["diff", "--cached", "--quiet", "--exit-code"])
    return diff.returncode == 1


def safe_changed_file_paths(changed_files: object) -> tuple[list[str], list[str]]:
    if not isinstance(changed_files, list):
        return [], []
    paths: list[str] = []
    skipped: list[str] = []
    for value in changed_files:
        text = str(value).strip()
        pure = PurePath(text)
        if not text or pure.is_absolute() or ".." in pure.parts or ".git" in pure.parts:
            skipped.append(text or "<empty>")
            continue
        paths.append(text)
    return paths, skipped


def append_commit(existing: object, commit: str) -> list[str]:
    values = [str(item) for item in existing] if isinstance(existing, list) else []
    if commit not in values:
        values.append(commit)
    return values


def mark_non_rate_failure(config: Config, task: dict, result: CodexResult, reason: str) -> None:
    max_attempts = int(task.get("max_attempts") or config.default_max_attempts)
    task["last_error"] = compact_error(result.stderr, reason)
    task["failure_count"] = int(task.get("failure_count", 0)) + 1
    if int(task.get("attempts", 0)) >= max_attempts:
        task["status"] = "failed"
    else:
        task["status"] = resumable_status(task)
    save_task(config, task)
    if task.get("status") == "failed":
        emit_task_event(
            config,
            "task_failed",
            task,
            source="run-next",
            summary=reason,
            payload=transition_payload(task, failure_count=task.get("failure_count"), reason=reason),
        )


def mark_startup_stall(
    config: Config,
    task: dict,
    result: CodexResult,
    previous_runnable_status: str,
) -> None:
    progress = result.progress or {}
    reason = result.watchdog_reason or "startup_stall"
    now = iso_now()
    if previous_runnable_status == "needs_resume":
        task["status"] = "needs_resume"
        task["cooldown_until"] = None
    else:
        task["status"] = "runnable"
        task["cooldown_until"] = add_seconds(config.codex_startup_stall_cooldown_seconds)
    task["last_error"] = startup_stall_error(reason, progress)
    task["last_progress"] = progress
    task["startup_stalled_at"] = now
    task["startup_stall_count"] = int(task.get("startup_stall_count", 0)) + 1
    save_task(config, task)
    payload = transition_payload(
        task,
        reason=reason,
        cooldown_until=task.get("cooldown_until"),
        startup_stall_count=task.get("startup_stall_count"),
        progress=progress,
    )
    emit_task_event(
        config,
        "task_startup_stalled",
        task,
        source="run-next",
        summary=startup_stall_error(reason, progress),
        payload=payload,
    )


def startup_stall_error(reason: str, progress: dict[str, Any]) -> str:
    if reason == FIRST_MEANINGFUL_STALL_REASON:
        return "codex turn stalled before meaningful JSONL events"
    if progress.get("stdout_empty"):
        return "codex startup stalled before any JSONL output"
    return "codex startup stalled before meaningful JSONL events"


def record_last_run(task: dict, result: CodexResult, *, execution_settings: ExecutionSettings | None = None) -> None:
    finished_at = iso_now()
    started_at = task.get("started_at")
    task["last_run"] = {
        "command_kind": result.command_kind,
        "returncode": result.returncode,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds(started_at, finished_at),
        "resume_id_used": result.resume_id_used,
        "log_path": str(result.log_path),
    }
    if execution_settings and execution_settings_has_metadata(execution_settings):
        task["last_run"]["execution_profile"] = execution_settings.profile_name
        task["last_run"]["execution_profile_source"] = execution_settings.profile_source
        task["last_run"]["execution_profile_reason"] = execution_settings.profile_reason
        task["last_run"]["model"] = execution_settings.model
        task["last_run"]["codex_profile"] = execution_settings.codex_profile
        task["last_run"]["config_override_keys"] = sorted((execution_settings.config_overrides or {}).keys())
        task["last_run"]["token_budget_hint"] = execution_settings.token_budget_hint
    if result.watchdog_reason:
        task["last_run"]["watchdog_reason"] = result.watchdog_reason


def execution_settings_has_metadata(settings: ExecutionSettings) -> bool:
    return any(
        value not in (None, "", {})
        for value in (
            settings.profile_name,
            settings.profile_source,
            settings.profile_reason,
            settings.model,
            settings.codex_profile,
            settings.config_overrides,
            settings.token_budget_hint,
        )
    )


def duration_seconds(started_at: object, finished_at: object) -> float | None:
    started = parse_time(started_at)
    finished = parse_time(finished_at)
    if not started or not finished:
        return None
    return round((finished - started).total_seconds(), 3)


def compact_error(stderr: str, fallback: str) -> str:
    text = stderr.strip()
    if not text:
        return fallback
    return text[-2000:]


def resumable_status(task: dict) -> str:
    if task.get("next_prompt") or resume_id(task):
        return "needs_resume"
    return "runnable"


def resume_id(task: dict) -> str | None:
    return task.get("session_id") or task.get("thread_id") or None


def inspect_task_git_status(cwd: object) -> dict[str, Any] | None:
    if not cwd or not shutil.which("git"):
        return None
    workdir = Path(str(cwd)).expanduser()
    repo_root = run_git(workdir, ["rev-parse", "--show-toplevel"])
    if repo_root.returncode != 0 or not repo_root.stdout.strip():
        return None

    repo_path = Path(repo_root.stdout.strip()).expanduser().resolve()
    status: dict[str, Any] = {
        "root": str(repo_path),
        "branch": None,
        "dirty": None,
        "upstream": None,
        "comparison_ref": None,
        "ahead": None,
        "behind": None,
        "has_unpushed": None,
        "unpushed_commits": [],
        "warnings": [],
        "inspected_at": iso_now(),
    }

    branch = run_git(repo_path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if branch.returncode == 0 and branch.stdout.strip():
        status["branch"] = branch.stdout.strip()
    else:
        head = run_git(repo_path, ["rev-parse", "--short", "HEAD"])
        status["branch"] = f"HEAD ({head.stdout.strip()})" if head.returncode == 0 and head.stdout.strip() else "HEAD"

    dirty = run_git(repo_path, ["status", "--porcelain=v1", "--untracked-files=all"])
    if dirty.returncode == 0:
        status["dirty"] = bool(dirty.stdout.strip())
    else:
        status["warnings"].append(f"cannot read dirty status: {clean_git_error(dirty)}")

    comparison_ref = git_comparison_ref(repo_path, status)
    if comparison_ref:
        counts = run_git(repo_path, ["rev-list", "--left-right", "--count", f"{comparison_ref}...HEAD"])
        if counts.returncode == 0:
            parts = counts.stdout.strip().split()
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                status["behind"] = int(parts[0])
                status["ahead"] = int(parts[1])
                status["has_unpushed"] = status["ahead"] > 0
                if status["ahead"]:
                    log = run_git(repo_path, ["log", "--format=%h %s", "--max-count=20", f"{comparison_ref}..HEAD"])
                    if log.returncode == 0:
                        status["unpushed_commits"] = [line for line in log.stdout.splitlines() if line.strip()]
                    else:
                        status["warnings"].append(f"cannot list unpushed commits: {clean_git_error(log)}")
            else:
                status["warnings"].append(f"cannot parse ahead/behind output for {comparison_ref}")
        else:
            status["warnings"].append(f"cannot read ahead/behind against {comparison_ref}: {clean_git_error(counts)}")

    return status


def git_comparison_ref(repo_path: Path, status: dict[str, Any]) -> str | None:
    upstream = run_git(repo_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if upstream.returncode == 0 and upstream.stdout.strip():
        value = upstream.stdout.strip()
        status["upstream"] = value
        status["comparison_ref"] = value
        return value

    branch = status.get("branch")
    if branch and not str(branch).startswith("HEAD"):
        origin_branch = f"origin/{branch}"
        if run_git(repo_path, ["show-ref", "--verify", "--quiet", f"refs/remotes/{origin_branch}"]).returncode == 0:
            status["comparison_ref"] = origin_branch
            status["warnings"].append(f"no upstream configured; using {origin_branch} for ahead/behind")
            return origin_branch

    if run_git(repo_path, ["show-ref", "--verify", "--quiet", "refs/remotes/origin/main"]).returncode == 0:
        status["comparison_ref"] = "origin/main"
        status["warnings"].append("no upstream configured; using origin/main for ahead/behind")
        return "origin/main"

    status["warnings"].append("no upstream or local origin ref available for ahead/behind")
    return None


def run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", "-C", str(cwd), *args], 1, "", str(exc))


def clean_git_error(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text.splitlines()[-1] if text else f"git exited with {result.returncode}"

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .codex import CodexResult, run_codex
from .config import Config
from .evidence import capture_rate_limit_evidence
from .fs import ensure_dir
from .lock import FileLock
from .prompts import build_prompt
from .queue import recover_stale_running_tasks, save_task, select_next_task
from .state import in_global_cooldown, mark_rate_limit, mark_run, mark_success
from .timeutil import add_seconds, iso_now


@dataclass
class RunOutcome:
    status: str
    message: str
    task_id: str | None = None


def run_next(config: Config) -> RunOutcome:
    ensure_dir(config.queue_dir)
    ensure_dir(config.log_dir)
    if in_global_cooldown(config):
        return RunOutcome(status="cooldown", message="global cooldown is active")

    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire():
        return RunOutcome(status="locked", message="another runner is active")

    task: dict[str, Any] | None = None
    try:
        recover_stale_running_tasks(config)
        task = select_next_task(config)
        if not task:
            mark_run(config, None)
            return RunOutcome(status="empty", message="no runnable task")

        task["status"] = "running"
        task["started_at"] = iso_now()
        task["attempts"] = int(task.get("attempts", 0)) + 1
        save_task(config, task)
        mark_run(config, task["id"])

        resume_unavailable = bool(task.get("next_prompt") and not resume_id(task))
        prompt = build_prompt(task, resume_unavailable=resume_unavailable)
        result = run_codex(config, task, prompt, task["attempts"])
        apply_codex_result(config, task, result)
        return RunOutcome(status=task["status"], message="task processed", task_id=task["id"])
    finally:
        lock.release()


def apply_codex_result(config: Config, task: dict, result: CodexResult) -> None:
    task.setdefault("log_paths", []).append(str(result.log_path))
    if result.session_id:
        task["session_id"] = result.session_id
    if result.thread_id:
        task["thread_id"] = result.thread_id

    previous_runnable_status = resumable_status(task)

    final_response = result.final_response
    if not final_response:
        if result.rate_limited:
            cooldown_until = add_seconds(config.rate_limit_cooldown_seconds)
            task["status"] = previous_runnable_status
            task["cooldown_until"] = cooldown_until
            task["last_error"] = compact_error(result.stderr, "rate-limit or usage-limit detected")
            save_task(config, task)
            mark_rate_limit(config, cooldown_until, task["id"])
            capture_rate_limit_evidence(config, task, result, cooldown_until)
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
    task["last_error"] = None
    task["cooldown_until"] = None

    if status == "completed":
        task["status"] = "completed"
        task["review_status"] = "unreviewed"
        task["reviewed_at"] = None
        task["review_reason"] = None
        task["next_prompt"] = None
        task["completed_at"] = iso_now()
        save_task(config, task)
        mark_success(config, task["id"])
        return

    if status == "needs_resume":
        task["status"] = "needs_resume"
        task["next_prompt"] = final_response.get("next_prompt") or ""
        save_task(config, task)
        return

    if status == "blocked_user":
        task["status"] = "blocked_user"
        task["next_prompt"] = final_response.get("next_prompt") or None
        save_task(config, task)
        return

    task["status"] = "failed"
    task["last_error"] = final_response.get("summary") or "Codex reported failed"
    save_task(config, task)


def mark_non_rate_failure(config: Config, task: dict, result: CodexResult, reason: str) -> None:
    max_attempts = int(task.get("max_attempts") or config.default_max_attempts)
    task["last_error"] = compact_error(result.stderr, reason)
    if int(task.get("attempts", 0)) >= max_attempts:
        task["status"] = "failed"
    else:
        task["status"] = resumable_status(task)
    save_task(config, task)


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

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex import CodexResult, run_codex
from .config import Config
from .events import emit_task_event, result_summary_payload, transition_payload
from .evidence import capture_rate_limit_evidence
from .fs import ensure_dir
from .lock import FileLock
from .prompts import build_prompt
from .queue import is_in_cooldown, recover_stale_running_tasks, save_task, select_next_task
from .state import in_global_cooldown, mark_rate_limit, mark_run, mark_success
from .timeutil import add_seconds, iso_now, parse_time
from .triggers import run_post_run_trigger


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
    outcome: RunOutcome | None = None
    try:
        recover_stale_running_tasks(config)
        task = select_next_task(config)
        if not task:
            mark_run(config, None)
            outcome = RunOutcome(status="empty", message="no runnable task")
            return outcome

        started_at = iso_now()
        resume_requested = task.get("status") == "needs_resume"
        resume_unavailable = bool(resume_requested and task.get("next_prompt") and not resume_id(task))
        prompt = build_prompt(task, resume_unavailable=resume_unavailable)
        task["status"] = "running"
        task["started_at"] = started_at
        task["resume_requested"] = resume_requested
        task["resume_unavailable"] = resume_unavailable
        task["resume_unavailable_at"] = started_at if resume_unavailable else None
        if resume_unavailable:
            task["resume_unavailable_attempts"] = int(task.get("resume_unavailable_attempts", 0)) + 1
        task["attempts"] = int(task.get("attempts", 0)) + 1
        task["run_count"] = int(task.get("run_count", 0)) + 1
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
            ),
        )
        mark_run(config, task["id"])

        result = run_codex(config, task, prompt, task["attempts"])
        apply_codex_result(config, task, result)
        outcome = RunOutcome(status=task["status"], message="task processed", task_id=task["id"])
        return outcome
    finally:
        lock.release()
        if outcome and outcome.task_id and should_trigger_post_run_wake(config, task):
            run_post_run_trigger(config)


def should_trigger_post_run_wake(config: Config, processed_task: dict[str, Any] | None) -> bool:
    if not processed_task:
        return False
    if in_global_cooldown(config):
        return False
    if is_in_cooldown(processed_task):
        return False
    return select_next_task(config) is not None


def apply_codex_result(config: Config, task: dict, result: CodexResult) -> None:
    task.setdefault("log_paths", []).append(str(result.log_path))
    record_last_run(task, result)
    if result.command_kind == "resume":
        task["resume_count"] = int(task.get("resume_count", 0)) + 1
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
    git_status = inspect_task_git_status(task.get("cwd"))
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


def record_last_run(task: dict, result: CodexResult) -> None:
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

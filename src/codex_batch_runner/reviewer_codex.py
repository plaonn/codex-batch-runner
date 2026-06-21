from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex import format_command, parse_json_line
from .config import Config
from .fs import ensure_dir
from .limits import matched_rate_limit_markers
from .timeutil import iso_now
from .transcript import sanitize

DECISIONS = {"pass", "needs_fix", "needs_human", "failed_review"}
CONFIDENCES = {"low", "medium", "high"}
SEVERITIES = {"info", "warning", "error"}


@dataclass(frozen=True)
class ReviewerCodexOutcome:
    invoked: bool
    decision: str
    reason: str
    result: dict[str, Any] | None = None
    rate_limited: bool = False
    rate_limit_markers: list[str] | None = None
    log_path: Path | None = None


def build_reviewer_prompt(
    task_id: str,
    bundle: dict[str, Any],
    *,
    calls_used_this_run: int,
    fix_loops_used_for_task: int,
) -> str:
    payload = {
        "task_id": task_id,
        "review_bundle": bundle,
        "reviewer_limits": {
            "calls_used_this_run": calls_used_this_run,
            "fix_loops_used_for_task": fix_loops_used_for_task,
        },
    }
    return (
        "You are reviewer Codex for codex-batch-runner.\n"
        "Use only the sanitized review_bundle below. Do not rely on prior conversation, raw logs, "
        "session ids, thread ids, private queue contents, or credentials.\n"
        "Return exactly one JSON object matching this schema:\n"
        "{"
        '"task_id":"string",'
        '"decision":"pass|needs_fix|needs_human|failed_review",'
        '"confidence":"low|medium|high",'
        '"reason":"string",'
        '"findings":[{"severity":"info|warning|error","summary":"string","evidence":"string"}],'
        '"required_human_checks":["string"],'
        '"suggested_fix_prompt":"string",'
        '"reviewer_limits":{"calls_used_this_run":1,"fix_loops_used_for_task":0,"cooldown_recommended_seconds":0}'
        "}\n"
        "Only use decision=pass when the task clearly satisfies the prompt, verification is adequate, "
        "and public repository safety constraints are satisfied. If evidence is incomplete, use needs_human.\n"
        "reviewer_task_id: "
        f"{task_id}\n"
        "review_input_json:\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n"
    )


def run_reviewer_codex(
    config: Config,
    task: dict[str, Any],
    bundle: dict[str, Any],
    *,
    calls_used_this_run: int,
    fix_loops_used_for_task: int = 0,
) -> ReviewerCodexOutcome:
    task_id = str(task.get("id") or "")
    prompt = build_reviewer_prompt(
        task_id,
        bundle,
        calls_used_this_run=calls_used_this_run,
        fix_loops_used_for_task=fix_loops_used_for_task,
    )
    log_dir = ensure_dir(config.log_dir / task_id)
    log_path = log_dir / f"reviewer-{calls_used_this_run}.jsonl"
    command = format_command(config.codex_command, {"id": task_id}, prompt)
    events: list[dict[str, Any]] = []
    stderr = ""

    try:
        process = subprocess.run(
            command,
            cwd=task.get("cwd") or None,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=reviewer_timeout_seconds(config),
        )
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(timeout_output(exc.stdout), encoding="utf-8")
        return ReviewerCodexOutcome(
            invoked=True,
            decision="failed_review",
            reason="reviewer Codex timed out",
            result=None,
            log_path=log_path,
        )
    except OSError as exc:
        log_path.write_text("", encoding="utf-8")
        return ReviewerCodexOutcome(
            invoked=True,
            decision="failed_review",
            reason=sanitize(f"reviewer Codex failed to start: {exc}"),
            result=None,
            log_path=log_path,
        )

    log_path.write_text(process.stdout, encoding="utf-8")
    stderr = process.stderr or ""
    for line in process.stdout.splitlines():
        parsed = parse_json_line(line)
        if isinstance(parsed, dict):
            events.append(parsed)

    raw_text = stderr + "\n" + process.stdout
    markers = matched_rate_limit_markers(raw_text)
    if markers:
        return ReviewerCodexOutcome(
            invoked=True,
            decision="failed_review",
            reason="reviewer Codex hit rate limit or usage limit",
            result=None,
            rate_limited=True,
            rate_limit_markers=markers,
            log_path=log_path,
        )

    candidate = extract_reviewer_result(events)
    if candidate is None:
        return ReviewerCodexOutcome(
            invoked=True,
            decision="failed_review",
            reason="reviewer Codex did not return reviewer JSON",
            result=None,
            log_path=log_path,
        )

    valid = validate_reviewer_result(candidate, task_id)
    if valid is not None:
        return ReviewerCodexOutcome(
            invoked=True,
            decision="failed_review",
            reason=valid,
            result=sanitize_reviewer_result(candidate),
            log_path=log_path,
        )

    result = sanitize_reviewer_result(candidate)
    return ReviewerCodexOutcome(
        invoked=True,
        decision=str(result["decision"]),
        reason=str(result["reason"]),
        result=result,
        log_path=log_path,
    )


def extract_reviewer_result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        found = find_reviewer_object(event)
        if found is not None:
            return found
    return None


def find_reviewer_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        required = {"task_id", "decision", "confidence", "reason", "findings", "required_human_checks"}
        if required.issubset(value.keys()):
            return value
        for child in value.values():
            found = find_reviewer_object(child)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = find_reviewer_object(child)
            if found is not None:
                return found
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return None
            return find_reviewer_object(parsed)
    return None


def validate_reviewer_result(result: dict[str, Any], task_id: str) -> str | None:
    if result.get("task_id") != task_id:
        return "reviewer JSON task_id mismatch"
    if result.get("decision") not in DECISIONS:
        return "reviewer JSON decision is invalid"
    if result.get("confidence") not in CONFIDENCES:
        return "reviewer JSON confidence is invalid"
    if not nonempty_string(result.get("reason")):
        return "reviewer JSON reason is required"
    if not isinstance(result.get("findings"), list):
        return "reviewer JSON findings must be a list"
    for finding in result.get("findings") or []:
        if not isinstance(finding, dict):
            return "reviewer JSON finding must be an object"
        if finding.get("severity") not in SEVERITIES:
            return "reviewer JSON finding severity is invalid"
        if not nonempty_string(finding.get("summary")) or not nonempty_string(finding.get("evidence")):
            return "reviewer JSON finding summary and evidence are required"
    if not string_list(result.get("required_human_checks")):
        return "reviewer JSON required_human_checks must be strings"
    if not isinstance(result.get("suggested_fix_prompt", ""), str):
        return "reviewer JSON suggested_fix_prompt must be a string"
    limits = result.get("reviewer_limits")
    if not isinstance(limits, dict):
        return "reviewer JSON reviewer_limits must be an object"
    for key in ("calls_used_this_run", "fix_loops_used_for_task", "cooldown_recommended_seconds"):
        if not isinstance(limits.get(key), int) or limits.get(key) < 0:
            return f"reviewer JSON reviewer_limits.{key} must be a non-negative integer"
    return None


def sanitize_reviewer_result(result: dict[str, Any]) -> dict[str, Any]:
    limits = result.get("reviewer_limits", {})
    limits = limits if isinstance(limits, dict) else {}
    return {
        "task_id": sanitize(result.get("task_id")),
        "decision": sanitize(result.get("decision")),
        "confidence": sanitize(result.get("confidence")),
        "reason": sanitize(result.get("reason")),
        "findings": [
            {
                "severity": sanitize(item.get("severity")),
                "summary": sanitize(item.get("summary")),
                "evidence": sanitize(item.get("evidence")),
            }
            for item in result.get("findings", [])
            if isinstance(item, dict)
        ],
        "required_human_checks": [sanitize(item) for item in result.get("required_human_checks", [])],
        "suggested_fix_prompt": sanitize(result.get("suggested_fix_prompt", "")),
        "reviewer_limits": {
            "calls_used_this_run": int(limits.get("calls_used_this_run", 0)),
            "fix_loops_used_for_task": int(limits.get("fix_loops_used_for_task", 0)),
            "cooldown_recommended_seconds": int(limits.get("cooldown_recommended_seconds", 0)),
        },
        "reviewed_at": iso_now(),
    }


def reviewer_timeout_seconds(config: Config) -> int | None:
    if config.codex_total_runtime_timeout_seconds:
        return config.codex_total_runtime_timeout_seconds
    return max(config.codex_startup_stall_seconds, 1) + max(config.codex_first_meaningful_timeout_seconds, 1)


def timeout_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def reviewer_clear_pass(result: dict[str, Any]) -> bool:
    findings = result.get("findings") if isinstance(result.get("findings"), list) else []
    return (
        result.get("decision") == "pass"
        and result.get("confidence") == "high"
        and not result.get("required_human_checks")
        and not any(isinstance(item, dict) and item.get("severity") == "error" for item in findings)
    )

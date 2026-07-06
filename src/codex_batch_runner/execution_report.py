from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import Config
from .queue import list_tasks, task_labels, task_project_id, task_project_root, task_title
from .routing_report import number
from .timeutil import iso_now, parse_time
from .transcript import sanitize

DEFAULT_EXECUTION_REPORT_LIMIT = 50
TOKEN_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def build_execution_report(
    config: Config,
    *,
    project_id: str | None = None,
    project_root: str | None = None,
    category: str | None = None,
    label: str | None = None,
    limit: int = DEFAULT_EXECUTION_REPORT_LIMIT,
    include_archived: bool = False,
) -> dict[str, Any]:
    tasks = list_tasks(config)
    total_available = len(tasks)
    tasks = filter_processed_tasks(
        tasks,
        project_id=project_id,
        project_root=project_root,
        category=category,
        label=label,
        include_archived=include_archived,
    )
    filtered_count = len(tasks)
    tasks.sort(key=execution_sort_key, reverse=True)
    if limit > 0:
        tasks = tasks[:limit]
    rows = [task_execution_row(config, task) for task in tasks]
    return {
        "generated_at": iso_now(),
        "filters": {
            "project": project_id,
            "project_root": project_root,
            "category": category,
            "label": label,
            "include_archived": include_archived,
            "limit": limit,
        },
        "total_available": total_available,
        "filtered_count": filtered_count,
        "row_count": len(rows),
        "rows": rows,
        "summary": summarize_rows(rows),
    }


def filter_processed_tasks(
    tasks: list[dict[str, Any]],
    *,
    project_id: str | None,
    project_root: str | None,
    category: str | None,
    label: str | None,
    include_archived: bool,
) -> list[dict[str, Any]]:
    selected = [task for task in tasks if isinstance(task.get("last_run"), dict)]
    if not include_archived:
        selected = [task for task in selected if task.get("status") != "archived"]
    if project_id:
        selected = [task for task in selected if task_project_id(task) == project_id]
    if project_root:
        selected = [task for task in selected if task_project_root(task) == project_root]
    if category:
        selected = [task for task in selected if task.get("category") == category]
    if label:
        selected = [task for task in selected if label in task_labels(task)]
    return selected


def execution_sort_key(task: dict[str, Any]) -> tuple[str, str]:
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else {}
    return (str(last_run.get("finished_at") or task.get("completed_at") or task.get("updated_at") or ""), str(task.get("id") or ""))


def task_execution_row(config: Config, task: dict[str, Any]) -> dict[str, Any]:
    last_run = task.get("last_run") if isinstance(task.get("last_run"), dict) else {}
    resolved_config = last_run.get("resolved_execution_config") if isinstance(last_run.get("resolved_execution_config"), dict) else {}
    backend = sanitize(last_run.get("execution_backend") or "codex")
    capacity_pool = sanitize(task.get("capacity_pool") or "codex")
    command = list_value(last_run.get("command"))
    token_usage, token_usage_source = derive_token_usage(config, last_run, backend)
    duration = number(last_run.get("duration_seconds"))
    queue_wait = duration_between(task.get("created_at"), last_run.get("started_at") or task.get("started_at"))
    changed_files = changed_files_count(task.get("last_result"))
    verification = verification_count(task.get("last_result"))
    return {
        "task_id": sanitize(task.get("id")),
        "title": sanitize(task_title(task)),
        "project": sanitize(task_project_id(task)),
        "category": sanitize(task.get("category") or ""),
        "labels": [sanitize(label) for label in task_labels(task)],
        "status": sanitize(task.get("status") or ""),
        "review_status": completed_review_status(task),
        "created_at": safe_time(task.get("created_at")),
        "started_at": safe_time(last_run.get("started_at") or task.get("started_at")),
        "finished_at": safe_time(last_run.get("finished_at") or task.get("completed_at")),
        "queue_wait_seconds": queue_wait,
        "duration_seconds": round(duration, 3),
        "execution": {
            "backend": backend,
            "command_kind": sanitize(last_run.get("command_kind") or ""),
            "capacity_pool": capacity_pool,
            "worker_family": worker_family(backend, capacity_pool, command),
            "returncode": int_value(last_run.get("returncode")),
            "timed_out": bool(last_run.get("timed_out")),
        },
        "model": {
            "model": sanitize(resolved_config.get("model") or command_option(command, "--model")),
            "model_group": sanitize(command_option(command, "--model-group")),
            "model_source": sanitize(resolved_config.get("model_source") or ""),
            "selection_rule": sanitize(resolved_config.get("selection_rule") or ""),
            "selection_reason": sanitize(resolved_config.get("selection_reason") or ""),
            "execution_target": sanitize(resolved_config.get("execution_target") or ""),
            "codex_profile": sanitize(resolved_config.get("codex_profile") or ""),
            "budget_hint": sanitize(resolved_config.get("budget_hint") or ""),
        },
        "result": {
            "status": sanitize(last_result_value(task, "status")),
            "reviewer_decision": sanitize(reviewer_decision(task)),
            "changed_files_count": changed_files,
            "verification_count": verification,
        },
        "token_usage": token_usage,
        "token_usage_source": token_usage_source,
    }


def derive_token_usage(config: Config, last_run: dict[str, Any], backend: str) -> tuple[dict[str, int | None], str]:
    stored = usage_dict(last_run.get("usage") if isinstance(last_run, dict) else None)
    if stored:
        return token_usage_payload(stored), "last_run"
    if backend == "shell":
        return token_usage_payload({}), "token_free"
    log_path = safe_log_path(config, last_run.get("log_path"))
    if log_path is None:
        return token_usage_payload({}), "unavailable"
    parsed = extract_latest_usage_from_jsonl(log_path)
    if parsed:
        return token_usage_payload(parsed), "codex_jsonl"
    return token_usage_payload({}), "unavailable"


def extract_latest_usage_from_jsonl(path: Path) -> dict[str, int] | None:
    latest: dict[str, int] | None = None
    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = usage_dict(event.get("usage")) if isinstance(event, dict) else None
                if usage:
                    latest = usage
    except OSError:
        return None
    return latest


def safe_log_path(config: Config, raw_path: object) -> Path | None:
    if not raw_path:
        return None
    path = Path(str(raw_path)).expanduser()
    if not path.is_absolute():
        path = config.root / path
    try:
        resolved = path.resolve(strict=False)
        log_root = config.log_dir.resolve(strict=False)
    except OSError:
        return None
    try:
        resolved.relative_to(log_root)
    except ValueError:
        return None
    return resolved if resolved.exists() else None


def usage_dict(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage: dict[str, int] = {}
    for key in TOKEN_USAGE_KEYS:
        parsed = int_value(value.get(key))
        if parsed is not None:
            usage[key] = parsed
    return usage or None


def token_usage_payload(usage: dict[str, int]) -> dict[str, int | None]:
    payload: dict[str, int | None] = {key: usage.get(key) for key in TOKEN_USAGE_KEYS}
    input_tokens = usage.get("input_tokens")
    cached_input_tokens = usage.get("cached_input_tokens")
    if input_tokens is not None and cached_input_tokens is not None:
        payload["uncached_input_tokens"] = max(0, input_tokens - cached_input_tokens)
    else:
        payload["uncached_input_tokens"] = None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is not None or output_tokens is not None:
        payload["known_total_tokens"] = int(input_tokens or 0) + int(output_tokens or 0)
    else:
        payload["known_total_tokens"] = None
    return payload


def command_option(command: list[str], option: str) -> str:
    for index, item in enumerate(command):
        if item == option and index + 1 < len(command):
            return command[index + 1]
        prefix = option + "="
        if item.startswith(prefix):
            return item[len(prefix) :]
    return ""


def worker_family(backend: str, capacity_pool: str, command: list[str]) -> str:
    command_text = " ".join(command)
    if capacity_pool.startswith("antigravity-") or "agy-cbr-wrapper.py" in command_text:
        return "antigravity"
    if backend == "codex":
        return "codex"
    if backend == "shell":
        return "shell"
    return "external"


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    token_sources = Counter(str(row.get("token_usage_source") or "unknown") for row in rows)
    worker_families = Counter(str(row.get("execution", {}).get("worker_family") or "unknown") for row in rows)
    model_groups = Counter(str(row.get("model", {}).get("model_group") or "none") for row in rows)
    duration_sum = sum(number(row.get("duration_seconds")) for row in rows)
    token_totals = defaultdict(int)
    token_rows = 0
    for row in rows:
        usage = row.get("token_usage") if isinstance(row.get("token_usage"), dict) else {}
        if usage.get("known_total_tokens") is not None:
            token_rows += 1
        for key in (*TOKEN_USAGE_KEYS, "uncached_input_tokens", "known_total_tokens"):
            value = int_value(usage.get(key))
            if value is not None:
                token_totals[key] += value
    return {
        "rows": len(rows),
        "duration_seconds_sum": round(duration_sum, 3),
        "avg_duration_seconds": round(duration_sum / len(rows), 3) if rows else 0.0,
        "token_usage_rows": token_rows,
        "token_usage_sources": dict(sorted(token_sources.items())),
        "worker_families": dict(sorted(worker_families.items())),
        "model_groups": dict(sorted(model_groups.items())),
        "token_totals": dict(sorted(token_totals.items())),
    }


def render_execution_report(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "EXECUTION REPORT",
        f"rows: {report.get('row_count')} filtered: {report.get('filtered_count')} total: {report.get('total_available')}",
        f"token_usage_rows: {summary.get('token_usage_rows', 0)} sources: {compact_mapping(summary.get('token_usage_sources'))}",
        f"worker_families: {compact_mapping(summary.get('worker_families'))}",
        "",
    ]
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    if not rows:
        lines.append("No processed task runs found.")
        return "\n".join(lines) + "\n"
    lines.append("FINISHED\tTASK\tWORKER\tPOOL\tMODEL\tSTATUS\tREVIEW\tDURATION\tTOKENS")
    for row in rows:
        execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
        model = row.get("model") if isinstance(row.get("model"), dict) else {}
        usage = row.get("token_usage") if isinstance(row.get("token_usage"), dict) else {}
        model_value = model.get("model") or model.get("model_group") or model.get("model_source") or "-"
        token_value = token_cell(usage, str(row.get("token_usage_source") or ""))
        lines.append(
            "\t".join(
                [
                    short_time(row.get("finished_at")),
                    str(row.get("title") or row.get("task_id") or "-"),
                    str(execution.get("worker_family") or "-"),
                    str(execution.get("capacity_pool") or "-"),
                    str(model_value),
                    str(row.get("status") or "-"),
                    str(row.get("review_status") or "-"),
                    duration_cell(row.get("duration_seconds")),
                    token_value,
                ]
            )
        )
    return "\n".join(lines) + "\n"


def token_cell(usage: dict[str, Any], source: str) -> str:
    total = usage.get("known_total_tokens")
    if total is None:
        return source or "-"
    cached = usage.get("cached_input_tokens")
    output = usage.get("output_tokens")
    pieces = [f"total={total}"]
    if cached is not None:
        pieces.append(f"cached={cached}")
    if output is not None:
        pieces.append(f"out={output}")
    return ",".join(pieces)


def compact_mapping(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "-"
    return ", ".join(f"{key}={value[key]}" for key in sorted(value))


def duration_cell(value: object) -> str:
    seconds = number(value)
    if seconds <= 0:
        return "-"
    return f"{seconds:.1f}s"


def short_time(value: object) -> str:
    text = str(value or "")
    return text[:19].replace("T", " ") if text else "-"


def completed_review_status(task: dict[str, Any]) -> str:
    if task.get("status") != "completed":
        return ""
    return sanitize(task.get("review_status") or "unreviewed")


def last_result_value(task: dict[str, Any], key: str) -> str:
    result = task.get("last_result") if isinstance(task.get("last_result"), dict) else {}
    return str(result.get(key) or "")


def changed_files_count(value: object) -> int:
    result = value if isinstance(value, dict) else {}
    changed = result.get("changed_files")
    return len(changed) if isinstance(changed, list) else 0


def verification_count(value: object) -> int:
    result = value if isinstance(value, dict) else {}
    verification = result.get("verification")
    return len(verification) if isinstance(verification, list) else 0


def reviewer_decision(task: dict[str, Any]) -> str:
    reviewer = task.get("reviewer_codex") if isinstance(task.get("reviewer_codex"), dict) else {}
    return str(reviewer.get("decision") or task.get("last_review_decision") or "")


def duration_between(start: object, finish: object) -> float | None:
    if not start or not finish:
        return None
    parsed_start = parse_time(str(start))
    parsed_finish = parse_time(str(finish))
    if parsed_start is None or parsed_finish is None:
        return None
    seconds = (parsed_finish - parsed_start).total_seconds()
    return round(max(0.0, seconds), 3)


def safe_time(value: object) -> str | None:
    return str(value) if value else None


def int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def list_value(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]

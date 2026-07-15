from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import Config
from .execution_evidence_v2 import reporting_evidence_view
from .queue import list_tasks, task_labels, task_project_id, task_project_root, task_title
from .review_outcome_evidence import review_outcome_view
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
EXECUTION_REPORT_TABLE_COLUMNS = (
    ("FINISHED", 19, "left"),
    ("TASK", 48, "left"),
    ("WORKER", 11, "left"),
    ("POOL", 18, "left"),
    ("ACTUAL/PLANNED", 20, "left"),
    ("STATUS", 9, "left"),
    ("REVIEW", 14, "left"),
    ("DURATION", 8, "right"),
    ("TOKENS", 42, "left"),
)
MODEL_MEASUREMENT_CONTRACT_VERSION = "model-measurement-summary-v1"
MODEL_MEASUREMENT_TABLE_COLUMNS = (
    ("TARGET", 20, "left"),
    ("MODEL", 22, "left"),
    ("EFFORT", 8, "left"),
    ("LANE", 9, "left"),
    ("RUNS", 5, "right"),
    ("QUALITY", 9, "right"),
    ("TOKENS", 12, "right"),
    ("AVG LAT", 9, "right"),
    ("INTEGRITY", 12, "left"),
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
    resolved_config = (
        last_run.get("resolved_execution_config")
        if isinstance(last_run.get("resolved_execution_config"), dict)
        else {}
    )
    resolved_worker_target = (
        last_run.get("resolved_worker_target") if isinstance(last_run.get("resolved_worker_target"), dict) else {}
    )
    backend = sanitize(last_run.get("execution_backend") or "codex")
    capacity_pool = sanitize(task.get("capacity_pool") or "codex")
    command = list_value(last_run.get("command"))
    observed_evidence = reporting_evidence_view(task)
    token_usage, token_usage_source = derive_token_usage(config, last_run, backend, observed_evidence)
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
            "worker_family": sanitize(
                resolved_worker_target.get("worker_family") or worker_family(backend, capacity_pool, command)
            ),
            "returncode": int_value(last_run.get("returncode")),
            "timed_out": bool(last_run.get("timed_out")),
        },
        "model": {
            "identity_kind": "planned_execution",
            "model": sanitize(resolved_config.get("model") or command_option(command, "--model")),
            "model_group": sanitize(
                command_option(command, "--model-group") or resolved_worker_target.get("model_group")
            ),
            "model_source": sanitize(resolved_config.get("model_source") or ""),
            "selection_rule": sanitize(
                resolved_config.get("selection_rule") or resolved_worker_target.get("selection_rule")
            ),
            "selection_reason": sanitize(
                resolved_config.get("selection_reason") or resolved_worker_target.get("selection_reason")
            ),
            "execution_target": sanitize(
                resolved_config.get("execution_target") or resolved_worker_target.get("worker_target")
            ),
            "codex_profile": sanitize(resolved_config.get("codex_profile") or ""),
            "budget_hint": sanitize(resolved_config.get("budget_hint") or resolved_worker_target.get("budget_hint")),
        },
        "actual_model": dict(observed_evidence.get("actual_model") or {}),
        "identity": dict(observed_evidence.get("identity") or {}),
        "evidence": {
            "schema_version": observed_evidence.get("schema_version"),
            "evidence_contract_version": observed_evidence.get("evidence_contract_version"),
            "evidence_id": observed_evidence.get("evidence_id"),
            "capture": dict(observed_evidence.get("capture") or {}),
            "cohort": dict(observed_evidence.get("cohort") or {}),
            "versions": dict(observed_evidence.get("versions") or {}),
            "routing": dict(observed_evidence.get("routing") or {}),
            "integrity": dict(observed_evidence.get("integrity") or {}),
            "monetary_cost": dict(observed_evidence.get("monetary_cost") or {}),
            "privacy": dict(observed_evidence.get("privacy") or {}),
        },
        "review_outcome": review_outcome_view(task),
        "result": {
            "status": sanitize(last_result_value(task, "status")),
            "reviewer_decision": sanitize(reviewer_decision(task)),
            "changed_files_count": changed_files,
            "verification_count": verification,
        },
        "token_usage": token_usage,
        "token_usage_source": token_usage_source,
    }


def derive_token_usage(
    config: Config,
    last_run: dict[str, Any],
    backend: str,
    observed_evidence: dict[str, Any] | None = None,
) -> tuple[dict[str, int | None], str]:
    if observed_evidence and observed_evidence.get("schema_version") in {2, 3}:
        observation = observed_evidence.get("token_usage")
        if isinstance(observation, dict):
            values = observation.get("values") if isinstance(observation.get("values"), dict) else {}
            return token_usage_payload({key: value for key, value in values.items() if value is not None}), str(
                observation.get("source") or observation.get("status") or "unavailable"
            )
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
    evidence_contracts = Counter(
        str(row.get("evidence", {}).get("evidence_contract_version") or "unknown") for row in rows
    )
    cohort_ids = Counter(
        str(row.get("evidence", {}).get("cohort", {}).get("cohort_id") or "unknown") for row in rows
    )
    model_comparability = Counter(
        "comparable"
        if bool(row.get("evidence", {}).get("cohort", {}).get("comparability", {}).get("model_quality"))
        else "non_comparable"
        for row in rows
    )
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
        "evidence_contracts": dict(sorted(evidence_contracts.items())),
        "cohort_ids": dict(sorted(cohort_ids.items())),
        "model_comparability": dict(sorted(model_comparability.items())),
        "token_totals": dict(sorted(token_totals.items())),
        "model_measurements": summarize_model_measurements(rows),
    }


def summarize_model_measurements(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build descriptive exact-v3 model measurements without inferring capability."""
    exact_rows = [
        row
        for row in rows
        if row.get("evidence", {}).get("evidence_contract_version") == "execution-evidence-v3"
    ]
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in exact_rows:
        identity = row.get("identity") if isinstance(row.get("identity"), dict) else {}
        routing = row.get("evidence", {}).get("routing", {})
        if not isinstance(routing, dict):
            routing = {}
        key = (
            str(routing.get("target_id") or "unknown"),
            str(identity.get("selected_model") or "unknown"),
            str(identity.get("reasoning_effort") or "unknown"),
            str(routing.get("selection_cohort") or "unknown"),
        )
        groups[key].append(row)

    targets = [
        _model_measurement_target(key, grouped_rows)
        for key, grouped_rows in sorted(groups.items())
    ]
    quality_models = {
        target["model"]
        for target in targets
        if target["review"]["comparable_quality_runs"] > 0
    }
    if not exact_rows:
        comparison_status = "no_exact_v3_evidence"
    elif len({target["model"] for target in targets}) < 2:
        comparison_status = "insufficient_models"
    elif len(quality_models) < 2:
        comparison_status = "insufficient_comparable_quality"
    else:
        comparison_status = "descriptive_only"
    return {
        "contract_version": MODEL_MEASUREMENT_CONTRACT_VERSION,
        "mode": "read_only",
        "mutation": {"allowed": False, "applied": False},
        "exact_v3_run_count": len(exact_rows),
        "non_exact_run_count": len(rows) - len(exact_rows),
        "automatic_run_count": sum(
            1 for row in exact_rows if row.get("evidence", {}).get("routing", {}).get("selection_cohort") == "automatic"
        ),
        "override_run_count": sum(
            1 for row in exact_rows if row.get("evidence", {}).get("routing", {}).get("selection_cohort") == "override"
        ),
        "adverse_run_count": sum(
            1 for row in exact_rows if bool(row.get("evidence", {}).get("integrity", {}).get("adverse"))
        ),
        "cross_model_quality_status": comparison_status,
        "inference_allowed": False,
        "targets": targets,
    }


def _model_measurement_target(
    key: tuple[str, str, str, str], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    target_id, model, reasoning_effort, selection_cohort = key
    integrity = Counter(
        str(row.get("evidence", {}).get("integrity", {}).get("status") or "unknown")
        for row in rows
    )
    attestations = Counter(
        str(row.get("identity", {}).get("attestation") or "unknown")
        for row in rows
    )
    execution_cohorts = {
        str(row.get("evidence", {}).get("cohort", {}).get("cohort_id") or "unknown")
        for row in rows
    }
    version_sets = {
        json.dumps(row.get("evidence", {}).get("versions", {}), sort_keys=True, separators=(",", ":"))
        for row in rows
    }
    review_contracts: Counter[str] = Counter()
    comparable_quality_runs = 0
    accepted_runs = 0
    objective_passed_runs = 0
    semantic_pass_runs = 0
    model_quality_comparable_runs = 0
    token_cost_comparable_runs = 0
    completed_latencies: list[float] = []
    censored_latencies: list[float] = []
    token_totals: defaultdict[str, int] = defaultdict(int)
    token_observed_runs = 0
    completed_runs = 0
    failed_runs = 0
    timed_out_runs = 0
    actual_model_observed_runs = 0
    for row in rows:
        cohort = row.get("evidence", {}).get("cohort", {})
        comparability = cohort.get("comparability", {}) if isinstance(cohort, dict) else {}
        model_quality_comparable_runs += int(bool(comparability.get("model_quality")))
        token_cost_comparable_runs += int(bool(comparability.get("token_cost")))
        review = row.get("review_outcome") if isinstance(row.get("review_outcome"), dict) else {}
        review_contracts[str(review.get("evidence_contract_version") or "unknown")] += 1
        review_comparability = review.get("cohort", {}).get("comparability", {})
        execution_quality_comparable = bool(comparability.get("model_quality"))
        integrity_adverse = bool(row.get("evidence", {}).get("integrity", {}).get("adverse"))
        if (
            isinstance(review_comparability, dict)
            and bool(review_comparability.get("quality"))
            and execution_quality_comparable
            and not integrity_adverse
        ):
            comparable_quality_runs += 1
        accepted_runs += int(bool(review.get("acceptance", {}).get("accepted")))
        objective_passed_runs += int(review.get("objective_verification", {}).get("status") == "passed")
        semantic_pass_runs += int(review.get("semantic_review", {}).get("status") == "pass")
        usage = row.get("token_usage") if isinstance(row.get("token_usage"), dict) else {}
        if usage.get("known_total_tokens") is not None:
            token_observed_runs += 1
        for token_key in (*TOKEN_USAGE_KEYS, "uncached_input_tokens", "known_total_tokens"):
            value = int_value(usage.get(token_key))
            if value is not None:
                token_totals[token_key] += value
        execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
        timed_out = bool(execution.get("timed_out"))
        duration = number(row.get("duration_seconds"))
        if timed_out:
            timed_out_runs += 1
            censored_latencies.append(duration)
        else:
            completed_latencies.append(duration)
        returncode = int_value(execution.get("returncode"))
        if returncode == 0 and not timed_out:
            completed_runs += 1
        elif returncode is not None or timed_out:
            failed_runs += 1
        actual = row.get("actual_model") if isinstance(row.get("actual_model"), dict) else {}
        actual_model_observed_runs += int(actual.get("status") == "observed")
    return {
        "target_id": target_id,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "selection_cohort": selection_cohort,
        "run_count": len(rows),
        "execution_cohort_count": len(execution_cohorts),
        "version_set_count": len(version_sets),
        "integrity": {
            "statuses": dict(sorted(integrity.items())),
            "adverse_runs": sum(
                1 for row in rows if bool(row.get("evidence", {}).get("integrity", {}).get("adverse"))
            ),
            "attestations": dict(sorted(attestations.items())),
        },
        "comparability": {
            "model_quality_runs": model_quality_comparable_runs,
            "token_cost_runs": token_cost_comparable_runs,
        },
        "review": {
            "evidence_contracts": dict(sorted(review_contracts.items())),
            "comparable_quality_runs": comparable_quality_runs,
            "accepted_runs": accepted_runs,
            "objective_passed_runs": objective_passed_runs,
            "semantic_pass_runs": semantic_pass_runs,
        },
        "tokens": {
            "observed_runs": token_observed_runs,
            "totals": dict(sorted(token_totals.items())),
        },
        "latency": {
            "completed": _descriptive_latency(completed_latencies),
            "censored": _descriptive_latency(censored_latencies),
        },
        "availability": {
            "completed_runs": completed_runs,
            "failed_runs": failed_runs,
            "timed_out_runs": timed_out_runs,
            "actual_model_observed_runs": actual_model_observed_runs,
        },
    }


def _descriptive_latency(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "sum_seconds": round(sum(values), 3),
        "avg_seconds": round(sum(values) / len(values), 3) if values else None,
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
    table_rows = [
        [
            short_time(row.get("finished_at")),
            str(row.get("title") or row.get("task_id") or "-"),
            execution_display_value(row, "worker_family"),
            execution_display_value(row, "capacity_pool"),
            model_display_value(row),
            str(row.get("status") or "-"),
            str(row.get("review_status") or "-"),
            duration_cell(row.get("duration_seconds")),
            token_cell(
                row.get("token_usage") if isinstance(row.get("token_usage"), dict) else {},
                str(row.get("token_usage_source") or ""),
            ),
        ]
        for row in rows
    ]
    lines.append(render_fixed_table(EXECUTION_REPORT_TABLE_COLUMNS, table_rows))
    measurements = summary.get("model_measurements") if isinstance(summary.get("model_measurements"), dict) else {}
    measurement_targets = measurements.get("targets") if isinstance(measurements.get("targets"), list) else []
    lines.extend(
        [
            "",
            "EXACT MODEL MEASUREMENTS",
            (
                f"exact_v3_runs: {measurements.get('exact_v3_run_count', 0)} "
                f"non_exact_runs: {measurements.get('non_exact_run_count', 0)} "
                f"cross_model_quality: {measurements.get('cross_model_quality_status', 'no_exact_v3_evidence')}"
            ),
        ]
    )
    if measurement_targets:
        lines.append(render_fixed_table(MODEL_MEASUREMENT_TABLE_COLUMNS, [_measurement_table_row(item) for item in measurement_targets]))
    else:
        lines.append("No exact execution-evidence-v3 model measurements found.")
    return "\n".join(lines) + "\n"


def _measurement_table_row(target: dict[str, Any]) -> list[str]:
    review = target.get("review") if isinstance(target.get("review"), dict) else {}
    tokens = target.get("tokens") if isinstance(target.get("tokens"), dict) else {}
    totals = tokens.get("totals") if isinstance(tokens.get("totals"), dict) else {}
    latency = target.get("latency", {}).get("completed", {})
    integrity = target.get("integrity") if isinstance(target.get("integrity"), dict) else {}
    statuses = integrity.get("statuses") if isinstance(integrity.get("statuses"), dict) else {}
    return [
        str(target.get("target_id") or "unknown"),
        str(target.get("model") or "unknown"),
        str(target.get("reasoning_effort") or "unknown"),
        str(target.get("selection_cohort") or "unknown"),
        str(target.get("run_count") or 0),
        str(review.get("comparable_quality_runs") or 0),
        str(totals.get("known_total_tokens") or "-"),
        duration_cell(latency.get("avg_seconds")),
        compact_mapping(statuses),
    ]


def execution_display_value(row: dict[str, Any], key: str) -> str:
    execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
    return str(execution.get(key) or "-")


def model_display_value(row: dict[str, Any]) -> str:
    actual_model = row.get("actual_model") if isinstance(row.get("actual_model"), dict) else {}
    if actual_model.get("status") == "observed" and actual_model.get("value"):
        return "actual:" + str(actual_model.get("value"))
    model = row.get("model") if isinstance(row.get("model"), dict) else {}
    planned = str(model.get("model") or model.get("model_group") or model.get("model_source") or "-")
    return "unknown" if planned == "-" else "planned:" + planned


def render_fixed_table(columns: tuple[tuple[str, int, str], ...], rows: list[list[str]]) -> str:
    header = [column[0] for column in columns]
    table_rows = [header, *rows]
    return "\n".join(render_fixed_table_row(columns, row) for row in table_rows)


def render_fixed_table_row(columns: tuple[tuple[str, int, str], ...], row: list[str]) -> str:
    cells = []
    for index, (_header, width, align) in enumerate(columns):
        raw_value = row[index] if index < len(row) else ""
        cells.append(fixed_cell(raw_value, width, align=align))
    return "  ".join(cells).rstrip()


def fixed_cell(value: object, width: int, *, align: str = "left") -> str:
    text = truncate_display(str(value or "-"), width)
    if align == "right":
        return text.rjust(width)
    return text.ljust(width)


def truncate_display(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 3].rstrip() + "..."


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

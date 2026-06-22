from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import zlib
from pathlib import Path

from .apply_plan import apply_queue_mutation_plan, build_apply_plan_report, render_apply_plan_report
from .config import Config
from .cooldown import MANUAL_COOLDOWN_SAFETY_OFFSET_SECONDS, cooldown_status, format_duration, parse_manual_cooldown
from .doctor import build_doctor_report, render_doctor_report
from .events import DEFAULT_EVENT_LIMIT, list_events, render_events_human, write_event_nonfatal
from .execution_profiles import config_overrides_value
from .evidence import list_rate_limit_evidence
from .follow import DEFAULT_INITIAL_LINES, DEFAULT_POLL_INTERVAL_SECONDS, FollowOptions, follow_task
from .prune import DEFAULT_PRUNE_AGE_DAYS, build_prune_report
from .queue import (
    DEFAULT_HIDDEN_LIST_STATUSES,
    RESOLUTIONS,
    RUNNABLE_STATUSES,
    archive_task,
    create_task,
    dependency_blockers,
    dependency_status,
    is_in_cooldown,
    list_tasks,
    load_task,
    set_resolution,
    set_review_status,
    task_labels,
    task_project_id,
    task_project_root,
    task_title,
)
from .review_bundle import build_review_bundle, render_review_bundle
from .review_next import build_review_next_apply_report, build_review_next_report, render_review_next_report
from .routing_report import DEFAULT_ROUTING_REPORT_LIMIT, build_routing_report, render_routing_report
from .runner import run_next
from .state import clear_global_cooldown, clear_reviewer_codex_cooldown, load_state, set_global_cooldown
from .summary import render_task_summary
from .timeutil import parse_time, utc_now
from .transcript import render_task_transcript
from .triggers import run_post_mutation_trigger
from .wake import schedule_manual_cooldown_wake
from .worktree import build_apply_report, build_cleanup_report, build_prepare_report, render_worktree_report, task_worktree_metadata


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    try:
        return args.func(config, args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cbr", description="Codex batch runner")
    parser.add_argument("--config", help="config JSON path")
    sub = parser.add_subparsers(dest="command", required=True)

    enqueue = sub.add_parser("enqueue", help="enqueue a task")
    enqueue.add_argument("--cwd", required=True, help="working directory for task execution")
    prompt_group = enqueue.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt", help="task prompt")
    prompt_group.add_argument("--prompt-file", help="file containing task prompt")
    enqueue.add_argument("--backend", choices=("codex", "shell"), default="codex", help="execution backend (default: codex)")
    command_group = enqueue.add_mutually_exclusive_group()
    command_group.add_argument("--command-json", help="shell backend argv as a JSON string list")
    command_group.add_argument("--command", nargs=argparse.REMAINDER, help="shell backend argv; must be the final cbr option")
    enqueue.add_argument("--shell-timeout", type=int, dest="shell_timeout_seconds", help="shell backend timeout in seconds")
    enqueue.add_argument("--id", dest="task_id", help="explicit task id")
    enqueue.add_argument("--depends-on", action="append", default=[], help="dependency task id, repeatable")
    enqueue.add_argument("--project", dest="project_id", help="project identifier")
    enqueue.add_argument("--category", help="task category")
    enqueue.add_argument("--label", action="append", default=[], help="task label, repeatable")
    enqueue.add_argument("--created-by", help="task creator")
    enqueue.add_argument("--title", help="human-readable task title")
    enqueue.add_argument("--description", help="optional human-readable task description")
    enqueue.add_argument("--profile", dest="execution_profile", help="cbr execution profile name")
    enqueue.add_argument("--model", help="Codex model override")
    enqueue.add_argument("--codex-profile", help="Codex CONFIG_PROFILE_V2 override")
    enqueue.add_argument(
        "--config-override",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="allowlisted Codex -c config override, repeatable",
    )
    enqueue.add_argument("--token-budget-hint", help="non-enforced token or budget hint")
    enqueue.add_argument("--routing-reason", help="public-safe reason for the profile/provider routing decision")
    enqueue.add_argument("--routing-risk-factor", action="append", default=[], help="public-safe routing risk factor, repeatable")
    enqueue.add_argument("--routing-experiment", help="routing experiment label such as baseline, downshift_probe, upshift_guard, or manual")
    enqueue.set_defaults(func=cmd_enqueue)

    list_cmd = sub.add_parser("list", help="list tasks")
    list_cmd.add_argument("--status", help="filter by status")
    list_cmd.add_argument("--project", dest="project_id", help="filter by project id")
    list_cmd.add_argument("--project-root", help="filter by project root")
    list_cmd.add_argument("--cwd", help="filter by task cwd")
    list_cmd.add_argument("--category", help="filter by category")
    list_cmd.add_argument("--label", help="filter by label")
    list_cmd.add_argument("--all", action="store_true", help="include completed and archived tasks")
    list_cmd.add_argument("--unreviewed", action="store_true", help="show completed tasks waiting for review")
    list_cmd.add_argument("--needs-review", action="store_true", help="show tasks that need operator review")
    list_cmd.add_argument("--verbose", action="store_true", help="include compact result and run summary columns")
    list_cmd.add_argument("--json", action="store_true", help="print JSON")
    list_cmd.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize human output (default: auto)",
    )
    list_cmd.set_defaults(func=cmd_list)

    run_cmd = sub.add_parser("run-next", help="run one eligible task")
    run_cmd.add_argument("--json", action="store_true", help="print JSON")
    run_cmd.set_defaults(func=cmd_run_next)

    show = sub.add_parser("show", help="show a task")
    show.add_argument("task_id")
    show.add_argument("--json", action="store_true", help="print raw JSON")
    show.set_defaults(func=cmd_show)

    summary = sub.add_parser("summary", help="show a compact task summary")
    summary.add_argument("task_id")
    summary.add_argument("--json", action="store_true", help="print raw JSON")
    summary.set_defaults(func=cmd_summary)

    review_bundle = sub.add_parser("review-bundle", help="show a self-contained review bundle")
    review_bundle.add_argument("task_id")
    review_bundle.add_argument("--json", action="store_true", help="print JSON")
    review_bundle.set_defaults(func=cmd_review_bundle)

    review_next = sub.add_parser("review-next", help="review report or opt-in local auto-review for the next completed task needing review")
    review_mode = review_next.add_mutually_exclusive_group()
    review_mode.add_argument("--dry-run", action="store_true", help="report only; this is the default")
    review_mode.add_argument("--apply", action="store_true", help="run local auto-review under the queue lock")
    review_next.add_argument(
        "--mechanical-auto-accept",
        action="store_true",
        help="allow --apply to accept when every local mechanical gate passes",
    )
    review_next.add_argument(
        "--reviewer-codex",
        action="store_true",
        help="allow --apply to invoke reviewer Codex when config call limits permit it",
    )
    review_next.add_argument("--project", dest="project_id", help="filter by project id")
    review_next.add_argument("--project-root", help="filter by project root")
    review_next.add_argument("--category", help="filter by category")
    review_next.add_argument("--label", help="filter by label")
    review_next.add_argument("--json", action="store_true", help="print JSON")
    review_next.set_defaults(func=cmd_review_next)

    routing_report = sub.add_parser("routing-report", help="summarize profile routing outcomes without mutating tasks")
    routing_report.add_argument("--project", dest="project_id", help="filter by project id")
    routing_report.add_argument("--project-root", help="filter by project root")
    routing_report.add_argument("--category", help="filter by category")
    routing_report.add_argument("--label", help="filter by label")
    routing_report.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_ROUTING_REPORT_LIMIT,
        help=f"maximum recent tasks to include after filtering; 0 means no limit (default: {DEFAULT_ROUTING_REPORT_LIMIT})",
    )
    routing_report.add_argument("--include-archived", action="store_true", help="include archived tasks")
    routing_report.add_argument("--json", action="store_true", help="print JSON")
    routing_report.set_defaults(func=cmd_routing_report)

    logs = sub.add_parser("logs", help="show task log paths or log contents")
    logs.add_argument("task_id")
    logs.add_argument("--cat", action="store_true", help="print log contents")
    logs.set_defaults(func=cmd_logs)

    transcript = sub.add_parser("transcript", help="show a readable task transcript")
    transcript.add_argument("task_id")
    transcript.add_argument("--raw", action="store_true", help="print raw JSONL logs")
    transcript.set_defaults(func=cmd_transcript)

    follow = sub.add_parser("follow", help="follow a compact readable task attempt stream")
    follow.add_argument("task_id")
    follow.add_argument("--lines", type=int, default=DEFAULT_INITIAL_LINES, help=f"initial JSONL lines to tail (default: {DEFAULT_INITIAL_LINES})")
    follow.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"seconds between polling reads (default: {DEFAULT_POLL_INTERVAL_SECONDS})",
    )
    follow.add_argument("--max-polls", type=int, help=argparse.SUPPRESS)
    follow.set_defaults(func=cmd_follow)

    accept = sub.add_parser("accept", help="mark a completed task as reviewed and accepted")
    accept.add_argument("task_id")
    accept.add_argument("--reason", help="review note")
    accept.add_argument("--json", action="store_true", help="print raw JSON")
    accept.set_defaults(func=cmd_accept)

    reject = sub.add_parser("reject", help="mark a task review as rejected")
    reject.add_argument("task_id")
    reject.add_argument("--reason", required=True, help="review note")
    reject.add_argument("--follow-up", action="store_true", help="mark as needs_followup instead of rejected")
    reject.add_argument("--json", action="store_true", help="print raw JSON")
    reject.set_defaults(func=cmd_reject)

    archive = sub.add_parser("archive", help="archive a task")
    archive.add_argument("task_id")
    archive.add_argument("--json", action="store_true", help="print raw JSON")
    archive.set_defaults(func=cmd_archive)

    resolve = sub.add_parser("resolve", help="record an operational resolution for failed or blocked tasks")
    resolve.add_argument("task_id")
    resolve.add_argument("--resolution", required=True, choices=sorted(RESOLUTIONS), help="resolution decision")
    resolve.add_argument("--reason", help="resolution note")
    resolve.add_argument("--json", action="store_true", help="print raw JSON")
    resolve.set_defaults(func=cmd_resolve)

    state = sub.add_parser("state", help="show runner state")
    state.set_defaults(func=cmd_state)

    cooldown = sub.add_parser("cooldown", help="show, set, or clear global cooldown")
    cooldown_sub = cooldown.add_subparsers(dest="cooldown_command", required=True)
    cooldown_show = cooldown_sub.add_parser("show", help="show global cooldown status")
    cooldown_show.set_defaults(func=cmd_cooldown_show)
    cooldown_clear = cooldown_sub.add_parser("clear", help="clear global cooldown")
    cooldown_clear.add_argument(
        "--reviewer-codex",
        action="store_true",
        help="clear reviewer Codex cooldown instead of global cooldown",
    )
    cooldown_clear.set_defaults(func=cmd_cooldown_clear)
    cooldown_set = cooldown_sub.add_parser("set", help="set global cooldown reset time")
    cooldown_set.add_argument("value", help="reset time such as 7:6, 6/21 7:06, 2026-06-21 07:06, +90m")
    cooldown_set.set_defaults(func=cmd_cooldown_set)

    rate_limits = sub.add_parser("rate-limits", help="list sanitized rate-limit evidence")
    rate_limits.add_argument("--json", action="store_true", help="print JSON")
    rate_limits.set_defaults(func=cmd_rate_limits)

    events = sub.add_parser("events", help="list recent sanitized event log entries")
    events.add_argument("--task-id", help="filter by task id")
    events.add_argument("--limit", type=int, default=DEFAULT_EVENT_LIMIT, help=f"maximum events to show (default: {DEFAULT_EVENT_LIMIT})")
    events.add_argument("--json", action="store_true", help="print JSON")
    events.set_defaults(func=cmd_events)

    doctor = sub.add_parser("doctor", help="check local cbr health and Codex CLI version without running Codex exec")
    doctor.add_argument("--json", action="store_true", help="print JSON")
    doctor.set_defaults(func=cmd_doctor)

    prune = sub.add_parser("prune", help="report or remove old archived and accepted tasks")
    prune.add_argument(
        "--older-than-days",
        type=int,
        default=DEFAULT_PRUNE_AGE_DAYS,
        help=f"minimum candidate age in days (default: {DEFAULT_PRUNE_AGE_DAYS})",
    )
    prune_mode = prune.add_mutually_exclusive_group()
    prune_mode.add_argument("--apply", action="store_true", help="delete reported safe files")
    prune_mode.add_argument("--dry-run", action="store_true", help="report only; this is the default")
    prune.add_argument(
        "--notifier-cursor-state",
        action="append",
        default=[],
        help="local notifier cursor state JSON path; repeatable",
    )
    prune.add_argument("--json", action="store_true", help="print JSON")
    prune.set_defaults(func=cmd_prune)

    apply_plan = sub.add_parser("apply-plan", help="validate or apply a queue mutation plan")
    apply_plan.add_argument("plan_path", help="queue plan JSON path")
    apply_mode = apply_plan.add_mutually_exclusive_group()
    apply_mode.add_argument("--dry-run", action="store_true", help="validate and report without queue mutations; this is the default")
    apply_mode.add_argument("--apply", action="store_true", help="apply validated safe queue mutations under the queue lock")
    apply_plan.add_argument("--json", action="store_true", help="print JSON")
    apply_plan.set_defaults(func=cmd_apply_plan)

    worktree = sub.add_parser("worktree", help="prepare or cleanup task git worktrees")
    worktree_sub = worktree.add_subparsers(dest="worktree_command", required=True)
    worktree_prepare = worktree_sub.add_parser("prepare", help="prepare a task git worktree without running Codex")
    worktree_prepare.add_argument("task_id")
    prepare_mode = worktree_prepare.add_mutually_exclusive_group(required=True)
    prepare_mode.add_argument("--dry-run", action="store_true", help="report planned worktree preparation")
    prepare_mode.add_argument("--apply", action="store_true", help="create/reuse the worktree and store task metadata under the queue lock")
    worktree_prepare.add_argument("--json", action="store_true", help="print JSON")
    worktree_prepare.set_defaults(func=cmd_worktree_prepare)

    worktree_cleanup = worktree_sub.add_parser("cleanup", help="cleanup a retained task git worktree without deleting the branch")
    worktree_cleanup.add_argument("task_id")
    cleanup_mode = worktree_cleanup.add_mutually_exclusive_group(required=True)
    cleanup_mode.add_argument("--dry-run", action="store_true", help="report planned worktree cleanup")
    cleanup_mode.add_argument("--apply", action="store_true", help="remove the worktree and store task metadata under the queue lock")
    worktree_cleanup.add_argument("--json", action="store_true", help="print JSON")
    worktree_cleanup.set_defaults(func=cmd_worktree_cleanup)

    worktree_apply = worktree_sub.add_parser("apply", help="fast-forward an accepted task worktree branch into the main worktree")
    worktree_apply.add_argument("task_id")
    apply_mode = worktree_apply.add_mutually_exclusive_group(required=True)
    apply_mode.add_argument("--dry-run", action="store_true", help="report planned fast-forward apply without changing git or task state")
    apply_mode.add_argument("--apply", action="store_true", help="fast-forward merge the accepted task branch under the queue lock")
    worktree_apply.add_argument("--json", action="store_true", help="print JSON")
    worktree_apply.set_defaults(func=cmd_worktree_apply)
    return parser


def cmd_enqueue(config: Config, args: argparse.Namespace) -> int:
    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    shell_command = parse_shell_command_args(args)
    if args.backend == "codex" and prompt is None:
        raise ValueError("Codex tasks require --prompt or --prompt-file")
    if args.backend == "shell" and not shell_command:
        raise ValueError("shell tasks require --command-json or --command")
    if args.backend == "shell" and prompt is None:
        prompt = "Shell task: " + shlex.join(shell_command or [])
    task = create_task(
        config=config,
        prompt=prompt or "",
        cwd=args.cwd,
        task_id=args.task_id,
        depends_on=args.depends_on,
        project_id=args.project_id,
        category=args.category,
        labels=args.label,
        created_by=args.created_by,
        title=args.title,
        description=args.description,
        execution_profile=args.execution_profile,
        model=args.model,
        codex_profile=args.codex_profile,
        codex_config_overrides=parse_config_overrides(args.config_override),
        token_budget_hint=args.token_budget_hint,
        routing_reason=args.routing_reason,
        routing_risk_factors=args.routing_risk_factor,
        routing_experiment=args.routing_experiment,
        execution_backend=args.backend,
        shell_command=shell_command,
        shell_timeout_seconds=args.shell_timeout_seconds,
    )
    run_post_mutation_trigger(config)
    print(task["id"])
    return 0


def parse_shell_command_args(args: argparse.Namespace) -> list[str] | None:
    if args.command_json:
        try:
            value = json.loads(args.command_json)
        except json.JSONDecodeError as exc:
            raise ValueError("--command-json must be a JSON list of strings") from exc
        if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
            raise ValueError("--command-json must be a non-empty JSON list of strings")
        if any(item == "" for item in value):
            raise ValueError("--command-json entries must be non-empty strings")
        return list(value)
    if args.command is not None:
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise ValueError("--command requires at least one argv item")
        return command
    return None


def parse_config_overrides(items: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError("--config-override must use KEY=VALUE")
        key, value = item.split("=", 1)
        overrides[key] = value
    return config_overrides_value("codex_config_overrides", overrides)


def cmd_list(config: Config, args: argparse.Namespace) -> int:
    tasks = list_tasks(config)
    by_id = {task.get("id"): task for task in tasks}
    explicit_filter = bool(
        args.status
        or args.project_id
        or args.project_root
        or args.cwd
        or args.category
        or args.label
        or args.unreviewed
        or args.needs_review
    )
    if args.status:
        tasks = [task for task in tasks if task.get("status") == args.status]
    if args.project_id:
        tasks = [task for task in tasks if task_project_id(task) == args.project_id]
    if args.project_root:
        project_root = normalized_path(args.project_root)
        tasks = [task for task in tasks if task_project_root(task) == project_root]
    if args.cwd:
        cwd = normalized_path(args.cwd)
        tasks = [task for task in tasks if normalized_path(task.get("cwd") or "") == cwd]
    if args.category:
        tasks = [task for task in tasks if task.get("category") == args.category]
    if args.label:
        tasks = [task for task in tasks if args.label in task_labels(task)]
    if args.unreviewed:
        tasks = [task for task in tasks if review_status(task) == "unreviewed"]
    if args.needs_review:
        tasks = [task for task in tasks if needs_review(task)]
    if not explicit_filter and not args.all:
        tasks = [task for task in tasks if visible_by_default(task)]
    tasks.sort(key=list_sort_key)
    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    color = list_colorizer(args.color)
    if not args.verbose:
        print(render_compact_list(tasks, by_id, config, color))
        return 0
    header = ["ID", "TITLE", "STATUS", "PROJECT", "ATTEMPTS", "DEPS", "NOTE"]
    header.extend(["RAW_STATUS", "LAST_RESULT", "LAST_RUN", "LAST_ERROR"])
    rows = []
    for task in tasks:
        row = list_table_row(task, by_id, config)
        row.extend(verbose_table_cells(task))
        rows.append(row)
    print(render_table(header, rows))
    return 0


def list_sort_key(task: dict) -> tuple[str, str]:
    return (str(task.get("created_at") or ""), str(task.get("id") or ""))


def render_table(header: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(row[index]) for row in [header, *rows])
        for index in range(len(header))
    ]
    return "\n".join(render_table_row(row, widths) for row in [header, *rows])


def render_table_row(row: list[str], widths: list[int]) -> str:
    padded = [cell.ljust(widths[index]) for index, cell in enumerate(row[:-1])]
    return "  ".join([*padded, row[-1]])


def render_compact_list(tasks: list[dict], by_id: dict[str, dict], config: Config, color: "ListColor") -> str:
    header = ["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"]
    row_groups = [compact_task_rows(task, by_id, config, color) for task in tasks]
    rows = [row for group in row_groups for row in group]
    widths = [max(visible_len(row[index]) for row in [header, *rows]) for index in range(len(header))]
    lines = [render_compact_row(header, widths)]
    for group in row_groups:
        lines.extend(render_compact_row(row, widths) for row in group)
    return "\n".join(lines)


def compact_task_rows(task: dict, by_id: dict[str, dict], config: Config, color: "ListColor") -> list[list[str]]:
    dep_ids = dependency_id_cells(task.get("depends_on"), by_id, config, color)
    note_segments = note_cells(task, by_id, config)
    row_count = max(2, len(dep_ids), len(note_segments))
    rows = [
        [
            color.project(scalar_cell(task_project_id(task))),
            color.task_id(scalar_cell(task.get("id"))),
            color.status(status_cell(task, by_id, config)),
            scalar_cell(task.get("attempts", 0)),
            dep_ids[0] if dep_ids else "-",
            note_segments[0] if note_segments else "-",
        ],
        [
            color.title(task_title(task)),
            "",
            "",
            "",
            dep_ids[1] if len(dep_ids) > 1 else "",
            note_segments[1] if len(note_segments) > 1 else "",
        ],
    ]
    rows.extend(
        [
            "",
            "",
            "",
            "",
            dep_ids[index] if index < len(dep_ids) else "",
            note_segments[index] if index < len(note_segments) else "",
        ]
        for index in range(2, row_count)
    )
    return rows


def render_compact_row(row: list[str], widths: list[int]) -> str:
    padded = [pad_visible(cell, widths[index]) for index, cell in enumerate(row[:-1])]
    return "  ".join([*padded, row[-1]])


def list_table_row(task: dict, by_id: dict[str, dict], config: Config) -> list[str]:
    return [
        scalar_cell(task.get("id")),
        scalar_cell(truncate_table_text(task_title(task), 72)),
        scalar_cell(status_cell(task, by_id, config)),
        scalar_cell(task_project_id(task)),
        scalar_cell(task.get("attempts", 0)),
        deps_cell(task.get("depends_on"), by_id),
        note_cell(task, by_id, config),
    ]


def scalar_cell(value: object) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def deps_cell(depends_on: object, by_id: dict[str, dict] | None = None, color: "ListColor | None" = None) -> str:
    if not isinstance(depends_on, list) or not depends_on:
        return "-"
    color = color or ListColor(False)
    return ",".join(color.task_id(str(dep_id)) for dep_id in depends_on)


def dependency_id_cells(depends_on: object, by_id: dict[str, dict], config: Config, color: "ListColor") -> list[str]:
    if not isinstance(depends_on, list):
        return []
    return [dependency_id_cell(str(dep_id), by_id, config, color) for dep_id in depends_on]


def dependency_id_cell(dep_id: str, by_id: dict[str, dict], config: Config, color: "ListColor") -> str:
    dep = by_id.get(dep_id)
    if dependency_satisfied(dep, config):
        return color.satisfied_dependency(dep_id)
    return color.task_id(dep_id)


def dependency_satisfied(dep: dict | None, config: Config) -> bool:
    if not dep or dep.get("status") != "completed":
        return False
    return not config.dependency_requires_accepted_review or dep.get("review_status") == "accepted"


def status_cell(task: dict, by_id: dict[str, dict] | None = None, config: Config | None = None) -> str:
    status = str(task.get("status") or "-")
    if (
        by_id is not None
        and config is not None
        and status in RUNNABLE_STATUSES
        and not dependency_status(
            task,
            by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )[0]
    ):
        return "blocked_dependency"
    if task.get("resolution") and status in {"failed", "blocked_user"}:
        return "resolved"
    if status == "completed":
        review = review_status(task)
        if review == "unreviewed":
            return "awaiting_review"
        if review == "rejected":
            return "review_failed"
        if review == "needs_followup":
            return "needs_followup"
        if review == "reviewing":
            return "reviewing"
    return status


def note_cell(task: dict, by_id: dict[str, dict], config: Config) -> str:
    notes = note_cells(task, by_id, config)
    return "; ".join(notes) if notes else "-"


def note_cells(task: dict, by_id: dict[str, dict], config: Config) -> list[str]:
    deps_ready = dependency_status(
        task,
        by_id,
        require_accepted_review=config.dependency_requires_accepted_review,
    )[0]
    notes = []
    if is_in_cooldown(task):
        notes.append("cooldown until " + scalar_cell(task.get("cooldown_until")))
    if startup_stalled(task):
        notes.append(startup_stall_note(task))
    if not deps_ready:
        notes.append("blocked by " + ",".join(dependency_blocker_labels(task, by_id, config)))
    if task.get("status") == "failed" and task.get("last_error"):
        notes.append("last error: " + one_line(task.get("last_error")))
    if task.get("status") == "running":
        notes.extend(running_notes(task, config))
    subtask_note = subtask_note_cell(task)
    if subtask_note:
        notes.append(subtask_note)
    profile_note = execution_profile_note(task)
    if profile_note:
        notes.append(profile_note)
    if task.get("resolution"):
        notes.append("resolution: " + str(task.get("resolution")))
    if task.get("status") == "completed":
        review = review_status(task)
        if review == "unreviewed":
            notes.append("awaiting review")
        elif review == "rejected":
            notes.append("review failed")
        elif review == "needs_followup":
            notes.append("needs follow-up")
        elif review == "reviewing":
            notes.append("reviewing")
        chain_note = chain_note_cell(task)
        if chain_note:
            notes.append(chain_note)
        worktree_note = worktree_apply_note(task)
        if worktree_note:
            notes.append(worktree_note)
        if config.auto_review_mechanical_accept and needs_review(task):
            notes.append("mechanical auto-review enabled")
    return notes or ["-"]


def worktree_apply_note(task: dict) -> str:
    if task.get("execution_mode") != "git_worktree":
        return ""
    if task.get("execution_rebase_status") == "blocked":
        return "worktree rebase blocked"
    if task.get("execution_rebase_status") == "rebased" and review_status(task) != "accepted":
        return "worktree rebased; awaiting re-review"
    if review_status(task) != "accepted":
        return ""
    if task.get("execution_apply_status") == "applied":
        target = one_line(task.get("execution_apply_target") or "main")
        return f"worktree applied to {target}"
    return "worktree not applied"


def execution_profile_note(task: dict) -> str:
    parts = []
    if task.get("execution_backend") and task.get("execution_backend") != "codex":
        parts.append("backend=" + one_line(task.get("execution_backend")))
    if task.get("execution_profile"):
        parts.append("profile=" + one_line(task.get("execution_profile")))
    if task.get("model"):
        parts.append("model=" + one_line(task.get("model")))
    if task.get("codex_profile"):
        parts.append("codex_profile=" + one_line(task.get("codex_profile")))
    return " ".join(parts)


def chain_note_cell(task: dict) -> str:
    chain_status = task.get("chain_status")
    decision = task.get("last_review_decision")
    if chain_status and decision:
        return f"chain {chain_status} after {decision}"
    if chain_status:
        return f"chain {chain_status}"
    if decision:
        return f"reviewer {decision}"
    return ""


def subtask_note_cell(task: dict) -> str:
    subtask_type = task.get("subtask_type")
    subtask_for = task.get("subtask_for")
    if subtask_type and subtask_for:
        return f"subtask {subtask_type} for {subtask_for}"
    if subtask_type:
        return f"subtask {subtask_type}"
    return ""


def startup_stalled(task: dict) -> bool:
    progress = task.get("last_progress")
    return bool(task.get("startup_stalled_at") or (isinstance(progress, dict) and progress.get("watchdog_reason")))


def startup_stall_note(task: dict) -> str:
    status = str(task.get("status") or "")
    if status in {"runnable", "needs_resume", "running"}:
        return "startup stall retry evidence"
    return "startup stall history"


def dependency_blocker_labels(task: dict, by_id: dict[str, dict], config: Config) -> list[str]:
    return [
        blocker["id"]
        if blocker["reason"] == "not_completed"
        else f"{blocker['id']}:{blocker['reason']}"
        for blocker in dependency_blockers(
            task,
            by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )
    ]


def dependency_label(dep_id: str, by_id: dict[str, dict]) -> str:
    dep = by_id.get(dep_id)
    if not dep:
        return dep_id
    label = task_title(dep)
    return truncate_table_text(label, 48)


def running_notes(task: dict, config: Config) -> list[str]:
    started = parse_time(task.get("started_at"))
    if not started:
        return []
    elapsed = max(0, int((utc_now() - started).total_seconds()))
    notes = [f"running for {format_elapsed(elapsed)}"]
    progress = task.get("last_progress")
    if not isinstance(progress, dict):
        return notes
    last_event = parse_time(progress.get("last_jsonl_event_at"))
    if last_event:
        age = max(0, int((utc_now() - last_event).total_seconds()))
        notes.append(f"last event {format_elapsed(age)} ago")
    elif elapsed >= config.codex_startup_stall_seconds and not progress.get("first_meaningful_event_at"):
        notes.append(f"no progress {format_elapsed(elapsed)}")
    return notes


def format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h {remainder:02d}m"


class ListColor:
    RESET = "\033[0m"
    DIM = "\033[2m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    LIGHT_CYAN = "\033[96m"
    BLUE = "\033[34m"
    BG_RED = "\033[41;37m"
    BG_YELLOW = "\033[43;30m"
    BG_GREEN = "\033[42;30m"
    BG_CYAN = "\033[46;30m"
    BG_BLUE = "\033[104;30m"
    BG_DIM = "\033[100;37m"
    ID_COLORS = ("\033[35m", "\033[36m", "\033[34m", "\033[32m", "\033[33m", "\033[91m")

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def apply(self, value: str, code: str) -> str:
        if not self.enabled or not value or value == "-":
            return value
        return f"{code}{value}{self.RESET}"

    def task_id(self, task_id: str) -> str:
        if not self.enabled or task_id == "-":
            return task_id
        index = zlib.crc32(task_id.encode("utf-8")) % len(self.ID_COLORS)
        return self.apply(task_id, self.ID_COLORS[index])

    def project(self, project_id: str) -> str:
        return self.apply(project_id, self.LIGHT_CYAN)

    def title(self, title: str) -> str:
        return title

    def satisfied_dependency(self, dep_id: str) -> str:
        if not self.enabled or dep_id == "-":
            return f"{dep_id} (done)"
        return self.apply(dep_id, self.DIM)

    def status(self, status: str) -> str:
        if status in {"failed", "blocked_user", "review_failed", "needs_followup", "blocked_dependency"}:
            return self.apply(status, self.BG_RED)
        if status in {"awaiting_review", "reviewing"}:
            return self.apply(status, self.BG_YELLOW)
        if status == "running":
            return self.apply(status, self.BG_CYAN)
        if status in {"runnable", "needs_resume"}:
            return self.apply(status, self.BG_BLUE)
        if status in {"cooldown", "usage_exhausted"}:
            return self.apply(status, self.BG_DIM)
        if status in {"completed", "accepted"}:
            return self.apply(status, self.BG_GREEN)
        return status


def list_colorizer(mode: str) -> ListColor:
    enabled = mode == "always" or (mode == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    return ListColor(enabled)


def visible_len(value: str) -> int:
    length = 0
    in_escape = False
    for char in value:
        if char == "\033":
            in_escape = True
        elif in_escape and char == "m":
            in_escape = False
        elif not in_escape:
            length += 1
    return length


def pad_visible(value: str, width: int) -> str:
    return value + " " * max(0, width - visible_len(value))


def truncate_table_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def verbose_table_cells(task: dict) -> list[str]:
    return [
        scalar_cell(task.get("status")),
        last_result_cell(task.get("last_result"), task.get("git_status")),
        last_run_cell(task.get("last_run")),
        excerpt_cell(task.get("last_error")),
    ]


def last_result_cell(last_result: object, git_status: object | None = None) -> str:
    if not isinstance(last_result, dict) and not isinstance(git_status, dict):
        return "-"
    parts = []
    if isinstance(last_result, dict):
        if last_result.get("status"):
            parts.append("status=" + one_line(last_result.get("status")))
        if last_result.get("summary"):
            parts.append("summary=" + excerpt(last_result.get("summary")))
        if last_result.get("commits"):
            parts.append("commits=" + commits_summary(last_result.get("commits")))
        if last_result.get("push_status"):
            parts.append("push_status=" + compact_value(last_result.get("push_status")))
    git_summary = git_status_cell(git_status)
    if git_summary != "-":
        parts.append("git=" + git_summary)
    return " ".join(parts) if parts else "-"


def last_run_cell(last_run: object) -> str:
    if not isinstance(last_run, dict):
        return "-"
    parts = []
    command_kind = last_run.get("command_kind")
    if command_kind:
        parts.append("command=" + one_line(command_kind))
    if last_run.get("execution_backend") and last_run.get("execution_backend") != "codex":
        parts.append("backend=" + one_line(last_run.get("execution_backend")))
    if "returncode" in last_run and last_run.get("returncode") is not None:
        parts.append("returncode=" + one_line(last_run.get("returncode")))
    if last_run.get("timed_out"):
        parts.append("timed_out=true")
    if "duration_seconds" in last_run and last_run.get("duration_seconds") is not None:
        parts.append("duration=" + one_line(last_run.get("duration_seconds")) + "s")
    return " ".join(parts) if parts else "-"


def git_status_cell(git_status: object) -> str:
    if not isinstance(git_status, dict):
        return "-"
    parts = []
    if git_status.get("branch"):
        parts.append("branch=" + one_line(git_status.get("branch")))
    if git_status.get("comparison_ref"):
        parts.append("compare=" + one_line(git_status.get("comparison_ref")))
    if git_status.get("ahead") is not None:
        parts.append("ahead=" + one_line(git_status.get("ahead")))
    if git_status.get("behind") is not None:
        parts.append("behind=" + one_line(git_status.get("behind")))
    if git_status.get("dirty") is not None:
        parts.append("dirty=" + str(bool(git_status.get("dirty"))).lower())
    return " ".join(parts) if parts else "-"


def commits_summary(value: object) -> str:
    if isinstance(value, list):
        return one_line(len(value))
    return compact_value(value)


def compact_value(value: object) -> str:
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            item = value.get(key)
            if isinstance(item, (dict, list)):
                parts.append(f"{key}={json.dumps(item, ensure_ascii=False, sort_keys=True)}")
            else:
                parts.append(f"{key}={item}")
        return excerpt(" ".join(parts))
    if isinstance(value, list):
        return excerpt(", ".join(one_line(item) for item in value))
    return excerpt(value)


def excerpt_cell(value: object) -> str:
    if not value:
        return "-"
    return excerpt(value)


def excerpt(value: object, limit: int = 120) -> str:
    text = one_line(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def one_line(value: object) -> str:
    return " ".join(str(value).split())


def normalized_path(value: object) -> str:
    return str(Path(str(value)).expanduser().resolve()) if value else ""


def review_status(task: dict) -> str:
    if task.get("status") == "completed":
        return str(task.get("review_status") or "unreviewed")
    return str(task.get("review_status") or "")


def needs_review(task: dict) -> bool:
    return task.get("status") == "completed" and review_status(task) in {"unreviewed", "rejected", "needs_followup"}


def visible_by_default(task: dict) -> bool:
    if task.get("status") == "archived":
        return False
    if task.get("status") == "completed":
        return needs_review(task)
    if task.get("status") in {"failed", "blocked_user"} and task.get("resolution"):
        return False
    return task.get("status") not in DEFAULT_HIDDEN_LIST_STATUSES


def cmd_run_next(config: Config, args: argparse.Namespace) -> int:
    outcome = run_next(config)
    if args.json:
        print(json.dumps(outcome.__dict__, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        task_part = f" task={outcome.task_id}" if outcome.task_id else ""
        print(f"{outcome.status}: {outcome.message}{task_part}")
    return 0


def cmd_show(config: Config, args: argparse.Namespace) -> int:
    task = load_task(config, args.task_id)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_summary(config: Config, args: argparse.Namespace) -> int:
    task = load_task(config, args.task_id)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    by_id = {item.get("id"): item for item in list_tasks(config)}
    print(
        render_task_summary(
            task,
            by_id=by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        ),
        end="",
    )
    return 0


def cmd_review_bundle(config: Config, args: argparse.Namespace) -> int:
    task = load_task(config, args.task_id)
    by_id = {item.get("id"): item for item in list_tasks(config)}
    bundle = build_review_bundle(
        task,
        by_id=by_id,
        require_accepted_review=config.dependency_requires_accepted_review,
    )
    if args.json:
        print(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(render_review_bundle(bundle), end="")
    return 0


def cmd_review_next(config: Config, args: argparse.Namespace) -> int:
    if args.mechanical_auto_accept and not args.apply:
        print("error: --mechanical-auto-accept requires --apply", file=sys.stderr)
        return 1
    if args.reviewer_codex and not args.apply:
        print("error: --reviewer-codex requires --apply", file=sys.stderr)
        return 1
    if args.apply:
        report = build_review_next_apply_report(
            config,
            args,
            mechanical_auto_accept=args.mechanical_auto_accept,
            reviewer_codex=args.reviewer_codex,
        )
        if report.get("mutated"):
            run_post_mutation_trigger(config)
    else:
        report = build_review_next_report(config, args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_review_next_report(report), end="")
    return 0


def cmd_routing_report(config: Config, args: argparse.Namespace) -> int:
    if args.limit < 0:
        print("error: --limit must be non-negative", file=sys.stderr)
        return 1
    report = build_routing_report(
        config,
        project_id=args.project_id,
        project_root=normalized_path(args.project_root) if args.project_root else None,
        category=args.category,
        label=args.label,
        limit=args.limit,
        include_archived=args.include_archived,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_routing_report(report), end="")
    return 0


def cmd_logs(config: Config, args: argparse.Namespace) -> int:
    task = load_task(config, args.task_id)
    log_paths = task.get("log_paths", [])
    if not args.cat:
        for path in log_paths:
            print(path)
        return 0
    for path_text in log_paths:
        path = Path(path_text)
        print(f"==> {path} <==")
        if path.exists():
            print(path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_transcript(config: Config, args: argparse.Namespace) -> int:
    task = load_task(config, args.task_id)
    print(render_task_transcript(task, raw=args.raw), end="")
    return 0


def cmd_follow(config: Config, args: argparse.Namespace) -> int:
    if args.lines < 0:
        print("error: --lines must be non-negative", file=sys.stderr)
        return 1
    if args.poll_interval < 0:
        print("error: --poll-interval must be non-negative", file=sys.stderr)
        return 1
    follow_task(
        config,
        FollowOptions(
            task_id=args.task_id,
            initial_lines=args.lines,
            poll_interval_seconds=args.poll_interval,
            max_polls=args.max_polls,
        ),
        sys.stdout,
    )
    return 0


def cmd_accept(config: Config, args: argparse.Namespace) -> int:
    task = set_review_status(config, args.task_id, "accepted", args.reason, require_completed=True)
    run_post_mutation_trigger(config)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_review_mutation(task, "accepted"), end="")
    return 0


def cmd_reject(config: Config, args: argparse.Namespace) -> int:
    status = "needs_followup" if args.follow_up else "rejected"
    task = set_review_status(config, args.task_id, status, args.reason)
    run_post_mutation_trigger(config)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_review_mutation(task, status), end="")
    return 0


def render_review_mutation(task: dict, status: str) -> str:
    lines = [f"{task.get('id')}\t{status}"]
    metadata = task_worktree_metadata(task)
    if metadata != {"execution_mode": "main_worktree"}:
        parts = [
            f"mode={metadata.get('execution_mode')}",
            f"branch={metadata.get('branch') or '-'}",
            f"status={metadata.get('worktree_status') or '-'}",
        ]
        if metadata.get("worktree_path"):
            parts.append(f"path={metadata.get('worktree_path')}")
        lines.append("worktree\t" + " ".join(parts))
    follow_up = task.get("review_follow_up") if isinstance(task.get("review_follow_up"), dict) else {}
    if follow_up and (follow_up.get("source_branch") or follow_up.get("source_execution_mode") == "git_worktree"):
        lines.append(
            "follow_up\t"
            + " ".join(
                [
                    f"source_task={follow_up.get('source_task_id') or '-'}",
                    f"source_branch={follow_up.get('source_branch') or '-'}",
                    f"task_generation={follow_up.get('task_generation') or '-'}",
                ]
            )
        )
    return "\n".join(lines) + "\n"


def cmd_archive(config: Config, args: argparse.Namespace) -> int:
    task = archive_task(config, args.task_id)
    run_post_mutation_trigger(config)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{task.get('id')}\tarchived")
    return 0


def cmd_resolve(config: Config, args: argparse.Namespace) -> int:
    task = set_resolution(config, args.task_id, args.resolution, args.reason)
    run_post_mutation_trigger(config)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{task.get('id')}\tresolved\t{task.get('resolution')}")
    return 0


def cmd_state(config: Config, args: argparse.Namespace) -> int:
    print(json.dumps(load_state(config), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_cooldown_show(config: Config, args: argparse.Namespace) -> int:
    print(render_cooldown_status(cooldown_status(load_state(config).get("global_cooldown_until"))), end="")
    return 0


def cmd_cooldown_clear(config: Config, args: argparse.Namespace) -> int:
    if args.reviewer_codex:
        previous = load_state(config).get("reviewer_codex_cooldown_until")
        clear_reviewer_codex_cooldown(config)
        write_event_nonfatal(
            config,
            "cooldown_updated",
            summary="reviewer Codex cooldown cleared",
            payload={"action": "clear_reviewer_codex", "previous_reviewer_codex_cooldown_until": previous},
        )
        run_post_mutation_trigger(config)
        print("reviewer Codex cooldown cleared")
        return 0

    previous = load_state(config).get("global_cooldown_until")
    clear_global_cooldown(config)
    write_event_nonfatal(
        config,
        "cooldown_updated",
        summary="global cooldown cleared",
        payload={"action": "clear", "previous_global_cooldown_until": previous},
    )
    run_post_mutation_trigger(config)
    print("global cooldown cleared")
    return 0


def cmd_cooldown_set(config: Config, args: argparse.Namespace) -> int:
    schedule = parse_manual_cooldown(args.value)
    set_global_cooldown(config, schedule.effective_cooldown_until.isoformat())
    wake_result = schedule_manual_cooldown_wake(config, schedule.effective_cooldown_until)
    write_event_nonfatal(
        config,
        "cooldown_updated",
        summary="global cooldown set",
        payload={
            "action": "set",
            "input_value": schedule.input_value,
            "interpreted_reset_at": schedule.interpreted_reset_at.isoformat(),
            "effective_cooldown_until": schedule.effective_cooldown_until.isoformat(),
            "safety_offset_seconds": MANUAL_COOLDOWN_SAFETY_OFFSET_SECONDS,
        },
    )
    print("global cooldown set")
    print(f"input: {schedule.input_value}")
    print(f"interpreted_reset_at: {schedule.interpreted_reset_at.isoformat()}")
    print(f"effective_cooldown_until: {schedule.effective_cooldown_until.isoformat()}")
    print(f"duration: {format_duration(schedule.duration_seconds)}")
    print(f"one_shot_wake: {wake_result.status} ({wake_result.message})")
    return 0


def render_cooldown_status(status: dict[str, object]) -> str:
    lines = [
        "global cooldown status",
        f"global_cooldown_until: {status.get('global_cooldown_until') or '-'}",
        f"active: {str(bool(status.get('active'))).lower()}",
        f"remaining: {status.get('remaining')}",
    ]
    return "\n".join(lines) + "\n"


def cmd_rate_limits(config: Config, args: argparse.Namespace) -> int:
    events = list_rate_limit_evidence(config)
    if args.json:
        print(json.dumps(events, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for event in events:
        markers = ",".join(event.get("matched_markers") or [])
        print(
            f"{event.get('detected_at')}\t{event.get('task_id')}\t"
            f"attempt={event.get('attempt')}\tcooldown_until={event.get('cooldown_until')}\tmarkers={markers}"
        )
    return 0


def cmd_events(config: Config, args: argparse.Namespace) -> int:
    events = list_events(config, task_id=args.task_id, limit=args.limit)
    if args.json:
        print(json.dumps(events, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_events_human(events), end="")
    return 0


def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    report = build_doctor_report(config)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_doctor_report(report), end="")
    return 0 if report["ok"] else 1


def cmd_prune(config: Config, args: argparse.Namespace) -> int:
    cursor_paths = [Path(path) for path in args.notifier_cursor_state]
    report = build_prune_report(config, age_days=args.older_than_days, apply=args.apply, notifier_cursor_state_paths=cursor_paths or None)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_prune_report(report), end="")
    return 0


def cmd_apply_plan(config: Config, args: argparse.Namespace) -> int:
    if args.apply:
        report = apply_queue_mutation_plan(config, args.plan_path)
    else:
        report = build_apply_plan_report(config, args.plan_path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_apply_plan_report(report), end="")
    return 0 if report["ok"] else 1


def cmd_worktree_prepare(config: Config, args: argparse.Namespace) -> int:
    report = build_prepare_report(config, args.task_id, apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_worktree_report(report), end="")
    return 1 if report.get("errors") else 0


def cmd_worktree_cleanup(config: Config, args: argparse.Namespace) -> int:
    report = build_cleanup_report(config, args.task_id, apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_worktree_report(report), end="")
    return 1 if report.get("errors") else 0


def cmd_worktree_apply(config: Config, args: argparse.Namespace) -> int:
    report = build_apply_report(config, args.task_id, apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_worktree_report(report), end="")
    return 1 if report.get("errors") else 0


def render_prune_report(report: dict) -> str:
    lines = [
        f"mode: {report['mode']}",
        f"older_than_days: {report['age_days']}",
        f"candidates: {report['candidate_count']}",
        f"task_candidates: {report.get('task_candidate_count', len(report['candidates']))}",
        f"event_candidates: {report.get('event_candidate_count', len(report.get('event_candidates', [])))}",
        f"deleted_files: {report['deleted_files']}",
    ]
    for warning in report.get("warnings", []):
        lines.append(f"warning: {warning}")
    if report["candidates"]:
        lines.append("task/log candidates:")
    for candidate in report["candidates"]:
        lines.append(f"{candidate['task_id']}\t{candidate['reason']}\t{candidate['timestamp']}")
        for file in candidate["files"]:
            flags = []
            if file["deleted"]:
                flags.append("deleted")
            elif file.get("skipped"):
                flags.append(f"skipped={file['reason']}")
            elif not file["exists"]:
                flags.append("missing")
            elif report["dry_run"]:
                flags.append("would-delete")
            if not file["safe"]:
                flags.append(f"blocked={file['reason']}")
            flag_text = ",".join(flags) if flags else "-"
            lines.append(f"  {file['kind']}\t{flag_text}\t{file['path']}")
    if report.get("event_candidates"):
        lines.append("event candidates:")
    for file in report.get("event_candidates", []):
        flags = []
        if file["deleted"]:
            flags.append("deleted")
        elif file.get("skipped"):
            flags.append(f"skipped={file['reason']}")
        elif not file["exists"]:
            flags.append("missing")
        elif report["dry_run"]:
            flags.append("would-delete")
        if not file["safe"]:
            flags.append(f"blocked={file['reason']}")
        flag_text = ",".join(flags) if flags else "-"
        lines.append(f"  {file['kind']}\t{flag_text}\t{file['path']}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())

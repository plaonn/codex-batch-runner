from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import shlex
import sys
import time
import unicodedata
import zlib
from datetime import timedelta
from pathlib import Path

from .apply_plan import apply_queue_mutation_plan, build_apply_plan_report, render_apply_plan_report
from .config import Config
from .cooldown import MANUAL_COOLDOWN_SAFETY_OFFSET_SECONDS, cooldown_status, format_duration, parse_manual_cooldown
from .doctor import build_doctor_report, render_doctor_report
from .direct_worktrees import build_direct_worktrees_report, render_direct_worktrees_report
from .events import DEFAULT_EVENT_LIMIT, list_events, render_events_human, write_event_nonfatal
from .model_requirements import REQUIREMENT_DIMENSIONS, REQUIREMENT_LEVELS, model_requirement_vector_value
from .evidence import list_rate_limit_evidence
from .follow import DEFAULT_INITIAL_LINES, DEFAULT_POLL_INTERVAL_SECONDS, FollowOptions, follow_task
from .index import build_rebuild_report, build_status_report, render_rebuild_report, render_status_report
from .lock import FileLock
from .maintenance import (
    build_codex_cli_maintenance_report,
    dump_json,
    render_codex_cli_maintenance_report,
    run_codex_cli_maintenance,
)
from .prune import DEFAULT_PRUNE_AGE_DAYS, build_prune_report
from .post_accept import accept_task_and_integrate
from .presentation import (
    TaskListPresentation,
    task_list_presentation,
    task_list_status,
    task_list_status_without_subtasks,
)
from .queue import (
    DEFAULT_HIDDEN_LIST_STATUSES,
    RESOLUTIONS,
    RUNNABLE_STATUSES,
    TASK_PRIORITIES,
    ROUTING_RISKS,
    ROUTING_SIZES,
    VERIFICATION_SCOPES,
    archive_task,
    capacity_blockers,
    create_task,
    dependency_status,
    is_in_cooldown,
    list_tasks,
    load_task,
    set_resolution,
    set_review_status,
    task_labels,
    task_priority,
    task_capacity_pool,
    task_project_id,
    task_project_root,
    task_title,
)
from .review_bundle import build_review_bundle, render_review_bundle
from .review_followup import REVIEW_FOLLOWUP_FOR_FIELD, review_follow_up_note
from .review_next import build_review_next_apply_report, build_review_next_report, render_review_next_report
from .routing_evaluation_report import (
    DEFAULT_ROUTING_EVAL_REPORT_LIMIT,
    build_routing_evaluation_report,
    render_routing_evaluation_report,
)
from .routing_report import DEFAULT_ROUTING_REPORT_LIMIT, build_routing_report, render_routing_report
from .runner import RunOutcome, run_next
from .state import (
    clear_global_cooldown,
    clear_reviewer_codex_cooldown,
    clear_runner_pause,
    get_runner_pause,
    load_state,
    set_global_cooldown,
    set_runner_pause,
)
from .summary import render_task_summary
from .timeutil import parse_time, utc_now
from .transcript import render_task_transcript
from .triggers import run_post_mutation_trigger
from .wake import schedule_manual_cooldown_wake
from .worktree import (
    build_apply_report,
    build_branch_prune_report,
    build_cleanup_report,
    build_prepare_report,
    render_worktree_report,
    task_worktree_metadata,
)

WATCH_RESTART_MESSAGE = "cbr source changed since this watch started; restart watch to use updated code"
COMPACT_TABLE_BLOCK_LAYOUT_WIDTH = 80
COMPACT_TABLE_COMFORT_WIDTH = 93
COMPACT_TABLE_MIN_TITLE_WIDTH = 30
COMPACT_TABLE_MIN_DETAIL_WIDTH = 24
COMPACT_TABLE_FLOOR_TITLE_WIDTH = 12
COMPACT_TABLE_FLOOR_DETAIL_WIDTH = 10
COMPACT_TABLE_NARROW_STATUS_LABELS = {
    "accepted_unapplied": "accepted*",
    "awaiting_review": "review",
    "blocked_dependency": "dep_block",
    "blocked_user": "blocked",
    "needs_followup": "followup",
    "needs_resume": "resume",
    "review_needs_fix": "needs_fix",
    "review_pass_pending": "review_pass",
    "subtasks_blocked": "subs_block",
    "usage_exhausted": "usage_out",
    "waiting_subtasks": "wait_subs",
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
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
    enqueue.add_argument(
        "--title",
        help="concise list title, usually 4-8 words: action + object + short qualifier",
    )
    enqueue.add_argument("--description", help="optional human-readable task description")
    enqueue.add_argument("--model-requirement-json", help="model requirement vector JSON object")
    for dimension in REQUIREMENT_DIMENSIONS:
        enqueue.add_argument(
            "--" + dimension.replace("_", "-"),
            choices=sorted(REQUIREMENT_LEVELS),
            help=f"model requirement dimension: {dimension}",
        )
    enqueue.add_argument("--routing-reason", help="public-safe reason for the model requirement decision")
    enqueue.add_argument("--routing-risk-factor", action="append", default=[], help="public-safe routing risk factor, repeatable")
    enqueue.add_argument("--routing-experiment", help="routing experiment label such as baseline, downshift_probe, upshift_guard, or manual")
    enqueue.add_argument("--routing-size", choices=ROUTING_SIZES, help="public-safe pre-enqueue work size estimate")
    enqueue.add_argument("--routing-risk", choices=ROUTING_RISKS, help="public-safe pre-enqueue implementation risk estimate")
    enqueue.add_argument("--verification-scope", action="append", choices=VERIFICATION_SCOPES, default=[], help="public-safe verification scope tag, repeatable")
    enqueue.add_argument("--capacity-pool", default="codex", help="capacity pool for scheduler admission (default: codex)")
    enqueue.add_argument("--priority", choices=TASK_PRIORITIES, default="normal", help="task priority within a project (default: normal)")
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
    list_cmd.add_argument("--demo", action="store_true", help="render synthetic demo tasks without reading the queue")
    list_cmd.add_argument(
        "--graph",
        action="store_true",
        dest="graph",
        help="print a human dependency edge list instead of the compact task list",
    )
    list_cmd.add_argument("--json", action="store_true", help="print JSON")
    list_cmd.add_argument("--watch", action="store_true", help="refresh the human list until interrupted")
    list_cmd.add_argument("--interval", type=float, default=2.0, help="seconds between --watch refreshes (default: 2.0)")
    list_cmd.add_argument("--max-refreshes", type=int, help=argparse.SUPPRESS)
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

    run_loop = sub.add_parser("run-loop", help="run eligible work until the queue is not immediately actionable")
    run_loop.add_argument("--json", action="store_true", help="print one JSON object per iteration as JSONL")
    run_loop.add_argument(
        "--max-iterations",
        type=int,
        default=100,
        help="safety fuse for repeated run-next iterations; set higher for large queues (default: 100)",
    )
    run_loop.set_defaults(func=cmd_run_loop)

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

    routing_report = sub.add_parser("routing-report", help="summarize model requirement outcomes without mutating tasks")
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

    routing_eval_report = sub.add_parser(
        "routing-eval-report",
        help="show bounded derived routing evaluation rows without mutating tasks",
    )
    routing_eval_report.add_argument("--project", dest="project_id", help="filter by project id")
    routing_eval_report.add_argument("--project-root", help="filter by project root")
    routing_eval_report.add_argument("--category", help="filter by category")
    routing_eval_report.add_argument("--label", help="filter by label")
    routing_eval_report.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_ROUTING_EVAL_REPORT_LIMIT,
        help=f"maximum recent tasks to include after filtering; 0 means no limit (default: {DEFAULT_ROUTING_EVAL_REPORT_LIMIT})",
    )
    routing_eval_report.add_argument("--include-archived", action="store_true", help="include archived tasks")
    routing_eval_report.add_argument("--json", action="store_true", help="print JSON")
    routing_eval_report.set_defaults(func=cmd_routing_eval_report)

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

    resolve = sub.add_parser(
        "resolve",
        help="record an operational resolution for failed, blocked, or completed rejected/follow-up tasks",
    )
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

    pause = sub.add_parser("pause", help="show, set, or clear global queue admission pause")
    pause_sub = pause.add_subparsers(dest="pause_command", required=True)
    pause_show = pause_sub.add_parser("show", help="show runner pause status")
    pause_show.set_defaults(func=cmd_pause_show)
    pause_set = pause_sub.add_parser("set", help="set runner pause without expiry")
    pause_set.add_argument("--reason", required=True, help="public-safe reason for pausing queue admission")
    pause_set.add_argument("--by", help="optional public-safe operator identifier")
    pause_set.set_defaults(func=cmd_pause_set)
    pause_clear = pause_sub.add_parser("clear", help="clear runner pause and wake configured scheduler hooks")
    pause_clear.set_defaults(func=cmd_pause_clear)

    rate_limits = sub.add_parser("rate-limits", help="list sanitized rate-limit evidence")
    rate_limits.add_argument("--json", action="store_true", help="print JSON")
    rate_limits.set_defaults(func=cmd_rate_limits)

    events = sub.add_parser("events", help="list recent sanitized event log entries")
    events.add_argument("--task-id", help="filter by task id")
    events.add_argument("--limit", type=int, default=DEFAULT_EVENT_LIMIT, help=f"maximum events to show (default: {DEFAULT_EVENT_LIMIT})")
    events.add_argument("--json", action="store_true", help="print JSON")
    events.set_defaults(func=cmd_events)

    index = sub.add_parser("index", help="inspect or rebuild the local SQLite read index")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    index_rebuild = index_sub.add_parser("rebuild", help="plan or rebuild the derived SQLite read index")
    index_rebuild_mode = index_rebuild.add_mutually_exclusive_group(required=True)
    index_rebuild_mode.add_argument("--dry-run", action="store_true", help="report rebuild counts without writing SQLite")
    index_rebuild_mode.add_argument("--apply", action="store_true", help="rebuild SQLite from retained task and event files")
    index_rebuild.add_argument("--json", action="store_true", help="print JSON")
    index_rebuild.set_defaults(func=cmd_index_rebuild)
    index_status = index_sub.add_parser("status", help="show local SQLite read index status")
    index_status.add_argument("--json", action="store_true", help="print JSON")
    index_status.set_defaults(func=cmd_index_status)

    doctor = sub.add_parser("doctor", help="check local cbr health and Codex CLI version without running Codex exec")
    doctor.add_argument("--json", action="store_true", help="print JSON")
    doctor.set_defaults(func=cmd_doctor)

    maintenance = sub.add_parser("maintenance", help="run guarded local maintenance workflows")
    maintenance_sub = maintenance.add_subparsers(dest="maintenance_command", required=True)
    codex_cli = maintenance_sub.add_parser("codex-cli", help="run configured Codex CLI update and smoke commands")
    codex_cli_mode = codex_cli.add_mutually_exclusive_group()
    codex_cli_mode.add_argument("--dry-run", action="store_true", help="report readiness; this is the default")
    codex_cli_mode.add_argument("--apply", action="store_true", help="pause admissions, run update and smoke, then clear pause")
    codex_cli.add_argument("--json", action="store_true", help="print JSON")
    codex_cli.set_defaults(func=cmd_maintenance_codex_cli)
    direct_worktrees = maintenance_sub.add_parser("direct-worktrees", help="audit or cleanup eligible direct operator worktrees")
    direct_worktrees_mode = direct_worktrees.add_mutually_exclusive_group()
    direct_worktrees_mode.add_argument("--dry-run", action="store_true", help="report eligible and blocked direct worktrees; this is the default")
    direct_worktrees_mode.add_argument("--apply", action="store_true", help="remove eligible direct worktrees and delete merged local branches")
    direct_worktrees.add_argument(
        "--repo-root",
        type=Path,
        help="target git repository to inspect; defaults to the current working directory's git root",
    )
    direct_worktrees.add_argument("--json", action="store_true", help="print JSON")
    direct_worktrees.set_defaults(func=cmd_maintenance_direct_worktrees)

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

    worktree_branch_prune = worktree_sub.add_parser("branch-prune", help="delete an eligible cleaned local task branch")
    worktree_branch_prune.add_argument("task_id")
    branch_prune_mode = worktree_branch_prune.add_mutually_exclusive_group(required=True)
    branch_prune_mode.add_argument("--dry-run", action="store_true", help="report branch prune eligibility")
    branch_prune_mode.add_argument("--apply", action="store_true", help="delete the local task branch with git branch -d under the queue lock")
    worktree_branch_prune.add_argument("--json", action="store_true", help="print JSON")
    worktree_branch_prune.set_defaults(func=cmd_worktree_branch_prune)

    worktree_apply = worktree_sub.add_parser("apply", help="fast-forward an accepted task worktree branch into the main worktree")
    worktree_apply.add_argument("task_id")
    apply_mode = worktree_apply.add_mutually_exclusive_group(required=True)
    apply_mode.add_argument("--dry-run", action="store_true", help="report planned fast-forward apply without changing git or task state")
    apply_mode.add_argument("--apply", action="store_true", help="fast-forward merge the accepted task branch under the queue lock")
    worktree_apply.add_argument("--json", action="store_true", help="print JSON")
    worktree_apply.set_defaults(func=cmd_worktree_apply)
    return parser


def cmd_enqueue(config: Config, args: argparse.Namespace) -> int:
    pause = get_runner_pause(config)
    if pause.get("active"):
        reason = str(pause.get("reason") or "no reason recorded")
        raise RuntimeError(f"cbr is currently unavailable: runner pause is active: {reason}")
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
        model_requirement_vector=parse_model_requirement_args(args),
        routing_reason=args.routing_reason,
        routing_risk_factors=args.routing_risk_factor,
        routing_experiment=args.routing_experiment,
        routing_size=args.routing_size,
        routing_risk=args.routing_risk,
        verification_scope=args.verification_scope,
        execution_backend=args.backend,
        shell_command=shell_command,
        shell_timeout_seconds=args.shell_timeout_seconds,
        capacity_pool=args.capacity_pool,
        task_priority=args.priority,
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


def parse_model_requirement_args(args: argparse.Namespace) -> dict | None:
    explicit = {}
    if args.model_requirement_json:
        try:
            explicit = json.loads(args.model_requirement_json)
        except json.JSONDecodeError as exc:
            raise ValueError("--model-requirement-json must be a JSON object") from exc
        if not isinstance(explicit, dict):
            raise ValueError("--model-requirement-json must be a JSON object")
    raw_dimensions = explicit.get("dimensions", {})
    if raw_dimensions in (None, ""):
        raw_dimensions = {}
    if not isinstance(raw_dimensions, dict):
        raise ValueError("--model-requirement-json dimensions must be an object")
    dimensions = dict(raw_dimensions)
    for dimension in REQUIREMENT_DIMENSIONS:
        value = getattr(args, dimension)
        if value:
            dimensions[dimension] = value
    if not dimensions and not explicit:
        return None
    explicit["dimensions"] = dimensions
    explicit.setdefault("source", "explicit_cli")
    explicit.setdefault("confidence", "high")
    return model_requirement_vector_value("model_requirement_vector", explicit)


def cmd_list(config: Config, args: argparse.Namespace) -> int:
    if args.watch:
        return cmd_list_watch(config, args)
    output = render_list_output(config, args, terminal_width=compact_terminal_width())
    print(output)
    return 0


def cmd_list_watch(config: Config, args: argparse.Namespace) -> int:
    if args.json:
        raise ValueError("--watch cannot be used with --json")
    if args.interval <= 0:
        raise ValueError("--interval must be positive")
    interval = args.interval
    max_refreshes = args.max_refreshes
    refresh_count = 0
    source_signature = watch_source_signature()
    old_terminal = None
    if sys.stdin.isatty():
        try:
            import termios
            import tty

            old_terminal = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            old_terminal = None
    try:
        while True:
            sys.stdout.write("\033[H\033[J")
            try:
                output = render_watch_output(
                    config,
                    args,
                    interval,
                    source_changed=watch_source_changed(source_signature),
                )
            except Exception as exc:
                output = render_watch_error_output(
                    interval,
                    exc,
                    source_changed=watch_source_changed(source_signature),
                )
            sys.stdout.write(output)
            sys.stdout.write("\n")
            sys.stdout.flush()
            refresh_count += 1
            if max_refreshes is not None and refresh_count >= max_refreshes:
                return 0
            action, interval = wait_for_watch_action(interval)
            if action == "quit":
                return 0
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 0
    finally:
        if old_terminal is not None:
            try:
                import termios

                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_terminal)
            except Exception:
                pass


def render_watch_output(config: Config, args: argparse.Namespace, interval: float, *, source_changed: bool = False) -> str:
    header = f"updated: {utc_now().isoformat()}  interval={interval:g}s  keys: q quit, r refresh, +/- interval"
    body = render_list_output(config, args, terminal_width=compact_terminal_width())
    return "\n".join([*watch_header_lines(header, source_changed), body])


def render_watch_error_output(interval: float, exc: Exception, *, source_changed: bool = False) -> str:
    header = f"updated: {utc_now().isoformat()}  interval={interval:g}s  keys: q quit, r refresh, +/- interval"
    error = f"refresh error: {type(exc).__name__}: {exc}"
    return "\n".join([*watch_header_lines(header, source_changed), error])


def watch_header_lines(header: str, source_changed: bool) -> list[str]:
    lines = [header]
    if source_changed:
        lines.append(WATCH_RESTART_MESSAGE)
    return lines


def watch_source_changed(initial_signature: tuple[int, int, int] | None) -> bool:
    if initial_signature is None:
        return False
    try:
        current_signature = watch_source_signature()
    except Exception:
        return False
    return current_signature is not None and current_signature != initial_signature


def watch_source_signature() -> tuple[int, int, int] | None:
    try:
        paths = list(Path(__file__).resolve().parent.rglob("*.py"))
    except OSError:
        return None
    count = 0
    latest_mtime_ns = 0
    total_mtime_ns = 0
    for path in paths:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        count += 1
        latest_mtime_ns = max(latest_mtime_ns, mtime_ns)
        total_mtime_ns += mtime_ns
    return count, latest_mtime_ns, total_mtime_ns


def wait_for_watch_action(interval: float) -> tuple[str, float]:
    if not sys.stdin.isatty():
        time.sleep(interval)
        return "refresh", interval
    deadline = time.monotonic() + interval
    current_interval = interval
    while True:
        timeout = max(0.0, deadline - time.monotonic())
        if timeout == 0:
            return "refresh", current_interval
        readable, _, _ = select.select([sys.stdin], [], [], timeout)
        if not readable:
            return "refresh", current_interval
        key = sys.stdin.read(1)
        if key == "q":
            return "quit", current_interval
        if key == "r":
            return "refresh", current_interval
        if key in {"+", "="}:
            current_interval = min(60.0, current_interval + 1.0)
            deadline = time.monotonic() + current_interval
        elif key == "-":
            current_interval = max(0.5, current_interval - 1.0)
            deadline = time.monotonic() + current_interval


def render_list_output(config: Config, args: argparse.Namespace, terminal_width: int | None = None) -> str:
    all_tasks = demo_list_tasks() if args.demo else list_tasks(config)
    tasks = list(all_tasks)
    by_id = {task.get("id"): task for task in all_tasks}
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
    if not args.all:
        if explicit_filter:
            if not args.json:
                tasks = visible_tasks_with_subtasks(tasks, all_tasks, by_id)
        else:
            tasks = default_visible_tasks_with_subtasks(tasks, by_id)
    tasks.sort(key=list_sort_key)
    if args.json:
        return json.dumps(tasks, ensure_ascii=False, indent=2, sort_keys=True)
    color = list_colorizer(args.color)
    banners = [] if args.demo else list_cooldown_banners(config)
    if args.graph:
        output = render_dependency_graph(
            tasks,
            by_id,
            config,
            color,
            terminal_width=terminal_width,
            include_capacity=not args.demo,
        )
        return "\n".join([*banners, output]) if banners else output
    if not args.verbose:
        output = render_compact_list(tasks, by_id, config, color, terminal_width=terminal_width, include_capacity=not args.demo)
        return "\n".join([*banners, output]) if banners else output
    header = ["ID", "TITLE", "STATUS", "PROJECT", "ATTEMPTS", "DEPS", "NOTE"]
    header.append("MODEL")
    header.extend(["RAW_STATUS", "LAST_RESULT", "LAST_RUN", "LAST_ERROR"])
    rows = []
    for task in tasks:
        row = list_table_row(task, by_id, config, include_capacity=not args.demo)
        row.extend(verbose_table_cells(task))
        rows.append(row)
    output = render_table(header, rows)
    return "\n".join([*banners, output]) if banners else output


def compact_terminal_width() -> int | None:
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size((120, 24)).columns


def list_cooldown_banners(config: Config) -> list[str]:
    state = load_state(config)
    banners = []
    global_until = parse_time(state.get("global_cooldown_until"))
    reviewer_until = parse_time(state.get("reviewer_codex_cooldown_until"))
    now = utc_now()
    if global_until and global_until > now:
        banners.append("global cooldown active until " + global_until.isoformat())
    if reviewer_until and reviewer_until > now:
        banners.append("reviewer Codex cooldown active until " + reviewer_until.isoformat())
    return banners


def list_sort_key(task: dict) -> tuple[str, str]:
    return (str(task.get("created_at") or ""), str(task.get("id") or ""))


def demo_list_tasks() -> list[dict]:
    now = utc_now()

    def ago(seconds: int) -> str:
        return (now - timedelta(seconds=seconds)).isoformat()

    def task(
        task_id: str,
        title: str,
        *,
        status: str = "runnable",
        attempts: int = 0,
        depends_on: list[str] | None = None,
        created_ago: int = 0,
        **extra: object,
    ) -> dict:
        return {
            "id": task_id,
            "title": title,
            "description": None,
            "prompt": "Synthetic demo task. This task is not stored in the queue.",
            "cwd": "/demo/repo",
            "project_id": "demo",
            "category": "demo",
            "labels": ["demo"],
            "status": status,
            "attempts": attempts,
            "depends_on": depends_on or [],
            "created_at": ago(created_ago),
            "updated_at": ago(max(0, created_ago - 60)),
            "demo": True,
            **extra,
        }

    low_requirement = {
        "schema_version": 1,
        "source": "demo",
        "confidence": "medium",
        "dimensions": {
            "reasoning_depth": "low",
            "context_need": "low",
            "tool_reliability": "medium",
            "latency_priority": "high",
            "cost_sensitivity": "high",
            "review_strictness": "medium",
        },
    }
    high_requirement = {
        "schema_version": 1,
        "source": "demo",
        "confidence": "medium",
        "dimensions": {
            "reasoning_depth": "high",
            "context_need": "high",
            "tool_reliability": "high",
            "latency_priority": "medium",
            "cost_sensitivity": "low",
            "review_strictness": "medium",
        },
    }

    return [
        task("demo-ready", "Prepare parser cleanup notes", created_ago=900, model_requirement_vector=low_requirement),
        task(
            "demo-build-parser",
            "Build shared parser",
            status="completed",
            created_ago=840,
            completed_at=ago(420),
            review_status="accepted",
            last_run={"duration_seconds": 180},
        ),
        task(
            "demo-parser-tests",
            "Add parser tests",
            status="completed",
            created_ago=810,
            completed_at=ago(390),
            review_status="accepted",
            last_run={"duration_seconds": 120},
        ),
        task(
            "demo-wire-cli",
            "Wire parser into CLI",
            depends_on=["demo-build-parser", "demo-parser-tests", "demo-missing-parser"],
            created_ago=780,
        ),
        task(
            "demo-parent",
            "Release CLI parser change",
            status="completed",
            created_ago=760,
            completed_at=ago(340),
            review_status="unreviewed",
            blocking_subtask_ids=["demo-subtask"],
        ),
        task(
            "demo-subtask",
            "Fix review comments for parser change",
            created_ago=750,
            parent_task_id="demo-parent",
            subtask_for="demo-parent",
            subtask_type="auto_review_fix",
        ),
        task(
            "demo-done",
            "Shared parser implementation complete",
            status="completed",
            created_ago=740,
            completed_at=ago(320),
            review_status="accepted",
            last_run={"duration_seconds": 180},
        ),
        task(
            "demo-review",
            "CLI docs draft awaiting review",
            status="completed",
            attempts=1,
            created_ago=720,
            completed_at=ago(300),
            review_status="unreviewed",
            last_result={"status": "completed", "summary": "demo result"},
            last_run={"command_kind": "exec", "returncode": 0, "duration_seconds": 240},
        ),
        task(
            "demo-worktree-review",
            "Release checklist review pending",
            status="completed",
            attempts=1,
            created_ago=700,
            completed_at=ago(260),
            review_status="unreviewed",
            execution_mode="git_worktree",
            execution_worktree_status="retained",
            execution_worktree_branch="codex/demo-worktree-review",
        ),
        task(
            "demo-worktree",
            "Release checklist merge ready, not applied",
            status="completed",
            attempts=2,
            created_ago=660,
            completed_at=ago(180),
            review_status="accepted",
            execution_mode="git_worktree",
            execution_apply_status="pending",
            execution_worktree_status="retained",
            execution_worktree_branch="codex/demo-worktree",
            last_run={"command_kind": "exec", "returncode": 0, "duration_seconds": 360},
        ),
        task(
            "demo-blocked",
            "Publish CLI parser release notes",
            depends_on=[
                "demo-parent",
                "demo-done",
                "demo-review",
                "demo-worktree-review",
                "demo-worktree",
                "demo-missing",
            ],
            created_ago=600,
        ),
        task(
            "demo-running",
            "Run full regression suite",
            status="running",
            attempts=1,
            created_ago=480,
            started_at=ago(125),
            last_progress={"last_jsonl_event_at": ago(35), "jsonl_event_count": 14},
            model_requirement_vector=high_requirement,
        ),
    ]


def default_visible_tasks_with_subtasks(tasks: list[dict], by_id: dict[str, dict]) -> list[dict]:
    visible_ids = {str(task.get("id")) for task in tasks if task.get("id") and visible_by_default(task)}
    return tasks_matching_visible_ids_with_subtasks(tasks, visible_ids, by_id)


def visible_tasks_with_subtasks(
    visible_tasks: list[dict],
    candidate_tasks: list[dict],
    by_id: dict[str, dict],
) -> list[dict]:
    visible_ids = {str(task.get("id")) for task in visible_tasks if task.get("id")}
    return tasks_matching_visible_ids_with_subtasks(candidate_tasks, visible_ids, by_id)


def tasks_matching_visible_ids_with_subtasks(
    candidate_tasks: list[dict],
    visible_ids: set[str],
    by_id: dict[str, dict],
) -> list[dict]:
    changed = True
    while changed:
        changed = False
        for task in candidate_tasks:
            task_id = str(task.get("id") or "")
            if not task_id or task_id in visible_ids or task.get("status") == "archived" or task.get("resolution"):
                continue
            if has_visible_parent(task, visible_ids, by_id):
                visible_ids.add(task_id)
                changed = True
    return [task for task in candidate_tasks if str(task.get("id") or "") in visible_ids]


def has_visible_parent(task: dict, visible_ids: set[str], by_id: dict[str, dict]) -> bool:
    task_id = str(task.get("id") or "")
    for key in ("parent_task_id", "subtask_for", "root_task_id"):
        value = task.get(key)
        if value is not None and str(value) in visible_ids:
            return True
    if not task_id:
        return False
    for parent_id in visible_ids:
        parent = by_id.get(parent_id)
        if parent and task_id in blocking_subtask_ids(parent):
            return True
    return False


def render_table(header: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(row[index]) for row in [header, *rows])
        for index in range(len(header))
    ]
    return "\n".join(render_table_row(row, widths) for row in [header, *rows])


def render_table_row(row: list[str], widths: list[int]) -> str:
    padded = [cell.ljust(widths[index]) for index, cell in enumerate(row[:-1])]
    return "  ".join([*padded, row[-1]])


def render_dependency_graph(
    tasks: list[dict],
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
    terminal_width: int | None = None,
    include_capacity: bool = True,
) -> str:
    if not tasks:
        return "(no tasks)"
    grouped: dict[str, list[dict]] = {}
    project_order: list[str] = []
    for task in tasks:
        project = task_project_id(task)
        if project not in grouped:
            grouped[project] = []
            project_order.append(project)
        grouped[project].append(task)
    render_width = graph_render_width(terminal_width)
    lines: list[str] = []
    for project in project_order:
        if lines:
            lines.append("")
        lines.append(project_section_header(project, render_width, color))
        project_tasks = dependency_graph_tasks_with_sources(grouped[project], by_id, project)
        source_nodes, child_items = dependency_graph_source_nodes(project_tasks, by_id)
        source_id_to_index = {
            str(task.get("id")): index for index, task in enumerate(source_nodes) if task.get("id")
        }
        dependency_runs = dependency_graph_runs(source_nodes, source_id_to_index)
        layout = dependency_graph_layout(source_nodes, dependency_runs)
        for index, task in enumerate(source_nodes):
            if index in layout["transition_prefixes"]:
                connector = layout["transition_prefixes"][index]
                if connector:
                    lines.append(format_dependency_graph_prefix(connector, layout["transition_glyph_metadata"][index], color))
            lines.extend(
                render_dependency_graph_source_node(
                    task,
                    by_id,
                    config,
                    color,
                    layout["source_prefixes"][index],
                    graph_glyph_metadata=layout["source_glyph_metadata"][index],
                    graph_continuation_prefix=layout["source_continuation_prefixes"][index],
                    graph_continuation_glyph_metadata=layout["source_continuation_glyph_metadata"][index],
                    tree_gap_prefix=graph_tree_parent_continuation_prefix()
                    if child_items.get(str(task.get("id") or ""))
                    else None,
                    terminal_width=render_width,
                )
            )
            for child in child_items.get(str(task.get("id") or ""), []):
                child_task = child["task"] if isinstance(child.get("task"), dict) else {}
                child_prefix = str(child.get("prefix") or "")
                child_relationship = str(child.get("relationship") or "")
                lines.extend(
                    render_dependency_graph_child_row(
                        child_task,
                        by_id,
                        config,
                        color,
                        child_prefix,
                        graph_prefix=layout["child_graph_prefixes"][index],
                        graph_glyph_metadata=layout["child_glyph_metadata"][index],
                        dim_child=child_relationship != "review_followup",
                        terminal_width=render_width,
                    )
                )
    return "\n".join(lines)


def graph_render_width(terminal_width: int | None) -> int | None:
    if terminal_width is None:
        return None
    return max(1, terminal_width - 1)


def dependency_graph_tasks_with_sources(tasks: list[dict], by_id: dict[str, dict], project: str) -> list[dict]:
    task_by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
    changed = True
    while changed:
        changed = False
        for task in list(task_by_id.values()):
            depends_on = task.get("depends_on")
            if not isinstance(depends_on, list):
                continue
            for dep_id_value in depends_on:
                dep_id = str(dep_id_value)
                dep = by_id.get(dep_id)
                if dep is None or dep_id in task_by_id or task_project_id(dep) != project:
                    continue
                task_by_id[dep_id] = dep
                changed = True
    return list(task_by_id.values())


def dependency_graph_source_nodes(tasks: list[dict], by_id: dict[str, dict]) -> tuple[list[dict], dict[str, list[dict[str, object]]]]:
    visible_ids = {str(task.get("id")) for task in tasks if task.get("id")}
    source: list[dict] = []
    children: dict[str, list[dict]] = {task_id: [] for task_id in visible_ids}
    for task in tasks:
        task_id = str(task.get("id") or "")
        parent_id, relationship = compact_parent_relationship(task, visible_ids, by_id)
        if parent_id and parent_id != task_id:
            children.setdefault(parent_id, []).append({"task": task, "relationship": relationship})
        else:
            source.append(task)
    source.sort(key=list_sort_key)
    for task_id in children:
        children[task_id].sort(key=lambda item: list_sort_key(item["task"] if isinstance(item.get("task"), dict) else {}))
    source_by_id = {str(task.get("id")): task for task in source if task.get("id")}
    ordered: list[dict] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(task: dict) -> None:
        task_id = str(task.get("id") or "")
        if not task_id:
            ordered.append(task)
            return
        if task_id in seen:
            return
        if task_id in visiting:
            ordered.append(task)
            seen.add(task_id)
            return
        visiting.add(task_id)
        depends_on = task.get("depends_on")
        if isinstance(depends_on, list):
            for dep_id in depends_on:
                dep = source_by_id.get(str(dep_id))
                if dep is not None:
                    visit(dep)
        visiting.remove(task_id)
        if task_id not in seen:
            ordered.append(task)
            seen.add(task_id)

    for task in source:
        visit(task)

    child_items: dict[str, list[dict[str, object]]] = {}

    def visit_child(
        parent_id: str,
        task: dict,
        relationship: str,
        ancestors: list[bool],
        seen_children: set[str],
    ) -> None:
        task_id = str(task.get("id") or "")
        child_items.setdefault(parent_id, []).append(
            {"task": task, "prefix": tree_prefix(ancestors), "relationship": relationship}
        )
        if not task_id or task_id in seen_children:
            return
        child_tasks = children.get(task_id, [])
        for index, child in enumerate(child_tasks):
            child_task = child["task"] if isinstance(child.get("task"), dict) else {}
            visit_child(
                parent_id,
                child_task,
                str(child.get("relationship") or ""),
                [*ancestors, index == len(child_tasks) - 1],
                {*seen_children, task_id},
            )

    for task in ordered:
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        task_children = children.get(task_id, [])
        for index, child in enumerate(task_children):
            child_task = child["task"] if isinstance(child.get("task"), dict) else {}
            visit_child(
                task_id,
                child_task,
                str(child.get("relationship") or ""),
                [index == len(task_children) - 1],
                {task_id},
            )
    return ordered, child_items


def dependency_graph_runs(source_nodes: list[dict], source_id_to_index: dict[str, int]) -> dict[int, dict[str, object]]:
    dependency_runs: dict[int, dict[str, object]] = {}
    for target_index, task in enumerate(source_nodes):
        depends_on = task.get("depends_on")
        if not isinstance(depends_on, list):
            continue
        dep_indices = sorted(
            {
                source_id_to_index[str(dep_id)]
                for dep_id in depends_on
                if str(dep_id) in source_id_to_index and source_id_to_index[str(dep_id)] < target_index
            }
        )
        if not dep_indices:
            continue
        dependency_runs[target_index] = {
            "dep_indices": dep_indices,
        }
    return dependency_runs


def dependency_graph_layout(source_nodes: list[dict], dependency_runs: dict[int, dict[str, object]]) -> dict[str, object]:
    width = 8
    source_count = len(source_nodes)
    node_color_indices = dependency_graph_node_color_indices(source_nodes, dependency_runs)
    prefixes = [("*" + (" " * (width - 1))) for _ in range(source_count)]
    source_glyph_metadata = [
        graph_prefix_glyph_metadata(prefixes[index], node_color_indices[index]) for index in range(source_count)
    ]
    source_continuation_prefixes = [(" " * width) for _ in range(source_count)]
    source_continuation_glyph_metadata = [
        graph_prefix_glyph_metadata(source_continuation_prefixes[index]) for index in range(source_count)
    ]
    transition_prefixes: dict[int, str] = {}
    transition_glyph_metadata: dict[int, list[int | None]] = {}
    child_graph_prefixes = [(" " * width) for _ in range(source_count)]
    child_glyph_metadata = [graph_prefix_glyph_metadata(child_graph_prefixes[index]) for index in range(source_count)]
    for target_index, dependency_run in dependency_runs.items():
        dep_indices = dependency_run.get("dep_indices")
        if not isinstance(dep_indices, list):
            continue
        dep_indices = [
            dep_index
            for dep_index in dep_indices
            if isinstance(dep_index, int) and 0 <= dep_index < source_count and dep_index < target_index
        ]
        if not dep_indices:
            continue
        first_dep_index = dep_indices[0]
        first_lane_color = node_color_indices[first_dep_index]
        prefixes[first_dep_index] = "*" + (" " * (width - 1))
        source_glyph_metadata[first_dep_index] = graph_prefix_glyph_metadata(
            prefixes[first_dep_index], node_color_indices[first_dep_index]
        )
        if len(dep_indices) == 1:
            for source_index in range(first_dep_index + 1, target_index):
                prefix = "| *"
                prefixes[source_index] = prefix + (" " * max(0, width - visible_len(prefix)))
                source_glyph_metadata[source_index] = graph_prefix_glyph_metadata(
                    prefixes[source_index],
                    None,
                    {0: first_lane_color, 2: node_color_indices[source_index]},
                )
            transition_prefix = "|"
            transition_prefixes[target_index] = transition_prefix
            transition_glyph_metadata[target_index] = graph_prefix_glyph_metadata(
                transition_prefix,
                first_lane_color,
            )
            target_prefix = "*"
            prefixes[target_index] = target_prefix + (" " * max(0, width - visible_len(target_prefix)))
            source_glyph_metadata[target_index] = graph_prefix_glyph_metadata(
                prefixes[target_index], node_color_indices[target_index]
            )
        else:
            for source_index in range(first_dep_index + 1, target_index):
                prefix = "| *"
                prefixes[source_index] = prefix + (" " * max(0, width - visible_len(prefix)))
                source_glyph_metadata[source_index] = graph_prefix_glyph_metadata(
                    prefixes[source_index],
                    None,
                    {0: first_lane_color, 2: node_color_indices[source_index]},
                )
            transition_prefix = " \\|"
            transition_prefixes[target_index] = transition_prefix
            transition_glyph_metadata[target_index] = graph_prefix_glyph_metadata(
                transition_prefix,
                None,
                {1: first_lane_color, 2: node_color_indices[dep_indices[-1]]},
            )
            target_prefix = "  *"
            prefixes[target_index] = target_prefix + (" " * max(0, width - visible_len(target_prefix)))
            source_glyph_metadata[target_index] = graph_prefix_glyph_metadata(
                target_prefix, node_color_indices[target_index]
            )
        for source_index in range(first_dep_index, target_index):
            source_continuation_prefixes[source_index] = graph_source_continuation_prefix(prefixes[source_index])
            source_continuation_glyph_metadata[source_index] = graph_source_continuation_glyph_metadata(
                source_continuation_prefixes[source_index],
                first_lane_color,
                node_color_indices[source_index],
            )
            child_graph_prefixes[source_index] = "|" + (" " * (width - 1))
            child_glyph_metadata[source_index] = graph_prefix_glyph_metadata(
                child_graph_prefixes[source_index], first_lane_color
            )
    return {
        "source_prefixes": prefixes,
        "source_glyph_metadata": source_glyph_metadata,
        "source_continuation_prefixes": source_continuation_prefixes,
        "source_continuation_glyph_metadata": source_continuation_glyph_metadata,
        "transition_prefixes": transition_prefixes,
        "transition_glyph_metadata": transition_glyph_metadata,
        "child_graph_prefixes": child_graph_prefixes,
        "child_glyph_metadata": child_glyph_metadata,
    }


def dependency_graph_node_color_indices(source_nodes: list[dict], dependency_runs: dict[int, dict[str, object]]) -> list[int]:
    palette_size = len(ListColor.ID_COLORS)
    colors: list[int] = []
    for index, task in enumerate(source_nodes):
        forbidden: set[int] = set()
        if colors:
            forbidden.add(colors[-1])
        for target_index, dependency_run in dependency_runs.items():
            dep_indices = dependency_run.get("dep_indices")
            if not isinstance(dep_indices, list):
                continue
            dep_indices = [
                dep_index
                for dep_index in dep_indices
                if isinstance(dep_index, int) and 0 <= dep_index < len(source_nodes) and dep_index < target_index
            ]
            if not dep_indices:
                continue
            first_dep_index = dep_indices[0]
            if first_dep_index < index < target_index and first_dep_index < len(colors):
                forbidden.add(colors[first_dep_index])
            if index in dep_indices:
                for dep_index in dep_indices:
                    if dep_index == index:
                        break
                    if dep_index < len(colors):
                        forbidden.add(colors[dep_index])
            if index == target_index and dep_indices[-1] < len(colors):
                forbidden.add(colors[dep_indices[-1]])
        base = zlib.crc32(str(task.get("id") or index).encode("utf-8")) % palette_size
        color_index = base
        for offset in range(palette_size):
            candidate = (base + offset) % palette_size
            if candidate not in forbidden:
                color_index = candidate
                break
        colors.append(color_index)
    return colors


GRAPH_LINE_GLYPHS = {"|", "\\", "/"}


def graph_prefix_glyph_metadata(
    prefix: str,
    default_color: int | None = None,
    overrides: dict[int, int] | None = None,
) -> list[int | None]:
    # Metadata is attached per glyph cell: "*" uses node identity, line glyphs use lane identity.
    overrides = overrides or {}
    keys: list[int | None] = []
    for index, char in enumerate(prefix):
        if char == "*" or char in GRAPH_LINE_GLYPHS:
            keys.append(overrides.get(index, default_color))
        else:
            keys.append(None)
    return keys


def graph_source_continuation_glyph_metadata(
    prefix: str,
    passing_lane_color: int,
    source_lane_color: int,
) -> list[int | None]:
    keys: list[int | None] = []
    rail_index = 0
    for char in prefix:
        if char == "|":
            keys.append(passing_lane_color if rail_index == 0 else source_lane_color)
            rail_index += 1
        else:
            keys.append(None)
    return keys


def format_dependency_graph_prefix(
    prefix: str,
    glyph_metadata: list[int | None] | int | str | None,
    color: "ListColor",
) -> str:
    parts = []
    for index, char in enumerate(prefix):
        if char == "*" or char in GRAPH_LINE_GLYPHS:
            color_key = glyph_metadata[index] if isinstance(glyph_metadata, list) and index < len(glyph_metadata) else glyph_metadata
            parts.append(color.graph_branch_color(color_key, char))
        else:
            parts.append(char)
    return "".join(parts)


def graph_source_continuation_prefix(prefix: str) -> str:
    parts = []
    for char in prefix:
        parts.append("|" if char == "*" else char)
    return "".join(parts)


GRAPH_TREE_INDENT = "   "


def graph_tree_prefix(tree_prefix: str) -> str:
    return GRAPH_TREE_INDENT + tree_prefix


def graph_tree_continuation_prefix(tree_prefix: str) -> str:
    return GRAPH_TREE_INDENT + tree_continuation_prefix(tree_prefix)


def graph_tree_parent_continuation_prefix() -> str:
    return GRAPH_TREE_INDENT + "│"


def render_dependency_graph_source_node(
    task: dict,
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
    graph_prefix: str,
    graph_glyph_metadata: list[int | None] | None = None,
    graph_continuation_prefix: str | None = None,
    graph_continuation_glyph_metadata: list[int | None] | None = None,
    tree_gap_prefix: str | None = None,
    terminal_width: int | None = None,
) -> list[str]:
    plain_color = ListColor(False)
    projection = task_list_presentation(task, by_id, config)
    status = color.projection_status(projection)
    plain_status = plain_color.projection_status(projection)
    branch_key = scalar_cell(task.get("id"))
    styled_prefix = format_dependency_graph_prefix(graph_prefix, graph_glyph_metadata or branch_key, color)
    plain_prefix = graph_prefix
    plain_continuation_prefix = graph_continuation_prefix or graph_continuation_prefix_for(plain_prefix)
    continuation_prefix = format_dependency_graph_prefix(
        plain_continuation_prefix,
        graph_continuation_glyph_metadata or graph_glyph_metadata or branch_key,
        color,
    )
    continuation_gap_prefix = color.dim_text(tree_gap_prefix) if tree_gap_prefix else None
    lines = graph_content_lines(
        styled_prefix,
        status,
        compact_title(task),
        terminal_width,
        continuation_prefix=continuation_prefix,
        continuation_gap_prefix=continuation_gap_prefix,
        title_style=lambda value: styled_compact_title_fragment(value, color),
        plain_prefix=plain_prefix,
        plain_label=plain_status,
        plain_continuation_prefix=plain_continuation_prefix,
        plain_continuation_gap_prefix=tree_gap_prefix,
    )
    return lines


def render_dependency_graph_child_row(
    task: dict,
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
    tree_prefix: str,
    graph_prefix: str | None = None,
    graph_glyph_metadata: list[int | None] | None = None,
    dim_child: bool = True,
    terminal_width: int | None = None,
) -> list[str]:
    plain_color = ListColor(False)
    projection = task_list_presentation(task, by_id, config)
    status = color.projection_status(projection)
    plain_status = plain_color.projection_status(projection)
    graph_gap = " " * 8 if graph_prefix is None else graph_prefix
    branch_key = scalar_cell(task.get("id"))
    graph_child_tree_prefix = graph_tree_prefix(tree_prefix)
    styled_tree_prefix = color.dim_text(graph_child_tree_prefix) if dim_child else graph_child_tree_prefix
    styled_prefix = format_dependency_graph_prefix(graph_gap, graph_glyph_metadata or branch_key, color) + styled_tree_prefix
    plain_prefix = graph_gap + graph_child_tree_prefix
    tree_continuation_suffix = graph_tree_continuation_prefix(tree_prefix)
    tree_continuation = graph_gap + tree_continuation_suffix
    styled_tree_continuation_suffix = (
        color.dim_text(tree_continuation_suffix) if dim_child and tree_continuation_suffix.strip() else tree_continuation_suffix
    )
    styled_tree_continuation = (
        format_dependency_graph_prefix(graph_gap, graph_glyph_metadata or branch_key, color) + styled_tree_continuation_suffix
    )
    return graph_content_lines(
        styled_prefix,
        status,
        compact_title(task),
        terminal_width,
        continuation_prefix=styled_tree_continuation,
        title_style=lambda value: styled_compact_title_fragment(value, color, dim_title=dim_child),
        plain_prefix=plain_prefix,
        plain_label=plain_status,
        plain_continuation_prefix=tree_continuation,
    )


def dependency_graph_edge_tail(index: int) -> str:
    edge_width = 7
    offset = min(max(0, index), edge_width - 1)
    return (" " * offset) + "\\" + (" " * (edge_width - offset - 1))


def styled_compact_title_fragment(value: str, color: "ListColor", *, dim_title: bool = False) -> str:
    for marker in ListColor.PROFILE_MARKER_STYLES:
        if value == marker:
            return color.model_marker(value)
        prefix = marker + " "
        if value.startswith(prefix):
            title = value[len(prefix) :]
            styled_title = color.dim_text(title) if dim_title else color.title(title)
            return f"{color.model_marker(marker)} {styled_title}"
    return color.dim_text(value) if dim_title else color.title(value)


def dependency_state_cell(dep: dict | None, by_id: dict[str, dict], config: Config, color: "ListColor") -> str:
    state, style_status = dependency_display_state(dep, by_id, config)
    return color.dependency_state(state, style_status)


def graph_content_lines(
    prefix: str,
    label: str,
    title: str,
    terminal_width: int | None,
    *,
    continuation_prefix: str | None = None,
    title_style=None,
    plain_prefix: str | None = None,
    plain_label: str | None = None,
    plain_title: str | None = None,
    plain_continuation_prefix: str | None = None,
    continuation_gap_prefix: str | None = None,
    plain_continuation_gap_prefix: str | None = None,
) -> list[str]:
    style = title_style or (lambda value: value)
    plain_prefix_value = prefix if plain_prefix is None else plain_prefix
    plain_label_value = label if plain_label is None else plain_label
    plain_title_value = title if plain_title is None else plain_title
    line = f"{prefix}{label}  {style(plain_title_value)}"
    plain_line = f"{plain_prefix_value}{plain_label_value}  {plain_title_value}"
    if terminal_width is None or visible_len(plain_line) <= terminal_width:
        return [line]
    width = max(1, terminal_width)
    protected = f"{prefix}{label}  "
    plain_protected = f"{plain_prefix_value}{plain_label_value}  "
    continuation_base = continuation_prefix or graph_continuation_prefix_for(prefix)
    plain_continuation_base = (
        graph_continuation_prefix_for(plain_prefix_value)
        if plain_continuation_prefix is None
        else plain_continuation_prefix
    )
    label_gap_width = visible_len(plain_label_value) + 2
    continuation_gap = graph_continuation_gap(label_gap_width, continuation_gap_prefix)
    plain_continuation_gap = graph_continuation_gap(
        label_gap_width,
        continuation_gap_prefix if plain_continuation_gap_prefix is None else plain_continuation_gap_prefix,
    )
    continuation = continuation_base + continuation_gap
    plain_continuation = plain_continuation_base + plain_continuation_gap
    return wrap_prefixed_plain_value(
        plain_title_value,
        width,
        protected,
        continuation,
        plain_first_prefix=plain_protected,
        plain_continuation_prefix=plain_continuation,
        style=style,
    )


def graph_continuation_gap(width: int, prefix: str | None = None) -> str:
    width = max(0, width)
    if width == 0:
        return ""
    if prefix is None:
        return " " * width
    visible_prefix_value = visible_prefix(prefix, width)
    return visible_prefix_value + (" " * max(0, width - visible_len(visible_prefix_value)))


def graph_continuation_prefix_for(prefix: str) -> str:
    width = visible_len(prefix)
    if prefix.startswith("|") and width > 0:
        return "|" + (" " * (width - 1))
    return " " * width


def tree_continuation_prefix(prefix: str) -> str:
    if not prefix:
        return ""
    parts = [prefix[index : index + 4] for index in range(0, max(0, len(prefix) - 3), 4)]
    connector = prefix[-3:]
    parts.append("│  " if connector == "├─ " else "   ")
    return "".join(parts)


def wrap_prefixed_value(
    value: str,
    width: int,
    first_prefix: str = "",
    continuation_prefix: str = "",
    *,
    style=None,
) -> list[str]:
    apply_style = style or (lambda item: item)
    prefix_width = max(visible_len(first_prefix), visible_len(continuation_prefix))
    wrapped = wrap_visible(value, max(1, width - prefix_width))
    return [
        (first_prefix if index == 0 else continuation_prefix) + apply_style(line)
        for index, line in enumerate(wrapped)
    ]


def wrap_prefixed_plain_value(
    value: str,
    width: int,
    first_prefix: str = "",
    continuation_prefix: str = "",
    *,
    plain_first_prefix: str | None = None,
    plain_continuation_prefix: str | None = None,
    style=None,
) -> list[str]:
    apply_style = style or (lambda item: item)
    plain_first = first_prefix if plain_first_prefix is None else plain_first_prefix
    plain_continuation = continuation_prefix if plain_continuation_prefix is None else plain_continuation_prefix
    prefix_width = max(visible_len(plain_first), visible_len(plain_continuation))
    wrapped = wrap_plain_text(value, max(1, width - prefix_width))
    return [
        (first_prefix if index == 0 else continuation_prefix) + apply_style(line)
        for index, line in enumerate(wrapped)
    ]


def render_compact_list(
    tasks: list[dict],
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
    terminal_width: int | None = None,
    include_capacity: bool = True,
) -> str:
    header = ["[M]", "TITLE", "STATUS", "DETAIL"]
    if terminal_width is not None and terminal_width < COMPACT_TABLE_BLOCK_LAYOUT_WIDTH:
        project_groups = compact_project_groups(tasks, by_id, config, color, include_capacity=include_capacity)
        return render_compact_block_list(project_groups, terminal_width, color)
    narrow_table = terminal_width is not None and terminal_width < COMPACT_TABLE_COMFORT_WIDTH
    project_groups = compact_project_groups(
        tasks,
        by_id,
        config,
        color,
        include_capacity=include_capacity,
        narrow_table=narrow_table,
    )
    row_groups = [group for _, groups in project_groups for group in groups]
    widths = compact_widths(header, row_groups, terminal_width)
    lines = [render_compact_row(header, widths)]
    for project, groups in project_groups:
        lines.append(project_section_header(project, terminal_width, color))
        for group in groups:
            lines.extend(render_compact_group(group, widths, terminal_width))
    return "\n".join(lines)


def compact_project_groups(
    tasks: list[dict],
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
    include_capacity: bool = True,
    narrow_table: bool = False,
) -> list[tuple[str, list[dict[str, object]]]]:
    grouped: dict[str, list[dict]] = {}
    project_order: list[str] = []
    for task in tasks:
        project = task_project_id(task)
        if project not in grouped:
            grouped[project] = []
            project_order.append(project)
        grouped[project].append(task)
    return [
        (
            project,
            [
                compact_task_group(
                    item["task"],
                    by_id,
                    config,
                    color,
                    tree_prefix=item["prefix"],
                    relationship=str(item.get("relationship") or ""),
                    include_capacity=include_capacity,
                    narrow_table=narrow_table,
                )
                for item in compact_tree_items(grouped[project], by_id)
            ],
        )
        for project in project_order
    ]


def compact_tree_items(tasks: list[dict], by_id: dict[str, dict]) -> list[dict[str, object]]:
    visible_ids = {str(task.get("id")) for task in tasks if task.get("id")}
    children: dict[str, list[dict]] = {task_id: [] for task_id in visible_ids}
    roots: list[dict] = []
    for task in tasks:
        task_id = str(task.get("id") or "")
        parent_id, relationship = compact_parent_relationship(task, visible_ids, by_id)
        if parent_id and parent_id != task_id:
            children.setdefault(parent_id, []).append({"task": task, "relationship": relationship})
        else:
            roots.append(task)

    def sort_key(task: dict) -> tuple[str, str]:
        return list_sort_key(task)

    for task_id in children:
        children[task_id].sort(key=lambda item: sort_key(item["task"] if isinstance(item.get("task"), dict) else {}))
    roots.sort(key=sort_key)

    items: list[dict[str, object]] = []

    def visit(task: dict, ancestors: list[bool], seen: set[str], relationship: str = "") -> None:
        task_id = str(task.get("id") or "")
        prefix = tree_prefix(ancestors) if ancestors else ""
        items.append({"task": task, "prefix": prefix, "relationship": relationship})
        if not task_id or task_id in seen:
            return
        child_items = children.get(task_id, [])
        for index, child in enumerate(child_items):
            child_task = child["task"] if isinstance(child.get("task"), dict) else {}
            visit(
                child_task,
                [*ancestors, index == len(child_items) - 1],
                {*seen, task_id},
                str(child.get("relationship") or ""),
            )

    for root in roots:
        visit(root, [], set())
    attached = {str(item["task"].get("id") or "") for item in items if isinstance(item.get("task"), dict)}
    for task in sorted(tasks, key=sort_key):
        task_id = str(task.get("id") or "")
        if task_id not in attached:
            visit(task, [], set())
    return items


def compact_parent_task_id(task: dict, visible_ids: set[str], by_id: dict[str, dict]) -> str:
    return compact_parent_relationship(task, visible_ids, by_id)[0]


def compact_parent_relationship(task: dict, visible_ids: set[str], by_id: dict[str, dict]) -> tuple[str, str]:
    followup_parent = compact_review_followup_parent_id(task, visible_ids, by_id)
    if followup_parent:
        return followup_parent, "review_followup"
    for key in ("parent_task_id", "subtask_for", "root_task_id"):
        value = task.get(key)
        if value is not None and str(value) in visible_ids:
            return str(value), "subtask"
    task_id = str(task.get("id") or "")
    for parent_id in visible_ids:
        parent = by_id.get(parent_id)
        if parent and task_id in blocking_subtask_ids(parent):
            return parent_id, "subtask"
    return "", ""


def compact_review_followup_parent_id(task: dict, visible_ids: set[str], by_id: dict[str, dict]) -> str:
    explicit_parent_id = str(task.get(REVIEW_FOLLOWUP_FOR_FIELD) or "")
    if explicit_parent_id in visible_ids:
        return explicit_parent_id
    for parent_id in visible_ids:
        parent = by_id.get(parent_id)
        if parent and is_review_followup_child(parent, task):
            return parent_id
    return ""


def is_review_followup_child(parent: dict, task: dict) -> bool:
    if parent.get("resolution"):
        return False
    parent_id = str(parent.get("id") or "")
    task_id = str(task.get("id") or "")
    if not parent_id or not task_id or parent_id == task_id:
        return False
    if str(task.get(REVIEW_FOLLOWUP_FOR_FIELD) or "") == parent_id:
        return True
    if str(task.get("subtask_type") or "") == "auto_review_fix" and parent_id in {
        str(task.get("subtask_for") or ""),
        str(task.get("parent_task_id") or ""),
        str(task.get("root_task_id") or ""),
    }:
        return review_status(parent) == "needs_followup" or str(parent.get("chain_status") or "") in {"needs_fix", "fixing"}
    if str(parent.get("last_auto_fix_task_id") or "") == task_id:
        return True
    follow_up = parent.get("review_follow_up") if isinstance(parent.get("review_follow_up"), dict) else {}
    for key in ("task_id", "follow_up_task_id", "generated_task_id", "linked_task_id"):
        if str(follow_up.get(key) or "") == task_id:
            return True
    return False


def tree_prefix(ancestors: list[bool]) -> str:
    parts = []
    for is_last in ancestors[:-1]:
        parts.append("    " if is_last else "│   ")
    parts.append("└─ " if ancestors[-1] else "├─ ")
    return "".join(parts)


def compact_group_title(task: dict, color: "ListColor", tree_prefix: str, relationship: str = "") -> str:
    if not tree_prefix:
        return styled_task_title(task, color)
    dim_child = relationship != "review_followup"
    styled_prefix = color.dim_text(tree_prefix) if dim_child else tree_prefix
    return styled_prefix + styled_task_title(task, color, dim_title=dim_child)


def compact_task_group(
    task: dict,
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
    tree_prefix: str = "",
    relationship: str = "",
    include_capacity: bool = True,
    narrow_table: bool = False,
) -> dict[str, object]:
    cells = task_display_cells(task, by_id, config, color, narrow_table=narrow_table)
    detail_segments = detail_cells(task, by_id, config, include_capacity=include_capacity)
    dim_child = bool(tree_prefix) and relationship != "review_followup"
    title_prefix = color.dim_text(tree_prefix) if dim_child else tree_prefix
    title_continuation_prefix = (
        color.dim_text(tree_continuation_prefix(tree_prefix)) if dim_child else tree_continuation_prefix(tree_prefix)
    )
    return {
        "model": cells["model"],
        "summary": [cells["status"]],
        "details": detail_segments or ["-"],
        "title": compact_group_title(task, color, tree_prefix, relationship),
        "title_prefix": title_prefix if tree_prefix else "",
        "title_continuation_prefix": title_continuation_prefix if tree_prefix else "",
        "title_value": styled_task_title(task, color, dim_title=dim_child),
    }


def task_display_cells(
    task: dict,
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
    narrow_table: bool = False,
) -> dict[str, str]:
    projection = task_list_presentation(task, by_id, config)
    return {
        "model": color.model_marker(model_requirement_marker(task)),
        "status": compact_table_status(projection, color, narrow_table=narrow_table),
        "title": task_title(task),
    }


def compact_table_status(projection: TaskListPresentation, color: "ListColor", narrow_table: bool = False) -> str:
    if not narrow_table:
        return color.projection_status(projection)
    label = COMPACT_TABLE_NARROW_STATUS_LABELS.get(projection.legacy_status, projection.status_label)
    if label == projection.status_label:
        return color.projection_status(projection)
    return color.projection_marker(projection) + color.status_label(label, projection.legacy_status)


def project_section_header(project: str, terminal_width: int | None, color: "ListColor | None" = None) -> str:
    label = f"[{project}]"
    if terminal_width is None:
        return color.project_section(label) if color else label
    width = max(1, terminal_width)
    label = fit_visible(label, width)
    if not color or not color.enabled:
        return label
    label = pad_visible(label, width)
    return color.project_section(label)


def compact_widths(header: list[str], row_groups: list[dict[str, object]], terminal_width: int | None) -> list[int]:
    model_width = max(
        [visible_len(header[0]), *(visible_len(str(group.get("model") or "")) for group in row_groups)]
        or [visible_len(header[0])]
    )
    details = [cell for group in row_groups for cell in group["details"]]  # type: ignore[index]
    titles = [
        str(group.get("title_prefix") or "") + str(group.get("title_value") or group.get("title") or "-")
        for group in row_groups
    ]
    title_min = max(visible_len(header[1]), COMPACT_TABLE_MIN_TITLE_WIDTH)
    detail_min = max(visible_len(header[3]), COMPACT_TABLE_MIN_DETAIL_WIDTH)
    title_width = max([title_min, *(visible_len(str(title)) for title in titles)] or [title_min])
    status = compact_status_width(header, row_groups)
    detail_width = max([detail_min, *(visible_len(str(detail)) for detail in details)] or [detail_min])
    widths = [model_width, title_width, status, detail_width]
    if terminal_width is None:
        return widths
    fixed = model_width + status + (len(widths) - 1) * 2
    available = max(3, terminal_width - fixed)
    minimums = [title_min, detail_min]
    minimums = compact_table_minimums_for_available(available, minimums)
    desired = [title_width, detail_width]
    title_share, detail_share = distribute_compact_widths(available, minimums, desired)
    widths[1] = title_share
    widths[3] = detail_share
    return widths


def compact_table_minimums_for_available(available: int, preferred: list[int]) -> list[int]:
    widths = preferred.copy()
    floors = [
        min(widths[0], COMPACT_TABLE_FLOOR_TITLE_WIDTH),
        min(widths[1], COMPACT_TABLE_FLOOR_DETAIL_WIDTH),
    ]
    target = max(3, available)
    for index in (1, 0):
        if sum(widths) <= target:
            break
        widths[index] -= min(widths[index] - floors[index], sum(widths) - target)
    while sum(widths) > target:
        candidates = [index for index, width in enumerate(widths) if width > 1]
        if not candidates:
            break
        index = max(candidates, key=lambda item: widths[item])
        widths[index] -= min(widths[index] - 1, sum(widths) - target)
    return widths


def compact_status_width(header: list[str], row_groups: list[dict[str, object]]) -> int:
    return column_width(header[2], [group["summary"] for group in row_groups], 0)


def distribute_compact_widths(available: int, minimums: list[int], desired: list[int]) -> list[int]:
    widths = minimums.copy()
    extra = max(0, available - sum(widths))
    weights = [2, 2]
    while extra > 0:
        progressed = False
        for index in sorted(range(len(widths)), key=lambda item: -weights[item]):
            capacity = max(0, desired[index] - widths[index])
            if capacity <= 0:
                continue
            share = max(1, min(capacity, extra // max(1, sum(weights)) or 1))
            widths[index] += share
            extra -= share
            progressed = True
            if extra <= 0:
                break
        if not progressed:
            widths[-1] += extra
            break
    return widths


def column_width(header: str, rows: list[object], index: int, cap: int | None = None) -> int:
    values = [visible_len(header)]
    for row in rows:
        if isinstance(row, list) and index < len(row):
            values.append(visible_len(str(row[index])))
    width = max(values)
    return min(width, cap) if cap else width


def render_compact_group(group: dict[str, object], widths: list[int], terminal_width: int | None) -> list[str]:
    details = group["details"] if isinstance(group["details"], list) else ["-"]
    detail_lines = wrap_cell_list([str(detail) for detail in details], widths[3])
    row_count = max(1, len(detail_lines))
    summary = group["summary"] if isinstance(group["summary"], list) else [""]
    model = str(group.get("model") or "")
    title_prefix = str(group.get("title_prefix") or "")
    title_continuation_prefix = str(group.get("title_continuation_prefix") or "")
    title_value = str(group.get("title_value") or group.get("title") or "-")
    title_lines = fit_title_lines(title_value, widths[1], row_count, title_prefix, title_continuation_prefix)
    lines = []
    for index in range(row_count):
        lines.append(
            render_compact_row(
                [
                    fit_visible(model, widths[0]) if index == 0 else "",
                    title_lines[index] if index < len(title_lines) else "",
                    fit_visible(str(summary[0]), widths[2]) if index == 0 else "",
                    detail_lines[index] if index < len(detail_lines) else "",
                ],
                widths,
            )
        )
    return lines


def fit_title_lines(value: str, width: int, max_lines: int, first_prefix: str = "", continuation_prefix: str = "") -> list[str]:
    lines = wrap_prefixed_value(value, width, first_prefix, continuation_prefix)
    if len(lines) <= max_lines:
        return [fit_visible(line, width) for line in lines]
    lines = lines[:max_lines]
    lines[-1] = force_ellipsis_visible(lines[-1], width)
    return lines


def force_ellipsis_visible(value: str, width: int) -> str:
    if visible_len(value) <= max(0, width - 4):
        return fit_visible(value + " ...", width)
    return truncate_visible(value, width)


def render_compact_block_list(
    project_groups: list[tuple[str, list[dict[str, object]]]],
    terminal_width: int,
    color: "ListColor",
) -> str:
    lines = []
    for project, row_groups in project_groups:
        if lines:
            lines.append("")
        lines.append(project_section_header(project, terminal_width, color))
        for index, group in enumerate(row_groups):
            if index:
                lines.append("")
            summary = group["summary"] if isinstance(group["summary"], list) else ["-", "-"]
            block_rows = [
                ("[M]", str(group.get("model") or "-")),
                ("STATUS", str(summary[0])),
                (
                    "TITLE",
                    str(group.get("title_value") or group.get("title") or "-"),
                    str(group.get("title_prefix") or ""),
                    str(group.get("title_continuation_prefix") or ""),
                ),
            ]
            details = group["details"] if isinstance(group["details"], list) else ["-"]
            block_rows.extend(("DETAIL", str(detail)) for detail in details)
            lines.extend(render_block_rows(block_rows, terminal_width))
    return "\n".join(lines)


def render_block_rows(rows: list[tuple[str, str] | tuple[str, str, str, str]], terminal_width: int) -> list[str]:
    normalized: list[tuple[str, str, str, str]] = []
    for row in rows:
        if len(row) == 2:
            label, value = row
            normalized.append((label, value, "", ""))
        else:
            label, value, first_prefix, continuation_prefix = row
            normalized.append((label, value, first_prefix, continuation_prefix))
    label_width = max(visible_len(label) for label, _, _, _ in normalized)
    value_width = max(8, terminal_width - label_width - 2)
    lines = []
    for label, value, first_prefix, continuation_prefix in normalized:
        wrapped = wrap_prefixed_value(value, value_width, first_prefix, continuation_prefix)
        for index, line in enumerate(wrapped):
            row_prefix = (label + ":").ljust(label_width + 1) + " " if index == 0 else " " * (label_width + 2)
            lines.append(row_prefix + line)
    return lines


def wrap_cell_list(values: list[str], width: int) -> list[str]:
    lines: list[str] = []
    for value in values:
        lines.extend(wrap_visible(value, width))
    return lines or [""]


def render_compact_row(row: list[str], widths: list[int]) -> str:
    padded = [pad_visible(cell, widths[index]) for index, cell in enumerate(row[:-1])]
    return "  ".join([*padded, row[-1]])


def list_table_row(task: dict, by_id: dict[str, dict], config: Config, include_capacity: bool = True) -> list[str]:
    return [
        scalar_cell(task.get("id")),
        scalar_cell(truncate_table_text(compact_title(task), 72)),
        scalar_cell(status_cell(task, by_id, config)),
        scalar_cell(task_project_id(task)),
        scalar_cell(task.get("attempts", 0)),
        deps_cell(task.get("depends_on"), by_id, config),
        note_cell(task, by_id, config, include_capacity=include_capacity),
    ]


def scalar_cell(value: object) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def deps_cell(
    depends_on: object,
    by_id: dict[str, dict] | None = None,
    config: Config | None = None,
    color: "ListColor | None" = None,
) -> str:
    if not isinstance(depends_on, list) or not depends_on:
        return "-"
    color = color or ListColor(False)
    if by_id is None or config is None:
        return ",".join(color.task_id(str(dep_id)) for dep_id in depends_on)
    return ",".join(dependency_id_cell(str(dep_id), by_id, config, color) for dep_id in depends_on)


def dependency_id_cell(dep_id: str, by_id: dict[str, dict], config: Config, color: "ListColor") -> str:
    dep = by_id.get(dep_id)
    state, style_status = dependency_display_state(dep, by_id, config)
    if state == "done":
        return color.satisfied_dependency(dep_id)
    return color.dependency(dep_id, state, style_status)


def dependency_title_cell(dep_id: str, by_id: dict[str, dict], config: Config, color: "ListColor") -> str:
    dep = by_id.get(dep_id)
    state, style_status = dependency_display_state(dep, by_id, config)
    title = f"missing dependency: {dep_id}" if dep is None else compact_title(dep)
    return color.dependency_title(title, state, style_status)


def dependency_display_state(dep: dict | None, by_id: dict[str, dict], config: Config) -> tuple[str, str]:
    if not dep:
        return ("missing", "missing")
    if dep.get("status") == "completed":
        if config.dependency_requires_accepted_review and dep.get("review_status") != "accepted":
            return ("not_accepted", "awaiting_review")
        if dep.get("execution_mode") == "git_worktree":
            if dep.get("review_status") != "accepted":
                return ("not_accepted", "awaiting_review")
            if dep.get("execution_apply_status") != "applied":
                return ("not_applied", "awaiting_review")
        return ("done", "completed")
    return ("blocked", status_cell(dep, by_id, config))


def status_cell(task: dict, by_id: dict[str, dict] | None = None, config: Config | None = None) -> str:
    return task_list_status(task, by_id, config)


def note_cell(task: dict, by_id: dict[str, dict], config: Config, include_capacity: bool = True) -> str:
    notes = note_cells(task, by_id, config, include_capacity=include_capacity)
    return "; ".join(notes) if notes else "-"


def detail_cells(task: dict, by_id: dict[str, dict], config: Config, include_capacity: bool = True) -> list[str]:
    projection = task_list_presentation(task, by_id, config)
    details: list[str] = []
    append_unique_detail(details, projection.detail)
    blocker_summary = projection_blocker_summary(projection, by_id)
    if blocker_summary:
        append_unique_detail(details, blocker_summary)
    for note in note_cells(task, by_id, config, include_capacity=include_capacity):
        if projection.kind == "capacity" and note.startswith("waiting for capacity:"):
            continue
        append_unique_detail(details, note)
    return details or ["-"]


def append_unique_detail(details: list[str], value: str) -> None:
    normalized = str(value or "").strip()
    if not normalized or normalized == "-":
        return
    if normalized not in details:
        details.append(normalized)


def projection_blocker_summary(projection: TaskListPresentation, by_id: dict[str, dict]) -> str:
    blockers = projection.blockers
    if not blockers:
        return ""
    if projection.kind == "dep":
        return dependency_projection_blocker_summary(blockers, by_id)
    if projection.kind == "capacity":
        reasons = [str(blocker.get("reason") or "").strip() for blocker in blockers if blocker.get("reason")]
        return "blocked by " + ", ".join(reasons) if reasons else ""
    if len(blockers) == 1:
        return generic_projection_blocker_label(blockers[0], by_id)
    return f"{len(blockers)} blockers: {generic_projection_blocker_label(blockers[0], by_id)}"


def dependency_projection_blocker_summary(blockers: list[dict[str, str]], by_id: dict[str, dict]) -> str:
    if not blockers:
        return ""
    first = dependency_projection_blocker_label(blockers[0], by_id)
    if len(blockers) == 1:
        return "blocked by " + first
    return f"{len(blockers)} dependency blockers: {first}"


def dependency_projection_blocker_label(blocker: dict[str, str], by_id: dict[str, dict]) -> str:
    dep_id = str(blocker.get("id") or "")
    title = dependency_label(dep_id, by_id) if dep_id else "dependency"
    reason = str(blocker.get("reason") or "blocked")
    return f"{title} ({reason})"


def generic_projection_blocker_label(blocker: dict[str, str], by_id: dict[str, dict]) -> str:
    blocker_type = str(blocker.get("type") or "blocker")
    blocker_id = str(blocker.get("id") or "")
    reason = str(blocker.get("reason") or blocker.get("status") or "").strip()
    if blocker_type == "dependency":
        return dependency_projection_blocker_label(blocker, by_id)
    if blocker_id:
        label = dependency_label(blocker_id, by_id)
        return f"{label} ({reason})" if reason else label
    return reason or blocker_type


def note_cells(task: dict, by_id: dict[str, dict], config: Config, include_capacity: bool = True) -> list[str]:
    notes = []
    capacity = []
    if include_capacity and task.get("status") in RUNNABLE_STATUSES:
        capacity = capacity_blockers(config, task)
        if capacity:
            notes.append("waiting for capacity: " + ",".join(capacity))
    if is_in_cooldown(task):
        notes.append(cooldown_note(task))
    elif task.get("status") == "needs_resume" and not capacity and dependency_status(
        task,
        by_id,
        require_accepted_review=config.dependency_requires_accepted_review,
    )[0]:
        notes.append("ready to resume")
    if startup_stalled(task):
        notes.append(startup_stall_note(task))
    if task.get("status") == "failed" and task.get("last_error"):
        notes.append("error: " + one_line(task.get("last_error")))
    if task.get("status") == "running":
        notes.extend(running_notes(task, config))
    blocking_subtasks = blocking_subtask_note_cell(task, by_id, config)
    if blocking_subtasks:
        notes.append(blocking_subtasks)
    subtask_note = subtask_note_cell(task)
    if subtask_note:
        notes.append(subtask_note)
    backend_note = execution_backend_note(task)
    if backend_note:
        notes.append(backend_note)
    scheduling_note = scheduling_note_cell(task)
    if scheduling_note:
        notes.append(scheduling_note)
    if task.get("resolution"):
        notes.append("resolved: " + str(task.get("resolution")))
    if not task.get("resolution") and rejected_discarded_result(task):
        notes.append("rejected; discarded; not applied")
    if task.get("status") == "completed" and not task.get("resolution"):
        if review_status(task) == "rejected" and not rejected_discarded_result(task):
            notes.append("rejected")
        reviewer_note = pending_reviewer_note(task)
        if reviewer_note:
            notes.append(reviewer_note)
        notes.extend(completed_timing_notes(task))
        follow_up_note = review_follow_up_note(task, by_id)
        if follow_up_note:
            notes.append(follow_up_note)
        chain_note = chain_note_cell(task)
        if chain_note and not (reviewer_note and chain_note in reviewer_note) and not follow_up_note:
            notes.append(chain_note)
        worktree_note = worktree_apply_note(task)
        if worktree_note:
            notes.append(worktree_note)
    return notes or ["-"]


def cooldown_note(task: dict) -> str:
    until = parse_time(task.get("cooldown_until"))
    if not until:
        return "cooldown until " + scalar_cell(task.get("cooldown_until"))
    seconds = max(0, int((until - utc_now()).total_seconds()))
    label = "cooldown"
    if task.get("status") == "needs_resume":
        label = "resume"
    elif task.get("status") == "runnable":
        label = "retry"
    return f"{label} in {format_elapsed(seconds)} ({format_clock(until)})"


def format_clock(value) -> str:
    return value.strftime("%H:%M")


def completed_timing_notes(task: dict) -> list[str]:
    notes = []
    completed = parse_time(task.get("completed_at"))
    if completed:
        seconds = max(0, int((utc_now() - completed).total_seconds()))
        notes.append(f"done {format_elapsed(seconds)} ago")
    duration = task_duration_seconds(task)
    if duration is not None:
        notes.append(f"ran {format_elapsed(duration)}")
    return notes


def task_duration_seconds(task: dict) -> int | None:
    last_run = task.get("last_run")
    if isinstance(last_run, dict) and last_run.get("duration_seconds") is not None:
        try:
            return max(0, int(float(last_run.get("duration_seconds"))))
        except (TypeError, ValueError):
            pass
    started = parse_time(task.get("started_at"))
    completed = parse_time(task.get("completed_at"))
    if not started or not completed:
        return None
    return max(0, int((completed - started).total_seconds()))


def scheduling_note_cell(task: dict) -> str:
    fields = []
    pool = task_capacity_pool(task)
    priority = task_priority(task)
    if pool != "codex":
        fields.append(f"pool={pool}")
    if priority != "normal":
        fields.append(f"priority={priority}")
    return ", ".join(fields) if fields else ""


def worktree_apply_note(task: dict) -> str:
    if task.get("execution_mode") != "git_worktree":
        return ""
    if task.get("execution_conflict_fix_status") == "queued" and task.get("execution_conflict_fix_task_id"):
        return "conflict fix queued"
    if task.get("execution_rebase_status") == "blocked":
        return "rebase blocked"
    if task.get("execution_rebase_status") == "rebased" and review_status(task) != "accepted":
        return "rebased; re-review needed"
    if review_status(task) != "accepted":
        return ""
    if task.get("execution_apply_status") == "applied":
        target = one_line(task.get("execution_apply_target") or "main")
        return f"applied to {target}"
    return "accepted_unapplied; not applied"


def model_requirement_note(task: dict) -> str:
    parts = []
    vector = task.get("model_requirement_vector")
    if isinstance(vector, dict) and vector.get("source") == "derived_from_task_vector":
        return ""
    dimensions = vector.get("dimensions") if isinstance(vector, dict) else {}
    if isinstance(dimensions, dict):
        parts.extend(f"{key}={one_line(value)}" for key, value in sorted(dimensions.items()))
    return " ".join(parts)


def execution_backend_note(task: dict) -> str:
    if task.get("execution_backend") and task.get("execution_backend") != "codex":
        return "backend=" + one_line(task.get("execution_backend"))
    return ""


def compact_title(task: dict) -> str:
    marker = model_requirement_marker(task)
    title = task_title(task)
    return f"{marker} {title}"


def styled_compact_title(task: dict, color: "ListColor", *, dim_title: bool = False) -> str:
    marker = color.model_marker(model_requirement_marker(task))
    title = task_title(task)
    if dim_title:
        title = color.dim_text(title)
    return f"{marker} {title}"


def styled_task_title(task: dict, color: "ListColor", *, dim_title: bool = False) -> str:
    title = task_title(task)
    return color.dim_text(title) if dim_title else color.title(title)


def model_requirement_marker(task: dict) -> str:
    vector = task.get("model_requirement_vector")
    dimensions = vector.get("dimensions") if isinstance(vector, dict) else {}
    if not isinstance(dimensions, dict) or not dimensions:
        return "[N]"
    if dimensions.get("reasoning_depth") == "low" and dimensions.get("cost_sensitivity") == "high":
        return "[S]"
    if dimensions.get("reasoning_depth") == "high" or dimensions.get("tool_reliability") == "high":
        return "[D]"
    return "[N]"


def chain_note_cell(task: dict) -> str:
    chain_status = task.get("chain_status")
    decision = task.get("last_review_decision")
    if decision == "needs_fix":
        return "fix requested"
    if decision == "needs_human":
        return "human review needed"
    if decision == "failed_review":
        return "review process failed"
    if chain_status and decision:
        return f"{chain_status_note_label(chain_status)} after {review_decision_note_label(decision)}"
    if chain_status:
        return chain_status_note_label(chain_status)
    if decision:
        return review_decision_note_label(decision)
    return ""


def pending_reviewer_note(task: dict) -> str:
    decision = pending_reviewer_decision(task)
    if decision == "needs_fix":
        return "reviewer needs fix; run reject --follow-up"
    if decision == "pass":
        return "reviewer passed; run accept"
    if decision == "failed_review":
        return "review process failed; rerun review-next"
    return ""


def pending_reviewer_decision(task: dict) -> str:
    if task.get("status") != "completed" or review_status(task) != "unreviewed":
        return ""
    reviewer = task.get("reviewer_codex") if isinstance(task.get("reviewer_codex"), dict) else {}
    decision = str(reviewer.get("decision") or task.get("last_review_decision") or "")
    chain_status = str(task.get("chain_status") or "")
    if decision == "needs_fix" or chain_status == "needs_fix":
        return "needs_fix"
    if decision == "pass":
        return "pass"
    if decision == "failed_review":
        return "failed_review"
    return ""


def chain_status_note_label(value: object) -> str:
    labels = {
        "awaiting_review": "review pending",
        "accepted": "review accepted",
        "fixing": "fix running",
        "needs_fix": "fix needed",
        "needs_human": "human review needed",
        "waiting_fix": "waiting for fix",
    }
    status = str(value or "")
    return labels.get(status, "chain " + one_line(status))


def review_decision_note_label(value: object) -> str:
    labels = {
        "needs_fix": "fix requested",
        "needs_human": "human review needed",
        "failed_review": "review process failed",
        "pass": "review passed",
    }
    decision = str(value or "")
    return labels.get(decision, "review " + one_line(decision))


def subtask_note_cell(task: dict, by_id: dict[str, dict] | None = None) -> str:
    subtask_type = task.get("subtask_type")
    subtask_for = task.get("subtask_for")
    subtask_for_label = str(subtask_for or "")
    if subtask_for and by_id and by_id.get(str(subtask_for)):
        subtask_for_label = f"{dependency_label(str(subtask_for), by_id)} ({subtask_for})"
    if subtask_type and subtask_for:
        return f"{subtask_type_note_label(subtask_type)} for {subtask_for_label}"
    if subtask_type:
        return subtask_type_note_label(subtask_type)
    return ""


def subtask_type_note_label(value: object) -> str:
    labels = {
        "auto_review_fix": "review fix",
        "conflict_fix": "conflict fix",
    }
    subtask_type = str(value or "")
    return labels.get(subtask_type, "subtask " + one_line(subtask_type))


def blocking_subtask_effective_status(task: dict, by_id: dict[str, dict], config: Config) -> str:
    active = active_blocking_subtasks(task, by_id)
    if not active:
        return ""
    statuses = [blocking_subtask_status(item, by_id, config) for item in active]
    blocked_statuses = {
        "missing",
        "failed",
        "blocked_user",
        "discarded",
        "review_failed",
        "review_rejected",
        "needs_followup",
        "review_needs_fix",
        "subtasks_blocked",
    }
    if any(status in blocked_statuses for status in statuses):
        return "subtasks_blocked"
    return "waiting_subtasks"


def blocking_subtask_note_cell(task: dict, by_id: dict[str, dict], config: Config) -> str:
    ids = blocking_subtask_ids(task)
    active = active_blocking_subtasks(task, by_id)
    if not active:
        return ""
    counts: dict[str, int] = {}
    for item in active:
        status = blocking_subtask_status(item, by_id, config)
        counts[status] = counts.get(status, 0) + 1
    summary = ", ".join(f"{count} {subtask_status_note_label(status)}" for status, count in sorted(counts.items())[:2])
    timing = active_subtask_timing_summary(active, by_id, config)
    note = f"waiting on {len(active)}/{len(ids)} subtasks: {summary}"
    return f"{note}, {timing}" if timing else note


def subtask_status_note_label(status: str) -> str:
    labels = {
        "awaiting_review": "review pending",
        "blocked_dependency": "dependency blocked",
        "blocked_user": "blocked",
        "failed": "failed",
        "missing": "missing",
        "needs_followup": "fix needed",
        "review_needs_fix": "review fix needed",
        "review_pass_pending": "accept pending",
        "discarded": "discarded",
        "review_rejected": "review rejected",
        "review_failed": "review failed",
        "reviewing": "reviewing",
        "running": "running",
        "runnable": "ready",
        "subtasks_blocked": "subtask blocked",
        "waiting_subtasks": "waiting",
    }
    return labels.get(status, one_line(status))


def active_subtask_timing_summary(active: list[dict | None], by_id: dict[str, dict], config: Config) -> str:
    status_rows = [(task, blocking_subtask_status(task, by_id, config)) for task in active if task]
    blocked_statuses = {
        "failed",
        "blocked_user",
        "discarded",
        "review_failed",
        "review_rejected",
        "needs_followup",
        "review_needs_fix",
        "subtasks_blocked",
    }
    blocked = [task for task, status in status_rows if status in blocked_statuses]
    if blocked:
        return oldest_age_note(blocked, ("completed_at", "updated_at", "started_at"), "oldest blocked")
    running = [task for task, status in status_rows if status == "running"]
    if running:
        return oldest_age_note(running, ("started_at", "updated_at"), "oldest running")
    review = [task for task, status in status_rows if status in {"awaiting_review", "reviewing", "review_pass_pending"}]
    if review:
        return oldest_age_note(review, ("completed_at", "updated_at", "started_at"), "oldest review")
    return ""


def oldest_age_note(tasks: list[dict], fields: tuple[str, ...], label: str) -> str:
    ages = []
    now = utc_now()
    for task in tasks:
        timestamp = first_task_time(task, fields)
        if timestamp:
            ages.append(max(0, int((now - timestamp).total_seconds())))
    if not ages:
        return label
    return f"{label} {format_elapsed(max(ages))}"


def first_task_time(task: dict, fields: tuple[str, ...]):
    for field in fields:
        timestamp = parse_time(task.get(field))
        if timestamp:
            return timestamp
    return None


def blocking_subtask_status(task: dict | None, by_id: dict[str, dict], config: Config) -> str:
    if not task:
        return "missing"
    return status_cell_without_subtasks(task, by_id, config)


def status_cell_without_subtasks(task: dict, by_id: dict[str, dict], config: Config) -> str:
    return task_list_status_without_subtasks(task, by_id, config)


def active_blocking_subtasks(task: dict, by_id: dict[str, dict]) -> list[dict | None]:
    active = []
    for task_id in blocking_subtask_ids(task):
        subtask = by_id.get(task_id)
        if subtask and subtask.get("status") == "completed" and review_status(subtask) == "accepted":
            continue
        active.append(subtask)
    return active


def blocking_subtask_ids(task: dict) -> list[str]:
    ids = task.get("blocking_subtask_ids")
    if not isinstance(ids, list):
        return []
    return [str(item) for item in ids if str(item)]


def startup_stalled(task: dict) -> bool:
    progress = task.get("last_progress")
    return bool(task.get("startup_stalled_at") or (isinstance(progress, dict) and progress.get("watchdog_reason")))


def startup_stall_note(task: dict) -> str:
    status = str(task.get("status") or "")
    if status in {"runnable", "needs_resume", "running"}:
        return "startup stalled; retrying"
    return "startup stalled earlier"


def dependency_blocker_labels(task: dict, by_id: dict[str, dict], config: Config) -> list[str]:
    return [
        dependency_blocker_label(blocker, by_id)
        for blocker in dependency_blockers(
            task,
            by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )
    ]


def dependency_blocker_label(blocker: dict[str, str], by_id: dict[str, dict]) -> str:
    dep_id = blocker["id"]
    label = dependency_label(dep_id, by_id)
    base = f"{label} ({dep_id})" if label != dep_id else dep_id
    return base if blocker["reason"] == "not_completed" else f"{base}:{blocker['reason']}"


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
    LIGHT_YELLOW = "\033[93m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    LIGHT_CYAN = "\033[96m"
    BLUE = "\033[34m"
    BG_RED = "\033[101;97;1m"
    BG_YELLOW = "\033[103;30m"
    BG_DIM = "\033[100;37m"
    BG_ACTIVE_GREEN_BEARING_CYAN = "\033[106;30m"
    BG_ACTIVE_GREEN_BEARING_YELLOW = "\033[103;30m"
    BG_ACTIVE_GREEN_BEARING_GREEN = "\033[102;30m"
    BG_ACTIVE_NON_GREEN_BLUE = "\033[104;97m"
    BG_ACTIVE_NON_GREEN_RED = "\033[101;97m"
    BG_PASSIVE_GREEN_BEARING_CYAN = "\033[100;96m"
    BG_PASSIVE_GREEN_BEARING_YELLOW = "\033[100;93m"
    BG_PASSIVE_GREEN_BEARING_GREEN = "\033[100;92m"
    BG_PASSIVE_NON_GREEN_RED = "\033[100;31m"
    BG_PASSIVE_NON_GREEN_BLUE = "\033[100;34m"
    BG_PASSIVE_WHITE = "\033[100;97m"
    PROJECT_SECTION = "\033[1;96;100m"
    STATUS_MARKER_BLUE = "\033[1;97;44m"
    STATUS_MARKER_CYAN = "\033[1;97;46m"
    STATUS_MARKER_YELLOW = "\033[1;97;43m"
    STATUS_MARKER_GREEN = "\033[1;97;42m"
    STATUS_MARKER_RED = "\033[1;97;41m"
    STATUS_MARKER_NEUTRAL = "\033[1;97;100m"
    ID_COLORS = ("\033[35m", "\033[36m", "\033[34m", "\033[32m", "\033[33m", "\033[91m")
    ACTIVE_STATUS_STYLES = {
        "running": BG_ACTIVE_GREEN_BEARING_CYAN,
        "awaiting_review": BG_ACTIVE_GREEN_BEARING_YELLOW,
        "reviewing": BG_ACTIVE_GREEN_BEARING_YELLOW,
        "review_pass_pending": BG_ACTIVE_GREEN_BEARING_GREEN,
        "needs_resume": BG_ACTIVE_NON_GREEN_BLUE,
        "waiting_subtasks": BG_ACTIVE_GREEN_BEARING_YELLOW,
        "cooldown": BG_DIM,
        "usage_exhausted": BG_DIM,
        "failed": BG_ACTIVE_NON_GREEN_RED,
        "review_failed": BG_ACTIVE_NON_GREEN_RED,
        "review_rejected": BG_ACTIVE_NON_GREEN_RED,
        "needs_followup": BG_ACTIVE_NON_GREEN_RED,
        "review_needs_fix": BG_ACTIVE_NON_GREEN_RED,
        "blocked_user": BG_ACTIVE_NON_GREEN_RED,
        "subtasks_blocked": BG_ACTIVE_NON_GREEN_RED,
        "accepted_unapplied": BG_ACTIVE_GREEN_BEARING_YELLOW,
    }
    PASSIVE_STATUS_STYLES = {
        "runnable": BG_PASSIVE_GREEN_BEARING_CYAN,
        "blocked_dependency": BG_PASSIVE_GREEN_BEARING_YELLOW,
        "completed": BG_PASSIVE_GREEN_BEARING_GREEN,
        "accepted": BG_PASSIVE_GREEN_BEARING_GREEN,
        "done": BG_PASSIVE_GREEN_BEARING_GREEN,
        "discarded": BG_PASSIVE_WHITE,
        "resolved": BG_PASSIVE_WHITE,
        "archived": BG_PASSIVE_WHITE,
    }
    DEPENDENCY_STATE_STYLES = {
        "missing": BG_RED,
        "not_accepted": BG_YELLOW,
        "not_applied": BG_YELLOW,
    }
    PROFILE_MARKER_STYLES = {
        "[S]": CYAN,
        "[N]": GREEN,
        "[D]": YELLOW,
    }
    STATUS_MARKERS = {
        "runnable": "..",
        "needs_resume": "..",
        "cooldown": "..",
        "usage_exhausted": "..",
        "blocked_dependency": "||",
        "waiting_subtasks": "||",
        "running": ">>",
        "awaiting_review": "??",
        "reviewing": "??",
        "review_pass_pending": "??",
        "accepted_unapplied": "??",
        "completed": "==",
        "accepted": "==",
        "done": "==",
        "failed": "!!",
        "review_failed": "!!",
        "review_rejected": "!!",
        "needs_followup": "!!",
        "review_needs_fix": "!!",
        "blocked_user": "!!",
        "subtasks_blocked": "!!",
        "discarded": "--",
        "resolved": "--",
        "archived": "--",
    }
    STATUS_MARKER_STYLES = {
        "runnable": STATUS_MARKER_CYAN,
        "needs_resume": STATUS_MARKER_BLUE,
        "cooldown": STATUS_MARKER_NEUTRAL,
        "usage_exhausted": STATUS_MARKER_NEUTRAL,
        "blocked_dependency": STATUS_MARKER_YELLOW,
        "waiting_subtasks": STATUS_MARKER_YELLOW,
        "running": STATUS_MARKER_CYAN,
        "awaiting_review": STATUS_MARKER_YELLOW,
        "reviewing": STATUS_MARKER_YELLOW,
        "review_pass_pending": STATUS_MARKER_GREEN,
        "accepted_unapplied": STATUS_MARKER_YELLOW,
        "completed": STATUS_MARKER_GREEN,
        "accepted": STATUS_MARKER_GREEN,
        "done": STATUS_MARKER_GREEN,
        "failed": STATUS_MARKER_RED,
        "review_failed": STATUS_MARKER_RED,
        "review_rejected": STATUS_MARKER_RED,
        "needs_followup": STATUS_MARKER_RED,
        "review_needs_fix": STATUS_MARKER_RED,
        "blocked_user": STATUS_MARKER_RED,
        "subtasks_blocked": STATUS_MARKER_RED,
        "discarded": STATUS_MARKER_NEUTRAL,
        "resolved": STATUS_MARKER_NEUTRAL,
        "archived": STATUS_MARKER_NEUTRAL,
    }

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

    def graph_branch(self, key: str, value: str) -> str:
        if not self.enabled or not value:
            return value
        index = zlib.crc32(key.encode("utf-8")) % len(self.ID_COLORS)
        return self.apply(value, self.ID_COLORS[index])

    def graph_branch_color(self, key: int | str | None, value: str) -> str:
        if not self.enabled or not value or key is None:
            return value
        if isinstance(key, int):
            return self.apply(value, self.ID_COLORS[key % len(self.ID_COLORS)])
        return self.graph_branch(key, value)

    def model_marker(self, marker: str) -> str:
        return self.apply(marker, self.PROFILE_MARKER_STYLES.get(marker, self.GREEN))

    def project(self, project_id: str) -> str:
        return self.apply(project_id, self.LIGHT_CYAN)

    def project_section(self, label: str) -> str:
        return self.apply(label, self.PROJECT_SECTION)

    def title(self, title: str) -> str:
        return title

    def dim_text(self, value: str) -> str:
        return self.apply(value, self.DIM)

    def satisfied_dependency(self, dep_id: str) -> str:
        if not self.enabled or dep_id == "-":
            return f"{dep_id} (done)"
        return self.apply(dep_id, self.DIM)

    def dependency(self, dep_id: str, state: str, style_status: str) -> str:
        if state == "not_accepted":
            label = f"{dep_id}:not_accepted" if self.enabled else f"{dep_id} (not_accepted)"
            return self.apply(label, self.DEPENDENCY_STATE_STYLES[state])
        if state == "missing":
            label = f"{dep_id}:missing" if self.enabled else f"{dep_id} (missing)"
            return self.apply(label, self.DEPENDENCY_STATE_STYLES[state])
        if state == "not_applied":
            label = f"{dep_id}:not_applied" if self.enabled else f"{dep_id} (not_applied)"
            return self.apply(label, self.DEPENDENCY_STATE_STYLES[state])
        if state == "blocked":
            label = dep_id if self.enabled else f"{dep_id} (blocked)"
            return self.status_label(label, style_status)
        label = dep_id if self.enabled else f"{dep_id} ({state})"
        return self.status_label(label, state)

    def dependency_title(self, title: str, state: str, style_status: str) -> str:
        label = f"{title} ({state})"
        if state == "done":
            return self.apply(label, self.DIM) if self.enabled else label
        if state in self.DEPENDENCY_STATE_STYLES:
            return self.apply(label, self.DEPENDENCY_STATE_STYLES[state])
        if state == "blocked":
            return self.status_label(label, style_status)
        return self.status_label(label, state)

    def dependency_state(self, state: str, style_status: str) -> str:
        if state == "done":
            return self.status_label(state, state)
        if state in self.DEPENDENCY_STATE_STYLES:
            return self.apply(state, self.DEPENDENCY_STATE_STYLES[state])
        if state == "blocked":
            return self.status_label(state, style_status)
        return self.status_label(state, state)

    def status(self, status: str) -> str:
        return self.status_marker(status) + self.status_label(status, status)

    def projection_status(self, projection: TaskListPresentation) -> str:
        return self.projection_marker(projection) + self.status_label(projection.kind, projection.legacy_status)

    def projection_marker(self, projection: TaskListPresentation) -> str:
        marker = projection.status_label[:2]
        if not self.enabled:
            return marker
        return self.apply(marker, self.STATUS_MARKER_STYLES.get(projection.legacy_status, self.STATUS_MARKER_NEUTRAL))

    def status_label(self, label: str, status: str) -> str:
        style = self.ACTIVE_STATUS_STYLES.get(status) or self.PASSIVE_STATUS_STYLES.get(status) or self.BG_PASSIVE_WHITE
        return self.apply(label, style)

    def status_marker(self, status: str) -> str:
        marker = self.STATUS_MARKERS.get(status, "--")
        if not self.enabled:
            return marker
        return self.apply(marker, self.STATUS_MARKER_STYLES.get(status, self.STATUS_MARKER_NEUTRAL))


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
            length += char_width(char)
    return length


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 2
    return 1


def pad_visible(value: str, width: int) -> str:
    return value + " " * max(0, width - visible_len(value))


def fit_visible(value: str, width: int) -> str:
    if visible_len(value) <= width:
        return value
    return truncate_visible(value, width)


def fit_middle_visible(value: str, width: int) -> str:
    if visible_len(value) <= width:
        return value
    if "\033[" in value:
        return fit_visible(value, width)
    if width <= 0:
        return ""
    if width <= 3:
        return value[:width]
    remaining = width - 3
    prefix = max(1, remaining // 2)
    suffix = max(1, remaining - prefix)
    if prefix + suffix >= len(value):
        return value[:width]
    return value[:prefix].rstrip() + "..." + value[-suffix:]


def fit_dependency_identifier(value: str, width: int) -> str:
    if visible_len(value) <= width:
        return value
    if "\033[" in value:
        return fit_visible(value, width)
    for separator in (" (", ":"):
        if separator in value:
            base, suffix = value.split(separator, 1)
            suffix = separator + suffix
            base_width = max(1, width - visible_len(suffix))
            if base_width >= 4:
                return fit_middle_visible(base, base_width) + suffix
    return fit_middle_visible(value, width)


def truncate_visible(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if width <= 3:
        return visible_prefix(value, width)
    return visible_prefix(value, width - 3).rstrip() + "..."


def visible_prefix(value: str, width: int) -> str:
    result = []
    visible = 0
    in_escape = False
    for char in value:
        if char == "\033":
            in_escape = True
            result.append(char)
            continue
        if in_escape:
            result.append(char)
            if char == "m":
                in_escape = False
            continue
        char_len = char_width(char)
        if visible + char_len > width:
            break
        visible += char_len
        result.append(char)
    if "\033[" in value:
        result.append(ListColor.RESET)
    return "".join(result)


def wrap_cell(value: str, width: int) -> list[str]:
    if visible_len(value) <= width:
        return [value]
    if "\033[" not in value:
        return wrap_plain_text(value, width)
    return wrap_ansi_hard(value, width)


def wrap_visible(value: str, width: int) -> list[str]:
    return wrap_cell(value, max(1, width))


def wrap_plain_text(value: str, width: int) -> list[str]:
    width = max(1, width)
    words = str(value).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif visible_len(current + " " + word) <= width:
            current += " " + word
        else:
            lines.extend(wrap_plain_word(current, width))
            current = word
    if current:
        lines.extend(wrap_plain_word(current, width))
    return lines


def wrap_plain_word(value: str, width: int) -> list[str]:
    if visible_len(value) <= width:
        return [value]
    lines = []
    current = ""
    current_width = 0
    for char in value:
        char_len = char_width(char)
        if current and current_width + char_len > width:
            lines.append(current)
            current = char
            current_width = char_len
        else:
            current += char
            current_width += char_len
    if current:
        lines.append(current)
    return lines


def wrap_ansi_hard(value: str, width: int) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    current_width = 0
    active_codes = ""
    in_escape = False
    escape = []
    last_space_index: int | None = None
    for char in value:
        if char == "\033":
            in_escape = True
            escape = [char]
            current.append(char)
            continue
        if in_escape:
            escape.append(char)
            current.append(char)
            if char == "m":
                in_escape = False
                code = "".join(escape)
                active_codes = "" if code == ListColor.RESET else code
            continue
        char_len = char_width(char)
        if current_width and current_width + char_len > width:
            if last_space_index is not None:
                line_parts = current[:last_space_index]
                remainder = current[last_space_index + 1 :]
                if active_codes:
                    line_parts.append(ListColor.RESET)
                lines.append("".join(line_parts))
                current = ([active_codes] if active_codes else []) + remainder
                current_width = visible_len("".join(remainder))
                last_space_index = None
            else:
                if active_codes:
                    current.append(ListColor.RESET)
                lines.append("".join(current))
                current = [active_codes] if active_codes else []
                current_width = 0
        if char.isspace():
            last_space_index = len(current)
        current.append(char)
        current_width += char_len
    if current:
        if active_codes and (not current or current[-1] != ListColor.RESET):
            current.append(ListColor.RESET)
        lines.append("".join(current))
    return lines or [""]


def truncate_table_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def verbose_table_cells(task: dict) -> list[str]:
    return [
        model_requirement_note(task) or "-",
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
    return (
        task.get("status") == "completed"
        and not task.get("resolution")
        and not rejected_discarded_result(task)
        and review_status(task) in {"unreviewed", "rejected", "needs_followup"}
    )


def visible_by_default(task: dict) -> bool:
    if task.get("status") == "archived":
        return False
    if task.get("status") == "completed":
        if task.get("resolution"):
            return False
        if accepted_worktree_not_applied(task):
            return True
        return needs_review(task)
    if task.get("status") in {"failed", "blocked_user"} and task.get("resolution"):
        return False
    return task.get("status") not in DEFAULT_HIDDEN_LIST_STATUSES


def accepted_worktree_not_applied(task: dict) -> bool:
    return (
        task.get("execution_mode") == "git_worktree"
        and review_status(task) == "accepted"
        and task.get("execution_apply_status") != "applied"
    )


def rejected_discarded_result(task: dict) -> bool:
    return (
        task.get("status") in {"completed", "archived"}
        and task.get("review_status") == "rejected"
        and task.get("execution_mode") == "git_worktree"
        and task.get("execution_worktree_status") == "cleaned"
        and task.get("execution_cleanup_kind") == "discard"
        and task.get("execution_cleanup_result_applied") is False
    )


RUN_LOOP_STOP_STATUSES = {"empty", "paused", "cooldown", "locked", "review_needed", "stale_finalization"}


def outcome_dict(outcome: RunOutcome) -> dict:
    return outcome.__dict__


def cmd_run_next(config: Config, args: argparse.Namespace) -> int:
    outcome = run_next(config)
    if args.json:
        print(json.dumps(outcome_dict(outcome), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_run_outcome(outcome))
    return 0


def cmd_run_loop(config: Config, args: argparse.Namespace) -> int:
    del config
    if args.max_iterations < 1:
        raise ValueError("--max-iterations must be a positive integer")

    for _ in range(args.max_iterations):
        iteration_config = Config.load(args.config)
        outcome = run_next(iteration_config, suppress_wake_hooks=True)
        if args.json:
            print(json.dumps(outcome_dict(outcome), ensure_ascii=False, sort_keys=True), flush=True)
        else:
            print(render_run_outcome(outcome), flush=True)
        if not run_loop_should_continue(outcome):
            break
    return 0


def render_run_outcome(outcome: RunOutcome) -> str:
    task_part = f" task={outcome.task_id}" if outcome.task_id else ""
    maintenance_part = ""
    if outcome.maintenance:
        maintenance_part = f" maintenance={outcome.maintenance.get('status') or 'unknown'}"
    return f"{outcome.status}: {outcome.message}{task_part}{maintenance_part}"


def run_loop_should_continue(outcome: RunOutcome) -> bool:
    if outcome.status in RUN_LOOP_STOP_STATUSES:
        return False
    return outcome.task_id is not None


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


def cmd_routing_eval_report(config: Config, args: argparse.Namespace) -> int:
    if args.limit < 0:
        print("error: --limit must be non-negative", file=sys.stderr)
        return 1
    report = build_routing_evaluation_report(
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
        print(render_routing_evaluation_report(report), end="")
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
    result = accept_task_and_integrate(config, args.task_id, args.reason, source="review")
    if result.get("task") is None:
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("accept\tlocked")
        return 1
    task = result["task"]
    post_accept = result.get("post_accept") if isinstance(result.get("post_accept"), dict) else {}
    if post_accept.get("should_wake"):
        run_post_mutation_trigger(config)
    if args.json:
        print(json.dumps({"task": task, "post_accept": post_accept}, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_review_mutation(task, "accepted", post_accept=post_accept), end="")
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


def render_review_mutation(task: dict, status: str, post_accept: dict | None = None) -> str:
    task_id = task.get("id")
    if status == "accepted":
        title = task_title(task)
        label = f"{title} ({task_id})"
    else:
        label = str(task_id)
    lines = [f"{label}\t{status}"]
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
    if post_accept and post_accept.get("status") not in {"not_worktree"}:
        lines.append("post_accept\t" + render_post_accept_status(post_accept))
    return "\n".join(lines) + "\n"


def render_post_accept_status(post_accept: dict) -> str:
    status = post_accept.get("status") or "-"
    if status == "applied":
        return "worktree applied"
    if status == "rebased_awaiting_re_review":
        return "worktree rebased; awaiting re-review"
    if status == "conflict_fix_subtask_queued":
        title = post_accept.get("conflict_fix_task_title") or "conflict-fix subtask"
        task_id = post_accept.get("conflict_fix_task_id") or "-"
        return f"{title} ({task_id}) queued"
    if status in {"not_worktree", "already_applied"}:
        return str(status)
    errors = []
    report = post_accept.get("worktree_apply") if isinstance(post_accept.get("worktree_apply"), dict) else {}
    if isinstance(report.get("errors"), list):
        errors = [str(item) for item in report.get("errors")[:2]]
    return f"{status}" + (": " + "; ".join(errors) if errors else "")


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


def cmd_pause_show(config: Config, args: argparse.Namespace) -> int:
    print(render_pause_status(get_runner_pause(config)), end="")
    return 0


def cmd_pause_set(config: Config, args: argparse.Namespace) -> int:
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire():
        raise RuntimeError("another runner is active; retry pause command")
    try:
        previous = get_runner_pause(config)
        paused_by = args.by or os.environ.get("USER") or os.environ.get("USERNAME")
        current = set_runner_pause(config, args.reason, paused_by)
    finally:
        lock.release()
    write_event_nonfatal(
        config,
        "runner_pause_updated",
        summary="runner pause set",
        payload={"action": "set", "runner_pause": current, "previous_runner_pause": previous},
    )
    print("runner pause set")
    print(f"reason: {current.get('reason') or '-'}")
    print(f"paused_at: {current.get('paused_at') or '-'}")
    print(f"paused_by: {current.get('paused_by') or '-'}")
    return 0


def cmd_pause_clear(config: Config, args: argparse.Namespace) -> int:
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire():
        raise RuntimeError("another runner is active; retry pause command")
    try:
        previous = clear_runner_pause(config)
        current = get_runner_pause(config)
    finally:
        lock.release()
    write_event_nonfatal(
        config,
        "runner_pause_updated",
        summary="runner pause cleared",
        payload={"action": "clear", "runner_pause": current, "previous_runner_pause": previous},
    )
    run_post_mutation_trigger(config)
    print("runner pause cleared")
    return 0


def render_cooldown_status(status: dict[str, object]) -> str:
    lines = [
        "global cooldown status",
        f"global_cooldown_until: {status.get('global_cooldown_until') or '-'}",
        f"active: {str(bool(status.get('active'))).lower()}",
        f"remaining: {status.get('remaining')}",
    ]
    return "\n".join(lines) + "\n"


def render_pause_status(pause: dict[str, object]) -> str:
    lines = [
        "runner pause status",
        f"active: {str(bool(pause.get('active'))).lower()}",
        f"reason: {pause.get('reason') or '-'}",
        f"paused_at: {pause.get('paused_at') or '-'}",
        f"paused_by: {pause.get('paused_by') or '-'}",
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


def cmd_index_rebuild(config: Config, args: argparse.Namespace) -> int:
    report = build_rebuild_report(config, apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_rebuild_report(report), end="")
    return 0 if report["ok"] else 1


def cmd_index_status(config: Config, args: argparse.Namespace) -> int:
    report = build_status_report(config)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_status_report(report), end="")
    return 0


def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    report = build_doctor_report(config)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_doctor_report(report), end="")
    return 0 if report["ok"] else 1


def cmd_maintenance_codex_cli(config: Config, args: argparse.Namespace) -> int:
    report = run_codex_cli_maintenance(config) if args.apply else build_codex_cli_maintenance_report(config)
    if args.json:
        print(dump_json(report), end="")
    else:
        print(render_codex_cli_maintenance_report(report), end="")
    return 0 if report.get("status") in {"ready", "succeeded"} else 1


def cmd_maintenance_direct_worktrees(config: Config, args: argparse.Namespace) -> int:
    report = build_direct_worktrees_report(config, apply=args.apply, repo_root=args.repo_root)
    if args.json:
        print(dump_json(report), end="")
    else:
        print(render_direct_worktrees_report(report), end="")
    if report.get("errors"):
        return 1
    if args.apply and report.get("status") in {"failed", "partial"}:
        return 1
    return 0


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


def cmd_worktree_branch_prune(config: Config, args: argparse.Namespace) -> int:
    report = build_branch_prune_report(config, args.task_id, apply=args.apply)
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

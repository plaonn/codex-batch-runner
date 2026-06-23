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
from pathlib import Path

from .apply_plan import apply_queue_mutation_plan, build_apply_plan_report, render_apply_plan_report
from .config import Config
from .cooldown import MANUAL_COOLDOWN_SAFETY_OFFSET_SECONDS, cooldown_status, format_duration, parse_manual_cooldown
from .doctor import build_doctor_report, render_doctor_report
from .events import DEFAULT_EVENT_LIMIT, list_events, render_events_human, write_event_nonfatal
from .execution_profiles import config_overrides_value
from .evidence import list_rate_limit_evidence
from .follow import DEFAULT_INITIAL_LINES, DEFAULT_POLL_INTERVAL_SECONDS, FollowOptions, follow_task
from .lock import FileLock
from .prune import DEFAULT_PRUNE_AGE_DAYS, build_prune_report
from .post_accept import accept_task_and_integrate
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
from .review_next import build_review_next_apply_report, build_review_next_report, render_review_next_report
from .routing_report import DEFAULT_ROUTING_REPORT_LIMIT, build_routing_report, render_routing_report
from .runner import run_next
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
from .worktree import build_apply_report, build_cleanup_report, build_prepare_report, render_worktree_report, task_worktree_metadata

WATCH_RESTART_MESSAGE = "cbr source changed since this watch started; restart watch to use updated code"
COMPACT_TABLE_MIN_NOTE_WIDTH = 28
COMPACT_TABLE_MIN_ID_WIDTH = 18
COMPACT_TABLE_MIN_DEPS_WIDTH = 10


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

    pause = sub.add_parser("pause", help="show, set, or clear global runner admission pause")
    pause_sub = pause.add_subparsers(dest="pause_command", required=True)
    pause_show = pause_sub.add_parser("show", help="show runner pause status")
    pause_show.set_defaults(func=cmd_pause_show)
    pause_set = pause_sub.add_parser("set", help="set runner pause without expiry")
    pause_set.add_argument("--reason", required=True, help="public-safe reason for pausing new runner admissions")
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


def parse_config_overrides(items: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError("--config-override must use KEY=VALUE")
        key, value = item.split("=", 1)
        overrides[key] = value
    return config_overrides_value("codex_config_overrides", overrides)


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
        tasks = default_visible_tasks_with_subtasks(tasks, by_id)
    tasks.sort(key=list_sort_key)
    if args.json:
        return json.dumps(tasks, ensure_ascii=False, indent=2, sort_keys=True)
    color = list_colorizer(args.color)
    banners = list_cooldown_banners(config)
    if args.graph:
        output = render_dependency_graph(tasks, by_id, config, color)
        return "\n".join([*banners, output]) if banners else output
    if not args.verbose:
        output = render_compact_list(tasks, by_id, config, color, terminal_width=terminal_width)
        return "\n".join([*banners, output]) if banners else output
    header = ["ID", "TITLE", "STATUS", "PROJECT", "ATTEMPTS", "DEPS", "NOTE"]
    header.append("PROFILE")
    header.extend(["RAW_STATUS", "LAST_RESULT", "LAST_RUN", "LAST_ERROR"])
    rows = []
    for task in tasks:
        row = list_table_row(task, by_id, config)
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


def default_visible_tasks_with_subtasks(tasks: list[dict], by_id: dict[str, dict]) -> list[dict]:
    visible_ids = {str(task.get("id")) for task in tasks if task.get("id") and visible_by_default(task)}
    changed = True
    while changed:
        changed = False
        for task in tasks:
            task_id = str(task.get("id") or "")
            if not task_id or task_id in visible_ids or task.get("status") == "archived" or task.get("resolution"):
                continue
            if has_visible_parent(task, visible_ids, by_id):
                visible_ids.add(task_id)
                changed = True
    return [task for task in tasks if str(task.get("id") or "") in visible_ids]


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


def render_visible_table(header: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(visible_len(row[index]) for row in [header, *rows])
        for index in range(len(header))
    ]
    return "\n".join(render_compact_row(row, widths) for row in [header, *rows])


def render_dependency_graph(tasks: list[dict], by_id: dict[str, dict], config: Config, color: "ListColor") -> str:
    header = ["PROJECT", "TASK", "STATUS", "WAITS_FOR", "DEP_STATE", "TASK_TITLE", "DEP_TITLE"]
    rows: list[list[str]] = []
    for task in tasks:
        deps = task.get("depends_on")
        dep_ids = [str(dep_id) for dep_id in deps if str(dep_id)] if isinstance(deps, list) else []
        if not dep_ids:
            rows.append(dependency_graph_row(task, "-", "none", "", "-", by_id, config, color))
            continue
        for dep_id in dep_ids:
            dep = by_id.get(dep_id)
            dep_state, dep_style_status = dependency_display_state(dep, by_id, config)
            dep_title = task_title(dep) if dep else "-"
            rows.append(dependency_graph_row(task, dep_id, dep_state, dep_style_status, dep_title, by_id, config, color))
    return render_visible_table(header, rows)


def dependency_graph_row(
    task: dict,
    dep_id: str,
    dep_state: str,
    dep_style_status: str,
    dep_title: str,
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
) -> list[str]:
    cells = task_display_cells(task, by_id, config, color)
    return [
        cells["project"],
        cells["id"],
        cells["status"],
        color.task_id(dep_id),
        color.dependency_state(dep_state, dep_style_status),
        cells["title"],
        scalar_cell(dep_title),
    ]


def render_compact_list(
    tasks: list[dict],
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
    terminal_width: int | None = None,
) -> str:
    header = ["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"]
    project_groups = compact_project_groups(tasks, by_id, config, color)
    row_groups = [group for _, groups in project_groups for group in groups]
    if terminal_width is not None and terminal_width < 80:
        return render_compact_block_list(project_groups, terminal_width)
    widths = compact_widths(header, row_groups, terminal_width)
    lines = [render_compact_row(header, widths)]
    for project, groups in project_groups:
        lines.append(project_section_header(project, terminal_width))
        for group in groups:
            lines.extend(render_compact_group(group, widths, terminal_width))
    return "\n".join(lines)


def compact_project_groups(
    tasks: list[dict],
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
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
                compact_task_group(item["task"], by_id, config, color, tree_prefix=item["prefix"])
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
        parent_id = compact_parent_task_id(task, visible_ids, by_id)
        if parent_id and parent_id != task_id:
            children.setdefault(parent_id, []).append(task)
        else:
            roots.append(task)

    def sort_key(task: dict) -> tuple[str, str]:
        return list_sort_key(task)

    for task_id in children:
        children[task_id].sort(key=sort_key)
    roots.sort(key=sort_key)

    items: list[dict[str, object]] = []

    def visit(task: dict, ancestors: list[bool], seen: set[str]) -> None:
        task_id = str(task.get("id") or "")
        prefix = tree_prefix(ancestors) if ancestors else ""
        items.append({"task": task, "prefix": prefix})
        if not task_id or task_id in seen:
            return
        child_tasks = children.get(task_id, [])
        for index, child in enumerate(child_tasks):
            visit(child, [*ancestors, index == len(child_tasks) - 1], {*seen, task_id})

    for root in roots:
        visit(root, [], set())
    attached = {str(item["task"].get("id") or "") for item in items if isinstance(item.get("task"), dict)}
    for task in sorted(tasks, key=sort_key):
        task_id = str(task.get("id") or "")
        if task_id not in attached:
            visit(task, [], set())
    return items


def compact_parent_task_id(task: dict, visible_ids: set[str], by_id: dict[str, dict]) -> str:
    for key in ("parent_task_id", "subtask_for", "root_task_id"):
        value = task.get(key)
        if value is not None and str(value) in visible_ids:
            return str(value)
    task_id = str(task.get("id") or "")
    for parent_id in visible_ids:
        parent = by_id.get(parent_id)
        if parent and task_id in blocking_subtask_ids(parent):
            return parent_id
    return ""


def tree_prefix(ancestors: list[bool]) -> str:
    parts = []
    for is_last in ancestors[:-1]:
        parts.append("    " if is_last else "|   ")
    parts.append("`-- " if ancestors[-1] else "|-- ")
    return "".join(parts)


def compact_task_group(
    task: dict,
    by_id: dict[str, dict],
    config: Config,
    color: "ListColor",
    tree_prefix: str = "",
) -> dict[str, object]:
    cells = task_display_cells(task, by_id, config, color)
    dep_ids = dependency_id_cells(task.get("depends_on"), by_id, config, color)
    note_segments = note_cells(task, by_id, config)
    return {
        "summary": [
            cells["project"],
            cells["id"],
            cells["status"],
            cells["attempts"],
        ],
        "deps": dep_ids or ["-"],
        "notes": note_segments or ["-"],
        "title": color.title(tree_prefix + cells["title"]),
    }


def task_display_cells(task: dict, by_id: dict[str, dict], config: Config, color: "ListColor") -> dict[str, str]:
    return {
        "project": color.project(scalar_cell(task_project_id(task))),
        "id": color.task_id(scalar_cell(task.get("id"))),
        "status": color.status(status_cell(task, by_id, config)),
        "attempts": scalar_cell(task.get("attempts", 0)),
        "title": compact_title(task),
    }


def project_section_header(project: str, terminal_width: int | None) -> str:
    label = f"[{project}]"
    if terminal_width is None:
        return label
    return fit_visible(label, max(1, terminal_width))


def compact_widths(header: list[str], row_groups: list[dict[str, object]], terminal_width: int | None) -> list[int]:
    summary_rows = [group["summary"] for group in row_groups]
    deps = [cell for group in row_groups for cell in group["deps"]]  # type: ignore[index]
    notes = [cell for group in row_groups for cell in group["notes"]]  # type: ignore[index]
    project = column_width(header[0], summary_rows, 0, cap=18 if terminal_width else None)
    task_id = column_width(header[1], summary_rows, 1, cap=36 if terminal_width else None)
    status = column_width(header[2], summary_rows, 2)
    attempts = column_width(header[3], summary_rows, 3)
    dep_width = max([visible_len(header[4]), *(visible_len(str(dep)) for dep in deps)] or [len(header[4])])
    note_width = max([visible_len(header[5]), *(visible_len(str(note)) for note in notes)] or [len(header[5])])
    widths = [project, task_id, status, attempts, dep_width, note_width]
    if terminal_width is None:
        return widths
    fixed = project + status + attempts + (len(widths) - 1) * 2
    available = max(10, terminal_width - fixed)
    min_id = min(task_id, max(visible_len(header[1]), COMPACT_TABLE_MIN_ID_WIDTH))
    min_deps = min(dep_width, max(visible_len(header[4]), COMPACT_TABLE_MIN_DEPS_WIDTH))
    min_note = min(note_width, max(visible_len(header[5]), COMPACT_TABLE_MIN_NOTE_WIDTH))
    if available < min_id + min_deps + min_note:
        min_note = max(visible_len(header[5]), available - min_id - min_deps)
    note_share = min(note_width, max(visible_len(header[5]), min_note))
    remaining = max(0, available - note_share)
    deps_share = min(dep_width, max(min_deps, min(24, remaining // 2 if remaining >= 2 else remaining)))
    id_share = min(task_id, max(visible_len(header[1]), remaining - deps_share))
    if id_share < min_id and deps_share > min_deps:
        take = min(min_id - id_share, deps_share - min_deps)
        id_share += take
        deps_share -= take
    if deps_share < min_deps and id_share > min_id:
        take = min(min_deps - deps_share, id_share - min_id)
        deps_share += take
        id_share -= take
    if id_share + deps_share + note_share < available:
        note_share = min(note_width, note_share + available - id_share - deps_share - note_share)
    widths[1] = max(visible_len(header[1]), id_share)
    widths[4] = max(visible_len(header[4]), deps_share)
    widths[5] = max(visible_len(header[5]), available - widths[1] - widths[4])
    return widths


def column_width(header: str, rows: list[object], index: int, cap: int | None = None) -> int:
    values = [visible_len(header)]
    for row in rows:
        if isinstance(row, list) and index < len(row):
            values.append(visible_len(str(row[index])))
    width = max(values)
    return min(width, cap) if cap else width


def render_compact_group(group: dict[str, object], widths: list[int], terminal_width: int | None) -> list[str]:
    deps = group["deps"] if isinstance(group["deps"], list) else ["-"]
    notes = group["notes"] if isinstance(group["notes"], list) else ["-"]
    dep_lines = wrap_cell_list([fit_dependency_identifier(str(dep), widths[4]) for dep in deps], widths[4])
    note_lines = wrap_cell_list([str(note) for note in notes], widths[5])
    summary = group["summary"] if isinstance(group["summary"], list) else ["", "", "", ""]
    first = [
        fit_visible(str(summary[0]), widths[0]),
        fit_middle_visible(str(summary[1]), widths[1]),
        fit_visible(str(summary[2]), widths[2]),
        fit_visible(str(summary[3]), widths[3]),
        dep_lines[0] if dep_lines else "-",
        note_lines[0] if note_lines else "-",
    ]
    title_cell_width = sum(widths[:4]) + 6
    title_width = max(10, title_cell_width - 2)
    title_lines = ["  " + line for line in wrap_plain_text(str(group.get("title") or "-"), title_width)]
    lines = [render_compact_row(first, widths)]
    row_count = max(len(title_lines), len(dep_lines) - 1, len(note_lines) - 1)
    detail_widths = [title_cell_width, widths[4], widths[5]]
    for index in range(row_count):
        lines.append(
            render_compact_row(
                [
                    title_lines[index] if index < len(title_lines) else "",
                    dep_lines[index + 1] if index + 1 < len(dep_lines) else "",
                    note_lines[index + 1] if index + 1 < len(note_lines) else "",
                ],
                detail_widths,
            )
        )
    return lines


def render_compact_block_list(project_groups: list[tuple[str, list[dict[str, object]]]], terminal_width: int) -> str:
    lines = []
    for project, row_groups in project_groups:
        if lines:
            lines.append("")
        lines.append(f"[{fit_visible(project, max(1, terminal_width - 2))}]")
        for index, group in enumerate(row_groups):
            if index:
                lines.append("")
            summary = group["summary"] if isinstance(group["summary"], list) else ["-", "-", "-", "-"]
            block_rows = [
                ("STATUS", str(summary[2])),
                ("ID", str(summary[1])),
                ("PROJECT", str(summary[0])),
                ("TITLE", str(group.get("title") or "-")),
            ]
            deps = group["deps"] if isinstance(group["deps"], list) else ["-"]
            notes = group["notes"] if isinstance(group["notes"], list) else ["-"]
            block_rows.extend(("DEPS", str(dep)) for dep in deps)
            block_rows.extend(("NOTE", str(note)) for note in notes)
            lines.extend(render_block_rows(block_rows, terminal_width))
    return "\n".join(lines)


def render_block_rows(rows: list[tuple[str, str]], terminal_width: int) -> list[str]:
    label_width = max(visible_len(label) for label, _ in rows)
    value_width = max(8, terminal_width - label_width - 2)
    lines = []
    for label, value in rows:
        wrapped = wrap_visible(value, value_width)
        for index, line in enumerate(wrapped):
            prefix = (label + ":").ljust(label_width + 1) + " " if index == 0 else " " * (label_width + 2)
            lines.append(prefix + line)
    return lines


def wrap_cell_list(values: list[str], width: int) -> list[str]:
    lines: list[str] = []
    for value in values:
        lines.extend(wrap_visible(value, width))
    return lines or [""]


def render_compact_row(row: list[str], widths: list[int]) -> str:
    padded = [pad_visible(cell, widths[index]) for index, cell in enumerate(row[:-1])]
    return "  ".join([*padded, row[-1]])


def list_table_row(task: dict, by_id: dict[str, dict], config: Config) -> list[str]:
    return [
        scalar_cell(task.get("id")),
        scalar_cell(truncate_table_text(compact_title(task), 72)),
        scalar_cell(status_cell(task, by_id, config)),
        scalar_cell(task_project_id(task)),
        scalar_cell(task.get("attempts", 0)),
        deps_cell(task.get("depends_on"), by_id, config),
        note_cell(task, by_id, config),
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


def dependency_id_cells(depends_on: object, by_id: dict[str, dict], config: Config, color: "ListColor") -> list[str]:
    if not isinstance(depends_on, list):
        return []
    return [dependency_id_cell(str(dep_id), by_id, config, color) for dep_id in depends_on]


def dependency_id_cell(dep_id: str, by_id: dict[str, dict], config: Config, color: "ListColor") -> str:
    dep = by_id.get(dep_id)
    state, style_status = dependency_display_state(dep, by_id, config)
    if state == "done":
        return color.satisfied_dependency(dep_id)
    return color.dependency(dep_id, state, style_status)


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
    if task.get("resolution") and status in {"failed", "blocked_user", "completed"}:
        return "resolved"
    if by_id is not None and config is not None:
        subtask_status = blocking_subtask_effective_status(task, by_id, config)
        if subtask_status:
            return subtask_status
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
        if review == "accepted" and accepted_worktree_not_applied(task):
            return "accepted_unapplied"
    return status


def note_cell(task: dict, by_id: dict[str, dict], config: Config) -> str:
    notes = note_cells(task, by_id, config)
    return "; ".join(notes) if notes else "-"


def note_cells(task: dict, by_id: dict[str, dict], config: Config) -> list[str]:
    notes = []
    capacity = []
    if task.get("status") in RUNNABLE_STATUSES:
        capacity = capacity_blockers(config, task)
        if capacity:
            notes.append("capacity blocked: " + ",".join(capacity))
    if is_in_cooldown(task):
        notes.append(cooldown_note(task))
    elif task.get("status") == "needs_resume" and not capacity and dependency_status(
        task,
        by_id,
        require_accepted_review=config.dependency_requires_accepted_review,
    )[0]:
        notes.append("resume ready")
    if startup_stalled(task):
        notes.append(startup_stall_note(task))
    if task.get("status") == "failed" and task.get("last_error"):
        notes.append("last error: " + one_line(task.get("last_error")))
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
        notes.append("resolution: " + str(task.get("resolution")))
    if task.get("status") == "completed" and not task.get("resolution"):
        review = review_status(task)
        if review == "unreviewed":
            notes.append("awaiting review")
        elif review == "rejected":
            notes.append("review failed")
        elif review == "needs_followup":
            notes.append("needs follow-up")
        elif review == "reviewing":
            notes.append("reviewing")
        notes.extend(completed_timing_notes(task))
        chain_note = chain_note_cell(task)
        if chain_note:
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
        notes.append(f"completed {format_elapsed(seconds)} ago")
    duration = task_duration_seconds(task)
    if duration is not None:
        notes.append(f"duration {format_elapsed(duration)}")
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
    return "scheduling: " + ", ".join(fields) if fields else ""


def worktree_apply_note(task: dict) -> str:
    if task.get("execution_mode") != "git_worktree":
        return ""
    if task.get("execution_conflict_fix_status") == "queued" and task.get("execution_conflict_fix_task_id"):
        return "conflict-fix subtask queued"
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
    if task.get("execution_profile"):
        parts.append("profile=" + one_line(task.get("execution_profile")))
    if task.get("model"):
        parts.append("model=" + one_line(task.get("model")))
    if task.get("codex_profile"):
        parts.append("codex_profile=" + one_line(task.get("codex_profile")))
    return " ".join(parts)


def execution_backend_note(task: dict) -> str:
    if task.get("execution_backend") and task.get("execution_backend") != "codex":
        return "backend=" + one_line(task.get("execution_backend"))
    return ""


def compact_title(task: dict) -> str:
    marker = execution_profile_marker(task)
    title = task_title(task)
    return f"{marker} {title}" if marker else title


def execution_profile_marker(task: dict) -> str:
    profile = str(task.get("execution_profile") or "").strip().lower()
    budget = str(task.get("token_budget_hint") or "").strip().lower()
    model = str(task.get("model") or "").strip().lower()
    codex_profile = str(task.get("codex_profile") or "").strip().lower()
    values = " ".join(value for value in [profile, budget, model, codex_profile] if value)
    if not values:
        return ""
    if any(term in values for term in ("small", "light", "low-cost", "low_cost", "lite")):
        return "[S]"
    if any(term in values for term in ("deep", "high-cost", "high_cost", "large", "max")):
        return "[D]"
    return ""


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


def subtask_note_cell(task: dict, by_id: dict[str, dict] | None = None) -> str:
    subtask_type = task.get("subtask_type")
    subtask_for = task.get("subtask_for")
    subtask_for_label = str(subtask_for or "")
    if subtask_for and by_id and by_id.get(str(subtask_for)):
        subtask_for_label = f"{dependency_label(str(subtask_for), by_id)} ({subtask_for})"
    if subtask_type and subtask_for:
        return f"subtask {subtask_type} for {subtask_for_label}"
    if subtask_type:
        return f"subtask {subtask_type}"
    return ""


def blocking_subtask_effective_status(task: dict, by_id: dict[str, dict], config: Config) -> str:
    active = active_blocking_subtasks(task, by_id)
    if not active:
        return ""
    statuses = [blocking_subtask_status(item, by_id, config) for item in active]
    if any(status in {"missing", "failed", "blocked_user", "review_failed", "needs_followup", "subtasks_blocked"} for status in statuses):
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
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items())[:2])
    timing = active_subtask_timing_summary(active, by_id, config)
    note = f"subtasks {len(active)}/{len(ids)} {summary}"
    return f"{note}, {timing}" if timing else note


def active_subtask_timing_summary(active: list[dict | None], by_id: dict[str, dict], config: Config) -> str:
    status_rows = [(task, blocking_subtask_status(task, by_id, config)) for task in active if task]
    blocked = [
        task
        for task, status in status_rows
        if status in {"failed", "blocked_user", "review_failed", "needs_followup", "subtasks_blocked"}
    ]
    if blocked:
        return oldest_age_note(blocked, ("completed_at", "updated_at", "started_at"), "blocked")
    running = [task for task, status in status_rows if status == "running"]
    if running:
        return oldest_age_note(running, ("started_at", "updated_at"), "running")
    review = [task for task, status in status_rows if status in {"awaiting_review", "reviewing"}]
    if review:
        return oldest_age_note(review, ("completed_at", "updated_at", "started_at"), "awaiting review")
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
    status = str(task.get("status") or "-")
    if (
        status in RUNNABLE_STATUSES
        and not dependency_status(
            task,
            by_id,
            require_accepted_review=config.dependency_requires_accepted_review,
        )[0]
    ):
        return "blocked_dependency"
    if task.get("resolution") and status in {"failed", "blocked_user", "completed"}:
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
        return "startup stall retry evidence"
    return "startup stall history"


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
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    LIGHT_CYAN = "\033[96m"
    BLUE = "\033[34m"
    BG_RED = "\033[101;30m"
    BG_YELLOW = "\033[103;30m"
    BG_GREEN = "\033[102;30m"
    BG_CYAN = "\033[106;30m"
    BG_BLUE = "\033[104;30m"
    BG_DIM = "\033[100;37m"
    BG_NEUTRAL_CYAN = "\033[100;96m"
    BG_NEUTRAL_YELLOW = "\033[100;93m"
    BG_NEUTRAL_GREEN = "\033[100;92m"
    BG_NEUTRAL_WHITE = "\033[100;37m"
    ID_COLORS = ("\033[35m", "\033[36m", "\033[34m", "\033[32m", "\033[33m", "\033[91m")
    ACTIVE_STATUS_STYLES = {
        "running": BG_CYAN,
        "awaiting_review": BG_YELLOW,
        "reviewing": BG_YELLOW,
        "needs_resume": BG_BLUE,
        "waiting_subtasks": BG_YELLOW,
        "cooldown": BG_DIM,
        "usage_exhausted": BG_DIM,
        "failed": BG_RED,
        "review_failed": BG_RED,
        "needs_followup": BG_RED,
        "blocked_user": BG_RED,
        "subtasks_blocked": BG_RED,
    }
    PASSIVE_STATUS_STYLES = {
        "runnable": BG_NEUTRAL_CYAN,
        "blocked_dependency": BG_NEUTRAL_YELLOW,
        "completed": BG_NEUTRAL_GREEN,
        "accepted": BG_NEUTRAL_GREEN,
        "resolved": BG_NEUTRAL_WHITE,
        "archived": BG_NEUTRAL_WHITE,
    }
    DEPENDENCY_STATE_STYLES = {
        "missing": BG_RED,
        "not_accepted": BG_YELLOW,
        "not_applied": BG_YELLOW,
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

    def project(self, project_id: str) -> str:
        return self.apply(project_id, self.LIGHT_CYAN)

    def title(self, title: str) -> str:
        return title

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

    def dependency_state(self, state: str, style_status: str) -> str:
        if state == "none":
            return state
        if state == "done":
            return self.status_label(state, "completed")
        if state == "blocked":
            return self.status_label(state, style_status)
        style = self.DEPENDENCY_STATE_STYLES.get(state)
        return self.apply(state, style) if style else state

    def status(self, status: str) -> str:
        return self.status_label(status, status)

    def status_label(self, label: str, status: str) -> str:
        style = self.ACTIVE_STATUS_STYLES.get(status) or self.PASSIVE_STATUS_STYLES.get(status)
        return self.apply(label, style) if style else label


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
            if active_codes:
                current.append(ListColor.RESET)
            lines.append("".join(current))
            current = [active_codes] if active_codes else []
            current_width = 0
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
        execution_profile_note(task) or "-",
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

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .apply_plan import build_apply_plan_report, render_apply_plan_report
from .config import Config
from .doctor import build_doctor_report, render_doctor_report
from .events import DEFAULT_EVENT_LIMIT, list_events, render_events_human
from .evidence import list_rate_limit_evidence
from .follow import DEFAULT_INITIAL_LINES, DEFAULT_POLL_INTERVAL_SECONDS, FollowOptions, follow_task
from .prune import DEFAULT_PRUNE_AGE_DAYS, build_prune_report
from .queue import (
    DEFAULT_HIDDEN_LIST_STATUSES,
    RESOLUTIONS,
    archive_task,
    create_task,
    dependency_status,
    is_in_cooldown,
    list_tasks,
    load_task,
    set_resolution,
    set_review_status,
    task_labels,
    task_project_id,
    task_project_root,
)
from .review_bundle import build_review_bundle, render_review_bundle
from .review_next import build_review_next_report, render_review_next_report
from .runner import run_next
from .state import load_state
from .summary import render_task_summary
from .transcript import render_task_transcript
from .triggers import run_post_mutation_trigger


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
    enqueue.add_argument("--cwd", required=True, help="working directory for Codex")
    prompt_group = enqueue.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="task prompt")
    prompt_group.add_argument("--prompt-file", help="file containing task prompt")
    enqueue.add_argument("--id", dest="task_id", help="explicit task id")
    enqueue.add_argument("--depends-on", action="append", default=[], help="dependency task id, repeatable")
    enqueue.add_argument("--project", dest="project_id", help="project identifier")
    enqueue.add_argument("--category", help="task category")
    enqueue.add_argument("--label", action="append", default=[], help="task label, repeatable")
    enqueue.add_argument("--created-by", help="task creator")
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

    review_next = sub.add_parser("review-next", help="dry-run review report for the next completed task needing review")
    review_next.add_argument("--dry-run", action="store_true", help="report only; required because auto-apply is not implemented")
    review_next.add_argument("--project", dest="project_id", help="filter by project id")
    review_next.add_argument("--project-root", help="filter by project root")
    review_next.add_argument("--category", help="filter by category")
    review_next.add_argument("--label", help="filter by label")
    review_next.add_argument("--json", action="store_true", help="print JSON")
    review_next.set_defaults(func=cmd_review_next)

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
    prune.add_argument("--json", action="store_true", help="print JSON")
    prune.set_defaults(func=cmd_prune)

    apply_plan = sub.add_parser("apply-plan", help="validate or apply a queue mutation plan")
    apply_plan.add_argument("plan_path", help="queue plan JSON path")
    apply_plan.add_argument("--dry-run", action="store_true", help="validate and report without queue mutations")
    apply_plan.add_argument("--json", action="store_true", help="print JSON")
    apply_plan.set_defaults(func=cmd_apply_plan)
    return parser


def cmd_enqueue(config: Config, args: argparse.Namespace) -> int:
    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
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
    )
    run_post_mutation_trigger(config)
    print(task["id"])
    return 0


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
    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    header = ["ID", "STATUS", "PROJECT", "ATTEMPTS", "DEPS", "FLAGS"]
    if args.verbose:
        header.extend(["LAST_RESULT", "LAST_RUN", "LAST_ERROR"])
    print("\t".join(header))
    for task in tasks:
        row = list_table_row(task, by_id)
        if args.verbose:
            row.extend(verbose_table_cells(task))
        print("\t".join(row))
    return 0


def list_table_row(task: dict, by_id: dict[str, dict]) -> list[str]:
    return [
        scalar_cell(task.get("id")),
        scalar_cell(task.get("status")),
        scalar_cell(task_project_id(task)),
        scalar_cell(task.get("attempts", 0)),
        deps_cell(task.get("depends_on")),
        flags_cell(task, by_id),
    ]


def scalar_cell(value: object) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def deps_cell(depends_on: object) -> str:
    if not isinstance(depends_on, list) or not depends_on:
        return "-"
    return ",".join(str(dep_id) for dep_id in depends_on)


def flags_cell(task: dict, by_id: dict[str, dict]) -> str:
    deps_ready, blocked_by = dependency_status(task, by_id)
    flags = []
    if is_in_cooldown(task):
        flags.append("cooldown")
    if not deps_ready:
        flags.append("blocked_by=" + ",".join(blocked_by))
    if task.get("status") == "failed" and task.get("last_error"):
        flags.append("last_error=" + one_line(task.get("last_error")))
    if task.get("resolution"):
        flags.append("resolution=" + str(task.get("resolution")))
    if task.get("status") == "completed":
        flags.append("review=" + review_status(task))
    return " ".join(flags) if flags else "-"


def verbose_table_cells(task: dict) -> list[str]:
    return [
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
    if "returncode" in last_run and last_run.get("returncode") is not None:
        parts.append("returncode=" + one_line(last_run.get("returncode")))
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
    print(render_task_summary(task, by_id=by_id), end="")
    return 0


def cmd_review_bundle(config: Config, args: argparse.Namespace) -> int:
    task = load_task(config, args.task_id)
    by_id = {item.get("id"): item for item in list_tasks(config)}
    bundle = build_review_bundle(task, by_id=by_id)
    if args.json:
        print(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(render_review_bundle(bundle), end="")
    return 0


def cmd_review_next(config: Config, args: argparse.Namespace) -> int:
    if not args.dry_run:
        print("error: auto-apply is not implemented yet; rerun with --dry-run", file=sys.stderr)
        return 1
    report = build_review_next_report(config, args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_review_next_report(report), end="")
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
        print(f"{task.get('id')}\taccepted")
    return 0


def cmd_reject(config: Config, args: argparse.Namespace) -> int:
    status = "needs_followup" if args.follow_up else "rejected"
    task = set_review_status(config, args.task_id, status, args.reason)
    run_post_mutation_trigger(config)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{task.get('id')}\t{status}")
    return 0


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
    report = build_prune_report(config, age_days=args.older_than_days, apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_prune_report(report), end="")
    return 0


def cmd_apply_plan(config: Config, args: argparse.Namespace) -> int:
    if not args.dry_run:
        print("error: apply mode is not implemented yet; rerun with --dry-run", file=sys.stderr)
        return 1
    report = build_apply_plan_report(config, args.plan_path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_apply_plan_report(report), end="")
    return 0 if report["ok"] else 1


def render_prune_report(report: dict) -> str:
    lines = [
        f"mode: {report['mode']}",
        f"older_than_days: {report['age_days']}",
        f"candidates: {report['candidate_count']}",
        f"task_candidates: {report.get('task_candidate_count', len(report['candidates']))}",
        f"event_candidates: {report.get('event_candidate_count', len(report.get('event_candidates', [])))}",
        f"deleted_files: {report['deleted_files']}",
    ]
    if report["candidates"]:
        lines.append("task/log candidates:")
    for candidate in report["candidates"]:
        lines.append(f"{candidate['task_id']}\t{candidate['reason']}\t{candidate['timestamp']}")
        for file in candidate["files"]:
            flags = []
            if file["deleted"]:
                flags.append("deleted")
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

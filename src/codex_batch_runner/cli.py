from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .evidence import list_rate_limit_evidence
from .queue import (
    DEFAULT_HIDDEN_LIST_STATUSES,
    archive_task,
    create_task,
    dependency_status,
    is_in_cooldown,
    list_tasks,
    load_task,
    set_review_status,
    task_labels,
    task_project_id,
    task_project_root,
)
from .runner import run_next
from .state import load_state
from .transcript import render_task_transcript


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
    list_cmd.add_argument("--json", action="store_true", help="print JSON")
    list_cmd.set_defaults(func=cmd_list)

    run_cmd = sub.add_parser("run-next", help="run one eligible task")
    run_cmd.add_argument("--json", action="store_true", help="print JSON")
    run_cmd.set_defaults(func=cmd_run_next)

    show = sub.add_parser("show", help="show a task")
    show.add_argument("task_id")
    show.add_argument("--json", action="store_true", help="print raw JSON")
    show.set_defaults(func=cmd_show)

    logs = sub.add_parser("logs", help="show task log paths or log contents")
    logs.add_argument("task_id")
    logs.add_argument("--cat", action="store_true", help="print log contents")
    logs.set_defaults(func=cmd_logs)

    transcript = sub.add_parser("transcript", help="show a readable task transcript")
    transcript.add_argument("task_id")
    transcript.add_argument("--raw", action="store_true", help="print raw JSONL logs")
    transcript.set_defaults(func=cmd_transcript)

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

    state = sub.add_parser("state", help="show runner state")
    state.set_defaults(func=cmd_state)

    rate_limits = sub.add_parser("rate-limits", help="list sanitized rate-limit evidence")
    rate_limits.add_argument("--json", action="store_true", help="print JSON")
    rate_limits.set_defaults(func=cmd_rate_limits)
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
    for task in tasks:
        deps_ready, blocked_by = dependency_status(task, by_id)
        flags = []
        if is_in_cooldown(task):
            flags.append("cooldown")
        if not deps_ready:
            flags.append("blocked_by=" + ",".join(blocked_by))
        if task.get("status") == "failed" and task.get("last_error"):
            flags.append("last_error=" + one_line(task.get("last_error")))
        if task.get("status") == "completed":
            flags.append("review=" + review_status(task))
        suffix = f" [{' '.join(flags)}]" if flags else ""
        print(f"{task.get('id')}\t{task.get('status')}\tattempts={task.get('attempts', 0)}{suffix}")
    return 0


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


def cmd_accept(config: Config, args: argparse.Namespace) -> int:
    task = set_review_status(config, args.task_id, "accepted", args.reason)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{task.get('id')}\taccepted")
    return 0


def cmd_reject(config: Config, args: argparse.Namespace) -> int:
    status = "needs_followup" if args.follow_up else "rejected"
    task = set_review_status(config, args.task_id, status, args.reason)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{task.get('id')}\t{status}")
    return 0


def cmd_archive(config: Config, args: argparse.Namespace) -> int:
    task = archive_task(config, args.task_id)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{task.get('id')}\tarchived")
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


if __name__ == "__main__":
    raise SystemExit(main())

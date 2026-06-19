from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .queue import create_task, dependency_status, is_in_cooldown, list_tasks, load_task
from .runner import run_next
from .state import load_state


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
    enqueue.set_defaults(func=cmd_enqueue)

    list_cmd = sub.add_parser("list", help="list tasks")
    list_cmd.add_argument("--status", help="filter by status")
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

    state = sub.add_parser("state", help="show runner state")
    state.set_defaults(func=cmd_state)
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
    )
    print(task["id"])
    return 0


def cmd_list(config: Config, args: argparse.Namespace) -> int:
    tasks = list_tasks(config)
    by_id = {task.get("id"): task for task in tasks}
    if args.status:
        tasks = [task for task in tasks if task.get("status") == args.status]
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
        suffix = f" [{' '.join(flags)}]" if flags else ""
        print(f"{task.get('id')}\t{task.get('status')}\tattempts={task.get('attempts', 0)}{suffix}")
    return 0


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


def cmd_state(config: Config, args: argparse.Namespace) -> int:
    print(json.dumps(load_state(config), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

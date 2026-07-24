from __future__ import annotations

import argparse
from collections.abc import Iterator
from types import ModuleType
from typing import Any

from .config import Config
from .post_accept import acceptance_eligibility_error
from .queue import archive_gate_result, is_resolvable_task, list_tasks_read_only, task_title
from .worktree import worktree_operation_state_eligibility_error


ALL_TASK_ROUTES = {
    ("show",),
    ("summary",),
    ("review-bundle",),
    ("logs",),
    ("transcript",),
    ("follow",),
    ("reject",),
    ("events",),
    ("enqueue-dependency",),
}

DIRECTORY_DESTS = {
    "cwd",
    "project_root",
    "repo_root",
    "working_directory",
}

FILE_DESTS = {
    "approval_path",
    "bundle",
    "config",
    "config_target",
    "executable",
    "execution_envelope",
    "exploration_policy_json",
    "manifest",
    "mapping_json",
    "plan_path",
    "policy",
    "policy_json",
    "posterior_policy_json",
    "preview",
    "preview_path",
    "prompt_file",
    "proposal_path",
    "snapshot_json",
    "stderr_path",
    "stdout_path",
    "trigger",
}


def activate_completion(parser: argparse.ArgumentParser) -> None:
    argcomplete = import_argcomplete()
    if argcomplete is None:
        return
    configure_parser(parser, argcomplete)
    argcomplete.autocomplete(
        parser,
        default_completer=argcomplete.completers.SuppressCompleter(),
    )


def completion_shellcode(shell: str) -> str:
    argcomplete = import_argcomplete()
    if argcomplete is None:
        raise RuntimeError(
            "shell completion support requires the optional completion extra; "
            "install with: pip install 'codex-batch-runner[completion]'"
        )
    return str(argcomplete.shellcode(["cbr"], shell=shell))


def import_argcomplete() -> ModuleType | None:
    try:
        import argcomplete
    except ModuleNotFoundError as exc:
        if exc.name != "argcomplete":
            raise
        return None
    return argcomplete


def configure_parser(parser: argparse.ArgumentParser, argcomplete: Any) -> None:
    for route, action in iter_parser_actions(parser):
        if action.dest == "task_id":
            if route == ("enqueue",):
                action.completer = argcomplete.completers.SuppressCompleter()
            else:
                action.completer = TaskCompleter(route)
        elif route == ("enqueue",) and action.dest == "depends_on":
            action.completer = TaskCompleter(("enqueue-dependency",))
        elif route == ("enqueue",) and action.dest == "command":
            action.completer = argcomplete.completers.SuppressCompleter()
        elif action.dest in DIRECTORY_DESTS:
            action.completer = argcomplete.completers.DirectoriesCompleter()
        elif action.dest in FILE_DESTS:
            action.completer = argcomplete.completers.FilesCompleter()


def iter_parser_actions(
    parser: argparse.ArgumentParser,
    route: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], argparse.Action]]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from iter_parser_actions(child, route + (name,))
        else:
            yield route, action


class TaskCompleter:
    def __init__(self, route: tuple[str, ...]) -> None:
        self.route = route

    def __call__(
        self,
        prefix: str,
        parsed_args: argparse.Namespace,
        **_: Any,
    ) -> dict[str, str]:
        try:
            config = Config.load(getattr(parsed_args, "config", None))
            tasks = list_tasks_read_only(config)
            candidates: dict[str, str] = {}
            for task in sorted(tasks, key=lambda item: str(item.get("id") or "")):
                task_id = str(task.get("id") or "")
                if not task_id or not task_id.startswith(prefix):
                    continue
                if not task_is_eligible(config, self.route, task):
                    continue
                status = str(task.get("status") or "-")
                title = " ".join(task_title(task).split())
                candidates[task_id] = f"{status} · {title}"
            return candidates
        except Exception:
            return {}


def task_is_eligible(config: Config, route: tuple[str, ...], task: dict[str, Any]) -> bool:
    if route in ALL_TASK_ROUTES:
        return True
    if route == ("accept",):
        return acceptance_eligibility_error(task) is None
    if route == ("archive",):
        return archive_gate_result(task)["status"] in {"passed", "grandfathered"}
    if route == ("resolve",):
        return is_resolvable_task(task)

    if len(route) == 2 and route[0] == "worktree":
        return worktree_operation_state_eligibility_error(
            config,
            route[1],
            task,
        ) is None
    return False

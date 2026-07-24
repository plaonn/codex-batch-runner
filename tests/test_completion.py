from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_batch_runner.cli import build_parser, main
from codex_batch_runner.completion import (
    TaskCompleter,
    configure_parser,
    iter_parser_actions,
)
from codex_batch_runner.config import Config
from codex_batch_runner.queue import list_tasks_read_only
from codex_batch_runner.worktree import worktree_operation_state_eligibility_error


class FakeFilesCompleter:
    pass


class FakeDirectoriesCompleter:
    pass


class FakeSuppressCompleter:
    pass


def fake_argcomplete(shellcode: str = "registered\n") -> SimpleNamespace:
    return SimpleNamespace(
        autocomplete=lambda *args, **kwargs: None,
        shellcode=lambda executables, shell: shellcode,
        completers=SimpleNamespace(
            FilesCompleter=FakeFilesCompleter,
            DirectoriesCompleter=FakeDirectoriesCompleter,
            SuppressCompleter=FakeSuppressCompleter,
        ),
    )


def write_config(root: Path) -> Path:
    path = root / "config.json"
    path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "tasks"),
                "log_dir": str(root / "logs"),
                "event_dir": str(root / "events"),
                "lock_file": str(root / "runner.lock"),
                "state_file": str(root / "state.json"),
            }
        ),
        encoding="utf-8",
    )
    return path


def write_task(root: Path, task: dict) -> None:
    queue_dir = root / "tasks"
    queue_dir.mkdir(parents=True, exist_ok=True)
    (queue_dir / f"{task['id']}.json").write_text(json.dumps(task), encoding="utf-8")


class CompletionTests(unittest.TestCase):
    def test_read_only_task_listing_does_not_create_queue_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(root=root)

            self.assertEqual([], list_tasks_read_only(config))
            self.assertFalse(config.queue_dir.exists())

    def test_task_completer_keeps_missing_config_and_queue_silent_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            queue_dir = root / "tasks"
            stderr = io.StringIO()
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                candidates = TaskCompleter(("show",))(
                    prefix="",
                    parsed_args=argparse.Namespace(config=str(config_path)),
                )

            self.assertEqual({}, candidates)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertFalse(queue_dir.exists())

    def test_task_completer_returns_described_command_eligible_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            write_task(root, {"id": "completed-task", "title": "Ready result", "status": "completed"})
            write_task(root, {"id": "failed-task", "title": "Needs resolution", "status": "failed"})
            parsed = argparse.Namespace(config=str(config_path))

            show = TaskCompleter(("show",))(prefix="", parsed_args=parsed)
            accept = TaskCompleter(("accept",))(prefix="", parsed_args=parsed)
            resolve = TaskCompleter(("resolve",))(prefix="", parsed_args=parsed)

            self.assertEqual(
                {
                    "completed-task": "completed · Ready result",
                    "failed-task": "failed · Needs resolution",
                },
                show,
            )
            self.assertEqual({"completed-task": "completed · Ready result"}, accept)
            self.assertEqual({"failed-task": "failed · Needs resolution"}, resolve)

    def test_worktree_completion_uses_bounded_state_gate_without_git_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
            config_data["worktree_mode"] = "task"
            config_path.write_text(json.dumps(config_data), encoding="utf-8")
            write_task(
                root,
                {
                    "id": "apply-task",
                    "title": "Apply result",
                    "status": "completed",
                    "review_status": "accepted",
                    "execution_mode": "git_worktree",
                    "execution_branch": "cbr/apply-task",
                    "execution_base_head": "base",
                    "execution_repo_root": "/missing/repo",
                    "execution_worktree_path": "/missing/worktree",
                    "execution_worktree_status": "retained",
                },
            )

            with patch("codex_batch_runner.worktree.git", side_effect=AssertionError("Git must not run")):
                candidates = TaskCompleter(("worktree", "apply"))(
                    prefix="",
                    parsed_args=argparse.Namespace(config=str(config_path)),
                )

            self.assertEqual({"apply-task": "completed · Apply result"}, candidates)

    def test_unknown_worktree_operation_fails_closed(self) -> None:
        config = Config.load(root=Path("/tmp"))
        self.assertEqual(
            "unknown worktree operation: unknown",
            worktree_operation_state_eligibility_error(config, "unknown", {}),
        )

    def test_malformed_queue_data_returns_no_dynamic_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            queue_dir = root / "tasks"
            queue_dir.mkdir()
            (queue_dir / "broken.json").write_text("{", encoding="utf-8")

            candidates = TaskCompleter(("show",))(
                prefix="",
                parsed_args=argparse.Namespace(config=str(config_path)),
            )

            self.assertEqual({}, candidates)

    def test_parser_configuration_assigns_task_path_and_remainder_completers(self) -> None:
        parser = build_parser()
        configure_parser(parser, fake_argcomplete())
        actions = {
            (route, action.dest): action
            for route, action in iter_parser_actions(parser)
        }

        self.assertIsInstance(actions[(("show",), "task_id")].completer, TaskCompleter)
        self.assertIsInstance(actions[(("enqueue",), "task_id")].completer, FakeSuppressCompleter)
        self.assertIsInstance(actions[(("enqueue",), "depends_on")].completer, TaskCompleter)
        self.assertIsInstance(actions[(("enqueue",), "cwd")].completer, FakeDirectoriesCompleter)
        self.assertIsInstance(actions[((), "config")].completer, FakeFilesCompleter)
        self.assertIsInstance(actions[(("enqueue",), "prompt_file")].completer, FakeFilesCompleter)
        self.assertIsInstance(actions[(("enqueue",), "command")].completer, FakeSuppressCompleter)

    def test_remainder_keeps_cbr_looking_options_in_external_command(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["enqueue", "--cwd", "/tmp", "--backend", "shell", "--command", "git", "--config", "other.json"]
        )

        self.assertIsNone(args.config)
        self.assertEqual(["git", "--config", "other.json"], args.command)

    def test_completion_command_does_not_require_config(self) -> None:
        stdout = io.StringIO()
        with patch("codex_batch_runner.completion.import_argcomplete", return_value=fake_argcomplete("script\n")):
            with contextlib.redirect_stdout(stdout):
                code = main(["completion", "zsh"])

        self.assertEqual(0, code)
        self.assertEqual("script\n", stdout.getvalue())

    def test_completion_command_reports_missing_optional_extra(self) -> None:
        stderr = io.StringIO()
        with patch("codex_batch_runner.completion.import_argcomplete", return_value=None):
            with contextlib.redirect_stderr(stderr):
                code = main(["completion", "bash"])

        self.assertEqual(1, code)
        self.assertIn("codex-batch-runner[completion]", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

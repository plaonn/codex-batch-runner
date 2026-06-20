from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.doctor import build_doctor_report
from codex_batch_runner.queue import create_task, save_task
from codex_batch_runner.timeutil import add_seconds


def write_config(tmp: str, codex_command: list[str]) -> Path:
    root = Path(tmp)
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "tasks"),
                "log_dir": str(root / "logs"),
                "lock_file": str(root / "runner.lock"),
                "state_file": str(root / "state.json"),
                "codex_command": codex_command,
            }
        ),
        encoding="utf-8",
    )
    return config_path


def run_cli(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, stdout.getvalue()


@contextlib.contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def run_git(cwd: Path, args: list[str]) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_healthy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable), "exec", "--json"])

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["ok"])
            self.assertEqual(str(Path(tmp) / "tasks"), report["paths"]["queue_dir"])
            self.assertEqual(str(Path(tmp) / "logs"), report["paths"]["log_dir"])
            self.assertTrue(report["codex_command"]["available"])

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_doctor_reports_clean_git_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            remote = root / "remote.git"
            repo.mkdir()
            run_git(repo, ["init"])
            run_git(repo, ["config", "user.email", "test@example.invalid"])
            run_git(repo, ["config", "user.name", "Test User"])
            (repo / "README.md").write_text("# temp\n", encoding="utf-8")
            run_git(repo, ["add", "README.md"])
            run_git(repo, ["commit", "-m", "initial"])
            run_git(repo, ["branch", "-M", "main"])
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            run_git(repo, ["remote", "add", "origin", str(remote)])
            run_git(repo, ["push", "-u", "origin", "main"])

            executable = root / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])

            with working_directory(repo):
                code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["git"]["available"])
            self.assertTrue(report["git"]["is_repository"])
            self.assertEqual(str(repo.resolve()), report["git"]["root"])
            self.assertEqual("main", report["git"]["branch"])
            self.assertFalse(report["git"]["dirty"])
            self.assertEqual("origin/main", report["git"]["upstream"])
            self.assertEqual("origin/main", report["git"]["comparison_ref"])
            self.assertEqual(0, report["git"]["ahead"])
            self.assertEqual(0, report["git"]["behind"])
            self.assertEqual([], report["git"]["warnings"])

    def test_doctor_warns_for_non_git_root_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])
            config = Config.load(str(config_path), root=Path(tmp))

            report = build_doctor_report(config)

            self.assertTrue(report["ok"])
            self.assertFalse(report["git"]["is_repository"])
            self.assertIn("not inside a git repository", report["git"]["warnings"][0])
            self.assertIn(
                {"name": "git", "level": "warning", "message": report["git"]["warnings"][0]},
                report["checks"],
            )

    def test_doctor_errors_when_codex_command_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, [str(Path(tmp) / "missing-codex")])

            code, output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(1, code)
            self.assertIn("error: codex_command", output)
            self.assertIn("executable not available", output)

    def test_doctor_summarizes_task_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])
            config = Config.load(str(config_path))

            create_task(config, "ready", tmp, task_id="ready")
            cooldown = create_task(config, "cooldown", tmp, task_id="cooldown")
            cooldown["cooldown_until"] = add_seconds(3600)
            save_task(config, cooldown)
            done = create_task(config, "done", tmp, task_id="done")
            done["status"] = "completed"
            done["review_status"] = "unreviewed"
            save_task(config, done)
            failed = create_task(config, "failed", tmp, task_id="failed")
            failed["status"] = "failed"
            failed["resolution"] = "manual"
            save_task(config, failed)

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(4, report["tasks"]["total"])
            self.assertEqual({"completed": 1, "failed": 1, "runnable": 2}, report["tasks"]["by_status"])
            self.assertEqual(1, report["tasks"]["needs_review_completed"])
            self.assertEqual(1, report["tasks"]["resolved_failed_or_blocked"])
            self.assertEqual(1, report["tasks"]["runnable"])
            self.assertEqual(1, report["tasks"]["cooldown"])


if __name__ == "__main__":
    unittest.main()

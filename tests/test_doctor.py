from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
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

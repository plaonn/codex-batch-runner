from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

from codex_batch_runner.cli import ListColor, main
from codex_batch_runner.config import Config
from codex_batch_runner.evidence import rate_limit_dir
from codex_batch_runner.events import list_events
from codex_batch_runner.fs import write_json_atomic
from codex_batch_runner.queue import create_task, dependency_status, list_tasks, load_task, save_task, select_next_task
from codex_batch_runner.review_bundle import build_review_bundle
from codex_batch_runner.review_next import apply_mechanical_accept, detectable_safety_violation, review_fingerprint
from codex_batch_runner.reviewer_codex import ReviewerCodexOutcome
from codex_batch_runner.state import load_state


FAKE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"


def write_config(
    tmp: str,
    trigger_command: list[str] | None = None,
    manual_cooldown_wake_scheduler: str | None = None,
    manual_cooldown_wake_command: list[str] | None = None,
    dependency_requires_accepted_review: bool = False,
    auto_review_mechanical_accept: bool = False,
    auto_review_codex_enabled: bool = False,
    auto_review_codex_max_calls_per_run: int = 0,
    auto_review_codex_max_fix_loops_per_task: int = 0,
    codex_command: list[str] | None = None,
    extra: dict | None = None,
) -> Path:
    root = Path(tmp)
    data = {
        "queue_dir": str(root / "tasks"),
        "log_dir": str(root / "logs"),
        "event_dir": str(root / "events"),
        "lock_file": str(root / "runner.lock"),
        "state_file": str(root / "state.json"),
        "dependency_requires_accepted_review": dependency_requires_accepted_review,
        "auto_review_mechanical_accept": auto_review_mechanical_accept,
        "auto_review_codex_enabled": auto_review_codex_enabled,
        "auto_review_codex_max_calls_per_run": auto_review_codex_max_calls_per_run,
        "auto_review_codex_max_fix_loops_per_task": auto_review_codex_max_fix_loops_per_task,
    }
    if codex_command is not None:
        data["codex_command"] = codex_command
    if extra:
        data.update(extra)
    if trigger_command is not None:
        data["post_mutation_trigger_command"] = trigger_command
    if manual_cooldown_wake_scheduler is not None:
        data["manual_cooldown_wake_scheduler"] = manual_cooldown_wake_scheduler
    if manual_cooldown_wake_command is not None:
        data["manual_cooldown_wake_command"] = manual_cooldown_wake_command
    config_path = root / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return config_path


def run_cli(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, stdout.getvalue()


def run_cli_with_stderr(args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def set_status(config: Config, task_id: str, status: str, last_error: str | None = None) -> None:
    task = load_task(config, task_id)
    task["status"] = status
    task["last_error"] = last_error
    save_task(config, task)


def list_lines(output: str) -> list[str]:
    return output.strip().splitlines()


def fixed_table_rows(output: str) -> list[dict[str, str]]:
    lines = list_lines(output)
    if not lines:
        return []
    headers = lines[0].split()
    starts = [lines[0].index(header) for header in headers]
    rows = []
    for line in lines[1:]:
        row = {}
        for index, header in enumerate(headers):
            start = starts[index]
            end = starts[index + 1] if index + 1 < len(starts) else None
            value = line[start:end].strip()
            row[header] = strip_status_marker(value) if header == "STATUS" else value
        rows.append(row)
    return rows


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def compact_list_rows(output: str) -> list[dict[str, str]]:
    lines = [strip_ansi(line) for line in list_lines(output)]
    if not lines:
        return []
    headers = lines[0].split()
    starts = [lines[0].index(header) for header in headers]
    rows = []
    current: dict[str, str] | None = None
    for line in lines[1:]:
        if line.startswith("[") and line.endswith("]"):
            current = None
            continue
        if current is not None and line.startswith("  ") and line.strip():
            title_segment = line[: starts[4]].strip() if len(starts) > 4 else line.strip()
            deps_segment = line[starts[4] : starts[5]].strip() if len(starts) > 5 else ""
            note_segment = line[starts[5] :].strip() if len(starts) > 5 else ""
            if title_segment and not current["TITLE"]:
                current["TITLE"] = title_segment
            if deps_segment:
                if current["DEPS"] in {"", "-"}:
                    current["DEPS"] = deps_segment
                else:
                    current["DEPS"] += "\n" + deps_segment
            if note_segment:
                if current["NOTE"] in {"", "-"}:
                    current["NOTE"] = note_segment
                else:
                    current["NOTE"] += "; " + note_segment
            continue
        parsed = {}
        for index, header in enumerate(headers):
            start = starts[index]
            end = starts[index + 1] if index + 1 < len(starts) else None
            parsed[header] = line[start:end].strip()
        if not parsed["ID"] and current is not None:
            title_segment = line[: starts[4]].strip() if len(starts) > 4 else parsed["PROJECT"]
            if title_segment and not current["TITLE"]:
                current["TITLE"] = title_segment
            if parsed["DEPS"]:
                if current["DEPS"] in {"", "-"}:
                    current["DEPS"] = parsed["DEPS"]
                else:
                    current["DEPS"] += "\n" + parsed["DEPS"]
            if parsed["NOTE"]:
                if current["NOTE"] in {"", "-"}:
                    current["NOTE"] = parsed["NOTE"]
                else:
                    current["NOTE"] += "; " + parsed["NOTE"]
            continue
        row = {
            "ID": parsed["ID"],
            "TITLE": "",
            "PROJECT": parsed["PROJECT"],
            "STATUS": strip_status_marker(parsed["STATUS"]),
            "ATT": parsed["ATT"],
            "DEPS": parsed["DEPS"],
            "NOTE": parsed["NOTE"],
        }
        rows.append(row)
        current = row
    return rows


def strip_status_marker(value: str) -> str:
    for marker in ("..", "||", ">>", "??", "==", "!!", "--"):
        if value.startswith(marker):
            return value[len(marker) :].lstrip()
    return value


def visible_line_widths(output: str) -> list[int]:
    return [len(strip_ansi(line)) for line in output.splitlines()]


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, stdout=subprocess.PIPE, text=True)
    return result.stdout.strip()


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, stdout=subprocess.PIPE)
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test User")


def create_pushed_repo(path: Path) -> Path:
    path.mkdir()
    init_repo(path)
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "file.txt")
    git(path, "commit", "-m", "initial")
    remote = path.parent / f"{path.name}-remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-u", "origin", "main")
    return remote


def create_clean_completed_task(config: Config, repo: Path, task_id: str = "reviewable") -> dict:
    task = create_task(config, "work", str(repo), task_id=task_id)
    task["status"] = "completed"
    task["review_status"] = "unreviewed"
    task["completed_at"] = "2026-01-01T00:00:00+00:00"
    task["last_result"] = {
        "task_id": task_id,
        "status": "completed",
        "summary": "done",
        "changed_files": ["file.txt"],
        "verification": ["unit tests"],
    }
    task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
    save_task(config, task)
    return task


def reviewer_needs_fix_result(
    *,
    confidence: str = "high",
    suggested_fix_prompt: str = "Update docs/spec.md to match the README behavior.",
    fingerprint: str = "missing-docs:docs-spec",
    auto_fix_allowed: bool = True,
) -> dict:
    return {
        "task_id": "reviewable",
        "decision": "needs_fix",
        "confidence": confidence,
        "reason": "documentation update is incomplete",
        "findings": [
            {
                "severity": "warning",
                "summary": "missing docs",
                "evidence": "README change is not reflected in docs/spec.md",
            }
        ],
        "required_human_checks": [],
        "auto_fix_allowed": auto_fix_allowed,
        "auto_fix_risk": "low",
        "suggested_fix_prompt": suggested_fix_prompt,
        "finding_fingerprints": [fingerprint] if fingerprint else [],
        "reviewer_limits": {
            "calls_used_this_run": 1,
            "fix_loops_used_for_task": 0,
            "cooldown_recommended_seconds": 0,
        },
    }


def reviewer_pass_result(task_id: str = "reviewable") -> dict:
    return {
        "task_id": task_id,
        "decision": "pass",
        "confidence": "high",
        "reason": "bundle evidence supports accepting the task",
        "findings": [{"severity": "info", "summary": "verified", "evidence": "verification evidence is present"}],
        "required_human_checks": [],
        "auto_fix_allowed": False,
        "auto_fix_risk": "low",
        "suggested_fix_prompt": "",
        "finding_fingerprints": [],
        "reviewer_limits": {
            "calls_used_this_run": 1,
            "fix_loops_used_for_task": 0,
            "cooldown_recommended_seconds": 0,
        },
    }


def write_plan(tmp: str, data: dict) -> Path:
    path = Path(tmp) / "queue-plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class CliTests(unittest.TestCase):
    def test_cooldown_set_time_only_zero_pads_and_stores_safety_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            fixed_now = datetime(2026, 6, 21, 20, 35, tzinfo=timezone(timedelta(hours=9)))

            with patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now):
                code, output = run_cli(["--config", str(config_path), "cooldown", "set", "7:6"])

            self.assertEqual(0, code)
            self.assertIn("interpreted_reset_at: 2026-06-22T07:06:00+09:00", output)
            self.assertIn("effective_cooldown_until: 2026-06-22T07:07:00+09:00", output)
            self.assertIn("one_shot_wake: skipped (manual cooldown one-shot wake disabled)", output)
            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual("2026-06-22T07:07:00+09:00", state["global_cooldown_until"])
            events = list_events(Config.load(str(config_path)), limit=0)
            self.assertTrue(any(event["event_type"] == "cooldown_wake_skipped" for event in events))

    def test_cooldown_set_schedules_macos_one_shot_wake_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                manual_cooldown_wake_scheduler="macos_launchd",
                manual_cooldown_wake_command=["launchctl", "start", "com.example.codex-batch-runner"],
            )
            fixed_now = datetime(2026, 6, 21, 20, 35, tzinfo=timezone(timedelta(hours=9)))

            with (
                patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now),
                patch("codex_batch_runner.wake.utc_now", return_value=fixed_now.astimezone(timezone.utc)),
                patch("codex_batch_runner.wake.platform.system", return_value="Darwin"),
                patch("codex_batch_runner.wake.subprocess.run") as run,
            ):
                run.return_value.returncode = 0
                code, output = run_cli(["--config", str(config_path), "cooldown", "set", "7:6"])

            self.assertEqual(0, code)
            self.assertIn("one_shot_wake: scheduled (manual cooldown one-shot wake scheduled)", output)
            scheduled_command = run.call_args.args[0]
            self.assertEqual("launchctl", scheduled_command[0])
            self.assertIn("submit", scheduled_command)
            self.assertIn("37920", scheduled_command)
            self.assertEqual(["launchctl", "start", "com.example.codex-batch-runner"], scheduled_command[-3:])
            self.assertNotIn("codex", scheduled_command)
            events = list_events(Config.load(str(config_path)), limit=0)
            scheduled_events = [event for event in events if event["event_type"] == "cooldown_wake_scheduled"]
            self.assertEqual(1, len(scheduled_events))
            self.assertEqual("scheduled", scheduled_events[0]["payload"]["status"])

    def test_cooldown_set_wake_failure_is_warning_and_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                manual_cooldown_wake_scheduler="macos_launchd",
                manual_cooldown_wake_command=["launchctl", "start", "com.example.codex-batch-runner"],
            )
            fixed_now = datetime(2026, 6, 21, 20, 35, tzinfo=timezone(timedelta(hours=9)))

            with (
                patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now),
                patch("codex_batch_runner.wake.utc_now", return_value=fixed_now.astimezone(timezone.utc)),
                patch("codex_batch_runner.wake.platform.system", return_value="Darwin"),
                patch("codex_batch_runner.wake.subprocess.run") as run,
            ):
                run.return_value.returncode = 7
                code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "cooldown", "set", "7:6"])

            self.assertEqual(0, code)
            self.assertIn("one_shot_wake: failed (manual cooldown one-shot wake scheduler exited with status 7)", output)
            self.assertIn("warning: manual cooldown one-shot wake scheduler exited with status 7", stderr)
            events = list_events(Config.load(str(config_path)), limit=0)
            self.assertTrue(any(event["event_type"] == "cooldown_wake_failed" for event in events))

    def test_cooldown_set_rejects_direct_codex_wake_command_without_invoking_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                manual_cooldown_wake_scheduler="macos_launchd",
                manual_cooldown_wake_command=["codex", "exec", "--json"],
            )
            fixed_now = datetime(2026, 6, 21, 20, 35, tzinfo=timezone(timedelta(hours=9)))

            with (
                patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now),
                patch("codex_batch_runner.wake.subprocess.run") as run,
            ):
                code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "cooldown", "set", "7:6"])

            self.assertEqual(0, code)
            self.assertIn("one_shot_wake: failed (manual cooldown one-shot wake command must not invoke codex directly)", output)
            self.assertIn("warning: manual cooldown one-shot wake command must not invoke codex directly", stderr)
            run.assert_not_called()

    def test_cooldown_set_time_only_future_uses_today_and_past_rolls_tomorrow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            fixed_now = datetime(2026, 6, 21, 7, 5, tzinfo=timezone(timedelta(hours=9)))

            with patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now):
                today_code, today_output = run_cli(["--config", str(config_path), "cooldown", "set", "7:6"])
                tomorrow_code, tomorrow_output = run_cli(["--config", str(config_path), "cooldown", "set", "7:4"])

            self.assertEqual(0, today_code)
            self.assertIn("interpreted_reset_at: 2026-06-21T07:06:00+09:00", today_output)
            self.assertEqual(0, tomorrow_code)
            self.assertIn("interpreted_reset_at: 2026-06-22T07:04:00+09:00", tomorrow_output)

    def test_cooldown_set_parses_slash_and_dash_month_day_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            fixed_now = datetime(2026, 6, 21, 7, 5, tzinfo=timezone(timedelta(hours=9)))

            with patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now):
                slash_code, slash_output = run_cli(["--config", str(config_path), "cooldown", "set", "6/22 7:6"])
                dash_code, dash_output = run_cli(["--config", str(config_path), "cooldown", "set", "6-23 8:9"])

            self.assertEqual(0, slash_code)
            self.assertIn("interpreted_reset_at: 2026-06-22T07:06:00+09:00", slash_output)
            self.assertEqual(0, dash_code)
            self.assertIn("interpreted_reset_at: 2026-06-23T08:09:00+09:00", dash_output)

    def test_cooldown_set_rejects_explicit_past_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            fixed_now = datetime(2026, 6, 21, 7, 5, tzinfo=timezone(timedelta(hours=9)))

            with patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now):
                code, output, stderr = run_cli_with_stderr(
                    ["--config", str(config_path), "cooldown", "set", "6/20 7:6"]
                )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("cooldown reset time is in the past", stderr)

    def test_cooldown_set_rejects_more_than_seven_days_future(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            fixed_now = datetime(2026, 6, 21, 7, 5, tzinfo=timezone(timedelta(hours=9)))

            with patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now):
                code, output, stderr = run_cli_with_stderr(
                    ["--config", str(config_path), "cooldown", "set", "2026-06-29 7:6"]
                )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("within 7 days", stderr)

    def test_cooldown_set_parses_relative_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            fixed_now = datetime(2026, 6, 21, 7, 5, tzinfo=timezone(timedelta(hours=9)))

            with patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now):
                code, output = run_cli(["--config", str(config_path), "cooldown", "set", "+2h30m"])

            self.assertEqual(0, code)
            self.assertIn("interpreted_reset_at: 2026-06-21T09:35:00+09:00", output)
            self.assertIn("effective_cooldown_until: 2026-06-21T09:36:00+09:00", output)

    def test_cooldown_clear_removes_global_cooldown_and_runs_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger)
            (Path(tmp) / "state.json").write_text(
                json.dumps({"global_cooldown_until": "2026-06-22T07:07:00+09:00"}),
                encoding="utf-8",
            )

            code, output = run_cli(["--config", str(config_path), "cooldown", "clear"])

            self.assertEqual(0, code)
            self.assertEqual("global cooldown cleared\n", output)
            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            self.assertIsNone(state["global_cooldown_until"])
            self.assertEqual(["x"], marker.read_text(encoding="utf-8").splitlines())

    def test_cooldown_clear_reviewer_codex_removes_reviewer_cooldown_preserves_rate_limit_marker_and_runs_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger)
            (Path(tmp) / "state.json").write_text(
                json.dumps(
                    {
                        "global_cooldown_until": "2026-06-22T07:07:00+09:00",
                        "reviewer_codex_cooldown_until": "2026-06-22T08:08:00+09:00",
                        "last_reviewer_codex_rate_limit_at": "2026-06-22T06:00:00+09:00",
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(["--config", str(config_path), "cooldown", "clear", "--reviewer-codex"])

            self.assertEqual(0, code)
            self.assertEqual("reviewer Codex cooldown cleared\n", output)
            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual("2026-06-22T07:07:00+09:00", state["global_cooldown_until"])
            self.assertIsNone(state["reviewer_codex_cooldown_until"])
            self.assertEqual("2026-06-22T06:00:00+09:00", state["last_reviewer_codex_rate_limit_at"])
            self.assertEqual(["x"], marker.read_text(encoding="utf-8").splitlines())

            events = list_events(Config.load(str(config_path)), limit=5)
            reviewer_events = [
                event
                for event in events
                if event["event_type"] == "cooldown_updated"
                and event["payload"].get("action") == "clear_reviewer_codex"
            ]
            self.assertEqual(1, len(reviewer_events))
            self.assertEqual(
                "2026-06-22T08:08:00+09:00",
                reviewer_events[0]["payload"]["previous_reviewer_codex_cooldown_until"],
            )

    def test_cooldown_show_reports_inactive_and_active_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            fixed_now = datetime(2026, 6, 21, 7, 5, tzinfo=timezone(timedelta(hours=9)))

            with patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now):
                inactive_code, inactive_output = run_cli(["--config", str(config_path), "cooldown", "show"])
            self.assertEqual(0, inactive_code)
            self.assertIn("active: false", inactive_output)
            self.assertIn("remaining: 0m", inactive_output)

            (Path(tmp) / "state.json").write_text(
                json.dumps({"global_cooldown_until": "2026-06-21T08:06:00+09:00"}),
                encoding="utf-8",
            )
            with patch("codex_batch_runner.cooldown.local_now", return_value=fixed_now):
                active_code, active_output = run_cli(["--config", str(config_path), "cooldown", "show"])

            self.assertEqual(0, active_code)
            self.assertIn("global_cooldown_until: 2026-06-21T08:06:00+09:00", active_output)
            self.assertIn("active: true", active_output)
            self.assertIn("remaining: 1h 1m", active_output)

    def test_pause_set_stores_sanitized_state_and_writes_event_without_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger)

            with patch.dict(os.environ, {"USER": "ops-user"}, clear=False):
                code, output = run_cli(
                    ["--config", str(config_path), "pause", "set", "--reason", "ops\nmaintenance window"]
                )

            self.assertEqual(0, code)
            self.assertEqual(
                "runner pause set\nreason: ops maintenance window\npaused_at: "
                + load_state(Config.load(str(config_path)))["runner_pause"]["paused_at"]
                + "\npaused_by: ops-user\n",
                output,
            )
            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "active": True,
                    "reason": "ops maintenance window",
                    "paused_at": state["runner_pause"]["paused_at"],
                    "paused_by": "ops-user",
                },
                state["runner_pause"],
            )
            self.assertFalse(marker.exists())
            events = list_events(Config.load(str(config_path)), limit=5)
            pause_events = [event for event in events if event["event_type"] == "runner_pause_updated"]
            self.assertEqual(1, len(pause_events))
            self.assertEqual("set", pause_events[0]["payload"]["action"])
            self.assertEqual("ops maintenance window", pause_events[0]["payload"]["runner_pause"]["reason"])

    def test_pause_clear_resets_state_runs_trigger_and_writes_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger)
            (Path(tmp) / "state.json").write_text(
                json.dumps(
                    {
                        "runner_pause": {
                            "active": True,
                            "reason": "operator drain",
                            "paused_at": "2026-06-22T07:07:00+09:00",
                            "paused_by": "ops-user",
                        }
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(["--config", str(config_path), "pause", "clear"])

            self.assertEqual(0, code)
            self.assertEqual("runner pause cleared\n", output)
            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {"active": False, "reason": None, "paused_at": None, "paused_by": None},
                state["runner_pause"],
            )
            self.assertEqual(["x"], marker.read_text(encoding="utf-8").splitlines())
            events = list_events(Config.load(str(config_path)), limit=5)
            pause_events = [event for event in events if event["event_type"] == "runner_pause_updated"]
            self.assertEqual(1, len(pause_events))
            self.assertEqual("clear", pause_events[0]["payload"]["action"])
            self.assertEqual("operator drain", pause_events[0]["payload"]["previous_runner_pause"]["reason"])

    def test_pause_show_and_state_output_include_runner_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            (Path(tmp) / "state.json").write_text(
                json.dumps(
                    {
                        "runner_pause": {
                            "active": True,
                            "reason": "operator drain",
                            "paused_at": "2026-06-22T07:07:00+09:00",
                            "paused_by": "ops-user",
                        }
                    }
                ),
                encoding="utf-8",
            )

            show_code, show_output = run_cli(["--config", str(config_path), "pause", "show"])
            state_code, state_output = run_cli(["--config", str(config_path), "state"])
            state = json.loads(state_output)

            self.assertEqual(0, show_code)
            self.assertIn("active: true", show_output)
            self.assertIn("reason: operator drain", show_output)
            self.assertIn("paused_by: ops-user", show_output)
            self.assertEqual(0, state_code)
            self.assertTrue(state["runner_pause"]["active"])
            self.assertEqual("operator drain", state["runner_pause"]["reason"])

    def test_maintenance_codex_cli_dry_run_reports_missing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output = run_cli(["--config", str(config_path), "maintenance", "codex-cli", "--dry-run"])

            self.assertEqual(1, code)
            self.assertIn("status: blocked", output)
            self.assertIn("codex_cli_update_command is not configured", output)
            self.assertIn("codex_cli_smoke_command is not configured", output)
            self.assertIn("rollback_configured: false", output)

    def test_maintenance_codex_cli_apply_requires_idle_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker.log"
            command = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran', encoding='utf-8')",
                str(marker),
            ]
            config_path = write_config(
                tmp,
                extra={
                    "codex_cli_update_command": command,
                    "codex_cli_smoke_command": command,
                },
            )
            create_task(Config.load(str(config_path)), "do work", tmp, task_id="ready-task")

            code, output = run_cli(["--config", str(config_path), "maintenance", "codex-cli", "--apply", "--json"])

            self.assertEqual(1, code)
            report = json.loads(output)
            self.assertEqual("blocked", report["status"])
            self.assertIn("task ready-task is runnable", report["blockers"])
            self.assertFalse(marker.exists())
            self.assertFalse(load_state(Config.load(str(config_path)))["runner_pause"]["active"])

    def test_maintenance_codex_cli_apply_runs_update_smoke_and_clears_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker.log"
            trigger_marker = Path(tmp) / "trigger.log"
            append_command = (
                "from pathlib import Path; import sys; "
                "Path(sys.argv[1]).open('a', encoding='utf-8').write(sys.argv[2] + '\\n')"
            )
            update = [sys.executable, "-c", append_command, str(marker), "update"]
            smoke = [sys.executable, "-c", append_command, str(marker), "smoke"]
            trigger = [sys.executable, "-c", append_command, str(trigger_marker), "trigger"]
            config_path = write_config(
                tmp,
                trigger,
                extra={
                    "codex_cli_update_command": update,
                    "codex_cli_smoke_command": smoke,
                    "shell_task_timeout_seconds": 10,
                },
            )

            code, output = run_cli(["--config", str(config_path), "maintenance", "codex-cli", "--apply", "--json"])

            self.assertEqual(0, code)
            report = json.loads(output)
            self.assertEqual("succeeded", report["status"])
            self.assertTrue(report["pause_cleared"])
            self.assertEqual(["update", "smoke"], marker.read_text(encoding="utf-8").splitlines())
            self.assertEqual(["trigger"], trigger_marker.read_text(encoding="utf-8").splitlines())
            state = load_state(Config.load(str(config_path)))
            self.assertFalse(state["runner_pause"]["active"])
            self.assertTrue(Path(report["doctor_before_path"]).is_file())
            self.assertTrue(Path(report["doctor_after_update_path"]).is_file())
            self.assertTrue(Path(report["doctor_after_smoke_path"]).is_file())
            self.assertTrue(Path(report["update"]["log_path"]).is_file())
            self.assertTrue(Path(report["smoke"]["log_path"]).is_file())
            events = list_events(Config.load(str(config_path)), limit=10)
            self.assertTrue(any(event["event_type"] == "codex_cli_maintenance_completed" for event in events))
            self.assertEqual(
                2,
                len([event for event in events if event["event_type"] == "runner_pause_updated"]),
            )

    def test_maintenance_codex_cli_apply_keeps_pause_when_smoke_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker.log"
            trigger_marker = Path(tmp) / "trigger.log"
            append_command = (
                "from pathlib import Path; import sys; "
                "Path(sys.argv[1]).open('a', encoding='utf-8').write(sys.argv[2] + '\\n')"
            )
            update = [
                sys.executable,
                "-c",
                append_command,
                str(marker),
                "update",
            ]
            smoke = [sys.executable, "-c", "import sys; sys.exit(9)"]
            rollback = [
                sys.executable,
                "-c",
                append_command,
                str(marker),
                "rollback",
            ]
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('trigger', encoding='utf-8')",
                str(trigger_marker),
            ]
            config_path = write_config(
                tmp,
                trigger,
                extra={
                    "codex_cli_update_command": update,
                    "codex_cli_smoke_command": smoke,
                    "codex_cli_rollback_command": rollback,
                    "shell_task_timeout_seconds": 10,
                },
            )

            code, output = run_cli(["--config", str(config_path), "maintenance", "codex-cli", "--apply", "--json"])

            self.assertEqual(1, code)
            report = json.loads(output)
            self.assertEqual("failed", report["status"])
            self.assertIn("smoke command failed", report["blockers"])
            self.assertFalse(report["pause_cleared"])
            self.assertEqual(["update", "rollback"], marker.read_text(encoding="utf-8").splitlines())
            self.assertTrue(Path(report["rollback"]["log_path"]).is_file())
            self.assertTrue(Path(report["doctor_after_rollback_path"]).is_file())
            state = load_state(Config.load(str(config_path)))
            self.assertTrue(state["runner_pause"]["active"])
            self.assertEqual("Codex CLI maintenance", state["runner_pause"]["reason"])
            self.assertFalse(trigger_marker.exists())

    def test_maintenance_codex_cli_reports_rollback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker.log"
            update = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('update', encoding='utf-8')",
                str(marker),
            ]
            smoke = [sys.executable, "-c", "import sys; sys.exit(9)"]
            rollback = [sys.executable, "-c", "import sys; sys.exit(12)"]
            config_path = write_config(
                tmp,
                extra={
                    "codex_cli_update_command": update,
                    "codex_cli_smoke_command": smoke,
                    "codex_cli_rollback_command": rollback,
                    "shell_task_timeout_seconds": 10,
                },
            )

            code, output = run_cli(["--config", str(config_path), "maintenance", "codex-cli", "--apply", "--json"])

            self.assertEqual(1, code)
            report = json.loads(output)
            self.assertEqual("failed", report["status"])
            self.assertIn("smoke command failed", report["blockers"])
            self.assertIn("rollback command failed", report["blockers"])
            self.assertEqual(12, report["rollback"]["returncode"])
            self.assertTrue(Path(report["doctor_after_rollback_path"]).is_file())

    def test_post_mutation_trigger_runs_after_enqueue_and_review_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger)
            config = Config.load(str(config_path))

            self.assertEqual(
                (0, "task\n"),
                run_cli(["--config", str(config_path), "enqueue", "--cwd", tmp, "--id", "task", "--prompt", "work"]),
            )
            task = load_task(config, "task")
            task["status"] = "completed"
            save_task(config, task)

            for args in (
                ["accept", "task", "--reason", "verified"],
                ["reject", "task", "--reason", "needs work"],
                ["reject", "task", "--follow-up", "--reason", "needs more"],
            ):
                with self.subTest(args=args):
                    code, _ = run_cli(["--config", str(config_path), *args])
                    self.assertEqual(0, code)

            self.assertEqual(["x", "x", "x", "x"], marker.read_text(encoding="utf-8").splitlines())

    def test_post_mutation_trigger_runs_after_resolve_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="task")
            set_status(config, "task", "failed", "failed")

            self.assertEqual(
                0,
                run_cli(["--config", str(config_path), "resolve", "task", "--resolution", "manual"])[0],
            )
            self.assertEqual(0, run_cli(["--config", str(config_path), "archive", "task"])[0])

            self.assertEqual(["x", "x"], marker.read_text(encoding="utf-8").splitlines())

    def test_post_mutation_trigger_is_not_called_for_read_only_or_run_next_without_follow_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="task")
            set_status(config, "task", "completed")

            for args in (
                ["list"],
                ["show", "task"],
                ["summary", "task"],
                ["review-bundle", "task"],
                ["logs", "task"],
                ["transcript", "task"],
                ["follow", "task", "--poll-interval", "0", "--max-polls", "1"],
                ["doctor"],
                ["events"],
                ["rate-limits"],
                ["prune"],
                ["run-next"],
            ):
                with self.subTest(args=args):
                    code, _ = run_cli(["--config", str(config_path), *args])
                    self.assertIn(code, {0, 1})

            self.assertFalse(marker.exists())

    def test_post_mutation_trigger_failure_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, [sys.executable, "-c", "import sys; sys.exit(7)"])

            code, output, stderr = run_cli_with_stderr(
                ["--config", str(config_path), "enqueue", "--cwd", tmp, "--id", "task", "--prompt", "work"]
            )

            self.assertEqual(0, code)
            self.assertEqual("task\n", output)
            self.assertIn("warning: post-mutation trigger exited with status 7", stderr)
            self.assertEqual("runnable", load_task(Config.load(str(config_path)), "task")["status"])

    def test_enqueue_records_project_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--id",
                    "metadata",
                    "--project",
                    "project-a",
                    "--category",
                    "implementation",
                    "--label",
                    "queue",
                    "--label",
                    "review",
                    "--created-by",
                    "test",
                    "--prompt",
                    "work",
                ]
            )
            task = load_task(Config.load(str(config_path)), "metadata")

            self.assertEqual(0, code)
            self.assertEqual("metadata\n", output)
            self.assertEqual("project-a", task["project_id"])
            self.assertEqual("implementation", task["category"])
            self.assertEqual(["queue", "review"], task["labels"])
            self.assertEqual("test", task["created_by"])
            self.assertEqual("work", task["title"])
            self.assertIsNone(task["description"])

    def test_enqueue_records_scheduling_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--id",
                    "scheduled",
                    "--capacity-pool",
                    "spark",
                    "--priority",
                    "high",
                    "--prompt",
                    "work",
                ]
            )
            task = load_task(Config.load(str(config_path)), "scheduled")

            self.assertEqual(0, code)
            self.assertEqual("scheduled\n", output)
            self.assertEqual("spark", task["capacity_pool"])
            self.assertEqual("high", task["task_priority"])

    def test_enqueue_records_title_and_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--id",
                    "metadata",
                    "--title",
                    "Improve list output",
                    "--description",
                    "Make central queue triage easier.",
                    "--prompt",
                    "Detailed implementation prompt",
                ]
            )
            task = load_task(Config.load(str(config_path)), "metadata")

            self.assertEqual(0, code)
            self.assertEqual("metadata\n", output)
            self.assertEqual("Improve list output", task["title"])
            self.assertEqual("Make central queue triage easier.", task["description"])

    def test_enqueue_records_execution_profile_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={
                    "execution_profiles": {
                        "small": {
                            "model": "gpt-5-small",
                            "config_overrides": {"model_reasoning_effort": "low"},
                        }
                    }
                },
            )

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--id",
                    "profiled",
                    "--profile",
                    "small",
                    "--model",
                    "gpt-5",
                    "--codex-profile",
                    "batch-normal",
                    "--config-override",
                    "model_reasoning_effort=medium",
                    "--token-budget-hint",
                    "under 20k tokens",
                    "--prompt",
                    "work",
                ]
            )
            task = load_task(Config.load(str(config_path)), "profiled")

            self.assertEqual(0, code)
            self.assertEqual("profiled\n", output)
            self.assertEqual("small", task["execution_profile"])
            self.assertEqual("gpt-5", task["model"])
            self.assertEqual("batch-normal", task["codex_profile"])
            self.assertEqual({"model_reasoning_effort": "medium"}, task["codex_config_overrides"])
            self.assertEqual("under 20k tokens", task["token_budget_hint"])

    def test_enqueue_records_routing_decision_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, extra={"execution_profiles": {"small": {"model": "gpt-5-small"}}})

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--id",
                    "routed",
                    "--profile",
                    "small",
                    "--routing-reason",
                    "docs-only bounded change",
                    "--routing-risk-factor",
                    "public-docs",
                    "--routing-risk-factor",
                    "low-blast-radius",
                    "--routing-experiment",
                    "downshift_probe",
                    "--routing-size",
                    "small",
                    "--routing-risk",
                    "low",
                    "--verification-scope",
                    "unit",
                    "--verification-scope",
                    "docs",
                    "--prompt",
                    "work",
                ]
            )
            task = load_task(Config.load(str(config_path)), "routed")

            self.assertEqual(0, code)
            self.assertEqual("routed\n", output)
            self.assertEqual("docs-only bounded change", task["routing_reason"])
            self.assertEqual(["public-docs", "low-blast-radius"], task["routing_risk_factors"])
            self.assertEqual("downshift_probe", task["routing_experiment"])
            self.assertEqual("small", task["routing_size"])
            self.assertEqual("low", task["routing_risk"])
            self.assertEqual(["unit", "docs"], task["verification_scope"])

    def test_enqueue_records_shell_command_json_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--id",
                    "shell-json",
                    "--backend",
                    "shell",
                    "--shell-timeout",
                    "120",
                    "--command-json",
                    json.dumps([sys.executable, "-c", "print('ok')"]),
                ]
            )
            task = load_task(Config.load(str(config_path)), "shell-json")

            self.assertEqual(0, code)
            self.assertEqual("shell-json\n", output)
            self.assertEqual("shell", task["execution_backend"])
            self.assertEqual([sys.executable, "-c", "print('ok')"], task["shell_command"])
            self.assertEqual(120, task["shell_timeout_seconds"])
            self.assertEqual("Shell task: " + shlex.join([sys.executable, "-c", "print('ok')"]), task["prompt"])

    def test_enqueue_records_shell_command_argv_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--id",
                    "shell-argv",
                    "--backend",
                    "shell",
                    "--command",
                    sys.executable,
                    "-c",
                    "print('ok')",
                ]
            )
            task = load_task(Config.load(str(config_path)), "shell-argv")

            self.assertEqual(0, code)
            self.assertEqual("shell-argv\n", output)
            self.assertEqual("shell", task["execution_backend"])
            self.assertEqual([sys.executable, "-c", "print('ok')"], task["shell_command"])

    def test_enqueue_codex_default_still_requires_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(
                ["--config", str(config_path), "enqueue", "--cwd", tmp, "--id", "missing-prompt"]
            )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("Codex tasks require --prompt or --prompt-file", stderr)

    def test_enqueue_rejects_unallowlisted_config_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--config-override",
                    "danger=true",
                    "--prompt",
                    "work",
                ]
            )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("is not allowlisted", stderr)

    def test_routing_report_groups_profile_category_and_label_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={
                    "execution_profiles": {
                        "small": {"model": "gpt-5-small"},
                        "normal": {"model": "gpt-5"},
                    }
                },
            )
            config = Config.load(str(config_path))
            accepted = create_task(
                config,
                "work",
                tmp,
                task_id="accepted-small",
                project_id="project-a",
                category="implementation",
                labels=["docs", "safe"],
                execution_profile="small",
            )
            accepted["status"] = "completed"
            accepted["review_status"] = "accepted"
            accepted["attempts"] = 1
            accepted["run_count"] = 1
            accepted["last_run"] = {"duration_seconds": 30}
            accepted["reviewer_codex"] = {"decision": "pass", "confidence": "high"}
            save_task(config, accepted)

            needs_fix = create_task(
                config,
                "work",
                tmp,
                task_id="needs-fix-normal",
                project_id="project-a",
                category="implementation",
                labels=["runner"],
                execution_profile="normal",
            )
            needs_fix["status"] = "completed"
            needs_fix["review_status"] = "needs_followup"
            needs_fix["attempts"] = 2
            needs_fix["run_count"] = 2
            needs_fix["fix_attempts"] = 1
            needs_fix["last_auto_fix_task_id"] = "fix-normal"
            needs_fix["last_run"] = {"duration_seconds": 90}
            needs_fix["reviewer_codex"] = {"decision": "needs_fix", "confidence": "high"}
            save_task(config, needs_fix)

            fix_task = create_task(
                config,
                "work",
                tmp,
                task_id="fix-normal",
                project_id="project-a",
                category="implementation",
                labels=["runner"],
                execution_profile="normal",
            )
            fix_task["status"] = "completed"
            fix_task["review_status"] = "accepted"
            fix_task["attempts"] = 1
            fix_task["subtask_type"] = "auto_review_fix"
            save_task(config, fix_task)

            other = create_task(
                config,
                "work",
                tmp,
                task_id="other-project",
                project_id="project-b",
                category="docs",
                labels=["docs"],
                execution_profile="small",
            )
            other["status"] = "completed"
            other["review_status"] = "accepted"
            save_task(config, other)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            profiles = {entry["key"]: entry for entry in report["groups"]["profile"]}
            labels = {entry["key"]: entry for entry in report["groups"]["label"]}
            categories = {entry["key"]: entry for entry in report["groups"]["category"]}

            self.assertEqual(0, code)
            self.assertEqual(3, report["task_count"])
            self.assertEqual(1, profiles["small"]["tasks"])
            self.assertEqual(1, profiles["small"]["first_pass_accepted"])
            self.assertEqual(1.0, profiles["small"]["first_pass_accept_rate"])
            self.assertEqual(2, profiles["normal"]["tasks"])
            self.assertEqual(1, profiles["normal"]["needs_fix_or_rejected"])
            self.assertEqual(1, profiles["normal"]["auto_fix_tasks"])
            self.assertEqual(1, profiles["normal"]["roots_with_auto_fix"])
            self.assertEqual(3, categories["implementation"]["tasks"])
            self.assertEqual(1, labels["docs"]["tasks"])
            self.assertEqual(2, labels["runner"]["tasks"])
            self.assertEqual(1, load_task(config, "accepted-small")["attempts"])

    def test_routing_report_exposes_routing_decision_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, extra={"execution_profiles": {"small": {"model": "gpt-5-small"}}})
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "work",
                tmp,
                task_id="downshift",
                project_id="project-a",
                category="docs",
                labels=["docs"],
                execution_profile="small",
                routing_reason="docs-only bounded change",
                routing_risk_factors=["public-docs", "low-blast-radius"],
                routing_experiment="downshift_probe",
                routing_size="small",
                routing_risk="low",
                verification_scope=["unit", "docs"],
            )
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["attempts"] = 1
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            experiments = {entry["key"]: entry for entry in report["groups"]["routing_experiment"]}
            sizes = {entry["key"]: entry for entry in report["groups"]["routing_size"]}
            risks_by_level = {entry["key"]: entry for entry in report["groups"]["routing_risk"]}
            risks = {entry["key"]: entry for entry in report["groups"]["routing_risk_factor"]}
            scopes = {entry["key"]: entry for entry in report["groups"]["verification_scope"]}
            decisions = {entry["key"]: entry for entry in report["groups"]["routing_decision"]}
            profile_decisions = {entry["key"]: entry for entry in report["groups"]["profile_routing_decision"]}
            profile_experiments = {entry["key"]: entry for entry in report["groups"]["profile_experiment"]}
            decision_key = "size=small risk=low verify=docs+unit"
            profile_decision_key = "profile=small size=small risk=low verify=docs+unit"

            self.assertEqual(0, code)
            self.assertEqual("docs-only bounded change", report["task_rows"][0]["routing_reason"])
            self.assertEqual(["public-docs", "low-blast-radius"], report["task_rows"][0]["routing_risk_factors"])
            self.assertEqual("small", report["task_rows"][0]["routing_size"])
            self.assertEqual("low", report["task_rows"][0]["routing_risk"])
            self.assertEqual(["unit", "docs"], report["task_rows"][0]["verification_scope"])
            self.assertEqual(1, experiments["downshift_probe"]["tasks"])
            self.assertEqual(1, sizes["small"]["tasks"])
            self.assertEqual(1, risks_by_level["low"]["tasks"])
            self.assertEqual(1, risks["public-docs"]["tasks"])
            self.assertEqual(1, risks["low-blast-radius"]["tasks"])
            self.assertEqual(1, scopes["unit"]["tasks"])
            self.assertEqual(1, scopes["docs"]["tasks"])
            self.assertEqual(decision_key, report["task_rows"][0]["routing_decision"])
            self.assertEqual(profile_decision_key, report["task_rows"][0]["profile_routing_decision"])
            self.assertEqual(1, decisions[decision_key]["tasks"])
            self.assertEqual(1, decisions[decision_key]["first_pass_accepted"])
            self.assertEqual(1, profile_decisions[profile_decision_key]["tasks"])
            self.assertEqual(1, profile_experiments["small/downshift_probe"]["first_pass_accepted"])

    def test_routing_report_exposes_resolved_profile_and_small_candidate_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={
                    "default_execution_profile": "normal",
                    "execution_profiles": {
                        "small": {"model": "gpt-5-small"},
                        "normal": {"model": "gpt-5"},
                    },
                },
            )
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "work",
                tmp,
                task_id="defaulted-normal",
                project_id="project-a",
                category="docs",
                labels=["docs"],
                routing_size="small",
                routing_risk="low",
                verification_scope=["docs"],
            )
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["attempts"] = 1
            task["last_run"] = {"execution_profile": "normal", "duration_seconds": 10}
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            profiles = {entry["key"]: entry for entry in report["groups"]["profile"]}
            resolved_profiles = {entry["key"]: entry for entry in report["groups"]["resolved_profile"]}
            profile_decisions = {entry["key"]: entry for entry in report["groups"]["profile_routing_decision"]}
            resolved_profile_decisions = {
                entry["key"]: entry for entry in report["groups"]["resolved_profile_routing_decision"]
            }
            small_candidates = {entry["key"]: entry for entry in report["groups"]["small_profile_candidate"]}
            row = report["task_rows"][0]

            self.assertEqual(0, code)
            self.assertEqual("default", row["profile"])
            self.assertEqual("normal", row["resolved_profile"])
            self.assertEqual("profile=default size=small risk=low verify=docs", row["profile_routing_decision"])
            self.assertEqual(
                "profile=normal size=small risk=low verify=docs",
                row["resolved_profile_routing_decision"],
            )
            self.assertEqual("candidate", row["small_profile_candidate"])
            self.assertEqual(1, profiles["default"]["tasks"])
            self.assertEqual(1, resolved_profiles["normal"]["tasks"])
            self.assertEqual(1, profile_decisions["profile=default size=small risk=low verify=docs"]["tasks"])
            self.assertEqual(1, resolved_profile_decisions["profile=normal size=small risk=low verify=docs"]["tasks"])
            self.assertEqual(1, small_candidates["candidate"]["tasks"])

    def test_routing_report_groups_missing_routing_metadata_under_fallback_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "work",
                tmp,
                task_id="unrouted",
                project_id="project-a",
                category="docs",
                labels=["docs"],
            )
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["attempts"] = 1
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            experiments = {entry["key"]: entry for entry in report["groups"]["routing_experiment"]}
            sizes = {entry["key"]: entry for entry in report["groups"]["routing_size"]}
            risks = {entry["key"]: entry for entry in report["groups"]["routing_risk"]}
            risk_factors = {entry["key"]: entry for entry in report["groups"]["routing_risk_factor"]}
            scopes = {entry["key"]: entry for entry in report["groups"]["verification_scope"]}
            decisions = {entry["key"]: entry for entry in report["groups"]["routing_decision"]}
            profile_decisions = {entry["key"]: entry for entry in report["groups"]["profile_routing_decision"]}
            row = report["task_rows"][0]
            decision_key = "size=unspecified risk=unspecified verify=none"
            profile_decision_key = "profile=default size=unspecified risk=unspecified verify=none"

            self.assertEqual(0, code)
            self.assertEqual("", row["routing_reason"])
            self.assertEqual(["none"], row["routing_risk_factors"])
            self.assertEqual("unspecified", row["routing_experiment"])
            self.assertEqual("unspecified", row["routing_size"])
            self.assertEqual("unspecified", row["routing_risk"])
            self.assertEqual(["none"], row["verification_scope"])
            self.assertEqual(decision_key, row["routing_decision"])
            self.assertEqual(profile_decision_key, row["profile_routing_decision"])
            self.assertEqual(1, experiments["unspecified"]["tasks"])
            self.assertEqual(1, sizes["unspecified"]["tasks"])
            self.assertEqual(1, risks["unspecified"]["tasks"])
            self.assertEqual(1, risk_factors["none"]["tasks"])
            self.assertEqual(1, scopes["none"]["tasks"])
            self.assertEqual(1, decisions[decision_key]["first_pass_accepted"])
            self.assertEqual(1, profile_decisions[profile_decision_key]["tasks"])

    def test_routing_report_human_output_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={"execution_profiles": {"small": {"model": "gpt-5-small"}}},
            )
            config = Config.load(str(config_path))
            for index in range(3):
                task = create_task(
                    config,
                    "work",
                    tmp,
                    task_id=f"task-{index}",
                    project_id="project-a",
                    category="docs",
                    labels=["docs"],
                    execution_profile="small",
                )
                task["status"] = "completed"
                task["review_status"] = "accepted"
                task["attempts"] = 1
                save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--limit", "2"])

            self.assertEqual(0, code)
            self.assertIn("# routing report", output)
            self.assertIn("tasks: 2 of 3 filtered", output)
            self.assertIn("## by_profile", output)
            self.assertIn("## by_resolved_profile", output)
            self.assertIn("## by_routing_size", output)
            self.assertIn("## by_verification_scope", output)
            self.assertIn("## by_routing_decision", output)
            self.assertIn("## by_profile_routing_decision", output)
            self.assertIn("## by_resolved_profile_routing_decision", output)
            self.assertIn("## by_small_profile_candidate", output)
            self.assertIn("small", output)

    def test_routing_report_rejects_negative_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "routing-report", "--limit", "-1"])

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("--limit must be non-negative", stderr)

    def test_list_filters_by_project_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(
                config,
                "work",
                tmp,
                task_id="match",
                project_id="project-a",
                category="implementation",
                labels=["queue", "review"],
            )
            other_dir = Path(tmp) / "other"
            other_dir.mkdir()
            create_task(
                config,
                "work",
                str(other_dir),
                task_id="other",
                project_id="project-b",
                category="docs",
                labels=["readme"],
            )

            filters = (
                ["--project", "project-a"],
                ["--project-root", tmp],
                ["--cwd", tmp],
                ["--category", "implementation"],
                ["--label", "queue"],
            )
            for filter_args in filters:
                with self.subTest(filter_args=filter_args):
                    code, output = run_cli(["--config", str(config_path), "list", *filter_args])

                    self.assertEqual(0, code)
                    self.assertEqual(["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"], list_lines(output)[0].split())
                    rows = {row["ID"]: row for row in compact_list_rows(output)}
                    self.assertEqual("runnable", rows["match"]["STATUS"])
                    self.assertNotIn("other", rows)

    def test_list_filters_legacy_task_by_cwd_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="legacy")
            for field in ("schema_version", "project_root", "project_id", "category", "labels", "created_by"):
                task.pop(field, None)
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--project", Path(tmp).name])

            self.assertEqual(0, code)
            self.assertEqual(["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"], list_lines(output)[0].split())
            self.assertEqual("runnable", compact_list_rows(output)[0]["STATUS"])

            code, output = run_cli(["--config", str(config_path), "list", "--project-root", tmp])

            self.assertEqual(0, code)
            self.assertEqual("runnable", compact_list_rows(output)[0]["STATUS"])

    def test_list_default_shows_reviewable_completed_and_hides_accepted_and_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id, status in (
                ("runnable", "runnable"),
                ("resume", "needs_resume"),
                ("running", "running"),
                ("blocked", "blocked_user"),
                ("failed", "failed"),
                ("completed", "completed"),
                ("accepted", "completed"),
                ("rejected", "completed"),
                ("needs-followup", "completed"),
                ("archived", "archived"),
            ):
                create_task(config, task_id, tmp, task_id=task_id)
                set_status(config, task_id, status)
            accepted = load_task(config, "accepted")
            accepted["review_status"] = "accepted"
            save_task(config, accepted)
            rejected = load_task(config, "rejected")
            rejected["review_status"] = "rejected"
            save_task(config, rejected)
            needs_followup = load_task(config, "needs-followup")
            needs_followup["review_status"] = "needs_followup"
            save_task(config, needs_followup)

            code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("runnable", rows["runnable"]["STATUS"])
            self.assertEqual("needs_resume", rows["resume"]["STATUS"])
            self.assertEqual("running", rows["running"]["STATUS"])
            self.assertEqual("blocked_user", rows["blocked"]["STATUS"])
            self.assertEqual("failed", rows["failed"]["STATUS"])
            self.assertEqual("awaiting_review", rows["completed"]["STATUS"])
            self.assertEqual("-", rows["completed"]["NOTE"])
            self.assertEqual("review_failed", rows["rejected"]["STATUS"])
            self.assertEqual("-", rows["rejected"]["NOTE"])
            self.assertEqual("needs_followup", rows["needs-followup"]["STATUS"])
            self.assertEqual("-", rows["needs-followup"]["NOTE"])
            self.assertNotIn("accepted", rows)
            self.assertNotIn("archived", rows)

    def test_list_review_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id, review in (
                ("unreviewed", None),
                ("accepted", "accepted"),
                ("rejected", "rejected"),
                ("followup", "needs_followup"),
            ):
                create_task(config, task_id, tmp, task_id=task_id)
                set_status(config, task_id, "completed")
                if review:
                    task = load_task(config, task_id)
                    task["review_status"] = review
                    save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--unreviewed"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("awaiting_review", rows["unreviewed"]["STATUS"])
            self.assertNotIn("accepted", rows)
            self.assertNotIn("rejected", rows)
            self.assertNotIn("followup", rows)

            code, output = run_cli(["--config", str(config_path), "list", "--needs-review"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("awaiting_review", rows["unreviewed"]["STATUS"])
            self.assertEqual("review_failed", rows["rejected"]["STATUS"])
            self.assertEqual("needs_followup", rows["followup"]["STATUS"])
            self.assertNotIn("accepted", rows)

    def test_list_distinguishes_pending_reviewer_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id in ("plain", "needs-fix", "chain-fix", "pass", "chain-pass"):
                task = create_task(config, task_id, tmp, task_id=task_id, project_id="project-a")
                task["status"] = "completed"
                task["review_status"] = "unreviewed"
                save_task(config, task)
            needs_fix = load_task(config, "needs-fix")
            needs_fix["reviewer_codex"] = {"decision": "needs_fix"}
            save_task(config, needs_fix)
            chain_fix = load_task(config, "chain-fix")
            chain_fix["chain_status"] = "needs_fix"
            save_task(config, chain_fix)
            passed = load_task(config, "pass")
            passed["reviewer_codex"] = {"decision": "pass"}
            save_task(config, passed)
            chain_pass = load_task(config, "chain-pass")
            chain_pass["chain_status"] = "awaiting_review"
            chain_pass["last_review_decision"] = "pass"
            save_task(config, chain_pass)

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--color=never"])
            needs_review_code, needs_review_output = run_cli(
                ["--config", str(config_path), "list", "--project", "project-a", "--needs-review", "--color=never"]
            )
            json_code, json_output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--json"])
            graph_code, graph_output = run_cli(
                ["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=never"]
            )
            color_code, color_output = run_cli(
                ["--config", str(config_path), "list", "--project", "project-a", "--color=always"]
            )

            self.assertEqual(0, code)
            self.assertEqual(0, needs_review_code)
            self.assertEqual(0, json_code)
            self.assertEqual(0, graph_code)
            self.assertEqual(0, color_code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("awaiting_review", rows["plain"]["STATUS"])
            self.assertEqual("review_needs_fix", rows["needs-fix"]["STATUS"])
            self.assertEqual("review_needs_fix", rows["chain-fix"]["STATUS"])
            self.assertEqual("review_pass_pending", rows["pass"]["STATUS"])
            self.assertEqual("review_pass_pending", rows["chain-pass"]["STATUS"])
            self.assertIn("reviewer needs fix; run reject --follow-up", rows["needs-fix"]["NOTE"])
            self.assertIn("reviewer needs fix; run reject --follow-up", rows["chain-fix"]["NOTE"])
            self.assertIn("reviewer passed; run accept", rows["pass"]["NOTE"])
            self.assertIn("reviewer passed; run accept", rows["chain-pass"]["NOTE"])
            needs_review_rows = {row["ID"]: row for row in compact_list_rows(needs_review_output)}
            self.assertEqual(set(rows), set(needs_review_rows))
            self.assertEqual("review_needs_fix", needs_review_rows["needs-fix"]["STATUS"])
            self.assertEqual("review_pass_pending", needs_review_rows["pass"]["STATUS"])
            json_tasks = {task["id"]: task for task in json.loads(json_output)}
            self.assertEqual("completed", json_tasks["needs-fix"]["status"])
            self.assertEqual("unreviewed", json_tasks["needs-fix"]["review_status"])
            self.assertNotIn("review_needs_fix", json_output)
            self.assertNotIn("review_pass_pending", json_output)
            self.assertIn("* !!review_needs_fix  [N] needs-fix", graph_output)
            self.assertIn("* ??review_pass_pending  [N] pass", graph_output)
            self.assertIn("\033[101;97mreview_needs_fix\033[0m", color_output)
            self.assertIn("\033[102;30mreview_pass_pending\033[0m", color_output)

    def test_list_narrow_wraps_pending_reviewer_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "Long reviewer-fix task title that should wrap",
                tmp,
                task_id="needs-fix",
                project_id="project-a",
            )
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["reviewer_codex"] = {"decision": "needs_fix"}
            save_task(config, task)

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=52):
                code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--color=never"])

            self.assertEqual(0, code)
            self.assertTrue(all(width <= 52 for width in visible_line_widths(output)))
            self.assertIn("STATUS:  !!review_needs_fix", output)
            self.assertIn("NOTE:    reviewer needs fix; run reject", output)
            self.assertIn("--follow-up", output)

    def test_list_note_hides_mechanical_auto_review_capability_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, auto_review_mechanical_accept=True)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="reviewable")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("-", rows["reviewable"]["NOTE"])
            self.assertNotIn("mechanical auto-review enabled", rows["reviewable"]["NOTE"])

    def test_list_shows_resume_timing_and_ready_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            resume = create_task(config, "resume", tmp, task_id="resume")
            resume["status"] = "needs_resume"
            resume["cooldown_until"] = "2026-06-21T00:22:00+00:00"
            save_task(config, resume)
            ready = create_task(config, "ready", tmp, task_id="ready")
            ready["status"] = "needs_resume"
            save_task(config, ready)
            fixed_now = datetime(2026, 6, 21, 0, 10, tzinfo=timezone.utc)

            with patch("codex_batch_runner.cli.utc_now", return_value=fixed_now), patch(
                "codex_batch_runner.queue.utc_now",
                return_value=fixed_now,
            ):
                code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertIn("resume in 12m (00:22)", rows["resume"]["NOTE"])
            self.assertEqual("ready to resume", rows["ready"]["NOTE"])

    def test_list_completed_tasks_show_elapsed_and_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "completed", tmp, task_id="completed")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["completed_at"] = "2026-06-21T00:05:00+00:00"
            task["last_run"] = {"duration_seconds": 3723.4}
            save_task(config, task)
            fixed_now = datetime(2026, 6, 21, 0, 10, tzinfo=timezone.utc)

            with patch("codex_batch_runner.cli.utc_now", return_value=fixed_now):
                code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertIn("done 5m ago", rows["completed"]["NOTE"])
            self.assertIn("ran 1h 02m", rows["completed"]["NOTE"])

    def test_list_all_includes_completed_and_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "completed", tmp, task_id="completed")
            create_task(config, "archived", tmp, task_id="archived")
            set_status(config, "completed", "completed")
            set_status(config, "archived", "archived")

            code, output = run_cli(["--config", str(config_path), "list", "--all"])

            self.assertEqual(0, code)
            self.assertEqual(["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"], list_lines(output)[0].split())
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("awaiting_review", rows["completed"]["STATUS"])
            self.assertEqual("archived", rows["archived"]["STATUS"])

    def test_status_filter_can_show_archived_without_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "archived", tmp, task_id="archived")
            create_task(config, "runnable", tmp, task_id="runnable")
            set_status(config, "archived", "archived")

            code, output = run_cli(["--config", str(config_path), "list", "--status", "archived"])

            self.assertEqual(0, code)
            self.assertEqual(["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"], list_lines(output)[0].split())
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("archived", rows["archived"]["STATUS"])
            self.assertNotIn("runnable", rows)

    def test_list_failed_task_shows_one_line_last_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "failed", tmp, task_id="failed")
            set_status(config, "failed", "failed", "first line\nsecond\tline")

            code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            self.assertIn("error: first line second line", output)

    def test_list_human_shows_dependency_blocked_runnable_as_effective_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "dep", tmp, task_id="dep")
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"], project_id="project-a")

            code, output = run_cli(["--config", str(config_path), "list", "--color=never"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("blocked_dependency", rows["child"]["STATUS"])
            self.assertEqual("dep (blocked)", rows["child"]["DEPS"])
            self.assertNotIn("blocked by dep", rows["child"]["NOTE"])
            self.assertNotIn("\033[", output)

    def test_list_human_distinguishes_not_completed_and_not_accepted_dependency_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, dependency_requires_accepted_review=True)
            config = Config.load(str(config_path))
            not_completed = create_task(config, "not completed", tmp, task_id="not-completed")
            save_task(config, not_completed)
            not_accepted = create_task(config, "not accepted", tmp, task_id="not-accepted")
            not_accepted["status"] = "completed"
            not_accepted["review_status"] = "unreviewed"
            save_task(config, not_accepted)
            create_task(
                config,
                "child",
                tmp,
                task_id="child",
                depends_on=["not-completed", "not-accepted"],
                project_id="project-a",
            )

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("blocked_dependency", rows["child"]["STATUS"])
            self.assertNotIn("blocked by", rows["child"]["NOTE"])
            self.assertEqual("not-completed (blocked)\nnot-accepted (not_accepted)", rows["child"]["DEPS"])

    def test_list_json_preserves_raw_status_for_dependency_blocked_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "dep", tmp, task_id="dep")
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"], project_id="project-a")

            code, output = run_cli(["--config", str(config_path), "list", "--json"])

            self.assertEqual(0, code)
            rows = {task["id"]: task for task in json.loads(output)}
            self.assertEqual("runnable", rows["child"]["status"])
            self.assertNotIn("blocked_dependency", output)

    def test_list_graph_renders_dependencies_as_git_style_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, dependency_requires_accepted_review=True)
            config = Config.load(str(config_path))
            done = create_task(config, "Done dependency", tmp, task_id="done-dep", project_id="project-a")
            done["status"] = "completed"
            done["review_status"] = "accepted"
            save_task(config, done)
            not_accepted = create_task(config, "Review dependency", tmp, task_id="review-dep", project_id="project-a")
            not_accepted["status"] = "completed"
            not_accepted["review_status"] = "unreviewed"
            save_task(config, not_accepted)
            child = create_task(
                config,
                "Child work",
                tmp,
                task_id="child",
                project_id="project-a",
                depends_on=["done-dep", "review-dep", "missing-dep"],
            )
            child["parent_task_id"] = "done-dep"
            save_task(config, child)

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--graph"])
            lines = list_lines(output)

            self.assertEqual(0, code)
            self.assertEqual("[project-a]", lines[0])
            self.assertNotIn("child", output)
            self.assertNotIn("done-dep", output)
            self.assertNotIn("review-dep", output)
            self.assertNotIn("missing-dep", output)
            self.assertIn("* ||blocked_dependency  [N] Child work", output)
            self.assertIn("│  |\\      done  [N] Done dependency", output)
            self.assertIn("│  | \\     not_accepted  [N] Review dependency", output)
            self.assertIn("│  |  \\    missing  missing dependency", output)
            self.assertNotIn("│  |       ├─", output)
            self.assertNotIn("│  |       └─", output)
            self.assertNotIn("ATT", output)
            self.assertNotIn("note:", output)

    def test_list_graph_wraps_titles_without_breaking_graph_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(
                config,
                "Very long dependency title that should wrap under the dependency edge",
                tmp,
                task_id="dep",
                project_id="project-a",
            )
            create_task(
                config,
                "Very long child title that should wrap under the source node",
                tmp,
                task_id="child",
                project_id="project-a",
                depends_on=["dep"],
            )

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=42):
                code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--graph"])

            self.assertEqual(0, code)
            self.assertTrue(all(width <= 42 for width in visible_line_widths(output)))
            self.assertIn("* ||blocked_dependency  [N] Very", output)
            self.assertIn("|       │               child title that", output)
            self.assertIn("|\\      blocked  [N] Very", output)
            self.assertIn("|                title that should wrap", output)
            self.assertNotIn("|       └─ blocked", output)

    def test_list_graph_wraps_subtask_tree_and_dependency_rails_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "Parent source task", tmp, task_id="parent", project_id="project-a")
            first_child = create_task(
                config,
                "Very long first child source title that should wrap inside graph mode",
                tmp,
                task_id="child1",
                project_id="project-a",
                depends_on=["dep"],
            )
            first_child["parent_task_id"] = "parent"
            save_task(config, first_child)
            second_child = create_task(config, "Second child source", tmp, task_id="child2", project_id="project-a")
            second_child["parent_task_id"] = "parent"
            save_task(config, second_child)
            create_task(
                config,
                "Very long dependency title that should wrap under the dependency edge",
                tmp,
                task_id="dep",
                project_id="project-a",
            )

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=48):
                code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=never"])

            self.assertEqual(0, code)
            self.assertTrue(all(width <= 48 for width in visible_line_widths(output)))
            self.assertIn("├─ * ||blocked_dependency  [N] Very long first", output)
            self.assertIn("│  |       │               child source title", output)
            self.assertIn("│  |\\      blocked  [N] Very long dependency", output)
            self.assertIn("│  |                under the dependency edge", output)
            self.assertIn("└─ * ..runnable  [N] Second child source", output)

    def test_list_graph_wraps_dependency_sibling_tree_continuations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(
                config,
                "Very long first dependency title that should keep its sibling tree rail",
                tmp,
                task_id="dep1",
                project_id="project-a",
            )
            create_task(
                config,
                "Very long second dependency title that should keep its own wrapped guide",
                tmp,
                task_id="dep2",
                project_id="project-a",
            )
            create_task(
                config,
                "Source task with multiple dependencies",
                tmp,
                task_id="child",
                project_id="project-a",
                depends_on=["dep1", "dep2"],
            )

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=48):
                code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=never"])

            self.assertEqual(0, code)
            self.assertTrue(all(width <= 48 for width in visible_line_widths(output)))
            self.assertIn("|\\      blocked  [N] Very long first", output)
            self.assertIn("|                title that should keep its", output)
            self.assertIn("|                sibling tree rail", output)
            self.assertIn("| \\     blocked  [N] Very long second", output)
            self.assertIn("|                dependency title that should", output)
            self.assertIn("|                keep its own wrapped guide", output)

    def test_list_graph_wraps_subtask_tree_without_dependency_rails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(
                config,
                "Very long parent source title that should keep its subtask guide",
                tmp,
                task_id="parent",
                project_id="project-a",
            )
            first_child = create_task(
                config,
                "Very long first child source title that should wrap inside graph mode",
                tmp,
                task_id="child1",
                project_id="project-a",
            )
            first_child["parent_task_id"] = "parent"
            save_task(config, first_child)
            second_child = create_task(
                config,
                "Very long second child source title that should keep its own wrapped guide",
                tmp,
                task_id="child2",
                project_id="project-a",
            )
            second_child["parent_task_id"] = "parent"
            save_task(config, second_child)

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=42):
                code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=never"])

            self.assertEqual(0, code)
            self.assertTrue(all(width <= 42 for width in visible_line_widths(output)))
            self.assertIn("* ..runnable  [N] Very long parent source", output)
            self.assertIn("│             title that should keep its", output)
            self.assertIn("│             subtask guide", output)
            self.assertIn("├─ * ..runnable  [N] Very long first", output)
            self.assertIn("│                child source title that", output)
            self.assertIn("│                should wrap inside graph", output)
            self.assertIn("│                mode", output)
            self.assertIn("└─ * ..runnable  [N] Very long second", output)
            self.assertIn("│                child source title that", output)
            self.assertIn("│                should keep its own", output)
            self.assertIn("│                wrapped guide", output)

    def test_list_graph_keeps_json_output_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "dep", tmp, task_id="dep")
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"], project_id="project-a")

            graph_code, graph_output = run_cli(["--config", str(config_path), "list", "--graph", "--json"])
            plain_code, plain_output = run_cli(["--config", str(config_path), "list", "--json"])

            self.assertEqual(0, graph_code)
            self.assertEqual(0, plain_code)
            self.assertEqual(json.loads(plain_output), json.loads(graph_output))
            self.assertEqual("runnable", {task["id"]: task for task in json.loads(graph_output)}["child"]["status"])
            self.assertNotIn("WAITS_FOR", graph_output)

    def test_list_demo_renders_without_reading_queue_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            with patch("codex_batch_runner.cli.list_tasks", side_effect=AssertionError("demo read queue")), patch(
                "codex_batch_runner.cli.load_state",
                side_effect=AssertionError("demo read state"),
            ):
                compact_code, compact_output = run_cli(["--config", str(config_path), "list", "--demo", "--color=never"])
                verbose_code, verbose_output = run_cli(["--config", str(config_path), "list", "--demo", "--verbose"])
                graph_code, graph_output = run_cli(["--config", str(config_path), "list", "--demo", "--graph"])
                color_code, color_output = run_cli(["--config", str(config_path), "list", "--demo", "--graph", "--color=always"])
                json_code, json_output = run_cli(["--config", str(config_path), "list", "--demo", "--json"])

            self.assertEqual(0, compact_code)
            self.assertEqual(0, verbose_code)
            self.assertEqual(0, graph_code)
            self.assertEqual(0, color_code)
            self.assertEqual(0, json_code)
            self.assertIn("demo-ready", compact_output)
            self.assertIn("demo-blocked", compact_output)
            self.assertIn("demo-parent", compact_output)
            self.assertIn("└─ [N] Blocking review fix subtask", compact_output)
            self.assertIn("LAST_RESULT", verbose_output)
            self.assertIn("[demo]", graph_output)
            self.assertIn("||blocked_dependency", compact_output)
            self.assertIn(">>running", compact_output)
            self.assertIn("* ||blocked_dependency  [N] Runnable task blocked by dependencies", graph_output)
            self.assertIn("|\\      done  [N] Completed accepted dependency", graph_output)
            self.assertIn("|    \\  missing  missing dependency", graph_output)
            self.assertNotIn("|       ├─", graph_output)
            self.assertNotIn("|       └─", graph_output)
            self.assertNotIn("demo-blocked", graph_output)
            self.assertNotIn("demo-done", graph_output)
            self.assertNotIn("demo-missing", graph_output)
            self.assertIn("not_accepted", graph_output)
            self.assertIn("not_applied", graph_output)
            self.assertIn("\033[1;97;43m??\033[0m\033[103;30mawaiting_review\033[0m", color_output)
            self.assertIn("\033[1;97;46m>>\033[0m\033[106;30mrunning\033[0m", color_output)
            self.assertIn("\033[1;97;43m||\033[0m\033[100;93mblocked_dependency\033[0m", color_output)
            self.assertIn("\033[100;92mdone\033[0m", color_output)
            self.assertRegex(color_output, r"\033\[(35|36|34|32|33|91)m\*\033\[0m")
            self.assertRegex(color_output, r"\033\[(35|36|34|32|33|91)m\|\033\[0m\033\[2m\\      \033\[0m")
            self.assertIn("\033[32m[N]\033[0m", color_output)
            self.assertIn("\033[2mCompleted accepted dependency\033[0m", color_output)
            self.assertIn("\033[103;30mnot_accepted\033[0m", color_output)
            self.assertIn("\033[2mWorktree dependency awaiting review\033[0m", color_output)
            self.assertIn("\033[103;30maccepted_unapplied\033[0m", color_output)
            rows = json.loads(json_output)
            self.assertTrue(rows)
            self.assertTrue(all(task.get("demo") is True for task in rows))

    def test_list_demo_graph_color_wraps_below_terminal_width(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=52):
                code, output = run_cli(["--config", str(config_path), "list", "--demo", "--graph", "--color=always"])

            text = strip_ansi(output)
            self.assertEqual(0, code)
            self.assertTrue(all(width <= 51 for width in visible_line_widths(output)))
            self.assertIn("|       │", text)
            self.assertIn("* ??awaiting_review  [N] Completed task awaiting\n|       │            review", text)
            self.assertIn("* ||blocked_dependency  [N] Runnable task blocked\n|       │               by dependencies", text)
            self.assertIn(
                "|  \\    not_accepted  [N] Worktree dependency\n|                     awaiting review",
                text,
            )
            self.assertIn(
                "* ||waiting_subtasks  [N] Parent task waiting for\n"
                "│                     blocking subtask\n"
                "└─ * ..runnable  [N] Blocking review fix subtask",
                text,
            )
            self.assertNotIn("revie\nw", text)
            self.assertNotIn("revi\n|       │            ew", text)
            self.assertNotIn("awaitin\ng", text)
            self.assertNotIn("awai\n|       │                ting", text)
            self.assertNotIn("no\n|       │               t", text)

    def test_list_default_filtering_keeps_dependency_blocked_runnable_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "dep", tmp, task_id="dep")
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])
            archived = create_task(config, "archived", tmp, task_id="archived")
            archived["status"] = "archived"
            save_task(config, archived)

            code, output = run_cli(["--config", str(config_path), "list"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertIn("child", rows)
            self.assertEqual("blocked_dependency", rows["child"]["STATUS"])
            self.assertNotIn("archived", rows)

    def test_list_default_uses_project_first_id_layout_and_prefix_free_title_detail_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id in ("parent-task", "second-parent"):
                parent = create_task(config, "Expanded dependency title", tmp, task_id=task_id)
                parent["status"] = "completed"
                parent["review_status"] = "accepted"
                save_task(config, parent)
            create_task(
                config,
                "Child task title",
                tmp,
                task_id="child-task",
                depends_on=["parent-task", "second-parent"],
                project_id="project-a",
            )

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a"])
            lines = list_lines(output)
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual(["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"], lines[0].split())
            self.assertEqual("[project-a]", lines[1])
            self.assertTrue(lines[2].startswith("project-a"))
            self.assertIn("child-task", lines[2])
            self.assertTrue(lines[3].startswith("  [N] Child task title"))
            self.assertNotIn("title:", output)
            self.assertNotIn("(child-task)", output)
            self.assertEqual("parent-task (done)\nsecond-parent (done)", rows["child-task"]["DEPS"])
            self.assertEqual("[N] Child task title", rows["child-task"]["TITLE"])
            self.assertNotIn("Expanded dependency title", rows["child-task"]["DEPS"])

    def test_list_compact_splits_note_segments_onto_continuation_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "Review task title", tmp, task_id="reviewable", project_id="project-a")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["chain_status"] = "waiting_fix"
            task["last_review_decision"] = "needs_fix"
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertIn("fix requested", rows["reviewable"]["NOTE"])
            self.assertIn("fix requested", output)

    def test_list_compact_renders_title_deps_and_note_continuations_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for dep_id in ("dep-one", "dep-two"):
                dep = create_task(config, dep_id, tmp, task_id=dep_id)
                dep["status"] = "completed"
                dep["review_status"] = "accepted"
                save_task(config, dep)
            task = create_task(
                config,
                "Review task title",
                tmp,
                task_id="reviewable",
                project_id="project-a",
                depends_on=["dep-one", "dep-two"],
            )
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["chain_status"] = "waiting_fix"
            task["last_review_decision"] = "needs_fix"
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--color=never"])

            self.assertEqual(0, code)
            lines = list_lines(output)
            detail = next(line for line in lines if "Review task title" in line)
            self.assertIn("dep-two (done)", detail)
            self.assertIn("fix requested", output)
            self.assertNotIn("\n\n", output)

    def test_list_color_modes_respect_auto_no_color_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "runnable", tmp, task_id="runnable")
            create_task(config, "resume", tmp, task_id="resume")
            set_status(config, "resume", "needs_resume")
            create_task(config, "running", tmp, task_id="running")
            set_status(config, "running", "running")
            create_task(config, "review", tmp, task_id="review")
            review = load_task(config, "review")
            review["status"] = "completed"
            review["review_status"] = "unreviewed"
            save_task(config, review)
            create_task(config, "completed", tmp, task_id="completed")
            completed = load_task(config, "completed")
            completed["status"] = "completed"
            completed["review_status"] = "accepted"
            save_task(config, completed)
            create_task(config, "failed", tmp, task_id="failed")
            set_status(config, "failed", "failed")

            code, never_output = run_cli(["--config", str(config_path), "list", "--color=never", "--all"])
            auto_code, auto_output = run_cli(["--config", str(config_path), "list", "--color=auto", "--all"])
            with patch.dict(os.environ, {"NO_COLOR": "1"}):
                no_color_code, no_color_output = run_cli(["--config", str(config_path), "list", "--color=always", "--all"])
                auto_no_color_code, auto_no_color_output = run_cli(["--config", str(config_path), "list", "--color=auto", "--all"])
            always_code, always_output = run_cli(["--config", str(config_path), "list", "--color=always", "--all"])
            json_code, json_output = run_cli(["--config", str(config_path), "list", "--color=always", "--all", "--json"])

            self.assertEqual(0, code)
            self.assertEqual(0, auto_code)
            self.assertEqual(0, no_color_code)
            self.assertEqual(0, auto_no_color_code)
            self.assertEqual(0, always_code)
            self.assertEqual(0, json_code)
            self.assertNotIn("\033[", never_output)
            self.assertNotIn("\033[", auto_output)
            self.assertIn("..runnable", never_output)
            self.assertIn("??awaiting_review", never_output)
            self.assertIn("==completed", never_output)
            self.assertIn("!!failed", never_output)
            self.assertIn("\033[1;97;46m..\033[0m\033[100;96mrunnable\033[0m", always_output)
            self.assertIn("\033[1;97;43m??\033[0m\033[103;30mawaiting_review\033[0m", always_output)
            self.assertIn("\033[1;97;42m==\033[0m\033[100;92mcompleted\033[0m", always_output)
            self.assertIn("\033[1;97;41m!!\033[0m\033[101;97mfailed\033[0m", always_output)
            self.assertIn("\033[100;96mrunnable\033[0m", always_output)
            self.assertIn("\033[104;97mneeds_resume\033[0m", always_output)
            self.assertIn("\033[103;30mawaiting_review\033[0m", always_output)
            self.assertIn("\033[106;30mrunning\033[0m", always_output)
            self.assertIn("\033[100;92mcompleted\033[0m", always_output)
            self.assertIn("\033[101;97mfailed\033[0m", always_output)
            self.assertRegex(always_output, r"\033\[96m[^\n]*\033\[0m")
            self.assertIn("\033[101;97mfailed\033[0m", no_color_output)
            self.assertNotIn("\033[", auto_no_color_output)
            self.assertNotIn("\033[", json_output)
            self.assertEqual(
                ["completed", "failed", "resume", "review", "runnable", "running"],
                sorted(task["id"] for task in json.loads(json_output)),
            )

    def test_status_markers_use_bold_bright_white_foreground(self) -> None:
        color = ListColor(enabled=True)

        for status in color.STATUS_MARKERS:
            marker = color.status_marker(status)
            self.assertTrue(marker.startswith("\033[1;97;"), marker.encode())

    def test_list_satisfied_dependency_uses_dim_color_or_done_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            dep = create_task(config, "dep", tmp, task_id="dep-done")
            dep["status"] = "completed"
            dep["review_status"] = "accepted"
            save_task(config, dep)
            create_task(config, "child", tmp, task_id="child", depends_on=["dep-done"])

            never_code, never_output = run_cli(["--config", str(config_path), "list", "--color=never", "--all"])
            always_code, always_output = run_cli(["--config", str(config_path), "list", "--color=always", "--all"])

            self.assertEqual(0, never_code)
            self.assertEqual(0, always_code)
            self.assertIn("dep-done (done)", never_output)
            self.assertIn("\033[2mdep-done\033[0m", always_output)

    def test_list_dependency_blockers_are_shown_in_deps_not_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, dependency_requires_accepted_review=True)
            config = Config.load(str(config_path))
            create_task(config, "blocked dep", tmp, task_id="blocked-dep")
            not_accepted = create_task(config, "not accepted", tmp, task_id="not-accepted")
            not_accepted["status"] = "completed"
            not_accepted["review_status"] = "unreviewed"
            save_task(config, not_accepted)
            create_task(
                config,
                "child",
                tmp,
                task_id="child",
                depends_on=["blocked-dep", "not-accepted", "missing-dep"],
            )

            code, output = run_cli(["--config", str(config_path), "list", "--color=never"])
            always_code, always_output = run_cli(["--config", str(config_path), "list", "--color=always"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual(0, always_code)
            self.assertEqual("blocked_dependency", rows["child"]["STATUS"])
            self.assertIn("blocked-dep (blocked)", rows["child"]["DEPS"])
            self.assertIn("not-accepted (not_accepted)", rows["child"]["DEPS"])
            self.assertIn("missing-dep (missing)", rows["child"]["DEPS"])
            self.assertNotIn("blocked by", rows["child"]["NOTE"])
            self.assertIn("\033[100;96mblocked-dep\033[0m", always_output)
            self.assertIn("\033[103;30mnot-accepted:not_accepted\033[0m", always_output)
            self.assertIn("\033[101;97;1mmissing-dep:missing\033[0m", always_output)

    def test_list_terminal_width_below_80_uses_block_layout_and_wraps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            dep = create_task(config, "done dependency", tmp, task_id="dependency-with-a-long-readable-id")
            dep["status"] = "completed"
            dep["review_status"] = "accepted"
            save_task(config, dep)
            create_task(
                config,
                "Very long task title that should wrap in block layout without exceeding a narrow terminal width",
                tmp,
                task_id="task-with-a-long-readable-id",
                project_id="project-a",
                depends_on=["dependency-with-a-long-readable-id"],
            )

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=79):
                code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--color=never"])

            self.assertEqual(0, code)
            self.assertNotEqual(["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"], list_lines(output)[0].split())
            self.assertIn("STATUS:", output)
            self.assertIn("ID:", output)
            self.assertIn("PROJECT:", output)
            self.assertIn("TITLE:", output)
            self.assertIn("DEPS:", output)
            self.assertIn("NOTE:", output)
            self.assertTrue(all(width <= 79 for width in visible_line_widths(output)))

    def test_list_terminal_width_80_uses_table_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "Table title", tmp, task_id="task", project_id="project-a")

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=80):
                code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--color=never"])

            self.assertEqual(0, code)
            self.assertEqual(["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"], list_lines(output)[0].split())
            self.assertTrue(any(line.startswith("  [N] Table title") for line in output.splitlines()))

    def test_list_mid_width_abbreviates_ids_to_preserve_note_width(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            dep = create_task(config, "dependency", tmp, task_id="task-2026-06-22T160751-381010Z0000")
            dep["status"] = "completed"
            dep["review_status"] = "accepted"
            save_task(config, dep)
            task = create_task(
                config,
                "Task title",
                tmp,
                task_id="task-2026-06-23T002728-133408Z0000",
                project_id="codex-batch-runner",
                depends_on=["task-2026-06-22T160751-381010Z0000"],
            )
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["completed_at"] = "2026-06-21T00:05:00+00:00"
            task["last_run"] = {"duration_seconds": 3723.4}
            save_task(config, task)
            fixed_now = datetime(2026, 6, 21, 0, 10, tzinfo=timezone.utc)

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=104), patch(
                "codex_batch_runner.cli.utc_now",
                return_value=fixed_now,
            ):
                code, output = run_cli(["--config", str(config_path), "list", "--color=never"])

            self.assertEqual(0, code)
            self.assertEqual(["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"], list_lines(output)[0].split())
            self.assertRegex(output, r"task-2026-\.\.\.[^\s]*3408Z0000")
            self.assertRegex(output, r"task-[^\s]*\.\.\.[^\s]*0Z0000 \(done\)")
            self.assertIn("done 5m ago", output)
            self.assertIn("ran 1h 02m", output)
            self.assertTrue(all(width <= 104 for width in visible_line_widths(output)))

    def test_list_compact_groups_by_project_and_renders_subtask_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "Parent work", tmp, task_id="parent", project_id="project-a")
            child = create_task(config, "Child work", tmp, task_id="child", project_id="project-a")
            child["parent_task_id"] = "parent"
            save_task(config, child)
            followup = create_task(config, "Follow-up work", tmp, task_id="followup", project_id="project-a")
            followup["subtask_for"] = "parent"
            save_task(config, followup)
            create_task(config, "Other project", tmp, task_id="other", project_id="project-b")

            code, output = run_cli(["--config", str(config_path), "list", "--all", "--color=never"])
            lines = list_lines(output)
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertLess(lines.index("[project-a]"), lines.index("[project-b]"))
            self.assertEqual("[N] Parent work", rows["parent"]["TITLE"])
            self.assertEqual("├─ [N] Child work", rows["child"]["TITLE"])
            self.assertEqual("└─ [N] Follow-up work", rows["followup"]["TITLE"])
            self.assertEqual("[N] Other project", rows["other"]["TITLE"])

    def test_list_compact_wraps_subtask_tree_prefix_continuations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "Parent title", tmp, task_id="parent", project_id="project-a")
            first_child = create_task(
                config,
                "Very long first child title that should wrap while keeping tree rails visible",
                tmp,
                task_id="child1",
                project_id="project-a",
            )
            first_child["parent_task_id"] = "parent"
            save_task(config, first_child)
            second_child = create_task(
                config,
                "Very long second child title that should keep its own wrapped guide",
                tmp,
                task_id="child2",
                project_id="project-a",
            )
            second_child["parent_task_id"] = "parent"
            save_task(config, second_child)

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=86):
                code, output = run_cli(["--config", str(config_path), "list", "--all", "--color=never"])

            self.assertEqual(0, code)
            self.assertTrue(all(width <= 86 for width in visible_line_widths(output)))
            self.assertIn("  ├─ [N] Very long first child", output)
            self.assertIn("  │  title that should wrap while", output)
            self.assertIn("  │  keeping tree rails visible", output)
            self.assertIn("  └─ [N] Very long second child", output)
            self.assertIn("  │  title that should keep its", output)
            self.assertIn("  │  own wrapped guide", output)

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=50):
                code, output = run_cli(["--config", str(config_path), "list", "--all", "--color=never"])

            self.assertEqual(0, code)
            self.assertTrue(all(width <= 50 for width in visible_line_widths(output)))
            self.assertIn("TITLE:   ├─ [N] Very long first child title", output)
            self.assertIn("         │  should wrap while keeping tree rails", output)
            self.assertIn("         │  visible", output)
            self.assertIn("TITLE:   └─ [N] Very long second child title", output)
            self.assertIn("         │  should keep its own wrapped guide", output)

    def test_list_default_keeps_hidden_subtasks_when_parent_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            parent = create_task(config, "Parent work", tmp, task_id="parent", project_id="project-a")
            parent["status"] = "completed"
            parent["review_status"] = "unreviewed"
            save_task(config, parent)
            child = create_task(config, "Accepted child", tmp, task_id="child", project_id="project-a")
            child["status"] = "completed"
            child["review_status"] = "accepted"
            child["parent_task_id"] = "parent"
            save_task(config, child)
            independent = create_task(config, "Accepted independent", tmp, task_id="independent", project_id="project-a")
            independent["status"] = "completed"
            independent["review_status"] = "accepted"
            save_task(config, independent)

            code, output = run_cli(["--config", str(config_path), "list", "--color=never"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("awaiting_review", rows["parent"]["STATUS"])
            self.assertEqual("└─ [N] Accepted child", rows["child"]["TITLE"])
            self.assertNotIn("independent", rows)

    def test_list_blocking_subtasks_affect_parent_effective_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            parent = create_task(config, "parent", tmp, task_id="parent")
            parent["status"] = "completed"
            parent["review_status"] = "unreviewed"
            parent["blocking_subtask_ids"] = ["fix-running", "fix-failed", "fix-done"]
            save_task(config, parent)
            running = create_task(config, "running fix", tmp, task_id="fix-running")
            running["status"] = "running"
            running["started_at"] = "2026-06-21T00:00:00+00:00"
            save_task(config, running)
            failed = create_task(config, "failed fix", tmp, task_id="fix-failed")
            failed["status"] = "failed"
            save_task(config, failed)
            done = create_task(config, "done fix", tmp, task_id="fix-done")
            done["status"] = "completed"
            done["review_status"] = "accepted"
            save_task(config, done)

            code, output = run_cli(["--config", str(config_path), "list", "--all", "--color=never"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("subtasks_blocked", rows["parent"]["STATUS"])
            self.assertIn("waiting on 2/3 subtasks", rows["parent"]["NOTE"])
            self.assertIn("failed", rows["parent"]["NOTE"])

            failed["status"] = "completed"
            failed["review_status"] = "accepted"
            save_task(config, failed)
            fixed_now = datetime(2026, 6, 21, 0, 12, tzinfo=timezone.utc)
            with patch("codex_batch_runner.cli.utc_now", return_value=fixed_now):
                code, output = run_cli(["--config", str(config_path), "list", "--all", "--color=never"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("waiting_subtasks", rows["parent"]["STATUS"])
            self.assertIn("waiting on 1/3 subtasks", rows["parent"]["NOTE"])
            self.assertIn("oldest running 12m", rows["parent"]["NOTE"])
            self.assertIn("running for 12m", rows["fix-running"]["NOTE"])

    def test_list_shows_global_and_reviewer_cooldown_banners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "task", tmp, task_id="task")
            config.state_file.write_text(
                json.dumps(
                    {
                        "global_cooldown_until": "2026-06-21T00:10:00+00:00",
                        "reviewer_codex_cooldown_until": "2026-06-21T00:20:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            fixed_now = datetime(2026, 6, 21, 0, 0, tzinfo=timezone.utc)

            with patch("codex_batch_runner.cli.utc_now", return_value=fixed_now):
                code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            self.assertIn("global cooldown active until 2026-06-21T00:10:00+00:00", output)
            self.assertIn("reviewer Codex cooldown active until 2026-06-21T00:20:00+00:00", output)

    def test_list_watch_rejects_json_and_supports_bounded_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "task", tmp, task_id="task")

            code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "list", "--watch", "--json"])
            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("--watch cannot be used with --json", stderr)

            code, output = run_cli(
                ["--config", str(config_path), "list", "--watch", "--interval", "0.5", "--max-refreshes", "1"]
            )
            self.assertEqual(0, code)
            self.assertIn("keys: q quit, r refresh, +/- interval", output)
            self.assertIn("task", output)

    def test_list_watch_ctrl_c_exits_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "task", tmp, task_id="task")

            with patch("codex_batch_runner.cli.wait_for_watch_action", side_effect=KeyboardInterrupt):
                code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "list", "--watch"])

            self.assertEqual(0, code)
            self.assertIn("keys: q quit, r refresh, +/- interval", output)
            self.assertIn("task", output)
            self.assertEqual("", stderr)

    def test_list_watch_reports_refresh_errors_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            with patch("codex_batch_runner.cli.render_list_output", side_effect=RuntimeError("boom")):
                code, output, stderr = run_cli_with_stderr(
                    ["--config", str(config_path), "list", "--watch", "--max-refreshes", "1"]
                )

            self.assertEqual(0, code)
            self.assertIn("keys: q quit, r refresh, +/- interval", output)
            self.assertIn("refresh error: RuntimeError: boom", output)
            self.assertEqual("", stderr)

    def test_list_watch_reports_source_change_restart_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "task", tmp, task_id="task")

            with patch("codex_batch_runner.cli.watch_source_signature", side_effect=[(1, 1, 1), (1, 2, 3)]):
                code, output = run_cli(["--config", str(config_path), "list", "--watch", "--max-refreshes", "1"])

            self.assertEqual(0, code)
            self.assertIn("restart watch to use updated code", output)
            self.assertIn("task", output)

    def test_list_color_uses_dependency_status_style_in_dependency_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "dep", tmp, task_id="dep-a")
            create_task(config, "child", tmp, task_id="child", depends_on=["dep-a"])

            code, output = run_cli(["--config", str(config_path), "list", "--color=always", "--all"])
            dep_color = re.search(r"(\x1b\[[0-9;]*m)dep-a\x1b\[0m", output)

            self.assertEqual(0, code)
            self.assertIsNotNone(dep_color)
            self.assertIn("\033[100;96mdep-a\033[0m", output)

    def test_list_running_task_shows_elapsed_and_progress_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "running", tmp, task_id="running")
            task["status"] = "running"
            task["started_at"] = "2026-06-21T00:00:00+00:00"
            task["last_progress"] = {"last_jsonl_event_at": "2026-06-21T01:03:25+00:00"}
            save_task(config, task)
            fixed_now = datetime(2026, 6, 21, 1, 4, tzinfo=timezone.utc)

            with patch("codex_batch_runner.cli.utc_now", return_value=fixed_now):
                code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertIn("running for 1h 04m", rows["running"]["NOTE"])
            self.assertIn("last event 35s ago", rows["running"]["NOTE"])

    def test_list_running_task_shows_no_progress_note_when_progress_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "running", tmp, task_id="running")
            task["status"] = "running"
            task["started_at"] = "2026-06-21T00:00:00+00:00"
            task["last_progress"] = {"first_meaningful_event_at": None}
            save_task(config, task)
            fixed_now = datetime(2026, 6, 21, 0, 9, tzinfo=timezone.utc)

            with patch("codex_batch_runner.cli.utc_now", return_value=fixed_now):
                code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertIn("running for 9m", rows["running"]["NOTE"])
            self.assertIn("no progress 9m", rows["running"]["NOTE"])

    def test_archive_command_marks_task_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "task", tmp, task_id="task")

            code, output = run_cli(["--config", str(config_path), "archive", "task"])
            task = load_task(config, "task")

            self.assertEqual(0, code)
            self.assertEqual("task\tarchived\n", output)
            self.assertEqual("archived", task["status"])
            self.assertEqual("runnable", task["previous_status"])
            self.assertIsNotNone(task["archived_at"])

    def test_resolve_command_records_resolution_and_hides_from_default_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "task", tmp, task_id="failed")
            set_status(config, "failed", "failed", "not worth retrying")

            code, output = run_cli(
                ["--config", str(config_path), "resolve", "failed", "--resolution", "wont_fix", "--reason", "obsolete"]
            )
            task = load_task(config, "failed")

            self.assertEqual(0, code)
            self.assertEqual("failed\tresolved\twont_fix\n", output)
            self.assertEqual("failed", task["status"])
            self.assertEqual("wont_fix", task["resolution"])
            self.assertEqual("obsolete", task["resolution_reason"])
            self.assertIsNotNone(task["resolved_at"])

            code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            self.assertNotIn("failed", {row["ID"] for row in compact_list_rows(output)})

            code, output = run_cli(["--config", str(config_path), "list", "--all"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("resolved", rows["failed"]["STATUS"])
            self.assertEqual("-", rows["failed"]["DEPS"])
            self.assertEqual("error: not worth retrying; resolved: wont_fix", rows["failed"]["NOTE"])

            code, output = run_cli(["--config", str(config_path), "summary", "failed"])

            self.assertEqual(0, code)
            self.assertIn("resolution: wont_fix", output)
            self.assertIn("resolution_reason: obsolete", output)

    def test_resolve_command_records_completed_review_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "task", tmp, task_id="parent")
            task["status"] = "completed"
            task["review_status"] = "needs_followup"
            save_task(config, task)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "resolve",
                    "parent",
                    "--resolution",
                    "superseded",
                    "--reason",
                    "fixed by follow-up",
                ]
            )
            task = load_task(config, "parent")

            self.assertEqual(0, code)
            self.assertEqual("parent\tresolved\tsuperseded\n", output)
            self.assertEqual("completed", task["status"])
            self.assertEqual("needs_followup", task["review_status"])
            self.assertEqual("superseded", task["resolution"])
            self.assertEqual("fixed by follow-up", task["resolution_reason"])

            code, output = run_cli(["--config", str(config_path), "list"])

            self.assertEqual(0, code)
            self.assertNotIn("parent", {row["ID"] for row in compact_list_rows(output)})

            code, output = run_cli(["--config", str(config_path), "list", "--all"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("resolved", rows["parent"]["STATUS"])
            self.assertEqual("resolved: superseded", rows["parent"]["NOTE"])

            code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run"])

            self.assertEqual(0, code)
            self.assertIn("selected: false", output)
            self.assertIn("no completed task needs review", output)

    def test_rate_limits_lists_sanitized_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            write_json_atomic(
                rate_limit_dir(config) / "event.json",
                {
                    "task_id": "task-rate",
                    "detected_at": "2026-06-20T12:00:00+00:00",
                    "attempt": 3,
                    "matched_markers": ["usage limit"],
                    "cooldown_until": "2026-06-20T12:30:00+00:00",
                    "stderr_excerpt": "usage limit reached",
                    "error_excerpt": "try again later",
                    "original_log_path": str(Path(tmp) / "logs" / "task-rate" / "attempt-3.jsonl"),
                },
            )

            code, output = run_cli(["--config", str(config_path), "rate-limits"])

            self.assertEqual(0, code)
            self.assertIn("task-rate", output)
            self.assertIn("attempt=3", output)
            self.assertIn("markers=usage limit", output)

            code, output = run_cli(["--config", str(config_path), "rate-limits", "--json"])
            events = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("task-rate", events[0]["task_id"])
            self.assertEqual(["usage limit"], events[0]["matched_markers"])

    def test_prune_dry_run_reports_archived_and_accepted_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            archived = create_task(config, "old archived", tmp, task_id="old-archived")
            accepted = create_task(config, "old accepted", tmp, task_id="old-accepted")
            unreviewed = create_task(config, "old unreviewed", tmp, task_id="old-unreviewed")
            for task in (archived, accepted, unreviewed):
                task["status"] = "completed"
                task["completed_at"] = "2000-01-01T00:00:00+00:00"
            archived["status"] = "archived"
            archived["archived_at"] = "2000-01-02T00:00:00+00:00"
            accepted["review_status"] = "accepted"
            accepted["reviewed_at"] = "2000-01-03T00:00:00+00:00"
            unreviewed["review_status"] = "unreviewed"
            for task in (archived, accepted, unreviewed):
                save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "prune", "--older-than-days", "30"])

            self.assertEqual(0, code)
            self.assertIn("mode: dry-run", output)
            self.assertIn("old-archived\tarchived\t2000-01-02T00:00:00+00:00", output)
            self.assertIn("old-accepted\tcompleted_accepted\t2000-01-03T00:00:00+00:00", output)
            self.assertNotIn("old-unreviewed", output)
            self.assertTrue((config.queue_dir / "old-archived.json").exists())
            self.assertTrue((config.queue_dir / "old-accepted.json").exists())

    def test_prune_default_does_not_delete_task_or_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "done", tmp, task_id="done")
            log_path = config.log_dir / "done" / "attempt-1.jsonl"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("{}\n", encoding="utf-8")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["reviewed_at"] = "2000-01-01T00:00:00+00:00"
            task["log_paths"] = [str(log_path)]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "prune"])

            self.assertEqual(0, code)
            self.assertIn("would-delete", output)
            self.assertTrue((config.queue_dir / "done.json").exists())
            self.assertTrue(log_path.exists())

    def test_prune_json_output_is_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "done", tmp, task_id="json-task")
            task["status"] = "archived"
            task["archived_at"] = "2000-01-01T00:00:00+00:00"
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "prune", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["dry_run"])
            self.assertEqual("dry-run", report["mode"])
            self.assertEqual(1, report["candidate_count"])
            self.assertEqual("json-task", report["candidates"][0]["task_id"])
            self.assertEqual("task", report["candidates"][0]["files"][0]["kind"])

    def test_prune_apply_deletes_only_paths_inside_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "done", tmp, task_id="safe")
            safe_log = config.log_dir / "safe" / "attempt-1.jsonl"
            safe_log.parent.mkdir(parents=True)
            safe_log.write_text("{}\n", encoding="utf-8")
            outside_log = Path(tmp) / "outside.jsonl"
            outside_log.write_text("{}\n", encoding="utf-8")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["reviewed_at"] = "2000-01-01T00:00:00+00:00"
            task["log_paths"] = [str(safe_log), str(outside_log)]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json"])
            report = json.loads(output)
            files = report["candidates"][0]["files"]
            outside = [file for file in files if file["path"] == str(outside_log.resolve())][0]

            self.assertEqual(0, code)
            self.assertFalse((config.queue_dir / "safe.json").exists())
            self.assertFalse(safe_log.exists())
            self.assertTrue(outside_log.exists())
            self.assertFalse(outside["safe"])
            self.assertFalse(outside["deleted"])
            self.assertEqual("outside configured log_dir", outside["reason"])

    def test_prune_dry_run_reports_old_event_jsonl_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))

            code, output = run_cli(["--config", str(config_path), "prune", "--older-than-days", "30"])

            self.assertEqual(0, code)
            self.assertIn("mode: dry-run", output)
            self.assertIn("event candidates:", output)
            self.assertIn(f"event\twould-delete\t{old_event.resolve()}", output)
            self.assertTrue(old_event.exists())

    def test_prune_apply_deletes_safe_old_event_jsonl_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertFalse(old_event.exists())
            self.assertEqual(1, report["event_candidate_count"])
            self.assertTrue(report["event_candidates"][0]["deleted"])
            self.assertEqual("event", report["event_candidates"][0]["kind"])

    def test_prune_does_not_delete_event_jsonl_resolved_outside_event_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            outside_event = Path(tmp) / "outside-event.jsonl"
            outside_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(outside_event, (946684800, 946684800))
            event_link = config.event_dir / "linked.jsonl"
            event_link.symlink_to(outside_event)

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json"])
            report = json.loads(output)
            event = report["event_candidates"][0]

            self.assertEqual(0, code)
            self.assertTrue(outside_event.exists())
            self.assertTrue(event_link.exists())
            self.assertFalse(event["safe"])
            self.assertFalse(event["deleted"])
            self.assertEqual(str(outside_event.resolve()), event["path"])
            self.assertEqual("outside configured event_dir", event["reason"])

    def test_prune_skips_event_file_when_cursor_has_not_fully_processed_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n{"event_type":"task_started"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))
            cursor = Path(tmp) / "notify-state.json"
            cursor.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "current_event_file": str(old_event),
                        "current_byte_offset": 1,
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(
                ["--config", str(config_path), "prune", "--apply", "--json", "--notifier-cursor-state", str(cursor)]
            )
            report = json.loads(output)
            event = report["event_candidates"][0]

            self.assertEqual(0, code)
            self.assertTrue(old_event.exists())
            self.assertFalse(event["deleted"])
            self.assertTrue(event["skipped"])
            self.assertEqual("notifier cursor has not fully processed this event file", event["reason"])

    def test_prune_malformed_cursor_warns_and_skips_event_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))
            cursor = Path(tmp) / "notify-state.json"
            cursor.write_text("{not json\n", encoding="utf-8")

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json", "--notifier-cursor-state", str(cursor)])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(old_event.exists())
            self.assertTrue(report["warnings"])
            self.assertTrue(report["event_candidates"][0]["skipped"])
            self.assertEqual("notifier cursor safety warning", report["event_candidates"][0]["reason"])

    def test_prune_cursor_outside_event_dir_warns_and_skips_event_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))
            outside_event = Path(tmp) / "outside.jsonl"
            cursor = Path(tmp) / "notify-state.json"
            cursor.write_text(json.dumps({"schema_version": 1, "current_event_file": str(outside_event)}), encoding="utf-8")

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json", "--notifier-cursor-state", str(cursor)])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(old_event.exists())
            self.assertIn("outside event_dir", report["warnings"][0])
            self.assertTrue(report["event_candidates"][0]["skipped"])

    def test_prune_deletes_old_event_when_cursor_is_beyond_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n', encoding="utf-8")
            newer_event = config.event_dir / "2000-01-02.jsonl"
            newer_event.write_text('{"event_type":"task_started"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))
            os.utime(newer_event, (946684800, 946684800))
            cursor = Path(tmp) / "notify-state.json"
            cursor.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "current_event_file": str(newer_event),
                        "current_byte_offset": 0,
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json", "--notifier-cursor-state", str(cursor)])
            report = json.loads(output)
            by_path = {event["path"]: event for event in report["event_candidates"]}

            self.assertEqual(0, code)
            self.assertFalse(old_event.exists())
            self.assertTrue(newer_event.exists())
            self.assertTrue(by_path[str(old_event.resolve())]["deleted"])
            self.assertTrue(by_path[str(newer_event.resolve())]["skipped"])

    def test_apply_plan_dry_run_accepts_valid_dependency_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a")
            create_task(config, "synthetic work", tmp, task_id="task-b")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": {"type": "operator", "id": "test"},
                    "reason": "implementation order changed",
                    "operations": [
                        {
                            "op": "dependency_changes",
                            "task_id": "task-b",
                            "fields": {"add": ["task-a"]},
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run"])

            self.assertEqual(0, code)
            self.assertIn("mode: dry-run", output)
            self.assertIn("valid: true", output)
            self.assertIn("op[0]\tdependency_changes\ttasks=task-b\twould_change=yes", output)
            self.assertEqual("runnable", load_task(config, "task-b")["status"])

    def test_apply_plan_dry_run_accepts_execution_profile_and_routing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, extra={"execution_profiles": {"small": {"model": "gpt-5-small"}}})
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a")
            original = load_task(config, "task-a")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": {"type": "operator", "id": "test"},
                    "reason": "retarget safe routing metadata",
                    "operations": [
                        {
                            "op": "retarget_metadata",
                            "task_id": "task-a",
                            "expected": {"updated_at": original["updated_at"]},
                            "fields": {
                                "execution_profile": "small",
                                "routing_reason": "docs-only bounded change",
                                "routing_risk_factors": ["public-docs", "low-blast-radius"],
                                "routing_experiment": "downshift_probe",
                                "routing_size": "small",
                                "routing_risk": "low",
                                "verification_scope": ["unit", "docs"],
                            },
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run"])

            self.assertEqual(0, code)
            self.assertIn("mode: dry-run", output)
            self.assertIn("valid: true", output)
            task = load_task(config, "task-a")
            self.assertNotIn("execution_profile", task)
            self.assertIsNone(task["routing_reason"])
            self.assertEqual([], task["routing_risk_factors"])
            self.assertIsNone(task["routing_experiment"])
            self.assertIsNone(task["routing_size"])
            self.assertIsNone(task["routing_risk"])
            self.assertEqual([], task["verification_scope"])

    def test_apply_plan_dry_run_rejects_missing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "pause stale task",
                    "operations": [{"op": "pause", "task_id": "missing"}],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run"])

            self.assertEqual(1, code)
            self.assertIn("valid: false", output)
            self.assertIn("task not found: missing", output)

    def test_apply_plan_dry_run_rejects_running_task_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="running-task")
            set_status(config, "running-task", "running")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "replan current work",
                    "operations": [{"op": "replan", "task_id": "running-task"}],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run"])

            self.assertEqual(1, code)
            self.assertIn("operation targets running task: running-task", output)

    def test_apply_plan_dry_run_rejects_unknown_execution_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, extra={"execution_profiles": {"small": {"model": "gpt-5-small"}}})
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "unsafe profile name",
                    "operations": [
                        {
                            "op": "retarget_metadata",
                            "task_id": "task-a",
                            "fields": {"execution_profile": "missing"},
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run"])

            self.assertEqual(1, code)
            self.assertIn("execution_profile references unknown execution profile: missing", output)

    def test_apply_plan_dry_run_rejects_dependency_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a", depends_on=["task-b"])
            create_task(config, "synthetic work", tmp, task_id="task-b")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "bad dependency rewrite",
                    "operations": [
                        {
                            "op": "dependency_changes",
                            "task_id": "task-b",
                            "fields": {"add": ["task-a"]},
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run"])

            self.assertEqual(1, code)
            self.assertIn("dependency graph would contain a cycle", output)

    def test_apply_plan_defaults_to_dry_run_without_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "validate only",
                    "operations": [],
                },
            )

            code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "apply-plan", str(plan_path)])

            self.assertEqual(0, code)
            self.assertIn("mode: dry-run", output)
            self.assertEqual("", stderr)

    def test_apply_plan_apply_updates_metadata_dependencies_emits_event_and_runs_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).open('a', encoding='utf-8').write('x\\n')",
                str(marker),
            ]
            config_path = write_config(tmp, trigger, extra={"execution_profiles": {"small": {"model": "gpt-5-small"}}})
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a")
            create_task(config, "synthetic work", tmp, task_id="task-b")
            original = load_task(config, "task-b")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "plan_id": "plan-1",
                    "actor": {"type": "operator", "id": "test"},
                    "reason": "implementation order changed",
                    "operations": [
                        {
                            "op": "retarget_metadata",
                            "task_id": "task-b",
                            "expected": {"status": "runnable", "updated_at": original["updated_at"]},
                            "fields": {
                                "title": "Updated title",
                                "description": "Updated description",
                                "category": "docs",
                                "labels": ["safe", "mutation"],
                                "depends_on": ["task-a"],
                                "status": "paused",
                                "execution_profile": "small",
                                "routing_reason": "docs-only bounded change",
                                "routing_risk_factors": ["public-docs", "low-blast-radius"],
                                "routing_experiment": "downshift_probe",
                                "routing_size": "small",
                                "routing_risk": "low",
                                "verification_scope": ["unit", "docs"],
                            },
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--apply", "--json"])
            report = json.loads(output)
            task = load_task(config, "task-b")
            events = list_events(config, task_id="task-b", limit=10)

            self.assertEqual(0, code)
            self.assertTrue(report["ok"])
            self.assertTrue(report["applied"])
            self.assertEqual(["task-b"], report["mutated_task_ids"])
            self.assertEqual("Updated title", task["title"])
            self.assertEqual("Updated description", task["description"])
            self.assertEqual("docs", task["category"])
            self.assertEqual(["safe", "mutation"], task["labels"])
            self.assertEqual(["task-a"], task["depends_on"])
            self.assertEqual("paused", task["status"])
            self.assertEqual("small", task["execution_profile"])
            self.assertEqual("docs-only bounded change", task["routing_reason"])
            self.assertEqual(["public-docs", "low-blast-radius"], task["routing_risk_factors"])
            self.assertEqual("downshift_probe", task["routing_experiment"])
            self.assertEqual("small", task["routing_size"])
            self.assertEqual("low", task["routing_risk"])
            self.assertEqual(["unit", "docs"], task["verification_scope"])
            self.assertEqual(["x"], marker.read_text(encoding="utf-8").splitlines())
            self.assertEqual("task_mutated", events[0]["event_type"])
            self.assertEqual(
                [
                    "category",
                    "depends_on",
                    "description",
                    "execution_profile",
                    "labels",
                    "routing_experiment",
                    "routing_reason",
                    "routing_risk",
                    "routing_risk_factors",
                    "routing_size",
                    "status",
                    "title",
                    "verification_scope",
                ],
                events[0]["payload"]["changed_fields"],
            )
            self.assertEqual("small", events[0]["payload"]["after"]["execution_profile"])
            self.assertEqual("docs-only bounded change", events[0]["payload"]["after"]["routing_reason"])
            self.assertEqual(["public-docs", "low-blast-radius"], events[0]["payload"]["after"]["routing_risk_factors"])
            self.assertEqual("downshift_probe", events[0]["payload"]["after"]["routing_experiment"])
            self.assertEqual("small", events[0]["payload"]["after"]["routing_size"])
            self.assertEqual("low", events[0]["payload"]["after"]["routing_risk"])
            self.assertEqual(["unit", "docs"], events[0]["payload"]["after"]["verification_scope"])

    def test_apply_plan_apply_rejects_stale_expected_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "stale update",
                    "operations": [
                        {
                            "op": "retarget_metadata",
                            "task_id": "task-a",
                            "expected": {"status": "failed"},
                            "fields": {"title": "Must not apply"},
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--apply", "--json"])
            report = json.loads(output)

            self.assertEqual(1, code)
            self.assertFalse(report["ok"])
            self.assertIn("stale task target", output)
            self.assertNotEqual("Must not apply", load_task(config, "task-a")["title"])

    def test_apply_plan_apply_rejects_running_task_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="running-task")
            set_status(config, "running-task", "running")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "replan current work",
                    "operations": [{"op": "retarget_metadata", "task_id": "running-task", "fields": {"title": "no"}}],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--apply"])

            self.assertEqual(1, code)
            self.assertIn("operation targets running task: running-task", output)
            self.assertNotEqual("no", load_task(config, "running-task")["title"])

    def test_apply_plan_apply_rejects_dependency_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a", depends_on=["task-b"])
            create_task(config, "synthetic work", tmp, task_id="task-b")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": "operator",
                    "reason": "bad dependency rewrite",
                    "operations": [{"op": "dependency_changes", "task_id": "task-b", "fields": {"add": ["task-a"]}}],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--apply"])

            self.assertEqual(1, code)
            self.assertIn("dependency graph would contain a cycle", output)
            self.assertEqual([], load_task(config, "task-b")["depends_on"])

    def test_apply_plan_json_output_is_machine_readable_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "synthetic work", tmp, task_id="task-a")
            plan_path = write_plan(
                tmp,
                {
                    "schema_version": 1,
                    "actor": {"type": "operator", "id": "test"},
                    "reason": "record safe note",
                    "operations": [
                        {
                            "op": "append_note",
                            "task_id": "task-a",
                            "fields": {
                                "note": "public-safe summary",
                                "next_prompt": "raw prompt must not appear",
                                "session_id": "session-must-not-appear",
                            },
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "apply-plan", str(plan_path), "--dry-run", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["ok"])
            self.assertEqual("dry-run", report["mode"])
            self.assertEqual(["task-a"], report["operations"][0]["task_ids"])
            self.assertEqual("[redacted]", report["operations"][0]["sanitized"]["fields"]["next_prompt"])
            self.assertEqual("[redacted]", report["operations"][0]["sanitized"]["fields"]["session_id"])
            self.assertNotIn("raw prompt must not appear", output)

    def test_prune_skips_non_jsonl_event_dir_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            cursor = config.event_dir / "notify-state.json"
            cursor.write_text('{"offset":0}\n', encoding="utf-8")
            text_log = config.event_dir / "2000-01-01.log"
            text_log.write_text("not jsonl\n", encoding="utf-8")
            for path in (cursor, text_log):
                os.utime(path, (946684800, 946684800))

            code, output = run_cli(["--config", str(config_path), "prune", "--apply", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(0, report["event_candidate_count"])
            self.assertTrue(cursor.exists())
            self.assertTrue(text_log.exists())

    def test_accept_and_reject_update_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "done task", tmp, task_id="done")
            create_task(config, "follow", tmp, task_id="follow")
            set_status(config, "done", "completed")
            set_status(config, "follow", "completed")

            code, output = run_cli(["--config", str(config_path), "accept", "done", "--reason", "verified"])
            accepted = load_task(config, "done")

            self.assertEqual(0, code)
            self.assertEqual("done task (done)\taccepted\n", output)
            self.assertNotEqual("done\taccepted\n", output)
            self.assertEqual("accepted", accepted["review_status"])
            self.assertEqual("verified", accepted["review_reason"])

            code, output = run_cli(
                ["--config", str(config_path), "reject", "follow", "--follow-up", "--reason", "needs tests"]
            )
            rejected = load_task(config, "follow")

            self.assertEqual(0, code)
            self.assertEqual("follow\tneeds_followup\n", output)
            self.assertEqual("needs_followup", rejected["review_status"])
            self.assertEqual("needs tests", rejected["review_reason"])

    def test_accept_rejects_non_completed_task_without_mutating_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="running")
            set_status(config, "running", "running")

            code, output, stderr = run_cli_with_stderr(
                ["--config", str(config_path), "accept", "running", "--reason", "too early"]
            )
            task = load_task(config, "running")

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("requires completed task status", stderr)
            self.assertIsNone(task["review_status"])

    def test_reject_remains_available_for_non_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="running")
            set_status(config, "running", "running")

            code, output = run_cli(["--config", str(config_path), "reject", "running", "--reason", "bad state"])
            task = load_task(config, "running")

            self.assertEqual(0, code)
            self.assertEqual("running\trejected\n", output)
            self.assertEqual("rejected", task["review_status"])

    def test_list_all_shows_completed_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "done", tmp, task_id="done")
            set_status(config, "done", "completed")

            code, output = run_cli(["--config", str(config_path), "list", "--all"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("awaiting_review", rows["done"]["STATUS"])
            self.assertEqual("-", rows["done"]["NOTE"])

    def test_list_compact_output_includes_header_title_project_deps_and_empty_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="plain", project_id="project-a")
            create_task(config, "parent work", tmp, task_id="parent", project_id="project-a")
            create_task(config, "work", tmp, task_id="child", depends_on=["parent"], project_id="project-a")
            parent = load_task(config, "parent")
            parent["status"] = "completed"
            parent["review_status"] = "accepted"
            save_task(config, parent)

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a"])
            lines = list_lines(output)
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual(["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"], lines[0].split())
            self.assertNotIn("\t", output)
            self.assertEqual(
                {"TITLE": "[N] work", "STATUS": "runnable", "PROJECT": "project-a", "ATT": "0", "DEPS": "-", "NOTE": "-"},
                {key: rows["plain"][key] for key in ("TITLE", "STATUS", "PROJECT", "ATT", "DEPS", "NOTE")},
            )
            self.assertEqual("parent (done)", rows["child"]["DEPS"])
            self.assertEqual("-", rows["child"]["NOTE"])

    def test_list_compact_output_sorts_by_created_at_then_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id, created_at, project_id in (
                ("later", "2026-06-20T12:00:00+09:00", "project-a"),
                ("same-b", "2026-06-19T12:00:00+09:00", "project-b"),
                ("same-a", "2026-06-19T12:00:00+09:00", "project-c"),
            ):
                task = create_task(config, "work", tmp, task_id=task_id, project_id=project_id)
                task["created_at"] = created_at
                save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--all"])
            lines = list_lines(output)
            rows = compact_list_rows(output)
            json_code, json_output = run_cli(["--config", str(config_path), "list", "--all", "--json"])

            self.assertEqual(0, code)
            self.assertEqual(0, json_code)
            self.assertNotIn("\t", output)
            self.assertEqual(["same-a", "same-b", "later"], [row["ID"] for row in rows])
            self.assertEqual(["same-a", "same-b", "later"], [task["id"] for task in json.loads(json_output)])
            self.assertEqual(["PROJECT", "ID", "STATUS", "ATT", "DEPS", "NOTE"], lines[0].split())

    def test_list_summary_and_review_next_report_unaccepted_dependency_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, dependency_requires_accepted_review=True)
            config = Config.load(str(config_path))
            dep = create_task(config, "dep", tmp, task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "unreviewed"
            save_task(config, dep)
            child = create_task(config, "child", tmp, task_id="child", depends_on=["dep"], project_id="project-a")
            child["status"] = "completed"
            child["review_status"] = "unreviewed"
            child["last_result"] = {"status": "completed", "changed_files": [], "verification": ["unit"]}
            save_task(config, child)

            list_code, list_output = run_cli(["--config", str(config_path), "list", "--all"])
            summary_code, summary_output = run_cli(["--config", str(config_path), "summary", "child"])
            review_code, review_output = run_cli(
                ["--config", str(config_path), "review-next", "--dry-run", "--project", "project-a"]
            )

            self.assertEqual(0, list_code)
            self.assertIn("dep (not_accepted)", list_output)
            self.assertEqual(0, summary_code)
            self.assertIn("dependency_blockers:\n- dep: not_accepted", summary_output)
            self.assertEqual(0, review_code)
            self.assertIn(
                "dependencies: ready=false requires_accepted_review=true blocked_by=dep:not_accepted",
                review_output,
            )

    def test_list_table_project_column_uses_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="legacy")
            task.pop("project_id", None)
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual(Path(tmp).name, rows["legacy"]["PROJECT"])
            self.assertEqual("-", rows["legacy"]["DEPS"])
            self.assertEqual("-", rows["legacy"]["NOTE"])

    def test_list_verbose_adds_compact_summary_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="verbose", project_id="project-a")
            task["status"] = "failed"
            task["last_result"] = {
                "status": "failed",
                "summary": "first line\nsecond\tline",
            }
            task["last_run"] = {
                "command_kind": "exec",
                "returncode": 1,
                "duration_seconds": 2.5,
                "log_path": str(Path(tmp) / "logs" / "verbose" / "attempt-1.jsonl"),
            }
            task["last_error"] = "error line one\nline two"
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--verbose"])
            lines = list_lines(output)
            rows = {row["ID"]: row for row in fixed_table_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual(
                [
                    "ID",
                    "TITLE",
                    "STATUS",
                    "PROJECT",
                    "ATTEMPTS",
                    "DEPS",
                    "NOTE",
                    "PROFILE",
                    "RAW_STATUS",
                    "LAST_RESULT",
                    "LAST_RUN",
                    "LAST_ERROR",
                ],
                lines[0].split(),
            )
            self.assertEqual("failed", rows["verbose"]["STATUS"])
            self.assertEqual("failed", rows["verbose"]["RAW_STATUS"])
            self.assertEqual("error: error line one line two", rows["verbose"]["NOTE"])
            self.assertEqual("-", rows["verbose"]["PROFILE"])
            self.assertEqual("status=failed summary=first line second line", rows["verbose"]["LAST_RESULT"])
            self.assertEqual("command=exec returncode=1 duration=2.5s", rows["verbose"]["LAST_RUN"])
            self.assertEqual("error line one line two", rows["verbose"]["LAST_ERROR"])

    def test_list_verbose_includes_result_push_and_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="push-meta", project_id="project-a")
            task["status"] = "completed"
            task["last_result"] = {
                "status": "completed",
                "summary": "done",
                "commits": ["abc1234 change"],
                "push_status": {"ahead": 1, "behind": 0},
            }
            task["git_status"] = {
                "branch": "main",
                "comparison_ref": "origin/main",
                "ahead": 1,
                "behind": 0,
                "dirty": False,
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "list", "--verbose", "--all"])
            rows = {row["ID"]: row for row in fixed_table_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("awaiting_review", rows["push-meta"]["STATUS"])
            self.assertEqual("completed", rows["push-meta"]["RAW_STATUS"])
            self.assertEqual("-", rows["push-meta"]["NOTE"])
            self.assertEqual(
                "status=completed summary=done commits=1 push_status=ahead=1 behind=0 "
                "git=branch=main compare=origin/main ahead=1 behind=0 dirty=false",
                rows["push-meta"]["LAST_RESULT"],
            )
            self.assertEqual("-", rows["push-meta"]["LAST_RUN"])
            self.assertEqual("-", rows["push-meta"]["LAST_ERROR"])

    def test_list_verbose_uses_dash_for_missing_summary_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="plain", project_id="project-a")

            code, output = run_cli(["--config", str(config_path), "list", "--verbose"])
            rows = {row["ID"]: row for row in fixed_table_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("-", rows["plain"]["DEPS"])
            self.assertEqual("-", rows["plain"]["NOTE"])
            self.assertEqual("runnable", rows["plain"]["RAW_STATUS"])
            self.assertEqual("-", rows["plain"]["LAST_RESULT"])
            self.assertEqual("-", rows["plain"]["LAST_RUN"])
            self.assertEqual("-", rows["plain"]["LAST_ERROR"])

    def test_list_verbose_distinguishes_effective_and_raw_status_for_dependency_blocked_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "dep", tmp, task_id="dep")
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"], project_id="project-a")

            code, output = run_cli(["--config", str(config_path), "list", "--verbose"])
            rows = {row["ID"]: row for row in fixed_table_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual("blocked_dependency", rows["child"]["STATUS"])
            self.assertEqual("runnable", rows["child"]["RAW_STATUS"])
            self.assertEqual("dep (blocked)", rows["child"]["DEPS"])
            self.assertNotIn("blocked by dep", rows["child"]["NOTE"])

    def test_list_verbose_does_not_change_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="json-task", project_id="project-a")
            task["depends_on"] = []
            task["last_result"] = {"status": "completed", "summary": "done"}
            task["last_run"] = {"command_kind": "exec", "returncode": 0, "duration_seconds": 1.25}
            save_task(config, task)

            plain_code, plain_output = run_cli(["--config", str(config_path), "list", "--json"])
            verbose_code, verbose_output = run_cli(["--config", str(config_path), "list", "--json", "--verbose"])

            self.assertEqual(0, plain_code)
            self.assertEqual(0, verbose_code)
            self.assertEqual(json.loads(plain_output), json.loads(verbose_output))
            self.assertEqual([], json.loads(plain_output)[0]["depends_on"])
            self.assertEqual("work", json.loads(plain_output)[0]["title"])
            self.assertIsNone(json.loads(plain_output)[0]["description"])

    def test_transcript_prints_sanitized_readable_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt", tmp, task_id="task-transcript")
            log_path = Path(tmp) / "logs" / "task-transcript" / "attempt-1.jsonl"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}}),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "agent_message", "message": "token=private-value done"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            task["log_paths"] = [str(log_path)]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "transcript", "task-transcript"])

            self.assertEqual(0, code)
            self.assertIn("## attempt 1: attempt-1.jsonl", output)
            self.assertIn("### user", output)
            self.assertIn("hello", output)
            self.assertIn("token [REDACTED]", output)
            self.assertNotIn("private-value", output)

    def test_follow_prints_compact_sanitized_attempt_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt with token=private-value", tmp, task_id="task-follow")
            task["status"] = "completed"
            log_path = config.log_dir / "task-follow" / "attempt-1.jsonl"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "raw prompt"}}),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "agent_message", "message": "working token=private-value"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "function_call",
                                "name": "exec_command",
                                "arguments": '{"cmd": "python3 -m unittest", "cwd": "/Users/alice/repo"}',
                            }
                        ),
                        json.dumps({"type": "function_call_output", "output": '{"exit_code": 0, "output": "ok"}'}),
                        json.dumps({"type": "error", "message": "usage limit reached; try again later"}),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "response": {
                                    "task_id": "task-follow",
                                    "status": "completed",
                                    "summary": "done with secret=private-value",
                                    "next_prompt": "private continuation",
                                    "changed_files": ["README.md"],
                                    "verification": ["unit tests"],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            task["log_paths"] = [str(log_path)]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "follow", "task-follow", "--lines", "20"])

            self.assertEqual(0, code)
            self.assertIn("==> attempt-1.jsonl <==", output)
            self.assertIn("assistant: working token [REDACTED]", output)
            self.assertIn("command start: exec_command", output)
            self.assertIn("/Users/[USER]/repo", output)
            self.assertIn("command finish: exit=0", output)
            self.assertIn("rate-limit: markers=", output)
            self.assertIn('final: {"changed_files": ["README.md"]', output)
            self.assertIn('"next_prompt": "[REDACTED]"', output)
            self.assertNotIn("raw prompt", output)
            self.assertNotIn("private-value", output)
            self.assertNotIn("private continuation", output)

    def test_follow_tails_initial_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt", tmp, task_id="tail")
            task["status"] = "completed"
            log_path = config.log_dir / "tail" / "attempt-1.jsonl"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                "\n".join(
                    json.dumps({"type": "agent_message", "message": f"line {index}"}) for index in range(4)
                )
                + "\n",
                encoding="utf-8",
            )
            task["log_paths"] = [str(log_path)]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "follow", "tail", "--lines", "2"])

            self.assertEqual(0, code)
            self.assertNotIn("line 0", output)
            self.assertNotIn("line 1", output)
            self.assertIn("line 2", output)
            self.assertIn("line 3", output)

    def test_follow_waits_for_running_task_log_to_appear_and_exits_when_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt", tmp, task_id="running-follow")
            task["status"] = "running"
            save_task(config, task)
            log_path = config.log_dir / "running-follow" / "attempt-1.jsonl"

            def finish_task() -> None:
                time.sleep(0.02)
                log_path.parent.mkdir(parents=True)
                log_path.write_text(
                    json.dumps({"type": "agent_message", "message": "appeared"}) + "\n",
                    encoding="utf-8",
                )
                loaded = load_task(config, "running-follow")
                loaded["status"] = "completed"
                loaded["log_paths"] = [str(log_path)]
                save_task(config, loaded)

            worker = threading.Thread(target=finish_task)
            worker.start()
            try:
                code, output = run_cli(
                    ["--config", str(config_path), "follow", "running-follow", "--poll-interval", "0.005"]
                )
            finally:
                worker.join(timeout=1)

            self.assertEqual(0, code)
            self.assertIn("==> attempt-1.jsonl <==", output)
            self.assertIn("assistant: appeared", output)

    def test_summary_prints_compact_review_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "prompt",
                tmp,
                task_id="task-summary",
                project_id="project-a",
                category="implementation",
                labels=["queue"],
                created_by="test",
                routing_reason="runner-state risk",
                routing_risk_factors=["queue-mutation"],
                routing_experiment="upshift_guard",
                routing_size="medium",
                routing_risk="high",
                verification_scope=["unit", "integration"],
            )
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "task-summary",
                "status": "completed",
                "summary": "Implemented token=private-value handling.",
                "next_prompt": "",
                "commits": ["abc1234 redact private handling"],
                "push_status": {"ahead": 1, "behind": 0},
                "changed_files": ["src/example.py"],
                "verification": ["python3 -m unittest"],
            }
            task["git_status"] = {
                "branch": "main",
                "comparison_ref": "origin/main",
                "ahead": 1,
                "behind": 0,
                "has_unpushed": True,
                "dirty": False,
                "unpushed_commits": ["abc1234 redact private handling"],
            }
            task["log_paths"] = [str(Path(tmp) / "logs" / "attempt-1.jsonl")]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "summary", "task-summary"])

            self.assertEqual(0, code)
            self.assertIn("# task task-summary", output)
            self.assertIn("status: completed", output)
            self.assertIn("review_status: unreviewed", output)
            self.assertIn("project_id: project-a", output)
            self.assertIn("category: implementation", output)
            self.assertIn("labels: queue", output)
            self.assertIn(
                "routing: routing_experiment=upshift_guard, routing_size=medium, routing_risk=high, "
                "routing_reason=runner-state risk, routing_risk_factors=queue-mutation, "
                "verification_scope=unit,integration",
                output,
            )
            self.assertIn("summary:", output)
            self.assertIn("Implemented token [REDACTED] handling.", output)
            self.assertIn("commits:", output)
            self.assertIn("- abc1234 redact private handling", output)
            self.assertIn("push_status:", output)
            self.assertIn("ahead: 1", output)
            self.assertIn("## git_status", output)
            self.assertIn("has_unpushed: True", output)
            self.assertIn("- src/example.py", output)
            self.assertIn("- python3 -m unittest", output)
            self.assertIn("## logs", output)
            self.assertNotIn("private-value", output)

    def test_list_and_summary_show_execution_profile_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, extra={"execution_profiles": {"small": {"model": "gpt-5-small"}}})
            config = Config.load(str(config_path))
            create_task(
                config,
                "work",
                tmp,
                task_id="profiled",
                project_id="project-a",
                execution_profile="small",
                model="gpt-5-small",
                codex_profile="batch-small",
            )

            list_code, list_output = run_cli(["--config", str(config_path), "list", "--color", "never"])
            summary_code, summary_output = run_cli(["--config", str(config_path), "summary", "profiled"])

            self.assertEqual(0, list_code)
            self.assertIn("[S] work", list_output)
            self.assertNotIn("profile=small", list_output)
            self.assertNotIn("model=gpt-5-small", list_output)
            verbose_code, verbose_output = run_cli(["--config", str(config_path), "list", "--verbose"])
            verbose_rows = {row["ID"]: row for row in fixed_table_rows(verbose_output)}
            self.assertEqual(0, verbose_code)
            self.assertEqual("profile=small model=gpt-5-small codex_profile=batch-small", verbose_rows["profiled"]["PROFILE"])
            self.assertEqual(0, summary_code)
            self.assertIn(
                "execution: execution_profile=small, model=gpt-5-small, codex_profile=batch-small",
                summary_output,
            )

    def test_summary_and_list_show_startup_stall_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt", tmp, task_id="task-stalled")
            task["status"] = "runnable"
            task["startup_stalled_at"] = "2026-06-20T12:00:00+00:00"
            task["startup_stall_count"] = 1
            task["last_error"] = "codex startup stalled before meaningful JSONL events"
            task["last_progress"] = {
                "first_jsonl_event_at": "2026-06-20T12:00:01+00:00",
                "last_jsonl_event_at": "2026-06-20T12:00:02+00:00",
                "first_meaningful_event_at": None,
                "last_meaningful_event_type": None,
                "stdout_empty": False,
                "only_startup_events": True,
                "jsonl_event_count": 2,
                "startup_event_count": 2,
                "meaningful_event_count": 0,
                "terminated_by_watchdog": True,
                "watchdog_reason": "startup_stall",
                "termination_signal": "SIGTERM",
            }
            save_task(config, task)
            historical = create_task(config, "prompt", tmp, task_id="task-stall-history")
            historical["status"] = "completed"
            historical["review_status"] = "accepted"
            historical["startup_stalled_at"] = "2026-06-20T11:00:00+00:00"
            historical["startup_stall_count"] = 1
            save_task(config, historical)

            list_code, list_output = run_cli(["--config", str(config_path), "list", "--all"])
            summary_code, summary_output = run_cli(["--config", str(config_path), "summary", "task-stalled"])

            self.assertEqual(0, list_code)
            rows = {row["ID"]: row for row in compact_list_rows(list_output)}
            self.assertIn("startup stalled; retrying", rows["task-stalled"]["NOTE"])
            self.assertIn("startup stalled earlier", rows["task-stall-history"]["NOTE"])
            self.assertEqual(0, summary_code)
            self.assertIn("startup_stalled_at: 2026-06-20T12:00:00+00:00", summary_output)
            self.assertIn("## last_progress", summary_output)
            self.assertIn("only_startup_events: True", summary_output)
            self.assertIn("watchdog_reason: startup_stall", summary_output)

    def test_summary_shows_dependency_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "dependency", tmp, task_id="dep")
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            code, output = run_cli(["--config", str(config_path), "summary", "child"])

            self.assertEqual(0, code)
            self.assertIn("dependencies: dep", output)
            self.assertIn("dependencies_ready: false", output)
            self.assertIn("blocked_by: dep", output)

    def test_review_bundle_prints_human_report_with_working_tree_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            (repo / "file.txt").write_text("base\nchange\n", encoding="utf-8")
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            dep = create_task(config, "dependency", str(repo), task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "accepted"
            save_task(config, dep)
            task = create_task(config, "Implement token=private-value handling.", str(repo), task_id="bundle", depends_on=["dep"])
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "bundle",
                "status": "completed",
                "summary": "Changed token=private-value handling.",
                "next_prompt": "",
                "changed_files": ["file.txt"],
                "verification": ["python3 -m unittest"],
            }
            task["last_run"] = {"command_kind": "exec", "returncode": 0, "duration_seconds": 1.2}
            task["log_paths"] = ["/Users/example/.codex-batch-runner/logs/attempt.jsonl"]
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-bundle", "bundle"])

            self.assertEqual(0, code)
            self.assertIn("# review bundle bundle", output)
            self.assertIn("## prompt_excerpt", output)
            self.assertIn("token [REDACTED]", output)
            self.assertIn('"kind": "working_tree"', output)
            self.assertIn("+change", output)
            self.assertIn("python3 -m unittest", output)
            self.assertIn("## current_git_repository", output)
            self.assertIn("transcript_contents_included: False", output)
            self.assertNotIn("private-value", output)
            self.assertNotIn("/Users/example", output)

    def test_review_bundle_json_output_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "work",
                tmp,
                task_id="bundle-json",
                project_id="project-a",
                routing_reason="baseline route",
                routing_risk_factors=["normal-risk"],
                routing_experiment="baseline",
                routing_size="medium",
                routing_risk="medium",
                verification_scope=["unit"],
            )
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["last_result"] = {
                "task_id": "bundle-json",
                "status": "completed",
                "summary": "done",
                "changed_files": ["README.md"],
                "verification": ["unit tests"],
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-bundle", "bundle-json", "--json"])
            bundle = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("bundle-json", bundle["task"]["id"])
            self.assertEqual("baseline route", bundle["task"]["routing_reason"])
            self.assertEqual(["normal-risk"], bundle["task"]["routing_risk_factors"])
            self.assertEqual("baseline", bundle["task"]["routing_experiment"])
            self.assertEqual("medium", bundle["task"]["routing_size"])
            self.assertEqual("medium", bundle["task"]["routing_risk"])
            self.assertEqual(["unit"], bundle["task"]["verification_scope"])
            self.assertEqual("completed", bundle["status"])
            self.assertEqual(["unit tests"], bundle["verification"])
            self.assertFalse(bundle["transcript_contents_included"])
            self.assertIn("task_git_status_snapshot", bundle)
            self.assertIn("current_git_repository", bundle)
            self.assertIn("git_repository", bundle)
            self.assertIn("safety_policy", bundle)

    def test_review_bundle_reports_missing_git_fallback_without_guessing_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="no-git")
            task["status"] = "completed"
            task["last_result"] = {
                "task_id": "no-git",
                "status": "completed",
                "summary": "done",
                "commits": ["local change without hash"],
                "changed_files": [],
                "verification": [],
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-bundle", "no-git", "--json"])
            bundle = json.loads(output)

            self.assertEqual(0, code)
            self.assertFalse(bundle["git_repository"]["available"])
            self.assertEqual("unavailable", bundle["commit_information"]["status"])
            self.assertIn("git repository unavailable", bundle["git_diff"]["warnings"])

    def test_review_bundle_sanitizes_obvious_secret_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "Use api_key=abc123 and bearer secret-token in /Users/alice/repo.", tmp, task_id="sanitize")
            task["status"] = "failed"
            task["last_error"] = "password=hunter2 in /Users/alice/repo"
            task["last_result"] = {
                "task_id": "sanitize",
                "status": "failed",
                "summary": "secret=private-value",
                "changed_files": [],
                "verification": ["TOKEN=private-value command"],
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-bundle", "sanitize", "--json"])

            self.assertEqual(0, code)
            self.assertNotIn("abc123", output)
            self.assertNotIn("hunter2", output)
            self.assertNotIn("private-value", output)
            self.assertNotIn("/Users/alice", output)
            self.assertIn("[REDACTED]", output)

    def test_review_next_dry_run_selects_oldest_review_needed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id, completed_at in (("newer", "2026-01-02T00:00:00+00:00"), ("older", "2026-01-01T00:00:00+00:00")):
                task = create_task(config, "work", tmp, task_id=task_id)
                task["status"] = "completed"
                task["review_status"] = "unreviewed"
                task["completed_at"] = completed_at
                task["last_result"] = {
                    "task_id": task_id,
                    "status": "completed",
                    "summary": f"{task_id} done",
                    "changed_files": ["README.md"],
                    "verification": ["unit tests"],
                }
                task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
                save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run"])

            self.assertEqual(0, code)
            self.assertIn("selected: true", output)
            self.assertIn("task_id: older", output)
            self.assertIn("dry_run: no task state changed", output)

    def test_review_next_dry_run_noops_when_no_review_needed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="accepted")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run"])

            self.assertEqual(0, code)
            self.assertIn("selected: false", output)
            self.assertIn("no completed task needs review", output)

    def test_review_next_dry_run_filters_by_project_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            other_root = Path(tmp) / "other"
            other_root.mkdir()
            for task_id, cwd, project_id, category, labels in (
                ("match", tmp, "project-a", "implementation", ["queue"]),
                ("other", str(other_root), "project-b", "docs", ["readme"]),
            ):
                task = create_task(config, "work", cwd, task_id=task_id, project_id=project_id, category=category, labels=labels)
                task["status"] = "completed"
                task["review_status"] = "unreviewed"
                task["last_result"] = {
                    "task_id": task_id,
                    "status": "completed",
                    "summary": "done",
                    "changed_files": [],
                    "verification": ["unit tests"],
                }
                task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
                save_task(config, task)

            filters = (
                ["--project", "project-a"],
                ["--project-root", tmp],
                ["--category", "implementation"],
                ["--label", "queue"],
            )
            for filter_args in filters:
                with self.subTest(filter_args=filter_args):
                    code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run", *filter_args])

                    self.assertEqual(0, code)
                    self.assertIn("task_id: match", output)
                    self.assertNotIn("task_id: other", output)

    def test_review_next_json_output_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="json-review", project_id="project-a")
            task["status"] = "completed"
            task["review_status"] = "needs_followup"
            task["last_result"] = {
                "task_id": "json-review",
                "status": "completed",
                "summary": "done",
                "changed_files": ["README.md"],
                "verification": ["unit tests"],
            }
            task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["selected"])
            self.assertEqual("json-review", report["task_id"])
            self.assertEqual("needs_followup", report["review_status"])
            self.assertFalse(report["mutated"])
            self.assertIn("gates", report)
            self.assertIn("bundle", report)
            self.assertEqual("json-review", report["bundle"]["task"]["id"])

    def test_review_next_dry_run_reports_allowed_auto_fix_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_max_fix_loops_per_task=1,
            )
            config = Config.load(str(config_path))
            task = create_clean_completed_task(config, repo, "planner-allowed")
            task["category"] = "docs"
            task["labels"] = ["review"]
            task["reviewer_codex"] = {
                "decision": "needs_fix",
                "confidence": "high",
                "reason": "documentation update is incomplete",
                "findings": [{"severity": "warning", "summary": "missing docs", "evidence": "spec mismatch"}],
                "required_human_checks": [],
                "auto_fix_allowed": True,
                "auto_fix_risk": "low",
                "suggested_fix_prompt": "Update docs/spec.md to match the README behavior.",
                "finding_fingerprints": ["missing-docs:docs-spec"],
            }
            save_task(config, task)
            before = load_task(config, "planner-allowed")

            code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            report = json.loads(output)
            after = load_task(config, "planner-allowed")

            self.assertEqual(0, code)
            self.assertEqual(before, after)
            planner = report["auto_fix_planner"]
            self.assertTrue(planner["allowed"])
            self.assertEqual([], planner["skip_reasons"])
            draft = planner["fix_task_draft"]
            self.assertEqual("planner-allowed", draft["root_task_id"])
            self.assertEqual("planner-allowed", draft["parent_task_id"])
            self.assertEqual(1, draft["review_cycle"])
            self.assertEqual("repo", draft["project_id"])
            self.assertEqual("docs", draft["category"])
            self.assertEqual(["review"], draft["labels"])
            self.assertEqual(str(repo), draft["cwd"])
            self.assertEqual([], draft["depends_on"])
            self.assertEqual("auto_review_fix", draft["subtask_type"])
            self.assertEqual("planner-allowed", draft["subtask_for"])
            self.assertTrue(draft["blocks_root_completion"])
            self.assertIn("unit tests", draft["required_verification_summary"])
            self.assertIn("Update docs/spec.md", draft["bounded_prompt_summary"])

            human_code, human_output = run_cli(["--config", str(config_path), "review-next", "--dry-run"])
            self.assertEqual(0, human_code)
            self.assertIn("auto_fix_planner: allowed=true", human_output)
            self.assertIn("fix_task_draft: root=planner-allowed", human_output)
            self.assertIn("subtask_type=auto_review_fix", human_output)

    def test_review_next_dry_run_reports_auto_fix_skip_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_clean_completed_task(config, repo, "planner-skipped")
            task["fix_attempts"] = 1
            task["finding_fingerprints"] = ["same-finding"]
            task["reviewer_codex"] = {
                "decision": "needs_fix",
                "confidence": "low",
                "reason": "auth behavior is ambiguous",
                "findings": [{"severity": "error", "summary": "same issue", "evidence": "same evidence"}],
                "required_human_checks": ["confirm policy"],
                "auto_fix_allowed": True,
                "auto_fix_risk": "high",
                "suggested_fix_prompt": "",
                "finding_fingerprints": ["same-finding"],
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            planner = report["auto_fix_planner"]
            self.assertFalse(planner["allowed"])
            self.assertIsNone(planner["fix_task_draft"])
            codes = {item["code"] for item in planner["skip_reasons"]}
            self.assertIn("disabled_config", codes)
            self.assertIn("confidence_risk_mismatch", codes)
            self.assertIn("missing_suggested_fix_prompt", codes)
            self.assertIn("repeated_finding", codes)
            self.assertIn("cooldown_limit_stale_gate", codes)
            self.assertIn("high_risk_blocker", codes)

    def test_review_next_dry_run_does_not_mutate_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="readonly")
            task["status"] = "completed"
            task["review_status"] = "rejected"
            task["last_result"] = {
                "task_id": "readonly",
                "status": "completed",
                "summary": "done",
                "changed_files": [],
                "verification": ["unit tests"],
            }
            task["git_status"] = {"has_unpushed": False, "ahead": 0, "dirty": False}
            save_task(config, task)
            before = load_task(config, "readonly")

            code, _ = run_cli(["--config", str(config_path), "review-next", "--dry-run"])
            after = load_task(config, "readonly")

            self.assertEqual(0, code)
            self.assertEqual(before, after)

    def test_review_next_defaults_to_read_only_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="readonly-default")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            save_task(config, task)

            code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "review-next"])

            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            self.assertIn("mode: dry-run", output)
            self.assertEqual("unreviewed", load_task(config, "readonly-default")["review_status"])

    def test_review_next_mechanical_auto_accept_requires_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(
                ["--config", str(config_path), "review-next", "--mechanical-auto-accept"]
            )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("--mechanical-auto-accept requires --apply", stderr)

    def test_review_next_apply_refuses_without_explicit_auto_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "not-enabled")

            code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertFalse(report["mutated"])
            self.assertEqual("needs_human", report["auto_review"]["decision"])
            self.assertIn("disabled", report["auto_review"]["reason"])
            self.assertEqual("unreviewed", load_task(config, "not-enabled")["review_status"])

    def test_review_next_mechanical_auto_accept_marks_task_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "auto-accept")

            code, output = run_cli(
                ["--config", str(config_path), "review-next", "--apply", "--mechanical-auto-accept", "--json"]
            )
            report = json.loads(output)
            task = load_task(config, "auto-accept")

            self.assertEqual(0, code)
            self.assertTrue(report["mutated"])
            self.assertEqual("accepted", report["auto_review"]["decision"])
            self.assertFalse(report["auto_review"]["reviewer_codex_invoked"])
            self.assertFalse(report["auto_review"]["follow_up_enqueued"])
            self.assertEqual("accepted", task["review_status"])
            self.assertEqual("auto-accepted by local mechanical review gates", task["review_reason"])

    def test_review_next_prefers_mechanical_safe_accept_for_local_only_when_reviewer_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / ".gitignore").write_text("*.local.md\n*.local.plist\n.codex-batch-runner/\n", encoding="utf-8")
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", ".gitignore", "file.txt")
            git(repo, "commit", "-m", "initial")
            (repo / "TASKS.local.md").write_text("- local operator task\n", encoding="utf-8")
            config_path = write_config(
                tmp,
                auto_review_mechanical_accept=True,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
            )
            config = Config.load(str(config_path))
            task = create_clean_completed_task(config, repo, "local-only")
            task["last_result"]["changed_files"] = ["TASKS.local.md"]
            task["last_result"]["commits"] = []
            task["last_result"]["verification"] = [
                "git status --short: clean",
                "git diff --stat: no output",
                "git diff --check: passed",
            ]
            save_task(config, task)

            with patch("codex_batch_runner.review_next.run_reviewer_codex") as reviewer:
                code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--json"])
            report = json.loads(output)
            task = load_task(config, "local-only")

            self.assertEqual(0, code)
            self.assertTrue(report["mutated"])
            self.assertEqual("accepted", report["auto_review"]["decision"])
            self.assertTrue(report["auto_review"]["mechanical_safe_accept"])
            self.assertFalse(report["auto_review"]["reviewer_codex_invoked"])
            reviewer.assert_not_called()
            self.assertEqual("accepted", task["review_status"])
            self.assertEqual("auto-accepted by narrow local-only mechanical review gates", task["review_reason"])
            self.assertNotIn("reviewer_codex", task)

    def test_review_next_mechanical_safe_accept_bypasses_reviewer_bundle_limit_for_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / ".gitignore").write_text("*.local.md\n.codex-batch-runner/\n", encoding="utf-8")
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", ".gitignore", "file.txt")
            git(repo, "commit", "-m", "initial")
            (repo / ".codex-batch-runner").mkdir()
            (repo / ".codex-batch-runner" / "TODO.local.md").write_text("- private todo\n", encoding="utf-8")
            config_path = write_config(
                tmp,
                auto_review_mechanical_accept=True,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                extra={"auto_review_codex_max_bundle_chars": 1, "auto_review_codex_max_diff_chars": 1},
            )
            config = Config.load(str(config_path))
            task = create_clean_completed_task(config, repo, "local-limit")
            task["last_result"]["changed_files"] = [".codex-batch-runner/TODO.local.md"]
            task["last_result"]["commits"] = []
            task["last_result"]["verification"] = [
                "git status --short confirmed no tracked/public file changes",
                "git diff --check: passed",
            ]
            save_task(config, task)

            with patch("codex_batch_runner.review_next.run_reviewer_codex") as reviewer:
                code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--json"])
            report = json.loads(output)
            task = load_task(config, "local-limit")

            self.assertEqual(0, code)
            self.assertTrue(report["mutated"])
            self.assertEqual("accepted", report["auto_review"]["decision"])
            self.assertTrue(report["auto_review"]["mechanical_safe_accept"])
            self.assertFalse(report["auto_review"]["reviewer_codex_invoked"])
            self.assertNotIn("reviewer_limit_exceeded", report["auto_review"])
            reviewer.assert_not_called()
            self.assertEqual("accepted", task["review_status"])
            self.assertNotIn("reviewer_codex", task)

    def test_review_next_routes_semantic_tracked_change_to_reviewer_when_both_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            (repo / "file.txt").write_text("base\nsemantic change\n", encoding="utf-8")
            git(repo, "commit", "-am", "semantic change")
            task_commit = git(repo, "rev-parse", "HEAD")
            config_path = write_config(
                tmp,
                auto_review_mechanical_accept=True,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
            )
            config = Config.load(str(config_path))
            task = create_clean_completed_task(config, repo, "semantic-public")
            task["last_result"]["commits"] = [task_commit]
            task["last_result"]["verification"] = [
                "git status --short: clean",
                "git diff --check: passed",
                "unit tests passed",
            ]
            save_task(config, task)

            with patch(
                "codex_batch_runner.review_next.run_reviewer_codex",
                return_value=ReviewerCodexOutcome(
                    invoked=True,
                    decision="pass",
                    reason="pass",
                    result=reviewer_pass_result("semantic-public"),
                ),
            ) as reviewer:
                code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--json"])
            report = json.loads(output)
            task = load_task(config, "semantic-public")

            self.assertEqual(0, code)
            self.assertTrue(report["mutated"])
            self.assertEqual("accepted", report["auto_review"]["decision"])
            self.assertTrue(report["auto_review"]["reviewer_codex_invoked"])
            self.assertNotIn("mechanical_safe_accept", report["auto_review"])
            reviewer.assert_called_once()
            self.assertEqual("pass", task["reviewer_codex"]["decision"])
            self.assertIn("reviewer Codex clear pass", task["review_reason"])

    def test_review_next_reviewer_codex_pass_accepts_task_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                codex_command=[sys.executable, str(FAKE_CODEX), "reviewer_pass"],
            )
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "reviewer-pass")

            code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)
            task = load_task(config, "reviewer-pass")

            self.assertEqual(0, code)
            self.assertTrue(report["mutated"])
            self.assertEqual("accepted", report["auto_review"]["decision"])
            self.assertTrue(report["auto_review"]["reviewer_codex_invoked"])
            self.assertEqual("pass", report["auto_review"]["reviewer_codex_result"]["decision"])
            self.assertEqual("accepted", task["review_status"])
            self.assertIn("reviewer Codex clear pass", task["review_reason"])

    def test_review_next_reviewer_codex_needs_fix_records_summary_without_accepting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                codex_command=[sys.executable, str(FAKE_CODEX), "reviewer_needs_fix"],
            )
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "reviewer-fix")

            code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)
            task = load_task(config, "reviewer-fix")

            self.assertEqual(0, code)
            self.assertTrue(report["mutated"])
            self.assertEqual("needs_fix", report["auto_review"]["decision"])
            self.assertTrue(report["auto_review"]["reviewer_codex_invoked"])
            self.assertEqual("unreviewed", task["review_status"])
            self.assertEqual("needs_fix", task["reviewer_codex"]["decision"])
            self.assertIn("Update docs/spec.md", task["reviewer_codex"]["suggested_fix_prompt"])
            self.assertTrue(task["reviewer_codex"]["auto_fix_allowed"])
            self.assertEqual("low", task["reviewer_codex"]["auto_fix_risk"])
            self.assertEqual(["missing-docs:docs-spec"], task["reviewer_codex"]["finding_fingerprints"])
            self.assertEqual("needs_fix", task["chain_status"])
            self.assertEqual("reviewer-fix", task["root_task_id"])
            self.assertEqual("needs_fix", task["last_review_decision"])
            self.assertEqual(1, task["review_attempts"])
            self.assertEqual(0, task["fix_attempts"])
            self.assertTrue(task["auto_fix_allowed"])
            self.assertEqual(["missing-docs:docs-spec"], task["finding_fingerprints"])
            self.assertIsNone(task["last_auto_fix_task_id"])
            self.assertFalse(report["auto_review"]["follow_up_enqueued"])
            self.assertEqual("needs_fix", report["chain"]["chain_status"])

            summary_code, summary_output = run_cli(["--config", str(config_path), "summary", "reviewer-fix"])
            list_code, list_output = run_cli(["--config", str(config_path), "list", "--project", "repo"])
            bundle_code, bundle_output = run_cli(["--config", str(config_path), "review-bundle", "reviewer-fix", "--json"])
            bundle = json.loads(bundle_output)

            self.assertEqual(0, summary_code)
            self.assertIn("chain_status=needs_fix", summary_output)
            self.assertIn("## reviewer_codex", summary_output)
            self.assertEqual(0, list_code)
            self.assertIn("fix requested", list_output)
            self.assertEqual(0, bundle_code)
            self.assertEqual("needs_fix", bundle["chain"]["chain_status"])
            self.assertEqual("needs_fix", bundle["reviewer_codex"]["decision"])

    def test_review_next_reviewer_codex_needs_fix_enqueues_one_auto_fix_when_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                auto_review_codex_max_fix_loops_per_task=1,
                codex_command=[sys.executable, str(FAKE_CODEX), "reviewer_needs_fix"],
            )
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "reviewer-fix-enqueue")

            code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)
            parent = load_task(config, "reviewer-fix-enqueue")
            fix_task_id = report["auto_review"]["follow_up_task_id"]
            fix_task = load_task(config, fix_task_id)
            tasks = list_tasks(config)
            events = list_events(config, limit=0)

            self.assertEqual(0, code)
            self.assertTrue(report["mutated"])
            self.assertEqual("needs_fix", report["auto_review"]["decision"])
            self.assertTrue(report["auto_review"]["follow_up_enqueued"])
            self.assertEqual(2, len(tasks))
            self.assertEqual(fix_task_id, parent["last_auto_fix_task_id"])
            self.assertEqual("fixing", parent["chain_status"])
            self.assertEqual(1, parent["fix_attempts"])
            self.assertEqual([fix_task_id], parent["blocking_subtask_ids"])
            self.assertEqual("runnable", fix_task["status"])
            self.assertEqual("reviewer-fix-enqueue", fix_task["root_task_id"])
            self.assertEqual("reviewer-fix-enqueue", fix_task["parent_task_id"])
            self.assertEqual([], fix_task["depends_on"])
            self.assertEqual("auto_review_fix", fix_task["subtask_type"])
            self.assertEqual("reviewer-fix-enqueue", fix_task["subtask_for"])
            self.assertTrue(fix_task["blocks_root_completion"])
            self.assertEqual(1, fix_task["review_cycle"])
            self.assertEqual(1, fix_task["fix_attempts"])
            self.assertEqual("fixing", fix_task["chain_status"])
            self.assertIn("Update docs/spec.md", fix_task["prompt"])
            self.assertIn("Preserve cbr final JSON schema requirements", fix_task["prompt"])
            self.assertFalse(any("synthetic-session" in json.dumps(event) for event in events))
            self.assertTrue(any(event["event_type"] == "task_auto_fix_enqueued" for event in events))

    def test_auto_fix_subtask_runs_with_strict_dependency_review_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                auto_review_codex_max_fix_loops_per_task=1,
                codex_command=[sys.executable, str(FAKE_CODEX), "reviewer_needs_fix"],
                extra={"dependency_requires_accepted_review": True},
            )
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "strict-parent")

            code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)
            fix_task_id = report["auto_review"]["follow_up_task_id"]
            parent = load_task(config, "strict-parent")
            fix_task = load_task(config, fix_task_id)
            external_child = create_task(config, "external child", str(repo), task_id="external-child", depends_on=["strict-parent"])
            by_id = {task.get("id"): task for task in list_tasks(config)}
            external_ready, external_blockers = dependency_status(
                external_child,
                by_id,
                require_accepted_review=config.dependency_requires_accepted_review,
            )
            selected = select_next_task(config)

            self.assertEqual(0, code)
            self.assertEqual("completed", parent["status"])
            self.assertEqual("unreviewed", parent["review_status"])
            self.assertEqual("fixing", parent["chain_status"])
            self.assertEqual([fix_task_id], parent["blocking_subtask_ids"])
            self.assertEqual([], fix_task["depends_on"])
            self.assertEqual("auto_review_fix", fix_task["subtask_type"])
            self.assertEqual("strict-parent", fix_task["subtask_for"])
            self.assertTrue(fix_task["blocks_root_completion"])
            self.assertFalse(external_ready)
            self.assertEqual(["strict-parent"], external_blockers)
            self.assertEqual(fix_task_id, selected["id"])
            self.assertNotEqual(external_child["id"], selected["id"])

    def test_review_next_auto_fix_disabled_config_refuses_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                codex_command=[sys.executable, str(FAKE_CODEX), "reviewer_needs_fix"],
            )
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "reviewer-fix-disabled")

            code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)
            events = list_events(config, limit=0)

            self.assertEqual(0, code)
            self.assertFalse(report["auto_review"]["follow_up_enqueued"])
            self.assertEqual(1, len(list_tasks(config)))
            self.assertIn("disabled_config", {item["code"] for item in report["auto_review"]["auto_fix_skip_reasons"]})
            self.assertTrue(any(event["event_type"] == "task_auto_fix_skipped" for event in events))

    def test_review_next_auto_fix_exceeded_loop_limit_refuses_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                auto_review_codex_max_fix_loops_per_task=1,
            )
            config = Config.load(str(config_path))
            task = create_clean_completed_task(config, repo, "reviewer-fix-limit")
            task["fix_attempts"] = 1
            save_task(config, task)

            with patch(
                "codex_batch_runner.review_next.run_reviewer_codex",
                return_value=ReviewerCodexOutcome(
                    invoked=True,
                    decision="needs_fix",
                    reason="needs fix",
                    result=reviewer_needs_fix_result(),
                ),
            ):
                code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertFalse(report["auto_review"]["follow_up_enqueued"])
            self.assertEqual(1, len(list_tasks(config)))
            self.assertIn("cooldown_limit_stale_gate", {item["code"] for item in report["auto_review"]["auto_fix_skip_reasons"]})

    def test_review_next_auto_fix_repeated_finding_refuses_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                auto_review_codex_max_fix_loops_per_task=2,
            )
            config = Config.load(str(config_path))
            task = create_clean_completed_task(config, repo, "reviewer-fix-repeat")
            task["fix_attempts"] = 1
            task["finding_fingerprints"] = ["missing-docs:docs-spec"]
            save_task(config, task)

            with patch(
                "codex_batch_runner.review_next.run_reviewer_codex",
                return_value=ReviewerCodexOutcome(
                    invoked=True,
                    decision="needs_fix",
                    reason="needs fix",
                    result=reviewer_needs_fix_result(),
                ),
            ):
                code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertFalse(report["auto_review"]["follow_up_enqueued"])
            self.assertEqual(1, len(list_tasks(config)))
            self.assertIn("repeated_finding", {item["code"] for item in report["auto_review"]["auto_fix_skip_reasons"]})

    def test_review_next_auto_fix_missing_suggested_prompt_refuses_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                auto_review_codex_max_fix_loops_per_task=1,
            )
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "reviewer-fix-missing-prompt")

            with patch(
                "codex_batch_runner.review_next.run_reviewer_codex",
                return_value=ReviewerCodexOutcome(
                    invoked=True,
                    decision="needs_fix",
                    reason="needs fix",
                    result=reviewer_needs_fix_result(suggested_fix_prompt=""),
                ),
            ):
                code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertFalse(report["auto_review"]["follow_up_enqueued"])
            self.assertEqual(1, len(list_tasks(config)))
            self.assertIn("missing_suggested_fix_prompt", {item["code"] for item in report["auto_review"]["auto_fix_skip_reasons"]})

    def test_review_next_auto_fix_stale_task_guard_refuses_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                auto_review_codex_max_fix_loops_per_task=1,
            )
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "reviewer-fix-stale")

            def stale_reviewer(*args: object, **kwargs: object) -> ReviewerCodexOutcome:
                task = load_task(config, "reviewer-fix-stale")
                task["last_result"]["summary"] = "changed while reviewer was running"
                save_task(config, task)
                return ReviewerCodexOutcome(
                    invoked=True,
                    decision="needs_fix",
                    reason="needs fix",
                    result=reviewer_needs_fix_result(),
                )

            with patch("codex_batch_runner.review_next.run_reviewer_codex", side_effect=stale_reviewer):
                code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)
            task = load_task(config, "reviewer-fix-stale")

            self.assertEqual(0, code)
            self.assertFalse(report["auto_review"]["follow_up_enqueued"])
            self.assertIn("stale_task_state", {item["code"] for item in report["auto_review"]["auto_fix_skip_reasons"]})
            self.assertEqual(1, len(list_tasks(config)))
            self.assertEqual("needs_human", task["chain_status"])
            self.assertEqual("needs_human", task["reviewer_codex"]["decision"])

    def test_review_next_reviewer_codex_legacy_needs_fix_is_preserved_without_auto_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                codex_command=[sys.executable, str(FAKE_CODEX), "reviewer_needs_fix_legacy"],
            )
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "reviewer-legacy-fix")

            code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)
            task = load_task(config, "reviewer-legacy-fix")

            self.assertEqual(0, code)
            self.assertTrue(report["mutated"])
            self.assertEqual("needs_fix", report["auto_review"]["decision"])
            self.assertEqual("unreviewed", task["review_status"])
            self.assertEqual("needs_fix", task["reviewer_codex"]["decision"])
            self.assertFalse(task["reviewer_codex"]["auto_fix_allowed"])
            self.assertEqual("", task["reviewer_codex"]["suggested_fix_prompt"])
            self.assertEqual([], task["reviewer_codex"]["finding_fingerprints"])
            self.assertEqual("needs_fix", task["chain_status"])
            self.assertEqual("reviewer-legacy-fix", task["root_task_id"])
            self.assertEqual(1, task["review_attempts"])
            self.assertFalse(task["auto_fix_allowed"])
            self.assertEqual([], task["finding_fingerprints"])
            self.assertFalse(report["auto_review"]["follow_up_enqueued"])

    def test_review_next_reviewer_codex_invalid_json_records_failed_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                codex_command=[sys.executable, str(FAKE_CODEX), "reviewer_invalid"],
            )
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "reviewer-invalid")

            code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)
            task = load_task(config, "reviewer-invalid")

            self.assertEqual(0, code)
            self.assertFalse(report["mutated"])
            self.assertEqual("failed_review", report["auto_review"]["decision"])
            self.assertTrue(report["auto_review"]["reviewer_codex_invoked"])
            self.assertEqual("unreviewed", task["review_status"])
            self.assertEqual("failed_review", task["reviewer_codex"]["decision"])

    def test_review_next_reviewer_codex_rate_limit_sets_reviewer_cooldown_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(
                tmp,
                auto_review_codex_enabled=True,
                auto_review_codex_max_calls_per_run=1,
                codex_command=[sys.executable, str(FAKE_CODEX), "rate_limit"],
            )
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "reviewer-rate-limit")

            code, output = run_cli(["--config", str(config_path), "review-next", "--apply", "--reviewer-codex", "--json"])
            report = json.loads(output)
            state = load_state(config)

            self.assertEqual(0, code)
            self.assertFalse(report["mutated"])
            self.assertEqual("failed_review", report["auto_review"]["decision"])
            self.assertTrue(report["auto_review"]["reviewer_codex_invoked"])
            self.assertTrue(report["auto_review"]["rate_limited"])
            self.assertIsNotNone(state["reviewer_codex_cooldown_until"])
            self.assertEqual("unreviewed", load_task(config, "reviewer-rate-limit")["review_status"])

    def test_review_next_prefers_current_unpushed_state_over_stale_task_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            create_pushed_repo(repo)
            commit = git(repo, "rev-parse", "--short", "HEAD")
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_clean_completed_task(config, repo, "stale-snapshot")
            task["git_status"] = {
                "branch": "main",
                "upstream": "origin/main",
                "comparison_ref": "origin/main",
                "ahead": 1,
                "behind": 0,
                "has_unpushed": True,
                "dirty": False,
                "unpushed_commits": [f"{commit} initial"],
            }
            save_task(config, task)

            dry_code, dry_output = run_cli(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            dry_report = json.loads(dry_output)
            gate_details = {gate["name"]: gate["detail"] for gate in dry_report["gates"]}
            apply_code, apply_output = run_cli(
                ["--config", str(config_path), "review-next", "--apply", "--mechanical-auto-accept", "--json"]
            )
            apply_report = json.loads(apply_output)

            self.assertEqual(0, dry_code)
            self.assertTrue(dry_report["gates_ok"])
            self.assertIn(
                "current_has_unpushed=False; current_ahead=0; snapshot_has_unpushed=True; snapshot_ahead=1",
                gate_details["no_unpushed_commits"],
            )
            self.assertFalse(dry_report["bundle"]["current_git_repository"]["has_unpushed"])
            self.assertTrue(dry_report["bundle"]["task_git_status_snapshot"]["has_unpushed"])
            self.assertEqual(0, apply_code)
            self.assertTrue(apply_report["mutated"])
            self.assertEqual("accepted", apply_report["auto_review"]["decision"])
            self.assertEqual("accepted", load_task(config, "stale-snapshot")["review_status"])

    def test_review_bundle_reports_ancestor_commit_as_acceptable_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            create_pushed_repo(repo)
            config_path = write_config(tmp)
            config = Config.load(str(config_path))

            (repo / "file.txt").write_text("base\ntask change\n", encoding="utf-8")
            git(repo, "commit", "-am", "task change")
            task_commit = git(repo, "rev-parse", "HEAD")
            (repo / "file.txt").write_text("base\ntask change\nlater change\n", encoding="utf-8")
            git(repo, "commit", "-am", "later change")
            git(repo, "push")

            task = create_clean_completed_task(config, repo, "ancestor-commit")
            task["last_result"]["commits"] = [task_commit]
            save_task(config, task)

            bundle_code, bundle_output = run_cli(["--config", str(config_path), "review-bundle", "ancestor-commit", "--json"])
            bundle = json.loads(bundle_output)
            review_code, review_output = run_cli(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            report = json.loads(review_output)
            gate_details = {gate["name"]: gate for gate in report["gates"]}

            self.assertEqual(0, bundle_code)
            self.assertEqual("ancestor", bundle["commit_information"]["ancestry"]["status"])
            self.assertTrue(bundle["commit_information"]["ancestry"]["ok"])
            self.assertEqual(0, review_code)
            self.assertTrue(gate_details["commit_ancestry_acceptable"]["ok"])
            self.assertTrue(report["gates_ok"])

            equal_task = create_clean_completed_task(config, repo, "equal-commit")
            equal_task["last_result"]["commits"] = [git(repo, "rev-parse", "HEAD")]
            save_task(config, equal_task)
            equal_code, equal_output = run_cli(["--config", str(config_path), "review-bundle", "equal-commit", "--json"])
            equal_bundle = json.loads(equal_output)

            self.assertEqual(0, equal_code)
            self.assertEqual("equal", equal_bundle["commit_information"]["ancestry"]["status"])
            self.assertTrue(equal_bundle["commit_information"]["ancestry"]["ok"])

    def test_review_bundle_reports_unreachable_commit_as_human_check_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            create_pushed_repo(repo)
            config_path = write_config(tmp)
            config = Config.load(str(config_path))

            git(repo, "checkout", "-b", "side")
            (repo / "file.txt").write_text("base\nside change\n", encoding="utf-8")
            git(repo, "commit", "-am", "side change")
            side_commit = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "main")
            (repo / "file.txt").write_text("base\nmain change\n", encoding="utf-8")
            git(repo, "commit", "-am", "main change")
            git(repo, "push")

            task = create_clean_completed_task(config, repo, "unreachable-commit")
            task["last_result"]["commits"] = [side_commit]
            save_task(config, task)

            bundle_code, bundle_output = run_cli(["--config", str(config_path), "review-bundle", "unreachable-commit", "--json"])
            bundle = json.loads(bundle_output)
            review_code, review_output = run_cli(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            report = json.loads(review_output)
            gate_details = {gate["name"]: gate for gate in report["gates"]}

            self.assertEqual(0, bundle_code)
            self.assertEqual("not_reachable", bundle["commit_information"]["ancestry"]["status"])
            self.assertFalse(bundle["commit_information"]["ancestry"]["ok"])
            self.assertIn("not reachable", " ".join(bundle["commit_information"]["warnings"]))
            self.assertEqual(0, review_code)
            self.assertFalse(gate_details["commit_ancestry_acceptable"]["ok"])
            self.assertFalse(report["gates_ok"])

    def test_review_next_safety_gate_ignores_private_operational_paths(self) -> None:
        task = {
            "cwd": "/Users/example/private-repo",
            "project_root": "/Users/example/private-repo",
            "prompt": "work in /Users/example/private-repo",
            "log_paths": ["/Users/example/private-repo/.codex-batch-runner/logs/attempt-1.jsonl"],
            "last_result": {
                "summary": "done",
                "changed_files": ["README.md"],
                "verification": ["unit tests"],
            },
        }
        bundle = {
            "prompt_excerpt": "work in /Users/example/private-repo",
            "relevant_log_paths": ["/Users/example/private-repo/.codex-batch-runner/logs/attempt-1.jsonl"],
            "last_result": task["last_result"],
            "changed_files": {"reported": ["README.md"]},
            "verification": ["unit tests"],
            "last_error": None,
        }

        self.assertFalse(detectable_safety_violation(task, bundle))

        task["last_result"]["summary"] = "wrote /Users/example/private-repo/secret.txt"

        self.assertTrue(detectable_safety_violation(task, bundle))

    def test_review_next_gate_failure_leaves_task_unaccepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_clean_completed_task(config, repo, "gate-fail")
            task["last_result"]["verification"] = []
            save_task(config, task)

            code, output = run_cli(
                ["--config", str(config_path), "review-next", "--apply", "--mechanical-auto-accept", "--json"]
            )
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertFalse(report["mutated"])
            self.assertEqual("needs_human", report["auto_review"]["decision"])
            self.assertIn("verification_present", report["auto_review"]["failing_gates"])
            self.assertEqual("unreviewed", load_task(config, "gate-fail")["review_status"])

    def test_review_next_apply_is_stale_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_clean_completed_task(config, repo, "stale")
            bundle = build_review_bundle(task, by_id={}, require_accepted_review=False)
            expected = review_fingerprint(task, bundle)
            task["last_result"]["summary"] = "changed after review gates"
            save_task(config, task)

            result = apply_mechanical_accept(config, "stale", expected)

            self.assertFalse(result["mutated"])
            self.assertEqual("needs_human", result["decision"])
            self.assertIn("stale review state", result["reason"])
            self.assertEqual("unreviewed", load_task(config, "stale")["review_status"])

    def test_review_next_apply_respects_runner_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "initial")
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_clean_completed_task(config, repo, "locked-review")
            config.lock_file.parent.mkdir(parents=True, exist_ok=True)
            config.lock_file.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "hostname": socket.gethostname(),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "task_id": "running-task",
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(
                ["--config", str(config_path), "review-next", "--apply", "--mechanical-auto-accept", "--json"]
            )
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("locked", report["status"])
            self.assertFalse(report["mutated"])
            self.assertEqual("unreviewed", load_task(config, "locked-review")["review_status"])

    def test_dependency_accepted_policy_blocks_dependent_run_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, dependency_requires_accepted_review=True)
            config = Config.load(str(config_path))
            dep = create_task(config, "dependency", tmp, task_id="dep")
            dep["status"] = "completed"
            dep["review_status"] = "unreviewed"
            save_task(config, dep)
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            code, output = run_cli(["--config", str(config_path), "run-next", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("empty", report["status"])
            self.assertEqual("runnable", load_task(config, "child")["status"])

    def test_transcript_includes_codex_session_log_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(config, "prompt", tmp, task_id="task-session")
            task["session_id"] = "session-123"
            save_task(config, task)
            session_path = (
                Path(tmp)
                / "codex-home"
                / "sessions"
                / "2026"
                / "06"
                / "20"
                / "rollout-2026-06-20T00-00-00-session-123.jsonl"
            )
            session_path.parent.mkdir(parents=True)
            session_path.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "session summary"}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"CODEX_HOME": str(Path(tmp) / "codex-home")}):
                code, output = run_cli(["--config", str(config_path), "transcript", "task-session"])

            self.assertEqual(0, code)
            self.assertIn("## codex session: rollout-2026-06-20T00-00-00-session-123.jsonl", output)
            self.assertIn("### assistant", output)
            self.assertIn("session summary", output)


if __name__ == "__main__":
    unittest.main()

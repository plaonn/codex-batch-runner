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
from codex_batch_runner.decision_cards import decision_card_next_action
from codex_batch_runner.evidence import rate_limit_dir
from codex_batch_runner.events import list_events
from codex_batch_runner.fs import write_json_atomic
from codex_batch_runner.queue import create_task, dependency_status, list_tasks, load_task, save_task, select_next_task
from codex_batch_runner.review_bundle import build_review_bundle
from codex_batch_runner.review_next import apply_mechanical_accept, detectable_safety_violation, review_fingerprint
from codex_batch_runner.reviewer_codex import ReviewerCodexOutcome
from codex_batch_runner.state import load_state, set_global_cooldown, set_runner_pause


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


def json_lines(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def requirement_vector(**overrides: str) -> dict:
    dimensions = {
        "reasoning_depth": "medium",
        "context_need": "medium",
        "tool_reliability": "medium",
        "latency_priority": "medium",
        "cost_sensitivity": "medium",
        "review_strictness": "medium",
    }
    dimensions.update(overrides)
    return {"source": "test", "confidence": "medium", "dimensions": dimensions}


def requirement_key(**overrides: str) -> str:
    return " ".join(f"{key}={value}" for key, value in sorted(requirement_vector(**overrides)["dimensions"].items()))


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
    current_project = ""
    for line in lines[1:]:
        section_line = line.rstrip()
        if section_line.startswith("[") and section_line.endswith("]"):
            current_project = section_line.strip("[]")
            current = None
            continue
        if headers in (
            ["TITLE", "STATUS", "ATT", "DEPS", "NOTE"],
            ["[M]", "TITLE", "STATUS", "ATT", "DEPS", "NOTE"],
            ["[M]", "TITLE", "STATUS", "DETAIL"],
        ):
            parsed = {}
            for index, header in enumerate(headers):
                start = starts[index]
                end = starts[index + 1] if index + 1 < len(starts) else None
                parsed[header] = line[start:end].strip()
            if headers == ["[M]", "TITLE", "STATUS", "DETAIL"]:
                if parsed["STATUS"] and parsed["TITLE"]:
                    row = {
                        "ID": compact_title_key(parsed["TITLE"]),
                        "MODEL": parsed.get("[M]", ""),
                        "TITLE": parsed["TITLE"],
                        "PROJECT": current_project,
                        "STATUS": parsed["STATUS"],
                        "ATT": "",
                        "DEPS": "-",
                        "NOTE": parsed["DETAIL"],
                        "DETAIL": parsed["DETAIL"],
                    }
                    rows.append(row)
                    current = row
                    continue
                if current is not None:
                    if parsed["TITLE"]:
                        current["TITLE"] += "\n" + parsed["TITLE"]
                    if parsed["DETAIL"]:
                        if current["DETAIL"] in {"", "-"}:
                            current["DETAIL"] = parsed["DETAIL"]
                            current["NOTE"] = parsed["DETAIL"]
                        else:
                            current["DETAIL"] += "; " + parsed["DETAIL"]
                            current["NOTE"] = current["DETAIL"]
                continue
            if (parsed["STATUS"] or parsed["ATT"]) and parsed["TITLE"]:
                row = {
                    "ID": compact_title_key(parsed["TITLE"]),
                    "MODEL": parsed.get("[M]", ""),
                    "TITLE": parsed["TITLE"],
                    "PROJECT": current_project,
                    "STATUS": strip_status_marker(parsed["STATUS"]),
                    "ATT": parsed["ATT"],
                    "DEPS": parsed["DEPS"],
                    "NOTE": parsed["NOTE"],
                }
                rows.append(row)
                current = row
                continue
            if current is not None:
                if parsed["TITLE"]:
                    current["TITLE"] += "\n" + parsed["TITLE"]
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


def compact_title_key(value: str) -> str:
    value = value.strip()
    for prefix in ("├─ ", "└─ "):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    for marker in ("[S] ", "[N] ", "[D] "):
        if value.startswith(marker):
            value = value[len(marker) :]
    return value.strip().lower().replace(" ", "-")


def strip_status_marker(value: str) -> str:
    for marker in ("..", "||", ">>", "??", "==", "!!", "--"):
        if value.startswith(marker):
            return value[len(marker) :].lstrip()
    return value


def visible_line_widths(output: str) -> list[int]:
    return [len(strip_ansi(line)) for line in output.splitlines()]


def assert_graph_connector_attaches(test_case: unittest.TestCase, output: str, target_marker: str) -> None:
    lines = [strip_ansi(line) for line in output.splitlines()]
    target_index = next(index for index, line in enumerate(lines) if target_marker in line)
    connector_line = lines[target_index - 1]
    test_case.assertIn("|", connector_line)
    test_case.assertEqual(connector_line.index("|"), lines[target_index].index("*"))


def ansi_code_for_visible_char(line: str, visible_index: int) -> str | None:
    position = 0
    visible_position = 0
    active_code: str | None = None
    while position < len(line):
        match = ANSI_RE.match(line, position)
        if match:
            code = match.group(0)
            active_code = None if code == "\033[0m" else code
            position = match.end()
            continue
        if visible_position == visible_index:
            return active_code
        position += 1
        visible_position += 1
    return None


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
    def test_decision_card_next_action_classifies_terminal_observation_and_actionable_cards(self) -> None:
        self.assertEqual("none", decision_card_next_action([]))
        self.assertEqual(
            "none",
            decision_card_next_action(
                [
                    {"user_decision_status": "approved"},
                    {"user_decision_status": "not_approved"},
                ]
            ),
        )
        self.assertEqual(
            "continue_observing",
            decision_card_next_action(
                [
                    {"user_decision_status": "not_ready"},
                    {"user_decision_status": "approved"},
                ]
            ),
        )
        self.assertEqual(
            "fix_invalid_decision_cards",
            decision_card_next_action([{"user_decision_status": "invalid"}]),
        )
        for status in ("decision_required", "approval_blocked", "decision_pending"):
            with self.subTest(status=status):
                self.assertEqual(
                    "review_decision_cards",
                    decision_card_next_action([{"user_decision_status": status}]),
                )

    def test_missing_config_reports_error_without_traceback(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            code, output, stderr = run_cli_with_stderr(["list"])

        self.assertEqual(1, code)
        self.assertEqual("", output)
        self.assertEqual("error: config required: pass --config /path/to/config.json or set CBR_CONFIG\n", stderr)

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

    def test_enqueue_refuses_when_runner_pause_is_active(self) -> None:
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
                            "reason": "control-plane migration",
                            "paused_at": "2026-06-27T00:00:00+09:00",
                            "paused_by": "operator",
                        }
                    }
                ),
                encoding="utf-8",
            )

            code, output, stderr = run_cli_with_stderr(
                ["--config", str(config_path), "enqueue", "--cwd", tmp, "--id", "task", "--prompt", "work"]
            )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertEqual(
                "error: cbr is currently unavailable: runner pause is active: control-plane migration\n",
                stderr,
            )
            self.assertFalse((Path(tmp) / "tasks" / "task.json").exists())
            self.assertFalse(marker.exists())

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
                ["status"],
                ["events"],
                ["rate-limits"],
                ["watching-report"],
                ["prune"],
                ["run-next"],
            ):
                with self.subTest(args=args):
                    code, _ = run_cli(["--config", str(config_path), *args])
                    self.assertIn(code, {0, 1})

            self.assertFalse(marker.exists())

    def test_status_command_is_lightweight_and_read_only_without_queue_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))

            code, output = run_cli(["--config", str(config_path), "status", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertFalse(config.queue_dir.exists())
            self.assertEqual("cbr_status", report["kind"])
            self.assertTrue(report["read_only"])
            self.assertFalse(report["mutation_allowed"])
            self.assertEqual("available", report["admission"]["status"])
            self.assertTrue(report["admission"]["can_enqueue"])
            self.assertTrue(report["admission"]["can_run_next"])
            self.assertEqual("idle", report["admission"]["recommended_action"])
            self.assertEqual(0, report["queue"]["task_count"])
            self.assertEqual(
                {
                    "max_running": 1,
                    "running": 0,
                    "remaining": 1,
                    "blocked": False,
                    "blocker_reason": None,
                },
                report["queue"]["capacity"]["capacity_pools"]["codex"],
            )

            code, output = run_cli(["--config", str(config_path), "status"])

            self.assertEqual(0, code)
            self.assertIn("# cbr status", output)
            self.assertIn("read_only: yes", output)
            self.assertIn("admission", output)
            self.assertIn("## capacity pools", output)
            self.assertIn("remaining=1", output)

    def test_status_command_reports_full_configured_capacity_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={
                    "max_total_running": 3,
                    "max_running_per_project": 3,
                    "capacity_pools": {
                        "codex": {"max_running": 2},
                        "codex-spark": {"max_running": 1},
                    },
                },
            )
            config = Config.load(str(config_path))
            running = create_task(config, "spark work", tmp, task_id="spark-running")
            running["status"] = "running"
            running["capacity_pool"] = "codex-spark"
            save_task(config, running)

            code, output = run_cli(["--config", str(config_path), "status", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            pools = report["queue"]["capacity"]["capacity_pools"]
            self.assertEqual(2, pools["codex"]["remaining"])
            self.assertFalse(pools["codex"]["blocked"])
            self.assertEqual(1, pools["codex-spark"]["running"])
            self.assertEqual(0, pools["codex-spark"]["remaining"])
            self.assertTrue(pools["codex-spark"]["blocked"])
            self.assertEqual("capacity_pool_full", pools["codex-spark"]["blocker_reason"])
            self.assertEqual({"codex-spark": 1}, report["queue"]["capacity"]["running_by_pool"])

            code, output = run_cli(["--config", str(config_path), "status"])

            self.assertEqual(0, code)
            self.assertIn("codex-spark", output)
            self.assertIn("remaining=0", output)
            self.assertIn("capacity_pool_full", output)

    def test_status_command_reports_admission_cooldown_pause_and_review_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="runnable")
            review = create_task(config, "review", tmp, task_id="review")
            review["status"] = "completed"
            review["review_status"] = "unreviewed"
            save_task(config, review)
            apply = create_task(config, "apply", tmp, task_id="apply")
            apply["status"] = "completed"
            apply["review_status"] = "accepted"
            apply["execution_mode"] = "git_worktree"
            apply["execution_worktree_status"] = "retained"
            save_task(config, apply)
            task_path = config.queue_dir / "runnable.json"
            before = task_path.read_text(encoding="utf-8")
            set_global_cooldown(
                config,
                (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
            )

            code, output = run_cli(["--config", str(config_path), "status", "--json"])
            after = task_path.read_text(encoding="utf-8")
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(before, after)
            self.assertEqual("cooldown", report["admission"]["status"])
            self.assertTrue(report["admission"]["can_enqueue"])
            self.assertFalse(report["admission"]["can_run_next"])
            self.assertEqual("do_not_start_runner", report["admission"]["recommended_action"])
            self.assertEqual(["global_cooldown"], report["admission"]["blocked_reasons"])
            self.assertEqual(3, report["queue"]["task_count"])
            self.assertEqual(1, report["queue"]["admissible_count"])
            self.assertEqual(1, report["review"]["needs_review_count"])
            self.assertEqual(1, report["review"]["accepted_unapplied_count"])
            self.assertTrue(report["cooldowns"]["global"]["active"])
            self.assertFalse(report["cooldowns"]["reviewer_codex"]["active"])
            self.assertIn("global_cooldown_until", report["cooldowns"]["global"])
            self.assertIn("reviewer_codex_cooldown_until", report["cooldowns"]["reviewer_codex"])

            set_runner_pause(config, "maintenance", paused_by="test")
            code, output = run_cli(["--config", str(config_path), "status", "--json"])
            paused = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("paused", paused["admission"]["status"])
            self.assertFalse(paused["admission"]["can_enqueue"])
            self.assertFalse(paused["admission"]["can_run_next"])
            self.assertEqual("do_not_enqueue_or_run", paused["admission"]["recommended_action"])
            self.assertEqual(["runner_pause", "global_cooldown"], paused["admission"]["blocked_reasons"])

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

    def test_enqueue_records_model_requirement_metadata(self) -> None:
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
                    "requirement",
                    "--reasoning-depth",
                    "low",
                    "--context-need",
                    "low",
                    "--tool-reliability",
                    "medium",
                    "--latency-priority",
                    "high",
                    "--cost-sensitivity",
                    "high",
                    "--review-strictness",
                    "medium",
                    "--prompt",
                    "work",
                ]
            )
            task = load_task(Config.load(str(config_path)), "requirement")

            self.assertEqual(0, code)
            self.assertEqual("requirement\n", output)
            vector = task["model_requirement_vector"]
            self.assertEqual(2, vector["schema_version"])
            self.assertEqual("legacy-derived", vector["derivation_identity"]["kind"])
            self.assertEqual("explicit_cli", vector["legacy_projection"]["source"])
            self.assertEqual("low", vector["legacy_projection"]["dimensions"]["reasoning_depth"])
            self.assertEqual("high", vector["legacy_projection"]["dimensions"]["cost_sensitivity"])

    def test_enqueue_records_routing_decision_metadata(self) -> None:
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
                    "routed",
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
            self.assertEqual(
                "medium", task["model_requirement_vector"]["legacy_projection"]["dimensions"]["reasoning_depth"]
            )

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

    def test_enqueue_records_external_json_command_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, extra={"external_json_command_timeout_seconds": 45})

            command = [sys.executable, "-c", "print('{\"task_id\":\"external-json\",\"status\":\"completed\",\"summary\":\"ok\",\"changed_files\":[],\"verification\":[]}')"]
            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--id",
                    "external-json",
                    "--backend",
                    "external-json-command",
                    "--external-timeout",
                    "120",
                    "--command-json",
                    json.dumps(command),
                ]
            )
            task = load_task(Config.load(str(config_path)), "external-json")

            self.assertEqual(0, code)
            self.assertEqual("external-json\n", output)
            self.assertEqual("external-json-command", task["execution_backend"])
            self.assertEqual(command, task["external_command"])
            self.assertEqual(120, task["external_timeout_seconds"])
            self.assertEqual("External JSON command task: " + shlex.join(command), task["prompt"])

    def test_worker_selection_rule_routes_codex_task_to_external_json_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker_script = Path(tmp) / "worker.py"
            worker_script.write_text(
                "import json\n"
                "print(json.dumps({"
                "'task_id':'routed-task',"
                "'status':'completed',"
                "'summary':'worker completed',"
                "'changed_files':[],"
                "'verification':['worker route']"
                "}))\n",
                encoding="utf-8",
            )
            command = [sys.executable, str(worker_script), "--model-group", "claude-gpt"]
            config_path = write_config(
                tmp,
                extra={
                    "capacity_pools": {
                        "codex": {"max_running": 1},
                        "antigravity-claude-gpt": {"max_running": 1},
                    },
                    "worker_targets": {
                        "antigravity_review": {
                            "execution_backend": "external-json-command",
                            "capacity_pool": "antigravity-claude-gpt",
                            "external_command": command,
                            "external_timeout_seconds": 120,
                            "worker_family": "antigravity",
                            "model_group": "claude-gpt",
                            "budget_hint": "review",
                        }
                    },
                    "worker_selection_rules": [
                        {
                            "name": "strict-review",
                            "when": {"review_strictness": "high"},
                            "worker_target": "antigravity_review",
                        }
                    ],
                },
            )
            config = Config.load(str(config_path))
            create_task(
                config,
                "review this",
                tmp,
                task_id="routed-task",
                title="Routed review task",
                model_requirement_vector=requirement_vector(review_strictness="high"),
            )

            code, output = run_cli(["--config", str(config_path), "run-next", "--json"])
            task = load_task(Config.load(str(config_path)), "routed-task")

            self.assertEqual(0, code)
            self.assertEqual("completed", json.loads(output)["status"])
            self.assertEqual("completed", task["status"])
            self.assertEqual("external-json-command", task["execution_backend"])
            self.assertEqual("antigravity-claude-gpt", task["capacity_pool"])
            self.assertEqual(command, task["external_command"])
            self.assertEqual(120, task["external_timeout_seconds"])
            self.assertEqual("antigravity_review", task["worker_target"])
            self.assertEqual("strict-review", task["worker_selection_rule"])
            self.assertEqual("claude-gpt", task["worker_model_group"])
            self.assertEqual("worker completed", task["last_result"]["summary"])
            resolved_target = task["last_run"]["resolved_worker_target"]
            self.assertEqual("antigravity_review", resolved_target["worker_target"])
            self.assertEqual("claude-gpt", resolved_target["model_group"])

    def test_explicit_codex_backend_is_not_overridden_by_worker_selection_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                codex_command=[sys.executable, str(FAKE_CODEX), "success"],
                extra={
                    "worker_targets": {
                        "external_review": {
                            "execution_backend": "external-json-command",
                            "external_command": [sys.executable, "-c", "print('{}')"],
                        }
                    },
                    "worker_selection_rules": [
                        {
                            "name": "strict-review",
                            "when": {"review_strictness": "high"},
                            "worker_target": "external_review",
                        }
                    ],
                },
            )

            code, _ = run_cli(
                [
                    "--config", str(config_path), "enqueue", "--cwd", tmp,
                    "--id", "explicit-codex", "--prompt", "review this",
                    "--backend", "codex", "--review-strictness", "high",
                ]
            )
            run_code, run_output = run_cli(["--config", str(config_path), "run-next", "--json"])
            task = load_task(Config.load(str(config_path)), "explicit-codex")

            self.assertEqual(0, code)
            self.assertEqual(0, run_code)
            self.assertEqual("completed", json.loads(run_output)["status"])
            self.assertEqual("codex", task["execution_backend"])
            self.assertTrue(task["execution_backend_explicit"])
            self.assertIsNone(task["external_command"])
            self.assertNotIn("worker_target", task)

    def test_worker_selection_rule_uses_planned_capacity_pool_for_admission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={
                    "max_total_running": 2,
                    "max_running_per_project": 2,
                    "capacity_pools": {
                        "codex": {"max_running": 1},
                        "antigravity-claude-gpt": {"max_running": 1},
                    },
                    "worker_targets": {
                        "antigravity_review": {
                            "execution_backend": "external-json-command",
                            "capacity_pool": "antigravity-claude-gpt",
                            "external_command": [sys.executable, "-c", "print('{}')"],
                            "model_group": "claude-gpt",
                        }
                    },
                    "worker_selection_rules": [
                        {
                            "name": "strict-review",
                            "when": {"review_strictness": "high"},
                            "worker_target": "antigravity_review",
                        }
                    ],
                },
            )
            config = Config.load(str(config_path))
            create_task(config, "already running", tmp, task_id="running-task")
            running = load_task(config, "running-task")
            running["status"] = "running"
            save_task(config, running)
            create_task(
                config,
                "review this",
                tmp,
                task_id="routed-task",
                model_requirement_vector=requirement_vector(review_strictness="high"),
            )

            selected = select_next_task(Config.load(str(config_path)))

            self.assertIsNotNone(selected)
            self.assertEqual("routed-task", selected["id"])

    def test_enqueue_rejects_empty_external_command_json(self) -> None:
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
                    "external-empty",
                    "--backend",
                    "external-json-command",
                    "--command-json",
                    "[]",
                ]
            )

            self.assertEqual(1, code)
            self.assertFalse((Path(tmp) / "tasks" / "external-empty.json").exists())

    def test_enqueue_codex_default_still_requires_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(
                ["--config", str(config_path), "enqueue", "--cwd", tmp, "--id", "missing-prompt"]
            )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("Codex tasks require --prompt or --prompt-file", stderr)

    def test_enqueue_rejects_invalid_model_requirement_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--model-requirement-json",
                    json.dumps({"dimensions": {"reasoning_depth": "extreme"}}),
                    "--prompt",
                    "work",
                ]
            )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("reasoning_depth must be one of", stderr)

    def test_enqueue_rejects_model_requirement_json_with_non_object_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(
                [
                    "--config",
                    str(config_path),
                    "enqueue",
                    "--cwd",
                    tmp,
                    "--model-requirement-json",
                    json.dumps({"dimensions": "bad"}),
                    "--prompt",
                    "work",
                ]
            )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("--model-requirement-json dimensions must be an object", stderr)

    def test_routing_report_groups_profile_category_and_label_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            accepted = create_task(
                config,
                "work",
                tmp,
                task_id="accepted-small",
                project_id="project-a",
                category="implementation",
                labels=["docs", "safe"],
                model_requirement_vector=requirement_vector(reasoning_depth="low", cost_sensitivity="high"),
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
                model_requirement_vector=requirement_vector(reasoning_depth="medium"),
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
                model_requirement_vector=requirement_vector(reasoning_depth="medium"),
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
                model_requirement_vector=requirement_vector(reasoning_depth="low", cost_sensitivity="high"),
            )
            other["status"] = "completed"
            other["review_status"] = "accepted"
            save_task(config, other)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            requirements = {entry["key"]: entry for entry in report["groups"]["model_requirement"]}
            labels = {entry["key"]: entry for entry in report["groups"]["label"]}
            categories = {entry["key"]: entry for entry in report["groups"]["category"]}

            self.assertEqual(0, code)
            self.assertEqual(3, report["task_count"])
            low_key = requirement_key(reasoning_depth="low", cost_sensitivity="high")
            medium_key = requirement_key(reasoning_depth="medium")
            self.assertEqual(1, requirements[low_key]["tasks"])
            self.assertEqual(1, requirements[low_key]["first_pass_accepted"])
            self.assertEqual(1.0, requirements[low_key]["first_pass_accept_rate"])
            self.assertEqual(2, requirements[medium_key]["tasks"])
            self.assertEqual(1, requirements[medium_key]["needs_fix_or_rejected"])
            self.assertEqual(1, requirements[medium_key]["auto_fix_tasks"])
            self.assertEqual(1, requirements[medium_key]["roots_with_auto_fix"])
            self.assertEqual(3, categories["implementation"]["tasks"])
            self.assertEqual(1, labels["docs"]["tasks"])
            self.assertEqual(2, labels["runner"]["tasks"])
            self.assertEqual(1, load_task(config, "accepted-small")["attempts"])

    def test_routing_report_exposes_routing_decision_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "work",
                tmp,
                task_id="downshift",
                project_id="project-a",
                category="docs",
                labels=["docs"],
                model_requirement_vector=requirement_vector(reasoning_depth="low", cost_sensitivity="high"),
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

            code, output = run_cli(
                ["--config", str(config_path), "routing-report", "--project", "project-a", "--limit", "0", "--json"]
            )
            report = json.loads(output)
            experiments = {entry["key"]: entry for entry in report["groups"]["routing_experiment"]}
            sizes = {entry["key"]: entry for entry in report["groups"]["routing_size"]}
            risks_by_level = {entry["key"]: entry for entry in report["groups"]["routing_risk"]}
            risks = {entry["key"]: entry for entry in report["groups"]["routing_risk_factor"]}
            scopes = {entry["key"]: entry for entry in report["groups"]["verification_scope"]}
            decisions = {entry["key"]: entry for entry in report["groups"]["routing_decision"]}
            provider_resources = {entry["key"]: entry for entry in report["groups"]["provider_resource"]}
            requirement_decisions = {
                entry["key"]: entry for entry in report["groups"]["model_requirement_routing_decision"]
            }
            requirement_experiments = {
                entry["key"]: entry for entry in report["groups"]["model_requirement_experiment"]
            }
            decision_key = "size=small risk=low verify=docs+unit"
            provider_key = "provider=codex quota_boundary=unknown sharing=not_independent"
            req_key = requirement_key(reasoning_depth="low", cost_sensitivity="high")
            requirement_decision_key = f"requirement={req_key} size=small risk=low verify=docs+unit"

            self.assertEqual(0, code)
            self.assertEqual("docs-only bounded change", report["task_rows"][0]["routing_reason"])
            self.assertEqual(["public-docs", "low-blast-radius"], report["task_rows"][0]["routing_risk_factors"])
            self.assertEqual("small", report["task_rows"][0]["routing_size"])
            self.assertEqual("low", report["task_rows"][0]["routing_risk"])
            self.assertEqual(["unit", "docs"], report["task_rows"][0]["verification_scope"])
            self.assertEqual("codex", report["task_rows"][0]["provider_resource"]["provider_id"])
            self.assertEqual("unknown", report["task_rows"][0]["provider_resource"]["quota_boundary"])
            self.assertEqual("not_independent", report["task_rows"][0]["provider_resource"]["sharing_assumption"])
            self.assertTrue(report["task_rows"][0]["provider_resource"]["advisory_only"])
            self.assertFalse(report["task_rows"][0]["provider_resource"]["derived_from_capacity_pool"])
            self.assertEqual(1, experiments["downshift_probe"]["tasks"])
            self.assertEqual(1, sizes["small"]["tasks"])
            self.assertEqual(1, risks_by_level["low"]["tasks"])
            self.assertEqual(1, risks["public-docs"]["tasks"])
            self.assertEqual(1, risks["low-blast-radius"]["tasks"])
            self.assertEqual(1, scopes["unit"]["tasks"])
            self.assertEqual(1, scopes["docs"]["tasks"])
            self.assertEqual(decision_key, report["task_rows"][0]["routing_decision"])
            self.assertEqual(requirement_decision_key, report["task_rows"][0]["model_requirement_routing_decision"])
            self.assertEqual(1, decisions[decision_key]["tasks"])
            self.assertEqual(1, decisions[decision_key]["first_pass_accepted"])
            self.assertEqual(1, provider_resources[provider_key]["tasks"])
            self.assertEqual(1, requirement_decisions[requirement_decision_key]["tasks"])
            self.assertEqual(1, requirement_experiments[f"{req_key}/downshift_probe"]["first_pass_accepted"])

    def test_routing_report_stratifies_probe_lanes_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))

            def save_routed(task_id: str, experiment: str, *, review_status: str = "accepted") -> None:
                task = create_task(
                    config,
                    "work",
                    tmp,
                    task_id=task_id,
                    project_id="project-a",
                    category="docs",
                    model_requirement_vector=requirement_vector(reasoning_depth="low", cost_sensitivity="high"),
                    routing_experiment=experiment,
                    routing_size="small",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
                task["status"] = "completed"
                task["review_status"] = review_status
                task["attempts"] = 1
                task["last_result"] = {"task_id": task_id, "status": "completed", "verification": ["unit"]}
                task["reviewer_codex"] = {"decision": "pass" if review_status == "accepted" else "needs_fix"}
                save_task(config, task)

            save_routed("baseline", "baseline")
            save_routed("probe", "downshift_probe", review_status="needs_followup")
            save_routed("guard", "upshift_guard")

            code, output = run_cli(
                ["--config", str(config_path), "routing-report", "--project", "project-a", "--limit", "0", "--json"]
            )
            report = json.loads(output)
            lane_groups = {entry["key"]: entry for entry in report["groups"]["routing_experiment_lane"]}
            probe_lanes = report["evaluation_diagnostics"]["probe_lanes"]
            family_groups = {entry["key"]: entry for entry in probe_lanes["by_lane_family"]}
            decision_lanes = {entry["key"]: entry for entry in probe_lanes["by_routing_decision_lane"]}
            req_lanes = {entry["key"]: entry for entry in probe_lanes["by_model_requirement_lane"]}
            decision_key = "size=small risk=low verify=unit"
            req_key = requirement_key(reasoning_depth="low", cost_sensitivity="high")

            self.assertEqual(0, code)
            self.assertTrue(probe_lanes["advisory"]["read_only"])
            self.assertFalse(probe_lanes["advisory"]["mutation_allowed"])
            self.assertEqual(1, lane_groups["baseline"]["tasks"])
            self.assertEqual(1, lane_groups["probe"]["needs_fix_or_rejected"])
            self.assertEqual(1, family_groups["guard"]["accepted"])
            self.assertEqual(1, decision_lanes[f"{decision_key} lane=baseline"]["first_pass_accepted"])
            self.assertEqual(1, decision_lanes[f"{decision_key} lane=probe"]["needs_fix_or_rejected"])
            self.assertEqual(1, req_lanes[f"{req_key}/lane=guard"]["accepted"])

    def test_routing_report_exposes_selection_rule_and_low_cost_candidate_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={
                    "model_selection_rules": [
                        {"name": "low-cost-docs", "when": {"reasoning_depth": "low"}, "model": "gpt-5-small"}
                    ],
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
            task["last_run"] = {
                "resolved_execution_config": {
                    "selection_rule": "low-cost-docs",
                    "model_source": "cli_default",
                    "execution_target": "local",
                },
                "duration_seconds": 10,
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            requirements = {entry["key"]: entry for entry in report["groups"]["model_requirement"]}
            selection_rules = {entry["key"]: entry for entry in report["groups"]["model_selection_rule"]}
            model_sources = {entry["key"]: entry for entry in report["groups"]["model_source"]}
            execution_targets = {entry["key"]: entry for entry in report["groups"]["execution_target"]}
            source_targets = {entry["key"]: entry for entry in report["groups"]["model_source_execution_target"]}
            requirement_decisions = {
                entry["key"]: entry for entry in report["groups"]["model_requirement_routing_decision"]
            }
            selection_decisions = {
                entry["key"]: entry for entry in report["groups"]["model_selection_routing_decision"]
            }
            low_cost_candidates = {entry["key"]: entry for entry in report["groups"]["low_cost_candidate"]}
            row = report["task_rows"][0]
            req_key = requirement_key(reasoning_depth="low", context_need="low", latency_priority="high", cost_sensitivity="high")

            self.assertEqual(0, code)
            self.assertEqual(req_key, row["model_requirement"])
            self.assertEqual("low-cost-docs", row["model_selection_rule"])
            self.assertEqual("cli_default", row["model_source"])
            self.assertEqual("local", row["execution_target"])
            self.assertEqual(
                "model_source=cli_default execution_target=local",
                row["model_source_execution_target"],
            )
            self.assertEqual(
                f"requirement={req_key} size=small risk=low verify=docs",
                row["model_requirement_routing_decision"],
            )
            self.assertEqual(
                "selection_rule=low-cost-docs size=small risk=low verify=docs",
                row["model_selection_routing_decision"],
            )
            self.assertEqual("candidate", row["low_cost_candidate"])
            self.assertEqual(1, requirements[req_key]["tasks"])
            self.assertEqual(1, selection_rules["low-cost-docs"]["tasks"])
            self.assertEqual(1, model_sources["cli_default"]["tasks"])
            self.assertEqual(1, execution_targets["local"]["tasks"])
            self.assertEqual(1, source_targets["model_source=cli_default execution_target=local"]["tasks"])
            self.assertEqual(1, requirement_decisions[f"requirement={req_key} size=small risk=low verify=docs"]["tasks"])
            self.assertEqual(1, selection_decisions["selection_rule=low-cost-docs size=small risk=low verify=docs"]["tasks"])
            self.assertEqual(1, low_cost_candidates["candidate"]["tasks"])

    def test_routing_report_keeps_execution_target_alias_axis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "work",
                tmp,
                task_id="target-alias-sample",
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
            task["last_run"] = {
                "execution_backend": "codex",
                "resolved_execution_config": {
                    "selection_rule": "low-cost-docs",
                    "model_source": "target_alias",
                    "execution_target": "low_cost_current",
                },
                "duration_seconds": 10,
            }
            task["last_result"] = {
                "task_id": "target-alias-sample",
                "status": "completed",
                "verification": ["docs"],
            }
            task["reviewer_codex"] = {"decision": "pass", "confidence": "high"}
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            execution_targets = {entry["key"]: entry for entry in report["groups"]["execution_target"]}
            source_targets = {entry["key"]: entry for entry in report["groups"]["model_source_execution_target"]}
            diagnostics = report["evaluation_diagnostics"]
            diagnostic_targets = {entry["key"]: entry for entry in diagnostics["execution_targets"]}
            diagnostic_source_targets = {
                entry["key"]: entry for entry in diagnostics["model_source_execution_targets"]
            }
            row = report["task_rows"][0]
            source_target_key = "model_source=target_alias execution_target=low_cost_current"

            self.assertEqual(0, code)
            self.assertEqual("target_alias", row["model_source"])
            self.assertEqual("low_cost_current", row["execution_target"])
            self.assertEqual(source_target_key, row["model_source_execution_target"])
            self.assertEqual(1, execution_targets["low_cost_current"]["tasks"])
            self.assertEqual(1, source_targets[source_target_key]["tasks"])
            self.assertEqual(1, diagnostic_targets["low_cost_current"]["target_recorded"])
            self.assertEqual(1, diagnostic_source_targets[source_target_key]["usable_for_worker_policy"])

    def test_routing_report_uses_last_run_requirement_for_outcome_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "work",
                tmp,
                task_id="changed-requirement-after-run",
                project_id="project-a",
                model_requirement_vector=requirement_vector(reasoning_depth="high"),
                routing_size="small",
                routing_risk="low",
                verification_scope=["docs"],
            )
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["last_run"] = {
                "resolved_execution_config": {
                    "selection_rule": "low-cost-docs",
                    "model_requirement_vector": requirement_vector(reasoning_depth="low", cost_sensitivity="high"),
                },
                "duration_seconds": 10,
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            row = report["task_rows"][0]
            low_key = requirement_key(reasoning_depth="low", cost_sensitivity="high")
            high_key = requirement_key(reasoning_depth="high")

            self.assertEqual(0, code)
            self.assertEqual(low_key, row["model_requirement"])
            self.assertNotEqual(high_key, row["model_requirement"])

    def test_routing_report_keeps_discarded_rejected_internal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "discarded result",
                tmp,
                task_id="discarded-result",
                project_id="project-a",
                routing_size="small",
                routing_risk="low",
                verification_scope=["docs"],
            )
            task["status"] = "completed"
            task["review_status"] = "rejected"
            task["execution_mode"] = "git_worktree"
            task["execution_worktree_status"] = "cleaned"
            task["execution_cleanup_kind"] = "discard"
            task["execution_cleanup_result_applied"] = False
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            rows = {row["id"]: row for row in report["task_rows"]}
            decisions = {entry["key"]: entry for entry in report["groups"]["routing_decision"]}

            self.assertEqual(0, code)
            self.assertEqual("rejected", rows["discarded-result"]["review_status"])
            self.assertEqual(1, decisions["size=small risk=low verify=docs"]["rejected"])
            self.assertEqual(1, decisions["size=small risk=low verify=docs"]["needs_fix_or_rejected"])

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
            requirement_decisions = {
                entry["key"]: entry for entry in report["groups"]["model_requirement_routing_decision"]
            }
            row = report["task_rows"][0]
            decision_key = "size=unspecified risk=unspecified verify=none"
            req_key = requirement_key()
            requirement_decision_key = f"requirement={req_key} size=unspecified risk=unspecified verify=none"

            self.assertEqual(0, code)
            self.assertEqual("", row["routing_reason"])
            self.assertEqual(["none"], row["routing_risk_factors"])
            self.assertEqual("unspecified", row["routing_experiment"])
            self.assertEqual("unspecified", row["routing_size"])
            self.assertEqual("unspecified", row["routing_risk"])
            self.assertEqual(["none"], row["verification_scope"])
            self.assertEqual(decision_key, row["routing_decision"])
            self.assertEqual(requirement_decision_key, row["model_requirement_routing_decision"])
            self.assertEqual(1, experiments["unspecified"]["tasks"])
            self.assertEqual(1, sizes["unspecified"]["tasks"])
            self.assertEqual(1, risks["unspecified"]["tasks"])
            self.assertEqual(1, risk_factors["none"]["tasks"])
            self.assertEqual(1, scopes["none"]["tasks"])
            self.assertEqual(1, decisions[decision_key]["first_pass_accepted"])
            self.assertEqual(1, requirement_decisions[requirement_decision_key]["tasks"])

    def test_routing_report_human_output_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
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
                    model_requirement_vector=requirement_vector(reasoning_depth="low", cost_sensitivity="high"),
                )
                task["status"] = "completed"
                task["review_status"] = "accepted"
                task["attempts"] = 1
                save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--limit", "2"])

            self.assertEqual(0, code)
            self.assertIn("# routing report", output)
            self.assertIn("tasks: 2 of 3 filtered", output)
            self.assertIn("## by_model_requirement", output)
            self.assertIn("## by_model_selection_rule", output)
            self.assertIn("## by_model_source", output)
            self.assertIn("## by_execution_target", output)
            self.assertIn("## by_model_source_execution_target", output)
            self.assertIn("## by_routing_size", output)
            self.assertIn("## by_verification_scope", output)
            self.assertIn("## by_routing_decision", output)
            self.assertIn("## by_model_requirement_routing_decision", output)
            self.assertIn("## by_model_selection_routing_decision", output)
            self.assertIn("## by_low_cost_candidate", output)
            self.assertIn("reasoning_depth=low", output)

    def test_routing_report_evaluation_diagnostics_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for index in range(3):
                task = create_task(
                    config,
                    "work",
                    tmp,
                    task_id=f"clean-{index}",
                    project_id="project-a",
                    category="implementation",
                    labels=["routing"],
                    model_requirement_vector=requirement_vector(reasoning_depth="low", cost_sensitivity="high"),
                    routing_size="small",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
                task["status"] = "completed"
                task["review_status"] = "accepted"
                task["attempts"] = 1
                resolved_config = {"selection_rule": "low-cost-docs"}
                if index == 0:
                    resolved_config["model_source"] = "explicit_model"
                    resolved_config["execution_target"] = "local"
                elif index == 1:
                    resolved_config["model_source"] = "cli_default"
                task["last_run"] = {
                    "execution_backend": "codex",
                    "resolved_execution_config": resolved_config,
                }
                task["last_result"] = {
                    "task_id": f"clean-{index}",
                    "status": "completed",
                    "verification": ["PYTHONPATH=src python3 -m unittest tests.test_cli"],
                }
                task["reviewer_codex"] = {"decision": "pass", "confidence": "high"}
                save_task(config, task)
            needs_fix = create_task(
                config,
                "work",
                tmp,
                task_id="needs-fix",
                project_id="project-a",
                category="implementation",
                labels=["routing"],
                model_requirement_vector=requirement_vector(reasoning_depth="low", cost_sensitivity="high"),
                routing_size="small",
                routing_risk="low",
                verification_scope=["unit"],
            )
            needs_fix["status"] = "completed"
            needs_fix["review_status"] = "needs_followup"
            needs_fix["last_result"] = {"task_id": "needs-fix", "status": "completed", "verification": ["unit"]}
            needs_fix["reviewer_codex"] = {
                "decision": "needs_fix",
                "confidence": "high",
                "findings": [{"severity": "error", "summary": "synthetic"}],
            }
            save_task(config, needs_fix)
            failed_review = create_task(
                config,
                "work",
                tmp,
                task_id="failed-review",
                project_id="project-a",
                category="implementation",
                labels=["routing"],
                model_requirement_vector=requirement_vector(reasoning_depth="high"),
                routing_size="medium",
                routing_risk="high",
                verification_scope=["manual"],
            )
            failed_review["status"] = "completed"
            failed_review["review_status"] = "needs_followup"
            failed_review["last_result"] = {"task_id": "failed-review", "status": "completed", "verification": ["manual"]}
            failed_review["reviewer_codex"] = {"decision": "failed_review", "confidence": "low"}
            save_task(config, failed_review)
            legacy = create_task(
                config,
                "legacy work",
                tmp,
                task_id="legacy-minimal",
                project_id="project-a",
                category="docs",
                labels=["legacy"],
            )
            legacy["status"] = "completed"
            legacy["review_status"] = "accepted"
            save_task(config, legacy)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            diagnostics = report["evaluation_diagnostics"]
            execution_surfaces = {entry["key"]: entry for entry in diagnostics["execution_surfaces"]}
            model_sources = {entry["key"]: entry for entry in diagnostics["model_sources"]}
            execution_targets = {entry["key"]: entry for entry in diagnostics["execution_targets"]}
            source_targets = {entry["key"]: entry for entry in diagnostics["model_source_execution_targets"]}
            worker_cells = {entry["key"]: entry for entry in diagnostics["worker_cells"]}
            reviewer_cells = {entry["key"]: entry for entry in diagnostics["reviewer_cells"]}
            exclusions = {entry["key"]: entry for entry in diagnostics["policy_exclusions"]}
            buckets = {entry["key"]: entry for entry in diagnostics["task_buckets"]}
            small_bucket = buckets["size=small risk=low verify=unit"]

            self.assertEqual(0, code)
            self.assertEqual(6, diagnostics["row_count"])
            self.assertEqual(4, diagnostics["policy_usage"]["usable_for_worker_policy"])
            self.assertEqual(5, diagnostics["policy_usage"]["usable_for_reviewer_calibration"])
            self.assertTrue(diagnostics["advisory"]["read_only"])
            self.assertEqual(6, execution_surfaces["cbr_batch"]["tasks"])
            self.assertEqual(6, execution_surfaces["cbr_batch"]["queue_tasks"])
            self.assertEqual(0, execution_surfaces["cbr_batch"]["supplemental_evidence"])
            self.assertEqual(1, model_sources["explicit_model"]["explicit_model_pins"])
            self.assertEqual(1, model_sources["cli_default"]["cli_default_runs"])
            self.assertEqual(4, model_sources["unknown"]["unknown_legacy_runs"])
            self.assertEqual(1, execution_targets["local"]["target_recorded"])
            self.assertEqual(5, execution_targets["none"]["target_absent"])
            self.assertEqual(1, source_targets["model_source=explicit_model execution_target=local"]["tasks"])
            self.assertEqual(1, source_targets["model_source=cli_default execution_target=none"]["tasks"])
            self.assertEqual(3, small_bucket["clean_samples"])
            self.assertTrue(small_bucket["policy_review_candidate"])
            self.assertEqual("advisory_read_only", small_bucket["policy_review_note"])
            self.assertEqual("insufficient_sample", small_bucket["threshold_advisory_status"])
            self.assertEqual(5, small_bucket["threshold_advisory"]["thresholds"]["min_accepted_count"])
            self.assertIn("accepted_count_below_min", small_bucket["threshold_advisory_reasons"])
            self.assertEqual(
                3,
                worker_cells[
                    "worker:backend=codex codex_profile_present=false model_present=false selection_rule=low-cost-docs"
                ]["usable_accepted_pass"],
            )
            self.assertEqual(
                1,
                reviewer_cells["reviewer:anchor=unknown policy=legacy present=true role=reviewer"]["reviewer_needs_fix"],
            )
            self.assertEqual(
                1,
                reviewer_cells["reviewer:anchor=unknown policy=legacy present=true role=reviewer"]["reviewer_failed_review"],
            )
            self.assertEqual(1, exclusions["review_process_failed"]["rows"])
            self.assertEqual(1, exclusions["reviewer_unusable"]["rows"])
            self.assertIn("objective_unavailable", exclusions)

    def test_routing_report_task_bucket_threshold_advisory_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))

            def save_completed(
                task_id: str,
                *,
                routing_size: str,
                routing_risk: str,
                verification_scope: list[str],
                review_status: str = "accepted",
                reviewer_decision: str = "pass",
                required_human_checks: list[str] | None = None,
            ) -> None:
                task = create_task(
                    config,
                    "work",
                    tmp,
                    task_id=task_id,
                    project_id="project-a",
                    category="implementation",
                    routing_size=routing_size,
                    routing_risk=routing_risk,
                    verification_scope=verification_scope,
                )
                task["status"] = "completed"
                task["review_status"] = review_status
                task["attempts"] = 1
                task["last_result"] = {
                    "task_id": task_id,
                    "status": "completed",
                    "verification": ["unit"],
                }
                task["reviewer_codex"] = {
                    "decision": reviewer_decision,
                    "confidence": "high",
                }
                if required_human_checks is not None:
                    task["reviewer_codex"]["required_human_checks"] = required_human_checks
                save_task(config, task)

            for index in range(5):
                save_completed(
                    f"reviewable-{index}",
                    routing_size="small",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
            save_completed(
                "insufficient",
                routing_size="small",
                routing_risk="medium",
                verification_scope=["unit"],
            )
            for index in range(5):
                save_completed(
                    f"below-accepted-{index}",
                    routing_size="medium",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
            save_completed(
                "below-human",
                routing_size="medium",
                routing_risk="low",
                verification_scope=["unit"],
                review_status="needs_followup",
                reviewer_decision="needs_human",
                required_human_checks=["manual verification"],
            )
            for index in range(48):
                save_completed(
                    f"repeated-fix-accepted-{index}",
                    routing_size="large",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
            for index in range(2):
                save_completed(
                    f"repeated-fix-{index}",
                    routing_size="large",
                    routing_risk="low",
                    verification_scope=["unit"],
                    review_status="needs_followup",
                    reviewer_decision="needs_fix",
                )

            code, output = run_cli(
                ["--config", str(config_path), "routing-report", "--project", "project-a", "--limit", "0", "--json"]
            )
            report = json.loads(output)
            buckets = {entry["key"]: entry for entry in report["evaluation_diagnostics"]["task_buckets"]}
            reviewable = buckets["size=small risk=low verify=unit"]
            insufficient = buckets["size=small risk=medium verify=unit"]
            below = buckets["size=medium risk=low verify=unit"]
            repeated_fix = buckets["size=large risk=low verify=unit"]

            self.assertEqual(0, code)
            self.assertEqual("reviewable", reviewable["threshold_advisory_status"])
            self.assertEqual([], reviewable["threshold_advisory_reasons"])
            self.assertTrue(reviewable["threshold_advisory"]["read_only"])
            self.assertEqual(5, reviewable["accepted"])
            self.assertEqual(1.0, reviewable["first_pass_accept_rate"])
            self.assertEqual("insufficient_sample", insufficient["threshold_advisory_status"])
            self.assertIn("accepted_count_below_min", insufficient["threshold_advisory_reasons"])
            self.assertEqual("below_threshold", below["threshold_advisory_status"])
            self.assertIn("reviewer_needs_human_present", below["threshold_advisory_reasons"])
            self.assertIn("required_human_checks_present", below["threshold_advisory_reasons"])
            self.assertIn("needs_fix_or_rejected_rate_above_max", below["threshold_advisory_reasons"])
            self.assertEqual("below_threshold", repeated_fix["threshold_advisory_status"])
            self.assertEqual(0.04, repeated_fix["needs_fix_or_rejected_rate"])
            self.assertIn("reviewer_needs_fix_repeated", repeated_fix["threshold_advisory_reasons"])

    def test_routing_policy_candidates_json_is_reviewable_only_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))

            def save_completed(
                task_id: str,
                *,
                routing_size: str,
                routing_risk: str,
                verification_scope: list[str],
                project_id: str = "project-a",
                labels: list[str] | None = None,
                review_status: str = "accepted",
                reviewer_decision: str = "pass",
            ) -> None:
                task = create_task(
                    config,
                    "work",
                    tmp,
                    task_id=task_id,
                    project_id=project_id,
                    category="implementation",
                    labels=labels or ["routing"],
                    routing_size=routing_size,
                    routing_risk=routing_risk,
                    verification_scope=verification_scope,
                )
                task["status"] = "completed"
                task["review_status"] = review_status
                task["attempts"] = 1
                task["last_result"] = {"task_id": task_id, "status": "completed", "verification": ["unit"]}
                task["reviewer_codex"] = {"decision": reviewer_decision, "confidence": "high"}
                save_task(config, task)

            for index in range(5):
                save_completed(
                    f"reviewable-{index}",
                    routing_size="small",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
            save_completed(
                "insufficient",
                routing_size="small",
                routing_risk="medium",
                verification_scope=["unit"],
            )
            for index in range(5):
                save_completed(
                    f"below-accepted-{index}",
                    routing_size="medium",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
            save_completed(
                "below-fix",
                routing_size="medium",
                routing_risk="low",
                verification_scope=["unit"],
                review_status="needs_followup",
                reviewer_decision="needs_fix",
            )
            for index in range(5):
                save_completed(
                    f"other-project-{index}",
                    routing_size="large",
                    routing_risk="low",
                    verification_scope=["unit"],
                    project_id="project-b",
                    labels=["routing"],
                )
            task_path = config.queue_dir / "reviewable-0.json"
            before = task_path.read_text(encoding="utf-8")

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "routing-policy-candidates",
                    "--project",
                    "project-a",
                    "--label",
                    "routing",
                    "--limit",
                    "0",
                    "--json",
                ]
            )
            after = task_path.read_text(encoding="utf-8")
            report = json.loads(output)
            candidate = report["candidates"][0]

            self.assertEqual(0, code)
            self.assertEqual(before, after)
            self.assertTrue(report["read_only"])
            self.assertFalse(report["mutation_allowed"])
            self.assertEqual("routing-report", report["source_report"]["kind"])
            self.assertEqual(1, report["summary"]["candidate_count"])
            self.assertEqual(1, report["summary"]["insufficient_sample"])
            self.assertEqual(1, report["summary"]["below_threshold"])
            self.assertEqual(1, report["summary"]["decision_card_count"])
            self.assertEqual(1, report["summary"]["decision_required_count"])
            self.assertEqual({"operator_review": 1}, report["summary"]["by_recommendation"])
            self.assertEqual({}, report["summary"]["by_blocked_reason"])
            self.assertEqual([], report["non_reviewable_buckets"])
            self.assertEqual("size=small risk=low verify=unit", candidate["task_bucket_key"])
            self.assertEqual("reviewable", candidate["advisory_status"])
            self.assertEqual([], candidate["advisory_reasons"])
            self.assertEqual("operator_review", candidate["recommended_next_step"])
            self.assertEqual(5, candidate["evidence"]["accepted"])
            self.assertEqual(1.0, candidate["evidence"]["first_pass_accept_rate"])
            self.assertEqual(0.0, candidate["evidence"]["needs_fix_or_rejected_rate"])
            self.assertFalse(candidate["mutation_allowed"])
            self.assertEqual(5, candidate["thresholds"]["min_accepted_count"])
            decision_card = report["decision_cards"][0]
            self.assertEqual("routing_policy_change", decision_card["decision_axis"])
            self.assertEqual("candidate_reported", decision_card["execution_task_status"])
            self.assertEqual("decision_required", decision_card["user_decision_status"])
            self.assertEqual(candidate["candidate_id"], decision_card["candidate_id"])
            self.assertEqual("operator_review", decision_card["recommendation"])
            self.assertEqual(5, decision_card["evidence_summary"]["accepted"])
            self.assertIn("approve_followup_proposal", decision_card["allowed_decisions"])
            self.assertIn("change_model_selection_rule", decision_card["prohibited_actions"])
            self.assertFalse(decision_card["mutation_allowed"])

    def test_routing_policy_candidates_human_output_can_include_non_reviewable_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for index in range(5):
                task = create_task(
                    config,
                    "work",
                    tmp,
                    task_id=f"accepted-{index}",
                    project_id="project-a",
                    category="implementation",
                    routing_size="small",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
                task["status"] = "completed"
                task["review_status"] = "accepted"
                task["attempts"] = 1
                task["last_result"] = {"task_id": task["id"], "status": "completed", "verification": ["unit"]}
                task["reviewer_codex"] = {"decision": "pass", "confidence": "high"}
                save_task(config, task)
            task = create_task(
                config,
                "manual follow-up",
                tmp,
                task_id="needs-human",
                project_id="project-a",
                category="implementation",
                routing_size="small",
                routing_risk="low",
                verification_scope=["unit"],
            )
            task["status"] = "completed"
            task["review_status"] = "needs_followup"
            task["attempts"] = 1
            task["last_result"] = {"task_id": "needs-human", "status": "completed", "verification": ["unit"]}
            task["reviewer_codex"] = {
                "decision": "needs_human",
                "confidence": "high",
                "required_human_checks": ["manual check"],
            }
            save_task(config, task)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "routing-policy-candidates",
                    "--project",
                    "project-a",
                    "--limit",
                    "0",
                    "--include-non-reviewable",
                ]
            )

            self.assertEqual(0, code)
            self.assertIn("# routing policy candidates", output)
            self.assertIn("read_only: yes", output)
            self.assertIn("mutation_allowed: no", output)
            self.assertIn("non_reviewable_included=true", output)
            self.assertIn("non_reviewable_emitted=1", output)
            self.assertIn("decision_cards=1", output)
            self.assertIn("decision_required=0", output)
            self.assertIn("recommendations:", output)
            self.assertIn("  - keep_current_policy: 1", output)
            self.assertIn("blocked_reasons:", output)
            self.assertIn("  - below_threshold: 1", output)
            self.assertIn("## non_reviewable_buckets", output)
            self.assertIn("below_threshold", output)
            self.assertIn("needs_fix_or_rejected_rate_above_max", output)
            self.assertIn("reviewer_needs_human_present", output)
            self.assertIn("keep_current_policy", output)
            self.assertIn("## decision_cards", output)
            self.assertIn("execution_task_status: observation_reported", output)
            self.assertIn("user_decision_status: not_ready", output)
            self.assertIn("question: No policy decision is requested for this bucket yet.", output)

    def test_routing_policy_candidates_combines_supplemental_execution_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for index in range(4):
                task = create_task(
                    config,
                    "queue work",
                    tmp,
                    task_id=f"queue-{index}",
                    project_id="project-a",
                    category="implementation",
                    routing_size="small",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
                task["status"] = "completed"
                task["review_status"] = "accepted"
                task["attempts"] = 1
                task["last_result"] = {"task_id": task["id"], "status": "completed", "verification": ["unit"]}
                task["reviewer_codex"] = {"decision": "pass", "confidence": "high"}
                save_task(config, task)
            task_path = config.queue_dir / "queue-0.json"
            before = task_path.read_text(encoding="utf-8")
            evidence_path = Path(tmp) / "subagent-evidence.json"
            evidence_path.write_text(
                json.dumps(
                    [
                        {
                            "record_kind": "codex_subagent_execution",
                            "work_id": "supplemental-reviewable",
                            "project_id": "project-a",
                            "category": "implementation",
                            "routing_size": "small",
                            "routing_risk": "low",
                            "verification_scope": ["unit"],
                            "review_status": "accepted",
                            "attempts": 1,
                            "last_result": {"status": "completed", "verification": ["unit"]},
                            "reviewer_codex": {"decision": "pass", "confidence": "high"},
                        },
                        {
                            "record_kind": "codex_subagent_execution",
                            "work_id": "supplemental-other-project",
                            "project_id": "project-b",
                            "category": "implementation",
                            "routing_size": "small",
                            "routing_risk": "low",
                            "verification_scope": ["unit"],
                            "review_status": "accepted",
                            "attempts": 1,
                            "last_result": {"status": "completed", "verification": ["unit"]},
                            "reviewer_codex": {"decision": "pass", "confidence": "high"},
                        },
                    ]
                ),
                encoding="utf-8",
            )

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "routing-policy-candidates",
                    "--project",
                    "project-a",
                    "--execution-evidence-json",
                    str(evidence_path),
                    "--limit",
                    "0",
                    "--json",
                ]
            )
            after = task_path.read_text(encoding="utf-8")
            report = json.loads(output)
            candidate = report["candidates"][0]

            self.assertEqual(0, code)
            self.assertEqual(before, after)
            self.assertEqual(1, report["summary"]["candidate_count"])
            self.assertEqual(4, candidate["evidence"]["evidence_sources"]["queue"]["rows"])
            self.assertEqual(1, candidate["evidence"]["evidence_sources"]["supplemental_execution_evidence"]["rows"])
            self.assertEqual(5, candidate["evidence"]["accepted"])
            self.assertEqual("operator_review", candidate["recommended_next_step"])

    def test_decision_cards_inventory_combines_policy_and_routing_cards_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={
                    "execution_targets": {
                        "low_cost_current": {
                            "model": "gpt-5-small",
                            "freshness": {
                                "owner": "operator",
                                "last_reviewed_at": "2000-01-01",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "model_selection_rules": [
                        {
                            "name": "low-cost-docs",
                            "when": {"reasoning_depth": "low"},
                            "execution_target": "low_cost_current",
                        }
                    ],
                },
            )
            config = Config.load(str(config_path))
            for index in range(5):
                task = create_task(
                    config,
                    "work",
                    tmp,
                    task_id=f"routing-{index}",
                    project_id="project-a",
                    category="implementation",
                    labels=["routing"],
                    routing_size="small",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
                task["status"] = "completed"
                task["review_status"] = "accepted"
                task["attempts"] = 1
                task["last_result"] = {"task_id": task["id"], "status": "completed", "verification": ["unit"]}
                task["reviewer_codex"] = {"decision": "pass", "confidence": "high"}
                save_task(config, task)
            task_path = config.queue_dir / "routing-0.json"
            before = task_path.read_text(encoding="utf-8")
            now = datetime(2026, 7, 4, tzinfo=timezone.utc)

            with (
                patch("codex_batch_runner.decision_cards.utc_now", return_value=now),
                patch("codex_batch_runner.policy_proposals.utc_now", return_value=now),
                patch("codex_batch_runner.routing_report.iso_now", return_value="2026-07-04T00:00:00+00:00"),
            ):
                code, output = run_cli(
                    [
                        "--config",
                        str(config_path),
                        "decision-cards",
                        "--project",
                        "project-a",
                        "--label",
                        "routing",
                        "--limit",
                        "0",
                        "--json",
                    ]
                )
            after = task_path.read_text(encoding="utf-8")
            report = json.loads(output)
            cards = {card["source"]: card for card in report["decision_cards"]}

            self.assertEqual(0, code)
            self.assertEqual(before, after)
            self.assertEqual("decision_card_inventory", report["kind"])
            self.assertTrue(report["read_only"])
            self.assertFalse(report["mutation_allowed"])
            self.assertEqual("2026-07-04T00:00:00+00:00", report["generated_at"])
            self.assertEqual(
                [
                    {
                        "card_count": 1,
                        "generated_at": "2026-07-04T00:00:00+00:00",
                        "mutation_allowed": False,
                        "read_only": True,
                        "source": "policy-proposals execution-target-freshness",
                    },
                    {
                        "card_count": 1,
                        "generated_at": "2026-07-04T00:00:00+00:00",
                        "mutation_allowed": False,
                        "read_only": True,
                        "source": "routing-policy-candidates",
                    },
                ],
                report["source_reports"],
            )
            self.assertEqual(2, report["summary"]["card_count"])
            self.assertEqual(2, report["summary"]["decision_required"])
            self.assertEqual(0, report["summary"]["not_ready"])
            self.assertEqual("review_decision_cards", report["summary"]["next_action"])
            self.assertEqual({"execution_target_freshness": 1, "routing_policy_change": 1}, report["summary"]["by_axis"])
            self.assertEqual(
                {"policy-proposals execution-target-freshness": 1, "routing-policy-candidates": 1},
                report["summary"]["by_source"],
            )
            self.assertEqual(
                {"operator_review": 2},
                report["summary"]["by_recommendation"],
            )
            self.assertEqual({}, report["summary"]["by_blocked_reason"])
            self.assertEqual("decision_required", cards["policy-proposals execution-target-freshness"]["user_decision_status"])
            self.assertEqual("decision_required", cards["routing-policy-candidates"]["user_decision_status"])
            self.assertFalse(cards["routing-policy-candidates"]["mutation_allowed"])
            self.assertNotIn("gpt-5-small", output)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "decision-cards",
                    "--project",
                    "project-a",
                    "--label",
                    "routing",
                    "--decision-axis",
                    "routing_policy_change",
                    "--limit",
                    "0",
                    "--json",
                ]
            )
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(["routing_policy_change"], report["summary"]["decision_axis_filter"])
            self.assertEqual(1, report["summary"]["card_count"])
            self.assertEqual({"routing_policy_change": 1}, report["summary"]["by_axis"])
            self.assertEqual("routing_policy_change", report["decision_cards"][0]["decision_axis"])

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "decision-cards",
                    "--project",
                    "project-a",
                    "--label",
                    "routing",
                    "--source",
                    "policy-proposals execution-target-freshness",
                    "--limit",
                    "0",
                    "--json",
                ]
            )
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(["policy-proposals execution-target-freshness"], report["summary"]["source_filter"])
            self.assertEqual(1, report["summary"]["card_count"])
            self.assertEqual({"policy-proposals execution-target-freshness": 1}, report["summary"]["by_source"])
            self.assertEqual(
                ["policy-proposals execution-target-freshness"],
                [source_report["source"] for source_report in report["source_reports"]],
            )
            self.assertEqual("policy-proposals execution-target-freshness", report["decision_cards"][0]["source"])

    def test_decision_cards_inventory_human_output_and_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "work",
                tmp,
                task_id="insufficient",
                project_id="project-a",
                category="implementation",
                labels=["routing"],
                routing_size="small",
                routing_risk="low",
                verification_scope=["unit"],
            )
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["attempts"] = 1
            task["last_result"] = {"task_id": task["id"], "status": "completed", "verification": ["unit"]}
            task["reviewer_codex"] = {"decision": "pass", "confidence": "high"}
            save_task(config, task)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "decision-cards",
                    "--project",
                    "project-a",
                    "--include-observations",
                    "--limit",
                    "0",
                ]
            )

            self.assertEqual(0, code)
            self.assertIn("# decision cards", output)
            self.assertIn("read_only: yes", output)
            self.assertIn("routing-policy-candidates", output)
            self.assertIn("not_ready", output)
            self.assertIn("open_decisions: none", output)
            self.assertIn("next_action: continue_observing", output)
            self.assertIn("recommendations:", output)
            self.assertIn("collect_more_evidence: 1", output)
            self.assertIn("BLOCKED", output)
            self.assertIn("insufficient_sample", output)
            self.assertRegex(
                output,
                r"routing-policy-candidates\s+[^\n]*\s+insufficient_sample\s+size=small risk=low verify=unit",
            )

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "decision-cards",
                    "--project",
                    "project-a",
                    "--include-observations",
                    "--user-decision-status",
                    "not_ready",
                    "--limit",
                    "0",
                    "--json",
                ]
            )
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(["not_ready"], report["summary"]["user_decision_status_filter"])
            self.assertEqual(1, report["summary"]["card_count"])
            self.assertEqual("continue_observing", report["summary"]["next_action"])
            self.assertEqual({"not_ready": 1}, report["summary"]["by_status"])
            self.assertEqual("not_ready", report["decision_cards"][0]["user_decision_status"])

    def test_decision_cards_human_output_marks_no_open_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output = run_cli(["--config", str(config_path), "decision-cards"])

            self.assertEqual(0, code)
            self.assertIn("summary: cards=0 decision_required=0 approval_blocked=0 not_ready=0", output)
            self.assertIn("open_decisions: none", output)
            self.assertIn("next_action: none", output)

    def test_watching_report_summarizes_read_only_evidence_areas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            applied = create_task(
                config,
                "applied work",
                tmp,
                task_id="applied",
                project_id="project-a",
                category="implementation",
                labels=["watching"],
                routing_size="small",
                routing_risk="low",
                verification_scope=["unit"],
            )
            applied["status"] = "completed"
            applied["review_status"] = "accepted"
            applied["attempts"] = 1
            applied["last_result"] = {"task_id": "applied", "status": "completed", "verification": ["unit"]}
            applied["reviewer_codex"] = {"decision": "pass", "confidence": "high"}
            applied["execution_mode"] = "git_worktree"
            applied["execution_apply_status"] = "applied"
            applied["execution_worktree_status"] = "cleaned"
            save_task(config, applied)
            unapplied = create_task(
                config,
                "unapplied work",
                tmp,
                task_id="unapplied",
                project_id="project-a",
                category="implementation",
                labels=["watching"],
            )
            unapplied["status"] = "completed"
            unapplied["review_status"] = "accepted"
            unapplied["execution_mode"] = "git_worktree"
            unapplied["execution_worktree_status"] = "retained"
            save_task(config, unapplied)
            failed = create_task(
                config,
                "failed work",
                tmp,
                task_id="failed",
                project_id="project-a",
                category="implementation",
                labels=["watching"],
            )
            failed["status"] = "failed"
            failed["last_error"] = "synthetic failure"
            save_task(config, failed)
            write_json_atomic(
                rate_limit_dir(config) / "event.json",
                {
                    "task_id": "applied",
                    "detected_at": "2026-06-20T12:00:00+00:00",
                    "attempt": 1,
                    "matched_markers": ["usage limit"],
                    "cooldown_until": "2026-06-20T12:30:00+00:00",
                },
            )
            task_path = config.queue_dir / "applied.json"
            before = task_path.read_text(encoding="utf-8")

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "watching-report",
                    "--project",
                    "project-a",
                    "--label",
                    "watching",
                    "--limit",
                    "0",
                    "--json",
                ]
            )
            after = task_path.read_text(encoding="utf-8")
            report = json.loads(output)
            areas = {area["area"]: area for area in report["areas"]}

            self.assertEqual(0, code)
            self.assertEqual(before, after)
            self.assertTrue(report["read_only"])
            self.assertFalse(report["mutation_allowed"])
            self.assertEqual("watching_evidence_report", report["kind"])
            self.assertEqual("resolve_action_required", report["summary"]["next_action"])
            self.assertEqual(5, report["summary"]["area_count"])
            self.assertEqual("action_required", areas["queue_execution"]["evidence_status"])
            self.assertIn("failed_or_blocked=1", areas["queue_execution"]["signals"])
            self.assertEqual("action_required", areas["review_apply"]["evidence_status"])
            self.assertEqual(["unapplied"], areas["review_apply"]["evidence"]["accepted_unapplied_task_ids"])
            self.assertEqual("action_required", areas["worktree_lifecycle"]["evidence_status"])
            self.assertEqual(["unapplied"], areas["worktree_lifecycle"]["evidence"]["accepted_unapplied_task_ids"])
            self.assertEqual("ready_for_close_review", areas["cooldown_rate_limits"]["evidence_status"])
            self.assertIn("rate_limit_events=1", areas["cooldown_rate_limits"]["signals"])
            self.assertEqual("continue_observing", areas["routing_policy"]["evidence_status"])
            self.assertIn("next_action=continue_observing", areas["routing_policy"]["signals"])

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "watching-report",
                    "--project",
                    "project-a",
                    "--label",
                    "watching",
                    "--limit",
                    "0",
                ]
            )

            self.assertEqual(0, code)
            self.assertIn("# watching evidence", output)
            self.assertIn("read_only: yes", output)
            self.assertIn("mutation_allowed: no", output)
            self.assertIn("next_action: resolve_action_required", output)
            self.assertIn("queue_execution", output)
            self.assertIn("cooldown_rate_limits", output)
            self.assertIn("ready_for_close_review", output)

    def test_decision_cards_inventory_summarizes_blocked_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={
                    "model_selection_rules": [
                        {
                            "name": "low-cost-docs",
                            "when": {"reasoning_depth": "low"},
                            "model": "gpt-5-small",
                        }
                    ],
                },
            )

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "decision-cards",
                    "--decision-axis",
                    "execution_target_freshness",
                    "--user-decision-status",
                    "approval_blocked",
                    "--json",
                ]
            )
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual({"create_bounded_migration_proposal": 1}, report["summary"]["by_recommendation"])
            self.assertEqual(
                {"direct_model_pin_requires_separate_migration_approval": 1},
                report["summary"]["by_blocked_reason"],
            )
            self.assertNotIn("gpt-5-small", output)

            code, output = run_cli(["--config", str(config_path), "decision-cards"])

            self.assertEqual(0, code)
            self.assertIn("blocked_reasons:", output)
            self.assertIn("open_decisions: present", output)
            self.assertIn("direct_model_pin_requires_separate_migration_approval: 1", output)

    def test_decision_cards_rejects_unknown_user_decision_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "--config",
                        str(config_path),
                        "decision-cards",
                        "--user-decision-status",
                        "surprise",
                        "--json",
                    ]
                )

            self.assertEqual(2, raised.exception.code)
            self.assertIn("invalid choice", stderr.getvalue())

    def test_decision_cards_rejects_unknown_decision_axis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "--config",
                        str(config_path),
                        "decision-cards",
                        "--decision-axis",
                        "unknown_axis",
                        "--json",
                    ]
                )

            self.assertEqual(2, raised.exception.code)
            self.assertIn("invalid choice", stderr.getvalue())

    def test_decision_cards_rejects_unknown_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "--config",
                        str(config_path),
                        "decision-cards",
                        "--source",
                        "unknown-source",
                        "--json",
                    ]
                )

            self.assertEqual(2, raised.exception.code)
            self.assertIn("invalid choice", stderr.getvalue())

    def test_routing_report_includes_request_fingerprint_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            private_path = "/Users/example/.codex-batch-runner/worktrees/private/task.md"
            for task_id in ("duplicate-a", "duplicate-b"):
                task = create_task(
                    config,
                    f"Implement parser validation using {private_path}.",
                    tmp,
                    task_id=task_id,
                    title="Parser validation",
                    project_id="project-a",
                    category="implementation",
                    routing_size="small",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
                task["status"] = "completed"
                task["review_status"] = "accepted"
                save_task(config, task)
            create_task(
                config,
                "Summarize queue state.",
                tmp,
                task_id="unrelated",
                title="Queue summary",
                project_id="project-a",
                category="docs",
            )

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a", "--json"])
            report = json.loads(output)
            candidates = report["request_fingerprint_candidates"]

            self.assertEqual(0, code)
            self.assertEqual(1, candidates["candidate_count"])
            self.assertTrue(candidates["advisory"]["read_only"])
            self.assertFalse(candidates["advisory"]["mutation_allowed"])
            self.assertEqual({"exact_duplicate": 1}, candidates["candidate_types"])
            self.assertEqual(["duplicate-a", "duplicate-b"], candidates["candidates"][0]["task_ids"])
            self.assertNotIn(private_path, output)
            self.assertNotIn("Implement parser validation", output)

    def test_routing_report_evaluation_diagnostics_human_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "work",
                tmp,
                task_id="needs-human",
                project_id="project-a",
                routing_size="small",
                routing_risk="medium",
                verification_scope=["manual"],
            )
            task["status"] = "completed"
            task["review_status"] = "needs_followup"
            task["last_result"] = {"task_id": "needs-human", "status": "completed", "verification": ["manual"]}
            task["reviewer_codex"] = {
                "decision": "needs_human",
                "confidence": "medium",
                "required_human_checks": ["manual verification"],
            }
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-report", "--project", "project-a"])

            self.assertEqual(0, code)
            self.assertIn("## evaluation_diagnostics", output)
            self.assertIn("policy_usage: usable_for_worker_policy=1", output)
            self.assertIn("execution_surfaces", output)
            self.assertIn("model_sources", output)
            self.assertIn("execution_targets", output)
            self.assertIn("model_source_execution_targets", output)
            self.assertIn("worker_cells", output)
            self.assertIn("reviewer_cells", output)
            self.assertIn("policy_exclusions", output)
            self.assertIn("task_buckets", output)
            self.assertIn("probe_lanes", output)
            self.assertIn("## request_fingerprint_candidates", output)
            self.assertIn("ADVISORY", output)
            self.assertIn("insufficient_sample", output)
            self.assertIn("HUMAN", output)
            self.assertIn("size=small risk=medium verify=manual", output)

    def test_routing_report_rejects_negative_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "routing-report", "--limit", "-1"])

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("--limit must be non-negative", stderr)

    def test_execution_report_json_summarizes_processed_runs_and_token_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            codex_log = config.log_dir / "codex-task" / "attempt-1.jsonl"
            codex_log.parent.mkdir(parents=True, exist_ok=True)
            codex_log.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "turn.started"}),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 2000,
                                    "cached_input_tokens": 1500,
                                    "output_tokens": 120,
                                    "reasoning_output_tokens": 30,
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            codex_task = create_task(
                config,
                "work",
                tmp,
                task_id="codex-task",
                project_id="project-a",
                category="implementation",
                labels=["usage"],
                title="Codex token usage",
            )
            codex_task["status"] = "completed"
            codex_task["review_status"] = "accepted"
            codex_task["created_at"] = "2026-07-01T00:00:00+00:00"
            codex_task["started_at"] = "2026-07-01T00:01:00+00:00"
            codex_task["completed_at"] = "2026-07-01T00:03:00+00:00"
            codex_task["last_run"] = {
                "command_kind": "exec",
                "returncode": 0,
                "started_at": "2026-07-01T00:01:00+00:00",
                "finished_at": "2026-07-01T00:03:00+00:00",
                "duration_seconds": 120,
                "log_path": str(codex_log),
                "resolved_execution_config": {
                    "selection_rule": "default_execution_config",
                    "selection_reason": "fallback",
                    "model_source": "cli_default",
                    "execution_target": "codex_cli",
                },
            }
            codex_task["last_result"] = {
                "status": "completed",
                "changed_files": ["src/example.py"],
                "verification": ["python -m unittest"],
            }
            save_task(config, codex_task)

            antigravity_task = create_task(
                config,
                "external work",
                tmp,
                task_id="agy-task",
                project_id="project-a",
                category="implementation",
                labels=["usage"],
                title="Antigravity model group",
                execution_backend="external-json-command",
                external_command=["python", ".private/bin/agy-cbr-wrapper.py"],
                capacity_pool="antigravity-claude-gpt",
            )
            antigravity_task["status"] = "failed"
            antigravity_task["last_run"] = {
                "execution_backend": "external-json-command",
                "command_kind": "external-json-command",
                "command": ["python", ".private/bin/agy-cbr-wrapper.py", "--model-group", "claude-gpt"],
                "returncode": 1,
                "started_at": "2026-07-01T00:04:00+00:00",
                "finished_at": "2026-07-01T00:05:00+00:00",
                "duration_seconds": 60,
            }
            save_task(config, antigravity_task)

            shell_task = create_task(
                config,
                "shell work",
                tmp,
                task_id="shell-task",
                project_id="project-a",
                category="maintenance",
                title="Shell token free",
                execution_backend="shell",
                shell_command=["true"],
            )
            shell_task["status"] = "completed"
            shell_task["review_status"] = "accepted"
            shell_task["last_run"] = {
                "execution_backend": "shell",
                "command_kind": "shell",
                "command": ["true"],
                "returncode": 0,
                "started_at": "2026-07-01T00:06:00+00:00",
                "finished_at": "2026-07-01T00:06:01+00:00",
                "duration_seconds": 1,
            }
            save_task(config, shell_task)

            code, output = run_cli(["--config", str(config_path), "execution-report", "--project", "project-a", "--limit", "0", "--json"])
            report = json.loads(output)
            rows = {row["task_id"]: row for row in report["rows"]}

            self.assertEqual(0, code)
            self.assertEqual(3, report["row_count"])
            self.assertEqual("codex_jsonl", rows["codex-task"]["token_usage_source"])
            self.assertEqual(2120, rows["codex-task"]["token_usage"]["known_total_tokens"])
            self.assertEqual(500, rows["codex-task"]["token_usage"]["uncached_input_tokens"])
            self.assertEqual(60.0, rows["codex-task"]["queue_wait_seconds"])
            self.assertEqual("antigravity", rows["agy-task"]["execution"]["worker_family"])
            self.assertEqual("claude-gpt", rows["agy-task"]["model"]["model_group"])
            self.assertEqual("unavailable", rows["agy-task"]["token_usage_source"])
            self.assertEqual("token_free", rows["shell-task"]["token_usage_source"])
            self.assertEqual(1, report["summary"]["token_usage_rows"])
            self.assertEqual(2120, report["summary"]["token_totals"]["known_total_tokens"])

            human_code, human_output = run_cli(["--config", str(config_path), "execution-report", "--project", "project-a"])

            self.assertEqual(0, human_code)
            self.assertIn("EXECUTION REPORT", human_output)
            self.assertNotIn("\t", human_output)
            self.assertIn("FINISHED             TASK", human_output)
            self.assertIn("total=2120", human_output)
            self.assertIn("claude-gpt", human_output)

    def test_execution_report_rejects_negative_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(["--config", str(config_path), "execution-report", "--limit", "-1"])

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("--limit must be non-negative", stderr)

    def test_routing_report_accepts_supplemental_execution_evidence_without_changing_task_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "queue task",
                tmp,
                task_id="queue-task",
                project_id="project-a",
                category="implementation",
                labels=["routing"],
            )
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)
            task_path = config.queue_dir / "queue-task.json"
            before = task_path.read_text(encoding="utf-8")
            private_path = f"{tmp}/private/session-transcript.jsonl"
            evidence_path = Path(tmp) / "subagent-evidence.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "record_kind": "codex_subagent_execution",
                        "work_id": "thread_sensitive_identifier_123456789",
                        "project_id": "project-a",
                        "category": "implementation",
                        "labels": ["routing"],
                        "routing_size": "small",
                        "routing_risk": "medium",
                        "verification_scope": ["unit"],
                        "prompt": f"raw prompt mentions {private_path}",
                        "session_id": "session_sensitive_identifier_123456789",
                        "thread_id": "thread_sensitive_identifier_123456789",
                        "last_run": {
                            "duration_seconds": 22,
                            "log_path": private_path,
                            "resolved_execution_config": {
                                "selection_rule": "codex-app-default",
                                "model_source": "codex_app_default",
                            },
                        },
                        "last_result": {
                            "status": "completed",
                            "summary": f"raw summary mentions {private_path}",
                            "changed_files": [private_path],
                            "verification": [f"python -m unittest {private_path}"],
                        },
                        "reviewer_codex": {
                            "decision": "pass",
                            "confidence": "high",
                            "findings": [{"severity": "info", "evidence": f"raw evidence {private_path}"}],
                        },
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "routing-report",
                    "--project",
                    "project-a",
                    "--execution-evidence-json",
                    str(evidence_path),
                    "--json",
                ]
            )
            after = task_path.read_text(encoding="utf-8")
            report = json.loads(output)
            serialized = json.dumps(report, sort_keys=True)
            evidence_row = report["execution_evidence_rows"][0]
            evidence_surfaces = {
                entry["key"]: entry for entry in report["execution_evidence_diagnostics"]["execution_surfaces"]
            }

            self.assertEqual(0, code)
            self.assertEqual(before, after)
            self.assertEqual(1, report["task_count"])
            self.assertEqual(1, report["execution_evidence_count"])
            self.assertEqual(1, report["groups"]["category"][0]["tasks"])
            self.assertEqual("codex_subagent", evidence_row["execution_surface"])
            self.assertFalse(evidence_row["subject"]["queue_task"])
            self.assertEqual("supplemental_execution_evidence", evidence_row["subject"]["kind"])
            self.assertEqual("codex_app_default", evidence_row["worker"]["model_source"])
            self.assertEqual(1, evidence_surfaces["codex_subagent"]["supplemental_evidence"])
            self.assertEqual(0, evidence_surfaces["codex_subagent"]["queue_tasks"])
            self.assertNotIn("raw prompt mentions", serialized)
            self.assertNotIn("raw summary mentions", serialized)
            self.assertNotIn("raw evidence", serialized)
            self.assertNotIn("session_sensitive_identifier_123456789", serialized)
            self.assertNotIn("thread_sensitive_identifier_123456789", serialized)
            self.assertNotIn(private_path, serialized)

    def test_routing_report_rejects_invalid_execution_evidence_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            evidence_path = Path(tmp) / "subagent-evidence.json"
            evidence_path.write_text(json.dumps({"record_kind": "raw_thread_dump"}), encoding="utf-8")

            code, output, stderr = run_cli_with_stderr(
                [
                    "--config",
                    str(config_path),
                    "routing-report",
                    "--execution-evidence-json",
                    str(evidence_path),
                    "--json",
                ]
            )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("unsupported execution evidence record_kind", stderr)

    def test_routing_report_accepts_checked_in_subagent_execution_evidence_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            example_path = Path(__file__).parents[1] / "examples" / "subagent-execution-evidence.example.json"

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "routing-report",
                    "--execution-evidence-json",
                    str(example_path),
                    "--json",
                ]
            )
            report = json.loads(output)
            serialized = json.dumps(report, sort_keys=True)
            row = report["execution_evidence_rows"][0]

            self.assertEqual(0, code)
            self.assertEqual(0, report["task_count"])
            self.assertEqual(1, report["execution_evidence_count"])
            self.assertEqual("codex_subagent", row["execution_surface"])
            self.assertFalse(row["subject"]["queue_task"])
            self.assertEqual("codex_app_default", row["worker"]["model_source"])
            self.assertNotIn("subagent-2026-07-03-routing-docs-001", serialized)

    def test_routing_eval_report_json_returns_bounded_public_safe_rows_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            private_path = f"{tmp}/private/session-transcript.jsonl"
            task = create_task(
                config,
                f"raw private prompt mentions {private_path}",
                tmp,
                task_id="eval-public-safe",
                project_id="project-a",
                category="implementation",
                labels=["routing", "eval"],
                routing_size="small",
                routing_risk="low",
                verification_scope=["unit"],
            )
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["session_id"] = "session_abcdefghijklmnopqrstuvwxyz"
            task["thread_id"] = "thread_abcdefghijklmnopqrstuvwxyz"
            task["execution_worktree_path"] = private_path
            task["last_run"] = {
                "execution_backend": "codex",
                "resolved_execution_config": {
                    "selection_rule": "standard",
                    "model_source": "explicit_model",
                    "execution_target": "local",
                },
            }
            task["last_result"] = {
                "task_id": "eval-public-safe",
                "status": "completed",
                "summary": f"raw summary mentions {private_path}",
                "changed_files": [private_path],
                "verification": [f"python -m unittest {private_path}"],
            }
            task["reviewer_codex"] = {"decision": "pass", "confidence": "high"}
            save_task(config, task)
            task_path = config.queue_dir / "eval-public-safe.json"
            before = task_path.read_text(encoding="utf-8")

            code, output = run_cli(["--config", str(config_path), "routing-eval-report", "--json"])
            after = task_path.read_text(encoding="utf-8")
            report = json.loads(output)
            serialized = json.dumps(report, sort_keys=True)
            row = report["evaluation_rows"][0]

            self.assertEqual(0, code)
            self.assertEqual(before, after)
            self.assertEqual(1, report["row_count"])
            self.assertIn("request_fingerprint", row)
            self.assertIn("task_vector", row)
            self.assertIn("worker", row)
            self.assertIn("reviewer", row)
            self.assertIn("provider_resource", row)
            self.assertIn("objective_checks", row)
            self.assertIn("task_vector_evaluation", row)
            self.assertIn("policy_usage", row)
            self.assertEqual("explicit_model", row["worker"]["model_source"])
            self.assertEqual("local", row["worker"]["execution_target"])
            self.assertEqual("codex", row["provider_resource"]["provider_id"])
            self.assertEqual("unknown", row["provider_resource"]["quota_boundary"])
            self.assertEqual("not_independent", row["provider_resource"]["sharing_assumption"])
            self.assertFalse(row["provider_resource"]["derived_from_capacity_pool"])
            self.assertNotIn("raw private prompt", serialized)
            self.assertNotIn("raw summary mentions", serialized)
            self.assertNotIn("session_abcdefghijklmnopqrstuvwxyz", serialized)
            self.assertNotIn("thread_abcdefghijklmnopqrstuvwxyz", serialized)
            self.assertNotIn(private_path, serialized)
            self.assertFalse(report["privacy"]["raw_paths_included"])
            self.assertFalse(row["privacy"]["raw_prompt_included"])
            self.assertFalse(row["task_vector_evaluation"]["privacy"]["raw_changed_files_included"])

    def test_routing_eval_report_keeps_execution_target_alias_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "alias routed work",
                tmp,
                task_id="eval-target-alias",
                project_id="project-a",
                category="implementation",
                labels=["routing", "eval"],
                routing_size="small",
                routing_risk="low",
                verification_scope=["unit"],
            )
            task["status"] = "completed"
            task["review_status"] = "accepted"
            task["last_run"] = {
                "execution_backend": "codex",
                "resolved_execution_config": {
                    "selection_rule": "high-capability",
                    "model_source": "target_alias",
                    "execution_target": "high_capability_current",
                },
            }
            task["last_result"] = {
                "task_id": "eval-target-alias",
                "status": "completed",
                "changed_files": ["src/example.py"],
                "verification": ["unit"],
            }
            task["reviewer_codex"] = {"decision": "pass", "confidence": "high"}
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "routing-eval-report", "--json"])
            report = json.loads(output)
            row = report["evaluation_rows"][0]

            self.assertEqual(0, code)
            self.assertEqual("target_alias", row["worker"]["model_source"])
            self.assertEqual("high_capability_current", row["worker"]["execution_target"])

    def test_routing_eval_report_stratifies_probe_lanes_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))

            def save_routed(task_id: str, experiment: str, *, review_status: str = "accepted") -> None:
                task = create_task(
                    config,
                    "eval work",
                    tmp,
                    task_id=task_id,
                    project_id="project-a",
                    category="implementation",
                    model_requirement_vector=requirement_vector(reasoning_depth="low", cost_sensitivity="high"),
                    routing_experiment=experiment,
                    routing_size="small",
                    routing_risk="low",
                    verification_scope=["unit"],
                )
                task["status"] = "completed"
                task["review_status"] = review_status
                task["attempts"] = 1
                task["last_result"] = {"task_id": task_id, "status": "completed", "verification": ["unit"]}
                task["reviewer_codex"] = {"decision": "pass" if review_status == "accepted" else "needs_fix"}
                save_task(config, task)

            save_routed("eval-baseline", "baseline")
            save_routed("eval-probe", "downshift_probe", review_status="needs_followup")
            save_routed("eval-guard", "upshift_guard")

            code, output = run_cli(
                ["--config", str(config_path), "routing-eval-report", "--project", "project-a", "--limit", "0", "--json"]
            )
            report = json.loads(output)
            probe_lanes = report["evaluation_diagnostics"]["probe_lanes"]
            family_groups = {entry["key"]: entry for entry in probe_lanes["by_lane_family"]}
            bucket_lanes = {entry["key"]: entry for entry in probe_lanes["by_task_bucket_lane"]}
            req_lanes = {entry["key"]: entry for entry in probe_lanes["by_model_requirement_lane"]}
            req_key = report["evaluation_rows"][0]["worker"]["model_requirement_key"]

            self.assertEqual(0, code)
            self.assertTrue(probe_lanes["advisory"]["read_only"])
            self.assertFalse(probe_lanes["advisory"]["mutation_allowed"])
            self.assertEqual(1, family_groups["baseline"]["accepted"])
            self.assertEqual(1, family_groups["probe"]["needs_fix_or_rejected"])
            self.assertEqual(1, family_groups["guard"]["first_pass_accepted"])
            self.assertEqual(1, bucket_lanes["size=small risk=low verify=unit lane=probe"]["needs_fix_or_rejected"])
            self.assertEqual(1, req_lanes[f"{req_key}/lane=guard"]["accepted"])
            self.assertEqual([], report["execution_evidence_diagnostics"]["probe_lanes"]["by_lane_family"])

    def test_routing_eval_report_filters_limit_and_hashes_project_root_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for index, project_id, category, labels in (
                (0, "project-a", "implementation", ["eval"]),
                (1, "project-a", "docs", ["eval"]),
                (2, "project-b", "implementation", ["other"]),
            ):
                task = create_task(
                    config,
                    "work",
                    tmp,
                    task_id=f"eval-filter-{index}",
                    project_id=project_id,
                    category=category,
                    labels=labels,
                )
                task["status"] = "completed"
                task["review_status"] = "accepted"
                save_task(config, task)

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "routing-eval-report",
                    "--project",
                    "project-a",
                    "--project-root",
                    tmp,
                    "--label",
                    "eval",
                    "--limit",
                    "1",
                    "--json",
                ]
            )
            report = json.loads(output)
            serialized = json.dumps(report, sort_keys=True)

            self.assertEqual(0, code)
            self.assertEqual(2, report["filtered_count"])
            self.assertEqual(1, report["row_count"])
            self.assertEqual("project-a", report["filters"]["project"])
            self.assertTrue(report["filters"]["project_root_filter_applied"])
            self.assertTrue(report["filters"]["project_root_hash"].startswith("sha256:"))
            self.assertNotIn(tmp, serialized)
            self.assertEqual("project-a", report["evaluation_rows"][0]["task_vector"]["project"]["project_id"])

    def test_routing_eval_report_rejects_negative_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output, stderr = run_cli_with_stderr(
                ["--config", str(config_path), "routing-eval-report", "--limit", "-1"]
            )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn("--limit must be non-negative", stderr)

    def test_routing_eval_report_keeps_supplemental_execution_evidence_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            evidence_path = Path(tmp) / "subagent-evidence.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "records": [
                            {
                                "record_kind": "codex_subagent_execution",
                                "work_id": "codex-thread-not-for-output-123456789",
                                "project_id": "project-a",
                                "category": "docs",
                                "labels": ["eval"],
                                "routing_experiment": "downshift_probe",
                                "routing_size": "small",
                                "routing_risk": "low",
                                "verification_scope": ["manual"],
                                "last_run": {
                                    "duration_seconds": 5,
                                    "resolved_execution_config": {"selection_rule": "codex-app-default"},
                                },
                                "last_result": {"status": "completed", "verification": ["manual review"]},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(
                [
                    "--config",
                    str(config_path),
                    "routing-eval-report",
                    "--execution-evidence-json",
                    str(evidence_path),
                    "--json",
                ]
            )
            report = json.loads(output)
            row = report["execution_evidence_rows"][0]
            serialized = json.dumps(report, sort_keys=True)
            evidence_lanes = report["execution_evidence_diagnostics"]["probe_lanes"]
            evidence_lane_groups = {entry["key"]: entry for entry in evidence_lanes["by_lane_family"]}

            self.assertEqual(0, code)
            self.assertEqual(0, report["row_count"])
            self.assertEqual([], report["evaluation_rows"])
            self.assertEqual(1, report["execution_evidence_count"])
            self.assertEqual("codex_subagent", row["execution_surface"])
            self.assertEqual("supplemental_execution_evidence", row["subject"]["kind"])
            self.assertEqual("codex_app_default", row["worker"]["model_source"])
            self.assertEqual("downshift_probe", row["routing"]["routing_experiment"])
            self.assertTrue(evidence_lanes["advisory"]["read_only"])
            self.assertFalse(evidence_lanes["advisory"]["mutation_allowed"])
            self.assertEqual(1, evidence_lane_groups["probe"]["tasks"])
            self.assertNotIn("codex-thread-not-for-output-123456789", serialized)

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
                    self.assertEqual(["[M]", "TITLE", "STATUS", "DETAIL"], list_lines(output)[0].split())
                    rows = compact_list_rows(output)
                    self.assertEqual(1, len(rows))
                    self.assertEqual("..new", rows[0]["STATUS"])
                    self.assertEqual("project-a", rows[0]["PROJECT"])

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
            self.assertEqual(["[M]", "TITLE", "STATUS", "DETAIL"], list_lines(output)[0].split())
            self.assertEqual("..new", compact_list_rows(output)[0]["STATUS"])

            code, output = run_cli(["--config", str(config_path), "list", "--project-root", tmp])

            self.assertEqual(0, code)
            self.assertEqual("..new", compact_list_rows(output)[0]["STATUS"])

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
                ("discarded", "completed"),
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
            discarded = load_task(config, "discarded")
            discarded["review_status"] = "rejected"
            discarded["execution_mode"] = "git_worktree"
            discarded["execution_worktree_status"] = "cleaned"
            discarded["execution_cleanup_kind"] = "discard"
            discarded["execution_cleanup_result_applied"] = False
            save_task(config, discarded)
            needs_followup = load_task(config, "needs-followup")
            needs_followup["review_status"] = "needs_followup"
            save_task(config, needs_followup)

            code, output = run_cli(["--config", str(config_path), "list"])
            all_code, all_output = run_cli(["--config", str(config_path), "list", "--all", "--color=never"])

            self.assertEqual(0, code)
            self.assertEqual(0, all_code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            all_rows = {row["ID"]: row for row in compact_list_rows(all_output)}
            self.assertEqual("||capacity", rows["runnable"]["STATUS"])
            self.assertEqual("||capacity", rows["resume"]["STATUS"])
            self.assertEqual(">>exec", rows["running"]["STATUS"])
            self.assertEqual("??review", rows["blocked"]["STATUS"])
            self.assertEqual("??error", rows["failed"]["STATUS"])
            self.assertEqual("??review", rows["completed"]["STATUS"])
            self.assertEqual("awaiting review", rows["completed"]["DETAIL"])
            self.assertEqual("??fix", rows["rejected"]["STATUS"])
            self.assertEqual("review rejected; rejected", rows["rejected"]["NOTE"])
            self.assertNotIn("discarded", rows)
            self.assertEqual("--rejected", all_rows["discarded"]["STATUS"])
            self.assertEqual("rejected; discarded; not applied", all_rows["discarded"]["NOTE"])
            self.assertEqual("++followup", rows["needs-followup"]["STATUS"])
            self.assertIn("needs follow-up: create/link fix or resolve", rows["needs-followup"]["NOTE"])
            self.assertNotIn("accepted", rows)
            self.assertNotIn("archived", rows)

    def test_needs_followup_reports_next_action_for_unlinked_and_accepted_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            parent = create_task(config, "parent work", tmp, task_id="parent")
            parent["status"] = "completed"
            parent["review_status"] = "needs_followup"
            parent["chain_status"] = "needs_fix"
            save_task(config, parent)
            fix = create_task(config, "fix work", tmp, task_id="fix")
            fix["status"] = "completed"
            fix["review_status"] = "accepted"
            fix["parent_task_id"] = "parent"
            fix["subtask_for"] = "parent"
            save_task(config, fix)

            code, output = run_cli(["--config", str(config_path), "list", "--color=never"])
            summary_code, summary_output = run_cli(["--config", str(config_path), "summary", "parent"])
            review_code, review_output = run_cli(["--config", str(config_path), "review-next", "--dry-run", "--json"])
            review_report = json.loads(review_output)
            text_code, text_output = run_cli(["--config", str(config_path), "review-next", "--dry-run"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("++followup", rows["parent-work"]["STATUS"])
            self.assertIn("follow-up fix accepted; resolve original", rows["parent-work"]["NOTE"])
            self.assertEqual(0, summary_code)
            self.assertIn("follow_up_state: accepted", summary_output)
            self.assertIn("follow_up_next_action: verify the accepted follow-up covers", summary_output)
            self.assertIn('cbr resolve parent --resolution superseded --reason "handled by follow-up task"', summary_output)
            self.assertIn("- fix state=accepted status=completed review_status=accepted", summary_output)
            self.assertEqual(0, review_code)
            self.assertEqual("accepted", review_report["follow_up_action"]["state"])
            self.assertEqual(["fix"], review_report["follow_up_action"]["linked_task_ids"])
            self.assertEqual(0, text_code)
            self.assertIn("follow_up_action: accepted", text_output)
            self.assertIn('resolve: cbr resolve parent --resolution superseded --reason "handled by follow-up task"', text_output)

    def test_list_review_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id, review in (
                ("unreviewed", None),
                ("accepted", "accepted"),
                ("rejected", "rejected"),
                ("discarded", "rejected"),
                ("followup", "needs_followup"),
            ):
                create_task(config, task_id, tmp, task_id=task_id)
                set_status(config, task_id, "completed")
                if review:
                    task = load_task(config, task_id)
                    task["review_status"] = review
                    save_task(config, task)
            discarded = load_task(config, "discarded")
            discarded["execution_mode"] = "git_worktree"
            discarded["execution_worktree_status"] = "cleaned"
            discarded["execution_cleanup_kind"] = "discard"
            discarded["execution_cleanup_result_applied"] = False
            save_task(config, discarded)

            code, output = run_cli(["--config", str(config_path), "list", "--unreviewed"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("??review", rows["unreviewed"]["STATUS"])
            self.assertNotIn("accepted", rows)
            self.assertNotIn("rejected", rows)
            self.assertNotIn("discarded", rows)
            self.assertNotIn("followup", rows)

            code, output = run_cli(["--config", str(config_path), "list", "--needs-review"])

            self.assertEqual(0, code)
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("??review", rows["unreviewed"]["STATUS"])
            self.assertEqual("??fix", rows["rejected"]["STATUS"])
            self.assertNotIn("discarded", rows)
            self.assertEqual("++followup", rows["followup"]["STATUS"])
            self.assertNotIn("accepted", rows)

    def test_list_distinguishes_pending_reviewer_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            for task_id in ("plain", "needs-fix", "chain-fix", "pass", "chain-pass", "failed-review"):
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
            failed_review = load_task(config, "failed-review")
            failed_review["reviewer_codex"] = {"decision": "failed_review"}
            save_task(config, failed_review)

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
            self.assertEqual("??review", rows["plain"]["STATUS"])
            self.assertEqual("??fix", rows["needs-fix"]["STATUS"])
            self.assertEqual("??fix", rows["chain-fix"]["STATUS"])
            self.assertEqual("??review", rows["pass"]["STATUS"])
            self.assertEqual("??review", rows["chain-pass"]["STATUS"])
            self.assertEqual("??error", rows["failed-review"]["STATUS"])
            self.assertIn("reviewer needs fix; run reject --follow-up", rows["needs-fix"]["NOTE"])
            self.assertIn("reviewer needs fix; run reject --follow-up", rows["chain-fix"]["NOTE"])
            self.assertIn("reviewer passed; run accept", rows["pass"]["NOTE"])
            self.assertIn("reviewer passed; run accept", rows["chain-pass"]["NOTE"])
            self.assertIn("review process failed; rerun review-next", rows["failed-review"]["NOTE"])
            needs_review_rows = {row["ID"]: row for row in compact_list_rows(needs_review_output)}
            self.assertEqual(set(rows), set(needs_review_rows))
            self.assertEqual("??fix", needs_review_rows["needs-fix"]["STATUS"])
            self.assertEqual("??review", needs_review_rows["pass"]["STATUS"])
            self.assertEqual("??error", needs_review_rows["failed-review"]["STATUS"])
            json_tasks = {task["id"]: task for task in json.loads(json_output)}
            self.assertEqual("completed", json_tasks["needs-fix"]["status"])
            self.assertEqual("unreviewed", json_tasks["needs-fix"]["review_status"])
            self.assertNotIn("review_needs_fix", json_output)
            self.assertNotIn("review_pass_pending", json_output)
            self.assertIn("*       ??fix  [N] needs-fix", graph_output)
            self.assertIn("*       ??review  [N] pass", graph_output)
            self.assertIn("*       ??error  [N] failed-review", graph_output)
            self.assertIn("\033[101;97mfix\033[0m", color_output)
            self.assertIn("\033[102;30mreview\033[0m", color_output)
            self.assertIn("\033[101;97merror\033[0m", color_output)

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
            self.assertIn("STATUS: ??fix", output)
            self.assertIn("DETAIL: reviewer needs fix; run reject", output)
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
            self.assertEqual("awaiting review", rows["work"]["DETAIL"])
            self.assertNotIn("mechanical auto-review enabled", rows["work"]["NOTE"])

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
            self.assertEqual(["[M]", "TITLE", "STATUS", "DETAIL"], list_lines(output)[0].split())
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("??review", rows["completed"]["STATUS"])
            self.assertEqual("--archived", rows["archived"]["STATUS"])

    def test_status_filter_can_show_archived_without_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "archived", tmp, task_id="archived")
            create_task(config, "runnable", tmp, task_id="runnable")
            set_status(config, "archived", "archived")

            code, output = run_cli(["--config", str(config_path), "list", "--status", "archived"])

            self.assertEqual(0, code)
            self.assertEqual(["[M]", "TITLE", "STATUS", "DETAIL"], list_lines(output)[0].split())
            rows = {row["ID"]: row for row in compact_list_rows(output)}
            self.assertEqual("--archived", rows["archived"]["STATUS"])
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
            self.assertEqual("##dep", rows["child"]["STATUS"])
            self.assertIn("blocked by dep (not_completed)", rows["child"]["DETAIL"])
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
            self.assertEqual("##dep", rows["child"]["STATUS"])
            self.assertIn("2 dependency blockers: not completed (not_completed)", rows["child"]["DETAIL"])
            self.assertNotIn("not accepted (not_accepted)\n", rows["child"]["DETAIL"])

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

    def test_list_graph_contract_separates_dependency_edges_from_subtask_rows(self) -> None:
        """Contract for the pending list --graph renderer rewrite."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, dependency_requires_accepted_review=True)
            config = Config.load(str(config_path))
            shared_parser = create_task(config, "Build shared parser", tmp, task_id="shared-parser", project_id="project-a")
            shared_parser["status"] = "completed"
            shared_parser["review_status"] = "accepted"
            save_task(config, shared_parser)
            parser_tests = create_task(config, "Add parser tests", tmp, task_id="parser-tests", project_id="project-a")
            parser_tests["status"] = "completed"
            parser_tests["review_status"] = "accepted"
            save_task(config, parser_tests)
            create_task(
                config,
                "Wire parser into CLI",
                tmp,
                task_id="wire-cli",
                project_id="project-a",
                depends_on=["shared-parser", "parser-tests"],
            )
            release = create_task(config, "Release CLI parser change", tmp, task_id="release-cli", project_id="project-a")
            release["status"] = "completed"
            release["review_status"] = "accepted"
            release["blocking_subtask_ids"] = ["fix-review"]
            save_task(config, release)
            fix_review = create_task(
                config,
                "Fix review comments for parser change",
                tmp,
                task_id="fix-review",
                project_id="project-a",
            )
            fix_review["status"] = "completed"
            fix_review["review_status"] = "unreviewed"
            fix_review["parent_task_id"] = "release-cli"
            fix_review["subtask_for"] = "release-cli"
            fix_review["subtask_type"] = "auto_review_fix"
            save_task(config, fix_review)

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=never"])
            color_code, color_output = run_cli(
                ["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=always"]
            )

            self.assertEqual(0, code)
            self.assertEqual(0, color_code)
            self.assertIn("[project-a]", output)
            self.assertIn("*       --success  [N] Build shared parser", output)
            self.assertIn("| *     --success  [N] Add parser tests", output)
            self.assertIn(" \\|", output)
            self.assertIn("  *     ..new  [N] Wire parser into CLI", output)
            assert_graph_connector_attaches(self, output, "Wire parser into CLI")
            self.assertIn("*       ++followup  [N] Release CLI parser change", output)
            self.assertIn("└─ ??review  [N] Fix review comments for parser change", output)
            self.assertNotIn("└─ * ??review", output)
            self.assertNotIn("Build shared parser", strip_ansi(color_output).split("Wire parser into CLI", maxsplit=1)[1])
            self.assertIn("\033[2mFix review comments for parser change\033[0m", color_output)
            self.assertNotIn("\033[2mBuild shared parser\033[0m", color_output)
            self.assertNotIn("\033[2mAdd parser tests\033[0m", color_output)

    def test_list_attaches_review_followup_children_without_repeating_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            source = create_task(config, "Source task", tmp, task_id="source", project_id="project-a")
            source["status"] = "completed"
            source["review_status"] = "needs_followup"
            source["chain_status"] = "fixing"
            source["last_auto_fix_task_id"] = "fix"
            source["blocking_subtask_ids"] = ["fix"]
            save_task(config, source)
            fix = create_task(
                config,
                "Follow-up fix",
                tmp,
                task_id="fix",
                project_id="project-a",
                model_requirement_vector=requirement_vector(
                    reasoning_depth="high",
                    context_need="high",
                    tool_reliability="high",
                    cost_sensitivity="low",
                ),
            )
            fix["review_followup_for"] = "source"
            fix["subtask_type"] = "auto_review_fix"
            fix["subtask_for"] = "source"
            fix["parent_task_id"] = "source"
            save_task(config, fix)
            separate = create_task(config, "Separate work", tmp, task_id="separate", project_id="project-a")
            save_task(config, separate)

            code, output = run_cli(["--config", str(config_path), "list", "--color=never"])
            rows = compact_list_rows(output)
            titles = [row["TITLE"] for row in rows]

            self.assertEqual(0, code)
            self.assertEqual(["Source task", "└─ Follow-up fix", "Separate work"], titles)
            self.assertEqual("[D]", rows[1]["MODEL"])
            self.assertEqual("..new", rows[1]["STATUS"])
            self.assertIn("review fix for source", rows[1]["DETAIL"])

    def test_list_graph_renders_review_followups_as_non_dim_attached_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            source = create_task(config, "Source task", tmp, task_id="source", project_id="project-a")
            source["status"] = "completed"
            source["review_status"] = "needs_followup"
            source["chain_status"] = "fixing"
            source["last_auto_fix_task_id"] = "fix"
            save_task(config, source)
            fix = create_task(config, "Follow-up fix", tmp, task_id="fix", project_id="project-a")
            fix["review_followup_for"] = "source"
            fix["subtask_type"] = "auto_review_fix"
            fix["subtask_for"] = "source"
            save_task(config, fix)

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=never"])
            color_code, color_output = run_cli(
                ["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=always"]
            )

            self.assertEqual(0, code)
            self.assertEqual(0, color_code)
            self.assertIn("*       ++followup  [N] Source task", output)
            self.assertIn("        └─ ..new  [N] Follow-up fix", output)
            self.assertNotIn("└─ * ..new", output)
            self.assertNotIn("\033[2mFollow-up fix\033[0m", color_output)
            self.assertNotIn("\033[2m        └─ \033[0m", color_output)

    def test_list_graph_uses_compact_projection_labels_without_inline_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "Dependency source", tmp, task_id="dep", project_id="project-a")
            create_task(
                config,
                "Dependent source",
                tmp,
                task_id="child",
                project_id="project-a",
                depends_on=["dep"],
            )
            review = create_task(config, "Review checkpoint", tmp, task_id="review", project_id="project-a")
            review["status"] = "completed"
            review["review_status"] = "unreviewed"
            save_task(config, review)

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=never"])

            self.assertEqual(0, code)
            self.assertIn("*       ..new  [N] Dependency source", output)
            self.assertIn("*       ##dep  [N] Dependent source", output)
            self.assertIn("*       ??review  [N] Review checkpoint", output)
            self.assertNotIn("runnable", output)
            self.assertNotIn("blocked_dependency", output)
            self.assertNotIn("dependency blocked", output)
            self.assertNotIn("awaiting review", output)
            self.assertNotIn("DETAIL", output)

    def test_list_graph_keeps_shared_dependency_sibling_edges_attached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "Record model source provenance", tmp, task_id="record", project_id="project-a")
            create_task(
                config,
                "Diagnose model freshness state",
                tmp,
                task_id="diagnose",
                project_id="project-a",
                depends_on=["record"],
            )
            create_task(
                config,
                "Group reports by model source",
                tmp,
                task_id="group",
                project_id="project-a",
                depends_on=["record"],
            )

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=never"])

            self.assertEqual(0, code)
            self.assertIn("*       ..new  [N] Record model source provenance", output)
            self.assertIn("*       ##dep  [N] Diagnose model freshness state", output)
            self.assertIn("*       ##dep  [N] Group reports by model source", output)
            self.assertNotIn("| *     ##dep  [N] Diagnose model freshness state", output)
            assert_graph_connector_attaches(self, output, "Diagnose model freshness state")
            assert_graph_connector_attaches(self, output, "Group reports by model source")

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
            self.assertIn("*       --success  [N] Done dependency", output)
            self.assertIn("        └─ ##dep  [N] Child work", output)
            self.assertIn("*       ??review  [N] Review dependency", output)
            self.assertNotIn("└─ * ##dep", output)
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
            self.assertIn("*       ..new  [N] Very long", output)
            self.assertIn("|              title that should wrap", output)
            self.assertIn("|              under the dependency edge", output)
            self.assertIn("|", output)
            self.assertIn("*       ##dep  [N] Very", output)
            self.assertIn("               that should wrap under the", output)
            self.assertIn("               source node", output)

    def test_list_graph_renders_linear_dependency_chain_without_join_connectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "Task A", tmp, task_id="task-a", project_id="project-a")
            create_task(
                config,
                "Task B",
                tmp,
                task_id="task-b",
                project_id="project-a",
                depends_on=["task-a"],
            )
            create_task(
                config,
                "Task C",
                tmp,
                task_id="task-c",
                project_id="project-a",
                depends_on=["task-b"],
            )
            create_task(
                config,
                "Task D",
                tmp,
                task_id="task-d",
                project_id="project-a",
                depends_on=["task-c"],
            )

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--graph"])

            self.assertEqual(0, code)
            self.assertNotIn(" \\|", output)
            self.assertIn("*       ..new  [N] Task A", output)
            self.assertIn("*       ##dep  [N] Task B", output)
            self.assertIn("*       ##dep  [N] Task C", output)
            self.assertIn("*       ##dep  [N] Task D", output)
            self.assertNotIn("  *     [N]", output)
            lines = [strip_ansi(line) for line in output.splitlines() if line.strip()]
            node_lines = [line for line in lines if "[N] Task" in line]
            self.assertEqual(4, len(node_lines))
            star_positions = {line.index("*") for line in node_lines}
            self.assertEqual(1, len(star_positions))
            transitions = [line for line in lines if line.strip() == "|"]
            self.assertGreaterEqual(len(transitions), 3)

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
            self.assertIn("*       ..new  [N] Parent source task", output)
            self.assertIn("           ├─ ##dep  [N] Very", output)
            self.assertIn("           │         source title that should", output)
            self.assertIn("           │         wrap inside graph mode", output)
            self.assertIn("           └─ ..new  [N] Second child", output)
            self.assertIn("*       ..new  [N] Very long dependency", output)
            self.assertIn("               dependency edge", output)
            self.assertNotIn("├─ * ##dep", output)

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
            self.assertIn("*       ..new  [N] Very long first", output)
            self.assertIn("|              title that should keep its", output)
            self.assertIn("|              sibling tree rail", output)
            self.assertIn("| *     ..new  [N] Very long second", output)
            self.assertIn("| |            title that should keep its own", output)
            self.assertIn("| |            wrapped guide", output)
            self.assertIn(" \\|", output)
            self.assertIn("  *     ##dep  [N] Source task", output)

    def test_list_graph_colors_nodes_and_dependency_lanes_from_glyph_metadata(self) -> None:
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
                code, output = run_cli(
                    ["--config", str(config_path), "list", "--project", "project-a", "--graph", "--color=always"]
                )

            self.assertEqual(0, code)
            lines = output.splitlines()
            plain_lines = [strip_ansi(line) for line in lines]
            dep1_line = next(line for line in lines if "Very long first" in strip_ansi(line))
            dep2_line = next(line for line in lines if "Very long second" in strip_ansi(line))
            dep2_wrap_line = next(line for line in lines if "title that should keep its own" in strip_ansi(line))
            target_line = next(line for line in lines if "Source task" in strip_ansi(line))
            target_index = plain_lines.index(strip_ansi(target_line))
            transition_line = lines[target_index - 1]

            dep1_node_color = ansi_code_for_visible_char(dep1_line, 0)
            passing_lane_color = ansi_code_for_visible_char(dep2_line, 0)
            dep2_node_color = ansi_code_for_visible_char(dep2_line, 2)
            self.assertIsNotNone(dep1_node_color)
            self.assertEqual(dep1_node_color, passing_lane_color)
            self.assertNotEqual(passing_lane_color, dep2_node_color)

            wrap_first_lane_color = ansi_code_for_visible_char(dep2_wrap_line, 0)
            wrap_second_lane_color = ansi_code_for_visible_char(dep2_wrap_line, 2)
            self.assertEqual(passing_lane_color, wrap_first_lane_color)
            self.assertEqual(dep2_node_color, wrap_second_lane_color)
            self.assertNotEqual(wrap_first_lane_color, wrap_second_lane_color)

            merging_lane_color = ansi_code_for_visible_char(transition_line, 1)
            incoming_lane_color = ansi_code_for_visible_char(transition_line, 2)
            target_node_color = ansi_code_for_visible_char(target_line, 2)
            self.assertEqual(passing_lane_color, merging_lane_color)
            self.assertEqual(dep2_node_color, incoming_lane_color)
            self.assertNotEqual(merging_lane_color, incoming_lane_color)
            self.assertNotEqual(incoming_lane_color, target_node_color)

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
            self.assertIn("*       ..new  [N] Very long parent", output)
            self.assertIn("           │   keep its subtask guide", output)
            self.assertIn("           ├─ ..new  [N] Very long", output)
            self.assertIn("           │         child source title", output)
            self.assertIn("           │         that should wrap", output)
            self.assertIn("           │         inside graph mode", output)
            self.assertIn("           └─ ..new  [N] Very long", output)
            self.assertIn("                     own wrapped guide", output)
            self.assertNotIn("├─ * ..new", output)

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
            self.assertIn("Prepare parser cleanup notes", compact_output)
            self.assertIn("Publish CLI parser release notes", compact_output)
            self.assertIn("Release CLI parser change", compact_output)
            self.assertIn("└─ Fix review comments for parser change", compact_output)
            self.assertIn("LAST_RESULT", verbose_output)
            self.assertIn("[demo]", graph_output)
            self.assertIn("##dep", compact_output)
            self.assertIn(">>exec", compact_output)
            self.assertIn("*       --success  [N] Build shared parser", graph_output)
            self.assertIn("| *     --success  [N] Add parser tests", graph_output)
            self.assertIn("  *     ##dep  [N] Wire parser into CLI", graph_output)
            assert_graph_connector_attaches(self, graph_output, "Wire parser into CLI")
            self.assertIn("*       ++followup  [N] Release CLI parser change", graph_output)
            self.assertIn("|          └─ ..new  [N] Fix review comments for parser change", graph_output)
            self.assertIn("| *     --success  [N] Shared parser implementation complete", graph_output)
            self.assertIn("| *     ??review  [N] CLI docs draft awaiting review", graph_output)
            self.assertIn("| *     ??review  [N] Release checklist review pending", graph_output)
            self.assertIn("| *     ++apply  [N] Release checklist merge ready, not applied", graph_output)
            self.assertIn("  *     ##dep  [N] Publish CLI parser release notes", graph_output)
            assert_graph_connector_attaches(self, graph_output, "Publish CLI parser release notes")
            self.assertNotIn("└─ * ..new", graph_output)
            self.assertNotIn("|       ├─", graph_output)
            self.assertNotIn("demo-blocked", graph_output)
            self.assertNotIn("demo-done", graph_output)
            self.assertNotIn("demo-missing", graph_output)
            self.assertNotIn("awaiting_review", graph_output)
            self.assertNotIn("accepted_unapplied", graph_output)
            self.assertNotIn("blocked_dependency", graph_output)
            self.assertEqual(
                "\n".join(
                    [
                        "[demo]",
                        "*       ..new  [S] Prepare parser cleanup notes",
                        "*       --success  [N] Build shared parser",
                        "| *     --success  [N] Add parser tests",
                        " \\|",
                        "  *     ##dep  [N] Wire parser into CLI",
                        "*       ++followup  [N] Release CLI parser change",
                        "|          └─ ..new  [N] Fix review comments for parser change",
                        "| *     --success  [N] Shared parser implementation complete",
                        "| *     ??review  [N] CLI docs draft awaiting review",
                        "| *     ??review  [N] Release checklist review pending",
                        "| *     ++apply  [N] Release checklist merge ready, not applied",
                        " \\|",
                        "  *     ##dep  [N] Publish CLI parser release notes",
                        "*       >>exec  [D] Run full regression suite",
                    ]
                )
                + "\n",
                graph_output,
            )
            self.assertIn("\033[1;97;43m??\033[0m\033[103;30mreview\033[0m", color_output)
            self.assertIn("\033[1;97;46m>>\033[0m\033[106;30mexec\033[0m", color_output)
            self.assertIn("\033[1;97;43m##\033[0m\033[100;93mdep\033[0m", color_output)
            self.assertIn("\033[100;92msuccess\033[0m", color_output)
            self.assertRegex(color_output, r"\033\[(35|36|34|32|33|91)m\*\033\[0m")
            self.assertRegex(color_output, r"\033\[(35|36|34|32|33|91)m\\\033\[0m")
            self.assertIn("\033[32m[N]\033[0m", color_output)
            self.assertNotIn("\033[2mShared parser implementation complete\033[0m", color_output)
            self.assertNotIn("\033[2mCLI docs draft awaiting review\033[0m", color_output)
            self.assertNotIn("\033[2mRelease checklist review pending\033[0m", color_output)
            self.assertNotIn("\033[2mRelease checklist merge ready, not applied\033[0m", color_output)
            self.assertIn("\033[2mFix review comments for parser change\033[0m", color_output)
            self.assertIn("\033[103;30mreview\033[0m", color_output)
            self.assertIn("\033[103;30mapply\033[0m", color_output)
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
            self.assertIn("| *     ??review  [N] CLI docs draft awaiting\n| |               review", text)
            self.assertIn("  *     ##dep  [N] Publish CLI parser release notes", text)
            self.assertIn(
                "| *     ??review  [N] Release checklist review\n| |               pending",
                text,
            )
            self.assertIn(
                "*       ++followup  [N] Release CLI parser change\n"
                "|          └─ ..new  [N] Fix review comments for\n"
                "|                    parser change",
                text,
            )
            self.assertRegex(output, r"\x1b\[[0-9;]*m\|\x1b\[0m       \x1b\[2m   └─")
            self.assertRegex(output, r"\x1b\[[0-9;]*m\|\x1b\[0m \x1b\[[0-9;]*m\|\x1b\[0m               review")
            self.assertRegex(output, r"\x1b\[[0-9;]*m\|\x1b\[0m                    \x1b\[2mparser change\x1b\[0m")
            shared_line = next(line for line in output.splitlines() if "Shared parser" in line)
            docs_line = next(line for line in output.splitlines() if "CLI docs draft" in line)
            checklist_line = next(line for line in output.splitlines() if "Release checklist" in line and "??" in strip_ansi(line))
            shared_colors = re.match(r"\x1b\[([0-9;]+)m\|\x1b\[0m \x1b\[([0-9;]+)m\*\x1b\[0m", shared_line)
            docs_colors = re.match(r"\x1b\[([0-9;]+)m\|\x1b\[0m \x1b\[([0-9;]+)m\*\x1b\[0m", docs_line)
            checklist_colors = re.match(r"\x1b\[([0-9;]+)m\|\x1b\[0m \x1b\[([0-9;]+)m\*\x1b\[0m", checklist_line)
            self.assertIsNotNone(shared_colors)
            self.assertIsNotNone(docs_colors)
            self.assertIsNotNone(checklist_colors)
            assert shared_colors is not None
            assert docs_colors is not None
            assert checklist_colors is not None
            self.assertNotEqual(shared_colors.group(1), shared_colors.group(2))
            node_colors = {shared_colors.group(2), docs_colors.group(2), checklist_colors.group(2)}
            self.assertEqual(3, len(node_colors))
            shared_continuation = next(line for line in output.splitlines() if "complete" in line)
            continuation_colors = re.match(
                r"\x1b\[([0-9;]+)m\|\x1b\[0m \x1b\[([0-9;]+)m\|\x1b\[0m",
                shared_continuation,
            )
            self.assertIsNotNone(continuation_colors)
            assert continuation_colors is not None
            self.assertEqual(shared_colors.group(1), continuation_colors.group(1))
            self.assertEqual(shared_colors.group(2), continuation_colors.group(2))
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
            self.assertEqual("##dep", rows["child"]["STATUS"])
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
            self.assertEqual(["[M]", "TITLE", "STATUS", "DETAIL"], lines[0].split())
            self.assertEqual("[project-a]", lines[1])
            self.assertTrue(lines[2].startswith("[N]  Child task title"))
            self.assertNotIn("title:", output)
            self.assertNotIn("(child-task)", output)
            self.assertEqual("-", rows["child-task-title"]["DEPS"])
            self.assertEqual("ready", rows["child-task-title"]["DETAIL"])
            self.assertEqual("[N]", rows["child-task-title"]["MODEL"])
            self.assertEqual("Child task title", rows["child-task-title"]["TITLE"])

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
            self.assertIn("fix requested", rows["review-task-title"]["NOTE"])
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
            detail = "\n".join(line for line in lines if "Review task title" in line or "fix requested" in line)
            self.assertNotIn("[N] dep-two (done)", detail)
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
            create_task(config, "unknown", tmp, task_id="unknown")
            unknown = load_task(config, "unknown")
            unknown["status"] = "external_unknown"
            save_task(config, unknown)

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
            self.assertIn("||capacity", never_output)
            self.assertIn("??review", never_output)
            self.assertIn("--success", never_output)
            self.assertIn("??error", never_output)
            self.assertIn("\033[1;97;46m||\033[0m\033[100;96mcapacity\033[0m", always_output)
            self.assertIn("\033[1;97;43m??\033[0m\033[103;30mreview\033[0m", always_output)
            self.assertIn("\033[1;97;42m--\033[0m\033[100;92msuccess\033[0m", always_output)
            self.assertIn("\033[1;97;41m??\033[0m\033[101;97merror\033[0m", always_output)
            self.assertIn("\033[100;96mcapacity\033[0m", always_output)
            self.assertIn("\033[103;30mreview\033[0m", always_output)
            self.assertIn("\033[106;30mexec\033[0m", always_output)
            self.assertIn("\033[100;92msuccess\033[0m", always_output)
            self.assertIn("\033[101;97merror\033[0m", always_output)
            self.assertIn("\033[32m[N]\033[0m", always_output)
            self.assertIn("\033[101;97merror\033[0m", no_color_output)
            self.assertNotIn("\033[", auto_no_color_output)
            self.assertNotIn("\033[", json_output)
            self.assertEqual(
                ["completed", "failed", "resume", "review", "runnable", "running", "unknown"],
                sorted(task["id"] for task in json.loads(json_output)),
            )

    def test_status_markers_use_bold_bright_white_foreground(self) -> None:
        color = ListColor(enabled=True)

        for status in color.STATUS_MARKERS:
            marker = color.status_marker(status)
            self.assertTrue(marker.startswith("\033[1;97;"), marker.encode())

    def test_list_project_sections_use_color_stripe_only_when_color_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="work", project_id="project-a")

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=72):
                always_code, always_output = run_cli(
                    ["--config", str(config_path), "list", "--project", "project-a", "--color=always"]
                )
                never_code, never_output = run_cli(
                    ["--config", str(config_path), "list", "--project", "project-a", "--color=never"]
                )

            always_section = always_output.splitlines()[0]
            never_section = never_output.splitlines()[0]
            self.assertEqual(0, always_code)
            self.assertEqual(0, never_code)
            self.assertTrue(always_section.startswith(ListColor.PROJECT_SECTION), always_section.encode())
            self.assertTrue(always_section.endswith(ListColor.RESET), always_section.encode())
            self.assertEqual(72, visible_line_widths(always_section)[0])
            self.assertEqual("[project-a]", strip_ansi(always_section).rstrip())
            self.assertEqual("[project-a]", never_section)

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
            self.assertNotIn("[N] dep (done)", never_output)
            self.assertIn("..new", never_output)
            self.assertNotIn("\033[2m[N] dep (done)\033[0m", always_output)

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
            self.assertEqual("##dep", rows["child"]["STATUS"])
            self.assertIn("3 dependency blockers: blocked dep (not_completed)", rows["child"]["DETAIL"])
            self.assertNotIn("[N] not accepted (not_accepted)", rows["child"]["DETAIL"])
            self.assertIn("\033[1;97;43m##\033[0m", always_output)

    def test_list_terminal_width_below_minimum_table_width_uses_block_layout_and_wraps(self) -> None:
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
            self.assertNotEqual(["[M]", "TITLE", "STATUS", "DETAIL"], list_lines(output)[0].split())
            self.assertIn("STATUS:", output)
            self.assertNotIn("ID:", output)
            self.assertNotIn("PROJECT:", output)
            self.assertIn("TITLE:", output)
            self.assertIn("DETAIL:", output)
            self.assertTrue(all(width <= 79 for width in visible_line_widths(output)))

    def test_list_uses_table_layout_at_fixed_width_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "Table title", tmp, task_id="task", project_id="project-a")

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=80):
                code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--color=never"])

            self.assertEqual(0, code)
            self.assertEqual(["[M]", "TITLE", "STATUS", "DETAIL"], list_lines(output)[0].split())
            self.assertIn("[project-a]", output)
            self.assertIn("[N]  Table title", output)
            self.assertTrue(all(width <= 80 for width in visible_line_widths(output)))

    def test_list_table_layout_threshold_is_independent_of_row_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "blocked dependency", tmp, task_id="blocked-dep")
            create_task(
                config,
                "Child task with dependency blocker",
                tmp,
                task_id="child",
                project_id="project-a",
                depends_on=["blocked-dep"],
            )

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=80):
                code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a", "--color=never"])

            self.assertEqual(0, code)
            self.assertEqual(["[M]", "TITLE", "STATUS", "DETAIL"], list_lines(output)[0].split())
            self.assertIn("dep_block", output)
            self.assertNotIn("STATUS:", output)
            self.assertTrue(all(width <= 80 for width in visible_line_widths(output)))

    def test_list_mid_width_uses_dependency_titles_and_wraps_notes(self) -> None:
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
            self.assertEqual(["[M]", "TITLE", "STATUS", "DETAIL"], list_lines(output)[0].split())
            self.assertIn("[N]  Task title", output)
            self.assertNotIn("[N] dependency (done)", output)
            self.assertNotIn("task-2026", output)
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
            self.assertEqual("[N]", rows["parent-work"]["MODEL"])
            self.assertEqual("Parent work", rows["parent-work"]["TITLE"])
            self.assertEqual("├─ Child work", rows["child-work"]["TITLE"])
            self.assertEqual("└─ Follow-up work", rows["follow-up-work"]["TITLE"])
            self.assertEqual("Other project", rows["other-project"]["TITLE"])

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

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=79):
                code, output = run_cli(["--config", str(config_path), "list", "--all", "--color=never"])

            self.assertEqual(0, code)
            self.assertTrue(all(width <= 79 for width in visible_line_widths(output)))
            self.assertIn("[M]:    [N]", output)
            self.assertIn("TITLE:  ├─ Very long first child title that should wrap while keeping tree", output)
            self.assertIn("        │  rails visible", output)
            self.assertIn("TITLE:  └─ Very long second child title that should keep its own wrapped guide", output)

            with patch("codex_batch_runner.cli.compact_terminal_width", return_value=50):
                code, output = run_cli(["--config", str(config_path), "list", "--all", "--color=never"])

            self.assertEqual(0, code)
            self.assertTrue(all(width <= 50 for width in visible_line_widths(output)))
            self.assertIn("TITLE:  ├─ Very long first child title", output)
            self.assertIn("        │  wrap while keeping tree rails visible", output)
            self.assertIn("TITLE:  └─ Very long second child title that", output)
            self.assertIn("           should keep its own wrapped guide", output)

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
            self.assertEqual("??review", rows["parent-work"]["STATUS"])
            self.assertEqual("└─ Accepted child", rows["accepted-child"]["TITLE"])
            self.assertNotIn("independent", rows)

    def test_list_review_filter_keeps_hidden_subtasks_when_parent_is_visible(self) -> None:
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

            code, output = run_cli(["--config", str(config_path), "list", "--needs-review", "--color=never"])
            json_code, json_output = run_cli(["--config", str(config_path), "list", "--needs-review", "--json"])
            rows = {row["ID"]: row for row in compact_list_rows(output)}

            self.assertEqual(0, code)
            self.assertEqual(0, json_code)
            self.assertEqual("??review", rows["parent-work"]["STATUS"])
            self.assertEqual("└─ Accepted child", rows["accepted-child"]["TITLE"])
            self.assertNotIn("independent", rows)
            self.assertEqual(["parent"], [task["id"] for task in json.loads(json_output)])

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
            self.assertEqual("??fix", rows["parent"]["STATUS"])
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
            self.assertEqual("++followup", rows["parent"]["STATUS"])
            self.assertIn("waiting on 1/3 subtasks", rows["parent"]["NOTE"])
            self.assertIn("oldest running 12m", rows["parent"]["NOTE"])
            self.assertIn("running for 12m", rows["running-fix"]["NOTE"])

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
            dep_color = re.search(r"(\x1b\[[0-9;]*m)##\x1b\[0m", output)

            self.assertEqual(0, code)
            self.assertIsNotNone(dep_color)
            self.assertIn("[100;93mdep[0m", output)

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
            self.assertEqual("--resolved", compact_list_rows(output)[0]["STATUS"])
            self.assertEqual("-", compact_list_rows(output)[0]["DEPS"])
            self.assertEqual("resolved: wont_fix; error: not worth retrying", compact_list_rows(output)[0]["NOTE"])

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
            self.assertEqual("--resolved", rows["task"]["STATUS"])
            self.assertEqual("resolved: superseded", rows["task"]["NOTE"])

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

    def test_apply_plan_dry_run_rejects_model_requirement_revision_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
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
                                "model_requirement_vector": {
                                    "dimensions": {"reasoning_depth": "low", "cost_sensitivity": "high"}
                                },
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

            self.assertEqual(1, code)
            self.assertIn("mode: dry-run", output)
            self.assertIn("model_requirement_vector is immutable", output)
            task = load_task(config, "task-a")
            self.assertEqual(2, task["model_requirement_vector"]["schema_version"])
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

    def test_apply_plan_dry_run_rejects_removed_execution_profile_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
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
            self.assertIn("execution_profile is no longer supported", output)

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
            config_path = write_config(tmp, trigger)
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
            self.assertEqual("??review", rows["done"]["STATUS"])
            self.assertEqual("awaiting review", rows["done"]["DETAIL"])

    def test_list_compact_output_includes_header_title_project_deps_and_empty_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)
            config = Config.load(str(config_path))
            create_task(config, "work", tmp, task_id="plain", project_id="project-a")
            create_task(config, "parent work", tmp, task_id="parent", project_id="project-a")
            create_task(config, "child work", tmp, task_id="child", depends_on=["parent"], project_id="project-a")
            parent = load_task(config, "parent")
            parent["status"] = "completed"
            parent["review_status"] = "accepted"
            save_task(config, parent)

            code, output = run_cli(["--config", str(config_path), "list", "--project", "project-a"])
            lines = list_lines(output)
            rows = compact_list_rows(output)
            plain_row = next(row for row in rows if row["TITLE"] == "work")
            child_row = next(row for row in rows if row["TITLE"] == "child work")

            self.assertEqual(0, code)
            self.assertEqual(["[M]", "TITLE", "STATUS", "DETAIL"], lines[0].split())
            self.assertNotIn("\t", output)
            self.assertEqual(
                {
                    "MODEL": "[N]",
                    "TITLE": "work",
                    "STATUS": "..new",
                    "PROJECT": "project-a",
                    "DETAIL": "ready",
                },
                {key: plain_row[key] for key in ("MODEL", "TITLE", "STATUS", "PROJECT", "DETAIL")},
            )
            self.assertEqual("ready", child_row["DETAIL"])

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
            self.assertEqual(["project-c", "project-b", "project-a"], [row["PROJECT"] for row in rows])
            self.assertEqual(["same-a", "same-b", "later"], [task["id"] for task in json.loads(json_output)])
            self.assertEqual(["[M]", "TITLE", "STATUS", "DETAIL"], lines[0].split())

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
            self.assertNotIn("dep (not_accepted)", list_output)
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
            self.assertEqual(Path(tmp).name, compact_list_rows(output)[0]["PROJECT"])
            self.assertEqual("-", compact_list_rows(output)[0]["DEPS"])
            self.assertEqual("ready", compact_list_rows(output)[0]["DETAIL"])

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
                    "MODEL",
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
            self.assertEqual("-", rows["verbose"]["MODEL"])
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

    def test_list_and_summary_show_model_requirement_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={
                    "model_selection_rules": [
                        {
                            "name": "low-cost-docs",
                            "when": {"reasoning_depth": "low"},
                            "config_overrides": {"model_reasoning_effort": "low"},
                            "budget_hint": "low-cost",
                        }
                    ]
                },
            )
            config = Config.load(str(config_path))
            create_task(
                config,
                "work",
                tmp,
                task_id="requirement",
                project_id="project-a",
                model_requirement_vector={
                    "source": "test",
                    "confidence": "medium",
                    "dimensions": {
                        "reasoning_depth": "low",
                        "context_need": "low",
                        "tool_reliability": "medium",
                        "latency_priority": "high",
                        "cost_sensitivity": "high",
                        "review_strictness": "medium",
                    },
                },
            )

            list_code, list_output = run_cli(["--config", str(config_path), "list", "--color", "never"])
            summary_code, summary_output = run_cli(["--config", str(config_path), "summary", "requirement"])

            self.assertEqual(0, list_code)
            self.assertIn("[S]  work", list_output)
            self.assertIn("plan cli_default/low-cost-docs/none", list_output)
            verbose_code, verbose_output = run_cli(["--config", str(config_path), "list", "--verbose"])
            verbose_rows = {row["ID"]: row for row in fixed_table_rows(verbose_output)}
            self.assertEqual(0, verbose_code)
            self.assertIn("reasoning_depth=low", verbose_rows["requirement"]["MODEL"])
            self.assertIn("cost_sensitivity=high", verbose_rows["requirement"]["MODEL"])
            self.assertEqual(0, summary_code)
            self.assertIn("model_requirement_vector=", summary_output)
            self.assertIn(
                "planned_execution: model_source=cli_default, selection_rule=low-cost-docs, "
                "execution_target=none, config_override_keys=model_reasoning_effort, budget_hint=low-cost",
                summary_output,
            )

    def test_planned_execution_note_is_hidden_when_actual_run_config_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(
                tmp,
                extra={
                    "model_selection_rules": [
                        {
                            "name": "low-cost-docs",
                            "when": {"reasoning_depth": "low"},
                            "config_overrides": {"model_reasoning_effort": "low"},
                        }
                    ]
                },
            )
            config = Config.load(str(config_path))
            task = create_task(
                config,
                "work",
                tmp,
                task_id="actual",
                model_requirement_vector=requirement_vector(reasoning_depth="low"),
            )
            task["last_run"] = {
                "resolved_execution_config": {
                    "selection_rule": "previous",
                    "model_source": "explicit_model",
                }
            }
            save_task(config, task)

            list_code, list_output = run_cli(["--config", str(config_path), "list", "--color", "never"])
            summary_code, summary_output = run_cli(["--config", str(config_path), "summary", "actual"])

            self.assertEqual(0, list_code)
            self.assertNotIn("plan cli_default/low-cost-docs/none", list_output)
            self.assertEqual(0, summary_code)
            self.assertNotIn("planned_execution:", summary_output)

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
            self.assertIn("startup stalled; retrying", list_output)
            self.assertIn("startup stalled earlier", list_output)
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
            self.assertEqual("reviewer-fix-enqueue", fix_task["review_followup_for"])
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

            human_code, human_output = run_cli(
                ["--config", str(config_path), "review-next", "--apply", "--mechanical-auto-accept"]
            )
            self.assertEqual(0, human_code)
            self.assertIn("- accept_deferred: mechanical gates failed (failing_gates=verification_present)", human_output)

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

    def test_run_next_json_emits_single_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            code, output = run_cli(["--config", str(config_path), "run-next", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("empty", report["status"])
            self.assertEqual(1, output.count('"status"'))

    def test_run_loop_json_continues_after_auto_accept_unblocks_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('trigger\\n', encoding='utf-8')",
                str(marker),
            ]
            config_path = write_config(
                tmp,
                trigger,
                dependency_requires_accepted_review=True,
                auto_review_mechanical_accept=True,
                codex_command=[sys.executable, str(FAKE_CODEX), "success"],
            )
            config = Config.load(str(config_path))
            repo = Path(tmp) / "repo"
            create_pushed_repo(repo)
            create_clean_completed_task(config, repo, "dep")
            create_task(config, "child", tmp, task_id="child", depends_on=["dep"])

            code, output = run_cli(["--config", str(config_path), "run-loop", "--json"])
            reports = json_lines(output)

            self.assertEqual(0, code)
            self.assertEqual(["review_accepted", "completed", "review_needed"], [item["status"] for item in reports])
            self.assertEqual("dep", reports[0]["task_id"])
            self.assertEqual("child", reports[1]["task_id"])
            self.assertEqual("accepted", load_task(config, "dep")["review_status"])
            self.assertEqual("completed", load_task(config, "child")["status"])
            self.assertFalse(marker.exists())

    def test_run_loop_json_stops_on_empty_pause_and_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as empty_tmp:
            empty_config_path = write_config(empty_tmp)

            empty_code, empty_output = run_cli(["--config", str(empty_config_path), "run-loop", "--json"])

            self.assertEqual(0, empty_code)
            self.assertEqual(["empty"], [item["status"] for item in json_lines(empty_output)])

        with tempfile.TemporaryDirectory() as pause_tmp:
            pause_config_path = write_config(pause_tmp)
            pause_config = Config.load(str(pause_config_path))
            create_task(pause_config, "ready", pause_tmp, task_id="ready")
            pause_config.state_file.write_text(
                json.dumps(
                    {
                        "runner_pause": {
                            "active": True,
                            "reason": "operator maintenance window",
                            "paused_at": "2026-06-22T00:00:00+00:00",
                            "paused_by": "operator",
                        }
                    }
                ),
                encoding="utf-8",
            )

            pause_code, pause_output = run_cli(["--config", str(pause_config_path), "run-loop", "--json"])

            self.assertEqual(0, pause_code)
            self.assertEqual(["paused"], [item["status"] for item in json_lines(pause_output)])
            self.assertEqual("runnable", load_task(pause_config, "ready")["status"])

        with tempfile.TemporaryDirectory() as cooldown_tmp:
            cooldown_config_path = write_config(cooldown_tmp)
            cooldown_config = Config.load(str(cooldown_config_path))
            create_task(cooldown_config, "ready", cooldown_tmp, task_id="ready")
            cooldown_config.state_file.write_text(
                json.dumps({"global_cooldown_until": "2999-01-01T00:00:00+00:00"}),
                encoding="utf-8",
            )

            cooldown_code, cooldown_output = run_cli(["--config", str(cooldown_config_path), "run-loop", "--json"])

            self.assertEqual(0, cooldown_code)
            self.assertEqual(["cooldown"], [item["status"] for item in json_lines(cooldown_output)])
            self.assertEqual("runnable", load_task(cooldown_config, "ready")["status"])

    def test_run_loop_suppresses_between_iteration_wake_hook_but_run_next_keeps_it(self) -> None:
        with tempfile.TemporaryDirectory() as loop_tmp:
            loop_marker = Path(loop_tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('trigger\\n', encoding='utf-8')",
                str(loop_marker),
            ]
            loop_config_path = write_config(
                loop_tmp,
                trigger,
                codex_command=[sys.executable, str(FAKE_CODEX), "success"],
            )
            loop_config = Config.load(str(loop_config_path))
            create_task(loop_config, "first", loop_tmp, task_id="task-1")
            create_task(loop_config, "second", loop_tmp, task_id="task-2")

            loop_code, loop_output = run_cli(["--config", str(loop_config_path), "run-loop", "--json"])

            self.assertEqual(0, loop_code)
            self.assertEqual(["completed", "completed", "empty"], [item["status"] for item in json_lines(loop_output)])
            self.assertFalse(loop_marker.exists())

        with tempfile.TemporaryDirectory() as next_tmp:
            next_marker = Path(next_tmp) / "trigger.log"
            trigger = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('trigger\\n', encoding='utf-8')",
                str(next_marker),
            ]
            next_config_path = write_config(
                next_tmp,
                trigger,
                codex_command=[sys.executable, str(FAKE_CODEX), "success"],
            )
            next_config = Config.load(str(next_config_path))
            create_task(next_config, "first", next_tmp, task_id="task-1")
            create_task(next_config, "second", next_tmp, task_id="task-2")

            next_code, next_output = run_cli(["--config", str(next_config_path), "run-next", "--json"])
            next_report = json.loads(next_output)

            self.assertEqual(0, next_code)
            self.assertEqual("completed", next_report["status"])
            self.assertEqual("task-1", next_report["task_id"])
            self.assertEqual("trigger\n", next_marker.read_text(encoding="utf-8"))

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

    def test_dashboard_command_dispatches_read_only_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp)

            with patch("codex_batch_runner.cli.serve_dashboard") as serve_dashboard:
                code, output = run_cli(
                    [
                        "--config",
                        str(config_path),
                        "dashboard",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "9876",
                    ]
                )

            self.assertEqual(0, code)
            self.assertEqual("", output)
            serve_dashboard.assert_called_once()
            called_config = serve_dashboard.call_args.args[0]
            self.assertIsInstance(called_config, Config)
            self.assertEqual("127.0.0.1", serve_dashboard.call_args.kwargs["host"])
            self.assertEqual(9876, serve_dashboard.call_args.kwargs["port"])


if __name__ == "__main__":
    unittest.main()

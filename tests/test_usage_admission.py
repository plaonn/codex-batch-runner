from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import codex_batch_runner.runner as runner_module
import codex_batch_runner.usage_admission as usage_module
from codex_batch_runner.codex import CodexResult
from codex_batch_runner.events import list_events
from codex_batch_runner.queue import create_task, load_task
from codex_batch_runner.runner import run_next
from codex_batch_runner.state import load_state
from codex_batch_runner.usage_admission import check_usage_admission
from tests.test_runner import make_config


NOW = datetime(2026, 7, 12, 3, 0, tzinfo=timezone.utc)
REAL_SUBPROCESS_RUN = subprocess.run


def admission_config(tmp: str, **overrides):
    values = {
        "usage_admission_enabled": True,
        "usage_admission_command": ["usage-snapshot", "--json"],
        "usage_admission_timeout_seconds": 2,
        "usage_admission_max_age_seconds": 300,
        "usage_admission_short_window_threshold_percent": 10.0,
        "usage_admission_reset_grace_seconds": 60,
    }
    values.update(overrides)
    return replace(make_config(tmp, "success"), **values)


def command_result(snapshot: object, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    stdout = snapshot if isinstance(snapshot, str) else json.dumps(snapshot)
    return subprocess.CompletedProcess(["usage-snapshot", "--json"], returncode, stdout=stdout, stderr="")


def snapshot_command_side_effect(config, value):
    def run(command, *args, **kwargs):
        if command == config.usage_admission_command:
            return command_result(value)
        return REAL_SUBPROCESS_RUN(command, *args, **kwargs)

    return run


def snapshot(
    *,
    observed_at: datetime,
    reset_at: datetime,
    primary: float,
    secondary: float | None = None,
    secondary_reset_at: datetime | None = None,
) -> dict:
    value = {
        "available": True,
        "observed_at": observed_at.isoformat(),
        "primary": {
            "remaining_percent": primary,
            "resets_at": reset_at.isoformat(),
            "window_minutes": 300,
        },
    }
    if secondary is not None:
        value["secondary"] = {"remaining_percent": secondary, "window_minutes": 10080}
        if secondary_reset_at is not None:
            value["secondary"]["resets_at"] = secondary_reset_at.isoformat()
    return value


class UsageAdmissionTests(unittest.TestCase):
    def test_disabled_gate_does_not_invoke_snapshot_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, "success")
            with patch.object(usage_module.subprocess, "run") as run:
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("allowed", decision.status)
            self.assertEqual("disabled", decision.reason)
            run.assert_not_called()

    def test_fresh_allowed_snapshot_does_not_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            value = snapshot(
                observed_at=NOW - timedelta(seconds=30),
                reset_at=NOW + timedelta(hours=1),
                primary=25,
            )
            with patch.object(usage_module.subprocess, "run", return_value=command_result(value)) as run:
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("allowed", decision.status)
            self.assertEqual("short_window_above_threshold_and_long_window_not_exhausted", decision.reason)
            run.assert_called_once()

    def test_fresh_low_short_window_gates_until_reset_plus_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp, usage_admission_reset_grace_seconds=90)
            reset_at = NOW + timedelta(hours=2)
            value = snapshot(observed_at=NOW - timedelta(seconds=20), reset_at=reset_at, primary=10)
            with patch.object(usage_module.subprocess, "run", return_value=command_result(value)):
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("gated", decision.status)
            self.assertEqual(reset_at + timedelta(seconds=90), decision.cooldown_until)
            self.assertEqual("short_window_remaining_at_or_below_threshold", decision.reason)
            self.assertEqual(("short_window",), decision.gate_windows)

    def test_lone_long_window_does_not_gate_unless_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            value = snapshot(
                observed_at=NOW - timedelta(seconds=20),
                reset_at=NOW + timedelta(days=4),
                primary=5,
            )
            value["primary"]["window_minutes"] = 10080
            with patch.object(usage_module.subprocess, "run", return_value=command_result(value)):
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("allowed", decision.status)
            self.assertEqual("short_window_absent_and_long_window_not_exhausted", decision.reason)
            self.assertIsNone(decision.short_window_remaining_percent)
            self.assertEqual(5.0, decision.long_window_remaining_percent)

    def test_lone_exhausted_long_window_gates_until_long_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp, usage_admission_reset_grace_seconds=90)
            reset_at = NOW + timedelta(days=4)
            value = snapshot(
                observed_at=NOW - timedelta(seconds=20),
                reset_at=reset_at,
                primary=0,
            )
            value["primary"]["window_minutes"] = 10080
            with patch.object(usage_module.subprocess, "run", return_value=command_result(value)):
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("gated", decision.status)
            self.assertEqual("long_window_exhausted", decision.reason)
            self.assertEqual(("long_window",), decision.gate_windows)
            self.assertEqual(reset_at, decision.reset_at)
            self.assertEqual(reset_at + timedelta(seconds=90), decision.cooldown_until)

    def test_unknown_lone_window_fails_open_instead_of_guessing_short(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            value = snapshot(
                observed_at=NOW - timedelta(seconds=20),
                reset_at=NOW + timedelta(days=1),
                primary=0,
            )
            value["primary"]["window_minutes"] = 1440
            stderr = io.StringIO()
            with (
                patch.object(usage_module.subprocess, "run", return_value=command_result(value)),
                redirect_stderr(stderr),
            ):
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("fail_open", decision.status)
            self.assertEqual("usage_windows_ambiguous", decision.reason)
            self.assertIn("usage_windows_ambiguous", stderr.getvalue())

    def test_low_long_window_does_not_gate_unless_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            primary_reset_at = NOW + timedelta(hours=2)
            secondary_reset_at = NOW + timedelta(days=4)
            value = snapshot(
                observed_at=NOW - timedelta(seconds=20),
                reset_at=primary_reset_at,
                primary=60,
                secondary=5,
                secondary_reset_at=secondary_reset_at,
            )
            with patch.object(usage_module.subprocess, "run", return_value=command_result(value)):
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("allowed", decision.status)

    def test_restored_windows_are_classified_by_duration_when_slots_are_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            short_reset_at = NOW + timedelta(hours=2)
            long_reset_at = NOW + timedelta(days=4)
            value = {
                "available": True,
                "observed_at": (NOW - timedelta(seconds=20)).isoformat(),
                "primary": {
                    "remaining_percent": 5,
                    "resets_at": long_reset_at.isoformat(),
                    "window_minutes": 10080,
                },
                "secondary": {
                    "remaining_percent": 10,
                    "resets_at": short_reset_at.isoformat(),
                    "window_minutes": 300,
                },
            }
            with patch.object(usage_module.subprocess, "run", return_value=command_result(value)):
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("gated", decision.status)
            self.assertEqual(("short_window",), decision.gate_windows)
            self.assertEqual(short_reset_at, decision.reset_at)
            self.assertEqual(10.0, decision.short_window_remaining_percent)
            self.assertEqual(5.0, decision.long_window_remaining_percent)

    def test_both_low_gates_only_until_short_window_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            primary_reset_at = NOW + timedelta(hours=2)
            secondary_reset_at = NOW + timedelta(days=4)
            value = snapshot(
                observed_at=NOW - timedelta(seconds=20),
                reset_at=primary_reset_at,
                primary=10,
                secondary=5,
                secondary_reset_at=secondary_reset_at,
            )
            with patch.object(usage_module.subprocess, "run", return_value=command_result(value)):
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("gated", decision.status)
            self.assertEqual("short_window_remaining_at_or_below_threshold", decision.reason)
            self.assertEqual(("short_window",), decision.gate_windows)
            self.assertEqual(primary_reset_at, decision.reset_at)
            self.assertEqual(primary_reset_at + timedelta(seconds=60), decision.cooldown_until)
            event = list_events(config)[-1]
            self.assertEqual("usage_admission_gated", event["event_type"])
            self.assertEqual(
                {
                    "status": "gated",
                    "reason": "short_window_remaining_at_or_below_threshold",
                    "observed_at": (NOW - timedelta(seconds=20)).isoformat(),
                    "reset_at": primary_reset_at.isoformat(),
                    "gate_windows": ["short_window"],
                    "cooldown_until": (primary_reset_at + timedelta(seconds=60)).isoformat(),
                    "short_window_remaining_percent": 10.0,
                    "long_window_remaining_percent": 5.0,
                },
                event["payload"],
            )

    def test_exhausted_long_window_requires_its_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            value = snapshot(
                observed_at=NOW - timedelta(seconds=20),
                reset_at=NOW + timedelta(hours=2),
                primary=60,
                secondary=0,
            )
            stderr = io.StringIO()
            with (
                patch.object(usage_module.subprocess, "run", return_value=command_result(value)),
                redirect_stderr(stderr),
            ):
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("fail_open", decision.status)
            self.assertEqual("long_window_reset_time_invalid", decision.reason)
            self.assertNotIn("usage-snapshot", stderr.getvalue())
            event = list_events(config)[-1]
            self.assertEqual("usage_admission_warning", event["event_type"])
            self.assertEqual("long_window_reset_time_invalid", event["payload"]["reason"])
            self.assertNotIn("usage-snapshot", json.dumps(event))

    def test_stale_low_snapshot_after_reset_allows_bounded_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            reset_at = NOW - timedelta(minutes=2)
            value = snapshot(
                observed_at=reset_at - timedelta(minutes=5),
                reset_at=reset_at,
                primary=0,
            )
            with patch.object(usage_module.subprocess, "run", return_value=command_result(value)):
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("allowed", decision.status)
            self.assertEqual("short_window_stale_after_reset_bounded_attempt", decision.reason)

    def test_stale_exhausted_long_window_after_reset_allows_bounded_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            primary_reset_at = NOW - timedelta(days=5)
            secondary_reset_at = NOW - timedelta(minutes=2)
            value = snapshot(
                observed_at=secondary_reset_at - timedelta(minutes=5),
                reset_at=primary_reset_at,
                primary=60,
                secondary=0,
                secondary_reset_at=secondary_reset_at,
            )
            with patch.object(usage_module.subprocess, "run", return_value=command_result(value)):
                decision = check_usage_admission(config, now=NOW)

            self.assertEqual("allowed", decision.status)
            self.assertEqual("long_window_stale_after_reset_bounded_attempt", decision.reason)
            self.assertEqual(("long_window",), decision.gate_windows)
            self.assertEqual(secondary_reset_at, decision.reset_at)

    def test_invalid_json_and_timeout_fail_open_with_sanitized_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            stderr = io.StringIO()
            with (
                patch.object(usage_module.subprocess, "run", return_value=command_result("not-json")),
                redirect_stderr(stderr),
            ):
                invalid = check_usage_admission(config, now=NOW)
            with (
                patch.object(usage_module.subprocess, "run", side_effect=subprocess.TimeoutExpired(["hidden"], 2)),
                redirect_stderr(stderr),
            ):
                timed_out = check_usage_admission(config, now=NOW)

            self.assertEqual("fail_open", invalid.status)
            self.assertEqual("fail_open", timed_out.status)
            self.assertNotIn("hidden", stderr.getvalue())
            warning_events = [
                event for event in list_events(config) if event.get("event_type") == "usage_admission_warning"
            ]
            self.assertEqual(2, len(warning_events))
            self.assertNotIn("usage-snapshot", json.dumps(warning_events))

    def test_unavailable_snapshot_and_command_failure_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            stderr = io.StringIO()
            with (
                patch.object(
                    usage_module.subprocess,
                    "run",
                    return_value=command_result({"available": False}),
                ),
                redirect_stderr(stderr),
            ):
                unavailable = check_usage_admission(config, now=NOW)
            with (
                patch.object(
                    usage_module.subprocess,
                    "run",
                    return_value=command_result({}, returncode=2),
                ),
                redirect_stderr(stderr),
            ):
                failed = check_usage_admission(config, now=NOW)

            self.assertEqual("fail_open", unavailable.status)
            self.assertEqual("snapshot_unavailable", unavailable.reason)
            self.assertEqual("fail_open", failed.status)
            self.assertEqual("snapshot_command_failed", failed.reason)

    def test_runner_sets_global_cooldown_schedules_wake_and_reads_snapshot_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            create_task(config, "work", tmp, task_id="task-1")
            now = datetime.now(timezone.utc)
            reset_at = now + timedelta(hours=1)
            value = snapshot(observed_at=now, reset_at=reset_at, primary=1)

            with (
                patch.object(
                    usage_module.subprocess,
                    "run",
                    side_effect=snapshot_command_side_effect(config, value),
                ) as snapshot_run,
                patch.object(runner_module, "schedule_manual_cooldown_wake") as wake,
                patch.object(runner_module, "run_codex", side_effect=AssertionError("unexpected Codex call")),
            ):
                outcome = run_next(config)

            self.assertEqual("cooldown", outcome.status)
            self.assertEqual(0, load_task(config, "task-1")["attempts"])
            self.assertIsNotNone(load_state(config)["global_cooldown_until"])
            snapshot_calls = [call for call in snapshot_run.call_args_list if call.args[0] == config.usage_admission_command]
            self.assertEqual(1, len(snapshot_calls))
            wake.assert_called_once()

    def test_runner_fail_open_and_stale_after_reset_each_make_normal_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = admission_config(tmp)
            create_task(config, "first", tmp, task_id="task-1")
            with (
                patch.object(
                    usage_module.subprocess,
                    "run",
                    side_effect=snapshot_command_side_effect(config, "invalid"),
                ) as first_read,
                redirect_stderr(io.StringIO()),
            ):
                first = run_next(config)

            create_task(config, "second", tmp, task_id="task-2")
            now = datetime.now(timezone.utc)
            reset_at = now - timedelta(minutes=1)
            value = snapshot(
                observed_at=reset_at - timedelta(minutes=5),
                reset_at=reset_at,
                primary=0,
            )
            with patch.object(
                usage_module.subprocess,
                "run",
                side_effect=snapshot_command_side_effect(config, value),
            ) as second_read:
                second = run_next(config)

            self.assertEqual("completed", first.status)
            self.assertEqual("completed", second.status)
            first_snapshot_calls = [call for call in first_read.call_args_list if call.args[0] == config.usage_admission_command]
            second_snapshot_calls = [call for call in second_read.call_args_list if call.args[0] == config.usage_admission_command]
            self.assertEqual(1, len(first_snapshot_calls))
            self.assertEqual(1, len(second_snapshot_calls))

    def test_stale_after_reset_allows_only_one_concurrent_bounded_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                admission_config(tmp),
                max_total_running=2,
                max_running_per_project=2,
                capacity_pools={"codex": {"max_running": 2}},
            )
            create_task(config, "first", tmp, task_id="task-1")
            create_task(config, "second", tmp, task_id="task-2")
            now = datetime.now(timezone.utc)
            reset_at = now - timedelta(minutes=1)
            value = snapshot(
                observed_at=reset_at - timedelta(minutes=5),
                reset_at=reset_at,
                primary=0,
            )
            nested = []
            codex_calls = []

            def fake_codex(config, task, prompt, attempt):
                codex_calls.append(task["id"])
                if task["id"] == "task-1":
                    nested.append(run_next(config))
                return CodexResult(
                    returncode=0,
                    log_path=Path(tmp) / f"{task['id']}.jsonl",
                    command_kind="exec",
                    resume_id_used=None,
                    stderr="",
                    events=[],
                    final_response={
                        "task_id": task["id"],
                        "status": "completed",
                        "summary": "done",
                        "changed_files": [],
                        "verification": [],
                    },
                    session_id=None,
                    thread_id=None,
                    rate_limited=False,
                    rate_limit_markers=[],
                )

            with (
                patch.object(
                    usage_module.subprocess,
                    "run",
                    side_effect=snapshot_command_side_effect(config, value),
                ),
                patch.object(runner_module, "run_codex", side_effect=fake_codex),
            ):
                outcome = run_next(config)

            self.assertEqual("completed", outcome.status)
            self.assertEqual(["task-1"], codex_calls)
            self.assertEqual(1, len(nested))
            self.assertEqual("locked", nested[0].status)
            self.assertEqual("runnable", load_task(config, "task-2")["status"])

    def test_stale_after_reset_provider_rejection_uses_existing_rate_limit_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rate_limit_config = make_config(tmp, "rate_limit")
            config = replace(
                admission_config(tmp),
                codex_command=rate_limit_config.codex_command,
                codex_resume_command=rate_limit_config.codex_resume_command,
            )
            create_task(config, "work", tmp, task_id="task-1")
            now = datetime.now(timezone.utc)
            reset_at = now - timedelta(minutes=1)
            value = snapshot(
                observed_at=reset_at - timedelta(minutes=5),
                reset_at=reset_at,
                primary=0,
            )

            with patch.object(
                usage_module.subprocess,
                "run",
                side_effect=snapshot_command_side_effect(config, value),
            ):
                outcome = run_next(config)

            task = load_task(config, "task-1")
            state = load_state(config)
            self.assertEqual("needs_resume", outcome.status)
            self.assertEqual(1, task["rate_limit_count"])
            self.assertEqual(task["cooldown_until"], state["global_cooldown_until"])


if __name__ == "__main__":
    unittest.main()

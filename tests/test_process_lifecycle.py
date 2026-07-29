from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.config import Config
from codex_batch_runner.codex import (
    finalize_codex_process_lifecycle,
    run_codex,
)
from codex_batch_runner.external_json_command import (
    run_external_json_command_task,
)
from codex_batch_runner.execution_report import task_execution_row
from codex_batch_runner.process_lifecycle import (
    ProcessLifecycleError,
    ProcessLifecyclePolicy,
    normal_exit_lifecycle,
    terminate_process_group,
    validate_process_lifecycle,
)


class FakeProcess:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.timeout:
            raise subprocess.TimeoutExpired(["fake"], timeout)
        return 0


class UnreapedProcess:
    returncode = None

    def __init__(self) -> None:
        self.poll_calls = 0
        self.wait_calls = 0

    def poll(self) -> None:
        self.poll_calls += 1
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        raise subprocess.TimeoutExpired(["fake"], timeout)


class BlockingStream:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.close_called = False

    def __iter__(self) -> BlockingStream:
        return self

    def __next__(self) -> str:
        self.release.wait(timeout=30)
        raise StopIteration

    def close(self) -> None:
        self.close_called = True
        raise AssertionError("live reader stream must not be closed")


class PersistentProcess:
    pid = 106
    returncode = None

    def __init__(self) -> None:
        self.stdout = BlockingStream()
        self.stderr = BlockingStream()
        self.kill_calls = 0

    def poll(self) -> None:
        return None

    def kill(self) -> None:
        self.kill_calls += 1
        raise OSError("synthetic direct kill failure")


class ProcessLifecycleTests(unittest.TestCase):
    def test_normal_exit_is_sanitized_and_direct_child_reaped(self) -> None:
        self.assertEqual(
            {
                "schema_version": 1,
                "policy": "posix_process_group_v1",
                "scope": "same_process_group",
                "trigger": "none",
                "term": "not_sent",
                "kill": "not_sent",
                "direct_child_reaped": True,
                "group_observation": "not_applicable",
                "outcome": "normal_exit",
            },
            normal_exit_lifecycle(),
        )

    def test_term_observed_absent_does_not_send_kill(self) -> None:
        process = FakeProcess()
        present = True
        signals: list[int] = []

        def killpg(_group: int, sent_signal: int) -> None:
            nonlocal present
            signals.append(sent_signal)
            if sent_signal == signal.SIGTERM:
                present = False
            elif sent_signal == 0 and not present:
                raise ProcessLookupError

        result = terminate_process_group(
            process,  # type: ignore[arg-type]
            process_group_id=101,
            trigger="startup_stall",
            grace_seconds=1,
            killpg=killpg,
        )

        self.assertEqual("sent", result["term"])
        self.assertEqual("not_needed", result["kill"])
        self.assertEqual("observed_absent", result["group_observation"])
        self.assertEqual("terminated_during_grace", result["outcome"])
        self.assertNotIn(signal.SIGKILL, signals)
        self.assertTrue(process.wait_calls)

    def test_same_group_ignores_term_and_requires_kill(self) -> None:
        process = FakeProcess()
        present = True
        signals: list[int] = []

        def killpg(_group: int, sent_signal: int) -> None:
            nonlocal present
            signals.append(sent_signal)
            if sent_signal == signal.SIGKILL:
                present = False
            elif sent_signal == 0 and not present:
                raise ProcessLookupError

        ticks = iter((0.0, 0.0, 1.0, 1.0, 1.0, 1.0))
        result = terminate_process_group(
            process,  # type: ignore[arg-type]
            process_group_id=102,
            trigger="total_runtime_timeout",
            grace_seconds=1,
            killpg=killpg,
            monotonic=lambda: next(ticks, 2.0),
            sleep=lambda _seconds: None,
        )

        self.assertEqual([signal.SIGTERM, 0, signal.SIGKILL, 0], signals)
        self.assertEqual("sent", result["kill"])
        self.assertEqual("observed_absent", result["group_observation"])
        self.assertEqual("killed_after_grace", result["outcome"])

    def test_probe_failure_is_bounded_unverified_evidence(self) -> None:
        process = FakeProcess()

        def killpg(_group: int, sent_signal: int) -> None:
            if sent_signal == 0:
                raise OSError("synthetic probe failure")

        result = terminate_process_group(
            process,  # type: ignore[arg-type]
            process_group_id=103,
            trigger="external_wall_timeout",
            grace_seconds=1,
            killpg=killpg,
        )

        self.assertTrue(result["direct_child_reaped"])
        self.assertEqual("probe_failed", result["group_observation"])
        self.assertEqual(
            "direct_child_reaped_group_unverified",
            result["outcome"],
        )

    def test_signal_or_direct_reap_failure_never_claims_success(self) -> None:
        process = FakeProcess(timeout=True)

        def killpg(_group: int, _sent_signal: int) -> None:
            raise OSError("synthetic signal failure")

        result = terminate_process_group(
            process,  # type: ignore[arg-type]
            process_group_id=104,
            trigger="mid_run_idle_timeout",
            grace_seconds=1,
            killpg=killpg,
        )

        self.assertEqual("failed", result["term"])
        self.assertFalse(result["direct_child_reaped"])
        self.assertEqual("termination_failed", result["outcome"])

    def test_evidence_is_enum_only_and_rejects_process_identifiers(self) -> None:
        evidence = normal_exit_lifecycle()
        evidence["process_group_id"] = 100
        with self.assertRaisesRegex(ProcessLifecycleError, "canonical"):
            validate_process_lifecycle(evidence)

    def test_execution_report_projects_only_valid_lifecycle_enums(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = {
                "id": "report-task",
                "status": "failed",
                "last_run": {
                    "execution_backend": "external-json-command",
                    "process_lifecycle": normal_exit_lifecycle(),
                },
            }
            row = task_execution_row(config, task)
            self.assertEqual(
                normal_exit_lifecycle(),
                row["execution"]["process_lifecycle"],
            )

            task["last_run"]["process_lifecycle"]["process_group_id"] = 100
            row = task_execution_row(config, task)
            self.assertNotIn("process_lifecycle", row["execution"])

    def test_native_unreaped_failure_returns_canonical_evidence_without_rewait(
        self,
    ) -> None:
        process = UnreapedProcess()

        def killpg(_group: int, _sent_signal: int) -> None:
            raise PermissionError("synthetic signal failure")

        lifecycle = terminate_process_group(
            process,  # type: ignore[arg-type]
            process_group_id=105,
            trigger="startup_stall",
            grace_seconds=1,
            killpg=killpg,
        )

        returncode, reconciled = finalize_codex_process_lifecycle(
            process,  # type: ignore[arg-type]
            lifecycle,
        )

        self.assertEqual(1, returncode)
        self.assertEqual(2, process.poll_calls)
        self.assertEqual(1, process.wait_calls)
        self.assertFalse(reconciled["direct_child_reaped"])
        self.assertEqual("termination_failed", reconciled["outcome"])

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_native_group_signal_failure_uses_bounded_direct_child_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                Config.load(root=Path(tmp)),
                codex_command=[
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                ],
                codex_startup_stall_seconds=1,
                codex_first_meaningful_timeout_seconds=0,
                codex_mid_run_idle_seconds=0,
                codex_total_runtime_timeout_seconds=0,
            )
            failure = {
                "schema_version": 1,
                "policy": "posix_process_group_v1",
                "scope": "same_process_group",
                "trigger": "startup_stall",
                "term": "failed",
                "kill": "failed",
                "direct_child_reaped": False,
                "group_observation": "unverified",
                "outcome": "termination_failed",
            }
            started = time.monotonic()
            with (
                patch(
                    "codex_batch_runner.codex."
                    "resolve_process_lifecycle_policy",
                    return_value=ProcessLifecyclePolicy(
                        name="posix_process_group_v1",
                        termination_grace_seconds=1,
                    ),
                ),
                patch(
                    "codex_batch_runner.codex.terminate_process_group",
                    return_value=failure,
                ),
            ):
                result = run_codex(
                    config,
                    {"id": "native-bounded-failure", "cwd": tmp},
                    "synthetic prompt",
                    1,
                )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 5)
            self.assertIsNotNone(result.process_lifecycle)
            assert result.process_lifecycle is not None
            self.assertTrue(
                result.process_lifecycle["direct_child_reaped"]
            )
            self.assertEqual(
                "termination_failed",
                result.process_lifecycle["outcome"],
            )
            self.assertEqual("failed", result.process_lifecycle["term"])
            self.assertEqual("failed", result.process_lifecycle["kill"])
            self.assertNotEqual(1, result.returncode)

    def test_native_direct_kill_failure_does_not_close_live_reader_streams(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(
                Config.load(root=Path(tmp)),
                codex_command=["synthetic-codex"],
                codex_startup_stall_seconds=1,
                codex_first_meaningful_timeout_seconds=0,
                codex_mid_run_idle_seconds=0,
                codex_total_runtime_timeout_seconds=0,
            )
            process = PersistentProcess()
            failure = {
                "schema_version": 1,
                "policy": "posix_process_group_v1",
                "scope": "same_process_group",
                "trigger": "startup_stall",
                "term": "failed",
                "kill": "failed",
                "direct_child_reaped": False,
                "group_observation": "unverified",
                "outcome": "termination_failed",
            }
            started = time.monotonic()
            try:
                with (
                    patch(
                        "codex_batch_runner.codex."
                        "resolve_process_lifecycle_policy",
                        return_value=ProcessLifecyclePolicy(
                            name="posix_process_group_v1",
                            termination_grace_seconds=1,
                        ),
                    ),
                    patch(
                        "codex_batch_runner.codex.terminate_process_group",
                        return_value=failure,
                    ),
                    patch(
                        "codex_batch_runner.codex.subprocess.Popen",
                        return_value=process,
                    ),
                ):
                    result = run_codex(
                        config,
                        {"id": "native-persistent-failure", "cwd": tmp},
                        "synthetic prompt",
                        1,
                    )
            finally:
                process.stdout.release.set()
                process.stderr.release.set()
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 5)
            self.assertEqual(1, result.returncode)
            self.assertEqual(1, process.kill_calls)
            self.assertFalse(process.stdout.close_called)
            self.assertFalse(process.stderr.close_called)
            self.assertIsNotNone(result.process_lifecycle)
            assert result.process_lifecycle is not None
            self.assertFalse(
                result.process_lifecycle["direct_child_reaped"]
            )
            self.assertEqual(
                "termination_failed",
                result.process_lifecycle["outcome"],
            )

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_external_timeout_preserves_legacy_failure_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = {
                "id": "external-timeout",
                "cwd": tmp,
                "external_command": [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                ],
                "external_timeout_seconds": 1,
            }
            with patch(
                "codex_batch_runner.external_json_command."
                "resolve_process_lifecycle_policy",
                return_value=ProcessLifecyclePolicy(
                    name="posix_process_group_v1",
                    termination_grace_seconds=1,
                ),
            ):
                result = run_external_json_command_task(
                    config,
                    task,
                    "synthetic prompt",
                    1,
                )

            self.assertTrue(result.timed_out)
            self.assertIsNone(result.returncode)
            self.assertEqual(
                "external-json-command timed out after 1s",
                result.error,
            )
            self.assertIsNotNone(result.process_lifecycle)
            assert result.process_lifecycle is not None
            self.assertEqual(
                "external_wall_timeout",
                result.process_lifecycle["trigger"],
            )
            self.assertTrue(
                result.process_lifecycle["direct_child_reaped"]
            )

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_external_term_handler_output_is_drained_during_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "external-ready.txt"
            output_size = 1024 * 1024
            handler_script = (
                "import signal,sys,time; "
                "signal.signal(signal.SIGTERM, lambda *_: "
                "(sys.stdout.buffer.write(b'x' * int(sys.argv[2])), "
                "sys.stdout.buffer.flush(), sys.exit(0))); "
                "open(sys.argv[1], 'w').write('ready'); "
                "time.sleep(30)"
            )
            config = Config.load(root=Path(tmp))
            task = {
                "id": "external-drain",
                "cwd": tmp,
                "external_command": [
                    sys.executable,
                    "-c",
                    handler_script,
                    str(ready),
                    str(output_size),
                ],
                "external_timeout_seconds": 1,
            }
            with patch(
                "codex_batch_runner.external_json_command."
                "resolve_process_lifecycle_policy",
                return_value=ProcessLifecyclePolicy(
                    name="posix_process_group_v1",
                    termination_grace_seconds=2,
                ),
            ):
                result = run_external_json_command_task(
                    config,
                    task,
                    "synthetic prompt",
                    1,
                )

            self.assertTrue(ready.exists())
            self.assertTrue(result.timed_out)
            self.assertIsNone(result.returncode)
            self.assertEqual(output_size, result.stdout_bytes)
            self.assertIsNotNone(result.process_lifecycle)
            assert result.process_lifecycle is not None
            self.assertEqual("not_needed", result.process_lifecycle["kill"])
            self.assertEqual(
                "terminated_during_grace",
                result.process_lifecycle["outcome"],
            )

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_wrapper_need_not_forward_signal_to_same_group_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "worker-term.txt"
            worker = (
                "import signal,time,sys; "
                "signal.signal(signal.SIGTERM, lambda *_: "
                "(open(sys.argv[1], 'w').write('term'), sys.exit(0))); "
                "time.sleep(30)"
            )
            wrapper = (
                "import signal,subprocess,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"subprocess.Popen([sys.executable, '-c', {worker!r}, sys.argv[1]]); "
                "time.sleep(30)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", wrapper, str(marker)],
                start_new_session=True,
            )
            try:
                time.sleep(0.2)
                evidence = terminate_process_group(
                    process,
                    process_group_id=process.pid,
                    trigger="external_wall_timeout",
                    grace_seconds=1,
                )
                self.assertTrue(marker.exists())
                self.assertTrue(evidence["direct_child_reaped"])
                self.assertIn(
                    evidence["outcome"],
                    {
                        "killed_after_grace",
                        "direct_child_reaped_group_unverified",
                    },
                )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_term_handling_direct_child_is_reaped_before_group_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready.txt"
            child = (
                "import signal,sys,time; "
                "open(sys.argv[1], 'w').write('ready'); "
                "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
                "time.sleep(30)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", child, str(ready)],
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not ready.exists():
                    time.sleep(0.02)
                self.assertTrue(ready.exists())

                evidence = terminate_process_group(
                    process,
                    process_group_id=process.pid,
                    trigger="startup_stall",
                    grace_seconds=1,
                )

                self.assertEqual("sent", evidence["term"])
                self.assertEqual("not_needed", evidence["kill"])
                self.assertTrue(evidence["direct_child_reaped"])
                self.assertEqual(
                    "observed_absent",
                    evidence["group_observation"],
                )
                self.assertEqual(
                    "terminated_during_grace",
                    evidence["outcome"],
                )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_reaped_leader_does_not_hide_same_group_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            child_ready = Path(tmp) / "child-ready.txt"
            child_pid_path = Path(tmp) / "child.pid"
            child = (
                "import os,signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "open(sys.argv[1], 'w').write(str(os.getpid())); "
                "open(sys.argv[2], 'w').write('ready'); "
                "time.sleep(30)"
            )
            parent = (
                "import signal,subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}, "
                "sys.argv[1], sys.argv[2]]); "
                "deadline=time.monotonic()+2; "
                "\nwhile time.monotonic() < deadline and "
                "not __import__('os').path.exists(sys.argv[2]): time.sleep(.02); "
                "\nsignal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
                "time.sleep(30)"
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    parent,
                    str(child_pid_path),
                    str(child_ready),
                ],
                start_new_session=True,
            )
            child_pid: int | None = None
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not child_ready.exists():
                    time.sleep(0.02)
                self.assertTrue(child_ready.exists())
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))

                evidence = terminate_process_group(
                    process,
                    process_group_id=process.pid,
                    trigger="external_wall_timeout",
                    grace_seconds=1,
                )

                self.assertTrue(evidence["direct_child_reaped"])
                self.assertEqual("sent", evidence["kill"])
                self.assertIn(
                    evidence["outcome"],
                    {
                        "killed_after_grace",
                        "direct_child_reaped_group_unverified",
                    },
                )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_new_session_descendant_is_outside_same_group_guarantee(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "escaped.pid"
            escaped = (
                "import os,sys,time; "
                "open(sys.argv[1], 'w').write(str(os.getpid())); "
                "time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {escaped!r}, sys.argv[1]], "
                "start_new_session=True); time.sleep(30)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", parent, str(pid_path)],
                start_new_session=True,
            )
            escaped_pid: int | None = None
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not pid_path.exists():
                    time.sleep(0.02)
                escaped_pid = int(pid_path.read_text(encoding="utf-8"))
                evidence = terminate_process_group(
                    process,
                    process_group_id=process.pid,
                    trigger="startup_stall",
                    grace_seconds=1,
                )
                os.kill(escaped_pid, 0)
                self.assertEqual("same_process_group", evidence["scope"])
                self.assertNotIn("descendant", evidence)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                if escaped_pid is not None:
                    try:
                        os.kill(escaped_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()

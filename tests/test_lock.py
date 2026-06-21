from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.lock import FileLock
from codex_batch_runner.timeutil import utc_now


class LockTests(unittest.TestCase):
    def test_active_lock_blocks_second_acquire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.lock"
            first = FileLock(path, stale_seconds=3600)
            second = FileLock(path, stale_seconds=3600)

            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_stale_lock_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.lock"
            old = utc_now() - timedelta(hours=7)
            path.write_text(json.dumps({"created_at": old.isoformat(), "pid": 1}), encoding="utf-8")

            lock = FileLock(path, stale_seconds=3600)

            self.assertTrue(lock.acquire())
            lock.release()

    def test_dead_same_host_pid_lock_is_recovered_without_age_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.lock"
            path.write_text(
                json.dumps(
                    {
                        "created_at": utc_now().isoformat(),
                        "hostname": "test-host",
                        "pid": 424242,
                    }
                ),
                encoding="utf-8",
            )
            lock = FileLock(path, stale_seconds=3600)

            with patch("codex_batch_runner.lock.socket.gethostname", return_value="test-host"), patch(
                "codex_batch_runner.lock.pid_exists", return_value=False
            ):
                self.assertTrue(lock.acquire())
            lock.release()

    def test_live_same_host_pid_lock_blocks_until_age_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.lock"
            path.write_text(
                json.dumps(
                    {
                        "created_at": utc_now().isoformat(),
                        "hostname": "test-host",
                        "pid": 424242,
                    }
                ),
                encoding="utf-8",
            )
            lock = FileLock(path, stale_seconds=3600)

            with patch("codex_batch_runner.lock.socket.gethostname", return_value="test-host"), patch(
                "codex_batch_runner.lock.pid_exists", return_value=True
            ):
                self.assertFalse(lock.acquire())

    def test_cross_host_pid_lock_uses_age_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.lock"
            path.write_text(
                json.dumps(
                    {
                        "created_at": utc_now().isoformat(),
                        "hostname": "other-host",
                        "pid": 424242,
                    }
                ),
                encoding="utf-8",
            )
            lock = FileLock(path, stale_seconds=3600)

            with patch("codex_batch_runner.lock.socket.gethostname", return_value="test-host"), patch(
                "codex_batch_runner.lock.pid_exists", return_value=False
            ) as exists:
                self.assertFalse(lock.acquire())

            exists.assert_not_called()


if __name__ == "__main__":
    unittest.main()

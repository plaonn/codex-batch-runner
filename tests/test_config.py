from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.config import Config, resolve_config_path


def write_config(path: Path, queue_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"queue_dir": str(queue_dir)}), encoding="utf-8")


class ConfigTests(unittest.TestCase):
    def test_explicit_config_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            queue_dir = Path(tmp) / "queue"
            write_config(config_path, queue_dir)

            config = Config.load(str(config_path), root=Path(tmp) / "other")

            self.assertEqual(queue_dir, config.queue_dir)

    def test_event_dir_defaults_next_to_log_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            log_dir = Path(tmp) / "runtime" / "logs"
            config_path.write_text(json.dumps({"log_dir": str(log_dir)}), encoding="utf-8")

            config = Config.load(str(config_path), root=Path(tmp) / "cwd")

            self.assertEqual(log_dir.parent / "events", config.event_dir)

    def test_cbr_config_env_is_used_when_explicit_path_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "env-config.json"
            queue_dir = Path(tmp) / "env-queue"
            write_config(config_path, queue_dir)

            with patch.dict("os.environ", {"CBR_CONFIG": str(config_path)}, clear=False):
                config = Config.load(root=Path(tmp) / "cwd")

            self.assertEqual(queue_dir, config.queue_dir)

    def test_user_config_is_used_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xdg_config_home = Path(tmp) / "xdg-config"
            config_path = xdg_config_home / "codex-batch-runner" / "config.json"
            queue_dir = Path(tmp) / "user-queue"
            write_config(config_path, queue_dir)

            with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(xdg_config_home)}, clear=True):
                config = Config.load()

            self.assertEqual(queue_dir.resolve(), config.queue_dir.resolve())

    def test_missing_config_falls_back_to_cwd_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "cwd"

            with patch.dict("os.environ", {"HOME": str(Path(tmp) / "home")}, clear=True):
                config = Config.load(root=cwd)

            self.assertEqual(cwd.resolve() / ".codex-batch-runner" / "tasks", config.queue_dir)
            self.assertEqual(cwd.resolve() / ".codex-batch-runner" / "events", config.event_dir)
            self.assertIsNone(resolve_config_path(include_user_config=False))


if __name__ == "__main__":
    unittest.main()

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
            self.assertEqual("disabled", config.worktree_mode)
            self.assertEqual(cwd.resolve() / ".codex-batch-runner" / "worktrees", config.worktree_root)
            self.assertFalse(config.dependency_requires_accepted_review)
            self.assertIsNone(resolve_config_path(include_user_config=False))
            self.assertEqual("disabled", config.manual_cooldown_wake_scheduler)
            self.assertEqual([], config.manual_cooldown_wake_command)

    def test_dependency_requires_accepted_review_can_be_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"dependency_requires_accepted_review": True}), encoding="utf-8")

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertTrue(config.dependency_requires_accepted_review)
            self.assertFalse(config.auto_review_mechanical_accept)
            self.assertFalse(config.auto_review_codex_enabled)

    def test_auto_review_options_can_be_enabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"auto_review_mechanical_accept": True, "auto_review_codex_enabled": True}),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertTrue(config.auto_review_mechanical_accept)
            self.assertTrue(config.auto_review_codex_enabled)

    def test_worktree_placeholders_can_be_enabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"worktree_mode": "task", "worktree_root": "runtime/worktrees"}),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertEqual("task", config.worktree_mode)
            self.assertEqual(Path(tmp).resolve() / "runtime" / "worktrees", config.worktree_root)

    def test_codex_watchdog_config_defaults_and_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            default_config = Config.load(root=Path(tmp) / "default")
            self.assertEqual(240, default_config.codex_startup_stall_seconds)
            self.assertEqual(420, default_config.codex_first_meaningful_timeout_seconds)
            self.assertEqual(1800, default_config.codex_mid_run_idle_seconds)
            self.assertFalse(default_config.codex_mid_run_idle_kill_enabled)
            self.assertIsNone(default_config.codex_total_runtime_timeout_seconds)

            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "codex_startup_stall_seconds": 180,
                        "codex_first_meaningful_timeout_seconds": 300,
                        "codex_mid_run_idle_seconds": 900,
                        "codex_mid_run_idle_kill_enabled": True,
                        "codex_total_runtime_timeout_seconds": 7200,
                        "codex_watchdog_grace_seconds": 2,
                        "codex_startup_stall_cooldown_seconds": 120,
                    }
                ),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertEqual(180, config.codex_startup_stall_seconds)
            self.assertEqual(300, config.codex_first_meaningful_timeout_seconds)
            self.assertEqual(900, config.codex_mid_run_idle_seconds)
            self.assertTrue(config.codex_mid_run_idle_kill_enabled)
            self.assertEqual(7200, config.codex_total_runtime_timeout_seconds)
            self.assertEqual(2, config.codex_watchdog_grace_seconds)
            self.assertEqual(120, config.codex_startup_stall_cooldown_seconds)

    def test_dependency_requires_accepted_review_must_be_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"dependency_requires_accepted_review": "true"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "dependency_requires_accepted_review must be a boolean"):
                Config.load(str(config_path), root=Path(tmp))

    def test_auto_review_options_must_be_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"auto_review_mechanical_accept": "true"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "auto_review_mechanical_accept must be a boolean"):
                Config.load(str(config_path), root=Path(tmp))

    def test_worktree_mode_must_be_known_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"worktree_mode": "enabled"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "worktree_mode must be one of: disabled, task"):
                Config.load(str(config_path), root=Path(tmp))

    def test_notifier_cursor_state_paths_are_optional_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"notifier_cursor_state_paths": ["notify-state.json", str(Path(tmp) / "state.json")]}),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertEqual([Path(tmp).resolve() / "notify-state.json", Path(tmp) / "state.json"], config.notifier_cursor_state_paths)

    def test_post_mutation_trigger_command_defaults_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))

            self.assertEqual([], config.post_mutation_trigger_command)

    def test_manual_cooldown_wake_options_can_be_enabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "manual_cooldown_wake_scheduler": "macos_launchd",
                        "manual_cooldown_wake_command": ["launchctl", "start", "com.example.codex-batch-runner"],
                    }
                ),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertEqual("macos_launchd", config.manual_cooldown_wake_scheduler)
            self.assertEqual(["launchctl", "start", "com.example.codex-batch-runner"], config.manual_cooldown_wake_command)

    def test_post_mutation_trigger_command_must_be_argv_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"post_mutation_trigger_command": "echo unsafe"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "post_mutation_trigger_command must be a list of strings"):
                Config.load(str(config_path), root=Path(tmp))

    def test_manual_cooldown_wake_config_must_be_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"manual_cooldown_wake_scheduler": "cron"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "manual_cooldown_wake_scheduler must be one of"):
                Config.load(str(config_path), root=Path(tmp))

            config_path.write_text(json.dumps({"manual_cooldown_wake_command": "codex exec"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "manual_cooldown_wake_command must be a list of strings"):
                Config.load(str(config_path), root=Path(tmp))


if __name__ == "__main__":
    unittest.main()

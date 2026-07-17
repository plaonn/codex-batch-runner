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
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict("os.environ", {"CBR_CONFIG": ""}, clear=False)
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

    def test_explicit_config_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            queue_dir = Path(tmp) / "queue"
            write_config(config_path, queue_dir)
            env_path = Path(tmp) / "env.json"
            write_config(env_path, Path(tmp) / "env-queue")

            with patch.dict("os.environ", {"CBR_CONFIG": str(env_path)}, clear=False):
                config = Config.load(str(config_path), root=Path(tmp) / "other")

            self.assertEqual(queue_dir, config.queue_dir)
            self.assertEqual(config_path.resolve(), config.config_path)
            self.assertEqual("cli", config.config_source)

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
            self.assertEqual(config_path.resolve(), config.config_path)
            self.assertEqual("environment", config.config_source)

    def test_xdg_config_home_is_used_after_empty_environment_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xdg_home = Path(tmp) / "xdg"
            config_path = xdg_home / "codex-batch-runner" / "config.json"
            queue_dir = Path(tmp) / "xdg-queue"
            write_config(config_path, queue_dir)

            with patch.dict(
                "os.environ",
                {"CBR_CONFIG": "", "XDG_CONFIG_HOME": str(xdg_home), "HOME": str(Path(tmp) / "home")},
                clear=True,
            ):
                self.assertEqual(config_path.resolve(), resolve_config_path())
                config = Config.load()

            self.assertEqual(queue_dir, config.queue_dir)
            self.assertEqual(config_path.resolve(), config.config_path)
            self.assertEqual("xdg", config.config_source)

    def test_home_config_is_used_when_xdg_config_home_is_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config_path = home / ".config" / "codex-batch-runner" / "config.json"
            queue_dir = Path(tmp) / "home-queue"
            write_config(config_path, queue_dir)

            with patch.dict("os.environ", {"CBR_CONFIG": "", "HOME": str(home)}, clear=True):
                config = Config.load()

            self.assertEqual(queue_dir, config.queue_dir)
            self.assertEqual(config_path.resolve(), config.config_path)
            self.assertEqual("xdg", config.config_source)

    def test_explicit_and_environment_selection_fail_closed_without_xdg_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xdg_home = Path(tmp) / "xdg"
            xdg_path = xdg_home / "codex-batch-runner" / "config.json"
            write_config(xdg_path, Path(tmp) / "xdg-queue")
            missing = Path(tmp) / "missing.json"

            with self.subTest(source="cli"), patch.dict(
                "os.environ", {"CBR_CONFIG": "", "XDG_CONFIG_HOME": str(xdg_home)}, clear=True
            ), self.assertRaises(ValueError) as raised:
                Config.load(str(missing))
            self.assertEqual(f"config not found (cli): {missing.resolve()}", str(raised.exception))

            with self.subTest(source="environment"), patch.dict(
                "os.environ", {"CBR_CONFIG": str(missing), "XDG_CONFIG_HOME": str(xdg_home)}, clear=True
            ), self.assertRaises(ValueError) as raised:
                Config.load()
            self.assertEqual(f"config not found (environment): {missing.resolve()}", str(raised.exception))

    def test_xdg_missing_and_no_location_errors_are_actionable_without_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            expected = home / ".config" / "codex-batch-runner" / "config.json"
            with patch.dict("os.environ", {"CBR_CONFIG": "", "HOME": str(home)}, clear=True), self.assertRaises(
                ValueError
            ) as raised:
                Config.load()
            self.assertIn(f"config not found (xdg): {expected.resolve()}", str(raised.exception))
            self.assertFalse(home.exists())

            with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(
                ValueError, "no XDG config location available"
            ):
                Config.load()

    def test_selected_config_must_be_regular_readable_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "directory.json"
            directory.mkdir()
            invalid = root / "invalid.json"
            invalid.write_text("{", encoding="utf-8")
            array = root / "array.json"
            array.write_text("[]", encoding="utf-8")
            readable = root / "readable.json"
            readable.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not a regular file"):
                Config.load(str(directory))
            with self.assertRaisesRegex(ValueError, "contains invalid JSON"):
                Config.load(str(invalid))
            with self.assertRaisesRegex(ValueError, "must contain a JSON object"):
                Config.load(str(array))
            with patch(
                "codex_batch_runner.config.Path.open",
                side_effect=PermissionError("denied"),
            ), self.assertRaisesRegex(ValueError, "not readable"):
                Config.load(str(readable))

    def test_config_root_makes_relative_runtime_paths_independent_of_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp) / "runtime-root"
            other_cwd = Path(tmp) / "other-cwd"
            other_cwd.mkdir()
            config_path = Path(tmp) / "config.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(
                    {
                        "root": str(runtime_root),
                        "queue_dir": ".cbr/tasks",
                        "log_dir": ".cbr/logs",
                        "lock_file": ".cbr/runner.lock",
                        "state_file": ".cbr/state.json",
                        "worktree_root": ".cbr/worktrees",
                        "notifier_cursor_state_paths": [".cbr/notifier.json"],
                    }
                ),
                encoding="utf-8",
            )

            with patch("codex_batch_runner.config.Path.cwd", return_value=other_cwd):
                config = Config.load(str(config_path))

            self.assertEqual(runtime_root.resolve(), config.root)
            self.assertEqual(runtime_root.resolve() / ".cbr" / "tasks", config.queue_dir)
            self.assertEqual(runtime_root.resolve() / ".cbr" / "logs", config.log_dir)
            self.assertEqual(runtime_root.resolve() / ".cbr" / "events", config.event_dir)
            self.assertEqual(runtime_root.resolve() / ".cbr" / "runner.lock", config.lock_file)
            self.assertEqual(runtime_root.resolve() / ".cbr" / "state.json", config.state_file)
            self.assertEqual(runtime_root.resolve() / ".cbr" / "worktrees", config.worktree_root)
            self.assertEqual([runtime_root.resolve() / ".cbr" / "notifier.json"], config.notifier_cursor_state_paths)

    def test_relative_config_root_resolves_from_config_file_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config-dir"
            config_path = config_dir / "config.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps({"root": "../runtime", "queue_dir": "tasks"}), encoding="utf-8")

            config = Config.load(str(config_path))

            self.assertEqual((config_dir / "../runtime").resolve(), config.root)
            self.assertEqual((config_dir / "../runtime/tasks").resolve(), config.queue_dir)

    def test_root_config_must_be_path_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"root": 1}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "root must be a path string"):
                Config.load(str(config_path))

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
            self.assertFalse(config.auto_review_codex_enabled)
            self.assertEqual(0, config.auto_review_codex_max_calls_per_run)
            self.assertEqual(0, config.auto_review_codex_max_fix_loops_per_task)
            self.assertEqual(1800, config.auto_review_codex_cooldown_seconds)
            self.assertEqual(120000, config.auto_review_codex_max_bundle_chars)
            self.assertEqual(60000, config.auto_review_codex_max_diff_chars)
            self.assertIsNone(config.config_path)
            self.assertEqual("internal", config.config_source)
            self.assertEqual("disabled", config.manual_cooldown_wake_scheduler)
            self.assertEqual([], config.manual_cooldown_wake_command)
            self.assertFalse(config.usage_admission_enabled)
            self.assertEqual([], config.usage_admission_command)
            self.assertEqual(5, config.usage_admission_timeout_seconds)
            self.assertEqual(300, config.usage_admission_max_age_seconds)
            self.assertIsNone(config.usage_admission_short_window_threshold_percent)
            self.assertEqual(60, config.usage_admission_reset_grace_seconds)
            self.assertEqual(1, config.max_total_running)
            self.assertEqual(1, config.max_running_per_project)
            self.assertEqual({"codex": {"max_running": 1}}, config.capacity_pools)
            self.assertEqual({}, config.project_priorities)
            self.assertEqual(100, config.default_project_priority)
            self.assertEqual(24, config.project_priority_aging_hours)
            self.assertEqual(900, config.external_json_command_timeout_seconds)

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

    def test_reviewer_codex_limit_placeholders_can_be_enabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "auto_review_codex_enabled": True,
                        "auto_review_codex_max_calls_per_run": 1,
                        "auto_review_codex_max_fix_loops_per_task": 1,
                        "auto_review_codex_cooldown_seconds": 900,
                        "auto_review_codex_max_bundle_chars": 50000,
                        "auto_review_codex_max_diff_chars": 20000,
                    }
                ),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertTrue(config.auto_review_codex_enabled)
            self.assertEqual(1, config.auto_review_codex_max_calls_per_run)
            self.assertEqual(1, config.auto_review_codex_max_fix_loops_per_task)
            self.assertEqual(900, config.auto_review_codex_cooldown_seconds)
            self.assertEqual(50000, config.auto_review_codex_max_bundle_chars)
            self.assertEqual(20000, config.auto_review_codex_max_diff_chars)

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

    def test_capacity_config_can_be_enabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "max_total_running": 3,
                        "max_running_per_project": 2,
                        "capacity_pools": {
                            "codex": {"max_running": 2},
                            "codex-spark": {"max_running": 1},
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertEqual(3, config.max_total_running)
            self.assertEqual(2, config.max_running_per_project)
            self.assertEqual(
                {"codex": {"max_running": 2}, "codex-spark": {"max_running": 1}},
                config.capacity_pools,
            )

    def test_capacity_config_must_be_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            invalid_cases = [
                ({"max_total_running": 0}, "max_total_running must be a positive integer"),
                ({"max_running_per_project": 0}, "max_running_per_project must be a positive integer"),
                ({"capacity_pools": []}, "capacity_pools must be an object"),
                ({"capacity_pools": {"spark": {"max_running": 1}}}, "capacity_pools must define codex"),
                ({"capacity_pools": {"codex": {"max_running": 0}}}, "capacity_pools.codex.max_running must be a positive integer"),
            ]
            for data, message in invalid_cases:
                with self.subTest(data=data):
                    config_path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        Config.load(str(config_path), root=Path(tmp))

    def test_worker_target_config_can_route_by_requirement_vector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "capacity_pools": {
                            "codex": {"max_running": 1},
                            "antigravity-claude-gpt": {"max_running": 1},
                        },
                        "worker_targets": {
                            "antigravity_review": {
                                "execution_backend": "external-json-command",
                                "capacity_pool": "antigravity-claude-gpt",
                                "external_command": ["agy-cbr-wrapper", "--model-group", "claude-gpt"],
                                "external_timeout_seconds": 900,
                                "worker_family": "antigravity",
                                "model_group": "claude-gpt",
                                "budget_hint": "review",
                            }
                        },
                        "worker_selection_rules": [
                            {
                                "name": "strict-review",
                                "when": {"review_strictness": ["high"]},
                                "worker_target": "antigravity_review",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertEqual("antigravity-claude-gpt", config.worker_targets["antigravity_review"]["capacity_pool"])
            self.assertEqual("claude-gpt", config.worker_targets["antigravity_review"]["model_group"])
            self.assertEqual("strict-review", config.worker_selection_rules[0]["name"])

    def test_worker_target_config_must_be_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            invalid_cases = [
                ({"worker_targets": []}, "worker_targets must be an object"),
                (
                    {
                        "worker_targets": {
                            "bad": {
                                "execution_backend": "external-json-command",
                                "external_command": ["worker"],
                                "capacity_pool": "missing",
                            }
                        }
                    },
                    "worker_targets.bad.capacity_pool references unknown capacity_pool: missing",
                ),
                (
                    {
                        "worker_targets": {
                            "bad": {
                                "execution_backend": "external-json-command",
                                "external_command": [],
                            }
                        }
                    },
                    "worker_targets.bad.external_command must be a non-empty list of strings",
                ),
                (
                    {
                        "worker_targets": {
                            "ok": {
                                "execution_backend": "external-json-command",
                                "external_command": ["worker"],
                            }
                        },
                        "worker_selection_rules": [{"worker_target": "missing"}],
                    },
                    "worker_selection_rules.rule-0.worker_target references unknown worker_target: missing",
                ),
            ]
            for data, message in invalid_cases:
                with self.subTest(data=data):
                    config_path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        Config.load(str(config_path), root=Path(tmp))

    def test_priority_config_can_be_enabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_priorities": {
                            "alpha": 10,
                            str(project_root): 20,
                        },
                        "default_project_priority": 50,
                        "project_priority_aging_hours": 6,
                    }
                ),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertEqual(10, config.project_priorities["alpha"])
            self.assertEqual(20, config.project_priorities[str(project_root.resolve())])
            self.assertEqual(50, config.default_project_priority)
            self.assertEqual(6, config.project_priority_aging_hours)

    def test_priority_config_must_be_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            invalid_cases = [
                ({"project_priorities": []}, "project_priorities must be an object"),
                ({"project_priorities": {"": 1}}, "project_priorities keys must be non-empty strings"),
                ({"project_priorities": {"alpha": "fast"}}, "project_priorities.alpha must be an integer"),
                ({"default_project_priority": True}, "default_project_priority must be an integer"),
                ({"project_priority_aging_hours": -1}, "project_priority_aging_hours must be a non-negative integer"),
            ]
            for data, message in invalid_cases:
                with self.subTest(data=data):
                    config_path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        Config.load(str(config_path), root=Path(tmp))

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

    def test_external_json_command_timeout_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"external_json_command_timeout_seconds": 42}), encoding="utf-8")

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertEqual(42, config.external_json_command_timeout_seconds)

    def test_model_requirements_default_to_disabled_and_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            default_config = Config.load(root=Path(tmp) / "default")
            self.assertEqual({}, default_config.default_model_requirement_vector)
            self.assertEqual({}, default_config.review_model_requirement_vector)
            self.assertEqual({}, default_config.default_execution_config)
            self.assertEqual({}, default_config.execution_targets)
            self.assertEqual([], default_config.model_selection_rules)

            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "default_model_requirement_vector": {
                            "source": "config_default",
                            "confidence": "medium",
                            "dimensions": {"reasoning_depth": "medium"},
                        },
                        "review_model_requirement_vector": {
                            "source": "config_review_default",
                            "confidence": "medium",
                            "dimensions": {"review_strictness": "high"},
                        },
                        "default_execution_config": {"model": "gpt-5"},
                        "execution_targets": {
                            "low_cost_current": {
                                "model": "gpt-5-small",
                                "config_overrides": {"model_reasoning_effort": "low"},
                                "freshness": {
                                    "owner": "operator",
                                    "last_reviewed_at": "2026-07-03",
                                    "review_after_days": 14,
                                },
                            }
                        },
                        "model_selection_rules": [
                            {
                                "name": "low-cost-docs",
                                "when": {"reasoning_depth": "low"},
                                "execution_target": "low_cost_current",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertEqual("medium", config.default_model_requirement_vector["dimensions"]["reasoning_depth"])
            self.assertEqual("high", config.review_model_requirement_vector["dimensions"]["review_strictness"])
            self.assertEqual("gpt-5", config.default_execution_config["model"])
            self.assertEqual("gpt-5-small", config.execution_targets["low_cost_current"]["model"])
            self.assertEqual({"model_reasoning_effort": "low"}, config.execution_targets["low_cost_current"]["config_overrides"])
            self.assertEqual("2026-07-03", config.execution_targets["low_cost_current"]["freshness"]["last_reviewed_at"])
            self.assertEqual("low_cost_current", config.model_selection_rules[0]["execution_target"])

    def test_execution_target_selection_cannot_be_mixed_with_direct_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "model_selection_rules": [
                            {
                                "name": "mixed",
                                "execution_target": "low_cost_current",
                                "model": "gpt-5-small",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "execution_target cannot be combined"):
                Config.load(str(config_path), root=Path(tmp))

    def test_execution_target_references_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "model_selection_rules": [
                            {
                                "name": "missing-target",
                                "execution_target": "not_configured",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "references unknown execution_target"):
                Config.load(str(config_path), root=Path(tmp))

    def test_removed_execution_profile_config_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"default_execution_profile": "missing"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "removed config field"):
                Config.load(str(config_path), root=Path(tmp))

    def test_model_selection_config_rejects_unknown_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"

            config_path.write_text(
                json.dumps({"model_selection_rules": [{"name": "bad", "config_overrides": {"danger": "true"}}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "is not allowlisted"):
                Config.load(str(config_path), root=Path(tmp))

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

    def test_reviewer_codex_limits_must_be_non_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"auto_review_codex_max_calls_per_run": -1}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "auto_review_codex_max_calls_per_run must be a non-negative integer"):
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
            self.assertEqual([], config.codex_cli_update_command)
            self.assertEqual([], config.codex_cli_smoke_command)
            self.assertEqual([], config.codex_cli_rollback_command)
            self.assertFalse(config.codex_cli_maintenance_on_empty)

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

    def test_usage_admission_options_can_be_enabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "usage_admission_enabled": True,
                        "usage_admission_command": ["usage-snapshot", "--json"],
                        "usage_admission_timeout_seconds": 3,
                        "usage_admission_max_age_seconds": 120,
                        "usage_admission_short_window_threshold_percent": 12.5,
                        "usage_admission_reset_grace_seconds": 90,
                    }
                ),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertTrue(config.usage_admission_enabled)
            self.assertEqual(["usage-snapshot", "--json"], config.usage_admission_command)
            self.assertEqual(3, config.usage_admission_timeout_seconds)
            self.assertEqual(120, config.usage_admission_max_age_seconds)
            self.assertEqual(12.5, config.usage_admission_short_window_threshold_percent)
            self.assertEqual(90, config.usage_admission_reset_grace_seconds)

    def test_enabled_usage_admission_requires_command_and_short_window_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            invalid_cases = [
                (
                    {"usage_admission_enabled": True, "usage_admission_short_window_threshold_percent": 10},
                    "usage_admission_command must be configured",
                ),
                (
                    {"usage_admission_enabled": True, "usage_admission_command": ["usage-snapshot"]},
                    "usage_admission_short_window_threshold_percent must be configured",
                ),
                (
                    {"usage_admission_short_window_threshold_percent": 101},
                    "usage_admission_short_window_threshold_percent must be a number from 0 to 100",
                ),
            ]
            for data, message in invalid_cases:
                with self.subTest(data=data):
                    config_path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        Config.load(str(config_path), root=Path(tmp))

    def test_codex_cli_maintenance_commands_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "codex_cli_update_command": ["npm", "install", "-g", "@openai/codex"],
                        "codex_cli_smoke_command": ["cbr", "doctor"],
                        "codex_cli_rollback_command": ["npm", "install", "-g", "@openai/codex@0.1.0"],
                        "codex_cli_maintenance_on_empty": True,
                    }
                ),
                encoding="utf-8",
            )

            config = Config.load(str(config_path), root=Path(tmp))

            self.assertEqual(["npm", "install", "-g", "@openai/codex"], config.codex_cli_update_command)
            self.assertEqual(["cbr", "doctor"], config.codex_cli_smoke_command)
            self.assertEqual(["npm", "install", "-g", "@openai/codex@0.1.0"], config.codex_cli_rollback_command)
            self.assertTrue(config.codex_cli_maintenance_on_empty)

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

            config_path.write_text(json.dumps({"codex_cli_update_command": "npm update"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "codex_cli_update_command must be a list of strings"):
                Config.load(str(config_path), root=Path(tmp))

            config_path.write_text(json.dumps({"codex_cli_smoke_command": "cbr doctor"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "codex_cli_smoke_command must be a list of strings"):
                Config.load(str(config_path), root=Path(tmp))

            config_path.write_text(json.dumps({"codex_cli_rollback_command": "npm install old"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "codex_cli_rollback_command must be a list of strings"):
                Config.load(str(config_path), root=Path(tmp))

            config_path.write_text(json.dumps({"codex_cli_maintenance_on_empty": "true"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "codex_cli_maintenance_on_empty must be a boolean"):
                Config.load(str(config_path), root=Path(tmp))


if __name__ == "__main__":
    unittest.main()

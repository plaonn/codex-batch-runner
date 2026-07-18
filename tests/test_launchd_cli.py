from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.cli import main
from codex_batch_runner.launchd_lifecycle import LaunchdPlanInput, render_launchd_plist
from codex_batch_runner.launchd_operations import LaunchdEnvironment


def write_config(root: Path, *, xdg: bool = False) -> Path:
    path = (
        root / "codex-batch-runner" / "config.json"
        if xdg
        else root / "config.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"root": str(root / "runtime")}), encoding="utf-8")
    return path


def plan_values(root: Path, config_path: Path, *, interval: int = 600, provenance: str = "cli") -> LaunchdPlanInput:
    return LaunchdPlanInput(
        label="com.example.codex-batch-runner",
        executable_path=str(root / "bin" / "cbr"),
        config_path=str(config_path.resolve()),
        config_provenance=provenance,
        working_directory=str(root / "work"),
        stdout_path=str(root / "logs" / "launchd.out.log"),
        stderr_path=str(root / "logs" / "launchd.err.log"),
        environment_path="/opt/cbr/bin:/usr/bin:/bin",
        start_interval_seconds=interval,
    )


def plan_args(
    root: Path,
    config_path: Path,
    *,
    interval: int = 600,
    existing: Path | None = None,
    environment_path: str = "/opt/cbr/bin:/usr/bin:/bin",
) -> list[str]:
    args = [
        "--config",
        str(config_path),
        "launchd",
        "plan",
        "--label",
        "com.example.codex-batch-runner",
        "--executable",
        str(root / "bin" / "cbr"),
        "--working-directory",
        str(root / "work"),
        "--stdout-path",
        str(root / "logs" / "launchd.out.log"),
        "--stderr-path",
        str(root / "logs" / "launchd.err.log"),
        "--environment-path",
        environment_path,
        "--start-interval-seconds",
        str(interval),
    ]
    if existing is not None:
        args.extend(["--existing-plist", str(existing)])
    return args


def install_args(root: Path, config_path: Path, *, apply: bool = False) -> list[str]:
    label = "com.example.codex-batch-runner"
    args = [
        "--config",
        str(config_path),
        "launchd",
        "install",
        "--label",
        label,
        "--executable",
        str(root / "bin" / "cbr"),
        "--working-directory",
        str(root / "work"),
        "--stdout-path",
        str(root / "logs" / "launchd.out.log"),
        "--stderr-path",
        str(root / "logs" / "launchd.err.log"),
        "--environment-path",
        "/opt/cbr/bin:/usr/bin:/bin",
        "--start-interval-seconds",
        "600",
        "--destination",
        str(root / "Library" / "LaunchAgents" / f"{label}.plist"),
        "--user-domain",
        "gui/501",
        "--json",
    ]
    if apply:
        args.append("--apply")
    return args


def run_cli(args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class LaunchdCliTests(unittest.TestCase):
    def test_install_defaults_to_dry_run_with_explicit_destination_and_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config_path = write_config(root)
            launchagents = root / "Library" / "LaunchAgents"
            launchagents.mkdir(parents=True)
            destination = launchagents / "com.example.codex-batch-runner.plist"
            before = file_snapshot(root)
            env = LaunchdEnvironment("Darwin", 501, root, "gui/501")

            with patch("codex_batch_runner.cli.current_launchd_environment", return_value=env):
                code, output, stderr = run_cli(install_args(root, config_path))
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            self.assertEqual(("dry_run", "planned", "install"), (report["mode"], report["status"], report["action"]))
            self.assertFalse(report["mutation_allowed"])
            self.assertFalse(report["changed"])
            self.assertFalse(destination.exists())
            self.assertEqual(before, file_snapshot(root))
            self.assertEqual(
                "accidental_or_non_adversarial",
                report["namespace_concurrency"]["supported_threat_model"],
            )

    def test_install_human_report_exposes_namespace_threat_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config_path = write_config(root)
            (root / "Library" / "LaunchAgents").mkdir(parents=True)
            env = LaunchdEnvironment("Darwin", 501, root, "gui/501")
            args = install_args(root, config_path)
            args.remove("--json")

            with patch("codex_batch_runner.cli.current_launchd_environment", return_value=env):
                code, output, stderr = run_cli(args)

            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            self.assertIn("namespace_concurrency_supported: accidental_or_non_adversarial", output)
            self.assertIn(
                "namespace_concurrency_unsupported: active_same_uid_adversarial_namespace_mutation",
                output,
            )
            self.assertIn("strict_protection_requires: protected directory or privileged helper", output)

    def test_install_apply_without_separate_confirmation_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config_path = write_config(root)
            (root / "Library" / "LaunchAgents").mkdir(parents=True)
            before = file_snapshot(root)
            env = LaunchdEnvironment("Darwin", 501, root, "gui/501")

            with patch("codex_batch_runner.cli.current_launchd_environment", return_value=env):
                code, output, stderr = run_cli(install_args(root, config_path, apply=True))

            self.assertEqual(2, code)
            self.assertEqual("", stderr)
            report = json.loads(output)
            self.assertEqual(("blocked", "blocked"), (report["status"], report["action"]))
            self.assertIn("--apply requires --confirm-label", report["reason"])
            self.assertFalse(report["mutation_allowed"])
            self.assertEqual(before, file_snapshot(root))

    def test_missing_plan_renders_json_without_reading_or_writing_implicit_plist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            implicit_foreign = root / "com.example.codex-batch-runner.plist"
            implicit_foreign.write_bytes(plistlib.dumps({"Label": "foreign"}))
            before = file_snapshot(root)

            code, output, stderr = run_cli([*plan_args(root, config_path), "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            self.assertEqual(("not_installed", "create"), (report["status"], report["action"]))
            self.assertEqual(
                {"source": "cli", "path": str(config_path.resolve())},
                report["config"],
            )
            self.assertEqual({"supplied": False, "path": None}, report["existing_plist"])
            self.assertTrue(report["read_only"])
            self.assertFalse(report["mutation_allowed"])
            self.assertIn(str(config_path.resolve()), report["intended_plist"])
            self.assertEqual(before, file_snapshot(root))
            self.assertFalse((root / "runtime").exists())

    def test_managed_and_drifted_plists_are_classified_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            managed_path = root / "managed.plist"
            managed_path.write_bytes(render_launchd_plist(plan_values(root, config_path)))
            drifted_path = root / "drifted.plist"
            drifted_path.write_bytes(render_launchd_plist(plan_values(root, config_path, interval=300)))

            for path, expected in (
                (managed_path, ("managed_ok", "none")),
                (drifted_path, ("drifted", "update_needed")),
            ):
                with self.subTest(path=path.name):
                    before = file_snapshot(root)
                    code, output, stderr = run_cli([*plan_args(root, config_path, existing=path), "--json"])
                    report = json.loads(output)
                    self.assertEqual(0, code)
                    self.assertEqual("", stderr)
                    self.assertEqual(expected, (report["status"], report["action"]))
                    self.assertRegex(report["digest"], r"^[0-9a-f]{64}$")
                    self.assertEqual(before, file_snapshot(root))

    def test_owned_plist_extra_behavior_and_environment_keys_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            extra_behavior = plistlib.loads(render_launchd_plist(plan_values(root, config_path)))
            extra_behavior["KeepAlive"] = True
            behavior_path = root / "extra-behavior.plist"
            behavior_path.write_bytes(plistlib.dumps(extra_behavior))

            extra_environment = plistlib.loads(render_launchd_plist(plan_values(root, config_path)))
            extra_environment["EnvironmentVariables"]["PYTHONPATH"] = "/opt/cbr/src"
            environment_path = root / "extra-environment.plist"
            environment_path.write_bytes(plistlib.dumps(extra_environment))

            for path, reason in (
                (behavior_path, "top-level keys"),
                (environment_path, "EnvironmentVariables keys"),
            ):
                with self.subTest(path=path.name):
                    before = file_snapshot(root)
                    code, output, stderr = run_cli(
                        [*plan_args(root, config_path, existing=path), "--json"]
                    )
                    report = json.loads(output)
                    self.assertEqual(2, code)
                    self.assertEqual("", stderr)
                    self.assertEqual(("unhealthy", "blocked"), (report["status"], report["action"]))
                    self.assertIn(reason, report["reason"])
                    self.assertEqual(before, file_snapshot(root))

    def test_foreign_and_malformed_plists_report_blocked_with_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            foreign = root / "foreign.plist"
            foreign.write_bytes(plistlib.dumps({"Label": "com.example.foreign"}))
            malformed = root / "malformed.plist"
            malformed.write_bytes(b"not a plist")

            for path, status in ((foreign, "foreign_conflict"), (malformed, "unhealthy")):
                with self.subTest(path=path.name):
                    before = file_snapshot(root)
                    code, output, stderr = run_cli([*plan_args(root, config_path, existing=path), "--json"])
                    report = json.loads(output)
                    self.assertEqual(2, code)
                    self.assertEqual("", stderr)
                    self.assertEqual(status, report["status"])
                    self.assertEqual("blocked", report["action"])
                    self.assertEqual(before, file_snapshot(root))

    def test_missing_explicit_existing_plist_fails_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            missing = root / "missing.plist"

            code, output, stderr = run_cli(
                [*plan_args(root, config_path, existing=missing), "--json"]
            )

            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertIn(f"existing plist not found: {missing.resolve()}", stderr)

    def test_relative_or_empty_environment_path_segment_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)

            for value in ("/usr/bin:relative/bin", "/usr/bin::/bin", "/usr/bin:."):
                with self.subTest(value=value):
                    before = file_snapshot(root)
                    code, output, stderr = run_cli(
                        [*plan_args(root, config_path, environment_path=value), "--json"]
                    )
                    self.assertEqual(1, code)
                    self.assertEqual("", output)
                    self.assertIn(
                        "environment_path segments must be non-empty absolute paths",
                        stderr,
                    )
                    self.assertEqual(before, file_snapshot(root))

    def test_xdg_provenance_is_injected_and_human_output_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xdg_home = root / "xdg"
            config_path = write_config(xdg_home, xdg=True)
            args = plan_args(root, config_path)
            args = args[2:]

            with patch.dict(
                os.environ,
                {"CBR_CONFIG": "", "XDG_CONFIG_HOME": str(xdg_home), "HOME": str(root / "home")},
                clear=True,
            ):
                code, output, stderr = run_cli(args)

            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            self.assertIn("status: not_installed", output)
            self.assertIn("action: create", output)
            self.assertIn("source: xdg", output)
            self.assertIn(f"path: {config_path.resolve()}", output)
            self.assertIn("digest:", output)
            self.assertIn("intended_plist:", output)
            self.assertIn("mutation_allowed: false", output)


if __name__ == "__main__":
    unittest.main()

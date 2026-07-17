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


def plan_args(root: Path, config_path: Path, *, interval: int = 600, existing: Path | None = None) -> list[str]:
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
        "/opt/cbr/bin:/usr/bin:/bin",
        "--start-interval-seconds",
        str(interval),
    ]
    if existing is not None:
        args.extend(["--existing-plist", str(existing)])
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

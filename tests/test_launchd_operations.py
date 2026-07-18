from __future__ import annotations

import plistlib
import unittest
from dataclasses import replace
from pathlib import Path

from codex_batch_runner.launchd_lifecycle import LaunchdPlanInput, render_launchd_plist
from codex_batch_runner.launchd_operations import (
    MAX_MANAGED_PLIST_BYTES,
    LaunchctlResult,
    LaunchdEnvironment,
    PathState,
    lifecycle_result_report,
    run_launchd_install,
    run_launchd_uninstall,
)


LABEL = "com.example.codex-batch-runner"
HOME = Path("/Users/example")
DESTINATION = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
CONFIG_PATH = "/Users/example/.config/codex-batch-runner/config.json"


def plan_input(*, interval: int = 600) -> LaunchdPlanInput:
    return LaunchdPlanInput(
        label=LABEL,
        executable_path="/opt/example/bin/cbr",
        config_path=CONFIG_PATH,
        config_provenance="cli",
        working_directory="/Users/example/runner",
        stdout_path="/Users/example/Library/Logs/cbr.out.log",
        stderr_path="/Users/example/Library/Logs/cbr.err.log",
        environment_path="/opt/example/bin:/usr/bin:/bin",
        start_interval_seconds=interval,
    )


def environment(**overrides: object) -> LaunchdEnvironment:
    values = {"platform_name": "Darwin", "uid": 501, "home": HOME, "user_domain": "gui/501"}
    values.update(overrides)
    return LaunchdEnvironment(**values)  # type: ignore[arg-type]


class FakeFilesystem:
    def __init__(self, existing: bytes | None = None, *, destination_kind: str | None = None) -> None:
        self.files: dict[Path, bytes] = {}
        self.kinds = {DESTINATION.parent: "directory"}
        if existing is not None:
            self.files[DESTINATION] = existing
        if destination_kind is not None:
            self.kinds[DESTINATION] = destination_kind
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.backup_counter = 0

    def inspect(self, path: Path) -> PathState:
        self.calls.append(f"inspect:{path}")
        if path in self.kinds:
            return PathState(self.kinds[path], len(self.files.get(path, b"")))
        if path in self.files:
            return PathState("file", len(self.files[path]))
        return PathState("absent")

    def read_bytes(self, path: Path) -> bytes:
        self.calls.append("read")
        if "read" in self.fail:
            raise OSError("injected")
        return self.files[path]

    def write_atomic(self, path: Path, data: bytes) -> None:
        self.calls.append("write")
        if "write" in self.fail:
            raise OSError("injected")
        self.files[path] = data

    def create_unique_backup(self, path: Path) -> Path:
        self.calls.append("backup")
        if "backup" in self.fail:
            raise OSError("injected")
        backup = self._backup_path(path)
        self.files[backup] = self.files[path]
        return backup

    def move_to_unique_backup(self, path: Path) -> Path:
        self.calls.append("move")
        if "move" in self.fail:
            raise OSError("injected")
        backup = self._backup_path(path)
        self.files[backup] = self.files.pop(path)
        return backup

    def restore_backup_atomic(self, backup: Path, destination: Path) -> None:
        self.calls.append("restore")
        if "restore" in self.fail:
            raise OSError("injected")
        self.files[destination] = self.files.pop(backup)

    def remove(self, path: Path) -> None:
        self.calls.append("remove")
        if "remove" in self.fail:
            raise OSError("injected")
        self.files.pop(path)

    def discard_backup(self, path: Path) -> None:
        self.calls.append("discard")
        if "discard" in self.fail:
            raise OSError("injected")
        self.files.pop(path)

    def _backup_path(self, path: Path) -> Path:
        self.backup_counter += 1
        return path.with_name(f".{path.name}.cbr-backup-test-{self.backup_counter}")


class FakeLaunchctl:
    def __init__(self, outcomes: list[bool] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[str] = []

    def bootstrap(self, user_domain: str, plist_path: Path) -> LaunchctlResult:
        return self._result("bootstrap", user_domain, plist_path)

    def bootout(self, user_domain: str, plist_path: Path) -> LaunchctlResult:
        return self._result("bootout", user_domain, plist_path)

    def _result(self, action: str, user_domain: str, plist_path: Path) -> LaunchctlResult:
        self.calls.append(f"{action}:{user_domain}:{plist_path}")
        ok = self.outcomes.pop(0) if self.outcomes else True
        return LaunchctlResult(action, ok, 0 if ok else 5, "fake success" if ok else "fake failure")


class LaunchdOperationTests(unittest.TestCase):
    def test_default_dry_run_has_no_mutation_or_launchctl_calls(self) -> None:
        filesystem = FakeFilesystem()
        launchctl = FakeLaunchctl()

        result = run_launchd_install(
            plan_input(), DESTINATION, environment=environment(), filesystem=filesystem, launchctl=launchctl
        )

        self.assertEqual(("planned", "install", False), (result.status, result.action, result.changed))
        self.assertNotIn(DESTINATION, filesystem.files)
        self.assertEqual([], launchctl.calls)
        self.assertNotIn("write", filesystem.calls)
        self.assertFalse(lifecycle_result_report(result)["mutation_allowed"])

    def test_apply_requires_separate_exact_confirmation_before_inspection(self) -> None:
        for confirmation in (None, "com.example.other"):
            filesystem = FakeFilesystem()
            launchctl = FakeLaunchctl()
            with self.subTest(confirmation=confirmation):
                result = run_launchd_install(
                    plan_input(),
                    DESTINATION,
                    apply=True,
                    confirm_label=confirmation,
                    environment=environment(),
                    filesystem=filesystem,
                    launchctl=launchctl,
                )
            self.assertEqual("blocked", result.status)
            self.assertFalse(lifecycle_result_report(result)["mutation_allowed"])
            self.assertEqual([], filesystem.calls)
            self.assertEqual([], launchctl.calls)

    def test_platform_user_domain_and_destination_guards_block_without_mutation(self) -> None:
        cases = (
            (environment(platform_name="Linux"), DESTINATION, "only available on macOS"),
            (environment(uid=0, user_domain="gui/0"), DESTINATION, "non-root"),
            (environment(user_domain="gui/502"), DESTINATION, "gui/501"),
            (environment(), Path("relative.plist"), "explicit absolute"),
            (environment(), HOME / "Library" / "LaunchAgents" / "other.plist", "exactly match"),
        )
        for env, destination, reason in cases:
            filesystem = FakeFilesystem()
            launchctl = FakeLaunchctl()
            with self.subTest(reason=reason):
                result = run_launchd_install(
                    plan_input(), destination, environment=env, filesystem=filesystem, launchctl=launchctl
                )
                self.assertEqual("blocked", result.status)
                self.assertIn(reason, result.reason)
                self.assertEqual([], launchctl.calls)
                self.assertNotIn("write", filesystem.calls)

    def test_unresolved_config_identity_blocks_before_filesystem_or_launchctl(self) -> None:
        filesystem = FakeFilesystem()
        launchctl = FakeLaunchctl()
        unresolved = replace(plan_input(), config_path="/Users/example/../example/config.json")

        result = run_launchd_install(
            unresolved, DESTINATION, environment=environment(), filesystem=filesystem, launchctl=launchctl
        )

        self.assertEqual("blocked", result.status)
        self.assertIn("resolved and absolute", result.reason)
        self.assertEqual([], filesystem.calls)
        self.assertEqual([], launchctl.calls)

    def test_install_success_and_bootstrap_rollback(self) -> None:
        success_fs = FakeFilesystem()
        success_ctl = FakeLaunchctl([True])
        success = run_launchd_install(
            plan_input(),
            DESTINATION,
            apply=True,
            confirm_label=LABEL,
            environment=environment(),
            filesystem=success_fs,
            launchctl=success_ctl,
        )
        self.assertEqual("applied", success.status)
        self.assertEqual(render_launchd_plist(plan_input()), success_fs.files[DESTINATION])
        self.assertEqual([f"bootstrap:gui/501:{DESTINATION}"], success_ctl.calls)

        recovered_fs = FakeFilesystem()
        recovered = run_launchd_install(
            plan_input(),
            DESTINATION,
            apply=True,
            confirm_label=LABEL,
            environment=environment(),
            filesystem=recovered_fs,
            launchctl=FakeLaunchctl([False]),
        )
        self.assertEqual("recovered", recovered.status)
        self.assertNotIn(DESTINATION, recovered_fs.files)

        required_fs = FakeFilesystem()
        required_fs.fail.add("remove")
        required = run_launchd_install(
            plan_input(),
            DESTINATION,
            apply=True,
            confirm_label=LABEL,
            environment=environment(),
            filesystem=required_fs,
            launchctl=FakeLaunchctl([False]),
        )
        self.assertEqual("recovery_required", required.status)
        self.assertIn(DESTINATION, required_fs.files)

    def test_same_digest_noop_and_foreign_or_invalid_block(self) -> None:
        managed = render_launchd_plist(plan_input())
        noop_fs = FakeFilesystem(managed)
        noop_ctl = FakeLaunchctl()
        noop = run_launchd_install(
            plan_input(),
            DESTINATION,
            apply=True,
            confirm_label=LABEL,
            environment=environment(),
            filesystem=noop_fs,
            launchctl=noop_ctl,
        )
        self.assertEqual("noop", noop.status)
        self.assertEqual([], noop_ctl.calls)

        for content in (plistlib.dumps({"Label": LABEL}), b"not a plist"):
            filesystem = FakeFilesystem(content)
            launchctl = FakeLaunchctl()
            with self.subTest(content=content[:10]):
                result = run_launchd_install(
                    plan_input(), DESTINATION, environment=environment(), filesystem=filesystem, launchctl=launchctl
                )
                self.assertEqual("blocked", result.status)
                self.assertEqual([], launchctl.calls)
                self.assertNotIn("write", filesystem.calls)

    def test_update_success_and_failure_recovery_paths(self) -> None:
        old = render_launchd_plist(plan_input(interval=300))
        new = render_launchd_plist(plan_input())
        success_fs = FakeFilesystem(old)
        success_ctl = FakeLaunchctl([True, True])
        success = run_launchd_install(
            plan_input(), DESTINATION, apply=True, confirm_label=LABEL,
            environment=environment(), filesystem=success_fs, launchctl=success_ctl,
        )
        self.assertEqual("applied", success.status)
        self.assertEqual(new, success_fs.files[DESTINATION])
        self.assertEqual(["read", "backup", "write", "discard"], [c for c in success_fs.calls if not c.startswith("inspect")])
        self.assertEqual(["bootout", "bootstrap"], [c.split(":", 1)[0] for c in success_ctl.calls])

        recovered_fs = FakeFilesystem(old)
        recovered_ctl = FakeLaunchctl([True, False, True])
        recovered = run_launchd_install(
            plan_input(), DESTINATION, apply=True, confirm_label=LABEL,
            environment=environment(), filesystem=recovered_fs, launchctl=recovered_ctl,
        )
        self.assertEqual("recovered", recovered.status)
        self.assertEqual(old, recovered_fs.files[DESTINATION])
        self.assertEqual(["bootout", "bootstrap", "bootstrap"], [c.split(":", 1)[0] for c in recovered_ctl.calls])

        for failure, outcomes in (("restore", [True, False]), (None, [True, False, False])):
            filesystem = FakeFilesystem(old)
            if failure:
                filesystem.fail.add(failure)
            result = run_launchd_install(
                plan_input(), DESTINATION, apply=True, confirm_label=LABEL,
                environment=environment(), filesystem=filesystem, launchctl=FakeLaunchctl(outcomes),
            )
            self.assertEqual("recovery_required", result.status)

    def test_update_bootout_failure_keeps_original_bytes(self) -> None:
        old = render_launchd_plist(plan_input(interval=300))
        filesystem = FakeFilesystem(old)
        result = run_launchd_install(
            plan_input(), DESTINATION, apply=True, confirm_label=LABEL,
            environment=environment(), filesystem=filesystem, launchctl=FakeLaunchctl([False]),
        )
        self.assertEqual("failed", result.status)
        self.assertEqual(old, filesystem.files[DESTINATION])
        self.assertNotIn("write", filesystem.calls)

    def test_uninstall_success_and_failure_paths(self) -> None:
        managed = render_launchd_plist(plan_input())
        dry_fs = FakeFilesystem(managed)
        dry_ctl = FakeLaunchctl()
        dry = run_launchd_uninstall(
            label=LABEL, config_path=CONFIG_PATH, config_source="cli", destination=DESTINATION,
            environment=environment(), filesystem=dry_fs, launchctl=dry_ctl,
        )
        self.assertEqual(("planned", "uninstall"), (dry.status, dry.action))
        self.assertIn(DESTINATION, dry_fs.files)
        self.assertEqual([], dry_ctl.calls)

        success_fs = FakeFilesystem(managed)
        success = run_launchd_uninstall(
            label=LABEL, config_path=CONFIG_PATH, config_source="cli", destination=DESTINATION,
            apply=True, confirm_label=LABEL, environment=environment(), filesystem=success_fs,
            launchctl=FakeLaunchctl([True]),
        )
        self.assertEqual("applied", success.status)
        self.assertNotIn(DESTINATION, success_fs.files)
        self.assertTrue(success.backup_retained)

        failed_fs = FakeFilesystem(managed)
        failed = run_launchd_uninstall(
            label=LABEL, config_path=CONFIG_PATH, config_source="cli", destination=DESTINATION,
            apply=True, confirm_label=LABEL, environment=environment(), filesystem=failed_fs,
            launchctl=FakeLaunchctl([False]),
        )
        self.assertEqual("failed", failed.status)
        self.assertEqual(managed, failed_fs.files[DESTINATION])
        self.assertNotIn("move", failed_fs.calls)

        recovery_fs = FakeFilesystem(managed)
        recovery_fs.fail.add("move")
        recovery = run_launchd_uninstall(
            label=LABEL, config_path=CONFIG_PATH, config_source="cli", destination=DESTINATION,
            apply=True, confirm_label=LABEL, environment=environment(), filesystem=recovery_fs,
            launchctl=FakeLaunchctl([True, False]),
        )
        self.assertEqual("recovery_required", recovery.status)
        self.assertEqual(managed, recovery_fs.files[DESTINATION])

    def test_symlink_nonregular_and_oversize_destinations_block(self) -> None:
        cases = (
            FakeFilesystem(destination_kind="symlink"),
            FakeFilesystem(destination_kind="directory"),
            FakeFilesystem(b"x" * (MAX_MANAGED_PLIST_BYTES + 1)),
        )
        for filesystem in cases:
            launchctl = FakeLaunchctl()
            result = run_launchd_install(
                plan_input(), DESTINATION, environment=environment(), filesystem=filesystem, launchctl=launchctl
            )
            self.assertEqual("blocked", result.status)
            self.assertEqual([], launchctl.calls)
            self.assertNotIn("write", filesystem.calls)


if __name__ == "__main__":
    unittest.main()

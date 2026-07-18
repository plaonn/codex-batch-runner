from __future__ import annotations

import plistlib
import platform
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_batch_runner.launchd_lifecycle import LaunchdPlanInput, render_launchd_plist
from codex_batch_runner.launchd_operations import (
    BackupSnapshot,
    FileSnapshot,
    IdentityChangedError,
    MAX_MANAGED_PLIST_BYTES,
    LaunchctlResult,
    LaunchdEnvironment,
    LocalLaunchdFilesystem,
    ParentHandle,
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
        self.identities = {DESTINATION.parent: 100}
        self.next_identity = 200
        if existing is not None:
            self.files[DESTINATION] = existing
            self.identities[DESTINATION] = self._new_identity()
        if destination_kind is not None:
            self.kinds[DESTINATION] = destination_kind
            self.identities[DESTINATION] = self._new_identity()
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.backup_counter = 0
        self.assert_count = 0
        self.race_at_assert: int | None = None
        self.race_target: str | None = None

    def inspect(self, path: Path) -> PathState:
        self.calls.append(f"inspect:{path}")
        if path in self.kinds:
            return self._state(path, self.kinds[path])
        if path in self.files:
            return self._state(path, "file")
        return PathState("absent")

    def open_parent(self, path: Path, expected: PathState) -> ParentHandle:
        self.calls.append("open_parent")
        self._require(self._peek(path), expected)
        return ParentHandle(path, expected, self)

    def close_parent(self, parent: ParentHandle) -> None:
        self.calls.append("close_parent")

    def read_snapshot(self, path: Path, max_bytes: int, parent: ParentHandle) -> FileSnapshot:
        self.calls.append("read")
        if "read" in self.fail:
            raise OSError("injected")
        data = self.files[path]
        if len(data) > max_bytes:
            raise ValueError("oversize")
        return FileSnapshot(data, self._state(path, "file"))

    def assert_unchanged(
        self,
        path: Path,
        expected: PathState,
        parent: ParentHandle,
    ) -> None:
        self.calls.append("assert")
        self.assert_count += 1
        if self.race_at_assert == self.assert_count:
            self._inject_race()
        self._require(self._peek(parent.path), parent.state)
        self._require(self._peek(path), expected)

    def write_atomic(
        self,
        path: Path,
        data: bytes,
        expected: PathState,
        parent: ParentHandle,
    ) -> PathState:
        self.calls.append("write")
        if "write" in self.fail:
            raise OSError("injected")
        self.assert_unchanged(path, expected, parent)
        self.files[path] = data
        self.kinds.pop(path, None)
        self.identities[path] = self._new_identity()
        return self._state(path, "file")

    def create_unique_backup(
        self,
        path: Path,
        data: bytes,
        expected: PathState,
        parent: ParentHandle,
    ) -> BackupSnapshot:
        self.calls.append("backup")
        if "backup" in self.fail:
            raise OSError("injected")
        self.assert_unchanged(path, expected, parent)
        backup = self._backup_path(path)
        self.files[backup] = data
        self.identities[backup] = self._new_identity()
        return BackupSnapshot(backup, self._state(backup, "file"), data)

    def move_to_unique_backup(
        self,
        path: Path,
        expected: PathState,
        parent: ParentHandle,
    ) -> BackupSnapshot:
        self.calls.append("move")
        if "move" in self.fail:
            raise OSError("injected")
        self.assert_unchanged(path, expected, parent)
        backup = self._backup_path(path)
        self.files[backup] = self.files.pop(path)
        self.identities[backup] = self.identities.pop(path)
        self.assert_unchanged(path, PathState("absent"), parent)
        return BackupSnapshot(backup, self._state(backup, "file"), self.files[backup])

    def restore_backup_atomic(
        self,
        backup: BackupSnapshot,
        destination: Path,
        expected_destination: PathState,
        parent: ParentHandle,
    ) -> PathState:
        self.calls.append("restore")
        if "restore" in self.fail:
            raise OSError("injected")
        self.assert_unchanged(destination, expected_destination, parent)
        self._require(self._peek(backup.path), backup.state)
        self.files[destination] = self.files.pop(backup.path)
        self.identities[destination] = self.identities.pop(backup.path)
        return self._state(destination, "file")

    def remove(self, path: Path, expected: PathState, parent: ParentHandle) -> None:
        self.calls.append("remove")
        if "remove" in self.fail:
            raise OSError("injected")
        self.assert_unchanged(path, expected, parent)
        self.files.pop(path)
        self.identities.pop(path)
        self.assert_unchanged(path, PathState("absent"), parent)

    def discard_backup(self, backup: BackupSnapshot, parent: ParentHandle) -> None:
        self.calls.append("discard")
        if "discard" in self.fail:
            raise OSError("injected")
        self.assert_unchanged(backup.path, backup.state, parent)
        self.files.pop(backup.path)
        self.identities.pop(backup.path)

    def _backup_path(self, path: Path) -> Path:
        self.backup_counter += 1
        return path.with_name(f".{path.name}.cbr-backup-test-{self.backup_counter}")

    def _state(self, path: Path, kind: str) -> PathState:
        size = len(self.files.get(path, b""))
        identity = self.identities.get(path)
        return PathState(kind, size, 1, identity, identity)

    def _peek(self, path: Path) -> PathState:
        if path in self.kinds:
            return self._state(path, self.kinds[path])
        if path in self.files:
            return self._state(path, "file")
        return PathState("absent")

    def _require(self, actual: PathState, expected: PathState) -> None:
        fields = ("kind", "device", "inode")
        if expected.kind == "file":
            fields += ("size", "modified_ns")
        if any(getattr(actual, field) != getattr(expected, field) for field in fields):
            raise IdentityChangedError("injected identity mismatch")

    def _new_identity(self) -> int:
        self.next_identity += 1
        return self.next_identity

    def _inject_race(self) -> None:
        if self.race_target == "destination_foreign":
            self.files[DESTINATION] = plistlib.dumps({"Label": "com.example.foreign"})
            self.kinds.pop(DESTINATION, None)
            self.identities[DESTINATION] = self._new_identity()
        elif self.race_target == "destination_symlink":
            self.files.pop(DESTINATION, None)
            self.kinds[DESTINATION] = "symlink"
            self.identities[DESTINATION] = self._new_identity()
        elif self.race_target == "parent_symlink":
            self.kinds[DESTINATION.parent] = "symlink"
            self.identities[DESTINATION.parent] = self._new_identity()
        else:
            raise AssertionError("race_target must be configured")


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
    def test_local_no_follow_snapshot_detects_destination_and_parent_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "LaunchAgents"
            parent.mkdir()
            destination = parent / "managed.plist"
            destination.write_bytes(b"managed")
            filesystem = LocalLaunchdFilesystem()
            parent_state = filesystem.inspect(parent)
            parent_handle = filesystem.open_parent(parent, parent_state)
            snapshot = filesystem.read_snapshot(destination, 1024, parent_handle)

            destination.unlink()
            destination.symlink_to(root / "foreign.plist")
            with self.assertRaises(IdentityChangedError):
                filesystem.assert_unchanged(destination, snapshot.state, parent_handle)

            destination.unlink()
            destination.write_bytes(b"managed")
            snapshot = filesystem.read_snapshot(destination, 1024, parent_handle)
            old_parent = root / "LaunchAgents-old"
            parent.rename(old_parent)
            parent.mkdir()
            (parent / destination.name).write_bytes(b"managed")
            with self.assertRaises(IdentityChangedError):
                filesystem.assert_unchanged(parent / destination.name, snapshot.state, parent_handle)
            filesystem.close_parent(parent_handle)

    @unittest.skipUnless(platform.system() == "Darwin", "requires Darwin renameatx_np")
    def test_local_final_check_races_restore_foreign_entries_for_all_mutations(self) -> None:
        foreign = b"foreign-entry"

        for action in ("write", "backup", "move", "remove"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                parent_path = Path(tmp) / "LaunchAgents"
                parent_path.mkdir()
                destination = parent_path / "managed.plist"
                destination.write_bytes(b"managed-old")

                def replace_destination(observed: str, parent: ParentHandle, name: str) -> None:
                    if observed != action:
                        return
                    target = parent.path / name
                    target.unlink()
                    target.write_bytes(foreign)

                filesystem = LocalLaunchdFilesystem(before_mutation=replace_destination)
                parent = filesystem.open_parent(parent_path, filesystem.inspect(parent_path))
                snapshot = filesystem.read_snapshot(destination, 1024, parent)
                try:
                    with self.assertRaises(OSError):
                        if action == "write":
                            filesystem.write_atomic(destination, b"managed-new", snapshot.state, parent)
                        elif action == "backup":
                            filesystem.create_unique_backup(destination, snapshot.data, snapshot.state, parent)
                        elif action == "move":
                            filesystem.move_to_unique_backup(destination, snapshot.state, parent)
                        else:
                            filesystem.remove(destination, snapshot.state, parent)
                    self.assertEqual(foreign, destination.read_bytes())
                    self.assertEqual([], list(parent_path.glob("*.cbr-*")))
                finally:
                    filesystem.close_parent(parent)

        with tempfile.TemporaryDirectory() as tmp:
            parent_path = Path(tmp) / "LaunchAgents"
            parent_path.mkdir()
            destination = parent_path / "managed.plist"

            def create_destination(action: str, parent: ParentHandle, name: str) -> None:
                if action == "write":
                    (parent.path / name).write_bytes(foreign)

            filesystem = LocalLaunchdFilesystem(before_mutation=create_destination)
            parent = filesystem.open_parent(parent_path, filesystem.inspect(parent_path))
            try:
                with self.assertRaises(OSError):
                    filesystem.write_atomic(destination, b"managed-new", PathState("absent"), parent)
                self.assertEqual(foreign, destination.read_bytes())
            finally:
                filesystem.close_parent(parent)

        for action in ("restore", "discard"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                parent_path = Path(tmp) / "LaunchAgents"
                parent_path.mkdir()
                destination = parent_path / "managed.plist"
                backup_path = parent_path / ".managed.plist.cbr-backup-test"
                destination.write_bytes(b"managed-new")
                backup_path.write_bytes(b"managed-old")

                def replace_selected(observed: str, parent: ParentHandle, name: str) -> None:
                    if observed != action:
                        return
                    target = parent.path / name
                    target.unlink()
                    target.write_bytes(foreign)

                filesystem = LocalLaunchdFilesystem(before_mutation=replace_selected)
                parent = filesystem.open_parent(parent_path, filesystem.inspect(parent_path))
                destination_snapshot = filesystem.read_snapshot(destination, 1024, parent)
                backup_snapshot = filesystem.read_snapshot(backup_path, 1024, parent)
                backup = BackupSnapshot(backup_path, backup_snapshot.state, backup_snapshot.data)
                try:
                    with self.assertRaises(OSError):
                        if action == "restore":
                            filesystem.restore_backup_atomic(backup, destination, destination_snapshot.state, parent)
                        else:
                            filesystem.discard_backup(backup, parent)
                    selected = destination if action == "restore" else backup_path
                    self.assertEqual(foreign, selected.read_bytes())
                    if action == "restore":
                        self.assertEqual(b"managed-old", backup_path.read_bytes())
                finally:
                    filesystem.close_parent(parent)

    @unittest.skipUnless(platform.system() == "Darwin", "requires Darwin renameatx_np")
    def test_local_parent_swap_after_final_check_does_not_touch_replacement_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_path = root / "LaunchAgents"
            parent_path.mkdir()
            destination = parent_path / "managed.plist"
            destination.write_bytes(b"managed-old")
            replacement = b"foreign-parent-entry"

            def replace_parent(action: str, parent: ParentHandle, name: str) -> None:
                if action != "write":
                    return
                parent.path.rename(root / "LaunchAgents-old")
                parent.path.mkdir()
                (parent.path / name).write_bytes(replacement)

            filesystem = LocalLaunchdFilesystem(before_mutation=replace_parent)
            parent = filesystem.open_parent(parent_path, filesystem.inspect(parent_path))
            snapshot = filesystem.read_snapshot(destination, 1024, parent)
            try:
                with self.assertRaises(OSError):
                    filesystem.write_atomic(destination, b"managed-new", snapshot.state, parent)
                self.assertEqual(replacement, (parent_path / destination.name).read_bytes())
            finally:
                filesystem.close_parent(parent)

    @unittest.skipUnless(platform.system() == "Darwin", "requires Darwin renameatx_np")
    def test_local_temp_cleanup_race_does_not_unlink_foreign_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent_path = Path(tmp) / "LaunchAgents"
            parent_path.mkdir()
            destination = parent_path / "managed.plist"
            destination.write_bytes(b"managed-old")
            foreign = b"foreign-cleanup-entry"

            def replace_destination_and_temp(action: str, parent: ParentHandle, name: str) -> None:
                target = parent.path / name
                if action == "write":
                    target.unlink()
                    target.write_bytes(b"foreign-destination")
                elif action == "cleanup":
                    target.unlink()
                    target.write_bytes(foreign)

            filesystem = LocalLaunchdFilesystem(before_mutation=replace_destination_and_temp)
            parent = filesystem.open_parent(parent_path, filesystem.inspect(parent_path))
            snapshot = filesystem.read_snapshot(destination, 1024, parent)
            try:
                with self.assertRaises(OSError):
                    filesystem.write_atomic(destination, b"managed-new", snapshot.state, parent)
                self.assertEqual(b"foreign-destination", destination.read_bytes())
                self.assertIn(foreign, [path.read_bytes() for path in parent_path.iterdir()])
            finally:
                filesystem.close_parent(parent)

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
        report = lifecycle_result_report(result)
        self.assertFalse(report["mutation_allowed"])
        self.assertFalse(report["intended_plist_included"])
        self.assertNotIn("intended_plist", report)
        self.assertNotIn(plan_input().working_directory, repr(report))

    def test_operation_report_does_not_trust_executor_reason_text(self) -> None:
        filesystem = FakeFilesystem()
        result = run_launchd_install(
            plan_input(), DESTINATION, apply=True, confirm_label=LABEL,
            environment=environment(), filesystem=filesystem, launchctl=FakeLaunchctl([True]),
        )
        untrusted = replace(
            result,
            launchctl_results=(LaunchctlResult("bootstrap", False, 7, "UNTRUSTED_ADAPTER_DETAIL"),),
        )

        report = lifecycle_result_report(untrusted)

        self.assertEqual("launchctl command failed", report["launchctl"][0]["reason"])
        self.assertNotIn("UNTRUSTED_ADAPTER_DETAIL", repr(report))

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
        self.assertEqual(
            ["read", "backup", "write", "discard"],
            [
                c
                for c in success_fs.calls
                if not c.startswith("inspect") and c not in {"assert", "open_parent", "close_parent"}
            ],
        )
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

    def test_destination_and_parent_races_block_before_bootout(self) -> None:
        old = render_launchd_plist(plan_input(interval=300))
        for race_target in ("destination_foreign", "parent_symlink"):
            filesystem = FakeFilesystem(old)
            filesystem.race_at_assert = 2
            filesystem.race_target = race_target
            launchctl = FakeLaunchctl()

            result = run_launchd_install(
                plan_input(), DESTINATION, apply=True, confirm_label=LABEL,
                environment=environment(), filesystem=filesystem, launchctl=launchctl,
            )

            with self.subTest(race_target=race_target):
                self.assertEqual("failed", result.status)
                self.assertIn("identity changed before bootout", result.reason)
                self.assertEqual([], launchctl.calls)
                self.assertNotIn("write", filesystem.calls)
                if race_target == "destination_foreign":
                    self.assertEqual(
                        {"Label": "com.example.foreign"},
                        plistlib.loads(filesystem.files[DESTINATION]),
                    )
                else:
                    self.assertEqual("symlink", filesystem.kinds[DESTINATION.parent])

    def test_destination_race_after_bootout_is_not_replaced_or_rebootstrapped(self) -> None:
        old = render_launchd_plist(plan_input(interval=300))
        filesystem = FakeFilesystem(old)
        filesystem.race_at_assert = 3
        filesystem.race_target = "destination_foreign"
        launchctl = FakeLaunchctl([True])

        result = run_launchd_install(
            plan_input(), DESTINATION, apply=True, confirm_label=LABEL,
            environment=environment(), filesystem=filesystem, launchctl=launchctl,
        )

        self.assertEqual("recovery_required", result.status)
        self.assertEqual(["bootout"], [call.split(":", 1)[0] for call in launchctl.calls])
        self.assertEqual({"Label": "com.example.foreign"}, plistlib.loads(filesystem.files[DESTINATION]))

    def test_install_remove_race_retains_replacement_without_touching_it(self) -> None:
        filesystem = FakeFilesystem()
        filesystem.race_at_assert = 3
        filesystem.race_target = "destination_foreign"
        launchctl = FakeLaunchctl([False])

        result = run_launchd_install(
            plan_input(), DESTINATION, apply=True, confirm_label=LABEL,
            environment=environment(), filesystem=filesystem, launchctl=launchctl,
        )

        self.assertEqual("recovery_required", result.status)
        self.assertEqual(["bootstrap"], [call.split(":", 1)[0] for call in launchctl.calls])
        self.assertEqual({"Label": "com.example.foreign"}, plistlib.loads(filesystem.files[DESTINATION]))

    def test_uninstall_move_race_does_not_move_or_rebootstrap_replacement(self) -> None:
        managed = render_launchd_plist(plan_input())
        filesystem = FakeFilesystem(managed)
        filesystem.race_at_assert = 2
        filesystem.race_target = "destination_symlink"
        launchctl = FakeLaunchctl([True])

        result = run_launchd_uninstall(
            label=LABEL, config_path=CONFIG_PATH, config_source="cli", destination=DESTINATION,
            apply=True, confirm_label=LABEL, environment=environment(), filesystem=filesystem,
            launchctl=launchctl,
        )

        self.assertEqual("recovery_required", result.status)
        self.assertEqual(["bootout"], [call.split(":", 1)[0] for call in launchctl.calls])
        self.assertEqual("symlink", filesystem.kinds[DESTINATION])
        self.assertFalse(any(path.name.startswith(f".{DESTINATION.name}.cbr-backup") for path in filesystem.files))


if __name__ == "__main__":
    unittest.main()

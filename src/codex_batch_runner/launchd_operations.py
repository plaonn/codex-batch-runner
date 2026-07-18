"""Guarded macOS user LaunchAgent lifecycle with injectable side-effect seams."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .launchd_lifecycle import (
    LaunchdPlanInput,
    inspect_launchd_plist,
    plan_launchd_lifecycle,
    validate_launchd_label,
)


MAX_MANAGED_PLIST_BYTES = 1024 * 1024
LAUNCHCTL_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class LaunchdEnvironment:
    platform_name: str
    uid: int
    home: Path
    user_domain: str


@dataclass(frozen=True)
class PathState:
    kind: str
    size: int | None = None
    device: int | None = None
    inode: int | None = None
    modified_ns: int | None = None


@dataclass(frozen=True)
class FileSnapshot:
    data: bytes
    state: PathState


@dataclass(frozen=True)
class BackupSnapshot:
    path: Path
    state: PathState
    data: bytes


@dataclass(frozen=True)
class ParentHandle:
    path: Path
    state: PathState
    token: object


class IdentityChangedError(OSError):
    """Raised before mutation when a guarded path no longer matches its snapshot."""


@dataclass(frozen=True)
class LaunchctlResult:
    action: str
    ok: bool
    returncode: int | None
    reason: str


@dataclass(frozen=True)
class LaunchdLifecycleResult:
    operation: str
    mode: str
    status: str
    action: str
    reason: str
    destination: Path
    config_source: str
    config_path: str
    changed: bool
    backup_path: Path | None = None
    backup_retained: bool = False
    recovery_attempted: bool = False
    recovery_succeeded: bool | None = None
    launchctl_results: tuple[LaunchctlResult, ...] = ()


class LaunchdFilesystem(Protocol):
    def inspect(self, path: Path) -> PathState: ...

    def open_parent(self, path: Path, expected: PathState) -> ParentHandle: ...

    def close_parent(self, parent: ParentHandle) -> None: ...

    def read_snapshot(self, path: Path, max_bytes: int, parent: ParentHandle) -> FileSnapshot: ...

    def assert_unchanged(self, path: Path, expected: PathState, parent: ParentHandle) -> None: ...

    def write_atomic(
        self,
        path: Path,
        data: bytes,
        expected: PathState,
        parent: ParentHandle,
    ) -> PathState: ...

    def create_unique_backup(
        self,
        path: Path,
        data: bytes,
        expected: PathState,
        parent: ParentHandle,
    ) -> BackupSnapshot: ...

    def move_to_unique_backup(
        self,
        path: Path,
        expected: PathState,
        parent: ParentHandle,
    ) -> BackupSnapshot: ...

    def restore_backup_atomic(
        self,
        backup: BackupSnapshot,
        destination: Path,
        expected_destination: PathState,
        parent: ParentHandle,
    ) -> PathState: ...

    def remove(self, path: Path, expected: PathState, parent: ParentHandle) -> None: ...

    def discard_backup(self, backup: BackupSnapshot, parent: ParentHandle) -> None: ...


class LaunchctlExecutor(Protocol):
    def bootstrap(self, user_domain: str, plist_path: Path) -> LaunchctlResult: ...

    def bootout(self, user_domain: str, plist_path: Path) -> LaunchctlResult: ...


class LocalLaunchdFilesystem:
    """Directory-fd anchored filesystem implementation for guarded lifecycle apply."""

    def __init__(self, before_mutation: Callable[[str, ParentHandle, str], None] | None = None) -> None:
        self._before_mutation = before_mutation
        self._rename = _DarwinAtomicRename()

    def inspect(self, path: Path) -> PathState:
        try:
            return _state_from_stat(path.lstat())
        except FileNotFoundError:
            return PathState("absent")

    def open_parent(self, path: Path, expected: PathState) -> ParentHandle:
        _require_dir_fd_support()
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
        fd = os.open(path, flags)
        try:
            state = _state_from_stat(os.fstat(fd))
            _require_identity(state, expected, "LaunchAgents directory")
            return ParentHandle(path, state, fd)
        except Exception:
            os.close(fd)
            raise

    def close_parent(self, parent: ParentHandle) -> None:
        os.close(_parent_fd(parent))

    def read_snapshot(self, path: Path, max_bytes: int, parent: ParentHandle) -> FileSnapshot:
        self._assert_parent_current(parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=_parent_fd(parent))
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise IdentityChangedError("LaunchAgent destination is not a regular file")
            if before.st_size > max_bytes:
                raise ValueError("LaunchAgent destination exceeds size limit")
            data = _read_bounded_fd(fd, max_bytes)
            state = _state_from_stat(os.fstat(fd))
            if _state_from_stat(before) != state or len(data) != state.size:
                raise IdentityChangedError("LaunchAgent destination changed while being read")
            return FileSnapshot(data, state)
        finally:
            os.close(fd)

    def assert_unchanged(self, path: Path, expected: PathState, parent: ParentHandle) -> None:
        self._assert_parent_current(parent)
        actual = self._inspect_entry(parent, path.name)
        _require_identity(actual, expected, "LaunchAgent destination")
        self._assert_parent_current(parent)

    def write_atomic(
        self,
        path: Path,
        data: bytes,
        expected: PathState,
        parent: ParentHandle,
    ) -> PathState:
        temp_name, temp_state = self._create_file(parent, f".{path.name}.tmp-", data)
        try:
            self.assert_unchanged(path, expected, parent)
            self._inject("write", parent, path.name)
            if expected.kind == "absent":
                self._rename.exclusive(parent, temp_name, path.name)
                try:
                    self._assert_parent_current(parent)
                except OSError:
                    self._rename.exclusive(parent, path.name, temp_name)
                    raise
            else:
                self._rename.swap(parent, temp_name, path.name)
                moved = self._inspect_entry(parent, temp_name)
                if not _identity_matches(moved, expected):
                    self._rename.swap(parent, temp_name, path.name)
                    raise IdentityChangedError("destination changed immediately before replacement")
                try:
                    self._assert_parent_current(parent)
                except OSError:
                    self._rename.swap(parent, temp_name, path.name)
                    raise
                self._unlink_exact(parent, temp_name, expected)
            _fsync_parent(parent)
            snapshot = self.read_snapshot(path, len(data), parent)
            if snapshot.data != data:
                raise IdentityChangedError("atomic replacement bytes changed unexpectedly")
            return snapshot.state
        except Exception:
            self._unlink_exact(parent, temp_name, temp_state, tolerate_missing=True)
            raise

    def create_unique_backup(
        self,
        path: Path,
        data: bytes,
        expected: PathState,
        parent: ParentHandle,
    ) -> BackupSnapshot:
        self.assert_unchanged(path, expected, parent)
        self._inject("backup", parent, path.name)
        name, state = self._create_file(parent, f".{path.name}.cbr-backup-", data)
        backup = path.with_name(name)
        try:
            self.assert_unchanged(path, expected, parent)
            snapshot = self.read_snapshot(backup, len(data), parent)
            if snapshot.data != data:
                raise IdentityChangedError("backup bytes changed unexpectedly")
            return BackupSnapshot(backup, snapshot.state, data)
        except Exception:
            self._unlink_exact(parent, name, state, tolerate_missing=True)
            raise

    def move_to_unique_backup(
        self,
        path: Path,
        expected: PathState,
        parent: ParentHandle,
    ) -> BackupSnapshot:
        source = self.read_snapshot(path, MAX_MANAGED_PLIST_BYTES, parent)
        _require_identity(source.state, expected, "LaunchAgent destination")
        backup_name = _unique_backup_path(path).name
        self.assert_unchanged(path, expected, parent)
        self._inject("move", parent, path.name)
        self._rename.exclusive(parent, path.name, backup_name)
        moved = self._inspect_entry(parent, backup_name)
        if not _identity_matches(moved, expected):
            self._rename.exclusive(parent, backup_name, path.name)
            raise IdentityChangedError("destination changed immediately before uninstall move")
        try:
            self._assert_parent_current(parent)
            snapshot = self.read_snapshot(path.with_name(backup_name), MAX_MANAGED_PLIST_BYTES, parent)
            self.assert_unchanged(path, PathState("absent"), parent)
            _fsync_parent(parent)
            return BackupSnapshot(path.with_name(backup_name), snapshot.state, snapshot.data)
        except Exception:
            self._rename.exclusive(parent, backup_name, path.name)
            raise

    def restore_backup_atomic(
        self,
        backup: BackupSnapshot,
        destination: Path,
        expected_destination: PathState,
        parent: ParentHandle,
    ) -> PathState:
        self.assert_unchanged(destination, expected_destination, parent)
        self.assert_unchanged(backup.path, backup.state, parent)
        self._inject("restore", parent, destination.name)
        self._rename.swap(parent, backup.path.name, destination.name)
        displaced = self._inspect_entry(parent, backup.path.name)
        restored = self._inspect_entry(parent, destination.name)
        if not _identity_matches(displaced, expected_destination) or not _identity_matches(restored, backup.state):
            self._rename.swap(parent, backup.path.name, destination.name)
            raise IdentityChangedError("restore targets changed immediately before atomic swap")
        try:
            self._assert_parent_current(parent)
        except OSError:
            self._rename.swap(parent, backup.path.name, destination.name)
            raise
        self._unlink_exact(parent, backup.path.name, expected_destination)
        _fsync_parent(parent)
        snapshot = self.read_snapshot(destination, MAX_MANAGED_PLIST_BYTES, parent)
        if snapshot.data != backup.data:
            raise IdentityChangedError("restore did not preserve backup bytes")
        return snapshot.state

    def remove(self, path: Path, expected: PathState, parent: ParentHandle) -> None:
        self._guarded_unlink(path, expected, parent, "remove")

    def discard_backup(self, backup: BackupSnapshot, parent: ParentHandle) -> None:
        self._guarded_unlink(backup.path, backup.state, parent, "discard")

    def _guarded_unlink(self, path: Path, expected: PathState, parent: ParentHandle, action: str) -> None:
        quarantine = f".{path.name}.cbr-remove-{uuid.uuid4().hex}"
        self.assert_unchanged(path, expected, parent)
        self._inject(action, parent, path.name)
        self._rename.exclusive(parent, path.name, quarantine)
        moved = self._inspect_entry(parent, quarantine)
        if not _identity_matches(moved, expected):
            self._rename.exclusive(parent, quarantine, path.name)
            raise IdentityChangedError("guarded file changed immediately before removal")
        try:
            self._assert_parent_current(parent)
        except OSError:
            self._rename.exclusive(parent, quarantine, path.name)
            raise
        self._unlink_exact(parent, quarantine, expected)
        _fsync_parent(parent)

    def _create_file(self, parent: ParentHandle, prefix: str, data: bytes) -> tuple[str, PathState]:
        for _ in range(100):
            name = f"{prefix}{uuid.uuid4().hex}"
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=_parent_fd(parent),
                )
            except FileExistsError:
                continue
            state: PathState | None = None
            try:
                _write_all_fd(fd, data)
                os.fsync(fd)
                state = _state_from_stat(os.fstat(fd))
            except Exception:
                state = _state_from_stat(os.fstat(fd))
                os.close(fd)
                self._unlink_exact(parent, name, state, tolerate_missing=True)
                raise
            else:
                os.close(fd)
            _fsync_parent(parent)
            return name, state
        raise OSError("could not allocate unique launchd temporary path")

    def _unlink_exact(
        self,
        parent: ParentHandle,
        name: str,
        expected: PathState,
        *,
        tolerate_missing: bool = False,
    ) -> None:
        actual = self._inspect_entry(parent, name)
        if tolerate_missing and actual.kind == "absent":
            return
        _require_identity(actual, expected, "temporary launchd file")
        self._inject("cleanup", parent, name)
        quarantine = f".{name}.cbr-cleanup-{uuid.uuid4().hex}"
        self._rename.exclusive(parent, name, quarantine)
        moved = self._inspect_entry(parent, quarantine)
        if not _identity_matches(moved, expected):
            self._rename.exclusive(parent, quarantine, name)
            raise IdentityChangedError("temporary path changed immediately before cleanup")
        os.unlink(quarantine, dir_fd=_parent_fd(parent))

    def _inspect_entry(self, parent: ParentHandle, name: str) -> PathState:
        try:
            value = os.stat(name, dir_fd=_parent_fd(parent), follow_symlinks=False)
        except FileNotFoundError:
            return PathState("absent")
        return _state_from_stat(value)

    def _assert_parent_current(self, parent: ParentHandle) -> None:
        _require_identity(_state_from_stat(os.fstat(_parent_fd(parent))), parent.state, "held LaunchAgents directory")
        current = _open_identity_no_follow(parent.path, "directory")
        _require_identity(current, parent.state, "LaunchAgents directory")

    def _inject(self, action: str, parent: ParentHandle, name: str) -> None:
        if self._before_mutation is not None:
            self._before_mutation(action, parent, name)


class SubprocessLaunchctlExecutor:
    """Bounded argv-only launchctl adapter with sanitized results."""

    def bootstrap(self, user_domain: str, plist_path: Path) -> LaunchctlResult:
        return self._run("bootstrap", ["launchctl", "bootstrap", user_domain, str(plist_path)])

    def bootout(self, user_domain: str, plist_path: Path) -> LaunchctlResult:
        return self._run("bootout", ["launchctl", "bootout", user_domain, str(plist_path)])

    def _run(self, action: str, argv: list[str]) -> LaunchctlResult:
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=LAUNCHCTL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return LaunchctlResult(action, False, None, "launchctl command timed out")
        except OSError:
            return LaunchctlResult(action, False, None, "launchctl executable unavailable")
        if result.returncode != 0:
            return LaunchctlResult(action, False, result.returncode, "launchctl command failed")
        return LaunchctlResult(action, True, 0, "launchctl command succeeded")


def current_launchd_environment(user_domain: str) -> LaunchdEnvironment:
    return LaunchdEnvironment(
        platform_name=platform.system(),
        uid=os.getuid(),
        home=Path.home().expanduser().resolve(),
        user_domain=user_domain,
    )


def run_launchd_install(
    plan_input: LaunchdPlanInput,
    destination: Path,
    *,
    apply: bool = False,
    confirm_label: str | None = None,
    environment: LaunchdEnvironment,
    filesystem: LaunchdFilesystem | None = None,
    launchctl: LaunchctlExecutor | None = None,
) -> LaunchdLifecycleResult:
    filesystem = filesystem or LocalLaunchdFilesystem()
    launchctl = launchctl or SubprocessLaunchctlExecutor()
    config_block = _config_identity_block_reason(plan_input.config_path, plan_input.config_provenance)
    if config_block:
        return _result(
            operation="install",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=config_block,
            destination=destination,
            plan_input=plan_input,
        )
    confirmation_block = _confirmation_block_reason(apply, confirm_label, plan_input.label)
    if confirmation_block:
        return _result(
            operation="install",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=confirmation_block,
            destination=destination,
            plan_input=plan_input,
        )
    destination, parent, blocked = _validated_destination(
        destination,
        label=plan_input.label,
        environment=environment,
        filesystem=filesystem,
    )
    if blocked:
        return _result(
            operation="install",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=blocked,
            destination=destination,
            plan_input=plan_input,
        )
    try:
        return _run_launchd_install_with_parent(
            plan_input,
            destination,
            parent,
            apply,
            filesystem,
            launchctl,
            environment,
        )
    finally:
        filesystem.close_parent(parent)


def _run_launchd_install_with_parent(
    plan_input: LaunchdPlanInput,
    destination: Path,
    parent: ParentHandle,
    apply: bool,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
    existing, blocked = _read_existing_managed_candidate(destination, parent, filesystem)
    if blocked:
        return _result(
            operation="install",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=blocked,
            destination=destination,
            plan_input=plan_input,
        )
    plan = plan_launchd_lifecycle(plan_input, existing.data if existing else None)
    if plan.action == "blocked":
        return _result(
            operation="install",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=plan.reason,
            destination=destination,
            plan_input=plan_input,
        )
    if plan.action == "none":
        return _result(
            operation="install",
            apply=apply,
            status="noop",
            action="none",
            reason=plan.reason,
            destination=destination,
            plan_input=plan_input,
        )
    action = "install" if plan.action == "create" else "update"
    if not apply:
        return _result(
            operation="install",
            apply=False,
            status="planned",
            action=action,
            reason=plan.reason,
            destination=destination,
            plan_input=plan_input,
        )
    if action == "install":
        return _apply_install(
            plan_input,
            destination,
            plan.rendered_plist,
            parent,
            filesystem,
            launchctl,
            environment,
        )
    if existing is None:
        raise AssertionError("update requires an existing snapshot")
    return _apply_update(
        plan_input,
        destination,
        plan.rendered_plist,
        existing,
        parent,
        filesystem,
        launchctl,
        environment,
    )


def run_launchd_uninstall(
    *,
    label: str,
    config_path: str,
    config_source: str,
    destination: Path,
    apply: bool = False,
    confirm_label: str | None = None,
    environment: LaunchdEnvironment,
    filesystem: LaunchdFilesystem | None = None,
    launchctl: LaunchctlExecutor | None = None,
) -> LaunchdLifecycleResult:
    filesystem = filesystem or LocalLaunchdFilesystem()
    launchctl = launchctl or SubprocessLaunchctlExecutor()
    validate_launchd_label(label)
    config_block = _config_identity_block_reason(config_path, config_source)
    base_input = _identity_plan_input(label, config_path, config_source)
    if config_block:
        return _result(
            operation="uninstall",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=config_block,
            destination=destination,
            plan_input=base_input,
        )
    confirmation_block = _confirmation_block_reason(apply, confirm_label, label)
    if confirmation_block:
        return _result(
            operation="uninstall",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=confirmation_block,
            destination=destination,
            plan_input=base_input,
        )
    destination, parent, blocked = _validated_destination(
        destination,
        label=label,
        environment=environment,
        filesystem=filesystem,
    )
    if blocked:
        return _result(
            operation="uninstall",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=blocked,
            destination=destination,
            plan_input=base_input,
        )
    try:
        return _run_launchd_uninstall_with_parent(
            base_input,
            label,
            config_path,
            destination,
            parent,
            apply,
            filesystem,
            launchctl,
            environment,
        )
    finally:
        filesystem.close_parent(parent)


def _run_launchd_uninstall_with_parent(
    base_input: LaunchdPlanInput,
    label: str,
    config_path: str,
    destination: Path,
    parent: ParentHandle,
    apply: bool,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
    existing, blocked = _read_existing_managed_candidate(destination, parent, filesystem)
    if blocked:
        return _result(
            operation="uninstall",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=blocked,
            destination=destination,
            plan_input=base_input,
        )
    if existing is None:
        return _result(
            operation="uninstall",
            apply=apply,
            status="noop",
            action="none",
            reason="managed plist is not installed",
            destination=destination,
            plan_input=base_input,
        )
    inspection = inspect_launchd_plist(existing.data)
    if inspection.status != "managed":
        return _result(
            operation="uninstall",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=inspection.reason,
            destination=destination,
            plan_input=base_input,
        )
    fields = inspection.managed_fields or {}
    if fields.get("label") != label or fields.get("config_path") != config_path:
        return _result(
            operation="uninstall",
            apply=apply,
            status="blocked",
            action="blocked",
            reason="managed plist label or config path does not match selected identity",
            destination=destination,
            plan_input=base_input,
        )
    if not apply:
        return _result(
            operation="uninstall",
            apply=False,
            status="planned",
            action="uninstall",
            reason="valid CBR-owned plist is eligible for guarded uninstall",
            destination=destination,
            plan_input=base_input,
        )
    return _apply_uninstall(
        base_input,
        destination,
        existing,
        parent,
        filesystem,
        launchctl,
        environment,
    )


def lifecycle_result_report(result: LaunchdLifecycleResult) -> dict[str, Any]:
    return {
        "contract": "launchd-lifecycle-operation-v1",
        "operation": result.operation,
        "mode": result.mode,
        "status": result.status,
        "action": result.action,
        "reason": result.reason,
        "mutation_allowed": result.mode == "apply" and result.status != "blocked",
        "changed": result.changed,
        "destination": str(result.destination),
        "config": {"source": result.config_source, "path": result.config_path},
        "backup": {
            "path": str(result.backup_path) if result.backup_path else None,
            "retained": result.backup_retained,
        },
        "recovery": {
            "attempted": result.recovery_attempted,
            "succeeded": result.recovery_succeeded,
        },
        "launchctl": [
            {
                "action": item.action,
                "ok": item.ok,
                "returncode": item.returncode,
                "reason": _safe_launchctl_reason(item),
            }
            for item in result.launchctl_results
        ],
        "intended_plist_included": False,
    }


def _apply_install(
    plan_input: LaunchdPlanInput,
    destination: Path,
    rendered: bytes,
    parent: ParentHandle,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
    try:
        installed_state = filesystem.write_atomic(
            destination,
            rendered,
            PathState("absent"),
            parent,
        )
    except OSError:
        return _result(
            "install",
            True,
            "failed",
            "install",
            "atomic plist write failed or guarded path identity changed",
            destination,
            plan_input,
        )
    try:
        filesystem.assert_unchanged(destination, installed_state, parent)
    except OSError:
        return _result(
            "install",
            True,
            "recovery_required",
            "install",
            "installed plist identity changed before bootstrap; no launchctl action attempted",
            destination,
            plan_input,
            changed=True,
            recovery_attempted=False,
            recovery_succeeded=False,
        )
    command = launchctl.bootstrap(environment.user_domain, destination)
    if command.ok:
        return _result(
            "install",
            True,
            "applied",
            "install",
            "managed plist installed and bootstrapped",
            destination,
            plan_input,
            changed=True,
            launchctl_results=(command,),
        )
    try:
        filesystem.remove(destination, installed_state, parent)
    except OSError:
        return _result(
            "install",
            True,
            "recovery_required",
            "install",
            "bootstrap failed and installed plist could not be removed",
            destination,
            plan_input,
            changed=True,
            recovery_attempted=True,
            recovery_succeeded=False,
            launchctl_results=(command,),
        )
    return _result(
        "install",
        True,
        "recovered",
        "install",
        "bootstrap failed; newly installed plist was removed",
        destination,
        plan_input,
        recovery_attempted=True,
        recovery_succeeded=True,
        launchctl_results=(command,),
    )


def _apply_update(
    plan_input: LaunchdPlanInput,
    destination: Path,
    rendered: bytes,
    existing: FileSnapshot,
    parent: ParentHandle,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
    try:
        backup = filesystem.create_unique_backup(
            destination,
            existing.data,
            existing.state,
            parent,
        )
    except OSError:
        return _result(
            "install",
            True,
            "failed",
            "update",
            "backup creation failed or guarded path identity changed",
            destination,
            plan_input,
        )
    try:
        filesystem.assert_unchanged(destination, existing.state, parent)
    except OSError:
        retained = not _discard_backup(filesystem, backup, parent)
        return _result(
            "install",
            True,
            "failed",
            "update",
            "guarded path identity changed before bootout; no launchctl action attempted",
            destination,
            plan_input,
            backup_path=backup.path if retained else None,
            backup_retained=retained,
        )
    bootout = launchctl.bootout(environment.user_domain, destination)
    if not bootout.ok:
        retained = not _discard_backup(filesystem, backup, parent)
        return _result(
            "install",
            True,
            "failed",
            "update",
            "bootout failed before managed plist replacement",
            destination,
            plan_input,
            backup_path=backup.path if retained else None,
            backup_retained=retained,
            launchctl_results=(bootout,),
        )
    try:
        updated_state = filesystem.write_atomic(
            destination,
            rendered,
            existing.state,
            parent,
        )
    except OSError:
        return _recover_update(
            plan_input,
            destination,
            rendered,
            backup,
            existing.state,
            parent,
            filesystem,
            launchctl,
            environment,
            (bootout,),
            "atomic replacement failed",
        )
    try:
        filesystem.assert_unchanged(destination, updated_state, parent)
    except OSError:
        return _recover_update(
            plan_input,
            destination,
            rendered,
            backup,
            updated_state,
            parent,
            filesystem,
            launchctl,
            environment,
            (bootout,),
            "updated plist identity changed before bootstrap",
        )
    bootstrap = launchctl.bootstrap(environment.user_domain, destination)
    if not bootstrap.ok:
        return _recover_update(
            plan_input,
            destination,
            rendered,
            backup,
            updated_state,
            parent,
            filesystem,
            launchctl,
            environment,
            (bootout, bootstrap),
            "bootstrap of updated plist failed",
        )
    retained = not _discard_backup(filesystem, backup, parent)
    return _result(
        "install",
        True,
        "applied",
        "update",
        "managed plist updated and bootstrapped",
        destination,
        plan_input,
        changed=True,
        backup_path=backup.path if retained else None,
        backup_retained=retained,
        launchctl_results=(bootout, bootstrap),
    )


def _recover_update(
    plan_input: LaunchdPlanInput,
    destination: Path,
    rendered: bytes,
    backup: BackupSnapshot,
    expected_destination: PathState,
    parent: ParentHandle,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
    commands: tuple[LaunchctlResult, ...],
    failure_reason: str,
) -> LaunchdLifecycleResult:
    try:
        restored_state = filesystem.restore_backup_atomic(
            backup,
            destination,
            expected_destination,
            parent,
        )
    except OSError:
        return _result(
            "install",
            True,
            "recovery_required",
            "update",
            f"{failure_reason}; previous plist restore failed",
            destination,
            plan_input,
            changed=True,
            backup_path=backup.path,
            backup_retained=True,
            recovery_attempted=True,
            recovery_succeeded=False,
            launchctl_results=commands,
        )
    try:
        filesystem.assert_unchanged(destination, restored_state, parent)
    except OSError:
        return _result(
            "install",
            True,
            "recovery_required",
            "update",
            f"{failure_reason}; previous plist restored but identity changed before re-bootstrap",
            destination,
            plan_input,
            recovery_attempted=True,
            recovery_succeeded=False,
            launchctl_results=commands,
        )
    restore_bootstrap = launchctl.bootstrap(environment.user_domain, destination)
    all_commands = (*commands, restore_bootstrap)
    if not restore_bootstrap.ok:
        return _result(
            "install",
            True,
            "recovery_required",
            "update",
            f"{failure_reason}; previous plist restored but re-bootstrap failed",
            destination,
            plan_input,
            recovery_attempted=True,
            recovery_succeeded=False,
            launchctl_results=all_commands,
        )
    return _result(
        "install",
        True,
        "recovered",
        "update",
        f"{failure_reason}; previous plist restored and re-bootstrapped",
        destination,
        plan_input,
        recovery_attempted=True,
        recovery_succeeded=True,
        launchctl_results=all_commands,
    )


def _apply_uninstall(
    plan_input: LaunchdPlanInput,
    destination: Path,
    existing: FileSnapshot,
    parent: ParentHandle,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
    try:
        filesystem.assert_unchanged(destination, existing.state, parent)
    except OSError:
        return _result(
            "uninstall",
            True,
            "failed",
            "uninstall",
            "guarded path identity changed before bootout; no launchctl action attempted",
            destination,
            plan_input,
        )
    bootout = launchctl.bootout(environment.user_domain, destination)
    if not bootout.ok:
        return _result(
            "uninstall",
            True,
            "failed",
            "uninstall",
            "bootout failed; managed plist was retained",
            destination,
            plan_input,
            launchctl_results=(bootout,),
        )
    try:
        backup = filesystem.move_to_unique_backup(destination, existing.state, parent)
    except OSError:
        try:
            filesystem.assert_unchanged(destination, existing.state, parent)
        except OSError:
            return _result(
                "uninstall",
                True,
                "recovery_required",
                "uninstall",
                "uninstall move failed and guarded identity changed; no re-bootstrap attempted",
                destination,
                plan_input,
                recovery_attempted=True,
                recovery_succeeded=False,
                launchctl_results=(bootout,),
            )
        restore_bootstrap = launchctl.bootstrap(environment.user_domain, destination)
        return _result(
            "uninstall",
            True,
            "recovered" if restore_bootstrap.ok else "recovery_required",
            "uninstall",
            "atomic uninstall move failed; existing plist re-bootstrapped"
            if restore_bootstrap.ok
            else "atomic uninstall move failed and existing plist re-bootstrap failed",
            destination,
            plan_input,
            recovery_attempted=True,
            recovery_succeeded=restore_bootstrap.ok,
            launchctl_results=(bootout, restore_bootstrap),
        )
    return _result(
        "uninstall",
        True,
        "applied",
        "uninstall",
        "managed plist booted out and moved to a recoverable backup",
        destination,
        plan_input,
        changed=True,
        backup_path=backup.path,
        backup_retained=True,
        launchctl_results=(bootout,),
    )


def _validated_destination(
    destination: Path,
    *,
    label: str,
    environment: LaunchdEnvironment,
    filesystem: LaunchdFilesystem,
) -> tuple[Path, ParentHandle, str | None]:
    validate_launchd_label(label)
    raw = destination.expanduser()
    unknown_parent = ParentHandle(raw.parent, PathState("unknown"), None)
    if environment.platform_name != "Darwin":
        return raw, unknown_parent, "launchd lifecycle is only available on macOS"
    if environment.uid <= 0:
        return raw, unknown_parent, "launchd lifecycle requires a non-root user"
    if environment.user_domain != f"gui/{environment.uid}":
        return raw, unknown_parent, f"user domain must exactly match gui/{environment.uid}"
    home = environment.home.expanduser().resolve()
    expected_parent = home / "Library" / "LaunchAgents"
    if not raw.is_absolute():
        return raw, unknown_parent, "LaunchAgent destination must be an explicit absolute path"
    if raw.parent != expected_parent or raw.name != f"{label}.plist":
        return raw, unknown_parent, "LaunchAgent destination must exactly match HOME/Library/LaunchAgents/LABEL.plist"
    parent_state = filesystem.inspect(expected_parent)
    if parent_state.kind != "directory":
        return raw, ParentHandle(expected_parent, parent_state, None), "user LaunchAgents directory must already exist and must not be a symlink"
    try:
        parent = filesystem.open_parent(expected_parent, parent_state)
    except OSError:
        return raw, ParentHandle(expected_parent, parent_state, None), "user LaunchAgents directory could not be opened with guarded directory-fd semantics"
    return raw, parent, None


def _read_existing_managed_candidate(
    destination: Path,
    parent: ParentHandle,
    filesystem: LaunchdFilesystem,
) -> tuple[FileSnapshot | None, str | None]:
    state = filesystem.inspect(destination)
    if state.kind == "absent":
        return None, None
    if state.kind != "file":
        return None, f"existing LaunchAgent destination is {state.kind}, not a regular file"
    if state.size is None or state.size > MAX_MANAGED_PLIST_BYTES:
        return None, f"existing LaunchAgent exceeds {MAX_MANAGED_PLIST_BYTES} byte limit"
    try:
        snapshot = filesystem.read_snapshot(destination, MAX_MANAGED_PLIST_BYTES, parent)
    except ValueError:
        return None, f"existing LaunchAgent exceeds {MAX_MANAGED_PLIST_BYTES} byte limit"
    except OSError:
        return None, "existing LaunchAgent is not a stable readable regular file"
    if snapshot.state.kind != "file":
        return None, "existing LaunchAgent is not a regular file"
    return snapshot, None


def _confirmation_block_reason(apply: bool, confirm_label: str | None, label: str) -> str | None:
    if apply and confirm_label != label:
        return "--apply requires --confirm-label matching the LaunchAgent label"
    if not apply and confirm_label is not None:
        return "--confirm-label is only valid with --apply"
    return None


def _config_identity_block_reason(config_path: str, config_source: str) -> str | None:
    if config_source not in {"cli", "environment", "xdg"}:
        return "config source must be cli, environment, or xdg"
    path = Path(config_path).expanduser()
    if not path.is_absolute() or str(path.resolve()) != config_path:
        return "config path must be resolved and absolute"
    return None


def _identity_plan_input(label: str, config_path: str, config_source: str) -> LaunchdPlanInput:
    return LaunchdPlanInput(
        label=label,
        executable_path="/managed/identity-only/cbr",
        config_path=config_path,
        config_provenance=config_source,
        working_directory="/managed/identity-only",
        stdout_path="/managed/identity-only/stdout.log",
        stderr_path="/managed/identity-only/stderr.log",
        environment_path="/usr/bin:/bin",
        start_interval_seconds=1,
    )


def _result(
    operation: str,
    apply: bool,
    status: str,
    action: str,
    reason: str,
    destination: Path,
    plan_input: LaunchdPlanInput,
    *,
    changed: bool = False,
    backup_path: Path | None = None,
    backup_retained: bool = False,
    recovery_attempted: bool = False,
    recovery_succeeded: bool | None = None,
    launchctl_results: tuple[LaunchctlResult, ...] = (),
) -> LaunchdLifecycleResult:
    return LaunchdLifecycleResult(
        operation=operation,
        mode="apply" if apply else "dry_run",
        status=status,
        action=action,
        reason=reason,
        destination=destination,
        config_source=plan_input.config_provenance,
        config_path=plan_input.config_path,
        changed=changed,
        backup_path=backup_path,
        backup_retained=backup_retained,
        recovery_attempted=recovery_attempted,
        recovery_succeeded=recovery_succeeded,
        launchctl_results=launchctl_results,
    )


def _discard_backup(
    filesystem: LaunchdFilesystem,
    backup: BackupSnapshot,
    parent: ParentHandle,
) -> bool:
    try:
        filesystem.discard_backup(backup, parent)
    except OSError:
        return False
    return True


def _unique_backup_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.cbr-backup-{uuid.uuid4().hex}")


def _state_from_stat(value: os.stat_result) -> PathState:
    if stat.S_ISREG(value.st_mode):
        kind = "file"
    elif stat.S_ISDIR(value.st_mode):
        kind = "directory"
    elif stat.S_ISLNK(value.st_mode):
        kind = "symlink"
    else:
        kind = "other"
    return PathState(kind, value.st_size, value.st_dev, value.st_ino, value.st_mtime_ns)


class _DarwinAtomicRename:
    RENAME_SWAP = 0x00000002
    RENAME_EXCL = 0x00000004

    def exclusive(self, parent: ParentHandle, source: str, destination: str) -> None:
        self._call(parent, source, destination, self.RENAME_EXCL)

    def swap(self, parent: ParentHandle, source: str, destination: str) -> None:
        self._call(parent, source, destination, self.RENAME_SWAP)

    def _call(self, parent: ParentHandle, source: str, destination: str, flags: int) -> None:
        if platform.system() != "Darwin":
            raise OSError(errno.ENOTSUP, "guarded atomic rename requires Darwin renameatx_np")
        function = getattr(ctypes.CDLL(None, use_errno=True), "renameatx_np", None)
        if function is None:
            raise OSError(errno.ENOTSUP, "renameatx_np is unavailable")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        fd = _parent_fd(parent)
        if function(fd, os.fsencode(source), fd, os.fsencode(destination), flags) != 0:
            error = ctypes.get_errno()
            if flags == self.RENAME_EXCL and error in {errno.EEXIST, errno.ENOTEMPTY}:
                raise IdentityChangedError("atomic exclusive rename destination appeared")
            raise OSError(error, os.strerror(error))


def _require_dir_fd_support() -> None:
    required_flags = ("O_CLOEXEC", "O_NOFOLLOW", "O_DIRECTORY")
    if any(not hasattr(os, name) for name in required_flags):
        raise OSError(errno.ENOTSUP, "required no-follow directory flags are unavailable")
    if not all(function in os.supports_dir_fd for function in (os.open, os.stat, os.unlink)):
        raise OSError(errno.ENOTSUP, "required directory-fd operations are unavailable")


def _parent_fd(parent: ParentHandle) -> int:
    if not isinstance(parent.token, int) or parent.token < 0:
        raise OSError(errno.EBADF, "guarded parent directory handle is unavailable")
    return parent.token


def _read_bounded_fd(fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise ValueError("LaunchAgent destination exceeds size limit")
    return data


def _write_all_fd(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short write while creating guarded launchd file")
        offset += written


def _fsync_parent(parent: ParentHandle) -> None:
    try:
        os.fsync(_parent_fd(parent))
    except OSError:
        pass


def _open_identity_no_follow(path: Path, expected_kind: str) -> PathState:
    _require_dir_fd_support()
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if expected_kind == "directory":
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return PathState("absent")
    except OSError as exc:
        raise IdentityChangedError("guarded path is not safely openable without following links") from exc
    try:
        return _state_from_stat(os.fstat(fd))
    finally:
        os.close(fd)


def _require_identity(actual: PathState, expected: PathState, description: str) -> None:
    fields = ("kind", "device", "inode")
    if expected.kind == "file":
        fields += ("size", "modified_ns")
    if any(getattr(actual, field) != getattr(expected, field) for field in fields):
        raise IdentityChangedError(f"{description} identity changed")


def _identity_matches(actual: PathState, expected: PathState) -> bool:
    try:
        _require_identity(actual, expected, "guarded path")
    except OSError:
        return False
    return True


def _safe_launchctl_reason(result: LaunchctlResult) -> str:
    if result.ok:
        return "launchctl command succeeded"
    if result.returncode is None:
        return "launchctl command unavailable or timed out"
    return "launchctl command failed"

"""Guarded macOS user LaunchAgent lifecycle with injectable side-effect seams."""

from __future__ import annotations

import os
import platform
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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

    def read_snapshot(self, path: Path, max_bytes: int) -> FileSnapshot: ...

    def assert_unchanged(self, path: Path, expected: PathState, parent: Path, expected_parent: PathState) -> None: ...

    def write_atomic(
        self,
        path: Path,
        data: bytes,
        expected: PathState,
        expected_parent: PathState,
    ) -> PathState: ...

    def create_unique_backup(
        self,
        path: Path,
        data: bytes,
        expected: PathState,
        expected_parent: PathState,
    ) -> BackupSnapshot: ...

    def move_to_unique_backup(
        self,
        path: Path,
        expected: PathState,
        expected_parent: PathState,
    ) -> BackupSnapshot: ...

    def restore_backup_atomic(
        self,
        backup: BackupSnapshot,
        destination: Path,
        expected_destination: PathState,
        expected_parent: PathState,
    ) -> PathState: ...

    def remove(self, path: Path, expected: PathState, expected_parent: PathState) -> None: ...

    def discard_backup(self, backup: BackupSnapshot, expected_parent: PathState) -> None: ...


class LaunchctlExecutor(Protocol):
    def bootstrap(self, user_domain: str, plist_path: Path) -> LaunchctlResult: ...

    def bootout(self, user_domain: str, plist_path: Path) -> LaunchctlResult: ...


class LocalLaunchdFilesystem:
    """Local filesystem implementation; callers decide whether apply is authorized."""

    def inspect(self, path: Path) -> PathState:
        try:
            value = path.lstat()
        except FileNotFoundError:
            return PathState("absent")
        if stat.S_ISLNK(value.st_mode):
            kind = "symlink"
        elif stat.S_ISREG(value.st_mode):
            kind = "file"
        elif stat.S_ISDIR(value.st_mode):
            kind = "directory"
        else:
            kind = "other"
        return PathState(kind, value.st_size, value.st_dev, value.st_ino, value.st_mtime_ns)

    def read_snapshot(self, path: Path, max_bytes: int) -> FileSnapshot:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise IdentityChangedError("LaunchAgent destination is not a regular file")
            if before.st_size > max_bytes:
                raise ValueError("LaunchAgent destination exceeds size limit")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(fd)
            state = _state_from_stat(after)
            if len(data) > max_bytes:
                raise ValueError("LaunchAgent destination exceeds size limit")
            if _state_from_stat(before) != state or len(data) != state.size:
                raise IdentityChangedError("LaunchAgent destination changed while being read")
            return FileSnapshot(data, state)
        finally:
            os.close(fd)

    def assert_unchanged(
        self,
        path: Path,
        expected: PathState,
        parent: Path,
        expected_parent: PathState,
    ) -> None:
        _require_identity(_open_identity_no_follow(parent, expected_parent.kind), expected_parent, "LaunchAgents directory")
        if expected.kind in {"file", "directory"}:
            actual = _open_identity_no_follow(path, expected.kind)
        else:
            actual = self.inspect(path)
        _require_identity(actual, expected, "LaunchAgent destination")
        _require_identity(_open_identity_no_follow(parent, expected_parent.kind), expected_parent, "LaunchAgents directory")

    def write_atomic(
        self,
        path: Path,
        data: bytes,
        expected: PathState,
        expected_parent: PathState,
    ) -> PathState:
        self.assert_unchanged(path, expected, path.parent, expected_parent)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        tmp_state: PathState | None = None
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
                tmp_state = _state_from_stat(os.fstat(file.fileno()))
            os.chmod(tmp_path, 0o600)
            self.assert_unchanged(path, expected, path.parent, expected_parent)
            os.replace(tmp_path, path)
            _fsync_directory(path.parent)
            snapshot = self.read_snapshot(path, len(data))
            if snapshot.data != data:
                raise IdentityChangedError("atomic replacement bytes changed unexpectedly")
            return snapshot.state
        except Exception:
            if tmp_state is not None:
                _remove_owned_temporary(tmp_path, tmp_state, path.parent, expected_parent)
            raise

    def create_unique_backup(
        self,
        path: Path,
        data: bytes,
        expected: PathState,
        expected_parent: PathState,
    ) -> BackupSnapshot:
        self.assert_unchanged(path, expected, path.parent, expected_parent)
        fd, backup_name = tempfile.mkstemp(prefix=f".{path.name}.cbr-backup-", dir=path.parent)
        backup = Path(backup_name)
        created_state: PathState | None = None
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
                created_state = _state_from_stat(os.fstat(file.fileno()))
            os.chmod(backup, 0o600)
            _fsync_directory(path.parent)
            snapshot = self.read_snapshot(backup, len(data))
            if snapshot.data != data:
                raise IdentityChangedError("backup bytes changed unexpectedly")
            return BackupSnapshot(backup, snapshot.state, data)
        except Exception:
            if created_state is not None:
                _remove_owned_temporary(backup, created_state, path.parent, expected_parent)
            raise

    def move_to_unique_backup(
        self,
        path: Path,
        expected: PathState,
        expected_parent: PathState,
    ) -> BackupSnapshot:
        source = self.read_snapshot(path, MAX_MANAGED_PLIST_BYTES)
        _require_identity(source.state, expected, "LaunchAgent destination")
        for _ in range(100):
            backup = _unique_backup_path(path)
            if self.inspect(backup).kind != "absent":
                continue
            self.assert_unchanged(path, expected, path.parent, expected_parent)
            os.replace(path, backup)
            _fsync_directory(path.parent)
            snapshot = self.read_snapshot(backup, MAX_MANAGED_PLIST_BYTES)
            if snapshot.data != source.data:
                raise IdentityChangedError("uninstall move did not preserve managed bytes")
            self.assert_unchanged(path, PathState("absent"), path.parent, expected_parent)
            return BackupSnapshot(backup, snapshot.state, snapshot.data)
        raise OSError("could not allocate unique launchd backup path")

    def restore_backup_atomic(
        self,
        backup: BackupSnapshot,
        destination: Path,
        expected_destination: PathState,
        expected_parent: PathState,
    ) -> PathState:
        self.assert_unchanged(destination, expected_destination, destination.parent, expected_parent)
        _require_identity(_open_identity_no_follow(backup.path, "file"), backup.state, "LaunchAgent backup")
        os.replace(backup.path, destination)
        _fsync_directory(destination.parent)
        snapshot = self.read_snapshot(destination, MAX_MANAGED_PLIST_BYTES)
        if snapshot.data != backup.data:
            raise IdentityChangedError("restore did not preserve backup bytes")
        return snapshot.state

    def remove(self, path: Path, expected: PathState, expected_parent: PathState) -> None:
        self.assert_unchanged(path, expected, path.parent, expected_parent)
        path.unlink()
        _fsync_directory(path.parent)
        self.assert_unchanged(path, PathState("absent"), path.parent, expected_parent)

    def discard_backup(self, backup: BackupSnapshot, expected_parent: PathState) -> None:
        self.assert_unchanged(backup.path, backup.state, backup.path.parent, expected_parent)
        backup.path.unlink()
        _fsync_directory(backup.path.parent)


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
    destination, parent_state, blocked = _validated_destination(
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
    existing, blocked = _read_existing_managed_candidate(destination, filesystem)
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
            parent_state,
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
        parent_state,
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
    destination, parent_state, blocked = _validated_destination(
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
    existing, blocked = _read_existing_managed_candidate(destination, filesystem)
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
        parent_state,
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
    parent_state: PathState,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
    try:
        installed_state = filesystem.write_atomic(
            destination,
            rendered,
            PathState("absent"),
            parent_state,
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
        filesystem.assert_unchanged(destination, installed_state, destination.parent, parent_state)
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
        filesystem.remove(destination, installed_state, parent_state)
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
    parent_state: PathState,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
    try:
        backup = filesystem.create_unique_backup(
            destination,
            existing.data,
            existing.state,
            parent_state,
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
        filesystem.assert_unchanged(destination, existing.state, destination.parent, parent_state)
    except OSError:
        retained = not _discard_backup(filesystem, backup, parent_state)
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
        retained = not _discard_backup(filesystem, backup, parent_state)
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
            parent_state,
        )
    except OSError:
        return _recover_update(
            plan_input,
            destination,
            rendered,
            backup,
            existing.state,
            parent_state,
            filesystem,
            launchctl,
            environment,
            (bootout,),
            "atomic replacement failed",
        )
    try:
        filesystem.assert_unchanged(destination, updated_state, destination.parent, parent_state)
    except OSError:
        return _recover_update(
            plan_input,
            destination,
            rendered,
            backup,
            updated_state,
            parent_state,
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
            parent_state,
            filesystem,
            launchctl,
            environment,
            (bootout, bootstrap),
            "bootstrap of updated plist failed",
        )
    retained = not _discard_backup(filesystem, backup, parent_state)
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
    parent_state: PathState,
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
            parent_state,
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
        filesystem.assert_unchanged(destination, restored_state, destination.parent, parent_state)
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
    parent_state: PathState,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
    try:
        filesystem.assert_unchanged(destination, existing.state, destination.parent, parent_state)
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
        backup = filesystem.move_to_unique_backup(destination, existing.state, parent_state)
    except OSError:
        try:
            filesystem.assert_unchanged(destination, existing.state, destination.parent, parent_state)
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
) -> tuple[Path, PathState, str | None]:
    validate_launchd_label(label)
    raw = destination.expanduser()
    unknown_parent = PathState("unknown")
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
        return raw, parent_state, "user LaunchAgents directory must already exist and must not be a symlink"
    return raw, parent_state, None


def _read_existing_managed_candidate(
    destination: Path,
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
        snapshot = filesystem.read_snapshot(destination, MAX_MANAGED_PLIST_BYTES)
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
    parent_state: PathState,
) -> bool:
    try:
        filesystem.discard_backup(backup, parent_state)
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


def _open_identity_no_follow(path: Path, expected_kind: str) -> PathState:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if expected_kind == "directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
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


def _safe_launchctl_reason(result: LaunchctlResult) -> str:
    if result.ok:
        return "launchctl command succeeded"
    if result.returncode is None:
        return "launchctl command unavailable or timed out"
    return "launchctl command failed"


def _remove_owned_temporary(
    path: Path,
    expected: PathState,
    parent: Path,
    expected_parent: PathState,
) -> None:
    try:
        _require_identity(_open_identity_no_follow(parent, "directory"), expected_parent, "LaunchAgents directory")
        _require_identity(_open_identity_no_follow(path, "file"), expected, "temporary launchd file")
        path.unlink()
    except OSError:
        # Never clean up a pathname after its identity or parent becomes ambiguous.
        return


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            # The rename/link has already completed. Avoid reporting a false
            # pre-mutation failure that could trigger an unsafe second action.
            pass
    finally:
        os.close(fd)

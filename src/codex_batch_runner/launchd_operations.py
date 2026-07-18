"""Guarded macOS user LaunchAgent lifecycle with injectable side-effect seams."""

from __future__ import annotations

import os
import platform
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
    intended_plist: bytes | None = None
    launchctl_results: tuple[LaunchctlResult, ...] = ()


class LaunchdFilesystem(Protocol):
    def inspect(self, path: Path) -> PathState: ...

    def read_bytes(self, path: Path) -> bytes: ...

    def write_atomic(self, path: Path, data: bytes) -> None: ...

    def create_unique_backup(self, path: Path) -> Path: ...

    def move_to_unique_backup(self, path: Path) -> Path: ...

    def restore_backup_atomic(self, backup: Path, destination: Path) -> None: ...

    def remove(self, path: Path) -> None: ...

    def discard_backup(self, path: Path) -> None: ...


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
        if path.is_symlink():
            return PathState("symlink", value.st_size)
        if path.is_file():
            return PathState("file", value.st_size)
        if path.is_dir():
            return PathState("directory", value.st_size)
        return PathState("other", value.st_size)

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def write_atomic(self, path: Path, data: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
            _fsync_directory(path.parent)
        except Exception:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise

    def create_unique_backup(self, path: Path) -> Path:
        for _ in range(100):
            backup = _unique_backup_path(path)
            try:
                os.link(path, backup, follow_symlinks=False)
            except FileExistsError:
                continue
            _fsync_directory(path.parent)
            return backup
        raise OSError("could not allocate unique launchd backup path")

    def move_to_unique_backup(self, path: Path) -> Path:
        for _ in range(100):
            backup = _unique_backup_path(path)
            if backup.exists():
                continue
            os.replace(path, backup)
            _fsync_directory(path.parent)
            return backup
        raise OSError("could not allocate unique launchd backup path")

    def restore_backup_atomic(self, backup: Path, destination: Path) -> None:
        os.replace(backup, destination)
        _fsync_directory(destination.parent)

    def remove(self, path: Path) -> None:
        path.unlink()
        _fsync_directory(path.parent)

    def discard_backup(self, path: Path) -> None:
        path.unlink()
        _fsync_directory(path.parent)


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
    destination, blocked = _validated_destination(
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
    plan = plan_launchd_lifecycle(plan_input, existing)
    if plan.action == "blocked":
        return _result(
            operation="install",
            apply=apply,
            status="blocked",
            action="blocked",
            reason=plan.reason,
            destination=destination,
            plan_input=plan_input,
            intended_plist=plan.rendered_plist,
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
            intended_plist=plan.rendered_plist,
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
            intended_plist=plan.rendered_plist,
        )
    if action == "install":
        return _apply_install(plan_input, destination, plan.rendered_plist, filesystem, launchctl, environment)
    return _apply_update(plan_input, destination, plan.rendered_plist, filesystem, launchctl, environment)


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
    destination, blocked = _validated_destination(
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
    inspection = inspect_launchd_plist(existing)
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
    return _apply_uninstall(base_input, destination, filesystem, launchctl, environment)


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
                "reason": item.reason,
            }
            for item in result.launchctl_results
        ],
        "intended_plist": result.intended_plist.decode("utf-8") if result.intended_plist else None,
    }


def _apply_install(
    plan_input: LaunchdPlanInput,
    destination: Path,
    rendered: bytes,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
    try:
        filesystem.write_atomic(destination, rendered)
    except OSError:
        return _result("install", True, "failed", "install", "atomic plist write failed", destination, plan_input)
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
            intended_plist=rendered,
            launchctl_results=(command,),
        )
    try:
        filesystem.remove(destination)
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
            intended_plist=rendered,
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
        intended_plist=rendered,
        launchctl_results=(command,),
    )


def _apply_update(
    plan_input: LaunchdPlanInput,
    destination: Path,
    rendered: bytes,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
    try:
        backup = filesystem.create_unique_backup(destination)
    except OSError:
        return _result("install", True, "failed", "update", "atomic backup creation failed", destination, plan_input)
    bootout = launchctl.bootout(environment.user_domain, destination)
    if not bootout.ok:
        retained = not _discard_backup(filesystem, backup)
        return _result(
            "install",
            True,
            "failed",
            "update",
            "bootout failed before managed plist replacement",
            destination,
            plan_input,
            backup_path=backup if retained else None,
            backup_retained=retained,
            intended_plist=rendered,
            launchctl_results=(bootout,),
        )
    try:
        filesystem.write_atomic(destination, rendered)
    except OSError:
        return _recover_update(
            plan_input,
            destination,
            rendered,
            backup,
            filesystem,
            launchctl,
            environment,
            (bootout,),
            "atomic replacement failed",
        )
    bootstrap = launchctl.bootstrap(environment.user_domain, destination)
    if not bootstrap.ok:
        return _recover_update(
            plan_input,
            destination,
            rendered,
            backup,
            filesystem,
            launchctl,
            environment,
            (bootout, bootstrap),
            "bootstrap of updated plist failed",
        )
    retained = not _discard_backup(filesystem, backup)
    return _result(
        "install",
        True,
        "applied",
        "update",
        "managed plist updated and bootstrapped",
        destination,
        plan_input,
        changed=True,
        backup_path=backup if retained else None,
        backup_retained=retained,
        intended_plist=rendered,
        launchctl_results=(bootout, bootstrap),
    )


def _recover_update(
    plan_input: LaunchdPlanInput,
    destination: Path,
    rendered: bytes,
    backup: Path,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
    commands: tuple[LaunchctlResult, ...],
    failure_reason: str,
) -> LaunchdLifecycleResult:
    try:
        filesystem.restore_backup_atomic(backup, destination)
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
            backup_path=backup,
            backup_retained=True,
            recovery_attempted=True,
            recovery_succeeded=False,
            intended_plist=rendered,
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
            intended_plist=rendered,
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
        intended_plist=rendered,
        launchctl_results=all_commands,
    )


def _apply_uninstall(
    plan_input: LaunchdPlanInput,
    destination: Path,
    filesystem: LaunchdFilesystem,
    launchctl: LaunchctlExecutor,
    environment: LaunchdEnvironment,
) -> LaunchdLifecycleResult:
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
        backup = filesystem.move_to_unique_backup(destination)
    except OSError:
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
        backup_path=backup,
        backup_retained=True,
        launchctl_results=(bootout,),
    )


def _validated_destination(
    destination: Path,
    *,
    label: str,
    environment: LaunchdEnvironment,
    filesystem: LaunchdFilesystem,
) -> tuple[Path, str | None]:
    validate_launchd_label(label)
    raw = destination.expanduser()
    if environment.platform_name != "Darwin":
        return raw, "launchd lifecycle is only available on macOS"
    if environment.uid <= 0:
        return raw, "launchd lifecycle requires a non-root user"
    if environment.user_domain != f"gui/{environment.uid}":
        return raw, f"user domain must exactly match gui/{environment.uid}"
    home = environment.home.expanduser().resolve()
    expected_parent = home / "Library" / "LaunchAgents"
    if not raw.is_absolute():
        return raw, "LaunchAgent destination must be an explicit absolute path"
    if raw.parent != expected_parent or raw.name != f"{label}.plist":
        return raw, "LaunchAgent destination must exactly match HOME/Library/LaunchAgents/LABEL.plist"
    if filesystem.inspect(expected_parent).kind != "directory":
        return raw, "user LaunchAgents directory must already exist and must not be a symlink"
    return raw, None


def _read_existing_managed_candidate(
    destination: Path,
    filesystem: LaunchdFilesystem,
) -> tuple[bytes | None, str | None]:
    state = filesystem.inspect(destination)
    if state.kind == "absent":
        return None, None
    if state.kind != "file":
        return None, f"existing LaunchAgent destination is {state.kind}, not a regular file"
    if state.size is None or state.size > MAX_MANAGED_PLIST_BYTES:
        return None, f"existing LaunchAgent exceeds {MAX_MANAGED_PLIST_BYTES} byte limit"
    try:
        return filesystem.read_bytes(destination), None
    except OSError:
        return None, "existing LaunchAgent is not readable"


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
    intended_plist: bytes | None = None,
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
        intended_plist=intended_plist,
        launchctl_results=launchctl_results,
    )


def _discard_backup(filesystem: LaunchdFilesystem, backup: Path) -> bool:
    try:
        filesystem.discard_backup(backup)
    except OSError:
        return False
    return True


def _unique_backup_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.cbr-backup-{uuid.uuid4().hex}")


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

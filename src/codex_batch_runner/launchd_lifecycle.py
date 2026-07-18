"""Pure, fail-closed planning primitives for a managed macOS LaunchAgent.

This module deliberately does not read or write plist files and never invokes
``launchctl``.  Callers provide any existing plist bytes and own all OS-facing
lifecycle actions.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any


MARKER_KEY = "CBRLaunchdLifecycle"
MARKER_VERSION = 1
_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MANAGED_TOP_LEVEL_KEYS = {
    "Label",
    "ProgramArguments",
    "WorkingDirectory",
    "EnvironmentVariables",
    "StartInterval",
    "StandardOutPath",
    "StandardErrorPath",
    MARKER_KEY,
}
_MANAGED_ENVIRONMENT_KEYS = {"PATH"}


@dataclass(frozen=True)
class LaunchdPlanInput:
    """Validated data injected by config discovery and operator-facing code."""

    label: str
    executable_path: str
    config_path: str
    config_provenance: str
    working_directory: str
    stdout_path: str
    stderr_path: str
    environment_path: str
    start_interval_seconds: int


@dataclass(frozen=True)
class LaunchdPlan:
    """A mutation-free classification and intended plist rendering."""

    status: str
    action: str
    reason: str
    managed_digest: str
    rendered_plist: bytes
    config_provenance: str
    config_path: str


@dataclass(frozen=True)
class LaunchdInspection:
    """Fail-closed ownership inspection for an existing plist."""

    status: str
    reason: str
    managed_digest: str | None
    managed_fields: dict[str, Any] | None


def render_launchd_plist(plan_input: LaunchdPlanInput) -> bytes:
    """Return deterministic XML plist bytes for a validated managed LaunchAgent."""

    fields = _managed_fields(plan_input)
    digest = _managed_digest(fields)
    plist = {
        "Label": fields["label"],
        "ProgramArguments": [
            fields["executable_path"],
            "--config",
            fields["config_path"],
            "run-loop",
            "--json",
        ],
        "WorkingDirectory": fields["working_directory"],
        "EnvironmentVariables": {"PATH": fields["environment_path"]},
        "StartInterval": fields["start_interval_seconds"],
        "StandardOutPath": fields["stdout_path"],
        "StandardErrorPath": fields["stderr_path"],
        MARKER_KEY: {"version": MARKER_VERSION, "digest": digest},
    }
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)


def plan_launchd_lifecycle(plan_input: LaunchdPlanInput, existing_plist: bytes | None) -> LaunchdPlan:
    """Classify a supplied plist without touching the filesystem or launchd."""

    expected_fields = _managed_fields(plan_input)
    expected_digest = _managed_digest(expected_fields)
    rendered = render_launchd_plist(plan_input)
    common = {
        "managed_digest": expected_digest,
        "rendered_plist": rendered,
        "config_provenance": plan_input.config_provenance,
        "config_path": plan_input.config_path,
    }
    if existing_plist is None:
        return LaunchdPlan(status="not_installed", action="create", reason="no existing plist supplied", **common)

    inspection = inspect_launchd_plist(existing_plist)
    if inspection.status != "managed":
        return LaunchdPlan(
            status=inspection.status,
            action="blocked",
            reason=inspection.reason,
            **common,
        )
    stored_digest = inspection.managed_digest
    if stored_digest == expected_digest:
        return LaunchdPlan(status="managed_ok", action="none", reason="managed plist matches requested inputs", **common)
    return LaunchdPlan(status="drifted", action="update_needed", reason="managed plist differs from requested inputs", **common)


def inspect_launchd_plist(existing_plist: bytes) -> LaunchdInspection:
    """Inspect exact CBR ownership without comparing against requested inputs."""

    try:
        existing = plistlib.loads(existing_plist)
    except (ValueError, TypeError, plistlib.InvalidFileException) as exc:
        return LaunchdInspection("unhealthy", f"malformed plist: {exc}", None, None)
    if not isinstance(existing, dict):
        return LaunchdInspection("unhealthy", "plist root must be a dictionary", None, None)

    marker = existing.get(MARKER_KEY)
    if marker is None:
        return LaunchdInspection("foreign_conflict", "missing CBR ownership marker", None, None)
    if set(existing) != _MANAGED_TOP_LEVEL_KEYS:
        return LaunchdInspection(
            "unhealthy",
            "managed plist top-level keys do not match contract",
            None,
            None,
        )
    if not isinstance(marker, dict) or marker.get("version") != MARKER_VERSION:
        return LaunchdInspection("unhealthy", "invalid CBR ownership marker", None, None)
    stored_digest = marker.get("digest")
    if not isinstance(stored_digest, str) or not _DIGEST_PATTERN.fullmatch(stored_digest):
        return LaunchdInspection("unhealthy", "invalid CBR ownership digest", None, None)

    try:
        actual_fields = _fields_from_existing(existing)
    except ValueError as exc:
        return LaunchdInspection("unhealthy", f"invalid managed plist: {exc}", None, None)
    if _managed_digest(actual_fields) != stored_digest:
        return LaunchdInspection(
            "unhealthy",
            "managed plist content does not match ownership digest",
            None,
            None,
        )
    return LaunchdInspection("managed", "valid CBR-owned plist", stored_digest, actual_fields)


def _managed_fields(plan_input: LaunchdPlanInput) -> dict[str, Any]:
    validate_launchd_label(plan_input.label)
    for name in (
        "executable_path",
        "config_path",
        "working_directory",
        "stdout_path",
        "stderr_path",
    ):
        value = getattr(plan_input, name)
        if not _is_absolute(value):
            raise ValueError(f"{name} must be an absolute path")
    if not isinstance(plan_input.environment_path, str) or not plan_input.environment_path:
        raise ValueError("environment_path must be a non-empty absolute PATH list")
    if any(not _is_absolute(segment) for segment in plan_input.environment_path.split(":")):
        raise ValueError("environment_path segments must be non-empty absolute paths")
    if not isinstance(plan_input.start_interval_seconds, int) or isinstance(plan_input.start_interval_seconds, bool) or plan_input.start_interval_seconds <= 0:
        raise ValueError("start_interval_seconds must be a positive integer")
    if plan_input.config_provenance not in {"cli", "environment", "xdg"}:
        raise ValueError("config_provenance must be cli, environment, or xdg")
    return {
        "label": plan_input.label,
        "executable_path": plan_input.executable_path,
        "config_path": plan_input.config_path,
        "working_directory": plan_input.working_directory,
        "stdout_path": plan_input.stdout_path,
        "stderr_path": plan_input.stderr_path,
        "environment_path": plan_input.environment_path,
        "start_interval_seconds": plan_input.start_interval_seconds,
    }


def _fields_from_existing(plist: dict[str, Any]) -> dict[str, Any]:
    arguments = plist.get("ProgramArguments")
    if not isinstance(arguments, list) or len(arguments) != 5 or arguments[1:] != ["--config", arguments[2], "run-loop", "--json"]:
        raise ValueError("ProgramArguments must be executable --config PATH run-loop --json")
    environment = plist.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        raise ValueError("EnvironmentVariables must be a dictionary")
    if set(environment) != _MANAGED_ENVIRONMENT_KEYS:
        raise ValueError("EnvironmentVariables keys do not match managed contract")
    fields = {
        "label": plist.get("Label"),
        "executable_path": arguments[0],
        "config_path": arguments[2],
        "working_directory": plist.get("WorkingDirectory"),
        "stdout_path": plist.get("StandardOutPath"),
        "stderr_path": plist.get("StandardErrorPath"),
        "environment_path": environment.get("PATH"),
        "start_interval_seconds": plist.get("StartInterval"),
    }
    return _managed_fields(LaunchdPlanInput(config_provenance="cli", **fields))


def _managed_digest(fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_absolute(value: object) -> bool:
    return isinstance(value, str) and bool(value) and PurePath(value).is_absolute()


def validate_launchd_label(value: object) -> str:
    if not isinstance(value, str) or not _LABEL_PATTERN.fullmatch(value):
        raise ValueError("label must contain only letters, numbers, dots, and hyphens")
    return value

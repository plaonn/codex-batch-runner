from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .config import Config
from .fs import read_json, write_json_atomic
from .timeutil import iso_now, parse_time, utc_now


POLICY_FILE = ".cbr.toml"
POLICY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PrepareStep:
    command: tuple[str, ...]
    cwd: PurePosixPath


@dataclass(frozen=True)
class WorktreePoolPolicy:
    copy: tuple[PurePosixPath, ...]
    retain: tuple[PurePosixPath, ...]
    prepare: tuple[PrepareStep, ...]
    max_slots: int
    idle_ttl_hours: int
    fingerprint: str


def load_worktree_pool_policy(repo_root: Path) -> WorktreePoolPolicy | None:
    policy_path = repo_root / POLICY_FILE
    if not policy_path.exists():
        return None
    tracked = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", POLICY_FILE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if tracked.returncode != 0:
        raise ValueError(f"{POLICY_FILE} must be tracked by Git before worktree pooling can be enabled")
    try:
        data = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid {POLICY_FILE}: {exc}") from exc
    if set(data) != {"worktree"} or not isinstance(data.get("worktree"), dict):
        raise ValueError(f"{POLICY_FILE} must contain only a [worktree] table")
    worktree = data["worktree"]
    allowed_worktree = {"copy", "retain", "pool", "prepare"}
    unknown = sorted(set(worktree) - allowed_worktree)
    if unknown:
        raise ValueError(f"invalid {POLICY_FILE}: unknown worktree keys: {', '.join(unknown)}")
    copy = _path_list("worktree.copy", worktree.get("copy", []))
    retain = _path_list("worktree.retain", worktree.get("retain", []))
    _validate_non_overlapping_paths(copy, retain)
    _validate_untracked_policy_paths(repo_root, copy, retain)
    pool = worktree.get("pool")
    if not isinstance(pool, dict):
        raise ValueError(f"invalid {POLICY_FILE}: [worktree.pool] is required")
    unknown_pool = sorted(set(pool) - {"max_slots", "idle_ttl_hours"})
    if unknown_pool:
        raise ValueError(f"invalid {POLICY_FILE}: unknown worktree.pool keys: {', '.join(unknown_pool)}")
    max_slots = _positive_int("worktree.pool.max_slots", pool.get("max_slots"))
    idle_ttl_hours = _positive_int("worktree.pool.idle_ttl_hours", pool.get("idle_ttl_hours"))
    raw_prepare = worktree.get("prepare", [])
    if not isinstance(raw_prepare, list):
        raise ValueError(f"invalid {POLICY_FILE}: [[worktree.prepare]] must be an array of tables")
    prepare: list[PrepareStep] = []
    for index, item in enumerate(raw_prepare):
        if not isinstance(item, dict) or set(item) - {"command", "cwd"}:
            raise ValueError(f"invalid {POLICY_FILE}: worktree.prepare[{index}] has unknown fields")
        command = item.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(value, str) or not value for value in command)
        ):
            raise ValueError(
                f"invalid {POLICY_FILE}: worktree.prepare[{index}].command must be a non-empty argv array"
            )
        cwd = _relative_path(f"worktree.prepare[{index}].cwd", item.get("cwd", "."))
        prepare.append(PrepareStep(tuple(command), cwd))
    normalized = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "repository": str(repo_root.resolve()),
        "copy": [path.as_posix() for path in copy],
        "retain": [path.as_posix() for path in retain],
        "prepare": [
            {"command": list(step.command), "cwd": step.cwd.as_posix()}
            for step in prepare
        ],
        "pool": {"max_slots": max_slots, "idle_ttl_hours": idle_ttl_hours},
    }
    fingerprint = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return WorktreePoolPolicy(
        copy=copy,
        retain=retain,
        prepare=tuple(prepare),
        max_slots=max_slots,
        idle_ttl_hours=idle_ttl_hours,
        fingerprint=fingerprint,
    )


def acquire_pool_slot(
    config: Config,
    repo_root: Path,
    base_head: str,
    branch: str,
    task_id: str,
    policy: WorktreePoolPolicy,
) -> dict[str, Any]:
    if config.worktree_root is None:
        raise ValueError("worktree_root is not configured")
    state = _load_state(config)
    _prune_expired_slots(config, repo_root, policy, state)
    slots = state.setdefault("slots", [])
    repo_key = str(repo_root.resolve())
    candidates = [
        slot
        for slot in slots
        if slot.get("repo_root") == repo_key
        and slot.get("policy_fingerprint") == policy.fingerprint
        and slot.get("status") == "idle"
    ]
    candidates.sort(key=lambda slot: str(slot.get("last_released_at") or ""))
    created = False
    if candidates:
        slot = candidates[0]
        slot_path = _guard_pool_path(config, Path(str(slot["path"])))
        _validate_idle_slot(repo_root, slot_path)
    else:
        repo_slots = [
            slot
            for slot in slots
            if slot.get("repo_root") == repo_key
            and slot.get("status") in {"idle", "leased", "recovery_required"}
        ]
        if len(repo_slots) >= policy.max_slots:
            raise ValueError(
                f"worktree pool has no idle slot and max_slots={policy.max_slots} is reached"
            )
        slot_id = _next_slot_id(slots, repo_root, policy)
        slot_path = _guard_pool_path(
            config,
            config.worktree_root / f"pool-{policy.fingerprint[:10]}-{slot_id:03d}",
        )
        if slot_path.exists():
            raise ValueError("worktree pool slot path already exists without pool metadata")
        _git(repo_root, "worktree", "add", "--detach", str(slot_path), base_head)
        slot = {
            "slot_id": slot_id,
            "repo_root": repo_key,
            "path": str(slot_path),
            "policy_fingerprint": policy.fingerprint,
            "status": "idle",
            "created_at": iso_now(),
        }
        slots.append(slot)
        created = True
    try:
        _prepare_slot(config, repo_root, slot_path, base_head, branch, policy)
    except Exception:
        recovered = _recover_failed_acquire(repo_root, slot_path, branch, policy)
        slot.update(
            {
                "status": "idle" if recovered else "recovery_required",
                "task_id": None if recovered else task_id,
                "branch": None if recovered else branch,
                "last_released_at": iso_now() if recovered else None,
            }
        )
        _save_state(config, state)
        raise
    slot.update(
        {
            "status": "leased",
            "task_id": task_id,
            "branch": branch,
            "base_head": base_head,
            "leased_at": iso_now(),
            "last_released_at": slot.get("last_released_at"),
        }
    )
    _save_state(config, state)
    return {
        "slot_id": slot["slot_id"],
        "path": slot_path,
        "policy_fingerprint": policy.fingerprint,
        "created": created,
    }


def release_pool_slot(
    config: Config,
    repo_root: Path,
    worktree_path: Path,
    branch: str,
    task_id: str,
) -> dict[str, Any]:
    policy = load_worktree_pool_policy(repo_root)
    if policy is None:
        raise ValueError("pooled task cannot be released because .cbr.toml is missing")
    state, slot = validate_pool_lease(
        config,
        repo_root,
        worktree_path,
        branch,
        task_id,
        policy.fingerprint,
    )
    baseline = _git(repo_root, "rev-parse", "HEAD")
    _git(worktree_path, "switch", "--detach", baseline)
    _git(worktree_path, "reset", "--hard", baseline)
    _clean_untracked(worktree_path, policy.retain)
    slot.update(
        {
            "status": "idle",
            "task_id": None,
            "branch": None,
            "base_head": baseline,
            "last_released_at": iso_now(),
        }
    )
    _save_state(config, state)
    return {"slot_id": slot["slot_id"], "path": worktree_path, "policy_fingerprint": policy.fingerprint}


def pool_state_summary(config: Config) -> dict[str, Any]:
    path = _state_path(config)
    if not path.exists():
        return {
            "status": "absent",
            "total": 0,
            "idle": 0,
            "leased": 0,
            "recovery_required": 0,
        }
    try:
        state = _load_state(config)
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "status": "invalid",
            "total": 0,
            "idle": 0,
            "leased": 0,
            "recovery_required": 0,
        }
    counts = {"idle": 0, "leased": 0, "recovery_required": 0}
    for slot in state.get("slots", []):
        status = str(slot.get("status") or "")
        if status in counts:
            counts[status] += 1
    return {
        "status": "current",
        "total": len(state.get("slots", [])),
        "idle": counts["idle"],
        "leased": counts["leased"],
        "recovery_required": counts["recovery_required"],
    }


def pool_acquire_preview(
    config: Config,
    repo_root: Path,
    policy: WorktreePoolPolicy,
) -> dict[str, Any]:
    state = _load_state(config)
    all_repo_slots = [
        slot
        for slot in state.get("slots", [])
        if slot.get("repo_root") == str(repo_root.resolve())
        and slot.get("status") in {"idle", "leased", "recovery_required"}
    ]
    cutoff = utc_now() - timedelta(hours=policy.idle_ttl_hours)
    repo_slots = []
    for slot in all_repo_slots:
        released = parse_time(str(slot.get("last_released_at") or ""))
        prunable = slot.get("status") == "idle" and (
            slot.get("policy_fingerprint") != policy.fingerprint
            or (released is not None and released < cutoff)
        )
        if not prunable:
            repo_slots.append(slot)
    matching_idle = [
        slot
        for slot in repo_slots
        if slot.get("status") == "idle"
        and slot.get("policy_fingerprint") == policy.fingerprint
    ]
    if matching_idle:
        return {
            "status": "idle_available",
            "eligible": True,
            "slot_count": len(repo_slots),
        }
    if len(repo_slots) < policy.max_slots:
        return {
            "status": "creatable",
            "eligible": True,
            "slot_count": len(repo_slots),
        }
    return {
        "status": "blocked",
        "eligible": False,
        "slot_count": len(repo_slots),
        "reason": f"worktree pool has no idle slot and max_slots={policy.max_slots} is reached",
    }


def validate_pool_lease(
    config: Config,
    repo_root: Path,
    worktree_path: Path,
    branch: str,
    task_id: str,
    policy_fingerprint: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = _load_state(config)
    slot = next(
        (
            item
            for item in state.get("slots", [])
            if Path(str(item.get("path") or "")).expanduser().resolve()
            == worktree_path.resolve()
        ),
        None,
    )
    if not slot:
        raise ValueError("pooled task worktree has no matching pool slot metadata")
    if slot.get("status") != "leased" or slot.get("task_id") != task_id:
        raise ValueError("pool slot lease metadata does not match the task")
    if slot.get("policy_fingerprint") != policy_fingerprint:
        raise ValueError("pool policy changed while the task lease was active")
    registry_branch = _git(worktree_path, "branch", "--show-current")
    if registry_branch != branch:
        raise ValueError("pool slot branch does not match task metadata")
    return state, slot


def _prepare_slot(
    config: Config,
    repo_root: Path,
    slot_path: Path,
    base_head: str,
    branch: str,
    policy: WorktreePoolPolicy,
) -> None:
    _git(slot_path, "switch", "--detach", base_head)
    _git(slot_path, "reset", "--hard", base_head)
    _clean_untracked(slot_path, policy.retain)
    _git(slot_path, "switch", "-c", branch, base_head)
    for relative in policy.copy:
        source = repo_root / relative.as_posix()
        destination = slot_path / relative.as_posix()
        if not source.exists() and not source.is_symlink():
            raise ValueError(f"configured copy source is missing: {relative.as_posix()}")
        _remove_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
    for step in policy.prepare:
        cwd = (slot_path / step.cwd.as_posix()).resolve()
        if cwd != slot_path.resolve() and slot_path.resolve() not in cwd.parents:
            raise ValueError("prepare cwd escapes the worktree slot")
        if not cwd.is_dir():
            raise ValueError(f"prepare cwd does not exist: {step.cwd.as_posix()}")
        try:
            result = subprocess.run(
                list(step.command),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=config.shell_task_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f"prepare command timed out after {config.shell_task_timeout_seconds}s: {step.command[0]}"
            ) from exc
        if result.returncode != 0:
            raise ValueError(
                f"prepare command failed with exit {result.returncode}: {step.command[0]}"
            )
    tracked_dirty = _git(slot_path, "status", "--porcelain", "--untracked-files=no")
    if tracked_dirty:
        raise ValueError("prepare commands modified tracked files")


def _recover_failed_acquire(
    repo_root: Path,
    slot_path: Path,
    branch: str,
    policy: WorktreePoolPolicy,
) -> bool:
    try:
        baseline = _git(repo_root, "rev-parse", "HEAD")
        _git(slot_path, "switch", "--detach", baseline)
        _git(slot_path, "reset", "--hard", baseline)
        _clean_untracked(slot_path, policy.retain)
        subprocess.run(
            ["git", "-C", str(repo_root), "branch", "-d", branch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        branch_exists = subprocess.run(
            ["git", "-C", str(repo_root), "show-ref", "--verify", f"refs/heads/{branch}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        return not branch_exists and not _git(slot_path, "branch", "--show-current")
    except Exception:
        return False


def _prune_expired_slots(
    config: Config,
    repo_root: Path,
    policy: WorktreePoolPolicy,
    state: dict[str, Any],
) -> None:
    cutoff = utc_now() - timedelta(hours=policy.idle_ttl_hours)
    retained: list[dict[str, Any]] = []
    changed = False
    for slot in state.get("slots", []):
        released = parse_time(str(slot.get("last_released_at") or ""))
        expired = (
            slot.get("repo_root") == str(repo_root.resolve())
            and slot.get("status") == "idle"
            and released is not None
            and released < cutoff
        )
        mismatched = (
            slot.get("repo_root") == str(repo_root.resolve())
            and slot.get("status") == "idle"
            and slot.get("policy_fingerprint") != policy.fingerprint
        )
        if not expired and not mismatched:
            retained.append(slot)
            continue
        path = _guard_pool_path(config, Path(str(slot.get("path") or "")))
        try:
            _validate_idle_slot(repo_root, path)
            baseline = _git(repo_root, "rev-parse", "HEAD")
            _git(path, "reset", "--hard", baseline)
            _clean_untracked(path, ())
        except (OSError, ValueError, subprocess.SubprocessError):
            retained.append(slot)
            continue
        result = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            changed = True
        else:
            retained.append(slot)
    if changed:
        state["slots"] = retained
        _save_state(config, state)


def _clean_untracked(worktree: Path, retain: tuple[PurePosixPath, ...]) -> None:
    retained = tuple(path.as_posix() for path in retain)
    outputs = [
        subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout,
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout,
    ]
    for raw in b"\0".join(outputs).split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape")
        if _is_retained(relative, retained):
            continue
        _remove_path(worktree / relative)
    for root, directories, _files in os.walk(worktree, topdown=False):
        root_path = Path(root)
        for name in directories:
            path = root_path / name
            if path.name == ".git":
                continue
            relative = path.relative_to(worktree).as_posix()
            if _is_retained(relative, retained) or any(
                retained_path.startswith(f"{relative}/") for retained_path in retained
            ):
                continue
            try:
                path.rmdir()
            except OSError:
                pass


def _is_retained(relative: str, retained: tuple[str, ...]) -> bool:
    return any(relative == path or relative.startswith(f"{path}/") for path in retained)


def _validate_idle_slot(repo_root: Path, slot_path: Path) -> None:
    if not slot_path.is_dir():
        raise ValueError("pool slot path is missing")
    branch = _git(slot_path, "branch", "--show-current")
    if branch:
        raise ValueError("idle pool slot is still attached to a task branch")
    registered = _git(repo_root, "worktree", "list", "--porcelain")
    if f"worktree {slot_path}" not in registered:
        raise ValueError("pool slot is not registered as a Git worktree")


def _load_state(config: Config) -> dict[str, Any]:
    state = read_json(_state_path(config), {"schema_version": 1, "slots": []})
    if not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(state.get("slots"), list):
        raise ValueError("invalid worktree pool state")
    return state


def _save_state(config: Config, state: dict[str, Any]) -> None:
    write_json_atomic(_state_path(config), state)


def _state_path(config: Config) -> Path:
    if config.worktree_root is None:
        raise ValueError("worktree_root is not configured")
    return config.worktree_root / ".pool-state.json"


def _guard_pool_path(config: Config, path: Path) -> Path:
    if config.worktree_root is None:
        raise ValueError("worktree_root is not configured")
    root = config.worktree_root.expanduser().resolve()
    resolved = path.expanduser().resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("pool slot path must be inside configured worktree_root")
    return resolved


def _next_slot_id(
    slots: list[dict[str, Any]],
    repo_root: Path,
    policy: WorktreePoolPolicy,
) -> int:
    ids = [
        int(slot.get("slot_id"))
        for slot in slots
        if slot.get("repo_root") == str(repo_root.resolve())
        and slot.get("policy_fingerprint") == policy.fingerprint
        and isinstance(slot.get("slot_id"), int)
    ]
    return max(ids, default=0) + 1


def _path_list(name: str, value: object) -> tuple[PurePosixPath, ...]:
    if not isinstance(value, list):
        raise ValueError(f"invalid {POLICY_FILE}: {name} must be an array")
    result = tuple(_relative_path(f"{name}[{index}]", item) for index, item in enumerate(value))
    if len(set(result)) != len(result):
        raise ValueError(f"invalid {POLICY_FILE}: {name} contains duplicates")
    return result


def _relative_path(name: str, value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {POLICY_FILE}: {name} must be a relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() in {"", ".", ".git"}:
        if path.as_posix() == "." and ".cwd" in name:
            return path
        raise ValueError(f"invalid {POLICY_FILE}: unsafe path for {name}")
    if path.parts and path.parts[0] == ".git":
        raise ValueError(f"invalid {POLICY_FILE}: {name} cannot address .git")
    return path


def _validate_non_overlapping_paths(
    copy: tuple[PurePosixPath, ...],
    retain: tuple[PurePosixPath, ...],
) -> None:
    all_paths = [("copy", path) for path in copy] + [("retain", path) for path in retain]
    for index, (kind, path) in enumerate(all_paths):
        for other_kind, other in all_paths[index + 1 :]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError(
                    f"invalid {POLICY_FILE}: {kind} path {path} overlaps {other_kind} path {other}"
                )


def _validate_untracked_policy_paths(
    repo_root: Path,
    copy: tuple[PurePosixPath, ...],
    retain: tuple[PurePosixPath, ...],
) -> None:
    for kind, paths in (("copy", copy), ("retain", retain)):
        for path in paths:
            tracked = subprocess.run(
                ["git", "-C", str(repo_root), "ls-files", "--", path.as_posix()],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            if tracked:
                raise ValueError(
                    f"invalid {POLICY_FILE}: worktree.{kind} path is tracked by Git: {path.as_posix()}"
                )


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid {POLICY_FILE}: {name} must be a positive integer")
    return value


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()

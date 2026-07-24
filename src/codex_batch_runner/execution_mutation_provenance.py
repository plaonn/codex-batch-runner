from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any


CONTRACT_VERSION = "cbr-execution-mutation-provenance-v1"
SNAPSHOT_CONTRACT_VERSION = "cbr-execution-mutation-snapshot-v1"
SCHEMA_VERSION = 1
SCOPE = "cbr-controlled-task-repository-worktree"
PHASES = {"pre_worker", "post_worker_pre_cbr_commit", "terminal_closure"}
PROVENANCE = {"no_mutation", "mutation_possible", "mutation_observed", "unknown"}
FORBIDDEN_KEYS = {
    "prompt",
    "transcript",
    "stdout",
    "stderr",
    "command",
    "argv",
    "cwd",
    "path",
    "session_id",
    "thread_id",
    "credential",
    "account",
    "email",
}


class ExecutionMutationProvenanceError(ValueError):
    pass


def capture_execution_mutation_snapshot(
    task: dict[str, Any],
    execution_root: Path | None,
    *,
    phase: str,
    captured_at: datetime | None = None,
    reported_changed_files: object = None,
) -> dict[str, Any]:
    if phase not in PHASES:
        raise ExecutionMutationProvenanceError("unsupported mutation snapshot phase")
    observed = _aware_utc(captured_at or datetime.now(timezone.utc), "captured_at")
    repository = _inspect_repository(execution_root, reported_changed_files)
    isolated = bool(
        execution_root
        and task.get("execution_mode") == "git_worktree"
        and task.get("execution_worktree_path")
        and Path(str(task["execution_worktree_path"])).expanduser().resolve()
        == execution_root.expanduser().resolve()
    )
    base_head = _commit(task.get("execution_base_head"))
    identity_components = {
        "task_id": _safe_id(task.get("id"), "task.id"),
        "attempt": _count(task.get("attempts"), "task.attempts"),
        "execution_branch": _safe_text(task.get("execution_branch")),
        "base_head": base_head,
    }
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "contract": SNAPSHOT_CONTRACT_VERSION,
        "kind": "execution_mutation_snapshot",
        "captured_at": observed.isoformat(),
        "phase": phase,
        "binding": {
            "task_id": identity_components["task_id"],
            "attempt": identity_components["attempt"],
        },
        "scope": {
            "name": SCOPE,
            "isolated_execution_root": isolated,
            "repository_identity_digest": _stable_id(identity_components),
            "worktree_identity_digest": _stable_id(
                {
                    "task_id": identity_components["task_id"],
                    "attempt": identity_components["attempt"],
                    "execution_branch": identity_components["execution_branch"],
                }
            ),
        },
        "repository": repository,
        "task_review_state": {
            "state_digest": _stable_id(_task_review_state(task)),
            "status": _safe_text(task.get("status")),
            "review_status": _safe_text(task.get("review_status")),
            "worktree_status": _safe_text(task.get("execution_worktree_status")),
            "cbr_commit_present": bool(task.get("execution_commit")),
            "retained_recovery_state": task.get("execution_worktree_status")
            in {"retained", "recovery_required"},
        },
        "privacy": {
            "raw_paths_included": False,
            "raw_prompt_included": False,
            "raw_transcript_included": False,
            "session_or_thread_ids_included": False,
            "credentials_included": False,
            "private_identity_included": False,
        },
        "report_only": True,
        "mutation_allowed": False,
    }
    snapshot["snapshot_id"] = _stable_id(snapshot)
    return validate_execution_mutation_snapshot(snapshot)


def attach_execution_mutation_snapshot(
    task: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    validated = validate_execution_mutation_snapshot(snapshot)
    if (
        validated["binding"]["task_id"] != str(task.get("id"))
        or validated["binding"]["attempt"] != task.get("attempts")
    ):
        raise ExecutionMutationProvenanceError(
            "mutation snapshot binding does not match task"
        )
    history = task.setdefault("execution_mutation_snapshot_history", [])
    if not isinstance(history, list):
        raise ExecutionMutationProvenanceError(
            "mutation snapshot history must be a list"
        )
    if not any(
        isinstance(item, dict)
        and item.get("snapshot_id") == validated["snapshot_id"]
        for item in history
    ):
        history.append(validated)


def build_execution_mutation_provenance(
    task: dict[str, Any],
    *,
    recorded_at: datetime | None = None,
    producer_revision: str,
) -> dict[str, Any]:
    observed = _aware_utc(recorded_at or datetime.now(timezone.utc), "recorded_at")
    task_id = _safe_id(task.get("id"), "task.id")
    attempt = _count(task.get("attempts"), "task.attempts")
    snapshots, conflicts = _attempt_snapshots(task, task_id, attempt)
    pre = snapshots.get("pre_worker")
    post = snapshots.get("post_worker_pre_cbr_commit")
    terminal = snapshots.get("terminal_closure")
    execution_id = _linked_execution_id(task)
    reasons: list[str] = []
    if conflicts:
        reasons.append("conflicting_snapshot")
    if not pre or not post or not terminal:
        reasons.append("missing_snapshot")
    selected = [item for item in (pre, post, terminal) if item]
    if any(not item["scope"]["isolated_execution_root"] for item in selected):
        reasons.append("non_isolated_execution_root")
    if len({item["scope"]["repository_identity_digest"] for item in selected}) > 1:
        reasons.append("ambiguous_repository_identity")
    if any(
        not item["repository"]["observed"]
        or not item["repository"]["head"]
        or not item["repository"]["head_tree"]
        or not item["repository"]["status_digest"]
        for item in selected
    ):
        reasons.append("repository_observation_unavailable")
    if task.get("resume_requested") or task.get("resume_unavailable"):
        reasons.append("resume_or_crash_gap")
    if selected:
        times = [
            _timestamp(item["captured_at"], f"{item['phase']}.captured_at")
            for item in (pre, post, terminal)
            if item
        ]
        if times != sorted(times) or any(item > observed for item in times):
            reasons.append("invalid_snapshot_chronology")

    worker_changed = False
    cbr_changed = False
    pre_existing_dirt = False
    unsafe_or_unreported = False
    worker_created_commit = False
    if pre and post and terminal:
        pre_repo = pre["repository"]
        post_repo = post["repository"]
        terminal_repo = terminal["repository"]
        pre_existing_dirt = bool(pre_repo["dirty"])
        worker_created_commit = bool(
            pre_repo["head"] and post_repo["head"]
            and pre_repo["head"] != post_repo["head"]
        )
        unsafe_or_unreported = bool(
            post_repo["unsafe_reported_count"]
            or post_repo["unreported_changed_count"]
            or (
                post_repo["reported_not_observed_count"]
                and not worker_created_commit
            )
        )
        worker_changed = bool(
            worker_created_commit
            or pre_repo["status_digest"] != post_repo["status_digest"]
        )
        cbr_changed = post_repo["head"] != terminal_repo["head"]
        if pre_existing_dirt:
            reasons.append("pre_existing_dirt")
        if unsafe_or_unreported:
            reasons.append("unsafe_or_unreported_paths")
        if worker_created_commit:
            reasons.append("worker_created_commit")
        if task.get("execution_commit") and terminal_repo["head"] != task.get(
            "execution_commit"
        ):
            reasons.append("cbr_commit_digest_mismatch")

    hard_unknown = {
        "conflicting_snapshot",
        "missing_snapshot",
        "non_isolated_execution_root",
        "ambiguous_repository_identity",
        "resume_or_crash_gap",
        "cbr_commit_digest_mismatch",
        "repository_observation_unavailable",
        "invalid_snapshot_chronology",
    }
    if hard_unknown.intersection(reasons):
        provenance = "unknown"
    elif pre_existing_dirt or unsafe_or_unreported:
        provenance = "mutation_possible"
    elif worker_changed or cbr_changed:
        provenance = "mutation_observed"
    else:
        provenance = "no_mutation"
    body = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT_VERSION,
        "kind": "execution_mutation_provenance",
        "recorded_at": observed.isoformat(),
        "binding": {
            "task_id": task_id,
            "attempt": attempt,
            "execution_evidence_id": execution_id,
        },
        "scope": {
            "name": SCOPE,
            "global_side_effects_verified": False,
            "external_services_verified": False,
            "arbitrary_filesystem_verified": False,
            "network_side_effects_verified": False,
        },
        "snapshot_ids": {
            phase: snapshots[phase]["snapshot_id"] if phase in snapshots else None
            for phase in sorted(PHASES)
        },
        "attribution": {
            "worker_observed_changes": worker_changed,
            "cbr_created_commit_or_state_changes": cbr_changed,
            "pre_existing_dirt": pre_existing_dirt,
            "retained_recovery_state": bool(
                terminal
                and terminal["task_review_state"]["retained_recovery_state"]
            ),
            "unsafe_or_unreported_paths": unsafe_or_unreported,
            "worker_created_commit": worker_created_commit,
        },
        "provenance": provenance,
        "global_provenance": "unknown",
        "fail_closed_reasons": sorted(set(reasons)),
        "producer_revision": _safe_id(producer_revision, "producer_revision"),
        "privacy": {
            "raw_paths_included": False,
            "raw_prompt_included": False,
            "raw_transcript_included": False,
            "session_or_thread_ids_included": False,
            "credentials_included": False,
            "private_identity_included": False,
        },
        "report_only": True,
        "routing_mutation_allowed": False,
        "promotion_authority": False,
        "worker_certification_projection_allowed": False,
    }
    body["provenance_id"] = _stable_id(body)
    return validate_execution_mutation_provenance(body)


def attach_execution_mutation_provenance(
    task: dict[str, Any], record: dict[str, Any]
) -> None:
    validated = validate_execution_mutation_provenance(record)
    if (
        validated["binding"]["task_id"] != str(task.get("id"))
        or validated["binding"]["attempt"] != task.get("attempts")
        or validated["binding"]["execution_evidence_id"] != _linked_execution_id(task)
    ):
        raise ExecutionMutationProvenanceError(
            "mutation provenance binding does not match task"
        )
    expected = build_execution_mutation_provenance(
        task,
        recorded_at=_timestamp(validated["recorded_at"], "recorded_at"),
        producer_revision=validated["producer_revision"],
    )
    if validated != expected:
        raise ExecutionMutationProvenanceError(
            "mutation provenance does not match canonical task snapshots"
        )
    history = task.setdefault("execution_mutation_provenance_history", [])
    if not isinstance(history, list):
        raise ExecutionMutationProvenanceError(
            "mutation provenance history must be a list"
        )
    if not any(
        isinstance(item, dict)
        and item.get("provenance_id") == validated["provenance_id"]
        for item in history
    ):
        history.append(validated)


def latest_execution_mutation_provenance(
    task: dict[str, Any],
    *,
    as_of: datetime | None = None,
) -> dict[str, Any] | None:
    history = task.get("execution_mutation_provenance_history")
    if not isinstance(history, list):
        return None
    task_id = str(task.get("id"))
    attempt = task.get("attempts")
    execution_id = _linked_execution_id(task, required=False)
    matches = []
    for item in history:
        validated = validate_execution_mutation_provenance(item)
        if as_of is not None and _timestamp(
            validated["recorded_at"], "recorded_at"
        ) > _aware_utc(as_of, "as_of"):
            raise ExecutionMutationProvenanceError(
                "future mutation provenance is ineligible"
            )
        binding = validated["binding"]
        if (
            binding["task_id"] == task_id
            and binding["attempt"] == attempt
            and binding["execution_evidence_id"] == execution_id
        ):
            matches.append(validated)
    if not matches:
        return None
    ids = {item["provenance_id"] for item in matches}
    if len(ids) != 1:
        raise ExecutionMutationProvenanceError(
            "conflicting mutation provenance records"
        )
    return matches[-1]


def execution_mutation_provenance_view(
    task: dict[str, Any], *, as_of: datetime | None = None
) -> dict[str, Any]:
    record = latest_execution_mutation_provenance(
        task, as_of=as_of or datetime.now(timezone.utc)
    )
    if record is not None:
        return record
    return {
        "schema_version": 0,
        "contract": "legacy-execution-mutation-provenance-unknown",
        "scope": {"name": SCOPE, "global_side_effects_verified": False},
        "provenance": "unknown",
        "global_provenance": "unknown",
        "fail_closed_reasons": ["missing_provenance_record"],
        "report_only": True,
        "routing_mutation_allowed": False,
        "promotion_authority": False,
        "worker_certification_projection_allowed": False,
    }


def validate_execution_mutation_snapshot(value: object) -> dict[str, Any]:
    expected = {
        "schema_version", "contract", "kind", "captured_at", "phase",
        "binding", "scope", "repository", "task_review_state", "privacy",
        "report_only", "mutation_allowed", "snapshot_id",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ExecutionMutationProvenanceError(
            "mutation snapshot fields are not canonical"
        )
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["contract"] != SNAPSHOT_CONTRACT_VERSION
        or value["kind"] != "execution_mutation_snapshot"
        or value["phase"] not in PHASES
        or value["report_only"] is not True
        or value["mutation_allowed"] is not False
    ):
        raise ExecutionMutationProvenanceError("invalid mutation snapshot contract")
    _timestamp(value["captured_at"], "captured_at")
    if (
        not isinstance(value["binding"], dict)
        or set(value["binding"]) != {"task_id", "attempt"}
        or not isinstance(value["scope"], dict)
        or set(value["scope"]) != {
            "name", "isolated_execution_root", "repository_identity_digest",
            "worktree_identity_digest",
        }
        or value["scope"]["name"] != SCOPE
        or not isinstance(value["scope"]["isolated_execution_root"], bool)
        or not _is_digest(value["scope"]["repository_identity_digest"])
        or not _is_digest(value["scope"]["worktree_identity_digest"])
    ):
        raise ExecutionMutationProvenanceError("invalid mutation snapshot binding or scope")
    _safe_id(value["binding"].get("task_id"), "binding.task_id")
    _count(value["binding"].get("attempt"), "binding.attempt")
    repository_keys = {
        "observed", "head", "head_tree", "dirty", "staged_count",
        "unstaged_count", "untracked_count", "status_digest",
        "reported_file_count", "unsafe_reported_count",
        "unreported_changed_count", "reported_not_observed_count",
    }
    state_keys = {
        "state_digest", "status", "review_status", "worktree_status",
        "cbr_commit_present", "retained_recovery_state",
    }
    if (
        not isinstance(value["repository"], dict)
        or set(value["repository"]) != repository_keys
        or not isinstance(value["repository"]["observed"], bool)
        or not isinstance(value["task_review_state"], dict)
        or set(value["task_review_state"]) != state_keys
        or not _is_digest(value["task_review_state"]["state_digest"])
    ):
        raise ExecutionMutationProvenanceError("invalid mutation snapshot observation")
    for field in (
        "staged_count", "unstaged_count", "untracked_count",
        "unsafe_reported_count", "unreported_changed_count",
        "reported_not_observed_count",
    ):
        _count(value["repository"][field], f"repository.{field}")
    _validate_common_privacy(value)
    claimed = value["snapshot_id"]
    body = dict(value)
    body.pop("snapshot_id")
    if claimed != _stable_id(body):
        raise ExecutionMutationProvenanceError("mutation snapshot digest mismatch")
    return value


def validate_execution_mutation_provenance(value: object) -> dict[str, Any]:
    expected = {
        "schema_version", "contract", "kind", "recorded_at", "binding", "scope",
        "snapshot_ids", "attribution", "provenance", "global_provenance",
        "fail_closed_reasons", "producer_revision", "privacy", "report_only",
        "routing_mutation_allowed", "promotion_authority",
        "worker_certification_projection_allowed", "provenance_id",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ExecutionMutationProvenanceError(
            "mutation provenance fields are not canonical"
        )
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["contract"] != CONTRACT_VERSION
        or value["kind"] != "execution_mutation_provenance"
        or value["provenance"] not in PROVENANCE
        or value["global_provenance"] != "unknown"
        or value["report_only"] is not True
        or value["routing_mutation_allowed"] is not False
        or value["promotion_authority"] is not False
        or value["worker_certification_projection_allowed"] is not False
    ):
        raise ExecutionMutationProvenanceError(
            "invalid mutation provenance contract"
        )
    _timestamp(value["recorded_at"], "recorded_at")
    binding = value["binding"]
    scope = value["scope"]
    snapshots = value["snapshot_ids"]
    attribution = value["attribution"]
    if (
        not isinstance(binding, dict)
        or set(binding) != {"task_id", "attempt", "execution_evidence_id"}
        or not isinstance(scope, dict)
        or set(scope) != {
            "name", "global_side_effects_verified", "external_services_verified",
            "arbitrary_filesystem_verified", "network_side_effects_verified",
        }
        or scope["name"] != SCOPE
        or any(scope[field] is not False for field in scope if field != "name")
        or not isinstance(snapshots, dict)
        or set(snapshots) != PHASES
        or not isinstance(attribution, dict)
        or set(attribution) != {
            "worker_observed_changes", "cbr_created_commit_or_state_changes",
            "pre_existing_dirt", "retained_recovery_state",
            "unsafe_or_unreported_paths", "worker_created_commit",
        }
        or any(not isinstance(item, bool) for item in attribution.values())
        or not isinstance(value["fail_closed_reasons"], list)
        or not all(isinstance(item, str) and item for item in value["fail_closed_reasons"])
    ):
        raise ExecutionMutationProvenanceError(
            "invalid mutation provenance binding or observation"
        )
    _safe_id(binding.get("task_id"), "binding.task_id")
    _count(binding.get("attempt"), "binding.attempt")
    _safe_id(binding.get("execution_evidence_id"), "binding.execution_evidence_id")
    for snapshot_id in snapshots.values():
        if snapshot_id is not None and not _is_digest(snapshot_id):
            raise ExecutionMutationProvenanceError("invalid mutation snapshot reference")
    _safe_id(value["producer_revision"], "producer_revision")
    _validate_common_privacy(value)
    claimed = value["provenance_id"]
    body = dict(value)
    body.pop("provenance_id")
    if claimed != _stable_id(body):
        raise ExecutionMutationProvenanceError("mutation provenance digest mismatch")
    return value


def _inspect_repository(
    root: Path | None, reported_changed_files: object
) -> dict[str, Any]:
    empty = {
        "observed": False,
        "head": None,
        "head_tree": None,
        "dirty": None,
        "staged_count": 0,
        "unstaged_count": 0,
        "untracked_count": 0,
        "status_digest": None,
        "reported_file_count": None,
        "unsafe_reported_count": 0,
        "unreported_changed_count": 0,
        "reported_not_observed_count": 0,
    }
    if root is None:
        return empty
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    tree = _git(root, "rev-parse", "--verify", "HEAD^{tree}")
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if any(item.returncode != 0 for item in (head, tree, status)):
        return empty
    entries = _porcelain_entries(status.stdout)
    actual = {entry[2] for entry in entries}
    safe, unsafe = _safe_reported_paths(reported_changed_files)
    return {
        "observed": True,
        "head": _commit(head.stdout.strip()),
        "head_tree": _commit(tree.stdout.strip()),
        "dirty": bool(entries),
        "staged_count": sum(entry[0] not in {" ", "?"} for entry in entries),
        "unstaged_count": sum(entry[1] not in {" ", "?"} for entry in entries),
        "untracked_count": sum(entry[:2] == ("?", "?") for entry in entries),
        "status_digest": _stable_id(sorted((x, y, name) for x, y, name in entries)),
        "reported_file_count": len(safe) if reported_changed_files is not None else None,
        "unsafe_reported_count": len(unsafe),
        "unreported_changed_count": len(actual - safe) if reported_changed_files is not None else 0,
        "reported_not_observed_count": len(safe - actual) if reported_changed_files is not None else 0,
    }


def _porcelain_entries(output: str) -> list[tuple[str, str, str]]:
    values = output.split("\0")
    entries: list[tuple[str, str, str]] = []
    index = 0
    while index < len(values):
        value = values[index]
        index += 1
        if not value:
            continue
        if len(value) < 4:
            continue
        x, y, name = value[0], value[1], value[3:]
        if x in {"R", "C"} and index < len(values):
            index += 1
        entries.append((x, y, name))
    return entries


def _safe_reported_paths(value: object) -> tuple[set[str], set[str]]:
    if not isinstance(value, list):
        return set(), set()
    safe: set[str] = set()
    unsafe: set[str] = set()
    for item in value:
        text = str(item).strip()
        pure = PurePath(text)
        if not text or pure.is_absolute() or ".." in pure.parts or ".git" in pure.parts:
            unsafe.add(text or "<empty>")
        else:
            safe.add(text)
    return safe, unsafe


def _attempt_snapshots(
    task: dict[str, Any], task_id: str, attempt: int
) -> tuple[dict[str, dict[str, Any]], bool]:
    history = task.get("execution_mutation_snapshot_history")
    if not isinstance(history, list):
        return {}, False
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in history:
        validated = validate_execution_mutation_snapshot(item)
        if validated["binding"] == {"task_id": task_id, "attempt": attempt}:
            grouped.setdefault(validated["phase"], []).append(validated)
    conflicts = any(
        len({item["snapshot_id"] for item in values}) > 1
        for values in grouped.values()
    )
    return {phase: values[-1] for phase, values in grouped.items()}, conflicts


def _linked_execution_id(task: dict[str, Any], *, required: bool = True) -> str | None:
    last_run = task.get("last_run")
    value = last_run.get("execution_evidence_id") if isinstance(last_run, dict) else None
    if isinstance(value, str) and value:
        return value
    if required:
        raise ExecutionMutationProvenanceError(
            "linked execution evidence is unavailable"
        )
    return None


def _task_review_state(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": task.get("status"),
        "review_status": task.get("review_status"),
        "execution_worktree_status": task.get("execution_worktree_status"),
        "execution_commit": bool(task.get("execution_commit")),
        "execution_apply_status": task.get("execution_apply_status"),
        "attempts": task.get("attempts"),
    }


def _validate_common_privacy(value: dict[str, Any]) -> None:
    for key in FORBIDDEN_KEYS:
        if _contains_key(value, key):
            raise ExecutionMutationProvenanceError(
                f"mutation evidence contains forbidden key: {key}"
            )
    privacy = value.get("privacy")
    if not isinstance(privacy, dict) or any(item is not False for item in privacy.values()):
        raise ExecutionMutationProvenanceError(
            "mutation evidence privacy flags must all be false"
        )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def _contains_key(value: object, needle: str) -> bool:
    if isinstance(value, dict):
        return any(key == needle or _contains_key(item, needle) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_key(item, needle) for item in value)
    return False


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value[7:]
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)


def _stable_id(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_id(value: object, name: str) -> str:
    text = str(value or "")
    if not text or len(text) > 256 or any(char.isspace() for char in text):
        raise ExecutionMutationProvenanceError(f"{name} is not a safe identifier")
    return text


def _safe_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= 256 and "\n" not in text else None


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutionMutationProvenanceError(f"{name} must be a non-negative integer")
    return value


def _commit(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) != 40 or any(char not in "0123456789abcdef" for char in text.lower()):
        return None
    return text.lower()


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ExecutionMutationProvenanceError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ExecutionMutationProvenanceError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionMutationProvenanceError(f"{name} must be an ISO timestamp") from exc
    return _aware_utc(parsed, name)

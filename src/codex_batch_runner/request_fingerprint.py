from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from typing import Any

SCHEMA_VERSION = 1
PREPROCESSING_VERSION = "request-fingerprint-v1"
DEFAULT_CANDIDATE_LIMIT = 20

TEXT_FIELDS = ("title", "description", "prompt")
METADATA_FIELDS = (
    "project_id",
    "category",
    "labels",
    "routing_size",
    "routing_risk",
    "verification_scope",
    "routing_risk_factors",
    "execution_backend",
    "routing_experiment",
)
PATH_FIELDS = ("cwd", "project_root", "worktree_path", "execution_worktree_path")
LINEAGE_FIELDS = (
    "root_task_id",
    "parent_task_id",
    "source_task_id",
    "subtask_type",
    "subtask_for",
    "review_followup_for",
    "review_cycle",
)

_ABSOLUTE_PATH_RE = re.compile(
    r"""
    (?:
        (?<![\w.-])
        (?:/(?:Users|home|private|var|tmp|Volumes|opt|workspace|mnt)/[^\s'"<>),;]+)
    )
    |
    (?:
        (?<![\w.-])
        [A-Za-z]:\\[^\s'"<>),;]+
    )
    """,
    re.VERBOSE,
)
_TASK_ID_RE = re.compile(r"\btask-\d{4}-\d{2}-\d{2}t[0-9a-z-]+\b", re.IGNORECASE)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_SESSION_THREAD_RE = re.compile(r"\b(?:session|thread)[_-]?[a-z0-9_.:-]{8,}\b", re.IGNORECASE)
_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[ tT]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.:/@+-]*", re.IGNORECASE)


def derive_request_fingerprint(task: dict[str, Any]) -> dict[str, Any]:
    """Derive public-safe deterministic request identity hints from an in-memory task."""
    title_text = _normalize_text(_string_value(task.get("title")))
    body_parts = [_string_value(task.get(field)) for field in ("description", "prompt")]
    body_text = _normalize_text("\n\n".join(part for part in body_parts if part))
    normalized_text = _normalize_text(f"title: {title_text}\nbody: {body_text}")
    metadata_hints = _metadata_hints(task)
    metadata_hash_input = _canonical_json(metadata_hints)

    normalized_text_hash = _sha256(normalized_text)
    title_hash = _sha256(title_text)
    metadata_hash = _sha256(metadata_hash_input)

    return {
        "schema_version": SCHEMA_VERSION,
        "fingerprint_id": "rfp-" + _sha256(f"{normalized_text_hash}\n{metadata_hash}").split(":", 1)[1][:16],
        "task_id_hash": _safe_id_hash(task.get("id") or task.get("task_id")),
        "preprocessing_version": PREPROCESSING_VERSION,
        "source_fields": _source_fields(task),
        "text_stats": {
            "title_length": len(title_text),
            "body_length": len(body_text),
            "normalized_text_length": len(normalized_text),
            "token_count_bucket": _token_count_bucket(len(_tokens(normalized_text))),
        },
        "hashes": {
            "normalized_text_hash": normalized_text_hash,
            "title_hash": title_hash,
            "metadata_hash": metadata_hash,
            "simhash64": _simhash64(normalized_text),
        },
        "metadata_hints": metadata_hints,
        "lineage_hints": _lineage_hints(task),
        "privacy": {
            "raw_text_stored": False,
            "private_paths_redacted": True,
            "prompt_hash_only": True,
        },
    }


def find_request_fingerprint_candidates(
    tasks: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    """Find read-only local candidate groups from safe deterministic fingerprints."""
    fingerprinted = [(task, derive_request_fingerprint(task)) for task in tasks]
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for task, fingerprint in fingerprinted:
        hashes = fingerprint.get("hashes") if isinstance(fingerprint.get("hashes"), dict) else {}
        normalized_text_hash = str(hashes.get("normalized_text_hash") or "")
        if normalized_text_hash:
            groups.setdefault(normalized_text_hash, []).append((task, fingerprint))

    candidates = [
        _exact_duplicate_candidate(normalized_text_hash, group)
        for normalized_text_hash, group in groups.items()
        if len(group) > 1
    ]
    candidates.sort(
        key=lambda candidate: (
            -int(candidate["task_count"]),
            str(candidate["candidate_type"]),
            str(candidate["candidate_id"]),
        )
    )
    if limit > 0:
        candidates = candidates[:limit]
    return {
        "schema_version": SCHEMA_VERSION,
        "preprocessing_version": PREPROCESSING_VERSION,
        "candidate_count": len(candidates),
        "candidate_types": _candidate_type_counts(candidates),
        "candidates": candidates,
        "advisory": {
            "read_only": True,
            "mutation_allowed": False,
            "candidate_limit": limit,
            "implemented_candidate_types": ["exact_duplicate"],
        },
        "privacy": {
            "raw_text_included": False,
            "raw_normalized_text_included": False,
            "raw_paths_included": False,
            "session_or_thread_ids_included": False,
        },
    }


def _exact_duplicate_candidate(
    normalized_text_hash: str,
    group: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    summaries = [_candidate_task_summary(task, fingerprint) for task, fingerprint in group]
    summaries.sort(key=lambda summary: str(summary.get("task_id") or ""))
    candidate_id = "rfpc-" + _sha256(f"exact_duplicate\n{normalized_text_hash}").split(":", 1)[1][:16]
    return {
        "candidate_type": "exact_duplicate",
        "candidate_id": candidate_id,
        "task_count": len(summaries),
        "task_ids": [summary["task_id"] for summary in summaries],
        "task_id_hashes": [summary["task_id_hash"] for summary in summaries if summary.get("task_id_hash")],
        "task_summaries": summaries,
        "evidence": {
            "basis": "normalized_text_hash",
            "normalized_text_hash_match": True,
            "distinct_fingerprint_ids": sorted({str(summary.get("fingerprint_id")) for summary in summaries}),
            "task_bucket_keys": sorted({str(summary.get("task_bucket_key")) for summary in summaries}),
            "project_root_hashes": sorted(
                {str(summary.get("project_root_hash")) for summary in summaries if summary.get("project_root_hash")}
            ),
            "cwd_classes": sorted({str(summary.get("cwd_class")) for summary in summaries}),
            "path_classes": sorted(
                {
                    str(path_class)
                    for summary in summaries
                    for path_class in summary.get("path_classes", [])
                    if path_class
                }
            ),
        },
        "privacy": {
            "raw_text_included": False,
            "raw_normalized_text_included": False,
            "raw_paths_included": False,
        },
    }


def _candidate_task_summary(task: dict[str, Any], fingerprint: dict[str, Any]) -> dict[str, Any]:
    metadata_hints = fingerprint.get("metadata_hints") if isinstance(fingerprint.get("metadata_hints"), dict) else {}
    text_stats = fingerprint.get("text_stats") if isinstance(fingerprint.get("text_stats"), dict) else {}
    lineage_hints = fingerprint.get("lineage_hints") if isinstance(fingerprint.get("lineage_hints"), dict) else {}
    return {
        "task_id": _safe_metadata_value(task.get("id") or task.get("task_id")),
        "task_id_hash": fingerprint.get("task_id_hash"),
        "fingerprint_id": fingerprint.get("fingerprint_id"),
        "status": _safe_metadata_value(task.get("status")),
        "review_status": _safe_metadata_value(task.get("review_status")),
        "task_bucket_key": _safe_metadata_value(metadata_hints.get("task_bucket_key")),
        "project_id": _safe_metadata_value(metadata_hints.get("project_id")),
        "category": _safe_metadata_value(metadata_hints.get("category")),
        "cwd_class": _safe_metadata_value(metadata_hints.get("cwd_class")),
        "project_root_hash": metadata_hints.get("project_root_hash"),
        "path_classes": list(metadata_hints.get("path_classes") or []),
        "token_count_bucket": _safe_metadata_value(text_stats.get("token_count_bucket")),
        "lineage_present": any(_has_value(value) for value in lineage_hints.values()),
    }


def _candidate_type_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        candidate_type = str(candidate.get("candidate_type") or "unknown")
        counts[candidate_type] = counts.get(candidate_type, 0) + 1
    return dict(sorted(counts.items()))


def _source_fields(task: dict[str, Any]) -> list[str]:
    fields = [field for field in (*TEXT_FIELDS, *METADATA_FIELDS, *PATH_FIELDS, *LINEAGE_FIELDS) if _has_value(task.get(field))]
    return sorted(fields)


def _metadata_hints(task: dict[str, Any]) -> dict[str, Any]:
    labels = _normalized_list(task.get("labels"))
    verification_scope = _normalized_list(task.get("verification_scope"))
    risk_factors = _normalized_list(task.get("routing_risk_factors"))
    path_values = [_string_value(task.get(field)) for field in PATH_FIELDS if _has_value(task.get(field))]
    text_path_classes = _path_classes_from_text("\n".join(_string_value(task.get(field)) for field in TEXT_FIELDS))
    path_classes = sorted(set(text_path_classes + [_classify_path(value) for value in path_values]))

    hints: dict[str, Any] = {
        "project_id": _safe_metadata_value(task.get("project_id")),
        "category": _safe_metadata_value(task.get("category")),
        "labels": labels,
        "routing_size": _safe_metadata_value(task.get("routing_size")),
        "routing_risk": _safe_metadata_value(task.get("routing_risk")),
        "verification_scope": verification_scope,
        "routing_risk_factors": risk_factors,
        "path_classes": [value for value in path_classes if value != "unknown"] or ["unknown"],
        "cwd_class": _cwd_class(task),
        "task_bucket_key": _task_bucket_key(task, verification_scope),
    }
    project_root_hash = _path_hash(task.get("project_root") or task.get("cwd"))
    if project_root_hash:
        hints["project_root_hash"] = project_root_hash
    execution_backend = _safe_metadata_value(task.get("execution_backend"))
    if execution_backend != "unknown":
        hints["execution_backend"] = execution_backend
    routing_experiment = _safe_metadata_value(task.get("routing_experiment"))
    if routing_experiment != "unknown":
        hints["routing_experiment"] = routing_experiment
    return hints


def _lineage_hints(task: dict[str, Any]) -> dict[str, Any]:
    hints: dict[str, Any] = {}
    for field in LINEAGE_FIELDS:
        value = task.get(field)
        if not _has_value(value):
            hints[field] = None
        elif field.endswith("_task_id"):
            hints[field] = _safe_id_hash(value)
        elif field == "review_cycle":
            hints[field] = value if isinstance(value, int) else _safe_metadata_value(value)
        else:
            hints[field] = _safe_metadata_value(value)
    return hints


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    value = _ABSOLUTE_PATH_RE.sub(lambda match: f" <path:{_classify_path(match.group(0))}> ", value)
    value = _TASK_ID_RE.sub(" <task_id> ", value)
    value = _UUID_RE.sub(" <uuid> ", value)
    value = _SESSION_THREAD_RE.sub(" <volatile_id> ", value)
    value = _TIMESTAMP_RE.sub(" <timestamp> ", value)
    value = value.lower()
    return " ".join(value.split())


def _normalized_scalar(value: Any) -> str:
    return _normalize_text(_string_value(value))


def _normalized_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
        values = value
    else:
        values = [value]
    normalized = {_safe_metadata_value(item) for item in values if _has_value(item)}
    normalized.discard("unknown")
    return sorted(normalized)


def _safe_metadata_value(value: Any) -> str:
    normalized = _normalized_scalar(value)
    if not normalized:
        return "unknown"
    if _looks_path_like(normalized):
        return "hash:" + _sha256(normalized).split(":", 1)[1][:16]
    return normalized


def _task_bucket_key(task: dict[str, Any], verification_scope: list[str]) -> str:
    size = _safe_metadata_value(task.get("routing_size"))
    risk = _safe_metadata_value(task.get("routing_risk"))
    scopes = ",".join(verification_scope) if verification_scope else "none"
    return f"size={size} risk={risk} verify={scopes}"


def _cwd_class(task: dict[str, Any]) -> str:
    cwd = _string_value(task.get("cwd"))
    root = _string_value(task.get("project_root"))
    if not cwd:
        return "unknown"
    if root and _normalize_path(cwd) == _normalize_path(root):
        return "repo_root"
    if root and _normalize_path(cwd).startswith(_normalize_path(root).rstrip("/") + "/"):
        return "subdir"
    if _looks_path_like(cwd):
        return "outside_repo"
    return "unknown"


def _path_classes_from_text(value: str) -> list[str]:
    return [_classify_path(match.group(0)) for match in _ABSOLUTE_PATH_RE.finditer(value)]


def _classify_path(value: str) -> str:
    normalized = _normalize_path(value)
    parts = [part for part in re.split(r"[/\\]+", normalized.lower()) if part]
    if ".git" in parts:
        return "git"
    if ".private" in parts or "private" in parts:
        return "private_docs"
    if ".codex-batch-runner" in parts or "worktrees" in parts or "runtime" in parts or "logs" in parts or "queue" in parts:
        return "runtime_state"
    if "src" in parts or "lib" in parts:
        return "source"
    if "tests" in parts or "test" in parts:
        return "tests"
    if "examples" in parts or "example" in parts:
        return "examples"
    if "docs" in parts or "readme.md" in parts:
        return "public_docs"
    if any(part.endswith((".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".plist")) for part in parts):
        return "config"
    if "worktree" in parts or "worktrees" in parts:
        return "worktree"
    return "unknown"


def _path_hash(value: Any) -> str | None:
    text = _string_value(value)
    if not text:
        return None
    return _sha256(_normalize_path(text))


def _safe_id_hash(value: Any) -> str | None:
    text = _normalized_scalar(value)
    if not text:
        return None
    return _sha256(text)


def _simhash64(value: str) -> str:
    tokens = _tokens(value)
    if not tokens:
        return "0000000000000000"
    weights = [0] * 64
    for token, count in Counter(tokens).items():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        token_int = int.from_bytes(digest[:8], "big")
        for bit in range(64):
            if token_int & (1 << bit):
                weights[bit] += count
            else:
                weights[bit] -= count
    result = 0
    for bit, weight in enumerate(weights):
        if weight >= 0:
            result |= 1 << bit
    return f"{result:016x}"


def _tokens(value: str) -> list[str]:
    return _TOKEN_RE.findall(value)


def _token_count_bucket(count: int) -> str:
    if count <= 50:
        return "0-50"
    if count <= 200:
        return "51-200"
    if count <= 1000:
        return "201-1000"
    if count <= 4000:
        return "1001-4000"
    return "4000+"


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _has_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _looks_path_like(value: str) -> bool:
    return bool(_ABSOLUTE_PATH_RE.search(value)) or value.startswith(("~/", "./", "../")) or "\\" in value


def _normalize_path(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\\", "/")
    return re.sub(r"/+", "/", value).rstrip("/")

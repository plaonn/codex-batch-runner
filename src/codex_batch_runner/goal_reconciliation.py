"""Strict, public-safe source goal manifests and read-only reconciliation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .orchestration_selection import stable_digest
from .orchestration_selection_funnel import (
    SelectionFunnelError,
    validate_selection_funnel,
)


MANIFEST_CONTRACT = "source-goal-manifest-v1"
REPORT_CONTRACT = "goal-reconciliation-report-v1"
EVIDENCE_CONTRACT = "goal-reconciliation-evidence-v1"
STATUSES = frozenset({"observed", "not_observed", "unknown", "not_applicable"})
AXES = (
    "contract_binding",
    "selection",
    "admission",
    "execution",
    "review",
    "apply",
    "attention_recorded",
    "attention_delivered",
    "attention_acknowledged",
    "parent_collection",
    "source_disposition",
)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
ABSOLUTE_PATH = re.compile(r"(?:^|\s)/(?:[^\s/]+/)*[^\s]*")
SENSITIVE_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|secret|token|password|credential)\s*=", re.IGNORECASE
)
FORBIDDEN = frozenset(
    {
        "prompt",
        "raw_prompt",
        "transcript",
        "credential",
        "credentials",
        "session_id",
        "thread_id",
        "user_id",
        "account_id",
        "path",
        "command",
        "argv",
        "todoist",
        "email",
        "token",
        "secret",
        "private",
    }
)
MAX_BYTES = 128 * 1024


class GoalReconciliationError(ValueError):
    pass


def load_goal_manifest(path: str | Path) -> dict[str, Any]:
    return validate_goal_manifest(_load(path, "manifest"))


def load_goal_evidence(path: str | Path) -> dict[str, Any]:
    return validate_goal_evidence(_load(path, "evidence"))


def _load(path: str | Path, kind: str) -> object:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise GoalReconciliationError(f"{kind} unreadable") from exc
    if len(raw) > MAX_BYTES:
        raise GoalReconciliationError(f"{kind} too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoalReconciliationError(f"{kind} is not valid JSON") from exc


def validate_goal_manifest(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GoalReconciliationError("manifest must be an object")
    _reject_private(value)
    expected = {
        "schema_version",
        "contract",
        "goal_id",
        "source",
        "revision",
        "manifest_digest",
        "root_outcome",
        "decision_authority",
        "automation_boundary",
        "nodes",
        "terminal_condition",
        "supersedes",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or value.get("contract") != MANIFEST_CONTRACT
    ):
        raise GoalReconciliationError("manifest fields are invalid")
    goal_id = _id("goal_id", value["goal_id"])
    source = _object(
        "source", value["source"], {"owner_kind", "source_ref", "adapter_revision"}
    )
    if source["owner_kind"] not in {"source_project", "external_adapter"}:
        raise GoalReconciliationError("source owner kind is invalid")
    source = {key: _id(f"source.{key}", item) for key, item in source.items()}
    revision = value["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise GoalReconciliationError("revision must be a positive integer")
    root = _object(
        "root_outcome", value["root_outcome"], {"summary", "acceptance_references"}
    )
    root = {
        "summary": _text("root outcome summary", root["summary"]),
        "acceptance_references": _id_list(
            "root acceptance references", root["acceptance_references"]
        ),
    }
    authority = _object(
        "decision_authority", value["decision_authority"], {"goal", "node_default"}
    )
    authority = {key: _authority(key, item) for key, item in authority.items()}
    boundary = _object(
        "automation_boundary",
        value["automation_boundary"],
        {"allowed_mutations", "prohibited_mutations", "attention_gates"},
    )
    if boundary["allowed_mutations"] != ["read_only"] or not {
        "dispatch",
        "source_write",
        "delivery",
        "acknowledgement",
        "completion",
    } <= set(boundary["prohibited_mutations"]):
        raise GoalReconciliationError("automation boundary must be read-only")
    boundary = {
        "allowed_mutations": ["read_only"],
        "prohibited_mutations": _id_list(
            "prohibited mutations", boundary["prohibited_mutations"]
        ),
        "attention_gates": _id_list("attention gates", boundary["attention_gates"]),
    }
    nodes = value["nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise GoalReconciliationError("nodes must be a non-empty list")
    canonical_nodes = [_node(item) for item in nodes]
    ids = [item["node_id"] for item in canonical_nodes]
    if len(ids) != len(set(ids)):
        raise GoalReconciliationError("node IDs must be unique")
    known = set(ids)
    for node in canonical_nodes:
        if (
            not set(node["dependencies"]) <= known
            or node["node_id"] in node["dependencies"]
        ):
            raise GoalReconciliationError(
                "node dependency is dangling or self-referential"
            )
    _no_cycles(canonical_nodes)
    terminal = _object(
        "terminal_condition",
        value["terminal_condition"],
        {"required_nodes", "root_acceptance_references"},
    )
    terminal = {
        "required_nodes": _id_list(
            "required terminal nodes", terminal["required_nodes"]
        ),
        "root_acceptance_references": _id_list(
            "terminal acceptance references", terminal["root_acceptance_references"]
        ),
    }
    if not terminal["required_nodes"] or not set(terminal["required_nodes"]) <= known:
        raise GoalReconciliationError("terminal required nodes are invalid")
    supersedes = value["supersedes"]
    if supersedes is not None:
        supersedes = _object(
            "supersedes", supersedes, {"goal_id", "revision", "manifest_digest"}
        )
        if (
            _id("supersedes.goal_id", supersedes["goal_id"]) != goal_id
            or not isinstance(supersedes["revision"], int)
            or supersedes["revision"] < 1
            or supersedes["revision"] >= revision
        ):
            raise GoalReconciliationError("supersedes is invalid")
        _digest("supersedes.manifest_digest", supersedes["manifest_digest"])
    canonical = {
        "schema_version": 1,
        "contract": MANIFEST_CONTRACT,
        "goal_id": goal_id,
        "source": source,
        "revision": revision,
        "root_outcome": root,
        "decision_authority": authority,
        "automation_boundary": boundary,
        "nodes": canonical_nodes,
        "terminal_condition": terminal,
        "supersedes": supersedes,
    }
    _digest("manifest_digest", value["manifest_digest"])
    if value["manifest_digest"] != stable_digest(canonical):
        raise GoalReconciliationError("manifest digest is invalid")
    canonical["manifest_digest"] = value["manifest_digest"]
    return canonical


def validate_goal_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GoalReconciliationError("evidence must be an object")
    _reject_private(value)
    expected = {
        "schema_version",
        "contract",
        "goal_id",
        "revision",
        "manifest_digest",
        "nodes",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or value.get("contract") != EVIDENCE_CONTRACT
    ):
        raise GoalReconciliationError("evidence fields are invalid")
    _id("evidence.goal_id", value["goal_id"])
    _digest("evidence.manifest_digest", value["manifest_digest"])
    if (
        not isinstance(value["revision"], int)
        or isinstance(value["revision"], bool)
        or value["revision"] < 1
        or not isinstance(value["nodes"], list)
    ):
        raise GoalReconciliationError("evidence identity is invalid")
    nodes = []
    seen: set[str] = set()
    for item in value["nodes"]:
        item = _object(
            "evidence node",
            item,
            {"node_id", "executable_contract_digest", "cbr_selection_funnel"},
        )
        node_id = _id("evidence node ID", item["node_id"])
        executable_contract_digest = _digest(
            "evidence executable contract digest", item["executable_contract_digest"]
        )
        if node_id in seen:
            raise GoalReconciliationError("evidence node IDs must be unique")
        seen.add(node_id)
        try:
            funnel = validate_selection_funnel(item["cbr_selection_funnel"])
        except SelectionFunnelError as exc:
            raise GoalReconciliationError("CBR funnel evidence is invalid") from exc
        nodes.append(
            {
                "node_id": node_id,
                "executable_contract_digest": executable_contract_digest,
                "cbr_selection_funnel": funnel,
            }
        )
    return {
        "schema_version": 1,
        "contract": EVIDENCE_CONTRACT,
        "goal_id": value["goal_id"],
        "revision": value["revision"],
        "manifest_digest": value["manifest_digest"],
        "nodes": nodes,
    }


def build_goal_reconciliation_report(
    manifest: dict[str, Any], evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    manifest = validate_goal_manifest(manifest)
    if evidence is not None:
        evidence = validate_goal_evidence(evidence)
        if (evidence["goal_id"], evidence["revision"], evidence["manifest_digest"]) != (
            manifest["goal_id"],
            manifest["revision"],
            manifest["manifest_digest"],
        ):
            raise GoalReconciliationError("evidence binding mismatch")
        evidence_rows = {row["node_id"]: row for row in evidence["nodes"]}
        if not set(evidence_rows) <= {node["node_id"] for node in manifest["nodes"]}:
            raise GoalReconciliationError("evidence contains an unknown node")
    else:
        evidence_rows = {}
    rows = []
    for node in manifest["nodes"]:
        axes = {axis: _axis("unknown", "no_trusted_cbr_evidence") for axis in AXES}
        axes["contract_binding"] = _axis("observed", "exact_manifest_binding")
        evidence_row = evidence_rows.get(node["node_id"])
        if evidence_row:
            if (
                evidence_row["executable_contract_digest"]
                != node["executable_contract_digest"]
                or evidence_row["cbr_selection_funnel"]["source_contract_digest"]
                != node["executable_contract_digest"]
            ):
                raise GoalReconciliationError("node evidence contract binding mismatch")
            stages = evidence_row["cbr_selection_funnel"]["surface_rows"]
            cbr = next((row for row in stages if row["surface"] == "cbr_batch"), None)
            if cbr:
                mapping = {
                    "selection": "selected",
                    "admission": "admitted",
                    "execution": "completed",
                    "review": "accepted",
                    "apply": "applied",
                    "attention_recorded": "parent_attention_recorded",
                }
                for axis, stage in mapping.items():
                    axes[axis] = _axis(cbr["stages"][stage]["status"], "cbr_" + stage)
        required = node["node_id"] in manifest["terminal_condition"]["required_nodes"]
        rows.append(
            {
                "node_id": node["node_id"],
                "required": required,
                "dependencies": node["dependencies"],
                "axes": axes,
                "recommendations": _recommend(axes, node["dependencies"]),
            }
        )
    body = {
        "schema_version": 1,
        "contract": REPORT_CONTRACT,
        "goal_id": manifest["goal_id"],
        "revision": manifest["revision"],
        "manifest_digest": manifest["manifest_digest"],
        "nodes": rows,
        "terminal_candidate": False,
        "authority_claims": {
            "dispatch": False,
            "review": False,
            "apply": False,
            "delivery": False,
            "acknowledgement": False,
            "parent_collection": False,
            "source_disposition": False,
            "root_completion": False,
        },
        "mutation": {"allowed": False, "applied": False},
    }
    body["report_digest"] = stable_digest(body)
    return validate_goal_reconciliation_report(body)


def validate_manifest_revision(
    previous: dict[str, Any],
    current: dict[str, Any],
    previous_report: dict[str, Any] | None = None,
) -> None:
    """Reject an admitted node's in-place contract rewrite across revisions.

    This is a validation-only helper for source adapters/operators; it writes no cursor.
    """
    previous, current = (
        validate_goal_manifest(previous),
        validate_goal_manifest(current),
    )
    if (
        previous["goal_id"] != current["goal_id"]
        or current["revision"] <= previous["revision"]
    ):
        raise GoalReconciliationError("manifest revision lineage is invalid")
    if current["supersedes"] != {
        "goal_id": previous["goal_id"],
        "revision": previous["revision"],
        "manifest_digest": previous["manifest_digest"],
    }:
        raise GoalReconciliationError("manifest supersession binding is invalid")
    admitted: set[str] = set()
    if previous_report is not None:
        report = validate_goal_reconciliation_report(previous_report)
        if (report["goal_id"], report["revision"], report["manifest_digest"]) != (
            previous["goal_id"],
            previous["revision"],
            previous["manifest_digest"],
        ):
            raise GoalReconciliationError("previous report binding mismatch")
        admitted = {
            row["node_id"]
            for row in report["nodes"]
            if row["axes"]["admission"]["status"] == "observed"
        }
    old = {node["node_id"]: node for node in previous["nodes"]}
    new = {node["node_id"]: node for node in current["nodes"]}
    if admitted - set(new):
        raise GoalReconciliationError("admitted node removal is forbidden")
    for node_id in admitted & set(old) & set(new):
        if old[node_id] != new[node_id]:
            raise GoalReconciliationError("admitted node in-place rewrite is forbidden")


def validate_goal_reconciliation_report(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GoalReconciliationError("report must be an object")
    expected = {
        "schema_version",
        "contract",
        "goal_id",
        "revision",
        "manifest_digest",
        "nodes",
        "terminal_candidate",
        "authority_claims",
        "mutation",
        "report_digest",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or value.get("contract") != REPORT_CONTRACT
    ):
        raise GoalReconciliationError("report fields are invalid")
    _id("report goal ID", value["goal_id"])
    _digest("report manifest digest", value["manifest_digest"])
    if (
        not isinstance(value["revision"], int)
        or value["revision"] < 1
        or value["terminal_candidate"] is not False
    ):
        raise GoalReconciliationError("report identity or terminal claim is invalid")
    claims = {
        key: False
        for key in (
            "dispatch",
            "review",
            "apply",
            "delivery",
            "acknowledgement",
            "parent_collection",
            "source_disposition",
            "root_completion",
        )
    }
    if value["authority_claims"] != claims or value["mutation"] != {
        "allowed": False,
        "applied": False,
    }:
        raise GoalReconciliationError("report authority boundary is invalid")
    if not isinstance(value["nodes"], list) or not value["nodes"]:
        raise GoalReconciliationError("report nodes are invalid")
    seen = set()
    rows = []
    for row in value["nodes"]:
        if not isinstance(row, dict) or set(row) != {
            "node_id",
            "required",
            "dependencies",
            "axes",
            "recommendations",
        }:
            raise GoalReconciliationError("report node fields are invalid")
        node_id = _id("report node ID", row["node_id"])
        if node_id in seen or not isinstance(row["required"], bool):
            raise GoalReconciliationError("report node identity is invalid")
        seen.add(node_id)
        deps = _id_list("report dependencies", row["dependencies"])
        if not isinstance(row["axes"], dict) or set(row["axes"]) != set(AXES):
            raise GoalReconciliationError("report axes are invalid")
        axes = {axis: _validated_axis(row["axes"][axis]) for axis in AXES}
        if not isinstance(row["recommendations"], list) or any(
            item
            not in {
                "attention_required",
                "source_disposition_required",
                "dependency_ready",
                "blocked_conflict",
            }
            for item in row["recommendations"]
        ):
            raise GoalReconciliationError("report recommendations are invalid")
        rows.append(
            {
                "node_id": node_id,
                "required": row["required"],
                "dependencies": deps,
                "axes": axes,
                "recommendations": list(row["recommendations"]),
            }
        )
    canonical = {
        "schema_version": 1,
        "contract": REPORT_CONTRACT,
        "goal_id": value["goal_id"],
        "revision": value["revision"],
        "manifest_digest": value["manifest_digest"],
        "nodes": rows,
        "terminal_candidate": False,
        "authority_claims": claims,
        "mutation": {"allowed": False, "applied": False},
    }
    if value["report_digest"] != stable_digest(canonical):
        raise GoalReconciliationError("report digest is invalid")
    canonical["report_digest"] = value["report_digest"]
    _reject_private(canonical)
    return canonical


def _node(value: object) -> dict[str, Any]:
    value = _object(
        "node",
        value,
        {
            "node_id",
            "executable_contract_digest",
            "authority",
            "dependencies",
            "dependency_mode",
            "required_outcome",
            "verification_references",
            "terminal_contribution",
        },
    )
    _digest("node executable contract digest", value["executable_contract_digest"])
    if value["dependency_mode"] not in {
        "wait-for-completion",
        "wait-for-acceptance",
    } or value["terminal_contribution"] not in {"required", "advisory"}:
        raise GoalReconciliationError("node mode is invalid")
    return {
        "node_id": _id("node ID", value["node_id"]),
        "executable_contract_digest": value["executable_contract_digest"],
        "authority": _authority("node authority", value["authority"]),
        "dependencies": _id_list("node dependencies", value["dependencies"]),
        "dependency_mode": value["dependency_mode"],
        "required_outcome": _text("required outcome", value["required_outcome"]),
        "verification_references": _id_list(
            "verification references", value["verification_references"]
        ),
        "terminal_contribution": value["terminal_contribution"],
    }


def _recommend(axes: dict[str, dict[str, str]], dependencies: list[str]) -> list[str]:
    result = []
    if (
        axes["attention_recorded"]["status"] == "observed"
        and axes["parent_collection"]["status"] != "observed"
    ):
        result.append("attention_required")
    if (
        axes["apply"]["status"] == "observed"
        and axes["source_disposition"]["status"] != "observed"
    ):
        result.append("source_disposition_required")
    if not dependencies and axes["source_disposition"]["status"] == "observed":
        result.append("dependency_ready")
    if any(axis["status"] == "unknown" for axis in axes.values()):
        result.append("blocked_conflict")
    return result


def _axis(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason_code": reason}


def _validated_axis(value: object) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"status", "reason_code"}
        or value["status"] not in STATUSES
    ):
        raise GoalReconciliationError("axis is invalid")
    return _axis(value["status"], _id("axis reason", value["reason_code"]))


def _object(name: str, value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise GoalReconciliationError(f"{name} fields are invalid")
    return value


def _id(name: str, value: object) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise GoalReconciliationError(f"{name} is invalid")
    return value


def _digest(name: str, value: object) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise GoalReconciliationError(f"{name} is invalid")
    return value


def _id_list(name: str, value: object) -> list[str]:
    if not isinstance(value, list):
        raise GoalReconciliationError(f"{name} is invalid")
    result = [_id(name, item) for item in value]
    if len(result) != len(set(result)):
        raise GoalReconciliationError(f"{name} contains duplicates")
    return result


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 240 or "\n" in value:
        raise GoalReconciliationError(f"{name} is invalid")
    return value


def _authority(name: str, value: object) -> str:
    if value not in {"source_owner", "codex_coordinator", "external_authority"}:
        raise GoalReconciliationError(f"{name} is invalid")
    return str(value)


def _no_cycles(nodes: list[dict[str, Any]]) -> None:
    graph = {node["node_id"]: node["dependencies"] for node in nodes}
    active, done = set(), set()

    def visit(node: str) -> None:
        if node in active:
            raise GoalReconciliationError("node dependency cycle")
        if node not in done:
            active.add(node)
            for dep in graph[node]:
                visit(dep)
            active.remove(node)
            done.add(node)

    for node in graph:
        visit(node)


def _reject_private(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in FORBIDDEN:
                raise GoalReconciliationError("privacy-sensitive field is forbidden")
            _reject_private(item)
    elif isinstance(value, list):
        for item in value:
            _reject_private(item)
    elif isinstance(value, str) and (
        ABSOLUTE_PATH.search(value)
        or SENSITIVE_ASSIGNMENT.search(value)
        or "~" in value
        or "@" in value
        and "." in value
    ):
        raise GoalReconciliationError("privacy-sensitive value is forbidden")

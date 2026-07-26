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
EXPLAIN_VIEW_CONTRACT = "goal-explain-view-v1"
EXPLAIN_ERROR_CONTRACT = "goal-explain-error-v1"
NEXT_DECISIONS = frozenset(
    {
        "resolve_input_conflict",
        "collect_parent_attention",
        "record_source_disposition",
        "consider_dependency_advance",
        "review_terminal_candidate",
        "await_trusted_evidence",
        "none",
    }
)
EXPLICIT_CONFLICT_REASONS = frozenset(
    {
        "admitted_drift",
        "binding_conflict",
        "chronology_conflict",
        "contract_conflict",
        "identity_conflict",
    }
)
REPORT_RECOMMENDATIONS = frozenset(
    {
        "attention_required",
        "source_disposition_required",
        "dependency_ready",
        "blocked_conflict",
    }
)
EXPLAIN_AUTHORITY_CLAIMS = {
    "dispatch": False,
    "routing": False,
    "selection": False,
    "review": False,
    "apply": False,
    "delivery": False,
    "acknowledgement": False,
    "parent_collection": False,
    "source_disposition": False,
    "root_completion": False,
    "goal_completion": False,
}
EXPLAIN_ERROR_REASON_CODES = frozenset(
    {
        "goal_explain_invalid",
        "goal_explain_internal_error",
    }
)
EXPLAIN_ERROR_VALIDATION_CODES = frozenset(
    {
        "failed_closed",
        "unexpected_failure",
    }
)
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


class GoalExplainError(GoalReconciliationError):
    pass


def load_goal_manifest(path: str | Path) -> dict[str, Any]:
    return validate_goal_manifest(_load(path, "manifest"))


def load_goal_evidence(path: str | Path) -> dict[str, Any]:
    return validate_goal_evidence(_load(path, "evidence"))


def load_goal_reconciliation_report(path: str | Path) -> dict[str, Any]:
    return validate_goal_reconciliation_report(_load(path, "report"))


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


def build_goal_explain_view(
    manifest: dict[str, Any], report: dict[str, Any]
) -> dict[str, Any]:
    manifest = validate_goal_manifest(manifest)
    report = validate_goal_reconciliation_report(report)

    if (
        report["goal_id"] != manifest["goal_id"]
        or report["revision"] != manifest["revision"]
        or report["manifest_digest"] != manifest["manifest_digest"]
    ):
        raise GoalExplainError("manifest and report binding mismatch")

    manifest_node_ids = [node["node_id"] for node in manifest["nodes"]]
    report_node_ids = [node["node_id"] for node in report["nodes"]]
    if manifest_node_ids != report_node_ids:
        raise GoalExplainError("manifest and report node mismatch")

    nodes = []
    for manifest_node, report_node in zip(
        manifest["nodes"], report["nodes"], strict=True
    ):
        if (
            report_node["required"]
            != (
                manifest_node["node_id"]
                in manifest["terminal_condition"]["required_nodes"]
            )
            or report_node["dependencies"] != manifest_node["dependencies"]
        ):
            raise GoalExplainError("manifest and report node semantics mismatch")
        if report_node["recommendations"] != _recommend(
            report_node["axes"], report_node["dependencies"]
        ):
            raise GoalExplainError("report recommendation semantics mismatch")
        issues = []
        for axis in AXES:
            axis_value = report_node["axes"][axis]
            if axis_value["reason_code"] in EXPLICIT_CONFLICT_REASONS:
                issues.append(
                    {
                        "kind": "conflict",
                        "axis": axis,
                        "reason_code": axis_value["reason_code"],
                    }
                )
            elif axis_value["status"] == "unknown":
                issues.append(
                    {
                        "kind": "unknown",
                        "axis": axis,
                        "reason_code": axis_value["reason_code"],
                    }
                )
        nodes.append(
            {
                "node_id": manifest_node["node_id"],
                "required": report_node["required"],
                "authority": manifest_node["authority"],
                "dependencies": manifest_node["dependencies"],
                "dependency_mode": manifest_node["dependency_mode"],
                "required_outcome": manifest_node["required_outcome"],
                "axes": report_node["axes"],
                "issues": issues,
                "recommendations": report_node["recommendations"],
            }
        )

    body = {
        "schema_version": 1,
        "contract": EXPLAIN_VIEW_CONTRACT,
        "goal": {
            "goal_id": manifest["goal_id"],
            "root_outcome": manifest["root_outcome"],
            "owner_kind": manifest["source"]["owner_kind"],
            "source_ref": manifest["source"]["source_ref"],
            "source_adapter_revision": manifest["source"]["adapter_revision"],
            "goal_decision_authority": manifest["decision_authority"]["goal"],
        },
        "binding": {
            "status": "observed",
            "desired": {
                "revision": manifest["revision"],
                "manifest_digest": manifest["manifest_digest"],
            },
            "current": {
                "revision": report["revision"],
                "manifest_digest": report["manifest_digest"],
                "report_digest": report["report_digest"],
            },
            "reason_code": "exact_goal_revision_binding",
        },
        "attention": _attention_summary(nodes),
        "nodes": nodes,
        "next_decision": _determine_next_decision(nodes, report),
        "terminal": {
            "report_candidate": report["terminal_candidate"],
            "operator_status": (
                "candidate_not_completion"
                if report["terminal_candidate"]
                else "not_candidate"
            ),
            "source_completion_required": True,
            "required_node_ids": manifest["terminal_condition"]["required_nodes"],
            "root_acceptance_references": manifest["terminal_condition"][
                "root_acceptance_references"
            ],
            "reason_codes": [
                (
                    "source_completion_required"
                    if report["terminal_candidate"]
                    else "reconciliation_report_not_terminal_candidate"
                )
            ],
        },
        "authority_claims": dict(EXPLAIN_AUTHORITY_CLAIMS),
        "mutation": {"allowed": False, "applied": False},
        "input_digests": {
            "manifest_digest": manifest["manifest_digest"],
            "report_digest": report["report_digest"],
        },
    }
    body["view_digest"] = stable_digest(body)
    return validate_goal_explain_view(body)


def _attention_summary(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    relevant = [node for node in nodes if node["required"]]
    pending = [
        node
        for node in relevant
        if node["axes"]["attention_recorded"]["status"] == "observed"
        and node["axes"]["parent_collection"]["status"] != "observed"
    ]
    if pending:
        status, selected = "observed", pending
    else:
        unknown = [
            node
            for node in relevant
            if "unknown"
            in {
                node["axes"]["attention_recorded"]["status"],
                node["axes"]["parent_collection"]["status"],
            }
        ]
        if unknown:
            status, selected = "unknown", unknown
        elif relevant and all(
            node["axes"]["attention_recorded"]["status"] == "not_applicable"
            and node["axes"]["parent_collection"]["status"] == "not_applicable"
            for node in relevant
        ):
            status, selected = "not_applicable", relevant
        else:
            status, selected = "not_observed", relevant
    reasons = []
    for node in selected:
        for axis in ("attention_recorded", "parent_collection"):
            reason = node["axes"][axis]["reason_code"]
            if reason not in reasons:
                reasons.append(reason)
    return {
        "status": status,
        "reason_codes": reasons,
        "node_ids": [node["node_id"] for node in selected],
    }


def _determine_next_decision(
    nodes: list[dict[str, Any]], report: dict[str, Any]
) -> dict[str, Any]:
    conflicts = [
        node
        for node in nodes
        if any(issue["kind"] == "conflict" for issue in node["issues"])
    ]
    if conflicts:
        return _decision(
            "resolve_input_conflict",
            _common_owner(conflicts),
            conflicts,
            [
                issue["reason_code"]
                for node in conflicts
                for issue in node["issues"]
                if issue["kind"] == "conflict"
            ],
        )

    attention = [
        node
        for node in nodes
        if node["axes"]["attention_recorded"]["status"] == "observed"
        and node["axes"]["parent_collection"]["status"] != "observed"
    ]
    if attention:
        return _decision(
            "collect_parent_attention",
            "codex_coordinator",
            attention,
            [node["axes"]["parent_collection"]["reason_code"] for node in attention],
        )

    source_disposition = [
        node
        for node in nodes
        if "source_disposition_required" in node["recommendations"]
        or (
            node["axes"]["apply"]["status"] == "observed"
            and node["axes"]["source_disposition"]["status"] != "observed"
        )
    ]
    if source_disposition:
        return _decision(
            "record_source_disposition",
            "source_owner",
            source_disposition,
            [
                node["axes"]["source_disposition"]["reason_code"]
                for node in source_disposition
            ],
        )

    dependency_ready = [
        node for node in nodes if "dependency_ready" in node["recommendations"]
    ]
    if dependency_ready:
        return _decision(
            "consider_dependency_advance",
            "codex_coordinator",
            dependency_ready,
            ["dependency_ready"],
        )

    if report["terminal_candidate"]:
        return _decision(
            "review_terminal_candidate",
            "source_owner",
            [node for node in nodes if node["required"]],
            ["source_completion_required"],
        )

    unknown = [
        node
        for node in nodes
        if any(issue["kind"] == "unknown" for issue in node["issues"])
    ]
    if unknown:
        return _decision(
            "await_trusted_evidence",
            _common_owner(unknown),
            unknown,
            [
                issue["reason_code"]
                for node in unknown
                for issue in node["issues"]
                if issue["kind"] == "unknown"
            ],
        )

    return _decision("none", "unknown", [], [])


def _decision(
    kind: str,
    owner: str,
    nodes: list[dict[str, Any]],
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "owner": owner,
        "node_ids": [node["node_id"] for node in nodes],
        "reason_codes": list(dict.fromkeys(reasons)),
        "claim": "advisory",
    }


def _common_owner(nodes: list[dict[str, Any]]) -> str:
    owners = {node["authority"] for node in nodes}
    return next(iter(owners)) if len(owners) == 1 else "unknown"


def validate_goal_explain_view(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GoalExplainError("view must be an object")
    _reject_private(value)
    expected = {
        "schema_version",
        "contract",
        "goal",
        "binding",
        "attention",
        "nodes",
        "next_decision",
        "terminal",
        "authority_claims",
        "mutation",
        "input_digests",
        "view_digest",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or value.get("contract") != EXPLAIN_VIEW_CONTRACT
    ):
        raise GoalExplainError("view fields are invalid")
    goal = _validate_explain_goal(value["goal"])
    binding = _validate_explain_binding(value["binding"])
    attention = _validate_attention(value["attention"])
    if value["authority_claims"] != EXPLAIN_AUTHORITY_CLAIMS or value["mutation"] != {
        "allowed": False,
        "applied": False,
    }:
        raise GoalExplainError("view authority boundary is invalid")
    if not isinstance(value["nodes"], list) or not value["nodes"]:
        raise GoalExplainError("view nodes are invalid")
    seen = set()
    rows = []
    for row in value["nodes"]:
        if not isinstance(row, dict) or set(row) != {
            "node_id",
            "required",
            "authority",
            "dependencies",
            "dependency_mode",
            "required_outcome",
            "axes",
            "issues",
            "recommendations",
        }:
            raise GoalExplainError("view node fields are invalid")
        node_id = _id("view node ID", row["node_id"])
        if node_id in seen or not isinstance(row["required"], bool):
            raise GoalExplainError("view node identity is invalid")
        authority = _authority("view node authority", row["authority"])
        if row["dependency_mode"] not in {
            "wait-for-completion",
            "wait-for-acceptance",
        }:
            raise GoalExplainError("view node dependency mode is invalid")
        seen.add(node_id)
        deps = _id_list("view dependencies", row["dependencies"])
        if not isinstance(row["axes"], dict) or set(row["axes"]) != set(AXES):
            raise GoalExplainError("view axes are invalid")
        axes = {axis: _validated_axis(row["axes"][axis]) for axis in AXES}
        issues = _validate_issues(row["issues"])
        expected_issues = []
        for axis in AXES:
            axis_value = axes[axis]
            if axis_value["reason_code"] in EXPLICIT_CONFLICT_REASONS:
                expected_issues.append(
                    {
                        "kind": "conflict",
                        "axis": axis,
                        "reason_code": axis_value["reason_code"],
                    }
                )
            elif axis_value["status"] == "unknown":
                expected_issues.append(
                    {
                        "kind": "unknown",
                        "axis": axis,
                        "reason_code": axis_value["reason_code"],
                    }
                )
        if issues != expected_issues:
            raise GoalExplainError("view issue classification is invalid")
        if (
            not isinstance(row["recommendations"], list)
            or len(row["recommendations"]) != len(set(row["recommendations"]))
            or any(
                item not in REPORT_RECOMMENDATIONS for item in row["recommendations"]
            )
        ):
            raise GoalExplainError("view recommendations are invalid")
        if row["recommendations"] != _recommend(axes, deps):
            raise GoalExplainError("view recommendation semantics are invalid")
        rows.append(
            {
                "node_id": node_id,
                "required": row["required"],
                "authority": authority,
                "dependencies": deps,
                "dependency_mode": row["dependency_mode"],
                "required_outcome": _text(
                    "view required outcome", row["required_outcome"]
                ),
                "axes": axes,
                "issues": issues,
                "recommendations": list(row["recommendations"]),
            }
        )
    next_decision = _validate_next_decision(value["next_decision"])
    terminal = _validate_terminal(value["terminal"])
    known_nodes = {row["node_id"] for row in rows}
    if (
        not set(attention["node_ids"]) <= known_nodes
        or not set(next_decision["node_ids"]) <= known_nodes
        or not set(terminal["required_node_ids"]) <= known_nodes
        or terminal["required_node_ids"]
        != [row["node_id"] for row in rows if row["required"]]
        or any(
            not set(row["dependencies"]) <= known_nodes
            or row["node_id"] in row["dependencies"]
            for row in rows
        )
    ):
        raise GoalExplainError("view node binding is invalid")
    _no_cycles(rows)
    if attention != _attention_summary(rows):
        raise GoalExplainError("view attention summary is invalid")
    if next_decision != _determine_next_decision(
        rows, {"terminal_candidate": terminal["report_candidate"]}
    ):
        raise GoalExplainError("view next decision priority is invalid")
    input_digests = _object(
        "view input digests",
        value["input_digests"],
        {"manifest_digest", "report_digest"},
    )
    input_digests = {
        "manifest_digest": _digest(
            "view input manifest digest", input_digests["manifest_digest"]
        ),
        "report_digest": _digest(
            "view input report digest", input_digests["report_digest"]
        ),
    }
    if (
        binding["desired"]["manifest_digest"] != input_digests["manifest_digest"]
        or binding["current"]["manifest_digest"] != input_digests["manifest_digest"]
        or binding["current"]["report_digest"] != input_digests["report_digest"]
    ):
        raise GoalExplainError("view digest binding is invalid")
    canonical = {
        "schema_version": 1,
        "contract": EXPLAIN_VIEW_CONTRACT,
        "goal": goal,
        "binding": binding,
        "attention": attention,
        "nodes": rows,
        "next_decision": next_decision,
        "terminal": terminal,
        "authority_claims": dict(EXPLAIN_AUTHORITY_CLAIMS),
        "mutation": {"allowed": False, "applied": False},
        "input_digests": input_digests,
    }
    _digest("view digest", value["view_digest"])
    if value["view_digest"] != stable_digest(canonical):
        raise GoalExplainError("view digest is invalid")
    canonical["view_digest"] = value["view_digest"]
    _reject_private(canonical)
    return canonical


def _validate_explain_goal(value: object) -> dict[str, Any]:
    goal = _object(
        "view goal",
        value,
        {
            "goal_id",
            "root_outcome",
            "owner_kind",
            "source_ref",
            "source_adapter_revision",
            "goal_decision_authority",
        },
    )
    root = _object(
        "view root outcome",
        goal["root_outcome"],
        {"summary", "acceptance_references"},
    )
    if goal["owner_kind"] not in {"source_project", "external_adapter"}:
        raise GoalExplainError("view goal owner kind is invalid")
    return {
        "goal_id": _id("view goal ID", goal["goal_id"]),
        "root_outcome": {
            "summary": _text("view root outcome", root["summary"]),
            "acceptance_references": _id_list(
                "view root acceptance references",
                root["acceptance_references"],
            ),
        },
        "owner_kind": goal["owner_kind"],
        "source_ref": _id("view source reference", goal["source_ref"]),
        "source_adapter_revision": _id(
            "view source adapter revision", goal["source_adapter_revision"]
        ),
        "goal_decision_authority": _authority(
            "view goal decision authority", goal["goal_decision_authority"]
        ),
    }


def _validate_explain_binding(value: object) -> dict[str, Any]:
    binding = _object(
        "view binding",
        value,
        {"status", "desired", "current", "reason_code"},
    )
    desired = _object(
        "view desired binding",
        binding["desired"],
        {"revision", "manifest_digest"},
    )
    current = _object(
        "view current binding",
        binding["current"],
        {"revision", "manifest_digest", "report_digest"},
    )
    if (
        binding["status"] != "observed"
        or binding["reason_code"] != "exact_goal_revision_binding"
        or not isinstance(desired["revision"], int)
        or isinstance(desired["revision"], bool)
        or desired["revision"] < 1
        or current["revision"] != desired["revision"]
    ):
        raise GoalExplainError("view binding is invalid")
    desired_digest = _digest("view desired manifest digest", desired["manifest_digest"])
    current_digest = _digest("view current manifest digest", current["manifest_digest"])
    if desired_digest != current_digest:
        raise GoalExplainError("view binding manifest digest mismatch")
    return {
        "status": "observed",
        "desired": {
            "revision": desired["revision"],
            "manifest_digest": desired_digest,
        },
        "current": {
            "revision": current["revision"],
            "manifest_digest": current_digest,
            "report_digest": _digest(
                "view current report digest", current["report_digest"]
            ),
        },
        "reason_code": "exact_goal_revision_binding",
    }


def _validate_attention(value: object) -> dict[str, Any]:
    attention = _object(
        "view attention",
        value,
        {"status", "reason_codes", "node_ids"},
    )
    if attention["status"] not in STATUSES:
        raise GoalExplainError("view attention status is invalid")
    return {
        "status": attention["status"],
        "reason_codes": _id_list("view attention reasons", attention["reason_codes"]),
        "node_ids": _id_list("view attention nodes", attention["node_ids"]),
    }


def _validate_issues(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise GoalExplainError("view issues are invalid")
    result = []
    seen = set()
    for issue in value:
        issue = _object("view issue", issue, {"kind", "axis", "reason_code"})
        if issue["kind"] not in {"unknown", "conflict"} or issue["axis"] not in AXES:
            raise GoalExplainError("view issue is invalid")
        reason = _id("view issue reason", issue["reason_code"])
        identity = (issue["kind"], issue["axis"], reason)
        if identity in seen:
            raise GoalExplainError("view issues contain duplicates")
        seen.add(identity)
        result.append(
            {"kind": issue["kind"], "axis": issue["axis"], "reason_code": reason}
        )
    return result


def _validate_next_decision(value: object) -> dict[str, Any]:
    decision = _object(
        "view next decision",
        value,
        {"kind", "owner", "node_ids", "reason_codes", "claim"},
    )
    if (
        decision["kind"] not in NEXT_DECISIONS
        or decision["owner"]
        not in {
            "source_owner",
            "codex_coordinator",
            "external_authority",
            "unknown",
        }
        or decision["claim"] != "advisory"
    ):
        raise GoalExplainError("view next decision is invalid")
    return {
        "kind": decision["kind"],
        "owner": decision["owner"],
        "node_ids": _id_list("view next decision nodes", decision["node_ids"]),
        "reason_codes": _id_list(
            "view next decision reasons", decision["reason_codes"]
        ),
        "claim": "advisory",
    }


def _validate_terminal(value: object) -> dict[str, Any]:
    terminal = _object(
        "view terminal",
        value,
        {
            "report_candidate",
            "operator_status",
            "source_completion_required",
            "required_node_ids",
            "root_acceptance_references",
            "reason_codes",
        },
    )
    if (
        terminal["report_candidate"] is not False
        or terminal["source_completion_required"] is not True
        or terminal["operator_status"] != "not_candidate"
    ):
        raise GoalExplainError("view terminal boundary is invalid")
    if terminal["reason_codes"] != ["reconciliation_report_not_terminal_candidate"]:
        raise GoalExplainError("view terminal reason is invalid")
    return {
        "report_candidate": terminal["report_candidate"],
        "operator_status": terminal["operator_status"],
        "source_completion_required": True,
        "required_node_ids": _id_list(
            "view terminal required nodes", terminal["required_node_ids"]
        ),
        "root_acceptance_references": _id_list(
            "view terminal acceptance references",
            terminal["root_acceptance_references"],
        ),
        "reason_codes": _id_list("view terminal reasons", terminal["reason_codes"]),
    }


def error_goal_explain_view(
    reason_codes: list[str] | tuple[str, ...] | str,
    validation_errors: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if isinstance(reason_codes, str):
        codes = [reason_codes]
    else:
        codes = list(reason_codes)
    return validate_goal_explain_error(
        {
            "schema_version": 1,
            "contract": EXPLAIN_ERROR_CONTRACT,
            "decision_status": "invalid",
            "reason_codes": codes,
            "validation_errors": list(validation_errors or []),
            "mutation": {"allowed": False, "applied": False},
        }
    )


def validate_goal_explain_error(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GoalExplainError("error view must be an object")
    _reject_private(value)
    expected = {
        "schema_version",
        "contract",
        "decision_status",
        "reason_codes",
        "validation_errors",
        "mutation",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or value.get("contract") != EXPLAIN_ERROR_CONTRACT
        or value.get("decision_status") != "invalid"
    ):
        raise GoalExplainError("error view fields are invalid")
    if not isinstance(value["reason_codes"], list) or not isinstance(
        value["validation_errors"], list
    ):
        raise GoalExplainError("error view lists are invalid")
    if (
        not value["reason_codes"]
        or len(value["reason_codes"]) != len(set(value["reason_codes"]))
        or any(item not in EXPLAIN_ERROR_REASON_CODES for item in value["reason_codes"])
        or len(value["validation_errors"]) != len(set(value["validation_errors"]))
        or any(
            item not in EXPLAIN_ERROR_VALIDATION_CODES
            for item in value["validation_errors"]
        )
    ):
        raise GoalExplainError("error view code is invalid")
    if value["mutation"] != {"allowed": False, "applied": False}:
        raise GoalExplainError("error view mutation boundary is invalid")
    return {
        "schema_version": 1,
        "contract": EXPLAIN_ERROR_CONTRACT,
        "decision_status": "invalid",
        "reason_codes": list(value["reason_codes"]),
        "validation_errors": list(value["validation_errors"]),
        "mutation": {"allowed": False, "applied": False},
    }


def render_goal_explain_view(view: dict[str, Any]) -> str:
    view = validate_goal_explain_view(view)
    goal = view["goal"]
    binding = view["binding"]
    attention = view["attention"]
    decision = view["next_decision"]
    terminal = view["terminal"]
    lines = [
        "Goal",
        (
            f"  {goal['goal_id']}: {goal['root_outcome']['summary']} "
            f"(owner={goal['owner_kind']}, authority={goal['goal_decision_authority']})"
        ),
        "Binding",
        (
            f"  {binding['status']}: revision={binding['desired']['revision']} "
            f"manifest={binding['desired']['manifest_digest']} "
            f"report={binding['current']['report_digest']}"
        ),
        "Attention",
        (
            f"  {attention['status']}: nodes={','.join(attention['node_ids']) or 'none'} "
            f"reasons={','.join(attention['reason_codes']) or 'none'}"
        ),
        "Nodes",
    ]
    for node in view["nodes"]:
        axes = " ".join(
            f"{axis}={node['axes'][axis]['status']}:{node['axes'][axis]['reason_code']}"
            for axis in AXES
        )
        issues = (
            ",".join(
                f"{issue['kind']}:{issue['axis']}:{issue['reason_code']}"
                for issue in node["issues"]
            )
            or "none"
        )
        lines.append(
            f"  {node['node_id']} (authority={node['authority']}, "
            f"required={str(node['required']).lower()}): {axes} issues={issues}"
        )
    lines.extend(
        [
            "Next decision",
            (
                f"  {decision['kind']} (owner={decision['owner']}, "
                f"claim={decision['claim']}): "
                f"nodes={','.join(decision['node_ids']) or 'none'} "
                f"reasons={','.join(decision['reason_codes']) or 'none'}"
            ),
            "Terminal / non-claims",
            (
                f"  report_candidate={str(terminal['report_candidate']).lower()} "
                f"operator_status={terminal['operator_status']} "
                "source_completion_required=true"
            ),
            "  mutation=false completion=false routing=false selection=false",
            f"  view_digest={view['view_digest']}",
        ]
    )
    return "\n".join(lines)

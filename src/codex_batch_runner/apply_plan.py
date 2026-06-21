from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config
from .fs import read_json
from .queue import list_tasks


SUPPORTED_OPERATIONS = {
    "pause",
    "unpause",
    "replan",
    "supersede",
    "split",
    "merge",
    "retarget_metadata",
    "dependency_changes",
    "append_note",
    "create_followup",
}

SENSITIVE_KEYS = {
    "prompt",
    "next_prompt",
    "log",
    "logs",
    "log_path",
    "log_paths",
    "session_id",
    "thread_id",
    "token",
    "credential",
    "credentials",
    "secret",
    "cwd",
    "path",
    "project_root",
}

REFERENCE_ID_FIELDS = {
    "by",
    "replacement_task_id",
    "superseded_by",
    "parent_task_id",
}

REFERENCE_IDS_FIELDS = {
    "source_task_ids",
    "child_task_ids",
    "merged_task_ids",
}


def build_apply_plan_report(config: Config, plan_path: str | Path) -> dict:
    plan = read_json(Path(plan_path).expanduser().resolve())
    tasks = list_tasks(config)
    by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
    report = {
        "mode": "dry-run",
        "ok": True,
        "plan": {},
        "operation_count": 0,
        "operations": [],
        "warnings": [],
        "errors": [],
    }

    if not isinstance(plan, dict):
        add_error(report, "plan must be a JSON object")
        return finalize(report)

    report["plan"] = {
        "schema_version": sanitize(plan.get("schema_version")),
        "plan_id": sanitize(plan.get("plan_id")),
        "actor": sanitize(plan.get("actor")),
        "reason_present": has_text(plan.get("reason")),
    }
    operations = plan.get("operations")
    if not isinstance(operations, list):
        add_error(report, "operations must be a list")
        return finalize(report)

    if "schema_version" not in plan:
        add_error(report, "schema_version is required")
    if not plan.get("actor"):
        add_error(report, "actor is required")
    if not has_text(plan.get("reason")):
        add_warning(report, "plan reason is empty; each operation must provide reason")

    report["operation_count"] = len(operations)
    graph = current_dependency_graph(by_id)
    created_ids: set[str] = set()

    for index, raw_operation in enumerate(operations):
        op_report = validate_operation(index, raw_operation, plan, by_id, graph, created_ids, report)
        report["operations"].append(op_report)

    cycle = find_cycle(graph)
    if cycle:
        add_error(report, "dependency graph would contain a cycle: " + " -> ".join(cycle))

    return finalize(report)


def validate_operation(
    index: int,
    raw_operation: object,
    plan: dict,
    by_id: dict[str, dict],
    graph: dict[str, set[str]],
    created_ids: set[str],
    report: dict,
) -> dict:
    op_report = {
        "index": index,
        "op": None,
        "task_ids": [],
        "reason_present": False,
        "would_change": False,
        "sanitized": {},
        "errors": [],
        "warnings": [],
    }
    if not isinstance(raw_operation, dict):
        add_op_error(report, op_report, "operation must be an object")
        return op_report

    op = raw_operation.get("op")
    op_report["op"] = sanitize(op)
    op_report["reason_present"] = has_text(raw_operation.get("reason")) or has_text(plan.get("reason"))
    op_report["sanitized"] = sanitize(raw_operation)

    if op not in SUPPORTED_OPERATIONS:
        add_op_error(report, op_report, f"unsupported operation: {op}")
    if not op_report["reason_present"]:
        add_op_error(report, op_report, "reason is required at plan or operation level")

    target_ids = operation_target_ids(raw_operation)
    op_report["task_ids"] = sorted(target_ids)
    requires_target = op != "create_followup"
    if requires_target and not target_ids:
        add_op_error(report, op_report, "task_id or task_ids is required")

    for task_id in sorted(target_ids):
        task = by_id.get(task_id)
        if not task:
            add_op_error(report, op_report, f"task not found: {task_id}")
            continue
        if task.get("status") == "running":
            add_op_error(report, op_report, f"operation targets running task: {task_id}")

    validate_dependency_references(raw_operation, by_id, created_ids, op_report, report)
    apply_dependency_simulation(raw_operation, by_id, graph, created_ids, op_report, report)
    op_report["would_change"] = not op_report["errors"] and op in SUPPORTED_OPERATIONS
    return op_report


def operation_target_ids(operation: dict) -> set[str]:
    ids: set[str] = set()
    task_id = operation.get("task_id")
    if isinstance(task_id, str) and task_id:
        ids.add(task_id)
    task_ids = operation.get("task_ids")
    if isinstance(task_ids, list):
        ids.update(item for item in task_ids if isinstance(item, str) and item)
    return ids


def validate_dependency_references(
    operation: dict,
    by_id: dict[str, dict],
    created_ids: set[str],
    op_report: dict,
    report: dict,
) -> None:
    existing_or_created = set(by_id) | created_ids
    for dep_id in sorted(dependency_ids_from_operation(operation)):
        if dep_id not in existing_or_created:
            add_op_error(report, op_report, f"dependency task not found: {dep_id}")


def apply_dependency_simulation(
    operation: dict,
    by_id: dict[str, dict],
    graph: dict[str, set[str]],
    created_ids: set[str],
    op_report: dict,
    report: dict,
) -> None:
    op = operation.get("op")
    if op == "dependency_changes":
        for task_id in operation_target_ids(operation):
            if task_id not in by_id:
                continue
            replacement = replacement_dependencies(operation)
            if replacement is not None:
                graph[task_id] = set(replacement)
            graph[task_id].update(add_dependencies(operation))
            graph[task_id].difference_update(remove_dependencies(operation))
            if task_id in graph[task_id]:
                add_op_error(report, op_report, f"task cannot depend on itself: {task_id}")
    for draft in create_drafts(operation):
        draft_id = draft.get("id") if isinstance(draft, dict) else None
        if not isinstance(draft_id, str) or not draft_id:
            add_op_error(report, op_report, "created task draft requires id")
            continue
        if draft_id in by_id or draft_id in created_ids:
            add_op_error(report, op_report, f"created task id already exists: {draft_id}")
            continue
        created_ids.add(draft_id)
        deps = draft.get("depends_on", [])
        graph[draft_id] = {item for item in deps if isinstance(item, str) and item}
        if draft_id in graph[draft_id]:
            add_op_error(report, op_report, f"task cannot depend on itself: {draft_id}")


def current_dependency_graph(by_id: dict[str, dict]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for task_id, task in by_id.items():
        depends_on = task.get("depends_on", [])
        graph[task_id] = {item for item in depends_on if isinstance(item, str) and item}
    return graph


def dependency_ids_from_operation(operation: dict) -> set[str]:
    ids = set()
    for value in (replacement_dependencies(operation), add_dependencies(operation), remove_dependencies(operation)):
        if value:
            ids.update(value)
    ids.update(reference_ids_from_operation(operation))
    for draft in create_drafts(operation):
        if isinstance(draft, dict):
            depends_on = draft.get("depends_on", [])
            if isinstance(depends_on, list):
                ids.update(item for item in depends_on if isinstance(item, str) and item)
    return ids


def replacement_dependencies(operation: dict) -> set[str] | None:
    fields = operation.get("fields")
    for source in (fields, operation):
        if isinstance(source, dict) and isinstance(source.get("depends_on"), list):
            return {item for item in source["depends_on"] if isinstance(item, str) and item}
    return None


def add_dependencies(operation: dict) -> set[str]:
    return dependency_field(operation, ("add", "add_depends_on", "add_dependencies"))


def remove_dependencies(operation: dict) -> set[str]:
    return dependency_field(operation, ("remove", "remove_depends_on", "remove_dependencies"))


def dependency_field(operation: dict, names: tuple[str, ...]) -> set[str]:
    values: set[str] = set()
    fields = operation.get("fields")
    for source in (fields, operation):
        if not isinstance(source, dict):
            continue
        for name in names:
            raw = source.get(name)
            if isinstance(raw, str) and raw:
                values.add(raw)
            elif isinstance(raw, list):
                values.update(item for item in raw if isinstance(item, str) and item)
    return values


def create_drafts(operation: dict) -> list[dict]:
    creates = operation.get("creates", [])
    if isinstance(creates, dict):
        return [creates]
    if isinstance(creates, list):
        return [item for item in creates if isinstance(item, dict)]
    return []


def reference_ids_from_operation(operation: dict) -> set[str]:
    ids = set()
    for name in REFERENCE_ID_FIELDS:
        value = operation.get(name)
        if isinstance(value, str) and value:
            ids.add(value)
    for name in REFERENCE_IDS_FIELDS:
        value = operation.get(name)
        if isinstance(value, list):
            ids.update(item for item in value if isinstance(item, str) and item)
    return ids


def find_cycle(graph: dict[str, set[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        if node in visiting:
            start = stack.index(node)
            return stack[start:] + [node]
        if node in visited:
            return []
        visiting.add(node)
        stack.append(node)
        for dep in sorted(graph.get(node, set())):
            if dep in graph:
                cycle = visit(dep)
                if cycle:
                    return cycle
        stack.pop()
        visiting.remove(node)
        visited.add(node)
        return []

    for node in sorted(graph):
        cycle = visit(node)
        if cycle:
            return cycle
    return []


def sanitize(value: object) -> object:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            text_key = str(key)
            if is_sensitive_key(text_key):
                sanitized[text_key] = "[redacted]"
            else:
                sanitized[text_key] = sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return sanitize_string(value)
    return value


def sanitize_string(value: str) -> str:
    if len(value) > 160:
        return value[:157].rstrip() + "..."
    return value


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in SENSITIVE_KEYS or any(part in lowered for part in ("token", "secret", "credential"))


def has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def add_warning(report: dict, message: str) -> None:
    report["warnings"].append(message)


def add_error(report: dict, message: str) -> None:
    report["errors"].append(message)


def add_op_error(report: dict, op_report: dict, message: str) -> None:
    op_report["errors"].append(message)
    add_error(report, f"op[{op_report['index']}]: {message}")


def finalize(report: dict) -> dict:
    report["ok"] = not report["errors"]
    return report


def render_apply_plan_report(report: dict) -> str:
    lines = [
        f"mode: {report['mode']}",
        f"valid: {str(report['ok']).lower()}",
        f"operations: {report['operation_count']}",
        f"errors: {len(report['errors'])}",
        f"warnings: {len(report['warnings'])}",
    ]
    if report["operations"]:
        lines.append("would_change:")
    for operation in report["operations"]:
        task_ids = ",".join(operation["task_ids"]) if operation["task_ids"] else "-"
        change = "yes" if operation["would_change"] else "no"
        lines.append(f"  op[{operation['index']}]\t{operation['op']}\ttasks={task_ids}\twould_change={change}")
    if report["warnings"]:
        lines.append("warnings:")
        lines.extend(f"  {warning}" for warning in report["warnings"])
    if report["errors"]:
        lines.append("errors:")
        lines.extend(f"  {error}" for error in report["errors"])
    return "\n".join(lines) + "\n"

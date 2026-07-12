from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config
from .events import emit_task_event
from .fs import read_json
from .lock import FileLock
from .queue import ROUTING_RISKS, ROUTING_SIZES, VERIFICATION_SCOPES, list_tasks, load_task, save_task
from .triggers import run_post_mutation_trigger


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

APPLY_MUTATION_FIELDS = {
    "title",
    "description",
    "category",
    "labels",
    "depends_on",
    "status",
    "routing_reason",
    "routing_risk_factors",
    "routing_experiment",
    "routing_size",
    "routing_risk",
    "verification_scope",
}
SAFE_STATUS_VALUES = {
    "runnable",
    "needs_resume",
    "blocked_user",
    "failed",
    "completed",
    "archived",
    "paused",
    "cancelled",
    "superseded",
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
        op_report = validate_operation(config, index, raw_operation, plan, by_id, graph, created_ids, report)
        report["operations"].append(op_report)

    cycle = find_cycle(graph)
    if cycle:
        add_error(report, "dependency graph would contain a cycle: " + " -> ".join(cycle))

    return finalize(report)


def validate_operation(
    config: Config,
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
            continue
        validate_expected_state(raw_operation, task, op_report, report)
        validate_safe_field_updates(config, raw_operation, task_id, op_report, report)

    validate_dependency_references(raw_operation, by_id, created_ids, op_report, report)
    apply_dependency_simulation(raw_operation, by_id, graph, created_ids, op_report, report)
    op_report["would_change"] = not op_report["errors"] and op in SUPPORTED_OPERATIONS
    return op_report


def apply_queue_mutation_plan(config: Config, plan_path: str | Path) -> dict:
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire():
        return {
            "mode": "apply",
            "ok": False,
            "plan": {},
            "operation_count": 0,
            "operations": [],
            "warnings": [],
            "errors": [f"another runner is active: {config.lock_file}"],
            "applied": False,
            "mutated_task_ids": [],
        }

    try:
        report = build_apply_plan_report(config, plan_path)
        report["mode"] = "apply"
        report["applied"] = False
        report["mutated_task_ids"] = []
        if not report["ok"]:
            return report

        plan = read_json(Path(plan_path).expanduser().resolve())
        apply_errors = validate_apply_supported_operations(plan)
        if apply_errors:
            report["ok"] = False
            report["errors"].extend(apply_errors)
            return report
        operations = plan.get("operations", []) if isinstance(plan, dict) else []
        mutated_task_ids: set[str] = set()
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            for task_id in sorted(operation_target_ids(operation)):
                task = load_task(config, task_id)
                before = mutation_summary(task)
                changed_fields = apply_operation_to_task(task, operation)
                if not changed_fields:
                    continue
                save_task(config, task)
                mutated_task_ids.add(task_id)
                emit_task_event(
                    config,
                    "task_mutated",
                    task,
                    actor=actor_summary(plan.get("actor")),
                    source="apply-plan",
                    summary=f"applied {operation.get('op')} to {task_id}",
                    payload={
                        "plan_id": plan.get("plan_id"),
                        "op": operation.get("op"),
                        "reason": operation_reason(plan, operation),
                        "changed_fields": changed_fields,
                        "before": before,
                        "after": mutation_summary(task),
                    },
                )

        report["applied"] = True
        report["mutated_task_ids"] = sorted(mutated_task_ids)
        if mutated_task_ids:
            run_post_mutation_trigger(config)
        return report
    finally:
        lock.release()


def apply_operation_to_task(task: dict[str, Any], operation: dict[str, Any]) -> list[str]:
    before = {field: task.get(field) for field in APPLY_MUTATION_FIELDS}
    op = operation.get("op")
    fields = operation.get("fields")
    field_updates = fields if isinstance(fields, dict) else {}

    for field, value in normalized_field_updates(field_updates).items():
        task[field] = value

    if op == "dependency_changes":
        task["depends_on"] = sorted(mutated_dependencies(task.get("depends_on"), operation))
    elif op == "pause":
        if task.get("status") != "paused":
            task["previous_status"] = task.get("status")
            task["status"] = "paused"
    elif op == "unpause":
        if task.get("status") == "paused":
            previous = task.get("previous_status")
            task["status"] = previous if previous in RUNNABLE_UNPAUSE_STATUSES else "runnable"
    elif op == "supersede":
        task["status"] = "superseded"

    return [field for field in sorted(APPLY_MUTATION_FIELDS) if task.get(field) != before.get(field)]


RUNNABLE_UNPAUSE_STATUSES = {"runnable", "needs_resume", "blocked_user", "failed"}


def normalized_field_updates(fields: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for field in APPLY_MUTATION_FIELDS:
        if field not in fields:
            continue
        value = fields[field]
        if field in {"title", "description", "category"}:
            updates[field] = None if value is None else str(value)
        elif field == "labels":
            updates[field] = [str(item) for item in value] if isinstance(value, list) else []
        elif field == "depends_on":
            updates[field] = [str(item) for item in value] if isinstance(value, list) else []
        elif field == "status":
            updates[field] = str(value)
        elif field in {"routing_reason", "routing_experiment", "routing_size", "routing_risk"}:
            updates[field] = None if value is None else str(value)
        elif field in {"routing_risk_factors", "verification_scope"}:
            updates[field] = [str(item) for item in value] if isinstance(value, list) else []
    return updates


def mutated_dependencies(current: object, operation: dict[str, Any]) -> set[str]:
    current_deps = {str(item) for item in current if isinstance(item, str) and item} if isinstance(current, list) else set()
    replacement = replacement_dependencies(operation)
    if replacement is not None:
        current_deps = set(replacement)
    current_deps.update(add_dependencies(operation))
    current_deps.difference_update(remove_dependencies(operation))
    return current_deps


def mutation_summary(task: dict[str, Any]) -> dict[str, Any]:
    return sanitize(
        {
            "title": task.get("title"),
            "description_present": has_text(task.get("description")),
            "status": task.get("status"),
            "category": task.get("category"),
            "labels": task.get("labels"),
            "depends_on": task.get("depends_on"),
            "model_requirement_vector": task.get("model_requirement_vector"),
            "routing_reason": task.get("routing_reason"),
            "routing_risk_factors": task.get("routing_risk_factors"),
            "routing_experiment": task.get("routing_experiment"),
            "routing_size": task.get("routing_size"),
            "routing_risk": task.get("routing_risk"),
            "verification_scope": task.get("verification_scope"),
        }
    )


def actor_summary(actor: object) -> str:
    if isinstance(actor, dict):
        actor_type = actor.get("type") or "actor"
        actor_id = actor.get("id") or ""
        return sanitize_string(f"{actor_type}:{actor_id}".strip(":"))
    return sanitize_string(str(actor or "cbr"))


def operation_reason(plan: dict[str, Any], operation: dict[str, Any]) -> str:
    reason = operation.get("reason") or plan.get("reason") or ""
    return sanitize_string(str(reason))


def operation_target_ids(operation: dict) -> set[str]:
    ids: set[str] = set()
    task_id = operation.get("task_id")
    if isinstance(task_id, str) and task_id:
        ids.add(task_id)
    task_ids = operation.get("task_ids")
    if isinstance(task_ids, list):
        ids.update(item for item in task_ids if isinstance(item, str) and item)
    return ids


def validate_expected_state(operation: dict, task: dict, op_report: dict, report: dict) -> None:
    expected = operation.get("expected")
    if not isinstance(expected, dict):
        return
    task_id = str(task.get("id") or "")
    for field, expected_value in sorted(expected.items()):
        actual_value = task.get(field)
        if actual_value != expected_value:
            add_op_error(
                report,
                op_report,
                f"stale task target: {task_id} expected {field}={sanitize(expected_value)!r}, found {sanitize(actual_value)!r}",
            )


def validate_safe_field_updates(config: Config, operation: dict, task_id: str, op_report: dict, report: dict) -> None:
    fields = operation.get("fields")
    if not isinstance(fields, dict):
        return
    if fields.get("status") == "running":
        add_op_error(report, op_report, f"status mutation cannot set running task: {task_id}")
    elif "status" in fields and str(fields.get("status")) not in SAFE_STATUS_VALUES:
        add_op_error(report, op_report, f"unsupported status mutation for {task_id}: {fields.get('status')}")
    if "execution_profile" in fields:
        add_op_error(
            report,
            op_report,
            f"{task_id}: execution_profile is no longer supported; use model_requirement_vector",
        )
    if "model_requirement_vector" in fields:
        add_op_error(
            report,
            op_report,
            f"{task_id}: model_requirement_vector is immutable; create a new task revision instead",
        )
    validate_choice_field(fields, "routing_size", ROUTING_SIZES, task_id, op_report, report)
    validate_choice_field(fields, "routing_risk", ROUTING_RISKS, task_id, op_report, report)
    validate_choice_list_field(fields, "verification_scope", VERIFICATION_SCOPES, task_id, op_report, report)


def validate_choice_field(
    fields: dict,
    field: str,
    choices: tuple[str, ...],
    task_id: str,
    op_report: dict,
    report: dict,
) -> None:
    if field not in fields or fields.get(field) is None:
        return
    value = str(fields.get(field))
    if value not in choices:
        add_op_error(report, op_report, f"{task_id}: {field} must be one of: " + ", ".join(choices))


def validate_choice_list_field(
    fields: dict,
    field: str,
    choices: tuple[str, ...],
    task_id: str,
    op_report: dict,
    report: dict,
) -> None:
    if field not in fields:
        return
    value = fields.get(field)
    if not isinstance(value, list):
        add_op_error(report, op_report, f"{task_id}: {field} must be a list")
        return
    invalid = [str(item) for item in value if str(item) not in choices]
    if invalid:
        add_op_error(report, op_report, f"{task_id}: {field} entries must be one of: " + ", ".join(choices))


def validate_apply_supported_operations(plan: object) -> list[str]:
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    errors: list[str] = []
    operations = plan.get("operations", [])
    if not isinstance(operations, list):
        return ["operations must be a list"]
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            continue
        op = operation.get("op")
        fields = operation.get("fields")
        allowed_extra = {"add", "add_depends_on", "add_dependencies", "remove", "remove_depends_on", "remove_dependencies"}
        if op == "dependency_changes":
            allowed = APPLY_MUTATION_FIELDS | allowed_extra
        else:
            allowed = APPLY_MUTATION_FIELDS
        if isinstance(fields, dict):
            unsupported = sorted(str(field) for field in fields if str(field) not in allowed)
            if unsupported:
                errors.append(f"op[{index}]: unsupported apply field(s): {', '.join(unsupported)}")
        elif fields is not None:
            errors.append(f"op[{index}]: fields must be an object when provided")
        if op in {"split", "merge", "create_followup"}:
            errors.append(f"op[{index}]: apply is not implemented for operation: {op}")
        if op in {"replan", "append_note"} and not isinstance(fields, dict):
            errors.append(f"op[{index}]: apply for {op} requires fields with supported metadata keys")
    return errors


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

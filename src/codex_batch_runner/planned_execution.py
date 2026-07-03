from __future__ import annotations

from typing import Any

from .model_requirements import resolve_execution_config
from .queue import RUNNABLE_STATUSES
from .transcript import sanitize

PLANNED_EXECUTION_TARGET = "none"


def planned_execution_summary(config: Any, task: dict[str, Any]) -> str:
    fields = planned_execution_fields(config, task)
    if not fields:
        return ""
    if fields.get("unavailable"):
        return "unavailable"
    return ", ".join(f"{key}={sanitize(value)}" for key, value in fields.items())


def planned_execution_compact_note(config: Any, task: dict[str, Any]) -> str:
    fields = planned_execution_fields(config, task)
    if not fields or not planned_execution_is_meaningful(fields):
        return ""
    if fields.get("unavailable"):
        return "plan unavailable"
    return "plan {model_source}/{selection_rule}/{execution_target}".format(
        model_source=sanitize(fields.get("model_source") or "unknown"),
        selection_rule=sanitize(fields.get("selection_rule") or "unresolved"),
        execution_target=sanitize(fields.get("execution_target") or PLANNED_EXECUTION_TARGET),
    )


def planned_execution_fields(config: Any, task: dict[str, Any]) -> dict[str, str]:
    if not planned_execution_applicable(task):
        return {}
    try:
        settings = resolve_execution_config(config, task)
    except ValueError:
        return {"unavailable": "true"}
    fields = {
        "model_source": settings.model_source or "unknown",
        "selection_rule": settings.selection_rule or "unresolved",
        "execution_target": settings.execution_target or PLANNED_EXECUTION_TARGET,
    }
    if settings.config_overrides:
        fields["config_override_keys"] = ",".join(sorted(str(key) for key in settings.config_overrides))
    if settings.budget_hint:
        fields["budget_hint"] = str(settings.budget_hint)
    return fields


def planned_execution_applicable(task: dict[str, Any]) -> bool:
    if task.get("status") not in RUNNABLE_STATUSES:
        return False
    return not resolved_execution_config_present(task)


def resolved_execution_config_present(task: dict[str, Any]) -> bool:
    last_run = task.get("last_run")
    if not isinstance(last_run, dict):
        return False
    resolved = last_run.get("resolved_execution_config")
    return isinstance(resolved, dict) and bool(resolved)


def planned_execution_is_meaningful(fields: dict[str, str]) -> bool:
    if fields.get("unavailable"):
        return True
    return (
        fields.get("selection_rule") not in {None, "", "unresolved", "default_execution_config"}
        or fields.get("model_source") not in {None, "", "cli_default"}
        or fields.get("config_override_keys") not in {None, ""}
        or fields.get("budget_hint") not in {None, ""}
    )

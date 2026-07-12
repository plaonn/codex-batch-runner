from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model_requirements import resolve_model_requirement_vector, rule_matches


@dataclass(frozen=True)
class ResolvedWorkerTarget:
    name: str
    selection_rule: str
    selection_reason: str
    requirement_vector: dict[str, Any]
    target: dict[str, Any]


def resolve_worker_target(config: Any, task: dict[str, Any]) -> ResolvedWorkerTarget | None:
    if not worker_target_applicable(task):
        return None
    vector = resolve_model_requirement_vector(config, task)
    dimensions = vector.get("dimensions") if isinstance(vector.get("dimensions"), dict) else {}
    for rule in getattr(config, "worker_selection_rules", []) or []:
        if not rule_matches(rule.get("when", {}), dimensions):
            continue
        name = str(rule.get("worker_target") or "")
        target = (getattr(config, "worker_targets", {}) or {}).get(name)
        if target is None:
            raise ValueError(f"worker_target {name!r} is not configured")
        return ResolvedWorkerTarget(
            name=name,
            selection_rule=str(rule.get("name") or ""),
            selection_reason="matched model_requirement_vector",
            requirement_vector=vector,
            target=target,
        )
    return None


def worker_target_applicable(task: dict[str, Any]) -> bool:
    if task.get("status") == "needs_resume":
        return False
    if str(task.get("execution_backend") or "codex") != "codex":
        return False
    if task.get("execution_backend_explicit") is True:
        return False
    return not task.get("shell_command") and not task.get("external_command")


def planned_worker_capacity_pool(config: Any, task: dict[str, Any]) -> str | None:
    try:
        resolved = resolve_worker_target(config, task)
    except ValueError:
        return None
    if not resolved:
        return None
    return str(resolved.target.get("capacity_pool") or "codex")


def apply_worker_target(task: dict[str, Any], resolved: ResolvedWorkerTarget) -> None:
    target = resolved.target
    backend = str(target.get("execution_backend") or "")
    if backend == "external-json-command":
        validate_worker_command("external_command", target.get("external_command"))
    elif backend == "shell":
        validate_worker_command("shell_command", target.get("shell_command"))
    else:
        raise ValueError(f"worker_target {resolved.name!r} has invalid execution_backend: {backend}")
    task["execution_backend"] = backend
    task["capacity_pool"] = str(target.get("capacity_pool") or "codex")
    if backend == "external-json-command":
        task["external_command"] = list(target.get("external_command") or [])
        if "external_timeout_seconds" in target:
            task["external_timeout_seconds"] = int(target["external_timeout_seconds"])
    elif backend == "shell":
        task["shell_command"] = list(target.get("shell_command") or [])
        if "shell_timeout_seconds" in target:
            task["shell_timeout_seconds"] = int(target["shell_timeout_seconds"])
    task["worker_target"] = resolved.name
    task["worker_selection_rule"] = resolved.selection_rule
    task["worker_selection_reason"] = resolved.selection_reason
    if target.get("worker_family"):
        task["worker_family"] = target.get("worker_family")
    if target.get("model_group"):
        task["worker_model_group"] = target.get("model_group")
    if target.get("budget_hint"):
        task["worker_budget_hint"] = target.get("budget_hint")


def validate_worker_command(name: str, value: object) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"worker_target requires non-empty {name} argv list")

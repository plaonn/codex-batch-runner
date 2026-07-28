from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from .config import Config
from .execution_delegation import (
    ExecutionDelegationError,
    resolved_execution_identity,
    validate_execution_delegation_contract,
)
from .model_requirements import resolve_execution_config
from .worker_routing import (
    apply_worker_target,
    resolve_worker_target,
    worker_target_applicable,
)


CONTRACT = "gateway-neutral-execution-plan-v1"
SCHEMA_VERSION = 1
POLICY_REVISION = "gateway-neutral-execution-policy-v1"
POLICY_FIELD = "gateway_neutral_execution_policy"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
ENVIRONMENT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SENSITIVE_ENVIRONMENT_KEY = re.compile(
    r"(ACCOUNT|AUTH|COOKIE|CREDENTIAL|KEY|PASSWORD|SECRET|SESSION|THREAD|TOKEN)"
)
SUPPORTED_ENVIRONMENT_POLICIES = {"legacy_inherit_current"}
SUPPORTED_CONFIG_MUTATION_POLICIES = {"no_persistent_mutation_v1"}
SUPPORTED_PROCESS_POLICIES = {"legacy_direct_child_timeout_v1"}
FORBIDDEN_OUTPUT_KEYS = {
    "account",
    "argv",
    "command",
    "credential",
    "cwd",
    "environment_values",
    "log",
    "path",
    "prompt",
    "provider_response",
    "session",
    "stderr",
    "stdout",
    "thread",
}


class GatewayNeutralExecutionPlanError(ValueError):
    pass


def build_gateway_neutral_execution_plan(
    config: Config,
    task: dict[str, Any],
) -> dict[str, Any]:
    projected_task = copy.deepcopy(task)
    reasons: list[str] = []
    task_id = _safe_id_or_unavailable(
        projected_task.get("id"),
        reasons,
        "task_binding_invalid",
    )
    task_revision = _task_revision(projected_task, reasons)

    settings = None
    identity: dict[str, Any] | None = None
    target: dict[str, Any] = {}
    if projected_task.get("capacity_target_ordering_canary_request") is not None:
        reasons.append("capacity_canary_projection_unavailable")
    else:
        try:
            resolved_worker = (
                resolve_worker_target(config, projected_task)
                if worker_target_applicable(projected_task)
                else None
            )
            if resolved_worker is not None:
                apply_worker_target(projected_task, resolved_worker)
            backend = str(projected_task.get("execution_backend") or "codex")
            if backend not in {"codex", "external-json-command"}:
                reasons.append("legacy_backend_projection_unavailable")
            else:
                settings = resolve_execution_config(config, projected_task)
                identity = resolved_execution_identity(projected_task, settings)
                snapshot = settings.selected_target_snapshot
                if not isinstance(snapshot, dict) or not isinstance(
                    snapshot.get("target"), dict
                ):
                    reasons.append("legacy_resolved_target_unavailable")
                else:
                    target = snapshot["target"]
        except (ExecutionDelegationError, ValueError):
            reasons.append("resolved_execution_identity_unavailable")

    resolved_backend = (
        str(identity["backend"])
        if identity is not None
        else str(projected_task.get("execution_backend") or "codex")
    )
    backend = (
        resolved_backend
        if resolved_backend in {"codex", "external-json-command", "shell"}
        else "legacy_unavailable"
    )
    timeout_seconds = _timeout_seconds(
        config,
        projected_task,
        resolved_backend,
        reasons,
    )
    policy = _policy_projection(target.get(POLICY_FIELD), reasons)
    target_id = (
        str(identity["target_id"])
        if identity is not None
        else _safe_id_or_unavailable(
            getattr(settings, "execution_target", None),
            reasons,
            "target_binding_unavailable",
        )
    )
    availability_reasons = sorted(set(reasons))
    plan = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT,
        "binding": {
            "task_id": task_id,
            "task_revision": task_revision,
            "target_id": target_id,
        },
        "provenance": {
            "resolved_target_digest": (
                identity["resolved_target_digest"] if identity else None
            ),
            "resolved_config_digest": (
                identity["resolved_config_digest"] if identity else None
            ),
            "command_contract_digest": (
                identity["command_contract_digest"] if identity else None
            ),
        },
        "execution": {
            "backend": backend,
            "timeout_seconds": timeout_seconds,
            "output_contract_revision": policy["output_contract_revision"],
        },
        "policy": {
            "environment": policy["environment"],
            "config_mutation": policy["config_mutation"],
            "process": policy["process"],
        },
        "availability": {
            "status": "available" if not availability_reasons else "unavailable",
            "fail_closed": bool(availability_reasons),
            "reason_codes": availability_reasons,
        },
        "mutation": {
            "allowed": False,
            "applied": False,
        },
    }
    plan["plan_digest"] = _stable_digest(plan)
    return validate_gateway_neutral_execution_plan(plan)


def validate_gateway_neutral_execution_plan(value: object) -> dict[str, Any]:
    expected = {
        "schema_version",
        "contract",
        "binding",
        "provenance",
        "execution",
        "policy",
        "availability",
        "mutation",
        "plan_digest",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise GatewayNeutralExecutionPlanError(
            "execution plan fields are not canonical"
        )
    if value["schema_version"] != SCHEMA_VERSION or value["contract"] != CONTRACT:
        raise GatewayNeutralExecutionPlanError("invalid execution plan contract")
    binding = value["binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "task_id",
        "task_revision",
        "target_id",
    }:
        raise GatewayNeutralExecutionPlanError("invalid execution plan binding")
    for key, item in binding.items():
        _safe_id(item, f"binding.{key}")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "resolved_target_digest",
        "resolved_config_digest",
        "command_contract_digest",
    }:
        raise GatewayNeutralExecutionPlanError("invalid execution plan provenance")
    availability = value["availability"]
    if not isinstance(availability, dict) or set(availability) != {
        "status",
        "fail_closed",
        "reason_codes",
    }:
        raise GatewayNeutralExecutionPlanError("invalid execution plan availability")
    status = availability["status"]
    if status not in {"available", "unavailable"}:
        raise GatewayNeutralExecutionPlanError("invalid availability status")
    reasons = availability["reason_codes"]
    if (
        not isinstance(reasons, list)
        or reasons != sorted(set(reasons))
        or not all(isinstance(item, str) and SAFE_ID.fullmatch(item) for item in reasons)
        or availability["fail_closed"] is not bool(reasons)
        or (status == "available") != (not reasons)
    ):
        raise GatewayNeutralExecutionPlanError("invalid availability reason codes")
    for key, item in provenance.items():
        if item is None:
            if status != "unavailable":
                raise GatewayNeutralExecutionPlanError(
                    f"{key} is required for an available plan"
                )
        else:
            _digest(item, f"provenance.{key}")
    execution = value["execution"]
    if not isinstance(execution, dict) or set(execution) != {
        "backend",
        "timeout_seconds",
        "output_contract_revision",
    }:
        raise GatewayNeutralExecutionPlanError("invalid execution plan settings")
    if execution["backend"] not in {
        "codex",
        "external-json-command",
        "shell",
        "legacy_unavailable",
    }:
        raise GatewayNeutralExecutionPlanError("invalid execution backend")
    timeout = execution["timeout_seconds"]
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1
    ):
        raise GatewayNeutralExecutionPlanError("invalid execution timeout")
    _safe_id(
        execution["output_contract_revision"],
        "execution.output_contract_revision",
    )
    policy = value["policy"]
    if not isinstance(policy, dict) or set(policy) != {
        "environment",
        "config_mutation",
        "process",
    }:
        raise GatewayNeutralExecutionPlanError("invalid execution policy")
    _validate_policy_output(policy)
    if status == "available" and (
        policy["environment"]["name"] not in SUPPORTED_ENVIRONMENT_POLICIES
        or policy["config_mutation"]["name"]
        not in SUPPORTED_CONFIG_MUTATION_POLICIES
        or policy["process"]["name"] not in SUPPORTED_PROCESS_POLICIES
    ):
        raise GatewayNeutralExecutionPlanError(
            "available plan uses unsupported execution policy"
        )
    if value["mutation"] != {"allowed": False, "applied": False}:
        raise GatewayNeutralExecutionPlanError("execution plan must be read-only")
    _validate_public_safe(value)
    expected_digest = _stable_digest(
        {key: item for key, item in value.items() if key != "plan_digest"}
    )
    if value["plan_digest"] != expected_digest:
        raise GatewayNeutralExecutionPlanError(
            "plan_digest does not match canonical plan"
        )
    return copy.deepcopy(value)


def render_gateway_neutral_execution_plan(plan: dict[str, Any]) -> str:
    validated = validate_gateway_neutral_execution_plan(plan)
    return json.dumps(
        validated,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )


def _task_revision(task: dict[str, Any], reasons: list[str]) -> str:
    raw = task.get("execution_delegation_contract")
    if raw is None:
        reasons.append("legacy_task_revision_unavailable")
        return "legacy_unavailable"
    try:
        contract = validate_execution_delegation_contract(raw)
    except ExecutionDelegationError:
        reasons.append("task_revision_invalid")
        return "legacy_unavailable"
    if contract["binding"]["task_id"] != str(task.get("id") or ""):
        reasons.append("task_revision_binding_mismatch")
        return "legacy_unavailable"
    return contract["binding"]["task_revision"]


def _timeout_seconds(
    config: Config,
    task: dict[str, Any],
    backend: str,
    reasons: list[str],
) -> int | None:
    raw: object
    if backend == "external-json-command":
        raw = task.get("external_timeout_seconds")
        if raw is None:
            raw = config.external_json_command_timeout_seconds
    elif backend == "shell":
        raw = task.get("shell_timeout_seconds")
        if raw is None:
            raw = config.shell_task_timeout_seconds
    else:
        raw = config.codex_total_runtime_timeout_seconds
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        reasons.append("timeout_unavailable")
        return None
    return raw


def _policy_projection(
    value: object,
    reasons: list[str],
) -> dict[str, Any]:
    unavailable = {
        "environment": {
            "name": "legacy_unavailable",
            "allowlisted_key_names": [],
        },
        "config_mutation": {"name": "legacy_unavailable"},
        "process": {"name": "legacy_unavailable"},
        "output_contract_revision": "legacy_unavailable",
    }
    if value is None:
        reasons.append("legacy_policy_metadata_unavailable")
        return unavailable
    if not isinstance(value, dict) or set(value) != {
        "revision",
        "environment",
        "config_mutation",
        "process",
        "output_contract_revision",
    }:
        reasons.append("policy_metadata_invalid")
        return unavailable
    if value.get("revision") != POLICY_REVISION:
        reasons.append("policy_revision_unknown")
        return unavailable
    environment = value.get("environment")
    config_mutation = value.get("config_mutation")
    process = value.get("process")
    if not isinstance(environment, dict) or set(environment) != {
        "name",
        "allowlisted_key_names",
    }:
        reasons.append("environment_policy_invalid")
        return unavailable
    try:
        environment_name = _safe_id(
            environment.get("name"),
            "environment.name",
        )
    except GatewayNeutralExecutionPlanError:
        reasons.append("environment_policy_invalid")
        return unavailable
    keys = environment.get("allowlisted_key_names")
    if (
        not isinstance(keys, list)
        or keys != sorted(set(keys))
        or not all(
            isinstance(item, str)
            and ENVIRONMENT_KEY.fullmatch(item)
            and not SENSITIVE_ENVIRONMENT_KEY.search(item)
            for item in keys
        )
    ):
        reasons.append("environment_allowlist_invalid")
        return unavailable
    if environment_name not in SUPPORTED_ENVIRONMENT_POLICIES:
        reasons.append("environment_policy_unknown")
    if (
        not isinstance(config_mutation, dict)
        or set(config_mutation) != {"name"}
    ):
        reasons.append("config_mutation_policy_invalid")
        return unavailable
    try:
        config_mutation_name = _safe_id(
            config_mutation.get("name"),
            "config_mutation.name",
        )
    except GatewayNeutralExecutionPlanError:
        reasons.append("config_mutation_policy_invalid")
        return unavailable
    if config_mutation_name not in SUPPORTED_CONFIG_MUTATION_POLICIES:
        reasons.append("config_mutation_policy_unknown")
    if not isinstance(process, dict) or set(process) != {"name"}:
        reasons.append("process_policy_invalid")
        return unavailable
    try:
        process_name = _safe_id(
            process.get("name"),
            "process.name",
        )
    except GatewayNeutralExecutionPlanError:
        reasons.append("process_policy_invalid")
        return unavailable
    if process_name not in SUPPORTED_PROCESS_POLICIES:
        reasons.append("process_policy_unknown")
    try:
        output_revision = _safe_id(
            value.get("output_contract_revision"),
            "output_contract_revision",
        )
    except GatewayNeutralExecutionPlanError:
        reasons.append("output_contract_revision_invalid")
        return unavailable
    return {
        "environment": {
            "name": environment_name,
            "allowlisted_key_names": list(keys),
        },
        "config_mutation": {"name": config_mutation_name},
        "process": {"name": process_name},
        "output_contract_revision": output_revision,
    }


def _validate_policy_output(policy: dict[str, Any]) -> None:
    environment = policy["environment"]
    if not isinstance(environment, dict) or set(environment) != {
        "name",
        "allowlisted_key_names",
    }:
        raise GatewayNeutralExecutionPlanError("invalid environment policy")
    _safe_id(environment["name"], "policy.environment.name")
    keys = environment["allowlisted_key_names"]
    if (
        not isinstance(keys, list)
        or keys != sorted(set(keys))
        or not all(
            isinstance(item, str)
            and ENVIRONMENT_KEY.fullmatch(item)
            and not SENSITIVE_ENVIRONMENT_KEY.search(item)
            for item in keys
        )
    ):
        raise GatewayNeutralExecutionPlanError("invalid environment key names")
    for name in ("config_mutation", "process"):
        item = policy[name]
        if not isinstance(item, dict) or set(item) != {"name"}:
            raise GatewayNeutralExecutionPlanError(f"invalid {name} policy")
        _safe_id(item["name"], f"policy.{name}.name")


def _safe_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise GatewayNeutralExecutionPlanError(
            f"{name} must be a public-safe identifier"
        )
    return value


def _safe_id_or_unavailable(
    value: object,
    reasons: list[str],
    reason: str,
) -> str:
    try:
        return _safe_id(value, reason)
    except GatewayNeutralExecutionPlanError:
        reasons.append(reason)
        return "legacy_unavailable"


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        value,
    ):
        raise GatewayNeutralExecutionPlanError(f"{name} must be a sha256 digest")
    return value


def _stable_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_public_safe(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_OUTPUT_KEYS:
                raise GatewayNeutralExecutionPlanError(
                    "private execution field is forbidden"
                )
            _validate_public_safe(item)
    elif isinstance(value, list):
        for item in value:
            _validate_public_safe(item)

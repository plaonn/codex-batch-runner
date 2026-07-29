from __future__ import annotations

import copy
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

from .execution_delegation import preexecution_delegation_view
from .gateway_neutral_execution_plan import (
    V2_CONTRACT,
    V2_PROCESS_POLICY,
    validate_gateway_neutral_execution_plan,
)


PROCESS_SCOPE = "same_process_group"
TRIGGERS = {
    "none",
    "startup_stall",
    "first_meaningful_timeout",
    "mid_run_idle_timeout",
    "total_runtime_timeout",
    "external_wall_timeout",
}
SIGNAL_STATES = {"not_sent", "sent", "not_needed", "failed"}
GROUP_OBSERVATIONS = {
    "not_applicable",
    "observed_absent",
    "still_present",
    "unverified",
    "probe_failed",
}
OUTCOMES = {
    "normal_exit",
    "terminated_during_grace",
    "killed_after_grace",
    "direct_child_reaped_group_unverified",
    "termination_failed",
}


class ProcessLifecycleError(ValueError):
    pass


@dataclass(frozen=True)
class ProcessLifecyclePolicy:
    name: str
    termination_grace_seconds: int


def lifecycle_policy_requested(execution_settings: Any) -> bool:
    snapshot = getattr(execution_settings, "selected_target_snapshot", None)
    target = snapshot.get("target") if isinstance(snapshot, dict) else None
    policy = (
        target.get("gateway_neutral_execution_policy")
        if isinstance(target, dict)
        else None
    )
    return isinstance(policy, dict) and policy.get("revision") == (
        "gateway-neutral-execution-policy-v2"
    )


def resolve_process_lifecycle_policy(
    task: dict[str, Any],
    *,
    backend: str,
    platform_name: str | None = None,
) -> ProcessLifecyclePolicy | None:
    raw_plan = task.get("active_gateway_neutral_execution_plan")
    if raw_plan is None:
        settings = task.get("_resolved_execution_settings")
        if lifecycle_policy_requested(settings):
            raise ProcessLifecycleError(
                "lifecycle policy requires a bound v2 execution plan"
            )
        return None
    plan = validate_gateway_neutral_execution_plan(raw_plan)
    if plan["contract"] != V2_CONTRACT or plan["schema_version"] != 2:
        raise ProcessLifecycleError("lifecycle execution plan must use v2")
    if plan["availability"] != {
        "status": "available",
        "fail_closed": False,
        "reason_codes": [],
    }:
        raise ProcessLifecycleError("lifecycle execution plan is unavailable")
    if (platform_name or os.name) != "posix":
        raise ProcessLifecycleError("lifecycle policy is unsupported on this platform")
    if backend not in {"codex", "external-json-command"}:
        raise ProcessLifecycleError("lifecycle policy is unsupported for backend")
    if plan["execution"]["backend"] != backend:
        raise ProcessLifecycleError("lifecycle plan runtime backend mismatch")

    receipt = preexecution_delegation_view(task)
    if receipt.get("status") != "verified-local-preexecution-binding":
        raise ProcessLifecycleError(
            "lifecycle policy requires a verified pre-execution receipt"
        )
    target = receipt.get("target")
    binding = plan["binding"]
    provenance = plan["provenance"]
    if (
        not isinstance(target, dict)
        or receipt.get("schema_version") != 2
        or receipt.get("contract") != "cbr-preexecution-delegation-receipt-v2"
        or target.get("gateway_neutral_execution_plan_digest")
        != plan["plan_digest"]
        or target.get("target_id") != binding["target_id"]
        or receipt.get("task_revision") != binding["task_revision"]
        or target.get("target_snapshot_digest")
        != provenance["resolved_target_digest"]
        or target.get("resolved_config_digest")
        != provenance["resolved_config_digest"]
        or target.get("command_contract_digest")
        != provenance["command_contract_digest"]
        or str(task.get("id") or "") != binding["task_id"]
    ):
        raise ProcessLifecycleError(
            "lifecycle plan and pre-execution receipt binding mismatch"
        )
    process = plan["policy"]["process"]
    if process.get("name") != V2_PROCESS_POLICY:
        raise ProcessLifecycleError("unsupported lifecycle process policy")
    grace = process.get("termination_grace_seconds")
    if isinstance(grace, bool) or not isinstance(grace, int) or grace < 1:
        raise ProcessLifecycleError("invalid lifecycle termination grace")
    return ProcessLifecyclePolicy(
        name=V2_PROCESS_POLICY,
        termination_grace_seconds=grace,
    )


def normal_exit_lifecycle() -> dict[str, Any]:
    return validate_process_lifecycle(
        {
            "schema_version": 1,
            "policy": V2_PROCESS_POLICY,
            "scope": PROCESS_SCOPE,
            "trigger": "none",
            "term": "not_sent",
            "kill": "not_sent",
            "direct_child_reaped": True,
            "group_observation": "not_applicable",
            "outcome": "normal_exit",
        }
    )


def terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    process_group_id: int,
    trigger: str,
    grace_seconds: int,
    killpg: Callable[[int, int], None] = os.killpg,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if trigger not in TRIGGERS - {"none"}:
        raise ProcessLifecycleError("invalid lifecycle termination trigger")
    term = "not_sent"
    kill = "not_sent"
    observation = "unverified"
    direct_child_reaped = False

    try:
        killpg(process_group_id, signal.SIGTERM)
        term = "sent"
    except ProcessLookupError:
        term = "not_needed"
        observation = "observed_absent"
    except OSError:
        term = "failed"

    deadline = monotonic() + grace_seconds
    if term in {"sent", "not_needed"}:
        while monotonic() < deadline:
            if poll_direct_child(process):
                direct_child_reaped = True
            observation = observe_process_group(process_group_id, killpg=killpg)
            if observation == "observed_absent":
                break
            if observation in {"probe_failed", "unverified"}:
                break
            sleep(min(0.05, max(0.0, deadline - monotonic())))
    elif term == "failed":
        observation = observe_process_group(process_group_id, killpg=killpg)

    if observation == "observed_absent":
        kill = "not_needed"
    else:
        try:
            killpg(process_group_id, signal.SIGKILL)
            kill = "sent"
        except ProcessLookupError:
            kill = "not_needed"
            observation = "observed_absent"
        except OSError:
            kill = "failed"

    if not direct_child_reaped:
        try:
            process.wait(timeout=max(1.0, float(grace_seconds)))
            direct_child_reaped = True
        except subprocess.TimeoutExpired:
            direct_child_reaped = False

    if kill in {"sent", "not_needed"} and observation != "observed_absent":
        final_deadline = monotonic() + grace_seconds
        while monotonic() < final_deadline:
            if poll_direct_child(process):
                direct_child_reaped = True
            observation = observe_process_group(process_group_id, killpg=killpg)
            if observation == "observed_absent":
                break
            if observation in {"probe_failed", "unverified"}:
                break
            sleep(min(0.05, max(0.0, final_deadline - monotonic())))

    if not direct_child_reaped or term == "failed" or kill == "failed":
        outcome = "termination_failed"
    elif observation in {"probe_failed", "unverified", "still_present"}:
        outcome = "direct_child_reaped_group_unverified"
    elif kill == "sent":
        outcome = "killed_after_grace"
    else:
        outcome = "terminated_during_grace"
    return validate_process_lifecycle(
        {
            "schema_version": 1,
            "policy": V2_PROCESS_POLICY,
            "scope": PROCESS_SCOPE,
            "trigger": trigger,
            "term": term,
            "kill": kill,
            "direct_child_reaped": direct_child_reaped,
            "group_observation": observation,
            "outcome": outcome,
        }
    )


def poll_direct_child(process: subprocess.Popen[Any]) -> bool:
    poll = getattr(process, "poll", None)
    return bool(callable(poll) and poll() is not None)


def observe_process_group(
    process_group_id: int,
    *,
    killpg: Callable[[int, int], None] = os.killpg,
) -> str:
    try:
        killpg(process_group_id, 0)
    except ProcessLookupError:
        return "observed_absent"
    except PermissionError:
        return "unverified"
    except OSError:
        return "probe_failed"
    return "still_present"


def validate_process_lifecycle(value: object) -> dict[str, Any]:
    expected = {
        "schema_version",
        "policy",
        "scope",
        "trigger",
        "term",
        "kill",
        "direct_child_reaped",
        "group_observation",
        "outcome",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ProcessLifecycleError("process lifecycle fields are not canonical")
    if (
        value["schema_version"] != 1
        or value["policy"] != V2_PROCESS_POLICY
        or value["scope"] != PROCESS_SCOPE
        or value["trigger"] not in TRIGGERS
        or value["term"] not in SIGNAL_STATES
        or value["kill"] not in SIGNAL_STATES
        or not isinstance(value["direct_child_reaped"], bool)
        or value["group_observation"] not in GROUP_OBSERVATIONS
        or value["outcome"] not in OUTCOMES
    ):
        raise ProcessLifecycleError("invalid process lifecycle evidence")
    return copy.deepcopy(value)

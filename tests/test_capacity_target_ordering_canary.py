from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

from codex_batch_runner.capacity_target_ordering_canary import (
    HARD_CEILING_PERCENT,
    REQUEST_CONTRACT,
    CapacityTargetOrderingCanaryError,
    apply_capacity_target_ordering_canary,
    capacity_target_ordering_canary_policy_value,
    record_capacity_target_ordering_canary_outcome,
    reconstruct_capacity_target_ordering_canary,
    selected_capacity_target_ordering_canary_target,
    stable_digest,
)
from codex_batch_runner.capacity_target_ordering_simulation import (
    simulate_capacity_target_ordering_activation,
)
from codex_batch_runner.model_requirements import (
    ResolvedExecutionConfig,
    resolve_execution_config,
)
from codex_batch_runner.runner import (
    apply_configured_worker_target,
    capacity_canary_dispatch_binding_error,
)
from tests.test_capacity_target_ordering_simulation import (
    alternative_shadow_request,
    simulation_request,
)
from tests.test_execution_target_selector import (
    codex_target,
    loaded_config,
    requirement,
)


SCOPE = {
    "project_id": "public-project",
    "repository_id": "public-repository",
    "task_class": "bounded-readonly-objective",
}
REQUIREMENT = {"schema_version": 2, "revision_id": "requirement-r1"}
ASSESSMENT = {
    "selection_policy_version": "execution-target-selector-v1",
    "inventory_snapshot_id": "inventory-r1",
    "ranked_eligible_target_ids": ["target-a", "target-b"],
}
DISPATCH_AT = "2030-01-02T04:00:00+00:00"


def policy(*, enabled: bool = True, kill_switch: bool = False) -> dict:
    return {
        "schema_version": 1,
        "contract": "capacity-target-ordering-canary-policy-v1",
        "revision": "capacity-target-ordering-canary-policy-v1",
        "enabled": enabled,
        "assignment_percent": 5,
        "hard_ceiling_percent": 10,
        "kill_switch_active": kill_switch,
        "max_evidence_age_seconds": 300,
        "allowed_scopes": [copy.deepcopy(SCOPE)],
    }


def _apply(**kwargs: object) -> str:
    return apply_capacity_target_ordering_canary(
        **kwargs,
        dispatch_evaluated_at=DISPATCH_AT,
    )


def task_with_request() -> dict:
    report = simulate_capacity_target_ordering_activation(
        simulation_request(alternative_shadow_request())
    )
    base = {
        "schema_version": 1,
        "contract": REQUEST_CONTRACT,
        "scope": copy.deepcopy(SCOPE),
        "policy_revision": "capacity-target-ordering-canary-policy-v1",
        "task_id": "task-canary-3",
        "evidence_revision": "capacity-evidence-r1",
        "activation_report": report,
        "activation_report_digest": stable_digest(report),
    }
    task = {
        "id": "task-canary-3",
        "project_id": SCOPE["project_id"],
        "project_root": "/public/public-repository",
        "category": SCOPE["task_class"],
        "capacity_target_ordering_assignment_id": "runner-issued-51",
        "status": "runnable",
        "attempts": 0,
        "capacity_target_ordering_canary_request": base,
    }
    return task


class CapacityTargetOrderingCanaryTests(unittest.TestCase):
    def test_policy_is_default_off_and_enforces_hard_ten_percent_ceiling(self) -> None:
        default = capacity_target_ordering_canary_policy_value(None)
        self.assertFalse(default["enabled"])
        self.assertTrue(default["kill_switch_active"])
        self.assertEqual(HARD_CEILING_PERCENT, default["hard_ceiling_percent"])

        excessive = policy()
        excessive["assignment_percent"] = 11
        with self.assertRaisesRegex(
            CapacityTargetOrderingCanaryError, "hard ceiling"
        ):
            capacity_target_ordering_canary_policy_value(excessive)

    def test_assigned_claim_reorders_only_the_exact_eligible_baseline(self) -> None:
        task = task_with_request()

        selected = _apply(
            policy=policy(),
            task=task,
            requirement=REQUIREMENT,
            assessment=ASSESSMENT,
        )

        self.assertEqual("target-b", selected)
        self.assertEqual(1, len(task["capacity_target_ordering_canary_decision_history"]))
        decision = task["capacity_target_ordering_canary_decision_history"][0]
        self.assertEqual(["target-a", "target-b"], decision["baseline"]["order"])
        self.assertEqual(["target-b", "target-a"], decision["canary"]["order"])
        self.assertFalse(decision["default_routing"])
        self.assertFalse(decision["global_activation"])
        self.assertFalse(decision["provider_priority_mutation"])
        self.assertFalse(decision["queue_mutation"])
        self.assertFalse(decision["promotion_authority"])

    def test_selector_consumer_is_read_only_and_revalidates_dispatch_inputs(self) -> None:
        task = task_with_request()
        _apply(
            policy=policy(),
            task=task,
            requirement=REQUIREMENT,
            assessment=ASSESSMENT,
        )
        before = copy.deepcopy(task)

        self.assertEqual(
            "target-b",
            selected_capacity_target_ordering_canary_target(
                task=task,
                requirement=REQUIREMENT,
                assessment=ASSESSMENT,
            ),
        )
        self.assertEqual(before, task)
        drifted = {**ASSESSMENT, "inventory_snapshot_id": "inventory-r2"}
        with self.assertRaisesRegex(
            CapacityTargetOrderingCanaryError, "drifted"
        ):
            selected_capacity_target_ordering_canary_target(
                task=task,
                requirement=REQUIREMENT,
                assessment=drifted,
            )

    def test_runner_claim_boundary_drives_both_selector_consumers_once(self) -> None:
        base = loaded_config(
            {
                "target-a": codex_target("model-a", quality=800),
                "target-b": codex_target("model-b", quality=750),
            }
        )
        inventory = copy.deepcopy(base.execution_target_inventory)
        inventory["snapshot_id"] = "inventory-r1"
        config = replace(
            base,
            execution_target_inventory=inventory,
            capacity_target_ordering_canary_policy=policy(),
        )
        task = task_with_request()
        vector = requirement(floor=500)
        vector["revision_id"] = "requirement-r1"
        task["model_requirement_vector"] = vector

        with patch(
            "codex_batch_runner.runner.iso_now", return_value=DISPATCH_AT
        ):
            self.assertIsNone(apply_configured_worker_target(config, task))
        first = copy.deepcopy(
            task["capacity_target_ordering_canary_decision_history"]
        )
        selected = resolve_execution_config(config, task)
        selected_again = resolve_execution_config(config, task)

        self.assertEqual("target-b", selected.execution_target)
        self.assertEqual("target-b", selected_again.execution_target)
        self.assertEqual(
            "bounded_capacity_target_ordering_canary",
            selected.selection_reason,
        )
        self.assertEqual(
            first, task["capacity_target_ordering_canary_decision_history"]
        )

    def test_malformed_or_manual_and_resume_paths_preserve_baseline(self) -> None:
        malformed = task_with_request()
        malformed["capacity_target_ordering_canary_request"][
            "activation_report_digest"
        ] = "sha256:" + ("0" * 64)
        self.assertEqual(
            "target-a",
            _apply(
                policy=policy(),
                task=malformed,
                requirement=REQUIREMENT,
                assessment=ASSESSMENT,
            ),
        )
        self.assertNotIn(
            "capacity_target_ordering_canary_decision_history", malformed
        )

        manual = task_with_request()
        manual["routing_override"] = {"mode": "pin", "target_id": "target-a"}
        self.assertEqual(
            "target-a",
            _apply(
                policy=policy(),
                task=manual,
                requirement=REQUIREMENT,
                assessment=ASSESSMENT,
            ),
        )

        resumed = task_with_request()
        resumed["status"] = "needs_resume"
        self.assertEqual(
            "target-a",
            _apply(
                policy=policy(),
                task=resumed,
                requirement=REQUIREMENT,
                assessment=ASSESSMENT,
            ),
        )
        self.assertEqual(
            "keep_baseline",
            resumed["capacity_target_ordering_canary_decision_history"][0][
                "decision"
            ],
        )

    def test_runner_time_and_immutable_assignment_identity_are_fail_closed(self) -> None:
        future = task_with_request()
        self.assertEqual(
            "target-a",
            apply_capacity_target_ordering_canary(
                policy=policy(),
                task=future,
                requirement=REQUIREMENT,
                assessment=ASSESSMENT,
                dispatch_evaluated_at="2026-07-26T00:00:00+00:00",
            ),
        )
        self.assertNotIn(
            "capacity_target_ordering_canary_decision_history", future
        )

        caller_seed = task_with_request()
        caller_seed["capacity_target_ordering_canary_request"][
            "assignment_key"
        ] = "caller-searchable-seed"
        self.assertEqual(
            "target-a",
            _apply(
                policy=policy(),
                task=caller_seed,
                requirement=REQUIREMENT,
                assessment=ASSESSMENT,
            ),
        )
        self.assertNotIn(
            "capacity_target_ordering_canary_decision_history", caller_seed
        )

        changed_id = task_with_request()
        changed_id["id"] = "caller-selected-different-id"
        changed_id["capacity_target_ordering_canary_request"]["task_id"] = (
            changed_id["id"]
        )
        self.assertEqual(
            "target-b",
            _apply(
                policy=policy(),
                task=changed_id,
                requirement=REQUIREMENT,
                assessment=ASSESSMENT,
            ),
        )

    def test_explicit_backend_never_records_selector_canary_evidence(self) -> None:
        config = loaded_config(
            {"target-a": codex_target("model-a")}
        )
        config = replace(
            config,
            capacity_target_ordering_canary_policy=policy(),
        )
        task = task_with_request()
        task.update(
            {
                "execution_backend": "shell",
                "execution_backend_explicit": True,
                "shell_command": ["true"],
            }
        )

        self.assertIsNone(apply_configured_worker_target(config, task))
        self.assertNotIn(
            "capacity_target_ordering_canary_decision_history", task
        )

    def test_resolved_dispatch_must_match_the_claim_decision(self) -> None:
        task = task_with_request()
        _apply(
            policy=policy(),
            task=task,
            requirement=REQUIREMENT,
            assessment=ASSESSMENT,
        )
        baseline = ResolvedExecutionConfig(
            requirement_vector=REQUIREMENT,
            execution_target="target-a",
        )
        canary = replace(baseline, execution_target="target-b")

        self.assertIn(
            "does not exact-bind",
            capacity_canary_dispatch_binding_error(task, baseline),
        )
        self.assertIsNone(
            capacity_canary_dispatch_binding_error(task, canary)
        )

    def test_kill_switch_and_adverse_outcome_stop_new_canary(self) -> None:
        killed = task_with_request()
        self.assertEqual(
            "target-a",
            _apply(
                policy=policy(kill_switch=True),
                task=killed,
                requirement=REQUIREMENT,
                assessment=ASSESSMENT,
            ),
        )
        self.assertEqual(
            "stop_new_canary",
            killed["capacity_target_ordering_canary_decision_history"][0][
                "decision"
            ],
        )

        task = task_with_request()
        _apply(
            policy=policy(),
            task=task,
            requirement=REQUIREMENT,
            assessment=ASSESSMENT,
        )
        task["attempts"] = 1
        task["status"] = "failed"
        outcome = record_capacity_target_ordering_canary_outcome(
            task, recorded_at="2030-01-02T04:01:00+00:00"
        )
        self.assertTrue(outcome["adverse"])
        self.assertTrue(outcome["rollback_applied"])
        reconstructed = reconstruct_capacity_target_ordering_canary(task)
        self.assertTrue(reconstructed["stop_new_canary"])
        self.assertTrue(reconstructed["baseline_reconstruction_only"])

    def test_missing_outcome_is_fail_closed_and_reconstructable(self) -> None:
        task = task_with_request()
        _apply(
            policy=policy(),
            task=task,
            requirement=REQUIREMENT,
            assessment=ASSESSMENT,
        )

        reconstructed = reconstruct_capacity_target_ordering_canary(task)

        self.assertEqual(1, len(reconstructed["unresolved_decision_ids"]))
        self.assertTrue(reconstructed["stop_new_canary"])
        self.assertTrue(reconstructed["baseline_reconstruction_only"])

    def test_preclaim_failure_closes_the_pending_attempt(self) -> None:
        task = task_with_request()
        _apply(
            policy=policy(),
            task=task,
            requirement=REQUIREMENT,
            assessment=ASSESSMENT,
        )
        task["status"] = "failed"

        outcome = record_capacity_target_ordering_canary_outcome(
            task, recorded_at="2030-01-02T04:01:00+00:00"
        )

        self.assertEqual(1, outcome["binding"]["attempt"])
        self.assertTrue(outcome["adverse"])
        self.assertTrue(outcome["rollback_applied"])


if __name__ == "__main__":
    unittest.main()

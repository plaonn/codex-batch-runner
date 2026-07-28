from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.execution_delegation import (
    build_execution_delegation_contract,
)
from codex_batch_runner.gateway_neutral_execution_plan import (
    GatewayNeutralExecutionPlanError,
    build_gateway_neutral_execution_plan,
    validate_gateway_neutral_execution_plan,
)
from codex_batch_runner.queue import create_task


QUALITY_AXES = (
    "semantic_reasoning",
    "context_integration",
    "planning_depth",
    "instruction_fidelity",
    "tool_execution_reliability",
    "adversarial_detection",
)


def requirement() -> dict:
    return {
        "schema_version": 2,
        "derivation_version": "requirement-rubric-v1",
        "revision_id": "public-requirement-r1",
        "quality_requirements": {
            axis: {
                "score": 500,
                "confidence": 750,
                "anchor": 500,
                "evidence_codes": [],
            }
            for axis in QUALITY_AXES
        },
        "hard_constraints": {
            "required_execution_surfaces": ["external"],
            "interactive_input_required": False,
        },
        "utility_preferences": {
            "latency_weight": 500,
            "cost_weight": 500,
        },
    }


def policy(*, keys: list[str] | None = None) -> dict:
    return {
        "revision": "gateway-neutral-execution-policy-v1",
        "environment": {
            "name": "legacy_inherit_current",
            "allowlisted_key_names": keys or ["LANG", "PATH"],
        },
        "config_mutation": {
            "name": "no_persistent_mutation_v1",
        },
        "process": {
            "name": "legacy_direct_child_timeout_v1",
        },
        "output_contract_revision": "cbr-external-json-final-v1",
    }


def delegation(task_id: str) -> dict:
    return build_execution_delegation_contract(
        task_id=task_id,
        task_revision="public-task-r1",
        task_class="readonly-objective",
        issuer_source_kind="adopted-task-contract",
        authority_revision="public-authority-r1",
        policy_revision="public-policy-r1",
        execution_revision="public-execution-r1",
        review_revision="public-review-r1",
        side_effect_boundary={
            "cbr_controlled_repository_write_allowed": False,
            "external_state_mutation_allowed": False,
            "credential_access_allowed": False,
            "deployment_or_publication_allowed": False,
            "destructive_action_allowed": False,
        },
    )


def write_config(root: Path, *, target_policy: object = None) -> Path:
    target = {
        "execution_surface": "external",
        "execution_backend": "external-json-command",
        "external_command": [
            "public-wrapper",
            "--model",
            "{model}",
            "--reasoning",
            "{reasoning_effort}",
        ],
        "model": "public-model-v1",
        "command_model": "public-model-v1",
        "reasoning_effort": "medium",
        "trust_state": "trusted",
        "static_fitness": {axis: 1000 for axis in QUALITY_AXES},
        "latency_score": 500,
        "cost_score": 500,
        "capabilities": {
            "required_execution_surfaces": ["external"],
            "interactive_input_required": False,
        },
        "capability_evidence": {
            "required_execution_surfaces": {"source": "surface_reported"},
            "interactive_input_required": {"source": "surface_reported"},
        },
    }
    if target_policy is not None:
        target["gateway_neutral_execution_policy"] = target_policy
    data = {
        "root": str(root),
        "queue_dir": str(root / "tasks"),
        "log_dir": str(root / "logs"),
        "event_dir": str(root / "events"),
        "lock_file": str(root / "runner.lock"),
        "state_file": str(root / "state.json"),
        "external_json_command_timeout_seconds": 321,
        "execution_target_inventory": {
            "schema_version": 1,
            "snapshot_id": "public-inventory-r1",
            "status": "current",
            "constraint_registry_version": "public-constraints-r1",
            "targets": {
                "public-external-target": target,
            },
        },
        "constraint_registry": {
            "schema_version": 1,
            "version": "public-constraints-r1",
            "constraints": {
                "required_execution_surfaces": {"unknown_policy": "reject"},
                "interactive_input_required": {"unknown_policy": "reject"},
            },
        },
    }
    path = root / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def create_external_task(config: Config, task_id: str = "public-plan-task") -> dict:
    return create_task(
        config,
        "Private prompt value must never enter the projection.",
        str(config.root),
        task_id=task_id,
        execution_backend="external-json-command",
        external_command=[
            "public-wrapper",
            "--model",
            "public-model-v1",
            "--reasoning",
            "medium",
        ],
        model_requirement_vector=requirement(),
        execution_delegation_contract=delegation(task_id),
    )


class GatewayNeutralExecutionPlanTests(unittest.TestCase):
    def test_public_example_is_canonical_and_valid(self) -> None:
        example = (
            Path(__file__).parent.parent
            / "examples"
            / "gateway-neutral-execution-plan-v1.example.json"
        )
        plan = json.loads(example.read_text(encoding="utf-8"))

        self.assertEqual(plan, validate_gateway_neutral_execution_plan(plan))

    def test_builder_is_deterministic_available_and_uses_receipt_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root, target_policy=policy())))
            task = create_external_task(config)

            first = build_gateway_neutral_execution_plan(config, task)
            second = build_gateway_neutral_execution_plan(config, task)

            self.assertEqual(first, second)
            self.assertEqual("available", first["availability"]["status"])
            self.assertEqual([], first["availability"]["reason_codes"])
            self.assertEqual(
                "public-task-r1",
                first["binding"]["task_revision"],
            )
            self.assertEqual(
                "public-external-target",
                first["binding"]["target_id"],
            )
            self.assertEqual(
                ["LANG", "PATH"],
                first["policy"]["environment"]["allowlisted_key_names"],
            )
            self.assertEqual(
                {
                    "allowed": False,
                    "applied": False,
                },
                first["mutation"],
            )
            for digest in (
                first["plan_digest"],
                *first["provenance"].values(),
            ):
                self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
            rendered = json.dumps(first, sort_keys=True)
            self.assertNotIn("Private prompt", rendered)
            self.assertNotIn("public-wrapper", rendered)
            self.assertNotIn(str(root), rendered)

    def test_missing_policy_is_explicit_legacy_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            task = create_external_task(config)
            before = copy.deepcopy(task)

            plan = build_gateway_neutral_execution_plan(config, task)

            self.assertEqual(before, task)
            self.assertEqual("unavailable", plan["availability"]["status"])
            self.assertTrue(plan["availability"]["fail_closed"])
            self.assertEqual(
                ["legacy_policy_metadata_unavailable"],
                plan["availability"]["reason_codes"],
            )
            self.assertEqual(
                "legacy_unavailable",
                plan["policy"]["environment"]["name"],
            )

    def test_missing_delegation_is_explicit_legacy_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root, target_policy=policy())))
            task = create_external_task(config)
            task.pop("execution_delegation_contract")

            plan = build_gateway_neutral_execution_plan(config, task)

            self.assertEqual("unavailable", plan["availability"]["status"])
            self.assertIn(
                "legacy_task_revision_unavailable",
                plan["availability"]["reason_codes"],
            )
            self.assertEqual(
                "legacy_unavailable",
                plan["binding"]["task_revision"],
            )

    def test_invalid_or_sensitive_policy_metadata_fails_closed_without_echo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(
                str(write_config(root, target_policy=policy(keys=["API_TOKEN"])))
            )
            task = create_external_task(config)

            plan = build_gateway_neutral_execution_plan(config, task)

            self.assertEqual(
                ["environment_allowlist_invalid"],
                plan["availability"]["reason_codes"],
            )
            self.assertNotIn("API_TOKEN", json.dumps(plan, sort_keys=True))

    def test_unknown_opt_in_policy_is_visible_but_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            opt_in = policy(keys=["LANG", "PATH"])
            opt_in["environment"]["name"] = "allowlist_v1"
            opt_in["process"]["name"] = "posix_process_group_v1"
            config = Config.load(str(write_config(root, target_policy=opt_in)))

            plan = build_gateway_neutral_execution_plan(
                config,
                create_external_task(config),
            )

            self.assertEqual("unavailable", plan["availability"]["status"])
            self.assertEqual(
                [
                    "environment_policy_unknown",
                    "process_policy_unknown",
                ],
                plan["availability"]["reason_codes"],
            )
            self.assertEqual(
                {
                    "name": "allowlist_v1",
                    "allowlisted_key_names": ["LANG", "PATH"],
                },
                plan["policy"]["environment"],
            )
            self.assertEqual(
                {"name": "posix_process_group_v1"},
                plan["policy"]["process"],
            )

    def test_unknown_backend_returns_canonical_unavailable_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root, target_policy=policy())))
            task = create_external_task(config)
            task["execution_backend"] = "unknown-backend"

            plan = build_gateway_neutral_execution_plan(config, task)

            self.assertEqual("unavailable", plan["availability"]["status"])
            self.assertEqual(
                "legacy_unavailable",
                plan["execution"]["backend"],
            )
            self.assertIn(
                "legacy_backend_projection_unavailable",
                plan["availability"]["reason_codes"],
            )

    def test_validator_rejects_digest_tamper_and_private_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root, target_policy=policy())))
            plan = build_gateway_neutral_execution_plan(
                config,
                create_external_task(config),
            )

            tampered = copy.deepcopy(plan)
            tampered["binding"]["target_id"] = "other-target"
            with self.assertRaisesRegex(
                GatewayNeutralExecutionPlanError,
                "plan_digest",
            ):
                validate_gateway_neutral_execution_plan(tampered)

            private = copy.deepcopy(plan)
            private["policy"]["prompt"] = "private"
            with self.assertRaisesRegex(
                GatewayNeutralExecutionPlanError,
                "execution policy",
            ):
                validate_gateway_neutral_execution_plan(private)

            non_hex = copy.deepcopy(plan)
            non_hex["provenance"]["resolved_target_digest"] = "sha256:" + "z" * 64
            non_hex.pop("plan_digest")
            non_hex["plan_digest"] = "sha256:" + hashlib.sha256(
                json.dumps(
                    non_hex,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest()
            with self.assertRaisesRegex(
                GatewayNeutralExecutionPlanError,
                "sha256",
            ):
                validate_gateway_neutral_execution_plan(non_hex)

    def test_cli_does_not_launch_or_mutate_queue_config_events_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root, target_policy=policy())
            config = Config.load(str(config_path))
            task = create_external_task(config)
            observed_paths = [
                config_path,
                config.queue_dir / f"{task['id']}.json",
                *sorted(config.event_dir.glob("**/*")),
            ]
            before = {
                path: path.read_bytes()
                for path in observed_paths
                if path.is_file()
            }
            stdout = io.StringIO()

            with (
                patch("subprocess.run") as run,
                patch("subprocess.Popen") as popen,
                contextlib.redirect_stdout(stdout),
            ):
                code = main(
                    [
                        "--config",
                        str(config_path),
                        "execution-plan",
                        task["id"],
                    ]
                )

            self.assertEqual(0, code)
            self.assertFalse(run.called)
            self.assertFalse(popen.called)
            self.assertEqual(
                before,
                {
                    path: path.read_bytes()
                    for path in observed_paths
                    if path.is_file()
                },
            )
            self.assertFalse(config.state_file.exists())
            output = json.loads(stdout.getvalue())
            self.assertEqual("gateway-neutral-execution-plan-v1", output["contract"])
            self.assertFalse(output["mutation"]["allowed"])
            self.assertFalse(output["mutation"]["applied"])


if __name__ == "__main__":
    unittest.main()

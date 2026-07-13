from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.config import Config
from codex_batch_runner.execution_target_selector import TargetSelectionError, select_execution_target, target_value
from codex_batch_runner.model_requirements import resolve_execution_config
from codex_batch_runner.worker_routing import resolve_worker_target


def requirement(*, floor: int = 500, constraints: dict | None = None, latency: int = 0, cost: int = 0) -> dict:
    axes = {
        name: {"score": floor, "confidence": 1000, "anchor": floor, "evidence_codes": []}
        for name in (
            "semantic_reasoning",
            "context_integration",
            "planning_depth",
            "instruction_fidelity",
            "tool_execution_reliability",
            "adversarial_detection",
        )
    }
    return {
        "schema_version": 2,
        "derivation_version": "requirement-rubric-v1",
        "revision_id": "reqrev-test",
        "quality_requirements": axes,
        "hard_constraints": constraints or {},
        "utility_preferences": {"latency_weight": latency, "cost_weight": cost},
    }


def codex_target(model: str, *, quality: int = 750, latency: int = 500, cost: int = 500) -> dict:
    return {
        "execution_surface": "codex",
        "model": model,
        "reasoning_effort": "high",
        "trust_state": "trusted",
        "static_fitness": {
            axis: quality
            for axis in (
                "semantic_reasoning", "context_integration", "planning_depth", "instruction_fidelity",
                "tool_execution_reliability", "adversarial_detection",
            )
        },
        "latency_score": latency,
        "cost_score": cost,
        "capabilities": {"required_tools": ["filesystem", "shell"], "minimum_context_tokens": 200000},
        "capability_evidence": {
            "required_tools": {"source": "operator_verified", "expires_at": "2099-01-01T00:00:00Z"},
            "minimum_context_tokens": {"source": "provider_declared"},
            "required_execution_surfaces": {"source": "surface_reported"},
            "allowed_reasoning_efforts": {"source": "surface_reported"},
        },
    }


def loaded_config(targets: dict, *, policies: dict | None = None) -> Config:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "config.json"
        constraints = policies or {
            "required_tools": {"unknown_policy": "reject"},
            "minimum_context_tokens": {"unknown_policy": "reject"},
            "required_execution_surfaces": {"unknown_policy": "reject"},
            "allowed_reasoning_efforts": {"unknown_policy": "reject"},
        }
        path.write_text(
            json.dumps(
                {
                    "execution_target_inventory": {
                        "schema_version": 1,
                        "snapshot_id": "sha256:test-snapshot",
                        "status": "current",
                        "constraint_registry_version": "constraints-v1",
                        "targets": targets,
                    },
                    "constraint_registry": {
                        "schema_version": 1,
                        "version": "constraints-v1",
                        "constraints": constraints,
                    },
                }
            ),
            encoding="utf-8",
        )
        return Config.load(str(path), root=root)


class ExecutionTargetSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict("os.environ", {"CBR_CONFIG": ""}, clear=False)
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

    def test_automatic_codex_target_is_exact_and_command_ready(self) -> None:
        config = loaded_config({"codex-high-v1": codex_target("gpt-exact")})

        selected = resolve_execution_config(config, {"model_requirement_vector": requirement()})

        self.assertEqual("codex-high-v1", selected.execution_target)
        self.assertEqual("gpt-exact", selected.model)
        self.assertEqual("high", selected.config_overrides["model_reasoning_effort"])
        self.assertEqual("automatic_static_non_learned", selected.selection_reason)

    def test_inventory_rejects_cli_default_automatic_codex_target(self) -> None:
        target = codex_target("gpt-exact")
        target.pop("model")
        target["model_source"] = "cli_default"

        with self.assertRaisesRegex(ValueError, "requires exact model and reasoning_effort"):
            loaded_config({"bad": target})

    def test_hard_constraint_unknown_rejects_target(self) -> None:
        target = codex_target("gpt-exact")
        target["capability_evidence"].pop("required_tools")
        config = loaded_config({"unknown-tools": target})

        with self.assertRaisesRegex(TargetSelectionError, "no_eligible_model"):
            select_execution_target(
                config,
                {},
                requirement(constraints={"required_tools": ["filesystem"]}),
            )

    def test_execution_surface_matches_any_allowed_option(self) -> None:
        config = loaded_config({"codex-high-v1": codex_target("gpt-exact")})

        selected = select_execution_target(
            config,
            {},
            requirement(constraints={"required_execution_surfaces": ["external", "codex"]}),
        )

        self.assertEqual("codex-high-v1", selected.target_id)

    def test_reasoning_effort_matches_any_allowed_option(self) -> None:
        config = loaded_config({"codex-high-v1": codex_target("gpt-exact")})

        selected = select_execution_target(
            config,
            {},
            requirement(constraints={"allowed_reasoning_efforts": ["medium", "high"]}),
        )

        self.assertEqual("codex-high-v1", selected.target_id)

    def test_required_tools_remain_required_subset(self) -> None:
        config = loaded_config({"codex-high-v1": codex_target("gpt-exact")})

        selected = select_execution_target(
            config,
            {},
            requirement(constraints={"required_tools": ["filesystem", "shell"]}),
        )

        self.assertEqual("codex-high-v1", selected.target_id)

    def test_quality_floor_is_not_relaxed_by_cost_preference(self) -> None:
        cheap = codex_target("cheap", quality=250, cost=1000)
        capable = codex_target("capable", quality=750, cost=0)
        config = loaded_config({"cheap": cheap, "capable": capable})

        selected = select_execution_target(config, {}, requirement(floor=750, cost=1000))

        self.assertEqual("capable", selected.target_id)

    def test_insufficient_quality_boundary_does_not_invent_posterior(self) -> None:
        target = codex_target("unknown-quality")
        target["quality_evidence_status"] = "insufficient"
        config = loaded_config({"unknown-quality": target})

        with self.assertRaisesRegex(TargetSelectionError, "insufficient_quality_evidence"):
            select_execution_target(config, {}, requirement())

    def test_deterministic_tie_break_uses_target_id(self) -> None:
        config = loaded_config({"z-target": codex_target("z"), "a-target": codex_target("a")})

        selected = select_execution_target(config, {}, requirement())

        self.assertEqual("a-target", selected.target_id)

    def test_pin_fails_closed_when_target_is_below_floor(self) -> None:
        config = loaded_config({"low": codex_target("low", quality=250), "high": codex_target("high")})
        task = {
            "routing_override": {
                "mode": "pin",
                "target_id": "low",
                "reason": "test",
                "scope": "single_task",
                "allow_fallback": False,
                "provenance": "operator_override",
            }
        }

        with self.assertRaisesRegex(TargetSelectionError, "manual_pin_unavailable"):
            select_execution_target(config, task, requirement(floor=500))

    def test_preference_fallback_reuses_normal_eligibility_and_ranking(self) -> None:
        config = loaded_config({"a": codex_target("a", cost=100), "b": codex_target("b", cost=900)})
        task = {
            "routing_override": {
                "mode": "preference",
                "target_id": "missing",
                "reason": "test",
                "scope": "single_task",
                "allow_fallback": True,
                "provenance": "operator_override",
            }
        }

        selected = select_execution_target(config, task, requirement(cost=1000))

        self.assertEqual("b", selected.target_id)
        self.assertEqual("operator_preference_fallback", selected.selection_reason)

    def test_unified_inventory_rejects_non_exact_external_worker(self) -> None:
        target = {
            "execution_surface": "external",
            "execution_backend": "external-json-command",
            "external_command": ["public-worker"],
            "capacity_pool": "codex",
            "trust_state": "trusted",
            "static_fitness": {
                "semantic_reasoning": 750,
                "context_integration": 750,
                "planning_depth": 750,
                "instruction_fidelity": 750,
                "tool_execution_reliability": 750,
                "adversarial_detection": 750,
            },
            "latency_score": 500,
            "cost_score": 500,
            "capabilities": {},
            "capability_evidence": {},
        }
        with self.assertRaisesRegex(ValueError, "automatic external target requires model, command_model, and reasoning_effort"):
            loaded_config({"external-v1": target})

    def test_direct_target_parser_rejects_partial_identity_and_shell(self) -> None:
        target = {
            "execution_surface": "external", "execution_backend": "external-json-command",
            "external_command": ["worker", "{model}", "{reasoning_effort}"],
            "model": "exact", "trust_state": "trusted",
            "static_fitness": {axis: 750 for axis in requirement()["quality_requirements"]},
            "latency_score": 500, "cost_score": 500, "capabilities": {}, "capability_evidence": {},
        }
        with self.assertRaisesRegex(ValueError, "automatic external target requires"):
            target_value("execution_target_inventory.targets.partial", "partial", target)

        shell = dict(target)
        shell.update({"execution_backend": "shell", "shell_command": ["true"]})
        shell.pop("external_command")
        with self.assertRaisesRegex(ValueError, "shell is not an automatic model target"):
            target_value("execution_target_inventory.targets.shell", "shell", shell)

    def test_selector_revalidates_mutated_inventory_before_selection(self) -> None:
        config = loaded_config({"exact": {
            "execution_surface": "external", "execution_backend": "external-json-command",
            "external_command": ["worker", "{model}", "{reasoning_effort}"],
            "model": "exact", "command_model": "exact", "reasoning_effort": "high",
            "trust_state": "trusted",
            "static_fitness": {axis: 750 for axis in requirement()["quality_requirements"]},
            "latency_score": 500, "cost_score": 500, "capabilities": {}, "capability_evidence": {},
        }})
        config.execution_target_inventory["targets"]["exact"].pop("command_model")

        with self.assertRaisesRegex(ValueError, "automatic external target requires"):
            select_execution_target(config, {}, requirement())

    def test_exact_external_target_requires_identity_bound_command_placeholders(self) -> None:
        target = {
            "execution_surface": "external",
            "execution_backend": "external-json-command",
            "external_command": ["public-worker", "{model}", "{reasoning_effort}"],
            "model": "external-exact-model",
            "command_model": "external-exact-model",
            "reasoning_effort": "high",
            "trust_state": "trusted",
            "static_fitness": {axis: 750 for axis in requirement()["quality_requirements"]},
            "latency_score": 500,
            "cost_score": 500,
            "capabilities": {},
            "capability_evidence": {},
        }
        selected = resolve_worker_target(
            loaded_config({"external-exact-v1": target}),
            {"model_requirement_vector": requirement()},
        )

        self.assertEqual("external-exact-model", selected.target["model"])
        broken = dict(target)
        broken["external_command"] = ["public-worker"]
        with self.assertRaisesRegex(ValueError, "requires .* argv placeholders"):
            loaded_config({"broken": broken})
        duplicate = dict(target)
        duplicate["external_command"] = [
            "public-worker", "{model}", "{model}", "{reasoning_effort}",
        ]
        with self.assertRaisesRegex(ValueError, "exactly one"):
            loaded_config({"duplicate": duplicate})
        mismatch = dict(target)
        mismatch["command_model"] = "other-model"
        with self.assertRaisesRegex(ValueError, "model == command_model"):
            loaded_config({"mismatch": mismatch})

    def test_native_inventory_preempts_both_legacy_first_match_paths(self) -> None:
        config = loaded_config({"exact-codex": codex_target("exact")})
        config = replace(
            config,
            model_selection_rules=[{"name": "legacy-model", "when": {}, "model": "legacy"}],
            worker_targets={
                "legacy-worker": {
                    "execution_backend": "external-json-command",
                    "external_command": ["legacy-worker"],
                }
            },
            worker_selection_rules=[{"name": "legacy-worker-rule", "when": {}, "worker_target": "legacy-worker"}],
        )
        task = {"model_requirement_vector": requirement()}

        self.assertIsNone(resolve_worker_target(config, task))
        selected = resolve_execution_config(config, task)
        self.assertEqual("exact-codex", selected.execution_target)
        self.assertEqual("exact", selected.model)

    def test_legacy_v1_config_remains_readable_and_does_not_enter_exact_selector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            resolved = resolve_execution_config(config, {"model_requirement_vector": {"dimensions": {"reasoning_depth": "low"}}})

        self.assertEqual("cli_default", resolved.model_source)
        self.assertIsNone(resolved.execution_target)


if __name__ == "__main__":
    unittest.main()

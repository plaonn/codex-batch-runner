from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.provider_resource_authority import (
    resource_gate_decision_key,
    resource_gate_key,
    resource_gate_wake_key,
)
from codex_batch_runner.provider_resource_simulator import (
    build_provider_resource_simulation,
    render_provider_resource_simulation,
    validate_simulation_request,
)

NOW = datetime(2030, 1, 2, 4, 0, tzinfo=timezone.utc)
AXES = (
    "semantic_reasoning",
    "context_integration",
    "planning_depth",
    "instruction_fidelity",
    "tool_execution_reliability",
    "adversarial_detection",
)


def target(model: str, *, quality: int = 750, tools: list[str] | None = None) -> dict:
    return {
        "execution_surface": "codex",
        "model": model,
        "reasoning_effort": "high",
        "trust_state": "trusted",
        "static_fitness": {axis: quality for axis in AXES},
        "latency_score": 500,
        "cost_score": 500,
        "capabilities": {"required_tools": tools if tools is not None else ["shell"]},
        "capability_evidence": {
            "required_tools": {"source": "provider_declared"},
        },
    }


def config_document(root: Path) -> dict:
    return {
        "root": str(root),
        "execution_target_inventory": {
            "schema_version": 1,
            "snapshot_id": "inventory-sim-r1",
            "status": "current",
            "constraint_registry_version": "constraints-sim-r1",
            "targets": {
                "target-a": target("model-a"),
                "target-b": target("model-b"),
                "target-low": target("model-low", quality=250),
                "target-no-tool": target("model-no-tool", tools=[]),
            },
        },
        "constraint_registry": {
            "schema_version": 1,
            "version": "constraints-sim-r1",
            "constraints": {
                "required_tools": {"unknown_policy": "reject"},
            },
        },
    }


def load_config(root: Path) -> Config:
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config_document(root)), encoding="utf-8")
    return Config.load(str(config_path), root=root)


def requirement() -> dict:
    return {
        "schema_version": 2,
        "derivation_version": "requirement-rubric-v1",
        "revision_id": "requirement-sim-r1",
        "quality_requirements": {
            axis: {
                "score": 500,
                "confidence": 1000,
                "anchor": 500,
                "evidence_codes": [],
            }
            for axis in AXES
        },
        "hard_constraints": {"required_tools": ["shell"]},
        "utility_preferences": {"latency_weight": 500, "cost_weight": 500},
    }


def request(*, global_status: str = "allowed", global_reset_at: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "contract": "provider-resource-simulation-request-v1",
        "selected_target_id": "target-a",
        "requirement": requirement(),
        "global_gate": {
            "status": global_status,
            "reason": "synthetic-global-input",
            "reset_at": global_reset_at,
        },
    }


def scope(target_id: str) -> dict:
    return {
        "scope_id": f"scope-{target_id}",
        "scope_revision": "scope-r1",
        "host_instance_id": "host-example",
        "codex_home_instance_id": "home-example",
        "source_surface": "cli",
        "credential_context_id": f"context-{target_id}",
    }


def mapping() -> dict:
    return {
        "schema_version": 2,
        "contract": "provider-resource-mapping-v2",
        "mapping_revision": "mapping-sim-r1",
        "target_inventory_snapshot_id": "inventory-sim-r1",
        "status": "current",
        "bindings": [
            {
                "binding_id": f"binding-{target_id}",
                "target_id": target_id,
                "capacity_pool": "codex",
                "provider_id": "provider-example",
                "quota_identity_id": f"quota-{target_id}",
                "identity_authority": "source_attested",
                "observation_scope": scope(target_id),
                "producer": {
                    "adapter_id": f"adapter-{target_id}",
                    "adapter_revision": "adapter-r1",
                },
                "verified_at": "2030-01-01T00:00:00Z",
                "expires_at": "2030-02-01T00:00:00Z",
                "status": "current",
                "invalidation_reason": None,
                "supersedes_binding_id": None,
            }
            for target_id in (
                "target-a",
                "target-b",
                "target-low",
                "target-no-tool",
            )
        ],
    }


def policy() -> dict:
    return {
        "schema_version": 1,
        "contract": "provider-resource-admission-policy-v1",
        "policy_revision": "policy-sim-r1",
        "status": "current",
        "enabled": True,
        "identity_authority": "source_attested",
        "allowed_mapping_revisions": ["mapping-sim-r1"],
        "target_rules": [
            {
                "target_id": target_id,
                "provider_id": "provider-example",
                "window_rules": [
                    {
                        "window_id": "primary",
                        "remaining_unit": "percent",
                        "gate_at_or_below": 5,
                    }
                ],
            }
            for target_id in (
                "target-a",
                "target-b",
                "target-low",
                "target-no-tool",
            )
        ],
        "accepted_timestamp_provenance": ["client_event_at"],
        "timing": {
            "max_age_seconds": 300,
            "allowed_clock_skew_seconds": 60,
            "reset_grace_seconds": 30,
        },
        "unknown_behavior": {
            "missing": "allow_existing_execution",
            "stale": "allow_existing_execution",
            "invalid": "allow_existing_execution",
        },
        "global_gate_interaction": {
            "evaluation_order": "global_first",
            "when_global_gated": "skip_target_evaluation",
            "same_reset": "covered_by_global_no_duplicate_wake",
        },
        "rollback": {
            "disable_behavior": "stop_new_target_decisions",
            "typed_state_behavior": "preserve_append_only_evidence",
            "legacy_scalar_behavior": "remain_global_only",
        },
    }


def snapshot(
    target_id: str,
    *,
    remaining: float,
    observed_at: str = "2030-01-02T03:59:00Z",
    reset_at: str = "2030-01-02T05:00:00Z",
) -> dict:
    return {
        "schema_version": 1,
        "contract": "provider-resource-snapshot-v1",
        "snapshot_id": f"snapshot-{target_id}",
        "generated_at": "2030-01-02T04:00:00Z",
        "producer": {
            "adapter_id": f"adapter-{target_id}",
            "adapter_version": "adapter-r1",
            "observation_mode": "cached_local",
            "read_only": True,
        },
        "resource": {
            "provider_id": "provider-example",
            "quota_identity": {
                "status": "verified",
                "id": f"quota-{target_id}",
                "source": "source_attested",
                "confidence": "verified",
            },
            "observation_scope": scope(target_id),
        },
        "windows": [
            {
                "window_id": "primary",
                "window_duration_seconds": 18000,
                "availability": "observed",
                "remaining": {
                    "status": "observed",
                    "value": remaining,
                    "unit": "percent",
                    "derivation": "provider_reported",
                },
                "resets_at": {"status": "observed", "value": reset_at},
                "observed_at": observed_at,
                "freshness": {
                    "status": "unknown",
                    "evaluated_at": "2030-01-02T04:00:00Z",
                    "max_age_seconds": None,
                    "expires_at": None,
                    "reason": "freshness_policy_unset",
                },
                "source": {
                    "kind": "local_cached_event",
                    "field": "rate_limits.primary",
                    "confidence": "experimental_observed_shape",
                    "timestamp_provenance": "client_event_at",
                },
            }
        ],
        "diagnostics": [],
    }


def all_snapshots() -> list[dict]:
    return [
        snapshot("target-a", remaining=5),
        snapshot("target-b", remaining=50),
        snapshot("target-low", remaining=50),
        snapshot("target-no-tool", remaining=50),
    ]


class ProviderResourceSimulatorTests(unittest.TestCase):
    def test_request_example_is_versioned_exact_and_public_safe(self) -> None:
        example = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "examples/provider-resource-simulation-request-v1.example.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            validate_simulation_request(example)["contract"],
            "provider-resource-simulation-request-v1",
        )

    def test_selected_defer_and_alternative_allow_reapply_selector_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp))
            report = build_provider_resource_simulation(
                config,
                request=request(),
                snapshots=all_snapshots(),
                mapping=mapping(),
                policy=policy(),
                evaluated_at=NOW,
            )

        selected = report["selected_target"]
        self.assertEqual(selected["recommendation"]["action"], "defer")
        decision = selected["recommendation"]["decision_previews"][0]
        self.assertEqual(
            decision["resource_key"],
            resource_gate_key(
                "provider-example",
                "quota-target-a",
                "scope-target-a",
                "primary",
            ),
        )
        self.assertEqual(decision["decision_key"], resource_gate_decision_key(decision))
        self.assertEqual(
            decision["wake_key"],
            resource_gate_wake_key(
                "provider-example",
                "quota-target-a",
                "scope-target-a",
                "primary",
                "2030-01-02T05:00:00Z",
            ),
        )
        self.assertEqual(decision["wake_at"], "2030-01-02T05:00:30+00:00")
        self.assertFalse(decision["wake_scheduled"])
        self.assertEqual(
            [
                (row["target_id"], row["recommendation"]["action"])
                for row in report["alternative_recommendations"]
            ],
            [("target-b", "allow")],
        )
        excluded = {row["target_id"]: row for row in report["excluded_targets"]}
        self.assertIn("below_quality_floor:semantic_reasoning", excluded["target-low"]["excluded_reasons"])
        self.assertIn("hard_constraint_not_satisfied:required_tools", excluded["target-no-tool"]["excluded_reasons"])
        self.assertEqual(report["decision_impact"]["runtime_mutations"], [])
        self.assertEqual(report["summary"]["runtime_mutation_count"], 0)

    def test_global_terminal_precedence_uses_covered_by_global_without_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = build_provider_resource_simulation(
                load_config(Path(tmp)),
                request=request(
                    global_status="gated",
                    global_reset_at="2030-01-02T05:00:00Z",
                ),
                snapshots=all_snapshots(),
                mapping=mapping(),
                policy=policy(),
                evaluated_at=NOW,
            )

        selected = report["selected_target"]["recommendation"]
        self.assertEqual(selected["action"], "covered_by_global")
        decision = selected["decision_previews"][0]
        self.assertEqual(decision["global_coverage"]["status"], "covered")
        self.assertTrue(decision["deduplicated_by_global"])
        self.assertIsNone(decision["wake_at"])
        self.assertFalse(decision["wake_scheduled"])
        self.assertEqual(
            report["alternative_recommendations"][0]["recommendation"]["action"],
            "evidence_only",
        )
        self.assertEqual(report["summary"]["defer_preview_count"], 0)

    def test_unknown_global_gate_fails_open_and_never_defers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = build_provider_resource_simulation(
                load_config(Path(tmp)),
                request=request(global_status="unknown"),
                snapshots=all_snapshots(),
                mapping=mapping(),
                policy=policy(),
                evaluated_at=NOW,
            )

        selected = report["selected_target"]["recommendation"]
        self.assertEqual(selected["action"], "evidence_only")
        self.assertEqual(selected["reason_codes"], ["global_gate_unknown"])
        self.assertTrue(selected["preserve_existing_execution"])
        self.assertTrue(
            all(
                row["recommendation"]["action"] != "defer"
                for row in [
                    report["selected_target"],
                    *report["alternative_recommendations"],
                ]
            )
        )

    def test_stale_missing_and_ambiguous_resource_evidence_never_defer(self) -> None:
        cases: list[tuple[str, list[dict], dict]] = []
        stale = all_snapshots()
        stale[0] = snapshot(
            "target-a",
            remaining=0,
            observed_at="2030-01-02T03:00:00Z",
        )
        cases.append(("stale", stale, mapping()))
        missing_mapping = mapping()
        missing_mapping["bindings"] = [
            binding
            for binding in missing_mapping["bindings"]
            if binding["target_id"] != "target-a"
        ]
        cases.append(("missing", all_snapshots(), missing_mapping))
        ambiguous = all_snapshots()
        duplicate = snapshot("target-a", remaining=0)
        duplicate["snapshot_id"] = "snapshot-target-a-duplicate"
        ambiguous.append(duplicate)
        cases.append(("ambiguous", ambiguous, mapping()))
        unknown_identity = all_snapshots()
        unknown_identity[0]["resource"]["quota_identity"] = {
            "status": "unknown",
            "id": None,
            "source": "source_reported_opaque_id",
            "confidence": "unverified",
        }
        cases.append(("unknown_identity", unknown_identity, mapping()))
        unavailable_identity = all_snapshots()
        unavailable_identity[0]["resource"]["quota_identity"] = {
            "status": "unavailable",
            "id": None,
            "source": "unavailable",
            "confidence": "unavailable",
        }
        cases.append(("unavailable_identity", unavailable_identity, mapping()))
        unknown_remaining = all_snapshots()
        unknown_remaining[0]["windows"][0]["remaining"] = {
            "status": "unknown",
            "value": None,
            "unit": None,
            "derivation": "unavailable",
        }
        cases.append(("unknown_remaining", unknown_remaining, mapping()))
        future_generated = all_snapshots()
        future_generated[0]["generated_at"] = "2030-01-02T04:10:00Z"
        cases.append(("future_generated", future_generated, mapping()))

        for name, snapshots, mapping_value in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                report = build_provider_resource_simulation(
                    load_config(Path(tmp)),
                    request=request(),
                    snapshots=snapshots,
                    mapping=mapping_value,
                    policy=policy(),
                    evaluated_at=NOW,
                )
                selected = report["selected_target"]["recommendation"]
                self.assertEqual(selected["action"], "evidence_only")
                self.assertTrue(selected["preserve_existing_execution"])
                self.assertNotEqual(selected["action"], "defer")
                self.assertEqual(selected["decision_previews"], [])

    def test_cli_json_and_human_render_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            load_config(root)
            inputs = {
                "request.json": request(),
                "mapping.json": mapping(),
                "policy.json": policy(),
            }
            for index, value in enumerate(all_snapshots()):
                inputs[f"snapshot-{index}.json"] = value
            for name, value in inputs.items():
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            argv = [
                "--config",
                str(root / "config.json"),
                "provider-resource-simulate",
                "--request-json",
                str(root / "request.json"),
                "--mapping-json",
                str(root / "mapping.json"),
                "--policy-json",
                str(root / "policy.json"),
                "--evaluated-at",
                NOW.isoformat(),
            ]
            for index in range(4):
                argv.extend(["--snapshot-json", str(root / f"snapshot-{index}.json")])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main([*argv, "--json"]), 0)
            parsed = json.loads(output.getvalue())
            self.assertEqual(parsed["contract"], "provider-resource-simulation-report-v1")
            human = io.StringIO()
            with contextlib.redirect_stdout(human):
                self.assertEqual(main(argv), 0)
            self.assertIn("runtime_mutations=0", human.getvalue())
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertFalse((root / ".codex-batch-runner").exists())

    def test_human_renderer_names_all_safety_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = build_provider_resource_simulation(
                load_config(Path(tmp)),
                request=request(),
                snapshots=all_snapshots(),
                mapping=mapping(),
                policy=policy(),
                evaluated_at=NOW,
            )
        rendered = render_provider_resource_simulation(report)
        self.assertIn("read_only: yes", rendered)
        self.assertIn("automatic_substitution: no", rendered)
        self.assertIn("D2-B activation: no", rendered)
        self.assertIn("wake_scheduled=no", rendered)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.provider_resource_authority import (
    GATE_DECISION_CONTRACT,
    GATE_STATE_CONTRACT,
    build_authority_preview,
    deduplicate_gate_decisions,
    global_gate_coverage,
    resource_gate_decision_key,
    resource_gate_key,
    resource_gate_wake_key,
    validate_admission_policy,
    validate_gate_decision,
    validate_gate_state,
    validate_mapping_v2,
)
from codex_batch_runner.provider_resource_report import (
    ProviderResourceValidationError,
    build_provider_resource_report,
    evaluate_snapshot_freshness,
    project_native_codex_cached_rollout,
    validate_snapshot,
)

NOW = datetime(2030, 1, 2, 4, 0, tzinfo=timezone.utc)


def scope() -> dict:
    return {
        "scope_id": "scope-a",
        "scope_revision": "scope-r1",
        "host_instance_id": "host-a",
        "codex_home_instance_id": "home-a",
        "source_surface": "cli",
        "credential_context_id": "credential-context-a",
    }


def mapping_v2(*, authority: str = "source_attested", status: str = "current") -> dict:
    return {
        "schema_version": 2,
        "contract": "provider-resource-mapping-v2",
        "mapping_revision": "mapping-r2",
        "target_inventory_snapshot_id": "inventory-a",
        "status": "current",
        "bindings": [
            {
                "binding_id": "binding-a",
                "target_id": "target-a",
                "capacity_pool": "codex",
                "provider_id": "provider-a",
                "quota_identity_id": "quota-a",
                "identity_authority": authority,
                "observation_scope": scope(),
                "producer": {"adapter_id": "adapter-a", "adapter_revision": "adapter-r2"},
                "verified_at": "2030-01-01T00:00:00Z",
                "expires_at": "2030-02-01T00:00:00Z",
                "status": status,
                "invalidation_reason": None if status == "current" else "operator_invalidated",
                "supersedes_binding_id": None,
            }
        ],
    }


def policy(*, enabled: bool = True, authority: str = "source_attested") -> dict:
    return {
        "schema_version": 1,
        "contract": "provider-resource-admission-policy-v1",
        "policy_revision": "policy-r1",
        "status": "current",
        "enabled": enabled,
        "identity_authority": authority,
        "allowed_mapping_revisions": ["mapping-r2"],
        "target_rules": [
            {
                "target_id": "target-a",
                "provider_id": "provider-a",
                "window_rules": [
                    {"window_id": "primary", "remaining_unit": "percent", "gate_at_or_below": 5}
                ],
            }
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


def authoritative_snapshot(*, provenance: str = "client_event_at") -> dict:
    return {
        "schema_version": 1,
        "contract": "provider-resource-snapshot-v1",
        "snapshot_id": "snapshot-authoritative",
        "generated_at": "2030-01-02T04:00:00Z",
        "producer": {
            "adapter_id": "adapter-a",
            "adapter_version": "adapter-r2",
            "observation_mode": "cached_local",
            "read_only": True,
        },
        "resource": {
            "provider_id": "provider-a",
            "quota_identity": {
                "status": "verified",
                "id": "quota-a",
                "source": "source_attested",
                "confidence": "verified",
            },
            "observation_scope": scope(),
        },
        "windows": [
            {
                "window_id": "primary",
                "window_duration_seconds": 18000,
                "availability": "observed",
                "remaining": {
                    "status": "observed",
                    "value": 42,
                    "unit": "percent",
                    "derivation": "provider_reported",
                },
                "resets_at": {"status": "observed", "value": "2030-01-02T05:00:00Z"},
                "observed_at": "2030-01-02T03:59:00Z",
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
                    "timestamp_provenance": provenance,
                },
            }
        ],
        "diagnostics": [],
    }


def inventory() -> dict:
    return {"status": "current", "snapshot_id": "inventory-a", "targets": {"target-a": {}}}


def config_inventory() -> dict:
    axes = (
        "semantic_reasoning",
        "context_integration",
        "planning_depth",
        "instruction_fidelity",
        "tool_execution_reliability",
        "adversarial_detection",
    )
    return {
        "schema_version": 1,
        "snapshot_id": "inventory-a",
        "status": "current",
        "constraint_registry_version": "constraints-r1",
        "targets": {
            "target-a": {
                "execution_surface": "codex",
                "model": "model-a",
                "reasoning_effort": "medium",
                "trust_state": "trusted",
                "static_fitness": {axis: 500 for axis in axes},
                "latency_score": 500,
                "cost_score": 500,
                "capabilities": {},
                "capability_evidence": {},
            }
        },
    }


def gate_decision(*, action: str = "defer", global_status: str = "not_covered") -> dict:
    value = {
        "schema_version": 1,
        "contract": GATE_DECISION_CONTRACT,
        "decision_key": "placeholder",
        "resource_key": resource_gate_key("provider-a", "quota-a", "scope-a", "primary"),
        "wake_key": resource_gate_wake_key(
            "provider-a",
            "quota-a",
            "scope-a",
            "primary",
            "2030-01-02T05:00:00Z",
        ),
        "policy_revision": "policy-r1",
        "mapping_revision": "mapping-r2",
        "provider_id": "provider-a",
        "quota_identity_id": "quota-a",
        "scope_id": "scope-a",
        "window_id": "primary",
        "observed_at": "2030-01-02T03:59:00Z",
        "reset_at": "2030-01-02T05:00:00Z",
        "action": action,
        "global_coverage": {
            "status": global_status,
            "global_reset_at": "2030-01-02T05:00:00Z" if global_status == "covered" else None,
        },
        "supersedes_decision_key": None,
    }
    value["decision_key"] = resource_gate_decision_key(value)
    return value


class ProviderResourceAuthorityContractTests(unittest.TestCase):
    def test_public_examples_validate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        validate_mapping_v2(json.loads((root / "examples/provider-resource-mapping-v2.example.json").read_text()))
        validate_admission_policy(
            json.loads((root / "examples/provider-resource-admission-policy-v1.example.json").read_text())
        )

    def test_mapping_v2_requires_scope_producer_expiry_and_invalidation_shape(self) -> None:
        self.assertEqual(validate_mapping_v2(mapping_v2())["mapping_revision"], "mapping-r2")
        invalidated = mapping_v2(status="invalidated")
        self.assertEqual(validate_mapping_v2(invalidated)["bindings"][0]["status"], "invalidated")
        missing_scope = mapping_v2()
        del missing_scope["bindings"][0]["observation_scope"]["credential_context_id"]
        with self.assertRaisesRegex(ProviderResourceValidationError, "fields are invalid"):
            validate_mapping_v2(missing_scope)
        expired_before_verified = mapping_v2()
        expired_before_verified["bindings"][0]["expires_at"] = "2029-01-01T00:00:00Z"
        with self.assertRaisesRegex(ProviderResourceValidationError, "expiry"):
            validate_mapping_v2(expired_before_verified)

    def test_policy_is_explicit_versioned_and_strict_source_attested_only(self) -> None:
        self.assertTrue(validate_admission_policy(policy())["enabled"])
        with self.assertRaisesRegex(ProviderResourceValidationError, "not enabled"):
            validate_admission_policy(policy(authority="operator_attested_single_context"))
        bad = policy()
        bad["timing"]["max_age_seconds"] = True
        with self.assertRaisesRegex(ProviderResourceValidationError, "non-negative integer"):
            validate_admission_policy(bad)

    def test_authority_preview_binds_exact_inventory_snapshot(self) -> None:
        snapshot = evaluate_snapshot_freshness(
            validate_snapshot(authoritative_snapshot()),
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        changed_inventory = inventory()
        changed_inventory["snapshot_id"] = "inventory-b"
        preview = build_authority_preview(
            snapshots=[snapshot],
            mapping=mapping_v2(),
            policy=policy(),
            inventory=changed_inventory,
            evaluated_at=NOW,
        )
        self.assertIn("mapping_stale", preview["targets"][0]["reasons"])

    def test_authority_preview_accepts_only_attested_scope_producer_and_event_time(self) -> None:
        snapshot = evaluate_snapshot_freshness(
            validate_snapshot(authoritative_snapshot()),
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        preview = build_authority_preview(
            snapshots=[snapshot],
            mapping=mapping_v2(),
            policy=policy(),
            inventory=inventory(),
            evaluated_at=NOW,
        )
        self.assertTrue(preview["targets"][0]["eligible"])
        self.assertFalse(preview["scheduling_authoritative"])

        for mutation, reason in (
            (lambda value: value["windows"][0]["source"].update(timestamp_provenance="source_file_mtime"), "timestamp_provenance_rejected"),
            (lambda value: value["windows"][0]["source"].update(timestamp_provenance="generated_at"), "timestamp_provenance_rejected"),
            (lambda value: value["resource"]["observation_scope"].update(scope_revision="scope-r0"), "snapshot_scope_mismatch"),
            (lambda value: value["producer"].update(adapter_version="adapter-r1"), "snapshot_producer_mismatch"),
            (lambda value: value["resource"]["quota_identity"].update(source="operator_verified"), "snapshot_identity_unverified"),
        ):
            value = authoritative_snapshot()
            mutation(value)
            evaluated = evaluate_snapshot_freshness(validate_snapshot(value), evaluated_at=NOW, max_age_seconds=300)
            rejected = build_authority_preview(
                snapshots=[evaluated],
                mapping=mapping_v2(),
                policy=policy(),
                inventory=inventory(),
                evaluated_at=NOW,
            )
            with self.subTest(reason=reason):
                self.assertIn(reason, rejected["targets"][0]["reasons"])

    def test_report_exposes_preview_without_becoming_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(Config.load(root=Path(tmp)), execution_target_inventory=inventory())
            report = build_provider_resource_report(
                config,
                snapshots=[authoritative_snapshot()],
                mapping=mapping_v2(),
                policy=policy(),
                evaluated_at=NOW,
                max_age_seconds=None,
            )
            self.assertTrue(report["authority_preview"]["targets"][0]["eligible"])
            self.assertEqual(report["contract"], "provider-resource-report-v2")
            self.assertFalse(report["scheduling_authoritative"])
            self.assertEqual(report["mapping_preview"]["targets"], [])
            self.assertEqual(report["provider_resources"][0]["windows"][0]["freshness"]["status"], "unknown")

    def test_radar_event_time_reaches_preview_but_unknown_identity_is_ineligible(self) -> None:
        radar_value = json.loads(
            (Path(__file__).parent / "fixtures" / "codex-radar-usage-v2.json").read_text()
        )
        projected = project_native_codex_cached_rollout(radar_value, generated_at=NOW)
        native_mapping = mapping_v2()
        native_mapping["bindings"][0]["provider_id"] = "codex"
        native_mapping["bindings"][0]["producer"] = {
            "adapter_id": "native-codex-rollout",
            "adapter_revision": "codex-session-rollout-v2",
        }
        native_policy = policy()
        native_policy["target_rules"][0]["provider_id"] = "codex"
        preview = build_authority_preview(
            snapshots=[projected],
            mapping=native_mapping,
            policy=native_policy,
            inventory=inventory(),
            evaluated_at=NOW,
        )
        self.assertEqual(
            projected["windows"][0]["source"]["timestamp_provenance"],
            "client_event_at",
        )
        self.assertEqual(
            projected["windows"][0]["observed_at"],
            radar_value["client_event_at"],
        )
        self.assertFalse(preview["targets"][0]["eligible"])
        self.assertIn("snapshot_missing", preview["targets"][0]["reasons"])
        self.assertEqual(preview["eligible_target_count"], 0)
        self.assertFalse(preview["scheduling_authoritative"])

    def test_cli_policy_preview_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "root": str(root),
                        "execution_target_inventory": config_inventory(),
                        "constraint_registry": {
                            "schema_version": 1,
                            "version": "constraints-r1",
                            "constraints": {},
                        },
                    }
                )
            )
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(authoritative_snapshot()))
            mapping_path = root / "mapping.json"
            mapping_path.write_text(json.dumps(mapping_v2()))
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(policy()))
            before = {path.relative_to(root) for path in root.rglob("*")}
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "--config",
                        str(config_path),
                        "provider-resource-report",
                        "--snapshot-json",
                        str(snapshot_path),
                        "--mapping-json",
                        str(mapping_path),
                        "--policy-json",
                        str(policy_path),
                        "--max-age-seconds",
                        "300",
                        "--evaluated-at",
                        NOW.isoformat(),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            parsed = json.loads(output.getvalue())
            self.assertEqual(parsed["contract"], "provider-resource-report-v2")
            self.assertTrue(parsed["authority_preview"]["targets"][0]["eligible"])
            self.assertEqual(before, {path.relative_to(root) for path in root.rglob("*")})


class ProviderResourceGateContractTests(unittest.TestCase):
    def test_keys_are_stable_and_recomputed(self) -> None:
        value = gate_decision()
        self.assertEqual(validate_gate_decision(value)["decision_key"], value["decision_key"])
        equivalent = gate_decision()
        equivalent["observed_at"] = "2030-01-02T12:59:00+09:00"
        equivalent["reset_at"] = "2030-01-02T14:00:00+09:00"
        equivalent["wake_key"] = resource_gate_wake_key(
            "provider-a", "quota-a", "scope-a", "primary", equivalent["reset_at"]
        )
        equivalent["decision_key"] = resource_gate_decision_key(equivalent)
        self.assertEqual(value["decision_key"], equivalent["decision_key"])
        self.assertEqual(value["wake_key"], equivalent["wake_key"])
        tampered = gate_decision()
        tampered["decision_key"] = "decision-tampered"
        with self.assertRaisesRegex(ProviderResourceValidationError, "decision_key"):
            validate_gate_decision(tampered)

    def test_global_coverage_prevents_duplicate_wake_semantics(self) -> None:
        self.assertEqual(
            global_gate_coverage(
                global_gated=True,
                global_reset_at="2030-01-02T05:00:00Z",
                target_reset_at="2030-01-02T05:00:00Z",
            )["status"],
            "covered",
        )
        covered = gate_decision(action="covered_by_global", global_status="covered")
        self.assertEqual(validate_gate_decision(covered)["action"], "covered_by_global")
        invalid = gate_decision(action="defer", global_status="covered")
        with self.assertRaisesRegex(ProviderResourceValidationError, "covering global"):
            validate_gate_decision(invalid)

    def test_decision_dedup_and_one_active_gate_per_resource(self) -> None:
        value = gate_decision()
        self.assertEqual(len(deduplicate_gate_decisions([value, value])), 1)
        state = {
            "schema_version": 1,
            "contract": GATE_STATE_CONTRACT,
            "migration": {
                "mode": "typed_primary_scalar_compatibility",
                "legacy_scalar_role": "global_gate_only",
                "rollback_mode": "disable_typed_evaluation_preserve_records",
                "evidence_history": "append_only",
            },
            "active_gates": [
                {
                    "resource_key": value["resource_key"],
                    "decision_key": value["decision_key"],
                    "wake_key": value["wake_key"],
                    "reset_at": value["reset_at"],
                    "status": "active",
                }
            ],
        }
        self.assertEqual(len(validate_gate_state(state)["active_gates"]), 1)
        state["active_gates"].append(dict(state["active_gates"][0]))
        with self.assertRaisesRegex(ProviderResourceValidationError, "one active gate"):
            validate_gate_state(state)


if __name__ == "__main__":
    unittest.main()

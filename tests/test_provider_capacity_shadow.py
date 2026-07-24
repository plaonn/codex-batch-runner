from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime
from pathlib import Path

from codex_batch_runner.provider_capacity_contract import (
    build_capacity_bundle,
    capacity_content_id,
    project_provider_resource_snapshot_capacity,
)
from codex_batch_runner.provider_capacity_shadow import (
    CapacityShadowValidationError,
    evaluate_capacity_shadow,
    validate_shadow_evaluation_request,
)
from codex_batch_runner.worker_certification import (
    certify_worker,
    simulate_report_only_canary,
)


NOW = "2030-01-02T04:00:00+00:00"


def digest(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def observation(
    *,
    health: str = "healthy",
    freshness_status: str = "fresh",
    verified_identity: bool = True,
) -> dict:
    if verified_identity:
        snapshot = {
            "schema_version": 1,
            "contract": "provider-resource-snapshot-v1",
            "snapshot_id": "snapshot-r1",
            "generated_at": "2030-01-02T04:00:00Z",
            "producer": {
                "adapter_id": "canonical-snapshot-adapter",
                "adapter_version": "adapter-r1",
                "observation_mode": "provided_snapshot",
                "read_only": True,
            },
            "resource": {
                "provider_id": "provider-example",
                "quota_identity": {
                    "status": "verified",
                    "id": "quota-shared",
                    "source": "source_attested",
                    "confidence": "verified",
                },
                "observation_scope": {
                    "scope_id": "scope-example",
                    "scope_revision": "scope-r1",
                    "host_instance_id": "host-example",
                    "codex_home_instance_id": "home-example",
                    "source_surface": "provider-api",
                    "credential_context_id": "context-example",
                },
            },
            "windows": [
                {
                    "window_id": "primary",
                    "window_duration_seconds": 18_000,
                    "availability": "observed",
                    "remaining": {
                        "status": "observed",
                        "value": 75,
                        "unit": "percent",
                        "derivation": "provider_reported",
                    },
                    "resets_at": {
                        "status": "observed",
                        "value": "2030-01-02T05:00:00Z",
                    },
                    "observed_at": "2030-01-02T03:59:00Z",
                    "freshness": {
                        "status": "unknown",
                        "evaluated_at": NOW,
                        "max_age_seconds": None,
                        "expires_at": None,
                        "reason": "freshness_policy_unset",
                    },
                    "source": {
                        "kind": "provided_snapshot",
                        "field": "windows.primary.remaining",
                        "confidence": "verified_source_timestamp",
                        "timestamp_provenance": "provider_observed_at",
                    },
                }
            ],
            "diagnostics": (
                [{"code": "snapshot_stale_age", "scope": "primary"}]
                if health == "degraded"
                else []
            ),
        }
        return project_provider_resource_snapshot_capacity(
            snapshot,
            evaluated_at=(
                "2030-01-02T04:10:00+00:00"
                if freshness_status == "stale"
                else NOW
            ),
            max_age_seconds=300,
        )

    identity = (
        {
            "status": "unknown",
            "id": None,
            "opaque_id": "opaque-example",
            "source": "source_reported_opaque_id",
            "confidence": "unverified",
        }
    )
    resource_id = capacity_content_id(
        {
            "provider_id": "provider-example",
            "quota_identity_key": "opaque:opaque-example",
        }
    )
    constraint = {
        "constraint_id": capacity_content_id(
            {"resource_id": resource_id, "window_id": "primary"}
        ),
        "window_id": "primary",
        "window_duration_seconds": 18_000.0,
        "remaining": {
            "status": "observed",
            "value": 0.75,
            "unit": "ratio",
            "provenance": "provider_reported",
        },
        "resets_at": {
            "status": "observed",
            "value": "2030-01-02T05:00:00+00:00",
            "relative_seconds": None,
        },
        "availability": "available",
        "source_field": "windows.primary.remaining",
    }
    if freshness_status == "fresh":
        freshness = {
            "status": "fresh",
            "evaluated_at": NOW,
            "max_age_seconds": 300.0,
            "reason": "within_max_age",
        }
    else:
        freshness = {
            "status": "stale",
            "evaluated_at": "2030-01-02T04:10:00+00:00",
            "max_age_seconds": 300.0,
            "reason": "exceeds_max_age",
        }
    body = {
        "provider_id": "provider-example",
        "source_contract": "codex-app-server-capacity-v1",
        "source_revision": "adapter-r1",
        "producer": {
            "id": "codex-app-server-capacity-adapter",
            "revision": "adapter-r1",
        },
        "observation_scope": {
            "kind": "provider_control_plane",
            "scope_id_status": "unknown",
            "scope_id": None,
        },
        "observed_at": "2030-01-02T03:59:00+00:00",
        "timestamp_provenance": "provider_observed_at",
        "acquisition_health": health,
        "freshness": freshness,
        "model_scope_revision": None,
        "canonical_snapshot": None,
        "resources": [
            {
                "resource_id": resource_id,
                "quota_identity": identity,
                "model_scope": {"status": "unknown", "model_ids": []},
                "constraints": [constraint],
            }
        ],
    }
    return {"observation_id": capacity_content_id(body), **body}


def bundle(
    *,
    health: str = "healthy",
    freshness_status: str = "fresh",
    verified_identity: bool = True,
) -> dict:
    value = observation(
        health=health,
        freshness_status=freshness_status,
        verified_identity=verified_identity,
    )
    resource_id = value["resources"][0]["resource_id"]
    pools = (
        [
            {
                "pool_id": "pool-shared",
                "provider_id": "provider-example",
                "resource_ids": [resource_id],
                "binding_status": "explicit",
                "source_revision": "mapping-r1",
            }
        ]
        if verified_identity
        else []
    )
    return build_capacity_bundle([value], pools=pools)


def revisions(bundle_id: str) -> dict:
    return {
        "requirement_revision": "requirement-r1",
        "inventory_snapshot_id": "inventory-r1",
        "selector_policy_revision": "execution-target-selector-v1",
        "mapping_revision": "mapping-r1",
        "authority_revision": "authority-r1",
        "simulator_revision": "simulator-r1",
        "capacity_policy_revision": "capacity-shadow-comparison-policy-v1",
        "capacity_bundle_revision": bundle_id,
        "certification_policy_revision": "worker-certification-policy-v1",
        "canary_policy_revision": "worker-certification-policy-v1",
    }


def request(
    *,
    capacity_bundle: dict | None = None,
    worker_gate: bool | str = False,
) -> dict:
    capacity_bundle = capacity_bundle or bundle()
    frozen_revisions = revisions(capacity_bundle["bundle_id"])
    baseline_decision = {
        "selected_target_id": "target-a",
        "selection_reason": "automatic_static_non_learned",
        "ranked_target_ids": ["target-a", "target-b"],
    }
    source = capacity_bundle["observations"][0]
    resource = source["resources"][0]
    constraint = resource["constraints"][0]
    targets = []
    for rank, (target_id, model_id) in enumerate(
        (("target-a", "model-a"), ("target-b", "model-b"))
    ):
        certification = None
        canary = None
        if worker_gate and target_id == "target-b":
            fixture = json.loads(
                (
                    Path(__file__).parent
                    / "fixtures"
                    / "worker-certification-bounded-write-v1.json"
                ).read_text(encoding="utf-8")
            )
            candidate = fixture["candidate"]
            evidence = fixture["evidence"]
            candidate["target_snapshot_id"] = "inventory-r1"
            evidence["target_snapshot_id"] = "inventory-r1"
            if worker_gate != "natural":
                evidence["records"] = [
                    item
                    for item in evidence["records"]
                    if item["evidence_class"] == "synthetic"
                ]
            evaluated = datetime.fromisoformat(NOW)
            record = certify_worker(
                candidate,
                evidence,
                evaluated_at=evaluated,
            )
            certification = {
                "record": record,
                "candidate": candidate,
                "evidence": evidence,
            }
            cohort_key = "target-b"
            canary_record = simulate_report_only_canary(
                record,
                cohort_key=cohort_key,
                candidate=candidate,
                evidence=evidence,
                evaluated_at=evaluated,
            )
            if worker_gate == "natural":
                for index in range(10_000):
                    cohort_key = f"target-b-{index}"
                    canary_record = simulate_report_only_canary(
                        record,
                        cohort_key=cohort_key,
                        candidate=candidate,
                        evidence=evidence,
                        evaluated_at=evaluated,
                    )
                    if canary_record["report_only_lane"] == "canary":
                        break
            canary = {
                "record": canary_record,
                "cohort_key": cohort_key,
                "adverse_signals": 0,
            }
        targets.append(
            {
                "target_id": target_id,
                "selector_rank": rank,
                "capability_pass": True,
                "safety_pass": True,
                "hard_constraints_pass": True,
                "quality_floor_pass": True,
                "binding": {
                    "binding_id": f"binding-{target_id}",
                    "target_id": target_id,
                    "observation_id": source["observation_id"],
                    "provider_id": source["provider_id"],
                    "resource_id": resource["resource_id"],
                    "quota_identity_id": "quota-shared",
                    "model_id": model_id,
                    "capacity_pool": "pool-shared",
                    "constraint_id": constraint["constraint_id"],
                    "remaining_unit": "percent",
                },
                "worker_certification": certification,
                "canary_gate": canary,
            }
        )
    return {
        "schema_version": 1,
        "contract": "capacity-shadow-evaluation-request-v1",
        "evaluated_at": NOW,
        "revisions": frozen_revisions,
        "revision_currentness": {
            "contract": "capacity-shadow-revision-currentness-v1",
            "current_revisions": copy.deepcopy(frozen_revisions),
        },
        "provider_resource_lineage": {
            "snapshot_ids": ["snapshot-r1"],
            "mapping_revision": "mapping-r1",
            "authority_revision": "authority-r1",
            "simulator_revision": "simulator-r1",
        },
        "provider_resource_mapping": {
            "schema_version": 2,
            "contract": "provider-resource-mapping-v2",
            "mapping_revision": "mapping-r1",
            "target_inventory_snapshot_id": "inventory-r1",
            "status": "current",
            "bindings": [
                {
                    "binding_id": f"binding-{target_id}",
                    "target_id": target_id,
                    "capacity_pool": "pool-shared",
                    "provider_id": "provider-example",
                    "quota_identity_id": "quota-shared",
                    "identity_authority": "source_attested",
                    "observation_scope": {
                        "scope_id": "scope-example",
                        "scope_revision": "scope-r1",
                        "host_instance_id": "host-example",
                        "codex_home_instance_id": "home-example",
                        "source_surface": "provider-api",
                        "credential_context_id": "context-example",
                    },
                    "producer": {
                        "adapter_id": "canonical-snapshot-adapter",
                        "adapter_revision": "adapter-r1",
                    },
                    "verified_at": "2030-01-01T00:00:00+00:00",
                    "expires_at": "2030-02-01T00:00:00+00:00",
                    "status": "current",
                    "invalidation_reason": None,
                    "supersedes_binding_id": None,
                }
                for target_id in ("target-a", "target-b")
            ],
        },
        "baseline": {
            "decision": baseline_decision,
            "decision_digest": digest(baseline_decision),
            "selected_target_id": "target-a",
            "selector_order": ["target-a", "target-b"],
        },
        "preeligible_targets": targets,
        "capacity_bundle": capacity_bundle,
    }


class CapacityShadowEvaluationTest(unittest.TestCase):
    def test_complete_evidence_preserves_baseline_and_is_report_only(self) -> None:
        source = request()
        baseline_before = copy.deepcopy(source["baseline"])

        first = evaluate_capacity_shadow(source)
        second = evaluate_capacity_shadow(copy.deepcopy(source))

        self.assertEqual(first, second)
        self.assertEqual(first["baseline"], baseline_before)
        self.assertEqual(
            first["baseline"]["decision_digest"],
            digest(first["baseline"]["decision"]),
        )
        self.assertEqual(
            first["preeligible_target_ids"],
            ["target-a", "target-b"],
        )
        self.assertEqual(
            first["shadow_recommendation"]["status"],
            "capacity_aware_shadow",
        )
        self.assertEqual(
            first["shadow_recommendation"]["recommended_target_id"],
            "target-a",
        )
        for field in (
            "mutation_allowed",
            "scheduling_authoritative",
            "automatic_substitution",
            "live_routing",
            "default_routing",
            "provider_promotion",
            "synthetic_fixture_promotion",
        ):
            self.assertIs(first[field], False)
        self.assertIs(first["read_only"], True)
        for field in (
            "queue_mutations",
            "config_mutations",
            "cooldown_mutations",
            "wake_mutations",
            "reservation_mutations",
            "routing_mutations",
        ):
            self.assertEqual(first[field], [])
        report_without_hash = copy.deepcopy(first)
        report_hash = report_without_hash.pop("report_hash")
        self.assertEqual(report_hash, digest(report_without_hash))

    def test_revision_mismatch_falls_back_without_changing_baseline(self) -> None:
        source = request()
        source["revision_currentness"]["current_revisions"][
            "mapping_revision"
        ] = "mapping-r2"

        report = evaluate_capacity_shadow(source)

        self.assertEqual(
            report["shadow_recommendation"]["status"],
            "capacity_unaware_baseline_fallback",
        )
        self.assertIn(
            "revision_mismatch",
            report["shadow_recommendation"]["reason_codes"],
        )
        self.assertEqual(
            report["shadow_recommendation"]["recommended_target_id"],
            source["baseline"]["selected_target_id"],
        )
        self.assertEqual(report["baseline"], source["baseline"])

    def test_distinct_mapped_resources_can_reorder_shadow_only(self) -> None:
        first = observation()
        second_snapshot = copy.deepcopy(first["canonical_snapshot"])
        second_snapshot["snapshot_id"] = "snapshot-r2"
        second_snapshot["resource"]["quota_identity"]["id"] = "quota-second"
        second_snapshot["windows"][0]["remaining"]["value"] = 95
        second = project_provider_resource_snapshot_capacity(
            second_snapshot,
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        first_resource = first["resources"][0]
        second_resource = second["resources"][0]
        capacity_bundle = build_capacity_bundle(
            [first, second],
            pools=[
                {
                    "pool_id": "pool-first",
                    "provider_id": "provider-example",
                    "resource_ids": [first_resource["resource_id"]],
                    "binding_status": "explicit",
                    "source_revision": "mapping-r1",
                },
                {
                    "pool_id": "pool-second",
                    "provider_id": "provider-example",
                    "resource_ids": [second_resource["resource_id"]],
                    "binding_status": "explicit",
                    "source_revision": "mapping-r1",
                },
            ],
        )
        source = request(capacity_bundle=capacity_bundle)
        source["revisions"]["capacity_bundle_revision"] = capacity_bundle[
            "bundle_id"
        ]
        source["revision_currentness"]["current_revisions"][
            "capacity_bundle_revision"
        ] = capacity_bundle["bundle_id"]
        source["provider_resource_lineage"]["snapshot_ids"] = [
            "snapshot-r1",
            "snapshot-r2",
        ]
        for index, (target, resource, observed, quota, pool) in enumerate(
            (
                (
                    source["preeligible_targets"][0],
                    first_resource,
                    first,
                    "quota-shared",
                    "pool-first",
                ),
                (
                    source["preeligible_targets"][1],
                    second_resource,
                    second,
                    "quota-second",
                    "pool-second",
                ),
            )
        ):
            target["binding"].update(
                {
                    "observation_id": observed["observation_id"],
                    "resource_id": resource["resource_id"],
                    "quota_identity_id": quota,
                    "capacity_pool": pool,
                    "constraint_id": resource["constraints"][0][
                        "constraint_id"
                    ],
                }
            )
            source["provider_resource_mapping"]["bindings"][index].update(
                {
                    "capacity_pool": pool,
                    "quota_identity_id": quota,
                }
            )

        report = evaluate_capacity_shadow(source)

        self.assertEqual(
            report["shadow_recommendation"]["status"],
            "capacity_aware_shadow",
        )
        self.assertEqual(
            report["shadow_recommendation"]["recommended_target_id"],
            "target-b",
        )
        self.assertEqual(report["baseline"]["selected_target_id"], "target-a")
        self.assertFalse(report["live_routing"])

    def test_unknown_stale_and_degraded_evidence_fail_closed(self) -> None:
        cases = (
            (bundle(verified_identity=False), "capacity_identity_unknown"),
            (
                bundle(freshness_status="stale"),
                "capacity_evidence_not_usable",
            ),
            (bundle(health="degraded"), "capacity_evidence_not_usable"),
        )
        for capacity_bundle, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                source = request(capacity_bundle=capacity_bundle)
                report = evaluate_capacity_shadow(source)
                recommendation = report["shadow_recommendation"]
                self.assertEqual(
                    recommendation["status"],
                    "capacity_unaware_baseline_fallback",
                )
                self.assertIn(expected_reason, recommendation["reason_codes"])
                self.assertEqual(
                    recommendation["recommended_target_id"],
                    "target-a",
                )

    def test_synthetic_worker_certification_never_promotes(self) -> None:
        source = request(worker_gate=True)

        report = evaluate_capacity_shadow(source)

        recommendation = report["shadow_recommendation"]
        self.assertEqual(
            recommendation["status"],
            "capacity_unaware_baseline_fallback",
        )
        self.assertIn(
            "worker_certification_ineligible",
            recommendation["reason_codes"],
        )
        self.assertIn(
            "worker_canary_gate_ineligible",
            recommendation["reason_codes"],
        )
        self.assertEqual(recommendation["recommended_target_id"], "target-a")
        self.assertIs(report["synthetic_fixture_promotion"], False)

    def test_rederived_natural_worker_gate_still_requires_external_attestation(self) -> None:
        source = request(worker_gate="natural")

        report = evaluate_capacity_shadow(source)

        self.assertEqual(
            report["shadow_recommendation"]["status"],
            "capacity_unaware_baseline_fallback",
        )
        self.assertIn(
            "worker_certification_ineligible",
            report["shadow_recommendation"]["reason_codes"],
        )
        self.assertFalse(report["live_routing"])
        self.assertFalse(report["mutation_allowed"])

    def test_strict_request_rejects_drift_or_ineligible_targets(self) -> None:
        cases = []
        digest_drift = request()
        digest_drift["baseline"]["decision_digest"] = "sha256:" + ("0" * 64)
        cases.append(digest_drift)

        order_drift = request()
        order_drift["preeligible_targets"].reverse()
        cases.append(order_drift)

        unsuitable = request()
        unsuitable["preeligible_targets"][1]["quality_floor_pass"] = False
        cases.append(unsuitable)

        unknown_field = request()
        unknown_field["unexpected"] = True
        cases.append(unknown_field)

        contradictory_baseline = request()
        contradictory_baseline["baseline"]["decision"][
            "selected_target_id"
        ] = "target-b"
        contradictory_baseline["baseline"]["decision_digest"] = digest(
            contradictory_baseline["baseline"]["decision"]
        )
        cases.append(contradictory_baseline)

        for value in cases:
            with self.subTest():
                with self.assertRaises(CapacityShadowValidationError):
                    validate_shadow_evaluation_request(value)

    def test_future_worker_evidence_and_mapping_injection_fail_closed(self) -> None:
        future = request(worker_gate="natural")
        future["evaluated_at"] = "2029-12-31T00:00:00+00:00"
        validated = validate_shadow_evaluation_request(future)
        worker = validated["preeligible_targets"][1]
        self.assertFalse(worker["worker_certification"]["current_at_evaluation"])
        self.assertFalse(worker["canary_gate"]["chronology_current"])

        injected = request()
        injected["provider_resource_mapping"]["bindings"][1][
            "capacity_pool"
        ] = "pool-injected"
        report = evaluate_capacity_shadow(injected)
        self.assertIn(
            "capacity_mapping_binding_invalid",
            report["shadow_recommendation"]["reason_codes"],
        )


if __name__ == "__main__":
    unittest.main()

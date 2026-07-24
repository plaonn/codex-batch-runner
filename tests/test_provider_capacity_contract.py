from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from codex_batch_runner.antigravity_statusline import collect_statusline_quota
from codex_batch_runner.codex_app_server_capacity import (
    project_rate_limits_response,
)
from codex_batch_runner.provider_capacity_contract import (
    CAPACITY_BUNDLE_CONTRACT,
    ProviderCapacityValidationError,
    build_capacity_bundle,
    capacity_content_id,
    project_antigravity_statusline_capacity,
    project_codex_app_server_capacity,
    project_provider_resource_snapshot_capacity,
    validate_capacity_bundle,
    validate_capacity_observation,
)


NOW = datetime(2030, 1, 2, 4, 0, tzinfo=timezone.utc)
FIXTURE = (
    Path(__file__).parent / "fixtures" / "provider-capacity-bundle-v1.json"
)


def codex_source(
    *,
    primary_used: float = 25,
    adapter_id: str = "codex-app-server-capacity-v1",
) -> dict:
    value = project_rate_limits_response(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "planType": "pro",
                    "primary": {
                        "windowDurationMins": 300,
                        "usedPercent": primary_used,
                        "resetsAt": "2030-01-02T05:00:00Z",
                    },
                    "secondary": {
                        "windowDurationMins": 10080,
                        "usedPercent": 100,
                        "resetsAt": "2030-01-09T04:00:00Z",
                    },
                }
            }
        },
        evaluated_at=NOW,
    )
    assert value is not None
    value["adapter_id"] = adapter_id
    return value


def antigravity_source(
    *,
    version: str = "statusline-v1",
    remaining: float = 0.375,
) -> dict:
    collection = collect_statusline_quota(
        {
            "version": version,
            "plan_tier": "pro",
            "quota": {
                "gemini-weekly": {
                    "remaining_fraction": remaining,
                    "reset_time": "2030-01-02T05:00:00Z",
                    "reset_in_seconds": 3600,
                }
            },
        },
        collected_at=NOW,
    )
    assert collection["cache"] is not None
    return collection["cache"]


def fixture_bundle() -> dict:
    codex = project_codex_app_server_capacity(
        codex_source(),
        evaluated_at=NOW,
        max_age_seconds=300,
    )
    antigravity = project_antigravity_statusline_capacity(
        antigravity_source(),
        evaluated_at="2030-01-02T04:10:00Z",
        max_age_seconds=300,
    )
    return build_capacity_bundle([antigravity, codex])


class ProviderCapacityContractTests(unittest.TestCase):
    def test_sanitized_cross_provider_fixture_is_deterministic(self) -> None:
        bundle = fixture_bundle()
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))

        self.assertEqual(bundle, expected)
        self.assertEqual(bundle["contract"], CAPACITY_BUNDLE_CONTRACT)
        self.assertTrue(bundle["read_only"])
        self.assertFalse(bundle["mutation_allowed"])
        self.assertFalse(bundle["scheduling_authoritative"])
        self.assertEqual(
            bundle["lineage"],
            {
                "canonical_contract": "provider-resource-snapshot-v1",
                "canonical_contract_unchanged": True,
                "extension_role": "advisory_evidence_input",
                "canonical_runtime_state": False,
            },
        )
        self.assertEqual(build_capacity_bundle(reversed(bundle["observations"])), bundle)

    def test_projection_keeps_quota_pool_and_model_semantics_separate(self) -> None:
        bundle = fixture_bundle()
        by_provider = {
            item["provider_id"]: item for item in bundle["observations"]
        }
        codex = by_provider["codex"]
        antigravity = by_provider["antigravity"]

        self.assertEqual(bundle["pools"], [])
        self.assertEqual(
            codex["resources"][0]["quota_identity"],
            {
                "status": "unknown",
                "id": None,
                "opaque_id": "codex",
                "source": "source_reported_opaque_id",
                "confidence": "unverified",
            },
        )
        self.assertEqual(
            antigravity["resources"][0]["model_scope"],
            {"status": "unknown", "model_ids": []},
        )
        codex_constraints = codex["resources"][0]["constraints"]
        self.assertEqual(
            {item["remaining"]["provenance"] for item in codex_constraints},
            {"derived_complement_of_used_ratio"},
        )
        self.assertEqual(
            {
                item["remaining"]["value"]: item["availability"]
                for item in codex_constraints
            },
            {0.0: "exhausted", 0.75: "available"},
        )
        antigravity_constraint = antigravity["resources"][0]["constraints"][0]
        self.assertIsNone(antigravity_constraint["window_duration_seconds"])
        self.assertEqual(
            antigravity_constraint["remaining"]["provenance"],
            "provider_reported",
        )
        self.assertEqual(
            antigravity_constraint["resets_at"]["relative_seconds"], 3600
        )

    def test_health_freshness_and_capacity_are_independent(self) -> None:
        bundle = fixture_bundle()
        by_provider = {
            item["provider_id"]: item for item in bundle["observations"]
        }
        self.assertEqual(by_provider["codex"]["acquisition_health"], "healthy")
        self.assertEqual(by_provider["codex"]["freshness"]["status"], "fresh")
        self.assertEqual(
            by_provider["antigravity"]["acquisition_health"], "healthy"
        )
        self.assertEqual(
            by_provider["antigravity"]["freshness"]["status"], "stale"
        )

        unavailable = codex_source()
        unavailable.update(
            {
                "status": "unavailable",
                "reason": "app_server_unavailable",
                "resources": [],
            }
        )
        observation = project_codex_app_server_capacity(unavailable)
        self.assertEqual(observation["acquisition_health"], "unavailable")
        self.assertEqual(observation["freshness"]["status"], "unknown")
        self.assertEqual(observation["resources"], [])

    def test_unknown_fields_sensitive_keys_and_digest_tampering_are_rejected(self) -> None:
        source = codex_source()
        source["account"] = {"email": "redacted@example.test"}
        with self.assertRaises(ProviderCapacityValidationError):
            project_codex_app_server_capacity(source)

        bundle = fixture_bundle()
        unknown = deepcopy(bundle)
        unknown["routing_authority"] = True
        with self.assertRaises(ProviderCapacityValidationError):
            validate_capacity_bundle(unknown)

        tampered = deepcopy(bundle)
        tampered["observations"][0]["resources"][0]["constraints"][0][
            "availability"
        ] = "constrained"
        with self.assertRaises(ProviderCapacityValidationError):
            validate_capacity_bundle(tampered)

    def test_validator_returns_a_defensive_copy(self) -> None:
        bundle = fixture_bundle()
        validated = validate_capacity_bundle(bundle)
        validated["observations"].clear()
        self.assertEqual(len(bundle["observations"]), 2)

    def test_conflicts_are_reported_not_silently_merged(self) -> None:
        first = project_codex_app_server_capacity(
            codex_source(primary_used=25),
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        second = project_codex_app_server_capacity(
            codex_source(primary_used=50),
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        bundle = build_capacity_bundle([first, second])
        constraint_conflicts = [
            item
            for item in bundle["conflict_evidence"]
            if item["kind"] == "inconsistent_constraint_evidence"
        ]
        self.assertEqual(len(constraint_conflicts), 1)
        self.assertIn("remaining", constraint_conflicts[0]["differing_fields"])

        revision_one = project_antigravity_statusline_capacity(
            antigravity_source(version="statusline-v1")
        )
        revision_two = project_antigravity_statusline_capacity(
            antigravity_source(version="statusline-v2", remaining=0.5)
        )
        revision_bundle = build_capacity_bundle([revision_one, revision_two])
        self.assertIn(
            "producer_revision_conflict",
            {
                item["kind"]
                for item in revision_bundle["conflict_evidence"]
            },
        )

    def test_explicit_pools_bind_resource_ids_without_rewriting_quota_identity(self) -> None:
        observation = project_codex_app_server_capacity(codex_source())
        resource_id = observation["resources"][0]["resource_id"]
        bundle = build_capacity_bundle(
            [observation],
            pools=[
                {
                    "pool_id": "codex-local",
                    "provider_id": "codex",
                    "resource_ids": [resource_id],
                    "binding_status": "explicit",
                    "source_revision": "operator-binding-r1",
                }
            ],
        )
        self.assertEqual(bundle["pools"][0]["resource_ids"], [resource_id])
        self.assertEqual(
            bundle["observations"][0]["resources"][0]["quota_identity"][
                "status"
            ],
            "unknown",
        )
        with self.assertRaisesRegex(
            ProviderCapacityValidationError, "unknown resource"
        ):
            build_capacity_bundle(
                [observation],
                pools=[
                    {
                        "pool_id": "bad",
                        "provider_id": "codex",
                        "resource_ids": ["sha256:" + "0" * 64],
                        "binding_status": "explicit",
                        "source_revision": "operator-binding-r1",
                    }
                ],
            )

    def test_canonical_lineage_requires_validated_snapshot_projection(self) -> None:
        observation = project_codex_app_server_capacity(codex_source())
        observation["source_contract"] = "provider-resource-snapshot-v1"
        body = {
            key: value
            for key, value in observation.items()
            if key != "observation_id"
        }
        observation["observation_id"] = capacity_content_id(body)
        with self.assertRaisesRegex(
            ProviderCapacityValidationError, "canonical snapshot"
        ):
            validate_capacity_observation(observation)

        snapshot = {
            "schema_version": 1,
            "contract": "provider-resource-snapshot-v1",
            "snapshot_id": "snapshot-r1",
            "generated_at": "2030-01-02T04:00:00Z",
            "producer": {
                "adapter_id": "canonical-snapshot-adapter",
                "adapter_version": "snapshot-r1",
                "observation_mode": "provided_snapshot",
                "read_only": True,
            },
            "resource": {
                "provider_id": "codex",
                "quota_identity": {
                    "status": "verified",
                    "id": "quota-codex-a",
                    "source": "source_attested",
                    "confidence": "verified",
                },
                "observation_scope": {
                    "scope_id": "scope-a",
                    "scope_revision": "scope-r1",
                    "host_instance_id": "host-a",
                    "codex_home_instance_id": "home-a",
                    "source_surface": "provider-api",
                    "credential_context_id": "credential-context-a",
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
                        "evaluated_at": "2030-01-02T04:00:00Z",
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
            "diagnostics": [],
        }
        validated = project_provider_resource_snapshot_capacity(
            snapshot,
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        self.assertEqual(
            validated["resources"][0]["quota_identity"]["status"], "verified"
        )
        self.assertEqual(
            validated["resources"][0]["model_scope"]["status"], "unknown"
        )
        self.assertEqual(
            validated["resources"][0]["constraints"][0]["remaining"]["unit"],
            "percent",
        )
        derived_snapshot = deepcopy(snapshot)
        derived_snapshot["snapshot_id"] = "snapshot-derived"
        derived_snapshot["windows"][0]["remaining"][
            "derivation"
        ] = "provider_used_percent_complement"
        derived = project_provider_resource_snapshot_capacity(
            derived_snapshot,
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        self.assertEqual(
            derived["resources"][0]["constraints"][0]["remaining"][
                "provenance"
            ],
            "derived_complement_of_used_ratio",
        )
        for index, unit in enumerate(("tokens", "credits", "requests")):
            unit_snapshot = deepcopy(snapshot)
            unit_snapshot["snapshot_id"] = f"snapshot-unit-{index}"
            unit_snapshot["windows"][0]["remaining"].update(
                {"unit": unit, "value": 25}
            )
            projected = project_provider_resource_snapshot_capacity(
                unit_snapshot,
                evaluated_at=NOW,
                max_age_seconds=300,
            )
            self.assertEqual(
                projected["resources"][0]["constraints"][0]["remaining"][
                    "unit"
                ],
                unit,
            )
        unknown_snapshot = deepcopy(snapshot)
        unknown_snapshot["snapshot_id"] = "snapshot-unknown"
        unknown_snapshot["windows"][0].update(
            {
                "availability": "unknown",
                "remaining": {
                    "status": "unknown",
                    "value": None,
                    "unit": None,
                    "derivation": "unavailable",
                },
                "resets_at": {"status": "unknown", "value": None},
            }
        )
        unknown = project_provider_resource_snapshot_capacity(
            unknown_snapshot,
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        self.assertIsNone(
            unknown["resources"][0]["constraints"][0]["remaining"]["unit"]
        )
        not_applicable_snapshot = deepcopy(snapshot)
        not_applicable_snapshot["snapshot_id"] = "snapshot-no-reset"
        not_applicable_snapshot["windows"][0]["resets_at"] = {
            "status": "not_applicable",
            "value": None,
        }
        not_applicable = project_provider_resource_snapshot_capacity(
            not_applicable_snapshot,
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        self.assertEqual(
            not_applicable["resources"][0]["constraints"][0]["resets_at"][
                "status"
            ],
            "not_applicable",
        )

        same_identity = deepcopy(snapshot)
        same_identity["snapshot_id"] = "snapshot-r2"
        same_identity["windows"][0]["remaining"]["value"] = 50
        same_identity_observation = project_provider_resource_snapshot_capacity(
            same_identity,
            evaluated_at=NOW,
            max_age_seconds=300,
        )
        same_identity_bundle = build_capacity_bundle(
            [validated, same_identity_observation]
        )
        self.assertIn(
            "inconsistent_constraint_evidence",
            {
                item["kind"]
                for item in same_identity_bundle["conflict_evidence"]
            },
        )

        different_identity = deepcopy(same_identity)
        different_identity["snapshot_id"] = "snapshot-r3"
        different_identity["resource"]["quota_identity"]["id"] = (
            "quota-codex-b"
        )
        different_identity_observation = (
            project_provider_resource_snapshot_capacity(
                different_identity,
                evaluated_at=NOW,
                max_age_seconds=300,
            )
        )
        different_identity_bundle = build_capacity_bundle(
            [validated, different_identity_observation]
        )
        self.assertNotIn(
            "inconsistent_constraint_evidence",
            {
                item["kind"]
                for item in different_identity_bundle["conflict_evidence"]
            },
        )


if __name__ == "__main__":
    unittest.main()

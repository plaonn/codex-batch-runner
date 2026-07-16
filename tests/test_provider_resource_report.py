from __future__ import annotations

import contextlib
import io
import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.provider_resource_report import (
    ProviderResourceValidationError,
    antigravity_unavailable_snapshot,
    build_provider_resource_report,
    evaluate_snapshot_freshness,
    mapping_preview,
    project_native_codex_cached_rollout,
    run_snapshot_adapter,
    validate_mapping,
    validate_snapshot,
)

NOW = datetime(2030, 1, 2, 4, 0, tzinfo=timezone.utc)


def window(
    window_id: str = "short",
    *,
    observed_at: str | None = "2030-01-02T03:59:00Z",
    resets_at: str | None = "2030-01-02T05:00:00Z",
    remaining: float = 42.0,
    unit: str = "percent",
) -> dict:
    return {
        "window_id": window_id,
        "window_duration_seconds": 18000,
        "availability": "observed",
        "remaining": {
            "status": "observed",
            "value": remaining,
            "unit": unit,
            "derivation": "provider_reported",
        },
        "resets_at": {"status": "observed", "value": resets_at} if resets_at else {"status": "unknown", "value": None},
        "observed_at": observed_at,
        "freshness": {
            "status": "unknown",
            "evaluated_at": "2030-01-02T04:00:00Z",
            "max_age_seconds": None,
            "expires_at": None,
            "reason": "freshness_policy_unset",
        },
        "source": {"kind": "provided_snapshot", "field": f"window.{window_id}", "confidence": "verified_source_timestamp"},
    }


def snapshot(*, windows: list[dict] | None = None, quota_id: str | None = "quota-a") -> dict:
    return {
        "schema_version": 1,
        "contract": "provider-resource-snapshot-v1",
        "snapshot_id": "snapshot-a",
        "generated_at": "2030-01-02T04:00:00Z",
        "producer": {
            "adapter_id": "synthetic-adapter",
            "adapter_version": "v1",
            "observation_mode": "provided_snapshot",
            "read_only": True,
        },
        "resource": {
            "provider_id": "provider-a",
            "quota_identity": {
                "status": "verified" if quota_id else "unknown",
                "id": quota_id,
                "source": "operator_verified" if quota_id else "source_reported_opaque_id",
                "confidence": "verified" if quota_id else "unverified",
            },
        },
        "windows": windows if windows is not None else [window()],
        "diagnostics": [],
    }


def binding(
    target_id: str = "target-a",
    *,
    binding_id: str = "binding-a",
    quota_id: str = "quota-a",
    pool: str = "codex",
    expires_at: str = "2030-02-01T00:00:00Z",
) -> dict:
    return {
        "binding_id": binding_id,
        "target_id": target_id,
        "capacity_pool": pool,
        "provider_id": "provider-a",
        "quota_identity_id": quota_id,
        "source": "operator_verified",
        "verified_at": "2030-01-01T00:00:00Z",
        "expires_at": expires_at,
    }


def mapping(*bindings: dict, status: str = "current") -> dict:
    return {
        "schema_version": 1,
        "contract": "provider-resource-mapping-v1",
        "mapping_revision": "mapping-r1",
        "status": status,
        "bindings": list(bindings),
    }


class ProviderResourceValidationTests(unittest.TestCase):
    def test_public_examples_validate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        validate_snapshot(json.loads((root / "examples/provider-resource-snapshot-v1.example.json").read_text(encoding="utf-8")))
        validate_mapping(json.loads((root / "examples/provider-resource-mapping-v1.example.json").read_text(encoding="utf-8")))

    def test_accepts_multiple_independent_windows(self) -> None:
        value = snapshot(windows=[window("short"), window("long", resets_at="2030-01-08T00:00:00Z")])
        self.assertEqual([item["window_id"] for item in validate_snapshot(value)["windows"]], ["short", "long"])

    def test_rejects_future_naive_and_invalid_timestamps(self) -> None:
        future = snapshot()
        future["windows"][0]["observed_at"] = "2030-01-02T04:02:00Z"
        with self.assertRaisesRegex(ProviderResourceValidationError, "observation_time_invalid"):
            validate_snapshot(future)
        naive = snapshot()
        naive["windows"][0]["observed_at"] = "2030-01-02T03:59:00"
        with self.assertRaisesRegex(ProviderResourceValidationError, "timezone-aware"):
            validate_snapshot(naive)
        invalid = snapshot()
        invalid["generated_at"] = "not-a-time"
        with self.assertRaisesRegex(ProviderResourceValidationError, "timezone-aware"):
            validate_snapshot(invalid)

    def test_missing_observation_time_is_valid_but_freshness_unknown(self) -> None:
        value = validate_snapshot(snapshot(windows=[window(observed_at=None)]))
        evaluated = evaluate_snapshot_freshness(value, evaluated_at=NOW, max_age_seconds=300)
        self.assertEqual(evaluated["windows"][0]["freshness"]["status"], "unknown")

    def test_percent_and_finite_number_validation(self) -> None:
        for bad in (-1, 101, math.inf, math.nan):
            value = snapshot(windows=[window(remaining=bad)])
            with self.subTest(bad=bad), self.assertRaisesRegex(ProviderResourceValidationError, "remaining_out_of_range"):
                validate_snapshot(value)

    def test_fresh_stale_age_and_stale_after_reset(self) -> None:
        fresh = validate_snapshot(snapshot())
        self.assertEqual(evaluate_snapshot_freshness(fresh, evaluated_at=NOW, max_age_seconds=300)["windows"][0]["freshness"]["status"], "fresh")
        old = validate_snapshot(snapshot(windows=[window(observed_at="2030-01-02T03:00:00Z")]))
        self.assertEqual(evaluate_snapshot_freshness(old, evaluated_at=NOW, max_age_seconds=300)["windows"][0]["freshness"]["status"], "stale_age")
        reset = validate_snapshot(snapshot(windows=[window(observed_at="2030-01-02T03:00:00Z", resets_at="2030-01-02T03:30:00Z")]))
        self.assertEqual(evaluate_snapshot_freshness(reset, evaluated_at=NOW, max_age_seconds=99999)["windows"][0]["freshness"]["status"], "stale_after_reset")

    def test_rejects_raw_path_credential_and_identity_leakage(self) -> None:
        for key in ("raw_output", "path", "credential", "session_id", "thread_id"):
            value = snapshot()
            value[key] = "secret"
            with self.subTest(key=key), self.assertRaises(ProviderResourceValidationError):
                validate_snapshot(value)
        value = snapshot()
        value["snapshot_id"] = "unsafe identifier"
        with self.assertRaisesRegex(ProviderResourceValidationError, "public-safe"):
            validate_snapshot(value)


class ProviderResourceAdapterTests(unittest.TestCase):
    def test_unknown_invalid_and_unavailable_adapter_results_are_sanitized(self) -> None:
        invalid = run_snapshot_adapter(["/bin/sh", "-c", "printf not-json"], timeout_seconds=1)
        unavailable = run_snapshot_adapter(["/definitely/missing"], timeout_seconds=1)
        timed_out = run_snapshot_adapter(["/bin/sh", "-c", "sleep 2"], timeout_seconds=1)
        self.assertEqual((invalid["status"], invalid["reason"]), ("invalid", "snapshot_json_invalid"))
        self.assertEqual((unavailable["status"], unavailable["reason"]), ("unavailable", "snapshot_command_failed"))
        self.assertEqual((timed_out["status"], timed_out["reason"]), ("unavailable", "snapshot_command_timed_out"))
        self.assertNotIn("argv", invalid)
        self.assertNotIn("stdout", invalid)

    def test_codex_projection_marks_file_mtime_confidence_and_unknown_identity(self) -> None:
        value = {
            "available": True,
            "observed_at": "2030-01-02T03:59:00Z",
            "primary": {"window_minutes": 300, "remaining_percent": 60, "resets_at_iso": "2030-01-02T05:00:00Z"},
        }
        projected = project_native_codex_cached_rollout(value, generated_at=NOW)
        self.assertEqual(projected["resource"]["quota_identity"]["status"], "unknown")
        self.assertEqual(projected["windows"][0]["source"]["confidence"], "source_file_mtime")

        missing = project_native_codex_cached_rollout(
            {"available": True, "observed_at": "2030-01-02T03:59:00Z", "primary": {"window_minutes": 300}},
            generated_at=NOW,
        )
        self.assertEqual(missing["windows"][0]["remaining"]["status"], "unknown")

    def test_antigravity_is_unavailable_only(self) -> None:
        projected = antigravity_unavailable_snapshot(generated_at=NOW)
        self.assertEqual(projected["windows"], [])
        self.assertEqual(projected["diagnostics"][0]["code"], "resource_capability_unavailable")


class ProviderResourceMappingTests(unittest.TestCase):
    def test_one_target_one_identity_and_many_targets_shared_identity(self) -> None:
        inventory = {"status": "current", "snapshot_id": "inventory-a", "targets": {"target-a": {}, "target-b": {}}}
        value = validate_mapping(mapping(binding(), binding("target-b", binding_id="binding-b")))
        preview = mapping_preview(value, inventory, evaluated_at=NOW)
        self.assertEqual([row["status"] for row in preview["targets"]], ["mapped", "mapped"])
        self.assertEqual(preview["pool_projection"][0]["quota_identity_count"], 1)

    def test_one_pool_multiple_identities_has_no_pool_summary(self) -> None:
        inventory = {"status": "current", "snapshot_id": "inventory-a", "targets": {"target-a": {}, "target-b": {}}}
        value = validate_mapping(mapping(binding(), binding("target-b", binding_id="binding-b", quota_id="quota-b")))
        preview = mapping_preview(value, inventory, evaluated_at=NOW)
        self.assertEqual(preview["pool_projection"][0]["status"], "multiple_quota_identities")
        self.assertFalse(preview["pool_projection"][0]["provider_resource_summary_allowed"])

    def test_missing_stale_and_ambiguous_mapping(self) -> None:
        inventory = {"status": "current", "snapshot_id": "inventory-a", "targets": {"target-a": {}, "target-b": {}}}
        missing = mapping_preview(validate_mapping(mapping(binding())), inventory, evaluated_at=NOW)
        self.assertEqual({row["target_id"]: row["status"] for row in missing["targets"]}["target-b"], "missing")
        stale = mapping_preview(validate_mapping(mapping(binding(expires_at="2030-01-02T03:00:00Z"))), inventory, evaluated_at=NOW)
        self.assertEqual(stale["targets"][0]["status"], "stale")
        ambiguous_map = mapping(binding(), binding(binding_id="binding-b", quota_id="quota-b"))
        ambiguous = mapping_preview(validate_mapping(ambiguous_map), {"status": "current", "targets": {"target-a": {}}}, evaluated_at=NOW)
        self.assertEqual(ambiguous["targets"][0]["status"], "ambiguous")

    def test_mapping_target_must_exist_in_exact_inventory(self) -> None:
        preview = mapping_preview(validate_mapping(mapping(binding())), {"status": "current", "targets": {}}, evaluated_at=NOW)
        self.assertEqual(preview["targets"][0]["status"], "invalid")
        self.assertEqual(preview["targets"][0]["reason"], "mapping_target_unknown")

    def test_stale_target_inventory_excludes_mapping(self) -> None:
        preview = mapping_preview(
            validate_mapping(mapping(binding())),
            {"status": "stale", "snapshot_id": "inventory-old", "targets": {"target-a": {}}},
            evaluated_at=NOW,
        )
        self.assertEqual(preview["targets"][0]["status"], "stale")
        self.assertEqual(preview["target_inventory_snapshot_id"], "inventory-old")


class ProviderResourceReportCliTests(unittest.TestCase):
    def test_report_separates_local_capacity_and_provider_resource_and_cli_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(root=root)
            config = replace(
                config,
                capacity_pools={"codex": {"max_running": 2}},
                execution_target_inventory={"status": "current", "snapshot_id": "inventory-a", "targets": {"target-a": {}}},
            )
            report = build_provider_resource_report(
                config,
                snapshots=[snapshot()],
                mapping=validate_mapping(mapping(binding())),
                evaluated_at=NOW,
                max_age_seconds=300,
            )
            self.assertFalse(report["local_capacity"]["provider_quota"])
            self.assertTrue(report["mapping_preview"]["targets"][0]["resource_aware_candidate"])
            self.assertNotIn("authority_preview", report)

            config_path = root / "config.json"
            config_path.write_text(json.dumps({"root": str(root), "capacity_pools": {"codex": {"max_running": 2}}}), encoding="utf-8")
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot()), encoding="utf-8")
            mapping_path = root / "mapping.json"
            mapping_path.write_text(json.dumps(mapping(binding())), encoding="utf-8")
            before_paths = {path.relative_to(root) for path in root.rglob("*")}
            for json_mode in (False, True):
                argv = ["--config", str(config_path), "provider-resource-report", "--snapshot-json", str(snapshot_path), "--mapping-json", str(mapping_path), "--max-age-seconds", "300", "--evaluated-at", NOW.isoformat()]
                if json_mode:
                    argv.append("--json")
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main(argv)
                self.assertEqual(code, 0)
                if json_mode:
                    self.assertEqual(json.loads(stdout.getvalue())["contract"], "provider-resource-report-v1")
                else:
                    self.assertIn("local capacity (scheduler admission; not provider quota)", stdout.getvalue())
            self.assertEqual(before_paths, {path.relative_to(root) for path in root.rglob("*")})

    def test_report_rejects_future_generated_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            value = snapshot()
            value["generated_at"] = "2030-01-02T04:02:00Z"
            value["windows"][0]["observed_at"] = "2030-01-02T04:00:00Z"
            with self.assertRaisesRegex(ProviderResourceValidationError, "future"):
                build_provider_resource_report(config, snapshots=[value], evaluated_at=NOW)

    def test_duplicate_resource_observations_are_not_candidates_or_summed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(Config.load(root=Path(tmp)), execution_target_inventory={"status": "current", "targets": {"target-a": {}}})
            second = snapshot()
            second["snapshot_id"] = "snapshot-b"
            report = build_provider_resource_report(
                config,
                snapshots=[snapshot(), second],
                mapping=validate_mapping(mapping(binding())),
                evaluated_at=NOW,
                max_age_seconds=300,
            )
            self.assertEqual(report["summary"]["ambiguous_resource_count"], 1)
            self.assertFalse(report["mapping_preview"]["targets"][0]["resource_aware_candidate"])


if __name__ == "__main__":
    unittest.main()

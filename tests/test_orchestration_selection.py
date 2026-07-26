from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.orchestration import validate_manifest
from codex_batch_runner.orchestration_selection import (
    POLICY_REVISION,
    OrchestrationSelectionError,
    SelectionReceiptConflict,
    apply_selection_record,
    build_selection_apply_preview,
    build_selection_preview,
    stable_digest,
    validate_selection_preview,
)


EVALUATED_AT = "2026-07-26T11:30:00+09:00"


def manifest(**changes: object) -> dict:
    value = {
        "schema_version": 1,
        "contract": "orchestration-intake-v1",
        "request_id": "sample-request",
        "idempotency_key": "sample-key",
        "source": {
            "kind": "codex_parent_thread",
            "collection_owner": "source_parent",
        },
        "summary": {
            "root_goal": "Sanitized goal",
            "requirement": "Sanitized requirement",
            "stop_condition": "Sanitized stop",
            "done_means": "Sanitized done",
        },
        "authority": {
            "decision_authority": "delegated_decision",
            "resolution": "resolved",
            "impact": "low",
            "approval_state": "not_required",
        },
        "work": {
            "kind": "implementation",
            "interaction": "none",
            "duration": "short",
            "persistence": "turn_bound",
            "resume": "not_needed",
            "dependency": "none",
            "collection": "immediate_parent",
            "context": "self_contained",
            "isolation": "worktree",
            "verification": "objective",
            "external_worker_boundary": "unavailable",
            "repository_scope": "present",
        },
        "mutation": {
            "allowed": ["tracked_files"],
            "prohibited": ["runtime_state", "external_state", "destructive"],
        },
        "automation_boundary": "advisory_only",
        "surface_preferences": [
            "codex_subagent",
            "cbr_batch",
            "codex_parent_thread",
            "external_worker",
            "codex_user_owned_thread",
        ],
    }
    value.update(changes)
    return validate_manifest(value)


def preview(
    value: dict | None = None,
    *,
    selected_surface: str | None = "codex_subagent",
    override: object | None = None,
    source_contract_digest: str | None = None,
    policy_revision: str = POLICY_REVISION,
) -> dict:
    current = value or manifest()
    return build_selection_preview(
        current,
        selected_surface=selected_surface,
        source_contract_digest=source_contract_digest or stable_digest(current),
        policy_revision=policy_revision,
        evaluated_at=EVALUATED_AT,
        override=override,
    )


def bound_override(value: dict, selected_surface: str = "codex_parent_thread") -> dict:
    base = preview(value)
    return {
        "actor_kind": "coordinator",
        "authority": "delegated_decision",
        "reason_code": "bounded_authorized_choice",
        "scope": {
            "request_fingerprint": base["decision"]["request_fingerprint"],
            "policy_revision": POLICY_REVISION,
            "selected_surface": selected_surface,
        },
        "expires_at": "2026-07-26T12:30:00+09:00",
    }


class OrchestrationSelectionTests(unittest.TestCase):
    def test_recommended_selection_is_report_only_and_deterministic(self) -> None:
        first = preview()
        second = preview()
        self.assertEqual(first, second)
        decision = first["decision"]
        self.assertEqual("recorded", decision["decision_status"])
        self.assertEqual(
            ["selected_recommended_surface"], decision["selection_reason_codes"]
        )
        self.assertFalse(decision["would_warn"])
        self.assertFalse(decision["mutation"])
        self.assertEqual(
            ["codex_subagent", "codex_parent_thread"], decision["eligible"]
        )
        self.assertEqual(
            [item["surface"] for item in decision["eligibility_snapshot"]],
            decision["candidates"],
        )
        self.assertTrue(
            all(item["evaluated"] for item in decision["eligibility_snapshot"])
        )

    def test_authorized_override_and_advisory_mismatch_matrix(self) -> None:
        value = manifest()
        accepted = preview(
            value,
            selected_surface="codex_parent_thread",
            override=bound_override(value),
        )["decision"]
        self.assertEqual("recorded", accepted["decision_status"])
        self.assertFalse(accepted["would_warn"])
        self.assertEqual(
            ["selected_authorized_override"],
            accepted["selection_reason_codes"],
        )

        missing = preview(value, selected_surface=None)["decision"]
        self.assertEqual(
            ("missing", None), (missing["decision_status"], missing["would_warn"])
        )
        self.assertEqual(["selection_missing"], missing["selection_reason_codes"])

        not_candidate_value = manifest(
            surface_preferences=["codex_subagent", "codex_parent_thread"]
        )
        noncandidate = preview(not_candidate_value, selected_surface="cbr_batch")[
            "decision"
        ]
        self.assertEqual(
            ("mismatch", True),
            (noncandidate["decision_status"], noncandidate["would_warn"]),
        )
        self.assertEqual(
            ["selected_surface_not_candidate"],
            noncandidate["selection_reason_codes"],
        )

        ineligible = preview(value, selected_surface="cbr_batch")["decision"]
        self.assertEqual(
            ("mismatch", True),
            (ineligible["decision_status"], ineligible["would_warn"]),
        )
        self.assertEqual(
            ["selected_surface_ineligible"],
            ineligible["selection_reason_codes"],
        )

    def test_invalid_override_source_and_policy_fail_closed(self) -> None:
        value = manifest()
        override = bound_override(value)
        override["scope"]["selected_surface"] = "cbr_batch"
        invalid = preview(
            value,
            selected_surface="codex_parent_thread",
            override=override,
        )["decision"]
        self.assertEqual(
            ("invalid", None), (invalid["decision_status"], invalid["would_warn"])
        )
        self.assertEqual(
            ["selected_without_valid_override", "override_scope_mismatch"],
            invalid["selection_reason_codes"],
        )
        self.assertIsNone(invalid["override"])

        wrong_source = preview(
            value,
            source_contract_digest="sha256:" + "0" * 64,
        )["decision"]
        self.assertEqual(
            ("invalid", None),
            (wrong_source["decision_status"], wrong_source["would_warn"]),
        )
        self.assertIn("source_binding_mismatch", wrong_source["selection_reason_codes"])

        wrong_policy = preview(value, policy_revision="other-policy-v1")["decision"]
        self.assertEqual(
            ("invalid", None),
            (wrong_policy["decision_status"], wrong_policy["would_warn"]),
        )
        self.assertIn(
            "policy_revision_mismatch", wrong_policy["selection_reason_codes"]
        )

    def test_expired_or_insufficient_override_is_not_persisted(self) -> None:
        value = manifest()
        expired = bound_override(value)
        expired["expires_at"] = "2026-07-26T10:30:00+09:00"
        decision = preview(
            value,
            selected_surface="codex_parent_thread",
            override=expired,
        )["decision"]
        self.assertEqual("invalid", decision["decision_status"])
        self.assertIn("override_expired", decision["selection_reason_codes"])
        self.assertIsNone(decision["override"])

        insufficient = bound_override(value)
        insufficient["authority"] = "advisory"
        decision = preview(
            value,
            selected_surface="codex_parent_thread",
            override=insufficient,
        )["decision"]
        self.assertIn(
            "override_authority_insufficient",
            decision["selection_reason_codes"],
        )
        self.assertIsNone(decision["override"])

    def test_source_cannot_manufacture_different_override_authority(self) -> None:
        value = manifest(
            authority={
                "decision_authority": "bounded_experiment",
                "resolution": "resolved",
                "impact": "low",
                "approval_state": "not_required",
            }
        )
        override = bound_override(value)
        decision = preview(
            value,
            selected_surface="codex_parent_thread",
            override=override,
        )["decision"]
        self.assertEqual(("invalid", None), (decision["decision_status"], decision["would_warn"]))
        self.assertIn(
            "override_authority_insufficient",
            decision["selection_reason_codes"],
        )
        self.assertIsNone(decision["override"])

    def test_early_blocked_plan_preserves_unevaluated_eligibility(self) -> None:
        cases = [
            manifest(
                authority={
                    "decision_authority": "recommend_and_pause",
                    "resolution": "needs_user_decision",
                    "impact": "low",
                    "approval_state": "not_required",
                },
                mutation={
                    "allowed": ["local_files"],
                    "prohibited": [
                        "runtime_state",
                        "external_state",
                        "destructive",
                    ],
                },
            ),
            manifest(
                authority={
                    "decision_authority": "delegated_decision",
                    "resolution": "blocked_external",
                    "impact": "low",
                    "approval_state": "not_required",
                },
                work={
                    **manifest()["work"],
                    "interaction": "external_required",
                },
            ),
        ]
        for value in cases:
            with self.subTest(resolution=value["authority"]["resolution"]):
                decision = preview(value)["decision"]
                self.assertEqual("blocked", decision["decision_status"])
                self.assertEqual([], decision["eligible"])
                self.assertTrue(
                    all(
                        item["evaluated"] is False
                        and item["eligible"] is None
                        and item["reason_codes"] == []
                        for item in decision["eligibility_snapshot"]
                    )
                )

    def test_no_eligible_surface_preserves_evaluated_exclusion(self) -> None:
        value = manifest(surface_preferences=["cbr_batch"])
        decision = preview(value, selected_surface="cbr_batch")["decision"]
        self.assertEqual("blocked", decision["decision_status"])
        self.assertEqual([], decision["eligible"])
        self.assertEqual(
            [
                {
                    "surface": "cbr_batch",
                    "evaluated": True,
                    "eligible": False,
                    "reason_codes": ["persistence_incompatible"],
                }
            ],
            decision["eligibility_snapshot"],
        )

    def test_all_canonical_surfaces_can_be_recorded(self) -> None:
        cases = {
            "codex_parent_thread": manifest(
                surface_preferences=["codex_parent_thread"]
            ),
            "codex_user_owned_thread": manifest(
                work={**manifest()["work"], "interaction": "user_required"},
                surface_preferences=["codex_user_owned_thread"],
            ),
            "codex_subagent": manifest(surface_preferences=["codex_subagent"]),
            "cbr_batch": manifest(
                work={**manifest()["work"], "duration": "long"},
                surface_preferences=["cbr_batch"],
            ),
            "external_worker": manifest(
                work={
                    **manifest()["work"],
                    "external_worker_boundary": "verified_bounded",
                },
                surface_preferences=["external_worker"],
            ),
        }
        for surface, value in cases.items():
            with self.subTest(surface=surface):
                decision = preview(value, selected_surface=surface)["decision"]
                self.assertEqual("recorded", decision["decision_status"])
                self.assertEqual(surface, decision["recommended_surface"])

    def test_preview_validation_rejects_tampering_and_private_content(self) -> None:
        report = preview()
        tampered = json.loads(json.dumps(report))
        tampered["decision"]["selected_surface"] = "codex_parent_thread"
        with self.assertRaises(OrchestrationSelectionError):
            validate_selection_preview(tampered)

        private = json.loads(json.dumps(report))
        private["decision"]["collection_owner"] = "/Users/private"
        private["decision"]["decision_id"] = stable_digest(
            {
                key: item
                for key, item in private["decision"].items()
                if key not in {"decision_id", "evaluated_at"}
            }
        )
        private["preview_digest"] = stable_digest(
            {key: item for key, item in private.items() if key != "preview_digest"}
        )
        with self.assertRaises(OrchestrationSelectionError):
            validate_selection_preview(private)

    def test_apply_preview_does_not_create_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            report = build_selection_apply_preview(
                config, preview(), manifest()
            )
            self.assertEqual("ready", report["status"])
            self.assertFalse(
                (config.log_dir.parent / "orchestration-selection-receipts").exists()
            )
            self.assertFalse(config.event_dir.exists())

    def test_rehashed_forged_d1_preview_cannot_be_recorded(self) -> None:
        value = manifest()
        forged = json.loads(json.dumps(preview(value)))
        decision = forged["decision"]
        decision["recommended_surface"] = "codex_parent_thread"
        decision["selected_surface"] = "codex_parent_thread"
        decision["recommendation_reason_codes"] = ["selected_parent_thread"]
        decision["selection_reason_codes"] = ["selected_recommended_surface"]
        decision["decision_status"] = "recorded"
        decision["would_warn"] = False
        decision["decision_id"] = stable_digest(
            {
                key: item
                for key, item in decision.items()
                if key not in {"decision_id", "evaluated_at"}
            }
        )
        forged["preview_digest"] = stable_digest(
            {
                key: item
                for key, item in forged.items()
                if key != "preview_digest"
            }
        )
        validate_selection_preview(forged)
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            with self.assertRaisesRegex(
                OrchestrationSelectionError, "canonical D1"
            ):
                apply_selection_record(config, forged, value)
            self.assertFalse(config.event_dir.exists())

    def test_apply_is_private_idempotent_and_repairs_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            report = preview()
            receipt = apply_selection_record(config, report, manifest())
            receipt_dir = config.log_dir.parent / "orchestration-selection-receipts"
            receipt_path = next(receipt_dir.glob("*.json"))
            self.assertEqual(0o700, stat.S_IMODE(receipt_dir.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(receipt_path.stat().st_mode))
            self.assertEqual(
                receipt, apply_selection_record(config, report, manifest())
            )
            event_path = next(config.event_dir.glob("*.jsonl"))
            events = [json.loads(line) for line in event_path.read_text().splitlines()]
            self.assertEqual(1, len(events))
            self.assertIsNone(events[0]["task_id"])
            self.assertNotIn("request_id", json.dumps(events[0]))
            event_path.unlink()
            event_path.write_text(
                json.dumps(
                    {
                        "event_type": "orchestration_selection_recorded",
                        "source": "orchestration-selection",
                        "summary": "orchestration selection shadow recorded",
                        "payload": {
                            "decision_id": receipt["decision"]["decision_id"]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                receipt, apply_selection_record(config, report, manifest())
            )
            recovered = [
                json.loads(line) for line in event_path.read_text().splitlines()
            ]
            self.assertEqual(2, len(recovered))
            self.assertEqual(
                receipt["receipt_id"],
                recovered[-1]["payload"]["receipt_id"],
            )

    def test_filesystem_failure_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            value = manifest()
            private_path = str(Path(tmp) / "private-receipt.json")
            with patch(
                "codex_batch_runner.orchestration_selection.write_json_atomic_create",
                side_effect=OSError(private_path),
            ):
                with self.assertRaises(OrchestrationSelectionError) as raised:
                    apply_selection_record(config, preview(value), value)
            self.assertNotIn(private_path, str(raised.exception))

    def test_conflict_and_unsafe_receipt_directory_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            report = preview()
            apply_selection_record(config, report, manifest())
            receipt_path = next(
                (config.log_dir.parent / "orchestration-selection-receipts").glob(
                    "*.json"
                )
            )
            receipt_path.write_text("{}", encoding="utf-8")
            os.chmod(receipt_path, 0o600)
            with self.assertRaises(SelectionReceiptConflict):
                apply_selection_record(config, report, manifest())

        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            receipt_dir = config.log_dir.parent / "orchestration-selection-receipts"
            receipt_dir.mkdir(parents=True, mode=0o755)
            with self.assertRaises(OrchestrationSelectionError):
                build_selection_apply_preview(config, preview(), manifest())

    def test_cli_preview_is_config_independent_and_apply_requires_confirmation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(value), encoding="utf-8")
            output = io.StringIO()
            with patch(
                "codex_batch_runner.cli.Config.load",
                side_effect=AssertionError("must not load config"),
            ) as load:
                with contextlib.redirect_stdout(output):
                    code = main(
                        [
                            "orchestration",
                            "selection-preview",
                            "--manifest",
                            str(manifest_path),
                            "--source-contract-digest",
                            stable_digest(value),
                            "--selected-surface",
                            "codex_subagent",
                            "--evaluated-at",
                            EVALUATED_AT,
                            "--json",
                        ]
                    )
            self.assertEqual(0, code)
            load.assert_not_called()
            report = json.loads(output.getvalue())
            preview_path = root / "preview.json"
            preview_path.write_text(json.dumps(report), encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    2,
                    main(
                        [
                            "--config",
                            str(root / "config.json"),
                            "orchestration",
                            "selection-record",
                            "--manifest",
                            str(manifest_path),
                            "--preview",
                            str(preview_path),
                            "--apply",
                        ]
                    ),
                )
            self.assertIn("failed closed", stderr.getvalue())

            config_path = root / "config.json"
            config_path.write_text(json.dumps({"root": str(root)}), encoding="utf-8")
            decision_id = report["decision"]["decision_id"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--config",
                            str(config_path),
                            "orchestration",
                            "selection-record",
                            "--manifest",
                            str(manifest_path),
                            "--preview",
                            str(preview_path),
                            "--apply",
                            "--confirm-decision-id",
                            decision_id,
                            "--json",
                        ]
                    ),
                )


if __name__ == "__main__":
    unittest.main()

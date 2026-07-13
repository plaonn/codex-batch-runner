from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_batch_runner.codex import run_codex
from codex_batch_runner.config import Config
from codex_batch_runner.execution_evidence_v2 import build_codex_execution_evidence, evidence_view
from codex_batch_runner.execution_evidence_v3 import (
    CommandIdentityError,
    attach_execution_evidence_v3,
    build_codex_execution_evidence_v3,
    build_external_execution_evidence_v3,
    validate_external_attestation_v3,
)
from codex_batch_runner.external_json_command import run_external_json_command_task
from codex_batch_runner.model_requirements import ResolvedExecutionConfig


def requirement() -> dict:
    return {
        "schema_version": 2,
        "derivation_version": "requirement-rubric-v1",
        "revision_id": "reqrev-v3-test",
        "quality_requirements": {},
        "hard_constraints": {},
        "utility_preferences": {},
    }


def settings(*, model: str = "exact-model", reason: str = "automatic_static_non_learned") -> ResolvedExecutionConfig:
    return ResolvedExecutionConfig(
        requirement_vector=requirement(), selection_rule="execution-target-selector-v1",
        selection_reason=reason, model=model, execution_target="exact-target-v1",
        config_overrides={"model_reasoning_effort": "high"},
    )


def config(root: Path) -> Config:
    base = Config.load(root=root)
    return Config(**{
        **base.__dict__,
        "execution_target_inventory": {
            "schema_version": 1, "snapshot_id": "sha256:inventory-test", "status": "current",
            "constraint_registry_version": "constraints-v1", "targets": {},
        },
        "constraint_registry": {"schema_version": 1, "version": "constraints-v1", "constraints": {}},
    })


def task() -> dict:
    return {
        "id": "public-v3-task", "attempts": 1, "execution_backend": "codex",
        "review_policy_version": "review-v1", "review_rubric_version": "rubric-v1",
    }


class ExecutionEvidenceV3Tests(unittest.TestCase):
    def test_codex_command_mismatch_fails_before_provider_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            cfg = Config(**{**cfg.__dict__, "codex_command": ["codex", "exec", "--model", "wrong-model"]})
            with patch("codex_batch_runner.codex.subprocess.Popen") as popen:
                with self.assertRaises(CommandIdentityError) as raised:
                    run_codex(cfg, task(), "prompt", 1, execution_settings=settings())

        popen.assert_not_called()
        record = raised.exception.record
        self.assertEqual("selected_command_mismatch", record["integrity"]["status"])
        self.assertFalse(record["cohort"]["comparability"]["model_quality"])

    def test_codex_reasoning_mismatch_fails_before_provider_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            cfg = Config(**{
                **cfg.__dict__,
                "codex_command": ["codex", "exec", "-c", "model_reasoning_effort=low"],
            })
            with patch("codex_batch_runner.codex.subprocess.Popen") as popen:
                with self.assertRaises(CommandIdentityError):
                    run_codex(cfg, task(), "prompt", 1, execution_settings=settings())
        popen.assert_not_called()

    def test_provider_match_and_omission_preserve_command_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            matched = build_codex_execution_evidence_v3(
                task(), SimpleNamespace(events=[{"type": "turn.completed", "model": "exact-model"}]), settings(), cfg,
            )
            omitted = build_codex_execution_evidence_v3(task(), SimpleNamespace(events=[]), settings(), cfg)

        self.assertEqual("verified", matched["identity"]["attestation"])
        self.assertEqual("command_attributed", omitted["identity"]["attestation"])
        self.assertEqual("exact-model", omitted["identity"]["command_model"])
        self.assertTrue(omitted["cohort"]["comparability"]["model_quality"])

    def test_provider_mismatch_is_adverse_and_does_not_rewrite_command_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = build_codex_execution_evidence_v3(
                task(), SimpleNamespace(events=[{"type": "turn.completed", "model": "other-model"}]),
                settings(), config(Path(tmp)),
            )

        self.assertEqual("provider_model_mismatch", record["integrity"]["status"])
        self.assertTrue(record["integrity"]["adverse"])
        self.assertEqual("exact-model", record["identity"]["command_model"])
        self.assertEqual("other-model", record["identity"]["provider_reported_model"]["value"])

    def test_exact_versions_and_automatic_override_cohorts_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            automatic = build_codex_execution_evidence_v3(task(), SimpleNamespace(events=[]), settings(), cfg)
            override_task = {**task(), "routing_override": {"mode": "pin"}}
            override = build_codex_execution_evidence_v3(
                override_task, SimpleNamespace(events=[]), settings(reason="operator_pin"), cfg,
            )

        self.assertEqual("automatic", automatic["cohort"]["components"]["selection_cohort"])
        self.assertEqual("override", override["cohort"]["components"]["selection_cohort"])
        self.assertNotEqual(automatic["cohort"]["cohort_id"], override["cohort"]["cohort_id"])
        self.assertEqual("high", automatic["identity"]["reasoning_effort"])
        self.assertEqual("sha256:inventory-test", automatic["versions"]["inventory_snapshot_id"])
        self.assertEqual("reqrev-v3-test", automatic["versions"]["requirement_revision_id"])
        self.assertEqual("review-v1", automatic["versions"]["review_policy_version"])

    def test_v3_history_is_append_only_and_dual_reader_keeps_v3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            item = task()
            item["last_run"] = {}
            record = build_codex_execution_evidence_v3(item, SimpleNamespace(events=[]), settings(), config(Path(tmp)))
            attach_execution_evidence_v3(item, record)
            attach_execution_evidence_v3(item, record)

        self.assertEqual(1, len(item["execution_evidence_history"]))
        self.assertEqual("execution-evidence-v3", evidence_view(item)["evidence_contract_version"])

    def test_pre_execution_evidence_without_last_run_remains_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            item = task()
            cfg = config(Path(tmp))
            cfg = Config(**{**cfg.__dict__, "codex_command": ["codex", "exec", "--model", "wrong"]})
            with self.assertRaises(CommandIdentityError) as raised:
                run_codex(cfg, item, "prompt", 1, execution_settings=settings())
            attach_execution_evidence_v3(item, raised.exception.record)

        self.assertNotIn("last_run", item)
        self.assertEqual("selected_command_mismatch", evidence_view(item)["integrity"]["status"])

    def test_cli_default_v2_legacy_and_exact_v3_boundaries_do_not_merge(self) -> None:
        legacy = evidence_view(task())
        cli_default_task = {**task(), "last_run": {"resolved_execution_config": {"model_source": "cli_default"}}}
        v2 = build_codex_execution_evidence(cli_default_task, SimpleNamespace(events=[]))
        with tempfile.TemporaryDirectory() as tmp:
            exact = build_codex_execution_evidence_v3(
                task(), SimpleNamespace(events=[]), settings(), config(Path(tmp)),
            )

        self.assertEqual("legacy-v1", legacy["evidence_contract_version"])
        self.assertEqual("execution-evidence-v2", v2["evidence_contract_version"])
        self.assertEqual("execution-evidence-v3", exact["evidence_contract_version"])
        self.assertNotEqual(v2["cohort"]["cohort_id"], exact["cohort"]["cohort_id"])

    def test_external_wrapper_v3_attestation_is_optional_and_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            item = {
                **task(), "execution_backend": "external-json-command",
                "worker_command_model": "exact-model", "worker_reasoning_effort": "high",
            }
            omitted = build_external_execution_evidence_v3(item, None, settings(), cfg)
            mismatch = build_external_execution_evidence_v3(
                item,
                {"schema_version": 3, "capability": "provider-model+usage-attestation", "provider_reported_model": "other-model"},
                settings(), cfg,
            )

        self.assertEqual("command_attributed", omitted["identity"]["attestation"])
        self.assertEqual("provider_model_mismatch", mismatch["integrity"]["status"])
        with self.assertRaisesRegex(ValueError, "unsupported key"):
            validate_external_attestation_v3({
                "schema_version": 3, "capability": "provider-model+usage-attestation",
                "provider_reported_model": "exact-model", "session_id": "private",
            })

    def test_external_command_identity_is_checked_before_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            item = {
                **task(), "external_command": ["wrapper", "{model}", "{reasoning_effort}"],
                "worker_command_model": "wrong-model", "worker_reasoning_effort": "high",
            }
            with patch("codex_batch_runner.external_json_command.subprocess.run") as run:
                with self.assertRaises(CommandIdentityError):
                    run_external_json_command_task(cfg, item, "prompt", 1, execution_settings=settings())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

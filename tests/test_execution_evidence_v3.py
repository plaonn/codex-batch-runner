from __future__ import annotations

import tempfile
import unittest
import copy
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from codex_batch_runner.codex import run_codex
from codex_batch_runner.config import Config
from codex_batch_runner.execution_evidence_v2 import build_codex_execution_evidence, evidence_view, reporting_evidence_view
from codex_batch_runner.evaluation import derive_evaluation_row
from codex_batch_runner.routing_cost_evidence import build_routing_cost_evidence
from codex_batch_runner.execution_report import task_execution_row
from codex_batch_runner.execution_evidence_v3 import (
    CommandIdentityError,
    attach_execution_evidence_v3,
    build_codex_execution_evidence_v3,
    build_external_execution_evidence_v3,
    validate_execution_evidence_v3,
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
        selected_target_snapshot={
            "target_id": "exact-target-v1",
            "target": {
                "target_id": "exact-target-v1", "execution_surface": "external",
                "execution_backend": "external-json-command",
                "external_command": ["wrapper", "{model}", "{reasoning_effort}"],
                "model": model, "command_model": model, "reasoning_effort": "high",
            },
            "inventory_schema_version": 1,
            "inventory_snapshot_id": "sha256:inventory-test",
            "constraint_registry_version": "constraints-v1",
            "selection_policy_version": "execution-target-selector-v1",
        },
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
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict("os.environ", {"CBR_CONFIG": ""}, clear=False)
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

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

        item = task()
        item["last_run"] = {"resolved_execution_config": {"model": "exact-model", "reasoning_effort": "high"}}
        attach_execution_evidence_v3(item, record)
        view = reporting_evidence_view(item)
        row = derive_evaluation_row(item)
        self.assertEqual("exact-model", view["identity"]["selected_model"])
        self.assertEqual("exact-model", view["identity"]["command_model"])
        self.assertEqual("other-model", view["identity"]["provider_reported_model"]["value"])
        self.assertEqual("provider_model_mismatch", row["worker"]["identity_integrity_status"])
        self.assertTrue(row["worker"]["identity_adverse"])

        cost = build_routing_cost_evidence(item, attribution_class="unavailable")
        self.assertEqual("routing-cost-evidence-v2", cost["evidence_contract_version"])
        self.assertFalse(cost["cohort"]["comparability"]["quality"])
        self.assertIn("provider_model_mismatch", cost["cohort"]["exclusion_reasons"])

        report_row = task_execution_row(config(Path.cwd()), item)
        self.assertEqual("exact-model", report_row["identity"]["selected_model"])
        self.assertEqual("exact-model", report_row["identity"]["command_model"])
        self.assertEqual("other-model", report_row["identity"]["provider_reported_model"]["value"])
        self.assertEqual("provider_model_mismatch", report_row["evidence"]["integrity"]["status"])

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
            command = ["wrapper", "exact-model", "high"]
            omitted = build_external_execution_evidence_v3(item, None, settings(), cfg, command=command)
            mismatch = build_external_execution_evidence_v3(
                item,
                {"schema_version": 3, "capability": "provider-model+usage-attestation", "provider_reported_model": "other-model"},
                settings(), cfg, command=command,
            )

        self.assertEqual("command_attributed", omitted["identity"]["attestation"])
        self.assertEqual("provider_model_mismatch", mismatch["integrity"]["status"])
        with self.assertRaisesRegex(ValueError, "unsupported key"):
            validate_external_attestation_v3({
                "schema_version": 3, "capability": "provider-model+usage-attestation",
                "provider_reported_model": "exact-model", "session_id": "private",
            })

    def test_external_exact_target_snapshot_overrides_mutable_task_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            item = {
                **task(), "external_command": ["mutated-wrapper", "wrong-model", "low"],
                "worker_command_model": "wrong-model", "worker_reasoning_effort": "high",
            }
            completed = SimpleNamespace(stdout="", stderr="", returncode=0)
            with patch("codex_batch_runner.external_json_command.subprocess.run", return_value=completed) as run:
                run_external_json_command_task(cfg, item, "prompt", 1, execution_settings=settings())
        self.assertEqual(["wrapper", "exact-model", "high", "prompt"], run.call_args.args[0])

    def test_external_duplicate_identity_fails_before_subprocess(self) -> None:
        broken = copy.deepcopy(settings().selected_target_snapshot)
        broken["target"]["external_command"] = [
            "wrapper", "{model}", "{model}", "{reasoning_effort}",
        ]
        selected = replace(settings(), selected_target_snapshot=broken)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("codex_batch_runner.external_json_command.subprocess.run") as run:
                with self.assertRaises(CommandIdentityError):
                    run_external_json_command_task(
                        config(Path(tmp)), task(), "prompt", 1, execution_settings=selected,
                    )
        run.assert_not_called()

    def test_external_mutated_template_without_placeholders_fails_before_subprocess(self) -> None:
        broken = copy.deepcopy(settings().selected_target_snapshot)
        broken["target"]["external_command"] = ["wrapper", "exact-model", "high"]
        selected = replace(settings(), selected_target_snapshot=broken)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("codex_batch_runner.external_json_command.subprocess.run") as run:
                with self.assertRaises(CommandIdentityError):
                    run_external_json_command_task(
                        config(Path(tmp)), task(), "prompt", 1, execution_settings=selected,
                    )
        run.assert_not_called()

    def test_validator_rejects_tampered_derived_identity_and_cohort_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = build_codex_execution_evidence_v3(
                task(), SimpleNamespace(events=[]), settings(), config(Path(tmp)),
            )
        mutations = (
            ("integrity adverse", lambda item: item["integrity"].update(adverse=True)),
            ("attestation", lambda item: item["identity"].update(attestation="verified")),
            ("comparability", lambda item: item["cohort"]["comparability"].update(model_quality=False)),
            ("cohort id", lambda item: item["cohort"].update(cohort_id="sha256:tampered")),
            ("components", lambda item: item["cohort"]["components"].update(review_policy_version="other")),
            ("exclusions", lambda item: item["cohort"].update(exclusion_reasons=["tampered"])),
            ("selection", lambda item: item["routing"].update(selection_cohort="override")),
            ("raw capture", lambda item: item["capture"].update(raw_provider_output_included=True)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                tampered = copy.deepcopy(record)
                mutate(tampered)
                with self.assertRaises(ValueError):
                    validate_execution_evidence_v3(tampered)

    def test_validator_rejects_selected_command_provider_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = build_codex_execution_evidence_v3(
                task(), SimpleNamespace(events=[]), settings(), config(Path(tmp)),
            )
        for field, value in (("selected_model", "other"), ("command_model", "other")):
            with self.subTest(field=field):
                tampered = copy.deepcopy(record)
                tampered["identity"][field] = value
                with self.assertRaises(ValueError):
                    validate_execution_evidence_v3(tampered)
        tampered = copy.deepcopy(record)
        tampered["identity"]["provider_reported_model"].update(status="observed", value="other")
        with self.assertRaises(ValueError):
            validate_execution_evidence_v3(tampered)


if __name__ == "__main__":
    unittest.main()

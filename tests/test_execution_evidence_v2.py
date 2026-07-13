from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_batch_runner.config import Config
from codex_batch_runner.execution_evidence import derive_execution_evidence_rows, load_execution_evidence_records
from codex_batch_runner.execution_evidence_v2 import (
    ExecutionEvidenceV2Error,
    attach_execution_evidence,
    build_codex_execution_evidence,
    build_external_execution_evidence,
    evidence_view,
    validate_execution_evidence_v2,
)
from codex_batch_runner.execution_report import task_execution_row
from codex_batch_runner.evaluation import derive_evaluation_row
from codex_batch_runner.queue import create_task
from codex_batch_runner.routing_report import summarize_evaluation_diagnostics


class ExecutionEvidenceV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict("os.environ", {"CBR_CONFIG": ""}, clear=False)
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

    def test_codex_capture_records_provider_observed_model_and_usage(self) -> None:
        task = task_fixture()
        result = SimpleNamespace(
            events=[
                {"type": "turn.started", "model": "must-not-count"},
                {
                    "type": "turn.completed",
                    "model": "provider-model-1",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 20,
                    },
                },
            ]
        )

        evidence = build_codex_execution_evidence(task, result)

        self.assertEqual(2, evidence["schema_version"])
        self.assertEqual("provider-model-1", evidence["actual_model"]["value"])
        self.assertEqual("provider_observed", evidence["actual_model"]["confidence"])
        self.assertEqual(120, evidence["token_usage"]["values"]["known_total_tokens"])
        self.assertTrue(evidence["cohort"]["comparability"]["model_quality"])
        self.assertTrue(evidence["cohort"]["comparability"]["token_cost"])
        self.assertFalse(evidence["cohort"]["comparability"]["monetary_cost"])

    def test_codex_capture_does_not_infer_model_from_untrusted_nested_content(self) -> None:
        task = task_fixture()
        result = SimpleNamespace(
            events=[
                {"type": "assistant.message", "model": "message-claim"},
                {"type": "turn.completed", "item": {"model": "nested-claim"}, "usage": {"output_tokens": 3}},
            ]
        )

        evidence = build_codex_execution_evidence(task, result)

        self.assertEqual("unavailable", evidence["actual_model"]["status"])
        self.assertIsNone(evidence["actual_model"]["value"])
        self.assertEqual("model_not_exposed_by_provider_output", evidence["actual_model"]["availability_reason"])
        self.assertFalse(evidence["cohort"]["comparability"]["model_quality"])
        self.assertTrue(evidence["cohort"]["comparability"]["token_cost"])

    def test_external_attestation_is_optional_and_never_falls_back_to_model_group(self) -> None:
        task = task_fixture(backend="external-json-command")
        task["last_run"]["resolved_worker_target"] = {
            "worker_family": "external-worker",
            "model_group": "planned-group",
        }

        unavailable = build_external_execution_evidence(task, None)
        attested = build_external_execution_evidence(
            task,
            {
                "schema_version": 2,
                "capability": "actual-model+usage-attestation",
                "actual_model": "wrapper-observed-model",
                "token_usage": {"input_tokens": 7, "output_tokens": 2},
            },
        )

        self.assertEqual("unavailable", unavailable["actual_model"]["status"])
        self.assertNotEqual("planned-group", unavailable["actual_model"]["value"])
        self.assertEqual("wrapper-observed-model", attested["actual_model"]["value"])
        self.assertEqual("wrapper_attested", attested["actual_model"]["confidence"])
        self.assertEqual(9, attested["token_usage"]["values"]["known_total_tokens"])

    def test_history_is_append_only_and_legacy_is_non_comparable(self) -> None:
        task = task_fixture()
        legacy = evidence_view(task)
        first = build_codex_execution_evidence(task, SimpleNamespace(events=[]))
        attach_execution_evidence(task, first)
        task["attempts"] = 2
        second = build_codex_execution_evidence(task, SimpleNamespace(events=[]))
        attach_execution_evidence(task, second)

        self.assertEqual("legacy-v1", legacy["evidence_contract_version"])
        self.assertFalse(legacy["cohort"]["comparability"]["model_quality"])
        self.assertEqual(2, len(task["execution_evidence_history"]))
        self.assertEqual(second["evidence_id"], evidence_view(task)["evidence_id"])

    def test_validator_rejects_private_identifiers_anywhere_in_v2_record(self) -> None:
        evidence = build_codex_execution_evidence(task_fixture(), SimpleNamespace(events=[]))
        evidence["capture"]["session_id"] = "private-session"

        with self.assertRaisesRegex(ExecutionEvidenceV2Error, "forbidden key: session_id"):
            validate_execution_evidence_v2(evidence)

    def test_execution_report_separates_planned_and_actual_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            task = create_task(config, "public-safe work", tmp, task_id="evidence-v2-report")
            task["status"] = "completed"
            task["attempts"] = 1
            task["last_run"] = {
                "execution_backend": "codex",
                "resolved_execution_config": {
                    "model": "planned-model",
                    "model_source": "explicit_model",
                    "selection_rule": "planned-rule",
                },
            }
            evidence = build_codex_execution_evidence(
                task,
                SimpleNamespace(events=[{"type": "turn.completed", "model": "actual-model", "usage": {"output_tokens": 2}}]),
            )
            attach_execution_evidence(task, evidence)

            row = task_execution_row(config, task)

        self.assertEqual("planned_execution", row["model"]["identity_kind"])
        self.assertEqual("planned-model", row["model"]["model"])
        self.assertEqual("actual-model", row["actual_model"]["value"])
        self.assertEqual("execution-evidence-v2", row["evidence"]["evidence_contract_version"])
        self.assertEqual("codex_jsonl", row["token_usage_source"])

    def test_routing_diagnostics_stratify_v2_and_legacy_cohorts(self) -> None:
        legacy_task = task_fixture()
        legacy_task.update({"status": "completed", "review_status": "accepted"})
        v2_task = task_fixture()
        v2_task.update({"id": "v2-task", "status": "completed", "review_status": "accepted"})
        evidence = build_codex_execution_evidence(
            v2_task,
            SimpleNamespace(events=[{"type": "turn.completed", "model": "actual-model", "usage": {"output_tokens": 2}}]),
        )
        attach_execution_evidence(v2_task, evidence)

        diagnostics = summarize_evaluation_diagnostics(
            [derive_evaluation_row(legacy_task), derive_evaluation_row(v2_task)]
        )
        contracts = {entry["key"]: entry for entry in diagnostics["evidence_contracts"]}
        cohorts = {entry["key"]: entry for entry in diagnostics["evidence_cohorts"]}

        self.assertEqual(1, contracts["legacy-v1"]["legacy_rows"])
        self.assertEqual(0, contracts["legacy-v1"]["model_quality_comparable"])
        self.assertEqual(1, contracts["execution-evidence-v2"]["v2_rows"])
        self.assertEqual(1, contracts["execution-evidence-v2"]["model_quality_comparable"])
        self.assertIn("legacy-v1-non-comparable", cohorts)
        self.assertIn(evidence["cohort"]["cohort_id"], cohorts)

    def test_checked_in_v2_fixture_dual_reads_as_separate_supplemental_row(self) -> None:
        fixture = Path(__file__).parents[1] / "examples" / "execution-evidence-v2.example.json"
        records = load_execution_evidence_records([str(fixture)])
        records[0]["session_id"] = "private-session-value"
        records[0]["thread_id"] = "private-thread-value"
        rows = derive_execution_evidence_rows(records)

        self.assertEqual(1, len(rows))
        self.assertEqual("execution-evidence-v2", rows[0]["execution_evidence"]["evidence_contract_version"])
        self.assertEqual("example-observed-model", rows[0]["worker"]["actual_model"])
        serialized = json.dumps(rows, sort_keys=True)
        self.assertNotIn("private-session-value", serialized)
        self.assertNotIn("private-thread-value", serialized)


def task_fixture(*, backend: str = "codex") -> dict:
    return {
        "id": "public-task-id",
        "attempts": 1,
        "execution_backend": backend,
        "routing_experiment": "baseline",
        "review_policy_version": "review-v1",
        "last_run": {
            "execution_backend": backend,
            "resolved_execution_config": {
                "selection_rule": "balanced",
                "execution_target": "balanced-current",
                "model_source": "cli_default",
                "model_requirement_vector": {"source": "routing-metadata-v1"},
            },
        },
    }


if __name__ == "__main__":
    unittest.main()

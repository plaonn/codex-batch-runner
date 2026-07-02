from __future__ import annotations

import json
import unittest

from codex_batch_runner.evaluation import derive_evaluation_row


def base_task(**overrides: object) -> dict:
    task = {
        "id": "task-2026-06-25T170111-447743Z0000",
        "title": "Implement evaluation helper",
        "prompt": "Private prompt text must not be emitted.",
        "description": "Synthetic description",
        "project_id": "sample-project",
        "category": "Implementation",
        "labels": ["routing", "telemetry"],
        "routing_size": "Small",
        "routing_risk": "Medium",
        "verification_scope": ["unit"],
        "routing_risk_factors": ["public-tests"],
        "execution_backend": "codex",
        "model_requirement_vector": {
            "dimensions": {
                "reasoning_depth": "medium",
                "context_need": "medium",
                "tool_reliability": "medium",
                "latency_priority": "medium",
                "cost_sensitivity": "medium",
                "review_strictness": "medium",
            }
        },
        "capacity_pool": "default",
        "cwd": "/Users/example/private/project",
        "project_root": "/Users/example/private/project",
        "attempts": 1,
        "run_count": 1,
        "last_run": {
            "resolved_execution_config": {
                "selection_rule": "standard",
                "model": "model-name-not-returned",
                "model_source": "explicit_model",
                "execution_target": "local",
            },
            "duration_seconds": 125,
            "log_path": "/Users/example/private/log.jsonl",
        },
        "last_result": {
            "task_id": "task-2026-06-25T170111-447743Z0000",
            "status": "completed",
            "summary": "Raw summary can include /Users/example/private/file.txt",
            "changed_files": ["src/example.py"],
            "verification": ["python -m unittest tests.test_example"],
            "commits": ["abc123"],
        },
    }
    task.update(overrides)
    return task


class EvaluationRowTests(unittest.TestCase):
    def test_accepted_applied_keeps_worker_reviewer_vector_and_fingerprint_separate(self) -> None:
        row = derive_evaluation_row(
            base_task(
                status="completed",
                review_status="accepted",
                execution_apply_status="applied",
                anchor_review=True,
                reviewer_codex={
                    "decision": "pass",
                    "confidence": "high",
                    "findings": [],
                    "required_human_checks": [],
                },
            )
        )

        self.assertEqual("evaluation-row-v1", row["derivation_version"])
        self.assertIn("request_fingerprint", row)
        self.assertIn("task_vector", row)
        self.assertIn("worker", row)
        self.assertIn("reviewer", row)
        self.assertIn("objective_checks", row)
        self.assertIn("task_vector_evaluation", row)
        self.assertEqual("accepted", row["outcomes"]["review_status"])
        self.assertTrue(row["outcomes"]["applied"])
        self.assertTrue(row["objective_checks"]["required_checks_passed"])
        self.assertEqual([], row["exclusion_reasons"])
        self.assertTrue(row["policy_usage"]["usable_for_worker_policy"])
        self.assertIn(row["task_bucket_key"], row["experiment_cell_key"])
        self.assertEqual("explicit_model", row["worker"]["model_source"])
        self.assertEqual("local", row["worker"]["execution_target"])

    def test_unreviewed_completed_is_not_worker_policy_usable(self) -> None:
        row = derive_evaluation_row(base_task(status="completed", review_status="unreviewed"))

        self.assertEqual("completed", row["worker"]["terminal_status"])
        self.assertEqual("unreviewed", row["reviewer"]["review_status"])
        self.assertFalse(row["reviewer"]["reviewer_codex_present"])
        self.assertFalse(row["policy_usage"]["usable_for_worker_policy"])
        self.assertTrue(row["policy_usage"]["usable_for_task_vector_evaluation"])

    def test_worker_requirement_uses_last_run_vector_for_outcome_attribution(self) -> None:
        row = derive_evaluation_row(
            base_task(
                model_requirement_vector={"dimensions": {"reasoning_depth": "high"}},
                last_run={
                    "resolved_execution_config": {
                        "selection_rule": "low-cost-docs",
                        "model_requirement_vector": {"dimensions": {"reasoning_depth": "low"}},
                    },
                    "duration_seconds": 10,
                },
            )
        )

        self.assertIn("reasoning_depth=low", row["worker"]["model_requirement_key"])
        self.assertNotIn("reasoning_depth=high", row["worker"]["model_requirement_key"])
        self.assertEqual("unknown", row["worker"]["model_source"])
        self.assertEqual("none", row["worker"]["execution_target"])

    def test_provider_resource_evidence_is_advisory_and_separate_from_capacity_pool(self) -> None:
        baseline = derive_evaluation_row(
            base_task(
                status="completed",
                review_status="accepted",
                anchor_review=True,
                reviewer_codex={"decision": "pass", "confidence": "high", "findings": []},
            )
        )
        with_pool = derive_evaluation_row(
            base_task(
                status="completed",
                review_status="accepted",
                capacity_pool="spark",
                anchor_review=True,
                reviewer_codex={"decision": "pass", "confidence": "high", "findings": []},
            )
        )

        self.assertEqual("spark", with_pool["routing"]["capacity_pool"])
        self.assertEqual("codex", with_pool["provider_resource"]["provider_id"])
        self.assertEqual("unknown", with_pool["provider_resource"]["quota_boundary"])
        self.assertEqual("not_independent", with_pool["provider_resource"]["sharing_assumption"])
        self.assertTrue(with_pool["provider_resource"]["read_only"])
        self.assertTrue(with_pool["provider_resource"]["advisory_only"])
        self.assertFalse(with_pool["provider_resource"]["derived_from_capacity_pool"])
        self.assertFalse(with_pool["provider_resource"]["derived_from_worker_role"])
        self.assertFalse(with_pool["provider_resource"]["derived_from_legacy_profile"])
        self.assertEqual(baseline["policy_usage"], with_pool["policy_usage"])

    def test_rejected_and_needs_followup_keep_reviewer_decision_separate(self) -> None:
        rejected = derive_evaluation_row(
            base_task(
                status="completed",
                review_status="rejected",
                reviewer_codex={
                    "decision": "needs_fix",
                    "confidence": "high",
                    "findings": [{"severity": "error", "summary": "synthetic", "evidence": "synthetic"}],
                },
            )
        )
        followup = derive_evaluation_row(
            base_task(
                status="completed",
                review_status="needs_followup",
                last_review_decision="needs_human",
                fix_attempts=2,
            )
        )

        self.assertTrue(rejected["outcomes"]["rejected"])
        self.assertEqual("needs_fix", rejected["reviewer"]["reviewer_decision"])
        self.assertEqual(1, rejected["reviewer"]["error_finding_count"])
        self.assertTrue(followup["outcomes"]["needs_followup"])
        self.assertEqual("needs_human", followup["reviewer"]["reviewer_decision"])
        self.assertEqual(2, followup["reviewer"]["fix_attempts"])

    def test_failed_running_and_runnable_examples(self) -> None:
        failed = derive_evaluation_row(
            base_task(
                status="failed",
                review_status=None,
                last_result={"task_id": "task-2026-06-25T170111-447743Z0000", "status": "failed"},
                last_error="startup_stall while reading output",
            )
        )
        running = derive_evaluation_row(base_task(status="running", review_status=None, last_result={}))
        runnable = derive_evaluation_row(base_task(status="runnable", review_status=None, last_result={}))

        self.assertTrue(failed["outcomes"]["failed"])
        self.assertTrue(failed["worker"]["startup_stalled"])
        self.assertIn("worker_not_terminal", running["exclusion_reasons"])
        self.assertTrue(running["outcomes"]["running"])
        self.assertIn("worker_not_terminal", runnable["exclusion_reasons"])
        self.assertTrue(runnable["outcomes"]["runnable"])

    def test_resolved_example(self) -> None:
        row = derive_evaluation_row(
            base_task(
                status="completed",
                review_status="needs_followup",
                resolution="superseded",
                resolution_reason="handled by follow-up",
            )
        )

        self.assertTrue(row["outcomes"]["resolved"])
        self.assertEqual("superseded", row["outcomes"]["resolution"])
        self.assertTrue(row["reviewer"]["human_override_present"])

    def test_objective_markers_are_flags_not_raw_values(self) -> None:
        row = derive_evaluation_row(
            base_task(
                status="completed",
                review_status="accepted",
                execution_rebase_status="stale_base_rebase",
                execution_conflict_fix_status="queued",
                last_conflict_fix_task_id="task-2026-06-25T180000-000000Z0000",
                last_result={
                    "task_id": "task-2026-06-25T170111-447743Z0000",
                    "status": "completed",
                    "verification": [],
                },
            )
        )

        self.assertTrue(row["objective_checks"]["stale_base_marker_present"])
        self.assertTrue(row["objective_checks"]["conflict_marker_present"])
        self.assertTrue(row["objective_checks"]["verification_missing"])
        self.assertIn("objective_checks_missing", row["exclusion_reasons"])
        self.assertTrue(row["reviewer"]["last_conflict_fix_task_id_hash"].startswith("sha256:"))

    def test_raw_prompt_session_thread_paths_logs_and_summaries_are_not_returned(self) -> None:
        private_prompt = "Private prompt text must not be emitted."
        private_path = "/Users/example/private/project/secret.txt"
        row = derive_evaluation_row(
            base_task(
                prompt=private_prompt,
                next_prompt="Resume with private next prompt",
                session_id="session_abcdefghijklmnopqrstuvwxyz",
                thread_id="thread_abcdefghijklmnopqrstuvwxyz",
                execution_worktree_path=private_path,
                log_paths=[private_path],
                last_result={
                    "task_id": "task-2026-06-25T170111-447743Z0000",
                    "status": "completed",
                    "summary": f"Raw summary mentions {private_path}",
                    "verification": [f"pytest {private_path}"],
                    "changed_files": [private_path],
                },
            )
        )
        serialized = json.dumps(row, sort_keys=True)

        self.assertNotIn(private_prompt, serialized)
        self.assertNotIn("Resume with private next prompt", serialized)
        self.assertNotIn("Raw summary mentions", serialized)
        self.assertNotIn("session_abcdefghijklmnopqrstuvwxyz", serialized)
        self.assertNotIn("thread_abcdefghijklmnopqrstuvwxyz", serialized)
        self.assertNotIn(private_path, serialized)
        self.assertFalse(row["privacy"]["raw_prompt_included"])
        self.assertFalse(row["privacy"]["raw_paths_included"])
        self.assertFalse(row["task_vector_evaluation"]["privacy"]["raw_changed_files_included"])
        self.assertFalse(row["task_vector_evaluation"]["privacy"]["raw_verification_included"])

    def test_task_vector_evaluation_compares_declared_vector_to_safe_observed_metadata(self) -> None:
        row = derive_evaluation_row(
            base_task(
                status="completed",
                review_status="accepted",
                routing_size="small",
                routing_risk="medium",
                category="implementation",
                execution_backend="codex",
                routing_risk_factors=["source"],
                last_run={"execution_backend": "codex", "resolved_execution_config": {"selection_rule": "standard"}},
                last_result={
                    "task_id": "task-2026-06-25T170111-447743Z0000",
                    "status": "completed",
                    "changed_files": ["src/example.py", "tests/test_example.py"],
                    "verification": ["PYTHONPATH=src python3 -m unittest tests.test_example"],
                },
            )
        )
        vector_evaluation = row["task_vector_evaluation"]

        self.assertTrue(vector_evaluation["read_only"])
        self.assertEqual("task-vector-evaluation-v1", vector_evaluation["derivation_version"])
        self.assertEqual("small", vector_evaluation["dimensions"]["routing_size"]["observed"])
        self.assertEqual("match", vector_evaluation["dimensions"]["routing_size"]["comparison"])
        self.assertEqual("medium", vector_evaluation["dimensions"]["routing_risk"]["observed"])
        self.assertEqual("match", vector_evaluation["dimensions"]["routing_risk"]["comparison"])
        self.assertEqual("implementation", vector_evaluation["dimensions"]["category"]["observed"])
        self.assertEqual("match", vector_evaluation["dimensions"]["category"]["comparison"])
        self.assertEqual("codex", vector_evaluation["dimensions"]["execution_backend"]["observed"])
        self.assertEqual("match", vector_evaluation["dimensions"]["execution_backend"]["comparison"])
        self.assertEqual(["unit"], vector_evaluation["dimensions"]["verification_scope"]["observed"])
        self.assertEqual("match", vector_evaluation["dimensions"]["verification_scope"]["comparison"])
        self.assertEqual(["source", "tests"], vector_evaluation["observed"]["changed_file_classes"])
        self.assertEqual(0, vector_evaluation["summary"]["mismatched_dimensions"])

    def test_task_vector_evaluation_records_mismatches_and_uncertainty_without_raw_paths(self) -> None:
        private_path = "/Users/example/private/project/src/secret.py"
        row = derive_evaluation_row(
            base_task(
                status="completed",
                review_status="accepted",
                routing_size="small",
                routing_risk="low",
                category="docs",
                execution_backend="codex",
                labels=["implementation"],
                last_run={"execution_backend": "codex", "resolved_execution_config": {"selection_rule": "standard"}},
                last_result={
                    "task_id": "task-2026-06-25T170111-447743Z0000",
                    "status": "completed",
                    "changed_files": [private_path],
                    "verification": [],
                },
            )
        )
        vector_evaluation = row["task_vector_evaluation"]
        serialized = json.dumps(vector_evaluation, sort_keys=True)

        self.assertNotIn(private_path, serialized)
        self.assertEqual(["private_docs"], vector_evaluation["observed"]["changed_file_classes"])
        self.assertEqual("observed_missing", vector_evaluation["dimensions"]["labels"]["comparison"])
        self.assertIn("safe_observed_signal_missing", vector_evaluation["summary"]["uncertainty_reasons"])
        self.assertIn("labels", vector_evaluation["summary"]["excluded_dimensions"])

    def test_missing_fields_are_unknown_or_empty_without_exceptions(self) -> None:
        row = derive_evaluation_row({})

        self.assertEqual("unknown", row["task_id"])
        self.assertEqual("unknown", row["worker"]["execution_backend"])
        self.assertEqual("unknown", row["worker"]["model_selection_rule"])
        self.assertEqual("unknown", row["reviewer"]["review_status"])
        self.assertEqual("unknown", row["objective_checks"]["final_result_status"])
        self.assertEqual([], row["task_vector"]["dimensions"]["labels"])
        self.assertEqual("not_observed", row["task_vector_evaluation"]["dimensions"]["routing_size"]["comparison"])
        self.assertIn("task_vector_uncertain", row["exclusion_reasons"])


if __name__ == "__main__":
    unittest.main()

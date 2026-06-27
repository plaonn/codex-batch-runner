from __future__ import annotations

import json
import unittest

from codex_batch_runner.task_vector import derive_normalized_task_vector


class TaskVectorTests(unittest.TestCase):
    def test_existing_routing_fields_map_deterministically(self) -> None:
        task_a = {
            "id": "task-2026-06-25T170111-341383Z0000",
            "project_id": "Project-A",
            "category": "Implementation",
            "labels": ["Queue", "safe", "queue"],
            "routing_size": "Small",
            "routing_risk": "Low",
            "verification_scope": ["unit", "Docs"],
            "routing_risk_factors": ["Public-Docs", "low-blast-radius"],
            "execution_backend": "codex",
            "cwd": "/Users/example/project",
            "project_root": "/Users/example/project",
        }
        task_b = {
            "project_root": "/Users/example/project",
            "cwd": "/Users/example/project",
            "execution_backend": "CODEX",
            "routing_risk_factors": ["low-blast-radius", "public-docs"],
            "verification_scope": ["docs", "unit"],
            "routing_risk": "low",
            "routing_size": "small",
            "labels": ["safe", "queue"],
            "category": "implementation",
            "project_id": "project-a",
            "id": "task-2026-06-25T170111-341383Z0000",
        }

        vector = derive_normalized_task_vector(task_a)

        self.assertEqual(vector, derive_normalized_task_vector(task_b))
        self.assertEqual("task-vector-v1", vector["preprocessing_version"])
        self.assertEqual("existing_metadata", vector["source"])
        self.assertEqual("deterministic", vector["derivation"])
        self.assertEqual("high", vector["confidence"])
        self.assertEqual(
            {
                "routing_size": "small",
                "routing_risk": "low",
                "category": "implementation",
                "execution_backend": "codex",
                "verification_scope": ["docs", "unit"],
                "routing_risk_factors": ["low-blast-radius", "public-docs"],
                "labels": ["queue", "safe"],
            },
            vector["dimensions"],
        )
        self.assertEqual("project-a", vector["project"]["project_id"])
        self.assertEqual("repo_root", vector["project"]["cwd_class"])
        self.assertTrue(vector["project"]["project_root_hash"].startswith("sha256:"))
        self.assertTrue(vector["task"]["task_id_hash"].startswith("sha256:"))
        self.assertFalse(vector["provenance"]["raw_prompt_used"])
        self.assertFalse(vector["provenance"]["persisted_to_task_json"])

    def test_missing_fields_are_explicit_unknown_or_empty(self) -> None:
        vector = derive_normalized_task_vector({})

        self.assertEqual("low", vector["confidence"])
        self.assertEqual(
            {
                "routing_size": "unknown",
                "routing_risk": "unknown",
                "category": "unknown",
                "execution_backend": "unknown",
                "verification_scope": [],
                "routing_risk_factors": [],
                "labels": [],
            },
            vector["dimensions"],
        )
        self.assertEqual({"project_id": "unknown", "cwd_class": "unknown"}, vector["project"])
        self.assertEqual({"task_id_hash": None, "subtask_type": "unknown"}, vector["task"])
        self.assertEqual([], vector["source_fields"])
        self.assertEqual(
            {"source": "missing", "confidence": "low"},
            vector["provenance"]["field_sources"]["routing_size"],
        )

    def test_provider_model_capacity_reviewer_and_outcome_fields_are_excluded(self) -> None:
        task = {
            "routing_size": "small",
            "routing_risk": "low",
            "category": "implementation",
            "execution_backend": "codex",
            "provider": "private-provider",
            "model": "private-model",
            "codex_profile": "private-profile",
            "model_selection_rule": "private-rule",
            "quota_bucket": "private-quota",
            "capacity_pool": "private-capacity",
            "reviewer_codex": {"decision": "pass", "confidence": "high"},
            "review_status": "accepted",
            "last_review_decision": "pass",
            "status": "completed",
            "last_run": {"duration_seconds": 1.2, "resolved_execution_config": {"selection_rule": "private-rule"}},
        }
        serialized = json.dumps(derive_normalized_task_vector(task), sort_keys=True)

        for excluded_key in (
            "provider",
            "model",
            "model_selection_rule",
            "codex_profile",
            "quota",
            "capacity",
            "reviewer",
            "review_status",
            "last_review_decision",
            "status",
            "outcome",
            "last_run",
        ):
            self.assertNotIn(excluded_key, serialized)
        for excluded_value in (
            "private-provider",
            "private-model",
            "private-profile",
            "private-quota",
            "private-capacity",
            "accepted",
            "completed",
        ):
            self.assertNotIn(excluded_value, serialized)

    def test_ordering_of_lists_is_stable(self) -> None:
        vector = derive_normalized_task_vector(
            {
                "labels": ["zeta", "Alpha", "alpha"],
                "routing_risk_factors": ["Paths", "config", "paths"],
                "verification_scope": ["smoke", "unit", "Smoke"],
            }
        )

        self.assertEqual(["alpha", "zeta"], vector["dimensions"]["labels"])
        self.assertEqual(["config", "paths"], vector["dimensions"]["routing_risk_factors"])
        self.assertEqual(["smoke", "unit"], vector["dimensions"]["verification_scope"])

    def test_does_not_depend_on_raw_prompt_content(self) -> None:
        base = {
            "routing_size": "small",
            "routing_risk": "low",
            "category": "implementation",
            "execution_backend": "codex",
            "prompt": "raw private task instructions that must not shape the vector",
        }
        changed_prompt = dict(base)
        changed_prompt["prompt"] = "completely different prompt"

        self.assertEqual(derive_normalized_task_vector(base), derive_normalized_task_vector(changed_prompt))


if __name__ == "__main__":
    unittest.main()

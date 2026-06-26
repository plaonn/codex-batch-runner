from __future__ import annotations

import json
import unittest

from codex_batch_runner.request_fingerprint import derive_request_fingerprint, find_request_fingerprint_candidates


class RequestFingerprintTests(unittest.TestCase):
    def test_equivalent_tasks_are_deterministic_across_key_and_list_order(self) -> None:
        task_a = {
            "prompt": "Update parser tests.\n\nRun unit tests.",
            "title": "Parser work",
            "labels": ["safe", "parser"],
            "routing_risk_factors": ["low-blast-radius", "public-tests"],
            "verification_scope": ["unit", "docs"],
            "routing_size": "small",
            "routing_risk": "low",
            "category": "implementation",
            "project_id": "sample-project",
        }
        task_b = {
            "project_id": "sample-project",
            "category": "Implementation",
            "routing_risk": "LOW",
            "routing_size": "Small",
            "verification_scope": ["docs", "unit"],
            "routing_risk_factors": ["public-tests", "low-blast-radius"],
            "labels": ["parser", "safe"],
            "title": "  Parser   work ",
            "prompt": "Update parser tests. Run unit tests.",
        }

        self.assertEqual(derive_request_fingerprint(task_a), derive_request_fingerprint(task_b))

    def test_raw_prompt_text_is_not_returned(self) -> None:
        raw_prompt = "Implement the private billing migration using synthetic-only fixtures."
        fingerprint = derive_request_fingerprint(
            {
                "title": "Billing migration",
                "prompt": raw_prompt,
                "description": "Do not expose the implementation text.",
            }
        )

        serialized = json.dumps(fingerprint, sort_keys=True)
        self.assertNotIn(raw_prompt, serialized)
        self.assertNotIn("private billing migration", serialized)
        self.assertFalse(fingerprint["privacy"]["raw_text_stored"])
        self.assertTrue(fingerprint["privacy"]["prompt_hash_only"])

    def test_private_looking_absolute_paths_are_not_returned_raw(self) -> None:
        private_path = "/Users/example/.codex-batch-runner/worktrees/task-demo/.private/notes.md"
        fingerprint = derive_request_fingerprint(
            {
                "title": "Path handling",
                "prompt": f"Review {private_path} and update tests.",
                "cwd": private_path,
                "project_root": "/Users/example/project",
            }
        )
        serialized = json.dumps(fingerprint, sort_keys=True)

        self.assertNotIn(private_path, serialized)
        self.assertNotIn("/Users/example", serialized)
        self.assertIn("private_docs", fingerprint["metadata_hints"]["path_classes"])
        self.assertEqual("outside_repo", fingerprint["metadata_hints"]["cwd_class"])
        self.assertTrue(fingerprint["metadata_hints"]["project_root_hash"].startswith("sha256:"))

    def test_metadata_hints_normalize_routing_fields(self) -> None:
        fingerprint = derive_request_fingerprint(
            {
                "title": "Queue report",
                "prompt": "Summarize queue state.",
                "project_id": "Project-A",
                "category": "Docs",
                "labels": ["Review", "queue", "review"],
                "routing_size": "Small",
                "routing_risk": "Low",
                "verification_scope": ["Docs", "unit"],
                "routing_risk_factors": ["Public-Docs", "low-blast-radius"],
            }
        )
        hints = fingerprint["metadata_hints"]

        self.assertEqual("project-a", hints["project_id"])
        self.assertEqual("docs", hints["category"])
        self.assertEqual(["queue", "review"], hints["labels"])
        self.assertEqual("small", hints["routing_size"])
        self.assertEqual("low", hints["routing_risk"])
        self.assertEqual(["docs", "unit"], hints["verification_scope"])
        self.assertEqual(["low-blast-radius", "public-docs"], hints["routing_risk_factors"])
        self.assertEqual("size=small risk=low verify=docs,unit", hints["task_bucket_key"])

    def test_simhash64_is_deterministic_hex_sketch(self) -> None:
        fingerprint = derive_request_fingerprint(
            {
                "title": "Add parser validation",
                "prompt": "Add parser validation and parser validation tests.",
            }
        )
        simhash = fingerprint["hashes"]["simhash64"]

        self.assertRegex(simhash, r"^[0-9a-f]{16}$")
        self.assertEqual(
            simhash,
            derive_request_fingerprint(
                {
                    "prompt": "Add parser validation and parser validation tests.",
                    "title": "Add parser validation",
                }
            )["hashes"]["simhash64"],
        )

    def test_exact_duplicate_candidate_report_is_public_safe(self) -> None:
        private_path = "/Users/example/.codex-batch-runner/worktrees/task-demo/.private/plan.md"
        report = find_request_fingerprint_candidates(
            [
                {
                    "id": "duplicate-a",
                    "title": "Parser validation",
                    "prompt": f"Update parser validation using {private_path}.",
                    "project_id": "project-a",
                    "routing_size": "small",
                    "routing_risk": "low",
                    "verification_scope": ["unit"],
                    "cwd": private_path,
                    "project_root": "/Users/example/project",
                },
                {
                    "id": "duplicate-b",
                    "title": "  Parser   validation ",
                    "prompt": f"Update parser validation using {private_path}.",
                    "project_id": "project-a",
                    "routing_size": "small",
                    "routing_risk": "low",
                    "verification_scope": ["unit"],
                    "cwd": private_path,
                    "project_root": "/Users/example/project",
                },
                {
                    "id": "unrelated",
                    "title": "Queue summary",
                    "prompt": "Summarize queue state.",
                    "project_id": "project-a",
                },
            ]
        )

        serialized = json.dumps(report, sort_keys=True)
        self.assertEqual(1, report["candidate_count"])
        self.assertEqual({"exact_duplicate": 1}, report["candidate_types"])
        candidate = report["candidates"][0]
        self.assertEqual("exact_duplicate", candidate["candidate_type"])
        self.assertEqual(["duplicate-a", "duplicate-b"], candidate["task_ids"])
        self.assertTrue(candidate["evidence"]["normalized_text_hash_match"])
        self.assertIn("private_docs", candidate["evidence"]["path_classes"])
        self.assertNotIn(private_path, serialized)
        self.assertNotIn("Update parser validation", serialized)
        self.assertFalse(report["privacy"]["raw_text_included"])
        self.assertFalse(candidate["privacy"]["raw_normalized_text_included"])

    def test_candidate_report_ignores_distinct_requests(self) -> None:
        report = find_request_fingerprint_candidates(
            [
                {"id": "task-a", "title": "Parser validation", "prompt": "Update parser validation."},
                {"id": "task-b", "title": "Parser validation", "prompt": "Update queue validation."},
            ]
        )

        self.assertEqual(0, report["candidate_count"])
        self.assertEqual({}, report["candidate_types"])
        self.assertEqual([], report["candidates"])


if __name__ == "__main__":
    unittest.main()

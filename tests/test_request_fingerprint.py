from __future__ import annotations

import json
import unittest

from codex_batch_runner.request_fingerprint import derive_request_fingerprint


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


if __name__ == "__main__":
    unittest.main()

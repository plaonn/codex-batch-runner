from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_batch_runner.execution_mutation_provenance import (
    ExecutionMutationProvenanceError,
    attach_execution_mutation_provenance,
    attach_execution_mutation_snapshot,
    build_execution_mutation_provenance,
    capture_execution_mutation_snapshot,
    execution_mutation_provenance_view,
    validate_execution_mutation_provenance,
)
from codex_batch_runner.natural_execution_attestation import (
    NaturalExecutionAttestationError,
    attach_natural_execution_attestation,
    build_natural_execution_attestation,
    build_natural_execution_attestation_report,
    build_worker_certification_evidence,
)
from tests.test_natural_execution_attestation import (
    MAPPING,
    RECORDED_AT,
    closed_task,
)


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
AS_OF = RECORDED_AT + timedelta(days=1)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def repository(root: Path) -> str:
    git(root, "init", "-q")
    git(root, "config", "user.email", "public@example.invalid")
    git(root, "config", "user.name", "Public Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "tracked.txt")
    git(root, "commit", "-qm", "base")
    return git(root, "rev-parse", "HEAD")


def task(root: Path, base: str) -> dict:
    return {
        "id": "mutation-task",
        "attempts": 1,
        "status": "running",
        "review_status": None,
        "execution_mode": "git_worktree",
        "execution_worktree_path": str(root),
        "execution_worktree_status": "running",
        "execution_branch": git(root, "branch", "--show-current"),
        "execution_base_head": base,
        "last_run": {"execution_evidence_id": "sha256:" + "a" * 64},
    }


def snapshot(
    value: dict,
    root: Path,
    phase: str,
    *,
    at: datetime,
    reported: object = None,
) -> None:
    attach_execution_mutation_snapshot(
        value,
        capture_execution_mutation_snapshot(
            value,
            root,
            phase=phase,
            captured_at=at,
            reported_changed_files=reported,
        ),
    )


class ExecutionMutationProvenanceTests(unittest.TestCase):
    def test_clean_readonly_execution_is_scoped_no_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = task(root, repository(root))
            snapshot(value, root, "pre_worker", at=NOW)
            snapshot(
                value,
                root,
                "post_worker_pre_cbr_commit",
                at=NOW + timedelta(seconds=1),
                reported=[],
            )
            value["status"] = "completed"
            value["review_status"] = "unreviewed"
            snapshot(value, root, "terminal_closure", at=NOW + timedelta(seconds=2))
            record = build_execution_mutation_provenance(
                value,
                recorded_at=NOW + timedelta(seconds=3),
                producer_revision="test-v1",
            )
            self.assertEqual("no_mutation", record["provenance"])
            self.assertEqual("unknown", record["global_provenance"])
            self.assertFalse(record["worker_certification_projection_allowed"])
            attach_execution_mutation_provenance(value, record)
            self.assertEqual(
                record["provenance_id"],
                execution_mutation_provenance_view(
                    value, as_of=NOW + timedelta(seconds=4)
                )["provenance_id"],
            )

    def test_worker_changes_and_cbr_commit_are_separated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = task(root, repository(root))
            snapshot(value, root, "pre_worker", at=NOW)
            (root / "tracked.txt").write_text("worker\n", encoding="utf-8")
            snapshot(
                value,
                root,
                "post_worker_pre_cbr_commit",
                at=NOW + timedelta(seconds=1),
                reported=["tracked.txt"],
            )
            git(root, "add", "tracked.txt")
            git(root, "commit", "-qm", "runner review commit")
            value["execution_commit"] = git(root, "rev-parse", "HEAD")
            snapshot(value, root, "terminal_closure", at=NOW + timedelta(seconds=2))
            record = build_execution_mutation_provenance(
                value,
                recorded_at=NOW + timedelta(seconds=3),
                producer_revision="test-v1",
            )
            self.assertEqual("mutation_observed", record["provenance"])
            self.assertTrue(record["attribution"]["worker_observed_changes"])
            self.assertTrue(
                record["attribution"]["cbr_created_commit_or_state_changes"]
            )
            self.assertFalse(record["attribution"]["worker_created_commit"])

    def test_preexisting_dirt_and_unsafe_unreported_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = task(root, repository(root))
            (root / "preexisting.txt").write_text("dirt\n", encoding="utf-8")
            snapshot(value, root, "pre_worker", at=NOW)
            (root / "unreported.txt").write_text("worker\n", encoding="utf-8")
            snapshot(
                value,
                root,
                "post_worker_pre_cbr_commit",
                at=NOW + timedelta(seconds=1),
                reported=["../unsafe"],
            )
            snapshot(value, root, "terminal_closure", at=NOW + timedelta(seconds=2))
            record = build_execution_mutation_provenance(
                value,
                recorded_at=NOW + timedelta(seconds=3),
                producer_revision="test-v1",
            )
            self.assertEqual("mutation_possible", record["provenance"])
            self.assertIn("pre_existing_dirt", record["fail_closed_reasons"])
            self.assertIn(
                "unsafe_or_unreported_paths", record["fail_closed_reasons"]
            )

    def test_worker_created_commit_is_observed_not_runner_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = task(root, repository(root))
            snapshot(value, root, "pre_worker", at=NOW)
            (root / "tracked.txt").write_text("worker commit\n", encoding="utf-8")
            git(root, "commit", "-qam", "worker commit")
            snapshot(
                value,
                root,
                "post_worker_pre_cbr_commit",
                at=NOW + timedelta(seconds=1),
                reported=["tracked.txt"],
            )
            snapshot(value, root, "terminal_closure", at=NOW + timedelta(seconds=2))
            record = build_execution_mutation_provenance(
                value,
                recorded_at=NOW + timedelta(seconds=3),
                producer_revision="test-v1",
            )
            self.assertEqual("mutation_observed", record["provenance"])
            self.assertTrue(record["attribution"]["worker_created_commit"])
            self.assertFalse(
                record["attribution"]["cbr_created_commit_or_state_changes"]
            )

    def test_missing_conflicting_and_resume_records_are_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = task(root, repository(root))
            snapshot(value, root, "pre_worker", at=NOW)
            missing = build_execution_mutation_provenance(
                value,
                recorded_at=NOW + timedelta(seconds=3),
                producer_revision="test-v1",
            )
            self.assertEqual("unknown", missing["provenance"])
            self.assertIn("missing_snapshot", missing["fail_closed_reasons"])

            snapshot(
                value,
                root,
                "post_worker_pre_cbr_commit",
                at=NOW + timedelta(seconds=1),
                reported=[],
            )
            snapshot(
                value,
                root,
                "post_worker_pre_cbr_commit",
                at=NOW + timedelta(seconds=2),
                reported=[],
            )
            snapshot(value, root, "terminal_closure", at=NOW + timedelta(seconds=3))
            value["resume_requested"] = True
            record = build_execution_mutation_provenance(
                value,
                recorded_at=NOW + timedelta(seconds=4),
                producer_revision="test-v1",
            )
            self.assertEqual("unknown", record["provenance"])
            self.assertIn("conflicting_snapshot", record["fail_closed_reasons"])
            self.assertIn("resume_or_crash_gap", record["fail_closed_reasons"])

    def test_unavailable_git_and_snapshot_chronology_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = {
                "id": "mutation-task",
                "attempts": 1,
                "status": "running",
                "execution_mode": "git_worktree",
                "execution_worktree_path": str(root),
                "execution_branch": "cbr/test",
                "last_run": {"execution_evidence_id": "sha256:" + "a" * 64},
            }
            snapshot(value, root, "pre_worker", at=NOW + timedelta(seconds=2))
            snapshot(
                value,
                root,
                "post_worker_pre_cbr_commit",
                at=NOW + timedelta(seconds=1),
                reported=[],
            )
            snapshot(value, root, "terminal_closure", at=NOW)
            record = build_execution_mutation_provenance(
                value,
                recorded_at=NOW + timedelta(seconds=1),
                producer_revision="test-v1",
            )
            self.assertEqual("unknown", record["provenance"])
            self.assertIn(
                "repository_observation_unavailable",
                record["fail_closed_reasons"],
            )
            self.assertIn(
                "invalid_snapshot_chronology", record["fail_closed_reasons"]
            )

    def test_reported_but_unobserved_path_is_mutation_possible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = task(root, repository(root))
            snapshot(value, root, "pre_worker", at=NOW)
            snapshot(
                value,
                root,
                "post_worker_pre_cbr_commit",
                at=NOW + timedelta(seconds=1),
                reported=["not-changed.txt"],
            )
            snapshot(value, root, "terminal_closure", at=NOW + timedelta(seconds=2))
            record = build_execution_mutation_provenance(
                value,
                recorded_at=NOW + timedelta(seconds=3),
                producer_revision="test-v1",
            )
            self.assertEqual("mutation_possible", record["provenance"])
            self.assertTrue(
                record["attribution"]["unsafe_or_unreported_paths"]
            )

    def test_digest_future_shape_and_privacy_tampering_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = task(root, repository(root))
            snapshot(value, root, "pre_worker", at=NOW)
            snapshot(
                value,
                root,
                "post_worker_pre_cbr_commit",
                at=NOW + timedelta(seconds=1),
                reported=[],
            )
            snapshot(value, root, "terminal_closure", at=NOW + timedelta(seconds=2))
            record = build_execution_mutation_provenance(
                value,
                recorded_at=NOW + timedelta(seconds=3),
                producer_revision="test-v1",
            )
            tampered = copy.deepcopy(record)
            tampered["provenance"] = "mutation_observed"
            with self.assertRaisesRegex(
                ExecutionMutationProvenanceError, "digest mismatch"
            ):
                validate_execution_mutation_provenance(tampered)
            future = copy.deepcopy(record)
            future["schema_version"] = 2
            with self.assertRaisesRegex(
                ExecutionMutationProvenanceError, "invalid mutation provenance"
            ):
                validate_execution_mutation_provenance(future)
            private = copy.deepcopy(record)
            private["private"] = {"cwd": "/private/operator"}
            with self.assertRaisesRegex(
                ExecutionMutationProvenanceError, "fields are not canonical"
            ):
                validate_execution_mutation_provenance(private)
            forged = copy.deepcopy(record)
            forged["scope"] = {"name": "forged"}
            body = copy.deepcopy(forged)
            body.pop("provenance_id")
            encoded = json.dumps(
                body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode()
            forged["provenance_id"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
            with self.assertRaisesRegex(
                ExecutionMutationProvenanceError,
                "binding or observation",
            ):
                attach_execution_mutation_provenance(value, forged)
            attach_execution_mutation_provenance(value, record)
            with self.assertRaisesRegex(
                ExecutionMutationProvenanceError, "future mutation provenance"
            ):
                execution_mutation_provenance_view(
                    value, as_of=NOW + timedelta(seconds=2)
                )

    def test_natural_attestation_binds_scoped_proof_but_keeps_global_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = repository(root)
            value = closed_task(root)
            value.update(
                {
                    "execution_mode": "git_worktree",
                    "execution_worktree_path": str(root),
                    "execution_worktree_status": "retained",
                    "execution_branch": git(root, "branch", "--show-current"),
                    "execution_base_head": base,
                }
            )
            snapshot(value, root, "pre_worker", at=NOW)
            snapshot(
                value,
                root,
                "post_worker_pre_cbr_commit",
                at=NOW + timedelta(seconds=1),
                reported=[],
            )
            snapshot(value, root, "terminal_closure", at=NOW + timedelta(seconds=2))
            mutation = build_execution_mutation_provenance(
                value,
                recorded_at=NOW + timedelta(seconds=3),
                producer_revision="test-v1",
            )
            attach_execution_mutation_provenance(value, mutation)
            attestation = build_natural_execution_attestation(
                value,
                mapping=MAPPING,
                evidence_class="natural-objective-run",
                scenario="objective_outcome",
                outcome="pass",
                mutation_provenance="unknown",
                attestor_revision="test-attestor-v1",
                recorded_at=RECORDED_AT,
                mutation_record=mutation,
            )
            attach_natural_execution_attestation(value, attestation)
            self.assertEqual(
                "no_mutation",
                attestation["mutation_binding"]["scoped_provenance"],
            )
            self.assertEqual(
                "unknown", attestation["mutation_binding"]["global_provenance"]
            )
            report = build_natural_execution_attestation_report(
                [attestation], as_of=AS_OF
            )
            self.assertEqual(1, report["scoped_mutation"]["verified_record_count"])
            self.assertFalse(
                report["scoped_mutation"][
                    "worker_certification_projection_allowed"
                ]
            )
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError,
                "no policy-eligible natural worker evidence",
            ):
                build_worker_certification_evidence(
                    [attestation],
                    candidate={
                        "worker_id": MAPPING["worker_id"],
                        "target_snapshot_id": MAPPING["target_snapshot_id"],
                        "task_class": MAPPING["task_class"],
                    },
                    as_of=AS_OF,
                )
            future_mutation = build_execution_mutation_provenance(
                value,
                recorded_at=RECORDED_AT + timedelta(days=1),
                producer_revision="test-v1",
            )
            value["execution_mutation_provenance_history"] = []
            attach_execution_mutation_provenance(value, future_mutation)
            with self.assertRaisesRegex(
                NaturalExecutionAttestationError,
                "future scoped mutation source",
            ):
                build_natural_execution_attestation(
                    value,
                    mapping=MAPPING,
                    evidence_class="natural-objective-run",
                    scenario="objective_outcome",
                    outcome="pass",
                    mutation_provenance="unknown",
                    attestor_revision="test-attestor-v1",
                    recorded_at=RECORDED_AT,
                    mutation_record=future_mutation,
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.fs import write_json_atomic
from codex_batch_runner.orchestration import (
    build_orchestration_plan,
    validate_manifest,
)
from codex_batch_runner.orchestration_dispatch import (
    apply_dispatch,
    validate_execution_envelope,
)
from codex_batch_runner.orchestration_selection import (
    POLICY_REVISION,
    build_selection_preview,
    stable_digest,
    validate_selection_receipt,
)
from codex_batch_runner.orchestration_selection_funnel import (
    SelectionFunnelError,
    build_selection_funnel,
    validate_selection_funnel,
)
from codex_batch_runner.parent_attention import (
    create_parent_attention,
    stable_event_id,
)
from codex_batch_runner.queue import load_task, save_task


EVALUATED_AT = "2026-07-25T12:00:00+09:00"
COMPLETED_AT = "2026-07-25T12:10:00+09:00"
REVIEWED_AT = "2026-07-25T12:20:00+09:00"
APPLIED_AT = "2026-07-25T12:30:00+09:00"


def cbr_manifest(**changes: object) -> dict:
    value = {
        "schema_version": 1,
        "contract": "orchestration-intake-v1",
        "request_id": "funnel-request",
        "idempotency_key": "funnel-key",
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
            "duration": "long",
            "persistence": "durable",
            "resume": "required",
            "dependency": "hard",
            "collection": "durable_attention",
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
        "automation_boundary": "manual_only",
        "surface_preferences": ["cbr_batch"],
    }
    value.update(changes)
    return validate_manifest(value)


def envelope(value: dict, repo: Path) -> dict:
    plan = build_orchestration_plan(value)
    return validate_execution_envelope(
        {
            "schema_version": 1,
            "contract": "orchestration-cbr-execution-v1",
            "request_id": value["request_id"],
            "request_fingerprint": plan["request_fingerprint"],
            "prompt": "Private prompt api_key=do-not-emit",
            "cwd": str(repo),
            "origin_parent_ref": "opaque-private-parent",
            "task": {
                "title": "Implement bounded change",
                "description": "Sanitized description",
                "project_id": "sample-project",
                "category": "implementation",
                "labels": ["safe"],
                "depends_on": [],
                "verification_scope": ["unit"],
                "capacity_pool": "codex",
                "priority": "normal",
            },
        }
    )


def selection_receipt(
    value: dict, *, selected_surface: str = "cbr_batch"
) -> dict:
    preview = build_selection_preview(
        value,
        selected_surface=selected_surface,
        source_contract_digest=stable_digest(value),
        policy_revision=POLICY_REVISION,
        evaluated_at=EVALUATED_AT,
    )
    body = {
        "schema_version": 1,
        "contract": "orchestration-selection-receipt-v1",
        "recorded_at": EVALUATED_AT,
        "decision": preview["decision"],
        "preview_digest": preview["preview_digest"],
        "audit_event": {
            "event_type": "orchestration_selection_recorded",
            "required": True,
        },
        "mutation": {"allowed": True, "applied": True},
    }
    body["receipt_id"] = stable_digest(body)
    return validate_selection_receipt(body)


def stage(report: dict, surface: str, name: str) -> str:
    row = next(
        item for item in report["surface_rows"] if item["surface"] == surface
    )
    return row["stages"][name]["status"]


class OrchestrationSelectionFunnelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test"],
            check=True,
        )
        (self.repo / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "README.md"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "initial"],
            check=True,
        )
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "root": str(self.root / "runtime"),
                    "queue_dir": "tasks",
                    "log_dir": "logs",
                    "event_dir": "events",
                    "lock_file": "runner.lock",
                    "state_file": "state.json",
                    "worktree_mode": "task",
                    "capacity_pools": {"codex": {"max_running": 2}},
                    "max_total_running": 2,
                    "max_running_per_project": 2,
                }
            ),
            encoding="utf-8",
        )
        self.config_path = config_path
        self.config = Config.load(str(config_path))
        self.manifest = cbr_manifest()
        self.envelope = envelope(self.manifest, self.repo)
        self.selection = selection_receipt(self.manifest)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def project(self) -> dict:
        return build_selection_funnel(
            self.config,
            self.manifest,
            self.envelope,
            self.selection,
        )

    def admit(self) -> dict:
        receipt, success = apply_dispatch(
            self.config, self.manifest, self.envelope
        )
        self.assertTrue(success)
        return load_task(self.config, receipt["task_id"])

    def complete_and_accept(self, task: dict) -> dict:
        task["status"] = "completed"
        task["completed_at"] = COMPLETED_AT
        task["last_result"] = {
            "status": "completed",
            "summary": "Sanitized completed result",
            "changed_files": ["README.md"],
            "verification": ["unit"],
        }
        task["review_status"] = "accepted"
        task["reviewed_at"] = REVIEWED_AT
        save_task(self.config, task)
        return task

    def apply_worktree_evidence(self, task: dict) -> dict:
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        task["execution_mode"] = "git_worktree"
        task["execution_apply_status"] = "applied"
        task["execution_applied_at"] = APPLIED_AT
        task["execution_applied_head"] = head
        task["execution_apply_target"] = "main"
        task["execution_repo_root"] = str(self.repo)
        save_task(self.config, task)
        return task

    def test_clean_absence_preserves_funnel_without_creating_runtime_dirs(self) -> None:
        before = sorted(str(path) for path in self.root.rglob("*"))
        report = self.project()
        after = sorted(str(path) for path in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual("observed", stage(report, "cbr_batch", "durable_eligible"))
        self.assertEqual("observed", stage(report, "cbr_batch", "planned"))
        self.assertEqual("observed", stage(report, "cbr_batch", "selected"))
        for name in (
            "admitted",
            "completed",
            "accepted",
            "applied",
            "parent_attention_recorded",
        ):
            self.assertEqual("not_observed", stage(report, "cbr_batch", name))
        self.assertEqual(["trusted_dispatch_not_observed"], report["reason_codes"])
        self.assertFalse(report["mutation"]["allowed"])

    def test_admitted_completed_and_accepted_use_canonical_task_state(self) -> None:
        task = self.admit()
        admitted = self.project()
        self.assertEqual("observed", stage(admitted, "cbr_batch", "admitted"))
        self.assertEqual("not_observed", stage(admitted, "cbr_batch", "completed"))
        self.complete_and_accept(task)
        accepted = self.project()
        self.assertEqual("observed", stage(accepted, "cbr_batch", "completed"))
        self.assertEqual("observed", stage(accepted, "cbr_batch", "accepted"))
        self.assertEqual("not_applicable", stage(accepted, "cbr_batch", "applied"))
        self.assertEqual(
            "unknown",
            stage(accepted, "cbr_batch", "parent_attention_recorded"),
        )

    def test_full_worktree_chain_and_parent_attention_are_observed(self) -> None:
        task = self.apply_worktree_evidence(
            self.complete_and_accept(self.admit())
        )
        create_parent_attention(
            self.config,
            parent_ref=task["origin_parent_ref"],
            work_item_ref=task["id"],
            completion_id=task["completed_at"],
            wake_reason="completed",
            summary="Sanitized completion",
        )
        report = self.project()
        for name in report["stage_order"]:
            self.assertEqual("observed", stage(report, "cbr_batch", name), name)
        self.assertEqual(
            {
                "parent_attention_recorded_is_parent_collected": False,
                "parent_attention_recorded_is_root_complete": False,
                "routing_authority": False,
            },
            report["semantic_non_claims"],
        )
        rendered = json.dumps(report, sort_keys=True)
        for private in (
            task["id"],
            task["orchestration_dispatch_id"],
            task["origin_parent_ref"],
            str(self.repo),
            "api_key",
        ):
            self.assertNotIn(private, rendered)
        self.assertEqual(report, validate_selection_funnel(report))

    def test_malformed_completion_and_dispatch_conflict_propagate_unknown(self) -> None:
        task = self.admit()
        task["status"] = "completed"
        task["completed_at"] = COMPLETED_AT
        save_task(self.config, task)
        report = self.project()
        self.assertEqual("unknown", stage(report, "cbr_batch", "completed"))
        self.assertEqual("unknown", stage(report, "cbr_batch", "accepted"))

        task = load_task(self.config, task["id"])
        task["orchestration_request_fingerprint"] = "sha256:" + "0" * 64
        write_json_atomic(self.config.queue_dir / f"{task['id']}.json", task)
        conflict = self.project()
        for name in (
            "admitted",
            "completed",
            "accepted",
            "applied",
            "parent_attention_recorded",
        ):
            self.assertEqual("unknown", stage(conflict, "cbr_batch", name))
        self.assertEqual(["trusted_dispatch_conflict"], conflict["reason_codes"])

    def test_stale_applied_head_and_bad_attention_binding_are_unknown(self) -> None:
        task = self.complete_and_accept(self.admit())
        task["execution_mode"] = "git_worktree"
        task["execution_apply_status"] = "applied"
        task["execution_applied_at"] = APPLIED_AT
        task["execution_applied_head"] = "0" * 40
        task["execution_apply_target"] = "main"
        task["execution_repo_root"] = str(self.repo)
        save_task(self.config, task)
        stale = self.project()
        self.assertEqual("unknown", stage(stale, "cbr_batch", "applied"))

        task = self.apply_worktree_evidence(task)
        record = create_parent_attention(
            self.config,
            parent_ref=task["origin_parent_ref"],
            work_item_ref=task["id"],
            completion_id=task["completed_at"],
            wake_reason="completed",
            summary="Sanitized completion",
        )
        self.assertIsNotNone(record)
        attention_path = (
            self.config.parent_attention_outbox_dir
            / f"{record['event_id']}.json"
        )
        broken = json.loads(attention_path.read_text(encoding="utf-8"))
        broken["event_id"] = "pa-" + "0" * 32
        attention_path.write_text(json.dumps(broken), encoding="utf-8")
        report = self.project()
        self.assertEqual(
            "unknown",
            stage(report, "cbr_batch", "parent_attention_recorded"),
        )

    def test_attention_requires_exact_completion_binding_and_valid_delivery(self) -> None:
        task = self.apply_worktree_evidence(
            self.complete_and_accept(self.admit())
        )
        record = create_parent_attention(
            self.config,
            parent_ref=task["origin_parent_ref"],
            work_item_ref=task["id"],
            completion_id=task["completed_at"],
            wake_reason="completed",
            summary="Sanitized completion",
        )
        self.assertIsNotNone(record)
        expected_path = (
            self.config.parent_attention_outbox_dir
            / f"{record['event_id']}.json"
        )
        wrong = json.loads(expected_path.read_text(encoding="utf-8"))
        wrong["completion_id"] = task["reviewed_at"]
        wrong["event_id"] = stable_event_id(
            task["origin_parent_ref"],
            task["id"],
            task["reviewed_at"],
            "completed",
        )
        expected_path.write_text(json.dumps(wrong), encoding="utf-8")
        self.assertEqual(
            "unknown",
            stage(self.project(), "cbr_batch", "parent_attention_recorded"),
        )

        invalid_delivery = json.loads(
            expected_path.read_text(encoding="utf-8")
        )
        invalid_delivery["completion_id"] = task["completed_at"]
        invalid_delivery["event_id"] = record["event_id"]
        invalid_delivery["delivery"]["attempts"] = -1
        expected_path.write_text(
            json.dumps(invalid_delivery), encoding="utf-8"
        )
        self.assertEqual(
            "unknown",
            stage(self.project(), "cbr_batch", "parent_attention_recorded"),
        )

    def test_chronology_and_execution_mode_fail_closed(self) -> None:
        task = self.complete_and_accept(self.admit())
        task["reviewed_at"] = "2026-07-25T12:05:00+09:00"
        save_task(self.config, task)
        report = self.project()
        self.assertEqual("unknown", stage(report, "cbr_batch", "accepted"))

        task["reviewed_at"] = REVIEWED_AT
        task["execution_mode"] = "remote_magic"
        save_task(self.config, task)
        report = self.project()
        self.assertEqual("unknown", stage(report, "cbr_batch", "applied"))

        self.apply_worktree_evidence(task)
        task["execution_applied_at"] = "2026-07-25T12:15:00+09:00"
        save_task(self.config, task)
        report = self.project()
        self.assertEqual("unknown", stage(report, "cbr_batch", "applied"))

    def test_archived_accepted_task_preserves_terminal_stages(self) -> None:
        task = self.complete_and_accept(self.admit())
        task["previous_status"] = "completed"
        task["status"] = "archived"
        task["archived_at"] = APPLIED_AT
        task["archive_gate_result"] = {
            "status": "passed",
            "checked_at": APPLIED_AT,
            "blockers": [],
            "warnings": [],
        }
        save_task(self.config, task)
        report = self.project()
        self.assertEqual("observed", stage(report, "cbr_batch", "completed"))
        self.assertEqual("observed", stage(report, "cbr_batch", "accepted"))

        task["archived_at"] = "2026-07-25T12:15:00+09:00"
        save_task(self.config, task)
        report = self.project()
        self.assertEqual("unknown", stage(report, "cbr_batch", "accepted"))

    def test_non_cbr_downstream_is_unknown_without_adapter(self) -> None:
        value = cbr_manifest(
            work={
                **cbr_manifest()["work"],
                "duration": "short",
                "persistence": "turn_bound",
                "resume": "not_needed",
                "dependency": "none",
                "collection": "immediate_parent",
            },
            surface_preferences=["codex_parent_thread"],
        )
        selected = selection_receipt(
            value, selected_surface="codex_parent_thread"
        )
        report = build_selection_funnel(
            self.config, value, envelope(value, self.repo), selected
        )
        for name in (
            "admitted",
            "completed",
            "accepted",
            "applied",
            "parent_attention_recorded",
        ):
            self.assertEqual(
                "unknown", stage(report, "codex_parent_thread", name)
            )

    def test_source_drift_and_report_tampering_fail_closed(self) -> None:
        drifted = cbr_manifest(idempotency_key="other-key")
        with self.assertRaises(Exception):
            build_selection_funnel(
                self.config,
                drifted,
                envelope(drifted, self.repo),
                self.selection,
            )
        report = self.project()
        report["surface_rows"][0]["stages"]["selected"]["status"] = "unknown"
        with self.assertRaises(SelectionFunnelError):
            validate_selection_funnel(report)

        recomputed = self.project()
        recomputed["surface_rows"][0]["stages"]["selected"] = {
            "status": "observed",
            "reason_codes": ["selected_surface_not_match"],
        }
        unsigned = {
            key: value
            for key, value in recomputed.items()
            if key != "report_digest"
        }
        recomputed["report_digest"] = stable_digest(unsigned)
        with self.assertRaises(SelectionFunnelError):
            validate_selection_funnel(recomputed)

        cross_stage = self.project()
        cross_stage["surface_rows"][0]["stages"]["durable_eligible"] = {
            "status": "observed",
            "reason_codes": ["task_completed"],
        }
        cross_stage["report_digest"] = stable_digest(
            {
                key: value
                for key, value in cross_stage.items()
                if key != "report_digest"
            }
        )
        with self.assertRaises(SelectionFunnelError):
            validate_selection_funnel(cross_stage)

        non_cbr_value = cbr_manifest(
            work={
                **cbr_manifest()["work"],
                "duration": "short",
                "persistence": "turn_bound",
                "resume": "not_needed",
                "dependency": "none",
                "collection": "immediate_parent",
            },
            surface_preferences=["codex_parent_thread"],
        )
        non_cbr = build_selection_funnel(
            self.config,
            non_cbr_value,
            envelope(non_cbr_value, self.repo),
            selection_receipt(
                non_cbr_value,
                selected_surface="codex_parent_thread",
            ),
        )
        non_cbr["surface_rows"][0]["stages"]["admitted"] = {
            "status": "observed",
            "reason_codes": ["trusted_dispatch_observed"],
        }
        non_cbr["report_digest"] = stable_digest(
            {
                key: value
                for key, value in non_cbr.items()
                if key != "report_digest"
            }
        )
        with self.assertRaises(SelectionFunnelError):
            validate_selection_funnel(non_cbr)

    def test_cli_is_read_only_and_sanitizes_errors(self) -> None:
        manifest_path = self.root / "manifest.json"
        envelope_path = self.root / "private-envelope.json"
        receipt_path = self.root / "private-selection-receipt.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        envelope_path.write_text(json.dumps(self.envelope), encoding="utf-8")
        receipt_path.write_text(json.dumps(self.selection), encoding="utf-8")
        os.chmod(receipt_path, 0o600)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "--config",
                    str(self.config_path),
                    "orchestration",
                    "selection-funnel",
                    "--manifest",
                    str(manifest_path),
                    "--execution-envelope",
                    str(envelope_path),
                    "--selection-receipt",
                    str(receipt_path),
                    "--json",
                ]
            )
        self.assertEqual(0, code)
        report = json.loads(stdout.getvalue())
        self.assertEqual(
            "orchestration-selection-funnel-projection-v1",
            report["contract"],
        )

        private = str(self.root / "private-error")
        stderr = io.StringIO()
        with patch(
            "codex_batch_runner.cli.Config.load",
            side_effect=OSError(private),
        ), contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "--config",
                    str(self.config_path),
                    "orchestration",
                    "selection-funnel",
                    "--manifest",
                    str(manifest_path),
                    "--execution-envelope",
                    str(envelope_path),
                    "--selection-receipt",
                    str(receipt_path),
                ]
            )
        self.assertEqual(1, code)
        self.assertNotIn(private, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.fs import write_json_atomic
from codex_batch_runner.orchestration import (
    build_orchestration_plan,
    validate_manifest,
)
from codex_batch_runner.orchestration_dispatch import (
    apply_dispatch,
    identity_for,
    validate_execution_envelope,
)
from codex_batch_runner.orchestration_guard import (
    GuardContractError,
    MINIMUM_EXPLICIT_SUCCESSES,
    POLICY_CONTRACT,
    SHADOW_CONTRACT,
    TRIGGER_CONTRACT,
    VALIDATION_ORDER,
    build_reconciliation_shadow,
    guard_idempotency_key,
    load_guard_policy,
    policy_fingerprint,
    trigger_id_for,
    validate_guard_policy,
    validate_guard_trigger,
)
from codex_batch_runner.parent_attention import create_parent_attention
from codex_batch_runner.queue import load_task, save_task


class OrchestrationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(self.repo)],
            check=True,
            capture_output=True,
        )
        self.runtime = self.root / "runtime"
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "root": str(self.runtime),
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
        self.config = Config.load(str(self.config_path))
        self.source_id = "source-one"
        self.source_event_id = "event-one"
        self.trigger_id = trigger_id_for(self.source_id, self.source_event_id)
        self.manifest = validate_manifest(
            {
                "schema_version": 1,
                "contract": "orchestration-intake-v1",
                "request_id": "guard-request",
                "idempotency_key": guard_idempotency_key(self.trigger_id),
                "source": {
                    "kind": "automation",
                    "collection_owner": "operator",
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
                    "prohibited": [
                        "runtime_state",
                        "external_state",
                        "destructive",
                    ],
                },
                "automation_boundary": "bounded_automatic",
                "surface_preferences": ["cbr_batch"],
            }
        )
        self.plan = build_orchestration_plan(self.manifest)
        self.envelope = validate_execution_envelope(
            {
                "schema_version": 1,
                "contract": "orchestration-cbr-execution-v1",
                "request_id": self.manifest["request_id"],
                "request_fingerprint": self.plan["request_fingerprint"],
                "prompt": "Private prompt api_key=do-not-emit",
                "cwd": str(self.repo),
                "origin_parent_ref": "opaque-private-parent",
                "task": {
                    "title": "Implement bounded change",
                    "description": "Sanitized description",
                    "project_id": "sample-project",
                    "category": "implementation",
                    "labels": ["guarded"],
                    "depends_on": [],
                    "verification_scope": ["docs", "unit"],
                    "capacity_pool": "codex",
                    "priority": "normal",
                },
            }
        )
        self.policy = validate_guard_policy(self.policy_value())
        identity = identity_for(self.manifest, self.envelope)
        self.trigger = validate_guard_trigger(
            {
                "schema_version": 1,
                "contract": TRIGGER_CONTRACT,
                "trigger_id": self.trigger_id,
                "source_id": self.source_id,
                "source_adapter_revision": "adapter-v1",
                "source_event_id": self.source_event_id,
                "explicit_opt_in": True,
                "policy_id": self.policy["policy_id"],
                "policy_revision": self.policy["revision"],
                "policy_fingerprint": policy_fingerprint(self.policy),
                "request_id": self.manifest["request_id"],
                "request_fingerprint": self.plan["request_fingerprint"],
                "execution_fingerprint": identity["execution_fingerprint"],
                "created_at": "2026-07-20T00:00:00+00:00",
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def policy_value(self, **changes: object) -> dict:
        value = {
            "schema_version": 1,
            "contract": POLICY_CONTRACT,
            "policy_id": "guard-policy",
            "revision": "revision-1",
            "active": True,
            "activation_mode": "shadow",
            "source": {
                "source_id": self.source_id,
                "adapter_revision": "adapter-v1",
            },
            "scope": {
                "source_kinds": ["automation"],
                "project_ids": ["sample-project"],
                "repository_roots": [str(self.repo)],
                "work_kinds": ["implementation"],
                "decision_authorities": ["delegated_decision"],
                "impacts": ["low"],
                "allowed_mutations": ["tracked_files"],
                "required_prohibited_mutations": [
                    "external_state",
                    "destructive",
                ],
                "isolations": ["worktree"],
                "work_verifications": ["objective"],
                "required_verification_scope": ["docs", "unit"],
                "capacity_pools": ["codex"],
            },
            "evidence": {
                "cohort_id": "cohort-one",
                "provenance": "operator_attested_explicit_d2",
                "successful_explicit_dispatches": MINIMUM_EXPLICIT_SUCCESSES,
                "identity_conflicts": 0,
                "safety_violations": 0,
            },
            "rollout": {"max_new_admissions_per_run": 1},
        }
        value.update(changes)
        return value

    def write_inputs(self) -> tuple[Path, Path, Path, Path]:
        policy_path = self.root / "policy.json"
        trigger_path = self.root / "trigger.json"
        manifest_path = self.root / "manifest.json"
        envelope_path = self.root / "envelope.json"
        for path, value in (
            (policy_path, self.policy),
            (trigger_path, self.trigger),
            (manifest_path, self.manifest),
            (envelope_path, self.envelope),
        ):
            path.write_text(json.dumps(value), encoding="utf-8")
        return policy_path, trigger_path, manifest_path, envelope_path

    def cli(self, *args: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return (
            code,
            json.loads(stdout.getvalue()) if stdout.getvalue() else {},
            stderr.getvalue(),
        )

    def test_shadow_is_eligible_and_strictly_read_only(self) -> None:
        before = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        report = build_reconciliation_shadow(
            self.config,
            policy=self.policy,
            trigger=self.trigger,
            manifest=self.manifest,
            envelope=self.envelope,
        )
        after = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        self.assertEqual(before, after)
        self.assertEqual(SHADOW_CONTRACT, report["contract"])
        self.assertEqual("eligible_shadow", report["decision_status"])
        self.assertEqual([], report["reason_codes"])
        self.assertEqual("not_admitted", report["state"]["queue_admission"])
        self.assertEqual({"allowed": False, "applied": False}, report["mutation"])

    def test_cli_shadow_is_read_only_and_redacts_private_values(self) -> None:
        paths = self.write_inputs()
        before = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        code, report, stderr = self.cli(
            "--config",
            str(self.config_path),
            "orchestration",
            "reconcile-shadow",
            "--policy",
            str(paths[0]),
            "--trigger",
            str(paths[1]),
            "--manifest",
            str(paths[2]),
            "--execution-envelope",
            str(paths[3]),
            "--json",
        )
        after = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        self.assertEqual(0, code)
        self.assertEqual(before, after)
        output = json.dumps(report) + stderr
        for private in (
            self.envelope["prompt"],
            self.envelope["cwd"],
            self.envelope["origin_parent_ref"],
            str(paths[0]),
        ):
            self.assertNotIn(private, output)

    def test_activation_evidence_and_policy_expansion_fail_closed(self) -> None:
        cases = (
            (
                validate_guard_policy(self.policy_value(activation_mode="guarded")),
                "activation_not_implemented",
            ),
            (
                validate_guard_policy(
                    self.policy_value(
                        evidence={
                            "cohort_id": "cohort-one",
                            "provenance": "operator_attested_explicit_d2",
                            "successful_explicit_dispatches": 4,
                            "identity_conflicts": 0,
                            "safety_violations": 0,
                        }
                    )
                ),
                "evidence_floor_not_met",
            ),
            (
                validate_guard_policy(
                    self.policy_value(
                        evidence={
                            "cohort_id": "cohort-one",
                            "provenance": "operator_attested_explicit_d2",
                            "successful_explicit_dispatches": 5,
                            "identity_conflicts": 1,
                            "safety_violations": 0,
                        }
                    )
                ),
                "evidence_conflict_present",
            ),
        )
        for policy, reason in cases:
            trigger = dict(self.trigger)
            trigger["policy_fingerprint"] = policy_fingerprint(policy)
            report = build_reconciliation_shadow(
                self.config,
                policy=policy,
                trigger=trigger,
                manifest=self.manifest,
                envelope=self.envelope,
            )
            with self.subTest(reason=reason):
                self.assertEqual("blocked", report["decision_status"])
                self.assertIn(reason, report["reason_codes"])
                self.assertFalse(report["mutation"]["allowed"])

    def test_identity_and_opt_in_drift_fail_closed(self) -> None:
        cases = (
            ("explicit_opt_in", False, "explicit_opt_in_required"),
            ("trigger_id", "ot-" + "0" * 32, "trigger_identity_mismatch"),
            ("request_id", "other-request", "request_binding_mismatch"),
        )
        for field, value, reason in cases:
            trigger = dict(self.trigger)
            trigger[field] = value
            report = build_reconciliation_shadow(
                self.config,
                policy=self.policy,
                trigger=trigger,
                manifest=self.manifest,
                envelope=self.envelope,
            )
            with self.subTest(field=field):
                self.assertEqual("blocked", report["decision_status"])
                self.assertIn(reason, report["reason_codes"])
                self.assertFalse(report["mutation"]["applied"])

    def test_admission_completion_and_attention_are_separate_axes(self) -> None:
        receipt, success = apply_dispatch(self.config, self.manifest, self.envelope)
        self.assertTrue(success)
        report = build_reconciliation_shadow(
            self.config,
            policy=self.policy,
            trigger=self.trigger,
            manifest=self.manifest,
            envelope=self.envelope,
        )
        self.assertEqual("admitted", report["state"]["queue_admission"])
        self.assertEqual("runnable", report["state"]["execution"])
        self.assertEqual("not_emitted", report["state"]["attention_delivery"])
        self.assertEqual("readable", report["state"]["attention_state_health"])

        task = load_task(self.config, receipt["task_id"])
        task["status"] = "completed"
        task["completed_at"] = "2026-07-20T00:01:00+00:00"
        task["review_status"] = "unreviewed"
        task["last_result"] = {"summary": "completed"}
        save_task(self.config, task)
        create_parent_attention(
            self.config,
            parent_ref=task["origin_parent_ref"],
            work_item_ref=task["id"],
            completion_id=task["completed_at"],
            wake_reason="needs_review",
            summary="completed",
        )
        report = build_reconciliation_shadow(
            self.config,
            policy=self.policy,
            trigger=self.trigger,
            manifest=self.manifest,
            envelope=self.envelope,
        )
        self.assertEqual("completed", report["state"]["execution"])
        self.assertEqual("unreviewed", report["state"]["review"])
        self.assertEqual("pending", report["state"]["attention_delivery"])
        self.assertEqual(
            "not_acknowledged", report["state"]["attention_acknowledgement"]
        )
        self.assertEqual("not_observed", report["state"]["source_disposition"])

    def test_concurrent_shadow_invocations_do_not_create_runtime_state(self) -> None:
        reports: list[dict] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(8)

        def run() -> None:
            try:
                barrier.wait(timeout=5)
                reports.append(
                    build_reconciliation_shadow(
                        self.config,
                        policy=self.policy,
                        trigger=self.trigger,
                        manifest=self.manifest,
                        envelope=self.envelope,
                    )
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual([], errors)
        self.assertEqual(8, len(reports))
        self.assertTrue(
            all(report["decision_status"] == "eligible_shadow" for report in reports)
        )
        self.assertFalse(self.runtime.exists())

    def test_contract_validation_codes_have_input_evidence(self) -> None:
        path = self.root / "guard.json"
        with self.assertRaises(GuardContractError) as caught:
            load_guard_policy(path)
        self.assertEqual(("input_unreadable",), caught.exception.codes)
        path.write_bytes(b"x" * (64 * 1024 + 1))
        with self.assertRaises(GuardContractError) as caught:
            load_guard_policy(path)
        self.assertEqual(("input_too_large",), caught.exception.codes)
        path.write_bytes(b"\xff")
        with self.assertRaises(GuardContractError) as caught:
            load_guard_policy(path)
        self.assertEqual(("input_not_utf8",), caught.exception.codes)
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(GuardContractError) as caught:
            load_guard_policy(path)
        self.assertEqual(("input_json_invalid",), caught.exception.codes)
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(GuardContractError) as caught:
            load_guard_policy(path)
        self.assertEqual(("input_not_object",), caught.exception.codes)

        invalid = self.policy_value()
        invalid["extra"] = True
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_policy(invalid)
        self.assertEqual(("fields_invalid",), caught.exception.codes)
        invalid = self.policy_value()
        invalid["active"] = "yes"
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_policy(invalid)
        self.assertEqual(("value_type_invalid",), caught.exception.codes)
        invalid = self.policy_value()
        invalid["activation_mode"] = "automatic"
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_policy(invalid)
        self.assertEqual(("value_enum_invalid",), caught.exception.codes)
        invalid = self.policy_value()
        invalid["activation_mode"] = {}
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_policy(invalid)
        self.assertEqual(("value_type_invalid",), caught.exception.codes)
        invalid = self.policy_value()
        invalid["scope"]["allowed_mutations"] = ["runtime_state"]
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_policy(invalid)
        self.assertEqual(("value_enum_invalid",), caught.exception.codes)
        invalid = self.policy_value()
        invalid["scope"]["allowed_mutations"] = [{}]
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_policy(invalid)
        self.assertEqual(("value_type_invalid",), caught.exception.codes)
        invalid = self.policy_value()
        invalid["scope"]["repository_roots"] = []
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_policy(invalid)
        self.assertEqual(("value_bounds_invalid",), caught.exception.codes)
        invalid = self.policy_value()
        invalid["policy_id"] = "unsafe id"
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_policy(invalid)
        self.assertEqual(("unsafe_identifier",), caught.exception.codes)
        invalid = self.policy_value()
        invalid["scope"]["project_ids"] = ["same", "same"]
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_policy(invalid)
        self.assertEqual(("duplicate_list_item",), caught.exception.codes)
        invalid = self.policy_value()
        invalid["rollout"]["max_new_admissions_per_run"] = 2
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_policy(invalid)
        self.assertEqual(("cross_field_conflict",), caught.exception.codes)
        self.assertEqual(
            set(VALIDATION_ORDER),
            {
                "input_unreadable",
                "input_too_large",
                "input_not_utf8",
                "input_json_invalid",
                "input_not_object",
                "fields_invalid",
                "value_type_invalid",
                "value_enum_invalid",
                "value_bounds_invalid",
                "unsafe_identifier",
                "duplicate_list_item",
                "cross_field_conflict",
            },
        )

    def test_trigger_validation_rejects_naive_time_and_unknown_fields(self) -> None:
        invalid = dict(self.trigger)
        invalid["created_at"] = "2026-07-20T00:00:00"
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_trigger(invalid)
        self.assertEqual(("value_bounds_invalid",), caught.exception.codes)
        invalid = dict(self.trigger)
        invalid["private_path"] = "/private"
        with self.assertRaises(GuardContractError) as caught:
            validate_guard_trigger(invalid)
        self.assertEqual(("fields_invalid",), caught.exception.codes)

    def test_corrupt_attention_record_does_not_mutate_or_crash_shadow(self) -> None:
        attention_dir = self.runtime / "parent-attention-outbox"
        attention_dir.mkdir(parents=True)
        (attention_dir / "pa-corrupt.json").write_text("{", encoding="utf-8")
        report = build_reconciliation_shadow(
            self.config,
            policy=self.policy,
            trigger=self.trigger,
            manifest=self.manifest,
            envelope=self.envelope,
        )
        self.assertEqual("unknown", report["state"]["attention_delivery"])
        self.assertEqual("unreadable", report["state"]["attention_state_health"])
        self.assertIn("attention_state_unreadable", report["reason_codes"])

    def test_multiple_attention_records_use_conservative_aggregate(self) -> None:
        receipt, success = apply_dispatch(self.config, self.manifest, self.envelope)
        self.assertTrue(success)
        task_id = receipt["task_id"]
        first = create_parent_attention(
            self.config,
            parent_ref="thread-parent",
            work_item_ref=task_id,
            completion_id="completion-1",
            wake_reason="needs_review",
            summary="first",
        )
        second = create_parent_attention(
            self.config,
            parent_ref="thread-parent",
            work_item_ref=task_id,
            completion_id="completion-2",
            wake_reason="needs_decision",
            summary="second",
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        first["delivery"]["state"] = "acknowledged"
        second["delivery"]["state"] = "delivered"
        write_json_atomic(
            self.runtime / "parent-attention-outbox" / f"{first['event_id']}.json",
            first,
        )
        write_json_atomic(
            self.runtime / "parent-attention-outbox" / f"{second['event_id']}.json",
            second,
        )
        report = build_reconciliation_shadow(
            self.config,
            policy=self.policy,
            trigger=self.trigger,
            manifest=self.manifest,
            envelope=self.envelope,
        )
        self.assertEqual("delivered", report["state"]["attention_delivery"])
        self.assertEqual("pending", report["state"]["attention_acknowledgement"])

        second["delivery"]["state"] = "pending"
        write_json_atomic(
            self.runtime / "parent-attention-outbox" / f"{second['event_id']}.json",
            second,
        )
        report = build_reconciliation_shadow(
            self.config,
            policy=self.policy,
            trigger=self.trigger,
            manifest=self.manifest,
            envelope=self.envelope,
        )
        self.assertEqual("pending", report["state"]["attention_delivery"])
        self.assertEqual(
            "not_acknowledged", report["state"]["attention_acknowledgement"]
        )

    def test_d2_identity_conflict_is_reported_without_repair(self) -> None:
        receipt, success = apply_dispatch(self.config, self.manifest, self.envelope)
        self.assertTrue(success)
        task = load_task(self.config, receipt["task_id"])
        task["prompt"] = "drifted"
        write_json_atomic(self.config.queue_dir / f"{task['id']}.json", task)
        report = build_reconciliation_shadow(
            self.config,
            policy=self.policy,
            trigger=self.trigger,
            manifest=self.manifest,
            envelope=self.envelope,
        )
        self.assertEqual("blocked", report["decision_status"])
        self.assertIn("d2_identity_conflict", report["reason_codes"])
        self.assertEqual("conflict", report["state"]["queue_admission"])

    def test_unreadable_d2_task_state_is_identity_conflict(self) -> None:
        identity = identity_for(self.manifest, self.envelope)
        self.config.queue_dir.mkdir(parents=True)
        (self.config.queue_dir / f"{identity['task_id']}.json").write_text(
            "{", encoding="utf-8"
        )
        report = build_reconciliation_shadow(
            self.config,
            policy=self.policy,
            trigger=self.trigger,
            manifest=self.manifest,
            envelope=self.envelope,
        )
        self.assertEqual("blocked", report["decision_status"])
        self.assertIn("d2_identity_conflict", report["reason_codes"])
        self.assertEqual("conflict", report["d2_preview"]["status"])
        self.assertFalse(report["mutation"]["allowed"])


if __name__ == "__main__":
    unittest.main()

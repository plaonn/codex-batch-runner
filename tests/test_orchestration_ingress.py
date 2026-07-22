from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.orchestration import (
    build_orchestration_plan,
    validate_manifest,
)
from codex_batch_runner.orchestration_dispatch import validate_execution_envelope
from codex_batch_runner.orchestration_dispatch import DispatchLockBusy
from codex_batch_runner.orchestration_guard import (
    guard_idempotency_key,
    trigger_id_for,
    validate_guard_policy,
)
from codex_batch_runner.orchestration_consumer import (
    ConsumerError,
    apply_consumer,
    build_consumer_preview,
    consumer_doctor_summary,
    consumer_state_path,
    disposition_path,
)
from codex_batch_runner.orchestration_ingress import (
    BUNDLE_CONTRACT,
    PRODUCER_REVISION,
    SOURCE_ID,
    LocalIngressError,
    apply_local_reconciliation,
    apply_publish,
    build_local_reconciliation,
    build_publish_preview,
    ingress_path,
    load_local_ingress_bundle,
    load_published_bundle,
    reconciliation_state_path,
    validate_local_ingress_bundle,
)
from codex_batch_runner.state import set_runner_pause


NOW = datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc)


class OrchestrationIngressTests(unittest.TestCase):
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
                    "worktree_mode": "disabled",
                    "capacity_pools": {"codex": {"max_running": 1}},
                }
            ),
            encoding="utf-8",
        )
        self.config = Config.load(str(self.config_path))
        self.bundle = self.bundle_value()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def bundle_value(
        self,
        *,
        event_id: str = "operator-event-one",
        evidence_successes: int = 5,
    ) -> dict:
        trigger_id = trigger_id_for(SOURCE_ID, event_id)
        manifest = validate_manifest(
            {
                "schema_version": 1,
                "contract": "orchestration-intake-v1",
                "request_id": "local-verification-request",
                "idempotency_key": guard_idempotency_key(trigger_id),
                "source": {"kind": "operator", "collection_owner": "operator"},
                "summary": {
                    "root_goal": "Verify a bounded local contract",
                    "requirement": "Read-only objective verification",
                    "stop_condition": "Report the result",
                    "done_means": "Verification evidence is available",
                },
                "authority": {
                    "decision_authority": "delegated_decision",
                    "resolution": "resolved",
                    "impact": "low",
                    "approval_state": "not_required",
                },
                "work": {
                    "kind": "verification",
                    "interaction": "none",
                    "duration": "long",
                    "persistence": "durable",
                    "resume": "required",
                    "dependency": "none",
                    "collection": "durable_attention",
                    "context": "self_contained",
                    "isolation": "none",
                    "verification": "objective",
                    "external_worker_boundary": "unavailable",
                    "repository_scope": "present",
                },
                "mutation": {
                    "allowed": ["read_only"],
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
        plan = build_orchestration_plan(manifest)
        envelope = validate_execution_envelope(
            {
                "schema_version": 1,
                "contract": "orchestration-cbr-execution-v1",
                "request_id": manifest["request_id"],
                "request_fingerprint": plan["request_fingerprint"],
                "prompt": "Run read-only verification and report evidence.",
                "cwd": str(self.repo),
                "origin_parent_ref": "opaque-local-parent",
                "task": {
                    "title": "Verify local ingress contract",
                    "description": "Read-only verification lane",
                    "project_id": "sample-project",
                    "category": "verification",
                    "labels": ["local-ingress"],
                    "depends_on": [],
                    "verification_scope": ["unit"],
                    "capacity_pool": "codex",
                    "priority": "normal",
                },
            }
        )
        policy = validate_guard_policy(
            {
                "schema_version": 1,
                "contract": "orchestration-guard-policy-v1",
                "policy_id": "local-ingress-policy",
                "revision": "revision-one",
                "active": True,
                "activation_mode": "shadow",
                "source": {
                    "source_id": SOURCE_ID,
                    "adapter_revision": PRODUCER_REVISION,
                },
                "scope": {
                    "source_kinds": ["operator"],
                    "project_ids": ["sample-project"],
                    "repository_roots": [str(self.repo)],
                    "work_kinds": ["verification"],
                    "decision_authorities": ["delegated_decision"],
                    "impacts": ["low"],
                    "allowed_mutations": ["read_only"],
                    "required_prohibited_mutations": [
                        "external_state",
                        "destructive",
                    ],
                    "isolations": ["none"],
                    "work_verifications": ["objective"],
                    "required_verification_scope": ["unit"],
                    "capacity_pools": ["codex"],
                },
                "evidence": {
                    "cohort_id": "local-verification-cohort",
                    "provenance": "operator_attested_explicit_d2",
                    "successful_explicit_dispatches": evidence_successes,
                    "identity_conflicts": 0,
                    "safety_violations": 0,
                },
                "rollout": {"max_new_admissions_per_run": 1},
            }
        )
        return validate_local_ingress_bundle(
            {
                "schema_version": 1,
                "contract": BUNDLE_CONTRACT,
                "producer": {
                    "source_id": SOURCE_ID,
                    "revision": PRODUCER_REVISION,
                },
                "source_event_id": event_id,
                "explicit_opt_in": True,
                "created_at": "2030-01-01T00:00:00+00:00",
                "expires_at": "2030-01-02T00:00:00+00:00",
                "policy": policy,
                "manifest": manifest,
                "execution_envelope": envelope,
            }
        )

    def write_bundle(self, bundle: dict | None = None) -> Path:
        path = self.root / "private-ingress.json"
        path.write_text(json.dumps(bundle or self.bundle), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

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

    def test_contract_is_exact_and_initial_lane_is_narrow(self) -> None:
        self.assertEqual(SOURCE_ID, self.bundle["producer"]["source_id"])
        cases: list[tuple[str, dict]] = []
        value = copy.deepcopy(self.bundle)
        value["producer"]["revision"] = "other"
        cases.append(("ingress_producer_mismatch", value))
        value = copy.deepcopy(self.bundle)
        value["explicit_opt_in"] = False
        cases.append(("ingress_explicit_opt_in_required", value))
        value = copy.deepcopy(self.bundle)
        value["manifest"]["source"] = {
            "kind": "todoist_task",
            "collection_owner": "operator",
        }
        cases.append(("ingress_manifest_source_not_operator", value))
        value = copy.deepcopy(self.bundle)
        value["manifest"]["mutation"] = {
            "allowed": ["tracked_files"],
            "prohibited": ["runtime_state", "external_state", "destructive"],
        }
        cases.append(("ingress_mutation_lane_not_read_only", value))
        value = copy.deepcopy(self.bundle)
        value["policy"]["scope"]["project_ids"].append("another-project")
        cases.append(("ingress_policy_scope_not_exact", value))
        for reason, value in cases:
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(LocalIngressError, reason),
            ):
                validate_local_ingress_bundle(value)

    def test_publish_preview_is_strictly_read_only(self) -> None:
        before = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        report = build_publish_preview(self.config, self.bundle, now=NOW)
        after = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        self.assertEqual(before, after)
        self.assertEqual("ready", report["status"])
        self.assertEqual("eligible_shadow", report["shadow_decision_status"])
        self.assertEqual({"allowed": False, "applied": False}, report["mutation"])
        self.assertFalse(self.runtime.exists())

    def test_publish_is_private_idempotent_and_never_admits(self) -> None:
        report, success = apply_publish(self.config, self.bundle, now=NOW)
        self.assertTrue(success)
        self.assertEqual("published", report["status"])
        path = ingress_path(self.config, self.bundle["source_event_id"])
        self.assertTrue(path.exists())
        self.assertEqual(0, stat.S_IMODE(path.stat().st_mode) & 0o077)
        self.assertEqual(0, stat.S_IMODE(path.parent.stat().st_mode) & 0o077)
        self.assertFalse(self.config.queue_dir.exists())
        self.assertFalse((self.runtime / "orchestration-dispatch-receipts").exists())

        retry, success = apply_publish(self.config, self.bundle, now=NOW)
        self.assertTrue(success)
        self.assertEqual("already_published", retry["status"])
        self.assertEqual(
            self.bundle, load_published_bundle(self.config, "operator-event-one")
        )

    def test_divergent_duplicate_and_insecure_record_fail_closed(self) -> None:
        _, success = apply_publish(self.config, self.bundle, now=NOW)
        self.assertTrue(success)
        changed = copy.deepcopy(self.bundle)
        changed["execution_envelope"]["prompt"] = "Different read-only verification."
        changed["manifest"]["request_id"] = "different-request"
        changed["execution_envelope"]["request_id"] = "different-request"
        changed["execution_envelope"]["request_fingerprint"] = build_orchestration_plan(
            validate_manifest(changed["manifest"])
        )["request_fingerprint"]
        changed = validate_local_ingress_bundle(changed)
        report, success = apply_publish(self.config, changed, now=NOW)
        self.assertFalse(success)
        self.assertEqual("blocked", report["status"])
        self.assertIn("ingress_identity_conflict", report["reason_codes"])

        path = ingress_path(self.config, self.bundle["source_event_id"])
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(
            LocalIngressError, "ingress_record_permissions_invalid"
        ):
            load_published_bundle(self.config, self.bundle["source_event_id"])

    def test_insecure_input_and_ingress_directory_fail_closed(self) -> None:
        input_path = self.write_bundle()
        os.chmod(input_path, 0o644)
        with self.assertRaisesRegex(
            LocalIngressError, "ingress_input_permissions_invalid"
        ):
            load_local_ingress_bundle(input_path)

        _, success = apply_publish(self.config, self.bundle, now=NOW)
        self.assertTrue(success)
        os.chmod(ingress_path(self.config, "operator-event-one").parent, 0o755)
        with self.assertRaisesRegex(
            LocalIngressError, "ingress_directory_permissions_invalid"
        ):
            load_published_bundle(self.config, "operator-event-one")

    def test_expired_or_overlong_bundle_is_not_published(self) -> None:
        expired = copy.deepcopy(self.bundle)
        expired["created_at"] = "2029-12-30T00:00:00+00:00"
        expired["expires_at"] = "2029-12-31T00:00:00+00:00"
        expired = validate_local_ingress_bundle(expired)
        report, success = apply_publish(self.config, expired, now=NOW)
        self.assertFalse(success)
        self.assertIn("ingress_expired", report["reason_codes"])
        self.assertFalse(ingress_path(self.config, expired["source_event_id"]).exists())

        overlong = copy.deepcopy(self.bundle)
        overlong["expires_at"] = "2030-01-02T00:00:01+00:00"
        report = build_publish_preview(
            self.config, validate_local_ingress_bundle(overlong), now=NOW
        )
        self.assertIn("ingress_ttl_exceeds_limit", report["reason_codes"])

    def test_reconciliation_state_is_durable_sanitized_and_shadow_only(self) -> None:
        _, success = apply_publish(self.config, self.bundle, now=NOW)
        self.assertTrue(success)
        preview = build_local_reconciliation(
            self.config, self.bundle["source_event_id"], now=NOW
        )
        self.assertEqual("eligible_shadow", preview["decision_status"])
        self.assertFalse(
            reconciliation_state_path(self.config, "operator-event-one").exists()
        )

        report, success = apply_local_reconciliation(
            self.config, self.bundle["source_event_id"], now=NOW
        )
        self.assertTrue(success)
        self.assertTrue(report["mutation"]["applied"])
        state_path = reconciliation_state_path(self.config, "operator-event-one")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("shadow", state["phase"])
        self.assertEqual(1, state["observation_count"])
        encoded = json.dumps(state)
        for private in (
            self.bundle["execution_envelope"]["prompt"],
            self.bundle["execution_envelope"]["cwd"],
            self.bundle["execution_envelope"]["origin_parent_ref"],
        ):
            self.assertNotIn(private, encoded)
        self.assertFalse(self.config.queue_dir.exists())

        repeated, success = apply_local_reconciliation(
            self.config, self.bundle["source_event_id"], now=NOW
        )
        self.assertTrue(success)
        self.assertEqual(2, repeated["reconciliation_state"]["observation_count"])

    def test_insufficient_evidence_can_be_observed_but_never_dispatched(self) -> None:
        bundle = self.bundle_value(event_id="insufficient-event", evidence_successes=0)
        published, success = apply_publish(self.config, bundle, now=NOW)
        self.assertTrue(success)
        self.assertEqual("blocked", published["shadow_decision_status"])
        report, success = apply_local_reconciliation(
            self.config, bundle["source_event_id"], now=NOW
        )
        self.assertTrue(success)
        self.assertEqual("blocked", report["decision_status"])
        self.assertIn("evidence_floor_not_met", report["reason_codes"])
        self.assertFalse(self.config.queue_dir.exists())

    def test_malformed_reconciliation_state_is_preserved_as_conflict(self) -> None:
        _, success = apply_publish(self.config, self.bundle, now=NOW)
        self.assertTrue(success)
        _, success = apply_local_reconciliation(
            self.config, self.bundle["source_event_id"], now=NOW
        )
        self.assertTrue(success)
        path = reconciliation_state_path(self.config, self.bundle["source_event_id"])
        malformed = json.loads(path.read_text(encoding="utf-8"))
        malformed["unexpected"] = True
        path.write_text(json.dumps(malformed), encoding="utf-8")
        os.chmod(path, 0o600)
        report, success = apply_local_reconciliation(
            self.config, self.bundle["source_event_id"], now=NOW
        )
        self.assertFalse(success)
        self.assertIn("reconciliation_state_conflict", report["reason_codes"])
        self.assertEqual(malformed, json.loads(path.read_text(encoding="utf-8")))

    def test_cli_requires_exact_confirmation_and_does_not_dispatch(self) -> None:
        current = datetime.now(timezone.utc)
        live_bundle = copy.deepcopy(self.bundle)
        live_bundle["created_at"] = (current - timedelta(minutes=1)).isoformat()
        live_bundle["expires_at"] = (current + timedelta(hours=1)).isoformat()
        live_bundle = validate_local_ingress_bundle(live_bundle)
        bundle_path = self.write_bundle(live_bundle)
        base = (
            "--config",
            str(self.config_path),
            "orchestration",
            "publish-local-ingress",
            "--bundle",
            str(bundle_path),
        )
        code, report, _ = self.cli(*base, "--apply", "--json")
        self.assertEqual(2, code)
        self.assertEqual(["confirmation_required"], report["reason_codes"])
        code, report, _ = self.cli(
            *base,
            "--apply",
            "--confirm-source-event-id",
            "wrong",
            "--json",
        )
        self.assertEqual(2, code)
        self.assertEqual(["confirmation_mismatch"], report["reason_codes"])
        code, report, _ = self.cli(
            *base,
            "--apply",
            "--confirm-source-event-id",
            live_bundle["source_event_id"],
            "--json",
        )
        self.assertEqual(0, code)
        self.assertEqual("published", report["status"])
        self.assertFalse(self.config.queue_dir.exists())

        reconcile = (
            "--config",
            str(self.config_path),
            "orchestration",
            "reconcile-local-shadow",
            "--source-event-id",
            live_bundle["source_event_id"],
        )
        code, report, _ = self.cli(*reconcile, "--dry-run", "--json")
        self.assertEqual(0, code)
        self.assertEqual("eligible_shadow", report["decision_status"])
        self.assertFalse(
            reconciliation_state_path(
                self.config, live_bundle["source_event_id"]
            ).exists()
        )

    def guarded_bundle(self, *, event_id: str = "guarded-event") -> dict:
        bundle = self.bundle_value(event_id=event_id)
        bundle["policy"]["activation_mode"] = "guarded"
        return validate_local_ingress_bundle(bundle)

    def prepare_guarded_event(self, *, event_id: str = "guarded-event") -> dict:
        bundle = self.guarded_bundle(event_id=event_id)
        _, published = apply_publish(self.config, bundle, now=NOW)
        self.assertTrue(published)
        shadow, observed = apply_local_reconciliation(self.config, event_id, now=NOW)
        self.assertTrue(observed)
        self.assertEqual(["activation_not_implemented"], shadow["reason_codes"])
        return bundle

    def test_consumer_dry_run_requires_guarded_observation_and_is_read_only(
        self,
    ) -> None:
        bundle = self.prepare_guarded_event()
        before = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        report = build_consumer_preview(self.config, bundle["source_event_id"], now=NOW)
        after = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        self.assertEqual(before, after)
        self.assertEqual("ready", report["status"])
        self.assertEqual("eligible_shadow", report["activation"]["decision_status"])
        self.assertFalse(
            consumer_state_path(self.config, bundle["source_event_id"]).exists()
        )
        self.assertFalse(self.config.queue_dir.exists())

    def test_consumer_missing_shadow_observation_does_not_burn_event(self) -> None:
        bundle = self.guarded_bundle(event_id="missing-observation")
        _, published = apply_publish(self.config, bundle, now=NOW)
        self.assertTrue(published)
        report, success = apply_consumer(
            self.config, bundle["source_event_id"], now=NOW
        )
        self.assertFalse(success)
        self.assertEqual("blocked", report["status"])
        self.assertIn("reconciliation_state_not_found", report["reason_codes"])
        self.assertFalse(
            consumer_state_path(self.config, bundle["source_event_id"]).exists()
        )
        self.assertFalse(self.config.queue_dir.exists())

    def test_consumer_admits_once_and_writes_immutable_local_disposition(self) -> None:
        bundle = self.prepare_guarded_event()
        report, success = apply_consumer(
            self.config, bundle["source_event_id"], now=NOW
        )
        self.assertTrue(success)
        self.assertEqual("admitted", report["status"])
        self.assertEqual(1, len(list(self.config.queue_dir.glob("*.json"))))
        disposition = json.loads(
            disposition_path(self.config, report["trigger_id"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("admitted", disposition["result"])
        self.assertNotIn(
            bundle["execution_envelope"]["prompt"], json.dumps(disposition)
        )
        doctor = consumer_doctor_summary(self.config, now=NOW)
        self.assertEqual({"admitted": 1}, doctor["states_by_phase"])
        self.assertEqual(1, doctor["disposition_count"])
        self.assertEqual(0, doctor["invalid_record_count"])

        state_before = consumer_state_path(
            self.config, bundle["source_event_id"]
        ).read_bytes()
        disposition_before = disposition_path(
            self.config, report["trigger_id"]
        ).read_bytes()
        repeated, success = apply_consumer(
            self.config, bundle["source_event_id"], now=NOW
        )
        self.assertTrue(success)
        self.assertEqual("admitted", repeated["status"])
        self.assertEqual({"allowed": False, "applied": False}, repeated["mutation"])
        self.assertEqual(
            state_before,
            consumer_state_path(self.config, bundle["source_event_id"]).read_bytes(),
        )
        self.assertEqual(
            disposition_before,
            disposition_path(self.config, report["trigger_id"]).read_bytes(),
        )
        self.assertEqual(1, len(list(self.config.queue_dir.glob("*.json"))))

    def test_consumer_retry_is_bounded_and_duplicate_call_respects_backoff(
        self,
    ) -> None:
        bundle = self.prepare_guarded_event(event_id="retry-event")
        with patch(
            "codex_batch_runner.orchestration_consumer.apply_dispatch",
            side_effect=DispatchLockBusy(),
        ):
            first, success = apply_consumer(
                self.config, bundle["source_event_id"], now=NOW
            )
            self.assertFalse(success)
            self.assertEqual("retry_wait", first["status"])
            duplicate, success = apply_consumer(
                self.config, bundle["source_event_id"], now=NOW
            )
            self.assertFalse(success)
            self.assertEqual("retry_wait", duplicate["status"])
            second, _ = apply_consumer(
                self.config,
                bundle["source_event_id"],
                now=NOW + timedelta(seconds=31),
            )
            self.assertEqual("retry_wait", second["status"])
            exhausted, _ = apply_consumer(
                self.config,
                bundle["source_event_id"],
                now=NOW + timedelta(seconds=152),
            )
            self.assertEqual("exhausted", exhausted["status"])
        self.assertFalse(self.config.queue_dir.exists())

    def test_consumer_shadow_policy_and_malformed_state_fail_closed(self) -> None:
        bundle = self.bundle_value(event_id="shadow-policy-event")
        _, published = apply_publish(self.config, bundle, now=NOW)
        self.assertTrue(published)
        _, observed = apply_local_reconciliation(
            self.config, bundle["source_event_id"], now=NOW
        )
        self.assertTrue(observed)
        report, success = apply_consumer(
            self.config, bundle["source_event_id"], now=NOW
        )
        self.assertFalse(success)
        self.assertEqual("blocked", report["status"])
        self.assertIn("consumer_activation_mode_not_guarded", report["reason_codes"])
        self.assertFalse(self.config.queue_dir.exists())

        guarded = self.prepare_guarded_event(event_id="malformed-consumer-state")
        path = consumer_state_path(self.config, guarded["source_event_id"])
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text('{"schema_version": 1}', encoding="utf-8")
        os.chmod(path, 0o600)
        with self.assertRaisesRegex(ConsumerError, "consumer_state_invalid"):
            build_consumer_preview(self.config, guarded["source_event_id"], now=NOW)
        self.assertFalse(
            disposition_path(
                self.config,
                trigger_id_for(SOURCE_ID, guarded["source_event_id"]),
            ).exists()
        )

    def test_runner_pause_retry_is_bounded_before_d2_call(self) -> None:
        bundle = self.prepare_guarded_event(event_id="paused-event")
        set_runner_pause(self.config, "maintenance", "test")
        first, _ = apply_consumer(self.config, bundle["source_event_id"], now=NOW)
        self.assertEqual("retry_wait", first["status"])
        second, _ = apply_consumer(
            self.config,
            bundle["source_event_id"],
            now=NOW + timedelta(seconds=31),
        )
        self.assertEqual("retry_wait", second["status"])
        exhausted, _ = apply_consumer(
            self.config,
            bundle["source_event_id"],
            now=NOW + timedelta(seconds=152),
        )
        self.assertEqual("exhausted", exhausted["status"])
        self.assertFalse(self.config.queue_dir.exists())

    def test_consumer_recovers_crash_after_d2_admission(self) -> None:
        bundle = self.prepare_guarded_event(event_id="crash-event")
        with patch(
            "codex_batch_runner.orchestration_consumer._finalize_admitted",
            side_effect=RuntimeError("synthetic crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
                apply_consumer(self.config, bundle["source_event_id"], now=NOW)
        self.assertEqual(1, len(list(self.config.queue_dir.glob("*.json"))))
        recovered, success = apply_consumer(
            self.config,
            bundle["source_event_id"],
            now=NOW + timedelta(seconds=121),
        )
        self.assertTrue(success)
        self.assertEqual("admitted", recovered["status"])
        self.assertEqual(1, len(list(self.config.queue_dir.glob("*.json"))))

    def test_consumer_recovers_existing_d2_after_third_attempt_crash(self) -> None:
        bundle = self.prepare_guarded_event(event_id="third-attempt-crash")
        with patch(
            "codex_batch_runner.orchestration_consumer.apply_dispatch",
            side_effect=DispatchLockBusy(),
        ):
            apply_consumer(self.config, bundle["source_event_id"], now=NOW)
            apply_consumer(
                self.config,
                bundle["source_event_id"],
                now=NOW + timedelta(seconds=31),
            )
        with patch(
            "codex_batch_runner.orchestration_consumer._finalize_admitted",
            side_effect=RuntimeError("synthetic third-attempt crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "third-attempt crash"):
                apply_consumer(
                    self.config,
                    bundle["source_event_id"],
                    now=NOW + timedelta(seconds=152),
                )
        recovered, success = apply_consumer(
            self.config,
            bundle["source_event_id"],
            now=NOW + timedelta(seconds=273),
        )
        self.assertTrue(success)
        self.assertEqual("admitted", recovered["status"])
        self.assertEqual(3, recovered["consumer"]["attempt_count"])
        self.assertEqual(1, len(list(self.config.queue_dir.glob("*.json"))))

    def test_cli_consumer_requires_confirmation(self) -> None:
        current = datetime.now(timezone.utc)
        bundle = self.guarded_bundle(event_id="cli-consumer-event")
        bundle["created_at"] = (current - timedelta(minutes=1)).isoformat()
        bundle["expires_at"] = (current + timedelta(hours=1)).isoformat()
        bundle = validate_local_ingress_bundle(bundle)
        _, published = apply_publish(self.config, bundle)
        self.assertTrue(published)
        _, observed = apply_local_reconciliation(self.config, bundle["source_event_id"])
        self.assertTrue(observed)
        base = (
            "--config",
            str(self.config_path),
            "orchestration",
            "consume-local-ingress",
            "--source-event-id",
            bundle["source_event_id"],
        )
        code, report, _ = self.cli(*base, "--apply", "--json")
        self.assertEqual(2, code)
        self.assertEqual(["confirmation_required"], report["reason_codes"])
        code, report, _ = self.cli(
            *base,
            "--apply",
            "--confirm-source-event-id",
            bundle["source_event_id"],
            "--json",
        )
        self.assertEqual(0, code)
        self.assertEqual("admitted", report["status"])


if __name__ == "__main__":
    unittest.main()

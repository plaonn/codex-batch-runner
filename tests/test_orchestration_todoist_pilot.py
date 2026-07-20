from __future__ import annotations

import contextlib
import hashlib
import io
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.fs import read_json, write_json_atomic
from codex_batch_runner.orchestration import (
    build_orchestration_plan,
    validate_manifest,
)
from codex_batch_runner.orchestration_dispatch import validate_execution_envelope
from codex_batch_runner.orchestration_guard import (
    guard_idempotency_key,
    trigger_id_for,
    validate_guard_policy,
)
from codex_batch_runner.orchestration_todoist_pilot import (
    BUNDLE_CONTRACT,
    SNAPSHOT_CONTRACT,
    SOURCE_CONTRACT,
    TodoistPilotContractError,
    build_trigger,
    reconcile_todoist_pilot,
    validate_local_request_bundle,
    validate_todoist_snapshot,
    validate_todoist_source,
)
from codex_batch_runner.queue import load_task, save_task
from tests import test_orchestration_guard


class TodoistPilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_orchestration_guard.OrchestrationGuardTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.runtime = self.fixture.runtime
        self.repo = self.fixture.repo
        self.task_id = "todoist-task-one"
        self.token = "approval-token-one"
        self.opt_in_at = "2026-07-21T00:00:00+00:00"
        event_digest = hashlib.sha256(
            (SNAPSHOT_CONTRACT + "\0" + self.task_id + "\0" + self.token).encode()
        ).hexdigest()[:32]
        self.trigger_id = trigger_id_for(
            self.fixture.source_id, "todoist-event-" + event_digest
        )

        manifest = dict(self.fixture.manifest)
        manifest["idempotency_key"] = guard_idempotency_key(self.trigger_id)
        self.manifest = validate_manifest(manifest)
        plan = build_orchestration_plan(self.manifest)
        envelope = dict(self.fixture.envelope)
        envelope["request_fingerprint"] = plan["request_fingerprint"]
        self.envelope = validate_execution_envelope(envelope)
        self.manifest_path = self.root / "manifest.json"
        self.envelope_path = self.root / "envelope.json"
        write_json_atomic(self.manifest_path, self.manifest)
        write_json_atomic(self.envelope_path, self.envelope)

        self.source = validate_todoist_source(
            {
                "schema_version": 1,
                "contract": SOURCE_CONTRACT,
                "active": True,
                "source_id": self.fixture.source_id,
                "adapter_revision": "adapter-v1",
                "account_id": "todoist-account",
                "project_id": "todoist-project",
                "parent_id": "todoist-parent",
                "required_label": "cbr-guarded",
                "require_unshared": True,
            }
        )
        self.snapshot = validate_todoist_snapshot(
            {
                "schema_version": 1,
                "contract": SNAPSHOT_CONTRACT,
                "task_id": self.task_id,
                "account_id": "todoist-account",
                "project_id": "todoist-project",
                "parent_id": "todoist-parent",
                "labels": ["cbr-guarded", "codex-managed"],
                "description": (
                    "Untrusted text api_key=never-emit\n"
                    'CBR-GUARDED-V1 {"request_id":"guard-request",'
                    '"opt_in_token":"approval-token-one",'
                    '"created_at":"2026-07-21T00:00:00+00:00"}'
                ),
                "checked": False,
                "shared": False,
                "observed_at": "2026-07-21T01:00:00+00:00",
            }
        )
        self.bundle = validate_local_request_bundle(
            {
                "schema_version": 1,
                "contract": BUNDLE_CONTRACT,
                "request_id": "guard-request",
                "manifest_path": str(self.manifest_path),
                "execution_envelope_path": str(self.envelope_path),
            }
        )
        self.policy = self.policy_value("shadow")
        self.disabled_config = self.config(False)
        self.enabled_config = self.config(True)

    def policy_value(self, mode: str) -> dict:
        value = self.fixture.policy_value(activation_mode=mode)
        return validate_guard_policy(value)

    def config(self, enabled: bool) -> Config:
        path = self.root / f"config-{enabled}.json"
        path.write_text(
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
                    "guarded_orchestration_enabled": enabled,
                }
            ),
            encoding="utf-8",
        )
        return Config.load(str(path))

    def write_cli_inputs(self) -> tuple[Path, Path, Path, Path]:
        source = self.root / "source.json"
        snapshot = self.root / "snapshot.json"
        bundle = self.root / "bundle.json"
        policy = self.root / "policy.json"
        for path, value in (
            (source, self.source),
            (snapshot, self.snapshot),
            (bundle, self.bundle),
            (policy, self.policy),
        ):
            write_json_atomic(path, value)
        return source, snapshot, bundle, policy

    def test_dry_run_is_strictly_read_only_and_redacted(self) -> None:
        before = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        report, success = reconcile_todoist_pilot(
            self.disabled_config,
            source=self.source,
            snapshot=self.snapshot,
            bundle=self.bundle,
            policy=self.policy,
            apply=False,
        )
        after = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        self.assertTrue(success)
        self.assertEqual(before, after)
        self.assertEqual("eligible_shadow", report["decision_status"])
        output = json.dumps(report)
        self.assertNotIn("never-emit", output)
        self.assertNotIn(str(self.manifest_path), output)
        self.assertFalse(report["mutation"]["durable_state"])

    def test_shadow_apply_persists_stable_trigger_and_reconciliation(self) -> None:
        first, success = reconcile_todoist_pilot(
            self.disabled_config,
            source=self.source,
            snapshot=self.snapshot,
            bundle=self.bundle,
            policy=self.policy,
            apply=True,
        )
        self.assertTrue(success)
        self.assertTrue(first["mutation"]["durable_state"])
        changed_snapshot = dict(self.snapshot)
        changed_snapshot["observed_at"] = "2026-07-21T02:00:00+00:00"
        second, success = reconcile_todoist_pilot(
            self.disabled_config,
            source=self.source,
            snapshot=changed_snapshot,
            bundle=self.bundle,
            policy=self.policy,
            apply=True,
        )
        self.assertTrue(success)
        self.assertEqual(first["trigger_id"], second["trigger_id"])
        trigger_files = list(
            (self.runtime / "orchestration-trigger-inbox").glob("*.json")
        )
        reconciliation_files = list(
            (self.runtime / "orchestration-reconciliation").glob("*.json")
        )
        self.assertEqual(1, len(trigger_files))
        self.assertEqual(1, len(reconciliation_files))
        self.assertEqual(self.opt_in_at, read_json(trigger_files[0])["created_at"])
        self.assertFalse((self.runtime / "tasks").exists())

    def test_guarded_mode_requires_runtime_enablement_and_confirmation(self) -> None:
        policy = self.policy_value("guarded")
        report, success = reconcile_todoist_pilot(
            self.disabled_config,
            source=self.source,
            snapshot=self.snapshot,
            bundle=self.bundle,
            policy=policy,
            apply=True,
        )
        self.assertFalse(success)
        self.assertIn("runtime_activation_disabled", report["reason_codes"])
        self.assertIn("explicit_confirmation_required", report["reason_codes"])
        self.assertFalse(self.runtime.exists())

    def test_guarded_apply_and_retry_admit_exactly_once(self) -> None:
        policy = self.policy_value("guarded")
        first, success = reconcile_todoist_pilot(
            self.enabled_config,
            source=self.source,
            snapshot=self.snapshot,
            bundle=self.bundle,
            policy=policy,
            apply=True,
            confirm_trigger_id=self.trigger_id,
        )
        self.assertTrue(success)
        self.assertTrue(first["mutation"]["queue_admission"])
        second, success = reconcile_todoist_pilot(
            self.enabled_config,
            source=self.source,
            snapshot=self.snapshot,
            bundle=self.bundle,
            policy=policy,
            apply=True,
            confirm_trigger_id=self.trigger_id,
        )
        self.assertTrue(success)
        self.assertFalse(second["mutation"]["queue_admission"])
        self.assertEqual(1, len(list((self.runtime / "tasks").glob("*.json"))))
        self.assertEqual(
            1,
            len(
                list((self.runtime / "orchestration-dispatch-receipts").glob("*.json"))
            ),
        )

    def test_concurrent_guarded_apply_never_duplicates_admission(self) -> None:
        policy = self.policy_value("guarded")
        reports: list[dict] = []
        barrier = threading.Barrier(8)

        def run() -> None:
            barrier.wait(timeout=5)
            report, _ = reconcile_todoist_pilot(
                self.enabled_config,
                source=self.source,
                snapshot=self.snapshot,
                bundle=self.bundle,
                policy=policy,
                apply=True,
                confirm_trigger_id=self.trigger_id,
            )
            reports.append(report)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(8, len(reports))
        self.assertEqual(
            1,
            sum(bool(report["mutation"]["queue_admission"]) for report in reports),
        )
        self.assertEqual(1, len(list((self.runtime / "tasks").glob("*.json"))))
        self.assertEqual(
            1,
            len(
                list((self.runtime / "orchestration-dispatch-receipts").glob("*.json"))
            ),
        )
        reconciliations = list(
            (self.runtime / "orchestration-reconciliation").glob("*.json")
        )
        self.assertEqual(1, len(reconciliations))
        self.assertEqual(
            "admitted", read_json(reconciliations[0])["state"]["queue_admission"]
        )

    def test_crash_after_dispatch_recovers_from_d2_identity(self) -> None:
        policy = self.policy_value("guarded")
        with patch(
            "codex_batch_runner.orchestration_todoist_pilot._persist_reconciliation",
            side_effect=OSError("injected crash"),
        ):
            with self.assertRaises(OSError):
                reconcile_todoist_pilot(
                    self.enabled_config,
                    source=self.source,
                    snapshot=self.snapshot,
                    bundle=self.bundle,
                    policy=policy,
                    apply=True,
                    confirm_trigger_id=self.trigger_id,
                )
        report, success = reconcile_todoist_pilot(
            self.enabled_config,
            source=self.source,
            snapshot=self.snapshot,
            bundle=self.bundle,
            policy=policy,
            apply=True,
            confirm_trigger_id=self.trigger_id,
        )
        self.assertTrue(success)
        self.assertEqual("admitted", report["state"]["queue_admission"])
        self.assertEqual(1, len(list((self.runtime / "tasks").glob("*.json"))))

    def test_terminal_observation_creates_withheld_disposition_only(self) -> None:
        policy = self.policy_value("guarded")
        report, success = reconcile_todoist_pilot(
            self.enabled_config,
            source=self.source,
            snapshot=self.snapshot,
            bundle=self.bundle,
            policy=policy,
            apply=True,
            confirm_trigger_id=self.trigger_id,
        )
        self.assertTrue(success)
        task_files = list((self.runtime / "tasks").glob("*.json"))
        task = load_task(self.enabled_config, task_files[0].stem)
        task["status"] = "completed"
        task["review_status"] = "unreviewed"
        task["completed_at"] = "2026-07-21T03:00:00+00:00"
        task["last_result"] = {"summary": "sanitized"}
        save_task(self.enabled_config, task)
        report, success = reconcile_todoist_pilot(
            self.enabled_config,
            source=self.source,
            snapshot=self.snapshot,
            bundle=self.bundle,
            policy=policy,
            apply=True,
            confirm_trigger_id=self.trigger_id,
        )
        self.assertTrue(success)
        self.assertEqual("completed", report["state"]["execution"])
        self.assertEqual("unreviewed", report["state"]["review"])
        self.assertEqual("withheld", report["state"]["source_disposition"])
        dispositions = list(
            (self.runtime / "orchestration-disposition-outbox").glob("*.json")
        )
        self.assertEqual(1, len(dispositions))
        record = read_json(dispositions[0])
        self.assertEqual("withheld", record["delivery"]["state"])
        self.assertEqual(
            "external_coordination_mutation_not_authorized",
            record["delivery"]["reason"],
        )
        self.assertFalse(report["mutation"]["todoist"])

    def test_source_and_opt_in_fail_closed_without_state(self) -> None:
        cases = []
        no_label = dict(self.snapshot)
        no_label["labels"] = ["codex-managed"]
        cases.append((no_label, "required_label_missing"))
        completed = dict(self.snapshot)
        completed["checked"] = True
        cases.append((completed, "task_already_completed"))
        ambiguous = dict(self.snapshot)
        ambiguous["description"] += "\n" + ambiguous["description"].splitlines()[-1]
        cases.append((ambiguous, "opt_in_record_ambiguous"))
        wrong_parent = dict(self.snapshot)
        wrong_parent["parent_id"] = "other-parent"
        cases.append((wrong_parent, "source_container_mismatch"))
        shared = dict(self.snapshot)
        shared["shared"] = True
        cases.append((shared, "shared_task_not_allowed"))
        wrong_account = dict(self.snapshot)
        wrong_account["account_id"] = "other-account"
        cases.append((wrong_account, "source_account_mismatch"))
        for snapshot, reason in cases:
            with self.subTest(reason=reason):
                report, success = reconcile_todoist_pilot(
                    self.disabled_config,
                    source=self.source,
                    snapshot=snapshot,
                    bundle=self.bundle,
                    policy=self.policy,
                    apply=True,
                )
                self.assertFalse(success)
                self.assertIn(reason, report["reason_codes"])
                self.assertFalse(report["mutation"]["durable_state"])
        self.assertFalse(self.runtime.exists())

    def test_existing_trigger_identity_drift_fails_closed(self) -> None:
        trigger = build_trigger(
            source=self.source,
            snapshot=self.snapshot,
            opt_in={
                "request_id": "guard-request",
                "opt_in_token": self.token,
                "created_at": self.opt_in_at,
            },
            policy=self.policy,
            manifest=self.manifest,
            envelope=self.envelope,
        )
        trigger["request_id"] = "drifted"
        path = self.runtime / "orchestration-trigger-inbox" / f"{self.trigger_id}.json"
        write_json_atomic(path, trigger)
        report, success = reconcile_todoist_pilot(
            self.disabled_config,
            source=self.source,
            snapshot=self.snapshot,
            bundle=self.bundle,
            policy=self.policy,
            apply=True,
        )
        self.assertFalse(success)
        self.assertIn("trigger_state_conflict", report["reason_codes"])
        self.assertFalse((self.runtime / "tasks").exists())

    def test_corrupt_reconciliation_is_not_silently_repaired(self) -> None:
        report, success = reconcile_todoist_pilot(
            self.disabled_config,
            source=self.source,
            snapshot=self.snapshot,
            bundle=self.bundle,
            policy=self.policy,
            apply=True,
        )
        self.assertTrue(success)
        path = (
            self.runtime
            / "orchestration-reconciliation"
            / f"{report['trigger_id']}.json"
        )
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(TodoistPilotContractError):
            reconcile_todoist_pilot(
                self.disabled_config,
                source=self.source,
                snapshot=self.snapshot,
                bundle=self.bundle,
                policy=self.policy,
                apply=True,
            )
        self.assertEqual("{", path.read_text(encoding="utf-8"))

    def test_cli_output_redacts_snapshot_and_bundle(self) -> None:
        inputs = self.write_cli_inputs()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "--config",
                    str(self.root / "config-False.json"),
                    "orchestration",
                    "reconcile-todoist",
                    "--source",
                    str(inputs[0]),
                    "--snapshot",
                    str(inputs[1]),
                    "--bundle",
                    str(inputs[2]),
                    "--policy",
                    str(inputs[3]),
                    "--dry-run",
                    "--json",
                ]
            )
        self.assertEqual(0, code)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("never-emit", output)
        self.assertNotIn(str(self.manifest_path), output)
        self.assertNotIn(self.task_id, output)
        self.assertIn(self.trigger_id, output)


if __name__ == "__main__":
    unittest.main()

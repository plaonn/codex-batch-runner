from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.events import list_events
from codex_batch_runner.fs import (
    read_json,
    write_json_atomic,
    write_json_atomic_create,
)
from codex_batch_runner.orchestration import build_orchestration_plan, validate_manifest
from codex_batch_runner.orchestration_dispatch import (
    IMMUTABLE_RECEIPT_KEYS,
    VALIDATION_ORDER,
    ExecutionEnvelopeError,
    apply_dispatch,
    build_dispatch_preview,
    identity_for,
    load_execution_envelope,
    validate_execution_envelope,
)
from codex_batch_runner.parent_attention import list_parent_attention
from codex_batch_runner.queue import create_task, load_task, save_task, task_path
from codex_batch_runner.runner import emit_parent_attention_for_task


def cbr_manifest(**changes: object) -> dict:
    value = {
        "schema_version": 1,
        "contract": "orchestration-intake-v1",
        "request_id": "dispatch-request",
        "idempotency_key": "dispatch-key",
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
    return value


class OrchestrationDispatchTests(unittest.TestCase):
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
        self.manifest = validate_manifest(cbr_manifest())
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
                    "labels": ["safe-b", "safe-a"],
                    "depends_on": [],
                    "verification_scope": ["unit", "docs"],
                    "capacity_pool": "codex",
                    "priority": "normal",
                },
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_inputs(self) -> tuple[Path, Path]:
        manifest_path = self.root / "manifest.json"
        envelope_path = self.root / "private-envelope.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        envelope_path.write_text(json.dumps(self.envelope), encoding="utf-8")
        return manifest_path, envelope_path

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

    def test_envelope_normalization_and_exact_validation(self) -> None:
        self.assertEqual(["safe-a", "safe-b"], self.envelope["task"]["labels"])
        self.assertEqual(["docs", "unit"], self.envelope["task"]["verification_scope"])
        for code in VALIDATION_ORDER:
            with self.subTest(code=code):
                self.assertIn(code, VALIDATION_ORDER)
        invalid = json.loads(json.dumps(self.envelope))
        invalid["command"] = ["unsafe"]
        with self.assertRaises(ExecutionEnvelopeError) as caught:
            validate_execution_envelope(invalid)
        self.assertEqual(
            ("envelope_sensitive_field_forbidden",),
            caught.exception.codes,
        )

    def test_every_envelope_validation_code_has_real_input_evidence(self) -> None:
        cases: list[tuple[str, bytes | None]] = [
            ("envelope_unreadable", None),
            ("envelope_too_large", b"x" * (256 * 1024 + 1)),
            ("envelope_not_utf8", b"\xff"),
            ("envelope_json_invalid", b"{"),
            ("envelope_not_object", b"[]"),
        ]
        for code, raw in cases:
            path = self.root / f"{code}.json"
            if raw is not None:
                path.write_bytes(raw)
            with (
                self.subTest(code=code),
                self.assertRaises(ExecutionEnvelopeError) as caught,
            ):
                load_execution_envelope(path)
            self.assertEqual((code,), caught.exception.codes)

        invalid_values: list[tuple[str, dict]] = []
        value = json.loads(json.dumps(self.envelope))
        value.pop("task")
        invalid_values.append(("envelope_fields_invalid", value))
        value = json.loads(json.dumps(self.envelope))
        value["task"]["labels"] = "bad"
        invalid_values.append(("envelope_value_type_invalid", value))
        value = json.loads(json.dumps(self.envelope))
        value["task"]["priority"] = "urgent"
        invalid_values.append(("envelope_value_enum_invalid", value))
        value = json.loads(json.dumps(self.envelope))
        value["origin_parent_ref"] = "x" * 513
        invalid_values.append(("envelope_value_bounds_invalid", value))
        value = json.loads(json.dumps(self.envelope))
        value["session_id"] = "private"
        invalid_values.append(("envelope_sensitive_field_forbidden", value))
        value = json.loads(json.dumps(self.envelope))
        value["task"]["labels"] = ["same", "same"]
        invalid_values.append(("envelope_duplicate_item", value))
        for code, value in invalid_values:
            with (
                self.subTest(code=code),
                self.assertRaises(ExecutionEnvelopeError) as caught,
            ):
                validate_execution_envelope(value)
            self.assertEqual((code,), caught.exception.codes)

    def test_dry_run_is_read_only_and_returns_exact_preview(self) -> None:
        manifest_path, envelope_path = self.write_inputs()
        before = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        code, report, stderr = self.cli(
            "--config",
            str(self.config_path),
            "orchestration",
            "dispatch-cbr",
            "--manifest",
            str(manifest_path),
            "--execution-envelope",
            str(envelope_path),
            "--dry-run",
            "--json",
        )
        after = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(before, after)
        self.assertEqual("ready", report["status"])
        self.assertEqual("unlocked_read_only", report["snapshot_consistency"])
        self.assertEqual(
            {
                "schema_version",
                "contract",
                "request_id",
                "dispatch_id",
                "request_fingerprint",
                "execution_fingerprint",
                "surface",
                "status",
                "task_id",
                "reason_codes",
                "admission_blockers",
                "task_present",
                "receipt_present",
                "origin_parent_linked",
                "snapshot_consistency",
                "mutation",
            },
            set(report),
        )

    def test_dry_run_never_calls_mutation_helpers(self) -> None:
        with (
            patch(
                "codex_batch_runner.orchestration_dispatch.create_task",
                side_effect=AssertionError("must not create"),
            ),
            patch(
                "codex_batch_runner.orchestration_dispatch.FileLock",
                side_effect=AssertionError("must not lock"),
            ),
        ):
            preview = build_dispatch_preview(self.config, self.manifest, self.envelope)
        self.assertEqual("ready", preview["status"])

    def test_confirmation_contract_is_structured(self) -> None:
        manifest_path, envelope_path = self.write_inputs()
        code, report, _ = self.cli(
            "--config",
            str(self.config_path),
            "orchestration",
            "dispatch-cbr",
            "--manifest",
            str(manifest_path),
            "--execution-envelope",
            str(envelope_path),
            "--dry-run",
            "--confirm-request-id",
            self.manifest["request_id"],
            "--json",
        )
        self.assertEqual(2, code)
        self.assertEqual(["confirmation_not_allowed"], report["reason_codes"])
        code, report, _ = self.cli(
            "--config",
            str(self.config_path),
            "orchestration",
            "dispatch-cbr",
            "--manifest",
            str(manifest_path),
            "--execution-envelope",
            str(envelope_path),
            "--apply",
            "--json",
        )
        self.assertEqual(2, code)
        self.assertEqual(["confirmation_required"], report["reason_codes"])

    def test_apply_retry_is_exactly_once_and_receipt_is_immutable(self) -> None:
        manifest_path, envelope_path = self.write_inputs()
        command = (
            "--config",
            str(self.config_path),
            "orchestration",
            "dispatch-cbr",
            "--manifest",
            str(manifest_path),
            "--execution-envelope",
            str(envelope_path),
            "--apply",
            "--confirm-request-id",
            self.manifest["request_id"],
            "--json",
        )
        with patch("codex_batch_runner.cli.run_post_mutation_trigger") as trigger:
            first_code, first, _ = self.cli(*command)
            second_code, second, _ = self.cli(*command)
        self.assertEqual((0, 0), (first_code, second_code))
        self.assertEqual(first, second)
        self.assertEqual("created", first["admission_result"])
        self.assertEqual(IMMUTABLE_RECEIPT_KEYS, set(first))
        self.assertEqual(2, trigger.call_count)
        self.assertEqual(1, len(list(self.config.queue_dir.glob("*.json"))))
        events = list_events(self.config, limit=0)
        admitted = [
            event
            for event in events
            if event["event_type"] == "orchestration_task_admitted"
        ]
        self.assertEqual(1, len(admitted))
        self.assertFalse(any(event["event_type"] == "task_created" for event in events))
        serialized_event = json.dumps(admitted[0])
        for private in (
            self.envelope["prompt"],
            self.envelope["cwd"],
            self.envelope["origin_parent_ref"],
            self.envelope["task"]["title"],
            self.envelope["task"]["project_id"],
        ):
            self.assertNotIn(private, serialized_event)

    def test_crash_after_task_write_recovers_without_duplicate(self) -> None:
        identity = identity_for(self.manifest, self.envelope)
        original = write_json_atomic_create

        def fail_receipt(path: Path, data: object) -> None:
            if "orchestration-dispatch-receipts" in str(path):
                raise OSError("injected receipt failure")
            original(path, data)

        with patch(
            "codex_batch_runner.orchestration_dispatch.write_json_atomic_create",
            side_effect=fail_receipt,
        ):
            with self.assertRaises(OSError):
                apply_dispatch(self.config, self.manifest, self.envelope)
        self.assertTrue(task_path(self.config, identity["task_id"]).exists())
        self.assertFalse(
            (
                self.config.log_dir.parent
                / "orchestration-dispatch-receipts"
                / f"{identity['dispatch_id']}.json"
            ).exists()
        )
        receipt, success = apply_dispatch(self.config, self.manifest, self.envelope)
        self.assertTrue(success)
        self.assertEqual("recovered", receipt["admission_result"])
        self.assertEqual(1, len(list(self.config.queue_dir.glob("*.json"))))
        admitted = [
            event
            for event in list_events(self.config, limit=0)
            if event["event_type"] == "orchestration_task_admitted"
        ]
        self.assertEqual(1, len(admitted))

    def test_concurrent_ordinary_create_never_clobbers_or_yields_false_receipt(
        self,
    ) -> None:
        for winner in ("ordinary", "d2"):
            manifest_value = cbr_manifest()
            manifest_value["idempotency_key"] = f"race-{winner}"
            manifest = validate_manifest(manifest_value)
            plan = build_orchestration_plan(manifest)
            envelope_value = json.loads(json.dumps(self.envelope))
            envelope_value["request_fingerprint"] = plan["request_fingerprint"]
            envelope = validate_execution_envelope(envelope_value)
            identity = identity_for(manifest, envelope)
            barrier = threading.Barrier(2)
            winner_published = threading.Event()
            original_create = write_json_atomic_create
            ordinary_result: dict[str, object] = {}

            def synchronized_create(path: Path, data: object) -> None:
                is_d2 = bool(
                    isinstance(data, dict) and data.get("orchestration_dispatch_id")
                )
                this_writer = "d2" if is_d2 else "ordinary"
                barrier.wait(timeout=5)
                if this_writer == winner:
                    try:
                        original_create(path, data)
                    finally:
                        winner_published.set()
                else:
                    self.assertTrue(winner_published.wait(timeout=5))
                    original_create(path, data)

            def ordinary_enqueue() -> None:
                try:
                    ordinary_result["task"] = create_task(
                        self.config,
                        "Ordinary competing prompt",
                        str(self.repo),
                        task_id=identity["task_id"],
                        project_id="ordinary-project",
                    )
                except Exception as exc:
                    ordinary_result["error"] = exc

            with (
                self.subTest(winner=winner),
                patch(
                    "codex_batch_runner.queue.write_json_atomic_create",
                    side_effect=synchronized_create,
                ),
            ):
                thread = threading.Thread(target=ordinary_enqueue)
                thread.start()
                report, success = apply_dispatch(self.config, manifest, envelope)
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            stored = load_task(self.config, identity["task_id"])
            receipt_path = (
                self.config.log_dir.parent
                / "orchestration-dispatch-receipts"
                / f"{identity['dispatch_id']}.json"
            )
            if winner == "d2":
                self.assertTrue(success)
                self.assertIsInstance(ordinary_result.get("error"), FileExistsError)
                self.assertEqual(envelope["prompt"], stored["prompt"])
                self.assertTrue(receipt_path.exists())
                self.assertEqual(identity["task_id"], report["task_id"])
            else:
                self.assertFalse(success)
                self.assertEqual("Ordinary competing prompt", stored["prompt"])
                self.assertEqual("conflict", report["status"])
                self.assertIn("task_identity_conflict", report["reason_codes"])
                self.assertFalse(receipt_path.exists())

    def test_receipt_barrier_rejects_task_missing_after_create(self) -> None:
        identity = identity_for(self.manifest, self.envelope)
        import codex_batch_runner.orchestration_dispatch as dispatch_module

        original_read = dispatch_module._read_task
        read_count = 0

        def remove_before_receipt(config: Config, task_id: str) -> object:
            nonlocal read_count
            read_count += 1
            if read_count == 2:
                task_path(config, task_id).unlink(missing_ok=True)
                return None
            return original_read(config, task_id)

        with patch(
            "codex_batch_runner.orchestration_dispatch._read_task",
            side_effect=remove_before_receipt,
        ):
            report, success = apply_dispatch(self.config, self.manifest, self.envelope)
        self.assertFalse(success)
        self.assertEqual("conflict", report["status"])
        self.assertIn("task_identity_conflict", report["reason_codes"])
        self.assertFalse(
            (
                self.config.log_dir.parent
                / "orchestration-dispatch-receipts"
                / f"{identity['dispatch_id']}.json"
            ).exists()
        )

    def test_identity_matrix_conflicts_fail_closed(self) -> None:
        receipt, success = apply_dispatch(self.config, self.manifest, self.envelope)
        self.assertTrue(success)
        identity = identity_for(self.manifest, self.envelope)
        task = load_task(self.config, identity["task_id"])
        task["prompt"] = "drifted"
        write_json_atomic(task_path(self.config, identity["task_id"]), task)
        preview = build_dispatch_preview(self.config, self.manifest, self.envelope)
        self.assertEqual("conflict", preview["status"])
        self.assertIn("task_identity_conflict", preview["reason_codes"])
        receipt_path = (
            self.config.log_dir.parent
            / "orchestration-dispatch-receipts"
            / f"{identity['dispatch_id']}.json"
        )
        task_path(self.config, identity["task_id"]).unlink()
        preview = build_dispatch_preview(self.config, self.manifest, self.envelope)
        self.assertEqual("conflict", preview["status"])
        self.assertIn("receipt_without_task", preview["reason_codes"])
        malformed = read_json(receipt_path)
        malformed["extra"] = True
        write_json_atomic(receipt_path, malformed)
        preview = build_dispatch_preview(self.config, self.manifest, self.envelope)
        self.assertIn("receipt_identity_conflict", preview["reason_codes"])

    def test_orchestrated_task_execution_identity_is_immutable(self) -> None:
        receipt, _ = apply_dispatch(self.config, self.manifest, self.envelope)
        task = load_task(self.config, receipt["task_id"])
        task["prompt"] = "changed"
        with self.assertRaisesRegex(ValueError, "immutable"):
            save_task(self.config, task)

    def test_pause_blocks_matching_retry_and_capacity_is_advisory(self) -> None:
        receipt, _ = apply_dispatch(self.config, self.manifest, self.envelope)
        self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            self.config.state_file,
            {"runner_pause": {"active": True, "reason": "maintenance"}},
        )
        preview = build_dispatch_preview(self.config, self.manifest, self.envelope)
        self.assertEqual("blocked", preview["status"])
        self.assertIn("runner_paused", preview["reason_codes"])
        report, success = apply_dispatch(self.config, self.manifest, self.envelope)
        self.assertFalse(success)
        self.assertEqual("blocked", report["status"])
        self.assertEqual(receipt["task_id"], report["task_id"])

    def test_authority_and_config_gates_are_exact(self) -> None:
        advisory = cbr_manifest()
        advisory["automation_boundary"] = "advisory_only"
        advisory["authority"]["decision_authority"] = "recommend_and_pause"
        advisory["mutation"]["allowed"] = ["local_files"]
        advisory["work"]["isolation"] = "none"
        advisory_manifest = validate_manifest(advisory)
        advisory_plan = build_orchestration_plan(advisory_manifest)
        advisory_envelope = dict(self.envelope)
        advisory_envelope["request_fingerprint"] = advisory_plan["request_fingerprint"]
        preview = build_dispatch_preview(
            self.config,
            advisory_manifest,
            validate_execution_envelope(advisory_envelope),
        )
        self.assertEqual("blocked", preview["status"])
        self.assertIn("decision_authority_incompatible", preview["reason_codes"])
        self.assertIn("automation_boundary_incompatible", preview["reason_codes"])

    def test_authority_automation_dispatch_table(self) -> None:
        combinations = (
            ("proposal_only", "manual_only", False),
            ("proposal_only", "advisory_only", False),
            ("recommend_and_pause", "manual_only", False),
            ("recommend_and_pause", "advisory_only", False),
            ("delegated_decision", "manual_only", True),
            ("delegated_decision", "advisory_only", False),
            ("delegated_decision", "bounded_automatic", True),
            ("bounded_experiment", "manual_only", True),
            ("bounded_experiment", "advisory_only", False),
            ("bounded_experiment", "bounded_automatic", True),
        )
        for authority, boundary, allowed in combinations:
            value = cbr_manifest()
            value["authority"]["decision_authority"] = authority
            value["automation_boundary"] = boundary
            if authority == "proposal_only":
                value["mutation"]["allowed"] = ["read_only"]
                value["mutation"]["prohibited"] = [
                    "runtime_state",
                    "external_state",
                    "destructive",
                ]
            elif authority == "recommend_and_pause":
                value["mutation"]["allowed"] = ["local_files"]
            manifest = validate_manifest(value)
            plan = build_orchestration_plan(manifest)
            envelope = json.loads(json.dumps(self.envelope))
            envelope["request_fingerprint"] = plan["request_fingerprint"]
            preview = build_dispatch_preview(
                self.config,
                manifest,
                validate_execution_envelope(envelope),
            )
            with self.subTest(authority=authority, boundary=boundary):
                self.assertEqual("ready" if allowed else "blocked", preview["status"])

    def test_parent_linkage_produces_attention_only_on_completion(self) -> None:
        receipt, _ = apply_dispatch(self.config, self.manifest, self.envelope)
        task = load_task(self.config, receipt["task_id"])
        self.assertEqual([], list_parent_attention(self.config))
        task["status"] = "completed"
        task["completed_at"] = "2026-01-01T00:00:00+00:00"
        task["last_result"] = {"summary": "completed"}
        save_task(self.config, task)
        emit_parent_attention_for_task(self.config, task, "needs_review")
        attention = list_parent_attention(self.config)
        self.assertEqual(1, len(attention))
        self.assertEqual("opaque-private-parent", attention[0]["parent_ref"])
        self.assertEqual("needs_review", attention[0]["wake_reason"])

    def test_public_outputs_do_not_disclose_private_envelope(self) -> None:
        manifest_path, envelope_path = self.write_inputs()
        code, report, stderr = self.cli(
            "--config",
            str(self.config_path),
            "orchestration",
            "dispatch-cbr",
            "--manifest",
            str(manifest_path),
            "--execution-envelope",
            str(envelope_path),
            "--dry-run",
            "--json",
        )
        self.assertEqual(0, code)
        serialized = json.dumps(report) + stderr
        for private in (
            self.envelope["prompt"],
            self.envelope["cwd"],
            self.envelope["origin_parent_ref"],
        ):
            self.assertNotIn(private, serialized)

    def test_trigger_is_zero_for_dry_run_blocked_conflict_and_lock_busy(self) -> None:
        manifest_path, envelope_path = self.write_inputs()
        base = (
            "--config",
            str(self.config_path),
            "orchestration",
            "dispatch-cbr",
            "--manifest",
            str(manifest_path),
            "--execution-envelope",
            str(envelope_path),
        )
        with patch("codex_batch_runner.cli.run_post_mutation_trigger") as trigger:
            self.cli(*base, "--dry-run", "--json")
            self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(
                self.config.state_file,
                {"runner_pause": {"active": True, "reason": "maintenance"}},
            )
            code, report, _ = self.cli(
                *base,
                "--apply",
                "--confirm-request-id",
                self.manifest["request_id"],
                "--json",
            )
        self.assertEqual(2, code)
        self.assertEqual("blocked", report["decision_status"])
        trigger.assert_not_called()

    def test_binding_failure_precedes_confirmation_and_config_with_null_ids(
        self,
    ) -> None:
        manifest_path, envelope_path = self.write_inputs()
        for field, value, reason in (
            ("request_id", "different-request", "request_id_mismatch"),
            (
                "request_fingerprint",
                "sha256:" + "0" * 64,
                "request_fingerprint_mismatch",
            ),
        ):
            changed = json.loads(envelope_path.read_text(encoding="utf-8"))
            changed[field] = value
            envelope_path.write_text(json.dumps(changed), encoding="utf-8")
            with patch(
                "codex_batch_runner.cli.Config.load",
                side_effect=AssertionError("config must not load"),
            ) as load:
                code, report, _ = self.cli(
                    "--config",
                    str(self.root / "invalid-config.json"),
                    "orchestration",
                    "dispatch-cbr",
                    "--manifest",
                    str(manifest_path),
                    "--execution-envelope",
                    str(envelope_path),
                    "--dry-run",
                    "--confirm-request-id",
                    "wrong-confirmation",
                    "--json",
                )
            self.assertEqual(2, code)
            self.assertIn(reason, report["reason_codes"])
            self.assertIsNone(report["request_id"])
            self.assertIsNone(report["dispatch_id"])
            load.assert_not_called()
            envelope_path.write_text(json.dumps(self.envelope), encoding="utf-8")

    def test_plan_and_authority_gates_precede_confirmation_and_config(self) -> None:
        cases: list[tuple[dict, str]] = []
        subagent = cbr_manifest()
        subagent["work"].update(
            {
                "duration": "short",
                "persistence": "turn_bound",
                "resume": "not_needed",
                "dependency": "none",
                "collection": "immediate_parent",
            }
        )
        subagent["surface_preferences"] = ["codex_subagent"]
        cases.append((subagent, "recommended_surface_not_cbr_batch"))
        advisory = cbr_manifest()
        advisory["authority"]["decision_authority"] = "recommend_and_pause"
        advisory["automation_boundary"] = "advisory_only"
        advisory["mutation"]["allowed"] = ["local_files"]
        cases.append((advisory, "decision_authority_incompatible"))

        for index, (manifest_value, expected_reason) in enumerate(cases):
            manifest = validate_manifest(manifest_value)
            plan = build_orchestration_plan(manifest)
            envelope = json.loads(json.dumps(self.envelope))
            envelope["request_id"] = manifest["request_id"]
            envelope["request_fingerprint"] = plan["request_fingerprint"]
            manifest_path = self.root / f"gate-manifest-{index}.json"
            envelope_path = self.root / f"gate-envelope-{index}.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
            with patch(
                "codex_batch_runner.cli.Config.load",
                side_effect=AssertionError("config must not load"),
            ) as load:
                code, report, _ = self.cli(
                    "--config",
                    str(self.root / "invalid-config.json"),
                    "orchestration",
                    "dispatch-cbr",
                    "--manifest",
                    str(manifest_path),
                    "--execution-envelope",
                    str(envelope_path),
                    "--apply",
                    "--json",
                )
            self.assertEqual(2, code)
            self.assertIn(expected_reason, report["reason_codes"])
            self.assertEqual(manifest["request_id"], report["request_id"])
            self.assertIsNotNone(report["dispatch_id"])
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.execution_target_selector_decision_envelope import (
    ENVELOPE_CONTRACT,
    MUTATION_FIELDS,
    PRODUCER_ID,
    PRODUCER_REVISION,
    ExecutionTargetSelectorDecisionEnvelopeError,
    build_execution_target_selector_decision_envelope,
    stable_digest,
    validate_execution_target_selector_decision_envelope,
)
from codex_batch_runner.cli import main
from tests.test_capacity_reservation_feedback_simulation import build_request


def request() -> dict:
    source = build_request()
    return copy.deepcopy(
        source["selector_binding"]["decision_envelopes"][0]["producer_request"]
    )


def set_override(source: dict, override: dict | None) -> None:
    manual = source["manual_override_source"]
    manual["status"] = "authoritative_absence" if override is None else "present"
    manual["source_projection"]["routing_override"] = copy.deepcopy(override)
    manual["source_projection_digest"] = stable_digest(manual["source_projection"])
    currentness = source["currentness"]
    currentness["source_projection_digest"] = manual["source_projection_digest"]
    body = copy.deepcopy(currentness)
    body.pop("currentness_digest")
    currentness["currentness_digest"] = stable_digest(body)


def override(*, mode: str, target_id: str, allow_fallback: bool = False) -> dict:
    return {
        "mode": mode,
        "target_id": target_id,
        "reason": "bounded-operator-choice",
        "scope": "single_task",
        "allow_fallback": allow_fallback,
        "provenance": "operator_override",
    }


class ExecutionTargetSelectorDecisionEnvelopeTests(unittest.TestCase):
    def test_self_asserted_authoritative_absence_is_unattested(self) -> None:
        source = request()
        envelope = build_execution_target_selector_decision_envelope(source)
        self.assertEqual(ENVELOPE_CONTRACT, envelope["contract"])
        self.assertEqual("unattested", envelope["disposition"])
        self.assertEqual(
            ["manual_override_source_not_trusted"], envelope["reason_codes"]
        )
        self.assertIsNone(envelope["selected_target_id"])
        self.assertTrue(envelope["report_only"])
        self.assertTrue(envelope["simulation_only"])
        for field in (
            "activation_authority",
            "selection_authority",
            "dispatch_authority",
            "runtime_reservation",
            "runtime_feedback_mutation",
            "automatic_half_open",
            "automatic_retry",
            "queue_mutation",
            "config_mutation",
            "cooldown_mutation",
            "wake_mutation",
            "provider_call",
            "promotion_authority",
        ):
            self.assertFalse(envelope[field])
        for field in MUTATION_FIELDS:
            self.assertEqual([], envelope[field])

    def test_ordering_v1_report_bytes_and_shape_are_unchanged(self) -> None:
        source = request()
        before = json.dumps(
            source["baseline_report"], sort_keys=True, separators=(",", ":")
        ).encode()
        envelope = build_execution_target_selector_decision_envelope(source)
        after = json.dumps(
            source["baseline_report"], sort_keys=True, separators=(",", ":")
        ).encode()
        embedded = json.dumps(
            envelope["producer_request"]["baseline_report"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.assertEqual(before, after)
        self.assertEqual(before, embedded)

    def test_exact_preference_and_pin_precede_capacity(self) -> None:
        for mode in ("preference", "pin"):
            with self.subTest(mode=mode):
                source = request()
                target = source["baseline_report"]["baseline_order"][-1]
                set_override(source, override(mode=mode, target_id=target))
                envelope = build_execution_target_selector_decision_envelope(source)
                self.assertEqual("operator_" + mode, envelope["disposition"])
                self.assertEqual(target, envelope["selected_target_id"])

    def test_preference_fallback_uses_immutable_automatic_baseline(self) -> None:
        source = request()
        set_override(
            source,
            override(
                mode="preference", target_id="target-unavailable", allow_fallback=True
            ),
        )
        envelope = build_execution_target_selector_decision_envelope(source)
        self.assertEqual("operator_preference_fallback", envelope["disposition"])
        self.assertEqual(
            source["baseline_report"]["baseline"]["selected_target_id"],
            envelope["selected_target_id"],
        )

    def test_unavailable_pin_and_disabled_fallback_fail_closed(self) -> None:
        cases = (
            (
                override(mode="pin", target_id="target-unavailable"),
                "manual_pin_unavailable",
            ),
            (
                override(mode="preference", target_id="target-unavailable"),
                "explicit_fallback_exhausted",
            ),
        )
        for value, reason in cases:
            with self.subTest(reason=reason):
                source = request()
                set_override(source, value)
                envelope = build_execution_target_selector_decision_envelope(source)
                self.assertEqual("fail_closed", envelope["disposition"])
                self.assertIsNone(envelope["selected_target_id"])
                self.assertEqual([reason], envelope["reason_codes"])

    def test_task_attempt_scope_revision_and_stale_drift_rejected(self) -> None:
        cases = []
        task = request()
        task["task"]["task_id"] = "task-drift"
        cases.append(task)
        attempt = request()
        attempt["task"]["attempt"] = 2
        cases.append(attempt)
        scope = request()
        scope["scope"]["project_id"] = "project-drift"
        cases.append(scope)
        revision = request()
        revision["selector_inputs"]["selector_policy_revision"] = "selector-drift"
        cases.append(revision)
        stale = request()
        stale["currentness"]["expires_at"] = stale["evaluated_at"]
        stale_body = copy.deepcopy(stale["currentness"])
        stale_body.pop("currentness_digest")
        stale["currentness"]["currentness_digest"] = stable_digest(stale_body)
        cases.append(stale)
        for source in cases:
            with self.subTest():
                with self.assertRaises(ExecutionTargetSelectorDecisionEnvelopeError):
                    build_execution_target_selector_decision_envelope(source)

    def test_self_asserted_absence_empty_override_unknown_and_forgery_rejected(
        self,
    ) -> None:
        wrong_producer = request()
        wrong_producer["manual_override_source"]["producer_id"] = "caller"
        empty = request()
        empty["manual_override_source"]["source_projection"]["routing_override"] = {}
        empty["manual_override_source"]["source_projection_digest"] = stable_digest(
            empty["manual_override_source"]["source_projection"]
        )
        unknown = request()
        unknown["manual_override_source"]["unexpected"] = True
        for source in (wrong_producer, empty, unknown):
            with self.subTest():
                with self.assertRaises(ExecutionTargetSelectorDecisionEnvelopeError):
                    build_execution_target_selector_decision_envelope(source)

        envelope = build_execution_target_selector_decision_envelope(request())
        forged = copy.deepcopy(envelope)
        forged["reason_codes"] = ["forged"]
        with self.assertRaises(ExecutionTargetSelectorDecisionEnvelopeError):
            validate_execution_target_selector_decision_envelope(forged)

    def test_baseline_report_and_digest_mismatch_rejected(self) -> None:
        source = request()
        source["baseline_report"]["baseline_order"].reverse()
        with self.assertRaises(ExecutionTargetSelectorDecisionEnvelopeError):
            build_execution_target_selector_decision_envelope(source)

        envelope = build_execution_target_selector_decision_envelope(request())
        envelope["baseline_binding"]["report_digest"] = "sha256:" + ("a" * 64)
        with self.assertRaises(ExecutionTargetSelectorDecisionEnvelopeError):
            validate_execution_target_selector_decision_envelope(envelope)

    def test_canonical_routing_override_semantics_are_reused(self) -> None:
        source = request()
        invalid = override(mode="pin", target_id="target-a", allow_fallback=True)
        set_override(source, invalid)
        with self.assertRaises(ExecutionTargetSelectorDecisionEnvelopeError):
            build_execution_target_selector_decision_envelope(source)

    def test_bool_integer_aliases_are_rejected(self) -> None:
        for path in ("schema", "attempt"):
            source = request()
            if path == "schema":
                source["schema_version"] = True
            else:
                source["task"]["attempt"] = True
            with self.subTest(path=path):
                with self.assertRaises(ExecutionTargetSelectorDecisionEnvelopeError):
                    build_execution_target_selector_decision_envelope(source)

    def test_fixed_producer_identity_is_bound(self) -> None:
        envelope = build_execution_target_selector_decision_envelope(request())
        self.assertEqual(PRODUCER_ID, envelope["currentness_binding"]["producer_id"])
        self.assertEqual(
            PRODUCER_REVISION,
            envelope["currentness_binding"]["producer_revision"],
        )

    def test_standalone_cli_uses_no_config_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_path = root / "selector-envelope-request.json"
            request_path.write_text(json.dumps(request()), encoding="utf-8")
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    0,
                    main(
                        [
                            "execution-target-selector-decision-envelope",
                            "--request-json",
                            str(request_path),
                            "--json",
                        ]
                    ),
                )
            self.assertEqual(
                before, {path.name: path.read_bytes() for path in root.iterdir()}
            )
            self.assertEqual(
                ENVELOPE_CONTRACT, json.loads(stdout.getvalue())["contract"]
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    2,
                    main(
                        [
                            "--config",
                            str(root / "must-not-load.json"),
                            "execution-target-selector-decision-envelope",
                            "--request-json",
                            str(request_path),
                        ]
                    ),
                )
            self.assertIn("--config is not supported", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

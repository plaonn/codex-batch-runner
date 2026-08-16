from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.config import Config
from codex_batch_runner.execution_evidence_adapter import (
    parse_evidence_source_file,
    validate_evidence_source,
)
from codex_batch_runner.post_accept import accept_task_and_integrate
from codex_batch_runner.queue import create_task, load_task, save_task, set_review_status
from codex_batch_runner.runner import run_next


CODEX_FIXTURE = Path(__file__).parent / "fixtures" / "fake_codex.py"


FAKE_ADAPTER = r'''
import json
import os
import subprocess
import sys
from pathlib import Path


def value(args, name):
    return args[args.index(name) + 1]


def source(args):
    return {"kind": value(args, "--source-kind"), "id": value(args, "--source-id"), "revision": value(args, "--source-revision")}


def append(path, payload):
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def main():
    args = sys.argv[1:]
    state_file = value(args, "--state-file")
    execution_id = value(args, "--execution-id")
    item_source = source(args)
    if args[0] == "run":
        env = os.environ.copy()
        env.update({
            "CHAMBER_CORRELATION_KIND": item_source["kind"],
            "CHAMBER_CORRELATION_ID": item_source["id"],
            "CHAMBER_CORRELATION_REVISION": item_source["revision"],
            "CHAMBER_EXECUTION_ID": execution_id,
        })
        child = subprocess.run(args[args.index("--") + 1:], env=env, check=False)
        append(state_file, {"kind": "run", "execution_id": execution_id, "source": item_source, "env": {key: env[key] for key in env if key.startswith("CHAMBER_")}})
        if "--omit-result" not in args:
            result = {
                "schema": "chamber-orca-evidence.v1",
                "execution_id": execution_id,
                "source": item_source,
                "child_exit_code": child.returncode,
                "acceptance_status": "unknown",
                "acceptance_state": "pending-authority",
                "outcome_status": None,
                "outcome_producer": None,
                "settlement_attempted": False,
                "settlement_status": None,
                "settlement_recorded": False,
                "settlement_disposition": "pending-authority",
            }
            Path(value(args, "--result-file")).write_text(json.dumps(result), encoding="utf-8")
            print("CHAMBER_EVIDENCE_RESULT " + json.dumps(result), flush=True)
        return child.returncode
    if args[0] == "settle":
        status = value(args, "--status")
        append(state_file, {"kind": "settle", "execution_id": execution_id, "source": item_source, "status": status, "producer": value(args, "--producer")})
        print(json.dumps({
            "schema": "chamber-orca-evidence-settle.v1",
            "execution_id": execution_id,
            "settlement_status": status,
            "settlement_recorded": True,
            "settlement_disposition": "recorded",
        }), flush=True)
        return 0
    return 2


raise SystemExit(main())
'''


RETRY_CODEX = r'''
import json
import pathlib
import sys

counter = pathlib.Path(sys.argv[1])
count = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(count))
prompt = sys.argv[-1]
task_id = next((line.split(":", 1)[1].strip() for line in prompt.splitlines() if line.startswith("task_id:")), "unknown")
if count == 1:
    raise SystemExit(1)
print(json.dumps({"type": "turn.completed", "response": {
    "task_id": task_id,
    "status": "completed",
    "summary": "retry completed",
    "next_prompt": "",
    "changed_files": [],
    "verification": ["retry verification"],
}}), flush=True)
'''


class AutomaticEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict("os.environ", {"CBR_CONFIG": ""}, clear=False)
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

    def source(self) -> dict[str, str]:
        return {"kind": "todoist-task", "id": "source-123", "revision": "rev-7"}

    def make_config(
        self,
        root: Path,
        adapter: Path,
        state_file: Path,
        *,
        codex_command: list[str] | None = None,
        omit_result: bool = False,
    ) -> Config:
        base = Config.load(root=root)
        run_arguments = ["--state-file", str(state_file)]
        if omit_result:
            run_arguments.insert(0, "--omit-result")
        return replace(
            base,
            codex_command=codex_command or [sys.executable, str(CODEX_FIXTURE), "success"],
            codex_resume_command=codex_command or [sys.executable, str(CODEX_FIXTURE), "success", "resume", "{session_id}"],
            default_max_attempts=3,
            execution_evidence_adapter={
                "command": [sys.executable, str(adapter)],
                "run_arguments": run_arguments,
                "settle_arguments": ["--state-file", str(state_file)],
                "timeout_seconds": 10,
                "producer": "execution-controller",
            },
        )

    def write_adapter(self, root: Path) -> Path:
        path = root / "fake_adapter.py"
        path.write_text(FAKE_ADAPTER, encoding="utf-8")
        return path

    def write_retry_codex(self, root: Path) -> Path:
        path = root / "retry_codex.py"
        path.write_text(RETRY_CODEX, encoding="utf-8")
        return path

    def read_state(self, state_file: Path) -> list[dict]:
        if not state_file.exists():
            return []
        return [json.loads(line) for line in state_file.read_text(encoding="utf-8").splitlines() if line]

    def test_source_contract_is_exact_and_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.json"
            path.write_text(json.dumps(self.source()), encoding="utf-8")
            self.assertEqual(self.source(), parse_evidence_source_file(path))
            with self.assertRaises(ValueError):
                validate_evidence_source({"kind": "todoist-task", "id": "source-123"})

    def test_ordinary_run_binds_source_and_pending_acceptance_without_chamber_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "adapter-state.jsonl"
            config = self.make_config(root, self.write_adapter(root), state_file)
            create_task(config, "ordinary work", str(root), task_id="automatic", evidence_source=self.source())

            outcome = run_next(config, suppress_wake_hooks=True)

            self.assertEqual("completed", outcome.status)
            task = load_task(config, "automatic")
            self.assertEqual("completed", task["status"])
            self.assertEqual("unreviewed", task["review_status"])
            self.assertEqual(1, len(task["automatic_evidence_attempts"]))
            attempt = task["automatic_evidence_attempts"][0]
            self.assertEqual(self.source(), attempt["source"])
            self.assertEqual("pending-authority", attempt["acceptance_state"])
            self.assertFalse(attempt["settlement_recorded"])
            runs = self.read_state(state_file)
            self.assertEqual(["run"], [item["kind"] for item in runs])
            self.assertEqual(attempt["execution_id"], runs[0]["execution_id"])
            self.assertEqual(self.source(), runs[0]["source"])
            self.assertEqual(self.source()["id"], runs[0]["env"]["CHAMBER_CORRELATION_ID"])

    def test_explicit_review_is_the_later_authority_and_targets_latest_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "adapter-state.jsonl"
            config = self.make_config(root, self.write_adapter(root), state_file)
            create_task(config, "ordinary work", str(root), task_id="accepted", evidence_source=self.source())
            run_next(config, suppress_wake_hooks=True)

            accepted = accept_task_and_integrate(config, "accepted", "reviewed", source="test-review")

            self.assertEqual("accepted", accepted["task"]["review_status"])
            task = load_task(config, "accepted")
            callback = task["automatic_evidence_last_callback"]
            self.assertEqual("recorded", callback["status"])
            self.assertEqual("accepted", callback["requested_status"])
            self.assertEqual(task["automatic_evidence_attempts"][-1]["execution_id"], callback["execution_id"])
            settles = [item for item in self.read_state(state_file) if item["kind"] == "settle"]
            self.assertEqual(1, len(settles))
            self.assertEqual(callback["execution_id"], settles[0]["execution_id"])
            self.assertEqual("accepted", settles[0]["status"])

    def test_rejected_review_uses_the_same_exact_callback_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "adapter-state.jsonl"
            config = self.make_config(root, self.write_adapter(root), state_file)
            create_task(config, "ordinary work", str(root), task_id="rejected", evidence_source=self.source())
            run_next(config, suppress_wake_hooks=True)

            task = set_review_status(config, "rejected", "rejected", "review rejected", require_completed=True)

            self.assertEqual("rejected", task["review_status"])
            callback = task["automatic_evidence_last_callback"]
            self.assertEqual("recorded", callback["status"])
            self.assertEqual("rejected", callback["requested_status"])
            settles = [item for item in self.read_state(state_file) if item["kind"] == "settle"]
            self.assertEqual(1, len(settles))
            self.assertEqual("rejected", settles[0]["status"])

    def test_source_revision_cannot_be_changed_after_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(root=root)
            task = create_task(config, "immutable source", str(root), task_id="immutable", evidence_source=self.source())
            task["evidence_source"] = {"kind": "todoist-task", "id": "source-123", "revision": "rev-8"}

            with self.assertRaisesRegex(ValueError, "evidence_source is immutable"):
                save_task(config, task)

    def test_retry_keeps_scheduler_retry_and_chamber_attempts_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "adapter-state.jsonl"
            counter = root / "codex-count"
            retry_command = [sys.executable, str(self.write_retry_codex(root)), str(counter)]
            config = self.make_config(root, self.write_adapter(root), state_file, codex_command=retry_command)
            create_task(config, "retry work", str(root), task_id="retry", evidence_source=self.source())

            first = run_next(config, suppress_wake_hooks=True)
            second = run_next(config, suppress_wake_hooks=True)

            self.assertEqual("runnable", first.status)
            self.assertEqual("completed", second.status)
            task = load_task(config, "retry")
            self.assertEqual(2, task["attempts"])
            attempts = task["automatic_evidence_attempts"]
            self.assertEqual(2, len(attempts))
            self.assertNotEqual(attempts[0]["execution_id"], attempts[1]["execution_id"])
            self.assertEqual([1, 2], [item["attempt"] for item in attempts])
            accept_task_and_integrate(config, "retry", "reviewed", source="test-review")
            settles = [item for item in self.read_state(state_file) if item["kind"] == "settle"]
            self.assertEqual([attempts[1]["execution_id"]], [item["execution_id"] for item in settles])

    def test_source_without_adapter_fails_closed_before_native_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = Config.load(root=root)
            config = replace(base, codex_command=[sys.executable, str(CODEX_FIXTURE), "success"])
            create_task(config, "must bind", str(root), task_id="unbound", evidence_source=self.source())

            outcome = run_next(config, suppress_wake_hooks=True)

            task = load_task(config, "unbound")
            self.assertEqual("failed", outcome.status)
            self.assertIn("requires configured execution_evidence_adapter", task["last_error"])
            self.assertEqual(0, task["attempts"])

    def test_ambiguous_adapter_result_requires_reconciliation_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "adapter-state.jsonl"
            config = self.make_config(root, self.write_adapter(root), state_file, omit_result=True)
            create_task(config, "ambiguous work", str(root), task_id="ambiguous", evidence_source=self.source())

            outcome = run_next(config, suppress_wake_hooks=True)
            after = load_task(config, "ambiguous")
            next_outcome = run_next(config, suppress_wake_hooks=True)

            self.assertEqual("blocked_user", outcome.status)
            self.assertTrue(after["automatic_evidence_reconciliation_required"])
            self.assertEqual(1, after["attempts"])
            self.assertEqual("empty", next_outcome.status)

    def test_ad_hoc_task_without_source_remains_execution_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "adapter-state.jsonl"
            config = self.make_config(root, self.write_adapter(root), state_file)
            create_task(config, "ad hoc session", str(root), task_id="adhoc")

            outcome = run_next(config, suppress_wake_hooks=True)

            task = load_task(config, "adhoc")
            self.assertEqual("completed", outcome.status)
            self.assertNotIn("automatic_evidence", task["last_run"])
            self.assertEqual([], self.read_state(state_file))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_batch_runner.config import Config
from codex_batch_runner.queue import create_task, save_task, set_review_status
from codex_batch_runner.runner import run_next
from codex_batch_runner.parent_attention import (
    acknowledge_parent_attention,
    create_parent_attention,
    deliver_parent_attention,
    list_parent_attention,
)


class ParentAttentionTests(unittest.TestCase):
    def config(self, tmp: str, command: list[str] | None = None, max_attempts: int = 3) -> Config:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({
            "parent_attention_delivery_command": command or [],
            "parent_attention_max_attempts": max_attempts,
            "parent_attention_retry_base_seconds": 1,
        }), encoding="utf-8")
        return Config.load(str(path), root=Path(tmp))

    def test_each_wake_reason_creates_common_durable_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(tmp)
            reasons = ["needs_review", "needs_decision", "needs_follow_up", "blocked_external", "completed"]
            for reason in reasons:
                create_parent_attention(config, parent_ref="opaque-parent", work_item_ref=f"work-{reason}", completion_id="completion-1", wake_reason=reason, summary="safe summary")
            records = list_parent_attention(config)
            self.assertEqual(set(reasons), {record["wake_reason"] for record in records})
            self.assertTrue(all(record["event_type"] == "parent_attention_required" for record in records))

    def test_missing_linkage_does_not_create_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(tmp)
            self.assertIsNone(create_parent_attention(config, parent_ref=None, work_item_ref="work", completion_id="done", wake_reason="completed", summary="done"))
            self.assertEqual([], list_parent_attention(config))

    def test_duplicate_is_idempotent_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(tmp)
            first = create_parent_attention(config, parent_ref="opaque-parent", work_item_ref="work", completion_id="done", wake_reason="completed", summary="api_key=secret", evidence_refs=["safe:evidence"])
            second = create_parent_attention(config, parent_ref="opaque-parent", work_item_ref="work", completion_id="done", wake_reason="completed", summary="different")
            self.assertEqual(first["event_id"], second["event_id"])
            self.assertEqual(1, len(list_parent_attention(config)))
            self.assertNotIn("secret", json.dumps(first))

    def test_delivery_ack_and_duplicate_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(tmp, ["/usr/bin/true"])
            record = create_parent_attention(config, parent_ref="opaque-parent", work_item_ref="work", completion_id="done", wake_reason="needs_review", summary="done")
            result = deliver_parent_attention(config, record["event_id"])
            self.assertEqual("delivered", result.state)
            self.assertTrue(result.attempted)
            duplicate = deliver_parent_attention(config, record["event_id"])
            self.assertFalse(duplicate.attempted)
            acknowledged = acknowledge_parent_attention(config, record["event_id"])
            self.assertEqual("acknowledged", acknowledged["delivery"]["state"])

    def test_unavailable_and_bounded_adapter_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unavailable = self.config(tmp)
            record = create_parent_attention(unavailable, parent_ref="opaque-parent", work_item_ref="unavailable", completion_id="done", wake_reason="completed", summary="done")
            self.assertEqual("unavailable", deliver_parent_attention(unavailable, record["event_id"]).state)

        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(tmp, ["/usr/bin/false"], max_attempts=2)
            record = create_parent_attention(config, parent_ref="opaque-parent", work_item_ref="failed", completion_id="done", wake_reason="blocked_external", summary="blocked")
            now = datetime.now(timezone.utc)
            first = deliver_parent_attention(config, record["event_id"], now=now)
            self.assertEqual("retry_wait", first.state)
            second = deliver_parent_attention(config, record["event_id"], now=now.replace(year=now.year + 1))
            self.assertEqual("failed", second.state)
            self.assertEqual(2, list_parent_attention(config)[0]["delivery"]["attempts"])

    def test_runner_completion_and_review_follow_up_collect_parent_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_codex = Path(__file__).parent / "fixtures" / "fake_codex.py"
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"codex_command": ["python3", str(fake_codex), "success"]}), encoding="utf-8")
            config = Config.load(str(config_path), root=Path(tmp))
            task = create_task(config, "synthetic", tmp, task_id="linked")
            task["origin_parent_ref"] = "opaque-parent"
            save_task(config, task)

            self.assertEqual("completed", run_next(config).status)
            self.assertEqual("needs_review", list_parent_attention(config)[0]["wake_reason"])
            set_review_status(config, "linked", "needs_followup", "more work")
            self.assertEqual(
                {"needs_review", "needs_follow_up"},
                {record["wake_reason"] for record in list_parent_attention(config)},
            )


if __name__ == "__main__":
    unittest.main()

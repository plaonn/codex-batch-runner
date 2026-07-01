from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from codex_batch_runner.config import Config
from codex_batch_runner.notifier_cursor import (
    NotifierCursorState,
    duplicate_key,
    load_notifier_cursor_state,
    plan_advance_for_records,
    plan_advance_from_jsonl_bytes,
)
from codex_batch_runner.prune import build_prune_report


def write_config(root: Path) -> Path:
    path = root / "config.json"
    path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "queue"),
                "log_dir": str(root / "logs"),
                "event_dir": str(root / "events"),
            }
        ),
        encoding="utf-8",
    )
    return path


class NotifierCursorTests(unittest.TestCase):
    def test_loads_valid_canonical_cursor_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_dir = root / "events"
            event_dir.mkdir()
            event_file = event_dir / "2026-07-01.jsonl"
            event_file.write_text("", encoding="utf-8")
            cursor = root / "notify-state.json"
            cursor.write_text(
                json.dumps(
                    {
                        "current_event_file": "2026-07-01.jsonl",
                        "current_byte_offset": 42,
                        "recent_event_ids": ["event_id:old-1"],
                        "last_processed_event_id": "old-1",
                    }
                ),
                encoding="utf-8",
            )

            result = load_notifier_cursor_state(cursor, event_dir)

            self.assertTrue(result.valid)
            self.assertEqual((), result.warnings)
            self.assertIsNotNone(result.state)
            assert result.state is not None
            self.assertEqual(event_file.resolve(), result.state.current_event_file)
            self.assertEqual(42, result.state.current_byte_offset)
            self.assertEqual(("event_id:old-1",), result.state.recent_event_ids)
            self.assertEqual("old-1", result.state.last_processed_event_id)

    def test_invalid_cursor_states_warn_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_dir = root / "events"
            event_dir.mkdir()
            missing = load_notifier_cursor_state(root / "missing.json", event_dir)
            self.assertFalse(missing.valid)
            self.assertIn("missing", missing.warnings[0])

            malformed = root / "malformed.json"
            malformed.write_text(json.dumps({"current_event_file": "2026-07-01.jsonl"}), encoding="utf-8")
            malformed_result = load_notifier_cursor_state(malformed, event_dir)
            self.assertFalse(malformed_result.valid)
            self.assertTrue(any("current_byte_offset is required" in warning for warning in malformed_result.warnings))

            outside = root / "outside.json"
            outside.write_text(
                json.dumps({"current_event_file": str(root / "outside.jsonl"), "current_byte_offset": 0}),
                encoding="utf-8",
            )
            outside_result = load_notifier_cursor_state(outside, event_dir)
            self.assertFalse(outside_result.valid)
            self.assertTrue(any("outside event_dir" in warning for warning in outside_result.warnings))

    def test_duplicate_suppression_prefers_event_id(self) -> None:
        state = NotifierCursorState(Path("/tmp/events/2026-07-01.jsonl"), 10, recent_event_ids=("event_id:dupe",))
        records = [
            {"event_id": "dupe", "event_type": "task_started", "task_id": "task-a", "occurred_at": "2026-07-01T00:00:00Z"},
            {"event_id": "new", "event_type": "task_started", "task_id": "task-a", "occurred_at": "2026-07-01T00:00:01Z"},
        ]

        plan = plan_advance_for_records(state, state.current_event_file, records, bytes_consumed=99)

        self.assertEqual(109, plan.state.current_byte_offset)
        self.assertEqual([True, False], [decision.duplicate for decision in plan.decisions])
        self.assertEqual((records[1],), plan.events_to_notify)
        self.assertEqual("new", plan.state.last_processed_event_id)
        self.assertIn("event_id:new", plan.state.recent_event_ids)

    def test_duplicate_suppression_falls_back_for_missing_event_id(self) -> None:
        first = {"event_type": "task_completed", "task_id": "task-a", "occurred_at": "2026-07-01T00:00:00Z"}
        second = {"event_type": "task_completed", "task_id": "task-a", "occurred_at": "2026-07-01T00:00:00Z"}
        state = NotifierCursorState(Path("/tmp/events/2026-07-01.jsonl"), 0)

        plan = plan_advance_for_records(state, state.current_event_file, [first, second], bytes_consumed=20)

        self.assertEqual("fallback:task_completed:task-a:2026-07-01T00:00:00Z", duplicate_key(first))
        self.assertEqual([False, True], [decision.duplicate for decision in plan.decisions])
        self.assertEqual((first,), plan.events_to_notify)

    def test_jsonl_advance_is_byte_offset_based_and_bounds_recent_ids(self) -> None:
        event_file = Path("/tmp/events/2026-07-01.jsonl")
        state = NotifierCursorState(event_file, 5, recent_event_ids=("event_id:old-a", "event_id:old-b"))
        complete = json.dumps({"event_id": "a", "event_type": "task_started"}) + "\n"
        partial = json.dumps({"event_id": "partial", "event_type": "task_completed"})
        data = (complete + partial).encode("utf-8")

        plan = plan_advance_from_jsonl_bytes(state, event_file, data, recent_event_ids_limit=2)

        self.assertEqual(5 + len(complete.encode("utf-8")), plan.state.current_byte_offset)
        self.assertEqual(("event_id:old-b", "event_id:a"), plan.state.recent_event_ids)
        self.assertEqual(["a"], [event.get("event_id") for event in plan.events_to_notify])

    def test_prune_cursor_safety_still_blocks_unprocessed_event_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            config.event_dir.mkdir(parents=True)
            old_event = config.event_dir / "2000-01-01.jsonl"
            old_event.write_text('{"event_type":"task_created"}\n{"event_type":"task_started"}\n', encoding="utf-8")
            os.utime(old_event, (946684800, 946684800))
            cursor = root / "notify-state.json"
            cursor.write_text(
                json.dumps({"schema_version": 1, "current_event_file": str(old_event), "current_byte_offset": 1}),
                encoding="utf-8",
            )

            report = build_prune_report(config, age_days=30, apply=True, notifier_cursor_state_paths=[cursor])
            event = report["event_candidates"][0]

            self.assertTrue(old_event.exists())
            self.assertFalse(event["deleted"])
            self.assertTrue(event["skipped"])
            self.assertEqual("notifier cursor has not fully processed this event file", event["reason"])


if __name__ == "__main__":
    unittest.main()

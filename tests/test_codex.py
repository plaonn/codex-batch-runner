from __future__ import annotations

import unittest

from codex_batch_runner.codex import (
    extract_final_response,
    first_recursive_value,
    format_command,
    is_meaningful_event,
    should_use_resume,
)


class CodexParserTests(unittest.TestCase):
    def test_extracts_final_response_from_item_text(self) -> None:
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": (
                        '{"task_id":"task-1","status":"completed","summary":"done",'
                        '"next_prompt":"","changed_files":[],"verification":[]}'
                    ),
                },
            }
        ]

        response = extract_final_response(events)

        self.assertEqual("task-1", response["task_id"])
        self.assertEqual("completed", response["status"])

    def test_extracts_final_response_with_optional_metadata(self) -> None:
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": (
                        '{"task_id":"task-optional","status":"completed","summary":"done",'
                        '"next_prompt":"","changed_files":[],"verification":[],'
                        '"commits":["abc1234 implement thing"],'
                        '"push_status":{"ahead":1,"behind":0}}'
                    ),
                },
            }
        ]

        response = extract_final_response(events)

        self.assertEqual(["abc1234 implement thing"], response["commits"])
        self.assertEqual({"ahead": 1, "behind": 0}, response["push_status"])

    def test_thread_id_can_be_used_as_resume_identifier(self) -> None:
        events = [{"type": "thread.started", "thread_id": "thread-123"}]

        thread_id = first_recursive_value(events, ("thread_id", "threadId"))
        session_id = first_recursive_value(events, ("session_id", "sessionId", "conversation_id")) or thread_id

        self.assertEqual("thread-123", session_id)

    def test_format_command_uses_thread_id_as_session_fallback(self) -> None:
        command = format_command(["codex", "resume", "{session_id}"], {"thread_id": "thread-123"}, "continue")

        self.assertEqual(["codex", "resume", "thread-123", "continue"], command)

    def test_should_use_resume_after_task_is_marked_running(self) -> None:
        task = {"status": "running", "resume_requested": True, "thread_id": "thread-123"}

        self.assertTrue(should_use_resume(task))

    def test_item_progress_events_are_meaningful(self) -> None:
        self.assertTrue(is_meaningful_event({"type": "item.started", "item": {"type": "command_execution"}}))
        self.assertTrue(is_meaningful_event({"type": "item.completed", "item": {"type": "file_change"}}))
        self.assertTrue(is_meaningful_event({"type": "item.completed", "item": {"type": "agent_message"}}))


if __name__ == "__main__":
    unittest.main()

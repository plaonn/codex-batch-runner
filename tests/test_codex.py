from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from codex_batch_runner.config import Config
from codex_batch_runner.codex import (
    extract_final_response,
    first_recursive_value,
    format_command,
    format_command_with_profile,
    is_meaningful_event,
    run_codex,
    should_use_resume,
)
from codex_batch_runner.execution_profiles import resolve_execution_settings


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

    def test_format_command_with_profile_is_noop_without_profile_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))

            command = format_command_with_profile(["codex", "exec", "--json"], {"id": "task-1"}, "prompt", config)

            self.assertEqual(["codex", "exec", "--json", "prompt"], command)

    def test_format_command_injects_profile_options_after_exec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            config = Config(
                **{
                    **config.__dict__,
                    "default_execution_profile": "normal",
                    "execution_profiles": {
                        "normal": {
                            "model": "gpt-5-small",
                            "codex_profile": "batch-small",
                            "config_overrides": {"model_reasoning_effort": "low"},
                        }
                    },
                }
            )

            command = format_command_with_profile(
                ["codex", "exec", "--sandbox", "workspace-write", "--json"],
                {"id": "task-1"},
                "prompt",
                config,
            )

            self.assertEqual(
                [
                    "codex",
                    "exec",
                    "--model",
                    "gpt-5-small",
                    "--profile",
                    "batch-small",
                    "-c",
                    "model_reasoning_effort=low",
                    "--sandbox",
                    "workspace-write",
                    "--json",
                    "prompt",
                ],
                command,
            )

    def test_resume_command_preserves_resume_session_order_with_profile_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            config = Config(
                **{
                    **config.__dict__,
                    "default_execution_profile": "normal",
                    "execution_profiles": {"normal": {"model": "gpt-5-small"}},
                }
            )

            command = format_command_with_profile(
                ["codex", "exec", "--sandbox", "workspace-write", "resume", "{session_id}", "--json"],
                {"id": "task-1", "session_id": "session-123"},
                "continue",
                config,
            )

            self.assertEqual(
                [
                    "codex",
                    "exec",
                    "--model",
                    "gpt-5-small",
                    "--sandbox",
                    "workspace-write",
                    "resume",
                    "session-123",
                    "--json",
                    "continue",
                ],
                command,
            )

    def test_task_model_and_profile_override_config_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            config = Config(
                **{
                    **config.__dict__,
                    "default_execution_profile": "normal",
                    "execution_profiles": {"normal": {"model": "gpt-5-small", "codex_profile": "batch-small"}},
                }
            )

            command = format_command_with_profile(
                ["codex", "exec"],
                {"id": "task-1", "model": "gpt-5", "codex_profile": "batch-deep"},
                "prompt",
                config,
            )

            self.assertEqual(["codex", "exec", "--model", "gpt-5", "--profile", "batch-deep", "prompt"], command)

    def test_deep_profile_fallback_uses_narrow_risk_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            config = Config(
                **{
                    **config.__dict__,
                    "default_execution_profile": "normal",
                    "execution_profiles": {
                        "normal": {"model": "gpt-5.4-mini"},
                        "deep": {"model": "gpt-5.5"},
                    },
                }
            )

            general_worktree = resolve_execution_settings(
                config,
                {"id": "task-1", "category": "implementation", "labels": ["worktree"]},
            )
            critical_worktree = resolve_execution_settings(
                config,
                {"id": "task-2", "category": "implementation", "labels": ["worktree-apply"]},
            )

            self.assertEqual("normal", general_worktree.profile_name)
            self.assertEqual("config_default", general_worktree.profile_source)
            self.assertEqual("deep", critical_worktree.profile_name)
            self.assertEqual("high_risk_fallback", critical_worktree.profile_source)
            self.assertEqual("matched category/label: worktree-apply", critical_worktree.profile_reason)

    def test_small_profile_fallback_uses_conservative_routing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            config = Config(
                **{
                    **config.__dict__,
                    "default_execution_profile": "normal",
                    "execution_profiles": {
                        "normal": {"model": "gpt-5.4-mini"},
                        "small": {"model": "gpt-5-small"},
                        "deep": {"model": "gpt-5.5"},
                    },
                }
            )

            low_risk_docs = resolve_execution_settings(
                config,
                {
                    "id": "task-1",
                    "routing_size": "small",
                    "routing_risk": "low",
                    "verification_scope": ["docs"],
                },
            )
            wider_verification = resolve_execution_settings(
                config,
                {
                    "id": "task-2",
                    "routing_size": "small",
                    "routing_risk": "low",
                    "verification_scope": ["unit", "docs"],
                },
            )
            high_risk_worktree = resolve_execution_settings(
                config,
                {
                    "id": "task-3",
                    "category": "implementation",
                    "labels": ["worktree-apply"],
                    "routing_size": "small",
                    "routing_risk": "low",
                    "verification_scope": ["docs"],
                },
            )
            explicit_profile = resolve_execution_settings(
                config,
                {
                    "id": "task-4",
                    "execution_profile": "normal",
                    "routing_size": "small",
                    "routing_risk": "low",
                    "verification_scope": ["docs"],
                },
            )

            self.assertEqual("small", low_risk_docs.profile_name)
            self.assertEqual("low_risk_downshift", low_risk_docs.profile_source)
            self.assertEqual("matched routing metadata: size=small risk=low verify=docs", low_risk_docs.profile_reason)
            self.assertEqual("normal", wider_verification.profile_name)
            self.assertEqual("deep", high_risk_worktree.profile_name)
            self.assertEqual("high_risk_fallback", high_risk_worktree.profile_source)
            self.assertEqual("normal", explicit_profile.profile_name)
            self.assertEqual("task", explicit_profile.profile_source)

    def test_should_use_resume_after_task_is_marked_running(self) -> None:
        task = {"status": "running", "resume_requested": True, "thread_id": "thread-123"}

        self.assertTrue(should_use_resume(task))

    def test_run_codex_starts_subprocess_in_task_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            execution_cwd = root / "execution-worktree"
            execution_cwd.mkdir()
            config = Config.load(root=root)
            config = Config(
                **{
                    **config.__dict__,
                    "codex_command": [
                        sys.executable,
                        "-c",
                        (
                            "import json, os; "
                            "response = dict(task_id='task-cwd', status='completed', summary=os.getcwd(), "
                            "next_prompt='', changed_files=[], verification=[os.getcwd()]); "
                            "print(json.dumps(dict(type='turn.completed', response=response)), flush=True)"
                        ),
                    ],
                }
            )

            result = run_codex(config, {"id": "task-cwd", "cwd": str(execution_cwd)}, "prompt", 1)

            expected_cwd = str(execution_cwd.resolve())
            self.assertEqual(0, result.returncode)
            self.assertIsNotNone(result.final_response)
            assert result.final_response is not None
            self.assertEqual(expected_cwd, result.final_response["summary"])
            self.assertEqual([expected_cwd], result.final_response["verification"])

    def test_item_progress_events_are_meaningful(self) -> None:
        self.assertTrue(is_meaningful_event({"type": "item.started", "item": {"type": "command_execution"}}))
        self.assertTrue(is_meaningful_event({"type": "item.completed", "item": {"type": "file_change"}}))
        self.assertTrue(is_meaningful_event({"type": "item.completed", "item": {"type": "agent_message"}}))


if __name__ == "__main__":
    unittest.main()

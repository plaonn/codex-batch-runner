from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.config import Config
from codex_batch_runner.codex import (
    extract_final_response,
    first_recursive_value,
    format_command,
    format_command_with_resolved_config,
    is_meaningful_event,
    run_codex,
    should_use_resume,
)
from codex_batch_runner.model_requirements import resolve_execution_config, resolve_model_requirement_vector


class CodexParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cbr_config_patcher = patch.dict("os.environ", {"CBR_CONFIG": ""}, clear=False)
        self._cbr_config_patcher.start()
        self.addCleanup(self._cbr_config_patcher.stop)

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

    def test_format_command_with_resolved_config_is_noop_without_model_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))

            command = format_command_with_resolved_config(["codex", "exec", "--json"], {"id": "task-1"}, "prompt", config)
            resolved = resolve_execution_config(config, {"id": "task-1"})

            self.assertEqual(["codex", "exec", "--json", "prompt"], command)
            self.assertEqual("cli_default", resolved.model_source)
            self.assertIsNone(resolved.model)

    def test_format_command_injects_model_selection_options_after_exec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            config = Config(
                **{
                    **config.__dict__,
                    "default_execution_config": {
                        "model": "gpt-5-small",
                        "codex_profile": "batch-small",
                        "config_overrides": {"model_reasoning_effort": "low"},
                    },
                    "model_selection_rules": [
                        {
                            "name": "low-cost-docs",
                            "when": {"reasoning_depth": "low"},
                            "model": "gpt-5-small",
                            "codex_profile": "batch-small",
                            "config_overrides": {"model_reasoning_effort": "low"},
                        }
                    ],
                }
            )

            command = format_command_with_resolved_config(
                ["codex", "exec", "--sandbox", "workspace-write", "--json"],
                {
                    "id": "task-1",
                    "model_requirement_vector": {
                        "dimensions": {
                            "reasoning_depth": "low",
                            "context_need": "low",
                            "tool_reliability": "medium",
                            "latency_priority": "high",
                            "cost_sensitivity": "high",
                            "review_strictness": "medium",
                        }
                    },
                },
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

    def test_resume_command_preserves_resume_session_order_with_model_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            config = Config(
                **{
                    **config.__dict__,
                    "default_execution_config": {"model": "gpt-5-small"},
                }
            )

            command = format_command_with_resolved_config(
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

    def test_model_selection_rule_overrides_config_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            config = Config(
                **{
                    **config.__dict__,
                    "default_execution_config": {"model": "gpt-5-small", "codex_profile": "batch-small"},
                    "model_selection_rules": [
                        {
                            "name": "high-capability",
                            "when": {"reasoning_depth": "high"},
                            "model": "gpt-5",
                            "codex_profile": "batch-deep",
                        }
                    ],
                }
            )

            command = format_command_with_resolved_config(
                ["codex", "exec"],
                {"id": "task-1", "model_requirement_vector": {"dimensions": {"reasoning_depth": "high"}}},
                "prompt",
                config,
            )

            self.assertEqual(["codex", "exec", "--model", "gpt-5", "--profile", "batch-deep", "prompt"], command)
            resolved = resolve_execution_config(
                config,
                {"id": "task-1", "model_requirement_vector": {"dimensions": {"reasoning_depth": "high"}}},
            )
            self.assertEqual("explicit_model", resolved.model_source)
            self.assertEqual("gpt-5", resolved.model)

    def test_model_selection_rule_without_model_uses_cli_default_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            config = Config(
                **{
                    **config.__dict__,
                    "model_selection_rules": [
                        {
                            "name": "high-effort-default-model",
                            "when": {"reasoning_depth": "high"},
                            "config_overrides": {"model_reasoning_effort": "high"},
                        }
                    ],
                }
            )

            command = format_command_with_resolved_config(
                ["codex", "exec"],
                {"id": "task-1", "model_requirement_vector": {"dimensions": {"reasoning_depth": "high"}}},
                "prompt",
                config,
            )
            resolved = resolve_execution_config(
                config,
                {"id": "task-1", "model_requirement_vector": {"dimensions": {"reasoning_depth": "high"}}},
            )

            self.assertEqual(["codex", "exec", "-c", "model_reasoning_effort=high", "prompt"], command)
            self.assertEqual("cli_default", resolved.model_source)
            self.assertIsNone(resolved.model)

    def test_high_risk_metadata_derives_high_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))

            general_worktree = resolve_model_requirement_vector(
                config,
                {"id": "task-1", "category": "implementation", "labels": ["worktree"]},
            )
            critical_worktree = resolve_model_requirement_vector(
                config,
                {"id": "task-2", "category": "implementation", "labels": ["worktree-apply"]},
            )

            self.assertEqual("medium", general_worktree["dimensions"]["reasoning_depth"])
            self.assertEqual("high", critical_worktree["dimensions"]["reasoning_depth"])
            self.assertEqual("high", critical_worktree["dimensions"]["tool_reliability"])

    def test_low_risk_docs_metadata_derives_low_cost_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))

            low_risk_docs = resolve_model_requirement_vector(
                config,
                {
                    "id": "task-1",
                    "routing_size": "small",
                    "routing_risk": "low",
                    "verification_scope": ["docs"],
                },
            )
            wider_verification = resolve_model_requirement_vector(
                config,
                {
                    "id": "task-2",
                    "routing_size": "small",
                    "routing_risk": "low",
                    "verification_scope": ["unit", "docs"],
                },
            )
            high_risk_worktree = resolve_model_requirement_vector(
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
            explicit_requirement = resolve_execution_config(
                config,
                {
                    "id": "task-4",
                    "model_requirement_vector": {"dimensions": {"reasoning_depth": "medium"}},
                    "routing_size": "small",
                    "routing_risk": "low",
                    "verification_scope": ["docs"],
                },
            )

            self.assertEqual("low", low_risk_docs["dimensions"]["reasoning_depth"])
            self.assertEqual("high", low_risk_docs["dimensions"]["cost_sensitivity"])
            self.assertEqual("medium", wider_verification["dimensions"]["reasoning_depth"])
            self.assertEqual("high", high_risk_worktree["dimensions"]["reasoning_depth"])
            self.assertEqual("medium", explicit_requirement.requirement_vector["dimensions"]["reasoning_depth"])

    def test_stored_derived_requirement_recomputes_from_current_routing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(root=Path(tmp))
            config = Config(
                **{
                    **config.__dict__,
                    "default_model_requirement_vector": {
                        "source": "config_default",
                        "dimensions": {"reasoning_depth": "medium", "cost_sensitivity": "medium"},
                    },
                }
            )

            resolved = resolve_model_requirement_vector(
                config,
                {
                    "id": "task-stale-derived",
                    "model_requirement_vector": {
                        "source": "derived_from_task_vector",
                        "dimensions": {"reasoning_depth": "low", "cost_sensitivity": "high"},
                    },
                    "routing_size": "small",
                    "routing_risk": "high",
                    "verification_scope": ["docs"],
                },
            )

            self.assertEqual("high", resolved["dimensions"]["reasoning_depth"])
            self.assertEqual("low", resolved["dimensions"]["cost_sensitivity"])

    def test_example_configs_route_reviewers_before_general_high_capability_rule(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for config_name in ("config.example.json", "config.automation.example.json"):
            with self.subTest(config_name=config_name):
                config = Config.load(str(repo_root / "examples" / config_name))

                resolved = resolve_execution_config(config, {}, reviewer=True)

                self.assertEqual("strict-review", resolved.selection_rule)
                self.assertEqual("batch-review", resolved.codex_profile)

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

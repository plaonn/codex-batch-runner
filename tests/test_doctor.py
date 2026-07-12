from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.doctor import build_doctor_report
from codex_batch_runner.queue import create_task, save_task
from codex_batch_runner.timeutil import add_seconds


def write_config(
    tmp: str,
    codex_command: list[str],
    auto_review_mechanical_accept: bool = False,
    worktree_mode: str = "disabled",
    extra: dict | None = None,
) -> Path:
    root = Path(tmp)
    config_path = root / "config.json"
    data = {
        "queue_dir": str(root / "tasks"),
        "log_dir": str(root / "logs"),
        "event_dir": str(root / "events"),
        "lock_file": str(root / "runner.lock"),
        "state_file": str(root / "state.json"),
        "codex_command": codex_command,
        "auto_review_mechanical_accept": auto_review_mechanical_accept,
        "worktree_mode": worktree_mode,
        "worktree_root": str(root / "worktrees"),
    }
    if extra:
        data.update(extra)
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return config_path


def run_cli(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, stdout.getvalue()


@contextlib.contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def run_git(cwd: Path, args: list[str]) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def git_output(cwd: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    run_git(path, ["config", "user.email", "test@example.invalid"])
    run_git(path, ["config", "user.name", "Test User"])
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    run_git(path, ["add", "file.txt"])
    run_git(path, ["commit", "-m", "initial"])


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_healthy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 1.2.3\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable), "exec", "--json"])

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["ok"])
            self.assertEqual(str(Path(tmp) / "tasks"), report["paths"]["queue_dir"])
            self.assertEqual(str(Path(tmp) / "logs"), report["paths"]["log_dir"])
            self.assertTrue(report["codex_command"]["available"])
            self.assertEqual(str(executable.resolve()), report["codex_command"]["resolved_executable"])
            self.assertEqual("codex-cli 1.2.3", report["codex_command"]["version_output"])
            self.assertIsNone(report["codex_command"]["version_error"])

    def test_doctor_human_output_includes_codex_executable_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 2.0.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable), "exec", "--json"], extra={"root": tmp})

            code, output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            self.assertIn("codex_command:", output)
            self.assertIn(f"configured_executable: {executable}", output)
            self.assertIn(f"resolved_executable: {executable.resolve()}", output)
            self.assertIn("available: true", output)
            self.assertIn("version_output: codex-cli 2.0.0", output)
            self.assertIn("checks:", output)
            self.assertIn("ok_count: 6", output)
            self.assertIn("warning_count: 3", output)
            self.assertIn("error_count: 0", output)
            self.assertNotIn("ok: queue_dir:", output)

    def test_doctor_reports_runner_pause_state_in_json_and_human_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 2.0.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable), "exec", "--json"])
            (Path(tmp) / "state.json").write_text(
                json.dumps(
                    {
                        "runner_pause": {
                            "active": True,
                            "reason": "operator drain",
                            "paused_at": "2026-06-22T07:07:00+09:00",
                            "paused_by": "ops-user",
                        }
                    }
                ),
                encoding="utf-8",
            )

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)
            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            self.assertTrue(report["state"]["runner_pause"]["active"])
            self.assertEqual("operator drain", report["state"]["runner_pause"]["reason"])
            self.assertEqual("2026-06-22T07:07:00+09:00", report["state"]["runner_pause"]["paused_at"])
            self.assertEqual("ops-user", report["state"]["runner_pause"]["paused_by"])
            self.assertEqual(0, human_code)
            self.assertIn("runner_pause_active: true", human_output)
            self.assertIn("runner_pause_reason: operator drain", human_output)
            self.assertIn("runner_pause_paused_by: ops-user", human_output)

    def test_doctor_reports_model_requirements_without_private_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 2.0.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(
                tmp,
                [str(executable), "exec", "--json"],
                extra={
                    "default_execution_config": {"model": "gpt-5"},
                    "model_selection_rules": [
                        {
                            "name": "low-cost-docs",
                            "when": {"reasoning_depth": "low"},
                            "model": "gpt-5-small",
                            "codex_profile": "batch-small",
                            "config_overrides": {"model_reasoning_effort": "low"},
                        },
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)
            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            self.assertEqual(["low-cost-docs"], report["model_requirements"]["model_selection_rules"])
            self.assertEqual(
                ["model_reasoning_effort"],
                report["model_requirements"]["rules"]["low-cost-docs"]["config_override_keys"],
            )
            provenance = report["model_requirements"]["model_selection_provenance"]
            self.assertEqual("explicit_model", provenance["default_execution_config"]["model_source"])
            self.assertTrue(provenance["default_execution_config"]["has_explicit_model_pin"])
            self.assertEqual("absent", provenance["default_execution_config"]["freshness_metadata"]["status"])
            self.assertEqual(
                "direct_model_pin_without_execution_target",
                provenance["default_execution_config"]["freshness_metadata"]["reason"],
            )
            self.assertEqual("explicit_model", provenance["rules"]["low-cost-docs"]["model_source"])
            self.assertTrue(provenance["rules"]["low-cost-docs"]["has_explicit_model_pin"])
            self.assertEqual("absent", provenance["rules"]["low-cost-docs"]["freshness_metadata"]["status"])
            self.assertNotIn('"model_reasoning_effort": "low"', json.dumps(report["model_requirements"], sort_keys=True))
            self.assertNotIn("gpt-5", json.dumps(provenance, sort_keys=True))
            self.assertIn(
                {
                    "name": "model_requirement_model_selection_default_execution_config_freshness",
                    "level": "warning",
                    "message": "default_execution_config has an explicit model pin without execution_target freshness metadata",
                },
                report["checks"],
            )
            self.assertIn(
                {
                    "name": "model_requirement_model_selection_rule_low-cost-docs_freshness",
                    "level": "warning",
                    "message": "model_selection_rule has an explicit model pin without execution_target freshness metadata",
                },
                report["checks"],
            )
            self.assertEqual(0, human_code)
            self.assertIn("model_requirements:", human_output)
            self.assertIn(
                "low-cost-docs: model=true target=- codex_profile=true "
                "config_overrides=model_reasoning_effort",
                human_output,
            )
            self.assertIn("model_selection_provenance:", human_output)
            self.assertIn(
                "default_execution_config: model_source=explicit_model target=- explicit_pin=true "
                "freshness=absent(direct_model_pin_without_execution_target)",
                human_output,
            )
            self.assertIn(
                "low-cost-docs: model_source=explicit_model target=- explicit_pin=true "
                "freshness=absent(direct_model_pin_without_execution_target)",
                human_output,
            )

    def test_doctor_reports_execution_target_alias_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 2.0.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(
                tmp,
                [str(executable), "exec", "--json"],
                extra={
                    "execution_targets": {
                        "low_cost_current": {
                            "model": "gpt-5.3-codex-spark",
                            "config_overrides": {"model_reasoning_effort": "low"},
                            "freshness": {
                                "owner": "operator",
                                "last_reviewed_at": "2026-07-03",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "model_selection_rules": [
                        {
                            "name": "low-cost-docs",
                            "when": {"reasoning_depth": "low"},
                            "execution_target": "low_cost_current",
                        },
                    ],
                },
            )

            with mock.patch(
                "codex_batch_runner.doctor.utc_now",
                return_value=datetime(2026, 7, 3, tzinfo=timezone.utc),
            ):
                code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
                report = json.loads(output)
                human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            self.assertEqual(["low_cost_current"], report["model_requirements"]["execution_targets"])
            rule = report["model_requirements"]["model_selection_provenance"]["rules"]["low-cost-docs"]
            self.assertEqual("target_alias", rule["model_source"])
            self.assertEqual("low_cost_current", rule["execution_target"])
            self.assertEqual("fresh", rule["freshness_metadata"]["status"])
            self.assertFalse(rule["freshness_metadata"]["stale"])
            self.assertEqual("2026-07-17", rule["freshness_metadata"]["review_due_at"])
            self.assertNotIn("gpt-5.3-codex-spark", json.dumps(report["model_requirements"], sort_keys=True))
            self.assertNotIn(
                {
                    "name": "model_requirement_model_selection_rule_low-cost-docs_target_freshness",
                    "level": "warning",
                    "message": "model_selection_rule execution_target has no freshness metadata",
                },
                report["checks"],
            )
            self.assertEqual(0, human_code)
            self.assertIn("execution_targets: low_cost_current", human_output)
            self.assertIn(
                "low-cost-docs: model_source=target_alias target=low_cost_current explicit_pin=false "
                "freshness=fresh(execution_target)",
                human_output,
            )

    def test_doctor_reports_decision_card_summary_without_raw_model_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 2.0.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(
                tmp,
                [str(executable), "exec", "--json"],
                extra={
                    "model_selection_rules": [
                        {
                            "name": "low-cost-docs",
                            "when": {"reasoning_depth": "low"},
                            "model": "gpt-5-small",
                        }
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)
            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            self.assertEqual(
                {
                    "policy-proposals execution-target-freshness": 1,
                },
                report["decision_cards"]["by_source"],
            )
            self.assertEqual(1, report["decision_cards"]["card_count"])
            self.assertEqual(0, report["decision_cards"]["decision_required"])
            self.assertEqual(1, report["decision_cards"]["approval_blocked"])
            self.assertEqual("review_decision_cards", report["decision_cards"]["next_action"])
            self.assertEqual({"create_bounded_migration_proposal": 1}, report["decision_cards"]["by_recommendation"])
            self.assertEqual(
                {"direct_model_pin_requires_separate_migration_approval": 1},
                report["decision_cards"]["by_blocked_reason"],
            )
            self.assertNotIn("gpt-5-small", output)
            self.assertEqual(0, human_code)
            self.assertIn("decision_cards:", human_output)
            self.assertIn("card_count: 1", human_output)
            self.assertIn("approval_blocked: 1", human_output)
            self.assertIn("open_decisions: present", human_output)
            self.assertIn("next_action: review_decision_cards", human_output)
            self.assertIn("direct_model_pin_requires_separate_migration_approval: 1", human_output)
            self.assertNotIn("gpt-5-small", human_output)

    def test_doctor_human_output_marks_no_open_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 2.0.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable), "exec", "--json"])

            code, output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            self.assertIn("decision_cards:", output)
            self.assertIn("card_count: 0", output)
            self.assertIn("open_decisions: none", output)
            self.assertIn("next_action: none", output)

    def test_doctor_warns_when_execution_target_freshness_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 2.0.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(
                tmp,
                [str(executable), "exec", "--json"],
                extra={
                    "execution_targets": {
                        "low_cost_current": {
                            "model": "gpt-5.3-codex-spark",
                            "freshness": {
                                "last_reviewed_at": "2026-06-19",
                                "review_after_days": 14,
                            },
                        }
                    },
                    "model_selection_rules": [
                        {
                            "name": "low-cost-docs",
                            "when": {"reasoning_depth": "low"},
                            "execution_target": "low_cost_current",
                        },
                    ],
                },
            )

            with mock.patch(
                "codex_batch_runner.doctor.utc_now",
                return_value=datetime(2026, 7, 3, tzinfo=timezone.utc),
            ):
                code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
                report = json.loads(output)
                human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            rule = report["model_requirements"]["model_selection_provenance"]["rules"]["low-cost-docs"]
            self.assertEqual("stale", rule["freshness_metadata"]["status"])
            self.assertTrue(rule["freshness_metadata"]["stale"])
            self.assertEqual("2026-07-03", rule["freshness_metadata"]["checked_at"])
            self.assertEqual("2026-07-03", rule["freshness_metadata"]["review_due_at"])
            self.assertIn(
                {
                    "name": "model_requirement_model_selection_rule_low-cost-docs_target_freshness_stale",
                    "level": "warning",
                    "message": "model_selection_rule execution_target freshness metadata is stale",
                },
                report["checks"],
            )
            self.assertEqual(0, human_code)
            self.assertIn(
                "low-cost-docs: model_source=target_alias target=low_cost_current explicit_pin=false "
                "freshness=stale(review_after_days_elapsed)",
                human_output,
            )

    def test_doctor_warns_when_execution_target_freshness_metadata_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 2.0.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(
                tmp,
                [str(executable), "exec", "--json"],
                extra={
                    "execution_targets": {
                        "balanced_current": {
                            "model": "gpt-5.3-codex",
                        }
                    },
                    "default_execution_config": {
                        "execution_target": "balanced_current",
                    },
                },
            )

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)
            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            provenance = report["model_requirements"]["model_selection_provenance"]["default_execution_config"]
            self.assertEqual("target_alias", provenance["model_source"])
            self.assertEqual("balanced_current", provenance["execution_target"])
            self.assertEqual("absent", provenance["freshness_metadata"]["status"])
            self.assertEqual(
                {
                    "name": "model_requirement_model_selection_default_execution_config_target_freshness",
                    "level": "warning",
                    "message": "default_execution_config execution_target has no freshness metadata",
                },
                next(
                    check
                    for check in report["checks"]
                    if check["name"] == "model_requirement_model_selection_default_execution_config_target_freshness"
                ),
            )
            self.assertEqual(0, human_code)
            self.assertIn(
                "default_execution_config: model_source=target_alias target=balanced_current explicit_pin=false "
                "freshness=absent(target_freshness_not_configured)",
                human_output,
            )

    def test_doctor_reports_cli_default_model_selection_provenance_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 2.0.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(
                tmp,
                [str(executable), "exec", "--json"],
                extra={
                    "model_selection_rules": [
                        {
                            "name": "high-effort-default-model",
                            "when": {"review_strictness": "high"},
                            "config_overrides": {"model_reasoning_effort": "high"},
                        },
                    ],
                },
            )

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)
            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            self.assertTrue(report["ok"])
            provenance = report["model_requirements"]["model_selection_provenance"]
            self.assertEqual("cli_default", provenance["default_execution_config"]["model_source"])
            self.assertFalse(provenance["default_execution_config"]["has_explicit_model_pin"])
            self.assertTrue(provenance["default_execution_config"]["uses_cli_default_model"])
            self.assertEqual(
                "not_applicable",
                provenance["default_execution_config"]["freshness_metadata"]["status"],
            )
            self.assertEqual("cli_default", provenance["reviewer_selected"]["model_source"])
            self.assertIsNone(provenance["reviewer_selected"]["selection_rule"])
            self.assertTrue(provenance["reviewer_selected"]["uses_cli_default_model"])
            self.assertIn(
                {
                    "name": "model_requirement_model_selection_reviewer_selected",
                    "level": "warning",
                    "message": "selected execution config relies on the Codex CLI default model because no model is configured",
                },
                report["checks"],
            )
            self.assertEqual(0, human_code)
            self.assertIn(
                "reviewer_selected: selection_rule=- "
                "model_source=cli_default target=- explicit_pin=false "
                "freshness=not_applicable(no_explicit_model_pin)",
                human_output,
            )

    def test_doctor_warns_when_codex_version_fails_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'bad version\\n' >&2\nexit 7\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["ok"])
            self.assertTrue(report["codex_command"]["available"])
            self.assertIsNone(report["codex_command"]["version_output"])
            self.assertIn("codex --version failed: bad version", report["codex_command"]["version_error"])
            self.assertIn(
                {"name": "codex_command_version", "level": "warning", "message": report["codex_command"]["version_error"]},
                report["checks"],
            )

    def test_doctor_warns_with_configured_executable_name_when_wrapper_version_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "agy-cbr-wrapper.py"
            executable.write_text(
                "#!/bin/sh\n"
                "printf 'usage: wrapper\\n' >&2\n"
                "printf 'agy-cbr-wrapper.py: error: unrecognized arguments: --version\\n' >&2\n"
                "exit 2\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["ok"])
            self.assertTrue(report["codex_command"]["available"])
            self.assertIsNone(report["codex_command"]["version_output"])
            self.assertIn(
                "agy-cbr-wrapper.py --version failed: agy-cbr-wrapper.py: error: unrecognized arguments: --version",
                report["codex_command"]["version_error"],
            )
            self.assertNotIn("codex --version failed", report["codex_command"]["version_error"])
            self.assertIn(
                {"name": "codex_command_version", "level": "warning", "message": report["codex_command"]["version_error"]},
                report["checks"],
            )

    def test_doctor_warns_when_codex_version_times_out_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["ok"])
            self.assertIn("timed out", report["codex_command"]["version_error"])
            self.assertIn(
                {"name": "codex_command_version", "level": "warning", "message": report["codex_command"]["version_error"]},
                report["checks"],
            )

    def test_doctor_resolves_path_command_without_macos_app_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            executable = bindir / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli path-test\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, ["codex", "exec", "--json"])

            with mock.patch.dict(os.environ, {"PATH": str(bindir)}, clear=False):
                code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("codex", report["codex_command"]["configured_executable"])
            self.assertEqual(str(executable.resolve()), report["codex_command"]["resolved_executable"])
            self.assertEqual("codex-cli path-test", report["codex_command"]["version_output"])
            self.assertNotIn("Codex.app", output)
            self.assertNotIn("/Applications", output)

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_doctor_reports_clean_git_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            remote = root / "remote.git"
            repo.mkdir()
            run_git(repo, ["init"])
            run_git(repo, ["config", "user.email", "test@example.invalid"])
            run_git(repo, ["config", "user.name", "Test User"])
            (repo / "README.md").write_text("# temp\n", encoding="utf-8")
            run_git(repo, ["add", "README.md"])
            run_git(repo, ["commit", "-m", "initial"])
            run_git(repo, ["branch", "-M", "main"])
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            run_git(repo, ["remote", "add", "origin", str(remote)])
            run_git(repo, ["push", "-u", "origin", "main"])

            executable = root / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])

            with working_directory(repo):
                code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertTrue(report["git"]["available"])
            self.assertTrue(report["git"]["is_repository"])
            self.assertEqual(str(repo.resolve()), report["git"]["root"])
            self.assertEqual("main", report["git"]["branch"])
            self.assertFalse(report["git"]["dirty"])
            self.assertEqual("origin/main", report["git"]["upstream"])
            self.assertEqual("origin/main", report["git"]["comparison_ref"])
            self.assertEqual(0, report["git"]["ahead"])
            self.assertEqual(0, report["git"]["behind"])
            self.assertEqual([], report["git"]["warnings"])

    def test_doctor_warns_for_non_git_root_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])
            config = Config.load(str(config_path), root=Path(tmp))

            report = build_doctor_report(config)

            self.assertTrue(report["ok"])
            self.assertFalse(report["git"]["is_repository"])
            self.assertIn("not inside a git repository", report["git"]["warnings"][0])
            self.assertIn(
                {"name": "git", "level": "warning", "message": report["git"]["warnings"][0]},
                report["checks"],
            )

    def test_doctor_errors_when_codex_command_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(tmp, [str(Path(tmp) / "missing-codex")])

            code, output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(1, code)
            self.assertIn("error_count: 1", output)
            self.assertIn("details:", output)
            self.assertIn("error: codex_command", output)
            self.assertIn("executable not available", output)

    def test_doctor_summarizes_task_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)], auto_review_mechanical_accept=True)
            config = Config.load(str(config_path))

            create_task(config, "ready", tmp, task_id="ready")
            cooldown = create_task(config, "cooldown", tmp, task_id="cooldown")
            cooldown["cooldown_until"] = add_seconds(3600)
            save_task(config, cooldown)
            done = create_task(config, "done", tmp, task_id="done")
            done["status"] = "completed"
            done["review_status"] = "unreviewed"
            save_task(config, done)
            failed = create_task(config, "failed", tmp, task_id="failed")
            failed["status"] = "failed"
            failed["resolution"] = "manual"
            save_task(config, failed)

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(4, report["tasks"]["total"])
            self.assertEqual({"completed": 1, "failed": 1, "runnable": 2}, report["tasks"]["by_status"])
            self.assertEqual(1, report["tasks"]["needs_review_completed"])
            self.assertEqual(1, report["tasks"]["resolved_failed_or_blocked"])
            self.assertEqual(1, report["tasks"]["runnable"])
            self.assertEqual(1, report["tasks"]["cooldown"])
            self.assertEqual(0, report["tasks"]["startup_stalled"])
            self.assertTrue(report["auto_review"]["mechanical_auto_accept_enabled"])
            self.assertFalse(report["auto_review"]["reviewer_codex_enabled"])
            self.assertEqual(1, report["auto_review"]["reviewable_completed"])
            self.assertEqual("disabled", report["worktree"]["mode"])
            self.assertEqual(0, report["worktree"]["tasks"]["retained"])

            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])
            self.assertEqual(0, human_code)
            self.assertIn("auto_review:", human_output)
            self.assertIn("mechanical_auto_accept_enabled: true", human_output)
            self.assertIn("reviewable_completed: 1", human_output)
            self.assertIn("worktree:", human_output)
            self.assertIn("mode: disabled", human_output)
            task_section = human_output[human_output.index("\ntasks:\n") :]
            task_lines = task_section.splitlines()
            expected_task_order = [
                "  total: 4",
                "  needs_review_completed: 1",
                "  runnable: 1",
                "  cooldown: 1",
                "  startup_stalled: 0",
                "  running_no_progress: 0",
                "  resolved_failed_or_blocked: 1",
                "  resolved_review_completed: 0",
                "  by_status:",
            ]
            self.assertEqual(
                sorted(task_lines.index(line) for line in expected_task_order),
                [task_lines.index(line) for line in expected_task_order],
            )

    def test_doctor_reports_capacity_config_and_running_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(
                tmp,
                [str(executable)],
                extra={
                    "max_total_running": 2,
                    "max_running_per_project": 1,
                    "capacity_pools": {
                        "codex": {"max_running": 1},
                        "codex-spark": {"max_running": 1},
                    },
                },
            )
            config = Config.load(str(config_path))
            running = create_task(config, "running", tmp, task_id="running")
            running["status"] = "running"
            running["project_id"] = "project-a"
            save_task(config, running)
            spark = create_task(config, "spark", tmp, task_id="spark")
            spark["status"] = "running"
            spark["project_id"] = "project-b"
            spark["capacity_pool"] = "codex-spark"
            save_task(config, spark)

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)
            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            self.assertEqual(2, report["capacity"]["max_total_running"])
            self.assertEqual(1, report["capacity"]["max_running_per_project"])
            self.assertEqual({"codex": 1, "codex-spark": 1}, report["capacity"]["running_by_pool"])
            self.assertEqual(2, report["capacity"]["running_total"])
            self.assertEqual(2, report["capacity"]["running_projects"])
            self.assertFalse(report["capacity"]["over_capacity"])
            self.assertEqual(0, human_code)
            self.assertIn("capacity:", human_output)
            self.assertIn(
                "summary: running=2/2 projects=2 max_project_running=1/1 admissible=0 blocked=0 over_capacity=false",
                human_output,
            )
            self.assertIn("pools: codex=1/1, codex-spark=1/1", human_output)
            self.assertNotIn("pool_details:", human_output)

    def test_doctor_reports_capacity_overages_without_mutating_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])
            config = Config.load(str(config_path))
            first = create_task(config, "first", tmp, task_id="first")
            first["status"] = "running"
            first["project_id"] = "project-a"
            save_task(config, first)
            second = create_task(config, "second", tmp, task_id="second")
            second["status"] = "running"
            second["project_id"] = "project-a"
            save_task(config, second)

            report = build_doctor_report(config)
            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])
            reloaded = json.loads((config.queue_dir / "second.json").read_text(encoding="utf-8"))

            self.assertTrue(report["capacity"]["over_total_capacity"])
            self.assertTrue(report["capacity"]["over_project_capacity"])
            self.assertTrue(report["capacity"]["over_pool_capacity"])
            self.assertTrue(report["capacity"]["over_capacity"])
            self.assertEqual("running", reloaded["status"])
            self.assertEqual(0, human_code)
            self.assertIn("over_capacity=true", human_output)
            self.assertIn("pool_details:", human_output)
            self.assertIn("codex: max_running=1 running=2", human_output)

    def test_doctor_reports_worktree_mode_and_recovery_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)], worktree_mode="task")
            config = Config.load(str(config_path))
            task = create_task(config, "work", tmp, task_id="retained")
            task["execution_mode"] = "git_worktree"
            task["execution_branch"] = "cbr/retained"
            task["execution_base_ref"] = "HEAD"
            task["execution_base_head"] = "abc123"
            task["execution_worktree_status"] = "retained"
            task["execution_worktree_path"] = str(Path(tmp) / "worktrees" / "retained")
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual("task", report["worktree"]["mode"])
            self.assertEqual(str(Path(tmp) / "worktrees"), report["worktree"]["root"])
            self.assertEqual({"retained": 1}, report["worktree"]["tasks"]["by_status"])
            self.assertEqual(1, report["worktree"]["tasks"]["retained"])
            self.assertEqual(1, report["worktree"]["tasks"]["recovery_required"])

    def test_doctor_flags_stale_applied_metadata_as_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(tmp, [str(executable)], worktree_mode="task")
            config = Config.load(str(config_path))
            initial_head = git_output(repo, ["rev-parse", "HEAD"])
            (repo / "file.txt").write_text("base\napplied\n", encoding="utf-8")
            run_git(repo, ["commit", "-am", "applied"])
            applied_head = git_output(repo, ["rev-parse", "HEAD"])
            run_git(repo, ["reset", "--hard", initial_head])
            run_git(repo, ["branch", "cbr/stale-applied", initial_head])
            worktree_path = root / "worktrees" / "stale-applied"
            worktree_path.mkdir(parents=True)
            task = create_task(config, "work", str(repo), task_id="stale-applied")
            task.update(
                {
                    "status": "completed",
                    "review_status": "accepted",
                    "execution_mode": "git_worktree",
                    "execution_branch": "cbr/stale-applied",
                    "execution_repo_root": str(repo),
                    "execution_base_ref": "HEAD",
                    "execution_base_head": initial_head,
                    "execution_worktree_status": "retained",
                    "execution_worktree_path": str(worktree_path),
                    "execution_apply_status": "applied",
                    "execution_applied_head": applied_head,
                }
            )
            save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(1, report["worktree"]["tasks"]["retained"])
            self.assertEqual(1, report["worktree"]["tasks"]["recovery_required"])
            branch = report["worktree"]["task_branches"][0]
            self.assertEqual("stale-applied", branch["task_id"])
            self.assertTrue(branch["recovery_required"])
            self.assertEqual("stale_applied_metadata", branch["applied_metadata"]["status"])
            self.assertIn("execution_applied_head is not contained", branch["applied_metadata"]["reason"])

    def test_doctor_reports_task_branch_lifecycle_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(tmp, [str(executable)], worktree_mode="task")
            config = Config.load(str(config_path))
            head = git_output(repo, ["rev-parse", "HEAD"])
            run_git(repo, ["branch", "cbr/lifecycle", head])
            run_git(repo, ["remote", "add", "origin", str(root / "origin.git")])
            run_git(repo, ["update-ref", "refs/remotes/origin/cbr/lifecycle", head])
            run_git(repo, ["config", "branch.cbr/lifecycle.remote", "origin"])
            run_git(repo, ["config", "branch.cbr/lifecycle.merge", "refs/heads/cbr/lifecycle"])

            retained = create_task(config, "work", str(repo), task_id="lifecycle")
            retained.update(
                {
                    "status": "completed",
                    "review_status": "accepted",
                    "execution_mode": "git_worktree",
                    "execution_branch": "cbr/lifecycle",
                    "execution_repo_root": str(repo),
                    "execution_base_ref": "HEAD",
                    "execution_base_head": head,
                    "execution_worktree_status": "retained",
                    "execution_worktree_path": str(root / "worktrees" / "lifecycle"),
                    "execution_apply_status": "applied",
                    "execution_applied_head": head,
                }
            )
            save_task(config, retained)
            pruned = create_task(config, "work", str(repo), task_id="pruned")
            pruned.update(
                {
                    "status": "completed",
                    "review_status": "accepted",
                    "execution_mode": "git_worktree",
                    "execution_branch": "cbr/pruned",
                    "execution_repo_root": str(repo),
                    "execution_base_ref": "HEAD",
                    "execution_base_head": head,
                    "execution_worktree_status": "cleaned",
                    "execution_apply_status": "applied",
                    "execution_applied_head": head,
                    "execution_cleanup_kind": "applied",
                    "execution_cleanup_result_applied": True,
                    "execution_cleanup_branch_retained": False,
                    "execution_branch_prune_status": "pruned",
                    "execution_branch_pruned_head": head,
                    "execution_branch_pruned_at": "2026-06-26T00:00:00+00:00",
                }
            )
            save_task(config, pruned)

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)
            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            branches = {item["task_id"]: item for item in report["worktree"]["task_branches"]}
            self.assertEqual({"lifecycle", "pruned"}, set(branches))
            self.assertTrue(branches["lifecycle"]["retained_metadata"])
            self.assertTrue(branches["lifecycle"]["local_branch_exists"])
            self.assertEqual(head, branches["lifecycle"]["local_branch_head"])
            self.assertEqual("origin/cbr/lifecycle", branches["lifecycle"]["remote_task_branch"]["configured_upstream"])
            self.assertEqual(["origin/cbr/lifecycle"], branches["lifecycle"]["remote_task_branch"]["known_remote_refs"])
            self.assertTrue(branches["lifecycle"]["remote_task_branch"]["known"])
            self.assertFalse(branches["pruned"].get("retained_metadata", True))
            self.assertFalse(branches["pruned"]["local_branch_exists"])
            self.assertEqual("pruned", branches["pruned"]["branch_prune_status"])
            self.assertFalse(branches["pruned"]["remote_task_branch"]["known"])
            self.assertEqual(0, human_code)
            self.assertIn("task_branches:", human_output)
            self.assertIn("lifecycle branch=cbr/lifecycle", human_output)
            self.assertIn("remote_known=true", human_output)
            self.assertIn("pruned branch=cbr/pruned", human_output)

    def test_doctor_human_output_limits_task_branch_lifecycle_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config_path = write_config(tmp, [str(executable)], worktree_mode="task")
            config = Config.load(str(config_path))
            head = git_output(repo, ["rev-parse", "HEAD"])
            for index in range(25):
                task_id = f"branch-{index:02d}"
                task = create_task(config, "work", str(repo), task_id=task_id)
                task.update(
                    {
                        "status": "completed",
                        "review_status": "accepted",
                        "execution_mode": "git_worktree",
                        "execution_branch": f"cbr/{task_id}",
                        "execution_repo_root": str(repo),
                        "execution_base_ref": "HEAD",
                        "execution_base_head": head,
                        "execution_worktree_status": "cleaned",
                        "execution_apply_status": "applied",
                        "execution_applied_head": head,
                        "execution_cleanup_kind": "applied",
                    }
                )
                save_task(config, task)

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)
            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            self.assertEqual(25, len(report["worktree"]["task_branches"]))
            self.assertEqual(0, human_code)
            self.assertIn("task_branches_total: 25", human_output)
            self.assertIn("task_branches_displayed: 20", human_output)
            self.assertIn("task_branches_omitted: 5", human_output)
            self.assertIn("branch-00 branch=cbr/branch-00", human_output)
            self.assertIn("branch-19 branch=cbr/branch-19", human_output)
            self.assertNotIn("branch-20 branch=cbr/branch-20", human_output)
            self.assertNotIn("branch-24 branch=cbr/branch-24", human_output)

    def test_doctor_reports_startup_stall_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])
            config = Config.load(str(config_path))
            stalled = create_task(config, "stalled", tmp, task_id="stalled")
            stalled["last_error"] = "codex startup stalled before meaningful JSONL events"
            stalled["startup_stalled_at"] = "2026-06-20T12:00:00+00:00"
            stalled["last_progress"] = {
                "watchdog_reason": "startup_stall",
                "stdout_empty": False,
                "only_startup_events": True,
                "jsonl_event_count": 2,
                "first_meaningful_event_at": None,
            }
            save_task(config, stalled)
            running = create_task(config, "running", tmp, task_id="running")
            running["status"] = "running"
            running["started_at"] = "2000-01-01T00:00:00+00:00"
            save_task(config, running)

            code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)
            human_code, human_output = run_cli(["--config", str(config_path), "doctor"])

            self.assertEqual(0, code)
            self.assertEqual(1, report["tasks"]["startup_stalled"])
            self.assertEqual("stalled", report["tasks"]["recently_stalled"][0]["id"])
            self.assertEqual("running", report["tasks"]["running_no_progress"][0]["id"])
            self.assertEqual(0, human_code)
            self.assertIn("startup_stalled: 1", human_output)
            self.assertIn("running_no_progress: 1", human_output)

    def test_doctor_lock_summary_includes_same_host_pid_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            config_path = write_config(tmp, [str(executable)])
            config = Config.load(str(config_path))
            config.lock_file.write_text(
                json.dumps(
                    {
                        "created_at": "2999-01-01T00:00:00+00:00",
                        "hostname": "test-host",
                        "pid": 424242,
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch("codex_batch_runner.lock.socket.gethostname", return_value="test-host"), mock.patch(
                "codex_batch_runner.lock.pid_exists", return_value=False
            ):
                code, output = run_cli(["--config", str(config_path), "doctor", "--json"])
            report = json.loads(output)

            self.assertEqual(0, code)
            self.assertEqual(424242, report["lock"]["pid"])
            self.assertFalse(report["lock"]["pid_alive"])
            self.assertTrue(report["lock"]["stale"])


if __name__ == "__main__":
    unittest.main()

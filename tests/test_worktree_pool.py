from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_batch_runner.cli import main
from codex_batch_runner.config import Config
from codex_batch_runner.queue import create_task, load_task, save_task
from codex_batch_runner.worktree_pool import pool_slot_observation


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def run_cli(args: list[str]) -> tuple[int, dict]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(args)
    return code, json.loads(stdout.getvalue())


def init_repo(path: Path, policy: str | None = None, *, commit_policy: bool = True) -> None:
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test User")
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "file.txt")
    if policy is not None:
        (path / ".cbr.toml").write_text(policy, encoding="utf-8")
        if commit_policy:
            git(path, "add", ".cbr.toml")
    git(path, "commit", "-m", "initial")


def write_config(root: Path) -> Path:
    path = root / "config.json"
    path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "tasks"),
                "log_dir": str(root / "logs"),
                "event_dir": str(root / "events"),
                "lock_file": str(root / "runner.lock"),
                "state_file": str(root / "state.json"),
                "worktree_mode": "task",
                "worktree_root": str(root / "worktrees"),
                "codex_command": [
                    sys.executable,
                    "-c",
                    "raise SystemExit('codex must not run')",
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


POLICY = """
[worktree]
copy = [".env"]
retain = ["node_modules"]

[worktree.pool]
max_slots = 1
idle_ttl_hours = 24

[[worktree.prepare]]
command = ["python", "-c", "from pathlib import Path; Path('node_modules').mkdir(exist_ok=True)"]
cwd = "."
"""


class WorktreePoolTests(unittest.TestCase):
    def test_pool_slot_observation_is_exact_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            config = Config.load(str(write_config(root)))
            state_path = root / "worktrees" / ".pool-state.json"
            state_path.parent.mkdir(parents=True)
            state = {
                "schema_version": 1,
                "slots": [
                    {
                        "slot_id": "slot-01",
                        "repo_root": str(repo.resolve()),
                        "path": str(root / "worktrees" / "slot-01"),
                        "policy_fingerprint": "policy-v1",
                        "status": "leased",
                        "task_id": "task-a",
                        "branch": "cbr/task-a",
                        "last_released_at": None,
                    }
                ],
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            before = state_path.read_bytes()

            observation = pool_slot_observation(config, repo, "slot-01")

            self.assertEqual("current", observation["state_status"])
            self.assertEqual("leased", observation["slot_status"])
            self.assertEqual(
                str(root / "worktrees" / "slot-01"), observation["path"]
            )
            self.assertEqual("task-a", observation["task_id"])
            self.assertEqual("cbr/task-a", observation["branch"])
            self.assertEqual("policy-v1", observation["policy_fingerprint"])
            self.assertEqual(before, state_path.read_bytes())
            self.assertEqual(
                {"state_status": "current", "slot_status": "missing"},
                pool_slot_observation(config, repo, "slot-02"),
            )

    def test_hibernate_releases_and_reattach_reacquires_exact_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo, POLICY)
            (repo / ".env").write_text("VALUE=1\n", encoding="utf-8")
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "hibernate", str(repo), task_id="pool-hibernate")
            self.assertEqual(
                0,
                run_cli(
                    [
                        "--config",
                        str(config_path),
                        "worktree",
                        "prepare",
                        "pool-hibernate",
                        "--apply",
                        "--json",
                    ]
                )[0],
            )
            task = load_task(config, "pool-hibernate")
            slot = Path(task["execution_worktree_path"])
            (slot / "file.txt").write_text("base\nchange\n", encoding="utf-8")
            git(slot, "commit", "-am", "change")
            head = git(slot, "rev-parse", "HEAD")
            task["status"] = "completed"
            task["review_status"] = "unreviewed"
            task["execution_branch_head"] = head
            task["execution_mutation_provenance_history"] = [{"fixture": True}]
            save_task(config, task)

            safe_provenance = {
                "status": "mutation_observed",
                "unsafe_or_unreported": False,
            }
            with patch(
                "codex_batch_runner.worktree_hibernation._provenance",
                return_value=safe_provenance,
            ):
                hibernate_code, hibernate = run_cli(
                    [
                        "--config",
                        str(config_path),
                        "worktree",
                        "hibernate",
                        "pool-hibernate",
                        "--apply",
                        "--json",
                    ]
                )
            self.assertEqual(0, hibernate_code)
            self.assertTrue(hibernate["applied"])
            self.assertTrue(slot.is_dir())
            self.assertEqual("", git(slot, "branch", "--show-current"))
            hibernated = load_task(config, "pool-hibernate")
            self.assertEqual("hibernated", hibernated["execution_worktree_status"])
            self.assertEqual("released", hibernated["execution_worktree_lease_status"])
            self.assertNotIn("execution_worktree_path", hibernated)

            reattach_code, reattach = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "reattach",
                    "pool-hibernate",
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(0, reattach_code)
            self.assertTrue(reattach["applied"])
            reattached = load_task(config, "pool-hibernate")
            self.assertEqual("retained", reattached["execution_worktree_status"])
            self.assertEqual("leased", reattached["execution_worktree_lease_status"])
            self.assertEqual(slot, Path(reattached["execution_worktree_path"]))
            self.assertEqual("cbr/pool-hibernate", git(slot, "branch", "--show-current"))
            self.assertEqual(head, git(slot, "rev-parse", "HEAD"))

    def test_untracked_policy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo, POLICY, commit_policy=False)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work", str(repo), task_id="untracked-policy")

            code, report = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "prepare",
                    "untracked-policy",
                    "--apply",
                    "--json",
                ]
            )

            self.assertEqual(1, code)
            self.assertIn("must be tracked", report["errors"][0])
            self.assertNotIn("execution_worktree_path", load_task(config, "untracked-policy"))

    def test_policy_cannot_classify_tracked_paths_as_copy_or_retain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            policy = """
[worktree]
copy = ["file.txt"]
retain = []

[worktree.pool]
max_slots = 1
idle_ttl_hours = 24
"""
            init_repo(repo, policy)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "tracked path", str(repo), task_id="tracked-path")

            code, report = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "prepare",
                    "tracked-path",
                    "--apply",
                    "--json",
                ]
            )

            self.assertEqual(1, code)
            self.assertIn("is tracked by Git", report["errors"][0])

    def test_pool_reuses_slot_refreshes_copy_and_preserves_retain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo, POLICY)
            (repo / ".env").write_text("FIRST=1\n", encoding="utf-8")
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "work one", str(repo), task_id="pool-one")

            code, first_report = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "prepare",
                    "pool-one",
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(0, code)
            first = load_task(config, "pool-one")
            slot = Path(first["execution_worktree_path"])
            self.assertTrue(first["execution_worktree_pool"])
            self.assertEqual("FIRST=1\n", (slot / ".env").read_text(encoding="utf-8"))
            (slot / "node_modules" / "cache.txt").write_text("cached\n", encoding="utf-8")
            (slot / "scratch.txt").write_text("remove\n", encoding="utf-8")
            first["status"] = "completed"
            first["review_status"] = "accepted"
            save_task(config, first)

            cleanup_code, cleanup = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "cleanup",
                    "pool-one",
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(0, cleanup_code)
            self.assertEqual("idle", cleanup["pool"]["status"])
            self.assertTrue(slot.exists())
            self.assertFalse((slot / ".env").exists())
            self.assertFalse((slot / "scratch.txt").exists())
            self.assertEqual(
                "cached\n",
                (slot / "node_modules" / "cache.txt").read_text(encoding="utf-8"),
            )
            self.assertIn("cbr/pool-one", git(repo, "branch", "--list", "cbr/pool-one"))

            (repo / ".env").write_text("SECOND=2\n", encoding="utf-8")
            create_task(config, "work two", str(repo), task_id="pool-two")
            second_code, second_report = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "prepare",
                    "pool-two",
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(0, second_code)
            second = load_task(config, "pool-two")
            self.assertEqual(slot, Path(second["execution_worktree_path"]))
            self.assertFalse(second_report["pool"]["created"])
            self.assertEqual("SECOND=2\n", (slot / ".env").read_text(encoding="utf-8"))
            self.assertEqual(
                "cached\n",
                (slot / "node_modules" / "cache.txt").read_text(encoding="utf-8"),
            )

    def test_expired_idle_slot_is_sanitized_and_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo, POLICY)
            (repo / ".env").write_text("VALUE=1\n", encoding="utf-8")
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "first", str(repo), task_id="ttl-first")
            self.assertEqual(
                0,
                run_cli(
                    [
                        "--config",
                        str(config_path),
                        "worktree",
                        "prepare",
                        "ttl-first",
                        "--apply",
                        "--json",
                    ]
                )[0],
            )
            first = load_task(config, "ttl-first")
            slot = Path(first["execution_worktree_path"])
            (slot / "node_modules" / "cache.txt").write_text("old\n", encoding="utf-8")
            first["status"] = "completed"
            first["review_status"] = "accepted"
            save_task(config, first)
            self.assertEqual(
                0,
                run_cli(
                    [
                        "--config",
                        str(config_path),
                        "worktree",
                        "cleanup",
                        "ttl-first",
                        "--apply",
                        "--json",
                    ]
                )[0],
            )
            state_path = root / "worktrees" / ".pool-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["slots"][0]["last_released_at"] = "2000-01-01T00:00:00+00:00"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            create_task(config, "second", str(repo), task_id="ttl-second")
            code, report = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "prepare",
                    "ttl-second",
                    "--apply",
                    "--json",
                ]
            )

            self.assertEqual(0, code)
            self.assertTrue(report["pool"]["created"])
            second_slot = Path(load_task(config, "ttl-second")["execution_worktree_path"])
            self.assertEqual(slot, second_slot)
            self.assertFalse((second_slot / "node_modules" / "cache.txt").exists())

    def test_pool_max_slots_blocks_second_active_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo, POLICY)
            (repo / ".env").write_text("VALUE=1\n", encoding="utf-8")
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "first", str(repo), task_id="first")
            create_task(config, "second", str(repo), task_id="second")
            self.assertEqual(
                0,
                run_cli(
                    [
                        "--config",
                        str(config_path),
                        "worktree",
                        "prepare",
                        "first",
                        "--apply",
                        "--json",
                    ]
                )[0],
            )

            code, report = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "prepare",
                    "second",
                    "--apply",
                    "--json",
                ]
            )

            self.assertEqual(1, code)
            self.assertIn("max_slots=1", report["errors"][0])

    def test_cleanup_fails_closed_if_policy_changes_during_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo, POLICY)
            (repo / ".env").write_text("VALUE=1\n", encoding="utf-8")
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "policy change", str(repo), task_id="policy-change")
            self.assertEqual(
                0,
                run_cli(
                    [
                        "--config",
                        str(config_path),
                        "worktree",
                        "prepare",
                        "policy-change",
                        "--apply",
                        "--json",
                    ]
                )[0],
            )
            task = load_task(config, "policy-change")
            task["status"] = "completed"
            task["review_status"] = "accepted"
            save_task(config, task)
            changed_policy = POLICY.replace("idle_ttl_hours = 24", "idle_ttl_hours = 48")
            (repo / ".cbr.toml").write_text(changed_policy, encoding="utf-8")
            git(repo, "add", ".cbr.toml")
            git(repo, "commit", "-m", "change pool policy")

            code, report = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "cleanup",
                    "policy-change",
                    "--dry-run",
                    "--json",
                ]
            )

            self.assertEqual(1, code)
            self.assertIn("policy changed", report["errors"][0])
            self.assertTrue(Path(task["execution_worktree_path"]).exists())

    def test_prepare_command_cannot_modify_tracked_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            policy = """
[worktree]
copy = []
retain = []

[worktree.pool]
max_slots = 1
idle_ttl_hours = 24

[[worktree.prepare]]
command = ["python", "-c", "from pathlib import Path; Path('file.txt').write_text('changed')"]
cwd = "."
"""
            init_repo(repo, policy)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            create_task(config, "tracked mutation", str(repo), task_id="tracked-mutation")

            code, report = run_cli(
                [
                    "--config",
                    str(config_path),
                    "worktree",
                    "prepare",
                    "tracked-mutation",
                    "--apply",
                    "--json",
                ]
            )

            self.assertEqual(1, code)
            self.assertIn("modified tracked files", report["errors"][0])
            self.assertNotIn("execution_worktree_path", load_task(config, "tracked-mutation"))

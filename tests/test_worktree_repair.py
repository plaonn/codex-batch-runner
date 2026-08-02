from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import nullcontext, redirect_stdout
from unittest.mock import patch

from codex_batch_runner.config import Config
from codex_batch_runner.cli import main
from codex_batch_runner.events import list_events, read_jsonl
from codex_batch_runner.index import retained_events
from codex_batch_runner.lock import FileLock
from codex_batch_runner.prune import build_prune_report
from codex_batch_runner.queue import load_task, save_task as real_save_task
from codex_batch_runner.retention import build_retention_inventory_report
from codex_batch_runner.worktree_reconciliation import (
    build_worktree_reconciliation_plan,
)
from codex_batch_runner.worktree_repair import (
    WorktreeReconciliationRepairError,
    _append_audit as real_append_audit,
    _append_jsonl_durable as real_append_jsonl_durable,
    _assert_audit_namespace_binding as real_assert_audit_namespace_binding,
    _audit_payload,
    _audit_event,
    _audit_path,
    _fsync_directory as real_fsync_directory,
    repair_worktree_reconciliation,
)


SAFE_PROVENANCE = {
    "status": "mutation_observed",
    "unsafe_or_unreported": False,
}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def write_config(root: Path) -> Path:
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "runtime" / "tasks"),
                "log_dir": str(root / "runtime" / "logs"),
                "event_dir": str(root / "runtime" / "events"),
                "lock_file": str(root / "runtime" / "runner.lock"),
                "state_file": str(root / "runtime" / "state.json"),
                "worktree_root": str(root),
            }
        ),
        encoding="utf-8",
    )
    return config_path


def create_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "CBR Test")
    git(repo, "config", "user.email", "cbr@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def save_raw_task(config: Config, task: dict[str, object]) -> None:
    config.queue_dir.mkdir(parents=True, exist_ok=True)
    (config.queue_dir / f"{task['id']}.json").write_text(
        json.dumps(task, sort_keys=True), encoding="utf-8"
    )


def create_task(
    config: Config,
    repo: Path,
    base: str,
    *,
    task_id: str,
) -> tuple[dict[str, object], Path]:
    branch = f"cbr/{task_id}"
    worktree = repo.parent / f"worktree-{task_id}"
    git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    (worktree / "change.txt").write_text("change\n", encoding="utf-8")
    git(worktree, "add", "change.txt")
    git(worktree, "commit", "-m", "task change")
    head = git(worktree, "rev-parse", "HEAD")
    task: dict[str, object] = {
        "id": task_id,
        "project_id": "example-project",
        "title": "Sanitized fixture task",
        "status": "completed",
        "review_status": "unreviewed",
        "execution_mode": "git_worktree",
        "execution_repo_root": str(repo),
        "execution_worktree_path": str(worktree),
        "execution_worktree_status": "retained",
        "execution_branch": branch,
        "execution_base_head": base,
        "execution_branch_head": head,
        "execution_mutation_provenance_history": [{"fixture": True}],
    }
    save_raw_task(config, task)
    return task, worktree


def make_exact_candidate(
    config: Config,
    repo: Path,
    base: str,
    *,
    task_id: str,
    worktree_status: str = "retained",
) -> dict[str, object]:
    task, worktree = create_task(config, repo, base, task_id=task_id)
    head = str(task["execution_branch_head"])
    git(repo, "merge", "--ff-only", str(task["execution_branch"]))
    git(repo, "worktree", "remove", str(worktree))
    task.update(
        {
            "review_status": "accepted",
            "execution_apply_status": "applied",
            "execution_applied_head": head,
            "execution_applied_at": "2030-01-01T00:00:00Z",
            "execution_apply_target": "main",
            "execution_cleanup_kind": "applied",
            "execution_cleanup_reason": "execution_apply_status=applied",
            "execution_cleanup_branch_retained": True,
            "execution_cleanup_result_applied": True,
            "execution_cleaned_at": "2030-01-01T00:01:00Z",
            "execution_worktree_status": worktree_status,
        }
    )
    save_raw_task(config, task)
    return task


def save_pool_state(
    root: Path,
    repo: Path,
    *,
    status: str,
    task_id: str | None,
    branch: str | None,
    last_released_at: str | None = None,
) -> Path:
    path = root / ".pool-state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "slots": [
                    {
                        "slot_id": "slot-01",
                        "repo_root": str(repo.resolve()),
                        "path": str((root / "pool-slot").resolve()),
                        "policy_fingerprint": "policy-v1",
                        "status": status,
                        "task_id": task_id,
                        "branch": branch,
                        "last_released_at": last_released_at,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def make_pooled_exact_candidate(
    config: Config,
    root: Path,
    repo: Path,
    base: str,
    *,
    task_id: str,
) -> tuple[dict[str, object], Path]:
    task = make_exact_candidate(config, repo, base, task_id=task_id)
    task.update(
        {
            "execution_worktree_pool": True,
            "execution_worktree_pool_slot_id": "slot-01",
            "execution_worktree_policy_fingerprint": "policy-v1",
            "execution_worktree_lease_status": "released",
            "execution_worktree_pool_released_at": "2030-01-01T00:02:00Z",
        }
    )
    pool_slot = root / "pool-slot"
    git(repo, "worktree", "add", "--detach", str(pool_slot), base)
    task["execution_worktree_path"] = str(pool_slot.resolve())
    save_raw_task(config, task)
    pool_path = save_pool_state(
        root,
        repo,
        status="idle",
        task_id=None,
        branch=None,
        last_released_at="2030-01-01T00:02:00Z",
    )
    return task, pool_path


def plan_item(config: Config, task_id: str) -> dict[str, object]:
    return build_worktree_reconciliation_plan(config, task_id=task_id)["items"][0]


def exact_plan_item(config: Config, task_id: str) -> dict[str, object]:
    with patch(
        "codex_batch_runner.worktree_reconciliation._provenance",
        return_value=SAFE_PROVENANCE,
    ):
        return plan_item(config, task_id)


def exact_repair(
    config: Config,
    task_id: str,
    digest: str,
    *,
    apply: bool = False,
) -> dict[str, object]:
    with patch(
        "codex_batch_runner.worktree_reconciliation._provenance",
        return_value=SAFE_PROVENANCE,
    ):
        return repair_worktree_reconciliation(
            config,
            task_id,
            approved_source_digest=digest,
            apply=apply,
        )


def event_records(config: Config) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    if not config.event_dir.exists():
        return records
    for path in sorted(config.event_dir.rglob("*.audit")):
        records.extend(read_jsonl(path))
    return records


def git_state(repo: Path) -> tuple[str, str]:
    return (
        git(repo, "worktree", "list", "--porcelain"),
        git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
    )


class WorktreeReconciliationRepairTests(unittest.TestCase):
    def test_retained_dry_run_and_apply_change_only_allowlisted_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            original = make_exact_candidate(
                config, repo, base, task_id="repair-retained"
            )
            item = exact_plan_item(config, "repair-retained")
            self.assertEqual("exact_repair_candidate", item["action_class"])
            digest = str(item["source_snapshot_digest"])
            before_git = git_state(repo)

            preview = exact_repair(config, "repair-retained", digest)
            self.assertEqual("planned", preview["action"])
            self.assertFalse(preview["mutation"]["task_performed"])
            self.assertEqual(original, load_task(config, "repair-retained"))
            self.assertFalse(config.event_dir.exists())

            applied = exact_repair(config, "repair-retained", digest, apply=True)
            self.assertEqual("applied", applied["action"])
            updated = load_task(config, "repair-retained")
            expected = copy.deepcopy(original)
            expected["execution_worktree_status"] = "cleaned"
            self.assertEqual(expected, updated)
            self.assertEqual(before_git, git_state(repo))
            records = event_records(config)
            self.assertEqual(2, len(records))
            self.assertEqual(["prepared", "committed"], [r["payload"]["phase"] for r in records])
            self.assertTrue(
                all(r["payload"]["subtype"] == "worktree_reconciliation_exact_repair_v1" for r in records)
            )
            self.assertTrue(all(r.get("project_root") is None for r in records))

    def test_recovery_required_to_cleaned_and_duplicate_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            original = make_exact_candidate(
                config,
                repo,
                base,
                task_id="repair-recovery",
                worktree_status="recovery_required",
            )
            digest = str(exact_plan_item(config, "repair-recovery")["source_snapshot_digest"])
            first = exact_repair(config, "repair-recovery", digest, apply=True)
            self.assertEqual("applied", first["action"])
            after_first = load_task(config, "repair-recovery")
            self.assertEqual("cleaned", after_first["execution_worktree_status"])

            second = exact_repair(config, "repair-recovery", digest, apply=True)
            self.assertEqual("noop", second["action"])
            self.assertFalse(second["mutation"]["task_performed"])
            self.assertEqual(after_first, load_task(config, "repair-recovery"))
            self.assertEqual(2, len(event_records(config)))
            expected = copy.deepcopy(original)
            expected["execution_worktree_status"] = "cleaned"
            self.assertEqual(expected, after_first)

    def test_stale_digest_and_live_drift_reject_without_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="repair-stale")
            approved = str(exact_plan_item(config, "repair-stale")["source_snapshot_digest"])
            task["status"] = "running"
            save_raw_task(config, task)
            before = load_task(config, "repair-stale")
            before_git = git_state(repo)

            with self.assertRaisesRegex(
                WorktreeReconciliationRepairError, "approved source digest"
            ):
                exact_repair(config, "repair-stale", approved, apply=True)
            self.assertEqual(before, load_task(config, "repair-stale"))
            self.assertEqual(before_git, git_state(repo))
            self.assertFalse(config.event_dir.exists())

    def test_requested_task_identity_mismatch_cannot_alias_save_target(self) -> None:
        for victim_exists in (False, True):
            with self.subTest(victim_exists=victim_exists), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = Config.load(str(write_config(root)))
                config.queue_dir.mkdir(parents=True)
                requested_path = config.queue_dir / "requested.json"
                victim_path = config.queue_dir / "victim.json"
                requested_path.write_text(
                    json.dumps(
                        {
                            "id": "victim",
                            "execution_worktree_status": "retained",
                        }
                    ),
                    encoding="utf-8",
                )
                if victim_exists:
                    victim_path.write_text(
                        json.dumps({"id": "victim", "sentinel": True}),
                        encoding="utf-8",
                    )
                requested_before = requested_path.read_bytes()
                victim_before = victim_path.read_bytes() if victim_exists else None

                with self.assertRaisesRegex(
                    WorktreeReconciliationRepairError, "document id does not match"
                ):
                    repair_worktree_reconciliation(
                        config,
                        "requested",
                        approved_source_digest="sha256:" + "0" * 64,
                        apply=True,
                    )
                self.assertEqual(requested_before, requested_path.read_bytes())
                self.assertEqual(victim_exists, victim_path.exists())
                if victim_exists:
                    self.assertEqual(victim_before, victim_path.read_bytes())
                self.assertEqual(
                    {path.name for path in config.queue_dir.glob("*.json")},
                    {"requested.json", "victim.json"}
                    if victim_exists
                    else {"requested.json"},
                )
                self.assertFalse(config.event_dir.exists())

    def test_post_prepare_branch_and_registry_drift_block_task_write(self) -> None:
        scenarios = ("branch_deleted", "registry_reattached")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = Config.load(str(write_config(root)))
                repo, base = create_repo(root)
                task = make_exact_candidate(
                    config, repo, base, task_id=f"repair-{scenario}"
                )
                digest = str(
                    exact_plan_item(config, f"repair-{scenario}")[
                        "source_snapshot_digest"
                    ]
                )
                original = copy.deepcopy(task)

                def append_then_drift(*args: object, **kwargs: object) -> None:
                    real_append_audit(*args, **kwargs)
                    if kwargs.get("phase") != "prepared":
                        return
                    if scenario == "branch_deleted":
                        git(repo, "branch", "-D", str(task["execution_branch"]))
                    else:
                        git(
                            repo,
                            "worktree",
                            "add",
                            str(root / "reattached"),
                            str(task["execution_branch"]),
                        )

                with patch(
                    "codex_batch_runner.worktree_repair._append_audit",
                    side_effect=append_then_drift,
                ):
                    with self.assertRaisesRegex(
                        WorktreeReconciliationRepairError,
                        "approved source digest|live C1 action",
                    ):
                        exact_repair(
                            config, f"repair-{scenario}", digest, apply=True
                        )
                self.assertEqual(original, load_task(config, f"repair-{scenario}"))
                self.assertEqual(
                    ["prepared"],
                    [r["payload"]["phase"] for r in event_records(config)],
                )

    def test_post_prepare_pool_state_drift_blocks_task_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task, pool_path = make_pooled_exact_candidate(
                config, root, repo, base, task_id="repair-pool-drift"
            )
            digest = str(
                exact_plan_item(config, "repair-pool-drift")[
                    "source_snapshot_digest"
                ]
            )
            original = copy.deepcopy(task)

            def append_then_drift(*args: object, **kwargs: object) -> None:
                real_append_audit(*args, **kwargs)
                if kwargs.get("phase") == "prepared":
                    save_pool_state(
                        root,
                        repo,
                        status="leased",
                        task_id="different-task",
                        branch="cbr/different-task",
                    )

            with patch(
                "codex_batch_runner.worktree_repair._append_audit",
                side_effect=append_then_drift,
            ):
                with self.assertRaisesRegex(
                    WorktreeReconciliationRepairError,
                    "approved source digest|live C1 action",
                ):
                    exact_repair(
                        config, "repair-pool-drift", digest, apply=True
                    )
            self.assertEqual(original, load_task(config, "repair-pool-drift"))
            self.assertTrue(pool_path.exists())
            self.assertEqual(
                ["prepared"],
                [r["payload"]["phase"] for r in event_records(config)],
            )

    def test_adverse_live_classes_reject_without_task_or_audit_mutation(self) -> None:
        scenarios = (
            ("active", {"status": "running"}),
            ("resumable", {"status": "needs_resume"}),
            ("ambiguous", {"execution_mutation_provenance_history": []}),
            ("pool-conflict", {
                "execution_worktree_pool": True,
                "execution_worktree_pool_slot_id": "slot-01",
                "execution_worktree_policy_fingerprint": "policy-v1",
                "execution_worktree_lease_status": "released",
                "execution_worktree_pool_released_at": "2030-01-01T00:02:00Z",
            }),
        )
        for name, updates in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = Config.load(str(write_config(root)))
                repo, base = create_repo(root)
                task = make_exact_candidate(config, repo, base, task_id=f"repair-{name}")
                task.update(updates)
                save_raw_task(config, task)
                pool_state = root / ".pool-state.json"
                if name == "pool-conflict":
                    pool_state.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "slots": [
                                    {
                                        "slot_id": "slot-01",
                                        "repo_root": str(repo),
                                        "path": str(root / "pool-slot"),
                                        "policy_fingerprint": "policy-v1",
                                        "status": "leased",
                                        "task_id": "different-task",
                                        "branch": "cbr/different-task",
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                pool_state_before = pool_state.read_bytes() if pool_state.exists() else None
                item = plan_item(config, f"repair-{name}")
                self.assertNotEqual("exact_repair_candidate", item["action_class"])
                before = load_task(config, f"repair-{name}")
                before_git = git_state(repo)
                with self.assertRaisesRegex(
                    WorktreeReconciliationRepairError, "live C1 action"
                ):
                    repair_worktree_reconciliation(
                        config,
                        f"repair-{name}",
                        approved_source_digest=str(item["source_snapshot_digest"]),
                        apply=True,
                    )
                self.assertEqual(before, load_task(config, f"repair-{name}"))
                self.assertEqual(before_git, git_state(repo))
                self.assertEqual(
                    pool_state_before,
                    pool_state.read_bytes() if pool_state.exists() else None,
                )
                self.assertFalse(config.event_dir.exists())

    def test_dirty_and_missing_branch_reject_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task, worktree = create_task(config, repo, base, task_id="repair-dirty")
            (worktree / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            dirty_item = plan_item(config, "repair-dirty")
            self.assertEqual("manual_review", dirty_item["action_class"])
            before = copy.deepcopy(task)
            before["execution_worktree_status"] = "retained"
            with self.assertRaisesRegex(WorktreeReconciliationRepairError, "live C1 action"):
                repair_worktree_reconciliation(
                    config,
                    "repair-dirty",
                    approved_source_digest=str(dirty_item["source_snapshot_digest"]),
                    apply=True,
                )
            self.assertEqual(before, load_task(config, "repair-dirty"))
            self.assertFalse(config.event_dir.exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            task = make_exact_candidate(config, repo, base, task_id="repair-missing")
            git(repo, "branch", "-D", str(task["execution_branch"]))
            missing_item = plan_item(config, "repair-missing")
            self.assertEqual(
                "unrecoverable_without_owner_decision", missing_item["action_class"]
            )
            before = load_task(config, "repair-missing")
            before_git = git_state(repo)
            with self.assertRaisesRegex(WorktreeReconciliationRepairError, "live C1 action"):
                repair_worktree_reconciliation(
                    config,
                    "repair-missing",
                    approved_source_digest=str(missing_item["source_snapshot_digest"]),
                    apply=True,
                )
            self.assertEqual(before, load_task(config, "repair-missing"))
            self.assertEqual(before_git, git_state(repo))
            self.assertFalse(config.event_dir.exists())

    def test_prepared_event_then_task_save_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            original = make_exact_candidate(config, repo, base, task_id="repair-save-fail")
            digest = str(exact_plan_item(config, "repair-save-fail")["source_snapshot_digest"])

            with patch(
                "codex_batch_runner.worktree_repair.save_task",
                side_effect=OSError("synthetic task save failure"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic task save failure"):
                    exact_repair(config, "repair-save-fail", digest, apply=True)
            self.assertEqual(original, load_task(config, "repair-save-fail"))
            self.assertEqual(["prepared"], [r["payload"]["phase"] for r in event_records(config)])

            retried = exact_repair(config, "repair-save-fail", digest, apply=True)
            self.assertEqual("applied", retried["action"])
            self.assertEqual(["prepared", "committed"], [r["payload"]["phase"] for r in event_records(config)])

    def test_committed_event_failure_recovers_without_extra_task_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            original = make_exact_candidate(config, repo, base, task_id="repair-event-fail")
            digest = str(exact_plan_item(config, "repair-event-fail")["source_snapshot_digest"])
            calls = 0

            def fail_second(*args: object, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic committed event failure")
                return real_append_jsonl_durable(*args, **kwargs)

            with patch(
                "codex_batch_runner.worktree_repair._append_jsonl_durable",
                side_effect=fail_second,
            ):
                with self.assertRaisesRegex(OSError, "synthetic committed event failure"):
                    exact_repair(config, "repair-event-fail", digest, apply=True)
            expected = copy.deepcopy(original)
            expected["execution_worktree_status"] = "cleaned"
            self.assertEqual(expected, load_task(config, "repair-event-fail"))
            self.assertEqual(["prepared"], [r["payload"]["phase"] for r in event_records(config)])

            recovered = exact_repair(config, "repair-event-fail", digest, apply=True)
            self.assertEqual("recovered", recovered["action"])
            self.assertFalse(recovered["mutation"]["task_performed"])
            self.assertEqual(expected, load_task(config, "repair-event-fail"))
            self.assertEqual(["prepared", "committed"], [r["payload"]["phase"] for r in event_records(config)])
            noop = exact_repair(config, "repair-event-fail", digest, apply=True)
            self.assertEqual("noop", noop["action"])
            self.assertEqual(2, len(event_records(config)))

    def test_repair_audit_is_isolated_from_ordinary_event_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            make_exact_candidate(config, repo, base, task_id="repair-visible")
            digest = str(
                exact_plan_item(config, "repair-visible")["source_snapshot_digest"]
            )

            exact_repair(config, "repair-visible", digest, apply=True)

            audit_path = _audit_path(config, "repair-visible")
            self.assertTrue(audit_path.is_file())
            self.assertEqual(".audit", audit_path.suffix)
            self.assertEqual(
                ["prepared", "committed"],
                [record["payload"]["phase"] for record in event_records(config)],
            )
            self.assertEqual(
                [], list_events(config, task_id="repair-visible", limit=0)
            )
            self.assertEqual([], retained_events(config))
            inventory = build_retention_inventory_report(config)
            self.assertEqual(0, inventory["summary"]["event_file_count"])
            self.assertEqual([], inventory["event_files"])

            prune_dry_run = build_prune_report(config, age_days=0)
            self.assertEqual(0, prune_dry_run["event_candidate_count"])
            prune_apply = build_prune_report(config, age_days=0, apply=True)
            self.assertEqual(0, prune_apply["event_candidate_count"])
            self.assertEqual(0, prune_apply["deleted_files"])
            self.assertTrue(audit_path.is_file())

    def test_repair_audit_does_not_disturb_date_event_cursor_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            make_exact_candidate(config, repo, base, task_id="repair-cursor-safe")
            digest = str(
                exact_plan_item(config, "repair-cursor-safe")[
                    "source_snapshot_digest"
                ]
            )
            exact_repair(config, "repair-cursor-safe", digest, apply=True)
            audit_path = _audit_path(config, "repair-cursor-safe")

            first = config.event_dir / "2000-01-01.jsonl"
            current = config.event_dir / "2000-01-02.jsonl"
            first.write_text('{"event_id":"first"}\n', encoding="utf-8")
            current.write_text('{"event_id":"current"}\n', encoding="utf-8")
            cursor = root / "notifier-cursor.json"
            cursor.write_text(
                json.dumps(
                    {
                        "current_event_file": str(current),
                        "current_byte_offset": 0,
                    }
                ),
                encoding="utf-8",
            )

            report = build_prune_report(
                config,
                age_days=0,
                apply=True,
                notifier_cursor_state_paths=[cursor],
            )

            self.assertEqual(2, report["event_candidate_count"])
            by_name = {
                Path(candidate["path"]).name: candidate
                for candidate in report["event_candidates"]
            }
            self.assertTrue(by_name["2000-01-01.jsonl"]["deleted"])
            self.assertTrue(by_name["2000-01-02.jsonl"]["skipped"])
            self.assertEqual(
                "notifier cursor has not fully processed this event file",
                by_name["2000-01-02.jsonl"]["reason"],
            )
            self.assertTrue(audit_path.is_file())

    def test_audit_namespace_symlink_or_non_directory_fails_before_mutation(self) -> None:
        for scenario in ("symlink", "non_directory"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = Config.load(str(write_config(root)))
                repo, base = create_repo(root)
                original = make_exact_candidate(
                    config, repo, base, task_id=f"repair-namespace-{scenario}"
                )
                digest = str(
                    exact_plan_item(config, f"repair-namespace-{scenario}")[
                        "source_snapshot_digest"
                    ]
                )
                namespace = _audit_path(
                    config, f"repair-namespace-{scenario}"
                ).parent
                namespace.parent.mkdir(parents=True, exist_ok=True)
                outside = root / "outside-audit"
                outside.mkdir()
                if scenario == "symlink":
                    namespace.symlink_to(outside, target_is_directory=True)
                else:
                    namespace.write_bytes(b"not a directory")
                namespace_before = (
                    namespace.read_bytes() if scenario == "non_directory" else None
                )

                with self.assertRaisesRegex(
                    WorktreeReconciliationRepairError,
                    "non-symlink directory|secure directory",
                ):
                    exact_repair(
                        config,
                        f"repair-namespace-{scenario}",
                        digest,
                        apply=True,
                    )

                self.assertEqual(
                    original,
                    load_task(config, f"repair-namespace-{scenario}"),
                )
                self.assertEqual([], list(outside.iterdir()))
                if scenario == "symlink":
                    self.assertTrue(namespace.is_symlink())
                else:
                    self.assertEqual(namespace_before, namespace.read_bytes())

    def test_audit_namespace_swap_is_detected_before_file_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            original = make_exact_candidate(
                config, repo, base, task_id="repair-namespace-swap"
            )
            digest = str(
                exact_plan_item(config, "repair-namespace-swap")[
                    "source_snapshot_digest"
                ]
            )
            namespace = _audit_path(config, "repair-namespace-swap").parent
            namespace.mkdir(parents=True)
            displaced = root / "displaced-audit"
            outside = root / "outside-audit"
            outside.mkdir()
            calls = 0

            def swap_before_final_open(event_fd: int, namespace_fd: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 4:
                    namespace.rename(displaced)
                    namespace.symlink_to(outside, target_is_directory=True)
                real_assert_audit_namespace_binding(event_fd, namespace_fd)

            with patch(
                "codex_batch_runner.worktree_repair._assert_audit_namespace_binding",
                side_effect=swap_before_final_open,
            ):
                with self.assertRaisesRegex(
                    WorktreeReconciliationRepairError,
                    "namespace binding changed",
                ):
                    exact_repair(
                        config,
                        "repair-namespace-swap",
                        digest,
                        apply=True,
                    )

            self.assertEqual(
                original, load_task(config, "repair-namespace-swap")
            )
            self.assertEqual([], list(outside.iterdir()))
            self.assertEqual([], list(displaced.iterdir()))
            self.assertTrue(namespace.is_symlink())

    def test_torn_committed_append_fails_closed_until_manual_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            original = make_exact_candidate(
                config, repo, base, task_id="repair-commit-torn"
            )
            digest = str(
                exact_plan_item(config, "repair-commit-torn")[
                    "source_snapshot_digest"
                ]
            )
            calls = 0

            def tear_second(path: Path, event: dict[str, object]) -> None:
                nonlocal calls
                calls += 1
                if calls != 2:
                    real_append_jsonl_durable(path, event)
                    return
                encoded = json.dumps(
                    event,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                with path.open("ab") as file:
                    file.write(encoded[: len(encoded) // 2])
                    file.flush()
                    os.fsync(file.fileno())
                raise OSError("synthetic torn committed append")

            with patch(
                "codex_batch_runner.worktree_repair._append_jsonl_durable",
                side_effect=tear_second,
            ):
                with self.assertRaisesRegex(
                    OSError, "synthetic torn committed append"
                ):
                    exact_repair(config, "repair-commit-torn", digest, apply=True)

            expected = copy.deepcopy(original)
            expected["execution_worktree_status"] = "cleaned"
            self.assertEqual(expected, load_task(config, "repair-commit-torn"))
            audit_path = _audit_path(config, "repair-commit-torn")
            torn_audit = audit_path.read_bytes()
            self.assertFalse(torn_audit.endswith(b"\n"))

            with self.assertRaisesRegex(
                WorktreeReconciliationRepairError, "torn tail"
            ):
                exact_repair(config, "repair-commit-torn", digest, apply=True)
            self.assertEqual(expected, load_task(config, "repair-commit-torn"))
            self.assertEqual(torn_audit, audit_path.read_bytes())

    def test_uncertain_task_save_after_atomic_write_recovers_from_prepared_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            original = make_exact_candidate(
                config, repo, base, task_id="repair-save-uncertain"
            )
            digest = str(
                exact_plan_item(config, "repair-save-uncertain")[
                    "source_snapshot_digest"
                ]
            )

            def write_then_fail(
                write_config: Config,
                task: dict[str, object],
                *,
                touch_updated_at: bool = True,
            ) -> None:
                real_save_task(
                    write_config, task, touch_updated_at=touch_updated_at
                )
                raise OSError("synthetic uncertain task save result")

            with patch(
                "codex_batch_runner.worktree_repair.save_task",
                side_effect=write_then_fail,
            ):
                with self.assertRaisesRegex(
                    OSError, "synthetic uncertain task save result"
                ):
                    exact_repair(
                        config, "repair-save-uncertain", digest, apply=True
                    )
            expected = copy.deepcopy(original)
            expected["execution_worktree_status"] = "cleaned"
            self.assertEqual(expected, load_task(config, "repair-save-uncertain"))
            self.assertEqual(
                ["prepared"],
                [r["payload"]["phase"] for r in event_records(config)],
            )

            recovered = exact_repair(
                config, "repair-save-uncertain", digest, apply=True
            )
            self.assertEqual("recovered", recovered["action"])
            self.assertEqual(expected, load_task(config, "repair-save-uncertain"))
            self.assertEqual(
                ["prepared", "committed"],
                [r["payload"]["phase"] for r in event_records(config)],
            )

    def test_strict_audit_reader_rejects_malformed_torn_and_unreadable_history(self) -> None:
        scenarios = ("malformed", "torn", "unreadable")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = Config.load(str(write_config(root)))
                repo, base = create_repo(root)
                task = make_exact_candidate(
                    config, repo, base, task_id=f"repair-audit-{scenario}"
                )
                digest = str(
                    exact_plan_item(config, f"repair-audit-{scenario}")[
                        "source_snapshot_digest"
                    ]
                )
                path = _audit_path(config, f"repair-audit-{scenario}")
                path.parent.mkdir(parents=True)
                path.write_bytes(b'{"incomplete":\n' if scenario != "torn" else b"{")
                before_audit = path.read_bytes()
                before_task = copy.deepcopy(task)

                if scenario == "unreadable":
                    original_open = os.open

                    def deny_read(
                        candidate: str | bytes | Path,
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        if str(candidate) == path.name:
                            raise PermissionError("synthetic unreadable audit")
                        if dir_fd is None:
                            return original_open(candidate, flags, mode)
                        return original_open(
                            candidate, flags, mode, dir_fd=dir_fd
                        )

                    reader_patch = patch(
                        "codex_batch_runner.worktree_repair.os.open",
                        side_effect=deny_read,
                    )
                    primitive_patch = patch(
                        "codex_batch_runner.worktree_repair._require_secure_directory_primitives",
                        return_value=None,
                    )
                else:
                    reader_patch = nullcontext()
                    primitive_patch = nullcontext()
                with reader_patch, primitive_patch, self.assertRaisesRegex(
                    WorktreeReconciliationRepairError,
                    "malformed JSON|torn tail|unreadable",
                ):
                    exact_repair(
                        config,
                        f"repair-audit-{scenario}",
                        digest,
                        apply=True,
                    )
                self.assertEqual(
                    before_task, load_task(config, f"repair-audit-{scenario}")
                )
                self.assertEqual(before_audit, path.read_bytes())

    def test_strict_audit_reader_rejects_sequence_duplicates_and_extra_fields(self) -> None:
        scenarios = (
            "committed_first",
            "duplicate_commit",
            "extra_event_field",
            "extra_payload_field",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = Config.load(str(write_config(root)))
                repo, base = create_repo(root)
                task = make_exact_candidate(
                    config, repo, base, task_id=f"repair-order-{scenario}"
                )
                digest = str(
                    exact_plan_item(config, f"repair-order-{scenario}")[
                        "source_snapshot_digest"
                    ]
                )
                postimage = copy.deepcopy(task)
                postimage["execution_worktree_status"] = "cleaned"
                audit = _audit_payload(task, postimage, digest)
                path = _audit_path(config, f"repair-order-{scenario}")

                prepared_payload = {**audit, "phase": "prepared"}
                committed_false = {
                    **audit,
                    "phase": "committed",
                    "recovered_from_partial": False,
                }
                if scenario == "committed_first":
                    real_append_jsonl_durable(
                        path,
                        _audit_event(str(task["id"]), committed_false),
                    )
                elif scenario == "duplicate_commit":
                    real_append_jsonl_durable(
                        path,
                        _audit_event(str(task["id"]), prepared_payload),
                    )
                    real_append_jsonl_durable(
                        path,
                        _audit_event(str(task["id"]), committed_false),
                    )
                    committed_true = {
                        **committed_false,
                        "recovered_from_partial": True,
                    }
                    real_append_jsonl_durable(
                        path,
                        _audit_event(str(task["id"]), committed_true),
                    )
                elif scenario == "extra_event_field":
                    event = _audit_event(str(task["id"]), prepared_payload)
                    event["unexpected"] = True
                    real_append_jsonl_durable(path, event)
                else:
                    event = _audit_event(
                        str(task["id"]),
                        {**prepared_payload, "unexpected": True},
                    )
                    real_append_jsonl_durable(path, event)
                before_audit = path.read_bytes()

                with self.assertRaisesRegex(
                    WorktreeReconciliationRepairError,
                    "precedes prepared|duplicate repair audit|not canonical|non-canonical",
                ):
                    exact_repair(
                        config,
                        f"repair-order-{scenario}",
                        digest,
                        apply=True,
                    )
                self.assertEqual(task, load_task(config, f"repair-order-{scenario}"))
                self.assertEqual(before_audit, path.read_bytes())

    def test_repair_local_fsync_failures_leave_recoverable_ordered_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            original = make_exact_candidate(
                config, repo, base, task_id="repair-event-fsync"
            )
            digest = str(
                exact_plan_item(config, "repair-event-fsync")[
                    "source_snapshot_digest"
                ]
            )
            with patch(
                "codex_batch_runner.worktree_repair._fsync_file",
                side_effect=OSError("synthetic event fsync failure"),
            ):
                with self.assertRaisesRegex(OSError, "event fsync failure"):
                    exact_repair(config, "repair-event-fsync", digest, apply=True)
            self.assertEqual(original, load_task(config, "repair-event-fsync"))
            self.assertEqual(
                ["prepared"],
                [r["payload"]["phase"] for r in event_records(config)],
            )
            applied = exact_repair(
                config, "repair-event-fsync", digest, apply=True
            )
            self.assertEqual("applied", applied["action"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            original = make_exact_candidate(
                config, repo, base, task_id="repair-task-dir-fsync"
            )
            digest = str(
                exact_plan_item(config, "repair-task-dir-fsync")[
                    "source_snapshot_digest"
                ]
            )

            def fail_queue_directory(candidate: Path) -> None:
                if candidate == config.queue_dir:
                    raise WorktreeReconciliationRepairError(
                        "synthetic task directory fsync failure"
                    )
                real_fsync_directory(candidate)

            with patch(
                "codex_batch_runner.worktree_repair._fsync_directory",
                side_effect=fail_queue_directory,
            ):
                with self.assertRaisesRegex(
                    WorktreeReconciliationRepairError,
                    "task directory fsync failure",
                ):
                    exact_repair(
                        config, "repair-task-dir-fsync", digest, apply=True
                    )
            expected = copy.deepcopy(original)
            expected["execution_worktree_status"] = "cleaned"
            self.assertEqual(expected, load_task(config, "repair-task-dir-fsync"))
            self.assertEqual(
                ["prepared"],
                [r["payload"]["phase"] for r in event_records(config)],
            )
            recovered = exact_repair(
                config, "repair-task-dir-fsync", digest, apply=True
            )
            self.assertEqual("recovered", recovered["action"])

    def test_apply_requires_available_owned_queue_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.load(str(write_config(root)))
            repo, base = create_repo(root)
            original = make_exact_candidate(config, repo, base, task_id="repair-lock")
            digest = str(exact_plan_item(config, "repair-lock")["source_snapshot_digest"])
            foreign = FileLock(config.lock_file, config.stale_lock_seconds)
            self.assertTrue(foreign.acquire(task_id="different-task"))
            try:
                with self.assertRaisesRegex(
                    WorktreeReconciliationRepairError, "active queue lock"
                ):
                    exact_repair(config, "repair-lock", digest, apply=True)
            finally:
                foreign.release()
            self.assertEqual(original, load_task(config, "repair-lock"))
            self.assertFalse(config.event_dir.exists())

            with patch(
                "codex_batch_runner.worktree_repair.read_lock_metadata",
                return_value={"pid": 999999, "hostname": "different-host"},
            ):
                with self.assertRaisesRegex(
                    WorktreeReconciliationRepairError, "does not own"
                ):
                    exact_repair(config, "repair-lock", digest, apply=True)
            self.assertEqual(original, load_task(config, "repair-lock"))
            self.assertFalse(config.event_dir.exists())

    def test_cli_is_dry_run_by_default_and_requires_exact_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            config = Config.load(str(config_path))
            repo, base = create_repo(root)
            original = make_exact_candidate(config, repo, base, task_id="repair-cli")
            digest = str(exact_plan_item(config, "repair-cli")["source_snapshot_digest"])
            output = io.StringIO()
            with patch(
                "codex_batch_runner.worktree_reconciliation._provenance",
                return_value=SAFE_PROVENANCE,
            ), redirect_stdout(output):
                code = main(
                    [
                        "--config",
                        str(config_path),
                        "worktree",
                        "reconciliation-repair",
                        "repair-cli",
                        "--approved-source-digest",
                        digest,
                        "--json",
                    ]
                )
            self.assertEqual(0, code)
            self.assertEqual("planned", json.loads(output.getvalue())["action"])
            self.assertEqual(original, load_task(config, "repair-cli"))
            self.assertFalse(config.event_dir.exists())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

from .config import Config
from .fs import write_json_atomic, write_json_atomic_create
from .lock import FileLock
from .parent_attention import (
    DELIVERY_STATES,
    SCHEMA_VERSION as PARENT_ATTENTION_SCHEMA_VERSION,
    WAKE_REASONS,
    outbox_dir as parent_attention_outbox_dir,
)
from .queue import RESOLUTIONS
from .retention import (
    CURSOR_UNCERTAINTY,
    ELIGIBLE_PAST_PROPOSAL_THRESHOLD,
    build_retention_inventory_report,
    validate_retention_inventory_report,
)
from .timeutil import parse_time, utc_now


RETENTION_COMPACTION_PLAN = "retention-compaction-plan-v1"
RETENTION_COMPACTION_BUNDLE = "retention-compaction-bundle-v1"
RETENTION_TRANSACTION = "retention-compaction-transaction-v1"
RESTORE_INDEX = "retention-restore-index-v1"
POLICY_REVISION = "retention-compact-v1"
REPORT_MAX_AGE_SECONDS = 300
REPORT_FUTURE_SKEW_SECONDS = 30
MAX_RECORD_BYTES = 1024 * 1024


class RetentionCompactionError(ValueError):
    """A sanitized, fail-closed retention compaction error."""


def build_retention_compaction_plan(
    config: Config,
    inventory_report_path: Path,
    task_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate an inventory snapshot and build a non-writing compact plan."""
    observed_now = now or utc_now()
    inventory = _load_inventory(inventory_report_path)
    item = _validated_inventory_item(inventory, task_id, observed_now)
    live_item = _live_item(config, inventory, task_id, item, observed_now)
    bundle = _build_bundle(item, inventory)
    operation_id = bundle["operation_id"]
    paths = _store_paths(config, operation_id)
    store_state = _inspect_store(paths, bundle)
    action = {
        "absent": "create",
        "bundle_only": "recover",
        "prepared": "recover",
        "committed": "noop",
    }[store_state]
    plan: dict[str, Any] = {
        "schema_version": RETENTION_COMPACTION_PLAN,
        "mode": "dry-run",
        "mutation": {
            "performed": False,
            "explicit_apply_required": True,
            "canonical_task_changed": False,
            "artifact_delete_or_move_supported": False,
        },
        "operation_id": operation_id,
        "policy_revision": POLICY_REVISION,
        "source_binding": {
            "source_task_ref": bundle["source"]["task_ref"],
            "source_task_digest": item["source_task_digest"],
            "inventory_report_digest": inventory["report_digest"],
            "inventory_preview_digest": item["compact_tombstone_preview"][
                "preview_digest"
            ],
            "inventory_scope_digest": _inventory_scope_digest(inventory),
            "cursor_scope_digest": inventory["cursor_safety"][
                "cursor_scope_digest"
            ],
            "proposal_age_days": inventory["proposal_parameters"]["age_days"],
            "project_scope": "all_projects",
            "live_source_digest": live_item["source_task_digest"],
            "cas_match": True,
            "live_eligible": True,
        },
        "action": action,
        "outputs": {
            "compact_record": True,
            "logical_tombstone": True,
            "restore_index_entry": True,
            "transaction_journal": True,
            "raw_artifact_content": False,
        },
        "restore_claims": bundle["restore_contract"],
    }
    plan["plan_digest"] = _digest(plan)
    validate_retention_compaction_plan(plan)
    return plan


def apply_retention_compaction(
    config: Config,
    inventory_report_path: Path,
    task_id: str,
    *,
    confirm_operation_id: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply an additive compact bundle and restore index under the queue lock."""
    lock = FileLock(config.lock_file, config.stale_lock_seconds)
    if not lock.acquire():
        raise RetentionCompactionError("active queue lock blocks retention apply")
    try:
        observed_now = now or utc_now()
        plan = build_retention_compaction_plan(
            config, inventory_report_path, task_id, now=observed_now
        )
        if confirm_operation_id != plan["operation_id"]:
            raise RetentionCompactionError(
                "exact --confirm-operation-id is required for retention apply"
            )

        inventory = _load_inventory(inventory_report_path)
        item = _validated_inventory_item(inventory, task_id, observed_now)
        # Rebuild once more while the queue lock is held immediately before writes.
        _live_item(config, inventory, task_id, item, observed_now)
        bundle = _build_bundle(item, inventory)
        paths = _store_paths(config, bundle["operation_id"])
        state = _inspect_store(paths, bundle)
        if state == "committed":
            return _apply_report(plan, performed=False, action="noop", recovered=False)

        _prepare_store(paths)
        transaction = _prepared_transaction(bundle, inventory)
        recovered = state in {"bundle_only", "prepared"}
        if paths["bundle"].exists():
            existing_bundle = _load_json_record(paths["bundle"], "compact bundle")
            validate_retention_compaction_bundle(existing_bundle)
            if existing_bundle != bundle:
                raise RetentionCompactionError("compact bundle digest conflict")
        else:
            write_json_atomic_create(paths["bundle"], bundle)

        if paths["transaction"].exists():
            transaction = _load_json_record(
                paths["transaction"], "transaction journal"
            )
            _validate_transaction(transaction, bundle)
        else:
            write_json_atomic_create(paths["transaction"], transaction)

        index = _load_restore_index(paths["index"])
        entry = _restore_index_entry(bundle)
        existing_entry = index["entries"].get(bundle["operation_id"])
        if existing_entry is not None and existing_entry != entry:
            raise RetentionCompactionError("restore index entry conflict")
        if existing_entry is None:
            index["entries"][bundle["operation_id"]] = entry
            index["updated_at"] = observed_now.isoformat()
            index["index_digest"] = _digest(
                {key: value for key, value in index.items() if key != "index_digest"}
            )
            write_json_atomic(paths["index"], index)

        committed = {
            **transaction,
            "state": "committed",
            "committed_at": observed_now.isoformat(),
            "recovered_from_partial": recovered,
        }
        write_json_atomic(paths["transaction"], committed)
        return _apply_report(
            plan,
            performed=True,
            action="recovered" if recovered else "created",
            recovered=recovered,
        )
    finally:
        lock.release()


def render_retention_compaction_report(report: dict[str, Any]) -> str:
    lines = [
        "Retention compact",
        f"mode: {report['mode']}",
        f"operation_id: {report['operation_id']}",
        f"action: {report['action']}",
        f"mutation_performed: {str(report['mutation']['performed']).lower()}",
        "canonical_task_changed: false",
        "artifact_delete_or_move_supported: false",
        f"digest: {report.get('result_digest') or report.get('plan_digest')}",
    ]
    return "\n".join(lines) + "\n"


def validate_retention_compaction_plan(plan: object) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise RetentionCompactionError("retention compact plan must be an object")
    expected_keys = {
        "schema_version",
        "mode",
        "mutation",
        "operation_id",
        "policy_revision",
        "source_binding",
        "action",
        "outputs",
        "restore_claims",
        "plan_digest",
    }
    if set(plan) != expected_keys or plan.get("schema_version") != RETENTION_COMPACTION_PLAN:
        raise RetentionCompactionError("retention compact plan is malformed")
    if plan.get("mode") != "dry-run" or plan.get("action") not in {
        "create",
        "recover",
        "noop",
    }:
        raise RetentionCompactionError("retention compact plan semantics are invalid")
    if plan.get("policy_revision") != POLICY_REVISION or not _is_operation_id(
        plan.get("operation_id")
    ):
        raise RetentionCompactionError("retention compact plan identity is invalid")
    if plan.get("mutation") != {
        "performed": False,
        "explicit_apply_required": True,
        "canonical_task_changed": False,
        "artifact_delete_or_move_supported": False,
    }:
        raise RetentionCompactionError("retention compact mutation boundary is invalid")
    if plan.get("outputs") != {
        "compact_record": True,
        "logical_tombstone": True,
        "restore_index_entry": True,
        "transaction_journal": True,
        "raw_artifact_content": False,
    }:
        raise RetentionCompactionError("retention compact output contract is invalid")
    binding = plan.get("source_binding")
    if (
        not isinstance(binding, dict)
        or set(binding)
        != {
            "source_task_ref",
            "source_task_digest",
            "inventory_report_digest",
            "inventory_preview_digest",
            "inventory_scope_digest",
            "cursor_scope_digest",
            "proposal_age_days",
            "project_scope",
            "live_source_digest",
            "cas_match",
            "live_eligible",
        }
        or any(
            not _is_digest(binding.get(key))
            for key in (
                "source_task_ref",
                "source_task_digest",
                "inventory_report_digest",
                "inventory_preview_digest",
                "inventory_scope_digest",
                "cursor_scope_digest",
                "live_source_digest",
            )
        )
        or not isinstance(binding.get("proposal_age_days"), int)
        or binding.get("project_scope") != "all_projects"
        or binding.get("cas_match") is not True
        or binding.get("live_eligible") is not True
    ):
        raise RetentionCompactionError("retention compact source binding is invalid")
    _validate_restore_contract(plan.get("restore_claims"))
    expected_digest = _digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    if plan.get("plan_digest") != expected_digest:
        raise RetentionCompactionError("retention compact plan digest mismatch")
    _validate_generated_record(plan)
    return plan


def _apply_report(
    plan: dict[str, Any], *, performed: bool, action: str, recovered: bool
) -> dict[str, Any]:
    report = {
        **{key: value for key, value in plan.items() if key != "plan_digest"},
        "mode": "apply",
        "mutation": {
            **plan["mutation"],
            "performed": performed,
        },
        "action": action,
        "recovered_from_partial": recovered,
    }
    report["result_digest"] = _digest(report)
    return report


def _load_inventory(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_RECORD_BYTES:
            raise RetentionCompactionError("inventory report exceeds size limit")
        value = json.loads(raw.decode("utf-8"))
        validated = validate_retention_inventory_report(value)
        _validate_inventory_contract(validated)
        return validated
    except RetentionCompactionError:
        raise
    except Exception as exc:
        raise RetentionCompactionError("inventory report is unreadable or malformed") from exc


def _validated_inventory_item(
    inventory: dict[str, Any], task_id: str, now: datetime
) -> dict[str, Any]:
    observed_at = parse_time(inventory.get("observed_at"))
    if observed_at is None:
        raise RetentionCompactionError("inventory report observed_at is invalid")
    if observed_at > now + timedelta(seconds=REPORT_FUTURE_SKEW_SECONDS):
        raise RetentionCompactionError("inventory report is from the future")
    if now - observed_at > timedelta(seconds=REPORT_MAX_AGE_SECONDS):
        raise RetentionCompactionError("inventory report is stale")
    cursor = inventory.get("cursor_safety")
    if (
        not isinstance(cursor, dict)
        or cursor.get("block_all_event_pruning") is not False
        or cursor.get("reason_codes")
    ):
        raise RetentionCompactionError("cursor safety is uncertain")
    params = inventory.get("proposal_parameters")
    if not isinstance(params, dict) or not isinstance(params.get("age_days"), int):
        raise RetentionCompactionError("explicit proposal age is required")
    if params.get("project_filter_applied") is not False:
        raise RetentionCompactionError("project-filtered inventory is not apply-authoritative")
    if any(item.get("status") == "invalid" for item in inventory["items"]):
        raise RetentionCompactionError("malformed unrelated task blocks compact apply")
    matches = [
        item
        for item in inventory["items"]
        if isinstance(item, dict) and item.get("task_id") == task_id
    ]
    if len(matches) != 1:
        raise RetentionCompactionError("inventory task binding is missing or ambiguous")
    item = matches[0]
    preview = item.get("compact_tombstone_preview")
    eligibility = item.get("eligibility")
    restore = item.get("restore_capability")
    if (
        not isinstance(preview, dict)
        or preview.get("candidate") is not True
        or preview.get("writes_performed") is not False
        or not isinstance(eligibility, dict)
        or eligibility.get("raw_log_prune_candidate") is not True
        or eligibility.get("canonical_task_json_protected") is not True
        or eligibility.get("reason_codes") != [ELIGIBLE_PAST_PROPOSAL_THRESHOLD]
        or not isinstance(restore, dict)
        or restore.get("deleted_artifact_reconstruction_claimed") is not False
        or not _is_digest(item.get("source_task_digest"))
    ):
        raise RetentionCompactionError("inventory task is not eligible for compact apply")
    return item


def _validate_inventory_contract(inventory: dict[str, Any]) -> None:
    if set(inventory) != {
        "schema_version",
        "mode",
        "mutation",
        "observed_at",
        "proposal_parameters",
        "cursor_safety",
        "summary",
        "items",
        "event_files",
        "report_digest",
    }:
        raise RetentionCompactionError("inventory report shape is not apply-authoritative")
    if inventory.get("mutation") != {
        "performed": False,
        "supported": False,
        "canonical_task_deletion_supported": False,
    }:
        raise RetentionCompactionError("inventory mutation boundary is invalid")
    params = inventory.get("proposal_parameters")
    if (
        not isinstance(params, dict)
        or set(params) != {"age_days", "age_is_policy", "project_filter_applied"}
        or params.get("age_is_policy") is not False
        or not isinstance(params.get("project_filter_applied"), bool)
        or (
            params.get("age_days") is not None
            and not isinstance(params.get("age_days"), int)
        )
    ):
        raise RetentionCompactionError("inventory proposal parameters are malformed")
    cursor = inventory.get("cursor_safety")
    if (
        not isinstance(cursor, dict)
        or set(cursor)
        != {
            "configured_cursor_count",
            "block_all_event_pruning",
            "cursor_scope_digest",
            "reason_codes",
        }
        or not isinstance(cursor.get("configured_cursor_count"), int)
        or not isinstance(cursor.get("block_all_event_pruning"), bool)
        or not _is_digest(cursor.get("cursor_scope_digest"))
        or not _string_list(cursor.get("reason_codes"))
    ):
        raise RetentionCompactionError("inventory cursor binding is malformed")
    expected_summary_keys = {
        "task_count",
        "event_file_count",
        "artifact_count",
        "lifecycle_class_counts",
        "raw_log_candidate_task_count",
        "event_candidate_count",
        "canonical_task_json_protected_count",
        "tombstone_candidate_count",
    }
    if not isinstance(inventory.get("summary"), dict) or set(
        inventory["summary"]
    ) != expected_summary_keys:
        raise RetentionCompactionError("inventory summary is malformed")
    seen_task_ids: set[str] = set()
    for item in inventory["items"]:
        _validate_inventory_task_item(item)
        task_id = item["task_id"]
        if task_id in seen_task_ids:
            raise RetentionCompactionError("inventory contains duplicate task identity")
        seen_task_ids.add(task_id)
    for event in inventory["event_files"]:
        if (
            not isinstance(event, dict)
            or set(event)
            != {
                "kind",
                "artifact_ref",
                "exists",
                "safe",
                "modified_at",
                "source_digest",
                "lifecycle_class",
                "prune_candidate",
                "reason_codes",
            }
            or event.get("kind") != "event_log"
            or not _is_digest(event.get("artifact_ref"))
            or not _is_digest(event.get("source_digest"))
            or parse_time(event.get("modified_at")) is None
            or not isinstance(event.get("prune_candidate"), bool)
            or not _string_list(event.get("reason_codes"))
        ):
            raise RetentionCompactionError("inventory event binding is malformed")


def _validate_inventory_task_item(item: object) -> None:
    if not isinstance(item, dict) or set(item) != {
        "task_id",
        "project_id",
        "status",
        "review_status",
        "resolution",
        "source_task_digest",
        "activity_timestamp",
        "activity_timestamp_source",
        "lifecycle_class",
        "eligibility",
        "compact_tombstone_preview",
        "restore_capability",
        "artifacts",
    }:
        raise RetentionCompactionError("inventory task binding is malformed")
    if (
        not isinstance(item.get("task_id"), str)
        or not _is_digest(item.get("source_task_digest"))
        or not isinstance(item.get("artifacts"), list)
        or not item["artifacts"]
    ):
        raise RetentionCompactionError("inventory task identity is malformed")
    eligibility = item.get("eligibility")
    if (
        not isinstance(eligibility, dict)
        or set(eligibility)
        != {
            "raw_log_prune_candidate",
            "canonical_task_json_protected",
            "reason_codes",
        }
        or not isinstance(eligibility.get("raw_log_prune_candidate"), bool)
        or eligibility.get("canonical_task_json_protected") is not True
        or not _string_list(eligibility.get("reason_codes"))
    ):
        raise RetentionCompactionError("inventory task eligibility is malformed")
    preview = item.get("compact_tombstone_preview")
    if (
        not isinstance(preview, dict)
        or set(preview)
        != {
            "candidate",
            "writes_performed",
            "projected_reason_code",
            "protected_source",
            "projected_fields",
            "blocker_reason_codes",
            "preview_digest",
        }
        or not isinstance(preview.get("candidate"), bool)
        or preview.get("writes_performed") is not False
        or preview.get("protected_source") != "canonical_task_json"
        or not _string_list(preview.get("projected_fields"))
        or not _string_list(preview.get("blocker_reason_codes"))
        or not _is_digest(preview.get("preview_digest"))
    ):
        raise RetentionCompactionError("inventory tombstone preview is malformed")
    restore = item.get("restore_capability")
    if (
        not isinstance(restore, dict)
        or set(restore)
        != {
            "canonical_task_state",
            "derived_index",
            "raw_transcript",
            "deleted_artifact_reconstruction_claimed",
        }
        or restore.get("deleted_artifact_reconstruction_claimed") is not False
    ):
        raise RetentionCompactionError("inventory restore preview is malformed")
    for artifact in item["artifacts"]:
        if (
            not isinstance(artifact, dict)
            or set(artifact)
            != {
                "kind",
                "artifact_ref",
                "exists",
                "safe",
                "lifecycle_class",
                "retention_status",
                "reason_codes",
            }
            or artifact.get("kind")
            not in {"canonical_task_json", "raw_execution_log"}
            or not _is_digest(artifact.get("artifact_ref"))
            or not isinstance(artifact.get("exists"), bool)
            or not isinstance(artifact.get("safe"), bool)
            or not _string_list(artifact.get("reason_codes"))
        ):
            raise RetentionCompactionError("inventory artifact binding is malformed")
    if item["artifacts"][0].get("kind") != "canonical_task_json":
        raise RetentionCompactionError("inventory canonical task artifact is missing")


def _inventory_scope_digest(inventory: dict[str, Any]) -> str:
    return _digest(
        {
            "proposal_parameters": inventory["proposal_parameters"],
            "cursor_safety": inventory["cursor_safety"],
            "summary": inventory["summary"],
            "items": inventory["items"],
            "event_files": inventory["event_files"],
        }
    )


def _live_item(
    config: Config,
    inventory: dict[str, Any],
    task_id: str,
    source_item: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    live = build_retention_inventory_report(
        config,
        proposal_age_days=inventory["proposal_parameters"]["age_days"],
        notifier_cursor_state_paths=None,
        now=now,
    )
    _validate_inventory_contract(live)
    if live["cursor_safety"]["reason_codes"]:
        raise RetentionCompactionError("live cursor safety is uncertain")
    matches = [item for item in live["items"] if item.get("task_id") == task_id]
    if len(matches) != 1:
        raise RetentionCompactionError("live task binding is missing or ambiguous")
    item = matches[0]
    if item["source_task_digest"] != source_item["source_task_digest"]:
        raise RetentionCompactionError("source task digest changed")
    if _inventory_scope_digest(live) != _inventory_scope_digest(inventory):
        raise RetentionCompactionError("task, event, cursor, or eligibility scope changed")
    tasks = _read_live_tasks(config)
    _validate_live_event_files(config)
    selected_tasks = [task for task in tasks if task.get("id") == task_id]
    if len(selected_tasks) != 1:
        raise RetentionCompactionError("live task source is missing or ambiguous")
    live_blockers = _live_apply_blockers(selected_tasks[0])
    _validate_parent_attention_state(config, task_id)
    if live_blockers:
        raise RetentionCompactionError(
            f"live task consistency rejected: {live_blockers[0]}"
        )
    if (
        item["compact_tombstone_preview"].get("candidate") is not True
        or item["eligibility"].get("reason_codes")
        != [ELIGIBLE_PAST_PROPOSAL_THRESHOLD]
    ):
        blockers = item["eligibility"].get("reason_codes") or []
        reason = blockers[0] if blockers else "ineligible"
        if reason == CURSOR_UNCERTAINTY:
            reason = "cursor_uncertainty"
        raise RetentionCompactionError(f"live task eligibility rejected: {reason}")
    return item


def _read_live_tasks(config: Config) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not config.queue_dir.is_dir():
        return tasks
    for path in sorted(config.queue_dir.glob("*.json")):
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_RECORD_BYTES:
                raise RetentionCompactionError("canonical task exceeds size limit")
            task = json.loads(raw.decode("utf-8"))
        except RetentionCompactionError:
            raise
        except Exception as exc:
            raise RetentionCompactionError(
                "malformed unrelated task blocks compact apply"
            ) from exc
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise RetentionCompactionError(
                "malformed unrelated task blocks compact apply"
            )
        if task["id"] in seen:
            raise RetentionCompactionError("duplicate live task identity blocks compact apply")
        seen.add(task["id"])
        tasks.append(task)
    return tasks


def _validate_live_event_files(config: Config) -> None:
    event_dir = config.event_dir.expanduser().resolve()
    if not event_dir.is_dir():
        return
    for path in sorted(event_dir.rglob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            raise RetentionCompactionError("unsafe event source blocks compact apply")
        try:
            with path.open("r", encoding="utf-8") as file:
                for line in file:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("event is not an object")
        except Exception as exc:
            raise RetentionCompactionError(
                "malformed unrelated event blocks compact apply"
            ) from exc


def _validate_parent_attention_state(config: Config, task_id: str) -> None:
    directory = parent_attention_outbox_dir(config).expanduser().resolve()
    if not directory.exists():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise RetentionCompactionError("parent attention outbox is unsafe")
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise RetentionCompactionError("parent attention record is unsafe")
        record = _load_json_record(path, "parent attention record")
        _validate_parent_attention_record(record, path.stem)
        if (
            record["work_item_ref"] == task_id
            and record["delivery"]["state"] != "acknowledged"
        ):
            raise RetentionCompactionError(
                "selected task has unresolved parent attention delivery"
            )


def _validate_parent_attention_record(
    record: dict[str, Any], expected_event_id: str
) -> None:
    if set(record) != {
        "schema_version",
        "event_type",
        "event_id",
        "parent_ref",
        "work_item_ref",
        "completion_id",
        "wake_reason",
        "result",
        "delivery",
        "created_at",
        "updated_at",
    }:
        raise RetentionCompactionError("parent attention record is malformed")
    delivery = record.get("delivery")
    if (
        record.get("schema_version") != PARENT_ATTENTION_SCHEMA_VERSION
        or record.get("event_type") != "parent_attention_required"
        or record.get("event_id") != expected_event_id
        or not isinstance(record.get("parent_ref"), str)
        or not isinstance(record.get("work_item_ref"), str)
        or not isinstance(record.get("completion_id"), str)
        or record.get("wake_reason") not in WAKE_REASONS
        or not isinstance(record.get("result"), dict)
        or parse_time(record.get("created_at")) is None
        or parse_time(record.get("updated_at")) is None
        or not isinstance(delivery, dict)
        or set(delivery)
        != {
            "state",
            "attempts",
            "max_attempts",
            "next_attempt_at",
            "last_attempt_at",
            "last_error",
            "delivered_at",
            "acknowledged_at",
        }
        or delivery.get("state") not in DELIVERY_STATES
        or isinstance(delivery.get("attempts"), bool)
        or not isinstance(delivery.get("attempts"), int)
        or delivery.get("attempts") < 0
        or isinstance(delivery.get("max_attempts"), bool)
        or not isinstance(delivery.get("max_attempts"), int)
        or delivery.get("max_attempts") < 1
    ):
        raise RetentionCompactionError("parent attention record is malformed")
    for key in (
        "next_attempt_at",
        "last_attempt_at",
        "delivered_at",
        "acknowledged_at",
    ):
        value = delivery.get(key)
        if value is not None and parse_time(value) is None:
            raise RetentionCompactionError("parent attention timestamp is malformed")
    if delivery.get("last_error") is not None and not isinstance(
        delivery.get("last_error"), str
    ):
        raise RetentionCompactionError("parent attention error state is malformed")
    if delivery["state"] == "acknowledged" and parse_time(
        delivery.get("acknowledged_at")
    ) is None:
        raise RetentionCompactionError("parent attention acknowledgement is malformed")


def _live_apply_blockers(task: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    status = task.get("status")
    review = task.get("review_status")
    resolution = task.get("resolution")
    if status == "completed":
        if review != "accepted":
            blockers.append("review_not_accepted")
    elif status == "archived":
        gate = task.get("archive_gate_result")
        blockers.extend(_archive_gate_blockers(gate))
        previous = task.get("previous_status")
        if previous == "completed":
            if review not in {"accepted", "rejected"}:
                blockers.append("archived_review_unresolved")
        elif previous in {"failed", "blocked_user"}:
            if resolution not in RESOLUTIONS:
                blockers.append("archived_failure_unresolved")
        else:
            blockers.append("archived_source_status_uncertain")
    else:
        blockers.append("active_or_nonterminal_status")

    if task.get("active_run_id"):
        blockers.append("active_run_present")
    if task.get("recovery_required") or task.get("execution_worktree_status") == "recovery_required":
        blockers.append("recovery_required")
    chain_status = task.get("chain_status")
    if chain_status in {
        "awaiting_review",
        "reviewing",
        "needs_fix",
        "fixing",
        "needs_human",
        "loop_limit_reached",
    }:
        blockers.append("review_or_fix_chain_active")
    elif chain_status not in {None, "accepted"}:
        blockers.append("chain_status_unknown")
    execution_mode = task.get("execution_mode")
    if execution_mode not in {None, "main_worktree", "git_worktree"}:
        blockers.append("execution_mode_unknown")
    if execution_mode == "git_worktree":
        if task.get("execution_worktree_status") != "cleaned":
            blockers.append("worktree_not_cleaned")
        if task.get("execution_worktree_pool") and task.get(
            "execution_worktree_lease_status"
        ) != "released":
            blockers.append("pool_lease_not_released")
        if review == "accepted":
            cleanup_kind = task.get("execution_cleanup_kind")
            if cleanup_kind not in {"applied", "no_change"}:
                blockers.append("accepted_worktree_cleanup_unresolved")
            if (
                cleanup_kind != "no_change"
                and task.get("execution_apply_status") != "applied"
            ):
                blockers.append("accepted_worktree_unapplied")
    return sorted(set(blockers))


def _archive_gate_blockers(gate: object) -> list[str]:
    if not isinstance(gate, dict) or set(gate) != {
        "status",
        "checked_at",
        "blockers",
        "warnings",
    }:
        return ["archive_gate_malformed"]
    if (
        gate.get("status") != "passed"
        or parse_time(gate.get("checked_at")) is None
        or gate.get("blockers") != []
        or not _string_list(gate.get("warnings"))
    ):
        return ["archive_terminal_consistency_unverified"]
    return []


def _build_bundle(
    item: dict[str, Any], inventory: dict[str, Any]
) -> dict[str, Any]:
    task_ref = item["artifacts"][0]["artifact_ref"]
    approval_identity = {
        "inventory_report_digest": inventory["report_digest"],
        "inventory_preview_digest": item["compact_tombstone_preview"][
            "preview_digest"
        ],
        "inventory_scope_digest": _inventory_scope_digest(inventory),
        "cursor_scope_digest": inventory["cursor_safety"][
            "cursor_scope_digest"
        ],
        "proposal_age_days": inventory["proposal_parameters"]["age_days"],
    }
    identity = {
        "policy_revision": POLICY_REVISION,
        "source_task_ref": task_ref,
        "source_task_digest": item["source_task_digest"],
        **approval_identity,
    }
    operation_id = "retention-" + _digest(identity).split(":", 1)[1][:32]
    status = item.get("status")
    review_status = item.get("review_status")
    resolution = item.get("resolution")
    if status not in {"completed", "archived"}:
        raise RetentionCompactionError("unsupported terminal status for compact bundle")
    if review_status not in {None, "accepted", "rejected"}:
        raise RetentionCompactionError("unsupported review disposition for compact bundle")
    if resolution not in {None, *RESOLUTIONS}:
        raise RetentionCompactionError("unsupported resolution for compact bundle")
    if status == "completed" and review_status != "accepted":
        raise RetentionCompactionError("completed compact source must be accepted")
    body: dict[str, Any] = {
        "schema_version": RETENTION_COMPACTION_BUNDLE,
        "operation_id": operation_id,
        "policy_revision": POLICY_REVISION,
        "approval_binding": {
            **approval_identity,
            "project_scope": "all_projects",
        },
        "source": {
            "task_ref": task_ref,
            "task_digest": item["source_task_digest"],
            "project_ref": _digest({"project": item.get("project_id")}),
            "status": status,
            "review_status": review_status,
            "resolution": resolution,
            "canonical_task_preserved": True,
        },
        "compact_record": {
            "retention_reason_code": ELIGIBLE_PAST_PROPOSAL_THRESHOLD,
            "artifact_content_stored": False,
            "raw_execution_material_stored": False,
            "canonical_source_changed": False,
        },
        "tombstone": {
            "kind": "logical_compaction_marker",
            "source_preserved": True,
            "artifact_deletion_performed": False,
            "artifact_move_performed": False,
            "raw_artifact_state": "unchanged",
        },
        "restore_contract": {
            "index_locator_available": True,
            "restore_action_supported": False,
            "canonical_task_json": "retained_source_of_truth",
            "compact_record": "readable",
            "raw_execution_log": "unsupported",
            "raw_transcript": "unsupported",
            "deleted_artifact_reconstruction_claimed": False,
        },
    }
    body["bundle_digest"] = _digest(body)
    validate_retention_compaction_bundle(body)
    _validate_generated_record(body)
    return body


def validate_retention_compaction_bundle(bundle: object) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise RetentionCompactionError("compact bundle must be an object")
    if set(bundle) != {
        "schema_version",
        "operation_id",
        "policy_revision",
        "approval_binding",
        "source",
        "compact_record",
        "tombstone",
        "restore_contract",
        "bundle_digest",
    }:
        raise RetentionCompactionError("compact bundle is malformed")
    if (
        bundle.get("schema_version") != RETENTION_COMPACTION_BUNDLE
        or bundle.get("policy_revision") != POLICY_REVISION
        or not _is_operation_id(bundle.get("operation_id"))
    ):
        raise RetentionCompactionError("compact bundle identity is invalid")
    approval = bundle.get("approval_binding")
    if (
        not isinstance(approval, dict)
        or set(approval)
        != {
            "inventory_report_digest",
            "inventory_preview_digest",
            "inventory_scope_digest",
            "cursor_scope_digest",
            "proposal_age_days",
            "project_scope",
        }
        or any(
            not _is_digest(approval.get(key))
            for key in (
                "inventory_report_digest",
                "inventory_preview_digest",
                "inventory_scope_digest",
                "cursor_scope_digest",
            )
        )
        or not isinstance(approval.get("proposal_age_days"), int)
        or approval.get("project_scope") != "all_projects"
    ):
        raise RetentionCompactionError("compact bundle approval binding is invalid")
    source = bundle.get("source")
    if (
        not isinstance(source, dict)
        or set(source)
        != {
            "task_ref",
            "task_digest",
            "project_ref",
            "status",
            "review_status",
            "resolution",
            "canonical_task_preserved",
        }
        or any(
            not _is_digest(source.get(key))
            for key in ("task_ref", "task_digest", "project_ref")
        )
        or source.get("status") not in {"completed", "archived"}
        or source.get("review_status") not in {None, "accepted", "rejected"}
        or source.get("resolution") not in {None, *RESOLUTIONS}
        or source.get("canonical_task_preserved") is not True
        or (
            source.get("status") == "completed"
            and source.get("review_status") != "accepted"
        )
    ):
        raise RetentionCompactionError("compact bundle source semantics are invalid")
    if bundle.get("compact_record") != {
        "retention_reason_code": ELIGIBLE_PAST_PROPOSAL_THRESHOLD,
        "artifact_content_stored": False,
        "raw_execution_material_stored": False,
        "canonical_source_changed": False,
    }:
        raise RetentionCompactionError("compact record semantics are invalid")
    if bundle.get("tombstone") != {
        "kind": "logical_compaction_marker",
        "source_preserved": True,
        "artifact_deletion_performed": False,
        "artifact_move_performed": False,
        "raw_artifact_state": "unchanged",
    }:
        raise RetentionCompactionError("logical tombstone semantics are invalid")
    _validate_restore_contract(bundle.get("restore_contract"))
    expected = _digest(
        {key: value for key, value in bundle.items() if key != "bundle_digest"}
    )
    if bundle.get("bundle_digest") != expected:
        raise RetentionCompactionError("compact bundle digest mismatch")
    _validate_generated_record(bundle)
    return bundle


def _validate_restore_contract(value: object) -> None:
    if value != {
        "index_locator_available": True,
        "restore_action_supported": False,
        "canonical_task_json": "retained_source_of_truth",
        "compact_record": "readable",
        "raw_execution_log": "unsupported",
        "raw_transcript": "unsupported",
        "deleted_artifact_reconstruction_claimed": False,
    }:
        raise RetentionCompactionError("restore contract overclaims capability")


def _store_paths(config: Config, operation_id: str) -> dict[str, Path]:
    queue_dir = config.queue_dir.expanduser().resolve()
    log_dir = config.log_dir.expanduser().resolve()
    event_dir = config.event_dir.expanduser().resolve()
    root = queue_dir.parent / "retention"
    if root in {queue_dir, log_dir, event_dir} or any(
        _is_relative_to(root, artifact_root)
        or _is_relative_to(artifact_root, root)
        for artifact_root in (queue_dir, log_dir, event_dir)
    ):
        raise RetentionCompactionError(
            "retention store must be outside queue, log, and event directories"
        )
    return {
        "root": root,
        "bundles": root / "bundles",
        "transactions": root / "transactions",
        "bundle": root / "bundles" / f"{operation_id}.json",
        "transaction": root / "transactions" / f"{operation_id}.json",
        "index": root / "restore-index-v1.json",
    }


def _inspect_store(paths: dict[str, Path], bundle: dict[str, Any]) -> str:
    _validate_store_locations(paths)
    transaction_exists = paths["transaction"].exists()
    bundle_exists = paths["bundle"].exists()
    index_exists = paths["index"].exists()
    index = _load_restore_index(paths["index"])
    _validate_restore_index_references(paths, index, bundle["operation_id"])
    entry = index["entries"].get(bundle["operation_id"])
    expected_entry = _restore_index_entry(bundle)

    if not transaction_exists:
        if entry is not None:
            raise RetentionCompactionError("restore index entry lacks transaction journal")
        if bundle_exists:
            existing_bundle = _load_json_record(paths["bundle"], "compact bundle")
            validate_retention_compaction_bundle(existing_bundle)
            if existing_bundle != bundle:
                raise RetentionCompactionError("orphaned compact bundle digest conflict")
            return "bundle_only"
        return "absent"

    transaction = _load_json_record(paths["transaction"], "transaction journal")
    _validate_transaction(transaction, bundle)
    if not bundle_exists:
        raise RetentionCompactionError("transaction journal lacks durable compact bundle")
    if transaction["state"] == "committed":
        if entry != expected_entry:
            raise RetentionCompactionError("committed retention transaction is inconsistent")
        existing_bundle = _load_json_record(paths["bundle"], "compact bundle")
        validate_retention_compaction_bundle(existing_bundle)
        if existing_bundle != bundle:
            raise RetentionCompactionError("committed compact bundle digest conflict")
        return "committed"

    if entry is not None and not bundle_exists:
        raise RetentionCompactionError("partial restore index lacks compact bundle")
    if bundle_exists:
        existing_bundle = _load_json_record(paths["bundle"], "compact bundle")
        validate_retention_compaction_bundle(existing_bundle)
        if existing_bundle != bundle:
            raise RetentionCompactionError("partial compact bundle digest conflict")
    if entry is not None and entry != expected_entry:
        raise RetentionCompactionError("partial restore index entry conflict")
    if index_exists and entry is None and not bundle_exists:
        # An index for other operations is not evidence of this operation.
        return "prepared"
    return "prepared"


def _validate_restore_index_references(
    paths: dict[str, Path],
    index: dict[str, Any],
    current_operation_id: str,
) -> None:
    bundle_files = _retention_operation_files(paths["bundles"], "bundle")
    transaction_files = _retention_operation_files(
        paths["transactions"], "transaction"
    )
    indexed_operations = set(index["entries"])
    observed_operations = set(bundle_files) | set(transaction_files)

    for operation_id in sorted(indexed_operations | observed_operations):
        bundle_path = bundle_files.get(operation_id)
        transaction_path = transaction_files.get(operation_id)
        entry = index["entries"].get(operation_id)
        if operation_id in indexed_operations and (
            bundle_path is None or transaction_path is None
        ):
            raise RetentionCompactionError(
                "restore index references missing or unsafe retention records"
            )
        if operation_id not in indexed_operations:
            if operation_id != current_operation_id:
                raise RetentionCompactionError(
                    "foreign unindexed retention records require recovery"
                )
            if bundle_path is None:
                raise RetentionCompactionError(
                    "current retention transaction lacks durable bundle"
                )

        if bundle_path is None:
            raise RetentionCompactionError("retention bundle is missing")
        existing_bundle = _load_json_record(bundle_path, "indexed compact bundle")
        validate_retention_compaction_bundle(existing_bundle)
        if existing_bundle.get("operation_id") != operation_id:
            raise RetentionCompactionError("restore index bundle identity mismatch")
        if entry is not None and entry != _restore_index_entry(existing_bundle):
            raise RetentionCompactionError("restore index bundle binding mismatch")
        if transaction_path is None:
            # Bundle-first publication is the only valid single-file partial state,
            # and only the matching current operation may recover it.
            if operation_id != current_operation_id or entry is not None:
                raise RetentionCompactionError(
                    "retention bundle lacks matching transaction journal"
                )
            continue
        transaction = _load_json_record(
            transaction_path, "indexed transaction journal"
        )
        _validate_transaction(transaction, existing_bundle)
        if entry is None and transaction["state"] != "prepared":
            raise RetentionCompactionError(
                "unindexed committed retention transaction is inconsistent"
            )
        if transaction["state"] == "prepared" and operation_id != current_operation_id:
            raise RetentionCompactionError(
                "foreign prepared retention transaction requires recovery"
            )


def _retention_operation_files(directory: Path, label: str) -> dict[str, Path]:
    if not directory.exists():
        return {}
    if directory.is_symlink() or not directory.is_dir():
        raise RetentionCompactionError(f"retention {label} directory is unsafe")
    records: dict[str, Path] = {}
    for path in sorted(directory.iterdir()):
        operation_id = path.stem
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix != ".json"
            or path.name != f"{operation_id}.json"
            or not _is_operation_id(operation_id)
            or operation_id in records
        ):
            raise RetentionCompactionError(
                f"retention {label} directory contains an unknown record"
            )
        records[operation_id] = path
    return records


def _prepare_store(paths: dict[str, Path]) -> None:
    _validate_store_locations(paths)
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["bundles"].mkdir(exist_ok=True)
    paths["transactions"].mkdir(exist_ok=True)
    _validate_store_locations(paths)


def _validate_store_locations(paths: dict[str, Path]) -> None:
    for key in ("root", "bundles", "transactions"):
        path = paths[key]
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise RetentionCompactionError("retention store location is unsafe")
    for key in ("bundle", "transaction", "index"):
        path = paths[key]
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RetentionCompactionError("retention record location is unsafe")


def _prepared_transaction(
    bundle: dict[str, Any], inventory: dict[str, Any]
) -> dict[str, Any]:
    transaction = {
        "schema_version": RETENTION_TRANSACTION,
        "operation_id": bundle["operation_id"],
        "state": "prepared",
        "prepared_at": inventory["observed_at"],
        "committed_at": None,
        "recovered_from_partial": False,
        "bundle_digest": bundle["bundle_digest"],
        "restore_index_entry_digest": _restore_index_entry(bundle)["entry_digest"],
        "source_task_digest": bundle["source"]["task_digest"],
    }
    _validate_generated_record(transaction)
    return transaction


def _validate_transaction(
    transaction: dict[str, Any], bundle: dict[str, Any]
) -> None:
    expected_keys = {
        "schema_version",
        "operation_id",
        "state",
        "prepared_at",
        "committed_at",
        "recovered_from_partial",
        "bundle_digest",
        "restore_index_entry_digest",
        "source_task_digest",
    }
    if (
        set(transaction) != expected_keys
        or transaction.get("schema_version") != RETENTION_TRANSACTION
        or transaction.get("operation_id") != bundle["operation_id"]
        or transaction.get("state") not in {"prepared", "committed"}
        or transaction.get("bundle_digest") != bundle["bundle_digest"]
        or transaction.get("restore_index_entry_digest")
        != _restore_index_entry(bundle)["entry_digest"]
        or transaction.get("source_task_digest") != bundle["source"]["task_digest"]
        or not _is_digest(transaction.get("bundle_digest"))
        or not _is_digest(transaction.get("restore_index_entry_digest"))
        or not _is_digest(transaction.get("source_task_digest"))
        or parse_time(transaction.get("prepared_at")) is None
        or not isinstance(transaction.get("recovered_from_partial"), bool)
    ):
        raise RetentionCompactionError("transaction journal is malformed or mismatched")
    if transaction["state"] == "committed":
        if parse_time(transaction.get("committed_at")) is None:
            raise RetentionCompactionError("committed transaction lacks timestamp")
    elif transaction.get("committed_at") is not None:
        raise RetentionCompactionError("prepared transaction has invalid commit state")
    _validate_generated_record(transaction)


def _restore_index_entry(bundle: dict[str, Any]) -> dict[str, Any]:
    body = {
        "operation_id": bundle["operation_id"],
        "bundle_ref": _digest({"bundle": bundle["operation_id"]}),
        "bundle_digest": bundle["bundle_digest"],
        "source_task_ref": bundle["source"]["task_ref"],
        "source_task_digest": bundle["source"]["task_digest"],
        "restore_action_supported": False,
        "raw_artifact_restore_supported": False,
        "canonical_task_json": "retained_source_of_truth",
    }
    return {**body, "entry_digest": _digest(body)}


def _load_restore_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        index = {
            "schema_version": RESTORE_INDEX,
            "updated_at": None,
            "entries": {},
        }
        index["index_digest"] = _digest(index)
        return index
    index = _load_json_record(path, "restore index")
    if set(index) != {"schema_version", "updated_at", "entries", "index_digest"}:
        raise RetentionCompactionError("restore index is malformed")
    if index.get("schema_version") != RESTORE_INDEX or not isinstance(
        index.get("entries"), dict
    ):
        raise RetentionCompactionError("restore index is malformed")
    expected = _digest(
        {key: value for key, value in index.items() if key != "index_digest"}
    )
    if index.get("index_digest") != expected:
        raise RetentionCompactionError("restore index digest mismatch")
    if index.get("updated_at") is not None and parse_time(index.get("updated_at")) is None:
        raise RetentionCompactionError("restore index timestamp is invalid")
    for operation_id, entry in index["entries"].items():
        if not isinstance(operation_id, str) or not isinstance(entry, dict):
            raise RetentionCompactionError("restore index entry is malformed")
        expected_entry_keys = set(_restore_index_entry_shape())
        if set(entry) != expected_entry_keys or entry.get("operation_id") != operation_id:
            raise RetentionCompactionError("restore index entry is malformed")
        if (
            not _is_operation_id(operation_id)
            or any(
                not _is_digest(entry.get(key))
                for key in (
                    "bundle_ref",
                    "bundle_digest",
                    "source_task_ref",
                    "source_task_digest",
                    "entry_digest",
                )
            )
        ):
            raise RetentionCompactionError("restore index entry identity is malformed")
        entry_body = {key: value for key, value in entry.items() if key != "entry_digest"}
        if entry.get("entry_digest") != _digest(entry_body):
            raise RetentionCompactionError("restore index entry digest mismatch")
        if (
            entry.get("restore_action_supported") is not False
            or entry.get("raw_artifact_restore_supported") is not False
            or entry.get("canonical_task_json") != "retained_source_of_truth"
        ):
            raise RetentionCompactionError("restore index overclaims restore capability")
        _validate_generated_record(entry)
    return index


def _restore_index_entry_shape() -> dict[str, object]:
    return {
        "operation_id": None,
        "bundle_ref": None,
        "bundle_digest": None,
        "source_task_ref": None,
        "source_task_digest": None,
        "restore_action_supported": None,
        "raw_artifact_restore_supported": None,
        "canonical_task_json": None,
        "entry_digest": None,
    }


def _load_json_record(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_RECORD_BYTES:
            raise RetentionCompactionError(f"{label} exceeds size limit")
        value = json.loads(raw.decode("utf-8"))
    except RetentionCompactionError:
        raise
    except Exception as exc:
        raise RetentionCompactionError(f"{label} is unreadable or malformed") from exc
    if not isinstance(value, dict):
        raise RetentionCompactionError(f"{label} must be an object")
    return value


def _validate_generated_record(value: object) -> None:
    forbidden_keys = {
        "prompt",
        "transcript",
        "stdout",
        "stderr",
        "path",
        "session_id",
        "thread_id",
        "account_id",
        "credential",
        "credentials",
        "token",
        "secret",
    }

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in forbidden_keys:
                    raise RetentionCompactionError("retention record contains a forbidden field")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and item.startswith(("/", "file://")):
            raise RetentionCompactionError("retention record contains a private path")

    visit(value)


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    body = value[7:]
    return len(body) == 64 and all(char in "0123456789abcdef" for char in body)


def _is_operation_id(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("retention-"):
        return False
    body = value[len("retention-") :]
    return len(body) == 32 and all(char in "0123456789abcdef" for char in body)


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()

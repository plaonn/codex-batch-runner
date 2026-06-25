from __future__ import annotations

from typing import Any

from .queue import task_title

# Mirrors the existing review_follow_up metadata name while keeping the child-to-source
# relationship separate from depends_on and subtask_for.
REVIEW_FOLLOWUP_FOR_FIELD = "review_followup_for"


def build_review_follow_up_action(task: dict[str, Any], by_id: dict[str, dict] | None = None) -> dict[str, Any]:
    if task.get("status") != "completed" or (task.get("review_status") or "unreviewed") != "needs_followup":
        return {}

    task_id = str(task.get("id") or "")
    linked_ids = review_follow_up_task_ids(task, by_id)
    linked = linked_follow_up_tasks(linked_ids, by_id)
    resolution_command = (
        f"cbr resolve {task_id} --resolution manual --reason \"handled outside cbr\""
        if task_id
        else "cbr resolve TASK_ID --resolution manual --reason \"handled outside cbr\""
    )

    if task.get("resolution"):
        return {
            "state": "resolved",
            "next_action": "resolved follow-up item is no longer active",
            "note": "follow-up resolved",
            "resolution_command": None,
            "linked_task_ids": linked_ids,
            "linked_tasks": linked,
        }

    if not linked:
        return {
            "state": "unlinked",
            "next_action": (
                "create or link follow-up work; if no follow-up remains, resolve this task as "
                "manual, superseded, wont_fix, or duplicate"
            ),
            "note": "needs follow-up: create/link fix or resolve",
            "resolution_command": resolution_command,
            "linked_task_ids": [],
            "linked_tasks": [],
        }

    missing = [item for item in linked if item["state"] == "missing"]
    if missing:
        return linked_action(
            "missing",
            "restore or relink the missing follow-up task; otherwise resolve this task intentionally",
            "follow-up link missing; relink or resolve",
            resolution_command,
            linked_ids,
            linked,
        )

    active = [item for item in linked if item["state"] == "active"]
    if active:
        return linked_action(
            "active",
            "run or monitor the linked follow-up task before resolving the original",
            f"follow-up {linked_task_label(active)} active",
            resolution_command,
            linked_ids,
            linked,
        )

    review_needed = [item for item in linked if item["state"] == "review_needed"]
    if review_needed:
        return linked_action(
            "review_needed",
            "review the linked follow-up task, then accept it or request another fix",
            f"review follow-up {linked_task_label(review_needed)}",
            resolution_command,
            linked_ids,
            linked,
        )

    blocked = [item for item in linked if item["state"] == "blocked"]
    if blocked:
        return linked_action(
            "blocked",
            "repair the linked follow-up task or resolve the original with an explicit resolution",
            f"follow-up {linked_task_label(blocked)} blocked; fix or resolve",
            resolution_command,
            linked_ids,
            linked,
        )

    return linked_action(
        "accepted",
        "verify the accepted follow-up covers the original findings, then resolve the original as superseded or manual",
        f"follow-up {linked_task_label(linked)} accepted; resolve original",
        (
            f"cbr resolve {task_id} --resolution superseded --reason \"handled by follow-up task\""
            if task_id
            else "cbr resolve TASK_ID --resolution superseded --reason \"handled by follow-up task\""
        ),
        linked_ids,
        linked,
    )


def linked_action(
    state: str,
    next_action: str,
    note: str,
    resolution_command: str | None,
    linked_ids: list[str],
    linked: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "state": state,
        "next_action": next_action,
        "note": note,
        "resolution_command": resolution_command,
        "linked_task_ids": linked_ids,
        "linked_tasks": linked,
    }


def review_follow_up_note(task: dict[str, Any], by_id: dict[str, dict] | None = None) -> str:
    action = build_review_follow_up_action(task, by_id)
    return str(action.get("note") or "")


def review_follow_up_task_ids(task: dict[str, Any], by_id: dict[str, dict] | None = None) -> list[str]:
    task_id = str(task.get("id") or "")
    ids: list[str] = []

    for value in task.get("blocking_subtask_ids") if isinstance(task.get("blocking_subtask_ids"), list) else []:
        append_unique(ids, value)
    append_unique(ids, task.get("last_auto_fix_task_id"))

    follow_up = task.get("review_follow_up") if isinstance(task.get("review_follow_up"), dict) else {}
    for key in ("task_id", "follow_up_task_id", "generated_task_id", "linked_task_id"):
        append_unique(ids, follow_up.get(key))

    if by_id:
        for candidate in by_id.values():
            candidate_id = str(candidate.get("id") or "")
            if not candidate_id or candidate_id == task_id:
                continue
            if str(candidate.get(REVIEW_FOLLOWUP_FOR_FIELD) or "") == task_id:
                append_unique(ids, candidate_id)
            if str(candidate.get("subtask_for") or "") == task_id or str(candidate.get("parent_task_id") or "") == task_id:
                append_unique(ids, candidate_id)

    return ids


def append_unique(values: list[str], value: object) -> None:
    text = str(value or "")
    if text and text not in values:
        values.append(text)


def linked_follow_up_tasks(linked_ids: list[str], by_id: dict[str, dict] | None) -> list[dict[str, Any]]:
    linked = []
    for task_id in linked_ids:
        task = by_id.get(task_id) if by_id else None
        linked.append(describe_linked_follow_up(task_id, task))
    return linked


def describe_linked_follow_up(task_id: str, task: dict | None) -> dict[str, Any]:
    if task is None:
        return {"id": task_id, "state": "missing", "status": "missing", "review_status": None, "title": None}

    status = str(task.get("status") or "")
    review = str(task.get("review_status") or ("unreviewed" if status == "completed" else ""))
    return {
        "id": task_id,
        "state": linked_follow_up_state(status, review),
        "status": status,
        "review_status": review or None,
        "title": task_title(task),
    }


def linked_follow_up_state(status: str, review: str) -> str:
    if status == "completed":
        if review == "accepted":
            return "accepted"
        if review in {"unreviewed", "reviewing"}:
            return "review_needed"
        return "blocked"
    if status in {"failed", "blocked_user", "archived"}:
        return "blocked"
    return "active"


def linked_task_label(linked: list[dict[str, Any]]) -> str:
    if not linked:
        return "-"
    task_id = str(linked[0].get("id") or "-")
    if len(linked) == 1:
        return task_id
    return f"{task_id}+{len(linked) - 1}"

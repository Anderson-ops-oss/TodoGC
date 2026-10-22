"""Shared, read-only task queries for the CLI and web UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from todogc.heap import State, Task
from todogc.i18n import t as _t

if TYPE_CHECKING:
    from todogc.store import Store

SCOPES = ("active", "inbox", "snoozed", "done", "collected", "all")
SORTS = ("id", "newest", "oldest", "title", "updated")


@dataclass(frozen=True)
class TaskQuery:
    text: str = ""
    scope: str = "active"
    root_id: int | None = None
    sort: str = "id"
    notes: bool = False
    limit: int | None = None
    state: State | None = None

    def __post_init__(self) -> None:
        if self.scope not in SCOPES or self.sort not in SORTS:
            raise ValueError(_t("query.error.options"))
        if self.limit is not None and self.limit < 1:
            raise ValueError(_t("query.error.limit"))


def query_tasks(store: Store, query: TaskQuery, *, now: datetime | None = None,
                candidate_ids: set[int] | None = None) -> list[Task]:
    """Match a literal, case-insensitive substring; apply every filter before limiting.

    Subtrees include the selected node and all containment descendants, in any state.
    Reason/dependency edges do not affect the subtree. No reads record views.
    """
    now = now or datetime.now(timezone.utc)
    tasks = store.list_tasks(record_view=False)
    subtree: set[int] | None = None
    if query.root_id is not None:
        if not any(task.id == query.root_id for task in tasks):
            raise ValueError(_t("store.error.task_not_found", id=query.root_id))
        subtree = {query.root_id, *(task.persisted_id for task in store.descendants(query.root_id))}
    needle = query.text.strip().casefold()
    notes = store.task_notes() if needle and query.notes else {}

    def matches(task: Task) -> bool:
        if subtree is not None and task.id not in subtree:
            return False
        if candidate_ids is not None:
            if task.id not in candidate_ids:
                return False
        elif query.state is not None and task.state is not query.state:
            return False
        if query.scope == "active" and (not task.state.is_active or task.in_inbox or task.is_snoozed(now)):
            return False
        if query.scope == "inbox" and (not task.state.is_active or not task.in_inbox or task.is_snoozed(now)):
            return False
        if query.scope == "snoozed" and not task.is_snoozed(now):
            return False
        if query.scope in {"done", "collected"} and task.state.value != query.scope:
            return False
        return not needle or needle in task.title.casefold() or any(
            needle in note.casefold() for note in notes.get(task.persisted_id, [])
        )

    result = [task for task in tasks if matches(task)]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    if query.sort == "title":
        result.sort(key=lambda task: (task.title.casefold(), task.persisted_id))
    elif query.sort in {"newest", "oldest"}:
        result.sort(key=lambda task: (task.created_at or epoch, task.persisted_id), reverse=query.sort == "newest")
    elif query.sort == "updated":
        activity = store.last_activity_times()
        result.sort(key=lambda task: (activity.get(task.persisted_id, task.created_at or epoch), task.persisted_id), reverse=True)
    else:
        result.sort(key=lambda task: task.persisted_id)
    return result[:query.limit] if query.limit is not None else result

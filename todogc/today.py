"""A local-day plan, kept separate from task lifecycle and GC reachability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from todogc.heap import State, Task

if TYPE_CHECKING:
    from todogc.store import Store


@dataclass(frozen=True)
class TodayPlan:
    day: date
    selected: list[Task]
    carryover: list[Task]
    waking: list[Task]
    completed: list[Task]


def build_today(store: Store, *, now: datetime | None = None) -> TodayPlan:
    now = now or datetime.now(timezone.utc)
    day = now.astimezone().date()
    tasks = {task.persisted_id: task for task in store.list_tasks(record_view=False)}
    selected = [tasks[tid] for tid in store.focus_task_ids(day) if tid in tasks]
    handled = store.handled_focus_ids(day)

    def available(task: Task) -> bool:
        return task.state.is_active and not task.in_inbox and not task.is_snoozed(now)

    carryover = [tasks[tid] for tid in store.previous_focus_ids(day) if tid in tasks and available(tasks[tid])]
    carried = {task.id for task in carryover}
    waking = [task for task in tasks.values() if available(task) and task.id not in handled | carried
              and task.snoozed_until is not None and task.snoozed_until <= now
              and task.snoozed_until.astimezone().date() == day]
    waking.sort(key=lambda task: (task.snoozed_until, task.persisted_id))
    completed = [task for task in tasks.values() if task.state is State.DONE and task.completed_at is not None
                 and task.completed_at.astimezone().date() == day]
    completed.sort(key=lambda task: (task.completed_at, task.persisted_id), reverse=True)
    return TodayPlan(day, selected, carryover, waking, completed)

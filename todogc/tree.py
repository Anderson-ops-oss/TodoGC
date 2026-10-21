"""Read-only parent/child tree model shared by the Web UI and its tests."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

from todogc.heap import State, Task
from todogc.i18n import t as _t


@dataclass
class TaskTree:
    tasks: dict[int, Task]
    parents: dict[int, int | None]
    children: dict[int, list[int]]
    groups: dict[str, list[int]]
    repaired_cycles: bool = False

    def path(self, task_id: int) -> list[Task]:
        """Return ancestors and the task itself, with a finite path even for legacy data."""
        path: list[Task] = []
        current: int | None = task_id
        while current is not None:
            path.append(self.tasks[current])
            current = self.parents[current]
        return list(reversed(path))

    def rows(self, group: str, collapsed: set[int]) -> Iterator[tuple[Task, int, bool]]:
        """Yield visible nodes in preorder without depending on Python's recursion limit."""
        stack = [(tid, 0) for tid in reversed(self.groups[group])]
        while stack:
            tid, depth = stack.pop()
            children = self.children.get(tid, [])
            yield self.tasks[tid], depth, bool(children)
            if tid not in collapsed:
                stack.extend((child, depth + 1) for child in reversed(children))


def build_task_tree(tasks: Iterable[Task], root_ids: set[int], *, show_completed: bool = False,
                    show_snoozed: bool = False, now: datetime | None = None) -> TaskTree:
    now = now or datetime.now(timezone.utc)
    visible = {
        task.persisted_id: task for task in tasks
        if task.state.is_active or (show_completed and task.state is State.DONE)
    }
    parents = {
        tid: task.root_id if task.root_id in visible and not task.in_inbox and not visible[task.root_id].in_inbox else None
        for tid, task in visible.items()
    }
    # Older versions allowed containment cycles. Break one display edge per cycle so
    # every task stays discoverable and can be moved, without changing stored history.
    visited: set[int] = set()
    repaired = False
    for start in sorted(visible):
        current: int | None = start
        chain: list[int] = []
        positions: dict[int, int] = {}
        while current is not None and current not in visited and current not in positions:
            positions[current] = len(chain)
            chain.append(current)
            current = parents[current]
        if current in positions:
            cycle = chain[positions[current]:]
            parents[min(cycle)] = None
            repaired = True
        visited.update(chain)

    if not show_snoozed:
        # Keep sleeping ancestors only as path context for visible descendants.
        keep: set[int] = set()
        for start, task in visible.items():
            if task.is_snoozed(now):
                continue
            current = start
            while current is not None and current not in keep:
                keep.add(current)
                current = parents[current]
        visible = {tid: task for tid, task in visible.items() if tid in keep}
        parents = {tid: parent for tid, parent in parents.items() if tid in keep}

    children: dict[int, list[int]] = defaultdict(list)
    groups: dict[str, list[int]] = {"inbox": [], "goals": [], "unlinked": [], "meta": []}
    for tid in sorted(visible):
        parent = parents[tid]
        if parent is not None:
            children[parent].append(tid)
        else:
            group = "inbox" if visible[tid].in_inbox else "goals" if tid in root_ids else "meta" if visible[tid].is_meta else "unlinked"
            groups[group].append(tid)
    return TaskTree(visible, parents, dict(children), groups, repaired)


def task_label(task: Task, root_ids: set[int], *, now: datetime, lang: str | None = None) -> str:
    marker = "✓" if task.state is State.DONE else "★" if task.id in root_ids else "⊛" if task.is_meta else "○"
    text = f"{marker} #{task.id} {task.title}"
    if task.is_snoozed(now):
        assert task.snoozed_until is not None
        text += " · " + _t("snooze.until", lang=lang, time=task.snoozed_until.astimezone().isoformat(timespec="minutes"))
    if task.in_inbox and task.state.is_active:
        text += " · " + _t("inbox.age", lang=lang, days=task.inbox_days(now))
        if task.needs_inbox_review(now):
            text += " · " + _t("inbox.needs_review", lang=lang)
    return text


def format_task_tree(
    tasks: Iterable[Task], root_ids: set[int], *, show_completed: bool = False,
    show_snoozed: bool = False, now: datetime | None = None, lang: str | None = None,
) -> str:
    """Render a terminal tree from the same hierarchy as the Web UI, without recording views."""
    now = now or datetime.now(timezone.utc)
    tree = build_task_tree(tasks, root_ids, show_completed=show_completed, show_snoozed=show_snoozed, now=now)
    if not tree.tasks:
        return _t("cli.tree.empty", lang=lang)
    lines = [_t("cli.tree.title", lang=lang)]
    if tree.repaired_cycles:
        lines.append(_t("webapp.tree.legacy_cycle", lang=lang))
    for group, ids in tree.groups.items():
        if not ids:
            continue
        lines.extend(["", _t(f"webapp.tree.group.{group}", lang=lang)])
        stack = [(tid, "", i == len(ids) - 1) for i, tid in reversed(list(enumerate(ids)))]
        while stack:
            tid, prefix, last = stack.pop()
            lines.append(prefix + ("└── " if last else "├── ") + task_label(tree.tasks[tid], root_ids, now=now, lang=lang))
            children = tree.children.get(tid, [])
            child_prefix = prefix + ("    " if last else "│   ")
            stack.extend((child, child_prefix, i == len(children) - 1) for i, child in reversed(list(enumerate(children))))
    return "\n".join(lines)

"""Read-only parent/child tree model shared by the Web UI and its tests."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from todogc.heap import State, Task


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


def build_task_tree(tasks: Iterable[Task], root_ids: set[int], *, show_completed: bool = False) -> TaskTree:
    visible = {
        task.persisted_id: task for task in tasks
        if task.state.is_active or (show_completed and task.state is State.DONE)
    }
    parents = {
        tid: task.root_id if task.root_id in visible else None
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

    children: dict[int, list[int]] = defaultdict(list)
    groups: dict[str, list[int]] = {"goals": [], "unlinked": [], "meta": []}
    for tid in sorted(visible):
        parent = parents[tid]
        if parent is not None:
            children[parent].append(tid)
        else:
            group = "goals" if tid in root_ids else "meta" if visible[tid].is_meta else "unlinked"
            groups[group].append(tid)
    return TaskTree(visible, parents, dict(children), groups, repaired)

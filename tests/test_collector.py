# test_collector.py — the GC engine: classification, reference-cycle grouping, released_pct, and COLLECTED isolation.

from __future__ import annotations

from todogc.collector import Collector, Reason, TaskMetrics
from todogc.heap import Generation, State, Task


def _task(i: int, meta: bool = False) -> Task:
    """Construct a test task with an id.

    Args:
        i (int): the task id.
        meta (bool): whether it's a meta-task.

    Returns:
        Task: a task object for testing.
    """
    return Task(title=f"t{i}", id=i, gen=(Generation.META if meta else Generation.EDEN))


def test_classification_on_graph():
    """root/meta are always alive; reachable and alive; reachable but dead metrics = stale; unreachable orphan = unreachable; unreachable cycle = cycle."""
    tasks = {i: _task(i, meta=(i == 9)) for i in range(12)}
    edges = {0: {1, 2, 10}, 4: {5}, 5: {4}, 6: {7}, 7: {8}, 8: {6}, 10: {11}, 11: {10}}
    alive = TaskMetrics(3, 0, 1.0)
    dead = TaskMetrics(0, 0, 40.0)
    metrics = {1: alive, 2: dead, 10: alive, 11: alive}
    res = Collector().collect(tasks, {0}, edges, lambda i: metrics[i])

    survivors = {t.id for t in res.survivors}
    by = {c.task.id: c for c in res.candidates}
    assert survivors == {0, 1, 9, 10, 11}
    assert by[2].reason is Reason.STALE
    assert by[3].reason is Reason.UNREACHABLE
    assert by[4].reason is Reason.CYCLE and by[4].cycle_id == by[5].cycle_id
    assert sorted([sorted(c.task_ids) for c in res.cycles]) == [[4, 5], [6, 7, 8]]
    assert 10 not in by and 11 not in by  # a legitimate cycle hanging off a root is never caught by mistake


def test_released_pct():
    """released_pct = candidate count / total."""
    tasks = {0: _task(0), 1: _task(1)}
    res = Collector().collect(tasks, {0}, {}, lambda i: TaskMetrics(0, 0, None))
    assert res.released_pct == 50.0  # #0 root is alive, #1 is unreachable


def test_run_on_store_finds_persisted_cycle(store, now):
    """Assemble the graph from persisted roots + deps and catch the cycle."""
    goal = store.add_task("G", is_root=True, ts=now)
    store.add_task("写模块", root_id=goal.id, ts=now)
    a = store.add_task("学 Rust", ts=now)
    b = store.add_task("重写博客", ts=now)
    store.add_dep(a.id, b.id)
    store.add_dep(b.id, a.id)
    res = Collector().run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)
    by = {c.task.id: c.reason for c in res.candidates}
    assert by[a.id] is Reason.CYCLE and by[b.id] is Reason.CYCLE


def test_run_on_store_excludes_collected(store, now):
    """A task already COLLECTED is never treated as a candidate again (reclaim isolation)."""
    store.add_task("G", is_root=True, ts=now)
    x = store.add_task("X", ts=now)
    t = store.get_task(x.id, record_view=False)
    t.state = State.COLLECTED
    store.update_task(t)
    res = Collector().run_on_store(store, store.root_ids(), now=now)
    assert x.id not in {c.task.id for c in res.candidates}

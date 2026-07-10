# test_collector.py —— GC 引擎：分类、循环引用分组、released_pct 与 COLLECTED 隔离。

from __future__ import annotations

from todogc.collector import Collector, Reason, TaskMetrics
from todogc.heap import Generation, State, Task


def _task(i: int, meta: bool = False) -> Task:
    """构造一个带 id 的测试任务。

    Args:
        i (int): 任务 id。
        meta (bool): 是否为元任务。

    Returns:
        Task: 测试用任务对象。
    """
    return Task(title=f"t{i}", id=i, gen=(Generation.META if meta else Generation.EDEN))


def test_classification_on_graph():
    """root/meta 恒活；可达且活；可达但指标死=stale；不可达孤儿=unreachable；不可达环=cycle。"""
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
    assert 10 not in by and 11 not in by  # 挂在 root 上的合法环不误伤


def test_released_pct():
    """released_pct = 候选数 / 总数。"""
    tasks = {0: _task(0), 1: _task(1)}
    res = Collector().collect(tasks, {0}, {}, lambda i: TaskMetrics(0, 0, None))
    assert res.released_pct == 50.0  # #0 root 活，#1 不可达


def test_run_on_store_finds_persisted_cycle(store, now):
    """从持久化的 roots + deps 组装图并抓到环。"""
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
    """已 COLLECTED 的任务不再被当候选(回收隔离)。"""
    store.add_task("G", is_root=True, ts=now)
    x = store.add_task("X", ts=now)
    t = store.get_task(x.id, record_view=False)
    t.state = State.COLLECTED
    store.update_task(t)
    res = Collector().run_on_store(store, store.root_ids(), now=now)
    assert x.id not in {c.task.id for c in res.candidates}

# test_store.py —— 持久化层：埋点、判活指标、时间窗、roots 与依赖边。

from __future__ import annotations

from datetime import timedelta

import pytest

from todogc.heap import Generation, State, TouchKind


def test_add_task_records_create(store, now):
    """add_task 落库并自动记一条 CREATE。"""
    t = store.add_task("学吉他", ts=now)
    assert t.id is not None and t.created_at == now
    assert [x.kind for x in store.touches(t.id)] == [TouchKind.CREATE]


def test_view_progress_defer_and_window(store, now):
    """view 不计 refcount；progress 计；defer 计 skip；时间窗排除 31 天前。"""
    t = store.add_task("t", ts=now)
    store.get_task(t.id, ts=now)
    assert store.view_count(t.id) == 1 and store.refcount(t.id) == 0
    store.progress(t.id, ts=now)
    assert store.refcount(t.id) == 1
    store.defer(t.id, ts=now)
    assert store.skip_count(t.id) == 1
    store.progress(t.id, ts=now - timedelta(days=31))
    assert store.refcount(t.id) == 2
    assert store.refcount(t.id, within_days=30, now=now) == 1


def test_record_view_flag(store, now):
    """record_view=False 时读取不留 view(供 collector 扫描用)。"""
    t = store.add_task("t", ts=now)
    store.get_task(t.id, record_view=False)
    store.list_tasks(record_view=False)
    assert store.view_count(t.id) == 0


def test_roots(store, now):
    """set_root / root_ids 增删正确。"""
    a = store.add_task("a", is_root=True, ts=now)
    b = store.add_task("b", ts=now)
    assert store.root_ids() == {a.id}
    store.set_root(b.id)
    assert store.root_ids() == {a.id, b.id}
    store.set_root(a.id, False)
    assert store.root_ids() == {b.id}


def test_deps(store, now):
    """add_dep 幂等 / remove 生效 / 自环与不存在 id 抛 ValueError。"""
    a = store.add_task("a", ts=now)
    b = store.add_task("b", ts=now)
    store.add_dep(a.id, b.id)
    store.add_dep(a.id, b.id)  # 幂等
    assert store.deps() == [(a.id, b.id)]
    store.remove_dep(a.id, b.id)
    assert store.deps() == []
    with pytest.raises(ValueError):
        store.add_dep(a.id, a.id)
    with pytest.raises(ValueError):
        store.add_dep(a.id, 999)


def test_update_task_persists(store, now):
    """update_task 回写分代 / 年龄 / 状态。"""
    t = store.add_task("t", ts=now)
    promoted = t.aged()
    promoted.state = State.CANDIDATE
    store.update_task(promoted)
    r = store.get_task(t.id, record_view=False)
    assert r.age == 1 and r.gen is Generation.SURVIVOR and r.state is State.CANDIDATE

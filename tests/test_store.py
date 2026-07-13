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


def test_record_watch_tick_upsert_semantics(store, now):
    """notified_at=None 只更新 status、不清空既有去抖时间戳；传了才盖新值。"""
    t = store.add_task("t", ts=now)
    store.record_watch_tick(t.id, "stale", notified_at=now)
    assert store.watch_states()[t.id].status == "stale"
    assert store.watch_states()[t.id].notified_at == now

    store.record_watch_tick(t.id, "cycle:0")  # 状态变了，但这轮没推送
    state = store.watch_states()[t.id]
    assert state.status == "cycle:0"
    assert state.notified_at == now  # 去抖时间戳保留，不是被清空成 None

    later = now + timedelta(hours=1)
    store.record_watch_tick(t.id, "cycle:0", notified_at=later)
    assert store.watch_states()[t.id].notified_at == later


def test_watch_meta_get_set_roundtrip(store):
    """watch_meta 是通用键值表：没写过返回 None，写过原样读回。"""
    assert store.watch_meta_get("last_digest_at") is None
    store.watch_meta_set("last_digest_at", "2026-07-10T12:00:00+00:00")
    assert store.watch_meta_get("last_digest_at") == "2026-07-10T12:00:00+00:00"
    store.watch_meta_set("last_digest_at", "2026-07-11T00:00:00+00:00")  # 覆盖而非报错
    assert store.watch_meta_get("last_digest_at") == "2026-07-11T00:00:00+00:00"


def test_watch_lock_single_owner_and_stale_takeover(store, now):
    """建议性锁：心跳新鲜时别的 pid 抢不走；心跳过期后允许接管；释放/续期认 pid。"""
    stale_after = timedelta(seconds=60)
    assert store.try_acquire_watch_lock(pid=100, now=now, stale_after=stale_after) is True
    # 心跳仍新鲜，另一个 pid 抢不到。
    assert store.try_acquire_watch_lock(pid=200, now=now + timedelta(seconds=10), stale_after=stale_after) is False

    # 错误的 pid 续期是 no-op，不影响持锁者的心跳时间戳。
    store.heartbeat_watch_lock(pid=200, now=now + timedelta(seconds=20))
    assert store.watch_meta_get("watch_lock_pid") == "100"

    # 持锁者正常续期。
    store.heartbeat_watch_lock(pid=100, now=now + timedelta(seconds=20))
    assert store.watch_meta_get("watch_lock_heartbeat_at") == store._to_iso(now + timedelta(seconds=20))

    # 心跳过期(超过 stale_after 没续)，新 pid 可以接管。
    stale_now = now + timedelta(seconds=20) + stale_after + timedelta(seconds=1)
    assert store.try_acquire_watch_lock(pid=200, now=stale_now, stale_after=stale_after) is True
    assert store.watch_meta_get("watch_lock_pid") == "200"

    # 错误 pid 释放是 no-op；真正持锁者释放才清空。
    store.release_watch_lock(pid=100)
    assert store.watch_meta_get("watch_lock_pid") == "200"
    store.release_watch_lock(pid=200)
    assert store.watch_meta_get("watch_lock_pid") is None
    assert store.watch_meta_get("watch_lock_heartbeat_at") is None

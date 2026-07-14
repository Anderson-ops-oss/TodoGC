# test_store.py — the persistence layer: event tracking, liveness metrics, time windows, roots, and dependency edges.

from __future__ import annotations

from datetime import timedelta

import pytest

from todogc.heap import Generation, State, TouchKind


def test_add_task_records_create(store, now):
    """add_task persists and automatically logs a CREATE."""
    t = store.add_task("学吉他", ts=now)
    assert t.id is not None and t.created_at == now
    assert [x.kind for x in store.touches(t.id)] == [TouchKind.CREATE]


def test_view_progress_defer_and_window(store, now):
    """view doesn't count toward refcount; progress does; defer counts toward skip; the time window excludes anything older than 31 days."""
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
    """With record_view=False, a read leaves no view (used by the collector's scans)."""
    t = store.add_task("t", ts=now)
    store.get_task(t.id, record_view=False)
    store.list_tasks(record_view=False)
    assert store.view_count(t.id) == 0


def test_roots(store, now):
    """set_root / root_ids add and remove correctly."""
    a = store.add_task("a", is_root=True, ts=now)
    b = store.add_task("b", ts=now)
    assert store.root_ids() == {a.id}
    store.set_root(b.id)
    assert store.root_ids() == {a.id, b.id}
    store.set_root(a.id, False)
    assert store.root_ids() == {b.id}


def test_deps(store, now):
    """add_dep is idempotent / remove takes effect / a self-loop or nonexistent id raises ValueError."""
    a = store.add_task("a", ts=now)
    b = store.add_task("b", ts=now)
    store.add_dep(a.id, b.id)
    store.add_dep(a.id, b.id)  # idempotent
    assert store.deps() == [(a.id, b.id)]
    store.remove_dep(a.id, b.id)
    assert store.deps() == []
    with pytest.raises(ValueError):
        store.add_dep(a.id, a.id)
    with pytest.raises(ValueError):
        store.add_dep(a.id, 999)


def test_update_task_persists(store, now):
    """update_task writes back generation / age / state."""
    t = store.add_task("t", ts=now)
    promoted = t.aged()
    promoted.state = State.CANDIDATE
    store.update_task(promoted)
    r = store.get_task(t.id, record_view=False)
    assert r.age == 1 and r.gen is Generation.SURVIVOR and r.state is State.CANDIDATE


def test_record_watch_tick_upsert_semantics(store, now):
    """notified_at=None only updates status without clearing the existing debounce timestamp; passing a value stamps a new one."""
    t = store.add_task("t", ts=now)
    store.record_watch_tick(t.id, "stale", notified_at=now)
    assert store.watch_states()[t.id].status == "stale"
    assert store.watch_states()[t.id].notified_at == now

    store.record_watch_tick(t.id, "cycle:0")  # status changed, but nothing was pushed this round
    state = store.watch_states()[t.id]
    assert state.status == "cycle:0"
    assert state.notified_at == now  # the debounce timestamp is preserved, not cleared to None

    later = now + timedelta(hours=1)
    store.record_watch_tick(t.id, "cycle:0", notified_at=later)
    assert store.watch_states()[t.id].notified_at == later


def test_watch_meta_get_set_roundtrip(store):
    """watch_meta is a generic key-value table: returns None if never written, reads back exactly what was written."""
    assert store.watch_meta_get("last_digest_at") is None
    store.watch_meta_set("last_digest_at", "2026-07-10T12:00:00+00:00")
    assert store.watch_meta_get("last_digest_at") == "2026-07-10T12:00:00+00:00"
    store.watch_meta_set("last_digest_at", "2026-07-11T00:00:00+00:00")  # overwrites rather than erroring
    assert store.watch_meta_get("last_digest_at") == "2026-07-11T00:00:00+00:00"


def test_watch_lock_single_owner_and_stale_takeover(store, now):
    """Advisory lock: another pid can't steal it while the heartbeat is fresh; takeover is allowed once the heartbeat is stale; release/renew respect the owning pid."""
    stale_after = timedelta(seconds=60)
    assert store.try_acquire_watch_lock(pid=100, now=now, stale_after=stale_after) is True
    # heartbeat still fresh, another pid can't take it.
    assert store.try_acquire_watch_lock(pid=200, now=now + timedelta(seconds=10), stale_after=stale_after) is False

    # renewing with the wrong pid is a no-op, doesn't affect the lock holder's heartbeat timestamp.
    store.heartbeat_watch_lock(pid=200, now=now + timedelta(seconds=20))
    assert store.watch_meta_get("watch_lock_pid") == "100"

    # the lock holder renews normally.
    store.heartbeat_watch_lock(pid=100, now=now + timedelta(seconds=20))
    assert store.watch_meta_get("watch_lock_heartbeat_at") == store._to_iso(now + timedelta(seconds=20))

    # heartbeat expired (not renewed within stale_after), a new pid can take over.
    stale_now = now + timedelta(seconds=20) + stale_after + timedelta(seconds=1)
    assert store.try_acquire_watch_lock(pid=200, now=stale_now, stale_after=stale_after) is True
    assert store.watch_meta_get("watch_lock_pid") == "200"

    # releasing with the wrong pid is a no-op; only the actual lock holder releasing clears it.
    store.release_watch_lock(pid=100)
    assert store.watch_meta_get("watch_lock_pid") == "200"
    store.release_watch_lock(pid=200)
    assert store.watch_meta_get("watch_lock_pid") is None
    assert store.watch_meta_get("watch_lock_heartbeat_at") is None

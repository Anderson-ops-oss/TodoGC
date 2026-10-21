# test_watcher.py — the scheduled checkup: run_tick's debounce/reminder-period/recovery
# semantics, heap-alarm edge-triggering, push message copy, Watcher's construction
# validation, and _run_once's orchestration (using a fake channel, no real network).

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from todogc.config import AppConfig
from todogc.notify import NotifyError
from todogc.watcher import (
    TickEvent,
    TickResult,
    Watcher,
    _digest_message,
    _event_message,
    _heap_alarm_message,
    run_tick,
)


class _FakeChannel:
    """A fake push channel that records send() call arguments; raises NotifyError every time when fail=True."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def send(self, title: str, message: str, *, priority: int = 3, tags: list[str] | None = None) -> None:
        self.calls.append({"title": title, "message": message, "priority": priority, "tags": tags})
        if self.fail:
            raise NotifyError("boom")


def _watcher(config: AppConfig) -> Watcher:
    """Construct a Watcher; db_path is never actually used (_run_once/_digest_due both take the passed-in store directly)."""
    return Watcher("unused.db", replace(config, watch_weekly_review_enabled=False))


# ============ run_tick: debounce / reminder period / recovery / heap alarm ============

def test_new_candidate_triggers_event_then_dedupes(store, now):
    """A brand-new candidate triggers an event on the first round; running another round right away with unchanged status doesn't push again."""
    root = store.add_task("G", is_root=True, ts=now - timedelta(days=40))
    task = store.add_task("僵尸", root_id=root.id, ts=now - timedelta(days=40))
    cfg = AppConfig()

    first = run_tick(store, cfg, now=now)
    assert [e.task_id for e in first.events] == [task.id]
    assert first.events[0].status == "stale"

    second = run_tick(store, cfg, now=now + timedelta(hours=1))
    assert second.events == []


def test_cycle_candidates_flagged_as_new_event(store, now):
    """A mutually-vouching reference cycle triggers an event too, with the status string carrying the cycle number (so a later change of cycle buddy is recognized as a new transition)."""
    old = now - timedelta(days=40)
    a = store.add_task("学Rust", ts=old)
    b = store.add_task("重写博客", ts=old)
    store.add_dep(a.id, b.id)
    store.add_dep(b.id, a.id)
    tick = run_tick(store, AppConfig(), now=now)
    statuses = {e.task_id: e.status for e in tick.events}
    assert statuses[a.id].startswith("cycle:")
    assert statuses[a.id] == statuses[b.id]


def test_remind_after_days_refires_unchanged_candidate(store, now):
    """Status unchanged, but longer than remind_days since the last push -> the reminder period is reached, push again."""
    root = store.add_task("G", is_root=True, ts=now - timedelta(days=40))
    task = store.add_task("僵尸", root_id=root.id, ts=now - timedelta(days=40))
    cfg = AppConfig(watch_remind_days=7)
    run_tick(store, cfg, now=now)

    quiet = run_tick(store, cfg, now=now + timedelta(days=3))
    assert quiet.events == []

    due = run_tick(store, cfg, now=now + timedelta(days=8))
    assert [e.task_id for e in due.events] == [task.id]


def test_recovery_resets_dedupe_state_silently(store, now):
    """A candidate reviving back to alive: no push (this is good news), and watch_state resets to alive so the next backslide counts as "new" again."""
    root = store.add_task("G", is_root=True, ts=now - timedelta(days=40))
    task = store.add_task("僵尸", root_id=root.id, ts=now - timedelta(days=40))
    cfg = AppConfig()
    run_tick(store, cfg, now=now)

    store.link(task.id, root.id, ts=now + timedelta(hours=1))
    store.progress(task.id, ts=now + timedelta(hours=1))
    recovered = run_tick(store, cfg, now=now + timedelta(hours=2))

    assert recovered.events == []
    assert store.watch_states()[task.id].status == "alive"


def test_heap_alarm_is_edge_triggered(store, now):
    """Alarms once at the moment heap usage crosses the threshold; doesn't repeat while persistently high."""
    cfg = AppConfig(watch_heap_threshold_pct=50, heap_max=4)
    for i in range(3):  # 3/4 = 75% >= 50%
        store.add_task(f"T{i}", ts=now)

    first = run_tick(store, cfg, now=now)
    second = run_tick(store, cfg, now=now + timedelta(minutes=1))
    assert first.heap_alarm is True
    assert second.heap_alarm is False


# ============ Push message copy (pure functions) ============

def test_event_message_bundles_multiple_events_into_one_push():
    """Multiple transitions merge into a single push (Chinese); cycles use 🔗, everything else uses 🆕, with a heap-usage summary attached."""
    result = TickResult(
        events=[
            TickEvent(task_id=1, title="学吉他", status="stale", detail="可达但已死"),
            TickEvent(task_id=2, title="学Rust", status="cycle:0", detail="陷在引用环 #0"),
        ],
        heap_size=3,
        heap_max=10,
    )
    title, body = _event_message(result, lang="zh")
    assert title == "TodoGC · 2 项新增候选"
    assert "🆕 #1 学吉他 —— 可达但已死" in body
    assert "🔗 #2 学Rust —— 陷在引用环 #0" in body
    assert "堆占用 3/10 (30%)" in body


def test_event_message_bundles_multiple_events_into_one_push_english_default():
    """The same data rendered in English (the default language) — the icon/placeholder logic is language-independent, only the wording follows lang."""
    result = TickResult(
        events=[
            TickEvent(task_id=1, title="Learn guitar", status="stale", detail="Reachable but dead"),
            TickEvent(task_id=2, title="Learn Rust", status="cycle:0", detail="Trapped in cycle #0"),
        ],
        heap_size=3,
        heap_max=10,
    )
    title, body = _event_message(result, lang="en")
    assert title == "TodoGC · 2 new candidates"
    assert "🆕 #1 Learn guitar — Reachable but dead" in body
    assert "🔗 #2 Learn Rust — Trapped in cycle #0" in body
    assert "Heap usage 3/10 (30%)" in body


def test_heap_alarm_message_format():
    title, body = _heap_alarm_message(TickResult(heap_size=8, heap_max=10), lang="zh")
    assert title == "TodoGC · 堆占用告警"
    assert "8/10 (80%)" in body


def test_digest_message_format():
    title, body = _digest_message(TickResult(candidate_count=5, heap_size=2, heap_max=10), lang="zh")
    assert title == "TodoGC · 定时体检"
    assert "候选 5 个" in body and "20%" in body


# ============ Watcher: construction validation + _run_once orchestration ============

def test_watcher_requires_ntfy_enabled():
    """Refuses construction when notify.ntfy_enabled isn't turned on, rather than discovering the push target isn't configured only at run()."""
    with pytest.raises(ValueError, match="ntfy_enabled"):
        Watcher("unused.db", AppConfig(ntfy_enabled=False))


def test_run_once_sends_only_the_event_push_when_nothing_else_due(store, now):
    """Tunes both the alarm threshold and the digest interval so neither triggers, verifying one tick sends only the push it should."""
    root = store.add_task("G", is_root=True, ts=now - timedelta(days=40))
    store.add_task("僵尸", root_id=root.id, ts=now - timedelta(days=40))
    cfg = AppConfig(
        ntfy_enabled=True, ntfy_topic="x",
        watch_heap_threshold_pct=100, heap_max=1000, watch_digest_seconds=10**9,
    )
    watcher = _watcher(cfg)
    fake = _FakeChannel()
    watcher._channel = fake  # pyright: ignore[reportAttributeAccessIssue]
    store.watch_meta_set("last_digest_at", now.isoformat())  # rule out the "first run always sends a digest" case

    watcher._run_once(store, now)

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["title"] == "TodoGC · New candidate"
    assert call["priority"] == 4
    assert call["tags"] == ["skull"]


def test_run_once_sends_all_three_categories_and_swallows_failures(store, now, caplog):
    """When event + heap alarm + heartbeat digest are all due, each sends one push; the channel failing entirely must not blow up the tick itself."""
    root = store.add_task("G", is_root=True, ts=now - timedelta(days=40))
    store.add_task("僵尸", root_id=root.id, ts=now - timedelta(days=40))
    cfg = AppConfig(ntfy_enabled=True, ntfy_topic="x", watch_heap_threshold_pct=1, heap_max=2)
    watcher = _watcher(cfg)
    fake = _FakeChannel(fail=True)
    watcher._channel = fake  # pyright: ignore[reportAttributeAccessIssue]

    with caplog.at_level("WARNING", logger="todogc.watcher"):
        watcher._run_once(store, now)  # should not raise

    titles = {c["title"] for c in fake.calls}
    assert titles == {"TodoGC · New candidate", "TodoGC · Heap usage alert", "TodoGC · Scheduled checkup"}
    assert sum("todogc watch:" in record.getMessage() for record in caplog.records) == 3


def test_digest_due_respects_interval(store, now):
    cfg = AppConfig(ntfy_enabled=True, ntfy_topic="x", watch_digest_seconds=100)
    watcher = _watcher(cfg)

    assert watcher._digest_due(store, now) is True  # a digest has never been sent before
    store.watch_meta_set("last_digest_at", now.isoformat())
    assert watcher._digest_due(store, now + timedelta(seconds=50)) is False
    assert watcher._digest_due(store, now + timedelta(seconds=101)) is True

# test_watcher.py —— 定时体检：run_tick 的去抖/复议期/恢复语义、堆告警边沿触发、
# 推送消息文案、Watcher 的构造校验与 _run_once 编排(用假 channel，不碰真网络)。

from __future__ import annotations

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
    """记录 send() 调用参数的假推送渠道；fail=True 时每次都抛 NotifyError。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def send(self, title: str, message: str, *, priority: int = 3, tags: list[str] | None = None) -> None:
        self.calls.append({"title": title, "message": message, "priority": priority, "tags": tags})
        if self.fail:
            raise NotifyError("boom")


def _watcher(config: AppConfig) -> Watcher:
    """构造一个 Watcher；db_path 不会被用到(_run_once/_digest_due 都直接吃传入的 store)。"""
    return Watcher("unused.db", config)


# ============ run_tick：去抖 / 复议期 / 恢复 / 堆告警 ============

def test_new_candidate_triggers_event_then_dedupes(store, now):
    """全新候选第一轮触发事件；立刻再跑一轮、状态不变 → 不重复推。"""
    root = store.add_task("G", is_root=True, ts=now - timedelta(days=40))
    task = store.add_task("僵尸", root_id=root.id, ts=now - timedelta(days=40))
    cfg = AppConfig()

    first = run_tick(store, cfg, now=now)
    assert [e.task_id for e in first.events] == [task.id]
    assert first.events[0].status == "stale"

    second = run_tick(store, cfg, now=now + timedelta(hours=1))
    assert second.events == []


def test_cycle_candidates_flagged_as_new_event(store, now):
    """互相担保的循环引用一样触发事件，状态串带上环号(供后续换环搭子时识别为新跃迁)。"""
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
    """状态没变，但距上次推送已超过 remind_days → 复议期到，再推一次。"""
    root = store.add_task("G", is_root=True, ts=now - timedelta(days=40))
    task = store.add_task("僵尸", root_id=root.id, ts=now - timedelta(days=40))
    cfg = AppConfig(watch_remind_days=7)
    run_tick(store, cfg, now=now)

    quiet = run_tick(store, cfg, now=now + timedelta(days=3))
    assert quiet.events == []

    due = run_tick(store, cfg, now=now + timedelta(days=8))
    assert [e.task_id for e in due.events] == [task.id]


def test_recovery_resets_dedupe_state_silently(store, now):
    """候选复活变回 alive：不推送(这是好消息)，且 watch_state 清回 alive 供下次堕落重新算"新"。"""
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
    """堆占用跨阈值那一刻告警一次；持续高位不重复告警。"""
    cfg = AppConfig(watch_heap_threshold_pct=50, heap_max=4)
    for i in range(3):  # 3/4 = 75% >= 50%
        store.add_task(f"T{i}", ts=now)

    first = run_tick(store, cfg, now=now)
    second = run_tick(store, cfg, now=now + timedelta(minutes=1))
    assert first.heap_alarm is True
    assert second.heap_alarm is False


# ============ 推送消息文案(纯函数) ============

def test_event_message_bundles_multiple_events_into_one_push():
    """多条跃迁合并成一条推送；循环用 🔗、其余用 🆕，附带堆占用摘要。"""
    result = TickResult(
        events=[
            TickEvent(task_id=1, title="学吉他", status="stale", detail="可达但已死"),
            TickEvent(task_id=2, title="学Rust", status="cycle:0", detail="陷在引用环 #0"),
        ],
        heap_size=3,
        heap_max=10,
    )
    title, body = _event_message(result)
    assert title == "TodoGC · 2 项新增候选"
    assert "🆕 #1 学吉他 —— 可达但已死" in body
    assert "🔗 #2 学Rust —— 陷在引用环 #0" in body
    assert "堆占用 3/10 (30%)" in body


def test_heap_alarm_message_format():
    title, body = _heap_alarm_message(TickResult(heap_size=8, heap_max=10))
    assert title == "TodoGC · 堆占用告警"
    assert "8/10 (80%)" in body


def test_digest_message_format():
    title, body = _digest_message(TickResult(candidate_count=5, heap_size=2, heap_max=10))
    assert title == "TodoGC · 定时体检"
    assert "候选 5 个" in body and "20%" in body


# ============ Watcher：构造校验 + _run_once 编排 ============

def test_watcher_requires_ntfy_enabled():
    """notify.ntfy_enabled 未开启时拒绝构造，而不是等 run() 时才发现推送目标没配。"""
    with pytest.raises(ValueError, match="ntfy_enabled"):
        Watcher("unused.db", AppConfig(ntfy_enabled=False))


def test_run_once_sends_only_the_event_push_when_nothing_else_due(store, now):
    """把告警阈值和心跳周期都调到不会触发，验证一次 tick 只发该发的那一条。"""
    root = store.add_task("G", is_root=True, ts=now - timedelta(days=40))
    store.add_task("僵尸", root_id=root.id, ts=now - timedelta(days=40))
    cfg = AppConfig(
        ntfy_enabled=True, ntfy_topic="x",
        watch_heap_threshold_pct=100, heap_max=1000, watch_digest_seconds=10**9,
    )
    watcher = _watcher(cfg)
    fake = _FakeChannel()
    watcher._channel = fake  # pyright: ignore[reportAttributeAccessIssue]
    store.watch_meta_set("last_digest_at", now.isoformat())  # 排除"首次必发摘要"这条

    watcher._run_once(store, now)

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["title"] == "TodoGC · 新增候选"
    assert call["priority"] == 4
    assert call["tags"] == ["skull"]


def test_run_once_sends_all_three_categories_and_swallows_failures(store, now, capsys):
    """事件 + 堆告警 + 心跳摘要都到齐时各发一条；渠道全部失败也不能让 tick 本身炸。"""
    root = store.add_task("G", is_root=True, ts=now - timedelta(days=40))
    store.add_task("僵尸", root_id=root.id, ts=now - timedelta(days=40))
    cfg = AppConfig(ntfy_enabled=True, ntfy_topic="x", watch_heap_threshold_pct=1, heap_max=2)
    watcher = _watcher(cfg)
    fake = _FakeChannel(fail=True)
    watcher._channel = fake  # pyright: ignore[reportAttributeAccessIssue]

    watcher._run_once(store, now)  # 不应抛出

    titles = {c["title"] for c in fake.calls}
    assert titles == {"TodoGC · 新增候选", "TodoGC · 堆占用告警", "TodoGC · 定时体检"}
    assert capsys.readouterr().err.count("todogc watch：") == 3


def test_digest_due_respects_interval(store, now):
    cfg = AppConfig(ntfy_enabled=True, ntfy_topic="x", watch_digest_seconds=100)
    watcher = _watcher(cfg)

    assert watcher._digest_due(store, now) is True  # 从没发过摘要
    store.watch_meta_set("last_digest_at", now.isoformat())
    assert watcher._digest_due(store, now + timedelta(seconds=50)) is False
    assert watcher._digest_due(store, now + timedelta(seconds=101)) is True

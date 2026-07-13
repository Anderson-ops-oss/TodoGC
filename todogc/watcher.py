# watcher.py —— 定时体检核心：轮询判活，跟上一轮 diff 出"状态跃迁"才推送(不是心跳污染)，
# 心跳粒度独立于轮询间隔(见 _HEARTBEAT_SECONDS)，单实例建议性锁防 CLI watch 与 Web 内置
# watcher 同时跑、对同一份跃迁重复推送。CLI 的 `todogc watch` 与 webapp.py 共用同一份逻辑，
# 只是谁在什么线程里调用 Watcher.run() 的区别。

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from todogc.collector import Candidate, Collector, GCPolicy, Reason
from todogc.config import AppConfig
from todogc.notify import NotifyError, NtfyChannel
from todogc.store import Store

# 心跳/可中断休眠的粒度，与用户可配的 watch_poll_seconds 解耦：如果心跳只在每次真正
# 轮询时才刷新，一个刚睡下、还没到下一次 poll 的存活 watcher 会被 watch_lock_stale_seconds
# 误判为"进程已死"。心跳按这个小粒度独立刷新，poll_seconds 只决定多久做一次真正的判活。
_HEARTBEAT_SECONDS = 30


def _status_of(candidate: Candidate) -> str:
    """把一条候选归一成"状态串"，用于跨轮 diff。

    STALE/UNREACHABLE 直接用 reason 值；CYCLE 额外带上环号——同为"陷入循环引用"，
    但换了个环搭子(cycle_id 变了)在语义上是一次新的跃迁，值得再推一次。

    Args:
        candidate (Candidate): 一条回收候选。

    Returns:
        str: 归一化状态串。
    """
    if candidate.reason is Reason.CYCLE:
        return f"cycle:{candidate.cycle_id}"
    return candidate.reason.value


def _policy_from_config(config: AppConfig) -> GCPolicy:
    """把 AppConfig 的 gc.* 字段映射成 GCPolicy(watch 场景没有 CLI 参数覆盖，直接照抄配置)。"""
    return GCPolicy(
        mode=config.gc_mode,
        recent_days=config.recent_days,
        max_skips=config.max_consecutive_skips,
        restore_grace_days=config.restore_grace_days,
    )


@dataclass
class TickEvent:
    """一次 tick 里判定为"值得推送"的一条候选跃迁。

    Attributes:
        task_id (int): 任务 id。
        title (str): 任务标题。
        status (str): 本轮归一化状态串(供消息里挑图标用)。
        detail (str): 人话解释，直接复用 collector.Candidate.detail，不重复造文案。
    """

    task_id: int
    title: str
    status: str
    detail: str


@dataclass
class TickResult:
    """一次 watch tick 的产出：算出来了什么、有没有值得推送的东西。

    Attributes:
        events (list[TickEvent]): 本轮触发的状态跃迁(新候选 / 新循环 / 复议期到)。
        candidate_count (int): 本轮回收候选总数(含未触发事件的老候选)，供心跳摘要用。
        heap_size (int): 当前活跃堆占用。
        heap_max (int): -Xmx 上限。
        heap_alarm (bool): 堆占用是否本轮新跨过阈值(边沿触发，不是"持续高位"每轮都报)。
    """

    events: list[TickEvent] = field(default_factory=list)
    candidate_count: int = 0
    heap_size: int = 0
    heap_max: int = 0
    heap_alarm: bool = False

    @property
    def heap_pct(self) -> int:
        """堆占用百分比(四舍五入)；heap_max <= 0 时视为 0。"""
        return 0 if self.heap_max <= 0 else round(100 * self.heap_size / self.heap_max)


def run_tick(store: Store, config: AppConfig, *, now: datetime | None = None) -> TickResult:
    """跑一次判活，跟上一轮 watch 状态 diff，把本轮状态落库，返回需要推送的内容。

    只负责"算"和"落库"，不碰网络，发不发、怎么发是 Watcher 的事，这样纯逻辑可以脱离
    ntfy 单独测试。

    去抖规则：
      - 任务从 alive(或从未被 watch 观测过)跃迁到候选 → 新事件。
      - 候选状态本身发生变化(如 stale → cycle，或换了个环搭子)→ 也算新事件。
      - 状态没变，但距上次为它推过事件已超过 config.watch_remind_days → 复议期到，再推一次。
      - 候选变回 alive → 静默重置去抖记录，不推送(这是好消息，且这样它下次再堕落会被当"新"跃迁)。

    Args:
        store (Store): 数据源。
        config (AppConfig): 含判活口径(gc.*)与体检节奏(watch.*)。
        now (datetime | None): 时间基准；None 用当前 UTC。

    Returns:
        TickResult: 本轮判活与去抖判定的结果。
    """
    now = now or datetime.now(timezone.utc)
    result = Collector(_policy_from_config(config)).run_on_store(
        store, store.root_ids(), extra_edges=store.deps(), now=now
    )
    previous = store.watch_states()
    remind_after = timedelta(days=config.watch_remind_days)

    events: list[TickEvent] = []
    seen_ids: set[int] = set()

    for candidate in result.candidates:
        tid = candidate.task.persisted_id
        seen_ids.add(tid)
        status = _status_of(candidate)
        prev = previous.get(tid)
        is_new = prev is None or prev.status != status
        is_due = (
            not is_new
            and prev is not None
            and prev.notified_at is not None
            and now - prev.notified_at >= remind_after
        )
        if is_new or is_due:
            events.append(TickEvent(task_id=tid, title=candidate.task.title, status=status, detail=candidate.detail))
            store.record_watch_tick(tid, status, notified_at=now)
        else:
            store.record_watch_tick(tid, status)

    for survivor in result.survivors:
        tid = survivor.persisted_id
        if tid in seen_ids:
            continue
        prev = previous.get(tid)
        if prev is not None and prev.status != "alive":
            store.record_watch_tick(tid, "alive")

    heap_size = store.heap_size()
    was_alarmed = store.watch_meta_get("heap_alarm_active") == "1"
    tick = TickResult(
        events=events,
        candidate_count=len(result.candidates),
        heap_size=heap_size,
        heap_max=config.heap_max,
    )
    now_alarmed = tick.heap_pct >= config.watch_heap_threshold_pct
    tick.heap_alarm = now_alarmed and not was_alarmed
    store.watch_meta_set("heap_alarm_active", "1" if now_alarmed else "0")
    return tick


def _event_message(result: TickResult) -> tuple[str, str]:
    """把本轮触发的事件汇总成一条推送(多条跃迁合并成一条，不逐条刷屏)。"""
    lines = []
    for event in result.events:
        icon = "🔗" if event.status.startswith("cycle:") else "🆕"
        lines.append(f"{icon} #{event.task_id} {event.title} —— {event.detail}")
    title = "TodoGC · 新增候选" if len(result.events) == 1 else f"TodoGC · {len(result.events)} 项新增候选"
    body = "\n".join(lines) + f"\n堆占用 {result.heap_size}/{result.heap_max} ({result.heap_pct}%)"
    return title, body


def _heap_alarm_message(result: TickResult) -> tuple[str, str]:
    """堆占用新跨过阈值的告警(边沿触发)。"""
    title = "TodoGC · 堆占用告警"
    body = f"🚨 -Xmx 逼近上限：{result.heap_size}/{result.heap_max} ({result.heap_pct}%)"
    return title, body


def _digest_message(result: TickResult) -> tuple[str, str]:
    """定时心跳摘要(不管这轮有没有新事件都可能发，证明 watcher 还活着)。"""
    title = "TodoGC · 定时体检"
    body = f"候选 {result.candidate_count} 个 · 堆占用 {result.heap_size}/{result.heap_max} ({result.heap_pct}%)"
    return title, body


class Watcher:
    """定时体检的执行体：poll → tick → (去抖判定后)推送 → 睡到下一次 poll，循环往复。

    `run()` 阻塞当前线程直至 `stop()` 被调用。CLI 的 `todogc watch` 在主线程直接跑；
    Web 端把它丢进一条 daemon 线程(见 webapp.py 的 st.cache_resource 包装)。
    """

    def __init__(self, db_path: str, config: AppConfig) -> None:
        """构造一个 watcher。

        Args:
            db_path (str): 要体检的数据库路径。
            config (AppConfig): 含 notify.*/watch.* 配置；ntfy_enabled 必须为 True。

        Returns:
            None

        Raises:
            ValueError: notify.ntfy_enabled 未开启时(调用方应在构造前自行检查，这里是兜底)。
        """
        if not config.ntfy_enabled:
            raise ValueError("Watcher 需要先在配置里开启 notify.ntfy_enabled 并配置 ntfy_topic")
        self._db_path = db_path
        self._config = config
        self._channel = NtfyChannel(topic=config.ntfy_topic, server=config.ntfy_server, token=config.ntfy_token)
        self._stop = threading.Event()
        self._pid = os.getpid()

    def stop(self) -> None:
        """请求循环在下一个心跳片结束后退出(不阻塞等待退出完成)。

        Returns:
            None
        """
        self._stop.set()

    def run(self) -> None:
        """阻塞运行体检循环，直至 stop() 被调用。

        启动时尝试拿单实例建议性锁；若另一个 watcher 心跳仍新鲜，直接返回(不占用/不重复推送)。

        Returns:
            None
        """
        store = Store(self._db_path)
        try:
            now = datetime.now(timezone.utc)
            stale_after = timedelta(seconds=self._config.watch_lock_stale_seconds)
            if not store.try_acquire_watch_lock(self._pid, now, stale_after=stale_after):
                print("todogc watch：检测到另一个 watcher 正在运行，本进程不再重复推送", file=sys.stderr)
                return
            elapsed = self._config.watch_poll_seconds  # 启动即跑一次，别等一个完整 poll 周期。
            while not self._stop.is_set():
                now = datetime.now(timezone.utc)
                store.heartbeat_watch_lock(self._pid, now)
                if elapsed >= self._config.watch_poll_seconds:
                    self._run_once(store, now)
                    elapsed = 0
                slept = self._stop.wait(min(_HEARTBEAT_SECONDS, self._config.watch_poll_seconds))
                if slept:
                    break
                elapsed += _HEARTBEAT_SECONDS
        finally:
            store.release_watch_lock(self._pid)
            store.close()

    def _run_once(self, store: Store, now: datetime) -> None:
        """跑一次 tick，把值得推送的部分发出去；推送失败只警告，不打断循环。"""
        result = run_tick(store, self._config, now=now)
        if result.events:
            self._safe_send(*_event_message(result), priority=4, tags=["skull"])
        if result.heap_alarm:
            self._safe_send(*_heap_alarm_message(result), priority=5, tags=["rotating_light"])
        if self._digest_due(store, now):
            self._safe_send(*_digest_message(result), priority=2, tags=["recycle"])
            store.watch_meta_set("last_digest_at", now.isoformat())

    def _digest_due(self, store: Store, now: datetime) -> bool:
        """距上次心跳摘要是否已超过 config.watch_digest_seconds。"""
        raw = store.watch_meta_get("last_digest_at")
        if raw is None:
            return True
        return (now - datetime.fromisoformat(raw)).total_seconds() >= self._config.watch_digest_seconds

    def _safe_send(self, title: str, message: str, *, priority: int, tags: list[str]) -> None:
        """推送一条消息；网络/服务端失败只打警告，绝不让通知拖垮体检循环。"""
        try:
            self._channel.send(title, message, priority=priority, tags=tags)
        except NotifyError as exc:
            print(f"todogc watch：「{title}」{exc}", file=sys.stderr)

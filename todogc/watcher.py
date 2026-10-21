# watcher.py — the scheduled-checkup core: polls liveness, only pushes when a diff against
# the previous round finds a "state transition" (never on heartbeat noise). Heartbeat
# granularity is decoupled from the poll interval (see _HEARTBEAT_SECONDS); the
# single-instance advisory lock prevents CLI watch and the Web UI's built-in watcher from
# running at once and double-pushing the same transition. The CLI's `todogc watch` and
# webapp.py share this exact logic — the only difference is which thread calls
# Watcher.run().

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from todogc.collector import Candidate, Collector, GCPolicy, Reason
from todogc.config import AppConfig
from todogc.i18n import n_, t
from todogc.heap import Task
from todogc.notify import NotifyError, NtfyChannel
from todogc.scheduling import local_time
from todogc.store import Store

logger = logging.getLogger(__name__)

# The granularity of the heartbeat/interruptible sleep, decoupled from the user-configurable
# watch_poll_seconds: if the heartbeat only refreshed on each real poll, a live watcher that
# just went to sleep and hasn't reached its next poll yet would be wrongly judged "dead" by
# watch_lock_stale_seconds. The heartbeat refreshes independently at this small granularity;
# poll_seconds only decides how often a real liveness check actually runs.
_HEARTBEAT_SECONDS = 30


def _status_of(candidate: Candidate) -> str:
    """Normalize a candidate into a "status string", for diffing across rounds.

    STALE/UNREACHABLE use the reason value directly; CYCLE additionally carries the cycle
    number — being "stuck in a reference cycle" is the same reason, but swapping to a
    different cycle buddy (cycle_id changed) is semantically a new transition, worth pushing
    again.

    Args:
        candidate (Candidate): a reclaim candidate.

    Returns:
        str: the normalized status string.
    """
    if candidate.reason is Reason.CYCLE:
        return f"cycle:{candidate.cycle_id}"
    return candidate.reason.value


def _policy_from_config(config: AppConfig) -> GCPolicy:
    """Map AppConfig's gc.* fields to a GCPolicy (the watch scenario has no CLI argument overrides, so the config is copied as-is)."""
    return GCPolicy(
        mode=config.gc_mode,
        recent_days=config.recent_days,
        max_skips=config.max_consecutive_skips,
        restore_grace_days=config.restore_grace_days,
    )


@dataclass
class TickEvent:
    """A candidate transition judged "worth pushing" within one tick.

    Attributes:
        task_id (int): the task id.
        title (str): the task title.
        status (str): this round's normalized status string (used to pick the message's
            icon).
        detail (str): a human-readable explanation, reusing collector.Candidate.detail
            directly rather than composing new copy.
    """

    task_id: int
    title: str
    status: str
    detail: str


@dataclass
class TickResult:
    """The output of one watch tick: what was computed, and whether there's anything worth pushing.

    Attributes:
        events (list[TickEvent]): state transitions triggered this round (new candidate /
            new cycle / reminder period reached).
        candidate_count (int): total reclaim candidates this round (including old candidates
            that didn't trigger an event), used for the heartbeat digest.
        heap_size (int): current active heap usage.
        heap_max (int): the -Xmx cap.
        heap_alarm (bool): whether heap usage newly crossed the threshold this round
            (edge-triggered, not reported every round while "persistently high").
    """

    events: list[TickEvent] = field(default_factory=list)
    candidate_count: int = 0
    heap_size: int = 0
    heap_max: int = 0
    heap_alarm: bool = False
    wakeups: list[Task] = field(default_factory=list)

    @property
    def heap_pct(self) -> int:
        """Heap usage percentage (rounded); treated as 0 when heap_max <= 0."""
        return 0 if self.heap_max <= 0 else round(100 * self.heap_size / self.heap_max)


def run_tick(store: Store, config: AppConfig, *, now: datetime | None = None) -> TickResult:
    """Run one liveness check, diff it against the previous watch state, persist this round's state, and return what's worth pushing.

    Only responsible for "computing" and "persisting" — never touches the network; whether
    and how to push is the Watcher's job, so this pure logic can be tested independently of
    ntfy.

    Debounce rules:
      - A task transitioning from alive (or never observed by watch before) to a candidate
        -> a new event.
      - The candidate's status itself changing (e.g. stale -> cycle, or switching to a
        different cycle buddy) -> also a new event.
      - Status unchanged, but it's been longer than config.watch_remind_days since an event
        was last pushed for it -> the reminder period is reached, push again.
      - A candidate reverting to alive -> silently resets the debounce record, no push (this
        is good news, and it means the next time it backslides it's treated as a "new"
        transition).

    Args:
        store (Store): the data source.
        config (AppConfig): contains the liveness thresholds (gc.*) and checkup cadence
            (watch.*).
        now (datetime | None): the time base; None uses the current UTC time.

    Returns:
        TickResult: this round's liveness and debounce judgment.
    """
    now = now or datetime.now(timezone.utc)
    result = Collector(_policy_from_config(config), lang=config.lang).run_on_store(
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
        wakeups=store.due_snoozes(now),
    )
    now_alarmed = tick.heap_pct >= config.watch_heap_threshold_pct
    tick.heap_alarm = now_alarmed and not was_alarmed
    store.watch_meta_set("heap_alarm_active", "1" if now_alarmed else "0")
    return tick


def _snooze_message(tasks: list[Task], *, lang: str) -> tuple[str, str]:
    title = t("snooze.push.title", lang=lang, count=len(tasks))
    body = "\n".join(t("snooze.push.line", lang=lang, id=task.id, title=task.title,
                       time=local_time(task.snoozed_until))
                     for task in tasks if task.snoozed_until is not None)
    return title, body


def _event_message(result: TickResult, *, lang: str) -> tuple[str, str]:
    """Roll this round's triggered events up into a single push (multiple transitions merged into one, not spammed one at a time)."""
    lines = []
    for event in result.events:
        icon = "🔗" if event.status.startswith("cycle:") else "🆕"
        lines.append(
            t(
                "watcher.push.event.line",
                lang=lang,
                icon=icon,
                task_id=event.task_id,
                title=event.title,
                detail=event.detail,
            )
        )
    title = n_(
        len(result.events), "watcher.push.event.title.one", "watcher.push.event.title.other", lang=lang
    )
    heap_line = t(
        "watcher.push.heap_usage", lang=lang, size=result.heap_size, max=result.heap_max, pct=result.heap_pct
    )
    body = "\n".join(lines) + f"\n{heap_line}"
    return title, body


def _heap_alarm_message(result: TickResult, *, lang: str) -> tuple[str, str]:
    """Alert for heap usage newly crossing the threshold (edge-triggered)."""
    title = t("watcher.push.heap_alarm.title", lang=lang)
    body = t(
        "watcher.push.heap_alarm.body", lang=lang, size=result.heap_size, max=result.heap_max, pct=result.heap_pct
    )
    return title, body


def _digest_message(result: TickResult, *, lang: str) -> tuple[str, str]:
    """The scheduled heartbeat digest (may be sent whether or not there's a new event this round, to prove the watcher is still alive)."""
    title = t("watcher.push.digest.title", lang=lang)
    body = t(
        "watcher.push.digest.body",
        lang=lang,
        count=result.candidate_count,
        size=result.heap_size,
        max=result.heap_max,
        pct=result.heap_pct,
    )
    return title, body


class Watcher:
    """The scheduled checkup's execution body: poll -> tick -> (after debounce judgment) push -> sleep until the next poll, and repeat.

    `run()` blocks the current thread until `stop()` is called. The CLI's `todogc watch` runs
    it directly on the main thread; the Web UI drops it into a daemon thread (see webapp.py's
    st.cache_resource wrapper).
    """

    def __init__(self, db_path: str, config: AppConfig) -> None:
        """Construct a watcher.

        Args:
            db_path (str): path to the database to check up on.
            config (AppConfig): contains notify.*/watch.* config; ntfy_enabled must be True.

        Returns:
            None

        Raises:
            ValueError: if notify.ntfy_enabled isn't turned on (the caller should check
                this itself before construction; this is a backstop).
        """
        if not config.ntfy_enabled:
            raise ValueError(t("watcher.error.ntfy_disabled", lang=config.lang))
        self._db_path = db_path
        self._config = config
        self._channel = NtfyChannel(
            topic=config.ntfy_topic, server=config.ntfy_server, token=config.ntfy_token, lang=config.lang
        )
        self._stop = threading.Event()
        self._pid = os.getpid()

    def stop(self) -> None:
        """Request the loop to exit after the next heartbeat tick ends (doesn't block waiting for the exit to complete).

        Returns:
            None
        """
        self._stop.set()

    def run(self) -> None:
        """Block and run the checkup loop until stop() is called.

        On startup, tries to acquire the single-instance advisory lock; if another watcher's
        heartbeat is still fresh, returns immediately (doesn't take over, doesn't push
        duplicates).

        Returns:
            None
        """
        store = Store(self._db_path)
        try:
            now = datetime.now(timezone.utc)
            stale_after = timedelta(seconds=self._config.watch_lock_stale_seconds)
            if not store.try_acquire_watch_lock(self._pid, now, stale_after=stale_after):
                logger.warning(t("watcher.lock.already_running", lang=self._config.lang))
                return
            elapsed = self._config.watch_poll_seconds  # run once immediately at startup, don't wait a full poll cycle
            while not self._stop.is_set():
                now = datetime.now(timezone.utc)
                store.heartbeat_watch_lock(self._pid, now)
                if elapsed >= self._config.watch_poll_seconds:
                    self._run_once(store, now)
                    elapsed = 0
                interval = min(_HEARTBEAT_SECONDS, self._config.watch_poll_seconds - elapsed)
                slept = self._stop.wait(interval)
                if slept:
                    break
                elapsed += interval
        finally:
            store.release_watch_lock(self._pid)
            store.close()

    def _run_once(self, store: Store, now: datetime) -> None:
        """Run one tick and send out whatever's worth pushing; a push failure only warns, it never breaks the loop."""
        result = run_tick(store, self._config, now=now)
        lang = self._config.lang
        if result.wakeups:
            if self._safe_send(*_snooze_message(result.wakeups, lang=lang), priority=3, tags=["alarm_clock"]):
                for task in result.wakeups:
                    store.acknowledge_snooze(task)
        if result.events:
            self._safe_send(*_event_message(result, lang=lang), priority=4, tags=["skull"])
        if result.heap_alarm:
            self._safe_send(*_heap_alarm_message(result, lang=lang), priority=5, tags=["rotating_light"])
        if self._digest_due(store, now):
            self._safe_send(*_digest_message(result, lang=lang), priority=2, tags=["recycle"])
            store.watch_meta_set("last_digest_at", now.isoformat())

    def _digest_due(self, store: Store, now: datetime) -> bool:
        """Whether it's been longer than config.watch_digest_seconds since the last heartbeat digest."""
        raw = store.watch_meta_get("last_digest_at")
        if raw is None:
            return True
        return (now - datetime.fromisoformat(raw)).total_seconds() >= self._config.watch_digest_seconds

    def _safe_send(self, title: str, message: str, *, priority: int, tags: list[str]) -> bool:
        """Send a push notification; a network/server failure only logs a warning, never lets a notification take down the checkup loop."""
        try:
            self._channel.send(title, message, priority=priority, tags=tags)
            return True
        except NotifyError as exc:
            logger.warning(t("watcher.push.failed", lang=self._config.lang, title=title, error=exc))
            return False

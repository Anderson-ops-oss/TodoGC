"""Daily planning is persistent and ordered, but never pretends to be progress."""

import sqlite3
import time
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from todogc.cli import main
from todogc.collector import Collector
from todogc.heap import State
from todogc.store import Store
from todogc.today import build_today


def ids(tasks):
    return [task.id for task in tasks]


def test_plan_order_idempotency_and_no_progress_or_gc_changes(store, now):
    tasks = [store.add_task(name, ts=now) for name in ("One", "Two", "Three")]
    before = [store.touches(task.id) for task in tasks]
    for task in tasks:
        store.focus_task(task.id, ts=now)
    store.focus_task(2, ts=now)
    assert ids(build_today(store, now=now).selected) == [1, 2, 3]
    store.focus_task(3, position=1, ts=now)
    assert ids(build_today(store, now=now).selected) == [3, 1, 2]
    with pytest.raises(ValueError, match="position"):
        store.focus_task(1, position=5, ts=now)
    assert ids(build_today(store, now=now).selected) == [3, 1, 2]
    store.unfocus_task(1, ts=now)
    assert ids(build_today(store, now=now).selected) == [3, 2]
    assert [store.touches(task.id) for task in tasks] == before
    assert len(Collector().run_on_store(store, store.root_ids(), now=now).candidates) == 3


def test_new_day_carryover_and_dismissal_do_not_rewrite_old_plan(store, now):
    yesterday = now - timedelta(days=1)
    for name in ("Continue", "Finished", "Sleeping", "Dismiss"):
        store.add_task(name, ts=yesterday)
    for tid in range(1, 5):
        store.focus_task(tid, ts=yesterday)
    store.complete_task(2, ts=yesterday)
    store.snooze_task(3, now + timedelta(days=1), ts=yesterday)
    plan = build_today(store, now=now)
    assert plan.selected == [] and ids(plan.carryover) == [1, 4]
    store.focus_task(1, ts=now)
    store.unfocus_task(4, ts=now)
    assert ids(build_today(store, now=now).selected) == [1]
    assert build_today(store, now=now).carryover == []
    store.unfocus_task(1, ts=now)
    assert build_today(store, now=now).carryover == []
    assert store.focus_task_ids(yesterday.astimezone().date()) == [1, 2, 3, 4]
    assert ids(build_today(store, now=now + timedelta(days=1)).carryover) == [3]


def test_today_preserves_done_and_paused_selections_and_tracks_actual_completions(store, now):
    for name in ("Finished", "Paused", "Outside plan"):
        store.add_task(name, ts=now)
    store.focus_task(1, ts=now)
    store.focus_task(2, ts=now)
    store.complete_task(1, ts=now)
    store.complete_task(3, ts=now)
    store.snooze_task(2, now + timedelta(hours=1), ts=now)
    plan = build_today(store, now=now)
    assert ids(plan.selected) == [1, 2]
    assert plan.selected[0].state is State.DONE and plan.selected[1].is_snoozed(now)
    assert set(ids(plan.completed)) == {1, 3}
    store.reopen_task(1, ts=now)
    assert ids(build_today(store, now=now).completed) == [3]


def test_waking_suggestions_survive_notification_ack_and_are_not_automatically_focused(store, now):
    task = store.add_task("Wake", ts=now - timedelta(hours=2))
    # Avoid relying on where the fixed clock falls in the local day.
    due = now
    store.snooze_task(task.id, due, ts=now - timedelta(hours=1))
    store.acknowledge_snooze(store.get_task(task.id, record_view=False))
    plan = build_today(store, now=now)
    assert plan.selected == [] and ids(plan.waking) == [task.id]
    store.unfocus_task(task.id, ts=now)
    assert build_today(store, now=now).waking == []
    assert build_today(store, now=now + timedelta(days=1)).waking == []


def test_focus_validates_lifecycle_and_rejects_inbox_snooze_and_bad_position(store, now):
    store.capture("Idea", ts=now)
    store.add_task("Sleeping", ts=now)
    store.snooze_task(2, now + timedelta(days=1), ts=now)
    store.add_task("Finished", ts=now)
    store.complete_task(3, ts=now)
    for tid in (1, 2, 3, 999):
        with pytest.raises(ValueError):
            store.focus_task(tid, ts=now)
    assert build_today(store, now=now).selected == []
    store.add_task("Ready", ts=now)
    with pytest.raises(ValueError):
        store.focus_task(4, position=2, ts=now)
    assert build_today(store, now=now).selected == []


def test_daily_plans_persist_and_v8_migration_backs_up_data(tmp_path, now):
    db = tmp_path / "plan.db"
    with Store(db) as store:
        store.add_task("Existing goal", is_root=True, ts=now)
        store.progress(1, "Existing note", ts=now)
        store._conn.execute("DROP TABLE daily_focus")
        store._conn.execute("UPDATE schema_meta SET value = '8' WHERE key = 'schema_version'")
    with Store(db) as store:
        assert store.schema_version == 9
        assert store.touches(1)[-1].note == "Existing note"
        store.focus_task(1, ts=now)
    with sqlite3.connect(str(db) + ".bak") as backup:
        assert backup.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "8"
        assert backup.execute("SELECT title FROM task").fetchone()[0] == "Existing goal"
    with Store(db) as store:
        assert ids(build_today(store, now=now).selected) == [1]
        assert store.root_ids() == {1} and store.refcount(1) == 1


def test_today_rolls_over_at_local_midnight_instead_of_utc(store, monkeypatch):
    from datetime import date, datetime, timezone

    if not hasattr(time, "tzset"):
        pytest.skip("Local timezone changes require tzset")
    try:
        with monkeypatch.context() as env:
            env.setenv("TZ", "Asia/Tokyo")
            time.tzset()
            before = datetime(2026, 10, 22, 14, 59, tzinfo=timezone.utc)
            after = before + timedelta(minutes=2)
            store.add_task("Continue", ts=before)
            store.add_task("Wake at midnight", ts=before)
            store.add_task("Finished after midnight", ts=before)
            store.focus_task(1, ts=before)
            store.snooze_task(2, before + timedelta(minutes=1), ts=before)
            store.complete_task(3, ts=after)
            assert build_today(store, now=before).day == date(2026, 10, 22)
            plan = build_today(store, now=after)
            assert plan.day == date(2026, 10, 23) and plan.selected == []
            assert ids(plan.carryover) == [1] and ids(plan.waking) == [2] and ids(plan.completed) == [3]
    finally:
        time.tzset()


def test_simultaneous_cli_web_planning_keeps_both_selections(tmp_path, now):
    db = tmp_path / "concurrent.db"
    with Store(db) as store:
        store.add_task("One", ts=now)
        store.add_task("Two", ts=now)
    barrier = Barrier(2)

    def focus(tid):
        with Store(db) as store:
            barrier.wait(timeout=5)
            store.focus_task(tid, ts=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(focus, (1, 2)))
    with Store(db) as store:
        assert set(ids(build_today(store, now=now).selected)) == {1, 2}


def test_cli_plan_order_completion_and_errors(tmp_path, capsys):
    db = str(tmp_path / "cli.db")

    def run(*args):
        return main(["--db", db, *args])

    assert run("add", "Goal", "--root") == 0
    assert run("add", "Action", "--root-id", "1") == 0
    assert run("focus", "1") == 0
    assert run("focus", "2", "--position", "1") == 0
    capsys.readouterr()
    assert run("today") == 0
    out = capsys.readouterr().out
    assert out.index("#2 Action") < out.index("#1 Goal")
    assert run("done", "2") == 0
    assert run("today") == 0
    assert "Completed today" in capsys.readouterr().out
    assert run("unfocus", "1") == 0
    assert run("focus", "2") == 1
    assert run("focus", "999") == 1
    with Store(db) as store:
        assert store.view_count(1) == store.view_count(2) == store.refcount(1) == store.refcount(2) == 0

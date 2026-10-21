"""Scheduled pauses: time boundaries, hierarchy, GC protection and durable reminders."""

import sqlite3
from datetime import timedelta

import pytest

from todogc import report
from todogc.cli import main, run_ceremony
from todogc.collector import Collector
from todogc.config import AppConfig, load_config
from todogc.heap import ReferenceStrength, TouchKind
from todogc.notify import NotifyError
from todogc.scheduling import parse_snooze_time
from todogc.store import Store
from todogc.tree import build_task_tree
from todogc.watcher import Watcher, run_tick


@pytest.mark.parametrize(('duration', 'delta'), [('30m', timedelta(minutes=30)), ('2h', timedelta(hours=2)),
                                                ('3d', timedelta(days=3)), ('1w', timedelta(weeks=1))])
def test_duration_is_elapsed_time(duration, delta, now):
    assert parse_snooze_time(duration=duration, now=now) == now + delta


def test_absolute_time_offsets_and_invalid_inputs(now):
    assert parse_snooze_time(until='2026-07-11 09:00+09:00', now=now).isoformat() == '2026-07-11T00:00:00+00:00'
    assert parse_snooze_time(until='2026-07-11T00:00Z', now=now).isoformat() == '2026-07-11T00:00:00+00:00'
    for kwargs in ({}, {'duration': '1d', 'until': '2026-07-11'}, {'duration': '0h'}, {'duration': '-2h'},
                   {'duration': '1.5d'}, {'duration': '99' * 500 + 'w'}, {'until': 'bad'}, {'until': now.isoformat()}):
        with pytest.raises(ValueError):
            parse_snooze_time(**kwargs, now=now)


def test_snooze_preserves_counts_capacity_and_parent_reachability(store, now):
    root = store.add_task('Goal', is_root=True, ts=now)
    child = store.add_task('Child', root_id=root.id, ts=now)
    orphan = store.add_task('Old orphan', ts=now - timedelta(days=100))
    store.defer(orphan.id, ts=now - timedelta(hours=1))
    until = now + timedelta(days=3)
    store.snooze_task(root.id, until, ts=now)
    store.snooze_task(orphan.id, until, ts=now)
    result = Collector().run_on_store(store, store.root_ids(), now=now)
    assert not result.candidates and {task.id for task in result.survivors} == {root.id, child.id, orphan.id}
    assert {task.id for task in result.snoozed} == {root.id, orphan.id}
    assert store.heap_size() == report.heap_used_count(result) == 3
    assert store.skip_count(orphan.id) == store.consecutive_skip_count(orphan.id) == 1
    assert store.refcount(orphan.id) == store.view_count(orphan.id) == 0
    assert all(task.age == 0 for task in result.survivors)
    assert 'Old orphan' not in report.format_doctor(store, result, now=now)
    assert 'Snoozed until' in report.format_heap_map(result, store.root_ids())
    assert not run_tick(store, AppConfig(), now=now).events


def test_expiry_is_read_only_and_grace_is_anchored_to_deadline(store, now):
    task = store.add_task('Old weak orphan', ref_strength=ReferenceStrength.WEAK, ts=now - timedelta(days=100))
    for _ in range(6):
        store.defer(task.id, ts=now - timedelta(hours=1))
    until = now + timedelta(hours=1)
    store.snooze_task(task.id, until, ts=now)
    touches = store.touches(task.id)
    assert store.get_task(task.id, record_view=False).is_snoozed(until - timedelta(microseconds=1))
    for moment in (until, until + timedelta(days=7) - timedelta(microseconds=1)):
        assert not store.get_task(task.id, record_view=False).is_snoozed(moment)
        assert not Collector().run_on_store(store, store.root_ids(), now=moment).candidates
        assert store.list_tasks(snoozed=False, record_view=False, ts=moment)[0].id == task.id
    expired = Collector().run_on_store(store, store.root_ids(), now=until + timedelta(days=7))
    assert [candidate.task.id for candidate in expired.candidates] == [task.id]
    assert store.touches(task.id) == touches and store.refcount(task.id) == 0


def test_early_wake_and_subtree_schedules_are_explicit(store, now):
    root = store.add_task('Goal', is_root=True, ts=now)
    child = store.add_task('Child', root_id=root.id, ts=now)
    leaf = store.add_task('Leaf', root_id=child.id, ts=now)
    until = now + timedelta(days=10)
    assert store.snooze_task(root.id, until, ts=now) == [root.id]
    tree = build_task_tree(store.list_tasks(record_view=False), store.root_ids(), now=now)
    assert list(tree.tasks) == [root.id, child.id, leaf.id]  # sleeping ancestor is path context
    assert tree.path(leaf.id)[0].is_snoozed(now)
    assert store.snooze_task(root.id, until, with_subtasks=True, ts=now) == [root.id, child.id, leaf.id]
    assert not build_task_tree(store.list_tasks(record_view=False), store.root_ids(), now=now).tasks
    assert len(build_task_tree(store.list_tasks(record_view=False), store.root_ids(), show_snoozed=True, now=now).tasks) == 3
    assert store.unsnooze_task(root.id, ts=now) == [root.id]
    assert store.get_task(child.id, record_view=False).is_snoozed(now)
    assert store.unsnooze_task(root.id, with_subtasks=True, ts=now) == [child.id, leaf.id]
    assert store.get_task(leaf.id, record_view=False).snoozed_until is None
    assert store.view_count(root.id) == store.refcount(root.id) == store.skip_count(root.id) == 0
    assert store.touches(leaf.id)[-1].kind is TouchKind.UNSNOOZE
    with pytest.raises(ValueError):
        store.unsnooze_task(root.id, ts=now)


def test_early_wake_of_orphan_has_its_own_seven_day_grace(store, now):
    task = store.add_task('Orphan', ts=now - timedelta(days=100))
    store.snooze_task(task.id, now + timedelta(days=100), ts=now - timedelta(days=1))
    store.unsnooze_task(task.id, ts=now)
    assert not Collector().run_on_store(store, set(), now=now + timedelta(days=6)).candidates
    assert Collector().run_on_store(store, set(), now=now + timedelta(days=7)).candidates


def test_closed_tasks_cancel_schedule_and_cannot_be_snoozed(store, now):
    idea = store.capture('Idea', ts=now)
    store.snooze_task(idea.id, now + timedelta(days=1), ts=now)
    store.complete_task(idea.id, ts=now)
    with pytest.raises(ValueError, match='completed'):
        store.snooze_task(idea.id, now + timedelta(days=1), ts=now)
    assert store.reopen_task(idea.id, ts=now).snoozed_until is None
    store.snooze_task(idea.id, now + timedelta(days=1), ts=now)
    store.discard_inbox(idea.id, ts=now)
    with pytest.raises(ValueError):
        store.snooze_task(idea.id, now + timedelta(days=1), ts=now)
    assert not store.due_snoozes(now + timedelta(days=2))
    assert store.restore_task(idea.id, ts=now).snoozed_until is None
    before = store.touches(idea.id)
    with pytest.raises(ValueError):
        store.snooze_task(idea.id, now, ts=now)
    assert store.touches(idea.id) == before


@pytest.mark.parametrize('stage', ['choice', 'eulogy'])
def test_stale_gc_snapshot_cannot_collect_newly_snoozed_task(store, now, stage):
    task = store.add_task('Orphan', ts=now)
    snapshot = Collector().run_on_store(store, set(), now=now)
    def snooze():
        store.snooze_task(task.id, now + timedelta(days=1), ts=now)
        return 'collect'
    outcome = run_ceremony(store, snapshot, decide=lambda _: snooze() if stage == 'choice' else 'collect',
                           eulogize=lambda _: snooze(), now=now)
    assert not outcome.collected
    assert store.get_task(task.id, record_view=False).is_snoozed(now)


def test_cli_schedules_lists_and_wakes_without_view_or_skip_events(tmp_path, capsys):
    db = str(tmp_path / 'snooze.db')
    def run(*args):
        return main(['--db', db, *args])
    assert run('add', 'Goal', '--root') == run('add', 'Work', '--root-id', '1') == 0
    assert run('snooze', '2', '--for', '2h', '--note', 'Waiting') == 0
    assert run('ls') == 0
    assert 'Work' not in capsys.readouterr().out.split('Snoozed 1 task(s) until')[-1].split('\n', 1)[-1]
    assert run('snoozed') == run('tree', '--show-snoozed') == run('show', '2') == 0
    assert 'Snoozed until' in capsys.readouterr().out
    with Store(db) as store:
        assert store.view_count(2) == store.skip_count(2) == store.refcount(2) == 0
        assert 'Waiting' in store.touches(2)[-1].note
    assert run('unsnooze', '2') == 0
    assert run('snooze', '2', '--until', '2000-01-01') == 1
    assert run('snooze', '99', '--for', '1d') == 1
    with pytest.raises(SystemExit):
        run('snooze', '2')
    assert run('defer', '2') == 0
    with Store(db) as store:
        assert store.skip_count(2) == 1


def test_schema_v6_migration_preserves_inbox_and_completion(tmp_path, now):
    path = tmp_path / 'v6.db'
    with Store(path) as store:
        goal = store.add_task('Goal', is_root=True, ts=now)
        idea = store.capture('Idea', ts=now)
        store.complete_task(goal.id, ts=now)
        store._conn.execute("DROP TRIGGER snapshot_new_touch")
        store._conn.execute("DROP TABLE touch_task_snapshot")
        store._conn.execute("DROP TABLE review_snapshot")
        store._conn.execute('ALTER TABLE task DROP COLUMN snoozed_until')
        store._conn.execute('ALTER TABLE task DROP COLUMN snooze_notified_until')
        store._conn.execute("UPDATE schema_meta SET value='6' WHERE key='schema_version'")
    with Store(path) as store:
        assert store.schema_version == 8
        assert store.get_task(goal.id, record_view=False).completed_at == now
        assert store.get_task(idea.id, record_view=False).in_inbox
        assert all(task.snoozed_until is None for task in store.list_tasks(record_view=False))
    with sqlite3.connect(str(path) + '.bak') as backup:
        assert backup.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == '6'
        assert 'snoozed_until' not in {row[1] for row in backup.execute('PRAGMA table_info(task)')}
    before = (tmp_path / 'v6.db.bak').read_bytes()
    with Store(path):
        pass
    assert (tmp_path / 'v6.db.bak').read_bytes() == before


def test_watch_defaults_to_one_minute_but_preserves_overrides(tmp_path):
    cfg = tmp_path / 'config.toml'
    assert AppConfig().watch_poll_seconds == load_config(cfg, environ={}).watch_poll_seconds == 60
    cfg.write_text('[watch]\npoll_seconds = 1800\n')
    assert load_config(cfg, environ={}).watch_poll_seconds == 1800
    assert load_config(cfg, environ={'TODOGC_WATCH_POLL_SECONDS': '20'}).watch_poll_seconds == 20


def test_wake_notifications_are_durable_deduped_and_retry_failures(store, now):
    one = store.add_task('First', is_root=True, ts=now)
    two = store.capture('Second', ts=now)
    until = now + timedelta(minutes=1)
    for task in (one, two):
        store.snooze_task(task.id, until, ts=now)
    cfg = AppConfig(ntfy_enabled=True, ntfy_topic='test', heap_max=1000, watch_weekly_review_enabled=False)
    store.watch_meta_set('last_digest_at', now.isoformat())
    calls = []
    class Channel:
        fail = True
        def send(self, title, message, **kwargs):
            calls.append((title, message))
            if self.fail:
                raise NotifyError('Temporary failure')
    channel = Channel()
    watcher = Watcher('unused.db', cfg)
    watcher._channel = channel
    watcher._run_once(store, now)
    assert not calls
    watcher._run_once(store, until)
    assert len(calls) == 1 and len(store.due_snoozes(until)) == 2
    old_schedule = store.due_snoozes(until)[0]
    channel.fail = False
    watcher._run_once(store, until + timedelta(minutes=1))
    assert len(calls) == 2 and not store.due_snoozes(until + timedelta(minutes=1))
    assert '#1 First' in calls[-1][1] and '#2 Second' in calls[-1][1]
    restarted = Watcher('unused.db', cfg)
    restarted._channel = channel
    restarted._run_once(store, until + timedelta(minutes=2))
    assert len(calls) == 2
    store.snooze_task(one.id, until + timedelta(hours=1), ts=until)
    assert not store.due_snoozes(until)
    store.acknowledge_snooze(old_schedule)
    assert store.due_snoozes(until + timedelta(hours=1))[0].id == one.id
    assert store.skip_count(one.id) == store.refcount(one.id) == store.view_count(one.id) == 0


@pytest.mark.parametrize('poll', [60, 20])
def test_watch_loop_uses_configured_interval_without_real_sleep(tmp_path, poll):
    ticks = []
    elapsed = 0
    class Stop:
        def is_set(self):
            return len(ticks) >= 4
        def wait(self, interval):
            nonlocal elapsed
            elapsed += interval
            return False
    watcher = Watcher(str(tmp_path / 'watch.db'), AppConfig(ntfy_enabled=True, ntfy_topic='test', watch_poll_seconds=poll))
    watcher._stop = Stop()
    watcher._run_once = lambda store, now: ticks.append(elapsed)
    watcher.run()
    assert ticks == [0, poll, poll * 2, poll * 3]

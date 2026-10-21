"""Friday reminders: wall-clock timing, persistent retries and concurrent CLI writes."""

from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from todogc import config as config_module
from todogc.config import AppConfig, ConfigError, load_config
from todogc.notify import NotifyError
from todogc.review import WeeklyReview, build_review
from todogc.scheduling import latest_weekly_due
from todogc.store import Store
from todogc.watcher import Watcher

UTC = timezone.utc
FRIDAY = datetime(2026, 7, 10, 8, tzinfo=UTC)  # 17:00 in Tokyo


class Channel:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def send(self, title, message, **kwargs):
        self.calls.append((title, message, kwargs))
        if self.fail:
            raise NotifyError('Temporary failure')


def watcher(channel, **kwargs):
    obj = Watcher('unused.db', AppConfig(ntfy_enabled=True, ntfy_topic='test',
                                       watch_weekly_review_timezone='Asia/Tokyo', **kwargs))
    obj._channel = channel
    return obj


def test_defaults_and_config_environment_precedence(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    defaults = load_config(path, environ={})
    assert defaults.watch_weekly_review_enabled
    assert defaults.watch_weekly_review_day == 4 and defaults.watch_weekly_review_time == '17:00'
    assert defaults.watch_weekly_review_timezone == 'local'
    path.write_text('[watch]\nweekly_review_enabled=false\nweekly_review_day="monday"\n'
                    'weekly_review_time="09:30"\nweekly_review_timezone="UTC"\n')
    cfg = load_config(path, environ={})
    assert not cfg.watch_weekly_review_enabled and cfg.watch_weekly_review_day == 0
    assert cfg.watch_weekly_review_time == '09:30' and cfg.watch_weekly_review_timezone == 'UTC'
    env = {'TODOGC_WATCH_WEEKLY_REVIEW_ENABLED': 'true', 'TODOGC_WATCH_WEEKLY_REVIEW_DAY': 'Friday',
           'TODOGC_WATCH_WEEKLY_REVIEW_TIME': '17:00', 'TODOGC_WATCH_WEEKLY_REVIEW_TIMEZONE': 'Asia/Tokyo'}
    cfg = load_config(path, environ=env)
    assert cfg.watch_weekly_review_enabled and cfg.watch_weekly_review_day == 4
    assert cfg.watch_weekly_review_time == '17:00' and cfg.watch_weekly_review_timezone == 'Asia/Tokyo'
    monkeypatch.setattr(config_module, 'tomllib', None)
    assert load_config(path, environ=env) == cfg


@pytest.mark.parametrize(('field', 'value'), [('enabled', 'perhaps'), ('day', 'funday'), ('day', '4'),
                                              ('time', '25:00'), ('time', '5pm'), ('time', '17:60'),
                                              ('timezone', 'No/Such_Zone'), ('timezone', '')])
def test_invalid_schedule_is_a_clean_config_error(tmp_path, field, value):
    with pytest.raises(ConfigError):
        load_config(tmp_path / 'missing.toml', environ={f'TODOGC_WATCH_WEEKLY_REVIEW_{field.upper()}': value})


def test_wall_clock_boundary_custom_schedule_and_dst():
    for moment in [FRIDAY, FRIDAY + timedelta(seconds=1), FRIDAY + timedelta(days=3)]:
        assert latest_weekly_due(moment, timezone_name='Asia/Tokyo') == FRIDAY
    assert latest_weekly_due(FRIDAY - timedelta(microseconds=1), timezone_name='Asia/Tokyo') == FRIDAY - timedelta(days=7)
    before_dst = latest_weekly_due(datetime(2026, 3, 7, tzinfo=UTC), timezone_name='America/New_York')
    after_dst = latest_weekly_due(datetime(2026, 3, 14, tzinfo=UTC), timezone_name='America/New_York')
    assert before_dst == datetime(2026, 3, 6, 22, tzinfo=UTC)
    assert after_dst == datetime(2026, 3, 13, 21, tzinfo=UTC)
    assert after_dst - before_dst == timedelta(hours=167)
    custom = latest_weekly_due(datetime(2026, 7, 13, 10, tzinfo=UTC), weekday=0, clock='09:30', timezone_name='UTC')
    assert custom == datetime(2026, 7, 13, 9, 30, tzinfo=UTC)


def test_local_timezone_observes_friday_at_five(monkeypatch):
    import time
    if not hasattr(time, 'tzset'):
        pytest.skip('Native timezone changes are unavailable')
    try:
        monkeypatch.setenv('TZ', 'Asia/Tokyo')
        time.tzset()
        assert latest_weekly_due(FRIDAY) == FRIDAY
    finally:
        monkeypatch.undo()
        time.tzset()


def test_cutoff_freezes_friday_but_records_actual_generation_time(store):
    before = store.add_task('Before five', ts=FRIDAY - timedelta(hours=1))
    after = store.add_task('After five', ts=FRIDAY - timedelta(hours=1))
    exact = store.add_task('At five', ts=FRIDAY - timedelta(hours=1))
    store.complete_task(before.id, ts=FRIDAY - timedelta(seconds=1))
    store.complete_task(exact.id, ts=FRIDAY)
    store.complete_task(after.id, ts=FRIDAY + timedelta(hours=1))
    monday = FRIDAY + timedelta(days=3)
    review = build_review(store, '2026-07-10', timezone_name='Asia/Tokyo', now=monday, through=FRIDAY)
    assert review.tasks_completed == 1 and review.completions[0].title == 'Before five'
    assert review.daily == (0, 0, 0, 0, 1, 0, 0)
    assert review.through == FRIDAY.isoformat() and review.generated_at == monday.isoformat()
    assert review.through_local == '2026-07-10T17:00+09:00'
    assert review.active_now == 0  # actual current inventory, not a guessed Friday balance
    for invalid in [FRIDAY - timedelta(days=7), monday + timedelta(days=1)]:
        with pytest.raises(ValueError):
            build_review(store, '2026-07-10', timezone_name='Asia/Tokyo', now=monday, through=invalid)


def test_success_dedupes_across_restart_and_next_week_gets_one_new_snapshot(tmp_path):
    path = tmp_path / 'review.db'
    channel = Channel()
    with Store(path) as store:
        goal = store.add_task('Private goal title', is_root=True, ts=FRIDAY - timedelta(days=1))
        store.complete_task(goal.id, ts=FRIDAY - timedelta(minutes=1))
        obj = watcher(channel)
        obj._run_weekly_review(store, FRIDAY)
        assert len(channel.calls) == len(store.saved_reviews()) == 1
        title, body, options = channel.calls[0]
        assert title == 'TodoGC · Weekly review' and 'Tasks completed: 0 · Goals closed: 1' in body
        assert 'Private goal title' not in body and 'todogc review --snapshot 1' in body
        assert options['tags'] == ['calendar']
        assert not WeeklyReview.from_json(store.saved_review(1).payload).highlight_ids
        assert store.view_count(goal.id) == store.refcount(goal.id) == 0
    with Store(path) as store:
        restarted = watcher(channel)
        restarted._run_weekly_review(store, FRIDAY + timedelta(minutes=1))
        restarted._run_weekly_review(store, FRIDAY + timedelta(days=6))
        assert len(channel.calls) == len(store.saved_reviews()) == 1
        restarted._run_weekly_review(store, FRIDAY + timedelta(days=7))
        assert len(channel.calls) == len(store.saved_reviews()) == 2


def test_failed_delivery_retries_same_frozen_snapshot_after_restart(tmp_path):
    path = tmp_path / 'retry.db'
    channel = Channel(fail=True)
    with Store(path) as store:
        task = store.add_task('Work', ts=FRIDAY - timedelta(days=1))
        store.complete_task(task.id, ts=FRIDAY - timedelta(minutes=1))
        watcher(channel)._run_weekly_review(store, FRIDAY)
        original = store.saved_review(1).payload
        # Later progress must not silently change the report being retried.
        store.reopen_task(task.id, ts=FRIDAY + timedelta(seconds=1))
        store.edit_task(task.id, title='Changed later', ts=FRIDAY + timedelta(seconds=2))
    channel.fail = False
    with Store(path) as store:
        watcher(channel)._run_weekly_review(store, FRIDAY + timedelta(minutes=1))
        watcher(channel)._run_weekly_review(store, FRIDAY + timedelta(minutes=2))
        assert len(channel.calls) == 2 and channel.calls[0] == channel.calls[1]
        assert len(store.saved_reviews()) == 1 and store.saved_review(1).payload == original
        assert store.view_count(task.id) == 0


def test_offline_catchup_selects_latest_due_only_and_disabled_means_no_snapshot(store):
    channel = Channel()
    watcher(channel, watch_weekly_review_enabled=False)._run_weekly_review(store, FRIDAY)
    assert not channel.calls and not store.saved_reviews()
    monday = FRIDAY + timedelta(days=3)
    watcher(channel)._run_weekly_review(store, monday)
    report = WeeklyReview.from_json(store.saved_review(1).payload)
    assert report.week_start == '2026-07-06' and report.through == FRIDAY.isoformat()
    # Missing several weeks never produces a backlog of notifications.
    watcher(channel)._run_weekly_review(store, FRIDAY + timedelta(days=28))
    assert len(channel.calls) == 2 and len(store.saved_reviews()) == 2
    assert WeeklyReview.from_json(store.saved_review(2).payload).through == (FRIDAY + timedelta(days=28)).isoformat()


def test_weekly_hook_uses_snapshot_and_successful_digest_ack_only(store):
    channel = Channel(fail=True)
    obj = watcher(channel)
    obj._run_once(store, FRIDAY)
    assert store.watch_meta_get('last_digest_at') is None
    assert any(call[0] == 'TodoGC · Weekly review' for call in channel.calls)
    channel.fail = False
    obj._run_once(store, FRIDAY + timedelta(minutes=1))
    assert store.watch_meta_get('last_digest_at') is not None
    assert len(store.saved_reviews()) == 1


def test_schedule_snapshot_creation_is_atomic_across_connections(tmp_path):
    path = tmp_path / 'atomic.db'
    with Store(path) as store:
        payload = build_review(store, now=FRIDAY).to_json()
    barrier = threading.Barrier(2)
    ids, errors = [], []
    def save():
        try:
            with Store(path) as store:
                barrier.wait(timeout=5)
                ids.append(store.save_review(week_start='2026-07-06', generated_at=FRIDAY, payload=payload,
                                              schedule_key='weekly:test'))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=save) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors and ids == [1, 1]
    with Store(path) as store:
        assert len(store.saved_reviews()) == 1


def test_cli_can_add_while_real_watch_process_is_running(tmp_path):
    ready = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')
            ready.set()
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / 'watch.toml'
    config.write_text('[notify]\nntfy_enabled=true\nntfy_topic="local-test"\n'
                      f'ntfy_server="http://127.0.0.1:{server.server_port}"\n'
                      '[watch]\npoll_seconds=1\nweekly_review_enabled=true\n')
    db = tmp_path / 'watch.db'
    command = [sys.executable, '-m', 'todogc.cli', '--db', str(db), '--config', str(config)]
    env = {key: value for key, value in os.environ.items() if not key.startswith('TODOGC_')}
    process = None
    try:
        with (tmp_path / 'watch.log').open('w') as log:
            process = subprocess.Popen([*command, 'watch'], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
            assert ready.wait(timeout=10), (tmp_path / 'watch.log').read_text()
            assert process.poll() is None
            result = subprocess.run([*command, 'add', 'Added while watch runs', '--root'],
                                    capture_output=True, text=True, timeout=10, env=env)
            assert result.returncode == 0, result.stderr
            assert process.poll() is None
            with Store(db) as store:
                assert store.list_tasks(record_view=False)[0].title == 'Added while watch runs'
                assert store.view_count(1) == 0
            process.terminate()
            assert process.wait(timeout=10) == 0
            with Store(db) as store:
                assert store.watch_meta_get('watch_lock_pid') is None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

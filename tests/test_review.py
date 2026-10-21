"""Weekly accounting uses event history, calendar boundaries and immutable snapshots."""

import builtins
import io
import sqlite3
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import pytest

from todogc.cli import main
from todogc.collector import Collector
from todogc.heap import TouchKind
from todogc.review import WeeklyReview, build_review, format_review, week_window
from todogc.review_card import HEIGHT, WIDTH, export_review, render_png, render_svg
from todogc.store import Store


def test_calendar_weeks_use_local_midnights_and_dst(now):
    japan = week_window('this', timezone_name='Asia/Tokyo', now=now)
    assert japan.monday.isoformat() == '2026-07-06'
    assert japan.start.isoformat() == '2026-07-05T15:00:00+00:00'
    assert japan.end.isoformat() == '2026-07-12T15:00:00+00:00'
    assert japan.cutoff == now
    previous = week_window('last', timezone_name='UTC', now=now)
    assert previous.monday.isoformat() == '2026-06-29' and previous.cutoff == previous.end
    spring = week_window('2026-03-08', timezone_name='America/New_York', now=now)
    assert spring.end - spring.start == timedelta(hours=167)
    autumn = week_window('2025-11-02', timezone_name='America/New_York', now=now)
    assert autumn.end - autumn.start == timedelta(hours=169)
    for week, zone in [('bad', 'UTC'), ('2026-07-20', 'UTC'), ('this', 'No/Such_Zone')]:
        with pytest.raises(ValueError):
            week_window(week, timezone_name=zone, now=now)


def test_unique_finishes_keep_first_day_and_event_time_title_role(store, now):
    monday = week_window(now=now, timezone_name='UTC').start
    goal = store.add_task('Ship Inbox', is_root=True, ts=monday)
    child = store.add_task('Capture action', root_id=goal.id, ts=monday)
    store.complete_task(goal.id, with_subtasks=True, ts=monday + timedelta(days=1))
    store.reopen_task(goal.id, ts=monday + timedelta(days=2))
    store.reopen_task(child.id, ts=monday + timedelta(days=2))
    store.edit_task(goal.id, title='A new name', ts=monday + timedelta(days=2))
    store.set_root(goal.id, False)
    store.complete_task(child.id, ts=monday + timedelta(days=3))
    review = build_review(store, timezone_name='UTC', now=now)
    assert review.tasks_completed == review.goals_completed == 1
    assert review.daily == (0, 2, 0, 0, 0, 0, 0)
    assert review.reopened == 2
    assert [(item.title, item.is_goal) for item in review.completions] == [('Ship Inbox', True), ('Capture action', False)]
    assert not review.legacy_labels
    assert review.partial and review.through == now.isoformat()


def test_half_open_week_boundary_and_local_daily_attribution(store, now):
    window = week_window('last', timezone_name='Asia/Tokyo', now=now)
    for index, when in enumerate([window.start - timedelta(microseconds=1), window.start,
                                   window.end - timedelta(microseconds=1), window.end]):
        task = store.add_task(f'Task {index}', ts=when)
        store.complete_task(task.id, ts=when)
    review = build_review(store, 'last', timezone_name='Asia/Tokyo', now=now)
    assert [item.title for item in review.completions] == ['Task 1', 'Task 2']
    assert review.daily == (1, 0, 0, 0, 0, 0, 1)
    assert not review.partial


def test_ideas_gc_discard_and_current_inventory_are_separate(store, now):
    old = now - timedelta(days=40)
    goal = store.add_task('Goal', is_root=True, ts=old)
    idea = store.capture('Idea', ts=now - timedelta(days=2))
    store.link(idea.id, goal.id, ts=now - timedelta(days=1))
    another = store.capture('Another', ts=now - timedelta(days=2))
    store.set_root(another.id)
    # set_root uses the real clock; an explicit event keeps this test's fixed period reproducible.
    store.record(another.id, TouchKind.ORGANIZE, ts=now - timedelta(days=1))
    discarded = store.capture('Not now', ts=now - timedelta(days=2))
    store.discard_inbox(discarded.id, ts=now - timedelta(days=1))
    store.restore_task(discarded.id, ts=now - timedelta(hours=1))
    sleeping = store.add_task('Sleeping', root_id=goal.id, ts=old)
    store.snooze_task(sleeping.id, now + timedelta(days=2), ts=now - timedelta(days=1))
    orphan = store.add_task('Orphan', ts=old)
    store.collect_candidate(orphan.id, note='Release', ts=now - timedelta(hours=1))
    store.record_gc_run(started_at=now - timedelta(hours=1), mode='normal', heap_before=7, survivors=6,
                        candidates=1, collected=1, revived=0, released_pct=14.3,
                        items=[(orphan.id, 'unreachable', 'collect')])
    report = build_review(store, timezone_name='UTC', now=now)
    assert (report.captured, report.organized, report.reclaimed, report.discarded) == (3, 2, 1, 1)
    assert (report.active_now, report.snoozed_now, report.inbox_now) == (3, 1, 1)
    assert not report.completions
    assert 'Current task load' in format_review(report)


def test_reads_exports_and_noncompletion_activity_do_not_create_finishes_or_views(store, now, tmp_path):
    task = store.add_task('Work', is_root=True, ts=now - timedelta(days=1))
    store.progress(task.id, ts=now - timedelta(hours=5))
    store.edit_task(task.id, title='Clear next step', ts=now - timedelta(hours=4))
    store.snooze_task(task.id, now + timedelta(days=1), ts=now - timedelta(hours=3))
    before = store.touches(task.id)
    review = build_review(store, now=now)
    assert not review.completions and sum(review.daily) == 0
    assert review.highlight_ids == ()
    export_review(review, str(tmp_path / 'review.svg'))
    export_review(review, str(tmp_path / 'review.json'))
    assert store.touches(task.id) == before
    assert store.view_count(task.id) == 0
    assert store.refcount(task.id) == 2 and store.skip_count(task.id) == 0
    assert not store.saved_reviews() and not store.gc_runs()


def test_snapshot_freezes_titles_statistics_and_selected_highlights(tmp_path, now):
    db = tmp_path / 'snapshot.db'
    with Store(db) as store:
        task = store.add_task('Original title', is_root=True, ts=now - timedelta(days=1))
        store.complete_task(task.id, ts=now - timedelta(hours=1))
        review = build_review(store, now=now).with_highlights([task.id])
        encoded = review.to_json()
        assert WeeklyReview.from_json(encoded) == review
        snapshot_id = store.save_review(week_start=review.week_start, generated_at=now, payload=encoded)
        store.reopen_task(task.id, ts=now)
        store.edit_task(task.id, title='Changed later', ts=now)
    with Store(db) as store:
        saved = WeeklyReview.from_json(store.saved_review(snapshot_id).payload)
        assert saved == review and saved.highlights[0].title == 'Original title'
        assert render_svg(saved) == render_svg(review)
        assert store.saved_reviews()[0].id == snapshot_id
        assert store.view_count(task.id) == 0
    with pytest.raises(ValueError):
        review.with_highlights([999])
    with pytest.raises(ValueError):
        WeeklyReview.from_json('{"format_version": 99}')


def test_legacy_migration_keeps_history_and_marks_fallback_labels(tmp_path, now):
    db = tmp_path / 'v7.db'
    with Store(db) as store:
        task = store.add_task('Legacy task', ts=now - timedelta(days=1))
        store.complete_task(task.id, ts=now - timedelta(hours=1))
        store._conn.execute('DROP TRIGGER snapshot_new_touch')
        store._conn.execute('DROP TABLE touch_task_snapshot')
        store._conn.execute('DROP TABLE review_snapshot')
        store._conn.execute("UPDATE schema_meta SET value = '7' WHERE key = 'schema_version'")
    with Store(db) as store:
        assert store.schema_version == 8
        review = build_review(store, now=now)
        assert review.tasks_completed == 1 and review.legacy_labels
        assert len(store.touches(task.id)) == 2
        assert 'current task names' in format_review(review)
        new = store.add_task('New task', ts=now - timedelta(hours=1))
        store.complete_task(new.id, ts=now - timedelta(minutes=30))
        assert store.review_events(now - timedelta(hours=1), now)[-1].historical_label
    backup = (tmp_path / 'v7.db.bak').read_bytes()
    with sqlite3.connect(str(db) + '.bak') as source:
        assert source.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == '7'
        assert source.execute("SELECT count(*) FROM touch").fetchone()[0] == 2
    with Store(db):
        pass
    assert (tmp_path / 'v7.db.bak').read_bytes() == backup


def test_svg_escapes_titles_has_no_hidden_titles_and_long_text_fits(store, now):
    task = store.add_task('Private <script>alert("x")</script> & ' + 'W' * 300, ts=now - timedelta(days=1))
    store.complete_task(task.id, ts=now - timedelta(hours=1))
    review = build_review(store, now=now)
    plain = render_svg(review)
    assert b'Private' not in plain
    svg = render_svg(review.with_highlights([task.id]))
    root = ElementTree.fromstring(svg)
    assert root.attrib['width'] == '1080' and root.attrib['height'] == '1350'
    assert '<script>' not in svg.decode() and '&lt;script&gt;' in svg.decode()
    assert '…' in svg.decode()
    assert render_svg(review.with_highlights([task.id])) == svg


def test_png_dimensions_and_optional_dependency_boundary(store, now, monkeypatch):
    Image = pytest.importorskip('PIL.Image')
    review = build_review(store, now=now)
    png = render_png(review)
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (WIDTH, HEIGHT) and image.format == 'PNG'
        assert image.getpixel((0, 0)) == (20, 33, 38)
    original_import = builtins.__import__
    def without_pillow(name, *args, **kwargs):
        if name == 'PIL' or name.startswith('PIL.'):
            raise ImportError('not installed')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_pillow)
    assert render_svg(review).startswith(b'<svg')
    with pytest.raises(ValueError, match=r'todogc\[export\]'):
        render_png(review)


def test_cli_review_export_save_reload_errors_and_no_task_mutations(tmp_path, capsys):
    db = str(tmp_path / 'review.db')
    moment = datetime.now(timezone.utc) - timedelta(seconds=2)
    with Store(db) as store:
        task = store.add_task('A real finish', is_root=True, ts=moment)
        store.complete_task(task.id, ts=moment)
    def run(*args):
        return main(['--db', db, *args])
    out = tmp_path / 'week.svg'
    assert run('review', '--week', 'this', '--highlight', '1', '--export', str(out), '--save') == 0
    original = out.read_bytes()
    assert b'A real finish' in original
    assert 'Saved review #1' in capsys.readouterr().out
    with Store(db) as store:
        store.reopen_task(1)
        store.edit_task(1, title='Later title')
        before = store.touches(1)
    assert run('review', '--snapshot', '1', '--export', str(out)) == 0
    assert out.read_bytes() == original
    assert run('review', '--list') == 0
    assert '#1' in capsys.readouterr().out
    for args in [('review', '--snapshot', '999'), ('review', '--week', 'bad'),
                 ('review', '--highlight', '999'), ('review', '--export', str(tmp_path / 'bad.jpg')),
                 ('review', '--list', '--save'), ('review', '--snapshot', '1', '--timezone', 'UTC')]:
        assert run(*args) == 1
    assert run('review', '--export', str(tmp_path / 'missing' / 'review.svg')) == 1
    with Store(db) as store:
        assert len(store.saved_reviews()) == 1
        assert store.touches(1) == before and store.view_count(1) == 0
        assert not store.gc_runs()
        assert not Collector().run_on_store(store, store.root_ids()).candidates

"""Weekly review navigation, highlight selection, saved snapshots and empty weeks."""

from datetime import timedelta
from pathlib import Path

import pytest

from todogc import webapp
from todogc.review import WeeklyReview, week_window
from todogc.store import Store


@pytest.fixture
def review_app(tmp_path, monkeypatch):
    pytest.importorskip('streamlit')
    from streamlit.testing.v1 import AppTest
    db = tmp_path / 'review.db'
    monkeypatch.setenv('TODOGC_DB', str(db))
    monkeypatch.setenv('TODOGC_CONFIG', str(tmp_path / 'missing.toml'))
    monkeypatch.setenv('TODOGC_LANG', 'en')
    start = week_window('last', timezone_name='UTC').start
    with Store(db) as store:
        goal = store.add_task('Ship the release', is_root=True, ts=start)
        store.add_task('Write the tests', root_id=goal.id, ts=start)
        task = store.add_task('Finish a loose end', ts=start)
        store.capture('An idea', ts=start)
        store.complete_task(goal.id, with_subtasks=True, ts=start + timedelta(days=1))
        store.complete_task(task.id, ts=start + timedelta(days=2))
        store.reopen_task(task.id, ts=start + timedelta(days=3))
        store.complete_task(task.id, ts=start + timedelta(days=4))
    app = AppTest.from_file(str(Path(webapp.__file__))).run(timeout=20)
    assert not app.exception
    app.radio(key='page').set_value('review').run(timeout=20)
    assert not app.exception
    return app, db


def test_review_empty_week_and_completed_week_have_downloads(review_app):
    app, db = review_app
    assert [metric.value for metric in app.metric] == ['0', '0', '0']
    assert any(button.proto.label == 'Download PNG' for button in app.get('download_button'))
    assert any(button.proto.label == 'Download SVG' for button in app.get('download_button'))
    app.selectbox(key='review_week').select('last').run(timeout=20)
    app.text_input(key='review_timezone').set_value('UTC').run(timeout=20)
    assert not app.exception
    assert [metric.value for metric in app.metric] == ['2', '1', '1']
    selector = next(item for item in app.multiselect if item.key.startswith('review_highlights_'))
    assert selector.options == ['#1 Ship the release', '#2 Write the tests', '#3 Finish a loose end']
    selector.set_value([1, 2]).run(timeout=20)
    assert not app.exception
    with Store(db) as store:
        assert not store.saved_reviews()
        assert all(store.view_count(task.id) == 0 for task in store.list_tasks(record_view=False))


def test_save_selects_an_immutable_snapshot_and_return_to_tasks_works(review_app):
    app, db = review_app
    app.selectbox(key='review_week').select('last').run(timeout=20)
    selector = next(item for item in app.multiselect if item.key.startswith('review_highlights_'))
    selector.set_value([1]).run(timeout=20)
    app.button(key='review_save').click().run(timeout=20)
    assert not app.exception
    assert app.selectbox(key='review_source').value == 1
    assert any('Saved review #1' in success.value for success in app.success)
    assert app.multiselect[0].disabled and app.multiselect[0].value == [1]
    with Store(db) as store:
        payload = store.saved_review(1).payload
        saved = WeeklyReview.from_json(payload)
        assert saved.highlights[0].title == 'Ship the release'
        store.reopen_task(1)
        store.edit_task(1, title='Changed title')
        store.add_task('New task')
        before = {task.id: store.touches(task.id) for task in store.list_tasks(record_view=False)}
    app.run(timeout=20)
    assert [metric.value for metric in app.metric] == ['2', '1', '1']
    assert 'Ship the release' in app.multiselect[0].options[0]
    app.radio(key='page').set_value('tasks').run(timeout=20)
    assert not app.exception and app.button(key='tree_select_1')
    app.radio(key='page').set_value('review').run(timeout=20)
    assert not app.exception
    with Store(db) as store:
        assert store.saved_review(1).payload == payload
        assert all(store.touches(tid) == touches and store.view_count(tid) == 0 for tid, touches in before.items())


def test_custom_period_and_invalid_timezone_report_cleanly(review_app):
    app, db = review_app
    app.selectbox(key='review_week').select('custom').run(timeout=20)
    monday = week_window('last', timezone_name='UTC').monday
    app.date_input(key='review_date').set_value(monday).run(timeout=20)
    app.text_input(key='review_timezone').set_value('UTC').run(timeout=20)
    assert not app.exception and [metric.value for metric in app.metric] == ['2', '1', '1']
    app.text_input(key='review_timezone').set_value('No/Such_Zone').run(timeout=20)
    assert not app.exception and app.error
    app.text_input(key='review_timezone').set_value('Asia/Tokyo').run(timeout=20)
    assert not app.exception and not app.error
    with Store(db) as store:
        assert not store.saved_reviews()
        assert store.view_count(1) == 0

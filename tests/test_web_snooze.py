"""Exercise scheduled pauses through the actual Streamlit controls."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from todogc import webapp
from todogc.store import Store


@pytest.fixture
def snooze_app(tmp_path, monkeypatch):
    pytest.importorskip('streamlit')
    from streamlit.testing.v1 import AppTest
    db = tmp_path / 'snooze.db'
    monkeypatch.setenv('TODOGC_DB', str(db))
    monkeypatch.setenv('TODOGC_CONFIG', str(tmp_path / 'missing.toml'))
    monkeypatch.setenv('TODOGC_LANG', 'en')
    with Store(db) as store:
        store.add_task('Release', is_root=True)
        store.add_task('Work', root_id=1)
        store.add_task('Leaf', root_id=2)
        store.capture('An idea')
    app = AppTest.from_file(str(Path(webapp.__file__))).run(timeout=20)
    assert not app.exception
    return app, db


def test_web_snooze_leaf_hide_reveal_and_wake(snooze_app):
    app, db = snooze_app
    app.button(key='tree_select_3').click().run()
    app.text_input(key='snooze_time_3_duration').set_value('2h')
    app.text_input(key='snooze_note_3').set_value('Waiting for feedback')
    app.button(key='snooze_3').click().run()
    assert not app.exception
    assert not any(button.key == 'tree_select_3' for button in app.button)
    app.checkbox(key='tree_show_snoozed').check().run()
    assert 'Snoozed until' in app.button(key='tree_select_3').label
    app.button(key='tree_select_3').click().run()
    app.button(key='unsnooze_3').click().run()
    assert not app.exception
    with Store(db) as store:
        assert store.get_task(3, record_view=False).snoozed_until is None
        assert all(store.view_count(tid) == store.refcount(tid) == store.skip_count(tid) == 0 for tid in range(1, 5))
        assert 'Waiting for feedback' in store.touches(3)[-2].note


def test_web_parent_context_and_explicit_subtree(snooze_app):
    app, db = snooze_app
    app.button(key='tree_select_1').click().run()
    app.button(key='snooze_1').click().run()
    assert not app.exception
    assert 'Snoozed until' in app.button(key='tree_select_1').label
    assert app.button(key='tree_select_3')
    assert any(info.value.startswith('Snoozed until') for info in app.info)
    app.checkbox(key='snooze_subtasks_1').check()
    app.button(key='snooze_1').click().run()
    assert not any(button.key == 'tree_select_1' for button in app.button)
    app.checkbox(key='tree_show_snoozed').check().run()
    app.button(key='tree_select_1').click().run()
    app.checkbox(key='snooze_subtasks_1').check()
    app.button(key='unsnooze_1').click().run()
    assert not app.exception
    with Store(db) as store:
        assert all(not store.get_task(tid, record_view=False).is_snoozed(datetime.now(timezone.utc)) for tid in range(1, 4))


def test_web_absolute_schedule_validation_reschedule_and_passive_expiry(snooze_app):
    app, db = snooze_app
    app.button(key='tree_select_4').click().run()
    app.text_input(key='snooze_time_4_duration').set_value('bad')
    app.button(key='snooze_4').click().run()
    assert app.error
    with Store(db) as store:
        assert store.get_task(4, record_view=False).snoozed_until is None
    app.selectbox(key='snooze_mode_4').select('until').run()
    deadline = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    app.text_input(key='snooze_time_4_until').set_value(deadline.isoformat())
    app.button(key='snooze_4').click().run()
    assert not app.exception
    app.checkbox(key='tree_show_snoozed').check().run()
    app.button(key='tree_select_4').click().run()
    app.selectbox(key='snooze_mode_4').select('duration').run()
    app.text_input(key='snooze_time_4_duration').set_value('3h')
    app.button(key='snooze_4').click().run()
    with Store(db) as store:
        assert store.get_task(4, record_view=False).snoozed_until < deadline
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        store._conn.execute('UPDATE task SET snoozed_until = ? WHERE id = 4', (past.isoformat(),))
        before = store.touches(4)
    app.checkbox(key='tree_show_snoozed').uncheck().run()
    assert not app.exception
    assert app.button(key='tree_select_4')
    with Store(db) as store:
        assert store.touches(4) == before
        assert store.view_count(4) == store.refcount(4) == store.skip_count(4) == 0

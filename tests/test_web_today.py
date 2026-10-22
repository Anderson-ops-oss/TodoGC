"""Search, daily execution, and navigation through the actual Streamlit app."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from todogc import webapp
from todogc.heap import State
from todogc.store import Store


@pytest.fixture
def planning_app(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "planning.db"
    monkeypatch.setenv("TODOGC_DB", str(db))
    monkeypatch.setenv("TODOGC_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("TODOGC_LANG", "en")
    now = datetime.now(timezone.utc)
    with Store(db) as store:
        store.add_task("Release", is_root=True)
        store.add_task("Documentation", root_id=1)
        store.add_task("Write release notes", root_id=2)
        store.progress(3, "Check accessibility with a screen reader")
        store.add_task("Website", is_root=True)
        store.add_task("Write homepage", root_id=4)
        store.capture("Explore a mobile app")
        store.add_task("Waiting for feedback", root_id=2)
        store.snooze_task(7, now + timedelta(days=1), ts=now)
        store.add_task("Write archived guide", root_id=2)
        store.complete_task(8)
        store.add_task("Abandoned prototype", root_id=1)
        task = store.get_task(9, record_view=False)
        task.state = State.COLLECTED
        store.update_task(task)
    app = AppTest.from_file(str(Path(webapp.__file__))).run(timeout=20)
    assert not app.exception
    app.radio(key="page").set_value("today").run()
    assert not app.exception
    return app, db



def test_today_add_reorder_complete_and_remove_persists_across_sessions(planning_app):
    app, db = planning_app
    assert app.radio(key="page").value == "today"
    assert not any("mobile" in option or "Waiting" in option or "archived" in option or "Abandoned" in option
                   for option in app.selectbox(key="today_picker").options)
    app.selectbox(key="today_picker").select(2).run()
    app.button(key="today_add").click().run()
    app.selectbox(key="today_picker").select(3).run()
    app.button(key="today_add").click().run()
    app.button(key="today_up_3").click().run()
    with Store(db) as store:
        assert store.focus_task_ids(datetime.now().date()) == [3, 2]
    app.button(key="today_select_3").click().run()
    app.text_input(key="event_note_3").set_value("Published")
    app.button(key="done_3").click().run()
    assert not app.exception
    assert any(text.value == "✓ #3 Write release notes" for text in app.text)
    app.button(key="today_remove_2").click().run()
    from streamlit.testing.v1 import AppTest
    fresh = AppTest.from_file(str(Path(webapp.__file__))).run(timeout=20)
    assert not fresh.exception
    fresh.radio(key="page").set_value("today").run()
    assert fresh.button(key="today_select_3")
    with Store(db) as store:
        assert store.focus_task_ids(datetime.now().date()) == [3]
        assert store.touches(3)[-1].note == "Published"
        assert store.view_count(3) == 0 and store.refcount(3) == 1



def test_today_suggestions_are_explicit_and_snoozed_plan_can_be_woken(planning_app):
    app, db = planning_app
    now = datetime.now(timezone.utc)
    with Store(db) as store:
        store.focus_task(5, ts=now - timedelta(days=1))
        store.snooze_task(3, now, ts=now - timedelta(hours=1))
    app.run()
    assert app.button(key="today_suggest_5") and app.button(key="today_suggest_3")
    app.button(key="today_dismiss_5").click().run()
    app.button(key="today_suggest_3").click().run()
    app.button(key="snooze_3").click().run()
    assert not app.exception
    assert any("1 unavailable" in caption.value for caption in app.caption)
    app.button(key="unsnooze_3").click().run()
    assert not app.exception
    assert app.button(key="done_3")
    with Store(db) as store:
        assert store.focus_task_ids(now.astimezone().date()) == [3]
        assert store.view_count(3) == 0 and store.refcount(3) == 1

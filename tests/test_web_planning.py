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
    return app, db



def test_navigation_separates_daily_work_from_maintenance(planning_app):
    app, db = planning_app
    assert app.radio(key="page").options == ["Today", "Tasks", "Inbox", "Weekly review", "Maintenance"]
    assert not app.code  # no GC logs on Today
    app.radio(key="page").set_value("tasks").run()
    assert not any("[info][gc]" in code.value for code in app.code)
    assert "tree_select_6" not in {button.key for button in app.button}
    app.radio(key="page").set_value("inbox").run()
    assert app.button(key="tree_select_6")
    assert "tree_select_1" not in {button.key for button in app.button}
    app.radio(key="page").set_value("maintenance").run()
    assert not app.exception
    assert any("[info][gc]" in code.value for code in app.code)
    assert any(header.value == "Leak Inspector · doctor" for header in app.subheader)
    assert app.button(key="restore_9")
    app.radio(key="page").set_value("review").run(timeout=20)
    assert not app.exception
    with Store(db) as store:
        assert all(store.view_count(tid) == 0 for tid in range(1, 10))

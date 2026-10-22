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



def search(app):
    app.radio(key="page").set_value("tasks").run()
    app.radio(key="task_view").set_value("search").run()
    assert not app.exception



def result_ids(app):
    return {int(button.key.removeprefix("search_select_")) for button in app.button
            if button.key and button.key.startswith("search_select_")}



def test_search_combines_title_subtree_ranges_sort_and_history_without_views(planning_app):
    app, db = planning_app
    search(app)
    assert result_ids(app) == {1, 2, 3, 4, 5}
    app.text_input(key="search_text").set_value("WRITE").run()
    assert result_ids(app) == {3, 5}
    app.selectbox(key="search_root").select(2).run()
    assert result_ids(app) == {3}
    app.selectbox(key="search_scope").select("all").run()
    assert result_ids(app) == {3, 8}
    app.selectbox(key="search_sort").select("title").run()
    assert [button.key for button in app.button if button.key and button.key.startswith("search_select_")] == ["search_select_8", "search_select_3"]
    app.text_input(key="search_text").set_value("screen reader").run()
    assert result_ids(app) == set()
    app.checkbox(key="search_notes").check().run()
    assert result_ids(app) == {3}
    app.button(key="search_reset").click().run()
    assert app.text_input(key="search_text").value == ""
    assert app.selectbox(key="search_scope").value == "active"
    assert result_ids(app) == {1, 2, 3, 4, 5}
    with Store(db) as store:
        assert all(store.view_count(tid) == 0 for tid in range(1, 10))
        assert store.refcount(3) == 1



def test_search_survives_navigation_and_supports_reopen_and_restore(planning_app):
    app, db = planning_app
    search(app)
    app.selectbox(key="search_scope").select("done").run()
    assert result_ids(app) == {8}
    app.radio(key="page").set_value("review").run()
    app.radio(key="page").set_value("tasks").run()
    assert app.radio(key="task_view").value == "search"
    assert app.selectbox(key="search_scope").value == "done"
    app.button(key="tree_reopen_8").click().run()
    assert result_ids(app) == set()
    app.selectbox(key="search_scope").select("collected").run()
    assert result_ids(app) == {9}
    app.button(key="restore_9").click().run()
    assert not app.exception and result_ids(app) == set()
    with Store(db) as store:
        assert store.get_task(8, record_view=False).state is State.LIVE
        assert store.get_task(9, record_view=False).state is State.LIVE

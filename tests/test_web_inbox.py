"""Inbox triage through the actual Streamlit app, including all four outcomes."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from todogc import webapp
from todogc.heap import State
from todogc.store import Store


@pytest.fixture
def inbox_app(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "inbox.db"
    monkeypatch.setenv("TODOGC_DB", str(db))
    monkeypatch.setenv("TODOGC_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("TODOGC_LANG", "en")
    with Store(db) as store:
        store.add_task("Release goal", is_root=True)
        store.add_task("Task", root_id=1)
        store.add_task("Unlinked task")
        store.capture("Old idea", ts=datetime.now(timezone.utc) - timedelta(days=8))
        store.capture("Fresh idea")
    app = AppTest.from_file(str(Path(webapp.__file__))).run(timeout=20)
    assert not app.exception
    app.radio(key="page").set_value("inbox").run()
    assert not app.exception
    return app, db


def test_capture_selects_idea_and_review_is_not_a_gc_candidate(inbox_app):
    app, db = inbox_app
    assert app.session_state["selected_task_id"] == 4
    assert any(warning.value == "Needs review" for warning in app.warning)
    assert "Needs review" in app.button(key="tree_select_4").label
    assert any("Needs review: 1" in caption.value for caption in app.caption)
    assert not any("idea" in option for option in app.selectbox(key="inbox_parent_4").options)
    app.text_input(key="capture_title").set_value("New idea")
    next(button for button in app.button if button.label == "Capture idea").click().run()
    assert not app.exception
    assert app.session_state["selected_task_id"] == 6
    with Store(db) as store:
        assert store.get_task(6, record_view=False).in_inbox
        assert store.heap_size() == 6
        assert all(store.view_count(task.id) == 0 for task in store.list_tasks(record_view=False))
        assert store.refcount(6) == 0


@pytest.mark.parametrize("outcome", ["move", "goal", "done", "discard"])
def test_all_four_inbox_outcomes_and_recovery(inbox_app, outcome):
    app, db = inbox_app
    if outcome == "move":
        options = app.selectbox(key="inbox_parent_4").options
        assert "#1 Release goal" in options and not any("idea" in option for option in options)
        app.selectbox(key="inbox_parent_4").select(1)
        app.button(key="inbox_organize_4").click().run()
    elif outcome == "goal":
        app.button(key="inbox_root_4").click().run()
    elif outcome == "done":
        app.button(key="done_4").click().run()
    else:
        app.text_input(key="inbox_note_4").set_value("Not now")
        app.button(key="inbox_discard_4").click().run()
    assert not app.exception
    with Store(db) as store:
        task = store.get_task(4, record_view=False)
        assert store.refcount(4) == 0
        if outcome == "move":
            assert not task.in_inbox and task.root_id == 1
        elif outcome == "goal":
            assert not task.in_inbox and 4 in store.root_ids()
        else:
            assert task.in_inbox
            assert task.state is (State.DONE if outcome == "done" else State.COLLECTED)
    if outcome in {"done", "discard"}:
        app.button(key="reopen_4" if outcome == "done" else "restore_4").click().run()
        assert not app.exception
        with Store(db) as store:
            task = store.get_task(4, record_view=False)
            assert task.in_inbox and task.state is State.LIVE
            assert not task.needs_inbox_review(datetime.now(timezone.utc))


def test_capture_respects_capacity_and_completion_frees_a_slot(inbox_app):
    app, db = inbox_app
    app.number_input(key="heap_max").set_value(5).run()
    app.text_input(key="capture_title").set_value("Overflow")
    next(button for button in app.button if button.label == "Capture idea").click().run()
    assert any("full" in error.value for error in app.error)
    with Store(db) as store:
        assert len(store.list_tasks(record_view=False)) == 5
    app.button(key="done_4").click().run()
    app.text_input(key="capture_title").set_value("Fits now")
    next(button for button in app.button if button.label == "Capture idea").click().run()
    assert not app.exception
    with Store(db) as store:
        assert store.get_task(6, record_view=False).title == "Fits now"


def test_existing_unlinked_task_moves_only_when_requested(inbox_app):
    app, db = inbox_app
    app.radio(key="page").set_value("tasks").run()
    app.button(key="tree_select_3").click().run()
    with Store(db) as store:
        assert not store.get_task(3, record_view=False).in_inbox
    app.button(key="inbox_move_3").click().run()
    assert not app.exception
    assert app.button(key="inbox_discard_3")
    with Store(db) as store:
        assert store.get_task(3, record_view=False).in_inbox
        assert store.refcount(3) == store.view_count(3) == 0

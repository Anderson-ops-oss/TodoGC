"""Exercise completion and reopening through the real Streamlit app."""

import re
from pathlib import Path

import pytest

from todogc import webapp
from todogc.heap import State
from todogc.store import Store


@pytest.fixture
def completion_app(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "web.db"
    monkeypatch.setenv("TODOGC_DB", str(db))
    monkeypatch.setenv("TODOGC_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("TODOGC_LANG", "en")
    with Store(db) as store:
        store.add_task("Release goal", is_root=True)
        store.add_task("Write documentation", root_id=1)
    app = AppTest.from_file(str(Path(webapp.__file__))).run(timeout=20)
    assert not app.exception
    return app, db


def test_web_completes_child_then_reopens_with_history(completion_app):
    app, db = completion_app
    app.selectbox(key="task_manager_id").select(2).run()
    app.text_input(key="event_note_2").set_value("Published").run()
    app.button(key="done_2").click().run()
    assert not app.exception
    with Store(db) as store:
        assert store.get_task(2, record_view=False).state is State.DONE
        assert store.touches(2)[-1].note == "Published"
        assert store.heap_size() == 1
    assert app.selectbox(key="task_manager_id").value == 1
    assert app.button(key="reopen_2").label == "Reopen"
    app.button(key="reopen_2").click().run()
    assert not app.exception
    with Store(db) as store:
        assert store.get_task(2, record_view=False).state is State.LIVE
        assert store.get_task(2, record_view=False).completed_at is None
        assert [t.kind.value for t in store.touches(2)][-2:] == ["done", "reopen"]


def test_web_goal_requires_opt_in_and_reopen_parent_before_child(completion_app):
    app, db = completion_app
    app.button(key="done_1").click().run()
    assert not app.exception
    assert any("unfinished subtasks" in error.value for error in app.error)
    with Store(db) as store:
        assert store.heap_size() == 2
    app.checkbox(key="done_subtasks_1").check().run()
    app.button(key="done_1").click().run()
    assert not app.exception
    with Store(db) as store:
        assert store.heap_size() == 0 and not store.root_ids()
    app.button(key="reopen_2").click().run()
    assert any("inactive" in error.value for error in app.error)
    app.button(key="reopen_1").click().run()
    assert not app.exception
    with Store(db) as store:
        assert store.root_ids() == {1}
        assert store.get_task(2, record_view=False).state is State.DONE
    app.button(key="reopen_2").click().run()
    assert not app.exception
    with Store(db) as store:
        assert store.heap_size() == 2


def test_english_web_has_no_chinese_labels_or_generation_names(completion_app):
    app, db = completion_app
    with Store(db) as store:
        store.add_task("Improve planning", meta=True)
        store.add_task("Orphan idea")
    app.run()
    # Check rendered content, widget labels and language options, rather than source code
    # or user-authored text (which is allowed to be in any language).
    text = []
    for kind in ("title", "subheader", "markdown", "caption", "text", "code", "error", "success", "warning"):
        text.extend(str(element.value) for element in app.get(kind))
    for kind in ("selectbox", "button", "checkbox", "text_input", "number_input", "radio"):
        for element in app.get(kind):
            text.append(element.label)
            if kind in {"selectbox", "radio"}:
                text.extend(element.options)
    assert not re.search(r"[\u3400-\u9fff]", "\n".join(text))
    assert app.selectbox(key="lang").options == ["English", "Chinese"]
    app.button(key="done_1").click().run()
    assert all(not re.search(r"[\u3400-\u9fff]", error.value) for error in app.error)

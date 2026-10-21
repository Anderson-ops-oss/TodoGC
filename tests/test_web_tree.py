"""Task tree interactions through Streamlit, using isolated data and English labels."""

from pathlib import Path

import pytest

from todogc import webapp
from todogc.heap import State
from todogc.store import Store


@pytest.fixture
def tree_app(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "tree.db"
    monkeypatch.setenv("TODOGC_DB", str(db))
    monkeypatch.setenv("TODOGC_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("TODOGC_LANG", "en")
    with Store(db) as store:
        store.add_task("Release TodoGC", is_root=True)
        store.add_task("Improve Web UI", root_id=1)
        store.add_task("Finish controls", root_id=2)
        store.add_task("Build task tree", root_id=2)
        store.complete_task(3)
        store.add_task("Explore an idea")
        store.add_task("Another goal", is_root=True)
    app = AppTest.from_file(str(Path(webapp.__file__))).run(timeout=20)
    assert not app.exception
    return app, db


def test_tree_collapses_expands_and_selection_does_not_record_views(tree_app):
    app, db = tree_app
    assert "tree_select_3" not in {button.key for button in app.button}
    app.button(key="tree_select_4").click().run()
    assert app.session_state["selected_task_id"] == 4
    assert any("#1 Release TodoGC / #2 Improve Web UI / #4 Build task tree" == text.value for text in app.text)
    app.button(key="tree_toggle_2").click().run()
    assert "tree_select_4" not in {button.key for button in app.button}
    app.button(key="tree_toggle_2").click().run()
    assert app.button(key="tree_select_4")
    assert not app.exception
    with Store(db) as store:
        assert all(store.view_count(task.id) == 0 for task in store.list_tasks(record_view=False))


def test_add_subtask_uses_selected_parent_and_selects_new_task(tree_app):
    app, db = tree_app
    app.button(key="tree_select_2").click().run()
    app.text_input(key="subtask_title_2").set_value("Write tree tests")
    next(button for button in app.button if button.label == "Add subtask").click().run()
    assert not app.exception
    with Store(db) as store:
        child = store.get_task(7, record_view=False)
        assert child.title == "Write tree tests" and child.root_id == 2
    assert app.session_state["selected_task_id"] == 7
    assert app.button(key="tree_select_7")


def test_move_uses_named_parent_choices_and_excludes_self_and_descendants(tree_app):
    app, db = tree_app
    app.button(key="tree_select_2").click().run()
    parent = app.selectbox(key="parent_2")
    assert "#1 Release TodoGC" in parent.options and "#6 Another goal" in parent.options
    assert not any("Improve Web UI" in option or "Build task tree" in option or "Finish controls" in option for option in parent.options)
    parent.select(6)
    app.button(key="link_2").click().run()
    assert not app.exception
    with Store(db) as store:
        assert store.get_task(2, record_view=False).root_id == 6
        assert store.get_task(4, record_view=False).root_id == 2
    assert any(text.value == "#6 Another goal / #2 Improve Web UI" for text in app.text)


def test_completed_toggle_allows_reopening_from_tree(tree_app):
    app, db = tree_app
    app.checkbox(key="tree_show_completed").check().run()
    assert app.button(key="tree_select_3").label.startswith("✓")
    app.button(key="tree_select_3").click().run()
    assert "done_3" not in {button.key for button in app.button}
    app.button(key="tree_reopen_3").click().run()
    assert not app.exception
    with Store(db) as store:
        assert store.get_task(3, record_view=False).state is State.LIVE
    assert app.button(key="done_3")


def test_tree_completion_and_subtask_allocation_respect_heap_cap(tree_app):
    app, db = tree_app
    app.number_input(key="heap_max").set_value(5).run()
    app.button(key="tree_select_2").click().run()
    app.text_input(key="subtask_title_2").set_value("Over capacity")
    next(button for button in app.button if button.label == "Add subtask").click().run()
    assert any("full" in error.value for error in app.error)
    with Store(db) as store:
        assert len(store.list_tasks(record_view=False)) == 6
    app.button(key="tree_select_4").click().run()
    app.button(key="done_4").click().run()
    assert "tree_select_4" not in {button.key for button in app.button}
    app.button(key="tree_select_2").click().run()
    app.text_input(key="subtask_title_2").set_value("Fits after completion")
    next(button for button in app.button if button.label == "Add subtask").click().run()
    assert not app.exception
    with Store(db) as store:
        assert store.get_task(7, record_view=False).root_id == 2

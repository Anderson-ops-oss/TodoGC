"""Explicit subtree focus, including availability, stable ordering, and atomicity."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from todogc.cli import main
from todogc.heap import State
from todogc.store import Store


def plan_ids(store, now):
    return store.focus_task_ids(now.astimezone().date())


def test_focus_defaults_to_one_task_and_bulk_selects_only_available_descendants(store, now):
    store.add_task("Goal", is_root=True, ts=now)  # 1
    store.add_task("Parent", root_id=1, ts=now)  # 2
    store.add_task("Leaf", root_id=2, ts=now)  # 3
    store.add_task("Done", root_id=1, ts=now)  # 4
    store.complete_task(4, ts=now)
    store.add_task("Sleeping parent", root_id=1, ts=now)  # 5
    store.snooze_task(5, now + timedelta(days=1), ts=now)
    store.add_task("Awake child", root_id=5, ts=now)  # 6
    store.add_task("Other goal", is_root=True, ts=now)  # 7
    store.add_task("Reason only", root_id=7, ts=now)  # 8
    store.add_dep(1, 8)
    candidate = store.add_task("Candidate", root_id=2, ts=now)  # 9
    candidate.state = State.CANDIDATE
    store.update_task(candidate)
    store.capture("Legacy linked idea", ts=now)  # 10
    store._conn.execute("UPDATE task SET root_id = 1 WHERE id = 10")
    collected = store.add_task("Collected", root_id=1, ts=now)  # 11
    collected.state = State.COLLECTED
    store.update_task(collected)
    before = store.list_tasks(record_view=False)
    touches = {task.id: store.touches(task.id) for task in before}

    store.focus_task(1, ts=now)
    assert plan_ids(store, now) == [1]
    store.focus_task(1, with_subtasks=True, ts=now)
    assert plan_ids(store, now) == [1, 2, 3, 9, 6]
    assert store.list_tasks(record_view=False) == before
    assert store.root_ids() == {1, 7}
    assert {task.id: store.touches(task.id) for task in before} == touches


def test_bulk_focus_preserves_existing_order_and_position_moves_a_preorder_block(store, now):
    store.add_task("Goal", is_root=True, ts=now)  # 1
    store.add_task("Older child", root_id=1, ts=now)  # 2
    store.add_task("Newer parent", root_id=1, ts=now)  # 3
    store.link(2, 3, ts=now)
    store.add_task("Grandchild", root_id=2, ts=now)  # 4
    store.add_task("Unrelated action", ts=now)  # 5
    store.focus_task(5, ts=now)
    store.focus_task(2, ts=now)
    store.focus_task(1, with_subtasks=True, ts=now)
    assert plan_ids(store, now) == [5, 2, 1, 3, 4]
    store.focus_task(1, with_subtasks=True, ts=now)
    assert plan_ids(store, now) == [5, 2, 1, 3, 4]
    store.focus_task(1, with_subtasks=True, position=1, ts=now)
    assert plan_ids(store, now) == [1, 3, 2, 4, 5]
    store.focus_task(1, with_subtasks=True, position=2, ts=now)
    assert plan_ids(store, now) == [5, 1, 3, 2, 4]
    with pytest.raises(ValueError, match="position"):
        store.focus_task(1, with_subtasks=True, position=3, ts=now)
    assert plan_ids(store, now) == [5, 1, 3, 2, 4]


def test_existing_root_can_add_new_or_previously_dismissed_subtasks(store, now):
    store.add_task("Goal", is_root=True, ts=now)
    store.add_task("Child", root_id=1, ts=now)
    store.focus_task(1, with_subtasks=True, ts=now)
    store.unfocus_task(2, ts=now)
    store.add_task("New child", root_id=1, ts=now)
    store.focus_task(1, with_subtasks=True, ts=now)
    assert plan_ids(store, now) == [1, 2, 3]
    store.focus_task(1, with_subtasks=True, ts=now)
    assert plan_ids(store, now) == [1, 2, 3]


def test_bulk_focus_write_failure_rolls_back_every_selection(store, now):
    store.add_task("Goal", is_root=True, ts=now)
    store.add_task("Child", root_id=1, ts=now)
    store.add_task("Grandchild", root_id=2, ts=now)
    store.add_task("Already planned", ts=now)
    store.focus_task(4, ts=now)
    before = {tid: store.touches(tid) for tid in range(1, 5)}
    store._conn.execute(
        """CREATE TRIGGER reject_focus BEFORE INSERT ON daily_focus
           WHEN NEW.task_id = 3
           BEGIN SELECT RAISE(ABORT, 'simulated selection failure'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="simulated selection failure"):
        store.focus_task(1, with_subtasks=True, position=1, ts=now)
    assert plan_ids(store, now) == [4]
    assert store._conn.execute("SELECT COUNT(*) FROM daily_focus").fetchone()[0] == 1
    assert {tid: store.touches(tid) for tid in range(1, 5)} == before


@pytest.mark.parametrize("status", ["done", "collected", "snoozed", "inbox", "missing"])
def test_bulk_focus_rejects_an_unavailable_start_without_changing_the_plan(store, now, status):
    store.add_task("Starting task", ts=now)
    store.add_task("Already planned", ts=now)
    store.focus_task(2, ts=now)
    if status == "done":
        store.complete_task(1, ts=now)
    elif status == "collected":
        task = store.get_task(1, record_view=False)
        task.state = State.COLLECTED
        store.update_task(task)
    elif status == "snoozed":
        store.snooze_task(1, now + timedelta(hours=1), ts=now)
    elif status == "inbox":
        store.move_to_inbox(1, ts=now)
    before = {tid: store.touches(tid) for tid in (1, 2)}
    with pytest.raises(ValueError):
        store.focus_task(999 if status == "missing" else 1, with_subtasks=True, ts=now)
    assert plan_ids(store, now) == [2]
    assert {tid: store.touches(tid) for tid in (1, 2)} == before


def test_bulk_focus_can_start_at_a_subtask_and_terminates_on_legacy_cycles(store, now):
    store.add_task("Goal", is_root=True, ts=now)
    store.add_task("Parent", root_id=1, ts=now)
    store.add_task("Leaf", root_id=2, ts=now)
    store.add_task("Reason only", ts=now)
    store.add_dep(3, 4)
    store.focus_task(2, with_subtasks=True, ts=now)
    assert plan_ids(store, now) == [2, 3]
    store._conn.execute("UPDATE task SET root_id = 3 WHERE id = 1")
    store.focus_task(1, with_subtasks=True, position=1, ts=now)
    assert plan_ids(store, now) == [1, 2, 3]


def test_cli_subtree_focus_is_explicit_idempotent_and_read_only_for_activity(tmp_path, capsys):
    db = str(tmp_path / "subtree-focus.db")
    now = datetime.now(timezone.utc)
    with Store(db) as store:
        store.add_task("Goal", is_root=True)
        store.add_task("Parent", root_id=1)
        store.add_task("Leaf", root_id=2)
        store.add_task("Done", root_id=1)
        store.complete_task(4)
        store.add_task("Sleeping", root_id=1)
        store.snooze_task(5, now + timedelta(days=1), ts=now)
        before = {tid: store.touches(tid) for tid in range(1, 6)}

    def run(*args):
        return main(["--db", db, *args])

    assert run("focus", "1") == 0
    with Store(db) as store:
        assert plan_ids(store, now) == [1]
    capsys.readouterr()
    assert run("focus", "1", "--with-subtasks") == 0
    assert "available subtasks" in capsys.readouterr().out
    assert run("focus", "1", "--with-subtasks") == 0
    with Store(db) as store:
        assert plan_ids(store, now) == [1, 2, 3]
    assert run("focus", "2", "--with-subtasks", "--position", "1") == 0
    capsys.readouterr()
    assert run("today") == 0
    output = capsys.readouterr().out.split("Completed today")[0]
    assert output.index("#2 Parent") < output.index("#3 Leaf") < output.index("#1 Goal")
    assert "#4 Done" not in output and "#5 Sleeping" not in output
    assert run("focus", "5", "--with-subtasks") == 1
    with Store(db) as store:
        assert plan_ids(store, now) == [2, 3, 1]
        assert {tid: store.touches(tid) for tid in range(1, 6)} == before


def test_cli_bulk_selection_appears_in_web_today(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from todogc import webapp

    db = tmp_path / "shared-plan.db"
    monkeypatch.setenv("TODOGC_DB", str(db))
    monkeypatch.setenv("TODOGC_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("TODOGC_LANG", "en")
    with Store(db) as store:
        store.add_task("Goal", is_root=True)
        store.add_task("Parent", root_id=1)
        store.add_task("Leaf", root_id=2)
        store.add_task("Finished", root_id=1)
        store.complete_task(4)
    assert main(["--db", str(db), "focus", "1", "--with-subtasks"]) == 0
    app = AppTest.from_file(str(Path(webapp.__file__))).run(timeout=20)
    assert not app.exception
    selections = {button.key for button in app.button if button.key and button.key.startswith("today_select_")}
    assert selections == {"today_select_1", "today_select_2", "today_select_3"}
    with Store(db) as store:
        assert all(store.view_count(tid) == store.refcount(tid) == 0 for tid in range(1, 5))

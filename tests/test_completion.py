"""Successful completion is separate from collection, including goals, capacity and audit history."""

import sqlite3
from datetime import timedelta

import pytest

from todogc import console
from todogc.cli import main
from todogc.collector import Collector
from todogc.config import AppConfig
from todogc.heap import State, TouchKind
from todogc.report import format_doctor, heap_used_count
from todogc.store import Store
from todogc.watcher import run_tick


def test_completion_frees_heap_and_stops_gc_diagnostics_and_notifications(store, now):
    root = store.add_task("Goal", is_root=True, ts=now)
    task = store.add_task("Finished work", root_id=root.id, ts=now - timedelta(days=60))
    store.record_watch_tick(task.id, "stale", notified_at=now)
    store.progress(task.id, "Shipped", ts=now)
    assert store.complete_task(task.id, note="Released", ts=now) == [task.id]
    done = store.get_task(task.id, record_view=False)
    assert done.state is State.DONE and done.completed_at == now
    assert store.heap_size() == 1
    assert task.id not in store.watch_states()
    result = Collector().run_on_store(store, store.root_ids(), now=now)
    assert [t.id for t in result.survivors] == [root.id]
    assert not result.candidates and heap_used_count(result) == 1
    assert "Finished work" not in format_doctor(store, result, now=now)
    assert not run_tick(store, AppConfig(), now=now).events
    assert store.list_tasks(state=State.COLLECTED, record_view=False) == []
    assert [t.kind for t in store.touches(task.id)] == [TouchKind.CREATE, TouchKind.PROGRESS, TouchKind.DONE]
    assert store.touches(task.id)[-1].note == "Released"


def test_goal_completion_requires_explicit_consent_for_nested_children(store, now):
    root = store.add_task("Goal", is_root=True, ts=now)
    child = store.add_task("Child", root_id=root.id, ts=now)
    grandchild = store.add_task("Grandchild", root_id=child.id, ts=now)
    with pytest.raises(ValueError, match="unfinished subtasks.*Child.*Grandchild"):
        store.complete_task(root.id, ts=now)
    assert store.heap_size() == 3 and store.root_ids() == {root.id}
    assert all(len(store.touches(t.id)) == 1 for t in (root, child, grandchild))
    assert store.complete_task(root.id, with_subtasks=True, ts=now) == [root.id, child.id, grandchild.id]
    assert store.heap_size() == 0 and store.root_ids() == set()
    assert all(t.state is State.DONE for t in store.list_tasks(record_view=False))
    assert not Collector().run_on_store(store, store.root_ids(), now=now).candidates
    store.reopen_task(root.id, ts=now)
    assert store.root_ids() == {root.id}
    assert store.get_task(child.id, record_view=False).state is State.DONE


def test_completing_last_child_does_not_complete_goal(store, now):
    root = store.add_task("Goal", is_root=True, ts=now)
    child = store.add_task("Child", root_id=root.id, ts=now)
    store.complete_task(child.id, ts=now)
    assert store.root_ids() == {root.id}
    assert store.get_task(root.id, record_view=False).state is State.LIVE
    store.complete_task(root.id, ts=now)
    assert store.heap_size() == 0


def test_batch_completion_rolls_back_every_task_if_a_write_fails(store, now):
    root = store.add_task("Goal", is_root=True, ts=now)
    child = store.add_task("Child", root_id=root.id, ts=now)
    store._conn.execute(
        """CREATE TRIGGER reject_done BEFORE INSERT ON touch
        WHEN NEW.task_id = 2 AND NEW.kind = 'done'
        BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
        store.complete_task(root.id, with_subtasks=True, ts=now)
    assert store.heap_size() == 2
    assert all(t.state is State.LIVE and t.completed_at is None for t in store.list_tasks(record_view=False))
    assert len(store.touches(root.id)) == len(store.touches(child.id)) == 1


def test_completed_task_rejects_active_mutations(store, now):
    task = store.add_task("Task", ts=now)
    store.complete_task(task.id, ts=now)
    for action in (
        lambda: store.progress(task.id), lambda: store.defer(task.id),
        lambda: store.edit_task(task.id, title="Changed"), lambda: store.set_root(task.id),
        lambda: store.add_task("Child", root_id=task.id), lambda: store.complete_task(task.id),
    ):
        with pytest.raises(ValueError, match="completed"):
            action()
    assert store.get_task(task.id, record_view=False).title == "Task"
    assert len(store.touches(task.id)) == 2


def test_reopen_keeps_history_and_resets_skips_with_grace_for_weak_tasks(store, now):
    old = now - timedelta(days=60)
    task = store.add_task("Task", ref_strength="weak", ts=old)
    for _ in range(5):
        store.defer(task.id, ts=old)
    store.complete_task(task.id, ts=old)
    task = store.reopen_task(task.id, ts=now)
    assert task.state is State.LIVE and task.completed_at is None
    assert store.consecutive_skip_count(task.id) == 0 and store.skip_count(task.id) == 5
    assert [t.kind for t in store.touches(task.id)][-2:] == [TouchKind.DONE, TouchKind.REOPEN]
    assert store.refcount(task.id) == 0
    assert not Collector().run_on_store(store, store.root_ids(), now=now).candidates
    assert len(Collector().run_on_store(store, store.root_ids(), now=now + timedelta(days=8)).candidates) == 1


def test_reopen_validates_state_parent_and_capacity_without_partial_writes(store, now):
    root = store.add_task("Goal", is_root=True, ts=now)
    child = store.add_task("Child", root_id=root.id, ts=now)
    store.complete_task(root.id, with_subtasks=True, ts=now)
    with pytest.raises(ValueError, match="inactive"):
        store.reopen_task(child.id, ts=now)
    active = store.add_task("Other goal", is_root=True, ts=now)
    for parent in (child.id, 999):
        with pytest.raises(ValueError):
            store.reopen_task(child.id, root_id=parent, ts=now)
    with pytest.raises(ValueError, match="full"):
        store.reopen_task(child.id, root_id=active.id, heap_max=1, ts=now)
    assert store.get_task(child.id, record_view=False).state is State.DONE
    assert store.get_task(child.id, record_view=False).root_id == root.id
    assert len(store.touches(child.id)) == 2
    reopened = store.reopen_task(child.id, root_id=active.id, heap_max=1, force=True, ts=now)
    assert reopened.root_id == active.id
    with pytest.raises(ValueError, match="not completed"):
        store.reopen_task(child.id)
    with pytest.raises(ValueError, match="does not exist"):
        store.reopen_task(999)


def test_completing_meta_task_and_reopening_does_not_use_capacity(store, now):
    store.add_task("Goal", is_root=True, ts=now)
    task = store.add_task("Meta", meta=True, ts=now)
    store.complete_task(task.id, ts=now)
    assert not Collector().run_on_store(store, store.root_ids(), now=now).candidates
    store.reopen_task(task.id, heap_max=1, ts=now)
    assert store.heap_size() == 1


def test_v4_migration_preserves_data_and_backup_and_persists_completion(tmp_path, now):
    path = tmp_path / "v4.db"
    with Store(path) as store:
        task = store.add_task("Existing goal", is_root=True, ts=now)
        store.progress(task.id, "Previous work", ts=now)
        store._conn.execute("ALTER TABLE task DROP COLUMN completed_at")
        store._conn.execute("ALTER TABLE task DROP COLUMN inbox_since")
        store._conn.execute("ALTER TABLE task DROP COLUMN snoozed_until")
        store._conn.execute("ALTER TABLE task DROP COLUMN snooze_notified_until")
        store._conn.execute("UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'")
    with Store(path) as store:
        assert store.schema_version == 7
        assert store.get_task(task.id, record_view=False).completed_at is None
        store.complete_task(task.id, ts=now)
    with sqlite3.connect(str(path) + ".bak") as backup:
        assert backup.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "4"
        assert "completed_at" not in {row[1] for row in backup.execute("PRAGMA table_info(task)")}
    with Store(path) as store:
        assert store.get_task(task.id, record_view=False).completed_at == now
        assert store.touches(task.id)[1].note == "Previous work"
        store.reopen_task(task.id, ts=now)
        store.complete_task(task.id, ts=now + timedelta(days=1))
        assert [t.kind for t in store.touches(task.id)].count(TouchKind.DONE) == 2


def test_cli_completion_lists_history_errors_and_reopening(tmp_path, capsys):
    db = str(tmp_path / "cli.db")

    def run(*args):
        return main(["--db", db, *args])

    assert run("add", "Goal", "--root") == 0
    assert run("add", "Child", "--root-id", "1") == 0
    assert run("done", "1") == 1
    assert "#2 Child" in capsys.readouterr().err
    assert run("done", "2", "Shipped") == 0
    capsys.readouterr()
    assert run("ls") == 0
    assert "Child" not in capsys.readouterr().out
    assert run("completed") == 0
    assert "#2 Child" in capsys.readouterr().out
    assert run("ls", "--state", "done") == 0
    assert "Child" in capsys.readouterr().out
    assert run("show", "2") == 0
    assert "completed=" in capsys.readouterr().out
    assert run("history", "2") == 0
    assert "done · Shipped" in capsys.readouterr().out
    assert run("graveyard") == 0
    assert "Child" not in capsys.readouterr().out
    assert run("restore", "2") == 1
    assert run("reopen", "2") == 0
    assert run("done", "1", "--with-subtasks") == 0
    assert run("reopen", "2") == 1
    assert run("reopen", "1") == 0
    assert run("reopen", "2") == 0
    assert run("done", "999") == 1


@pytest.mark.parametrize("rich", [False, True])
def test_default_ls_only_records_views_for_displayed_active_tasks(tmp_path, capsys, monkeypatch, rich):
    if rich:
        pytest.importorskip("rich")
    else:
        monkeypatch.setattr(console, "_HAVE_RICH", False)
    db = str(tmp_path / "views.db")
    with Store(db) as store:
        goal = store.add_task("Completed goal", is_root=True)
        child = store.add_task("Completed child", root_id=goal.id)
        store.complete_task(goal.id, with_subtasks=True)
        active = store.add_task("Active goal", is_root=True)
        candidate = store.add_task("Active candidate", root_id=active.id)
        candidate.state = State.CANDIDATE
        store.update_task(candidate)
        collected = store.add_task("Collected task")
        collected.state = State.COLLECTED
        store.update_task(collected)
    for expected_views in (1, 2):
        assert main(["--db", db, "ls"]) == 0
        output = capsys.readouterr().out
        assert "Active goal" in output and "Active candidate" in output
        assert "Completed" not in output and "Collected task" not in output
        with Store(db) as store:
            assert store.view_count(active.id) == store.view_count(candidate.id) == expected_views
            assert store.view_count(goal.id) == store.view_count(child.id) == store.view_count(collected.id) == 0
    assert main(["--db", db, "ls", "--state", "done"]) == 0
    output = capsys.readouterr().out
    assert "Completed goal" in output and "Completed child" in output
    assert "Active goal" not in output and "Collected task" not in output
    with Store(db) as store:
        assert store.view_count(goal.id) == store.view_count(child.id) == 1
        assert store.view_count(active.id) == store.view_count(candidate.id) == 2
        assert store.view_count(collected.id) == 0

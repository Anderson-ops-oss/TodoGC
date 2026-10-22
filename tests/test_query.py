"""Search semantics, shared filters, and views after filtering (never before)."""

from datetime import timedelta

import pytest

from todogc import console
from todogc.cli import main
from todogc.heap import State
from todogc.query import TaskQuery, query_tasks
from todogc.store import Store


def ids(tasks):
    return [task.id for task in tasks]


def test_literal_unicode_search_and_optional_history_notes(store, now):
    task = store.add_task("Straße 100%_ready", ts=now)
    other = store.add_task("Another task", ts=now)
    store.progress(other.id, "Waiting for STRASSE approval", ts=now)
    assert ids(query_tasks(store, TaskQuery(text=" STRASSE "))) == [task.id]
    assert ids(query_tasks(store, TaskQuery(text="strasse", notes=True))) == [task.id, other.id]
    assert ids(query_tasks(store, TaskQuery(text="%_"))) == [task.id]
    assert query_tasks(store, TaskQuery(text="' OR 1=1 --")) == []
    assert all(store.view_count(tid) == 0 for tid in (task.id, other.id))


def test_ranges_and_subtree_include_nested_children_but_not_reason_edges(store, now):
    root = store.add_task("Goal", is_root=True, ts=now)
    child = store.add_task("Child", root_id=root.id, ts=now)
    leaf = store.add_task("Leaf", root_id=child.id, ts=now)
    store.add_task("Reason only", ts=now)
    store.add_dep(4, root.id)
    store.complete_task(leaf.id, ts=now)
    idea = store.capture("Idea", ts=now)
    sleeping = store.add_task("Sleep", root_id=child.id, ts=now)
    store.snooze_task(sleeping.id, now + timedelta(hours=1), ts=now)
    assert ids(query_tasks(store, TaskQuery(root_id=root.id), now=now)) == [root.id, child.id]
    assert ids(query_tasks(store, TaskQuery(root_id=child.id, scope="all"), now=now)) == [child.id, leaf.id, sleeping.id]
    assert ids(query_tasks(store, TaskQuery(scope="done"), now=now)) == [leaf.id]
    assert ids(query_tasks(store, TaskQuery(scope="inbox"), now=now)) == [idea.id]
    assert ids(query_tasks(store, TaskQuery(scope="snoozed"), now=now)) == [sleeping.id]
    assert sleeping.id in ids(query_tasks(store, TaskQuery(), now=now + timedelta(hours=1)))
    with pytest.raises(ValueError, match="does not exist"):
        query_tasks(store, TaskQuery(root_id=999))
    # Legacy containment cycles terminate and still include each node exactly once.
    store._conn.execute("UPDATE task SET root_id = ? WHERE id = ?", (child.id, root.id))
    assert ids(query_tasks(store, TaskQuery(root_id=root.id, scope="all"), now=now)) == [root.id, child.id, leaf.id, sleeping.id]


def test_sorting_has_stable_ties_and_views_never_change_updated_order(store, now):
    a = store.add_task("Zebra", ts=now - timedelta(days=2))
    b = store.add_task("alpha", ts=now - timedelta(days=1))
    c = store.add_task("Alpha", ts=now - timedelta(days=1))
    store.progress(a.id, ts=now)
    store.get_task(b.id, ts=now + timedelta(hours=1))
    assert ids(query_tasks(store, TaskQuery(sort="newest", limit=2))) == [c.id, b.id]
    assert ids(query_tasks(store, TaskQuery(sort="oldest"))) == [a.id, b.id, c.id]
    assert ids(query_tasks(store, TaskQuery(sort="title"))) == [b.id, c.id, a.id]
    assert ids(query_tasks(store, TaskQuery(sort="updated"))) == [a.id, c.id, b.id]


@pytest.mark.parametrize("rich", [True, False])
def test_cli_filter_limit_views_and_read_only_global_search(tmp_path, capsys, monkeypatch, rich):
    if rich:
        pytest.importorskip("rich")
    else:
        monkeypatch.setattr(console, "_HAVE_RICH", False)
    db = str(tmp_path / "query.db")
    with Store(db) as store:
        store.add_task("Root", is_root=True)
        store.add_task("Publish alpha", root_id=1)
        store.add_task("Publish beta", root_id=1)
        store.add_task("Publish finished", root_id=1)
        store.complete_task(4)
    assert main(["--db", db, "ls", "-q", "publish", "--root-id", "1", "--sort", "title", "--limit", "1"]) == 0
    output = capsys.readouterr().out
    assert "Publish alpha" in output and "Publish beta" not in output and "finished" not in output
    assert main(["--db", db, "search", "PUBLISH"]) == 0
    output = capsys.readouterr().out
    assert all(word in output for word in ("alpha", "beta", "finished"))
    with Store(db) as store:
        assert [store.view_count(tid) for tid in range(1, 5)] == [0, 1, 0, 0]
    assert main(["--db", db, "search", "publish", "--scope", "done"]) == 0
    assert "finished" in capsys.readouterr().out
    assert main(["--db", db, "ls", "--root-id", "999"]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_candidate_filter_preserves_computed_gc_semantics(tmp_path, capsys):
    db = str(tmp_path / "candidate.db")
    with Store(db) as store:
        store.add_task("Goal", is_root=True)
        store.add_task("Orphan")
        store.add_task("Other orphan")
    assert main(["--db", db, "ls", "--state", "candidate", "-q", "Other"]) == 0
    output = capsys.readouterr().out
    assert "Other orphan" in output and "Goal" not in output
    with Store(db) as store:
        assert all(task.state is State.LIVE and store.view_count(task.id) == 0 for task in store.list_tasks(record_view=False))

"""Containment trees, including deep nesting and safe handling of legacy data."""

import pytest

from todogc.cli import main
from todogc.heap import State, Task
from todogc.store import Store
from todogc.tree import build_task_tree


def test_tree_groups_paths_and_collapse_keep_every_task_once(store):
    goal = store.add_task("Release", is_root=True)
    child = store.add_task("Web UI", root_id=goal.id)
    grandchild = store.add_task("Task tree", root_id=child.id)
    unlinked = store.add_task("An idea")
    meta = store.add_task("Plan better", meta=True)
    store.add_dep(goal.id, unlinked.id)
    tree = build_task_tree(store.list_tasks(record_view=False), store.root_ids())
    assert tree.groups == {"goals": [goal.id], "unlinked": [unlinked.id], "meta": [meta.id]}
    assert [(t.id, depth) for t, depth, _ in tree.rows("goals", set())] == [(goal.id, 0), (child.id, 1), (grandchild.id, 2)]
    assert [t.id for t, _, _ in tree.rows("goals", {child.id})] == [goal.id, child.id]
    assert [t.id for t, _, _ in tree.rows("goals", {goal.id})] == [goal.id]
    assert [t.title for t in tree.path(grandchild.id)] == ["Release", "Web UI", "Task tree"]
    assert all(store.view_count(tid) == 0 for tid in tree.tasks)


def test_completed_visibility_preserves_goal_identity_and_excludes_collected(store):
    root = store.add_task("Goal", is_root=True)
    child = store.add_task("Child", root_id=root.id)
    collected = store.add_task("Discarded")
    collected.state = State.COLLECTED
    store.update_task(collected)
    store.complete_task(root.id, with_subtasks=True)
    roots = store.root_ids(active_only=False)
    tasks = store.list_tasks(record_view=False)
    assert not build_task_tree(tasks, roots).tasks
    tree = build_task_tree(tasks, roots, show_completed=True)
    assert tree.groups["goals"] == [root.id]
    assert set(tree.tasks) == {root.id, child.id}
    assert tree.parents[child.id] == root.id


def test_children_of_unavailable_parents_remain_discoverable(store):
    parent = store.add_task("Gone")
    child = store.add_task("Still active", root_id=parent.id)
    parent.state = State.COLLECTED
    store.update_task(parent)
    tree = build_task_tree(store.list_tasks(record_view=False), store.root_ids())
    assert tree.groups["unlinked"] == [child.id]
    assert [t.id for t in tree.path(child.id)] == [child.id]


def test_legacy_parent_cycles_are_displayed_once_without_mutating_data(store):
    a = store.add_task("A")
    b = store.add_task("B", root_id=a.id)
    c = store.add_task("C", root_id=b.id)
    store._conn.execute("UPDATE task SET root_id = ? WHERE id = ?", (c.id, a.id))
    store._conn.commit()
    tree = build_task_tree(store.list_tasks(record_view=False), store.root_ids())
    assert tree.repaired_cycles
    assert [t.id for t, _, _ in tree.rows("unlinked", set())] == [a.id, b.id, c.id]
    assert [t.id for t in tree.path(c.id)] == [a.id, b.id, c.id]
    assert store.get_task(a.id, record_view=False).root_id == c.id
    store.unlink(a.id)
    assert not build_task_tree(store.list_tasks(record_view=False), store.root_ids()).repaired_cycles


def test_deep_tree_does_not_depend_on_recursion_limit():
    tasks = [Task(f"Task {tid}", id=tid, root_id=tid - 1 if tid > 1 else None) for tid in range(1, 3001)]
    tree = build_task_tree(tasks, {1})
    assert len(list(tree.rows("goals", set()))) == 3000
    assert len(tree.path(3000)) == 3000


def test_move_rejects_descendant_parent_without_partial_writes(store):
    a = store.add_task("A", is_root=True)
    b = store.add_task("B", root_id=a.id)
    c = store.add_task("C", root_id=b.id)
    before = store.touches(a.id)
    with pytest.raises(ValueError, match="descendants"):
        store.link(a.id, c.id)
    assert store.get_task(a.id, record_view=False).root_id is None
    assert store.touches(a.id) == before
    # A closed intermediate task must not hide a descendant from validation.
    b.state = State.COLLECTED
    store.update_task(b)
    with pytest.raises(ValueError, match="descendants"):
        store.link(a.id, c.id)


def test_nested_roots_follow_their_actual_parent_link(store):
    a = store.add_task("A", is_root=True)
    b = store.add_task("B", is_root=True)
    store.link(b.id, a.id)
    tree = build_task_tree(store.list_tasks(record_view=False), store.root_ids())
    assert tree.groups["goals"] == [a.id]
    assert [task.id for task in tree.path(b.id)] == [a.id, b.id]
    assert store.root_ids() == {a.id, b.id}


def test_cli_link_and_restore_cannot_introduce_parent_cycles(tmp_path, capsys):
    db = str(tmp_path / "tree.db")
    with Store(db) as store:
        a = store.add_task("A", is_root=True)
        store.add_task("B", root_id=a.id)
    assert main(["--db", db, "link", "1", "2"]) == 1
    assert "descendants" in capsys.readouterr().err
    with Store(db) as store:
        a.state = State.COLLECTED
        store.update_task(a)
    assert main(["--db", db, "restore", "1", "--root-id", "2"]) == 1
    with Store(db) as store:
        assert store.get_task(1, record_view=False).state is State.COLLECTED

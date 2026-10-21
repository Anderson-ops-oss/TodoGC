"""Terminal hierarchy shares the Web tree and never records task views."""

from todogc.cli import main
from todogc.heap import State
from todogc.store import Store


def test_terminal_tree_hierarchy_completion_and_read_only(tmp_path, capsys):
    db = str(tmp_path / "tree.db")
    with Store(db) as store:
        goal = store.add_task("Release", is_root=True)
        child = store.add_task("Web UI", root_id=goal.id)
        store.add_task("Write tests", root_id=child.id)
        done = store.add_task("Already shipped", root_id=goal.id)
        store.complete_task(done.id)
        collected = store.add_task("Discarded")
        collected.state = State.COLLECTED
        store.update_task(collected)
        orphan = store.add_task("Unlinked work")
        store.add_task("Reflect", meta=True)
        store.add_dep(goal.id, orphan.id)
        before = {task.id: store.touches(task.id) for task in store.list_tasks(record_view=False)}
    assert main(["--db", db, "tree"]) == 0
    output = capsys.readouterr().out
    assert "└── ★ #1 Release\n    └── ○ #2 Web UI\n        └── ○ #3 Write tests" in output
    assert "Unlinked\n└── ○ #6 Unlinked work" in output
    assert "⊛ #7 Reflect" in output
    assert "Already shipped" not in output and "Discarded" not in output
    assert main(["--db", db, "tree", "--show-completed"]) == 0
    output = capsys.readouterr().out
    assert "✓ #4 Already shipped" in output and "Discarded" not in output
    with Store(db) as store:
        assert all(store.view_count(tid) == 0 and store.touches(tid) == touches for tid, touches in before.items())


def test_terminal_tree_empty_and_legacy_cycle_are_safe(tmp_path, capsys):
    db = str(tmp_path / "tree.db")
    assert main(["--db", db, "tree"]) == 0
    assert "No visible tasks" in capsys.readouterr().out
    with Store(db) as store:
        a = store.add_task("First")
        b = store.add_task("Second", root_id=a.id)
        store._conn.execute("UPDATE task SET root_id = ? WHERE id = ?", (b.id, a.id))
        store._conn.commit()
    assert main(["--db", db, "tree"]) == 0
    output = capsys.readouterr().out
    assert "cycle" in output and output.count("#1 First") == output.count("#2 Second") == 1
    with Store(db) as store:
        assert store.get_task(a.id, record_view=False).root_id == b.id

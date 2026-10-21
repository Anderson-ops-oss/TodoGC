"""Inbox location stays independent of completion, GC, capacity and task history."""

import sqlite3
from datetime import timedelta

import pytest

from todogc import console, report
from todogc.cli import main, run_ceremony
from todogc.collector import Collector, Reason
from todogc.config import AppConfig
from todogc.heap import State, TouchKind
from todogc.store import Store
from todogc.tree import build_task_tree, format_task_tree
from todogc.watcher import run_tick


def test_capture_stays_out_of_gc_and_diagnostics_but_counts_toward_capacity(store, now):
    idea = store.capture("Explore an extension", ts=now - timedelta(days=60))
    assert idea.in_inbox and idea.inbox_days(now) == 60 and idea.needs_inbox_review(now)
    assert store.heap_size() == 1 and not store.root_ids()
    result = Collector().run_on_store(store, store.root_ids(), now=now)
    assert not result.survivors and not result.candidates
    assert [task.id for task in result.inbox] == [idea.id]
    assert report.heap_used_count(result) == 1 and result.released_pct == 0
    assert "1 -> 1 tasks" in report.format_gc_log(result)
    assert "-Xmx usage: 1 / 50" in report.format_heap_map(result, set(), heap_max=50)
    assert "Inbox: 1" in report.format_heap_map(result, set())
    assert "Explore an extension" not in report.format_doctor(store, result, now=now)
    assert not run_tick(store, AppConfig(), now=now).events
    assert store.refcount(idea.id) == store.view_count(idea.id) == 0
    assert [touch.kind for touch in store.touches(idea.id)] == [TouchKind.CAPTURE]


def test_review_boundary_and_future_capture_time(store, now):
    idea = store.capture("Idea", ts=now)
    assert not idea.needs_inbox_review(now + timedelta(days=6, hours=23))
    assert idea.needs_inbox_review(now + timedelta(days=7))
    assert idea.inbox_days(now - timedelta(days=1)) == 0
    store.complete_task(idea.id, ts=now)
    assert not store.get_task(idea.id, record_view=False).needs_inbox_review(now + timedelta(days=20))


def test_organizing_an_old_idea_resets_skips_without_claiming_progress(store, now):
    goal = store.add_task("Goal", is_root=True, ts=now)
    task = store.add_task("Idea", ts=now - timedelta(days=60))
    for _ in range(5):
        store.defer(task.id, ts=now - timedelta(days=8))
    store.record_watch_tick(task.id, "unreachable", notified_at=now)
    store.move_to_inbox(task.id, ts=now - timedelta(days=8))
    assert task.id not in store.watch_states()
    store.link(task.id, goal.id, ts=now)
    organized = store.get_task(task.id, record_view=False)
    assert not organized.in_inbox and organized.root_id == goal.id
    assert store.refcount(task.id) == 0 and store.skip_count(task.id) == 5
    assert store.consecutive_skip_count(task.id) == 0
    result = Collector().run_on_store(store, store.root_ids(), now=now)
    assert not result.candidates and not result.inbox
    assert store.touches(task.id)[-1].kind is TouchKind.ORGANIZE


def test_make_goal_leaves_inbox_without_changing_capacity_or_progress(store, now):
    idea = store.capture("A goal", ts=now)
    store.set_root(idea.id)
    assert store.root_ids() == {idea.id}
    assert not store.get_task(idea.id, record_view=False).in_inbox
    assert store.heap_size() == 1 and store.refcount(idea.id) == 0
    assert [touch.kind for touch in store.touches(idea.id)] == [TouchKind.CAPTURE, TouchKind.ORGANIZE]


def test_capture_and_restore_enforce_capacity_without_partial_writes(store, now):
    idea = store.capture("Idea", heap_max=1, ts=now)
    with pytest.raises(ValueError, match="full"):
        store.capture("Too many", heap_max=1, ts=now)
    assert len(store.list_tasks(record_view=False)) == 1
    store.discard_inbox(idea.id, ts=now)
    store.add_task("Goal", is_root=True, ts=now)
    with pytest.raises(ValueError, match="full"):
        store.restore_task(idea.id, heap_max=1, ts=now)
    assert store.get_task(idea.id, record_view=False).state is State.COLLECTED
    assert store.touches(idea.id)[-1].kind is TouchKind.COLLECT
    store.restore_task(idea.id, heap_max=1, force=True, ts=now)
    assert store.heap_size() == 2


def test_inbox_requires_explicit_move_and_standalone_tasks(store, now):
    root = store.add_task("Goal", is_root=True, ts=now)
    child = store.add_task("Child", root_id=root.id, ts=now)
    meta = store.add_task("Meta", meta=True, ts=now)
    unlinked = store.add_task("Unlinked", ts=now)
    assert not unlinked.in_inbox
    for task in (root, child, meta):
        before = store.touches(task.id)
        with pytest.raises(ValueError, match="standalone"):
            store.move_to_inbox(task.id, ts=now)
        assert store.touches(task.id) == before
    store.move_to_inbox(unlinked.id, ts=now)
    with pytest.raises(ValueError, match="already in Inbox"):
        store.move_to_inbox(unlinked.id)
    with pytest.raises(ValueError, match="standalone"):
        store.add_task("Invalid", inbox=True, is_root=True)


def test_pending_ideas_cannot_be_parents_or_bridge_reachability(store, now):
    goal = store.add_task("Goal", is_root=True, ts=now)
    idea = store.capture("Idea", ts=now)
    orphan = store.add_task("Orphan", ts=now)
    with pytest.raises(ValueError, match="Organize"):
        store.add_task("Invalid child", root_id=idea.id)
    with pytest.raises(ValueError, match="Organize"):
        store.link(orphan.id, idea.id)
    store.add_dep(goal.id, idea.id)
    store.add_dep(idea.id, orphan.id)
    result = Collector().run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)
    assert [(candidate.task.id, candidate.reason) for candidate in result.candidates] == [(orphan.id, Reason.UNREACHABLE)]
    assert result.released_pct == 33.3


def test_discard_and_restore_return_idea_to_inbox_with_history(store, now):
    idea = store.capture("Idea", ts=now - timedelta(days=20))
    store.discard_inbox(idea.id, note="Not for now", ts=now - timedelta(days=1))
    assert store.heap_size() == 0 and not store.gc_runs()
    assert store.get_task(idea.id, record_view=False).state is State.COLLECTED
    restored = store.restore_task(idea.id, ts=now)
    assert restored.in_inbox and restored.inbox_since == now and restored.state is State.LIVE
    assert not restored.needs_inbox_review(now)
    assert [touch.kind for touch in store.touches(idea.id)] == [TouchKind.CAPTURE, TouchKind.COLLECT, TouchKind.RESTORE]
    assert store.touches(idea.id)[1].note == "Not for now"
    assert store.refcount(idea.id) == store.view_count(idea.id) == 0


def test_done_and_reopen_preserve_inbox_origin_or_allow_explicit_parent(store, now):
    goal = store.add_task("Goal", is_root=True, ts=now)
    idea = store.capture("Small task", ts=now - timedelta(days=10))
    store.complete_task(idea.id, ts=now - timedelta(days=1))
    reopened = store.reopen_task(idea.id, ts=now)
    assert reopened.in_inbox and reopened.inbox_since == now and reopened.completed_at is None
    store.complete_task(idea.id, ts=now)
    reopened = store.reopen_task(idea.id, root_id=goal.id, ts=now)
    assert not reopened.in_inbox and reopened.root_id == goal.id
    assert store.refcount(idea.id) == 0


def test_restore_can_organize_discarded_idea_under_a_goal(store, now):
    goal = store.add_task("Goal", is_root=True, ts=now)
    idea = store.capture("Idea", ts=now)
    store.discard_inbox(idea.id, ts=now)
    restored = store.restore_task(idea.id, root_id=goal.id, ts=now)
    assert not restored.in_inbox and restored.root_id == goal.id


def test_inbox_still_triggers_heap_capacity_alerts(store, now):
    for i in range(4):
        store.capture(f"Idea {i}", ts=now)
    tick = run_tick(store, AppConfig(heap_max=5), now=now)
    assert not tick.events and tick.candidate_count == 0
    assert tick.heap_size == 4 and tick.heap_alarm


def test_v5_migration_preserves_existing_locations_and_completion(tmp_path, now):
    path = tmp_path / "v5.db"
    with Store(path) as store:
        goal = store.add_task("Goal", is_root=True, ts=now)
        orphan = store.add_task("Existing unlinked task", ts=now)
        done = store.add_task("Done", ts=now)
        store.complete_task(done.id, ts=now)
        store._conn.execute("DROP TRIGGER snapshot_new_touch")
        store._conn.execute("DROP TABLE touch_task_snapshot")
        store._conn.execute("DROP TABLE review_snapshot")
        store._conn.execute("ALTER TABLE task DROP COLUMN inbox_since")
        store._conn.execute("ALTER TABLE task DROP COLUMN snoozed_until")
        store._conn.execute("ALTER TABLE task DROP COLUMN snooze_notified_until")
        store._conn.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")
    with Store(path) as store:
        assert store.schema_version == 8
        assert all(not task.in_inbox for task in store.list_tasks(record_view=False))
        assert store.get_task(done.id, record_view=False).completed_at == now
        result = Collector().run_on_store(store, store.root_ids(), now=now)
        assert [candidate.task.id for candidate in result.candidates] == [orphan.id]
        assert store.root_ids() == {goal.id}
    backup = (tmp_path / "v5.db.bak").read_bytes()
    with sqlite3.connect(str(path) + ".bak") as original:
        assert original.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "5"
        assert "inbox_since" not in {row[1] for row in original.execute("PRAGMA table_info(task)")}
    with Store(path):
        pass
    assert (tmp_path / "v5.db.bak").read_bytes() == backup


def test_terminal_tree_and_inbox_browsing_never_count_views(tmp_path, capsys, monkeypatch, now):
    monkeypatch.setattr(console, "_HAVE_RICH", False)
    db = str(tmp_path / "cli.db")
    with Store(db) as store:
        goal = store.add_task("Release", is_root=True, ts=now)
        child = store.add_task("Web UI", root_id=goal.id, ts=now)
        leaf = store.add_task("Write tests", root_id=child.id, ts=now)
        idea = store.capture("Explore an extension", ts=now - timedelta(days=8))
        done = store.add_task("Shipped", root_id=goal.id, ts=now)
        store.complete_task(done.id, ts=now)
        tree = build_task_tree(store.list_tasks(record_view=False), store.root_ids())
        assert tree.groups["inbox"] == [idea.id]
        output = format_task_tree(store.list_tasks(record_view=False), store.root_ids(), now=now)
        assert "└── ★ #1 Release\n    └── ○ #2 Web UI\n        └── ○ #3 Write tests" in output
    for command in (["inbox"], ["tree"], ["tree", "--show-completed"], ["heap"], ["gc", "--dry-run"]):
        assert main(["--db", db, *command]) == 0
    output = capsys.readouterr().out
    assert "Needs review" in output and "✓ #5 Shipped" in output
    with Store(db) as store:
        assert all(store.view_count(task.id) == 0 for task in store.list_tasks(record_view=False))
    assert main(["--db", db, "ls"]) == 0
    assert "Explore an extension" not in capsys.readouterr().out
    with Store(db) as store:
        assert store.view_count(idea.id) == 0 and store.view_count(leaf.id) == 1


def test_cli_capture_organize_complete_discard_restore_and_explicit_move(tmp_path, capsys):
    db = str(tmp_path / "commands.db")

    def run(*args):
        return main(["--db", db, *args])

    assert run("add", "Goal", "--root") == 0
    assert run("capture", "Small idea") == 0
    assert run("done", "2") == 0
    assert run("reopen", "2") == 0
    assert run("discard", "2", "Not now") == 0
    assert run("graveyard") == 0
    assert "Not now" in capsys.readouterr().out
    assert run("restore", "2") == 0
    assert run("link", "2", "1") == 0
    assert run("capture", "Big idea") == 0
    assert run("root", "3") == 0
    assert run("add", "Unlinked") == 0
    assert run("inbox", "--move", "4") == 0
    assert run("discard", "1") == 1
    with Store(db) as store:
        assert store.root_ids() == {1, 3}
        assert not store.get_task(2, record_view=False).in_inbox
        assert store.get_task(4, record_view=False).in_inbox
    assert main(["--db", db, "--heap-max", "4", "capture", "Overflow"]) == 1
    assert main(["--db", db, "--heap-max", "4", "capture", "Explicit overflow", "--force"]) == 0


def test_gc_audit_keeps_inbox_in_retained_count(tmp_path):
    db = str(tmp_path / "gc.db")
    with Store(db) as store:
        store.capture("Idea")
    assert main(["--db", db, "gc"]) == 0
    with Store(db) as store:
        assert store.gc_runs()[0].survivors == 1
        assert store.gc_runs()[0].candidates == 0


@pytest.mark.parametrize("stage", ["decision", "eulogy"])
def test_stale_gc_snapshot_cannot_collect_a_task_moved_to_inbox(store, now, stage):
    task = store.add_task("An idea", ts=now)
    result = Collector().run_on_store(store, store.root_ids(), now=now)

    def decide(_):
        if stage == "decision":
            store.move_to_inbox(task.id, ts=now)
        return "collect"

    def eulogize(_):
        store.move_to_inbox(task.id, ts=now)
        return "Stale decision"

    outcome = run_ceremony(store, result, decide=decide, eulogize=eulogize, now=now)
    assert not outcome.collected and outcome.skipped == [task.id]
    current = store.get_task(task.id, record_view=False)
    assert current.in_inbox and current.state is State.LIVE
    assert not any(touch.kind is TouchKind.COLLECT for touch in store.touches(task.id))

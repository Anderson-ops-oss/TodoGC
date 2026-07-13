"""对抗测试：恶意/异常输入、深图、状态错乱与 HTML 注入。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

import todogc.webapp as webapp
from todogc.cli import main
from todogc.collector import Collector, Reason, TaskMetrics
from todogc.heap import State, Task
from todogc.report import HeapRow, RowKind
from todogc.store import Store
from todogc.webapp import _row_markup


def test_invalid_relationships_fail_without_partial_writes(store, now):
    a = store.add_task("A", ts=now)
    with pytest.raises(ValueError):
        store.add_task("bad parent", root_id=999, ts=now)
    assert len(store.list_tasks(record_view=False)) == 1
    with pytest.raises(ValueError):
        store.link(a.id, a.id, ts=now)
    with pytest.raises(ValueError):
        store.set_root(999)
    assert store.get_task(a.id, record_view=False).root_id is None


def test_collected_task_rejects_progress_and_live_restore(tmp_path, capsys, now):
    db = str(tmp_path / "states.db")
    with Store(db) as store:
        task = store.add_task("A", ts=now)
        task.state = State.COLLECTED
        store.update_task(task)
    assert main(["--db", db, "progress", "1"]) == 1
    assert main(["--db", db, "restore", "1", "--root-id", "999"]) == 1
    with Store(db) as store:
        assert store.get_task(1, record_view=False).state is State.COLLECTED
    with Store(db) as store:
        task = store.get_task(1, record_view=False)
        task.state = State.LIVE
        store.update_task(task)
    assert main(["--db", db, "restore", "1"]) == 1
    assert "has not been collected" in capsys.readouterr().err


def test_cli_rejects_bad_config_and_nonpositive_limits(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[heap]\nmax=0\n", encoding="utf-8")
    assert main(["--config", str(bad), "--db", str(tmp_path / "x.db"), "ls"]) == 2
    assert "config error" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--heap-max", "0", "ls"])


def test_iterative_tarjan_handles_deep_cycle_without_recursion():
    size = 3000
    tasks = {i: Task(title=f"t{i}", id=i) for i in range(size)}
    edges = {i: {(i + 1) % size} for i in range(size)}
    result = Collector().collect(tasks, set(), edges, lambda _: TaskMetrics(0, 0, None))
    assert len(result.cycles) == 1
    assert len(result.cycles[0].task_ids) == size
    assert all(c.reason is Reason.CYCLE for c in result.candidates)


def test_html_title_is_escaped_in_web_markup():
    task = Task(title='<script>alert("x")</script>', id=7)
    markup = _row_markup(HeapRow(task=task, kind=RowKind.NORMAL))
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_defer_does_not_refresh_effective_activity(store, now):
    old = now - timedelta(days=60)
    root = store.add_task("G", is_root=True, ts=old)
    task = store.add_task("T", root_id=root.id, ts=old)
    store.defer(task.id, "继续逃避", ts=now)
    result = Collector().run_on_store(store, store.root_ids(), now=now)
    by_id = {c.task.id: c.reason for c in result.candidates}
    assert by_id[task.id] is Reason.STALE


def test_streamlit_app_renders_without_runtime_exception(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("TODOGC_DB", str(tmp_path / "web.db"))
    monkeypatch.setenv("TODOGC_CONFIG", str(tmp_path / "missing.toml"))
    app = AppTest.from_file(str(Path(webapp.__file__))).run(timeout=15)
    assert not app.exception

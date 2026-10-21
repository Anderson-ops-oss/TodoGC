"""v0.2 features: migration, reference strength, policy, consecutive skips, config, the new CLI, and stats."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

import todogc.config as config_module
from todogc import report
from todogc.cli import main
from todogc.collector import Collector, GCPolicy, Reason
from todogc.config import ConfigError, load_config
from todogc.heap import ReferenceStrength, State, TouchKind
from todogc.store import Store


def _old_database(path, now) -> None:
    """Create a v1 database with no schema_meta/ref_strength."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE task (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, root_id INTEGER,
            is_root INTEGER NOT NULL DEFAULT 0, gen TEXT NOT NULL DEFAULT 'eden',
            age INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'live', created_at TEXT NOT NULL
        );
        CREATE TABLE touch (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
            kind TEXT NOT NULL, ts TEXT NOT NULL, note TEXT
        );
        CREATE TABLE dep (task_id INTEGER NOT NULL, ref_id INTEGER NOT NULL, PRIMARY KEY(task_id, ref_id));
        """
    )
    conn.execute(
        "INSERT INTO task(title, is_root, gen, state, created_at) VALUES('旧任务', 1, 'eden', 'live', ?)",
        (now.isoformat(),),
    )
    conn.execute(
        "INSERT INTO touch(task_id, kind, ts) VALUES(1, 'create', ?)",
        (now.isoformat(),),
    )
    conn.commit()
    conn.close()


def test_old_database_migrates_and_backs_up(tmp_path, now):
    db = tmp_path / "old.db"
    _old_database(db, now)
    with Store(db) as store:
        assert store.schema_version == 7
        task = store.get_task(1, record_view=False)
        assert task.ref_strength is ReferenceStrength.SOFT
        assert store.gc_runs() == []
    assert (tmp_path / "old.db.bak").exists()
    # Reopening is idempotent and must not corrupt the data.
    with Store(db) as store:
        assert store.schema_version == 7
        assert store.get_task(1, record_view=False).title == "旧任务"


def test_consecutive_skips_reset_after_real_progress(store, now):
    root = store.add_task("G", is_root=True, ts=now - timedelta(days=40))
    task = store.add_task("T", root_id=root.id, ts=now - timedelta(days=40))
    for _ in range(5):
        store.defer(task.id, ts=now)
    assert store.skip_count(task.id) == store.consecutive_skip_count(task.id) == 5
    before = Collector().run_on_store(store, store.root_ids(), now=now)
    assert {c.task.id for c in before.candidates} == {task.id}
    store.progress(task.id, "真的动了", ts=now)
    assert store.skip_count(task.id) == 5
    assert store.consecutive_skip_count(task.id) == 0
    after = Collector().run_on_store(store, store.root_ids(), now=now)
    assert task.id in {t.id for t in after.survivors}


def test_reference_strength_semantics(store, now):
    old = now - timedelta(days=40)
    root = store.add_task("G", is_root=True, ts=old)
    strong = store.add_task("strong", root_id=root.id, ref_strength="strong", ts=old)
    soft = store.add_task("soft", root_id=root.id, ref_strength="soft", ts=old)
    weak_live = store.add_task("weak-live", root_id=root.id, ref_strength="weak", ts=old)
    weak_dead = store.add_task("weak-dead", root_id=root.id, ref_strength="weak", ts=old)
    store.progress(weak_live.id, ts=now - timedelta(days=2))
    result = Collector().run_on_store(store, store.root_ids(), now=now)
    alive = {t.id for t in result.survivors}
    stale = {c.task.id for c in result.candidates if c.reason is Reason.STALE}
    assert {root.id, strong.id, weak_live.id} <= alive
    assert {soft.id, weak_dead.id} <= stale


def test_gc_modes_change_time_window(store, now):
    old = now - timedelta(days=40)
    root = store.add_task("G", is_root=True, ts=old)
    task = store.add_task("T", root_id=root.id, ts=old)
    gentle = Collector(GCPolicy(mode="gentle")).run_on_store(store, store.root_ids(), now=now)
    normal = Collector(GCPolicy(mode="normal")).run_on_store(store, store.root_ids(), now=now)
    assert task.id in {t.id for t in gentle.survivors}
    assert task.id in {c.task.id for c in normal.candidates}


def test_restore_grace_overrides_old_activity(store, now):
    old = now - timedelta(days=100)
    task = store.add_task("T", ts=old)
    store.record(task.id, TouchKind.RESTORE, ts=now - timedelta(days=2))
    result = Collector(GCPolicy(restore_grace_days=7)).run_on_store(store, store.root_ids(), now=now)
    assert task.id in {t.id for t in result.survivors}
    expired = Collector(GCPolicy(restore_grace_days=1)).run_on_store(store, store.root_ids(), now=now)
    assert task.id in {c.task.id for c in expired.candidates}


def test_toml_config_and_environment_precedence(tmp_path):
    config = tmp_path / "todogc.toml"
    config.write_text(
        '[gc]\nmode="gentle"\nrecent_days=60\nmax_consecutive_skips=8\nrestore_grace_days=9\n'
        '[heap]\nmax=88\n',
        encoding="utf-8",
    )
    cfg = load_config(config, environ={"TODOGC_HEAP_MAX": "21", "TODOGC_GC_MODE": "aggressive"})
    assert cfg.heap_max == 21 and cfg.gc_mode == "aggressive"
    assert cfg.recent_days == 60 and cfg.max_consecutive_skips == 8
    config.write_text("[gc]\nmode='impossible'\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config, environ={})


def test_i18n_lang_config_and_environment_precedence(tmp_path):
    config = tmp_path / "todogc.toml"
    config.write_text("[i18n]\nlang=\"zh\"\n", encoding="utf-8")
    assert load_config(config, environ={}).lang == "zh"
    assert load_config(config, environ={"TODOGC_LANG": "en"}).lang == "en"
    assert load_config(tmp_path / "missing.toml", environ={}).lang == "en"
    config.write_text("[i18n]\nlang=\"fr\"\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config, environ={})


def test_python310_zero_dependency_config_fallback(tmp_path, monkeypatch):
    config = tmp_path / "todogc.toml"
    config.write_text('[gc]\nmode="gentle"\nrecent_days=12\n[heap]\nmax=9\n', encoding="utf-8")
    monkeypatch.setattr(config_module, "tomllib", None)
    cfg = config_module.load_config(config, environ={})
    assert cfg.gc_mode == "gentle" and cfg.recent_days == 12 and cfg.heap_max == 9


def test_new_cli_commands_and_gc_stats(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    assert main(["--db", db, "add", "Goal", "--root"]) == 0
    assert main(["--db", db, "add", "Task", "--root-id", "1"]) == 0
    assert main(["--db", db, "edit", "2", "Renamed", "--ref", "strong"]) == 0
    assert main(["--db", db, "show", "2"]) == 0
    assert main(["--db", db, "history", "2", "--limit", "2"]) == 0
    assert main(["--db", db, "link", "2", "--off"]) == 0
    assert main(["--db", db, "link", "2", "1"]) == 0
    output = capsys.readouterr().out
    assert "Renamed [strong]" in output and "consecutive_skips" in output and "edit" in output

    # Two strong reachable objects, no candidates; a real gc writes an audit record, dry-run doesn't.
    assert main(["--db", db, "gc"]) == 0
    with Store(db) as store:
        assert len(store.gc_runs()) == 1
    assert main(["--db", db, "gc", "--dry-run"]) == 0
    with Store(db) as store:
        assert len(store.gc_runs()) == 1
    assert main(["--db", db, "stats", "--since", "30d"]) == 0
    assert "GC History Stats" in capsys.readouterr().out


def test_doctor_finds_view_only_leak(store, now):
    task = store.add_task("只是反复看看", is_root=True, ts=now - timedelta(days=90))
    for _ in range(10):
        store.get_task(task.id, ts=now)
    result = Collector().run_on_store(store, store.root_ids(), now=now)
    text = report.format_doctor(store, result, now=now)
    assert "cognitive memory leaks" in text
    assert "viewed 10 times but never progressed" in text


def test_interactive_gc_audit_records_actual_action(tmp_path, monkeypatch):
    db = str(tmp_path / "audit.db")
    assert main(["--db", db, "add", "orphan"]) == 0
    answers = iter(["c", "再见"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert main(["--db", db, "gc"]) == 0
    with Store(db) as store:
        runs = store.gc_runs()
        assert len(runs) == 1
        assert runs[0].candidates == runs[0].collected == 1
        assert runs[0].revived == 0 and runs[0].released_pct == 100.0
        assert store.get_task(1, record_view=False).state is State.COLLECTED

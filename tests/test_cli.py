# test_cli.py —— 命令行入口与回收仪式：命令链、错误处理、复活/回收/遗言、finalize 单次。

from __future__ import annotations

from datetime import timedelta

import pytest

from todogc.cli import _finalize_spent, _prescan_lang, main, run_ceremony
from todogc.collector import Collector
from todogc.heap import State, TouchKind
from todogc.store import Store


def _db(tmp_path) -> str:
    """给出一个临时数据库文件路径。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        str: 该目录下的 db 文件路径。
    """
    return str(tmp_path / "t.db")


def test_command_chain(tmp_path, capsys):
    """add/root/dep/progress/snooze/ls/gc --dry-run 全链路退出码 0 且落库正确。"""
    db = _db(tmp_path)
    assert main(["--db", db, "add", "上线X", "--root"]) == 0
    assert main(["--db", db, "add", "写模块", "--root-id", "1"]) == 0
    assert main(["--db", db, "add", "学Rust"]) == 0
    assert main(["--db", db, "add", "重写博客"]) == 0
    assert main(["--db", db, "dep", "3", "4"]) == 0
    assert main(["--db", db, "dep", "4", "3"]) == 0
    assert main(["--db", db, "progress", "2"]) == 0
    assert main(["--db", db, "snooze", "3"]) == 0
    assert main(["--db", db, "ls"]) == 0
    assert main(["--db", db, "gc", "--dry-run"]) == 0
    assert "reference cycle" in capsys.readouterr().out
    with Store(db) as s:
        assert s.root_ids() == {1}
        assert set(s.deps()) == {(3, 4), (4, 3)}
        assert s.refcount(2) == 1 and s.skip_count(3) == 1


def test_bad_id_is_clean_error(tmp_path):
    """错 id 返回 1 而非抛 traceback。"""
    db = _db(tmp_path)
    main(["--db", db, "add", "A"])
    assert main(["--db", db, "dep", "1", "99"]) == 1  # ref 不存在
    assert main(["--db", db, "dep", "1", "1"]) == 1  # 自环
    assert main(["--db", db, "progress", "99"]) == 1  # 任务不存在


def test_ceremony_revive_collect_eulogy(store, now):
    """复活 age+1 并重连 root；回收写遗言；首次回收触发 finalize。"""
    goal = store.add_task("G", is_root=True, ts=now)
    orphan = store.add_task("O", ts=now)
    a = store.add_task("A", ts=now)
    b = store.add_task("B", ts=now)
    store.add_dep(a.id, b.id)
    store.add_dep(b.id, a.id)
    res = Collector().run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)
    plan = {orphan.id: "revive", a.id: "collect", b.id: "collect"}
    out = run_ceremony(
        store,
        res,
        decide=lambda c: plan[c.task.id],
        eulogize=lambda c: f"遗言{c.task.id}",
        revive_root=lambda c: goal.id,
        now=now,
    )
    assert set(out.revived) == {orphan.id} and set(out.collected) == {a.id, b.id}
    ot = store.get_task(orphan.id, record_view=False)
    assert ot.age == 1 and ot.state is State.LIVE and ot.root_id == goal.id
    acol = [t for t in store.touches(a.id) if t.kind is TouchKind.COLLECT][-1]
    assert acol.note == f"遗言{a.id}"


def test_finalize_only_once(store, now):
    """复活过的任务再被回收，不再收集遗言，盖章 finalize() already called。"""
    store.add_task("G", is_root=True, ts=now)
    x = store.add_task("X", ts=now)
    store.record(x.id, TouchKind.REVIVE, ts=now - timedelta(days=8))  # 上轮复活，宽限期已过
    assert _finalize_spent(store, x.id)
    res = Collector().run_on_store(store, store.root_ids(), now=now)
    run_ceremony(store, res, decide=lambda c: "collect", eulogize=lambda c: "不该采用", now=now)
    note = [t for t in store.touches(x.id) if t.kind is TouchKind.COLLECT][-1].note
    assert "finalize() already called" in note and "不该采用" not in note


def test_graveyard_and_restore(tmp_path, capsys):
    """回收后 graveyard 显示墓志铭；restore 恢复为 live。"""
    db = _db(tmp_path)
    main(["--db", db, "add", "X"])
    with Store(db) as s:
        t = s.get_task(1, record_view=False)
        t.state = State.COLLECTED
        s.update_task(t)
        s.record(1, TouchKind.COLLECT, note="再见")
    assert main(["--db", db, "graveyard"]) == 0
    assert "再见" in capsys.readouterr().out
    assert main(["--db", db, "restore", "1"]) == 0
    with Store(db) as s:
        assert s.get_task(1, record_view=False).state is State.LIVE


def test_oom_blocks_add_and_force_bypasses(tmp_path, capsys):
    """活跃堆满时 add 被 Allocation Failure 拦下(退出 1)，--force 可强行超配。"""
    db = _db(tmp_path)
    main(["--heap-max", "2", "--db", db, "add", "G", "--root"])  # #1，堆=1
    main(["--heap-max", "2", "--db", db, "add", "orphan"])  # #2，堆=2(满)
    assert main(["--heap-max", "2", "--db", db, "add", "第三个"]) == 1
    assert "Allocation Failure" in capsys.readouterr().err
    with Store(db) as s:
        assert s.heap_size() == 2  # 第三个没进去
    assert main(["--heap-max", "2", "--db", db, "add", "硬加", "--force"]) == 0
    with Store(db) as s:
        assert s.heap_size() == 3


def test_meta_not_counted_toward_heap(tmp_path):
    """元任务(Metaspace)不占 -Xmx，堆满时仍可 add --meta。"""
    db = _db(tmp_path)
    main(["--heap-max", "1", "--db", db, "add", "G", "--root"])  # 堆=1(满)
    assert main(["--heap-max", "1", "--db", db, "add", "变得更自律", "--meta"]) == 0
    with Store(db) as s:
        assert s.heap_size() == 1  # meta 不计入


def test_true_oom_gives_diagnosis(tmp_path, capsys):
    """堆满且无可回收僵尸时，给 Full GC / 砍需求 的诊断。"""
    db = _db(tmp_path)
    main(["--heap-max", "1", "--db", db, "add", "G", "--root"])  # root 存活、非候选
    assert main(["--heap-max", "1", "--db", db, "add", "又一个"]) == 1
    assert "GC is powerless" in capsys.readouterr().err


# ============ i18n：--lang / 环境变量 / 配置文件 优先级 ============

def test_default_language_is_english(tmp_path, capsys):
    db = _db(tmp_path)
    assert main(["--db", db, "add", "A"]) == 0
    assert "Allocated #1" in capsys.readouterr().out


def test_lang_flag_switches_command_output(tmp_path, capsys):
    db = _db(tmp_path)
    assert main(["--db", db, "--lang", "zh", "add", "A"]) == 0
    assert "分配 #1" in capsys.readouterr().out


def test_lang_config_file_sets_default_when_no_flag_or_env(tmp_path, capsys):
    config = tmp_path / "todogc.toml"
    config.write_text('[i18n]\nlang="zh"\n', encoding="utf-8")
    db = _db(tmp_path)
    assert main(["--config", str(config), "--db", db, "add", "A"]) == 0
    assert "分配 #1" in capsys.readouterr().out


def test_lang_flag_overrides_config_file(tmp_path, capsys):
    config = tmp_path / "todogc.toml"
    config.write_text('[i18n]\nlang="zh"\n', encoding="utf-8")
    db = _db(tmp_path)
    assert main(["--config", str(config), "--db", db, "--lang", "en", "add", "A"]) == 0
    assert "Allocated #1" in capsys.readouterr().out


def test_lang_env_var_overrides_config_file(tmp_path, capsys, monkeypatch):
    config = tmp_path / "todogc.toml"
    config.write_text('[i18n]\nlang="zh"\n', encoding="utf-8")
    monkeypatch.setenv("TODOGC_LANG", "en")
    db = _db(tmp_path)
    assert main(["--config", str(config), "--db", db, "add", "A"]) == 0
    assert "Allocated #1" in capsys.readouterr().out


def test_lang_invalid_value_rejected_by_argparse(tmp_path):
    with pytest.raises(SystemExit):
        main(["--db", _db(tmp_path), "--lang", "fr", "add", "A"])


def test_prescan_lang_reads_space_and_equals_forms():
    assert _prescan_lang(["--lang", "zh", "ls"], {}) == "zh"
    assert _prescan_lang(["--lang=zh", "ls"], {}) == "zh"
    assert _prescan_lang(["ls"], {"TODOGC_LANG": "zh"}) == "zh"
    assert _prescan_lang(["ls"], {}) == "en"

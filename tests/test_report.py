# test_report.py —— 展示层：GC 日志、时长格式、分代打脸文案与候选分组。

from __future__ import annotations

from datetime import timedelta

from todogc import report
from todogc.collector import Collector, Reason
from todogc.heap import Generation


def _result(store, now):
    """造一个含存活/陈旧/孤儿/环的真实 GCResult。

    Args:
        store: 内存 Store fixture。
        now: 固定时间基准。

    Returns:
        GCResult: 用于渲染测试的结果。
    """
    goal = store.add_task("G", is_root=True, ts=now)
    active = store.add_task("A", root_id=goal.id, ts=now)
    store.progress(active.id, ts=now)
    store.add_task("S", root_id=goal.id, ts=now - timedelta(days=40))  # stale
    store.add_task("orphan", ts=now)  # unreachable
    a = store.add_task("学 Rust", ts=now)
    b = store.add_task("重写博客", ts=now)
    store.add_dep(a.id, b.id)
    store.add_dep(b.id, a.id)
    return Collector().run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)


def test_gc_log_young_and_full(store, now):
    """分别渲染 Pause Young / Full，并含运行时长、回收比例、环名。"""
    res = _result(store, now)
    y = report.format_gc_log(
        res, gc_id=7, uptime=timedelta(days=3, hours=12), pause=timedelta(seconds=252), heap_max=50
    )
    assert "GC(7) Pause Young (Allocation Failure)" in y
    assert "运行 3d12h" in y and "释放认知内存" in y and "00:04:12" in y
    assert "reference cycle: 1 处" in y and "学 Rust" in y
    assert "Pause Full (Ergonomics)" in report.format_gc_log(res, pause_full=True)


def test_duration_format():
    """时长格式化为 HH:MM:SS。"""
    assert report._fmt_duration(timedelta(seconds=252)) == "00:04:12"


def test_generation_taunt_tiers():
    """打脸文案按分代/年龄分档。"""
    assert report.generation_taunt(Generation.EDEN, 0) == "岁月静好。"
    assert "幸存者" in report.generation_taunt(Generation.SURVIVOR, 1)
    assert report.generation_taunt(Generation.SURVIVOR, 5) == "老油条预备役，你已跳过它 5 次。"
    assert "Tenured" in report.generation_taunt(Generation.OLD, 15)
    assert "人设" in report.generation_taunt(Generation.META, 99)


def test_candidates_grouping(store, now):
    """候选清单按 环 / 不可达 / 陈旧 分组，并能注入 views_of 打脸。"""
    res = _result(store, now)
    txt = report.format_candidates(res, views_of=store.view_count)
    assert "互相担保" in txt and "unreachable" in txt and "stale" in txt


def test_heap_map(store, now):
    """堆图：分代分区、-Xmx 占用(不计 Metaspace)、僵尸标 ☠(环/孤儿/陈旧)、root 标 ★、meta 标 ⊛。"""
    goal = store.add_task("G", is_root=True, ts=now)
    store.add_task("A", root_id=goal.id, ts=now)  # 刚建、可达 → 存活，无标记
    store.add_task("S", root_id=goal.id, ts=now - timedelta(days=40))  # 可达但陈旧
    store.add_task("orphan", ts=now - timedelta(days=40))  # 不可达孤儿
    a = store.add_task("学 Rust", ts=now - timedelta(days=40))
    b = store.add_task("重写博客", ts=now - timedelta(days=40))
    store.add_dep(a.id, b.id)
    store.add_dep(b.id, a.id)
    meta = store.add_task("变得更自律", meta=True, ts=now)
    res = Collector().run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)

    txt = report.format_heap_map(res, store.root_ids(), heap_max=50)
    assert "堆内存快照" in txt and "jmap" in txt
    assert "-Xmx 占用: 6 / 50" in txt  # 6 个非 meta 对象；Metaspace 不计入 -Xmx
    assert f"★ #{goal.id} G" in txt
    assert f"☠ #{a.id} 学 Rust  (cycle #0)" in txt
    assert "(unreachable)" in txt and "(stale" in txt
    assert f"⊛ #{meta.id} 变得更自律" in txt
    assert "Eden" in txt and "Metaspace" in txt
    assert "█" in txt and "░" in txt  # -Xmx 占用填充条

    plain = report.format_heap_map(res, store.root_ids())  # 不给 -Xmx 的变体
    assert "堆对象: 6 个" in plain and "未设 -Xmx" in plain


def test_heap_view(store, now):
    """heap_view 结构化视图：分区顺序/命名、每行 kind 与 candidate、id 升序、used 计数、candidate_note。"""
    goal = store.add_task("G", is_root=True, ts=now)
    live = store.add_task("A", root_id=goal.id, ts=now)  # 刚建、可达 → 存活普通
    s = store.add_task("S", root_id=goal.id, ts=now - timedelta(days=40))  # 可达但陈旧
    orphan = store.add_task("orphan", ts=now - timedelta(days=40))  # 不可达
    a = store.add_task("学 Rust", ts=now - timedelta(days=40))
    b = store.add_task("重写博客", ts=now - timedelta(days=40))
    store.add_dep(a.id, b.id)
    store.add_dep(b.id, a.id)  # 环
    meta = store.add_task("变得更自律", meta=True, ts=now)
    res = Collector().run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)

    sections = report.heap_view(res, store.root_ids())

    # 分区顺序与中英文名固定：Eden → Survivor → Old → Metaspace
    assert [(s_.gen, s_.name_en, s_.name_cn) for s_ in sections] == [
        (Generation.EDEN, "Eden", "新生代"),
        (Generation.SURVIVOR, "Survivor", "幸存区"),
        (Generation.OLD, "Old", "老年代"),
        (Generation.META, "Metaspace", "元空间"),
    ]
    by_gen = {s_.gen: s_ for s_ in sections}

    # 空代 → 空 rows
    assert by_gen[Generation.SURVIVOR].rows == [] and by_gen[Generation.OLD].rows == []

    # Eden 内按 id 升序
    eden = by_gen[Generation.EDEN].rows
    assert [r.task.id for r in eden] == sorted(r.task.id for r in eden)

    # 逐行断言标记类别与候选
    row = {r.task.id: r for r in eden}
    assert row[goal.id].kind is report.RowKind.ROOT and row[goal.id].candidate is None
    assert row[live.id].kind is report.RowKind.NORMAL and row[live.id].candidate is None
    assert row[s.id].kind is report.RowKind.ZOMBIE and row[s.id].candidate.reason is Reason.STALE
    assert row[orphan.id].candidate.reason is Reason.UNREACHABLE
    assert row[a.id].kind is report.RowKind.ZOMBIE
    assert row[a.id].candidate.reason is Reason.CYCLE and row[a.id].candidate.cycle_id == 0

    # meta 落 Metaspace，kind=META、非候选
    meta_rows = by_gen[Generation.META].rows
    assert len(meta_rows) == 1
    assert meta_rows[0].task.id == meta.id
    assert meta_rows[0].kind is report.RowKind.META and meta_rows[0].candidate is None

    # used 计数排除 meta：goal/A/S/orphan/学/重写 = 6
    assert report.heap_used_count(res) == 6

    # candidate_note 三种原因的短注解(与文本堆图括号内容同源)
    assert report.candidate_note(row[a.id].candidate) == "cycle #0"
    assert report.candidate_note(row[orphan.id].candidate) == "unreachable"
    assert report.candidate_note(row[s.id].candidate) == "stale · 推进0 推迟0"

# test_report.py — the presentation layer: the GC log, duration formatting, per-generation taunts, and candidate grouping.

from __future__ import annotations

from datetime import timedelta

from todogc import report
from todogc.collector import Collector, Reason
from todogc.heap import Generation


def _result(store, now):
    """Build a real GCResult containing alive/stale/orphan/cycle tasks.

    Args:
        store: the in-memory Store fixture.
        now: the fixed time base.

    Returns:
        GCResult: a result for rendering tests.
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
    """Renders Pause Young / Full respectively, including uptime, reclaim ratio, and cycle names."""
    res = _result(store, now)
    y = report.format_gc_log(
        res, gc_id=7, uptime=timedelta(days=3, hours=12), pause=timedelta(seconds=252), heap_max=50
    )
    assert "GC(7) Pause Young (Allocation Failure)" in y
    assert "uptime 3d12h" in y and "cognitive memory freed" in y and "00:04:12" in y
    assert "reference cycle: 1 found" in y and "学 Rust" in y
    assert "Pause Full (Ergonomics)" in report.format_gc_log(res, pause_full=True)

    zh = report.format_gc_log(res, uptime=timedelta(days=3, hours=12), lang="zh")
    assert "运行 3d12h" in zh and "释放认知内存" in zh and "reference cycle: 1 处" in zh


def test_duration_format():
    """Duration is formatted as HH:MM:SS."""
    assert report._fmt_duration(timedelta(seconds=252)) == "00:04:12"


def test_generation_taunt_tiers():
    """The taunt is tiered by generation/age."""
    assert report.generation_taunt(Generation.EDEN, 0) == "All quiet."
    assert "survivor" in report.generation_taunt(Generation.SURVIVOR, 1)
    assert report.generation_taunt(Generation.SURVIVOR, 5) == "Veteran-in-waiting — you've skipped it 5 times."
    assert "Tenured" in report.generation_taunt(Generation.OLD, 15)
    assert "personality" in report.generation_taunt(Generation.META, 99)

    assert report.generation_taunt(Generation.EDEN, 0, lang="zh") == "岁月静好。"
    assert "人设" in report.generation_taunt(Generation.META, 99, lang="zh")


def test_candidates_grouping(store, now):
    """The candidate listing is grouped by cycle / unreachable / stale, and can inject a views_of taunt."""
    res = _result(store, now)
    txt = report.format_candidates(res, views_of=store.view_count)
    assert "mutually vouching" in txt and "unreachable" in txt and "stale" in txt

    zh = report.format_candidates(res, views_of=store.view_count, lang="zh")
    assert "互相担保" in zh and "unreachable" in zh and "stale" in zh


def test_format_report_accepts_typed_log_options(store, now):
    """format_report's explicit parameters should pass through completely, avoiding **object breaking static typing."""
    txt = report.format_report(
        _result(store, now),
        gc_id=9,
        heap_max=20,
        pause_full=True,
        views_of=store.view_count,
    )
    assert "GC(9) Pause Full" in txt and "cap 20" in txt


def test_heap_map(store, now):
    """Heap map: generation sections, -Xmx usage (Metaspace excluded), zombies marked ☠ (cycle/orphan/stale), root marked ★, meta marked ⊛."""
    goal = store.add_task("G", is_root=True, ts=now)
    store.add_task("A", root_id=goal.id, ts=now)  # just created, reachable -> alive, unmarked
    store.add_task("S", root_id=goal.id, ts=now - timedelta(days=40))  # reachable but stale
    store.add_task("orphan", ts=now - timedelta(days=40))  # unreachable orphan
    a = store.add_task("学 Rust", ts=now - timedelta(days=40))
    b = store.add_task("重写博客", ts=now - timedelta(days=40))
    store.add_dep(a.id, b.id)
    store.add_dep(b.id, a.id)
    meta = store.add_task("变得更自律", meta=True, ts=now)
    res = Collector().run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)

    txt = report.format_heap_map(res, store.root_ids(), heap_max=50)
    assert "Heap Memory Snapshot" in txt and "jmap" in txt
    assert "-Xmx usage: 6 / 50" in txt  # 6 non-meta objects; Metaspace doesn't count toward -Xmx
    assert f"★ #{goal.id} G" in txt
    assert f"☠ #{a.id} 学 Rust  (cycle #0)" in txt
    assert "(unreachable)" in txt and "(stale" in txt
    assert f"⊛ #{meta.id} 变得更自律" in txt
    assert "Eden" in txt and "Metaspace" in txt
    assert "█" in txt and "░" in txt  # the -Xmx usage fill bar

    plain = report.format_heap_map(res, store.root_ids())  # the variant with no -Xmx given
    assert "heap objects: 6" in plain and "no -Xmx set" in plain

    zh = report.format_heap_map(res, store.root_ids(), heap_max=50, lang="zh")
    assert "堆内存快照" in zh and "-Xmx 占用: 6 / 50" in zh


def test_heap_view(store, now):
    """heap_view's structured view: section order/naming, each row's kind and candidate, ascending id, used count, candidate_note."""
    goal = store.add_task("G", is_root=True, ts=now)
    live = store.add_task("A", root_id=goal.id, ts=now)  # just created, reachable -> alive, normal
    s = store.add_task("S", root_id=goal.id, ts=now - timedelta(days=40))  # reachable but stale
    orphan = store.add_task("orphan", ts=now - timedelta(days=40))  # unreachable
    a = store.add_task("学 Rust", ts=now - timedelta(days=40))
    b = store.add_task("重写博客", ts=now - timedelta(days=40))
    store.add_dep(a.id, b.id)
    store.add_dep(b.id, a.id)  # a cycle
    meta = store.add_task("变得更自律", meta=True, ts=now)
    res = Collector().run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)

    sections = report.heap_view(res, store.root_ids())

    # Section order and English/Chinese names are fixed: Eden -> Survivor -> Old -> Metaspace
    assert [(s_.gen, s_.name_en, s_.name_cn) for s_ in sections] == [
        (Generation.EDEN, "Eden", "新生代"),
        (Generation.SURVIVOR, "Survivor", "幸存区"),
        (Generation.OLD, "Old", "老年代"),
        (Generation.META, "Metaspace", "元空间"),
    ]
    by_gen = {s_.gen: s_ for s_ in sections}

    # Empty generation -> empty rows
    assert by_gen[Generation.SURVIVOR].rows == [] and by_gen[Generation.OLD].rows == []

    # Ascending by id within Eden
    eden = by_gen[Generation.EDEN].rows
    assert [r.task.id for r in eden] == sorted(r.task.id for r in eden)

    # Assert marker category and candidate row by row
    row = {r.task.id: r for r in eden}
    assert row[goal.id].kind is report.RowKind.ROOT and row[goal.id].candidate is None
    assert row[live.id].kind is report.RowKind.NORMAL and row[live.id].candidate is None
    assert row[s.id].kind is report.RowKind.ZOMBIE and row[s.id].candidate.reason is Reason.STALE
    assert row[orphan.id].candidate.reason is Reason.UNREACHABLE
    assert row[a.id].kind is report.RowKind.ZOMBIE
    assert row[a.id].candidate.reason is Reason.CYCLE and row[a.id].candidate.cycle_id == 0

    # meta lands in Metaspace, kind=META, not a candidate
    meta_rows = by_gen[Generation.META].rows
    assert len(meta_rows) == 1
    assert meta_rows[0].task.id == meta.id
    assert meta_rows[0].kind is report.RowKind.META and meta_rows[0].candidate is None

    # used count excludes meta: goal/A/S/orphan/学/重写 = 6
    assert report.heap_used_count(res) == 6

    # candidate_note's short annotation for all three causes (same source as the text heap map's parenthetical)
    assert report.candidate_note(row[a.id].candidate) == "cycle #0"
    assert report.candidate_note(row[orphan.id].candidate) == "unreachable"
    assert report.candidate_note(row[s.id].candidate) == "stale · progressed 0 skipped 0"
    assert report.candidate_note(row[s.id].candidate, lang="zh") == "stale · 推进0 推迟0"

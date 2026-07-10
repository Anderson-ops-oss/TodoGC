# webapp.py —— 可选的 Streamlit Web 端：把堆内存图搬进浏览器(僵尸标红)，并支持 add/gc/restore 交互；复用内核(report.heap_view / run_ceremony)，不重判、不另起炉灶。

from __future__ import annotations

import html
import os
from datetime import datetime, timedelta, timezone

from todogc import report
from todogc.cli import HEAP_MAX, run_ceremony  # 共享的 -Xmx 默认与回收仪式纯逻辑核心
from todogc.collector import Collector, GCResult
from todogc.heap import State, TouchKind
from todogc.report import HeapRow, RowKind
from todogc.store import Store

try:
    import streamlit as st
    from streamlit.runtime import exists as _st_runtime_exists

    _HAVE_STREAMLIT = True
except ImportError:  # 没装 streamlit：模块仍可被 import(供测试/CLI 探测)，只是起不了服务
    st = None
    _st_runtime_exists = None
    _HAVE_STREAMLIT = False


# RowKind → HTML 颜色(与 CLI 的 rich 主题对齐：root 描金、僵尸红、元任务品红)。
_ROW_COLOR: dict[RowKind, str] = {
    RowKind.ROOT: "#e0a800",
    RowKind.META: "#a855f7",
    RowKind.ZOMBIE: "#e5484d",
}


# ============ 无依赖辅助(不碰 streamlit，可被安全 import) ============

def _db_path() -> str:
    """解析数据库路径：优先环境变量 TODOGC_DB，与 CLI 的 --db 默认同源。

    Returns:
        str: 数据库文件路径(默认 "todogc.db")。
    """
    return os.environ.get("TODOGC_DB", "todogc.db")


def _default_heap_max() -> int:
    """解析 -Xmx 默认值：优先环境变量 TODOGC_HEAP_MAX，否则用 CLI 的 HEAP_MAX。

    Args:
        无。

    Returns:
        int: 活跃堆上限默认值。
    """
    raw = os.environ.get("TODOGC_HEAP_MAX", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else HEAP_MAX


def _gc(store: Store, now: datetime) -> GCResult:
    """跑一次标记-清除(与 CLI 同口径：root + 理由边)。

    Args:
        store (Store): 数据源。
        now (datetime): 判活时间基准。

    Returns:
        GCResult: 本轮 GC 结果。
    """
    return Collector().run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)


def _row_markup(row: HeapRow) -> str:
    """把一个 HeapRow 渲染成一行带色 HTML(僵尸红 + 灰色判死注解，root 金、meta 品红)。

    标题经 html.escape 转义，任意内容都不会破坏标记。

    Args:
        row (HeapRow): 堆图行(kind 与候选信息已由 report.heap_view 判定)。

    Returns:
        str: 一行 HTML 文本。
    """
    tid = row.task.id
    title = html.escape(row.task.title)
    color = _ROW_COLOR.get(row.kind)
    if row.kind is RowKind.ZOMBIE:
        note = html.escape(report.candidate_note(row.candidate)) if row.candidate is not None else ""
        return (
            f'<span style="color:{color}">☠ #{tid} {title}</span> '
            f'<span style="color:#888">({note})</span>'
        )
    if row.kind is RowKind.ROOT:
        return f'<span style="color:{color}">★ #{tid} {title}</span>'
    if row.kind is RowKind.META:
        return f'<span style="color:{color}">⊛ #{tid} {title}</span>'
    return f'<span style="color:#777">·</span> #{tid} {title}'


def _running_under_streamlit() -> bool:
    """当前是否处于 `streamlit run` 运行时(而非被 import 或裸执行)。

    Returns:
        bool: 装了 streamlit 且其 runtime 已启动时为 True。
    """
    if not _HAVE_STREAMLIT:
        return False
    try:
        return bool(_st_runtime_exists())
    except Exception:
        return False


# ============ 渲染层(仅在 streamlit run 下调用) ============

def _render_heap(result: GCResult, roots: set[int], heap_max: int) -> None:
    """渲染堆内存图：-Xmx 占用条 + 分代卡片(僵尸标红) + 图例。

    Args:
        result (GCResult): 本轮 GC 结果。
        roots (set[int]): 活跃 root 的 id 集合。
        heap_max (int): 活跃堆上限(-Xmx)。

    Returns:
        None
    """
    used = report.heap_used_count(result)
    pct = used / heap_max if heap_max > 0 else 0.0
    st.progress(
        min(pct, 1.0),
        text=f"-Xmx 占用 {used}/{heap_max} obj · {round(pct * 100, 1)}%　(Metaspace 不计)",
    )

    cols = st.columns(2)
    for i, sec in enumerate(report.heap_view(result, roots)):
        with cols[i % 2].container(border=True):
            st.markdown(f"**{sec.name_en}** · {sec.name_cn}　({len(sec.rows)})")
            if sec.rows:
                st.markdown("<br>".join(_row_markup(r) for r in sec.rows), unsafe_allow_html=True)
            else:
                st.markdown(":gray[(空)]")
    st.caption("图例：★ root · ☠ 回收候选(僵尸) · ⊛ 元任务")


def _render_report(
    result: GCResult, store: Store, *, uptime: timedelta | None, heap_max: int, full: bool
) -> None:
    """渲染 JVM 风格 GC 日志与候选清单(纯文本，复用 report，与 CLI 同源)。

    Args:
        result (GCResult): 本轮 GC 结果。
        store (Store): 数据源(取 view 次数供打脸)。
        uptime (timedelta | None): 堆运行时长；None 显示 "—"。
        heap_max (int): 活跃堆上限。
        full (bool): 是否渲染为 Full GC。

    Returns:
        None
    """
    st.code(
        report.format_gc_log(result, uptime=uptime, pause_full=full, heap_max=heap_max),
        language=None,
    )
    with st.expander(f"候选清单文本 ({len(result.candidates)} 项)"):
        st.code(report.format_candidates(result, views_of=store.view_count), language=None)


def _render_ceremony(store: Store, result: GCResult, now: datetime) -> None:
    """渲染 Stop-the-world 交互仪式：逐候选选复活/回收/跳过，执行时复用 run_ceremony。

    Args:
        store (Store): 数据源(会被写入)。
        result (GCResult): 本轮 GC 结果。
        now (datetime): 事件时间基准。

    Returns:
        None
    """
    if not result.candidates:
        st.success("无回收候选。这次你居然没骗自己，难得。")
        return

    label_map = {"跳过": "skip", "复活": "revive", "回收": "collect"}
    choices: dict[int, str] = {}
    eulogies: dict[int, str] = {}
    for cand in result.candidates:
        tid = cand.task.id
        c0, c1, c2 = st.columns([4, 3, 4])
        c0.markdown(
            f"**#{tid}** {html.escape(cand.task.title)}  \n"
            f":gray[{cand.reason.value} · {report.generation_taunt(cand.task.gen, cand.task.age)}]"
        )
        choices[tid] = c1.radio(
            "处置", list(label_map), key=f"decide_{tid}", horizontal=True, label_visibility="collapsed"
        )
        eulogies[tid] = c2.text_input(
            "遗言", key=f"eulogy_{tid}", label_visibility="collapsed", placeholder="finalize() 遗言(回收时留)"
        )

    if st.button("▶ 执行 Stop-the-world 仪式", type="primary"):
        roots = sorted(store.root_ids())
        out = run_ceremony(
            store,
            result,
            decide=lambda c: label_map[choices[c.task.id]],
            eulogize=lambda c: eulogies.get(c.task.id, ""),
            revive_root=(roots[0] if roots else None),
            now=now,
        )
        st.success(
            f"仪式结束：复活 {len(out.revived)} · 回收 {len(out.collected)} · 跳过 {len(out.skipped)}"
        )
        st.rerun()


def _render_add(store: Store, heap_max: int, now: datetime) -> None:
    """渲染侧栏"分配任务"表单；活跃堆满(非 meta/force)时按 -Xmx 语义拒绝并提示先回收。

    Args:
        store (Store): 数据源(会被写入)。
        heap_max (int): 活跃堆上限(-Xmx)。
        now (datetime): 事件时间基准。

    Returns:
        None
    """
    with st.form("add_task", clear_on_submit=True):
        st.subheader("➕ 分配任务 (add)")
        title = st.text_input("标题")
        is_root = st.checkbox("设为活跃 root（★ 目标）")
        root_id = st.number_input("关联到 root-id（0 = 不关联）", min_value=0, value=0, step=1)
        meta = st.checkbox("元任务（Metaspace，不计 -Xmx）")
        force = st.checkbox("--force 强行超配")
        submitted = st.form_submit_button("分配")

    if not (submitted and title.strip()):
        return
    size = store.heap_size()
    if not force and not meta and size >= heap_max:
        st.error(f"[GC · Allocation Failure] 活跃堆 {size}/{heap_max} 已满——先回收再分配。")
        if _gc(store, now).candidates:
            st.caption("→ 到主区跑 Stop-the-world 处置僵尸腾空间，或勾 --force 强行超配。")
        else:
            st.warning("[Full GC · Ergonomics] 且无可回收——-Xmx 拉满，砍需求而非加内存。")
        return
    t = store.add_task(title.strip(), root_id=(int(root_id) or None), is_root=is_root, meta=meta, ts=now)
    st.success(f"分配 #{t.id}: {t.title}")
    st.rerun()


def _render_graveyard(store: Store, now: datetime) -> None:
    """渲染墓园：已回收任务的墓志铭 + 一键恢复(restore)。

    Args:
        store (Store): 数据源(会被写入)。
        now (datetime): restore 事件时间基准。

    Returns:
        None
    """
    dead = store.list_tasks(state=State.COLLECTED, record_view=False)
    if not dead:
        st.caption("墓园空空。你还没对自己诚实过。")
        return
    for t in dead:
        collects = [x for x in store.touches(t.id) if x.kind is TouchKind.COLLECT]
        last = collects[-1] if collects else None
        days = (last.ts - t.created_at).days if last is not None and t.created_at is not None else 0
        c0, c1 = st.columns([6, 1])
        c0.markdown(
            f"† **{html.escape(t.title)}** · 存活 {days}d · 推进 {store.refcount(t.id)} · "
            f"查看 {store.view_count(t.id)}  \n"
            f":gray[「{html.escape(last.note) if last is not None and last.note else ''}」]"
        )
        if c1.button("恢复", key=f"restore_{t.id}"):
            t.state = State.LIVE
            store.update_task(t)
            store.record(t.id, TouchKind.RESTORE, ts=now)
            st.rerun()


def main() -> None:
    """Streamlit 入口：装配侧栏(add + -Xmx)与主区(堆图 / GC 日志 / 仪式 / 墓园)。

    浏览用的读取一律 record_view=False——Web 交互会频繁 rerun，不该把自动刷新算成
    "你在查看某任务"而虚增 view 次数(与 CLI 的 heap/jmap 只读诊断同理)。写操作
    (add/progress/gc/restore)才是有意的埋点。

    Returns:
        None
    """
    st.set_page_config(page_title="TodoGC · 堆内存", page_icon="🗑️", layout="wide")
    now = datetime.now(timezone.utc)
    store = Store(_db_path())
    try:
        with st.sidebar:
            st.title("🗑️ TodoGC")
            st.caption(f"DB：`{_db_path()}`")
            heap_max = int(
                st.number_input("-Xmx（活跃堆上限）", min_value=1, value=_default_heap_max(), step=1)
            )
            full = st.checkbox("以 Full GC 视角渲染日志")
            st.divider()
            _render_add(store, heap_max, now)

        st.title("堆内存快照 · jmap -heap")
        result = _gc(store, now)
        roots = store.root_ids()
        _render_heap(result, roots, heap_max)

        live = store.list_tasks(record_view=False)
        earliest = min((t.created_at for t in live if t.created_at is not None), default=None)
        uptime = (now - earliest) if earliest is not None else None

        st.subheader("GC 日志")
        _render_report(result, store, uptime=uptime, heap_max=heap_max, full=full)

        st.subheader("Stop-the-world 回收仪式")
        _render_ceremony(store, result, now)

        st.subheader("墓园 · graveyard")
        _render_graveyard(store, now)
    finally:
        store.close()


if _running_under_streamlit():
    main()

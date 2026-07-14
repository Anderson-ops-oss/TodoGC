# webapp.py —— 可选的 Streamlit Web 端：把堆内存图搬进浏览器(僵尸标红)，并支持 add/gc/restore 交互；复用内核(report.heap_view / run_ceremony)，不重判、不另起炉灶。

from __future__ import annotations

import html
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from todogc import report
from todogc.cli import HEAP_MAX, run_ceremony  # 共享的 -Xmx 默认与回收仪式纯逻辑核心
from todogc.collector import Collector, GCPolicy, GCResult
from todogc.config import AppConfig, load_config
from todogc.heap import ReferenceStrength, State, TouchKind
from todogc.i18n import SUPPORTED_LOCALES
from todogc.i18n import t as _t
from todogc.report import HeapRow, RowKind
from todogc.store import Store
from todogc.watcher import Watcher

if TYPE_CHECKING:
    import streamlit as st  # pyright: ignore[reportMissingImports]
    from streamlit.runtime import exists as _st_runtime_exists  # pyright: ignore[reportMissingImports]

    _HAVE_STREAMLIT = True
else:
    try:
        import streamlit as st
        from streamlit.runtime import exists as _st_runtime_exists

        _HAVE_STREAMLIT = True
    except ImportError:  # 没装 streamlit：模块仍可被 import(供测试/CLI 探测)，只是起不了服务
        st = None
        _st_runtime_exists = None
        _HAVE_STREAMLIT = False


def _start_watcher(db_path: str, config: AppConfig) -> Watcher:
    """启动一条后台体检 daemon 线程(真正的"只起一条"由下面 st.cache_resource 包装保证)。

    Streamlit 每次 rerun 都会重新跑到这一行，若不用 st.cache_resource 包一层，
    每次交互都会新开一条线程——这里只是线程本体，缓存单例化在模块底部按
    _HAVE_STREAMLIT 条件包装，避免没装 streamlit 时对 st.cache_resource 的裸引用报错。

    Args:
        db_path (str): 要体检的数据库路径。
        config (AppConfig): 含 notify.*/watch.* 配置；调用方需确保 ntfy_enabled 为 True。

    Returns:
        Watcher: 已启动(daemon 线程在跑)的 watcher；随 streamlit 子进程一起终止，无需手动停止。
    """
    watcher = Watcher(db_path, config)
    threading.Thread(target=watcher.run, name="todogc-watch", daemon=True).start()
    return watcher


if _HAVE_STREAMLIT:
    _start_watcher = st.cache_resource(show_spinner=False)(_start_watcher)


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


def _gc(store: Store, now: datetime, policy: GCPolicy | None = None) -> GCResult:
    """跑一次标记-清除(与 CLI 同口径：root + 理由边)。

    Args:
        store (Store): 数据源。
        now (datetime): 判活时间基准。

    Returns:
        GCResult: 本轮 GC 结果。
    """
    return Collector(policy).run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)


def _row_markup(row: HeapRow, *, lang: str | None = None) -> str:
    """把一个 HeapRow 渲染成一行带色 HTML(僵尸红 + 灰色判死注解，root 金、meta 品红)。

    标题经 html.escape 转义，任意内容都不会破坏标记。

    Args:
        row (HeapRow): 堆图行(kind 与候选信息已由 report.heap_view 判定)。
        lang (str | None): 本次 rerun 的 UI 显示语言；None 用当前全局默认。

    Returns:
        str: 一行 HTML 文本。
    """
    tid = row.task.id
    title = html.escape(row.task.title)
    color = _ROW_COLOR.get(row.kind)
    if row.kind is RowKind.ZOMBIE:
        note = html.escape(report.candidate_note(row.candidate, lang=lang)) if row.candidate is not None else ""
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

def _render_heap(result: GCResult, roots: set[int], heap_max: int, *, lang: str) -> None:
    """渲染堆内存图：-Xmx 占用条 + 分代卡片(僵尸标红) + 图例。

    Args:
        result (GCResult): 本轮 GC 结果。
        roots (set[int]): 活跃 root 的 id 集合。
        heap_max (int): 活跃堆上限(-Xmx)。
        lang (str): 本次 rerun 的 UI 显示语言。

    Returns:
        None
    """
    used = report.heap_used_count(result)
    pct = used / heap_max if heap_max > 0 else 0.0
    st.progress(
        min(pct, 1.0),
        text=_t("webapp.heap.progress", lang=lang, used=used, max=heap_max, pct=round(pct * 100, 1)),
    )

    cols = st.columns(2)
    for i, sec in enumerate(report.heap_view(result, roots)):
        with cols[i % 2].container(border=True):
            st.markdown(f"**{sec.name_en}** · {sec.name_cn}　({len(sec.rows)})")
            if sec.rows:
                st.markdown("<br>".join(_row_markup(r, lang=lang) for r in sec.rows), unsafe_allow_html=True)
            else:
                st.markdown(_t("webapp.heap.empty_section", lang=lang))
    st.caption(_t("webapp.heap.legend", lang=lang))


def _render_report(
    result: GCResult, store: Store, *, uptime: timedelta | None, heap_max: int, full: bool, lang: str
) -> None:
    """渲染 JVM 风格 GC 日志与候选清单(纯文本，复用 report，与 CLI 同源)。

    Args:
        result (GCResult): 本轮 GC 结果。
        store (Store): 数据源(取 view 次数供打脸)。
        uptime (timedelta | None): 堆运行时长；None 显示 "—"。
        heap_max (int): 活跃堆上限。
        full (bool): 是否渲染为 Full GC。
        lang (str): 本次 rerun 的 UI 显示语言。

    Returns:
        None
    """
    st.code(
        report.format_gc_log(result, uptime=uptime, pause_full=full, heap_max=heap_max, lang=lang),
        language=None,
    )
    with st.expander(_t("webapp.report.candidates_expander", lang=lang, count=len(result.candidates))):
        st.code(report.format_candidates(result, views_of=store.view_count, lang=lang), language=None)


def _render_ceremony(
    store: Store, result: GCResult, now: datetime, *, mode: str = "normal", heap_before: int = 0, lang: str
) -> None:
    """渲染 Stop-the-world 交互仪式：逐候选选复活/回收/跳过，执行时复用 run_ceremony。

    Args:
        store (Store): 数据源(会被写入)。
        result (GCResult): 本轮 GC 结果。
        now (datetime): 事件时间基准。
        lang (str): 本次 rerun 的 UI 显示语言。

    Returns:
        None
    """
    if not result.candidates:
        st.success(_t("webapp.ceremony.no_candidates", lang=lang))
        return

    label_map = {
        _t("webapp.ceremony.choice.skip", lang=lang): "skip",
        _t("webapp.ceremony.choice.revive", lang=lang): "revive",
        _t("webapp.ceremony.choice.collect", lang=lang): "collect",
    }
    choices: dict[int, str] = {}
    eulogies: dict[int, str] = {}
    for cand in result.candidates:
        tid = cand.task.persisted_id
        c0, c1, c2 = st.columns([4, 3, 4])
        c0.markdown(
            f"**#{tid}** {html.escape(cand.task.title)}  \n"
            f":gray[{cand.reason.value} · {report.generation_taunt(cand.task.gen, cand.task.age, lang=lang)}]"
        )
        choices[tid] = c1.radio(
            _t("webapp.ceremony.decide_label", lang=lang),
            list(label_map), key=f"decide_{tid}", horizontal=True, label_visibility="collapsed"
        )
        eulogies[tid] = c2.text_input(
            _t("webapp.ceremony.eulogy_label", lang=lang), key=f"eulogy_{tid}", label_visibility="collapsed",
            placeholder=_t("webapp.ceremony.eulogy_placeholder", lang=lang),
        )

    if st.button(_t("webapp.ceremony.execute_button", lang=lang), type="primary"):
        roots = sorted(store.root_ids())
        out = run_ceremony(
            store,
            result,
            decide=lambda c: label_map[choices[c.task.persisted_id]],
            eulogize=lambda c: eulogies.get(c.task.persisted_id, ""),
            revive_root=(roots[0] if roots else None),
            now=now,
        )
        actions = {
            **{tid: "revive" for tid in out.revived},
            **{tid: "collect" for tid in out.collected},
            **{tid: "skip" for tid in out.skipped},
        }
        store.record_gc_run(
            started_at=now,
            mode=mode,
            heap_before=heap_before,
            survivors=len(result.survivors),
            candidates=len(result.candidates),
            collected=len(out.collected),
            revived=len(out.revived),
            released_pct=round(
                100 * len(out.collected) / (len(result.survivors) + len(result.candidates)), 1
            ),
            items=[
                (c.task.persisted_id, c.reason.value, actions[c.task.persisted_id])
                for c in result.candidates
            ],
        )
        st.success(
            _t(
                "webapp.ceremony.done", lang=lang,
                revived=len(out.revived), collected=len(out.collected), skipped=len(out.skipped),
            )
        )
        st.rerun()


def _render_add(store: Store, heap_max: int, now: datetime, *, lang: str) -> None:
    """渲染侧栏"分配任务"表单；活跃堆满(非 meta/force)时按 -Xmx 语义拒绝并提示先回收。

    Args:
        store (Store): 数据源(会被写入)。
        heap_max (int): 活跃堆上限(-Xmx)。
        now (datetime): 事件时间基准。
        lang (str): 本次 rerun 的 UI 显示语言。

    Returns:
        None
    """
    with st.form("add_task", clear_on_submit=True):
        st.subheader(_t("webapp.add.subheader", lang=lang))
        title = st.text_input(_t("webapp.field.title", lang=lang))
        is_root = st.checkbox(_t("webapp.add.root_checkbox", lang=lang))
        root_id = st.number_input(_t("webapp.add.root_id_label", lang=lang), min_value=0, value=0, step=1)
        meta = st.checkbox(_t("webapp.add.meta_checkbox", lang=lang))
        ref_strength = st.selectbox(
            _t("webapp.field.ref_strength", lang=lang), [r.value for r in ReferenceStrength], index=1
        )
        force = st.checkbox(_t("webapp.add.force_checkbox", lang=lang))
        submitted = st.form_submit_button(_t("webapp.add.submit_button", lang=lang))

    if not (submitted and title.strip()):
        return
    size = store.heap_size()
    if not force and not meta and size >= heap_max:
        st.error(_t("webapp.add.heap_full", lang=lang, size=size, max=heap_max))
        if _gc(store, now).candidates:
            st.caption(_t("webapp.add.hint_gc_or_force", lang=lang))
        else:
            st.warning(_t("webapp.add.full_gc_no_candidates", lang=lang))
        return
    try:
        t = store.add_task(
            title.strip(), root_id=(int(root_id) or None), is_root=is_root, meta=meta,
            ref_strength=ref_strength, ts=now,
        )
    except ValueError as exc:
        st.error(str(exc))
        return
    st.success(_t("webapp.add.allocated", lang=lang, id=t.id, title=t.title))
    st.rerun()


def _render_graveyard(store: Store, now: datetime, *, lang: str) -> None:
    """渲染墓园：已回收任务的墓志铭 + 一键恢复(restore)。

    Args:
        store (Store): 数据源(会被写入)。
        now (datetime): restore 事件时间基准。
        lang (str): 本次 rerun 的 UI 显示语言。

    Returns:
        None
    """
    dead = store.list_tasks(state=State.COLLECTED, record_view=False)
    if not dead:
        st.caption(_t("cli.graveyard.empty", lang=lang))
        return
    for t in dead:
        task_id = t.persisted_id
        collects = [x for x in store.touches(task_id) if x.kind is TouchKind.COLLECT]
        last = collects[-1] if collects else None
        days = (last.ts - t.created_at).days if last is not None and t.created_at is not None else 0
        c0, c1 = st.columns([6, 1])
        note = html.escape(last.note) if last is not None and last.note else ""
        c0.markdown(
            _t(
                "webapp.graveyard.entry", lang=lang,
                title=html.escape(t.title), days=days,
                refcount=store.refcount(task_id), views=store.view_count(task_id),
            )
            + "  \n"
            + f":gray[{_t('webapp.graveyard.epitaph', lang=lang, note=note)}]"
        )
        if c1.button(_t("webapp.graveyard.restore_button", lang=lang), key=f"restore_{task_id}"):
            t.state = State.LIVE
            store.update_task(t)
            store.record(task_id, TouchKind.RESTORE, ts=now)
            st.rerun()


def _render_task_manager(store: Store, now: datetime, *, lang: str) -> None:
    """任务详情、编辑、推进、推迟、关联和事件时间线。"""
    tasks = [t for t in store.list_tasks(record_view=False) if t.state is not State.COLLECTED]
    if not tasks:
        st.caption(_t("webapp.task_manager.empty", lang=lang))
        return
    by_id = {t.persisted_id: t for t in tasks}
    selected_value = st.selectbox(
        _t("webapp.task_manager.select_label", lang=lang),
        list(by_id), format_func=lambda tid: f"#{tid} {by_id[tid].title}", key="task_manager_id"
    )
    selected = int(selected_value)
    task = by_id[selected]
    task_id = task.persisted_id
    c0, c1 = st.columns(2)
    title = c0.text_input(_t("webapp.field.title", lang=lang), value=task.title, key=f"edit_title_{task_id}")
    strength_values = [r.value for r in ReferenceStrength]
    strength = c1.selectbox(
        _t("webapp.field.ref_strength", lang=lang), strength_values,
        index=strength_values.index(task.ref_strength.value), key=f"edit_ref_{task_id}",
    )
    note = st.text_input(_t("webapp.task_manager.note_label", lang=lang), key=f"event_note_{task_id}")
    b0, b1, b2 = st.columns(3)
    if b0.button(_t("webapp.task_manager.save_button", lang=lang), key=f"save_{task_id}"):
        try:
            store.edit_task(task_id, title=title, ref_strength=strength, ts=now)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if b1.button(_t("webapp.task_manager.progress_button", lang=lang), key=f"progress_{task_id}"):
        store.progress(task_id, note, ts=now)
        st.rerun()
    if b2.button(_t("webapp.task_manager.defer_button", lang=lang), key=f"defer_{task_id}"):
        store.defer(task_id, note, ts=now)
        st.rerun()

    parent = st.number_input(
        _t("webapp.task_manager.parent_label", lang=lang), min_value=0, value=int(task.root_id or 0), step=1,
        key=f"parent_{task_id}",
    )
    if st.button(_t("webapp.task_manager.link_button", lang=lang), key=f"link_{task_id}"):
        try:
            store.unlink(task_id, ts=now) if int(parent) == 0 else store.link(task_id, int(parent), ts=now)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    dep_id = st.number_input(
        _t("webapp.task_manager.dep_id_label", lang=lang), min_value=1, value=1, step=1, key=f"dep_{task_id}"
    )
    d0, d1 = st.columns(2)
    if d0.button(_t("webapp.task_manager.dep_add_button", lang=lang), key=f"dep_add_{task_id}"):
        try:
            store.add_dep(task_id, int(dep_id))
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if d1.button(_t("webapp.task_manager.dep_remove_button", lang=lang), key=f"dep_remove_{task_id}"):
        store.remove_dep(task_id, int(dep_id))
        st.rerun()

    deps = [ref for owner, ref in store.deps() if owner == task_id]
    deps_text = ", ".join(f"#{ref}" for ref in deps) if deps else _t("webapp.task_manager.deps_none", lang=lang)
    st.caption(_t("webapp.task_manager.deps_prefix", lang=lang, deps=deps_text))

    with st.expander(_t("webapp.task_manager.timeline_expander", lang=lang, count=len(store.touches(task_id)))):
        for touch in reversed(store.touches(task_id)):
            st.code(
                f"{touch.ts.isoformat()}  {touch.kind.value}"
                + (f" · {touch.note}" if touch.note else ""),
                language=None,
            )


def main() -> None:
    """Streamlit 入口：装配侧栏(add + -Xmx)与主区(堆图 / GC 日志 / 仪式 / 墓园)。

    浏览用的读取一律 record_view=False——Web 交互会频繁 rerun，不该把自动刷新算成
    "你在查看某任务"而虚增 view 次数(与 CLI 的 heap/jmap 只读诊断同理)。写操作
    (add/progress/gc/restore)才是有意的埋点。

    Returns:
        None
    """
    if "lang" not in st.session_state:
        st.session_state["lang"] = load_config().lang
    lang = st.session_state["lang"]
    st.set_page_config(page_title=_t("webapp.page.title", lang=lang), page_icon="🗑️", layout="wide")
    now = datetime.now(timezone.utc)
    cfg = load_config()
    policy = GCPolicy(
        mode=cfg.gc_mode,
        recent_days=cfg.recent_days,
        max_skips=cfg.max_consecutive_skips,
        restore_grace_days=cfg.restore_grace_days,
    )
    store = Store(_db_path())
    try:
        with st.sidebar:
            st.title("🗑️ TodoGC")
            lang = st.selectbox(
                "Language / 语言", SUPPORTED_LOCALES,
                format_func=lambda code: {"en": "English", "zh": "中文"}[code], key="lang",
            )
            st.caption(_t("webapp.sidebar.db_caption", lang=lang, db=_db_path()))
            if cfg.ntfy_enabled:
                _start_watcher(_db_path(), cfg)
                st.caption(
                    _t(
                        "webapp.sidebar.watcher_running", lang=lang,
                        poll_seconds=cfg.watch_poll_seconds, topic=cfg.ntfy_topic,
                    )
                )
            else:
                st.caption(_t("webapp.sidebar.watcher_disabled", lang=lang))
            heap_max = int(
                st.number_input(
                    _t("webapp.sidebar.heap_max_label", lang=lang), min_value=1, value=_default_heap_max(), step=1
                )
            )
            full = st.checkbox(_t("webapp.sidebar.full_checkbox", lang=lang))
            mode = st.selectbox(
                _t("webapp.sidebar.mode_label", lang=lang), ["gentle", "normal", "aggressive"],
                index=["gentle", "normal", "aggressive"].index(cfg.gc_mode),
            )
            policy = GCPolicy(
                mode=mode,
                recent_days=cfg.recent_days,
                max_skips=cfg.max_consecutive_skips,
                restore_grace_days=cfg.restore_grace_days,
            )
            st.divider()
            _render_add(store, heap_max, now, lang=lang)

        st.title(_t("webapp.main.heap_title", lang=lang))
        result = _gc(store, now, policy)
        roots = store.root_ids()
        _render_heap(result, roots, heap_max, lang=lang)

        live = store.list_tasks(record_view=False)
        earliest = min((t.created_at for t in live if t.created_at is not None), default=None)
        uptime = (now - earliest) if earliest is not None else None

        st.subheader(_t("webapp.main.gc_log_subheader", lang=lang))
        _render_report(result, store, uptime=uptime, heap_max=heap_max, full=full, lang=lang)

        st.subheader(_t("webapp.main.ceremony_subheader", lang=lang))
        _render_ceremony(
            store, result, now, mode=policy.mode, heap_before=store.heap_size(), lang=lang
        )

        st.subheader(_t("webapp.main.task_manager_subheader", lang=lang))
        _render_task_manager(store, now, lang=lang)

        st.subheader("Leak Inspector · doctor")
        st.code(report.format_doctor(store, result, now=now, lang=lang), language=None)

        st.subheader(_t("webapp.main.stats_subheader", lang=lang))
        st.code(report.format_stats(store.gc_runs(), lang=lang), language=None)

        st.subheader(_t("webapp.main.graveyard_subheader", lang=lang))
        _render_graveyard(store, now, lang=lang)
    finally:
        store.close()


if _running_under_streamlit():
    main()

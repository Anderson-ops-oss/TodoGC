# webapp.py — the optional Streamlit Web UI: brings the heap map into the browser (zombies
# marked red) and supports add/gc/restore interactions; reuses the core (report.heap_view /
# run_ceremony) rather than re-judging anything or building a parallel implementation.

from __future__ import annotations

import html
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from todogc import report
from todogc.cli import HEAP_MAX, run_ceremony  # shared -Xmx default and the ceremony's pure-logic core
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
    except ImportError:  # streamlit not installed: the module can still be imported (for tests/CLI probing), it just can't serve
        st = None
        _st_runtime_exists = None
        _HAVE_STREAMLIT = False


def _start_watcher(db_path: str, config: AppConfig) -> Watcher:
    """Start a background checkup daemon thread ("only ever one" is actually guaranteed by the st.cache_resource wrapper below).

    Streamlit re-runs this line on every rerun; without the st.cache_resource wrapper, every
    interaction would spin up a new thread — this function is just the thread body, and the
    singleton caching is applied at the bottom of the module, conditioned on
    _HAVE_STREAMLIT so a bare reference to st.cache_resource doesn't error out when
    streamlit isn't installed.

    Args:
        db_path (str): path to the database to check up on.
        config (AppConfig): config containing notify.*/watch.*; the caller must ensure
            ntfy_enabled is True.

    Returns:
        Watcher: the watcher, already started (its daemon thread is running); it terminates
            along with the streamlit subprocess, no manual stop needed.
    """
    watcher = Watcher(db_path, config)
    threading.Thread(target=watcher.run, name="todogc-watch", daemon=True).start()
    return watcher


if _HAVE_STREAMLIT:
    _start_watcher = st.cache_resource(show_spinner=False)(_start_watcher)


# RowKind -> HTML color (aligned with the CLI's rich theme: root gold, zombie red, meta-task magenta).
_ROW_COLOR: dict[RowKind, str] = {
    RowKind.ROOT: "#e0a800",
    RowKind.META: "#a855f7",
    RowKind.ZOMBIE: "#e5484d",
}


# ============ Dependency-free helpers (don't touch streamlit, safe to import) ============

def _db_path() -> str:
    """Resolve the database path: prefers the TODOGC_DB env var, same source as the CLI's --db default.

    Returns:
        str: the database file path (default "todogc.db").
    """
    return os.environ.get("TODOGC_DB", "todogc.db")


def _default_heap_max() -> int:
    """Resolve the -Xmx default: prefers the TODOGC_HEAP_MAX env var, otherwise the CLI's HEAP_MAX.

    Args:
        None.

    Returns:
        int: the default active heap cap.
    """
    raw = os.environ.get("TODOGC_HEAP_MAX", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else HEAP_MAX


def _gc(store: Store, now: datetime, policy: GCPolicy | None = None) -> GCResult:
    """Run one mark-and-sweep pass (same accounting as the CLI: root + reason edges).

    Args:
        store (Store): the data source.
        now (datetime): the liveness time base.

    Returns:
        GCResult: this round's GC result.
    """
    return Collector(policy).run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)


def _row_markup(row: HeapRow, *, lang: str | None = None) -> str:
    """Render a HeapRow as one line of colored HTML (zombie red + gray cause-of-death annotation, root gold, meta magenta).

    The title is escaped via html.escape, so no content can break the markup.

    Args:
        row (HeapRow): the heap-map row (kind and candidate info already resolved by
            report.heap_view).
        lang (str | None): this rerun's UI display language; None uses the current global
            default.

    Returns:
        str: one line of HTML.
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
    """Whether we're currently inside a `streamlit run` runtime (as opposed to being imported or run bare).

    Returns:
        bool: True if streamlit is installed and its runtime has started.
    """
    if not _HAVE_STREAMLIT:
        return False
    try:
        return bool(_st_runtime_exists())
    except Exception:
        return False


# ============ Rendering layer (only called under streamlit run) ============

def _render_heap(result: GCResult, roots: set[int], heap_max: int, *, lang: str) -> None:
    """Render the heap map: -Xmx usage bar + per-generation cards (zombies marked red) + legend.

    Args:
        result (GCResult): this round's GC result.
        roots (set[int]): ids of the active roots.
        heap_max (int): the active heap cap (-Xmx).
        lang (str): this rerun's UI display language.

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
            st.markdown(f"**{sec.display_name(lang)}** ({len(sec.rows)})")
            if sec.rows:
                st.markdown("<br>".join(_row_markup(r, lang=lang) for r in sec.rows), unsafe_allow_html=True)
            else:
                st.markdown(_t("webapp.heap.empty_section", lang=lang))
    st.caption(_t("webapp.heap.legend", lang=lang))


def _render_report(
    result: GCResult, store: Store, *, uptime: timedelta | None, heap_max: int, full: bool, lang: str
) -> None:
    """Render the JVM-style GC log and candidate listing (plain text, reuses report, same source as the CLI).

    Args:
        result (GCResult): this round's GC result.
        store (Store): the data source (for view counts, for the taunt).
        uptime (timedelta | None): heap uptime; shows "—" if None.
        heap_max (int): the active heap cap.
        full (bool): whether to render as a Full GC.
        lang (str): this rerun's UI display language.

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
    """Render the interactive Stop-the-world ceremony: pick revive/collect/skip per candidate, executing via run_ceremony.

    Args:
        store (Store): the data source (will be written to).
        result (GCResult): this round's GC result.
        now (datetime): the event time base.
        lang (str): this rerun's UI display language.

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
        ) or next(iter(label_map))
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
    """Render the sidebar's "allocate task" form; rejects per -Xmx semantics and suggests reclaiming first when the active heap is full (and not meta/force).

    Args:
        store (Store): the data source (will be written to).
        heap_max (int): the active heap cap (-Xmx).
        now (datetime): the event time base.
        lang (str): this rerun's UI display language.

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
            ref_strength=ref_strength or ReferenceStrength.SOFT, ts=now,
        )
    except ValueError as exc:
        st.error(str(exc))
        return
    st.success(_t("webapp.add.allocated", lang=lang, id=t.id, title=t.title))
    st.rerun()


def _render_graveyard(store: Store, now: datetime, *, lang: str) -> None:
    """Render the graveyard: epitaphs for collected tasks + one-click restore.

    Args:
        store (Store): the data source (will be written to).
        now (datetime): the restore event time base.
        lang (str): this rerun's UI display language.

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
    """Task details, edit, progress, snooze, linking, and the event timeline."""
    tasks = [t for t in store.list_tasks(record_view=False) if t.state.is_active]
    if not tasks:
        st.caption(_t("webapp.task_manager.empty", lang=lang))
        return
    by_id = {t.persisted_id: t for t in tasks}
    selected_value = st.selectbox(
        _t("webapp.task_manager.select_label", lang=lang),
        list(by_id), format_func=lambda tid: f"#{tid} {by_id[tid].title}", key="task_manager_id"
    )
    if selected_value is None:
        return
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

    children = store.active_descendants(task_id)
    with_subtasks = False
    if children:
        st.caption(_t("webapp.done.unfinished", lang=lang))
        for child in children:
            st.text(f"#{child.id} {child.title}")
        with_subtasks = st.checkbox(
            _t("webapp.done.with_subtasks", lang=lang), key=f"done_subtasks_{task_id}"
        )
    if st.button(_t("webapp.done.button", lang=lang), key=f"done_{task_id}"):
        try:
            store.complete_task(task_id, with_subtasks=with_subtasks, note=note, ts=now)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

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


def _render_completed(store: Store, heap_max: int, now: datetime, *, lang: str) -> None:
    tasks = store.list_tasks(state=State.DONE, record_view=False)
    if not tasks:
        st.caption(_t("cli.completed.empty", lang=lang))
        return
    for task in tasks:
        tid = task.persisted_id
        with st.expander(f"#{tid} {task.title}"):
            st.text(_t(
                "webapp.completed.timestamp", lang=lang,
                time=task.completed_at.isoformat() if task.completed_at else "-",
            ))
            touches = store.touches(tid)
            st.caption(_t("webapp.task_manager.timeline_expander", lang=lang, count=len(touches)))
            for touch in reversed(touches):
                st.text(f"{touch.ts.isoformat()}  {touch.kind.value}" + (f" · {touch.note}" if touch.note else ""))
            parent = st.number_input(
                _t("webapp.completed.parent", lang=lang), min_value=0, value=int(task.root_id or 0),
                step=1, key=f"reopen_parent_{tid}",
            )
            if st.button(_t("webapp.completed.reopen", lang=lang), key=f"reopen_{tid}"):
                try:
                    store.reopen_task(tid, root_id=int(parent) or None, heap_max=heap_max, ts=now)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def main() -> None:
    """Streamlit entry point: assembles the sidebar (add + -Xmx) and the main area (heap map / GC log / ceremony / graveyard).

    Browsing reads always use record_view=False — Web interactions rerun frequently, and an
    automatic refresh shouldn't be counted as "you're viewing this task" and inflate its view
    count (same reasoning as the CLI's read-only heap/jmap diagnostics). Write operations
    (add/progress/gc/restore) are the only ones meant to be tracked.

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
                _t("webapp.sidebar.language_label", lang=lang), SUPPORTED_LOCALES,
                format_func=lambda code: _t(f"webapp.language.{code}", lang=lang), key="lang",
            ) or cfg.lang
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
            ) or cfg.gc_mode
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

        st.subheader(_t("webapp.completed.title", lang=lang))
        _render_completed(store, heap_max, now, lang=lang)
    finally:
        store.close()


if _running_under_streamlit():
    main()

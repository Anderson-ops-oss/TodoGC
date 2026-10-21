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
from todogc.heap import ReferenceStrength, State, Task, TouchKind
from todogc.i18n import SUPPORTED_LOCALES
from todogc.i18n import t as _t
from todogc.report import HeapRow, RowKind
from todogc.store import Store
from todogc.scheduling import local_time, parse_snooze_time
from todogc.tree import TaskTree, build_task_tree, task_label
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
    if row.kind is RowKind.SNOOZED and row.task.snoozed_until is not None:
        note = html.escape(_t("snooze.until", lang=lang, time=local_time(row.task.snoozed_until)))
        return f'<span style="color:#777">⏸ #{tid} {title} · {note}</span>'
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
    if result.inbox:
        st.caption(_t("inbox.heap_summary", lang=lang, count=len(result.inbox)))

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
            survivors=len(result.survivors) + len(result.inbox),
            candidates=len(result.candidates),
            collected=len(out.collected),
            revived=len(out.revived),
            released_pct=round(
                100 * len(out.collected) / (len(result.survivors) + len(result.candidates) + len(result.inbox)), 1
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
        parents = {
            task.persisted_id: task for task in store.list_tasks(active_only=True, inbox=False, record_view=False)
        }
        root_id = st.selectbox(
            _t("webapp.tree.add_parent", lang=lang), [0, *parents],
            format_func=lambda value: f"#{value} {parents[value].title}" if value else _t("webapp.tree.no_parent", lang=lang),
            key="add_parent",
        )
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
            title.strip(), root_id=root_id or None, is_root=is_root, meta=meta,
            ref_strength=ref_strength or ReferenceStrength.SOFT, ts=now,
        )
    except ValueError as exc:
        st.error(str(exc))
        return
    st.success(_t("webapp.add.allocated", lang=lang, id=t.id, title=t.title))
    st.rerun()


def _render_graveyard(store: Store, heap_max: int, now: datetime, *, lang: str) -> None:
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
            try:
                store.restore_task(task_id, heap_max=heap_max, ts=now)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


def _parent_options(store: Store, task_id: int) -> dict[int, Task]:
    excluded = {task_id, *(task.persisted_id for task in store.descendants(task_id))}
    return {
        task.persisted_id: task for task in store.list_tasks(active_only=True, inbox=False, record_view=False)
        if task.persisted_id not in excluded
    }


def _select_tree_task(task_id: int) -> None:
    st.session_state["selected_task_id"] = task_id


def _toggle_tree_node(task_id: int) -> None:
    collapsed = set(st.session_state.get("tree_collapsed", set()))
    if task_id in collapsed:
        collapsed.remove(task_id)
    else:
        collapsed.add(task_id)
    st.session_state["tree_collapsed"] = collapsed


def _render_tree_nodes(tree: TaskTree, root_ids: set[int], selected: int, now: datetime, *, lang: str) -> None:
    collapsed = set(st.session_state.get("tree_collapsed", set()))
    # Pixel-based indentation keeps nested rows readable when Streamlit stacks the
    # surrounding tree/detail columns on narrow screens. No task text enters CSS.
    row_styles = []
    for group in tree.groups:
        for task, depth, _ in tree.rows(group, collapsed):
            row_styles.append(
                '[data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] > '
                '[data-testid="stVerticalBlock"] > '
                f'.st-key-tree_select_{task.persisted_id}) '
                f'{{ grid-template-columns: {min(depth, 6) * 16}px 24px minmax(0, 1fr); }}'
            )
    st.markdown("<style>" + "\n".join(row_styles) + "</style>", unsafe_allow_html=True)
    if tree.repaired_cycles:
        st.warning(_t("webapp.tree.legacy_cycle", lang=lang))
    for group, ids in tree.groups.items():
        if not ids:
            continue
        if group == "inbox":
            pending = [tree.tasks[tid] for tid in ids if tree.tasks[tid].state.is_active]
            review = sum(task.needs_inbox_review(now) for task in pending)
            st.caption(_t("inbox.group_review" if review else "inbox.list_title", lang=lang, count=len(pending), review=review))
        else:
            st.caption(_t(f"webapp.tree.group.{group}", lang=lang))
        for task, depth, has_children in tree.rows(group, collapsed):
            tid = task.persisted_id
            indent = min(depth, 6)
            spacer, toggle, node = st.columns([indent + 0.1, 0.8, 12 - indent])
            if depth > 6:
                spacer.caption(f"+{depth - 6}")
            if has_children:
                toggle.button(
                    "▸" if tid in collapsed else "▾", key=f"tree_toggle_{tid}",
                    help=_t("webapp.tree.expand" if tid in collapsed else "webapp.tree.collapse", lang=lang),
                    on_click=_toggle_tree_node, args=(tid,),
                )
            else:
                toggle.caption("·")
            generation = _t(f"webapp.tree.generation.{task.gen.value}", lang=lang)
            node.button(
                task_label(task, root_ids, now=now, lang=lang), key=f"tree_select_{tid}",
                type="primary" if tid == selected else "secondary", use_container_width=True,
                help=task_label(task, root_ids, now=now, lang=lang) if task.in_inbox else generation,
                on_click=_select_tree_task, args=(tid,),
            )


def _render_selected_completed(store: Store, task: Task, heap_max: int, now: datetime, *, lang: str) -> None:
    tid = task.persisted_id
    st.caption(_t("webapp.completed.timestamp", lang=lang, time=task.completed_at.isoformat() if task.completed_at else "-"))
    parents = _parent_options(store, tid)
    choices = [0, *parents]
    parent = st.selectbox(
        _t("webapp.tree.reopen_parent", lang=lang), choices,
        index=choices.index(task.root_id) if task.root_id in choices else 0,
        format_func=lambda value: f"#{value} {parents[value].title}" if value else _t("webapp.tree.keep_parent", lang=lang),
        key=f"tree_reopen_parent_{tid}",
    )
    if st.button(_t("webapp.completed.reopen", lang=lang), key=f"tree_reopen_{tid}"):
        try:
            store.reopen_task(tid, root_id=parent or None, heap_max=heap_max, ts=now)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    _render_task_history(store, tid, lang=lang)


def _render_task_history(store: Store, task_id: int, *, lang: str) -> None:
    touches = store.touches(task_id)
    with st.expander(_t("webapp.task_manager.timeline_expander", lang=lang, count=len(touches))):
        for touch in reversed(touches):
            st.code(f"{touch.ts.isoformat()}  {touch.kind.value}" + (f" · {touch.note}" if touch.note else ""), language=None)


def _render_snooze(store: Store, task: Task, now: datetime, *, lang: str) -> None:
    tid = task.persisted_id
    sleeping = task.is_snoozed(now)
    with st.expander(_t("snooze.web.title", lang=lang), expanded=sleeping):
        if sleeping:
            assert task.snoozed_until is not None
            st.info(_t("snooze.until", lang=lang, time=local_time(task.snoozed_until)))
            st.caption(_t("snooze.web.context", lang=lang))
        mode = st.selectbox(
            _t("snooze.web.mode", lang=lang), ["duration", "until"], key=f"snooze_mode_{tid}",
            format_func=lambda value: _t(f"snooze.web.{value}", lang=lang),
        )
        value = st.text_input(
            _t(f"snooze.web.{mode}_input", lang=lang), key=f"snooze_time_{tid}_{mode}",
            value="1d" if mode == "duration" else "", placeholder="2026-10-25 09:00" if mode == "until" else "2h",
        )
        st.caption(_t("snooze.web.time_hint", lang=lang, time=local_time(now)))
        note = st.text_input(_t("webapp.task_manager.note_label", lang=lang), key=f"snooze_note_{tid}")
        subtasks = st.checkbox(_t("snooze.help.subtasks", lang=lang), key=f"snooze_subtasks_{tid}")
        if st.button(_t("snooze.web.reschedule" if sleeping else "snooze.web.title", lang=lang), key=f"snooze_{tid}"):
            try:
                until = parse_snooze_time(duration=value if mode == "duration" else None,
                                           until=value if mode == "until" else None, now=now)
                store.snooze_task(tid, until, with_subtasks=subtasks, note=note, ts=now)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if sleeping and st.button(_t("snooze.web.wake", lang=lang), key=f"unsnooze_{tid}"):
            try:
                store.unsnooze_task(tid, with_subtasks=subtasks, ts=now)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


def _render_task_details(store: Store, task: Task, heap_max: int, now: datetime, *, lang: str) -> None:
    """Operate on the selected node; selection and browsing never record view events."""
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

    if task.root_id is None and not task.is_meta and task_id not in store.root_ids():
        if st.button(_t("inbox.move_button", lang=lang), key=f"inbox_move_{task_id}"):
            try:
                store.move_to_inbox(task_id, ts=now)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    parents = _parent_options(store, task_id)
    options = [0, *parents]
    parent = st.selectbox(
        _t("webapp.tree.move_to", lang=lang), options,
        index=options.index(task.root_id) if task.root_id in options else 0,
        format_func=lambda value: f"#{value} {parents[value].title}" if value else _t("webapp.tree.no_parent", lang=lang),
        key=f"parent_{task_id}",
    )
    if st.button(_t("webapp.tree.move_button", lang=lang), key=f"link_{task_id}"):
        try:
            store.unlink(task_id, ts=now) if not parent else store.link(task_id, parent, ts=now)
            st.session_state["tree_collapsed"] = set()
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    with st.form(f"subtask_{task_id}", clear_on_submit=True):
        st.caption(_t("webapp.tree.add_under", lang=lang, id=task_id, title=task.title))
        subtask_title = st.text_input(_t("webapp.tree.subtask_title", lang=lang), key=f"subtask_title_{task_id}")
        add_subtask = st.form_submit_button(_t("webapp.tree.add_subtask", lang=lang))
    if add_subtask:
        if store.heap_size() >= heap_max:
            st.error(_t("webapp.add.heap_full", lang=lang, size=store.heap_size(), max=heap_max))
        else:
            try:
                child = store.add_task(subtask_title, root_id=task_id, ts=now)
                st.session_state["selected_task_id"] = child.persisted_id
                st.session_state["tree_collapsed"] = set()
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

    _render_task_history(store, task_id, lang=lang)


def _render_capture(store: Store, heap_max: int, now: datetime, *, lang: str) -> None:
    with st.form("capture_idea", clear_on_submit=True):
        st.caption(_t("inbox.capture_hint", lang=lang))
        entry, submit = st.columns([8, 2])
        title = entry.text_input(
            _t("inbox.idea_title", lang=lang), key="capture_title",
            placeholder=_t("inbox.idea_title", lang=lang), label_visibility="collapsed",
        )
        captured = submit.form_submit_button(_t("inbox.capture_button", lang=lang), use_container_width=True)
    if captured:
        try:
            task = store.capture(title, heap_max=heap_max, ts=now)
            st.session_state["selected_task_id"] = task.persisted_id
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def _render_inbox_details(store: Store, task: Task, now: datetime, *, lang: str) -> None:
    tid = task.persisted_id
    st.caption(_t("inbox.details", lang=lang, time=task.inbox_since.isoformat(timespec="minutes") if task.inbox_since else "-"))
    if task.needs_inbox_review(now):
        st.warning(_t("inbox.needs_review", lang=lang))
    st.caption(_t("inbox.organize_hint", lang=lang))
    parents = _parent_options(store, tid)
    parent = st.selectbox(
        _t("webapp.tree.move_to", lang=lang), [0, *parents], key=f"inbox_parent_{tid}",
        format_func=lambda value: f"#{value} {parents[value].title}" if value else _t("inbox.choose_parent", lang=lang),
    )
    move, goal = st.columns(2)
    if move.button(_t("webapp.tree.move_button", lang=lang), key=f"inbox_organize_{tid}"):
        if not parent:
            st.error(_t("inbox.choose_parent", lang=lang))
        else:
            try:
                store.link(tid, parent, ts=now)
                st.session_state["tree_collapsed"] = set()
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    if goal.button(_t("inbox.make_goal", lang=lang), key=f"inbox_root_{tid}"):
        try:
            store.set_root(tid)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    note = st.text_input(_t("webapp.task_manager.note_label", lang=lang), key=f"inbox_note_{tid}")
    done, discard = st.columns(2)
    if done.button(_t("webapp.done.button", lang=lang), key=f"done_{tid}"):
        try:
            store.complete_task(tid, note=note, ts=now)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if discard.button(_t("inbox.discard_button", lang=lang), key=f"inbox_discard_{tid}"):
        try:
            store.discard_inbox(tid, note=note, ts=now)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    _render_task_history(store, tid, lang=lang)


def _render_task_tree(store: Store, heap_max: int, now: datetime, *, lang: str) -> None:
    # Scope layout rules to keyed tree controls; leave the rest of Streamlit unchanged.
    st.markdown(
        """<style>
        [class*="st-key-tree_select_"] button,
        [class*="st-key-tree_select_"] button > div,
        [class*="st-key-tree_select_"] [data-testid="stMarkdownContainer"] {
            justify-content: flex-start; text-align: left;
        }
        [class*="st-key-tree_select_"] button { min-height: 2rem; }
        [class*="st-key-tree_select_"] button[kind="secondary"] {
            border-color: transparent; background: transparent;
        }
        [class*="st-key-tree_toggle_"] button {
            padding: 0; min-width: 1.5rem; min-height: 2rem;
            border-color: transparent; background: transparent;
        }
        [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] >
            [data-testid="stVerticalBlock"] > [class*="st-key-tree_select_"]) {
            display: grid; gap: .25rem; align-items: center;
        }
        [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] >
            [data-testid="stVerticalBlock"] > [class*="st-key-tree_select_"])
            > [data-testid="stColumn"] { min-width: 0; width: 100%; }
        </style>""", unsafe_allow_html=True,
    )
    _render_capture(store, heap_max, now, lang=lang)
    show_completed = st.checkbox(_t("webapp.tree.show_completed", lang=lang), key="tree_show_completed")
    show_snoozed = st.checkbox(_t("snooze.show", lang=lang), key="tree_show_snoozed")
    roots = store.root_ids(active_only=False)
    tree = build_task_tree(store.list_tasks(record_view=False), roots, show_completed=show_completed,
                           show_snoozed=show_snoozed, now=now)
    if not tree.tasks:
        st.info(_t("webapp.tree.empty", lang=lang))
        return
    selected = st.session_state.get("selected_task_id")
    if selected not in tree.tasks:
        selected = next(tid for ids in tree.groups.values() for tid in ids)
        st.session_state["selected_task_id"] = selected
    left, right = st.columns([4, 6], gap="large")
    with left:
        _render_tree_nodes(tree, roots, selected, now, lang=lang)
    with right:
        task = tree.tasks[selected]
        st.text(f"#{task.id} {task.title}")
        st.caption(_t("webapp.tree.path", lang=lang))
        st.text(" / ".join(f"#{item.id} {item.title}" for item in tree.path(selected)))
        st.caption(_t("webapp.tree.generation_label", lang=lang, name=_t(f"webapp.tree.generation.{task.gen.value}", lang=lang)))
        if task.state.is_active:
            _render_snooze(store, task, now, lang=lang)
        if task.state is State.DONE:
            _render_selected_completed(store, task, heap_max, now, lang=lang)
        elif task.is_snoozed(now):
            _render_task_history(store, selected, lang=lang)
        elif task.in_inbox:
            _render_inbox_details(store, task, now, lang=lang)
        else:
            _render_task_details(store, task, heap_max, now, lang=lang)


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
                    _t("webapp.sidebar.heap_max_label", lang=lang), min_value=1, value=_default_heap_max(), step=1,
                    key="heap_max",
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

        st.title(_t("webapp.tree.title", lang=lang))
        _render_task_tree(store, heap_max, now, lang=lang)

        st.divider()
        st.subheader(_t("webapp.main.heap_title", lang=lang))
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

        st.subheader("Leak Inspector · doctor")
        st.code(report.format_doctor(store, result, now=now, lang=lang), language=None)

        st.subheader(_t("webapp.main.stats_subheader", lang=lang))
        st.code(report.format_stats(store.gc_runs(), lang=lang), language=None)

        st.subheader(_t("webapp.main.graveyard_subheader", lang=lang))
        _render_graveyard(store, heap_max, now, lang=lang)

        st.subheader(_t("webapp.completed.title", lang=lang))
        _render_completed(store, heap_max, now, lang=lang)
    finally:
        store.close()


if _running_under_streamlit():
    main()

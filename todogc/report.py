# report.py — presentation layer: renders a GC's GCResult as a JVM-flavored GC log, the
# reclaim candidate listing, and the per-generation taunts.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from todogc.collector import Candidate, GCResult, Reason
from todogc.heap import TENURING_THRESHOLD, Generation, Task, TouchKind
from todogc.i18n import get_locale
from todogc.i18n import t as _t
from todogc.store import GCRun, Store


def _fmt_uptime(td: timedelta | None) -> str:
    """Format "heap uptime" into the compact form used in the GC log header.

    Args:
        td (timedelta | None): time since the earliest task was created; None if unknown.

    Returns:
        str: a string like "3d12h"; "—" if td is None.
    """
    if td is None:
        return "—"
    return f"{td.days}d{td.seconds // 3600:02d}h"


def _fmt_duration(td: timedelta) -> str:
    """Format the ceremony's pause duration as HH:MM:SS.

    Args:
        td (timedelta): how long the pause (collection ceremony) lasted.

    Returns:
        str: a string like "00:04:12".
    """
    s = int(td.total_seconds())
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def generation_taunt(gen: Generation, age: int, *, lang: str | None = None) -> str:
    """Produce a taunt keyed off generation and age (age -> survivor/veteran/Tenured/Metaspace).

    Args:
        gen (Generation): the task's current generation.
        age (int): how many GC rounds it has survived.
        lang (str | None): output language; None uses the current global/default.

    Returns:
        str: the taunt for that tier.
    """
    if gen is Generation.META:
        return _t("report.taunt.meta", lang=lang)
    if gen is Generation.OLD or age >= TENURING_THRESHOLD:
        return _t("report.taunt.tenured", lang=lang)
    if age >= 3:
        return _t("report.taunt.veteran", lang=lang, age=age)
    if age >= 1:
        return _t("report.taunt.survivor", lang=lang)
    return _t("report.taunt.calm", lang=lang)


def format_gc_log(
    result: GCResult,
    *,
    gc_id: int = 0,
    uptime: timedelta | None = None,
    pause_full: bool = False,
    cause: str | None = None,
    pause: timedelta | None = None,
    heap_max: int | None = None,
    lang: str | None = None,
) -> str:
    """Render one GC as a multi-line report in JVM unified-logging style.

    Args:
        result (GCResult): this round's GC result.
        gc_id (int): GC sequence number, shown as GC(N) in the log.
        uptime (timedelta | None): heap uptime; shows "—" if None.
        pause_full (bool): True renders Pause Full, otherwise Pause Young.
        cause (str | None): the GC cause; defaults based on pause_full
            (Ergonomics / Allocation Failure) if None.
        pause (timedelta | None): the ceremony's pause duration; the "pause time" line is
            omitted if None.
        heap_max (int | None): heap cap (task count); the "(cap N)" suffix is omitted if
            None.
        lang (str | None): output language; None uses the current global/default.

    Returns:
        str: multi-line GC log text.
    """
    total = len(result.survivors) + len(result.candidates) + len(result.inbox)
    after = len(result.survivors) + len(result.inbox)
    reclaim = len(result.candidates)
    kind = "Full" if pause_full else "Young"
    cause = cause or ("Ergonomics" if pause_full else "Allocation Failure")
    cap = _t("report.gc_log.cap_suffix", lang=lang, heap_max=heap_max) if heap_max is not None else ""

    lines = [
        _t("report.gc_log.header", lang=lang, uptime=_fmt_uptime(uptime), gc_id=gc_id, kind=kind, cause=cause),
        _t("report.gc_log.active_heap", lang=lang, total=total, after=after, cap=cap),
        _t("report.gc_log.reclaimed", lang=lang, reclaim=reclaim, pct=result.released_pct),
    ]
    if result.cycles:
        titles = {c.task.id: c.task.title for c in result.candidates}
        first = result.cycles[0]
        names = " ⇄ ".join(f'"{titles.get(t, "#" + str(t))}"' for t in first.task_ids)
        lines.append(_t("report.gc_log.cycles", lang=lang, count=len(result.cycles), names=names))
    if pause is not None:
        lines.append(_t("report.gc_log.pause", lang=lang, duration=_fmt_duration(pause)))
    anxiety = round(reclaim * 0.12, 1)
    blame = round(reclaim * 0.05, 1)
    lines.append(_t("report.gc_log.times", lang=lang, anxiety=anxiety, blame=blame))
    return "\n".join(lines)


def format_candidates(
    result: GCResult, *, views_of: Callable[[int], int] | None = None, lang: str | None = None
) -> str:
    """Render the reclaim candidate listing grouped by cycle / unreachable / stale, for the ceremony.

    Args:
        result (GCResult): this round's GC result.
        views_of (Callable[[int], int] | None): optional callback that looks up view count by
            id, used to add a "viewed N times" taunt to stale entries; omitted if None.
        lang (str | None): output language; None uses the current global/default.

    Returns:
        str: multi-line candidate listing text; a rare affirmation if there are no candidates.
    """
    titles = {c.task.id: c.task.title for c in result.candidates}
    lines = [_t("report.candidates.header", lang=lang, count=len(result.candidates))]

    for cyc in result.cycles:
        lines.append(_t("report.candidates.cycle_line", lang=lang, id=cyc.id))
        for t in cyc.task_ids:
            lines.append(f"   ⇄ #{t} {titles.get(t, '')}")

    unreachable = [c for c in result.candidates if c.reason is Reason.UNREACHABLE]
    if unreachable:
        lines.append(_t("report.candidates.unreachable_line", lang=lang))
        for c in unreachable:
            lines.append(f"   · #{c.task.id} {c.task.title}")

    stale = [c for c in result.candidates if c.reason is Reason.STALE]
    if stale:
        lines.append(_t("report.candidates.stale_line", lang=lang))
        for c in stale:
            bits = []
            if c.metrics is not None:
                bits.append(_t("report.candidates.recent_progress", lang=lang, count=c.metrics.refcount_30d))
                skips = (
                    c.metrics.consecutive_skips
                    if c.metrics.consecutive_skips is not None
                    else c.metrics.skip_count
                )
                bits.append(_t("report.candidates.consecutive_skips", lang=lang, count=skips))
            if views_of is not None and c.task.id is not None:
                bits.append(_t("report.candidates.viewed", lang=lang, count=views_of(c.task.id)))
            suffix = f"  ({' · '.join(bits)})" if bits else ""
            lines.append(f"   · #{c.task.id} {c.task.title}{suffix}")

    if not result.candidates:
        lines.append(_t("report.candidates.empty", lang=lang))
    return "\n".join(lines)


# ---- Heap map (jmap-style overview: generation layout + -Xmx usage + zombie markers) ----

# Fixed display order and English/Chinese names for generations in the heap map
# (Eden → Survivor → Old → Metaspace).
_GEN_ORDER: tuple[tuple[Generation, str, str], ...] = (
    (Generation.EDEN, "Eden", "新生代"),
    (Generation.SURVIVOR, "Survivor", "幸存区"),
    (Generation.OLD, "Old", "老年代"),
    (Generation.META, "Metaspace", "元空间"),
)


class RowKind(str, Enum):
    """Marker category for a heap-map row; drives its prefix symbol and color, shared by the text heap map and the Web UI."""

    ROOT = "root"      # active target (GC root), marked ★
    META = "meta"      # meta-task that permanently lives in Metaspace, marked ⊛
    ZOMBIE = "zombie"  # reclaim candidate (zombie), marked ☠, highlighted red
    NORMAL = "normal"  # ordinary live object, unmarked
    SNOOZED = "snoozed"


# RowKind -> prefix symbol in the text heap map.
_ROW_MARKER: dict[RowKind, str] = {
    RowKind.ROOT: "★",
    RowKind.META: "⊛",
    RowKind.ZOMBIE: "☠",
    RowKind.NORMAL: " ",
    RowKind.SNOOZED: "⏸",
}


@dataclass
class HeapRow:
    """One row in the heap map: a heap object + its marker category + (if a candidate) the matching reclaim candidate.

    Attributes:
        task (Task): the heap object this row represents.
        kind (RowKind): its marker category (root / meta / zombie / normal).
        candidate (Candidate | None): the matching Candidate if this object is a reclaim
            candidate, otherwise None.
    """

    task: Task
    kind: RowKind
    candidate: Candidate | None = None


@dataclass
class HeapSection:
    """One generation section in the heap map: generation + English/Chinese name + its object rows (ascending by id).

    Attributes:
        gen (Generation): the generation.
        name_en (str): the English generation name (e.g. "Eden").
        name_cn (str): the Chinese generation name (e.g. "新生代").
        rows (list[HeapRow]): this generation's object rows, already sorted ascending by id.
    """

    gen: Generation
    name_en: str
    name_cn: str
    rows: list[HeapRow]

    def display_name(self, lang: str | None = None) -> str:
        return self.name_cn if (lang or get_locale()) == "zh" else self.name_en


def _row_kind(task: Task, roots: set[int], is_candidate: bool) -> RowKind:
    """Determine a row's marker category by priority: root(★) > meta-task(⊛) > zombie(☠) > normal(blank).

    A root and a meta-task are always live and never appear as candidates, so the three
    categories are mutually exclusive and this priority order is safe.

    Args:
        task (Task): the task being classified.
        roots (set[int]): ids of the active roots.
        is_candidate (bool): whether this task is a reclaim candidate this round.

    Returns:
        RowKind: the matching marker category.
    """
    if task.id in roots:
        return RowKind.ROOT
    if task.is_meta:
        return RowKind.META
    if is_candidate:
        return RowKind.ZOMBIE
    return RowKind.NORMAL


def heap_view(result: GCResult, roots: set[int]) -> list[HeapSection]:
    """Organize one GC result into a structured, per-generation heap view (the single source shared by the text heap map and the Web UI).

    Survivors and candidates form the execution heap; Inbox is displayed separately.
    Tasks are grouped by generation via
    _GEN_ORDER, sorted ascending by id within each generation, with each row's marker
    category and (if a candidate) its Candidate resolved. Already-collected objects are in
    the trash, not on the heap, and never appear here.

    Args:
        result (GCResult): one GC's result.
        roots (set[int]): ids of the active roots (used to mark ★).

    Returns:
        list[HeapSection]: sections ordered Eden → Survivor → Old → Metaspace; each section
            holds that generation's HeapRows, sorted ascending by id.
    """
    cand_of = {c.task.id: c for c in result.candidates}
    snoozed_ids = {task.id for task in result.snoozed}
    tasks = list(result.survivors) + [c.task for c in result.candidates]
    by_gen: dict[Generation, list[Task]] = {gen: [] for gen, _, _ in _GEN_ORDER}
    for t in tasks:
        by_gen.setdefault(t.gen, []).append(t)

    sections: list[HeapSection] = []
    for gen, en, cn in _GEN_ORDER:
        items = sorted(by_gen.get(gen, []), key=lambda t: t.id or 0)
        rows = [
            HeapRow(task=t, kind=RowKind.SNOOZED if t.id in snoozed_ids else _row_kind(t, roots, t.id in cand_of), candidate=cand_of.get(t.id))
            for t in items
        ]
        sections.append(HeapSection(gen=gen, name_en=en, name_cn=cn, rows=rows))
    return sections


def heap_used_count(result: GCResult) -> int:
    """Count heap objects counted toward -Xmx (excludes Metaspace meta-tasks, same accounting as store.heap_size).

    Args:
        result (GCResult): one GC's result, including Inbox capacity.

    Returns:
        int: total non-Metaspace heap objects.
    """
    tasks = list(result.survivors) + [c.task for c in result.candidates] + result.inbox
    return sum(1 for t in tasks if t.gen is not Generation.META)


def candidate_note(cand: Candidate, *, lang: str | None = None) -> str:
    """Condense a reclaim candidate's cause of death into a short annotation (shared source for the text heap map's parenthetical and the Web UI's badge).

    Args:
        cand (Candidate): the reclaim candidate.
        lang (str | None): output language; None uses the current global/default.

    Returns:
        str: a short string like "cycle #0" / "unreachable" / "stale · progressed 3 skipped 2" /
            "stale".
    """
    if cand.reason is Reason.CYCLE:
        return f"cycle #{cand.cycle_id}"
    if cand.reason is Reason.UNREACHABLE:
        return "unreachable"
    if cand.metrics is not None:
        skips = (
            cand.metrics.consecutive_skips
            if cand.metrics.consecutive_skips is not None
            else cand.metrics.skip_count
        )
        return _t(
            "report.candidate_note.stale_detail", lang=lang, progress=cand.metrics.refcount_30d, skips=skips
        )
    return "stale"


def _fill_bar(used: int, cap: int, *, width: int = 20) -> str:
    """Draw heap usage as a fixed-width [██░░] fill bar.

    Args:
        used (int): number of objects in use.
        cap (int): heap capacity (-Xmx); <= 0 is treated as unlimited and drawn as an
            all-empty bar.
        width (int): total number of cells in the bar.

    Returns:
        str: a fixed-width string like "[██████░░░░░░░░░░░░░░]" (brackets included); fills
            completely without overflowing when used exceeds cap.
    """
    if cap <= 0:
        return "[" + "░" * width + "]"
    filled = min(width, round(width * used / cap))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _heap_row_text(row: HeapRow, *, lang: str | None = None) -> str:
    """Render a HeapRow as one line of the text heap map: prefix marker + #id title + cause-of-death annotation.

    The marker and candidate info were already resolved in heap_view; this just handles the
    text presentation.

    Args:
        row (HeapRow): the heap-map row to render.
        lang (str | None): output language; None uses the current global/default.

    Returns:
        str: a single line like "  │   ☠ #3 Learn Rust  (cycle #0)" (with the left-hand
            vertical rail).
    """
    marker = _ROW_MARKER[row.kind]
    annot = f"  ({candidate_note(row.candidate, lang=lang)})" if row.candidate is not None else ""
    if row.kind is RowKind.SNOOZED and row.task.snoozed_until is not None:
        annot = "  (" + _t("snooze.until", lang=lang, time=row.task.snoozed_until.astimezone().isoformat(timespec="minutes")) + ")"
    return f"  │   {marker} #{row.task.id} {row.task.title}{annot}"


def format_heap_map(
    result: GCResult, roots: set[int], *, heap_max: int | None = None, lang: str | None = None
) -> str:
    """Render the current active heap as a JVM-style generational heap map (zombies marked with ☠).

    Lists heap objects generation by generation (Eden / Survivor / Old / Metaspace), with a
    -Xmx usage fill bar at the top, and annotates each reclaim candidate with its cause of
    death. Already-collected objects are in the trash, not on the heap, and never appear in
    this map; Metaspace (meta-tasks) doesn't count toward -Xmx usage, same accounting as
    store.heap_size.

    Args:
        result (GCResult): one GC's result, including Inbox capacity.
        roots (set[int]): ids of the active roots (marked ★).
        heap_max (int | None): heap capacity (-Xmx); draws the usage fill bar if given,
            otherwise just reports the object count.
        lang (str | None): output language; None uses the current global/default.

    Returns:
        str: multi-line heap map text.
    """
    used = heap_used_count(result)
    if heap_max is not None:
        pct = round(100 * used / heap_max, 1) if heap_max > 0 else 0.0
        usage = _t(
            "report.heap_map.usage_with_max",
            lang=lang,
            used=used,
            max=heap_max,
            bar=_fill_bar(used, heap_max),
            pct=pct,
        )
    else:
        usage = _t("report.heap_map.usage_no_max", lang=lang, used=used)

    lines = [_t("report.heap_map.title", lang=lang), usage, ""]
    if result.inbox:
        lines.append(_t("inbox.heap_summary", lang=lang, count=len(result.inbox)))
        lines.append("")
    for i, section in enumerate(heap_view(result, roots)):
        conn = "┌" if i == 0 else "├"
        lines.append(f"  {conn} {section.display_name(lang)} ({len(section.rows)})")
        if section.rows:
            lines.extend(_heap_row_text(r, lang=lang) for r in section.rows)
        else:
            lines.append(_t("report.heap_map.empty_row", lang=lang))
    lines.append("  └" + "─" * 14)
    lines.append(_t("report.heap_map.legend", lang=lang))
    return "\n".join(lines)


def format_report(
    result: GCResult,
    *,
    views_of: Callable[[int], int] | None = None,
    gc_id: int = 0,
    uptime: timedelta | None = None,
    pause_full: bool = False,
    cause: str | None = None,
    pause: timedelta | None = None,
    heap_max: int | None = None,
    lang: str | None = None,
) -> str:
    """Combine the GC log and the candidate listing into one full reclaim report.

    Args:
        result (GCResult): this round's GC result.
        views_of (Callable[[int], int] | None): passed through to format_candidates as the
            view-count callback.
        lang (str | None): output language; passed through to both format_gc_log and
            format_candidates.
        Remaining parameters: passed through to format_gc_log.

    Returns:
        str: GC log + blank line + candidate listing.
    """
    log = format_gc_log(
        result,
        gc_id=gc_id,
        uptime=uptime,
        pause_full=pause_full,
        cause=cause,
        pause=pause,
        heap_max=heap_max,
        lang=lang,
    )
    return log + "\n\n" + format_candidates(result, views_of=views_of, lang=lang)


def format_doctor(store: Store, result: GCResult, *, now: datetime | None = None, lang: str | None = None) -> str:
    """Generate the cognitive-memory-leak diagnosis; read-only, produces no VIEW events."""
    now = now or datetime.now(timezone.utc)
    candidates = {c.task.id: c for c in result.candidates}
    roots = store.root_ids()
    findings: list[tuple[int, Task, list[str], list[str]]] = []
    for task in store.list_tasks(record_view=False):
        if not task.state.is_active or task.in_inbox or task.is_snoozed(now) or task.id is None:
            continue
        views = store.view_count(task.id)
        progress = store.refcount(task.id)
        skips = store.consecutive_skip_count(task.id)
        touches = store.touches(task.id)
        churn = sum(t.kind in {TouchKind.COLLECT, TouchKind.RESTORE, TouchKind.REVIVE} for t in touches)
        issues: list[str] = []
        advice: list[str] = []
        score = 0
        if views >= 10 and progress == 0:
            issues.append(_t("report.doctor.viewed_no_progress", lang=lang, views=views))
            advice.append(_t("report.doctor.advice.rename_actionable", lang=lang))
            score += min(40, views)
        if skips >= 3:
            issues.append(_t("report.doctor.consecutive_skips", lang=lang, skips=skips))
            advice.append(_t("report.doctor.advice.progress_or_admit", lang=lang))
            score += skips * 8
        if task.gen is Generation.OLD and progress == 0:
            issues.append(_t("report.doctor.old_zero_progress", lang=lang, age=task.age))
            advice.append(_t("report.doctor.advice.downgrade_weak", lang=lang))
            score += 35
        if churn >= 3:
            issues.append(_t("report.doctor.churn", lang=lang, churn=churn))
            advice.append(_t("report.doctor.advice.relink_or_collect", lang=lang))
            score += churn * 6
        cand = candidates.get(task.id)
        if cand is not None and cand.reason is Reason.CYCLE:
            issues.append(_t("report.doctor.in_cycle", lang=lang, cycle_id=cand.cycle_id))
            advice.append(_t("report.doctor.advice.break_cycle", lang=lang))
            score += 50
        elif cand is not None and cand.reason is Reason.UNREACHABLE:
            issues.append(_t("report.doctor.unreachable_from_roots", lang=lang))
            advice.append(_t("report.doctor.advice.link_or_collect", lang=lang))
            score += 25
        elif cand is not None and cand.reason is Reason.STALE:
            issues.append(_t("report.doctor.stale_by_policy", lang=lang))
            advice.append(_t("report.doctor.advice.log_progress_or_adjust", lang=lang))
            score += 20
        if issues:
            if task.id in roots:
                issues.append(_t("report.doctor.is_root", lang=lang))
            findings.append((score, task, issues, list(dict.fromkeys(advice))))

    if not findings:
        return _t("report.doctor.ok", lang=lang)

    lines = [f"[WARN] Suspected cognitive memory leaks: {len(findings)}"]
    for score, task, issues, advice in sorted(findings, key=lambda x: (-x[0], x[1].id or 0)):
        age_days = (now - task.created_at).days if task.created_at is not None else 0
        lines.extend(
            [
                "",
                f"  #{task.id} {task.title}",
                f"  risk={score} · retained={age_days}d · ref={task.ref_strength.value}",
                *(f"  ! {issue}" for issue in issues),
                *(f"  → {tip}" for tip in advice),
            ]
        )
    return "\n".join(lines)


def format_stats(runs: list[GCRun], *, lang: str | None = None) -> str:
    """Summarize the GC audit history."""
    if not runs:
        return _t("report.stats.empty", lang=lang)
    total_candidates = sum(r.candidates for r in runs)
    total_collected = sum(r.collected for r in runs)
    total_revived = sum(r.revived for r in runs)
    avg_release = sum(r.released_pct for r in runs) / len(runs)
    avg_heap = sum(r.heap_before for r in runs) / len(runs)
    revival_rate = 0.0 if total_candidates == 0 else 100 * total_revived / total_candidates
    modes: dict[str, int] = {}
    for run in runs:
        modes[run.mode] = modes.get(run.mode, 0) + 1
    mode_text = ", ".join(f"{k}={v}" for k, v in sorted(modes.items()))
    return "\n".join(
        [
            _t("report.stats.title", lang=lang),
            f"  runs              : {len(runs)} ({mode_text})",
            f"  average heap       : {avg_heap:.1f} obj",
            f"  candidates         : {total_candidates}",
            f"  collected / revived: {total_collected} / {total_revived}",
            f"  average release    : {avg_release:.1f}%",
            f"  revival rate       : {revival_rate:.1f}%",
            f"  latest             : {runs[0].started_at.isoformat()}",
        ]
    )

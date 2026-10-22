# cli.py — the command-line entry point: strings together the add/mark/dep/GC pipeline, with
# the Stop-the-world collection ceremony (revive or let go) at its core.

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from todogc import console, report
from todogc.collector import Candidate, Collector, GCPolicy, GCResult
from todogc.config import AppConfig, ConfigError, load_config
from todogc.heap import ReferenceStrength, State, TouchKind
from todogc.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, set_locale
from todogc.i18n import t as _t
from todogc.query import SCOPES, SORTS, TaskQuery, query_tasks
from todogc.store import Store
from todogc.scheduling import local_time, parse_snooze_time
from todogc.review import WeeklyReview, build_review, format_review
from todogc.review_card import export_review
from todogc.tree import format_task_tree, task_label
from todogc.today import build_today
from todogc.watcher import Watcher
from todogc.watch_service import (
    WatchServiceError, configure_watch_logging, format_service_status,
    read_watch_logs, send_test, service_status, start_background, stop_watch,
)

HEAP_MAX = 50  # -Xmx: the max number of tasks you can carry at once, used only for GC log display.


# ============ Collection ceremony (the testable core) ============

@dataclass
class CeremonyOutcome:
    """The outcome of one Stop-the-world ceremony.

    Attributes:
        revived (list[int]): ids of revived tasks.
        collected (list[int]): ids of collected tasks.
        skipped (list[int]): ids of skipped (deferred) tasks.
    """

    revived: list[int] = field(default_factory=list)
    collected: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)


def _finalize_spent(store: Store, task_id: int) -> bool:
    """Determine whether a task's finalize() has already been used up (it was revived once before).

    Mirrors the JVM rule: an object's finalize() is called at most once; once revived, the
    next collection no longer triggers it.

    Args:
        store (Store): the data source.
        task_id (int): the task id.

    Returns:
        bool: True if REVIVE appears in its touch history.
    """
    return any(t.kind is TouchKind.REVIVE for t in store.touches(task_id))


def run_ceremony(
    store: Store,
    result: GCResult,
    *,
    decide: Callable[[Candidate], str],
    eulogize: Callable[[Candidate], str] | None = None,
    revive_root: Callable[[Candidate], int | None] | int | None = None,
    now: datetime | None = None,
) -> CeremonyOutcome:
    """Execute a decision for each candidate in the listing: revive / collect / skip (the ceremony's pure-logic core, decoupled from interaction).

    On collect, if the task has never been revived before, calls finalize() to collect a
    eulogy first; if it has, just clears it and stamps "finalize() already called". Revive
    bumps age +1 (survived a round) and re-links the root if needed.

    Args:
        store (Store): the data source; this method writes to it.
        result (GCResult): the GC result to process.
        decide (Callable[[Candidate], str]): the disposition decision, returning "revive" /
            "collect" / "skip".
        eulogize (Callable[[Candidate], str] | None): callback that collects the eulogy;
            None means a silent collection.
        revive_root (Callable[[Candidate], int | None] | int | None): on revive, if the task
            has no root, used to get the root id to re-link to; can be a fixed int or a
            callback, None means no re-link.
        now (datetime | None): the event time base; None uses the current UTC time.

    Returns:
        CeremonyOutcome: id lists for each disposition category.
    """
    now = now or datetime.now(timezone.utc)
    out = CeremonyOutcome()
    for cand in result.candidates:
        task = cand.task
        assert task.id is not None
        choice = decide(cand)
        current = store.get_task(task.id, record_view=False)
        if current is None or not current.state.is_active or current.in_inbox or current.is_snoozed(now):
            out.skipped.append(task.id)
            continue
        task = current
        assert task.id is not None
        if choice == "collect":
            if _finalize_spent(store, task.id):
                note = _t("cli.ceremony.finalize_spent")
            else:
                eulogy = eulogize(cand).strip() if eulogize else ""
                note = eulogy or _t("cli.ceremony.silent_collect")
            if store.collect_candidate(task.persisted_id, note=note, ts=now):
                out.collected.append(task.persisted_id)
            else:
                out.skipped.append(task.persisted_id)
        elif choice == "revive":
            if task.is_unlinked and revive_root is not None:
                r = revive_root if isinstance(revive_root, int) else revive_root(cand)
                if r is not None:
                    store.link(task.id, r, ts=now)
                    task.root_id = r
            survivor = task.aged()  # survived a round: age +1, generation promoted accordingly
            survivor.state = State.LIVE
            store.update_task(survivor)
            store.record(task.id, TouchKind.REVIVE, ts=now)
            out.revived.append(task.id)
        else:
            out.skipped.append(task.id)
    return out


# ============ Subcommands ============

def _run_gc(store: Store, now: datetime, policy: GCPolicy | None = None) -> GCResult:
    """Build the reference graph and run one mark-and-sweep (shared by add's OOM check and the gc command).

    Args:
        store (Store): the data source.
        now (datetime): the liveness time base.

    Returns:
        GCResult: this round's GC result.
    """
    return Collector(policy).run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)


def cmd_add(store: Store, args: argparse.Namespace) -> int:
    """add: record (allocate) a task; triggers Allocation Failure when the active heap (excluding Metaspace) is full, forcing a reclaim first.

    Args:
        store (Store): the data source.
        args (argparse.Namespace): needs title / root_id / root / meta / force / heap_max.

    Returns:
        int: exit code; returns 1 (allocation refused) when the heap is full, it's not a
            meta-task, and --force wasn't given.
    """
    size = store.heap_size()
    if not args.force and not args.meta and size >= args.heap_max:
        result = _run_gc(store, datetime.now(timezone.utc), args.gc_policy)
        lines = [_t("cli.add.heap_full", size=size, max=args.heap_max)]
        if result.candidates:
            lines.append(report.format_candidates(result, views_of=store.view_count))
            lines.append(_t("cli.add.hint_gc_or_force"))
        else:
            lines.append(_t("cli.add.full_gc_no_candidates"))
            lines.append(_t("cli.add.hint_cut_scope"))
        block = "\n".join(lines)
        if not console.print_text(block, err=True):
            print(block, file=sys.stderr)
        return 1
    try:
        t = store.add_task(
            args.title,
            root_id=args.root_id,
            is_root=args.root,
            meta=args.meta,
            ref_strength=args.ref_strength,
        )
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    tag = " ★root" if args.root else (" [meta]" if args.meta else "")
    print(_t("cli.add.allocated", id=t.id, title=t.title, tag=tag))
    return 0


def cmd_root(store: Store, args: argparse.Namespace) -> int:
    """root: mark / unmark a task as an active GC root.

    Args:
        store (Store): the data source.
        args (argparse.Namespace): needs id / off.

    Returns:
        int: exit code.
    """
    try:
        store.set_root(args.id, not args.off)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("cli.root.off" if args.off else "cli.root.on", id=args.id))
    return 0


def cmd_dep(store: Store, args: argparse.Namespace) -> int:
    """dep: add / remove a "reason" edge (a task's reason is its ref).

    Args:
        store (Store): the data source.
        args (argparse.Namespace): needs task_id / ref_id / remove.

    Returns:
        int: exit code; returns 1 for invalid input such as a self-loop.
    """
    try:
        if args.remove:
            store.remove_dep(args.task_id, args.ref_id)
            print(_t("cli.dep.removed", task_id=args.task_id, ref_id=args.ref_id))
        else:
            store.add_dep(args.task_id, args.ref_id)
            print(_t("cli.dep.added", task_id=args.task_id, ref_id=args.ref_id))
    except ValueError as e:
        print(_t("cli.error", error=e), file=sys.stderr)
        return 1
    return 0


def cmd_ls(store: Store, args: argparse.Namespace) -> int:
    """ls: list tasks (records a view event), marking generation / age / root / link / state.

    Args:
        store (Store): the data source.
        args (argparse.Namespace): needs state.

    Returns:
        int: exit code.
    """
    now = datetime.now(timezone.utc)
    state = State(args.state) if args.state else None
    is_search = args.cmd == "search"
    scope = args.scope or ("all" if is_search or state is not None else "active")
    candidates = None
    if state is State.CANDIDATE:
        result = _run_gc(store, now, args.gc_policy)
        candidates = {c.task.persisted_id for c in result.candidates}
    try:
        tasks = query_tasks(store, TaskQuery(
            text=args.query if is_search else args.search, scope=scope, root_id=args.root_id,
            sort=args.sort, notes=args.notes, limit=args.limit, state=state,
        ), now=now, candidate_ids=candidates)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    if not is_search and state is not State.CANDIDATE:
        store.record_views(tasks, ts=now)
    roots = store.root_ids()
    if not tasks:
        print(_t("query.empty"))
        return 0
    if console.render_ls(tasks, roots):  # rich installed -> color table
        return 0
    for t in tasks:
        mark = "★" if t.id in roots else " "
        link = f" →#{t.root_id}" if t.root_id is not None else ""
        meta = " [meta]" if t.is_meta else " [inbox]" if t.in_inbox else ""
        print(f" {mark} #{t.id:>3} [{t.gen.value}:{t.age}] {t.title}{link}{meta}  <{t.state.value}>")
    return 0


def cmd_today(store: Store, args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    plan = build_today(store, now=now)
    roots = store.root_ids(active_only=False)
    print(_t("today.heading", day=plan.day.isoformat()))
    if not plan.selected:
        print(_t("today.empty"))
    for i, task in enumerate(plan.selected, 1):
        print(f" {i}. {task_label(task, roots, now=now)} [{task.state.value}]")
    for key, tasks in (("carryover", plan.carryover), ("waking", plan.waking), ("completed", plan.completed)):
        if tasks:
            print("\n" + _t(f"today.{key}"))
            for task in tasks:
                print(" " + task_label(task, roots, now=now))
    print("\n" + _t("today.cli.hint"))
    return 0


def cmd_focus(store: Store, args: argparse.Namespace) -> int:
    try:
        if args.cmd == "unfocus":
            store.unfocus_task(args.id)
            message = "today.removed"
        else:
            store.focus_task(args.id, position=args.position, with_subtasks=args.with_subtasks)
            message = "today.added_subtasks" if args.with_subtasks else "today.added"
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t(message, id=args.id))
    return 0


def cmd_progress(store: Store, args: argparse.Namespace) -> int:
    """progress: log one bit of real progress (counted toward refcount).

    Args:
        store (Store): the data source.
        args (argparse.Namespace): needs id / note.

    Returns:
        int: exit code.
    """
    if store.get_task(args.id, record_view=False) is None:
        print(_t("cli.error.not_found", id=args.id), file=sys.stderr)
        return 1
    try:
        store.progress(args.id, args.note)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("cli.progress.done", id=args.id))
    return 0


def cmd_snooze(store: Store, args: argparse.Namespace) -> int:
    """Schedule a task to return later; never counts as progress or a skip."""
    try:
        until = parse_snooze_time(duration=args.duration, until=args.until)
        ids = store.snooze_task(args.id, until, with_subtasks=args.with_subtasks, note=args.note)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("snooze.scheduled", count=len(ids), time=local_time(until)))
    return 0


def cmd_unsnooze(store: Store, args: argparse.Namespace) -> int:
    try:
        ids = store.unsnooze_task(args.id, with_subtasks=args.with_subtasks)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("snooze.woken", count=len(ids)))
    return 0


def cmd_snoozed(store: Store, args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    tasks = store.list_tasks(active_only=True, snoozed=True, record_view=False, ts=now)
    print(_t("snooze.list_title", count=len(tasks)))
    for task in tasks:
        print(task_label(task, store.root_ids(), now=now))
    return 0


def cmd_defer(store: Store, args: argparse.Namespace) -> int:
    """defer: record one skip without scheduling a wake-up.

    Args:
        store (Store): the data source.
        args (argparse.Namespace): needs id / note.

    Returns:
        int: exit code.
    """
    if store.get_task(args.id, record_view=False) is None:
        print(_t("cli.error.not_found", id=args.id), file=sys.stderr)
        return 1
    try:
        store.defer(args.id, args.note)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("cli.snooze.done", id=args.id))
    return 0


def cmd_done(store: Store, args: argparse.Namespace) -> int:
    try:
        ids = store.complete_task(args.id, with_subtasks=args.with_subtasks, note=args.note)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("cli.done.success", ids=", ".join(f"#{tid}" for tid in ids)))
    return 0


def cmd_reopen(store: Store, args: argparse.Namespace) -> int:
    try:
        store.reopen_task(args.id, root_id=args.root_id, heap_max=args.heap_max, force=args.force)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("cli.reopen.success", id=args.id))
    return 0


def cmd_completed(store: Store, args: argparse.Namespace) -> int:
    tasks = store.list_tasks(state=State.DONE, record_view=False)
    if not tasks:
        print(_t("cli.completed.empty"))
    for task in tasks:
        print(f"#{task.id} {task.title}  completed={task.completed_at.isoformat() if task.completed_at else '-'}")
    return 0


def cmd_capture(store: Store, args: argparse.Namespace) -> int:
    try:
        task = store.capture(args.title, heap_max=args.heap_max, force=args.force)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("inbox.captured", id=task.id))
    return 0


def cmd_inbox(store: Store, args: argparse.Namespace) -> int:
    if args.move is not None:
        try:
            store.move_to_inbox(args.move)
        except ValueError as exc:
            print(_t("cli.error", error=exc), file=sys.stderr)
            return 1
        print(_t("inbox.moved", id=args.move))
        return 0
    now = datetime.now(timezone.utc)
    tasks = store.list_tasks(active_only=True, inbox=True, snoozed=False, record_view=False, ts=now)
    print(_t("inbox.list_title", count=len(tasks)))
    if not tasks:
        print(_t("inbox.empty"))
    for task in tasks:
        print(task_label(task, set(), now=now))
    return 0


def cmd_discard(store: Store, args: argparse.Namespace) -> int:
    try:
        store.discard_inbox(args.id, note=args.note)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("inbox.discarded", id=args.id))
    return 0


def cmd_tree(store: Store, args: argparse.Namespace) -> int:
    print(format_task_tree(
        store.list_tasks(record_view=False), store.root_ids(active_only=False),
        show_completed=args.show_completed, show_snoozed=args.show_snoozed,
    ))
    return 0


def cmd_gc(store: Store, args: argparse.Namespace) -> int:
    """gc: run mark-and-sweep, print the GC log and candidate listing, then enter the Stop-the-world collection ceremony.

    Args:
        store (Store): the data source.
        args (argparse.Namespace): needs full / dry_run.

    Returns:
        int: exit code.
    """
    now = datetime.now(timezone.utc)
    result = _run_gc(store, now, args.gc_policy)
    heap_before = store.heap_size()
    live = store.list_tasks(record_view=False)
    earliest = min((t.created_at for t in live if t.created_at is not None), default=None)
    uptime = (now - earliest) if earliest is not None else None

    body = (
        report.format_gc_log(result, uptime=uptime, pause_full=args.full, heap_max=args.heap_max)
        + "\n\n"
        + report.format_candidates(result, views_of=store.view_count)
    )
    if not console.print_text(body):  # rich installed -> keyword highlighting; otherwise plain text
        print(body)

    if args.dry_run:
        return 0

    if not result.candidates:
        store.record_gc_run(
            started_at=now,
            mode=args.gc_policy.mode,
            heap_before=heap_before,
            survivors=len(result.survivors) + len(result.inbox),
            candidates=0,
            collected=0,
            revived=0,
            released_pct=0.0,
        )
        return 0

    print(_t("cli.gc.stw_banner"))

    def decide(cand: Candidate) -> str:
        print(f"\n  #{cand.task.id} {cand.task.title}  [{cand.reason.value}]")
        print(f"    {report.generation_taunt(cand.task.gen, cand.task.age)}")
        while True:
            ans = input(_t("cli.gc.prompt_choice")).strip().lower()
            if ans in {"r", "c", "s"}:
                return {"r": "revive", "c": "collect", "s": "skip"}[ans]

    def eulogize(cand: Candidate) -> str:
        return input(_t("cli.gc.prompt_eulogy"))

    def revive_root(cand: Candidate) -> int | None:
        roots = sorted(store.root_ids())
        if not roots:
            print(_t("cli.gc.no_root_to_revive"))
            return None
        return roots[0]

    out = run_ceremony(store, result, decide=decide, eulogize=eulogize, revive_root=revive_root, now=now)
    actions = {
        **{tid: "revive" for tid in out.revived},
        **{tid: "collect" for tid in out.collected},
        **{tid: "skip" for tid in out.skipped},
    }
    store.record_gc_run(
        started_at=now,
        mode=args.gc_policy.mode,
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
    print(_t("cli.gc.ceremony_done", revived=len(out.revived), collected=len(out.collected), skipped=len(out.skipped)))
    return 0


def cmd_heap(store: Store, args: argparse.Namespace) -> int:
    """heap (alias jmap): print the generational heap map — per-generation object distribution, -Xmx usage, zombies (☠) marked red.

    A read-only diagnostic snapshot: runs one mark-and-sweep to get the judgment, but never
    enters the collection ceremony, never writes to the store, and never records a view event
    (reuses the GC scan's record_view=False accounting, so looking at the heap map never
    inflates view counts).

    Args:
        store (Store): the data source.
        args (argparse.Namespace): needs heap_max.

    Returns:
        int: exit code (always 0).
    """
    now = datetime.now(timezone.utc)
    result = _run_gc(store, now, args.gc_policy)
    body = report.format_heap_map(result, store.root_ids(), heap_max=args.heap_max)
    if not console.print_text(body):  # rich installed -> zombies red/★ gold/fill bar colored; otherwise plain text
        print(body)
    return 0


def cmd_web(store: Store, args: argparse.Namespace) -> int:
    """web: launch the Streamlit Web UI (the heap map in the browser + add/gc/restore interactions).

    Runs `python -m streamlit run webapp.py` as a subprocess, passing the current --db /
    --heap-max through via environment variables (TODOGC_DB / TODOGC_HEAP_MAX) so it uses the
    same database as the CLI. Just prints an install hint and never raises if streamlit isn't
    installed. This command doesn't use store (it only needs the path argument); store is
    opened uniformly by main() and ignored here.

    Args:
        store (Store): the data source (unused by this command; present only to keep the
            subcommand signature uniform).
        args (argparse.Namespace): needs db / heap_max / port.

    Returns:
        int: the streamlit subprocess's exit code; returns 1 if streamlit isn't installed.
    """
    if importlib.util.find_spec("streamlit") is None:
        print(_t("cli.web.not_installed"), file=sys.stderr)
        return 1
    webapp = os.path.join(os.path.dirname(__file__), "webapp.py")
    env = {
        **os.environ,
        "TODOGC_DB": args.db,
        "TODOGC_HEAP_MAX": str(args.heap_max),
        "TODOGC_GC_MODE": args.gc_policy.mode,
        "TODOGC_RECENT_DAYS": str(args.gc_policy.recent_days),
        "TODOGC_MAX_SKIPS": str(args.gc_policy.max_skips),
        "TODOGC_RESTORE_GRACE_DAYS": str(args.gc_policy.restore_grace_days),
        "TODOGC_LANG": args.app_config.lang,
    }
    cmd = [sys.executable, "-m", "streamlit", "run", webapp]
    if args.port is not None:
        cmd += ["--server.port", str(args.port)]
    print(_t("cli.web.starting", db=args.db, heap_max=args.heap_max))
    return subprocess.run(cmd, env=env).returncode


def cmd_watch(store: Store | None, args: argparse.Namespace) -> int:
    """watch: scheduled checkup — polls liveness, pushes an ntfy notification on state transitions/heartbeat digests/heap-usage alerts.

    The Watcher opens its own independent Store connection to run its loop (unrelated to the
    store main() opens uniformly, same treatment of the store argument as cmd_web). Ctrl-C
    (SIGINT) or SIGTERM both make the loop exit gracefully and release the single-instance
    lock after the next heartbeat tick (<=30s).

    Args:
        store (Store): the data source (unused by this command; present only to keep the
            subcommand signature uniform).
        args (argparse.Namespace): needs db and app_config (the full AppConfig loaded in
            main()).

    Returns:
        int: exit code; returns 1 if notify.ntfy_enabled isn't on / ntfy_topic isn't
            configured.
    """
    cfg = replace(args.app_config, heap_max=args.heap_max, gc_mode=args.gc_policy.mode,
                  recent_days=args.gc_policy.recent_days, max_consecutive_skips=args.gc_policy.max_skips,
                  restore_grace_days=args.gc_policy.restore_grace_days)
    try:
        if args.watch_action == "status":
            status = service_status(args.db, cfg)
            status["current_config_available"] = not args.watch_config_unavailable
            if args.json:
                print(json.dumps(status, indent=2))
            else:
                print(format_service_status(status))
                if args.watch_config_unavailable:
                    print(_t("service.config_unavailable"))
        elif args.watch_action == "logs":
            print(read_watch_logs(args.db, lines=args.lines) or _t("service.logs.empty"))
        elif args.watch_action == "test":
            send_test(args.db, cfg)
            print(_t("service.test.sent"))
        elif args.watch_action == "start":
            status = start_background(args.db, cfg)
            print(_t("service.start.result", state=_t(f"service.state.{status['state']}"),
                     mode=status["mode"] or "-", pid=status["pid"] or "-"))
        elif args.watch_action == "stop":
            status = stop_watch(args.db, cfg)
            print(_t("service.stop.done" if status["state"] in {"stopped", "failed"} else "service.stop.pending"))
        else:
            if not cfg.ntfy_enabled:
                raise WatchServiceError(_t("cli.watch.not_configured"))
            configure_watch_logging(args.db)
            watcher = Watcher(args.db, cfg)
            previous_handler = signal.signal(signal.SIGTERM, lambda signum, frame: watcher.stop())
            print(_t("cli.watch.starting", db=args.db, poll_seconds=cfg.watch_poll_seconds))
            try:
                if not watcher.run():
                    return 1
            except KeyboardInterrupt:
                pass
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
            print(_t("cli.watch.stopped"))
        return 0
    except (WatchServiceError, OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1


def cmd_graveyard(store: Store, args: argparse.Namespace) -> int:
    """graveyard: the epitaph wall, listing epitaphs and eulogies for collected tasks.

    Args:
        store (Store): the data source.
        args (argparse.Namespace): no extra fields.

    Returns:
        int: exit code.
    """
    dead = store.list_tasks(state=State.COLLECTED, record_view=False)
    if not dead:
        print(_t("cli.graveyard.empty"))
        return 0
    for t in dead:
        task_id = t.persisted_id
        collects = [x for x in store.touches(task_id) if x.kind is TouchKind.COLLECT]
        last = collects[-1] if collects else None
        days = (last.ts - t.created_at).days if last and t.created_at else 0
        print(
            _t(
                "cli.graveyard.entry",
                title=t.title,
                days=days,
                refcount=store.refcount(task_id),
                views=store.view_count(task_id),
            )
        )
        print(_t("cli.graveyard.epitaph", note=last.note if last else ""))
    return 0


def cmd_restore(store: Store, args: argparse.Namespace) -> int:
    """restore: restore a task from the trash back to live (a safety net for slip-ups).

    Args:
        store (Store): the data source.
        args (argparse.Namespace): needs id.

    Returns:
        int: exit code; returns 1 if the task doesn't exist.
    """
    try:
        store.restore_task(args.id, root_id=args.root_id, heap_max=args.heap_max, force=args.force)
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("cli.restore.done", id=args.id))
    return 0


def cmd_show(store: Store, args: argparse.Namespace) -> int:
    """Show task details and key metrics; does not write a VIEW event."""
    task = store.get_task(args.id, record_view=False)
    if task is None:
        print(_t("cli.error.not_found", id=args.id), file=sys.stderr)
        return 1
    task_id = task.persisted_id
    roots = store.root_ids()
    print(f"#{task_id} {task.title}")
    if task.in_inbox:
        print(_t("inbox.details", time=task.inbox_since.isoformat() if task.inbox_since else "-"))
    if task.snoozed_until is not None:
        print(_t("snooze.until" if task.is_snoozed(datetime.now(timezone.utc)) else "snooze.returned",
                 time=local_time(task.snoozed_until)))
    if task.completed_at is not None:
        print(f"  completed={task.completed_at.isoformat()}")
    print(
        f"  state={task.state.value} gen={task.gen.value}:{task.age} "
        f"ref={task.ref_strength.value} root={'yes' if task_id in roots else 'no'} "
        f"parent={task.root_id or '-'}"
    )
    print(
        f"  progress={store.refcount(task_id)} views={store.view_count(task_id)} "
        f"skips={store.skip_count(task_id)} "
        f"consecutive_skips={store.consecutive_skip_count(task_id)}"
    )
    deps = [ref for tid, ref in store.deps() if tid == task_id]
    print(f"  deps={','.join(map(str, deps)) if deps else '-'}")
    return 0


def cmd_history(store: Store, args: argparse.Namespace) -> int:
    """Print the append-only event timeline."""
    if store.get_task(args.id, record_view=False) is None:
        print(_t("cli.error.not_found", id=args.id), file=sys.stderr)
        return 1
    touches = store.touches(args.id)
    if args.limit is not None:
        touches = touches[-args.limit:]
    for touch in touches:
        note = f" · {touch.note}" if touch.note else ""
        print(f"{touch.ts.isoformat()}  {touch.kind.value}{note}")
    return 0


def cmd_edit(store: Store, args: argparse.Namespace) -> int:
    """Edit a task's title/reference strength."""
    if args.title is None and args.ref_strength is None:
        print(_t("cli.edit.needs_field"), file=sys.stderr)
        return 1
    try:
        task = store.edit_task(args.id, title=args.title, ref_strength=args.ref_strength)
    except (ValueError, KeyError) as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    print(_t("cli.edit.done", id=task.id, title=task.title, ref=task.ref_strength.value))
    return 0


def cmd_link(store: Store, args: argparse.Namespace) -> int:
    """Link or unlink a parent/target association."""
    try:
        if args.off:
            store.unlink(args.id)
            print(_t("cli.link.unlinked", id=args.id))
        else:
            if args.root_id is None:
                raise ValueError(_t("cli.link.needs_root_id"))
            store.link(args.id, args.root_id)
            print(_t("cli.link.linked", id=args.id, root_id=args.root_id))
    except ValueError as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    return 0


def cmd_doctor(store: Store, args: argparse.Namespace) -> int:
    """Scan for cognitive memory leaks."""
    now = datetime.now(timezone.utc)
    result = _run_gc(store, now, args.gc_policy)
    print(report.format_doctor(store, result, now=now))
    return 0


def _days(value: str) -> int:
    """Parse stats' ``30d`` form or a plain integer day count."""
    raw = value[:-1] if value.lower().endswith("d") else value
    try:
        days = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(_t("cli.parse.time_range_invalid")) from exc
    if days <= 0:
        raise argparse.ArgumentTypeError(_t("cli.parse.time_range_positive"))
    return days


def _positive_int(value: str) -> int:
    """argparse type for a positive integer."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(_t("cli.parse.must_be_positive_int")) from exc
    if number <= 0:
        raise argparse.ArgumentTypeError(_t("cli.parse.must_be_positive"))
    return number


def cmd_stats(store: Store, args: argparse.Namespace) -> int:
    """Show aggregated stats for executed GCs."""
    print(report.format_stats(store.gc_runs(since_days=args.since)))
    return 0


def cmd_review(store: Store, args: argparse.Namespace) -> int:
    try:
        if args.list_reviews:
            if args.export or args.save or args.highlight or args.review_timezone:
                raise ValueError(_t("review.error.options"))
            saved = store.saved_reviews()
            print(_t("review.saved_list", count=len(saved)))
            for snapshot in saved:
                print(f"#{snapshot.id}  {snapshot.week_start}  {snapshot.generated_at.isoformat()}")
            return 0
        if args.snapshot is not None:
            if args.review_timezone:
                raise ValueError(_t("review.error.options"))
            snapshot = store.saved_review(args.snapshot)
            if snapshot is None:
                raise ValueError(_t("review.error.not_found", id=args.snapshot))
            review = WeeklyReview.from_json(snapshot.payload)
        else:
            review = build_review(store, args.week or "this", timezone_name=args.review_timezone or "local")
        if args.highlight is not None:
            review = review.with_highlights(args.highlight)
        if args.export:
            export_review(review, args.export)
        print(format_review(review))
        if args.export:
            print(_t("review.exported", path=args.export))
        if args.save:
            snapshot_id = store.save_review(week_start=review.week_start,
                                           generated_at=datetime.fromisoformat(review.generated_at),
                                           payload=review.to_json())
            print(_t("review.saved", id=snapshot_id))
    except (ValueError, OSError) as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1
    return 0


# ============ Assembly and entry point ============

def _add_query_arguments(parser: argparse.ArgumentParser, *, search: bool = False) -> None:
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--state", choices=[s.value for s in State], default=None)
    scope.add_argument("--scope", choices=SCOPES, help=_t("query.help.scope"))
    if not search:
        parser.add_argument("--search", "-q", default="", help=_t("query.help.text"))
    parser.add_argument("--root-id", type=_positive_int, help=_t("query.help.root"))
    parser.add_argument("--sort", choices=SORTS, default="id", help=_t("query.help.sort"))
    parser.add_argument("--notes", action="store_true", help=_t("query.help.notes"))
    parser.add_argument("--limit", type=_positive_int, help=_t("query.help.limit"))


def cmd_skill(args: argparse.Namespace) -> int:
    """Manage bundled skills without opening or migrating the user's task database."""
    from todogc.skill_install import TARGETS, install_skills, skill_status

    try:
        if args.skill_action == "install":
            results = install_skills(args.target, database=args.db,
                                     config=args.config or os.environ.get("TODOGC_CONFIG"),
                                     destination=args.directory, force=args.force)
        else:
            if args.target == "all" and args.directory is not None:
                raise ValueError("--directory requires a single target")
            targets = TARGETS[:-1] if args.target == "all" else (args.target,)
            results = [skill_status(target, destination=args.directory) for target in targets]
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            for result in results:
                print(_t("skill.result", target=result["target"], state=result["state"], path=result["path"]))
                if "database" in result:
                    print(_t("skill.runtime", database=result["database"], config=result["config"]))
                if args.skill_action == "install":
                    print(_t("skill.ready", invocation=result["invocation"]))
        return 0
    except (OSError, ValueError) as exc:
        print(_t("cli.error", error=exc), file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with all subcommands wired up.

    Returns:
        argparse.ArgumentParser: a parser with subcommands and their func defaults attached.
    """
    parser = argparse.ArgumentParser(prog="todogc", description=_t("cli.help.description"))
    parser.add_argument("--db", default=os.environ.get("TODOGC_DB"), help=_t("cli.help.db"))
    parser.add_argument("--config", default=None, help=_t("cli.help.config"))
    parser.add_argument(
        "--heap-max", type=_positive_int, default=None, help=_t("cli.help.heap_max")
    )
    parser.add_argument("--gc-mode", choices=["gentle", "normal", "aggressive"], default=None)
    parser.add_argument("--recent-days", type=_positive_int, default=None)
    parser.add_argument("--max-skips", type=_positive_int, default=None, help=_t("cli.help.max_skips"))
    parser.add_argument("--restore-grace-days", type=_positive_int, default=None)
    parser.add_argument("--lang", choices=SUPPORTED_LOCALES, default=None, help=_t("cli.help.lang"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("skill", help=_t("skill.help.command"))
    actions = p.add_subparsers(dest="skill_action", required=True)
    for action in ("install", "status"):
        operation = actions.add_parser(action, help=_t(f"skill.help.{action}"))
        operation.add_argument("--target", choices=("codex", "claude-code", "all"),
                               required=action == "install", default="all", help=_t("skill.help.target"))
        operation.add_argument("--directory", help=_t("skill.help.directory"))
        operation.add_argument("--json", action="store_true", help=_t("service.help.json"))
        if action == "install":
            operation.add_argument("--force", action="store_true", help=_t("skill.help.force"))

    p = sub.add_parser("add", help=_t("cli.help.cmd_add"))
    p.add_argument("title")
    p.add_argument("--root-id", type=int, default=None, help=_t("cli.help.add_root_id"))
    p.add_argument("--root", action="store_true", help=_t("cli.help.add_root"))
    p.add_argument("--meta", action="store_true", help=_t("cli.help.add_meta"))
    p.add_argument(
        "--ref", dest="ref_strength", choices=[r.value for r in ReferenceStrength],
        default=ReferenceStrength.SOFT.value, help=_t("cli.help.add_ref"),
    )
    p.add_argument("--force", action="store_true", help=_t("cli.help.add_force"))
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("capture", help=_t("cli.help.cmd_capture"))
    p.add_argument("title")
    p.add_argument("--force", action="store_true", help=_t("cli.help.add_force"))
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("inbox", help=_t("cli.help.cmd_inbox"))
    p.add_argument("--move", type=int, default=None, help=_t("cli.help.inbox_move"))
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("discard", help=_t("cli.help.cmd_discard"))
    p.add_argument("id", type=int)
    p.add_argument("note", nargs="?", default=None)
    p.set_defaults(func=cmd_discard)

    p = sub.add_parser("tree", help=_t("cli.help.cmd_tree"))
    p.add_argument("--show-completed", action="store_true", help=_t("webapp.tree.show_completed"))
    p.add_argument("--show-snoozed", action="store_true", help=_t("snooze.show"))
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("root", help=_t("cli.help.cmd_root"))
    p.add_argument("id", type=int)
    p.add_argument("--off", action="store_true", help=_t("cli.help.root_off"))
    p.set_defaults(func=cmd_root)

    p = sub.add_parser("dep", help=_t("cli.help.cmd_dep"))
    p.add_argument("task_id", type=int)
    p.add_argument("ref_id", type=int)
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_dep)

    p = sub.add_parser("ls", help=_t("cli.help.cmd_ls"))
    _add_query_arguments(p)
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("search", help=_t("query.help.command"))
    p.add_argument("query", help=_t("query.help.text"))
    _add_query_arguments(p, search=True)
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("today", help=_t("today.help.command"))
    p.set_defaults(func=cmd_today)

    p = sub.add_parser("focus", help=_t("today.help.focus"))
    p.add_argument("id", type=_positive_int)
    p.add_argument("--position", type=_positive_int, help=_t("today.help.position"))
    p.add_argument("--with-subtasks", action="store_true", help=_t("today.help.subtasks"))
    p.set_defaults(func=cmd_focus)

    p = sub.add_parser("unfocus", help=_t("today.help.unfocus"))
    p.add_argument("id", type=_positive_int)
    p.set_defaults(func=cmd_focus)

    p = sub.add_parser("progress", help=_t("cli.help.cmd_progress"))
    p.add_argument("id", type=int)
    p.add_argument("note", nargs="?", default=None)
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser("snooze", help=_t("cli.help.cmd_snooze"))
    p.add_argument("id", type=int)
    timing = p.add_mutually_exclusive_group(required=True)
    timing.add_argument("--for", dest="duration", help=_t("snooze.help.for"))
    timing.add_argument("--until", help=_t("snooze.help.until"))
    p.add_argument("--note", default=None)
    p.add_argument("--with-subtasks", action="store_true", help=_t("snooze.help.subtasks"))
    p.set_defaults(func=cmd_snooze)

    p = sub.add_parser("snoozed", help=_t("snooze.help.list"))
    p.set_defaults(func=cmd_snoozed)

    p = sub.add_parser("unsnooze", help=_t("snooze.help.unsnooze"))
    p.add_argument("id", type=int)
    p.add_argument("--with-subtasks", action="store_true", help=_t("snooze.help.subtasks"))
    p.set_defaults(func=cmd_unsnooze)

    p = sub.add_parser("defer", help=_t("snooze.help.defer"))
    p.add_argument("id", type=int)
    p.add_argument("note", nargs="?", default=None)
    p.set_defaults(func=cmd_defer)

    p = sub.add_parser("done", help=_t("cli.help.cmd_done"))
    p.add_argument("id", type=int)
    p.add_argument("note", nargs="?", default=None)
    p.add_argument("--with-subtasks", action="store_true", help=_t("cli.help.done_subtasks"))
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("reopen", help=_t("cli.help.cmd_reopen"))
    p.add_argument("id", type=int)
    p.add_argument("--root-id", type=int, default=None, help=_t("cli.help.restore_root_id"))
    p.add_argument("--force", action="store_true", help=_t("cli.help.add_force"))
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("completed", help=_t("cli.help.cmd_completed"))
    p.set_defaults(func=cmd_completed)

    p = sub.add_parser("gc", help=_t("cli.help.cmd_gc"))
    p.add_argument(
        "--mode", dest="command_gc_mode", choices=["gentle", "normal", "aggressive"], default=None
    )
    p.add_argument("--full", action="store_true", help=_t("cli.help.gc_full"))
    p.add_argument("--dry-run", action="store_true", help=_t("cli.help.gc_dry_run"))
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("heap", aliases=["jmap"], help=_t("cli.help.cmd_heap"))
    p.set_defaults(func=cmd_heap)

    p = sub.add_parser("web", help=_t("cli.help.cmd_web"))
    p.add_argument("--port", type=int, default=None, help=_t("cli.help.web_port"))
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("watch", help=_t("cli.help.cmd_watch"))
    p.set_defaults(func=cmd_watch)
    actions = p.add_subparsers(dest="watch_action")
    actions.add_parser("start", help=_t("service.help.start"))
    actions.add_parser("stop", help=_t("service.help.stop"))
    status = actions.add_parser("status", help=_t("service.help.status"))
    status.add_argument("--json", action="store_true", help=_t("service.help.json"))
    actions.add_parser("test", help=_t("service.help.test"))
    logs = actions.add_parser("logs", help=_t("service.help.logs"))
    logs.add_argument("--lines", type=_positive_int, default=50, help=_t("service.help.lines"))

    p = sub.add_parser("graveyard", help=_t("cli.help.cmd_graveyard"))
    p.set_defaults(func=cmd_graveyard)

    p = sub.add_parser("restore", help=_t("cli.help.cmd_restore"))
    p.add_argument("id", type=int)
    p.add_argument("--root-id", type=int, default=None, help=_t("cli.help.restore_root_id"))
    p.add_argument("--force", action="store_true", help=_t("cli.help.add_force"))
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("show", help=_t("cli.help.cmd_show"))
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("history", help=_t("cli.help.cmd_history"))
    p.add_argument("id", type=int)
    p.add_argument("--limit", type=_positive_int, default=None)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("edit", help=_t("cli.help.cmd_edit"))
    p.add_argument("id", type=int)
    p.add_argument("title", nargs="?", default=None)
    p.add_argument("--ref", dest="ref_strength", choices=[r.value for r in ReferenceStrength], default=None)
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("link", help=_t("cli.help.cmd_link"))
    p.add_argument("id", type=int)
    p.add_argument("root_id", type=int, nargs="?", default=None)
    p.add_argument("--off", action="store_true")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("doctor", help=_t("cli.help.cmd_doctor"))
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("stats", help=_t("cli.help.cmd_stats"))
    p.add_argument("--since", type=_days, default=None, help=_t("cli.help.stats_since"))
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("review", help=_t("review.help.command"))
    source = p.add_mutually_exclusive_group()
    source.add_argument("--week", help=_t("review.help.week"))
    source.add_argument("--snapshot", type=int, help=_t("review.help.snapshot"))
    source.add_argument("--list", dest="list_reviews", action="store_true", help=_t("review.help.list"))
    p.add_argument("--timezone", dest="review_timezone", help=_t("review.help.timezone"))
    p.add_argument("--highlight", type=int, action="append", help=_t("review.help.highlight"))
    p.add_argument("--export", help=_t("review.help.export"))
    p.add_argument("--save", action="store_true", help=_t("review.help.save"))
    p.set_defaults(func=cmd_review)

    return parser


def _prescan_lang(argv: list[str], environ: Mapping[str, str]) -> str:
    """Find --lang before the parser is built (which language argparse's own help text should use).

    Doesn't read [i18n].lang from the config file — that needs --config parsed first, which
    isn't possible at this stage, so it's left to load_config() in main() as a fallback
    (meaning: if the language is set only in the config file, with no --lang / env var, --help
    itself still shows in the default language, but the command's actual output uses the
    language specified in the config).

    Args:
        argv (list[str]): the raw argument list (before build_parser()).
        environ (dict[str, str]): environment variables.

    Returns:
        str: the detected language code; DEFAULT_LOCALE if nothing valid was detected.
    """
    for i, token in enumerate(argv):
        if token == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith("--lang="):
            return token.split("=", 1)[1]
    return str(environ.get("TODOGC_LANG", DEFAULT_LOCALE))


def main(argv: list[str] | None = None) -> int:
    """CLI main entry point: parses arguments, opens the store, dispatches to a subcommand.

    Args:
        argv (list[str] | None): the argument list; uses sys.argv[1:] if None.

    Returns:
        int: the process exit code.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    prelim_lang = _prescan_lang(argv, os.environ)
    if prelim_lang in SUPPORTED_LOCALES:
        set_locale(prelim_lang)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "skill":
        return cmd_skill(args)
    args.db = args.db if args.db is not None else "todogc.db"
    try:
        args.watch_config_unavailable = False
        try:
            cfg = load_config(args.config)
        except ConfigError:
            if args.cmd != "watch" or args.watch_action not in {"stop", "status", "logs"}:
                raise
            cfg = AppConfig()
            args.watch_config_unavailable = True
        set_locale(args.lang or cfg.lang)  # --lang > env var/config file (already folded into cfg.lang) > default
        args.heap_max = args.heap_max if args.heap_max is not None else cfg.heap_max
        mode = getattr(args, "command_gc_mode", None) or args.gc_mode or cfg.gc_mode
        recent_days = args.recent_days if args.recent_days is not None else cfg.recent_days
        max_skips = args.max_skips if args.max_skips is not None else cfg.max_consecutive_skips
        grace = args.restore_grace_days if args.restore_grace_days is not None else cfg.restore_grace_days
        args.gc_policy = GCPolicy(
            mode=mode,
            recent_days=recent_days,
            max_skips=max_skips,
            restore_grace_days=grace,
        )
        args.app_config = cfg  # the full config (including notify.*/watch.*); needed by the watch subcommand, ignored by the rest.
    except (ConfigError, ValueError) as exc:
        print(_t("cli.error.config", error=exc), file=sys.stderr)
        return 2
    if args.cmd == "watch":
        return cmd_watch(None, args)
    store = Store(args.db)
    try:
        return args.func(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())

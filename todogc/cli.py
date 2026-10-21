# cli.py — the command-line entry point: strings together the add/mark/dep/GC pipeline, with
# the Stop-the-world collection ceremony (revive or let go) at its core.

from __future__ import annotations

import argparse
import importlib.util
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from todogc import console, report
from todogc.collector import Candidate, Collector, GCPolicy, GCResult
from todogc.config import AppConfig, ConfigError, load_config
from todogc.heap import ReferenceStrength, State, TouchKind
from todogc.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, set_locale
from todogc.i18n import t as _t
from todogc.store import Store
from todogc.watcher import Watcher

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
        if choice == "collect":
            if _finalize_spent(store, task.id):
                note = _t("cli.ceremony.finalize_spent")
            else:
                eulogy = eulogize(cand).strip() if eulogize else ""
                note = eulogy or _t("cli.ceremony.silent_collect")
            task.state = State.COLLECTED
            store.update_task(task)
            store.record(task.id, TouchKind.COLLECT, note=note, ts=now)
            out.collected.append(task.id)
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
    if args.state == State.CANDIDATE.value:
        result = _run_gc(store, datetime.now(timezone.utc), args.gc_policy)
        tasks = [c.task for c in result.candidates]
    else:
        state = State(args.state) if args.state else None
        tasks = store.list_tasks(state=state, active_only=state is None)
    roots = store.root_ids()
    if console.render_ls(tasks, roots):  # rich installed -> color table
        return 0
    if not tasks:
        print(_t("cli.ls.empty_heap"))
        return 0
    for t in tasks:
        mark = "★" if t.id in roots else " "
        link = f" →#{t.root_id}" if t.root_id is not None else ""
        meta = " [meta]" if t.is_meta else ""
        print(f" {mark} #{t.id:>3} [{t.gen.value}:{t.age}] {t.title}{link}{meta}  <{t.state.value}>")
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
    """snooze: defer a task (feeds skip_count).

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
            survivors=len(result.survivors),
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


def cmd_watch(store: Store, args: argparse.Namespace) -> int:
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
    cfg: AppConfig = args.app_config
    if not cfg.ntfy_enabled:
        print(_t("cli.watch.not_configured"), file=sys.stderr)
        return 1
    watcher = Watcher(args.db, cfg)
    signal.signal(signal.SIGTERM, lambda signum, frame: watcher.stop())
    print(_t("cli.watch.starting", db=args.db, poll_seconds=cfg.watch_poll_seconds))
    try:
        watcher.run()
    except KeyboardInterrupt:
        pass
    print(_t("cli.watch.stopped"))
    return 0


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
    t = store.get_task(args.id, record_view=False)
    if t is None:
        print(_t("cli.error.not_found", id=args.id), file=sys.stderr)
        return 1
    if t.state is not State.COLLECTED:
        print(_t("cli.restore.not_collected", id=args.id), file=sys.stderr)
        return 1
    if args.root_id is not None:
        try:
            root = store.get_task(args.root_id, record_view=False)
            if root is None or not root.state.is_active:
                raise ValueError(_t("cli.restore.invalid_root", root_id=args.root_id))
            if args.root_id == args.id:
                raise ValueError(_t("cli.restore.self_link"))
            store.validate_parent(args.id, args.root_id)
            t.root_id = args.root_id
        except ValueError as exc:
            print(_t("cli.error", error=exc), file=sys.stderr)
            return 1
    t.state = State.LIVE
    store.update_task(t)
    store.record(args.id, TouchKind.RESTORE)
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


# ============ Assembly and entry point ============

def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with all subcommands wired up.

    Returns:
        argparse.ArgumentParser: a parser with subcommands and their func defaults attached.
    """
    parser = argparse.ArgumentParser(prog="todogc", description=_t("cli.help.description"))
    parser.add_argument("--db", default=os.environ.get("TODOGC_DB", "todogc.db"), help=_t("cli.help.db"))
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
    p.add_argument("--state", choices=[s.value for s in State], default=None)
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("progress", help=_t("cli.help.cmd_progress"))
    p.add_argument("id", type=int)
    p.add_argument("note", nargs="?", default=None)
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser("snooze", help=_t("cli.help.cmd_snooze"))
    p.add_argument("id", type=int)
    p.add_argument("note", nargs="?", default=None)
    p.set_defaults(func=cmd_snooze)

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

    p = sub.add_parser("graveyard", help=_t("cli.help.cmd_graveyard"))
    p.set_defaults(func=cmd_graveyard)

    p = sub.add_parser("restore", help=_t("cli.help.cmd_restore"))
    p.add_argument("id", type=int)
    p.add_argument("--root-id", type=int, default=None, help=_t("cli.help.restore_root_id"))
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
    try:
        cfg = load_config(args.config)
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
    store = Store(args.db)
    try:
        return args.func(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())

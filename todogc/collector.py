# collector.py — the GC engine's core: marks reachability from the roots, catches
# reference-cycle islands via strongly connected components, then sweeps by the liveness
# formula to produce a reclaim candidate listing.

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from todogc.heap import ReferenceStrength, Task, TouchKind
from todogc.i18n import DEFAULT_LOCALE
from todogc.i18n import t as _t

if TYPE_CHECKING:  # type-checking only, avoids a hard runtime collector -> store dependency
    from todogc.store import Store


@dataclass
class GCPolicy:
    """Liveness thresholds (kept in one place so you can tune them until they make you uneasy).

    Attributes:
        recent_days (int): the window, in days, for "recently touched".
        max_skips (int): reaching this many skips (exclusive) marks a task dead.
    """

    recent_days: int = 30
    max_skips: int = 5
    restore_grace_days: int = 7
    mode: str = "normal"

    def __post_init__(self) -> None:
        if self.mode not in {"gentle", "normal", "aggressive"}:
            raise ValueError(_t("collector.error.invalid_mode"))
        if min(self.recent_days, self.max_skips, self.restore_grace_days) <= 0:
            raise ValueError(_t("collector.error.invalid_threshold"))

    @property
    def effective_recent_days(self) -> int:
        """Adjust the active time window by mode."""
        if self.mode == "gentle":
            return self.recent_days * 2
        if self.mode == "aggressive":
            return max(1, self.recent_days // 2)
        return self.recent_days

    @property
    def effective_max_skips(self) -> int:
        """Adjust the consecutive-skip tolerance by mode."""
        if self.mode == "gentle":
            return self.max_skips + 2
        if self.mode == "aggressive":
            return max(1, self.max_skips - 2)
        return self.max_skips


@dataclass
class TaskMetrics:
    """A single task's liveness metrics snapshot (aggregated by store from touch events).

    Attributes:
        refcount_30d (int): count of real progress events (PROGRESS/EDIT) in the last
            recent_days days.
        skip_count (int): cumulative count of skips (DEFER).
        days_since_touch (float | None): days since the most recent non-"view" touch
            (including CREATE); None if there's never been one.
    """

    refcount_30d: int
    skip_count: int
    days_since_touch: float | None
    consecutive_skips: int | None = None
    days_since_progress: float | None = None
    days_since_restore: float | None = None


class Reason(str, Enum):
    """Why a task was listed as a reclaim candidate."""

    STALE = "stale"              # reachable, but its liveness metrics are dead (no progress / too many skips)
    UNREACHABLE = "unreachable"  # an orphan unreachable from any active root
    CYCLE = "cycle"              # unreachable, and stuck in a mutually-vouching reference cycle


@dataclass
class Cycle:
    """A reference cycle (island) marked dead.

    Attributes:
        id (int): the cycle's sequence number (referenced back from candidates).
        task_ids (list[int]): task ids on the cycle, ascending.
    """

    id: int
    task_ids: list[int]


@dataclass
class Candidate:
    """A reclaim candidate: a zombie task + why it deserves to die.

    Attributes:
        task (Task): the candidate task itself.
        reason (Reason): the cause of death.
        cycle_id (int | None): the id of the cycle it belongs to, when reason is CYCLE.
        metrics (TaskMetrics | None): a liveness metrics snapshot (attached for STALE, for
            the taunt).
        detail (str): a one-sentence human-readable explanation.
    """

    task: Task
    reason: Reason
    cycle_id: int | None = None
    metrics: TaskMetrics | None = None
    detail: str = ""


@dataclass
class GCResult:
    """The output of one GC.

    Attributes:
        survivors (list[Task]): tasks judged to be alive.
        candidates (list[Candidate]): the reclaim candidate listing.
        cycles (list[Cycle]): reference cycles detected.
    """

    survivors: list[Task] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    cycles: list[Cycle] = field(default_factory=list)
    inbox: list[Task] = field(default_factory=list)

    @property
    def released_pct(self) -> float:
        """This round's reclaim ratio = candidate count / total, used for the GC log's "cognitive memory freed %".

        Returns:
            float: a percentage rounded to 1 decimal place; 0.0 if there are no tasks.
        """
        total = len(self.survivors) + len(self.candidates) + len(self.inbox)
        return 0.0 if total == 0 else round(100 * len(self.candidates) / total, 1)


class Collector:
    """Mark-and-sweep collector: judges a task reference graph into alive / candidate, and roots out reference cycles."""

    def __init__(self, policy: GCPolicy | None = None, *, lang: str = DEFAULT_LOCALE) -> None:
        """Construct a collector.

        Args:
            policy (GCPolicy | None): liveness thresholds; None uses the default (30 days /
                5 skips).
            lang (str): language used for candidate.detail's human-readable explanation;
                defaults to English.

        Returns:
            None
        """
        self.policy = policy or GCPolicy()
        self.lang = lang

    def collect(
        self,
        tasks: dict[int, Task],
        roots: set[int],
        edges: dict[int, set[int]],
        metrics_of: Callable[[int], TaskMetrics],
    ) -> GCResult:
        """Run one mark-and-sweep pass over the given task reference graph.

        Liveness: a task is alive if it's a root, a meta-task (Metaspace), or "reachable
        and its liveness metrics are alive". Everything else becomes a candidate: unreachable
        tasks split into CYCLE / UNREACHABLE depending on whether they're on a cycle;
        reachable tasks with dead metrics become STALE.

        Args:
            tasks (dict[int, Task]): id -> Task for the entire heap.
            roots (set[int]): ids of the GC roots (active targets).
            edges (dict[int, set[int]]): reference adjacency list; edges[x] = ids x
                references (needs).
            metrics_of (Callable[[int], TaskMetrics]): callback that looks up liveness
                metrics by id.

        Returns:
            GCResult: survivors, candidates, and detected reference cycles.
        """
        pending = [task for task in tasks.values() if task.state.is_active and task.in_inbox]
        tasks = {tid: task for tid, task in tasks.items() if task.state.is_active and not task.in_inbox}
        all_ids = set(tasks)
        reachable = self._mark(roots, edges, all_ids)
        unreachable = all_ids - reachable
        cycles = self._find_cycles(unreachable, edges)
        cycle_of = {tid: c.id for c in cycles for tid in c.task_ids}

        result = GCResult(cycles=cycles, inbox=pending)
        for tid in sorted(all_ids):
            task = tasks[tid]
            if tid in roots or task.is_meta:
                result.survivors.append(task)
            elif tid in unreachable:
                if tid in cycle_of:
                    result.candidates.append(
                        Candidate(task, Reason.CYCLE, cycle_id=cycle_of[tid],
                                  detail=_t("collector.detail.cycle", lang=self.lang, cycle_id=cycle_of[tid]))
                    )
                else:
                    result.candidates.append(
                        Candidate(task, Reason.UNREACHABLE, detail=_t("collector.detail.unreachable", lang=self.lang))
                    )
            else:
                m = metrics_of(tid)
                if self._metrics_alive(task, m):
                    result.survivors.append(task)
                else:
                    skips = m.consecutive_skips if m.consecutive_skips is not None else m.skip_count
                    result.candidates.append(
                        Candidate(task, Reason.STALE, metrics=m,
                                  detail=_t(
                                      "collector.detail.stale",
                                      lang=self.lang,
                                      days=self.policy.effective_recent_days,
                                      refcount=m.refcount_30d,
                                      skips=skips,
                                  ))
                    )
        return result

    def run_on_store(
        self,
        store: Store,
        root_ids: Iterable[int],
        *,
        extra_edges: Iterable[tuple[int, int]] | None = None,
        now: datetime | None = None,
    ) -> GCResult:
        """Assemble the reference graph from a Store and run one GC (the end-to-end entry point).

        Scans the full table with record_view=False, so the GC's own scan doesn't pollute
        view tracking.

        Args:
            store (Store): the data source.
            root_ids (Iterable[int]): ids of tasks declared as active roots.
            extra_edges (Iterable[tuple[int, int]] | None): extra task->task "reason" edges;
                (a, b) means a references b. Persisting these edges needs a dep table in
                store, passed in here for now.
            now (datetime | None): the liveness time base; None uses the current UTC time.

        Returns:
            GCResult: this round's GC result.
        """
        now = now or datetime.now(timezone.utc)
        # Already-collected (COLLECTED) tasks sit in the trash; the next GC round shouldn't
        # dig them back up as candidates.
        tasks = {
            t.persisted_id: t
            for t in store.list_tasks(record_view=False)
            if t.state.is_active
        }
        edges: dict[int, set[int]] = defaultdict(set)
        for t in tasks.values():
            if t.root_id is not None:
                edges[t.root_id].add(t.persisted_id)  # the parent (goal/task) references its child task
        for a, b in extra_edges or ():
            edges[a].add(b)

        def metrics_of(tid: int) -> TaskMetrics:
            touches = store.touches(tid)
            effective_kinds = {
                TouchKind.CREATE, TouchKind.PROGRESS, TouchKind.EDIT,
                TouchKind.LINK, TouchKind.REVIVE, TouchKind.RESTORE,
                TouchKind.REOPEN,
                TouchKind.ORGANIZE,
            }
            last = max(
                (tc.ts for tc in touches if tc.kind in effective_kinds),
                default=None,
            )
            days = None if last is None else (now - last).total_seconds() / 86400.0
            last_progress = max((tc.ts for tc in touches if tc.kind.is_progress), default=None)
            progress_days = None if last_progress is None else (now - last_progress).total_seconds() / 86400.0
            last_restore = max(
                (tc.ts for tc in touches if tc.kind in {TouchKind.REVIVE, TouchKind.RESTORE, TouchKind.REOPEN}),
                default=None,
            )
            restore_days = None if last_restore is None else (now - last_restore).total_seconds() / 86400.0
            return TaskMetrics(
                refcount_30d=store.refcount(tid, within_days=self.policy.effective_recent_days, now=now),
                skip_count=store.skip_count(tid),
                days_since_touch=days,
                consecutive_skips=store.consecutive_skip_count(tid),
                days_since_progress=progress_days,
                days_since_restore=restore_days,
            )

        # An orphan that was just revived/restored acts as a temporary GC root during the
        # grace period, so the next round doesn't immediately reclaim it again; once the
        # grace period ends without being re-linked, it naturally falls back to an
        # unreachable candidate.
        root_set = set(root_ids)
        for tid in tasks:
            last_restore = max(
                (
                    tc.ts
                    for tc in store.touches(tid)
                    if tc.kind in {TouchKind.REVIVE, TouchKind.RESTORE, TouchKind.REOPEN}
                ),
                default=None,
            )
            if last_restore is not None:
                age_days = (now - last_restore).total_seconds() / 86400.0
                if age_days < self.policy.restore_grace_days:
                    root_set.add(tid)

        return self.collect(tasks, root_set, dict(edges), metrics_of)

    # ---- internal: mark / cycle / liveness ----

    @staticmethod
    def _mark(roots: set[int], edges: dict[int, set[int]], all_ids: set[int]) -> set[int]:
        """MARK phase: mark every id reachable from the roots along reference edges as alive.

        Args:
            roots (set[int]): ids to start from.
            edges (dict[int, set[int]]): reference adjacency list.
            all_ids (set[int]): all task ids (used to filter out dangling references).

        Returns:
            set[int]: the reachable (alive) id set, including the roots themselves.
        """
        seen: set[int] = set()
        stack = [r for r in roots if r in all_ids]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            for y in edges.get(x, ()):  # noqa: SIM118 - edges is a plain dict
                if y in all_ids and y not in seen:
                    stack.append(y)
        return seen

    @staticmethod
    def _find_cycles(nodes: set[int], edges: dict[int, set[int]]) -> list[Cycle]:
        """Run Tarjan's algorithm (iterative) on the induced subgraph of the given nodes to find strongly connected components, and pull out the cycles among them.

        A cycle is a strongly connected component of size >= 2, or a single node with a
        self-loop. Computed only over edges internal to nodes (typically the unreachable
        set), so a legitimate cycle hanging off a root is never caught by mistake.

        Args:
            nodes (set[int]): node ids to check (vertices of the induced subgraph).
            edges (dict[int, set[int]]): the full-graph reference adjacency list.

        Returns:
            list[Cycle]: detected cycles, numbered in discovery order.
        """
        sub = {n: [y for y in edges.get(n, ()) if y in nodes] for n in nodes}
        index: dict[int, int] = {}
        low: dict[int, int] = {}
        on_stack: set[int] = set()
        scc_stack: list[int] = []
        counter = 0
        sccs: list[list[int]] = []

        for start in nodes:
            if start in index:
                continue
            work: list[tuple[int, int]] = [(start, 0)]
            while work:
                node, pi = work[-1]
                if pi == 0:
                    index[node] = low[node] = counter
                    counter += 1
                    scc_stack.append(node)
                    on_stack.add(node)
                recursed = False
                children = sub[node]
                i = pi
                while i < len(children):
                    w = children[i]
                    if w not in index:
                        work[-1] = (node, i + 1)
                        work.append((w, 0))
                        recursed = True
                        break
                    if w in on_stack:
                        low[node] = min(low[node], index[w])
                    i += 1
                if recursed:
                    continue
                if low[node] == index[node]:
                    comp: list[int] = []
                    while True:
                        w = scc_stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    sccs.append(comp)
                work.pop()
                if work:  # backtrack: merge the child's low value into the parent's
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])

        cycles: list[Cycle] = []
        for comp in sccs:
            is_cycle = len(comp) >= 2 or (len(comp) == 1 and comp[0] in sub.get(comp[0], []))
            if is_cycle:
                cycles.append(Cycle(id=len(cycles), task_ids=sorted(comp)))
        return cycles

    def _metrics_alive(self, task: Task, m: TaskMetrics) -> bool:
        """Apply the liveness formula to decide whether the metrics are "still alive" (the second gate beyond reachability).

        alive = (touched recently, or had real progress in the last N days) and skips
        haven't exceeded the threshold.

        Args:
            m (TaskMetrics): the liveness metrics snapshot.

        Returns:
            bool: True if alive.
        """
        if task.ref_strength is ReferenceStrength.STRONG:
            return True
        skips = m.consecutive_skips if m.consecutive_skips is not None else m.skip_count
        if m.days_since_restore is not None and m.days_since_restore < self.policy.restore_grace_days:
            return True
        if task.ref_strength is ReferenceStrength.WEAK:
            recent_progress = (
                m.days_since_progress is not None
                and m.days_since_progress < self.policy.effective_recent_days
            )
            return recent_progress and skips < self.policy.effective_max_skips
        recent = m.days_since_touch is not None and m.days_since_touch < self.policy.effective_recent_days
        progressed = m.refcount_30d > 0
        return (recent or progressed) and skips < self.policy.effective_max_skips

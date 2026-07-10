# collector.py —— GC 引擎核心：从 roots 出发做可达性标记、用强连通分量抓循环引用孤岛，再按判活公式清除，产出一份回收候选清单。

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from todogc.heap import State, Task, TouchKind

if TYPE_CHECKING:  # 仅类型标注用，避免运行期 collector→store 的硬依赖
    from todogc.store import Store


@dataclass
class GCPolicy:
    """判活口径(阈值集中一处，方便调到"让人心虚"的程度)。

    Attributes:
        recent_days (int): "最近碰过"的时间窗天数。
        max_skips (int): 推迟次数达到该值即判死(不含)。
    """

    recent_days: int = 30
    max_skips: int = 5


@dataclass
class TaskMetrics:
    """单个任务的判活指标快照(由 store 聚合触碰事件得来)。

    Attributes:
        refcount_30d (int): 近 recent_days 天的真推进(PROGRESS/EDIT)次数。
        skip_count (int): 累计推迟(DEFER)次数。
        days_since_touch (float | None): 距最近一次"非 view"触碰(含 CREATE)的天数；
            无任何此类事件时为 None。
    """

    refcount_30d: int
    skip_count: int
    days_since_touch: float | None


class Reason(str, Enum):
    """一个任务被列为回收候选的原因。"""

    STALE = "stale"              # 可达，但判活指标已死(没推进 / 推迟太多)
    UNREACHABLE = "unreachable"  # 从任何活跃 root 都不可达的孤儿
    CYCLE = "cycle"              # 不可达，且陷在"互相担保"的引用环里


@dataclass
class Cycle:
    """一个被判死的引用环(孤岛)。

    Attributes:
        id (int): 环的序号(供候选项回指)。
        task_ids (list[int]): 环上的任务 id，升序。
    """

    id: int
    task_ids: list[int]


@dataclass
class Candidate:
    """一条回收候选：一个僵尸任务 + 它为什么该死。

    Attributes:
        task (Task): 候选任务本身。
        reason (Reason): 判死原因。
        cycle_id (int | None): 当 reason 为 CYCLE 时，所属环的 id。
        metrics (TaskMetrics | None): 判活指标快照(STALE 时附带，便于打脸)。
        detail (str): 一句人话解释。
    """

    task: Task
    reason: Reason
    cycle_id: int | None = None
    metrics: TaskMetrics | None = None
    detail: str = ""


@dataclass
class GCResult:
    """一次 GC 的产出。

    Attributes:
        survivors (list[Task]): 判定存活的任务。
        candidates (list[Candidate]): 回收候选清单。
        cycles (list[Cycle]): 检出的引用环。
    """

    survivors: list[Task] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    cycles: list[Cycle] = field(default_factory=list)

    @property
    def released_pct(self) -> float:
        """本轮回收比例 = 候选数 / 总数，用于 GC 日志的"释放认知内存 %"。

        Returns:
            float: 保留 1 位小数的百分比；无任务时为 0.0。
        """
        total = len(self.survivors) + len(self.candidates)
        return 0.0 if total == 0 else round(100 * len(self.candidates) / total, 1)


class Collector:
    """标记-清除回收器：把一张任务引用图判成 存活 / 候选，并揪出循环引用。"""

    def __init__(self, policy: GCPolicy | None = None) -> None:
        """构造回收器。

        Args:
            policy (GCPolicy | None): 判活口径；None 用默认(30 天 / 5 次)。

        Returns:
            None
        """
        self.policy = policy or GCPolicy()

    def collect(
        self,
        tasks: dict[int, Task],
        roots: set[int],
        edges: dict[int, set[int]],
        metrics_of: Callable[[int], TaskMetrics],
    ) -> GCResult:
        """对给定的任务引用图跑一次标记-清除。

        存活判定：是 root、是元任务(Metaspace)、或"可达 且 判活指标活着"。
        其余进候选：不可达者按是否成环分 CYCLE / UNREACHABLE，可达但指标死者为 STALE。

        Args:
            tasks (dict[int, Task]): id → Task 的全体堆对象。
            roots (set[int]): GC roots(活跃目标)的 id 集合。
            edges (dict[int, set[int]]): 引用邻接表；edges[x] = x 所引用(需要)的 id 集合。
            metrics_of (Callable[[int], TaskMetrics]): 按 id 取判活指标的回调。

        Returns:
            GCResult: 存活、候选与检出的引用环。
        """
        all_ids = set(tasks)
        reachable = self._mark(roots, edges, all_ids)
        unreachable = all_ids - reachable
        cycles = self._find_cycles(unreachable, edges)
        cycle_of = {tid: c.id for c in cycles for tid in c.task_ids}

        result = GCResult(cycles=cycles)
        for tid in sorted(all_ids):
            task = tasks[tid]
            if tid in roots or task.is_meta:
                result.survivors.append(task)
            elif tid in unreachable:
                if tid in cycle_of:
                    result.candidates.append(
                        Candidate(task, Reason.CYCLE, cycle_id=cycle_of[tid],
                                  detail=f"陷在引用环 #{cycle_of[tid]}：互相担保，谁都不连 root")
                    )
                else:
                    result.candidates.append(
                        Candidate(task, Reason.UNREACHABLE, detail="从任何活跃目标都不可达")
                    )
            else:
                m = metrics_of(tid)
                if self._metrics_alive(m):
                    result.survivors.append(task)
                else:
                    result.candidates.append(
                        Candidate(task, Reason.STALE, metrics=m,
                                  detail=f"可达但已死：近 {self.policy.recent_days} 天推进 "
                                         f"{m.refcount_30d} 次，累计推迟 {m.skip_count} 次")
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
        """从 Store 组装引用图并跑一次 GC(端到端入口)。

        用 record_view=False 扫全表，避免 GC 自身的扫描污染 view 埋点。

        Args:
            store (Store): 数据源。
            root_ids (Iterable[int]): 声明为活跃 root 的任务 id。
            extra_edges (Iterable[tuple[int, int]] | None): 额外的 task→task "理由"边
                (a, b) 表示 a 引用 b；持久化这类边需 store 增 dep 表，暂由此传入。
            now (datetime | None): 判活时间基准；None 用当前 UTC。

        Returns:
            GCResult: 本轮 GC 结果。
        """
        now = now or datetime.now(timezone.utc)
        # 已回收(COLLECTED)的任务躺在垃圾桶里，不该被下一轮 GC 再刨出来当候选。
        tasks = {
            t.id: t
            for t in store.list_tasks(record_view=False)
            if t.id is not None and t.state is not State.COLLECTED
        }
        edges: dict[int, set[int]] = defaultdict(set)
        for t in tasks.values():
            if t.root_id is not None:
                edges[t.root_id].add(t.id)  # 父(目标/任务)引用子任务
        for a, b in extra_edges or ():
            edges[a].add(b)

        def metrics_of(tid: int) -> TaskMetrics:
            last = max(
                (tc.ts for tc in store.touches(tid) if tc.kind is not TouchKind.VIEW),
                default=None,
            )
            days = None if last is None else (now - last).total_seconds() / 86400.0
            return TaskMetrics(
                refcount_30d=store.refcount(tid, within_days=self.policy.recent_days, now=now),
                skip_count=store.skip_count(tid),
                days_since_touch=days,
            )

        return self.collect(tasks, set(root_ids), dict(edges), metrics_of)

    # ---- 内部：mark / cycle / 判活 ----

    @staticmethod
    def _mark(roots: set[int], edges: dict[int, set[int]], all_ids: set[int]) -> set[int]:
        """MARK 阶段：从 roots 出发沿引用边可达的 id 全部标记存活。

        Args:
            roots (set[int]): 起点 id 集合。
            edges (dict[int, set[int]]): 引用邻接表。
            all_ids (set[int]): 全体任务 id(用于过滤悬空引用)。

        Returns:
            set[int]: 可达(存活)的 id 集合，含 roots 自身。
        """
        seen: set[int] = set()
        stack = [r for r in roots if r in all_ids]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            for y in edges.get(x, ()):  # noqa: SIM118 - edges 是普通 dict
                if y in all_ids and y not in seen:
                    stack.append(y)
        return seen

    @staticmethod
    def _find_cycles(nodes: set[int], edges: dict[int, set[int]]) -> list[Cycle]:
        """在给定节点的诱导子图上用 Tarjan(迭代版)求强连通分量，取出其中的环。

        环 = 规模 >= 2 的强连通分量，或带自环的单点。仅在 nodes(通常是不可达集)
        内部的边上计算，因此挂在 root 上的合法环不会被误伤。

        Args:
            nodes (set[int]): 参与检测的节点 id(诱导子图顶点)。
            edges (dict[int, set[int]]): 全图引用邻接表。

        Returns:
            list[Cycle]: 检出的环，按发现顺序编号。
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
                if work:  # 回溯：把子节点的 low 合并给父节点
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])

        cycles: list[Cycle] = []
        for comp in sccs:
            is_cycle = len(comp) >= 2 or (len(comp) == 1 and comp[0] in sub.get(comp[0], []))
            if is_cycle:
                cycles.append(Cycle(id=len(cycles), task_ids=sorted(comp)))
        return cycles

    def _metrics_alive(self, m: TaskMetrics) -> bool:
        """按判活公式判断"指标是否还活着"(可达之外的第二道门槛)。

        alive = (最近碰过 或 近 N 天有真推进) 且 推迟未超阈值。

        Args:
            m (TaskMetrics): 判活指标快照。

        Returns:
            bool: 存活为 True。
        """
        recent = m.days_since_touch is not None and m.days_since_touch < self.policy.recent_days
        progressed = m.refcount_30d > 0
        return (recent or progressed) and m.skip_count < self.policy.max_skips

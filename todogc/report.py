# report.py —— 展示层：把一次 GC 的 GCResult 渲染成 JVM 风格 GC 日志、回收候选清单，以及分代打脸文案。

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from todogc.collector import Candidate, GCResult, Reason
from todogc.heap import TENURING_THRESHOLD, Generation, Task


def _fmt_uptime(td: timedelta | None) -> str:
    """把"堆运行时长"格式化成 GC 日志头里的紧凑形式。

    Args:
        td (timedelta | None): 距最早任务创建至今的时长；None 表示未知。

    Returns:
        str: 形如 "3d12h" 的字符串；td 为 None 时返回 "—"。
    """
    if td is None:
        return "—"
    return f"{td.days}d{td.seconds // 3600:02d}h"


def _fmt_duration(td: timedelta) -> str:
    """把仪式暂停时长格式化成 HH:MM:SS。

    Args:
        td (timedelta): 暂停(回收仪式)持续时间。

    Returns:
        str: 形如 "00:04:12" 的字符串。
    """
    s = int(td.total_seconds())
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def generation_taunt(gen: Generation, age: int) -> str:
    """按分代与年龄产出一句打脸文案(age→幸存者/老油条/Tenured/Metaspace 的梯度)。

    Args:
        gen (Generation): 任务当前分代。
        age (int): 熬过的 GC 轮数。

    Returns:
        str: 对应梯度的吐槽文案。
    """
    if gen is Generation.META:
        return "元任务常驻。这不是待办，是你的人设。"
    if gen is Generation.OLD or age >= TENURING_THRESHOLD:
        return "已 Tenured。恭喜，把自欺做成了长期主义。"
    if age >= 3:
        return f"老油条预备役，你已跳过它 {age} 次。"
    if age >= 1:
        return "young gen 幸存者，还年轻，还有救。"
    return "岁月静好。"


def format_gc_log(
    result: GCResult,
    *,
    gc_id: int = 0,
    uptime: timedelta | None = None,
    pause_full: bool = False,
    cause: str | None = None,
    pause: timedelta | None = None,
    heap_max: int | None = None,
) -> str:
    """把一次 GC 渲染成 JVM 统一日志风格的多行 GC 报告。

    Args:
        result (GCResult): 本轮 GC 结果。
        gc_id (int): GC 序号，对应日志里的 GC(N)。
        uptime (timedelta | None): 堆运行时长；None 显示 "—"。
        pause_full (bool): True 渲染 Pause Full，否则 Pause Young。
        cause (str | None): GC 原因；None 时按 pause_full 取默认
            (Ergonomics / Allocation Failure)。
        pause (timedelta | None): 仪式暂停时长；None 则不输出"暂停耗时"行。
        heap_max (int | None): 堆上限(任务数)；None 则不输出"(上限 N)"。

    Returns:
        str: 多行 GC 日志文本。
    """
    total = len(result.survivors) + len(result.candidates)
    after = len(result.survivors)
    reclaim = len(result.candidates)
    kind = "Full" if pause_full else "Young"
    cause = cause or ("Ergonomics" if pause_full else "Allocation Failure")
    cap = f" (上限 {heap_max})" if heap_max is not None else ""

    lines = [
        f"[运行 {_fmt_uptime(uptime)}][info][gc] GC({gc_id}) Pause {kind} ({cause})",
        f"  活跃堆         : {total} -> {after} tasks{cap}",
        f"  本轮回收       : {reclaim} zombies   →  释放认知内存 {result.released_pct}%",
    ]
    if result.cycles:
        titles = {c.task.id: c.task.title for c in result.candidates}
        first = result.cycles[0]
        names = " ⇄ ".join(f"「{titles.get(t, '#' + str(t))}」" for t in first.task_ids)
        lines.append(f"  reference cycle: {len(result.cycles)} 处  {names}")
    if pause is not None:
        lines.append(f"  暂停耗时       : {_fmt_duration(pause)}   (你在仪式上逐个纠结的真实时长)")
    anxiety = round(reclaim * 0.12, 1)
    blame = round(reclaim * 0.05, 1)
    lines.append(f"  [Times: 焦虑={anxiety}h  自责={blame}h  实际推进=0.0h]")
    return "\n".join(lines)


def format_candidates(result: GCResult, *, views_of: Callable[[int], int] | None = None) -> str:
    """把回收候选清单按 环 / 不可达 / 陈旧 分组渲染，供仪式展示。

    Args:
        result (GCResult): 本轮 GC 结果。
        views_of (Callable[[int], int] | None): 可选回调，按 id 取查看次数，
            用于给 stale 项补一句"看了 N 次"的打脸；None 则不显示。

    Returns:
        str: 多行候选清单文本；无候选时给一句难得的肯定。
    """
    titles = {c.task.id: c.task.title for c in result.candidates}
    lines = [f"━━ 回收候选清单 ({len(result.candidates)} 项) ━━━━━━━━━━━━━━━━"]

    for cyc in result.cycles:
        lines.append(f"[reference cycle #{cyc.id}] 互相担保，谁都不连 root：")
        for t in cyc.task_ids:
            lines.append(f"   ⇄ #{t} {titles.get(t, '')}")

    unreachable = [c for c in result.candidates if c.reason is Reason.UNREACHABLE]
    if unreachable:
        lines.append("[unreachable] 从任何活跃目标都不可达：")
        for c in unreachable:
            lines.append(f"   · #{c.task.id} {c.task.title}")

    stale = [c for c in result.candidates if c.reason is Reason.STALE]
    if stale:
        lines.append("[stale] 可达但判活指标已死：")
        for c in stale:
            bits = []
            if c.metrics is not None:
                bits.append(f"近期推进 {c.metrics.refcount_30d} 次")
                bits.append(f"推迟 {c.metrics.skip_count} 次")
            if views_of is not None and c.task.id is not None:
                bits.append(f"看了 {views_of(c.task.id)} 次")
            suffix = f"  ({' · '.join(bits)})" if bits else ""
            lines.append(f"   · #{c.task.id} {c.task.title}{suffix}")

    if not result.candidates:
        lines.append("   (无候选。这次你居然没骗自己，难得。)")
    return "\n".join(lines)


# ---- 堆内存图(jmap 式一览：分代分布 + -Xmx 占用 + 僵尸标记) ----

# 分代在堆图里的固定展示顺序与中/英文名(Eden → Survivor → Old → Metaspace)。
_GEN_ORDER: tuple[tuple[Generation, str, str], ...] = (
    (Generation.EDEN, "Eden", "新生代"),
    (Generation.SURVIVOR, "Survivor", "幸存区"),
    (Generation.OLD, "Old", "老年代"),
    (Generation.META, "Metaspace", "元空间"),
)


def _fill_bar(used: int, cap: int, *, width: int = 20) -> str:
    """把堆占用画成定宽的 [██░░] 填充条。

    Args:
        used (int): 已占用的对象数。
        cap (int): 堆容量上限(-Xmx)；<= 0 视为无上限，画成全空条。
        width (int): 填充条的总格子数。

    Returns:
        str: 形如 "[██████░░░░░░░░░░░░░░]" 的定宽字符串(含首尾方括号)；
            used 超配(> cap)时填满，不越界。
    """
    if cap <= 0:
        return "[" + "░" * width + "]"
    filled = min(width, round(width * used / cap))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _heap_row(task: Task, roots: set[int], cand_of: dict[int, Candidate]) -> str:
    """渲染堆图里的一行对象：前缀标记 + #id 标题 + 判死注解。

    标记优先级：root(★) > 元任务(⊛) > 回收候选/僵尸(☠) > 普通存活(空格)。
    root 与元任务恒为存活、绝不会出现在候选里，故三者互斥、优先级安全。

    Args:
        task (Task): 待渲染的任务。
        roots (set[int]): 活跃 root 的 id 集合。
        cand_of (dict[int, Candidate]): id → 回收候选的映射(非候选者不在其中)。

    Returns:
        str: 形如 "  │   ☠ #3 学 Rust  (cycle #0)" 的单行文本(含左侧竖线导轨)。
    """
    cand = cand_of.get(task.id)
    if task.id in roots:
        marker = "★"
    elif task.is_meta:
        marker = "⊛"
    elif cand is not None:
        marker = "☠"
    else:
        marker = " "

    annot = ""
    if cand is not None:
        if cand.reason is Reason.CYCLE:
            annot = f"  (cycle #{cand.cycle_id})"
        elif cand.reason is Reason.UNREACHABLE:
            annot = "  (unreachable)"
        elif cand.metrics is not None:
            annot = f"  (stale · 推进{cand.metrics.refcount_30d} 推迟{cand.metrics.skip_count})"
        else:
            annot = "  (stale)"
    return f"  │   {marker} #{task.id} {task.title}{annot}"


def format_heap_map(result: GCResult, roots: set[int], *, heap_max: int | None = None) -> str:
    """把当前活跃堆渲染成 JVM 风格的分代堆内存图(僵尸对象以 ☠ 标出)。

    逐代(Eden / Survivor / Old / Metaspace)列出堆上对象，顶部给出 -Xmx 占用填充条，
    并为每个回收候选标注判死原因。已回收(COLLECTED)的对象在垃圾桶而非堆上，不出现在
    此图；Metaspace(元任务)不计入 -Xmx 占用，与 store.heap_size 同口径。

    Args:
        result (GCResult): 一次 GC 的结果；survivors + candidates 即堆上全部对象。
        roots (set[int]): 活跃 root 的 id 集合(标 ★)。
        heap_max (int | None): 堆容量上限(-Xmx)；给出则画占用填充条，None 则只报对象数。

    Returns:
        str: 多行堆内存图文本。
    """
    cand_of = {c.task.id: c for c in result.candidates}
    tasks = list(result.survivors) + [c.task for c in result.candidates]
    by_gen: dict[Generation, list[Task]] = {gen: [] for gen, _, _ in _GEN_ORDER}
    for t in tasks:
        by_gen.setdefault(t.gen, []).append(t)
    used = sum(1 for t in tasks if t.gen is not Generation.META)

    if heap_max is not None:
        pct = round(100 * used / heap_max, 1) if heap_max > 0 else 0.0
        usage = (
            f"  -Xmx 占用: {used} / {heap_max} obj  "
            f"{_fill_bar(used, heap_max)} {pct}%   (Metaspace 不计)"
        )
    else:
        usage = f"  堆对象: {used} 个(未设 -Xmx)   (Metaspace 不计)"

    lines = ["━━ 堆内存快照 · jmap -heap ━━━━━━━━━━━━━━━━", usage, ""]
    for i, (gen, en, cn) in enumerate(_GEN_ORDER):
        conn = "┌" if i == 0 else "├"
        items = sorted(by_gen.get(gen, []), key=lambda t: t.id or 0)
        lines.append(f"  {conn} {en} · {cn} ({len(items)})")
        if items:
            lines.extend(_heap_row(t, roots, cand_of) for t in items)
        else:
            lines.append("  │   (空)")
    lines.append("  └" + "─" * 14)
    lines.append("  图例: ★ root · ☠ 回收候选(僵尸) · ⊛ 元任务")
    return "\n".join(lines)


def format_report(
    result: GCResult,
    *,
    views_of: Callable[[int], int] | None = None,
    **log_kwargs: object,
) -> str:
    """把 GC 日志与候选清单拼成一份完整回收报告。

    Args:
        result (GCResult): 本轮 GC 结果。
        views_of (Callable[[int], int] | None): 透传给 format_candidates 的查看次数回调。
        **log_kwargs (object): 透传给 format_gc_log 的关键字参数
            (gc_id / uptime / pause_full / cause / pause / heap_max)。

    Returns:
        str: GC 日志 + 空行 + 候选清单。
    """
    return format_gc_log(result, **log_kwargs) + "\n\n" + format_candidates(result, views_of=views_of)

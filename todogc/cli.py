# cli.py —— 命令行入口：把录入/标记/依赖/GC 全链路串起来，核心是 Stop-the-world 回收仪式(复活或了断)。

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from todogc import console, report
from todogc.collector import Candidate, Collector, GCPolicy, GCResult
from todogc.config import ConfigError, load_config
from todogc.heap import ReferenceStrength, State, TouchKind
from todogc.store import Store

HEAP_MAX = 50  # -Xmx：你能同时扛的任务数上限，仅用于 GC 日志展示。


# ============ 回收仪式(可测核心) ============

@dataclass
class CeremonyOutcome:
    """一次 Stop-the-world 仪式的处置结果。

    Attributes:
        revived (list[int]): 被复活的任务 id。
        collected (list[int]): 被回收的任务 id。
        skipped (list[int]): 被跳过(暂缓处置)的任务 id。
    """

    revived: list[int] = field(default_factory=list)
    collected: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)


def _finalize_spent(store: Store, task_id: int) -> bool:
    """判断某任务的 finalize() 是否已用掉(此前复活过一次)。

    对应 JVM 规则：对象只调用一次 finalize，复活过的下次回收不再触发。

    Args:
        store (Store): 数据源。
        task_id (int): 任务 id。

    Returns:
        bool: 触碰历史里出现过 REVIVE 则为 True。
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
    """对候选清单逐个执行处置：复活 / 回收 / 跳过(仪式的纯逻辑核心，与交互解耦)。

    回收时若该任务从未复活过，先调 finalize() 收集遗言；复活过的直接清并盖章
    "finalize() already called"。复活会 age +1(熬过一轮)并按需重连 root。

    Args:
        store (Store): 数据源，会被本方法写入。
        result (GCResult): 待处置的 GC 结果。
        decide (Callable[[Candidate], str]): 处置决策，返回 "revive" / "collect" / "skip"。
        eulogize (Callable[[Candidate], str] | None): 收集遗言的回调；None 则沉默回收。
        revive_root (Callable[[Candidate], int | None] | int | None): 复活时若任务未关联
            root，用它取要重连的 root id；可传固定 int 或回调，None 表示不重连。
        now (datetime | None): 事件时间基准；None 用当前 UTC。

    Returns:
        CeremonyOutcome: 各处置分类的 id 列表。
    """
    now = now or datetime.now(timezone.utc)
    out = CeremonyOutcome()
    for cand in result.candidates:
        task = cand.task
        assert task.id is not None
        choice = decide(cand)
        if choice == "collect":
            if _finalize_spent(store, task.id):
                note = "finalize() already called —— 上次复活过，这次没得聊"
            else:
                eulogy = eulogize(cand).strip() if eulogize else ""
                note = eulogy or "(沉默回收，未留遗言)"
            task.state = State.COLLECTED
            store.update_task(task)
            store.record(task.id, TouchKind.COLLECT, note=note, ts=now)
            out.collected.append(task.id)
        elif choice == "revive":
            if task.is_unlinked and revive_root is not None:
                r = revive_root(cand) if callable(revive_root) else revive_root
                if r is not None:
                    store.link(task.id, r, ts=now)
                    task.root_id = r
            survivor = task.aged()  # 熬过一轮：age +1，分代相应晋升
            survivor.state = State.LIVE
            store.update_task(survivor)
            store.record(task.id, TouchKind.REVIVE, ts=now)
            out.revived.append(task.id)
        else:
            out.skipped.append(task.id)
    return out


# ============ 子命令 ============

def _run_gc(store: Store, now: datetime, policy: GCPolicy | None = None) -> GCResult:
    """构建引用图并跑一次标记-清除(add 的 OOM 检查与 gc 命令共用)。

    Args:
        store (Store): 数据源。
        now (datetime): 判活时间基准。

    Returns:
        GCResult: 本轮 GC 结果。
    """
    return Collector(policy).run_on_store(store, store.root_ids(), extra_edges=store.deps(), now=now)


def cmd_add(store: Store, args: argparse.Namespace) -> int:
    """add：录入(分配)一个任务；活跃堆(不含 Metaspace)满时触发 Allocation Failure，先逼你回收。

    Args:
        store (Store): 数据源。
        args (argparse.Namespace): 需含 title / root_id / root / meta / force / heap_max。

    Returns:
        int: 退出码；堆满且非元任务、未 --force 时返回 1(拒绝分配)。
    """
    size = store.heap_size()
    if not args.force and not args.meta and size >= args.heap_max:
        result = _run_gc(store, datetime.now(timezone.utc), args.gc_policy)
        lines = [f"[GC (Allocation Failure)] 活跃堆 {size}/{args.heap_max} 已满——想分配新对象，先回收。"]
        if result.candidates:
            lines.append(report.format_candidates(result, views_of=store.view_count))
            lines.append("→ 跑 `todogc gc` 处置这些僵尸腾出空间，或 `add --force` 强行超配。")
        else:
            lines.append("[Full GC (Ergonomics)] 且没有可回收的僵尸——-Xmx 已拉满，GC 无力回天。")
            lines.append("→ 砍需求，不是加内存(真要硬来：add --force)。")
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
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    tag = " ★root" if args.root else (" [meta]" if args.meta else "")
    print(f"分配 #{t.id}: {t.title}{tag}")
    return 0


def cmd_root(store: Store, args: argparse.Namespace) -> int:
    """root：把任务标记 / 取消标记为活跃 GC root。

    Args:
        store (Store): 数据源。
        args (argparse.Namespace): 需含 id / off。

    Returns:
        int: 退出码。
    """
    try:
        store.set_root(args.id, not args.off)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"#{args.id} {'已取消 root' if args.off else '设为 ★root'}")
    return 0


def cmd_dep(store: Store, args: argparse.Namespace) -> int:
    """dep：新增 / 删除一条"理由"边(task 的理由是 ref)。

    Args:
        store (Store): 数据源。
        args (argparse.Namespace): 需含 task_id / ref_id / remove。

    Returns:
        int: 退出码；自环等非法输入返回 1。
    """
    try:
        if args.remove:
            store.remove_dep(args.task_id, args.ref_id)
            print(f"删除理由边 #{args.task_id} → #{args.ref_id}")
        else:
            store.add_dep(args.task_id, args.ref_id)
            print(f"#{args.task_id} 的理由 → #{args.ref_id}")
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


def cmd_ls(store: Store, args: argparse.Namespace) -> int:
    """ls：列出任务(会记 view 埋点)，标出分代 / 年龄 / root / 关联 / 状态。

    Args:
        store (Store): 数据源。
        args (argparse.Namespace): 需含 state。

    Returns:
        int: 退出码。
    """
    if args.state == State.CANDIDATE.value:
        result = _run_gc(store, datetime.now(timezone.utc), args.gc_policy)
        tasks = [c.task for c in result.candidates]
    else:
        state = State(args.state) if args.state else None
        tasks = store.list_tasks(state=state)
    roots = store.root_ids()
    if console.render_ls(tasks, roots):  # 装了 rich → 彩色表格
        return 0
    if not tasks:
        print("(空堆)")
        return 0
    for t in tasks:
        mark = "★" if t.id in roots else " "
        link = f" →#{t.root_id}" if t.root_id is not None else ""
        meta = " [meta]" if t.is_meta else ""
        print(f" {mark} #{t.id:>3} [{t.gen.value}:{t.age}] {t.title}{link}{meta}  <{t.state.value}>")
    return 0


def cmd_progress(store: Store, args: argparse.Namespace) -> int:
    """progress：记一次真推进(计入 refcount)。

    Args:
        store (Store): 数据源。
        args (argparse.Namespace): 需含 id / note。

    Returns:
        int: 退出码。
    """
    if store.get_task(args.id, record_view=False) is None:
        print(f"✗ 没有 #{args.id}", file=sys.stderr)
        return 1
    try:
        store.progress(args.id, args.note)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"#{args.id} +1 refcount。这才算数。")
    return 0


def cmd_snooze(store: Store, args: argparse.Namespace) -> int:
    """snooze：推迟一个任务(喂 skip_count)。

    Args:
        store (Store): 数据源。
        args (argparse.Namespace): 需含 id / note。

    Returns:
        int: 退出码。
    """
    if store.get_task(args.id, record_view=False) is None:
        print(f"✗ 没有 #{args.id}", file=sys.stderr)
        return 1
    try:
        store.defer(args.id, args.note)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"#{args.id} 又往后推了一次。skip_count +1，它在记账。")
    return 0


def cmd_gc(store: Store, args: argparse.Namespace) -> int:
    """gc：跑标记-清除，打 GC 日志与候选清单，再进 Stop-the-world 回收仪式。

    Args:
        store (Store): 数据源。
        args (argparse.Namespace): 需含 full / dry_run。

    Returns:
        int: 退出码。
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
    if not console.print_text(body):  # 装了 rich → 关键词上色；否则纯文本
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

    print("\n—— Stop-the-world：世界暂停，逐个处置 ——")

    def decide(cand: Candidate) -> str:
        print(f"\n  #{cand.task.id} {cand.task.title}  [{cand.reason.value}]")
        print(f"    {report.generation_taunt(cand.task.gen, cand.task.age)}")
        while True:
            ans = input("    [r]复活  [c]回收  [s]跳过 > ").strip().lower()
            if ans in {"r", "c", "s"}:
                return {"r": "revive", "c": "collect", "s": "skip"}[ans]

    def eulogize(cand: Candidate) -> str:
        return input("    finalize() 遗言(回车=沉默回收) > ")

    def revive_root(cand: Candidate) -> int | None:
        roots = sorted(store.root_ids())
        if not roots:
            print("    ⚠ 没有 root 可复活到，先 add --root；本次按原样保留。")
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
        items=[(c.task.id, c.reason.value, actions[c.task.id]) for c in result.candidates],
    )
    print(f"\n[GC] 仪式结束：复活 {len(out.revived)} · 回收 {len(out.collected)} · 跳过 {len(out.skipped)}")
    return 0


def cmd_heap(store: Store, args: argparse.Namespace) -> int:
    """heap(别名 jmap)：打印分代堆内存图——各代对象分布、-Xmx 占用、僵尸(☠)标红。

    只读诊断快照：跑一次标记-清除拿到判定，但不进回收仪式、不写库，也不记 view 埋点
    (复用 GC 扫描的 record_view=False 口径，避免每次看堆图都虚增查看次数)。

    Args:
        store (Store): 数据源。
        args (argparse.Namespace): 需含 heap_max。

    Returns:
        int: 退出码(恒 0)。
    """
    now = datetime.now(timezone.utc)
    result = _run_gc(store, now, args.gc_policy)
    body = report.format_heap_map(result, store.root_ids(), heap_max=args.heap_max)
    if not console.print_text(body):  # 装了 rich → 僵尸标红/★描金/填充条上色；否则纯文本
        print(body)
    return 0


def cmd_web(store: Store, args: argparse.Namespace) -> int:
    """web：启动 Streamlit Web 端(浏览器里的堆内存图 + add/gc/restore 交互)。

    以子进程运行 `python -m streamlit run webapp.py`，把当前 --db / --heap-max 通过
    环境变量(TODOGC_DB / TODOGC_HEAP_MAX)透传给 Web 端，确保它和 CLI 用同一个库。
    未安装 streamlit 时只给安装提示、绝不抛栈。本命令不使用 store(仅需路径参数)，
    store 由 main() 统一开关，这里忽略。

    Args:
        store (Store): 数据源(本命令不用，仅为子命令签名统一)。
        args (argparse.Namespace): 需含 db / heap_max / port。

    Returns:
        int: streamlit 子进程的退出码；未安装 streamlit 时返回 1。
    """
    if importlib.util.find_spec("streamlit") is None:
        print(
            "未安装 streamlit —— Web 端是可选功能。\n"
            '  安装：pip install -e ".[web]"\n'
            "  再跑：todogc web",
            file=sys.stderr,
        )
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
    }
    cmd = [sys.executable, "-m", "streamlit", "run", webapp]
    if args.port is not None:
        cmd += ["--server.port", str(args.port)]
    print(f"启动 Web 端(库 {args.db} · -Xmx {args.heap_max}) —— Ctrl-C 停止")
    return subprocess.run(cmd, env=env).returncode


def cmd_graveyard(store: Store, args: argparse.Namespace) -> int:
    """graveyard：悼词墙，列出已回收任务的墓志铭与遗言。

    Args:
        store (Store): 数据源。
        args (argparse.Namespace): 无额外字段。

    Returns:
        int: 退出码。
    """
    dead = store.list_tasks(state=State.COLLECTED, record_view=False)
    if not dead:
        print("墓园空空。你还没对自己诚实过。")
        return 0
    for t in dead:
        collects = [x for x in store.touches(t.id) if x.kind is TouchKind.COLLECT]
        last = collects[-1] if collects else None
        days = (last.ts - t.created_at).days if last and t.created_at else 0
        print(f"† {t.title}   存活 {days}d · 推进 {store.refcount(t.id)} · 查看 {store.view_count(t.id)}")
        print(f"  「{last.note if last else ''}」")
    return 0


def cmd_restore(store: Store, args: argparse.Namespace) -> int:
    """restore：把某任务从垃圾桶恢复为存活(防手滑)。

    Args:
        store (Store): 数据源。
        args (argparse.Namespace): 需含 id。

    Returns:
        int: 退出码；任务不存在返回 1。
    """
    t = store.get_task(args.id, record_view=False)
    if t is None:
        print(f"✗ 没有 #{args.id}", file=sys.stderr)
        return 1
    if t.state is not State.COLLECTED:
        print(f"✗ #{args.id} 尚未被回收", file=sys.stderr)
        return 1
    if args.root_id is not None:
        try:
            root = store.get_task(args.root_id, record_view=False)
            if root is None or root.state is State.COLLECTED:
                raise ValueError(f"恢复目标 #{args.root_id} 不存在或已回收")
            if args.root_id == args.id:
                raise ValueError("任务不能恢复到自己下面")
            t.root_id = args.root_id
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
    t.state = State.LIVE
    store.update_task(t)
    store.record(args.id, TouchKind.RESTORE)
    print(f"#{args.id} 已恢复。手滑难免，这次不算自欺。")
    return 0


def cmd_show(store: Store, args: argparse.Namespace) -> int:
    """显示任务详情和关键指标，不写 VIEW 事件。"""
    task = store.get_task(args.id, record_view=False)
    if task is None:
        print(f"✗ 没有 #{args.id}", file=sys.stderr)
        return 1
    roots = store.root_ids()
    print(f"#{task.id} {task.title}")
    print(
        f"  state={task.state.value} gen={task.gen.value}:{task.age} "
        f"ref={task.ref_strength.value} root={'yes' if task.id in roots else 'no'} "
        f"parent={task.root_id or '-'}"
    )
    print(
        f"  progress={store.refcount(task.id)} views={store.view_count(task.id)} "
        f"skips={store.skip_count(task.id)} consecutive_skips={store.consecutive_skip_count(task.id)}"
    )
    deps = [ref for tid, ref in store.deps() if tid == task.id]
    print(f"  deps={','.join(map(str, deps)) if deps else '-'}")
    return 0


def cmd_history(store: Store, args: argparse.Namespace) -> int:
    """打印 append-only 事件时间线。"""
    if store.get_task(args.id, record_view=False) is None:
        print(f"✗ 没有 #{args.id}", file=sys.stderr)
        return 1
    touches = store.touches(args.id)
    if args.limit is not None:
        touches = touches[-args.limit:]
    for touch in touches:
        note = f" · {touch.note}" if touch.note else ""
        print(f"{touch.ts.isoformat()}  {touch.kind.value}{note}")
    return 0


def cmd_edit(store: Store, args: argparse.Namespace) -> int:
    """编辑任务标题/引用强度。"""
    if args.title is None and args.ref_strength is None:
        print("✗ edit 至少需要新标题或 --ref", file=sys.stderr)
        return 1
    try:
        task = store.edit_task(args.id, title=args.title, ref_strength=args.ref_strength)
    except (ValueError, KeyError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"#{task.id} 已更新：{task.title} [{task.ref_strength.value}]")
    return 0


def cmd_link(store: Store, args: argparse.Namespace) -> int:
    """建立或解除父/目标关联。"""
    try:
        if args.off:
            store.unlink(args.id)
            print(f"#{args.id} 已解除关联")
        else:
            if args.root_id is None:
                raise ValueError("link 需要 root_id，或使用 --off")
            store.link(args.id, args.root_id)
            print(f"#{args.id} 已关联到 #{args.root_id}")
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(store: Store, args: argparse.Namespace) -> int:
    """扫描认知内存泄漏。"""
    now = datetime.now(timezone.utc)
    result = _run_gc(store, now, args.gc_policy)
    print(report.format_doctor(store, result, now=now))
    return 0


def _days(value: str) -> int:
    """解析 stats 的 ``30d`` 或纯整数天数。"""
    raw = value[:-1] if value.lower().endswith("d") else value
    try:
        days = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("时间范围应为 30d 或正整数") from exc
    if days <= 0:
        raise argparse.ArgumentTypeError("时间范围必须大于 0")
    return days


def _positive_int(value: str) -> int:
    """argparse 正整数类型。"""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def cmd_stats(store: Store, args: argparse.Namespace) -> int:
    """显示已执行 GC 的聚合统计。"""
    print(report.format_stats(store.gc_runs(since_days=args.since)))
    return 0


# ============ 组装与入口 ============

def build_parser() -> argparse.ArgumentParser:
    """构造带全部子命令的 argparse 解析器。

    Returns:
        argparse.ArgumentParser: 已挂好子命令与 func 默认值的解析器。
    """
    parser = argparse.ArgumentParser(prog="todogc", description="用 GC 冷酷回收你永远不会做的任务。")
    parser.add_argument("--db", default=os.environ.get("TODOGC_DB", "todogc.db"), help="数据库路径")
    parser.add_argument("--config", default=None, help="配置文件路径(默认 todogc.toml)")
    parser.add_argument(
        "--heap-max", type=_positive_int, default=None, help="活跃堆上限(-Xmx)，满则触发 Allocation Failure"
    )
    parser.add_argument("--gc-mode", choices=["gentle", "normal", "aggressive"], default=None)
    parser.add_argument("--recent-days", type=_positive_int, default=None)
    parser.add_argument("--max-skips", type=_positive_int, default=None, help="连续推迟阈值")
    parser.add_argument("--restore-grace-days", type=_positive_int, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="录入一个任务")
    p.add_argument("title")
    p.add_argument("--root-id", type=int, default=None, help="关联到的父/目标 id")
    p.add_argument("--root", action="store_true", help="本身设为活跃 root")
    p.add_argument("--meta", action="store_true", help="标为元任务(Metaspace)")
    p.add_argument(
        "--ref", dest="ref_strength", choices=[r.value for r in ReferenceStrength],
        default=ReferenceStrength.SOFT.value, help="引用强度",
    )
    p.add_argument("--force", action="store_true", help="堆满时强行超配(跳过 Allocation Failure)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("root", help="标记/取消活跃 root")
    p.add_argument("id", type=int)
    p.add_argument("--off", action="store_true", help="取消 root")
    p.set_defaults(func=cmd_root)

    p = sub.add_parser("dep", help="记录/删除一条理由边(task 的理由是 ref)")
    p.add_argument("task_id", type=int)
    p.add_argument("ref_id", type=int)
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_dep)

    p = sub.add_parser("ls", help="列出任务")
    p.add_argument("--state", choices=[s.value for s in State], default=None)
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("progress", help="记一次真推进")
    p.add_argument("id", type=int)
    p.add_argument("note", nargs="?", default=None)
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser("snooze", help="推迟一个任务")
    p.add_argument("id", type=int)
    p.add_argument("note", nargs="?", default=None)
    p.set_defaults(func=cmd_snooze)

    p = sub.add_parser("gc", help="触发垃圾回收 + 回收仪式")
    p.add_argument(
        "--mode", dest="command_gc_mode", choices=["gentle", "normal", "aggressive"], default=None
    )
    p.add_argument("--full", action="store_true", help="渲染为 Full GC")
    p.add_argument("--dry-run", action="store_true", help="只出报告，不进仪式")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("heap", aliases=["jmap"], help="打印分代堆内存图(僵尸标红)")
    p.set_defaults(func=cmd_heap)

    p = sub.add_parser("web", help="启动 Streamlit Web 端(堆内存图 + 交互，需 [web] extra)")
    p.add_argument("--port", type=int, default=None, help="Web 端端口(默认 streamlit 的 8501)")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("graveyard", help="悼词墙")
    p.set_defaults(func=cmd_graveyard)

    p = sub.add_parser("restore", help="从垃圾桶恢复")
    p.add_argument("id", type=int)
    p.add_argument("--root-id", type=int, default=None, help="恢复时重新关联到父/目标")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("show", help="查看任务详情")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("history", help="查看任务事件时间线")
    p.add_argument("id", type=int)
    p.add_argument("--limit", type=_positive_int, default=None)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("edit", help="编辑标题或引用强度")
    p.add_argument("id", type=int)
    p.add_argument("title", nargs="?", default=None)
    p.add_argument("--ref", dest="ref_strength", choices=[r.value for r in ReferenceStrength], default=None)
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("link", help="关联或解除父/目标")
    p.add_argument("id", type=int)
    p.add_argument("root_id", type=int, nargs="?", default=None)
    p.add_argument("--off", action="store_true")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("doctor", help="诊断认知内存泄漏")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("stats", help="查看 GC 历史统计")
    p.add_argument("--since", type=_days, default=None, help="例如 30d")
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口：解析参数、打开 store、分发到子命令。

    Args:
        argv (list[str] | None): 参数列表；None 时用 sys.argv[1:]。

    Returns:
        int: 进程退出码。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
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
    except (ConfigError, ValueError) as exc:
        print(f"✗ 配置错误：{exc}", file=sys.stderr)
        return 2
    store = Store(args.db)
    try:
        return args.func(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())

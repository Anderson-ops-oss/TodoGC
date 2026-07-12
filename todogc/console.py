# console.py —— 可选的 rich 彩色渲染层：装了 rich 就上色，没装就回退纯文本(核心零依赖不受影响)。

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from todogc.heap import Generation, State, Task

if TYPE_CHECKING:
    from rich.console import Console  # pyright: ignore[reportMissingImports]
    from rich.highlighter import RegexHighlighter  # pyright: ignore[reportMissingImports]
    from rich.markup import escape  # pyright: ignore[reportMissingImports]
    from rich.table import Table  # pyright: ignore[reportMissingImports]
    from rich.theme import Theme  # pyright: ignore[reportMissingImports]

    _HAVE_RICH = True
else:
    try:
        from rich.console import Console
        from rich.highlighter import RegexHighlighter
        from rich.markup import escape
        from rich.table import Table
        from rich.theme import Theme

        _HAVE_RICH = True
    except ImportError:  # rich 未安装：一切回退纯文本
        _HAVE_RICH = False


def have_rich() -> bool:
    """rich 是否可用。

    Returns:
        bool: 安装了 rich 为 True。
    """
    return _HAVE_RICH


# 分代 / 状态 → 颜色。
_GEN_STYLE = {
    Generation.EDEN: "green",
    Generation.SURVIVOR: "yellow",
    Generation.OLD: "red",
    Generation.META: "magenta",
}
_STATE_STYLE = {
    State.LIVE: "green",
    State.CANDIDATE: "yellow",
    State.COLLECTED: "dim",
}


if _HAVE_RICH:

    class _GCHighlighter(RegexHighlighter):
        """给 GC 日志 / 候选清单里的关键字上色(纯正则匹配，不靠文本里的方括号)。"""

        base_style = "gc."
        highlights = [
            r"(?P<full>Pause Full \([^)]*\))",
            r"(?P<young>Pause Young \([^)]*\))",
            r"(?P<cycle>reference cycle[^\n]*|\[reference cycle[^\]]*\][^\n]*|互相担保[^\n]*)",
            r"(?P<stale>\[stale\][^\n]*)",
            r"(?P<unreach>\[unreachable\][^\n]*)",
            r"(?P<oom>Allocation Failure|Ergonomics|无力回天)",
            r"(?P<pct>释放认知内存 [\d.]+%)",
            r"(?P<zombie>\d+ zombies)",
            # —— 堆内存图(format_heap_map)：僵尸行标红是核心诉求，其余为点缀。
            # 用 │\s+ 作前缀锚点(只匹配对象行、放过图例行)，仅命名组内的文本被上色。
            r"│\s+(?P<heap_zombie>☠[^\n]*)",
            r"│\s+(?P<heap_root>★)",
            r"│\s+(?P<heap_meta>⊛)",
            r"(?P<heap_bar>\[[█░]+\])",
            r"(?P<heap_region>[┌├] [A-Za-z]+ ·[^\n]*)",
        ]

    _THEME = Theme(
        {
            "gc.full": "bold red",
            "gc.young": "bold yellow",
            "gc.cycle": "red",
            "gc.stale": "yellow",
            "gc.unreach": "yellow",
            "gc.oom": "bold red",
            "gc.pct": "bold cyan",
            "gc.zombie": "bold red",
            "gc.heap_zombie": "red",
            "gc.heap_root": "bold gold1",
            "gc.heap_meta": "magenta",
            "gc.heap_bar": "cyan",
            "gc.heap_region": "bold",
        }
    )


def _make_console(err: bool):
    """构造带主题与高亮的 rich Console。

    Args:
        err (bool): True 输出到 stderr，否则 stdout。

    Returns:
        Console | None: rich 可用时返回 Console，否则 None。
    """
    if not _HAVE_RICH:
        return None
    return Console(stderr=err, theme=_THEME, highlighter=_GCHighlighter())


def print_text(text: str, *, err: bool = False) -> bool:
    """带高亮打印一段 GC 日志 / 候选文本；关掉 markup 以免文本里的方括号被误解析。

    Args:
        text (str): report 生成的纯文本。
        err (bool): 是否输出到 stderr。

    Returns:
        bool: True=已用 rich 打印；False=rich 不可用，需调用方回退纯文本。
    """
    c = _make_console(err)
    if c is None:
        return False
    c.print(text, markup=False, soft_wrap=True)
    return True


def render_ls(tasks: Iterable[Task], root_ids: set[int]) -> bool:
    """把任务列表渲染成彩色表格(分代 / 状态按色区分，root 标 ★)。

    Args:
        tasks (Iterable[Task]): 待渲染任务。
        root_ids (set[int]): 活跃 root 的 id 集合。

    Returns:
        bool: True=已用 rich 渲染；False=rich 不可用，需回退纯文本。
    """
    if not _HAVE_RICH:
        return False
    c = Console()
    rows = list(tasks)
    if not rows:
        c.print("[dim](空堆)[/dim]")
        return True
    table = Table(show_header=True, header_style="bold")
    columns: tuple[tuple[str, Literal["left", "right"]], ...] = (
        ("#", "right"),
        ("gen", "left"),
        ("age", "right"),
        ("title", "left"),
        ("ref", "left"),
        ("→root", "left"),
        ("state", "left"),
    )
    for col, just in columns:
        table.add_column(col, justify=just)
    for t in rows:
        star = "[gold1]★[/] " if t.id in root_ids else ""
        table.add_row(
            str(t.id),
            f"[{_GEN_STYLE.get(t.gen, 'white')}]{t.gen.value}[/]",
            str(t.age),
            f"{star}{escape(t.title)}",
            t.ref_strength.value,
            f"#{t.root_id}" if t.root_id is not None else "",
            f"[{_STATE_STYLE.get(t.state, 'white')}]{t.state.value}[/]",
        )
    c.print(table)
    return True

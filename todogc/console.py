# console.py — optional rich-based color rendering layer: renders in color if rich is
# installed, falls back to plain text if not (the core stays dependency-free either way).

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from todogc.heap import Generation, State, Task
from todogc.i18n import t as _t

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
    except ImportError:  # rich not installed: everything falls back to plain text
        _HAVE_RICH = False


def have_rich() -> bool:
    """Whether rich is available.

    Returns:
        bool: True if rich is installed.
    """
    return _HAVE_RICH


# Generation / state -> color.
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
    State.DONE: "bold cyan",
}


if _HAVE_RICH:

    class _GCHighlighter(RegexHighlighter):
        """Highlights keywords in the GC log / candidate listing (plain regex matching, doesn't rely on brackets in the text)."""

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
            # -- Heap map (format_heap_map): highlighting zombie rows in red is the core
            # requirement, the rest is decoration. Uses │\s+ as a prefix anchor (matches only
            # object rows, skips the legend row); only the text inside the named group gets
            # colored.
            r"│\s+(?P<heap_zombie>☠[^\n]*)",
            r"│\s+(?P<heap_root>★)",
            r"│\s+(?P<heap_meta>⊛)",
            r"(?P<heap_bar>\[[█░]+\])",
            r"(?P<heap_region>[┌├] [^\n]+ \(\d+\))",
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
    """Build a rich Console with the theme and highlighter wired up.

    Args:
        err (bool): True to write to stderr, otherwise stdout.

    Returns:
        Console | None: a Console if rich is available, otherwise None.
    """
    if not _HAVE_RICH:
        return None
    return Console(stderr=err, theme=_THEME, highlighter=_GCHighlighter())


def print_text(text: str, *, err: bool = False) -> bool:
    """Print a GC log / candidate listing with highlighting; markup is disabled so brackets
    in the text aren't misparsed as rich markup.

    Args:
        text (str): plain text produced by report.
        err (bool): whether to write to stderr.

    Returns:
        bool: True if printed via rich; False if rich isn't available and the caller needs
            to fall back to plain text.
    """
    c = _make_console(err)
    if c is None:
        return False
    c.print(text, markup=False, soft_wrap=True)
    return True


def render_ls(tasks: Iterable[Task], root_ids: set[int]) -> bool:
    """Render the task list as a color table (generation / state distinguished by color,
    root marked with ★).

    Args:
        tasks (Iterable[Task]): tasks to render.
        root_ids (set[int]): ids of the active roots.

    Returns:
        bool: True if rendered via rich; False if rich isn't available and the caller needs
            to fall back to plain text.
    """
    if not _HAVE_RICH:
        return False
    c = Console()
    rows = list(tasks)
    if not rows:
        c.print(_t("cli.ls.empty_heap"), markup=False, style="dim")
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
            f"{star}{escape(t.title)}" + (" [inbox]" if t.in_inbox else ""),
            t.ref_strength.value,
            f"#{t.root_id}" if t.root_id is not None else "",
            f"[{_STATE_STYLE.get(t.state, 'white')}]{t.state.value}[/]",
        )
    c.print(table)
    return True

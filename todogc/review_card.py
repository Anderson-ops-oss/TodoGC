"""A shared vector layout for SVG and optional Pillow PNG exports. No network access."""

from __future__ import annotations

import html
import io
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from todogc.i18n import t as _t
from todogc.review import WeeklyReview

WIDTH, HEIGHT = 1080, 1350
INK, PAPER, MINT, MUTED, RULE, TRACK = '#142126', '#f1f4ee', '#9bdfbf', '#9bafb2', '#385056', '#23373d'


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float
    fill: str
    radius: float = 0


@dataclass(frozen=True)
class Line:
    x: float
    y: float
    end_x: float
    end_y: float
    color: str
    width: float = 2


@dataclass(frozen=True)
class Text:
    x: float
    y: float
    value: str
    size: int
    color: str = PAPER
    bold: bool = False
    mono: bool = False


Element = Rect | Line | Text


def _clean(value: str) -> str:
    return ' '.join(''.join(c for c in value if c in '\n\t' or 32 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF).split())


def _units(value: str) -> float:
    return sum(1 if unicodedata.east_asian_width(c) in {'W', 'F'} else
               .95 if c in 'WM@%' else .3 if c in 'ijlI.,:! ' else .66 if c.isupper() else .58 for c in value)


def _wrap(value: str, size: int, width: int, limit: int = 2) -> list[str]:
    text = _clean(value)
    lines: list[str] = []
    while text and len(lines) < limit:
        end = 0
        while end < len(text) and _units(text[:end + 1]) * size <= width:
            end += 1
        if end == len(text):
            lines.append(text)
            break
        end = max(1, end)
        space = text.rfind(' ', 0, end)
        if space > end // 2:
            end = space
        line, text = text[:end].rstrip(), text[end:].lstrip()
        if len(lines) == limit - 1 and text:
            while line and _units(line + '…') * size > width:
                line = line[:-1]
            line += '…'
        lines.append(line)
    return lines


def card_layout(review: WeeklyReview) -> list[Element]:
    start, end = date.fromisoformat(review.week_start), date.fromisoformat(review.week_end)
    months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
    period = f'{months[start.month - 1]} {start.day} – {months[end.month - 1]} {end.day}, {end.year}'
    if start.year != end.year:
        period = f'{start.isoformat()} – {end.isoformat()}'
    status = 'Full week'
    if review.partial:
        status = f'In progress · through {review.through_local}'
    elements: list[Element] = [Rect(0, 0, WIDTH, HEIGHT, INK), Rect(64, 65, 10, 23, MINT),
                               Rect(80, 55, 10, 33, MINT), Rect(96, 69, 10, 19, MINT),
                               Text(125, 57, 'TodoGC', 32, bold=True),
                               Text(64, 130, 'Weekly review', 48, bold=True),
                               Text(64, 197, period, 26, MUTED), Text(64, 235, status, 20, MUTED)]
    for x, count, label in [(64, review.tasks_completed, 'Tasks completed'), (574, review.goals_completed, 'Goals closed')]:
        number = f'{count:,}'
        elements += [Text(x, 287, number, 108 if len(number) <= 4 else 76, MINT, bold=True),
                     Text(x + 2, 407, label, 26), Line(x, 454, x + 442, 454, RULE)]
    elements += [Text(64, 485, 'Finishes by day', 27),
                 Text(64, 525, 'Tasks + goals, counted once per week', 20, MUTED)]
    maximum = max(max(review.daily), 1)
    for i, (name, value) in enumerate(zip(('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'), review.daily)):
        x = 78 + i * 136
        height = 135 * value / maximum
        elements.append(Rect(x + 13, 577, 56, 135, TRACK, 6))
        if value:
            elements.append(Rect(x + 13, 712 - height, 56, height, MINT, min(6, height / 2)))
        elements += [Text(x + 16, 722, name, 21, MUTED), Text(x + 28, 556, str(value), 18, MINT)]
    elements += [Line(64, 774, 1016, 774, RULE), Text(64, 807, 'Highlights', 27)]
    if review.highlights:
        for i, item in enumerate(review.highlights):
            y = 861 + i * 80
            elements += [Line(68, y + 13, 76, y + 21, MINT, 3), Line(76, y + 21, 91, y + 3, MINT, 3)]
            for j, line in enumerate(_wrap(item.title, 25, 875)):
                elements.append(Text(112, y + j * 31, line, 25))
    else:
        elements += [Text(64, 876, 'Room for what comes next.', 30, MUTED),
                     Text(64, 923, 'One week at a time.', 23, MUTED)]
    elements.append(Line(64, 1118, 1016, 1118, RULE))
    for x, count, label in [(64, review.reclaimed, 'Reclaimed'), (310, review.discarded, 'Discarded'),
                             (556, review.captured, 'Captured'), (802, review.organized, 'Organized')]:
        elements += [Text(x, 1150, f'{count:,}', 39, PAPER), Text(x, 1203, label, 21, MUTED)]
    elements += [Line(64, 1250, 1016, 1250, RULE), Text(64, 1280, 'Less clutter. More room.', 24, MINT),
                 Text(867, 1285, 'todogc', 20, MUTED, mono=True)]
    return elements


def render_svg(review: WeeklyReview) -> bytes:
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img">',
             '<title>TodoGC weekly review</title>',
             f'<desc>{review.tasks_completed} tasks completed, {review.goals_completed} goals closed. '
             f'{review.week_start} to {review.week_end}.</desc>']
    for item in card_layout(review):
        if isinstance(item, Rect):
            parts.append(f'<rect x="{item.x}" y="{item.y}" width="{item.width}" height="{item.height}" rx="{item.radius}" fill="{item.fill}"/>')
        elif isinstance(item, Line):
            parts.append(f'<line x1="{item.x}" y1="{item.y}" x2="{item.end_x}" y2="{item.end_y}" stroke="{item.color}" stroke-width="{item.width}"/>')
        else:
            font = 'ui-monospace, Menlo, monospace' if item.mono else 'Arial, Helvetica, sans-serif'
            parts.append(f'<text x="{item.x}" y="{item.y + item.size * .82}" fill="{item.color}" '
                         f'font-family="{font}" font-size="{item.size}" font-weight="{600 if item.bold else 400}">'
                         f'{html.escape(item.value)}</text>')
    return ('\n'.join(parts) + '\n</svg>').encode('utf-8')


def render_png(review: WeeklyReview) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise ValueError(_t('review.error.pillow')) from exc
    scale = 2
    image = Image.new('RGB', (WIDTH * scale, HEIGHT * scale), INK)
    draw = ImageDraw.Draw(image)
    fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
    for item in card_layout(review):
        if isinstance(item, Rect):
            draw.rounded_rectangle((round(item.x * scale), round(item.y * scale),
                                    round((item.x + item.width) * scale), round((item.y + item.height) * scale)),
                                   radius=round(item.radius * scale), fill=item.fill)
        elif isinstance(item, Line):
            draw.line((round(item.x * scale), round(item.y * scale), round(item.end_x * scale), round(item.end_y * scale)),
                      fill=item.color, width=round(item.width * scale))
        else:
            key = item.size, item.mono
            if key not in fonts:
                candidates = ['/System/Library/Fonts/Menlo.ttc' if item.mono else '/System/Library/Fonts/Helvetica.ttc',
                              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 'C:/Windows/Fonts/arial.ttf']
                for filename in candidates:
                    if Path(filename).is_file():
                        try:
                            fonts[key] = ImageFont.truetype(filename, item.size * scale)
                            break
                        except OSError:
                            continue
                else:
                    fonts[key] = ImageFont.load_default(size=item.size * scale)
            draw.text((round(item.x * scale), round(item.y * scale)), item.value, font=fonts[key], fill=item.color,
                      anchor='lt', stroke_width=1 if item.bold else 0)
    image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def export_review(review: WeeklyReview, filename: str) -> None:
    path = Path(filename)
    if path.suffix.lower() == '.svg':
        data = render_svg(review)
    elif path.suffix.lower() == '.png':
        data = render_png(review)
    elif path.suffix.lower() == '.json':
        data = review.to_json().encode('utf-8')
    else:
        raise ValueError(_t('review.error.extension'))
    path.write_bytes(data)

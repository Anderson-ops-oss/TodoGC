"""Weekly reviews computed from immutable events, never from current completion flags."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from todogc.heap import TouchKind
from todogc.i18n import t as _t

if TYPE_CHECKING:
    from todogc.store import Store


@dataclass(frozen=True)
class ReviewWindow:
    monday: date
    start: datetime
    end: datetime
    cutoff: datetime
    timezone_name: str

    def local(self, value: datetime) -> datetime:
        return value.astimezone(ZoneInfo(self.timezone_name)) if self.timezone_name != "local" else value.astimezone()


def week_window(week: str = "this", *, timezone_name: str = "local",
                now: datetime | None = None) -> ReviewWindow:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        zone = None if timezone_name == "local" else ZoneInfo(timezone_name)
        today = now.astimezone(zone).date()
        anchor = today if week in {"this", "last"} else date.fromisoformat(week)
        monday = anchor - timedelta(days=anchor.weekday() + (7 if week == "last" else 0))
        # Resolve both local midnights separately: a DST week need not be 168 hours.
        start = datetime.combine(monday, time(), tzinfo=zone).astimezone(timezone.utc)
        end = datetime.combine(monday + timedelta(days=7), time(), tzinfo=zone).astimezone(timezone.utc)
    except (ValueError, ZoneInfoNotFoundError, OverflowError) as exc:
        raise ValueError(_t("review.error.week")) from exc
    if start > now:
        raise ValueError(_t("review.error.future"))
    return ReviewWindow(monday, start, end, min(end, now), timezone_name)


@dataclass(frozen=True)
class Completion:
    task_id: int
    title: str
    is_goal: bool
    completed_at: str


@dataclass(frozen=True)
class WeeklyReview:
    week_start: str
    week_end: str
    timezone_name: str
    period_start: str
    period_end: str
    through: str
    generated_at: str
    through_local: str
    generated_local: str
    partial: bool
    completions: tuple[Completion, ...]
    daily: tuple[int, ...]
    reopened: int
    captured: int
    organized: int
    reclaimed: int
    discarded: int
    active_now: int
    snoozed_now: int
    inbox_now: int
    highlight_ids: tuple[int, ...] = ()
    legacy_labels: bool = False

    @property
    def tasks_completed(self) -> int:
        return sum(not item.is_goal for item in self.completions)

    @property
    def goals_completed(self) -> int:
        return sum(item.is_goal for item in self.completions)

    @property
    def highlights(self) -> tuple[Completion, ...]:
        items = {item.task_id: item for item in self.completions}
        return tuple(items[tid] for tid in self.highlight_ids)

    def with_highlights(self, task_ids: list[int] | tuple[int, ...]) -> WeeklyReview:
        ids = tuple(dict.fromkeys(task_ids))
        if len(ids) > 3 or not set(ids) <= {item.task_id for item in self.completions}:
            raise ValueError(_t("review.error.highlights"))
        return replace(self, highlight_ids=ids)

    def to_json(self) -> str:
        return json.dumps({"format_version": 1, **asdict(self)}, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, payload: str) -> WeeklyReview:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or data.pop("format_version", None) != 1:
                raise ValueError
            data["completions"] = tuple(Completion(**item) for item in data["completions"])
            data["daily"] = tuple(data["daily"])
            data["highlight_ids"] = tuple(data["highlight_ids"])
            result = cls(**data)
            if len(result.daily) != 7:
                raise ValueError
            return result.with_highlights(result.highlight_ids)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(_t("review.error.snapshot")) from exc


def build_review(store: Store, week: str = "this", *, timezone_name: str = "local",
                 now: datetime | None = None, through: datetime | None = None) -> WeeklyReview:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window = week_window(week, timezone_name=timezone_name, now=now)
    if through is not None:
        through = through.astimezone(timezone.utc)
        if not window.start <= through <= window.cutoff:
            raise ValueError(_t("review.error.cutoff"))
        window = replace(window, cutoff=through)
    events = store.review_events(window.start, window.cutoff)
    done: dict[int, Completion] = {}
    daily = [0] * 7
    legacy = False
    for event in events:
        if event.kind in {TouchKind.DONE, TouchKind.COLLECT}:
            legacy |= not event.historical_label
        if event.kind is TouchKind.DONE and event.task_id not in done:
            done[event.task_id] = Completion(event.task_id, event.title, event.is_goal, event.ts.isoformat())
            daily[(window.local(event.ts).date() - window.monday).days] += 1
    def count(kind: TouchKind) -> int:
        return len({event.task_id for event in events if event.kind is kind})
    discarded = {event.task_id for event in events if event.kind is TouchKind.COLLECT and event.in_inbox}
    active = [task for task in store.list_tasks(record_view=False) if task.state.is_active]
    return WeeklyReview(
        week_start=window.monday.isoformat(), week_end=(window.monday + timedelta(days=6)).isoformat(),
        timezone_name=window.timezone_name, period_start=window.start.isoformat(), period_end=window.end.isoformat(),
        through=window.cutoff.isoformat(), generated_at=now.isoformat(), partial=window.cutoff < window.end,
        through_local=window.local(window.cutoff).isoformat(timespec="minutes"),
        generated_local=window.local(now).isoformat(timespec="minutes"),
        completions=tuple(done.values()), daily=tuple(daily), reopened=count(TouchKind.REOPEN),
        captured=count(TouchKind.CAPTURE), organized=count(TouchKind.ORGANIZE),
        reclaimed=len(store.review_collected_ids(window.start, window.cutoff)), discarded=len(discarded),
        active_now=sum(not task.in_inbox and not task.is_snoozed(now) for task in active),
        snoozed_now=sum(task.is_snoozed(now) for task in active),
        inbox_now=sum(task.in_inbox and not task.is_snoozed(now) for task in active), legacy_labels=legacy,
    )


def format_review(review: WeeklyReview, *, lang: str | None = None) -> str:
    lines = [_t("review.title", lang=lang), f"{review.week_start} — {review.week_end} ({review.timezone_name})",
             _t("review.through", lang=lang, time=review.through_local), "",
             _t("review.completed", lang=lang, tasks=review.tasks_completed, goals=review.goals_completed,
                reopened=review.reopened)]
    for index, value in enumerate(review.daily):
        day = date.fromisoformat(review.week_start) + timedelta(days=index)
        lines.append(f"  {day.isoformat()}  {value:>3}  " + "█" * min(value, 40))
    lines += ["", _t("review.ideas", lang=lang, captured=review.captured, organized=review.organized),
              _t("review.released", lang=lang, reclaimed=review.reclaimed, discarded=review.discarded),
              _t("review.inventory", lang=lang, active=review.active_now, snoozed=review.snoozed_now, inbox=review.inbox_now),
              _t("review.inventory_time", lang=lang, time=review.generated_local), "", _t("review.done_list", lang=lang)]
    for item in review.completions:
        lines.append(f"  {'★' if item.is_goal else '✓'} #{item.task_id} {item.title}")
    if not review.completions:
        lines.append(_t("review.empty", lang=lang))
    if review.legacy_labels:
        lines.append(_t("review.legacy", lang=lang))
    return "\n".join(lines)

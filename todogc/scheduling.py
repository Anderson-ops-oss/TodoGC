"""Time input shared by the CLI and Web UI; stored schedules always use UTC."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from todogc.i18n import t as _t


def parse_snooze_time(*, duration: str | None = None, until: str | None = None,
                      now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if (duration is None) == (until is None):
        raise ValueError(_t("snooze.error.choose_time"))
    try:
        if duration is not None:
            match = re.fullmatch(r"\s*([0-9]+)\s*([mhdw])\s*", duration.lower())
            if match is None:
                raise ValueError(_t("snooze.error.duration"))
            count = int(match[1])
            seconds = count * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
            wake = now + timedelta(seconds=seconds)
        else:
            assert until is not None
            # astimezone uses the local offset at the requested date, including DST.
            wake = datetime.fromisoformat(until.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(_t("snooze.error.time")) from exc
    if wake <= now:
        raise ValueError(_t("snooze.error.future"))
    return wake.astimezone(timezone.utc)


def local_time(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="minutes")


def latest_weekly_due(now: datetime, *, weekday: int = 4, clock: str = "17:00",
                      timezone_name: str = "local") -> datetime:
    """Return the most recent weekly wall-clock occurrence, including offline catch-up."""
    zone = None if timezone_name == "local" else ZoneInfo(timezone_name)
    local = now.astimezone(zone)
    day = local.date() - timedelta(days=(local.weekday() - weekday) % 7)
    when = time.fromisoformat(clock)
    due = datetime.combine(day, when, tzinfo=zone).astimezone(timezone.utc)
    if due > now:
        # Rebuild local time for the earlier date; subtracting 168 UTC hours breaks DST.
        due = datetime.combine(day - timedelta(days=7), when, tzinfo=zone).astimezone(timezone.utc)
    return due

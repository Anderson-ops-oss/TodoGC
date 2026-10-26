"""TodoGC configuration: reads the project-level ``todogc.toml`` with zero dependencies, and
merges in environment variable overrides. Python 3.11+ uses the standard library's tomllib;
Python 3.10 uses a strict fallback parser that only covers this project's own keys, keeping
the core dependency-free either way.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from todogc.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES
from todogc.i18n import t as _t

try:  # Python 3.11+
    import tomllib  # pyright: ignore[reportMissingImports] - pyrightconfig.json pins 3.10, which doesn't have this module anyway
except ImportError:  # pragma: no cover - covered by the 3.10 job in CI
    tomllib = None


class ConfigError(ValueError):
    """Raised when the config file or an environment variable is invalid."""


@dataclass(frozen=True)
class AppConfig:
    """Runtime configuration shared by the CLI and the Web UI."""

    heap_max: int = 50
    gc_mode: str = "normal"
    recent_days: int = 30
    max_consecutive_skips: int = 5
    restore_grace_days: int = 7

    # [i18n]: default language at CLI startup, and the language the Watcher pushes in. The
    # Web UI has its own per-session language (see webapp.py's session_state) and doesn't
    # read this field directly — this is only where that per-session default comes from.
    lang: str = DEFAULT_LOCALE

    # [notify]: the ntfy push target for the scheduled checkup (todogc watch); when
    # ntfy_enabled is False, watch still computes but never pushes.
    ntfy_enabled: bool = False
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str | None = None

    # [watch]: cadence of the scheduled checkup (seconds/days), matching the design's
    # "event-driven + heartbeat fallback + debounce" model.
    watch_poll_seconds: int = 60  # poll every minute
    watch_digest_seconds: int = 86400  # digest every 1 day
    watch_remind_days: int = 7
    watch_heap_threshold_pct: int = 80
    watch_lock_stale_seconds: int = 300
    watch_weekly_review_enabled: bool = True
    watch_weekly_review_day: int = 4  # Monday = 0, Friday = 4
    watch_weekly_review_time: str = "17:00"
    watch_weekly_review_timezone: str = "local"


def _scalar(raw: str) -> object:
    """Parse the string/int/bool scalar types the fallback TOML parser needs."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return raw[1:-1]
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(_t("config.error.unsupported_value", value=raw)) from exc


def _fallback_toml(text: str) -> dict[str, dict[str, object]]:
    """Parse the simple ``[section] key=value`` TOML subset this project actually uses."""
    out: dict[str, dict[str, object]] = {}
    section: str | None = None
    for lineno, original in enumerate(text.splitlines(), 1):
        line = original.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section:
                raise ConfigError(_t("config.error.empty_section", lineno=lineno))
            out.setdefault(section, {})
            continue
        if section is None or "=" not in line:
            raise ConfigError(_t("config.error.invalid_format", lineno=lineno))
        key, raw = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(_t("config.error.empty_key", lineno=lineno))
        out[section][key] = _scalar(raw)
    return out


def _positive(value: Any, name: str) -> int:
    """Validate a config value as a positive integer; bool is not accepted as int."""
    if isinstance(value, bool):
        raise ConfigError(_t("config.error.must_be_positive_int", name=name))
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(_t("config.error.must_be_positive_int", name=name)) from exc
    if number <= 0:
        raise ConfigError(_t("config.error.must_be_positive", name=name))
    return number


def _flag(value: Any, name: str) -> bool:
    """Normalize a config value to bool — real TOML gives a native bool, an env var gives a string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "1", "yes", "on"}:
            return True
        if low in {"false", "0", "no", "off"}:
            return False
    raise ConfigError(_t("config.error.must_be_bool", name=name))


def _percent(value: Any, name: str) -> int:
    """Validate a config value as a percentage integer between 1 and 100."""
    number = _positive(value, name)
    if number > 100:
        raise ConfigError(_t("config.error.must_be_percent", name=name))
    return number


def _weekday(value: Any) -> int:
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    if not isinstance(value, str) or value.strip().lower() not in days:
        raise ConfigError(_t("config.error.weekly_day"))
    return days.index(value.strip().lower())


def _clock_time(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value.strip()):
        raise ConfigError(_t("config.error.weekly_time"))
    return value.strip()


def _review_timezone(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError(_t("config.error.weekly_timezone"))
    name = value.strip()
    try:
        if name != "local":
            ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ConfigError(_t("config.error.weekly_timezone")) from exc
    return name


def load_config(path: str | Path | None = None, *, environ: dict[str, str] | None = None) -> AppConfig:
    """Read the config file and apply environment variable overrides.

    Paths use the same explicit/environment/profile/bound/shared defaults as the
    CLI and skill installer. Environment settings override file values; CLI wins last.
    """
    env = os.environ if environ is None else environ
    from todogc.paths import resolve_config

    config_path = Path(resolve_config(path, environ=env))
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw = config_path.read_bytes()
            data = tomllib.loads(raw.decode("utf-8")) if tomllib is not None else _fallback_toml(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(_t("config.error.read_failed", path=config_path, error=exc)) from exc

    gc = data.get("gc", {})
    heap = data.get("heap", {})
    i18n_section = data.get("i18n", {})
    notify = data.get("notify", {})
    watch = data.get("watch", {})
    if not isinstance(gc, dict) or not isinstance(heap, dict):
        raise ConfigError(_t("config.error.gc_heap_sections"))
    if not isinstance(i18n_section, dict):
        raise ConfigError(_t("config.error.i18n_section"))
    if not isinstance(notify, dict) or not isinstance(watch, dict):
        raise ConfigError(_t("config.error.notify_watch_sections"))

    mode = env.get("TODOGC_GC_MODE", gc.get("mode", "normal"))
    if mode not in {"gentle", "normal", "aggressive"}:
        raise ConfigError(_t("config.error.invalid_gc_mode"))

    lang = str(env.get("TODOGC_LANG", i18n_section.get("lang", DEFAULT_LOCALE)))
    if lang not in SUPPORTED_LOCALES:
        raise ConfigError(_t("config.error.invalid_lang", langs="/".join(SUPPORTED_LOCALES)))

    ntfy_enabled = _flag(env.get("TODOGC_NTFY_ENABLED", notify.get("ntfy_enabled", False)), "notify.ntfy_enabled")
    ntfy_topic = str(env.get("TODOGC_NTFY_TOPIC", notify.get("ntfy_topic", "")))
    if ntfy_enabled and not ntfy_topic:
        raise ConfigError(_t("config.error.ntfy_topic_required"))
    ntfy_token_raw = env.get("TODOGC_NTFY_TOKEN", notify.get("ntfy_token")) or None
    ntfy_token = str(ntfy_token_raw) if ntfy_token_raw else None

    return AppConfig(
        heap_max=_positive(env.get("TODOGC_HEAP_MAX", heap.get("max", 50)), "heap.max"),
        gc_mode=str(mode),
        recent_days=_positive(env.get("TODOGC_RECENT_DAYS", gc.get("recent_days", 30)), "gc.recent_days"),
        max_consecutive_skips=_positive(
            env.get("TODOGC_MAX_SKIPS", gc.get("max_consecutive_skips", 5)),
            "gc.max_consecutive_skips",
        ),
        restore_grace_days=_positive(
            env.get("TODOGC_RESTORE_GRACE_DAYS", gc.get("restore_grace_days", 7)),
            "gc.restore_grace_days",
        ),
        lang=lang,
        ntfy_enabled=ntfy_enabled,
        ntfy_server=str(env.get("TODOGC_NTFY_SERVER", notify.get("ntfy_server", "https://ntfy.sh"))),
        ntfy_topic=ntfy_topic,
        ntfy_token=ntfy_token,
        watch_poll_seconds=_positive(
            env.get("TODOGC_WATCH_POLL_SECONDS", watch.get("poll_seconds", 60)), "watch.poll_seconds"
        ),
        watch_digest_seconds=_positive(
            env.get("TODOGC_WATCH_DIGEST_SECONDS", watch.get("digest_seconds", 86400)), "watch.digest_seconds"
        ),
        watch_remind_days=_positive(
            env.get("TODOGC_WATCH_REMIND_DAYS", watch.get("remind_days", 7)), "watch.remind_days"
        ),
        watch_heap_threshold_pct=_percent(
            env.get("TODOGC_WATCH_HEAP_THRESHOLD_PCT", watch.get("heap_threshold_pct", 80)),
            "watch.heap_threshold_pct",
        ),
        watch_lock_stale_seconds=_positive(
            env.get("TODOGC_WATCH_LOCK_STALE_SECONDS", watch.get("lock_stale_seconds", 300)),
            "watch.lock_stale_seconds",
        ),
        watch_weekly_review_enabled=_flag(
            env.get("TODOGC_WATCH_WEEKLY_REVIEW_ENABLED", watch.get("weekly_review_enabled", True)),
            "watch.weekly_review_enabled",
        ),
        watch_weekly_review_day=_weekday(
            env.get("TODOGC_WATCH_WEEKLY_REVIEW_DAY", watch.get("weekly_review_day", "friday")),
        ),
        watch_weekly_review_time=_clock_time(
            env.get("TODOGC_WATCH_WEEKLY_REVIEW_TIME", watch.get("weekly_review_time", "17:00")),
        ),
        watch_weekly_review_timezone=_review_timezone(
            env.get("TODOGC_WATCH_WEEKLY_REVIEW_TIMEZONE", watch.get("weekly_review_timezone", "local")),
        ),
    )

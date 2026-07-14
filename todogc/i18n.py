# i18n.py — a lightweight message catalog + locale resolver, standing in for the gettext
# toolchain (zero build step, the dict is the code).
#
# Each of the three call sites owns its own notion of "current language"; this module makes
# no assumption about which:
#   - CLI: single process, single language — set_locale() runs once at startup, and every
#     t() call afterwards falls back on that global default.
#   - Watcher (background thread): AppConfig.lang is passed to the Watcher explicitly, and
#     every call is t(..., lang=cfg.lang) — it never relies on the global here, since a
#     threading.Thread doesn't inherit globals set by the thread that spawned it.
#   - Web (one language per browser session): on every rerun, lang is read from
#     st.session_state and passed explicitly — same reasoning, so sessions can't bleed
#     their language setting into each other.

from __future__ import annotations

from typing import Any

from todogc.locales import en as _en
from todogc.locales import zh as _zh

SUPPORTED_LOCALES = ("en", "zh")
DEFAULT_LOCALE = "en"

_CATALOGS: dict[str, dict[str, str]] = {
    "en": _en.MESSAGES,
    "zh": _zh.MESSAGES,
}

_current_locale = DEFAULT_LOCALE


def set_locale(locale: str) -> None:
    """Set the global default language, for the CLI's use case."""
    global _current_locale
    if locale not in _CATALOGS:
        raise ValueError(f"unsupported locale: {locale!r}, expected one of {SUPPORTED_LOCALES}")
    _current_locale = locale


def get_locale() -> str:
    """Get the global default language, for the CLI's use case."""
    return _current_locale


def t(key: str, *, lang: str | None = None, **kwargs: Any) -> str:
    """Look up the message for `key` and interpolate `kwargs` into it.

    Lookup order: lang (if given) or the current global language -> that language's catalog
    -> falls back to the English catalog if missing -> falls back to the key itself if still
    missing (never raises, so a key that hasn't been translated yet doesn't break the run).
    """
    locale = lang if lang is not None else _current_locale
    catalog = _CATALOGS.get(locale, _CATALOGS[DEFAULT_LOCALE])
    template = catalog.get(key)
    if template is None:
        template = _CATALOGS[DEFAULT_LOCALE].get(key, key)
    return template.format(**kwargs) if kwargs else template


def n_(count: int, one_key: str, other_key: str, *, lang: str | None = None, **kwargs: Any) -> str:
    """Pick singular/plural for English (the two Chinese keys are usually the same string, since Chinese doesn't inflect for number)."""
    key = one_key if count == 1 else other_key
    return t(key, lang=lang, count=count, **kwargs)

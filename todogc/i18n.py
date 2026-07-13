# i18n.py —— 轻量文案目录 + 语言解析，替代 gettext 工具链(零构建步骤，字典即代码)。
#
# 三种调用场景各自管理"当前语言"，这个模块本身不做场景假设：
#   - CLI：单进程单语言，启动时调用一次 set_locale()，后续 t() 都吃全局默认值。
#   - Watcher(后台线程)：AppConfig.lang 显式传给 Watcher，每次 t(..., lang=cfg.lang) 显式传参，
#     不依赖这里的全局状态(threading.Thread 不会继承调用线程设置的全局值)。
#   - Web(每个浏览器 session 一个语言)：每次 rerun 从 st.session_state 读出 lang，显式传参，
#     同样不吃全局状态，避免多 session 互相污染。

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
    """设置 CLI 场景下的全局默认语言。"""
    global _current_locale
    if locale not in _CATALOGS:
        raise ValueError(f"unsupported locale: {locale!r}, expected one of {SUPPORTED_LOCALES}")
    _current_locale = locale


def get_locale() -> str:
    """读取 CLI 场景下的全局默认语言。"""
    return _current_locale


def t(key: str, *, lang: str | None = None, **kwargs: Any) -> str:
    """按 key 取文案并用 kwargs 插值。

    查找顺序：lang(显式传入) 或当前全局语言 -> 该语言 catalog -> 缺失则退回英文 catalog ->
    仍缺失则返回 key 本身(不炸，方便新 key 还没来得及翻译时也能跑)。
    """
    locale = lang if lang is not None else _current_locale
    catalog = _CATALOGS.get(locale, _CATALOGS[DEFAULT_LOCALE])
    template = catalog.get(key)
    if template is None:
        template = _CATALOGS[DEFAULT_LOCALE].get(key, key)
    return template.format(**kwargs) if kwargs else template


def n_(count: int, one_key: str, other_key: str, *, lang: str | None = None, **kwargs: Any) -> str:
    """英文单复数选择(中文两个 key 通常写成同一句，中文本身不做数量形态变化)。"""
    key = one_key if count == 1 else other_key
    return t(key, lang=lang, count=count, **kwargs)

from __future__ import annotations

import re

import pytest

from todogc import i18n
from todogc.locales import en as en_locale
from todogc.locales import zh as zh_locale

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


@pytest.fixture
def sample_catalogs(monkeypatch):
    monkeypatch.setitem(
        i18n._CATALOGS,
        "en",
        {"greet": "Hello, {name}!", "count.one": "{count} day", "count.other": "{count} days"},
    )
    monkeypatch.setitem(
        i18n._CATALOGS,
        "zh",
        {"greet": "你好，{name}！", "count.one": "{count} 天", "count.other": "{count} 天"},
    )


def test_t_uses_current_global_locale(sample_catalogs):
    i18n.set_locale("zh")
    assert i18n.t("greet", name="安") == "你好，安！"


def test_t_explicit_lang_overrides_global_locale(sample_catalogs):
    i18n.set_locale("en")
    assert i18n.t("greet", lang="zh", name="安") == "你好，安！"


def test_t_falls_back_to_english_when_key_missing_in_locale(sample_catalogs, monkeypatch):
    monkeypatch.setitem(i18n._CATALOGS, "zh", {})
    assert i18n.t("greet", lang="zh", name="安") == "Hello, 安!"


def test_t_returns_key_itself_when_missing_everywhere(sample_catalogs):
    assert i18n.t("no.such.key") == "no.such.key"


def test_t_without_kwargs_returns_template_unformatted(sample_catalogs):
    assert i18n.t("greet", lang="en") == "Hello, {name}!"


def test_set_locale_rejects_unsupported_locale():
    with pytest.raises(ValueError):
        i18n.set_locale("fr")
    assert i18n.get_locale() == i18n.DEFAULT_LOCALE


def test_get_locale_reflects_set_locale():
    i18n.set_locale("zh")
    assert i18n.get_locale() == "zh"


def test_n__picks_singular_and_plural_by_count(sample_catalogs):
    assert i18n.n_(1, "count.one", "count.other") == "1 day"
    assert i18n.n_(2, "count.one", "count.other") == "2 days"
    assert i18n.n_(0, "count.one", "count.other") == "0 days"


def test_real_catalogs_have_matching_keys():
    assert set(en_locale.MESSAGES.keys()) == set(zh_locale.MESSAGES.keys())


def test_real_catalogs_have_matching_placeholders():
    for key, en_template in en_locale.MESSAGES.items():
        en_placeholders = set(_PLACEHOLDER_RE.findall(en_template))
        zh_placeholders = set(_PLACEHOLDER_RE.findall(zh_locale.MESSAGES.get(key, "")))
        assert en_placeholders == zh_placeholders, (
            f"placeholder mismatch for key {key!r}: en={en_placeholders} zh={zh_placeholders}"
        )

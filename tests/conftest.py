# conftest.py — shared pytest fixtures: a fixed clock and an in-memory Store, so time-window
# and liveness tests are reproducible.

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from todogc import i18n
from todogc.store import Store


@pytest.fixture(autouse=True)
def _isolated_user_paths(tmp_path, monkeypatch):
    """Tests must never discover or open the developer's personal task database."""
    home = tmp_path / "isolated home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    for name in ("TODOGC_DB", "TODOGC_CONFIG", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture(autouse=True)
def _reset_i18n_locale():
    """main()/set_locale() changes the global current language; reset it after every test to avoid cross-test pollution.

    Yields:
        None
    """
    yield
    i18n.set_locale(i18n.DEFAULT_LOCALE)


@pytest.fixture
def now() -> datetime:
    """A fixed UTC time base.

    Returns:
        datetime: 2026-07-10T12:00:00Z, shared by tests that need a deterministic time.
    """
    return datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    """An in-memory Store, closed automatically at the end of the test.

    Yields:
        Store: an isolated database backed by ":memory:".
    """
    s = Store(":memory:")
    yield s
    s.close()

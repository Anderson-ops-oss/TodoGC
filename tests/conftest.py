# conftest.py —— pytest 公用 fixtures：固定时钟与内存 Store，让时间窗与判活测试可复现。

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from todogc.store import Store


@pytest.fixture
def now() -> datetime:
    """一个固定的 UTC 时间基准。

    Returns:
        datetime: 2026-07-10T12:00:00Z，供需要确定时间的测试共用。
    """
    return datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    """一个内存 Store，测试结束自动关闭。

    Yields:
        Store: 基于 ":memory:" 的隔离数据库。
    """
    s = Store(":memory:")
    yield s
    s.close()

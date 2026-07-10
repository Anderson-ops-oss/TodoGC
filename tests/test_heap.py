# test_heap.py —— 领域模型：分代阈值、refcount 口径、Task 默认值与 aged() 值语义。

from __future__ import annotations

from datetime import datetime, timezone

from todogc.heap import TENURING_THRESHOLD, Generation, State, Task, Touch, TouchKind


def test_for_age_boundaries():
    """分代随年龄晋升，阈值锁定在 15(4-bit 对象头)。"""
    assert TENURING_THRESHOLD == 15
    assert Generation.for_age(0) is Generation.EDEN
    assert Generation.for_age(1) is Generation.SURVIVOR
    assert Generation.for_age(14) is Generation.SURVIVOR
    assert Generation.for_age(15) is Generation.OLD


def test_is_progress():
    """只有 PROGRESS/EDIT 计入真推进，反复 VIEW / DEFER 不算。"""
    assert TouchKind.PROGRESS.is_progress and TouchKind.EDIT.is_progress
    assert not TouchKind.VIEW.is_progress and not TouchKind.DEFER.is_progress
    assert Touch(1, TouchKind.VIEW, datetime.now(timezone.utc)).is_progress is False


def test_task_defaults_and_aged():
    """默认未关联/EDEN/LIVE；aged() 返回新对象且不改原对象。"""
    t = Task(title="学吉他")
    assert t.is_unlinked and t.gen is Generation.EDEN and t.state is State.LIVE
    t2 = t.aged()
    assert t2.age == 1 and t2.gen is Generation.SURVIVOR
    assert t.age == 0  # 值语义：原对象不被就地修改


def test_meta_not_promoted():
    """元任务不参与常规晋升。"""
    m = Task(title="变得更自律", gen=Generation.META)
    assert m.is_meta and m.aged().gen is Generation.META

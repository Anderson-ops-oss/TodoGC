# test_heap.py — the domain model: generation thresholds, refcount accounting, Task defaults, and aged()'s value semantics.

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from todogc.heap import TENURING_THRESHOLD, Generation, State, Task, Touch, TouchKind


def test_for_age_boundaries():
    """Generation is promoted by age, with the threshold locked at 15 (4-bit object header)."""
    assert TENURING_THRESHOLD == 15
    assert Generation.for_age(0) is Generation.EDEN
    assert Generation.for_age(1) is Generation.SURVIVOR
    assert Generation.for_age(14) is Generation.SURVIVOR
    assert Generation.for_age(15) is Generation.OLD


def test_is_progress():
    """Only PROGRESS/EDIT count as real progress; repeated VIEW / DEFER don't."""
    assert TouchKind.PROGRESS.is_progress and TouchKind.EDIT.is_progress
    assert not TouchKind.VIEW.is_progress and not TouchKind.DEFER.is_progress
    assert Touch(1, TouchKind.VIEW, datetime.now(timezone.utc)).is_progress is False


def test_task_defaults_and_aged():
    """Defaults to unlinked/EDEN/LIVE; aged() returns a new object and doesn't modify the original."""
    t = Task(title="学吉他")
    assert t.is_unlinked and t.gen is Generation.EDEN and t.state is State.LIVE
    with pytest.raises(ValueError, match="not been persisted"):
        _ = t.persisted_id
    t2 = t.aged()
    assert t2.age == 1 and t2.gen is Generation.SURVIVOR
    assert t.age == 0  # value semantics: the original object is never mutated in place


def test_meta_not_promoted():
    """Meta-tasks don't participate in regular promotion."""
    m = Task(title="变得更自律", gen=Generation.META)
    assert m.is_meta and m.aged().gen is Generation.META

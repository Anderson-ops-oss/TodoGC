# heap.py — the domain model: models a todo as an object on the GC heap (Task), and each
# interaction as a touch event (Touch).

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from todogc.i18n import t as _t

# The age threshold for promotion to Old generation. 15, because in the real JVM an object's
# age lives in its object header's mark word, which only has 4 bits and tops out at 15 — so
# -XX:MaxTenuringThreshold defaults to 15 too. Fifteen rounds of self-deception earns tenure.
TENURING_THRESHOLD: int = 15


class TouchKind(str, Enum):
    """The type of a touch event; liveness, the GC log, and generational age are all aggregated from these events."""

    CREATE = "create"      # the task was created (object allocation)
    VIEW = "view"          # you looked at it — logged, but doesn't count as progress
    PROGRESS = "progress"  # you actually made progress on it (counts toward refcount)
    EDIT = "edit"          # you edited its content (counts toward refcount)
    DEFER = "defer"        # you pushed it back (feeds skip_count)
    LINK = "link"          # (re-)linked to a GC root
    REVIVE = "revive"      # revived during the collection ceremony
    COLLECT = "collect"    # collected (the eulogy goes in note)
    RESTORE = "restore"    # restored from the trash
    DONE = "done"          # successfully completed
    REOPEN = "reopen"      # completed task returned to the active heap

    @property
    def is_progress(self) -> bool:
        """Whether this event counts as "real progress" (counted toward refcount).

        Returns:
            bool: True for PROGRESS / EDIT, False for everything else (including repeated
                VIEWs).
        """
        return self in _PROGRESS_KINDS


# Event types that count toward refcount: only active progress/editing counts, repeated
# viewing earns no points.
_PROGRESS_KINDS: frozenset[TouchKind] = frozenset({TouchKind.PROGRESS, TouchKind.EDIT})


class Generation(str, Enum):
    """The generation a task is in, mirroring the JVM heap's generational structure."""

    EDEN = "eden"          # a newly allocated object
    SURVIVOR = "survivor"  # a survivor of at least one GC round
    OLD = "old"            # a deadbeat promoted to Old generation (Tenured)
    META = "meta"          # Metaspace: a "meta-task" about tasks, almost never reclaimed

    @classmethod
    def for_age(cls, age: int) -> Generation:
        """Derive the regular generation from "GC rounds survived" (excludes META, which must be set explicitly).

        Args:
            age (int): the cumulative number of times this task was listed as a reclaim
                candidate and then revived.

        Returns:
            Generation: EDEN if age <= 0; SURVIVOR if 0 < age < TENURING_THRESHOLD; OLD if
                age >= TENURING_THRESHOLD.
        """
        if age <= 0:
            return cls.EDEN
        if age < TENURING_THRESHOLD:
            return cls.SURVIVOR
        return cls.OLD


class ReferenceStrength(str, Enum):
    """A task's retention strength: determines how hard a reachable object is to reclaim under the liveness check."""

    STRONG = "strong"  # reachable is enough to keep it
    SOFT = "soft"      # default: liveness by recent real activity and consecutive skips
    WEAK = "weak"      # must have real progress within the time window


class State(str, Enum):
    """A task's lifecycle state within the collection pipeline."""

    LIVE = "live"            # alive
    CANDIDATE = "candidate"  # marked as a reclaim candidate, awaiting the ceremony
    COLLECTED = "collected"  # collected, sitting in the trash (recoverable)
    DONE = "done"            # completed successfully, outside the active heap

    @property
    def is_active(self) -> bool:
        return self in {State.LIVE, State.CANDIDATE}


@dataclass
class Touch:
    """A touch event: the single source of truth for liveness, append-only and never modified.

    Attributes:
        task_id (int): id of the task it belongs to.
        kind (TouchKind): the event type.
        ts (datetime): when the event occurred (recommended to always store as UTC).
        note (str | None): attached text — progress note / snooze reason / eulogy.
        id (int | None): the primary key; None if not yet persisted.
    """

    task_id: int
    kind: TouchKind
    ts: datetime
    note: str | None = None
    id: int | None = None

    @property
    def is_progress(self) -> bool:
        """Whether this touch counts as real progress.

        Returns:
            bool: delegates to self.kind.is_progress.
        """
        return self.kind.is_progress


@dataclass
class Task:
    """A todo = an object on the GC heap.

    Attributes:
        title (str): the task title.
        root_id (int | None): id of the linked GC root (active goal); None means unlinked, a
            natural reclaim candidate.
        gen (Generation): the current generation.
        age (int): GC rounds survived, used for generation promotion.
        state (State): the lifecycle state.
        created_at (datetime | None): creation time; written by store when persisted.
        completed_at (datetime | None): current completion time; cleared on reopen, retained in history.
        id (int | None): the primary key; None if not yet persisted.
    """

    title: str
    root_id: int | None = None
    gen: Generation = Generation.EDEN
    age: int = 0
    state: State = State.LIVE
    created_at: datetime | None = None
    id: int | None = None
    ref_strength: ReferenceStrength = ReferenceStrength.SOFT
    completed_at: datetime | None = None

    @property
    def is_unlinked(self) -> bool:
        """Whether this task isn't linked to any GC root (unlinked is a natural reclaim candidate).

        Returns:
            bool: True if root_id is None.
        """
        return self.root_id is None

    @property
    def persisted_id(self) -> int:
        """Return the persisted id; fails immediately if called on an object that isn't persisted yet.

        This is the type-safety boundary between a ``Task``'s draft state and persistence
        operations: the caller always gets back an ``int``, with no need to propagate
        ``int | None`` into Store, audit, or the UI layer.
        """
        if self.id is None:
            raise ValueError(_t("heap.error.not_persisted"))
        return self.id

    @property
    def is_meta(self) -> bool:
        """Whether this is a meta-task permanently living in Metaspace.

        Returns:
            bool: True if gen is Generation.META.
        """
        return self.gen is Generation.META

    def aged(self) -> Task:
        """Produce a new task after "surviving one more GC round": age +1, with its regular generation updated accordingly.

        Meta-tasks (META) don't participate in regular promotion and get back an unchanged
        copy. Returns a new object rather than mutating in place, to preserve the dataclass's
        value semantics and keep testing and auditing easy.

        Returns:
            Task: a new Task instance with age incremented and gen promoted accordingly.
        """
        if self.is_meta:
            return replace(self)
        new_age = self.age + 1
        return replace(self, age=new_age, gen=Generation.for_age(new_age))

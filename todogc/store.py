# store.py — the persistence layer: lands Task and Touch in SQLite; automatically tracks a
# view event whenever a task is read, so the "touch history" that liveness is built on grows
# on its own.

from __future__ import annotations

import sqlite3
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from todogc.heap import SNOOZE_GRACE_DAYS, Generation, ReferenceStrength, State, Task, Touch, TouchKind
from todogc.i18n import t as _t

# Event type values counted toward refcount: reuses TouchKind.is_progress as the single
# source of truth, not redefined here.
_PROGRESS_KIND_VALUES: tuple[str, ...] = tuple(k.value for k in TouchKind if k.is_progress)

# Table-creation statements. task = heap objects; touch = the append-only touch-event stream
# (the single source of truth for liveness).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    root_id    INTEGER,
    is_root    INTEGER NOT NULL DEFAULT 0,
    gen        TEXT    NOT NULL DEFAULT 'eden',
    age        INTEGER NOT NULL DEFAULT 0,
    state      TEXT    NOT NULL DEFAULT 'live',
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS touch (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    kind    TEXT    NOT NULL,
    ts      TEXT    NOT NULL,
    note    TEXT
);

-- task->task "reason" edges: dep(task_id, ref_id) means task_id's reason/prerequisite is
-- ref_id (i.e. task_id references ref_id).
CREATE TABLE IF NOT EXISTS dep (
    task_id INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    ref_id  INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_touch_task ON touch(task_id);
"""

_SCHEMA_VERSION = 9


@dataclass(frozen=True)
class ReviewEvent:
    task_id: int
    kind: TouchKind
    ts: datetime
    title: str
    is_goal: bool
    in_inbox: bool
    historical_label: bool


@dataclass(frozen=True)
class ReviewSnapshot:
    id: int
    week_start: str
    generated_at: datetime
    payload: str


@dataclass(frozen=True)
class WatchState:
    """A task's judged status and debounce timestamp from the previous ``todogc watch`` tick.

    Attributes:
        task_id (int): the task id.
        status (str): the previous round's judged status ("alive" or a collector.Reason
            value).
        notified_at (datetime | None): the last time an event push was sent for this task's
            current status; None means this status hasn't been pushed since it appeared (or
            was never recorded).
    """

    task_id: int
    status: str
    notified_at: datetime | None


@dataclass(frozen=True)
class GCRun:
    """An audit record for a GC that was actually executed."""

    id: int
    started_at: datetime
    mode: str
    heap_before: int
    survivors: int
    candidates: int
    collected: int
    revived: int
    released_pct: float


class Store:
    """TodoGC's persistence facade: wraps SQLite, exposing only Task / Touch semantics and hiding the SQL details."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        """Open (or create) a TodoGC database and set up its schema.

        Args:
            path (str | Path): the database file path; defaults to ":memory:" for an
                in-memory database (test-friendly).

        Returns:
            None
        """
        self.path = str(path)
        existed = self.path != ":memory:" and Path(self.path).exists()
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema(existed=existed)

    # ---- resource management ----

    def __enter__(self) -> Store:
        """Enter the with-context.

        Returns:
            Store: self, for use as with ... as store.
        """
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Commit and close the connection when exiting the with-context.

        Args:
            exc_type (object): the exception type (None if no exception).
            exc (object): the exception instance (None if no exception).
            tb (object): the exception traceback (None if no exception).

        Returns:
            None: never swallows the exception.
        """
        self.close()

    def close(self) -> None:
        """Commit and close the underlying connection.

        Returns:
            None
        """
        self._conn.commit()
        self._conn.close()

    # ---- internal helpers ----

    def _init_schema(self, *, existed: bool = False) -> None:
        """Execute the table-creation statements (idempotent, IF NOT EXISTS).

        Returns:
            None
        """
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        version = int(row[0]) if row is not None else 1
        if version < _SCHEMA_VERSION and existed:
            self._conn.commit()
            shutil.copy2(self.path, self.path + ".bak")
        if version < 2:
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(task)").fetchall()}
            if "ref_strength" not in columns:
                self._conn.execute(
                    "ALTER TABLE task ADD COLUMN ref_strength TEXT NOT NULL DEFAULT 'soft'"
                )
            version = 2
        if version < 3:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS gc_run (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at   TEXT NOT NULL,
                    mode         TEXT NOT NULL,
                    heap_before  INTEGER NOT NULL,
                    survivors    INTEGER NOT NULL,
                    candidates   INTEGER NOT NULL,
                    collected    INTEGER NOT NULL DEFAULT 0,
                    revived      INTEGER NOT NULL DEFAULT 0,
                    released_pct REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gc_run_item (
                    run_id  INTEGER NOT NULL REFERENCES gc_run(id) ON DELETE CASCADE,
                    task_id INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
                    reason  TEXT NOT NULL,
                    action  TEXT NOT NULL,
                    PRIMARY KEY (run_id, task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_gc_run_started ON gc_run(started_at);
                """
            )
            version = 3
        if version < 4:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watch_state (
                    task_id     INTEGER PRIMARY KEY REFERENCES task(id) ON DELETE CASCADE,
                    status      TEXT NOT NULL,
                    notified_at TEXT
                );
                CREATE TABLE IF NOT EXISTS watch_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            version = 4
        if version < 5:
            self._conn.execute("ALTER TABLE task ADD COLUMN completed_at TEXT")
            version = 5
        if version < 6:
            self._conn.execute("ALTER TABLE task ADD COLUMN inbox_since TEXT")
            version = 6
        if version < 7:
            self._conn.execute("ALTER TABLE task ADD COLUMN snoozed_until TEXT")
            self._conn.execute("ALTER TABLE task ADD COLUMN snooze_notified_until TEXT")
            version = 7
        if version < 8:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS touch_task_snapshot (
                    touch_id INTEGER PRIMARY KEY REFERENCES touch(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    is_goal INTEGER NOT NULL,
                    in_inbox INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_touch_ts ON touch(ts);
                CREATE TRIGGER IF NOT EXISTS snapshot_new_touch AFTER INSERT ON touch
                WHEN NEW.kind IN ('done', 'reopen', 'capture', 'organize', 'collect') BEGIN
                    INSERT INTO touch_task_snapshot(touch_id, title, is_goal, in_inbox)
                    SELECT NEW.id, title, is_root, inbox_since IS NOT NULL
                    FROM task WHERE id = NEW.task_id;
                END;
                CREATE TABLE IF NOT EXISTS review_snapshot (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )
            version = 8
        if version < 9:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_focus (
                    day TEXT NOT NULL,
                    task_id INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    removed INTEGER NOT NULL DEFAULT 0 CHECK(removed IN (0, 1)),
                    PRIMARY KEY(day, task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_focus_task ON daily_focus(task_id, day);
                """
            )
            version = 9
        self._conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version),),
        )
        self._conn.commit()

    @property
    def schema_version(self) -> int:
        """The current database schema version."""
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _now() -> datetime:
        """Get the current UTC time.

        Returns:
            datetime: the current time, timezone-aware (UTC).
        """
        return datetime.now(timezone.utc)

    @staticmethod
    def _to_iso(dt: datetime) -> str:
        """Normalize a datetime to UTC and convert it to an ISO 8601 string (guarantees times can be compared lexicographically).

        Args:
            dt (datetime): the time to convert; a naive (tz-less) time is treated as UTC.

        Returns:
            str: a string like '2026-07-10T12:00:00+00:00'.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _from_iso(s: str) -> datetime:
        """Parse an ISO 8601 string back into a timezone-aware datetime.

        Args:
            s (str): a time string produced by _to_iso.

        Returns:
            datetime: a timezone-aware (UTC) time.
        """
        return datetime.fromisoformat(s)

    @staticmethod
    def _lastrowid(cur: sqlite3.Cursor) -> int:
        """Get the INSERT's primary key; raises a clear error if the driver didn't return one."""
        row_id = cur.lastrowid
        if row_id is None:
            raise RuntimeError(_t("store.error.insert_no_rowid"))
        return row_id

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        """Map a row from the task table to a Task domain object.

        Args:
            row (sqlite3.Row): one row from a task-table query result.

        Returns:
            Task: the reconstructed task object.
        """
        return Task(
            id=row["id"],
            title=row["title"],
            root_id=row["root_id"],
            ref_strength=ReferenceStrength(row["ref_strength"]),
            gen=Generation(row["gen"]),
            age=row["age"],
            state=State(row["state"]),
            created_at=Store._from_iso(row["created_at"]),
            completed_at=Store._from_iso(row["completed_at"]) if row["completed_at"] else None,
            inbox_since=Store._from_iso(row["inbox_since"]) if row["inbox_since"] else None,
            snoozed_until=Store._from_iso(row["snoozed_until"]) if row["snoozed_until"] else None,
        )

    @staticmethod
    def _row_to_touch(row: sqlite3.Row) -> Touch:
        """Map a row from the touch table to a Touch domain object.

        Args:
            row (sqlite3.Row): one row from a touch-table query result.

        Returns:
            Touch: the reconstructed touch-event object.
        """
        return Touch(
            id=row["id"],
            task_id=row["task_id"],
            kind=TouchKind(row["kind"]),
            ts=Store._from_iso(row["ts"]),
            note=row["note"],
        )

    # ---- writes ----

    def add_task(
        self,
        title: str,
        *,
        root_id: int | None = None,
        is_root: bool = False,
        meta: bool = False,
        ref_strength: ReferenceStrength | str = ReferenceStrength.SOFT,
        inbox: bool = False,
        ts: datetime | None = None,
    ) -> Task:
        """Add (allocate) a new task, and log a CREATE touch event.

        Args:
            title (str): the task title.
            root_id (int | None): id of the parent (goal/task) to link to; None means
                unlinked (a natural candidate).
            is_root (bool): whether this is itself an active GC root (a declared goal);
                rootness is queried afterward via root_ids().
            meta (bool): whether this is a meta-task that permanently lives in Metaspace
                (e.g. "be more disciplined").
            ts (datetime | None): the creation time; None uses the current UTC time.

        Returns:
            Task: the persisted task object, with an auto-incremented id and created_at.
        """
        title = title.strip()
        if not title:
            raise ValueError(_t("store.error.empty_title"))
        if inbox and (root_id is not None or is_root or meta):
            raise ValueError(_t("inbox.error.standalone"))
        now = ts or self._now()
        strength = ReferenceStrength(ref_strength)
        if root_id is not None:
            self._require_parent_task(root_id)
        iso = self._to_iso(now)
        gen = Generation.META if meta else Generation.EDEN
        cur = self._conn.execute(
            "INSERT INTO task (title, root_id, is_root, gen, age, state, created_at, ref_strength, inbox_since) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (title, root_id, int(is_root), gen.value, State.LIVE.value, iso, strength.value, iso if inbox else None),
        )
        task_id = self._lastrowid(cur)
        self.record(task_id, TouchKind.CAPTURE if inbox else TouchKind.CREATE, ts=now)
        self._conn.commit()
        return Task(
            id=task_id,
            title=title,
            root_id=root_id,
            ref_strength=strength,
            gen=gen,
            age=0,
            state=State.LIVE,
            created_at=self._from_iso(iso),
            inbox_since=self._from_iso(iso) if inbox else None,
        )

    def capture(self, title: str, *, heap_max: int = 50, force: bool = False, ts: datetime | None = None) -> Task:
        if not force and self.heap_size() >= heap_max:
            raise ValueError(_t("inbox.error.full", max=heap_max))
        return self.add_task(title, inbox=True, ts=ts)

    def move_to_inbox(self, task_id: int, *, ts: datetime | None = None) -> None:
        """Only explicit, standalone unlinked tasks can enter the Inbox; never migrate implicitly."""
        with self._conn:
            task = self._require_live_task(task_id)
            if task.in_inbox:
                raise ValueError(_t("inbox.error.already_inbox", id=task_id))
            if task.root_id is not None or task.is_meta or task_id in self.root_ids() or self.descendants(task_id):
                raise ValueError(_t("inbox.error.standalone"))
            iso = self._to_iso(ts or self._now())
            self._conn.execute("UPDATE task SET inbox_since = ? WHERE id = ?", (iso, task_id))
            self._conn.execute(
                "INSERT INTO touch(task_id, kind, ts) VALUES (?, ?, ?)", (task_id, TouchKind.INBOX.value, iso),
            )
            self._conn.execute("DELETE FROM watch_state WHERE task_id = ?", (task_id,))

    def discard_inbox(self, task_id: int, *, note: str | None = None, ts: datetime | None = None) -> None:
        with self._conn:
            task = self._require_live_task(task_id)
            if not task.in_inbox:
                raise ValueError(_t("inbox.error.not_inbox", id=task_id))
            iso = self._to_iso(ts or self._now())
            self._conn.execute(
                "UPDATE task SET state = ?, snoozed_until = NULL, snooze_notified_until = NULL WHERE id = ?",
                (State.COLLECTED.value, task_id),
            )
            self._conn.execute(
                "INSERT INTO touch(task_id, kind, ts, note) VALUES (?, ?, ?, ?)",
                (task_id, TouchKind.COLLECT.value, iso, note or _t("inbox.discard.default")),
            )
            self._conn.execute("DELETE FROM watch_state WHERE task_id = ?", (task_id,))

    def collect_candidate(self, task_id: int, *, note: str, ts: datetime) -> bool:
        """A stale interactive GC snapshot must not overwrite completion or Inbox triage."""
        with self._conn:
            changed = self._conn.execute(
                "UPDATE task SET state = ? WHERE id = ? AND state IN (?, ?) AND inbox_since IS NULL "
                "AND (snoozed_until IS NULL OR snoozed_until <= ?) "
                "AND NOT EXISTS (SELECT 1 FROM touch WHERE touch.task_id = task.id AND kind = ? AND ts > ?)",
                (State.COLLECTED.value, task_id, State.LIVE.value, State.CANDIDATE.value,
                 self._to_iso(ts - timedelta(days=SNOOZE_GRACE_DAYS)), TouchKind.UNSNOOZE.value,
                 self._to_iso(ts - timedelta(days=SNOOZE_GRACE_DAYS))),
            )
            if changed.rowcount == 0:
                return False
            self._conn.execute(
                "INSERT INTO touch(task_id, kind, ts, note) VALUES (?, ?, ?, ?)",
                (task_id, TouchKind.COLLECT.value, self._to_iso(ts), note),
            )
        return True

    def record(
        self,
        task_id: int,
        kind: TouchKind,
        *,
        note: str | None = None,
        ts: datetime | None = None,
    ) -> Touch:
        """Append a touch event (the single low-level event write path; append-only, never modified).

        Args:
            task_id (int): the target task id.
            kind (TouchKind): the event type.
            note (str | None): attached text (progress note / snooze reason / eulogy).
            ts (datetime | None): the event time; None uses the current UTC time.

        Returns:
            Touch: the persisted touch-event object, with an auto-incremented id.
        """
        now = ts or self._now()
        iso = self._to_iso(now)
        cur = self._conn.execute(
            "INSERT INTO touch (task_id, kind, ts, note) VALUES (?, ?, ?, ?)",
            (task_id, kind.value, iso, note),
        )
        self._conn.commit()
        return Touch(
            id=self._lastrowid(cur), task_id=task_id, kind=kind,
            ts=self._from_iso(iso), note=note,
        )

    def progress(self, task_id: int, note: str | None = None, ts: datetime | None = None) -> Touch:
        """Mark "I actually made progress on this task" — logs a PROGRESS event (counted toward refcount).

        Args:
            task_id (int): the target task id.
            note (str | None): the progress note.
            ts (datetime | None): the event time; None uses the current UTC time.

        Returns:
            Touch: the newly logged touch event.
        """
        self._require_live_task(task_id)
        return self.record(task_id, TouchKind.PROGRESS, note=note, ts=ts)

    def defer(self, task_id: int, note: str | None = None, ts: datetime | None = None) -> Touch:
        """Defer (snooze) a task — logs a DEFER event (feeds skip_count, hard evidence of self-deception).

        Args:
            task_id (int): the target task id.
            note (str | None): the reason for deferring.
            ts (datetime | None): the event time; None uses the current UTC time.

        Returns:
            Touch: the newly logged touch event.
        """
        self._require_live_task(task_id)
        return self.record(task_id, TouchKind.DEFER, note=note, ts=ts)

    def snooze_task(self, task_id: int, until: datetime, *, with_subtasks: bool = False,
                    note: str | None = None, ts: datetime | None = None) -> list[int]:
        now = ts or self._now()
        if until.tzinfo is None or until <= now:
            raise ValueError(_t("snooze.error.future"))
        with self._conn:
            self._require_live_task(task_id)
            ids = [task_id, *(task.persisted_id for task in self.active_descendants(task_id))] if with_subtasks else [task_id]
            for tid in ids:
                self._conn.execute(
                    "UPDATE task SET snoozed_until = ?, snooze_notified_until = NULL WHERE id = ?",
                    (self._to_iso(until), tid),
                )
                self._conn.execute(
                    "INSERT INTO touch(task_id, kind, ts, note) VALUES (?, ?, ?, ?)",
                    (tid, TouchKind.SNOOZE.value, self._to_iso(now), f"until={self._to_iso(until)}" + (f" · {note}" if note else "")),
                )
                self._conn.execute("DELETE FROM watch_state WHERE task_id = ?", (tid,))
        return ids

    def unsnooze_task(self, task_id: int, *, with_subtasks: bool = False,
                      ts: datetime | None = None) -> list[int]:
        now = ts or self._now()
        with self._conn:
            task = self._require_live_task(task_id)
            tasks = [task, *self.active_descendants(task_id)] if with_subtasks else [task]
            waking = [task for task in tasks if task.is_snoozed(now)]
            if not waking:
                raise ValueError(_t("snooze.error.not_snoozed", id=task_id))
            for task in waking:
                self._conn.execute(
                    "UPDATE task SET snoozed_until = NULL, snooze_notified_until = NULL WHERE id = ?", (task.id,),
                )
                self._conn.execute(
                    "INSERT INTO touch(task_id, kind, ts) VALUES (?, ?, ?)",
                    (task.id, TouchKind.UNSNOOZE.value, self._to_iso(now)),
                )
                self._conn.execute("DELETE FROM watch_state WHERE task_id = ?", (task.id,))
        return [task.persisted_id for task in waking]

    def due_snoozes(self, now: datetime) -> list[Task]:
        """Return expired schedules until their notification has been delivered successfully."""
        rows = self._conn.execute(
            "SELECT * FROM task WHERE state IN (?, ?) AND snoozed_until <= ? "
            "AND (snooze_notified_until IS NULL OR snooze_notified_until != snoozed_until) ORDER BY id",
            (State.LIVE.value, State.CANDIDATE.value, self._to_iso(now)),
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def acknowledge_snooze(self, task: Task) -> None:
        """A concurrent reschedule must never be acknowledged as the old reminder."""
        assert task.snoozed_until is not None
        iso = self._to_iso(task.snoozed_until)
        with self._conn:
            self._conn.execute(
                "UPDATE task SET snooze_notified_until = ? WHERE id = ? AND snoozed_until = ?",
                (iso, task.id, iso),
            )

    def link(self, task_id: int, root_id: int, ts: datetime | None = None) -> None:
        """(Re-)link a task to a GC root: updates root_id and logs a LINK event.

        Args:
            task_id (int): the target task id.
            root_id (int): id of the root (active goal) to link to.
            ts (datetime | None): the event time; None uses the current UTC time.

        Returns:
            None
        """
        task = self._require_live_task(task_id)
        self.validate_parent(task_id, root_id)
        self._conn.execute("UPDATE task SET root_id = ?, inbox_since = NULL WHERE id = ?", (root_id, task_id))
        self.record(task_id, TouchKind.ORGANIZE if task.in_inbox else TouchKind.LINK, note=f"parent=#{root_id}", ts=ts)
        self._conn.commit()

    def update_task(self, task: Task) -> None:
        """Write an in-memory Task's current state (title/link/generation/age/state) back to the database.

        Used by the collector to persist after marking candidates, promoting generations, or
        reviving/collecting; this method logs no touch event itself — a semantic event (like
        REVIVE/COLLECT) is recorded separately by the caller via record().

        Args:
            task (Task): a task object that must have a non-null id.

        Returns:
            None

        Raises:
            ValueError: if task.id is None (not yet persisted).
        """
        if task.id is None:
            raise ValueError(_t("store.error.update_requires_persisted"))
        self._conn.execute(
            "UPDATE task SET title = ?, root_id = ?, gen = ?, age = ?, state = ?, ref_strength = ?, completed_at = ?, inbox_since = ?, snoozed_until = ? WHERE id = ?",
            (
                task.title, task.root_id, task.gen.value, task.age, task.state.value,
                task.ref_strength.value,
                self._to_iso(task.completed_at) if task.completed_at else None,
                self._to_iso(task.inbox_since) if task.inbox_since else None,
                self._to_iso(task.snoozed_until) if task.snoozed_until else None, task.id,
            ),
        )
        self._conn.commit()

    def edit_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        ref_strength: ReferenceStrength | str | None = None,
        ts: datetime | None = None,
    ) -> Task:
        """Edit the title and/or reference strength, and log an EDIT event."""
        task = self._require_live_task(task_id)
        if title is not None:
            title = title.strip()
            if not title:
                raise ValueError(_t("store.error.empty_title"))
            task.title = title
        if ref_strength is not None:
            task.ref_strength = ReferenceStrength(ref_strength)
        self.update_task(task)
        self.record(task_id, TouchKind.EDIT, note=title, ts=ts)
        return task

    def unlink(self, task_id: int, ts: datetime | None = None) -> None:
        """Unlink the parent/target association; logs a LINK event with an audit note."""
        self._require_live_task(task_id)
        self._conn.execute("UPDATE task SET root_id = NULL WHERE id = ?", (task_id,))
        self.record(task_id, TouchKind.LINK, note="unlink", ts=ts)
        self._conn.commit()

    def _require_live_task(self, task_id: int) -> Task:
        """Fetch a live task, or raise a user-facing ValueError."""
        row = self._conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(_t("store.error.task_not_found", id=task_id))
        task = self._row_to_task(row)
        if task.state is State.COLLECTED:
            raise ValueError(_t("store.error.task_already_collected", id=task_id))
        if task.state is State.DONE:
            raise ValueError(_t("store.error.task_already_done", id=task_id))
        return task

    def _require_parent_task(self, task_id: int) -> Task:
        task = self._require_live_task(task_id)
        if task.in_inbox:
            raise ValueError(_t("inbox.error.parent", id=task_id))
        return task

    def descendants(self, task_id: int) -> list[Task]:
        """Find descendants in every state, without looping on legacy parent cycles."""
        rows = self._conn.execute(
            """WITH RECURSIVE subtree(id) AS (
                SELECT id FROM task WHERE id = ?
                UNION
                SELECT t.id FROM task t JOIN subtree s ON t.root_id = s.id
            )
            SELECT t.* FROM task t JOIN subtree s ON t.id = s.id
            WHERE t.id != ? ORDER BY t.id""",
            (task_id, task_id),
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def active_descendants(self, task_id: int) -> list[Task]:
        return [task for task in self.descendants(task_id) if task.state.is_active]

    def validate_parent(self, task_id: int, parent_id: int) -> None:
        """Validate a containment link; dependency edges remain a separate graph."""
        if task_id == parent_id:
            raise ValueError(_t("store.error.self_link"))
        self._require_parent_task(parent_id)
        if any(task.persisted_id == parent_id for task in self.descendants(task_id)):
            raise ValueError(_t("store.error.parent_cycle"))

    def complete_task(
        self, task_id: int, *, with_subtasks: bool = False,
        note: str | None = None, ts: datetime | None = None,
    ) -> list[int]:
        """Complete a task and, only on explicit request, its unfinished descendants atomically."""
        with self._conn:
            self._require_live_task(task_id)
            children = self.active_descendants(task_id)
            if children and not with_subtasks:
                tasks = "; ".join(f"#{t.id} {t.title}" for t in children)
                raise ValueError(_t("store.error.unfinished_subtasks", id=task_id, tasks=tasks))
            ids = [task_id, *(t.persisted_id for t in children)]
            iso = self._to_iso(ts or self._now())
            for tid in ids:
                self._conn.execute(
                    "UPDATE task SET state = ?, completed_at = ?, snoozed_until = NULL, snooze_notified_until = NULL WHERE id = ?",
                    (State.DONE.value, iso, tid),
                )
                self._conn.execute(
                    "INSERT INTO touch(task_id, kind, ts, note) VALUES (?, ?, ?, ?)",
                    (tid, TouchKind.DONE.value, iso, note),
                )
                self._conn.execute("DELETE FROM watch_state WHERE task_id = ?", (tid,))
        return ids

    def reopen_task(
        self, task_id: int, *, root_id: int | None = None,
        heap_max: int = 50, force: bool = False, ts: datetime | None = None,
    ) -> Task:
        """Reopen only this completed task; retain root identity, links, and its full history."""
        with self._conn:
            task = self.get_task(task_id, record_view=False)
            if task is None:
                raise ValueError(_t("store.error.task_not_found", id=task_id))
            if task.state is not State.DONE:
                raise ValueError(_t("store.error.task_not_done", id=task_id))
            parent = root_id if root_id is not None else task.root_id
            if parent is not None:
                if parent == task_id:
                    raise ValueError(_t("store.error.self_link"))
                target = self.get_task(parent, record_view=False)
                if target is None or not target.state.is_active:
                    raise ValueError(_t("store.error.reopen_parent_inactive", id=parent))
                self.validate_parent(task_id, parent)
            if not force and not task.is_meta and self.heap_size() >= heap_max:
                raise ValueError(_t("store.error.reopen_heap_full", max=heap_max))
            iso = self._to_iso(ts or self._now())
            inbox_since = iso if task.in_inbox and root_id is None else None
            self._conn.execute(
                "UPDATE task SET state = ?, completed_at = NULL, root_id = ?, inbox_since = ?, snoozed_until = NULL, snooze_notified_until = NULL WHERE id = ?",
                (State.LIVE.value, parent, inbox_since, task_id),
            )
            self._conn.execute(
                "INSERT INTO touch(task_id, kind, ts, note) VALUES (?, ?, ?, ?)",
                (task_id, TouchKind.REOPEN.value, iso, f"parent=#{parent}" if parent else None),
            )
            self._conn.execute("DELETE FROM watch_state WHERE task_id = ?", (task_id,))
        task.state = State.LIVE
        task.completed_at = None
        task.root_id = parent
        task.inbox_since = self._from_iso(inbox_since) if inbox_since else None
        task.snoozed_until = None
        return task

    def restore_task(
        self, task_id: int, *, root_id: int | None = None, heap_max: int = 50,
        force: bool = False, ts: datetime | None = None,
    ) -> Task:
        """Restore the original location, or organize into an explicitly selected parent."""
        with self._conn:
            task = self.get_task(task_id, record_view=False)
            if task is None:
                raise ValueError(_t("store.error.task_not_found", id=task_id))
            if task.state is not State.COLLECTED:
                raise ValueError(_t("cli.restore.not_collected", id=task_id))
            if root_id is not None:
                self.validate_parent(task_id, root_id)
            if not force and not task.is_meta and self.heap_size() >= heap_max:
                raise ValueError(_t("inbox.error.full", max=heap_max))
            iso = self._to_iso(ts or self._now())
            inbox_since = iso if task.in_inbox and root_id is None else None
            self._conn.execute(
                "UPDATE task SET state = ?, root_id = ?, inbox_since = ?, snoozed_until = NULL, snooze_notified_until = NULL WHERE id = ?",
                (State.LIVE.value, root_id if root_id is not None else task.root_id, inbox_since, task_id),
            )
            self._conn.execute(
                "INSERT INTO touch(task_id, kind, ts) VALUES (?, ?, ?)", (task_id, TouchKind.RESTORE.value, iso),
            )
            self._conn.execute("DELETE FROM watch_state WHERE task_id = ?", (task_id,))
        restored = self.get_task(task_id, record_view=False)
        assert restored is not None
        return restored

    # ---- roots and "reason" edges (option A) ----

    def set_root(self, task_id: int, is_root: bool = True) -> None:
        """Mark / unmark a task as an active GC root (a declared goal).

        Args:
            task_id (int): the target task id.
            is_root (bool): True to mark as root, False to unmark.

        Returns:
            None
        """
        task = self._require_live_task(task_id)
        self._conn.execute(
            "UPDATE task SET is_root = ?, inbox_since = CASE WHEN ? THEN NULL ELSE inbox_since END WHERE id = ?",
            (int(is_root), int(is_root), task_id),
        )
        if task.in_inbox and is_root:
            self.record(task_id, TouchKind.ORGANIZE, note="made goal")
        self._conn.commit()

    def root_ids(self, *, active_only: bool = True) -> set[int]:
        """Get declared root IDs, excluding inactive roots by default for GC.

        Args:
            active_only (bool): False includes historical roots for the completed task tree.

        Returns:
            set[int]: ids of tasks with is_root true (may be empty).
        """
        if active_only:
            rows = self._conn.execute(
                "SELECT id FROM task WHERE is_root = 1 AND inbox_since IS NULL AND state IN (?, ?)",
                (State.LIVE.value, State.CANDIDATE.value),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT id FROM task WHERE is_root = 1").fetchall()
        return {int(r["id"]) for r in rows}

    def add_dep(self, task_id: int, ref_id: int) -> None:
        """Add a "reason" edge: task_id's reason/prerequisite is ref_id (i.e. task_id references ref_id).

        Idempotent (adding the same edge twice doesn't error). These task->task edges are the
        input to reference-cycle detection.

        Args:
            task_id (int): id of the task doing the referencing.
            ref_id (int): id of the task being referenced (as reason/prerequisite).

        Returns:
            None

        Raises:
            ValueError: if task_id == ref_id (a self-loop), or task_id / ref_id points to a
                nonexistent task.
        """
        if task_id == ref_id:
            raise ValueError(_t("store.error.self_dep"))
        self._require_live_task(task_id)
        self._require_live_task(ref_id)
        self._conn.execute(
            "INSERT OR IGNORE INTO dep (task_id, ref_id) VALUES (?, ?)", (task_id, ref_id)
        )
        self._conn.commit()

    def remove_dep(self, task_id: int, ref_id: int) -> None:
        """Remove a "reason" edge (a no-op if it doesn't exist).

        Args:
            task_id (int): id of the task doing the referencing.
            ref_id (int): id of the referenced task.

        Returns:
            None
        """
        self._conn.execute("DELETE FROM dep WHERE task_id = ? AND ref_id = ?", (task_id, ref_id))
        self._conn.commit()

    def deps(self) -> list[tuple[int, int]]:
        """Get all "reason" edges, for the collector to assemble the reference graph.

        Returns:
            list[tuple[int, int]]: a list of (task_id, ref_id); each entry means task_id
                references ref_id.
        """
        rows = self._conn.execute(
            "SELECT task_id, ref_id FROM dep ORDER BY task_id, ref_id"
        ).fetchall()
        return [(int(r["task_id"]), int(r["ref_id"])) for r in rows]

    def heap_size(self) -> int:
        """Count active, non-Metaspace tasks; completed and collected tasks do not occupy the heap.

        Matches -Xmx's accounting — Metaspace (meta-tasks) never counts toward -Xmx in the
        JVM either, hence the exclusion.

        Returns:
            int: the number of tasks on the active heap.
        """
        row = self._conn.execute(
            "SELECT count(*) FROM task WHERE state IN (?, ?) AND gen != ?",
            (State.LIVE.value, State.CANDIDATE.value, Generation.META.value),
        ).fetchone()
        return int(row[0])

    # ---- daily planning (independent of GC state and progress) ----

    def focus_task_ids(self, day: date) -> list[int]:
        return [int(row[0]) for row in self._conn.execute(
            "SELECT task_id FROM daily_focus WHERE day = ? AND removed = 0 ORDER BY position, task_id",
            (day.isoformat(),),
        )]

    def previous_focus_ids(self, day: date) -> list[int]:
        """Latest earlier selections, excluding anything explicitly handled today."""
        return [int(row[0]) for row in self._conn.execute(
            """SELECT f.task_id FROM daily_focus f
               WHERE f.day < ? AND f.removed = 0
                 AND f.day = (SELECT MAX(latest.day) FROM daily_focus latest
                              WHERE latest.task_id = f.task_id AND latest.day <= ?)
               ORDER BY f.day DESC, f.position, f.task_id""",
            (day.isoformat(), day.isoformat()),
        )]

    def _order_focus(self, day: date, ids: list[int]) -> None:
        self._conn.executemany(
            "UPDATE daily_focus SET position = ? WHERE day = ? AND task_id = ?",
            [(i, day.isoformat(), task_id) for i, task_id in enumerate(ids, 1)],
        )

    def focus_task(self, task_id: int, *, position: int | None = None, with_subtasks: bool = False,
                   ts: datetime | None = None) -> None:
        """Atomically select a task and optionally its available containment descendants.

        New selections append in parent-before-child order; existing selections keep
        their positions. An explicit position moves the selected branch as one block.
        Planning never writes touch events or changes lifecycle/GC state.
        """
        now = ts or self._now()
        day = now.astimezone().date()
        with self._conn:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            task = self._require_live_task(task_id)
            if task.in_inbox or task.is_snoozed(now):
                raise ValueError(_t("today.error.unavailable", id=task_id))
            branch = [task_id]
            if with_subtasks:
                children: dict[int, list[Task]] = {}
                for child in self.descendants(task_id):
                    if child.root_id is not None:
                        children.setdefault(child.root_id, []).append(child)
                pending = list(reversed(children.get(task_id, [])))
                seen = {task_id}
                while pending:
                    child = pending.pop()
                    tid = child.persisted_id
                    if tid in seen:
                        continue
                    seen.add(tid)
                    if child.state.is_active and not child.in_inbox and not child.is_snoozed(now):
                        branch.append(tid)
                    # A sleeping parent can still have awake, actionable children.
                    pending.extend(reversed(children.get(tid, [])))
            ids = self.focus_task_ids(day)
            if position is None:
                existing = set(ids)
                additions = [tid for tid in branch if tid not in existing]
                if not additions:
                    return
                ids.extend(additions)
            else:
                selected = set(branch)
                ids = [tid for tid in ids if tid not in selected]
                if not 1 <= position <= len(ids) + 1:
                    raise ValueError(_t("today.error.position", max=len(ids) + 1))
                ids[position - 1:position - 1] = branch
            self._conn.executemany(
                """INSERT INTO daily_focus(day, task_id, position, removed) VALUES (?, ?, 0, 0)
                   ON CONFLICT(day, task_id) DO UPDATE SET removed = 0""",
                [(day.isoformat(), tid) for tid in branch],
            )
            self._order_focus(day, ids)

    def unfocus_task(self, task_id: int, *, ts: datetime | None = None) -> None:
        """Remove a selection/suggestion today; keep earlier daily plans intact."""
        day = (ts or self._now()).astimezone().date()
        with self._conn:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            if self.get_task(task_id, record_view=False) is None:
                raise ValueError(_t("store.error.task_not_found", id=task_id))
            self._conn.execute(
                """INSERT INTO daily_focus(day, task_id, position, removed) VALUES (?, ?, 0, 1)
                   ON CONFLICT(day, task_id) DO UPDATE SET removed = 1""",
                (day.isoformat(), task_id),
            )
            self._order_focus(day, self.focus_task_ids(day))

    def handled_focus_ids(self, day: date) -> set[int]:
        return {int(row[0]) for row in self._conn.execute(
            "SELECT task_id FROM daily_focus WHERE day = ?", (day.isoformat(),),
        )}

    # ---- reads (auto-track a view event by default) ----

    def get_task(self, task_id: int, *, record_view: bool = True, ts: datetime | None = None) -> Task | None:
        """Read a task by id; logs a VIEW event along the way by default (the tool is "watching you look").

        Args:
            task_id (int): the task id.
            record_view (bool): whether to log a VIEW event; internal scans like the
                collector's should pass False, so they don't pollute the event stream and
                inflate view counts.
            ts (datetime | None): the VIEW event time; None uses the current UTC time.

        Returns:
            Task | None: the Task if found; None if it doesn't exist (and no VIEW is logged).
        """
        row = self._conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        if record_view:
            self.record(task_id, TouchKind.VIEW, ts=ts)
        return self._row_to_task(row)

    def list_tasks(
        self,
        *,
        state: State | None = None,
        active_only: bool = False,
        inbox: bool | None = None,
        snoozed: bool | None = None,
        record_view: bool = True,
        ts: datetime | None = None,
    ) -> list[Task]:
        """List tasks (optionally filtered by state); logs a VIEW event for each listed task by default.

        Args:
            state (State | None): only return tasks in this state; None returns all.
            active_only (bool): restrict the query to active tasks before recording views.
            record_view (bool): whether to log a VIEW event for the listed tasks; internal
                scans should pass False.
            ts (datetime | None): the VIEW event time; None uses the current UTC time.

        Returns:
            list[Task]: tasks ordered ascending by id (may be empty).
        """
        conditions: list[str] = []
        params: list[str] = []
        if state is not None:
            conditions.append("state = ?")
            params.append(state.value)
        if active_only:
            conditions.append("state IN (?, ?)")
            params.extend((State.LIVE.value, State.CANDIDATE.value))
        if inbox is not None:
            conditions.append("inbox_since IS NOT NULL" if inbox else "inbox_since IS NULL")
        if snoozed is not None:
            conditions.append("snoozed_until > ?" if snoozed else "(snoozed_until IS NULL OR snoozed_until <= ?)")
            params.append(self._to_iso(ts or self._now()))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self._conn.execute("SELECT * FROM task" + where + " ORDER BY id", params).fetchall()
        tasks = [self._row_to_task(r) for r in rows]
        if record_view:
            self.record_views(tasks, ts=ts)
        return tasks

    def record_views(self, tasks: list[Task], *, ts: datetime | None = None) -> None:
        """Record views only after a caller has finished filtering and limiting."""
        if not tasks:
            return
        iso = self._to_iso(ts or self._now())
        with self._conn:
            self._conn.executemany(
                "INSERT INTO touch (task_id, kind, ts, note) VALUES (?, ?, ?, NULL)",
                [(task.persisted_id, TouchKind.VIEW.value, iso) for task in tasks],
            )

    def task_notes(self) -> dict[int, list[str]]:
        notes: dict[int, list[str]] = {}
        for row in self._conn.execute("SELECT task_id, note FROM touch WHERE note IS NOT NULL"):
            notes.setdefault(int(row[0]), []).append(str(row[1]))
        return notes

    def last_activity_times(self) -> dict[int, datetime]:
        """Browsing never changes the 'recently updated' order."""
        return {
            int(row[0]): self._from_iso(row[1])
            for row in self._conn.execute(
                "SELECT task_id, MAX(ts) FROM touch WHERE kind != 'view' GROUP BY task_id"
            )
        }

    def touches(self, task_id: int) -> list[Touch]:
        """Get all touch events for a task, ascending by time (the raw material for liveness / the GC log).

        Args:
            task_id (int): the task id.

        Returns:
            list[Touch]: the task's touch events (may be empty).
        """
        rows = self._conn.execute(
            "SELECT * FROM touch WHERE task_id = ? ORDER BY ts, id", (task_id,)
        ).fetchall()
        return [self._row_to_touch(r) for r in rows]

    # ---- liveness metrics (pure queries, no writes) ----

    def _count_touches(
        self,
        task_id: int,
        kinds: tuple[str, ...],
        *,
        within_days: int | None = None,
        now: datetime | None = None,
    ) -> int:
        """Count a task's touch events of the given types (and optional time window).

        Args:
            task_id (int): the task id.
            kinds (tuple[str, ...]): the set of event type values (TouchKind.value).
            within_days (int | None): only count the last N days; None means no time limit.
            now (datetime | None): the "now" base for the time window; None uses the current
                UTC time.

        Returns:
            int: the count of matching touch events.
        """
        if not kinds:
            return 0
        placeholders = ",".join("?" for _ in kinds)
        sql = f"SELECT count(*) FROM touch WHERE task_id = ? AND kind IN ({placeholders})"
        params: list[object] = [task_id, *kinds]
        if within_days is not None:
            cutoff = (now or self._now()) - timedelta(days=within_days)
            sql += " AND ts >= ?"
            params.append(self._to_iso(cutoff))
        return int(self._conn.execute(sql, params).fetchone()[0])

    def refcount(self, task_id: int, *, within_days: int | None = None, now: datetime | None = None) -> int:
        """A task's reference count = count of real progress events (PROGRESS/EDIT); optionally limited to the last N days.

        Args:
            task_id (int): the task id.
            within_days (int | None): only count the last N days (liveness defaults to 30);
                None means no limit.
            now (datetime | None): the time-window base; None uses the current UTC time.

        Returns:
            int: the refcount value.
        """
        return self._count_touches(task_id, _PROGRESS_KIND_VALUES, within_days=within_days, now=now)

    def skip_count(self, task_id: int) -> int:
        """Cumulative count of times a task has been deferred (DEFER).

        Args:
            task_id (int): the task id.

        Returns:
            int: the skip_count value.
        """
        return self._count_touches(task_id, (TouchKind.DEFER.value,))

    def consecutive_skip_count(self, task_id: int) -> int:
        """Count of consecutive DEFERs since the last progress/edit/revive/restore."""
        reset = {
            TouchKind.PROGRESS, TouchKind.EDIT, TouchKind.REVIVE, TouchKind.RESTORE,
            TouchKind.REOPEN,
            TouchKind.ORGANIZE,
        }
        count = 0
        for touch in reversed(self.touches(task_id)):
            if touch.kind in reset:
                break
            if touch.kind is TouchKind.DEFER:
                count += 1
            elif touch.kind not in {TouchKind.VIEW, TouchKind.LINK, TouchKind.CREATE, TouchKind.SNOOZE, TouchKind.UNSNOOZE}:
                break
        return count

    def view_count(self, task_id: int) -> int:
        """Cumulative count of times a task has been viewed (VIEW) — usually far greater than refcount, ammunition for the taunts.

        Args:
            task_id (int): the task id.

        Returns:
            int: the view_count value.
        """
        return self._count_touches(task_id, (TouchKind.VIEW.value,))

    def last_touch_at(self, task_id: int, *, progress_only: bool = False) -> datetime | None:
        """The last time a task was touched.

        Args:
            task_id (int): the task id.
            progress_only (bool): if True, only considers real progress (PROGRESS/EDIT),
                used for liveness's "recently touched".

        Returns:
            datetime | None: the last touch time; None if there's no matching event.
        """
        if progress_only:
            placeholders = ",".join("?" for _ in _PROGRESS_KIND_VALUES)
            sql = f"SELECT max(ts) FROM touch WHERE task_id = ? AND kind IN ({placeholders})"
            params: list[object] = [task_id, *_PROGRESS_KIND_VALUES]
        else:
            sql = "SELECT max(ts) FROM touch WHERE task_id = ?"
            params = [task_id]
        row = self._conn.execute(sql, params).fetchone()
        return self._from_iso(row[0]) if row and row[0] is not None else None

    # ---- GC audit history ----

    def record_gc_run(
        self,
        *,
        started_at: datetime,
        mode: str,
        heap_before: int,
        survivors: int,
        candidates: int,
        collected: int,
        revived: int,
        released_pct: float,
        items: list[tuple[int, str, str]] | None = None,
    ) -> int:
        """Record a GC that was actually executed; each item is ``(task_id, reason, action)``."""
        cur = self._conn.execute(
            "INSERT INTO gc_run(started_at, mode, heap_before, survivors, candidates, collected, revived, released_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._to_iso(started_at), mode, heap_before, survivors, candidates,
                collected, revived, released_pct,
            ),
        )
        run_id = self._lastrowid(cur)
        if items:
            self._conn.executemany(
                "INSERT INTO gc_run_item(run_id, task_id, reason, action) VALUES (?, ?, ?, ?)",
                [(run_id, task_id, reason, action) for task_id, reason, action in items],
            )
        self._conn.commit()
        return run_id

    def gc_runs(self, *, since_days: int | None = None, now: datetime | None = None) -> list[GCRun]:
        """Read the GC history, most recent first."""
        sql = "SELECT * FROM gc_run"
        params: list[object] = []
        if since_days is not None:
            cutoff = (now or self._now()) - timedelta(days=since_days)
            sql += " WHERE started_at >= ?"
            params.append(self._to_iso(cutoff))
        sql += " ORDER BY started_at DESC, id DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [
            GCRun(
                id=int(r["id"]), started_at=self._from_iso(r["started_at"]), mode=r["mode"],
                heap_before=int(r["heap_before"]), survivors=int(r["survivors"]),
                candidates=int(r["candidates"]), collected=int(r["collected"]),
                revived=int(r["revived"]), released_pct=float(r["released_pct"]),
            )
            for r in rows
        ]

    # ---- weekly review history ----

    def review_events(self, start: datetime, end: datetime) -> list[ReviewEvent]:
        """Read event-time labels where available; legacy events use current task labels."""
        rows = self._conn.execute(
            "SELECT e.task_id, e.kind, e.ts, COALESCE(s.title, t.title) AS title, "
            "COALESCE(s.is_goal, t.is_root) AS is_goal, "
            "COALESCE(s.in_inbox, t.inbox_since IS NOT NULL) AS in_inbox, "
            "s.touch_id IS NOT NULL AS historical_label "
            "FROM touch e JOIN task t ON t.id = e.task_id "
            "LEFT JOIN touch_task_snapshot s ON s.touch_id = e.id "
            "WHERE e.ts >= ? AND e.ts < ? ORDER BY e.ts, e.id",
            (self._to_iso(start), self._to_iso(end)),
        ).fetchall()
        return [ReviewEvent(int(row["task_id"]), TouchKind(row["kind"]), self._from_iso(row["ts"]),
                            row["title"], bool(row["is_goal"]), bool(row["in_inbox"]),
                            bool(row["historical_label"])) for row in rows]

    def review_collected_ids(self, start: datetime, end: datetime) -> set[int]:
        rows = self._conn.execute(
            "SELECT DISTINCT i.task_id FROM gc_run_item i JOIN gc_run r ON r.id = i.run_id "
            "WHERE i.action = 'collect' AND r.started_at >= ? AND r.started_at < ?",
            (self._to_iso(start), self._to_iso(end)),
        ).fetchall()
        return {int(row[0]) for row in rows}

    def save_review(self, *, week_start: str, generated_at: datetime, payload: str,
                    schedule_key: str | None = None) -> int:
        with self._conn:
            if schedule_key is not None:
                # This insert acquires SQLite's write lock before checking the key.
                # The empty reservation and snapshot commit atomically, including on crash.
                self._conn.execute("INSERT OR IGNORE INTO watch_meta(key, value) VALUES (?, '')", (schedule_key,))
                existing = self.watch_meta_get(schedule_key)
                if existing:
                    return int(existing)
            cur = self._conn.execute(
                "INSERT INTO review_snapshot(week_start, generated_at, payload) VALUES (?, ?, ?)",
                (week_start, self._to_iso(generated_at), payload),
            )
            snapshot_id = self._lastrowid(cur)
            if schedule_key is not None:
                self._conn.execute("UPDATE watch_meta SET value = ? WHERE key = ?", (str(snapshot_id), schedule_key))
        return snapshot_id

    def saved_reviews(self) -> list[ReviewSnapshot]:
        rows = self._conn.execute("SELECT * FROM review_snapshot ORDER BY id DESC").fetchall()
        return [ReviewSnapshot(int(row["id"]), row["week_start"], self._from_iso(row["generated_at"]),
                               row["payload"]) for row in rows]

    def saved_review(self, snapshot_id: int) -> ReviewSnapshot | None:
        row = self._conn.execute("SELECT * FROM review_snapshot WHERE id = ?", (snapshot_id,)).fetchone()
        return ReviewSnapshot(int(row["id"]), row["week_start"], self._from_iso(row["generated_at"]), row["payload"]) if row else None

    # ---- scheduled checkup (watch): state debouncing + single-instance advisory lock ----

    def watch_states(self) -> dict[int, WatchState]:
        """Read every task's status recorded on the previous watch tick, for this round to diff out "transitions".

        Returns:
            dict[int, WatchState]: task_id -> previous round's status; tasks that have never
                been through watch aren't included.
        """
        rows = self._conn.execute("SELECT task_id, status, notified_at FROM watch_state").fetchall()
        return {
            int(r["task_id"]): WatchState(
                task_id=int(r["task_id"]),
                status=r["status"],
                notified_at=self._from_iso(r["notified_at"]) if r["notified_at"] is not None else None,
            )
            for r in rows
        }

    def record_watch_tick(self, task_id: int, status: str, *, notified_at: datetime | None = None) -> None:
        """Record a task's judged status for this round's tick; only stamps the debounce timestamp if an event was actually pushed for it this round.

        Args:
            task_id (int): the task id.
            status (str): this round's judged status ("alive" or a collector.Reason value).
            notified_at (datetime | None): pass the push time if an event was pushed for this
                task this round; otherwise pass None to leave the previous notified_at
                unchanged (this does not mean "never pushed" should be reset to zero).

        Returns:
            None
        """
        if notified_at is None:
            self._conn.execute(
                "INSERT INTO watch_state(task_id, status, notified_at) VALUES (?, ?, NULL) "
                "ON CONFLICT(task_id) DO UPDATE SET status = excluded.status",
                (task_id, status),
            )
        else:
            self._conn.execute(
                "INSERT INTO watch_state(task_id, status, notified_at) VALUES (?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET status = excluded.status, notified_at = excluded.notified_at",
                (task_id, status, self._to_iso(notified_at)),
            )
        self._conn.commit()

    def watch_meta_get(self, key: str) -> str | None:
        """Read a watch_meta key-value pair (e.g. last_digest_at).

        Args:
            key (str): the key name.

        Returns:
            str | None: the matching value; None if it doesn't exist.
        """
        row = self._conn.execute("SELECT value FROM watch_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else None

    def watch_meta_set(self, key: str, value: str) -> None:
        """Write (insert or overwrite) a watch_meta key-value pair (e.g. last_digest_at).

        Args:
            key (str): the key name.
            value (str): the value.

        Returns:
            None
        """
        self._conn.execute(
            "INSERT INTO watch_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def try_acquire_watch_lock(self, pid: int, now: datetime, *, stale_after: timedelta) -> bool:
        """Try to acquire the advisory lock for "this process is the only watcher for this database" (not a database-level mutex, just a convention).

        Succeeds in taking over when there's no lock record, or the existing lock's heartbeat
        predates ``stale_after`` (the lock-holding process is judged dead); if another watcher
        holds the lock with a still-fresh heartbeat, returns False and the caller should give
        up starting, to avoid duplicate pushes.

        Args:
            pid (int): this process's pid.
            now (datetime): the current time.
            stale_after (timedelta): a heartbeat older than this is judged expired and may be
                taken over.

        Returns:
            bool: True if the lock was acquired (or renewed); False if another watcher is
                still alive and this process should back off.
        """
        heartbeat_raw = self.watch_meta_get("watch_lock_heartbeat_at")
        if heartbeat_raw is not None and now - self._from_iso(heartbeat_raw) < stale_after:
            owner_raw = self.watch_meta_get("watch_lock_pid")
            if owner_raw is not None and int(owner_raw) != pid:
                return False
        self.watch_meta_set("watch_lock_pid", str(pid))
        self.watch_meta_set("watch_lock_heartbeat_at", self._to_iso(now))
        return True

    def heartbeat_watch_lock(self, pid: int, now: datetime) -> None:
        """Renew the watch lock held by this process (only takes effect if the lock actually belongs to this pid).

        Args:
            pid (int): this process's pid.
            now (datetime): the current time.

        Returns:
            None
        """
        owner_raw = self.watch_meta_get("watch_lock_pid")
        if owner_raw is not None and int(owner_raw) == pid:
            self.watch_meta_set("watch_lock_heartbeat_at", self._to_iso(now))

    def release_watch_lock(self, pid: int) -> None:
        """Voluntarily release the watch lock held by this process (only takes effect if the lock actually belongs to this pid); call this on graceful shutdown.

        Args:
            pid (int): this process's pid.

        Returns:
            None
        """
        owner_raw = self.watch_meta_get("watch_lock_pid")
        if owner_raw is not None and int(owner_raw) == pid:
            self._conn.execute(
                "DELETE FROM watch_meta WHERE key IN ('watch_lock_pid', 'watch_lock_heartbeat_at')"
            )
            self._conn.commit()

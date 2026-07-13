# store.py —— 持久化层：用 SQLite 落地 Task 与 Touch；读取任务时自动埋点 view 事件，让"触碰历史"这份判活地基自己长出来。

from __future__ import annotations

import sqlite3
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from todogc.heap import Generation, ReferenceStrength, State, Task, Touch, TouchKind

# 计入 refcount 的事件类型值：复用 TouchKind.is_progress 这一单一口径，不在此重复定义。
_PROGRESS_KIND_VALUES: tuple[str, ...] = tuple(k.value for k in TouchKind if k.is_progress)

# 建表语句。task = 堆对象；touch = append-only 的触碰事件流(判活的唯一真相来源)。
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

-- task→task 的"理由"边：dep(task_id, ref_id) 表示 task_id 的理由/前置是 ref_id(即 task_id 引用 ref_id)。
CREATE TABLE IF NOT EXISTS dep (
    task_id INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    ref_id  INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_touch_task ON touch(task_id);
"""

_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class WatchState:
    """某任务在上一轮 ``todogc watch`` tick 里的判定状态与去抖时间戳。

    Attributes:
        task_id (int): 任务 id。
        status (str): 上一轮判定状态("alive" 或 collector.Reason 的值)。
        notified_at (datetime | None): 最近一次为该任务当前状态发过事件推送的时间；
            None 表示该状态自出现以来还没推送过(或从未记录)。
    """

    task_id: int
    status: str
    notified_at: datetime | None


@dataclass(frozen=True)
class GCRun:
    """一次已实际执行的 GC 审计记录。"""

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
    """TodoGC 的持久化门面：包一层 SQLite，对外只暴露 Task / Touch 语义，隐藏 SQL 细节。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        """打开(或新建)一个 TodoGC 数据库并建好表结构。

        Args:
            path (str | Path): 数据库文件路径；默认 ":memory:" 用内存库(测试友好)。

        Returns:
            None
        """
        self.path = str(path)
        existed = self.path != ":memory:" and Path(self.path).exists()
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema(existed=existed)

    # ---- 资源管理 ----

    def __enter__(self) -> Store:
        """进入 with 上下文。

        Returns:
            Store: 自身，供 with ... as store 使用。
        """
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """退出 with 上下文时提交并关闭连接。

        Args:
            exc_type (object): 异常类型(无异常为 None)。
            exc (object): 异常实例(无异常为 None)。
            tb (object): 异常回溯(无异常为 None)。

        Returns:
            None: 不吞异常。
        """
        self.close()

    def close(self) -> None:
        """提交并关闭底层连接。

        Returns:
            None
        """
        self._conn.commit()
        self._conn.close()

    # ---- 内部工具 ----

    def _init_schema(self, *, existed: bool = False) -> None:
        """执行建表语句(幂等，IF NOT EXISTS)。

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
        self._conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version),),
        )
        self._conn.commit()

    @property
    def schema_version(self) -> int:
        """当前数据库 schema 版本。"""
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _now() -> datetime:
        """取当前 UTC 时间。

        Returns:
            datetime: 带时区(UTC)的当前时间。
        """
        return datetime.now(timezone.utc)

    @staticmethod
    def _to_iso(dt: datetime) -> str:
        """把 datetime 归一化为 UTC 并转成 ISO 8601 字符串(保证可按字典序比较时间)。

        Args:
            dt (datetime): 待转换时间；naive(无时区)时间按 UTC 处理。

        Returns:
            str: 形如 '2026-07-10T12:00:00+00:00' 的字符串。
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _from_iso(s: str) -> datetime:
        """把 ISO 8601 字符串解析回带时区的 datetime。

        Args:
            s (str): _to_iso 产出的时间字符串。

        Returns:
            datetime: 带时区(UTC)的时间。
        """
        return datetime.fromisoformat(s)

    @staticmethod
    def _lastrowid(cur: sqlite3.Cursor) -> int:
        """取得 INSERT 主键；驱动未返回主键时给出明确错误。"""
        row_id = cur.lastrowid
        if row_id is None:
            raise RuntimeError("SQLite INSERT 未返回 lastrowid")
        return row_id

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        """把 task 表的一行映射为 Task 领域对象。

        Args:
            row (sqlite3.Row): task 表查询结果的一行。

        Returns:
            Task: 还原出的任务对象。
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
        )

    @staticmethod
    def _row_to_touch(row: sqlite3.Row) -> Touch:
        """把 touch 表的一行映射为 Touch 领域对象。

        Args:
            row (sqlite3.Row): touch 表查询结果的一行。

        Returns:
            Touch: 还原出的触碰事件对象。
        """
        return Touch(
            id=row["id"],
            task_id=row["task_id"],
            kind=TouchKind(row["kind"]),
            ts=Store._from_iso(row["ts"]),
            note=row["note"],
        )

    # ---- 写入 ----

    def add_task(
        self,
        title: str,
        *,
        root_id: int | None = None,
        is_root: bool = False,
        meta: bool = False,
        ref_strength: ReferenceStrength | str = ReferenceStrength.SOFT,
        ts: datetime | None = None,
    ) -> Task:
        """新增(分配)一个任务，并记一条 CREATE 触碰事件。

        Args:
            title (str): 任务标题。
            root_id (int | None): 关联到的父(目标/任务)id；None 表示未关联(天然候选)。
            is_root (bool): 是否本身就是一个活跃 GC root(声明的目标)；rootness 之后由 root_ids() 查询。
            meta (bool): 是否为常驻 Metaspace 的元任务(如"变得更自律")。
            ts (datetime | None): 创建时间；None 用当前 UTC。

        Returns:
            Task: 已落库、带自增 id 与 created_at 的任务对象。
        """
        title = title.strip()
        if not title:
            raise ValueError("任务标题不能为空")
        now = ts or self._now()
        strength = ReferenceStrength(ref_strength)
        if root_id is not None:
            self._require_live_task(root_id)
        iso = self._to_iso(now)
        gen = Generation.META if meta else Generation.EDEN
        cur = self._conn.execute(
            "INSERT INTO task (title, root_id, is_root, gen, age, state, created_at, ref_strength) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (title, root_id, int(is_root), gen.value, State.LIVE.value, iso, strength.value),
        )
        task_id = self._lastrowid(cur)
        self.record(task_id, TouchKind.CREATE, ts=now)
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
        )

    def record(
        self,
        task_id: int,
        kind: TouchKind,
        *,
        note: str | None = None,
        ts: datetime | None = None,
    ) -> Touch:
        """追加一条触碰事件(唯一的底层事件写入口，只增不改)。

        Args:
            task_id (int): 目标任务 id。
            kind (TouchKind): 事件类型。
            note (str | None): 附加文本(进展说明 / 推迟理由 / 遗言)。
            ts (datetime | None): 事件时间；None 用当前 UTC。

        Returns:
            Touch: 已落库、带自增 id 的触碰事件对象。
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
        """标记"我真的推进了这个任务"——记一条 PROGRESS 事件(计入 refcount)。

        Args:
            task_id (int): 目标任务 id。
            note (str | None): 进展说明。
            ts (datetime | None): 事件时间；None 用当前 UTC。

        Returns:
            Touch: 新记录的触碰事件。
        """
        self._require_live_task(task_id)
        return self.record(task_id, TouchKind.PROGRESS, note=note, ts=ts)

    def defer(self, task_id: int, note: str | None = None, ts: datetime | None = None) -> Touch:
        """推迟(snooze)一个任务——记一条 DEFER 事件(喂 skip_count，是自欺的铁证)。

        Args:
            task_id (int): 目标任务 id。
            note (str | None): 推迟理由。
            ts (datetime | None): 事件时间；None 用当前 UTC。

        Returns:
            Touch: 新记录的触碰事件。
        """
        self._require_live_task(task_id)
        return self.record(task_id, TouchKind.DEFER, note=note, ts=ts)

    def link(self, task_id: int, root_id: int, ts: datetime | None = None) -> None:
        """把任务(重新)关联到某个 GC root：更新 root_id 并记一条 LINK 事件。

        Args:
            task_id (int): 目标任务 id。
            root_id (int): 要关联到的 root(活跃目标)id。
            ts (datetime | None): 事件时间；None 用当前 UTC。

        Returns:
            None
        """
        if task_id == root_id:
            raise ValueError("一个任务不能关联到自己")
        self._require_live_task(task_id)
        self._require_live_task(root_id)
        self._conn.execute("UPDATE task SET root_id = ? WHERE id = ?", (root_id, task_id))
        self.record(task_id, TouchKind.LINK, ts=ts)
        self._conn.commit()

    def update_task(self, task: Task) -> None:
        """把内存中 Task 的当前状态(标题/关联/分代/年龄/状态)回写数据库。

        供 collector 在标记候选、晋升分代、复活/回收后持久化用；本方法不记触碰事件，
        语义事件(如 REVIVE/COLLECT)由调用方另行 record()。

        Args:
            task (Task): 必须带非空 id 的任务对象。

        Returns:
            None

        Raises:
            ValueError: 当 task.id 为 None(尚未落库)时。
        """
        if task.id is None:
            raise ValueError("update_task 需要一个已落库(id 非空)的 Task")
        self._conn.execute(
            "UPDATE task SET title = ?, root_id = ?, gen = ?, age = ?, state = ?, ref_strength = ? WHERE id = ?",
            (
                task.title, task.root_id, task.gen.value, task.age, task.state.value,
                task.ref_strength.value, task.id,
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
        """编辑标题和/或引用强度，并写入 EDIT 事件。"""
        task = self._require_live_task(task_id)
        if title is not None:
            title = title.strip()
            if not title:
                raise ValueError("任务标题不能为空")
            task.title = title
        if ref_strength is not None:
            task.ref_strength = ReferenceStrength(ref_strength)
        self.update_task(task)
        self.record(task_id, TouchKind.EDIT, note=title, ts=ts)
        return task

    def unlink(self, task_id: int, ts: datetime | None = None) -> None:
        """解除父/目标关联；用 LINK 事件记录审计说明。"""
        self._require_live_task(task_id)
        self._conn.execute("UPDATE task SET root_id = NULL WHERE id = ?", (task_id,))
        self.record(task_id, TouchKind.LINK, note="unlink", ts=ts)
        self._conn.commit()

    def _require_live_task(self, task_id: int) -> Task:
        """取存活任务，否则抛出面向用户的 ValueError。"""
        row = self._conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"任务 #{task_id} 不存在")
        task = self._row_to_task(row)
        if task.state is State.COLLECTED:
            raise ValueError(f"任务 #{task_id} 已回收，请先 restore")
        return task

    # ---- roots 与"理由"边(方案 A) ----

    def set_root(self, task_id: int, is_root: bool = True) -> None:
        """把某任务标记 / 取消标记为活跃 GC root(声明的目标)。

        Args:
            task_id (int): 目标任务 id。
            is_root (bool): True 标记为 root，False 取消。

        Returns:
            None
        """
        self._require_live_task(task_id)
        self._conn.execute("UPDATE task SET is_root = ? WHERE id = ?", (int(is_root), task_id))
        self._conn.commit()

    def root_ids(self) -> set[int]:
        """取当前所有活跃 GC root 的 id 集合(判活标记的起点)。

        Returns:
            set[int]: is_root 为真的任务 id 集合(可能为空)。
        """
        rows = self._conn.execute("SELECT id FROM task WHERE is_root = 1").fetchall()
        return {int(r["id"]) for r in rows}

    def add_dep(self, task_id: int, ref_id: int) -> None:
        """新增一条"理由"边：task_id 的理由 / 前置是 ref_id(即 task_id 引用 ref_id)。

        幂等(重复添加同一条边不报错)。这类 task→task 边是循环引用检测的输入。

        Args:
            task_id (int): 发出引用的任务 id。
            ref_id (int): 被引用(作为理由 / 前置)的任务 id。

        Returns:
            None

        Raises:
            ValueError: 当 task_id == ref_id(自环)，或 task_id / ref_id 指向不存在的任务时。
        """
        if task_id == ref_id:
            raise ValueError("一个任务不能把自己当理由(不允许自环)")
        self._require_live_task(task_id)
        self._require_live_task(ref_id)
        self._conn.execute(
            "INSERT OR IGNORE INTO dep (task_id, ref_id) VALUES (?, ?)", (task_id, ref_id)
        )
        self._conn.commit()

    def remove_dep(self, task_id: int, ref_id: int) -> None:
        """删除一条"理由"边(不存在则无操作)。

        Args:
            task_id (int): 发出引用的任务 id。
            ref_id (int): 被引用的任务 id。

        Returns:
            None
        """
        self._conn.execute("DELETE FROM dep WHERE task_id = ? AND ref_id = ?", (task_id, ref_id))
        self._conn.commit()

    def deps(self) -> list[tuple[int, int]]:
        """取全部"理由"边，供 collector 组装引用图。

        Returns:
            list[tuple[int, int]]: (task_id, ref_id) 列表；每项表示 task_id 引用 ref_id。
        """
        rows = self._conn.execute(
            "SELECT task_id, ref_id FROM dep ORDER BY task_id, ref_id"
        ).fetchall()
        return [(int(r["task_id"]), int(r["ref_id"])) for r in rows]

    def heap_size(self) -> int:
        """当前占用活跃堆的任务数：非 COLLECTED、且非 Metaspace 元任务。

        对应 -Xmx 的口径——Metaspace(元任务)在 JVM 里本就不算进 -Xmx，故排除。

        Returns:
            int: 活跃堆上的任务数。
        """
        row = self._conn.execute(
            "SELECT count(*) FROM task WHERE state != ? AND gen != ?",
            (State.COLLECTED.value, Generation.META.value),
        ).fetchone()
        return int(row[0])

    # ---- 读取(默认带 view 自动埋点) ----

    def get_task(self, task_id: int, *, record_view: bool = True, ts: datetime | None = None) -> Task | None:
        """按 id 读取任务；默认顺手记一条 VIEW 事件(工具在"看着你看")。

        Args:
            task_id (int): 任务 id。
            record_view (bool): 是否记 VIEW 埋点；collector 等内部扫描应传 False，
                以免污染事件流、虚增查看次数。
            ts (datetime | None): VIEW 事件时间；None 用当前 UTC。

        Returns:
            Task | None: 命中则返回 Task；不存在时返回 None(且不记 VIEW)。
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
        record_view: bool = True,
        ts: datetime | None = None,
    ) -> list[Task]:
        """列出任务(可按状态过滤)；默认为每个被列出的任务各记一条 VIEW 事件。

        Args:
            state (State | None): 仅返回该状态的任务；None 返回全部。
            record_view (bool): 是否为列出的任务记 VIEW 埋点；内部扫描应传 False。
            ts (datetime | None): VIEW 事件时间；None 用当前 UTC。

        Returns:
            list[Task]: 按 id 升序的任务列表(可能为空)。
        """
        if state is None:
            rows = self._conn.execute("SELECT * FROM task ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM task WHERE state = ? ORDER BY id", (state.value,)
            ).fetchall()
        tasks = [self._row_to_task(r) for r in rows]
        if record_view and tasks:
            iso = self._to_iso(ts or self._now())
            self._conn.executemany(
                "INSERT INTO touch (task_id, kind, ts, note) VALUES (?, ?, ?, NULL)",
                [(t.id, TouchKind.VIEW.value, iso) for t in tasks],
            )
            self._conn.commit()
        return tasks

    def touches(self, task_id: int) -> list[Touch]:
        """取某任务的全部触碰事件，按时间升序(判活 / GC 日志的原始素材)。

        Args:
            task_id (int): 任务 id。

        Returns:
            list[Touch]: 该任务的触碰事件列表(可能为空)。
        """
        rows = self._conn.execute(
            "SELECT * FROM touch WHERE task_id = ? ORDER BY ts, id", (task_id,)
        ).fetchall()
        return [self._row_to_touch(r) for r in rows]

    # ---- 判活指标(纯查询，不落库) ----

    def _count_touches(
        self,
        task_id: int,
        kinds: tuple[str, ...],
        *,
        within_days: int | None = None,
        now: datetime | None = None,
    ) -> int:
        """统计某任务在指定类型(和可选时间窗)内的触碰次数。

        Args:
            task_id (int): 任务 id。
            kinds (tuple[str, ...]): 事件类型值集合(TouchKind.value)。
            within_days (int | None): 仅统计最近 N 天；None 表示不限时间。
            now (datetime | None): 时间窗的"现在"基准；None 用当前 UTC。

        Returns:
            int: 满足条件的触碰事件数量。
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
        """任务的引用计数 = 真推进(PROGRESS/EDIT)次数；可限定最近 N 天。

        Args:
            task_id (int): 任务 id。
            within_days (int | None): 仅统计最近 N 天(判活默认用 30)；None 表示不限。
            now (datetime | None): 时间窗基准；None 用当前 UTC。

        Returns:
            int: refcount 值。
        """
        return self._count_touches(task_id, _PROGRESS_KIND_VALUES, within_days=within_days, now=now)

    def skip_count(self, task_id: int) -> int:
        """任务被推迟(DEFER)的累计次数。

        Args:
            task_id (int): 任务 id。

        Returns:
            int: skip_count 值。
        """
        return self._count_touches(task_id, (TouchKind.DEFER.value,))

    def consecutive_skip_count(self, task_id: int) -> int:
        """最后一次推进/编辑/复活/恢复之后的连续 DEFER 数。"""
        reset = {
            TouchKind.PROGRESS, TouchKind.EDIT, TouchKind.REVIVE, TouchKind.RESTORE,
        }
        count = 0
        for touch in reversed(self.touches(task_id)):
            if touch.kind in reset:
                break
            if touch.kind is TouchKind.DEFER:
                count += 1
            elif touch.kind not in {TouchKind.VIEW, TouchKind.LINK, TouchKind.CREATE}:
                break
        return count

    def view_count(self, task_id: int) -> int:
        """任务被查看(VIEW)的累计次数——通常远大于 refcount，是打脸金句的弹药。

        Args:
            task_id (int): 任务 id。

        Returns:
            int: view_count 值。
        """
        return self._count_touches(task_id, (TouchKind.VIEW.value,))

    def last_touch_at(self, task_id: int, *, progress_only: bool = False) -> datetime | None:
        """任务最后一次被触碰的时间。

        Args:
            task_id (int): 任务 id。
            progress_only (bool): True 时只看真推进(PROGRESS/EDIT)，用于判活的"最近碰过"。

        Returns:
            datetime | None: 最后触碰时间；无相应事件时返回 None。
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

    # ---- GC 审计历史 ----

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
        """记录一次真正执行的 GC；item 为 ``(task_id, reason, action)``。"""
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
        """读取 GC 历史，最近的在前。"""
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

    # ---- 定时体检(watch)：状态去抖 + 单实例建议性锁 ----

    def watch_states(self) -> dict[int, WatchState]:
        """读取全部任务上一轮 watch tick 记录的状态，供本轮 diff 出"跃迁"用。

        Returns:
            dict[int, WatchState]: task_id → 上一轮状态；从未跑过 watch 的任务不在其中。
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
        """记录本轮 tick 里某任务的判定状态；仅当本轮确实为它推送过事件才顺带盖去抖时间戳。

        Args:
            task_id (int): 任务 id。
            status (str): 本轮判定状态("alive" 或 collector.Reason 的值)。
            notified_at (datetime | None): 本轮为该任务发过事件推送则传推送时间；
                否则传 None，保留上一次的 notified_at 不变(不代表"未推送过"要清零)。

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
        """读取一条 watch_meta 键值(如 last_digest_at)。

        Args:
            key (str): 键名。

        Returns:
            str | None: 对应值；不存在时 None。
        """
        row = self._conn.execute("SELECT value FROM watch_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else None

    def watch_meta_set(self, key: str, value: str) -> None:
        """写入(新增或覆盖)一条 watch_meta 键值(如 last_digest_at)。

        Args:
            key (str): 键名。
            value (str): 值。

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
        """尝试拿到"本进程是这个库唯一 watcher"的建议性锁(不是数据库级互斥锁，只是约定)。

        没有锁记录、或已有锁的心跳早于 ``stale_after``(判定持锁进程已死)时，本进程接管成功；
        若已有别的、心跳仍新鲜的 watcher 持锁，返回 False，调用方应放弃启动、避免重复推送。

        Args:
            pid (int): 本进程 pid。
            now (datetime): 当前时间。
            stale_after (timedelta): 心跳早于此时长即判定锁已过期，允许被接管。

        Returns:
            bool: True 表示已拿到(或续期)锁；False 表示另一个 watcher 仍存活，本进程应放弃。
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
        """续期本进程持有的 watch 锁(仅当锁确实属于本 pid 时才生效)。

        Args:
            pid (int): 本进程 pid。
            now (datetime): 当前时间。

        Returns:
            None
        """
        owner_raw = self.watch_meta_get("watch_lock_pid")
        if owner_raw is not None and int(owner_raw) == pid:
            self.watch_meta_set("watch_lock_heartbeat_at", self._to_iso(now))

    def release_watch_lock(self, pid: int) -> None:
        """主动释放本进程持有的 watch 锁(仅当锁确实属于本 pid 时才生效)，供优雅退出时调用。

        Args:
            pid (int): 本进程 pid。

        Returns:
            None
        """
        owner_raw = self.watch_meta_get("watch_lock_pid")
        if owner_raw is not None and int(owner_raw) == pid:
            self._conn.execute(
                "DELETE FROM watch_meta WHERE key IN ('watch_lock_pid', 'watch_lock_heartbeat_at')"
            )
            self._conn.commit()

# store.py —— 持久化层：用 SQLite 落地 Task 与 Touch；读取任务时自动埋点 view 事件，让"触碰历史"这份判活地基自己长出来。

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from todogc.heap import Generation, State, Task, Touch, TouchKind

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


class Store:
    """TodoGC 的持久化门面：包一层 SQLite，对外只暴露 Task / Touch 语义，隐藏 SQL 细节。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        """打开(或新建)一个 TodoGC 数据库并建好表结构。

        Args:
            path (str | Path): 数据库文件路径；默认 ":memory:" 用内存库(测试友好)。

        Returns:
            None
        """
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

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

    def _init_schema(self) -> None:
        """执行建表语句(幂等，IF NOT EXISTS)。

        Returns:
            None
        """
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

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
        now = ts or self._now()
        iso = self._to_iso(now)
        gen = Generation.META if meta else Generation.EDEN
        cur = self._conn.execute(
            "INSERT INTO task (title, root_id, is_root, gen, age, state, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
            (title, root_id, int(is_root), gen.value, State.LIVE.value, iso),
        )
        task_id = int(cur.lastrowid)
        self.record(task_id, TouchKind.CREATE, ts=now)
        self._conn.commit()
        return Task(
            id=task_id,
            title=title,
            root_id=root_id,
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
        return Touch(id=int(cur.lastrowid), task_id=task_id, kind=kind, ts=self._from_iso(iso), note=note)

    def progress(self, task_id: int, note: str | None = None, ts: datetime | None = None) -> Touch:
        """标记"我真的推进了这个任务"——记一条 PROGRESS 事件(计入 refcount)。

        Args:
            task_id (int): 目标任务 id。
            note (str | None): 进展说明。
            ts (datetime | None): 事件时间；None 用当前 UTC。

        Returns:
            Touch: 新记录的触碰事件。
        """
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
            "UPDATE task SET title = ?, root_id = ?, gen = ?, age = ?, state = ? WHERE id = ?",
            (task.title, task.root_id, task.gen.value, task.age, task.state.value, task.id),
        )
        self._conn.commit()

    # ---- roots 与"理由"边(方案 A) ----

    def set_root(self, task_id: int, is_root: bool = True) -> None:
        """把某任务标记 / 取消标记为活跃 GC root(声明的目标)。

        Args:
            task_id (int): 目标任务 id。
            is_root (bool): True 标记为 root，False 取消。

        Returns:
            None
        """
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
        for tid in (task_id, ref_id):
            if self._conn.execute("SELECT 1 FROM task WHERE id = ?", (tid,)).fetchone() is None:
                raise ValueError(f"任务 #{tid} 不存在")
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

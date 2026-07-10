# heap.py —— 领域模型：把一条待办建模成 GC 堆里的一个对象(Task)，把每次交互建模成一次触碰事件(Touch)。

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

# 对象年龄晋升老年代的阈值。取 15 —— 真实 JVM 里对象年龄存于对象头 mark word，
# 只有 4 bit，最多存到 15，故 -XX:MaxTenuringThreshold 默认即 15。自欺满 15 次强制转正。
TENURING_THRESHOLD: int = 15


class TouchKind(str, Enum):
    """一次触碰事件的类型；判活、GC 日志、分代年龄都由这些事件聚合而来。"""

    CREATE = "create"      # 任务被创建(对象分配 / allocation)
    VIEW = "view"          # 你查看了它——被记录，但不算推进
    PROGRESS = "progress"  # 你真正推进了它(算 refcount)
    EDIT = "edit"          # 你编辑了内容(算 refcount)
    DEFER = "defer"        # 你把它往后推(喂 skip_count)
    LINK = "link"          # (重新)关联到某个 GC root
    REVIVE = "revive"      # 在回收仪式里被复活
    COLLECT = "collect"    # 被回收(note 里存遗言)
    RESTORE = "restore"    # 从垃圾桶恢复

    @property
    def is_progress(self) -> bool:
        """该事件是否算作"真推进"(计入 refcount)。

        Returns:
            bool: PROGRESS / EDIT 为 True，其余(含反复 VIEW)为 False。
        """
        return self in _PROGRESS_KINDS


# 计入 refcount 的事件类型：只有主动推进/编辑算数，反复查看不加分。
_PROGRESS_KINDS: frozenset[TouchKind] = frozenset({TouchKind.PROGRESS, TouchKind.EDIT})


class Generation(str, Enum):
    """任务所处的分代，对应 JVM 堆的分代结构。"""

    EDEN = "eden"          # 新分配的对象
    SURVIVOR = "survivor"  # 熬过至少一轮 GC 的幸存者
    OLD = "old"            # 晋升老年代(Tenured)的老赖
    META = "meta"          # Metaspace：关于任务的"元任务"，几乎不回收

    @classmethod
    def for_age(cls, age: int) -> Generation:
        """根据"熬过的 GC 轮数"推出常规分代(不含 META，META 需显式指定)。

        Args:
            age (int): 该任务被列为回收候选却又被复活的累计次数。

        Returns:
            Generation: age <= 0 → EDEN；0 < age < TENURING_THRESHOLD → SURVIVOR；
                age >= TENURING_THRESHOLD → OLD。
        """
        if age <= 0:
            return cls.EDEN
        if age < TENURING_THRESHOLD:
            return cls.SURVIVOR
        return cls.OLD


class State(str, Enum):
    """任务在回收流程中的生命周期状态。"""

    LIVE = "live"            # 存活
    CANDIDATE = "candidate"  # 已被标记为回收候选，等待仪式处置
    COLLECTED = "collected"  # 已回收，躺在垃圾桶里(可恢复)


@dataclass
class Touch:
    """一次触碰事件：判活的唯一真相来源，只追加、不修改。

    Attributes:
        task_id (int): 所属任务的 id。
        kind (TouchKind): 事件类型。
        ts (datetime): 事件发生时间(建议统一存 UTC)。
        note (str | None): 附加文本——进展说明 / 推迟理由 / 遗言。
        id (int | None): 主键；尚未落库时为 None。
    """

    task_id: int
    kind: TouchKind
    ts: datetime
    note: str | None = None
    id: int | None = None

    @property
    def is_progress(self) -> bool:
        """该次触碰是否算作真推进。

        Returns:
            bool: 委托给 self.kind.is_progress 的结果。
        """
        return self.kind.is_progress


@dataclass
class Task:
    """一条待办 = GC 堆里的一个对象。

    Attributes:
        title (str): 任务标题。
        root_id (int | None): 关联到的 GC root(活跃目标)id；None 表示未关联，天然回收候选。
        gen (Generation): 当前分代。
        age (int): 熬过的 GC 轮数，用于分代晋升。
        state (State): 生命周期状态。
        created_at (datetime | None): 创建时间；由 store 在落库时写入。
        id (int | None): 主键；尚未落库时为 None。
    """

    title: str
    root_id: int | None = None
    gen: Generation = Generation.EDEN
    age: int = 0
    state: State = State.LIVE
    created_at: datetime | None = None
    id: int | None = None

    @property
    def is_unlinked(self) -> bool:
        """是否未关联任何 GC root(未关联即天然的回收候选)。

        Returns:
            bool: root_id 为 None 时返回 True。
        """
        return self.root_id is None

    @property
    def is_meta(self) -> bool:
        """是否为常驻 Metaspace 的元任务。

        Returns:
            bool: gen 为 Generation.META 时返回 True。
        """
        return self.gen is Generation.META

    def aged(self) -> Task:
        """产出"又熬过一轮 GC"后的新任务：age +1，并据此更新常规分代。

        元任务(META)不参与常规晋升，返回一个原值副本。不就地修改而是返回新对象，
        以保持 dataclass 的值语义，便于测试与审计。

        Returns:
            Task: age 递增、gen 相应晋升后的新 Task 实例。
        """
        if self.is_meta:
            return replace(self)
        new_age = self.age + 1
        return replace(self, age=new_age, gen=Generation.for_age(new_age))

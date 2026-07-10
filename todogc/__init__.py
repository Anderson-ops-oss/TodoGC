# __init__.py —— todogc 包入口：把领域模型、存储、回收器聚合成一个干净的公共 API。

from __future__ import annotations

from todogc.collector import (
    Candidate,
    Collector,
    Cycle,
    GCPolicy,
    GCResult,
    Reason,
    TaskMetrics,
)
from todogc.heap import TENURING_THRESHOLD, Generation, State, Task, Touch, TouchKind
from todogc.store import Store

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # heap
    "Task",
    "Touch",
    "TouchKind",
    "Generation",
    "State",
    "TENURING_THRESHOLD",
    # store
    "Store",
    # collector
    "Collector",
    "GCPolicy",
    "GCResult",
    "Candidate",
    "Cycle",
    "Reason",
    "TaskMetrics",
]

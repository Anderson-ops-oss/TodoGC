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
from todogc.config import AppConfig, ConfigError, load_config
from todogc.heap import (
    TENURING_THRESHOLD,
    Generation,
    ReferenceStrength,
    State,
    Task,
    Touch,
    TouchKind,
)
from todogc.notify import NotifyError, NtfyChannel
from todogc.store import GCRun, Store
from todogc.watcher import TickEvent, TickResult, Watcher, run_tick

__version__ = "0.3.0"

__all__ = [
    "__version__",
    # heap
    "Task",
    "Touch",
    "TouchKind",
    "Generation",
    "ReferenceStrength",
    "State",
    "TENURING_THRESHOLD",
    # store
    "Store",
    "GCRun",
    # config
    "AppConfig",
    "ConfigError",
    "load_config",
    # collector
    "Collector",
    "GCPolicy",
    "GCResult",
    "Candidate",
    "Cycle",
    "Reason",
    "TaskMetrics",
    # notify
    "NotifyError",
    "NtfyChannel",
    # watcher
    "Watcher",
    "TickEvent",
    "TickResult",
    "run_tick",
]

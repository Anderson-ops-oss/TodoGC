# __init__.py — package entry point: aggregates the domain model, store, and collector into one clean public API.

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
from todogc.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, get_locale, n_, set_locale, t
from todogc.notify import NotifyError, NtfyChannel
from todogc.store import GCRun, Store
from todogc.query import TaskQuery, query_tasks
from todogc.watcher import TickEvent, TickResult, Watcher, run_tick

__version__ = "0.10.0"

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
    "TaskQuery",
    "query_tasks",
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
    # i18n
    "t",
    "n_",
    "set_locale",
    "get_locale",
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    # notify
    "NotifyError",
    "NtfyChannel",
    # watcher
    "Watcher",
    "TickEvent",
    "TickResult",
    "run_tick",
]

"""TodoGC 配置：零依赖读取项目级 ``todogc.toml``，并合并环境变量。
Python 3.11+ 使用标准库 tomllib；Python 3.10 使用一个只覆盖本项目键值的严格后备解析器，
从而保持核心零依赖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib  # pyright: ignore[reportMissingImports] - pyrightconfig.json 钉死 3.10，3.10 下本就没有
except ImportError:  # pragma: no cover - 当前 CI 的 3.10 job 覆盖
    tomllib = None


class ConfigError(ValueError):
    """配置文件或环境变量无效。"""


@dataclass(frozen=True)
class AppConfig:
    """CLI 与 Web 共用的运行配置。"""

    heap_max: int = 50
    gc_mode: str = "normal"
    recent_days: int = 30
    max_consecutive_skips: int = 5
    restore_grace_days: int = 7

    # [notify]：定时体检(todogc watch)的 ntfy 推送目标；ntfy_enabled 为 False 时 watch 只算不推。
    ntfy_enabled: bool = False
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str | None = None

    # [watch]：定时体检的节奏(秒/天为单位，对应设计里的"事件驱动 + 心跳兜底 + 去抖")。
    watch_poll_seconds: int = 1800  # 30 分钟轮训
    watch_digest_seconds: int = 86400 # 1 天汇总
    watch_remind_days: int = 7 
    watch_heap_threshold_pct: int = 80
    watch_lock_stale_seconds: int = 300


def _scalar(raw: str) -> object:
    """解析后备 TOML 所需的字符串、整数和布尔标量。"""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return raw[1:-1]
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"不支持的配置值：{raw}") from exc


def _fallback_toml(text: str) -> dict[str, dict[str, object]]:
    """解析本项目使用的简单 ``[section] key=value`` TOML 子集。"""
    out: dict[str, dict[str, object]] = {}
    section: str | None = None
    for lineno, original in enumerate(text.splitlines(), 1):
        line = original.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section:
                raise ConfigError(f"第 {lineno} 行：空 section")
            out.setdefault(section, {})
            continue
        if section is None or "=" not in line:
            raise ConfigError(f"第 {lineno} 行：配置格式无效")
        key, raw = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"第 {lineno} 行：空配置键")
        out[section][key] = _scalar(raw)
    return out


def _positive(value: Any, name: str) -> int:
    """把配置值校验为正整数；bool 不作为 int 接受。"""
    if isinstance(value, bool):
        raise ConfigError(f"{name} 必须是正整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是正整数") from exc
    if number <= 0:
        raise ConfigError(f"{name} 必须大于 0")
    return number


def _flag(value: Any, name: str) -> bool:
    """把配置值统一解析为 bool——真实 TOML 给的是原生 bool，环境变量给的是字符串。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "1", "yes", "on"}:
            return True
        if low in {"false", "0", "no", "off"}:
            return False
    raise ConfigError(f"{name} 必须是布尔值(true/false)")


def _percent(value: Any, name: str) -> int:
    """把配置值校验为 1~100 的百分比整数。"""
    number = _positive(value, name)
    if number > 100:
        raise ConfigError(f"{name} 必须在 1~100 之间")
    return number


def load_config(path: str | Path | None = None, *, environ: dict[str, str] | None = None) -> AppConfig:
    """读取配置文件并应用环境变量覆盖。

    优先级：环境变量 > 指定/当前目录配置文件 > 默认值。CLI 参数在 ``cli.main`` 再覆盖。
    """
    env = os.environ if environ is None else environ
    config_path = Path(path or env.get("TODOGC_CONFIG", "todogc.toml"))
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw = config_path.read_bytes()
            data = tomllib.loads(raw.decode("utf-8")) if tomllib is not None else _fallback_toml(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(f"无法读取配置 {config_path}: {exc}") from exc

    gc = data.get("gc", {})
    heap = data.get("heap", {})
    notify = data.get("notify", {})
    watch = data.get("watch", {})
    if not isinstance(gc, dict) or not isinstance(heap, dict):
        raise ConfigError("[gc] 和 [heap] 必须是配置 section")
    if not isinstance(notify, dict) or not isinstance(watch, dict):
        raise ConfigError("[notify] 和 [watch] 必须是配置 section")

    mode = env.get("TODOGC_GC_MODE", gc.get("mode", "normal"))
    if mode not in {"gentle", "normal", "aggressive"}:
        raise ConfigError("gc.mode 必须是 gentle / normal / aggressive")

    ntfy_enabled = _flag(env.get("TODOGC_NTFY_ENABLED", notify.get("ntfy_enabled", False)), "notify.ntfy_enabled")
    ntfy_topic = str(env.get("TODOGC_NTFY_TOPIC", notify.get("ntfy_topic", "")))
    if ntfy_enabled and not ntfy_topic:
        raise ConfigError("notify.ntfy_enabled 为 true 时必须配置 notify.ntfy_topic")
    ntfy_token_raw = env.get("TODOGC_NTFY_TOKEN", notify.get("ntfy_token")) or None
    ntfy_token = str(ntfy_token_raw) if ntfy_token_raw else None

    return AppConfig(
        heap_max=_positive(env.get("TODOGC_HEAP_MAX", heap.get("max", 50)), "heap.max"),
        gc_mode=str(mode),
        recent_days=_positive(env.get("TODOGC_RECENT_DAYS", gc.get("recent_days", 30)), "gc.recent_days"),
        max_consecutive_skips=_positive(
            env.get("TODOGC_MAX_SKIPS", gc.get("max_consecutive_skips", 5)),
            "gc.max_consecutive_skips",
        ),
        restore_grace_days=_positive(
            env.get("TODOGC_RESTORE_GRACE_DAYS", gc.get("restore_grace_days", 7)),
            "gc.restore_grace_days",
        ),
        ntfy_enabled=ntfy_enabled,
        ntfy_server=str(env.get("TODOGC_NTFY_SERVER", notify.get("ntfy_server", "https://ntfy.sh"))),
        ntfy_topic=ntfy_topic,
        ntfy_token=ntfy_token,
        watch_poll_seconds=_positive(
            env.get("TODOGC_WATCH_POLL_SECONDS", watch.get("poll_seconds", 1800)), "watch.poll_seconds"
        ),
        watch_digest_seconds=_positive(
            env.get("TODOGC_WATCH_DIGEST_SECONDS", watch.get("digest_seconds", 86400)), "watch.digest_seconds"
        ),
        watch_remind_days=_positive(
            env.get("TODOGC_WATCH_REMIND_DAYS", watch.get("remind_days", 7)), "watch.remind_days"
        ),
        watch_heap_threshold_pct=_percent(
            env.get("TODOGC_WATCH_HEAP_THRESHOLD_PCT", watch.get("heap_threshold_pct", 80)),
            "watch.heap_threshold_pct",
        ),
        watch_lock_stale_seconds=_positive(
            env.get("TODOGC_WATCH_LOCK_STALE_SECONDS", watch.get("lock_stale_seconds", 300)),
            "watch.lock_stale_seconds",
        ),
    )

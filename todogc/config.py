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
    import tomllib
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
    if not isinstance(gc, dict) or not isinstance(heap, dict):
        raise ConfigError("[gc] 和 [heap] 必须是配置 section")

    mode = env.get("TODOGC_GC_MODE", gc.get("mode", "normal"))
    if mode not in {"gentle", "normal", "aggressive"}:
        raise ConfigError("gc.mode 必须是 gentle / normal / aggressive")

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
    )

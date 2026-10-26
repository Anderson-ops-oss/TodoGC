"""Shared, side-effect-free path selection for the CLI, Web UI, and installed skills."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

TARGETS = ("codex", "claude-code", "all")


def skill_directory(target: str, *, destination: str | Path | None = None,
                    environ: Mapping[str, str] | None = None) -> Path:
    if target not in TARGETS[:-1]:
        raise ValueError("Choose codex or claude-code for a single skill directory")
    if destination is not None:
        return Path(destination).expanduser().absolute()
    if target == "codex":
        return Path.home() / ".agents" / "skills" / "todogc"
    env = os.environ if environ is None else environ
    config_home = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    return config_home.absolute() / "skills" / "todogc"


def _check_path(directory: Path, relative: str) -> Path:
    # Never follow a skill or a bundled file replaced with a symlink during an update.
    path = directory
    for part in ("", *PurePosixPath(relative).parts):
        path = path / part if part else path
        if path.is_symlink():
            raise ValueError(f"Refusing to overwrite a symlink: {path}")
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"Skill destination is not a directory: {directory}")
    return path


def read_runtime(directory: Path) -> dict[str, Any] | None:
    path = _check_path(directory, "runtime.json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("managed_by") != "todogc"
                or type(data.get("schema")) is not int or data["schema"] != 1
                or not isinstance(data.get("version"), str)
                or not isinstance(data.get("files"), dict)
                or not all(isinstance(value, str) for value in data["files"].values())):
            return None
        for key in ("python", "database", "config"):
            if (not isinstance(data.get(key), str) or "\0" in data[key]
                    or not Path(data[key]).is_absolute()):
                return None
        return data
    except (ValueError, UnicodeError, RecursionError):
        return None


def resolve_database(value: str | Path | None = None, *,
                     environ: Mapping[str, str] | None = None) -> str:
    from todogc.runtime import resolve_path

    return resolve_path(value, key="database", environ=environ)


def resolve_config(value: str | Path | None = None, *,
                   environ: Mapping[str, str] | None = None) -> str:
    from todogc.runtime import resolve_path

    return resolve_path(value, key="config", environ=environ)


def prepare_database(database: str) -> str:
    """Create the parent only when a task command is about to open its database."""
    from todogc.runtime import ensure_database_parent

    ensure_database_parent(database)
    return database

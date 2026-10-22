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
                or data.get("schema") != 1 or not isinstance(data.get("version"), str)
                or not isinstance(data.get("files"), dict)
                or not all(isinstance(value, str) for value in data["files"].values())):
            return None
        for key in ("python", "database", "config"):
            if not isinstance(data.get(key), str) or not Path(data[key]).is_absolute():
                return None
        return data
    except (ValueError, UnicodeError):
        return None


def _bound_path(key: str, environ: Mapping[str, str]) -> str | None:
    bindings: set[str] = set()
    for target in TARGETS[:-1]:
        directory = skill_directory(target, environ=environ)
        try:
            runtime = read_runtime(directory)
        except ValueError:
            continue  # An unrelated symlink is not a managed runtime.
        if runtime is not None:
            bindings.add(str(Path(runtime[key]).resolve()))
    if len(bindings) > 1:
        option = "--db / TODOGC_DB" if key == "database" else "--config / TODOGC_CONFIG"
        raise ValueError(f"Installed skills use different {key} paths; choose one with {option}.")
    return next(iter(bindings), None)


def _resolve(value: str | Path | None, *, key: str, variable: str, filename: str,
             environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    selected = value if value is not None else env.get(variable)
    if selected is None:
        selected = _bound_path(key, env)
    if selected is None:
        shared = Path.home() / ".todogc" / filename
        local = Path.cwd() / filename
        # Keep legacy local data discoverable, but never let another project hide
        # an established shared database or an installed skill's binding.
        selected = local if not shared.is_file() and local.is_file() else shared
    if str(selected) == ":memory:" and key == "database":
        return ":memory:"
    return str(Path(selected).expanduser().resolve())


def resolve_database(value: str | Path | None = None, *,
                     environ: Mapping[str, str] | None = None) -> str:
    return _resolve(value, key="database", variable="TODOGC_DB", filename="todogc.db", environ=environ)


def resolve_config(value: str | Path | None = None, *,
                   environ: Mapping[str, str] | None = None) -> str:
    return _resolve(value, key="config", variable="TODOGC_CONFIG", filename="todogc.toml", environ=environ)


def prepare_database(database: str) -> str:
    """Create the parent only when a task command is about to open its database."""
    if database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    return database

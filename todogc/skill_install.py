"""Install the bundled, provider-neutral skill without requiring a source checkout."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from typing import Any

from todogc import __version__

TARGETS = ("codex", "claude-code", "all")
BUNDLE_FILES = ("SKILL.md", "references/commands.md", "agents/openai.yaml")


def _assets() -> dict[str, bytes]:
    bundle = files("todogc").joinpath("skills").joinpath("todogc")
    result = {}
    for name in BUNDLE_FILES:
        resource = bundle
        for part in PurePosixPath(name).parts:
            resource = resource.joinpath(part)
        result[name] = resource.read_bytes()
    return result


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def skill_directory(target: str, *, destination: str | Path | None = None) -> Path:
    if target not in TARGETS[:-1]:
        raise ValueError("Choose codex or claude-code for a single skill directory")
    if destination is not None:
        return Path(destination).expanduser().absolute()
    if target == "codex":
        return Path.home() / ".agents" / "skills" / "todogc"
    config_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
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


def _runtime(directory: Path) -> dict[str, Any] | None:
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


def _file_path(value: str | Path) -> str:
    if str(value) == ":memory:":
        raise ValueError("An installed skill needs a persistent database, not :memory:")
    path = Path(value).expanduser().resolve()
    if path.is_dir():
        raise ValueError(f"Expected a file path: {path}")
    return str(path)


def _binding(previous: dict[str, Any] | None, *, database: str | None, config: str | None) -> dict[str, Any]:
    # Preserve explicit bindings on upgrades, including when invoked from another repo.
    if database is None:
        database = str(previous["database"]) if previous else str(
            Path.cwd() / "todogc.db" if (Path.cwd() / "todogc.db").is_file()
            else Path.home() / ".todogc" / "todogc.db")
    if config is None:
        config = str(previous["config"]) if previous else str(
            Path.cwd() / "todogc.toml" if (Path.cwd() / "todogc.toml").is_file()
            else Path.home() / ".todogc" / "todogc.toml")
    return {
        "managed_by": "todogc", "schema": 1, "version": __version__,
        # Preserve the venv/pipx executable path: resolving its symlink loses the environment.
        "python": os.path.abspath(sys.executable),
        "database": _file_path(database), "config": _file_path(config),
    }


def skill_status(target: str, *, destination: str | Path | None = None) -> dict[str, Any]:
    directory = skill_directory(target, destination=destination)
    previous = _runtime(directory)
    result: dict[str, Any] = {"target": target, "path": str(directory), "state": "missing",
                              "invocation": "$todogc" if target == "codex" else "/todogc",
                              "up_to_date": False}
    if not directory.exists():
        return result
    result["state"] = "unmanaged"
    if previous is not None:
        result.update({key: previous[key] for key in ("version", "python", "database", "config")})
        result["state"] = "installed"
        for name, content in _assets().items():
            path = _check_path(directory, name)
            if not path.is_file() or _digest(path.read_bytes()) != previous["files"].get(name):
                result["state"] = "modified"
                break
        result["up_to_date"] = (result["state"] == "installed"
                                and previous.get("version") == __version__
                                and all(previous["files"].get(name) == _digest(content)
                                        for name, content in _assets().items())
                                and previous["python"] == os.path.abspath(sys.executable))
    return result


def _write(path: Path, content: bytes) -> None:
    if path.is_file() and path.read_bytes() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write the complete file before replacement so a reader never sees a partial skill.
    handle, name = tempfile.mkstemp(prefix=".todogc-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def install_skills(target: str, *, database: str | None = None, config: str | None = None,
                   destination: str | Path | None = None, force: bool = False) -> list[dict[str, Any]]:
    if target not in TARGETS:
        raise ValueError(f"Unknown skill target: {target}")
    if target == "all" and destination is not None:
        raise ValueError("--directory requires a single target")
    targets = TARGETS[:-1] if target == "all" else (target,)
    assets = _assets()
    shared_runtime = None
    if destination is None:
        for platform in TARGETS[:-1]:
            try:
                shared_runtime = _runtime(skill_directory(platform))
            except (OSError, ValueError):
                continue  # An unrelated/unmanaged platform must not block installation.
            if shared_runtime is not None:
                break
    plans: list[tuple[str, Path, dict[str, bytes]]] = []
    for platform in targets:
        directory = skill_directory(platform, destination=destination)
        previous = _runtime(directory)
        if directory.exists() and previous is None and any(directory.iterdir()) and not force:
            raise ValueError(f"Skill directory is not managed by TodoGC: {directory}. Use --force to replace bundled files.")
        for name in assets:
            path = _check_path(directory, name)
            if path.exists() and not path.is_file():
                raise ValueError(f"Bundled file path is not a file: {path}")
            if path.exists() and not force:
                current = _digest(path.read_bytes())
                expected = previous["files"].get(name) if previous else None
                if current not in {expected, _digest(assets[name])}:
                    raise ValueError(f"Locally edited skill file: {path}. Use --force to replace bundled files.")
        runtime = _binding(previous or shared_runtime, database=database, config=config)
        runtime["files"] = {name: _digest(content) for name, content in assets.items()}
        payload = dict(assets)
        payload["runtime.json"] = (json.dumps(runtime, indent=2) + "\n").encode("utf-8")
        plans.append((platform, directory, payload))
    # Validate every requested target before starting any writes.
    for _, directory, payload in plans:
        for name, content in payload.items():
            _write(_check_path(directory, name), content)
    return [skill_status(platform, destination=directory) for platform, directory, _ in plans]

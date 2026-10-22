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

from todogc.paths import (
    TARGETS, _check_path, read_runtime as _runtime, resolve_config, resolve_database, skill_directory,
)
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


def _file_path(value: str | Path) -> str:
    if str(value) == ":memory:":
        raise ValueError("An installed skill needs a persistent database, not :memory:")
    path = Path(value).expanduser().resolve()
    if path.is_dir():
        raise ValueError(f"Expected a file path: {path}")
    return str(path)


def _binding(previous: dict[str, Any] | None, *, database: str | None, config: str | None) -> dict[str, Any]:
    # Preserve explicit bindings on upgrades, including when invoked from another repo.
    if database is None and "TODOGC_DB" not in os.environ and previous:
        database = str(previous["database"])
    if config is None and "TODOGC_CONFIG" not in os.environ and previous:
        config = str(previous["config"])
    database = resolve_database(database)
    config = resolve_config(config)
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

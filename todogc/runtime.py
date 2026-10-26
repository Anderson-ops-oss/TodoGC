"""Shared personal runtime bindings and read-only installation diagnostics.

The profile contains paths, never tasks or notification credentials.  Resolving it
does not create directories or open SQLite; only setup writes the profile.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping

from todogc import __version__


class RuntimeConfigError(ValueError):
    """A binding is corrupt or ambiguous; do not silently start another task list."""


@dataclass(frozen=True)
class RuntimePaths:
    database: str
    config: str


def profile_path() -> Path:
    return Path.home() / ".todogc" / "runtime.json"


def _check_profile_path() -> Path:
    path = profile_path()
    if path.parent.is_symlink() or path.is_symlink():
        raise RuntimeConfigError(f"Refusing a symlink runtime profile: {path}")
    if path.parent.exists() and not path.parent.is_dir():
        raise RuntimeConfigError(f"Runtime directory is not a directory: {path.parent}")
    return path


def read_runtime(path: Path) -> dict[str, Any] | None:
    """Read a managed binding without executing its Python or opening its database."""
    if path.is_symlink():
        raise RuntimeConfigError(f"Refusing a symlink runtime profile: {path}")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("managed_by") != "todogc"
                or type(data.get("schema")) is not int or data["schema"] != 1
                or not isinstance(data.get("version"), str)):
            raise ValueError("not a supported TodoGC runtime")
        for key in ("python", "database", "config"):
            value = data.get(key)
            if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
                raise ValueError(f"{key} must be an absolute file path on this OS")
        return data
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise RuntimeConfigError(f"Invalid runtime profile {path}: {exc}. Restore the profile before retrying.") from exc


def _file_path(value: str | Path, *, memory: bool = False) -> str:
    if str(value) == ":memory:" and memory:
        return ":memory:"
    if not str(value) or "\0" in str(value) or str(value) == ":memory:":
        raise RuntimeConfigError("A persistent file path is required")
    try:
        path = Path(value).expanduser().resolve()
        try:
            path.stat()
        except FileNotFoundError:
            pass
    except (OSError, RuntimeError) as exc:
        raise RuntimeConfigError(f"Cannot resolve file path {value}: {exc}") from exc
    if path.is_dir():
        raise RuntimeConfigError(f"Expected a file path: {path}")
    return str(path)


def _legacy_runtimes(env: Mapping[str, str]) -> list[dict[str, Any]]:
    from todogc.skill_install import skill_directory

    candidates = []
    for target in ("codex", "claude-code"):
        directory = (Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser().absolute()
                     / "skills/todogc" if target == "claude-code" else skill_directory(target))
        path = directory / "runtime.json"
        if directory.is_symlink():
            raise RuntimeConfigError(f"Refusing a symlink skill directory: {directory}")
        runtime = read_runtime(path)
        if runtime is not None:
            candidates.append(runtime)
    return candidates


def resolve_paths(database: str | Path | None = None, config: str | Path | None = None, *,
                  environ: Mapping[str, str] | None = None,
                  previous: Mapping[str, Any] | None = None,
                  inspect_skills: bool = True) -> RuntimePaths:
    """CLI > environment > personal profile > old skill > legacy cwd > personal defaults.

    Existing directory-local files are a compatibility fallback only until setup
    registers a personal binding. Explicit paths always allow a different list.
    """
    env = os.environ if environ is None else environ
    chosen = {"database": database if database is not None else env.get("TODOGC_DB"),
              "config": config if config is not None else env.get("TODOGC_CONFIG")}
    candidates: list[Mapping[str, Any]] = []
    if any(value is None for value in chosen.values()):
        profile = read_runtime(_check_profile_path())
        if profile is not None:
            candidates = [profile]
        elif previous is not None:
            candidates = [previous]
        elif inspect_skills:
            candidates = [*_legacy_runtimes(env)]
    for key, filename in (("database", "todogc.db"), ("config", "todogc.toml")):
        if chosen[key] is None:
            values = {str(runtime[key]) for runtime in candidates}
            if len(values) > 1:
                option = "--db" if key == "database" else "--config"
                raise RuntimeConfigError(f"Conflicting skill {key} bindings. Choose {option} explicitly and run setup.")
            chosen[key] = next(iter(values)) if values else (
                Path.cwd() / filename if (Path.cwd() / filename).is_file()
                else Path.home() / ".todogc" / filename)
    return RuntimePaths(_file_path(str(chosen["database"]), memory=True),
                        _file_path(str(chosen["config"])))


def setup_runtime(database: str | None = None, config: str | None = None) -> dict[str, Any]:
    """Register a stable user binding, preserving existing choices on upgrades."""
    path = _check_profile_path()
    read_runtime(path)  # Even explicit paths must not overwrite an unrelated/broken file.
    paths = resolve_paths(database, config)
    if paths.database == ":memory:":
        raise RuntimeConfigError("Setup requires a persistent database, not :memory:")
    runtime = {"managed_by": "todogc", "schema": 1, "version": __version__,
               "python": os.path.abspath(sys.executable),
               "database": paths.database, "config": paths.config}
    content = (json.dumps(runtime, indent=2) + "\n").encode("utf-8")
    if not path.is_file() or path.read_bytes() != content:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as output:
                output.write(content)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    return {**runtime, "path": str(path)}


def ensure_database_parent(database: str) -> None:
    """Create the selected data directory only when a task command will use it."""
    if database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)


def doctor(database: str | None = None, config: str | None = None) -> dict[str, Any]:
    """Diagnose the current process and PATH without running an unknown launcher."""
    from todogc.skill_install import skill_status

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": "" if ok else fix})

    result: dict[str, Any] = {"version": __version__, "python": os.path.abspath(sys.executable),
                              "profile": str(profile_path()), "database": None, "config": None}
    try:
        profile = read_runtime(_check_profile_path())
        check("profile", profile is not None, str(profile_path()), "Run todogc setup using the persistent installation.")
        if profile is not None:
            check("profile_version", profile["version"] == __version__, profile["version"],
                  "Run todogc setup after upgrading the persistent installation.")
            check("profile_python", profile["python"] == os.path.abspath(sys.executable), profile["python"],
                  "Run the registered TodoGC installation, or run setup from the intended persistent installation.")
        paths = resolve_paths(database, config)
        result.update(database=paths.database, config=paths.config)
        check("data_paths", True, f"database={paths.database}; config={paths.config}")
        from todogc.config import load_config

        try:
            load_config(paths.config)
            check("configuration", True, "Configuration is valid (an absent file uses defaults).")
        except (OSError, ValueError) as exc:
            check("configuration", False, str(exc), "Fix the selected configuration or select --config explicitly.")
    except (OSError, ValueError) as exc:
        check("profile", False, str(exc), "Restore the profile or select unambiguous paths with --db and --config.")

    command = shutil.which("todogc")
    result["command"] = command
    expected = Path(sys.executable).parent / ("todogc.exe" if os.name == "nt" else "todogc")
    try:
        matches = command is not None and expected.is_file() and (
            Path(command).samefile(expected)
            or (os.name == "nt" and Path(command).read_bytes() == expected.read_bytes()))
    except OSError:
        matches = False
    check("command", matches, f"PATH: {command or 'not found'}; expected: {expected}",
          "Run the official installer to configure the tool directory on your user PATH, then open a new terminal. Resolve any earlier conflicting command.")

    # -I excludes the checkout and PYTHONPATH: importing only from the current cwd
    # is not proof that the installed command works from an ordinary terminal.
    try:
        probe = subprocess.run([sys.executable, "-I", "-B", "-c",
                                "import json,todogc,todogc.cli;print(json.dumps(todogc.__version__))"],
                               capture_output=True, text=True, timeout=15, check=False)
        imported = json.loads(probe.stdout) if probe.returncode == 0 else None
        check("installed_package", imported == __version__,
              f"isolated import version: {imported}" if imported else "TodoGC cannot be imported outside the checkout.",
              "Reinstall TodoGC with uv tool install or pipx in a persistent environment.")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        check("installed_package", False, str(exc), "Repair the persistent TodoGC installation.")

    for target in ("codex", "claude-code"):
        try:
            status = skill_status(target)
            if status["state"] == "missing":
                continue  # The CLI does not require either agent to be installed.
            matches = (status["up_to_date"] and status.get("database") == result["database"]
                       and status.get("config") == result["config"])
            check(f"skill_{target}", bool(matches), json.dumps(status),
                  f"Run todogc skill install --target {target} from the intended installation, preserving the selected database.")
        except (OSError, ValueError) as exc:
            check(f"skill_{target}", False, str(exc), f"Repair the {target} skill binding and reinstall it.")
    result["checks"] = checks
    result["ok"] = all(item["ok"] for item in checks)
    result["new_terminal_verified"] = False  # A process cannot verify a future terminal's inherited environment.
    return result

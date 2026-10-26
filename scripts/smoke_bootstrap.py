"""Test the real installer against a wheel in an isolated, persistent tool environment.

Usage: python scripts/smoke_bootstrap.py path/to/todogc.whl
Requires uv on PATH. All tool/data/shell paths are temporary. On Windows this
must run on disposable GitHub Actions runners (User PATH is saved and restored).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def smoke(wheel: Path) -> None:
    if os.name == "nt":
        from smoke_windows_installer import smoke as smoke_windows
        smoke_windows(wheel)
        return
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("Install uv before running the bootstrap integration test")
    wheel = wheel.resolve(strict=True)
    installer = Path(__file__).resolve().parents[1] / "install.sh"
    user_shell = "/bin/zsh" if sys.platform == "darwin" else "/bin/bash"
    with tempfile.TemporaryDirectory(prefix="todogc native installer ; literal ") as temporary:
        root = Path(temporary).resolve()
        home = root / "user home"
        home.mkdir()
        commands = root / "manager commands"
        commands.mkdir()
        (commands / "uv").symlink_to(uv)
        # Keep CI's editable todogc entry point out of the installer's PATH while
        # retaining an available compatible base interpreter for offline install.
        (commands / "python3").symlink_to(getattr(sys, "_base_executable", sys.executable))
        other = root / "unrelated project"
        other.mkdir()
        sentinel = b"Do not read or migrate this project database"
        (other / "todogc.db").write_bytes(sentinel)
        (other / "todogc.toml").write_text("Invalid unrelated project settings")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("UV_", "PIPX_", "TODOGC_", "XDG_"))
               and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "BASH_ENV", "ENV", "ZDOTDIR"}}
        env.update(HOME=str(home), SHELL=user_shell, ZDOTDIR=str(home),
                   UV_TOOL_DIR=str(root / "persistent tools"),
                   UV_TOOL_BIN_DIR=str(root / "user bin"),
                   UV_CACHE_DIR=str(root / "cache"), UV_OFFLINE="1",
                   XDG_DATA_HOME=str(root / "xdg data"), XDG_CONFIG_HOME=str(root / "xdg config"),
                   PATH=str(commands) + ":/usr/bin:/bin:/usr/sbin:/sbin")

        def run(args: list[str], *, cwd: Path = home, environment: dict[str, str] | None = None) -> str:
            result = subprocess.run(args, cwd=cwd, env=environment or env, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"Command failed: {args}\n{result.stdout}\n{result.stderr}")
            return result.stdout

        # Both relative wheel paths and a different project directory are intentional.
        run(["/bin/sh", str(installer), "--source", os.path.relpath(wheel, home), "--offline"])
        run(["/bin/sh", str(installer), "--source", os.path.relpath(wheel, other), "--offline"], cwd=other)
        # This process starts without the tool directory on PATH. Only the persisted
        # shell configuration can make the command visible to this new user shell.
        fresh = dict(env)
        fresh["PATH"] = os.defpath
        diagnostics = json.loads(run([user_shell, "-lic", "todogc doctor --runtime --json"], cwd=other, environment=fresh))
        if not diagnostics["ok"]:
            raise ValueError(f"Fresh user shell could not use installed TodoGC: {diagnostics}")
        run([user_shell, "-lic", "todogc capture 'Bootstrap shared list'"], cwd=other, environment=fresh)
        for target in (".agents", ".claude"):
            runtime = json.loads((home / target / "skills" / "todogc" / "runtime.json").read_text())
            output = run([runtime["python"], "-m", "todogc.cli", "--db", runtime["database"],
                          "--config", runtime["config"], "search", "Bootstrap shared list", "--scope", "inbox"], cwd=other)
            if "Bootstrap shared list" not in output:
                raise ValueError("CLI and installed skill did not use the same task database")
        if (other / "todogc.db").read_bytes() != sentinel:
            raise ValueError("Installer touched an unrelated project database")
        if (home / "todogc.db").exists():
            raise ValueError("Installer created a working-directory database")
    print("Native installer: actual offline wheel, spaces/shell characters, repeated install, fresh shell, shared CLI/skills database and doctor passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    smoke(parser.parse_args().wheel)

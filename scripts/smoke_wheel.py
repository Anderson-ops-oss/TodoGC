"""Install a built wheel in isolation and exercise its CLI without a checkout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import venv
from zipfile import ZipFile
from email.parser import BytesParser


def smoke_wheel(wheel: Path) -> None:
    wheel = wheel.resolve(strict=True)
    with ZipFile(wheel) as archive:
        metadata_path = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
        version = str(metadata["Version"])
        if metadata["Name"] != "todogc":
            raise ValueError("Expected a todogc wheel")
        if not any(name.endswith(".dist-info/licenses/LICENSE") for name in archive.namelist()):
            raise ValueError("MIT license is missing from the wheel")
        for name in ("SKILL.md", "references/commands.md", "agents/openai.yaml"):
            if f"todogc/skills/todogc/{name}" not in archive.namelist():
                raise ValueError(f"Bundled skill file is missing from the wheel: {name}")
        if any(name.startswith("todogc/skills/") and name.endswith("runtime.json")
               for name in archive.namelist()):
            raise ValueError("Personal skill runtime settings must not be shipped in the wheel")

    env = {key: value for key, value in os.environ.items()
           if not key.startswith("TODOGC_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="todogc-wheel-") as directory:
        root = Path(directory).resolve()
        home = root / "user home with spaces"
        home.mkdir()
        env.update(HOME=str(home), USERPROFILE=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"),
                   PYTHONUTF8="1")
        venv.EnvBuilder(with_pip=True).create(root / "env")
        executable_dir = root / "env" / ("Scripts" if os.name == "nt" else "bin")
        python = executable_dir / ("python.exe" if os.name == "nt" else "python")
        cli = executable_dir / ("todogc.exe" if os.name == "nt" else "todogc")
        env["PATH"] = str(executable_dir) + os.pathsep + os.environ.get("PATH", "")
        env.pop("VIRTUAL_ENV", None)
        env.pop("CONDA_PREFIX", None)

        def run(*args: str, cwd: Path | None = None) -> str:
            result = subprocess.run(args, cwd=cwd or root, env=env, text=True, capture_output=True,
                                    encoding="utf-8", check=True)
            return result.stdout

        run(str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel))
        installed = json.loads(run(str(python), "-c",
                                  "import json, todogc; print(json.dumps([todogc.__file__, todogc.__version__]))"))
        if not Path(installed[0]).is_relative_to(root / "env") or installed[1] != version:
            raise ValueError("Smoke test imported the checkout or an unexpected version")
        run("todogc", "--help")
        profile = json.loads(run("todogc", "setup", "--json"))
        database = Path(profile["database"])
        if database != home / ".todogc" / "todogc.db" or database.exists():
            raise ValueError("Setup did not bind a personal database without creating it")
        run("todogc", "add", "Release smoke test", "--root")
        run(str(cli), "add", "Verify installed commands", "--root-id", "1")
        run(str(cli), "focus", "2")
        run(str(cli), "today")
        run(str(cli), "progress", "2", "Installed CLI works")
        run(str(cli), "done", "2")
        run(str(cli), "tree", "--show-completed")
        run(str(cli), "review", "--export", "review.svg")
        status = json.loads(run(str(cli), "watch", "status", "--json"))
        if status["state"] != "stopped" or not (root / "review.svg").is_file():
            raise ValueError("Installed CLI smoke test failed")
        other_project = root / "another project"
        other_project.mkdir()
        (other_project / "todogc.toml").write_text("Invalid unrelated project configuration")
        (other_project / "todogc.db").write_bytes(b"Unrelated database")
        for platform in ("codex", "claude-code"):
            result = json.loads(run("todogc", "skill", "install", "--target", platform, "--json"))[0]
            directory = Path(result["path"])
            if not result["up_to_date"]:
                raise ValueError(f"Installed skill is not current: {platform}")
            runtime = json.loads((directory / "runtime.json").read_text())
            prefix = [runtime["python"], "-m", "todogc.cli", "--db", runtime["database"],
                      "--config", runtime["config"]]
            title = f"Skill runtime for {platform}"
            run(*prefix, "capture", title, cwd=other_project)
            if title not in run(*prefix, "search", title, "--scope", "inbox", cwd=other_project):
                raise ValueError(f"Installed skill could not use its bound runtime: {platform}")
            if title not in run("todogc", "search", title, "--scope", "inbox", cwd=other_project):
                raise ValueError(f"Terminal CLI did not share the skill's database: {platform}")
        diagnostic = json.loads(run("todogc", "doctor", "--json", cwd=other_project))
        if not diagnostic["ok"] or diagnostic["database"] != str(database):
            raise ValueError(f"Installed runtime diagnostics failed: {diagnostic}")
        if (other_project / "todogc.db").read_bytes() != b"Unrelated database":
            raise ValueError("Skill command touched the wrong database")
    print(f"todogc {version}: isolated install, command-name lookup outside checkout, shared data, both skills, and doctor passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    smoke_wheel(parser.parse_args().wheel)

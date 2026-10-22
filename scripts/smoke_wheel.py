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

    env = {key: value for key, value in os.environ.items()
           if not key.startswith("TODOGC_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="todogc-wheel-") as directory:
        root = Path(directory)
        venv.EnvBuilder(with_pip=True).create(root / "env")
        executable_dir = root / "env" / ("Scripts" if os.name == "nt" else "bin")
        python = executable_dir / ("python.exe" if os.name == "nt" else "python")
        cli = executable_dir / ("todogc.exe" if os.name == "nt" else "todogc")

        def run(*args: str) -> str:
            result = subprocess.run(args, cwd=root, env=env, text=True, capture_output=True, check=True)
            return result.stdout

        run(str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel))
        installed = json.loads(run(str(python), "-c",
                                  "import json, todogc; print(json.dumps([todogc.__file__, todogc.__version__]))"))
        if not Path(installed[0]).is_relative_to(root / "env") or installed[1] != version:
            raise ValueError("Smoke test imported the checkout or an unexpected version")
        run(str(cli), "--help")
        run(str(cli), "add", "Release smoke test", "--root")
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
    print(f"todogc {version}: isolated pip install and CLI smoke test passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    smoke_wheel(parser.parse_args().wheel)

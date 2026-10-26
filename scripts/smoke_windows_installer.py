"""Exercise the actual PowerShell installer in disposable Windows CI.

All installation/data paths live in a temporary home with spaces. The test briefly
adds that tool bin directory to the CI user's User PATH, verifies it, and restores
the exact prior registry value in a PowerShell finally block. Never run it against
a normal user's registry as part of pytest.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


_WINDOWS_CHECK = r'''
$ErrorActionPreference = 'Stop'
$beforeUser = [Environment]::GetEnvironmentVariable('Path', 'User')
$beforeMachine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$beforeProcess = $env:PATH
$beforeVenv = $env:VIRTUAL_ENV
try {
    foreach ($attempt in @(1, 2)) {
        if ($attempt -eq 1) { Set-Location -LiteralPath $env:USERPROFILE }
        else { Set-Location -LiteralPath $env:TEST_OTHER_PROJECT }
        & $env:TEST_INSTALLER -Source $env:TEST_WHEEL -Target all -Offline
        if (-not $?) { throw "Installer failed on attempt $attempt" }
        if ($env:PATH -cne $beforeProcess -or $env:VIRTUAL_ENV -cne $beforeVenv) {
            throw 'Installer changed its caller process environment'
        }
    }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $matches = @($userPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $env:UV_TOOL_BIN_DIR.TrimEnd('\') })
    if ($matches.Count -ne 1) { throw 'Tool directory was not persisted exactly once in User PATH' }
    if ([Environment]::GetEnvironmentVariable('Path', 'Machine') -cne $beforeMachine) {
        throw 'Installer changed Machine PATH'
    }
    if ($beforeUser) {
        foreach ($entry in @($beforeUser -split ';' | Where-Object { $_ })) {
            if ($entry -cnotin ($userPath -split ';')) { throw 'Installer lost an existing User PATH entry' }
        }
    }
    # The agent's active project bin and PYTHONPATH must never reach User PATH.
    if ($userPath.Contains($env:VIRTUAL_ENV) -or $userPath.Contains('untrusted-checkout')) {
        throw 'Installer persisted an agent/project environment'
    }
    # Rebuild only from persisted registry PATH values, then create a fresh shell.
    # Do not inject UV_TOOL_BIN_DIR directly: that would hide a persistence failure.
    $env:PATH = [Environment]::ExpandEnvironmentVariables($beforeMachine + ';' + $userPath)
    $env:VIRTUAL_ENV = $null
    $env:PYTHONPATH = $null
    Set-Location -LiteralPath $env:USERPROFILE
    $childShell = (Get-Process -Id $PID).Path
    & $childShell -NoProfile -NonInteractive -Command "todogc --help | Out-Null; if (`$LASTEXITCODE -ne 0) { exit 1 }; todogc capture 'Bootstrap shared list'; if (`$LASTEXITCODE -ne 0) { exit 1 }; todogc doctor --runtime --json; exit `$LASTEXITCODE"
    if ($LASTEXITCODE -ne 0) { throw 'CLI command name failed in a new shell using persisted PATH' }
    foreach ($provider in @('.agents', '.claude')) {
        $runtime = Get-Content -LiteralPath (Join-Path $env:USERPROFILE "$provider\skills\todogc\runtime.json") -Raw | ConvertFrom-Json
        $found = & $runtime.python -m todogc.cli --db $runtime.database --config $runtime.config search 'Bootstrap shared list' --scope inbox
        if ($LASTEXITCODE -ne 0 -or ($found -join "`n") -notmatch 'Bootstrap shared list') {
            throw 'CLI and installed skill did not use the same task database'
        }
    }
} finally {
    [Environment]::SetEnvironmentVariable('Path', $beforeUser, 'User')
    if ([Environment]::GetEnvironmentVariable('Path', 'User') -cne $beforeUser) {
        throw 'Could not restore the exact original User PATH'
    }
}
'''


def smoke(wheel: Path) -> None:
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This registry-restoring integration check is only for disposable Windows GitHub Actions runners")
    uv = shutil.which("uv")
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not uv or not powershell:
        raise SystemExit("Install uv and PowerShell before running this check")
    wheel = wheel.resolve(strict=True)
    installer = Path(__file__).resolve().parents[1] / "install.ps1"
    with tempfile.TemporaryDirectory(prefix="todogc native installer $literal ") as temporary:
        root = Path(temporary)
        home = root / "user home"
        home.mkdir()
        commands = root / "manager commands"
        commands.mkdir()
        shutil.copy2(uv, commands / "uv.exe")
        other_project = root / "unrelated project"
        other_project.mkdir()
        (other_project / "todogc.db").write_bytes(b"Do not read or migrate this project database")
        (other_project / "todogc.toml").write_text("Invalid unrelated project settings")
        active = root / "agent venv"
        (active / "Scripts").mkdir(parents=True)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("UV_", "PIPX_", "TODOGC_"))
                       and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX"}}
        environment.update(HOME=str(home), USERPROFILE=str(home),
                           UV_TOOL_DIR=str(root / "persistent tools"),
                           UV_TOOL_BIN_DIR=str(root / "user bin"),
                           UV_CACHE_DIR=str(root / "uv cache"), UV_OFFLINE="1",
                           TEST_INSTALLER=str(installer), TEST_WHEEL=str(wheel), TEST_OTHER_PROJECT=str(other_project),
                           VIRTUAL_ENV=str(active), PYTHONPATH="untrusted-checkout")
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        python_directory = Path(getattr(sys, "_base_executable", sys.executable)).parent
        environment["PATH"] = os.pathsep.join(map(str, [active / "Scripts", commands, python_directory,
                                                       system_root / "System32", system_root]))
        harness = root / "check installer.ps1"
        harness.write_text(_WINDOWS_CHECK, encoding="utf-8")
        subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                       env=environment, cwd=other_project, check=True)
        if (other_project / "todogc.db").read_bytes() != b"Do not read or migrate this project database":
            raise ValueError("Installer touched the unrelated project database")
    print("PowerShell installer: actual offline wheel, spaces, repeated install, PATH preservation, command lookup and doctor passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    smoke(parser.parse_args().wheel)

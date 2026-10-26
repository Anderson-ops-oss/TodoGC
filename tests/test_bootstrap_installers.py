"""Exercise installer processes with isolated homes and deterministic tool managers.

The stubs model only the external manager boundary. They record argv/environment,
create actual executable links, and fail on unrecognized commands; no network,
real shell profiles, or actual tool installations are touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]

_MANAGER = r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ["TEST_INSTALL_ROOT"])
manager = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps({"program": manager, "args": args, "cwd": os.getcwd(),
                          "venv": os.environ.get("VIRTUAL_ENV"),
                          "pythonpath": os.environ.get("PYTHONPATH"),
                          "shell_markers": {key: os.environ[key] for key in ("NU_VERSION", "FISH_VERSION", "BASH_VERSION", "ZSH_VERSION", "KSH_VERSION", "PSModulePath") if key in os.environ},
                          "offline": os.environ.get("UV_OFFLINE"), "path": os.environ.get("PATH")}) + "\n")
uv_root, pipx_root, bins = root / "uv tools", root / "pipx tools", root / "user bin"
if manager == "uv" and args == ["tool", "dir"]:
    print(uv_root)
elif manager == "uv" and args == ["tool", "dir", "--bin"]:
    print(bins)
elif manager == "pipx" and args == ["environment", "--value", "PIPX_HOME"]:
    print(pipx_root)
elif manager == "pipx" and args == ["environment", "--value", "PIPX_BIN_DIR"]:
    print(bins)
elif (manager == "uv" and args[:2] in (["tool", "install"], ["tool", "upgrade"])) or (manager == "pipx" and args[:1] in (["install"], ["upgrade"])):
    if os.environ.get("TEST_FAIL") == "install":
        sys.exit(9)
    if manager == "uv":
        directory, metadata = uv_root / "todogc", "uv-receipt.toml"
    else:
        directory, metadata = pipx_root / "venvs" / "todogc", "pipx_metadata.json"
    cli = directory / "bin" / "todogc"
    cli.parent.mkdir(parents=True, exist_ok=True)
    (directory / metadata).write_text("managed by " + manager)
    cli.write_text("#!" + sys.executable + "\n" + os.environ["TEST_CLI_SOURCE"])
    cli.chmod(0o755)
    bins.mkdir(parents=True, exist_ok=True)
    exported = bins / "todogc"
    if not exported.exists():
        exported.symlink_to(cli)
elif (manager == "uv" and args == ["tool", "update-shell"]) or (manager == "pipx" and args == ["ensurepath"]):
    if os.environ.get("TEST_FAIL") == "path":
        sys.exit(8)
    if os.environ.get("TEST_FAIL") == "path-current":
        print("error: executable directory is not in PATH, but the Zsh configuration files are already up-to-date")
        sys.exit(2)
    if os.environ.get("TEST_FAIL") == "path-unrelated":
        print("error: cannot write shell configuration")
        sys.exit(2)
    (root / "fake shell profile").write_text(str(bins))
else:
    raise RuntimeError("Unexpected manager invocation: " + repr(args))
'''

_CLI = r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ["TEST_INSTALL_ROOT"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps({"program": "todogc", "args": args, "cwd": os.getcwd(),
                          "venv": os.environ.get("VIRTUAL_ENV"),
                          "pythonpath": os.environ.get("PYTHONPATH")}) + "\n")
if os.environ.get("TEST_FAIL") in args:
    sys.exit(7)
if "setup" in args or "doctor" in args:
    print('{"ok": true}')
elif "skill" in args:
    print('[]')
elif args != ["--help"]:
    raise RuntimeError("Unexpected CLI invocation: " + repr(args))
'''


@pytest.fixture
def sandbox(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX installer process tests; Windows installer is covered by native CI")
    root = tmp_path / "user space ; literal"
    root.mkdir()
    home = root / "home"
    home.mkdir()
    commands = root / "commands"
    commands.mkdir()
    uv = commands / "uv"
    uv.write_text(f"#!{sys.executable}\n{_MANAGER}")
    uv.chmod(0o755)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("UV_", "PIPX_", "TODOGC_"))
                   and key not in {"VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME", "PYTHONPATH"}}
    environment.update(HOME=str(home), PATH=str(commands) + os.pathsep + os.defpath,
                       TEST_INSTALL_ROOT=str(root), TEST_CLI_SOURCE=_CLI, SHELL="/bin/sh")
    return root, commands, environment


def run_installer(sandbox, *arguments, **changes):
    root, _, environment = sandbox
    return subprocess.run(["/bin/sh", str(ROOT / "install.sh"), *arguments],
                          cwd=root, env={**environment, **changes}, capture_output=True, text=True)


def calls(sandbox):
    path = sandbox[0] / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_persistent_install_handles_spaces_and_verifies_command_outside_project(sandbox):
    result = run_installer(sandbox, "--target", "codex", "--offline")
    assert result.returncode == 0, result.stderr
    rows = calls(sandbox)
    assert any(row["args"] == ["tool", "install", "--python", ">=3.10", "--upgrade", "--", "todogc>=0.14.0"]
               and row["offline"] == "1" for row in rows)
    cli = [row for row in rows if row["program"] == "todogc"]
    assert [row["args"] for row in cli] == [
        ["setup", "--json"], ["skill", "install", "--target", "codex", "--json"],
        ["--help"], ["doctor", "--runtime", "--json"]]
    assert cli[2]["cwd"] == sandbox[2]["HOME"]
    assert "independently opened terminal has not been verified" in result.stdout
    assert (sandbox[0] / "fake shell profile").is_file()


def test_paths_and_source_are_arguments_never_shell_code(sandbox):
    marker = sandbox[0] / "INJECTED"
    hostile = f"local wheel $(touch '{marker}') ; ' \" $HOME.whl"
    database = str(sandbox[0] / "tasks ; $(echo hacked).db")
    config = str(sandbox[0] / "configuration $literal.toml")
    result = run_installer(sandbox, "--source", hostile, "--db", database, "--config", config)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert any(row["args"] == ["tool", "install", "--python", ">=3.10", "--upgrade", "--", hostile] for row in calls(sandbox))
    cli = [row for row in calls(sandbox) if row["program"] == "todogc"]
    assert cli[0]["args"] == ["--db", database, "--config", config, "setup", "--json"]
    assert cli[-1]["args"] == ["--db", database, "--config", config, "doctor", "--runtime", "--json"]


def test_activated_agent_environment_cannot_supply_todogc_or_pythonpath(sandbox):
    root, _, environment = sandbox
    venv = root / "agent venv"
    (venv / "bin").mkdir(parents=True)
    wrong = venv / "bin" / "todogc"
    wrong.write_text("#!/bin/sh\nexit 99\n")
    wrong.chmod(0o755)
    result = run_installer(sandbox, VIRTUAL_ENV=str(venv), PYTHONPATH="untrusted checkout",
                           PATH=str(venv / "bin") + ":" + environment["PATH"])
    assert result.returncode == 0, result.stderr
    assert all(row["venv"] is None and row["pythonpath"] is None for row in calls(sandbox))


def test_reinstall_uses_manager_upgrade_to_preserve_extras(sandbox):
    assert run_installer(sandbox).returncode == 0
    (sandbox[0] / "calls.jsonl").unlink()
    result = run_installer(sandbox)
    assert result.returncode == 0, result.stderr
    rows = calls(sandbox)
    assert any(row["args"] == ["tool", "upgrade", "todogc"] for row in rows)
    assert not any(row["args"][:2] == ["tool", "install"] for row in rows)
    assert not any("--force" in row["args"] for row in rows)


def test_existing_pipx_is_reused_even_when_uv_is_available(sandbox):
    _, commands, _ = sandbox
    pipx = commands / "pipx"
    shutil.copyfile(commands / "uv", pipx)
    pipx.chmod(0o755)
    # Seed a pipx-managed environment through the same external boundary.
    subprocess.run([str(pipx), "install", "todogc[web]"], env=sandbox[2], check=True)
    (sandbox[0] / "calls.jsonl").unlink()
    result = run_installer(sandbox)
    assert result.returncode == 0, result.stderr
    rows = calls(sandbox)
    assert any(row["program"] == "pipx" and row["args"] == ["upgrade", "todogc"] for row in rows)
    assert not any(row["program"] == "uv" and row["args"][:2] in
                   (["tool", "install"], ["tool", "upgrade"]) for row in rows)


def test_two_managers_fail_before_upgrading_or_writing_user_configuration(sandbox):
    root, commands, _ = sandbox
    assert run_installer(sandbox).returncode == 0
    pipx = commands / "pipx"
    shutil.copyfile(commands / "uv", pipx)
    pipx.chmod(0o755)
    folder = root / "pipx tools" / "venvs" / "todogc"
    folder.mkdir(parents=True)
    (folder / "pipx_metadata.json").write_text("existing metadata")
    (root / "calls.jsonl").unlink()
    (root / "fake shell profile").unlink()
    result = run_installer(sandbox)
    assert result.returncode != 0
    assert "Both uv and pipx" in result.stderr
    assert not (root / "fake shell profile").exists()
    assert all(row["program"] != "todogc" for row in calls(sandbox))
    assert not any(row["args"][:2] in (["tool", "install"], ["tool", "upgrade"]) for row in calls(sandbox))


def test_foreign_todogc_is_not_overwritten_or_masked(sandbox):
    root, commands, _ = sandbox
    foreign = commands / "todogc"
    foreign.write_text("#!/bin/sh\nexit 99\n")
    foreign.chmod(0o755)
    before = foreign.read_bytes()
    result = run_installer(sandbox)
    assert result.returncode != 0
    assert "Another todogc" in result.stderr
    assert foreign.read_bytes() == before
    assert not (root / "user bin").exists()
    assert not any(row["args"][:2] == ["tool", "install"] for row in calls(sandbox))


@pytest.mark.parametrize("failure", ["install", "setup", "skill", "doctor", "path", "path-unrelated"])
def test_failure_is_propagated_and_never_reported_as_success(sandbox, failure):
    result = run_installer(sandbox, TEST_FAIL=failure)
    assert result.returncode != 0
    assert "Installed TodoGC using" not in result.stdout


@pytest.mark.parametrize("arguments", [
    ["--target", "unknown"], ["--source", "--force"], ["--source", ""],
    ["--target"], ["--unexpected"],
])
def test_invalid_arguments_fail_without_running_managers(sandbox, arguments):
    result = run_installer(sandbox, *arguments)
    assert result.returncode != 0
    assert calls(sandbox) == []


def test_bin_path_is_retained_without_an_activated_environment(sandbox):
    # Regression: an unset VIRTUAL_ENV must not turn its bin comparison into /bin.
    result = run_installer(sandbox, PATH=str(sandbox[1]) + ":/bin")
    assert result.returncode == 0, result.stderr


def test_powershell_script_parses_with_no_syntax_errors():
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("PowerShell is not installed on this host")
    script = "$errors = $null; $tokens = $null; [void][System.Management.Automation.Language.Parser]::ParseFile($env:TEST_PS_SCRIPT, [ref]$tokens, [ref]$errors); if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }"
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
                            env={**os.environ, "TEST_PS_SCRIPT": str(ROOT / "install.ps1")},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_agent_path_does_not_suppress_persistent_shell_update(sandbox):
    assert run_installer(sandbox).returncode == 0
    (sandbox[0] / "calls.jsonl").unlink()
    bin_dir = str(sandbox[0] / "user bin")
    result = run_installer(sandbox, PATH=bin_dir + ":" + sandbox[2]["PATH"])
    assert result.returncode == 0, result.stderr
    updates = [row for row in calls(sandbox) if row["args"] == ["tool", "update-shell"]]
    assert len(updates) == 1
    assert bin_dir not in updates[0]["path"].split(os.pathsep)


def test_existing_shell_configuration_is_an_idempotent_success(sandbox):
    result = run_installer(sandbox, TEST_FAIL="path-current")
    assert result.returncode == 0, result.stderr
    assert "Shell configuration already contains" in result.stdout


def test_login_shell_is_not_overridden_by_inherited_agent_shell_markers(sandbox):
    result = run_installer(sandbox, SHELL="/bin/bash", PSModulePath="PowerShell modules",
                           NU_VERSION="inherited", FISH_VERSION="inherited", BASH_VERSION="inherited",
                           ZSH_VERSION="inherited", KSH_VERSION="inherited")
    assert result.returncode == 0, result.stderr
    updates = [row for row in calls(sandbox) if row["args"] == ["tool", "update-shell"]]
    assert len(updates) == 1
    assert updates[0]["shell_markers"] == {}


@pytest.mark.parametrize("component", ["$literal", "$(echo injected)", "`echo injected`", 'a"quote', "line\nbreak", "colon:directory", r"back\slash"])
def test_unsafe_shell_persistence_paths_fail_before_profile_or_install_mutation(sandbox, component):
    unsafe = sandbox[0] / component
    unsafe.mkdir()
    result = run_installer(sandbox, TEST_INSTALL_ROOT=str(unsafe))
    assert result.returncode != 0
    assert "cannot safely represent" in result.stderr
    assert not (unsafe / "fake shell profile").exists()
    assert not (unsafe / "user bin").exists()
    rows = [json.loads(line) for line in (unsafe / "calls.jsonl").read_text().splitlines()]
    assert not any(row["args"][:2] == ["tool", "install"] for row in rows)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only installer preflight")
@pytest.mark.parametrize("component", ["semi;colon", "percent%literal", "line\nbreak"])
def test_powershell_unsafe_profile_paths_fail_before_any_installation(tmp_path, component):
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("PowerShell is not installed")
    # PowerShell itself reads USERPROFILE during initialization. In particular,
    # a semicolon can make the host fail before the installer is ever invoked.
    # Start with the valid inherited profile, then inject the hostile value only
    # inside the initialized child process, as data rather than command text.
    environment = {**os.environ, "TEST_UNSAFE_USERPROFILE": str(tmp_path / component),
                   "TEST_PS_SCRIPT": str(ROOT / "install.ps1"), "TEST_ROOT": str(tmp_path)}
    script = r'''
$ErrorActionPreference = 'Stop'
$originalProfile = $env:USERPROFILE
$beforeUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$beforeMachinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
# Snapshot after host initialization, so its own startup cache is outside the
# installer boundary. Use .NET APIs to avoid module-autoload filesystem writes.
[string[]]$beforeFiles = [IO.Directory]::GetFileSystemEntries($env:TEST_ROOT, '*', [IO.SearchOption]::AllDirectories)
[Array]::Sort($beforeFiles, [StringComparer]::Ordinal)
$rejected = $false
try {
    $env:USERPROFILE = $env:TEST_UNSAFE_USERPROFILE
    try {
        & $env:TEST_PS_SCRIPT -Offline
    } catch {
        if ($_.Exception.Message -notlike '*cannot safely represent*') { throw }
        $rejected = $true
    }
} finally {
    $env:USERPROFILE = $originalProfile
}
if (-not $rejected) { throw 'Installer accepted an unsafe USERPROFILE' }
if ([Environment]::GetEnvironmentVariable('Path', 'User') -cne $beforeUserPath -or
    [Environment]::GetEnvironmentVariable('Path', 'Machine') -cne $beforeMachinePath) {
    throw 'Installer changed persistent PATH before rejecting the profile'
}
[string[]]$afterFiles = [IO.Directory]::GetFileSystemEntries($env:TEST_ROOT, '*', [IO.SearchOption]::AllDirectories)
[Array]::Sort($afterFiles, [StringComparer]::Ordinal)
if ([string]::Join("`0", $beforeFiles) -cne [string]::Join("`0", $afterFiles)) {
    throw 'Installer changed the filesystem before rejecting the profile'
}
Write-Output 'Installer rejected unsafe USERPROFILE without changing persistent PATH'
'''
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Installer rejected unsafe USERPROFILE without changing persistent PATH" in result.stdout

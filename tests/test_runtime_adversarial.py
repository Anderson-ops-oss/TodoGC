"""Exercise setup and diagnostics through the public CLI in hostile environments."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

from todogc.cli import main
from todogc.skill_install import install_skills


@pytest.fixture
def isolated_user(tmp_path, monkeypatch):
    home = tmp_path / "user home with spaces"
    work = tmp_path / "first project"
    home.mkdir()
    work.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in list(os.environ):
        if name.startswith("TODOGC_") or name == "CLAUDE_CONFIG_DIR":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(work)
    return home, work


def _setup(capsys, *options):
    assert main([*options, "setup", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["managed_by"] == "todogc"
    assert result["schema"] == 1
    assert Path(result["path"]).is_absolute()
    return result


def _snapshot(directory):
    return {
        str(path.relative_to(directory)): (
            ("symlink", str(path.readlink())) if path.is_symlink()
            else ("file", path.read_bytes()) if path.is_file()
            else ("directory", None)
        )
        for path in directory.rglob("*")
    }


def _doctor(capsys):
    code = main(["doctor", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == (0 if result["ok"] else 1)
    assert isinstance(result["checks"], list)
    assert all({"name", "ok", "detail", "fix"} <= check.keys() for check in result["checks"])
    return result


def _executable(directory, marker):
    directory.mkdir()
    script = directory / ("todogc.cmd" if os.name == "nt" else "todogc")
    if os.name == "nt":
        script.write_text(f'@echo off\n@echo executed>"{marker}"\n', encoding="utf-8")
    else:
        script.write_text(f"#!/bin/sh\nprintf executed > {shlex.quote(str(marker))}\n", encoding="utf-8")
        script.chmod(0o755)
    return script


def test_setup_and_cli_keep_one_database_after_changing_projects(isolated_user, tmp_path, monkeypatch, capsys):
    home, work = isolated_user
    result = _setup(capsys)
    assert result["database"] == str(home / ".todogc" / "todogc.db")
    assert not Path(result["database"]).exists()
    assert main(["capture", "Task from the first terminal"]) == 0
    capsys.readouterr()
    other = tmp_path / "another project with spaces"
    other.mkdir()
    # A profile must win over unrelated local project files, including broken ones.
    (other / "todogc.db").write_bytes(b"unrelated database must remain untouched")
    (other / "todogc.toml").write_text("not valid TOML", encoding="utf-8")
    monkeypatch.chdir(other)
    assert main(["search", "Task from the first terminal", "--scope", "inbox"]) == 0
    assert "Task from the first terminal" in capsys.readouterr().out
    assert (other / "todogc.db").read_bytes() == b"unrelated database must remain untouched"
    assert not (work / "todogc.db").exists()


def test_setup_binds_legacy_files_without_opening_them(isolated_user, capsys):
    _, work = isolated_user
    database, config = work / "todogc.db", work / "todogc.toml"
    database.write_bytes(b"legacy database is deliberately not SQLite")
    config.write_bytes(b"legacy configuration is deliberately not TOML")
    before = _snapshot(work)
    result = _setup(capsys)
    assert result["database"] == str(database)
    assert result["config"] == str(config)
    assert _snapshot(work) == before


def test_repeated_setup_preserves_binding_but_refreshes_python(isolated_user, tmp_path, monkeypatch, capsys):
    first = _setup(capsys)
    profile = Path(first["path"])
    data = json.loads(profile.read_text(encoding="utf-8"))
    data["python"] = str(tmp_path / "removed old environment" / "python")
    profile.write_text(json.dumps(data), encoding="utf-8")
    other = tmp_path / "new project"
    other.mkdir()
    monkeypatch.chdir(other)
    second = _setup(capsys)
    assert second["python"] == os.path.abspath(sys.executable)
    assert second["database"] == first["database"]
    assert second["config"] == first["config"]


def test_explicit_options_and_environment_override_profile_independently(isolated_user, tmp_path, monkeypatch, capsys):
    _setup(capsys)
    env_db = tmp_path / "environment.db"
    env_config = tmp_path / "environment.toml"
    env_config.write_text("[heap]\nmax = 0\n", encoding="utf-8")
    monkeypatch.setenv("TODOGC_DB", str(env_db))
    monkeypatch.setenv("TODOGC_CONFIG", str(env_config))
    assert main(["capture", "invalid configuration must fail"]) == 2
    capsys.readouterr()
    assert not env_db.exists()
    cli_config = tmp_path / "valid.toml"
    cli_config.write_text("[heap]\nmax = 17\n", encoding="utf-8")
    assert main(["--config", str(cli_config), "capture", "Environment database selected"]) == 0
    capsys.readouterr()
    assert env_db.exists()
    cli_db = tmp_path / "explicit database.db"
    assert main(["--db", str(cli_db), "--config", str(cli_config), "capture", "Explicit database selected"]) == 0
    capsys.readouterr()
    assert cli_db.exists()
    assert main(["--db", str(cli_db), "--config", str(cli_config), "search", "Environment database selected", "--scope", "inbox"]) == 0
    assert "Environment database selected" not in capsys.readouterr().out


@pytest.mark.parametrize("mutation", [
    {"schema": 999},
    {"schema": True},
    {"managed_by": "someone else"},
    {"database": "relative.db"},
    {"database": 42},
    {"database": "/invalid\0database"},
    {"config": []},
    {"python": "relative-python"},
])
def test_invalid_profile_is_reported_and_never_overwritten(isolated_user, tmp_path, capsys, mutation):
    home, _ = isolated_user
    result = _setup(capsys)
    profile = Path(result["path"])
    data = json.loads(profile.read_text(encoding="utf-8"))
    data.update(mutation)
    profile.write_text(json.dumps(data), encoding="utf-8")
    before = _snapshot(home)
    assert main(["--db", str(tmp_path / "replacement.db"), "setup", "--json"]) != 0
    capsys.readouterr()
    report = _doctor(capsys)
    assert not report["ok"]
    assert _snapshot(home) == before
    assert not (tmp_path / "replacement.db").exists()


@pytest.mark.parametrize("payload", [
    pytest.param(b"{", id="truncated-json"),
    pytest.param(b"[]", id="wrong-json-type"),
    pytest.param(b"null", id="json-null"),
    pytest.param(b"\xff\xfe", id="invalid-utf8"),
    pytest.param(b"[" * 10000 + b"0" + b"]" * 10000, id="excessive-json-depth"),
])
def test_malformed_profile_does_not_crash_or_write(isolated_user, capsys, payload):
    home, _ = isolated_user
    profile = home / ".todogc" / "runtime.json"
    profile.parent.mkdir()
    profile.write_bytes(payload)
    before = _snapshot(home)
    assert main(["setup", "--json"]) != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert not _doctor(capsys)["ok"]
    assert _snapshot(home) == before


@pytest.mark.parametrize("link_parent", [False, True])
def test_setup_refuses_profile_and_parent_symlinks(isolated_user, tmp_path, capsys, link_parent):
    home, _ = isolated_user
    outside = tmp_path / "outside user configuration"
    outside.mkdir()
    victim = outside / "runtime.json"
    victim.write_text("user file that must survive", encoding="utf-8")
    target = home / ".todogc"
    try:
        if link_parent:
            target.symlink_to(outside, target_is_directory=True)
        else:
            target.mkdir()
            (target / "runtime.json").symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"This user cannot create symlinks: {exc}")
    assert main(["setup", "--json"]) != 0
    capsys.readouterr()
    assert victim.read_text(encoding="utf-8") == "user file that must survive"
    assert not _doctor(capsys)["ok"]


def test_doctor_without_setup_has_no_filesystem_side_effects(isolated_user, monkeypatch, capsys):
    home, work = isolated_user
    monkeypatch.setenv("PATH", str(home / "no commands here"))
    before = _snapshot(home), _snapshot(work)
    assert not _doctor(capsys)["ok"]
    assert (_snapshot(home), _snapshot(work)) == before


def test_doctor_reports_missing_path_even_with_a_valid_profile(isolated_user, monkeypatch, capsys):
    result = _setup(capsys)
    monkeypatch.setenv("PATH", str(Path(result["path"]).parent / "no commands here"))
    report = _doctor(capsys)
    assert not report["ok"]
    assert report["command"] is None
    assert report["database"] == result["database"]
    assert not Path(result["database"]).exists()


def test_doctor_never_executes_a_shadowing_path_command(isolated_user, tmp_path, monkeypatch, capsys):
    _setup(capsys)
    marker = tmp_path / "UNKNOWN COMMAND WAS EXECUTED"
    script = _executable(tmp_path / "untrusted bin", marker)
    monkeypatch.setenv("PATH", str(script.parent))
    report = _doctor(capsys)
    assert not report["ok"]
    assert Path(report["command"]) == script
    assert not marker.exists()


def test_doctor_never_executes_python_from_a_profile(isolated_user, tmp_path, capsys):
    result = _setup(capsys)
    marker = tmp_path / "PROFILE PYTHON WAS EXECUTED"
    script = _executable(tmp_path / "untrusted python", marker)
    profile = Path(result["path"])
    data = json.loads(profile.read_text(encoding="utf-8"))
    data["python"] = str(script)
    profile.write_text(json.dumps(data), encoding="utf-8")
    before = _snapshot(isolated_user[0])
    assert not _doctor(capsys)["ok"]
    assert not marker.exists()
    assert _snapshot(isolated_user[0]) == before


def test_doctor_accepts_a_copied_windows_launcher(isolated_user, tmp_path, monkeypatch, capsys):
    import todogc.runtime as runtime

    _setup(capsys)
    runtime_dir = tmp_path / "persistent tool" / "Scripts"
    runtime_dir.mkdir(parents=True)
    expected = runtime_dir / "todogc.exe"
    expected.write_bytes(b"known console launcher content")
    exposed_dir = tmp_path / "user commands"
    exposed_dir.mkdir()
    exposed = exposed_dir / "todogc.exe"
    exposed.write_bytes(expected.read_bytes())
    monkeypatch.setattr(sys, "executable", str(runtime_dir / "python.exe"))
    monkeypatch.setenv("PATH", str(exposed_dir))
    # Replace this module's os reference; changing os.name globally breaks pathlib.
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt", path=os.path, environ=os.environ))
    monkeypatch.setattr(runtime.shutil, "which", lambda name: str(exposed))
    report = _doctor(capsys)
    command_check = next(item for item in report["checks"] if item["name"] == "command")
    assert command_check["ok"], command_check


def test_database_symlink_loop_is_a_clean_error(isolated_user, tmp_path, capsys):
    result = _setup(capsys)
    loop = tmp_path / "loop.db"
    try:
        loop.symlink_to(loop.name)
    except OSError as exc:
        pytest.skip(f"This user cannot create symlinks: {exc}")
    profile = Path(result["path"])
    data = json.loads(profile.read_text(encoding="utf-8"))
    data["database"] = str(loop)
    profile.write_text(json.dumps(data), encoding="utf-8")
    before = _snapshot(isolated_user[0])
    assert main(["capture", "Must not resolve a cyclic symlink"]) != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert not _doctor(capsys)["ok"]
    assert _snapshot(isolated_user[0]) == before


def test_conflicting_skill_bindings_require_explicit_selection(isolated_user, tmp_path, capsys):
    home, work = isolated_user
    install_skills("codex", database=str(tmp_path / "codex.db"), config=str(tmp_path / "codex.toml"))
    install_skills("claude-code", database=str(tmp_path / "claude.db"), config=str(tmp_path / "claude.toml"))
    before = _snapshot(home), _snapshot(work)
    assert main(["capture", "Must not guess a task database"]) != 0
    capsys.readouterr()
    assert (_snapshot(home), _snapshot(work)) == before
    assert not _doctor(capsys)["ok"]
    chosen = tmp_path / "chosen.db"
    assert main(["--db", str(chosen), "--config", str(tmp_path / "chosen.toml"), "capture", "Explicit choice"]) == 0
    capsys.readouterr()
    assert chosen.exists()
    assert not (home / ".todogc" / "todogc.db").exists()


@pytest.mark.parametrize("conflicting_field", ["database", "config"])
def test_skill_conflict_overrides_are_resolved_per_field(isolated_user, tmp_path, capsys, conflicting_field):
    common_db = str(tmp_path / "common.db")
    common_config = str(tmp_path / "common.toml")
    install_skills("codex", database=common_db, config=common_config)
    install_skills(
        "claude-code",
        database=str(tmp_path / "different.db") if conflicting_field == "database" else common_db,
        config=str(tmp_path / "different.toml") if conflicting_field == "config" else common_config,
    )
    option = "--db" if conflicting_field == "database" else "--config"
    selected = tmp_path / ("chosen.db" if conflicting_field == "database" else "chosen.toml")
    result = _setup(capsys, option, str(selected))
    assert result[conflicting_field] == str(selected)
    other = "config" if conflicting_field == "database" else "database"
    assert result[other] == (common_config if other == "config" else common_db)


def test_a_legacy_skill_wins_over_unrelated_local_files(isolated_user, tmp_path, capsys):
    _, work = isolated_user
    database, config = tmp_path / "personal.db", tmp_path / "personal.toml"
    install_skills("codex", database=str(database), config=str(config))
    (work / "todogc.db").write_bytes(b"another project's database")
    (work / "todogc.toml").write_bytes(b"invalid configuration in another project")
    before = _snapshot(work)
    assert main(["capture", "Task in the existing Skill database"]) == 0
    capsys.readouterr()
    assert database.exists()
    assert _snapshot(work) == before


def test_setup_reuses_an_existing_skill_without_changing_user_data(isolated_user, tmp_path, capsys):
    database = tmp_path / "bound personal tasks.db"
    config = tmp_path / "bound settings.toml"
    database.write_bytes(b"existing data must not be opened during setup")
    config.write_bytes(b"existing config must not be parsed during setup")
    install_skills("codex", database=str(database), config=str(config))
    result = _setup(capsys)
    assert result["database"] == str(database)
    assert result["config"] == str(config)
    assert database.read_bytes() == b"existing data must not be opened during setup"
    assert config.read_bytes() == b"existing config must not be parsed during setup"


def test_doctor_reports_skill_drift_without_rebinding_it(isolated_user, tmp_path, capsys):
    result = _setup(capsys)
    installed = install_skills("codex", database=str(tmp_path / "other.db"), config=result["config"])[0]
    skill_runtime = Path(installed["path"]) / "runtime.json"
    before = skill_runtime.read_bytes()
    assert not _doctor(capsys)["ok"]
    assert skill_runtime.read_bytes() == before

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from todogc.cli import main
from todogc.skill_install import BUNDLE_FILES, install_skills, skill_directory, skill_status


@pytest.fixture
def skill_home(tmp_path, monkeypatch):
    home = tmp_path / "user home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    for name in ("TODOGC_DB", "TODOGC_CONFIG", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    return home


def test_status_has_no_filesystem_or_database_side_effects(skill_home, tmp_path, capsys):
    broken = tmp_path / "broken.toml"
    broken.write_text("not valid TOML")
    assert main(["--config", str(broken), "skill", "status", "--json"]) == 0
    results = json.loads(capsys.readouterr().out)
    assert {row["target"] for row in results} == {"codex", "claude-code"}
    assert all(row["state"] == "missing" for row in results)
    assert list(skill_home.iterdir()) == []
    assert not (tmp_path / "todogc.db").exists()


def test_install_both_platforms_binds_stable_paths_without_creating_data(skill_home, tmp_path):
    results = install_skills("all")
    assert skill_directory("codex") == skill_home / ".agents/skills/todogc"
    assert skill_directory("claude-code") == skill_home / ".claude/skills/todogc"
    assert all(row["up_to_date"] and row["state"] == "installed" for row in results)
    assert {row["database"] for row in results} == {str(skill_home / ".todogc/todogc.db")}
    assert not (skill_home / ".todogc").exists()
    assert not (tmp_path / "todogc.db").exists()
    for row in results:
        folder = Path(row["path"])
        assert all((folder / name).is_file() for name in BUNDLE_FILES)
        runtime = json.loads((folder / "runtime.json").read_text())
        assert runtime["managed_by"] == "todogc" and runtime["schema"] == 1
        assert set(runtime["files"]) == set(BUNDLE_FILES)


def test_existing_database_and_config_are_bound_without_reading_or_migrating(skill_home, tmp_path):
    database = tmp_path / "todogc.db"
    database.write_bytes(b"old database must not be opened by the installer")
    config = tmp_path / "todogc.toml"
    config.write_text("invalid local configuration")
    row = install_skills("codex")[0]
    assert row["database"] == str(database)
    assert row["config"] == str(config)
    assert database.read_bytes() == b"old database must not be opened by the installer"
    assert not database.with_suffix(".db.bak").exists()


def test_reinstall_preserves_custom_bindings_across_projects(skill_home, tmp_path, monkeypatch):
    database, config = tmp_path / "custom tasks.db", tmp_path / "settings.toml"
    first = install_skills("codex", database=str(database), config=str(config))[0]
    folder = Path(first["path"])
    before = {name: (folder / name).stat().st_mtime_ns for name in (*BUNDLE_FILES, "runtime.json")}
    project = tmp_path / "another project"
    project.mkdir()
    (project / "todogc.db").write_bytes(b"unrelated")
    monkeypatch.chdir(project)
    again = install_skills("codex")[0]
    assert again == first
    assert before == {name: (folder / name).stat().st_mtime_ns for name in before}
    assert (project / "todogc.db").read_bytes() == b"unrelated"


def test_managed_upgrade_replaces_unedited_assets(skill_home, monkeypatch):
    import todogc.skill_install as module

    first = install_skills("codex")[0]
    assets = module._assets()
    assets["SKILL.md"] += b"\nUpdated official instructions.\n"
    monkeypatch.setattr(module, "_assets", lambda: assets)
    assert not skill_status("codex")["up_to_date"]
    result = install_skills("codex")[0]
    assert result["up_to_date"]
    assert (Path(first["path"]) / "SKILL.md").read_bytes() == assets["SKILL.md"]


def test_new_platform_reuses_other_platform_database_and_config(skill_home, tmp_path, monkeypatch):
    codex = install_skills("codex", database=str(tmp_path / "personal.db"),
                           config=str(tmp_path / "personal.toml"))[0]
    project = tmp_path / "new project"
    project.mkdir()
    (project / "todogc.db").write_bytes(b"Unrelated project list")
    monkeypatch.chdir(project)
    claude = install_skills("claude-code")[0]
    assert claude["database"] == codex["database"]
    assert claude["config"] == codex["config"]
    assert (project / "todogc.db").read_bytes() == b"Unrelated project list"


def test_local_edits_require_explicit_force_and_extra_files_survive(skill_home):
    row = install_skills("codex")[0]
    directory = Path(row["path"])
    (directory / "SKILL.md").write_text("My custom instructions")
    (directory / "personal.txt").write_text("Keep me")
    assert skill_status("codex")["state"] == "modified"
    with pytest.raises(ValueError, match="Locally edited"):
        install_skills("codex")
    assert (directory / "SKILL.md").read_text() == "My custom instructions"
    assert install_skills("codex", force=True)[0]["up_to_date"]
    assert (directory / "personal.txt").read_text() == "Keep me"


def test_all_targets_are_checked_before_any_files_are_written(skill_home):
    foreign = skill_directory("claude-code")
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("Unmanaged skill")
    with pytest.raises(ValueError, match="not managed"):
        install_skills("all")
    assert not skill_directory("codex").exists()
    assert (foreign / "SKILL.md").read_text() == "Unmanaged skill"


@pytest.mark.skipif(sys.platform == "win32", reason="Requires local symlink permissions")
@pytest.mark.parametrize("kind", ["directory", "file", "nested"])
def test_symlink_targets_are_not_overwritten_even_with_force(skill_home, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "SKILL.md"
    sentinel.write_text("Keep me")
    directory = skill_directory("codex")
    directory.parent.mkdir(parents=True)
    if kind == "directory":
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        if kind == "file":
            (directory / "SKILL.md").symlink_to(sentinel)
        else:
            (directory / "references").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        install_skills("codex", force=True)
    assert sentinel.read_text() == "Keep me"


def test_cli_honors_global_paths_and_environment(skill_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TODOGC_DB", "from-env.db")
    monkeypatch.setenv("TODOGC_CONFIG", "from-env.toml")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom claude"))
    assert main(["skill", "install", "--target", "claude-code", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["path"] == str(tmp_path / "custom claude/skills/todogc")
    assert row["database"] == str(tmp_path / "from-env.db")
    assert row["config"] == str(tmp_path / "from-env.toml")
    assert main(["--db", "chosen.db", "--config", "chosen.toml", "skill", "install",
                 "--target", "claude-code", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["database"] == str(tmp_path / "chosen.db")
    assert row["config"] == str(tmp_path / "chosen.toml")
    assert not list(tmp_path.glob("*.db"))


def test_invalid_runtime_is_reported_without_crashing_and_can_be_reinstalled(skill_home):
    row = install_skills("codex")[0]
    runtime = Path(row["path"]) / "runtime.json"
    runtime.write_text('{"managed_by": "todogc", "schema": 1, "files": {"SKILL.md": []}}')
    assert skill_status("codex")["state"] == "unmanaged"
    with pytest.raises(ValueError, match="not managed"):
        install_skills("codex")
    assert install_skills("codex", force=True)[0]["up_to_date"]


def test_install_requires_persistent_file_paths(skill_home, tmp_path):
    for database in (":memory:", str(tmp_path)):
        with pytest.raises(ValueError):
            install_skills("codex", database=database)
    assert not skill_directory("codex").exists()
    with pytest.raises(ValueError, match="single target"):
        install_skills("all", destination=tmp_path / "skill")


def test_cli_reports_install_conflict_without_traceback(skill_home, capsys):
    folder = skill_directory("codex")
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("Other skill")
    assert main(["skill", "install", "--target", "codex"]) == 1
    assert "--force" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="Requires local symlink permissions")
def test_python_binding_keeps_venv_symlink(skill_home, tmp_path, monkeypatch):
    alias = tmp_path / "venv/bin/python"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(alias))
    assert install_skills("codex")[0]["python"] == str(alias)

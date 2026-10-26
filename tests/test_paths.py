"""Regression coverage for the installed skill / ordinary CLI database split."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from todogc.cli import main
from todogc.config import load_config
from todogc.paths import resolve_config, resolve_database
from todogc.skill_install import install_skills
from todogc.webapp import _db_path


@pytest.fixture
def projects(tmp_path, monkeypatch):
    first = tmp_path / "first project"
    second = tmp_path / "second project"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    return first, second


@pytest.mark.parametrize("platform", ["codex", "claude-code"])
@pytest.mark.parametrize("skill_first", [True, False])
def test_clean_install_shares_tasks_in_both_directions(projects, monkeypatch, capsys, platform, skill_first):
    first, second = projects
    expected = Path.home() / ".todogc/todogc.db"
    if not skill_first:
        assert main(["add", "CLI before skill"]) == 0
    row = install_skills(platform)[0]
    assert row["database"] == str(expected)
    if skill_first:
        assert not expected.parent.exists()  # Installation does not create data.
    prefix = ["--db", row["database"], "--config", row["config"]]
    assert main([*prefix, "add", "Skill task"]) == 0
    capsys.readouterr()
    monkeypatch.chdir(second)
    assert main(["ls"]) == 0
    output = capsys.readouterr().out
    assert "Skill task" in output
    if not skill_first:
        assert "CLI before skill" in output
    assert main(["add", "CLI after skill"]) == 0
    capsys.readouterr()
    assert main([*prefix, "search", "CLI after skill"]) == 0
    assert "CLI after skill" in capsys.readouterr().out
    assert _db_path() == row["database"]
    assert not (first / "todogc.db").exists()
    assert not (second / "todogc.db").exists()


def test_cli_alone_uses_shared_database_across_directories(projects, monkeypatch, capsys):
    first, second = projects
    assert main(["add", "Cross-directory task"]) == 0
    capsys.readouterr()
    monkeypatch.chdir(second)
    assert main(["ls"]) == 0
    assert "Cross-directory task" in capsys.readouterr().out
    assert not (first / "todogc.db").exists()
    assert not (second / "todogc.db").exists()


def test_legacy_local_database_is_preserved_and_skill_binds_it(projects, monkeypatch, capsys):
    first, second = projects
    legacy = first / "todogc.db"
    assert main(["--db", str(legacy), "add", "Existing task"]) == 0
    capsys.readouterr()
    assert resolve_database() == str(legacy)
    row = install_skills("codex")[0]
    assert row["database"] == str(legacy)
    monkeypatch.chdir(second)
    assert main(["ls"]) == 0
    assert "Existing task" in capsys.readouterr().out
    assert not (Path.home() / ".todogc/todogc.db").exists()


def test_bound_database_and_config_win_over_unrelated_project(projects, monkeypatch, capsys):
    first, second = projects
    custom = first / "my tasks.db"
    config = first / "my settings.toml"
    config.write_text('[heap]\nmax = 17\n', encoding="utf-8")
    row = install_skills("codex", database=str(custom), config=str(config))[0]
    assert main(["add", "Bound task"]) == 0
    unrelated = second / "todogc.db"
    unrelated.write_bytes(b"Do not open or migrate this database")
    (second / "todogc.toml").write_text("Invalid unrelated config", encoding="utf-8")
    monkeypatch.chdir(second)
    capsys.readouterr()
    assert main(["ls"]) == 0
    assert "Bound task" in capsys.readouterr().out
    assert load_config().heap_max == 17
    assert _db_path() == row["database"]
    assert unrelated.read_bytes() == b"Do not open or migrate this database"
    assert install_skills("codex")[0] == row


def test_shared_database_wins_over_legacy_local_file(projects):
    first, _ = projects
    assert main(["add", "Personal task"]) == 0
    local = first / "todogc.db"
    local.write_bytes(b"Unrelated old local file")
    assert resolve_database() == str(Path.home() / ".todogc/todogc.db")
    assert install_skills("codex")[0]["database"] == resolve_database()
    assert local.read_bytes() == b"Unrelated old local file"


def test_explicit_and_environment_paths_override_binding(projects, monkeypatch):
    first, _ = projects
    install_skills("codex", database=str(first / "bound.db"))
    monkeypatch.setenv("TODOGC_DB", "environment.db")
    monkeypatch.setenv("TODOGC_CONFIG", "environment.toml")
    assert resolve_database() == str(first / "environment.db")
    assert _db_path() == resolve_database()
    assert resolve_config() == str(first / "environment.toml")
    assert resolve_database("chosen.db") == str(first / "chosen.db")
    assert resolve_config("chosen.toml") == str(first / "chosen.toml")
    assert resolve_database(":memory:") == ":memory:"


def test_shared_config_is_used_by_cli_and_skill(projects):
    config = Path.home() / ".todogc/todogc.toml"
    config.parent.mkdir()
    config.write_text('[heap]\nmax = 19\n', encoding="utf-8")
    assert load_config().heap_max == 19
    assert install_skills("codex")[0]["config"] == str(config)


def test_watch_status_and_skill_status_do_not_create_default_data(projects, capsys):
    assert main(["watch", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["database"] == resolve_database()
    assert main(["skill", "status", "--json"]) == 0
    assert not (Path.home() / ".todogc").exists()


def test_different_agent_bindings_require_explicit_database(projects, capsys):
    first, _ = projects
    install_skills("codex", database=str(first / "codex.db"))
    install_skills("claude-code", database=str(first / "claude.db"))
    assert main(["ls"]) == 2
    assert "different database paths" in capsys.readouterr().err
    assert main(["--db", str(first / "chosen.db"), "add", "Explicit task"]) == 0


def test_runtime_without_skill_manifest_does_not_override_personal_defaults(projects):
    first, _ = projects
    directory = Path.home() / ".agents/skills/todogc"
    directory.mkdir(parents=True)
    # A personal profile copied into an unrelated skill directory is not an
    # installed official skill and must not redirect ordinary task commands.
    (directory / "runtime.json").write_text(json.dumps({
        "managed_by": "todogc", "schema": 1, "version": "0.14.0",
        "python": str(first / "python"), "database": str(first / "unrelated.db"),
        "config": str(first / "unrelated.toml"),
    }), encoding="utf-8")
    assert resolve_database() == str(Path.home() / ".todogc/todogc.db")
    assert resolve_config() == str(Path.home() / ".todogc/todogc.toml")

"""English is the default product language; Chinese labels require an explicit locale."""

import re

import pytest

from todogc import console, report
from todogc.cli import main
from todogc.collector import Collector
from todogc.locales.en import MESSAGES
from todogc.store import Store


def test_english_catalog_contains_no_chinese():
    assert not re.search(r"[\u3400-\u9fff]", "\n".join(MESSAGES.values()))


@pytest.mark.parametrize("rich", [False, True])
def test_english_cli_empty_and_populated_output_has_no_chinese(tmp_path, capsys, monkeypatch, rich):
    if rich:
        pytest.importorskip("rich")
    else:
        monkeypatch.setattr(console, "_HAVE_RICH", False)
    db = str(tmp_path / "english.db")
    assert main(["--db", db, "ls"]) == 0
    with Store(db) as store:
        store.add_task("Goal", is_root=True)
        store.add_task("Child", root_id=1)
        store.add_task("Orphan")
        store.add_task("Plan better", meta=True)
    for command in (["ls"], ["heap"], ["doctor"], ["gc", "--dry-run"], ["completed"], ["graveyard"], ["stats"]):
        assert main(["--db", db, *command]) == 0
    output = capsys.readouterr().out
    assert "Eden" in output and not re.search(r"[\u3400-\u9fff]", output)
    with Store(db) as store:
        result = Collector().run_on_store(store, store.root_ids())
        zh = report.format_heap_map(result, store.root_ids(), lang="zh")
        assert "新生代" in zh
        english = report.format_heap_map(result, store.root_ids(), lang="en")
        assert "新生代" not in english

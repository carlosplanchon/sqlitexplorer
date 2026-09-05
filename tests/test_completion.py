"""Tests of the shell-completion callbacks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sqlitexplorer.completion import complete_table


def test_complete_table_filters_by_prefix(database: Path) -> None:
    ctx = SimpleNamespace(params={"database": str(database)})
    assert complete_table(ctx, "") == ["adults", "empty", "users"]
    assert complete_table(ctx, "u") == ["users"]


def test_complete_table_without_database_returns_nothing() -> None:
    assert complete_table(SimpleNamespace(params={}), "u") == []


def test_complete_table_ignores_unreadable_database(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.db"
    bogus.write_text("not a database")
    assert complete_table(SimpleNamespace(params={"database": str(bogus)}), "") == []
    missing = tmp_path / "missing.db"
    assert complete_table(SimpleNamespace(params={"database": str(missing)}), "") == []

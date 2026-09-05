"""Unit tests of the SQLite access layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sqlitexplorer.core import ExplorerError, ReadOnlyError, open_database, quote_identifier


def make_database(path: Path, script: str) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(script)
    finally:
        connection.close()
    return path


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("users", '"users"'),
        ('we"ird', '"we""ird"'),
        ("users; DROP TABLE users; --", '"users; DROP TABLE users; --"'),
    ],
)
def test_quote_identifier(name: str, expected: str) -> None:
    assert quote_identifier(name) == expected


def test_open_database_never_creates_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(ExplorerError), open_database(missing):
        pass
    assert not missing.exists()


def test_tables_survive_a_broken_view(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "broken.db",
        "CREATE TABLE t (x); CREATE VIEW v AS SELECT x FROM t; DROP TABLE t;",
    )
    with open_database(path) as db:
        result = db.tables()
    assert result.columns == ("type", "name", "rows")
    assert result.rows == [("view", "v", "?")]


def test_writes_require_write_mode(tmp_path: Path) -> None:
    path = make_database(tmp_path / "rw.db", "CREATE TABLE t (x); INSERT INTO t VALUES (1);")
    with pytest.raises(ReadOnlyError), open_database(path) as db:
        db.execute("DELETE FROM t")
    with open_database(path, write=True) as db:
        assert db.execute("DELETE FROM t").rowcount == 1
    with open_database(path) as db:
        assert db.execute("SELECT COUNT(*) FROM t").rows == [(0,)]


def test_write_mode_rolls_back_on_error(tmp_path: Path) -> None:
    path = make_database(tmp_path / "rb.db", "CREATE TABLE t (x); INSERT INTO t VALUES (1);")
    with pytest.raises(ExplorerError), open_database(path, write=True) as db:
        db.execute("DELETE FROM t")
        db.execute("SELECT * FROM nope")
    with open_database(path) as db:
        assert db.execute("SELECT COUNT(*) FROM t").rows == [(1,)]


def test_quoted_identifiers_reach_sqlite_intact(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "quoted.db",
        'CREATE TABLE "odd name" ("a b" TEXT); INSERT INTO "odd name" VALUES (\'x\');',
    )
    with open_database(path) as db:
        assert db.columns("odd name").rows[0][1] == "a b"
        assert db.rows("odd name").rows == [("x",)]
        assert db.rows("odd name", limit=0).rows == []
        with pytest.raises(ExplorerError, match="no such table or view"):
            db.columns("missing")

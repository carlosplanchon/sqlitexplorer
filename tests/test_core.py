"""Unit tests of the SQLite access layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sqlitexplorer.core import (
    ExplorerError,
    ReadOnlyError,
    ResultSet,
    open_database,
    plan_tree,
    quote_identifier,
    split_statements,
)


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


def test_split_statements_handles_strings_comments_and_triggers() -> None:
    sql = "SELECT 'a;b'; -- c;\nCREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT 1; END; SELECT 2"
    assert split_statements(sql) == [
        "SELECT 'a;b';",
        "-- c;\nCREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT 1; END;",
        "SELECT 2",
    ]
    assert split_statements("  ;; \n") == []
    assert split_statements("SELECT 1;") == ["SELECT 1;"]


def test_plan_tree_indents_by_depth() -> None:
    result = ResultSet(
        columns=("id", "parent", "notused", "detail"),
        rows=[(1, 0, 0, "root"), (2, 1, 0, "child"), (3, 2, 0, "grandchild")],
    )
    tree = plan_tree(result)
    assert tree.columns == ("id", "parent", "detail")
    assert [row[2] for row in tree.rows] == ["root", "  child", "    grandchild"]


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


def test_execute_accepts_named_parameters(database: Path) -> None:
    with open_database(database) as db:
        result = db.execute("SELECT name FROM users WHERE age > :age ORDER BY id", {"age": 20})
    assert result.rows == [("Marie",)]


def test_explain_returns_an_indented_plan(database: Path) -> None:
    with open_database(database) as db:
        result = db.explain("SELECT * FROM users WHERE name = 'Marie'")
    assert result.columns == ("id", "parent", "detail")
    assert any("users" in row[2] for row in result.rows)


def test_attach_is_read_only_by_default(database: Path, tmp_path: Path) -> None:
    other = make_database(tmp_path / "other.db", "CREATE TABLE t (x); INSERT INTO t VALUES (1);")
    with open_database(database) as db:
        db.attach("o", other)
        assert db.execute("SELECT x FROM o.t").rows == [(1,)]
    with pytest.raises(ReadOnlyError), open_database(database) as db:
        db.attach("o", other)
        db.execute("INSERT INTO o.t VALUES (2)")


def test_attach_missing_file_fails(database: Path, tmp_path: Path) -> None:
    with open_database(database) as db, pytest.raises(ExplorerError, match="cannot attach"):
        db.attach("o", tmp_path / "missing.db")


def test_names_and_objects(database: Path) -> None:
    with open_database(database) as db:
        assert db.names() == ["adults", "empty", "users"]
        assert db.names(kinds=("table",)) == ["empty", "users"]
        assert "sqlite_sequence" in db.names(include_internal=True)
        kinds = [kind for kind, _, _ in db.objects()]
    assert kinds == ["table", "index", "view", "table"]


def test_rows_with_columns_where_order_by_desc(database: Path) -> None:
    with open_database(database) as db:
        result = db.rows(
            "users",
            columns=["NAME", "age"],
            where="age IS NOT NULL",
            order_by="Age",
            descending=True,
        )
    assert result.columns == ("name", "age")
    assert result.rows == [("Marie", 30), ("Ana", 12)]


def test_rows_rejects_unknown_column(database: Path) -> None:
    with (
        open_database(database) as db,
        pytest.raises(ExplorerError, match="no such column in users: nope"),
    ):
        db.rows("users", columns=["nope"])


def test_stats_reports_nulls_distinct_min_max_top(database: Path) -> None:
    with open_database(database) as db:
        result = db.stats("users", top=2)
    assert result.columns == ("column", "type", "nulls", "distinct", "min", "max", "top")
    by_column = {row[0]: row for row in result.rows}
    assert by_column["age"][1:6] == ("INTEGER", 1, 2, 12, 30)
    assert by_column["age"][6] == "12 (1), 30 (1)"
    assert by_column["avatar"][6] == "X'0102' (1)"


def test_stats_on_view_falls_back_to_typeof_for_expressions(database: Path) -> None:
    connection = sqlite3.connect(database)
    connection.execute("CREATE VIEW doubled AS SELECT name, age * 2 AS twice FROM users")
    connection.close()
    with open_database(database) as db:
        adults = db.stats("adults")
        doubled = db.stats("doubled")
    assert [(row[0], row[1]) for row in adults.rows] == [("name", "TEXT"), ("age", "INTEGER")]
    assert [(row[0], row[1]) for row in doubled.rows] == [("name", "TEXT"), ("twice", "integer")]


def test_search_finds_text_in_any_table(relational_database: Path) -> None:
    with open_database(relational_database) as db:
        result = db.search("world")
    assert result.columns == ("table", "column", "rowid", "value")
    assert result.rows == [("posts", "title", 1, "Hello world")]


def test_search_escapes_like_wildcards(relational_database: Path) -> None:
    with open_database(relational_database) as db:
        assert [row[3] for row in db.search("100%").rows] == ["100% done"]
        assert [row[3] for row in db.search("a_b").rows] == ["a_b test"]


def test_search_without_rowid_table_has_null_rowid(relational_database: Path) -> None:
    with open_database(relational_database) as db:
        result = db.search("plain")
    assert result.rows == [("notes", "body", None, "plain")]


def test_search_reports_every_matching_column_and_numbers(database: Path) -> None:
    with open_database(database) as db:
        assert db.search("30").rows == [("users", "age", 1, 30)]
        db_rows = db.search("a", tables=["users"]).rows
    # One row per matching column: Marie/Ana by name, Ana's row also matches nothing else.
    assert ("users", "name", 1, "Marie") in db_rows
    assert ("users", "name", 3, "Ana") in db_rows
    assert all(column != "avatar" for _, column, _, _ in db_rows)  # BLOBs are skipped


def test_search_limit_and_table_filter(relational_database: Path) -> None:
    with open_database(relational_database) as db:
        assert len(db.search("e", limit=2).rows) == 2
        assert {row[0] for row in db.search("e", tables=["users"]).rows} == {"users"}
        with pytest.raises(ExplorerError, match="no such table or view: nope"):
            db.search("e", tables=["nope"])
        with pytest.raises(ExplorerError, match="nothing to search"):
            db.search("")


def test_indexes_lists_index_columns(relational_database: Path) -> None:
    with open_database(relational_database) as db:
        everything = db.indexes()
        posts_only = db.indexes("posts")
        with pytest.raises(ExplorerError, match="no such table or view: nope"):
            db.indexes("nope")
    assert everything.columns == ("table", "name", "unique", "origin", "partial", "columns")
    assert ("posts", "posts_user_idx", 0, "c", 0, "user_id") in everything.rows
    assert [row[1] for row in posts_only.rows] == ["posts_user_idx"]


def test_foreign_keys_lists_references(relational_database: Path) -> None:
    with open_database(relational_database) as db:
        result = db.foreign_keys()
        assert db.foreign_keys("users").rows == []
    assert result.rows == [
        ("posts", 0, 0, "user_id", "users", "id", "NO ACTION", "CASCADE", "NONE")
    ]


def test_info_reports_pragmas_and_counts(database: Path) -> None:
    with open_database(database) as db:
        entries = dict(db.info(check=True).rows)
    assert Path(entries["path"]).resolve() == database.resolve()
    assert entries["size"] > 0
    assert isinstance(entries["page_size"], int)
    assert entries["journal_mode"] == "delete"
    assert (entries["tables"], entries["views"], entries["indexes"], entries["triggers"]) == (
        2,
        1,
        1,
        0,
    )
    assert entries["integrity_check"] == "ok"


def test_import_rows_creates_table_and_inserts(tmp_path: Path) -> None:
    path = make_database(tmp_path / "imp.db", "CREATE TABLE existing (x);")
    with open_database(path, write=True) as db:
        count = db.import_rows(
            "people", [("name", "TEXT"), ("age", "INTEGER")], [("Marie", 30), ("Ana", None)]
        )
        with pytest.raises(ExplorerError, match="no such table: ghost"):
            db.import_rows("ghost", [("x", "TEXT")], [("1",)], create=False)
    assert count == 2
    with open_database(path) as db:
        assert db.rows("people").rows == [("Marie", 30), ("Ana", None)]
        assert [row[2] for row in db.columns("people").rows] == ["TEXT", "INTEGER"]


def test_dump_is_replayable(database: Path, tmp_path: Path) -> None:
    with open_database(database) as db:
        script = "\n".join(db.dump())
    copy = sqlite3.connect(tmp_path / "copy.db")
    try:
        copy.executescript(script)
        assert copy.execute("SELECT COUNT(*) FROM users").fetchone() == (3,)
    finally:
        copy.close()

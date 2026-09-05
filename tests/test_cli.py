"""End-to-end tests of the command-line interface."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from sqlitexplorer import __version__
from sqlitexplorer.cli import app

runner = CliRunner()

SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    age INTEGER,
    avatar BLOB
);
CREATE INDEX users_name_idx ON users (name);
CREATE VIEW adults AS SELECT name, age FROM users WHERE age >= 18;
CREATE TABLE empty (x TEXT);
INSERT INTO users (name, age, avatar) VALUES ('Marie', 30, X'0102');
INSERT INTO users (name, age, avatar) VALUES ('Joseph', NULL, NULL);
INSERT INTO users (name, age, avatar) VALUES ('Ana', 12, NULL);
"""


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
    finally:
        connection.close()
    return path


def invoke(*args: object, **kwargs: object) -> Result:
    return runner.invoke(app, [str(arg) for arg in args], **kwargs)


def count_users(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        return connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        connection.close()


def test_version() -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert result.output.strip() == f"sqlitexplorer {__version__}"


def test_no_arguments_shows_help() -> None:
    result = invoke()
    assert "Usage" in result.output
    assert "tables" in result.output


def test_missing_database_is_a_usage_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    result = invoke("tables", missing)
    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert not missing.exists()


def test_tables_lists_tables_and_views_with_counts(database: Path) -> None:
    result = invoke("tables", database)
    assert result.exit_code == 0
    rows = [line.split() for line in result.output.splitlines()]
    assert rows[0] == ["type", "name", "rows"]
    assert ["table", "empty", "0"] in rows
    assert ["table", "users", "3"] in rows
    assert ["view", "adults", "1"] in rows
    assert "sqlite_sequence" not in result.output


def test_tables_all_includes_internal_tables(database: Path) -> None:
    result = invoke("tables", database, "--all")
    assert result.exit_code == 0
    assert "sqlite_sequence" in result.output


def test_schema_prints_every_statement(database: Path) -> None:
    result = invoke("schema", database)
    assert result.exit_code == 0
    assert "CREATE TABLE users" in result.output
    assert "CREATE INDEX users_name_idx" in result.output
    assert "CREATE VIEW adults" in result.output
    assert "CREATE TABLE empty" in result.output
    assert "sqlite_sequence" not in result.output
    assert result.output.count(";") == 4
    # Creation order, so the output can be replayed: the view comes after its table.
    assert result.output.index("CREATE TABLE users") < result.output.index("CREATE VIEW adults")


def test_schema_all_includes_internal_objects(database: Path) -> None:
    result = invoke("schema", database, "--all")
    assert result.exit_code == 0
    assert "CREATE TABLE sqlite_sequence" in result.output
    assert result.output.count(";") == 5


def test_schema_of_an_internal_table_by_name(database: Path) -> None:
    result = invoke("schema", database, "sqlite_sequence")
    assert result.exit_code == 0
    assert result.output.count(";") == 1
    assert "CREATE TABLE sqlite_sequence" in result.output


def test_schema_of_one_table_includes_only_its_objects(database: Path) -> None:
    result = invoke("schema", database, "users")
    assert result.exit_code == 0
    assert "CREATE TABLE users" in result.output
    assert "CREATE INDEX users_name_idx" in result.output
    assert "CREATE VIEW adults" not in result.output
    assert "CREATE TABLE empty" not in result.output


def test_schema_of_unknown_table_fails(database: Path) -> None:
    result = invoke("schema", database, "nope")
    assert result.exit_code == 1
    assert "no such table or view: nope" in result.output


def test_describe_lists_columns(database: Path) -> None:
    result = invoke("describe", database, "users")
    assert result.exit_code == 0
    header, *rows = [line.split() for line in result.output.splitlines()]
    assert header == ["cid", "name", "type", "notnull", "dflt_value", "pk"]
    assert ["0", "id", "INTEGER", "0", "NULL", "1"] in rows
    assert ["3", "avatar", "BLOB", "0", "NULL", "0"] in rows


def test_describe_unknown_table_fails(database: Path) -> None:
    result = invoke("describe", database, "nope")
    assert result.exit_code == 1
    assert "no such table or view: nope" in result.output


def test_show_prints_rows_with_sql_style_null_and_blob(database: Path) -> None:
    result = invoke("show", database, "users")
    assert result.exit_code == 0
    header, *rows = [line.split() for line in result.output.splitlines()]
    assert header == ["id", "name", "age", "avatar"]
    assert ["1", "Marie", "30", "X'0102'"] in rows
    assert ["2", "Joseph", "NULL", "NULL"] in rows
    assert "\x1b[" not in result.output  # no ANSI codes outside a terminal


def test_show_limit_and_offset(database: Path) -> None:
    result = invoke("show", database, "users", "--limit", "1", "--offset", "1")
    assert result.exit_code == 0
    assert "Joseph" in result.output
    assert "Marie" not in result.output
    assert "Ana" not in result.output


def test_show_empty_table(database: Path) -> None:
    result = invoke("show", database, "empty")
    assert result.exit_code == 0
    assert "(no rows)" in result.output


def test_show_a_view(database: Path) -> None:
    result = invoke("show", database, "adults")
    assert result.exit_code == 0
    assert "Marie" in result.output
    assert "Ana" not in result.output


def test_show_unknown_table_fails(database: Path) -> None:
    result = invoke("show", database, "nope")
    assert result.exit_code == 1
    assert "no such table: nope" in result.output


def test_show_can_force_colors(database: Path) -> None:
    result = invoke("show", database, "users", "--color")
    assert result.exit_code == 0
    assert "\x1b[" in result.output


def test_width_option_is_accepted(database: Path) -> None:
    result = invoke("show", database, "users", "--width", "200")
    assert result.exit_code == 0
    assert "Marie" in result.output


def test_query_select_uses_column_names_as_labels(database: Path) -> None:
    result = invoke("query", database, "SELECT name AS person, age FROM users ORDER BY id")
    assert result.exit_code == 0
    header, first, *_ = [line.split() for line in result.output.splitlines()]
    assert header == ["person", "age"]
    assert first == ["Marie", "30"]


def test_query_reads_sql_from_stdin(database: Path) -> None:
    result = invoke("query", database, "-", input="SELECT COUNT(*) AS total FROM users\n")
    assert result.exit_code == 0
    assert result.output.split() == ["total", "3"]


def test_query_rejects_empty_sql(database: Path) -> None:
    result = invoke("query", database, "-", input="  \n")
    assert result.exit_code == 1
    assert "no SQL statement" in result.output


def test_query_is_read_only_by_default(database: Path) -> None:
    result = invoke("query", database, "DELETE FROM users")
    assert result.exit_code == 1
    assert "readonly" in result.output
    assert "--write" in result.output
    assert count_users(database) == 3


def test_query_write_commits_changes(database: Path) -> None:
    result = invoke("query", database, "DELETE FROM users WHERE age < 18", "--write")
    assert result.exit_code == 0
    assert result.output.strip() == "OK (1 row affected)"
    assert count_users(database) == 2


def test_query_ddl_reports_ok(database: Path) -> None:
    result = invoke("query", database, "CREATE TABLE t (x)", "--write")
    assert result.exit_code == 0
    assert result.output.strip() == "OK"
    assert "CREATE TABLE t" in invoke("schema", database, "t").output


def test_query_with_invalid_sql_fails(database: Path) -> None:
    result = invoke("query", database, "SELEC 1")
    assert result.exit_code == 1
    assert "syntax error" in result.output

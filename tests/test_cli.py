"""End-to-end tests of the command-line interface."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from sqlitexplorer import __version__

# --- Basics -------------------------------------------------------------------


def test_version(invoke: Callable) -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert result.output.strip() == f"sqlitexplorer {__version__}"


def test_no_arguments_shows_help(invoke: Callable) -> None:
    result = invoke()
    assert "Usage" in result.output
    assert "tables" in result.output


def test_missing_database_is_a_usage_error(invoke: Callable, tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    result = invoke("tables", missing)
    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert not missing.exists()


# --- tables / schema / describe ----------------------------------------------


def test_tables_lists_tables_and_views_with_counts(invoke: Callable, database: Path) -> None:
    result = invoke("tables", database)
    assert result.exit_code == 0
    rows = [line.split() for line in result.output.splitlines()]
    assert rows[0] == ["type", "name", "rows"]
    assert ["table", "empty", "0"] in rows
    assert ["table", "users", "3"] in rows
    assert ["view", "adults", "1"] in rows
    assert "sqlite_sequence" not in result.output


def test_tables_all_includes_internal_tables(invoke: Callable, database: Path) -> None:
    result = invoke("tables", database, "--all")
    assert result.exit_code == 0
    assert "sqlite_sequence" in result.output


def test_tables_format_csv_has_header(invoke: Callable, database: Path) -> None:
    result = invoke("tables", database, "--format", "csv")
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "type,name,rows"
    assert "table,users,3" in result.output


def test_schema_prints_every_statement(invoke: Callable, database: Path) -> None:
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


def test_schema_all_includes_internal_objects(invoke: Callable, database: Path) -> None:
    result = invoke("schema", database, "--all")
    assert result.exit_code == 0
    assert "CREATE TABLE sqlite_sequence" in result.output
    assert result.output.count(";") == 5


def test_schema_of_one_table_includes_only_its_objects(invoke: Callable, database: Path) -> None:
    result = invoke("schema", database, "users")
    assert result.exit_code == 0
    assert "CREATE TABLE users" in result.output
    assert "CREATE INDEX users_name_idx" in result.output
    assert "CREATE VIEW adults" not in result.output
    assert "CREATE TABLE empty" not in result.output


def test_schema_of_unknown_table_fails(invoke: Callable, database: Path) -> None:
    result = invoke("schema", database, "nope")
    assert result.exit_code == 1
    assert "no such table or view: nope" in result.output


def test_describe_lists_columns(invoke: Callable, database: Path) -> None:
    result = invoke("describe", database, "users")
    assert result.exit_code == 0
    header, *rows = [line.split() for line in result.output.splitlines()]
    assert header == ["cid", "name", "type", "notnull", "dflt_value", "pk"]
    assert ["0", "id", "INTEGER", "0", "NULL", "1"] in rows
    assert ["3", "avatar", "BLOB", "0", "NULL", "0"] in rows


def test_describe_unknown_table_fails(invoke: Callable, database: Path) -> None:
    result = invoke("describe", database, "nope")
    assert result.exit_code == 1
    assert "no such table or view: nope" in result.output


# --- show ---------------------------------------------------------------------


def test_show_prints_rows_with_sql_style_null_and_blob(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users")
    assert result.exit_code == 0
    header, *rows = [line.split() for line in result.output.splitlines()]
    assert header == ["id", "name", "age", "avatar"]
    assert ["1", "Marie", "30", "X'0102'"] in rows
    assert ["2", "Joseph", "NULL", "NULL"] in rows
    assert "\x1b[" not in result.output  # no ANSI codes outside a terminal


def test_show_limit_and_offset(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "--limit", "1", "--offset", "1")
    assert result.exit_code == 0
    assert "Joseph" in result.output
    assert "Marie" not in result.output
    assert "Ana" not in result.output


def test_show_empty_table(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "empty")
    assert result.exit_code == 0
    assert "(no rows)" in result.output


def test_show_a_view(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "adults")
    assert result.exit_code == 0
    assert "Marie" in result.output
    assert "Ana" not in result.output


def test_show_unknown_table_fails(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "nope")
    assert result.exit_code == 1
    assert "no such table: nope" in result.output


def test_show_can_force_colors(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "--color")
    assert result.exit_code == 0
    assert "\x1b[" in result.output


def test_color_auto_detection_follows_tty_and_no_color(
    invoke: Callable, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sqlitexplorer.render.stdout_is_tty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert "\x1b[" in invoke("show", database, "users").output
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b[" not in invoke("show", database, "users").output


def test_width_option_is_accepted(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "--width", "200")
    assert result.exit_code == 0
    assert "Marie" in result.output


def test_show_format_json_is_parseable(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "-f", "JSON")
    assert result.exit_code == 0
    records = json.loads(result.output)
    assert records[0] == {"id": 1, "name": "Marie", "age": 30, "avatar": "AQI="}
    assert records[1]["age"] is None


def test_show_markdown_and_tsv(invoke: Callable, database: Path) -> None:
    markdown = invoke("show", database, "users", "--format", "markdown").output.splitlines()
    assert markdown[0] == "| id | name | age | avatar |"
    assert markdown[1] == "| --- | --- | --- | --- |"
    tsv = invoke("show", database, "users", "--format", "tsv").output.splitlines()
    assert tsv[0] == "id\tname\tage\tavatar"


def test_show_null_option(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "--null", "<null>", "--format", "csv")
    assert result.exit_code == 0
    assert "2,Joseph,<null>,<null>" in result.output


def test_show_truncate_adds_ellipsis(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "--truncate", "4", "--format", "csv")
    assert result.exit_code == 0
    assert "Jos…" in result.output
    assert "Joseph" not in result.output


def test_show_page_prints_footer_on_stderr(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "--page", "2", "--page-size", "1")
    assert result.exit_code == 0
    assert "Joseph" in result.stdout
    assert "Marie" not in result.stdout
    assert "page 2 of 3 (3 rows)" in result.stderr


def test_show_page_out_of_range_fails(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "--page", "9", "--page-size", "2")
    assert result.exit_code == 1
    assert "page 9 is out of range (1-2)" in result.output


def test_pager_is_ignored_without_a_terminal(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "--pager")
    assert result.exit_code == 0
    assert "Marie" in result.output


def test_pager_runs_pager_command(
    invoke: Callable, database: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sqlitexplorer.render.stdout_is_tty", lambda: True)
    paged = tmp_path / "paged.txt"
    monkeypatch.setenv("PAGER", f"tee {paged}")
    result = invoke("show", database, "users", "--pager", "--no-color")
    assert result.exit_code == 0
    assert "Marie" in paged.read_text()
    assert "Marie" not in result.output


def test_show_columns_where_order_by_desc(invoke: Callable, database: Path) -> None:
    result = invoke(
        "show",
        database,
        "users",
        "-c",
        "name,age",
        "--where",
        "age IS NOT NULL",
        "--order-by",
        "age",
        "--desc",
        "--format",
        "csv",
    )
    assert result.exit_code == 0
    assert result.output.splitlines() == ["name,age", "Marie,30", "Ana,12"]


def test_show_unknown_column_fails(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "--order-by", "nope")
    assert result.exit_code == 1
    assert "no such column in users: nope" in result.output


def test_show_where_sql_error_fails(invoke: Callable, database: Path) -> None:
    result = invoke("show", database, "users", "--where", "age >")
    assert result.exit_code == 1
    assert "syntax error" in result.output


# --- stats / search / indexes / foreign-keys / info --------------------------


def test_stats_reports_per_column_summary(invoke: Callable, database: Path) -> None:
    result = invoke("stats", database, "users", "--top", "1", "--format", "csv")
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "column,type,nulls,distinct,min,max,top"
    assert "age,INTEGER,1,2,12,30,12 (1)" in result.output


def test_stats_unknown_table_fails(invoke: Callable, database: Path) -> None:
    result = invoke("stats", database, "nope")
    assert result.exit_code == 1
    assert "no such table or view: nope" in result.output


def test_search_finds_text(invoke: Callable, relational_database: Path) -> None:
    result = invoke("search", relational_database, "world", "--format", "csv")
    assert result.exit_code == 0
    assert result.output.splitlines() == ["table,column,rowid,value", "posts,title,1,Hello world"]


def test_search_reports_no_matches(invoke: Callable, relational_database: Path) -> None:
    result = invoke("search", relational_database, "zzz")
    assert result.exit_code == 0
    assert "(no matches)" in result.output


def test_search_limit_and_table_filter(invoke: Callable, relational_database: Path) -> None:
    result = invoke("search", relational_database, "e", "-t", "users", "-n", "1", "-f", "csv")
    assert result.exit_code == 0
    assert result.output.splitlines() == ["table,column,rowid,value", "users,name,1,Marie"]


def test_indexes_and_foreign_keys(invoke: Callable, relational_database: Path) -> None:
    indexes = invoke("indexes", relational_database, "posts", "--format", "csv")
    assert indexes.exit_code == 0
    assert "posts,posts_user_idx,0,c,0,user_id" in indexes.output
    assert "(no indexes)" in invoke("indexes", relational_database, "users").output
    keys = invoke("foreign-keys", relational_database, "--format", "csv")
    assert keys.exit_code == 0
    assert "posts,0,0,user_id,users,id,NO ACTION,CASCADE,NONE" in keys.output
    assert "(no foreign keys)" in invoke("foreign-keys", relational_database, "users").output


def test_info_reports_facts(invoke: Callable, database: Path) -> None:
    result = invoke("info", database, "--check", "--format", "csv")
    assert result.exit_code == 0
    entries = dict(line.split(",", 1) for line in result.output.splitlines()[1:])
    assert entries["tables"] == "2"
    assert entries["views"] == "1"
    assert entries["integrity_check"] == "ok"
    assert entries["sqlite_version"] == sqlite3.sqlite_version


# --- query --------------------------------------------------------------------


def test_query_select_uses_column_names_as_labels(invoke: Callable, database: Path) -> None:
    result = invoke("query", database, "SELECT name AS person, age FROM users ORDER BY id")
    assert result.exit_code == 0
    header, first, *_ = [line.split() for line in result.output.splitlines()]
    assert header == ["person", "age"]
    assert first == ["Marie", "30"]


def test_query_reads_sql_from_stdin(invoke: Callable, database: Path) -> None:
    result = invoke("query", database, "-", input="SELECT COUNT(*) AS total FROM users\n")
    assert result.exit_code == 0
    assert result.output.split() == ["total", "3"]


def test_query_rejects_empty_sql(invoke: Callable, database: Path) -> None:
    result = invoke("query", database, "-", input="  \n")
    assert result.exit_code == 1
    assert "no SQL statement" in result.output


def test_query_requires_sql_or_file(invoke: Callable, database: Path, tmp_path: Path) -> None:
    assert "give either" in invoke("query", database).output
    script = tmp_path / "s.sql"
    script.write_text("SELECT 1;")
    result = invoke("query", database, "SELECT 1", "--file", script)
    assert result.exit_code == 1
    assert "not both" in result.output


def test_query_is_read_only_by_default(
    invoke: Callable, database: Path, count_users: Callable[[Path], int]
) -> None:
    result = invoke("query", database, "DELETE FROM users")
    assert result.exit_code == 1
    assert "readonly" in result.output
    assert "--write" in result.output
    assert count_users(database) == 3


def test_query_write_commits_changes(
    invoke: Callable, database: Path, count_users: Callable[[Path], int]
) -> None:
    result = invoke("query", database, "DELETE FROM users WHERE age < 18", "--write")
    assert result.exit_code == 0
    assert result.output.strip() == "OK (1 row affected)"
    assert count_users(database) == 2


def test_query_ddl_reports_ok(invoke: Callable, database: Path) -> None:
    result = invoke("query", database, "CREATE TABLE t (x)", "--write")
    assert result.exit_code == 0
    assert result.output.strip() == "OK"
    assert "CREATE TABLE t" in invoke("schema", database, "t").output


def test_query_with_invalid_sql_fails(invoke: Callable, database: Path) -> None:
    result = invoke("query", database, "SELEC 1")
    assert result.exit_code == 1
    assert "syntax error" in result.output


def test_query_multiple_statements_in_argument(invoke: Callable, database: Path) -> None:
    result = invoke("query", database, "SELECT 1 AS first; SELECT 2 AS second", "-f", "csv")
    assert result.exit_code == 0
    assert result.output.splitlines() == ["first", "1", "second", "2"]


def test_query_file_runs_every_statement(
    invoke: Callable, database: Path, tmp_path: Path, count_users: Callable[[Path], int]
) -> None:
    script = tmp_path / "script.sql"
    script.write_text(
        "INSERT INTO users (name) VALUES ('Zoe');\n"
        "-- a comment; with a semicolon\n"
        "SELECT COUNT(*) AS total FROM users;\n"
    )
    result = invoke("query", database, "--file", script, "--write", "-f", "csv")
    assert result.exit_code == 0
    assert result.output.splitlines() == ["OK (1 row affected)", "total", "4"]
    assert count_users(database) == 4


def test_query_file_rolls_back_on_error(
    invoke: Callable, database: Path, tmp_path: Path, count_users: Callable[[Path], int]
) -> None:
    script = tmp_path / "script.sql"
    script.write_text("INSERT INTO users (name) VALUES ('Zoe'); SELECT * FROM nope;")
    result = invoke("query", database, "--file", script, "--write")
    assert result.exit_code == 1
    assert "no such table: nope" in result.output
    assert count_users(database) == 3


def test_query_explain_shows_plan(invoke: Callable, database: Path) -> None:
    result = invoke("query", database, "SELECT * FROM users WHERE name = 'Marie'", "--explain")
    assert result.exit_code == 0
    assert "detail" in result.output
    assert re.search(r"SEARCH|SCAN", result.output)


def test_query_time_reports_on_stderr(invoke: Callable, database: Path) -> None:
    result = invoke("query", database, "SELECT * FROM users", "--time")
    assert result.exit_code == 0
    assert re.search(r"3 rows in \d+\.\d ms", result.stderr)


def test_query_param_binds_named_values(invoke: Callable, database: Path) -> None:
    result = invoke(
        "query",
        database,
        "SELECT name FROM users WHERE age > :min AND name != @skip",
        "-p",
        "min=20",
        "-p",
        ":skip=Nobody",
        "-f",
        "csv",
    )
    assert result.exit_code == 0
    assert result.output.splitlines() == ["name", "Marie"]


def test_query_param_requires_name_equals_value(invoke: Callable, database: Path) -> None:
    result = invoke("query", database, "SELECT 1", "-p", "min")
    assert result.exit_code == 1
    assert "expects NAME=VALUE" in result.output


def test_query_attach_queries_other_database(
    invoke: Callable, database: Path, relational_database: Path
) -> None:
    result = invoke(
        "query",
        database,
        "SELECT title FROM o.posts WHERE id = 1",
        "--attach",
        f"o={relational_database}",
        "-f",
        "csv",
    )
    assert result.exit_code == 0
    assert result.output.splitlines() == ["title", "Hello world"]
    missing = invoke("query", database, "SELECT 1", "--attach", "o=/nowhere/none.db")
    assert missing.exit_code == 1
    assert "no such file" in missing.output


def test_query_watch_stops_on_keyboard_interrupt(
    invoke: Callable, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("sqlitexplorer.cli.time.sleep", interrupt)
    result = invoke(
        "query", database, "SELECT COUNT(*) AS n FROM users", "--watch", "1", "-f", "csv"
    )
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["n", "3"]


def test_query_watch_rejects_pager_and_write(invoke: Callable, database: Path) -> None:
    result = invoke("query", database, "SELECT 1", "--watch", "1", "--write")
    assert result.exit_code == 1
    assert "--watch cannot be combined" in result.output


# --- chart --------------------------------------------------------------------


def braille(text: str) -> bool:
    return any(0x2800 <= ord(char) <= 0x28FF for char in text)


def test_chart_line_prints_braille(invoke: Callable, database: Path) -> None:
    result = invoke("chart", database, "SELECT id, age FROM users ORDER BY id")
    assert result.exit_code == 0
    assert braille(result.stdout)
    assert "\x1b[" not in result.stdout
    assert "skipped 1 row with NULL values" in result.stderr


def test_chart_hist_uses_first_column(invoke: Callable, database: Path) -> None:
    result = invoke("chart", database, "SELECT age FROM users", "--kind", "hist", "--bins", "2")
    assert result.exit_code == 0
    assert braille(result.stdout)
    assert "(age)" in result.stdout


def test_chart_failures(invoke: Callable, database: Path) -> None:
    assert (
        "no rows to plot" in invoke("chart", database, "SELECT id, age FROM users WHERE 0").output
    )
    assert "is not numeric" in invoke("chart", database, "SELECT id, name FROM users").output
    assert "returned no rows" in invoke("chart", database, "PRAGMA foreign_keys = ON").output


def test_chart_color_flag_adds_ansi(invoke: Callable, database: Path) -> None:
    result = invoke("chart", database, "SELECT id, age FROM users", "--color")
    assert result.exit_code == 0
    assert "\x1b[" in result.stdout


# --- dump / export / import / diff --------------------------------------------


def test_dump_prints_replayable_sql(invoke: Callable, database: Path) -> None:
    result = invoke("dump", database)
    assert result.exit_code == 0
    copy = sqlite3.connect(":memory:")
    copy.executescript(result.output)
    assert copy.execute("SELECT COUNT(*) FROM users").fetchone() == (3,)


def test_dump_output_writes_file(invoke: Callable, database: Path, tmp_path: Path) -> None:
    target = tmp_path / "dump.sql"
    result = invoke("dump", database, "--output", target)
    assert result.exit_code == 0
    assert result.output == ""
    assert "CREATE TABLE users" in target.read_text()


def test_export_table_to_csv_file(invoke: Callable, database: Path, tmp_path: Path) -> None:
    target = tmp_path / "users.csv"
    result = invoke("export", database, "users", "-o", target)
    assert result.exit_code == 0
    lines = target.read_text().splitlines()
    assert lines[0] == "id,name,age,avatar"
    assert lines[1] == "1,Marie,30,X'0102'"


def test_export_json_to_stdout(invoke: Callable, database: Path) -> None:
    result = invoke("export", database, "adults", "--format", "json")
    assert result.exit_code == 0
    assert json.loads(result.output) == [{"name": "Marie", "age": 30}]


def test_export_all_writes_one_file_per_table(
    invoke: Callable, database: Path, tmp_path: Path
) -> None:
    target = tmp_path / "out"
    result = invoke("export", database, "--all", "-o", target, "--format", "json")
    assert result.exit_code == 0
    assert sorted(path.name for path in target.iterdir()) == [
        "adults.json",
        "empty.json",
        "users.json",
    ]
    assert json.loads((target / "empty.json").read_text()) == []


def test_export_requires_table_or_all(invoke: Callable, database: Path, tmp_path: Path) -> None:
    assert "give a table name or --all" in invoke("export", database).output
    assert "give a table name or --all" in invoke("export", database, "users", "--all").output
    assert "--all needs --output" in invoke("export", database, "--all").output


def test_import_creates_table_with_inferred_types(
    invoke: Callable, database: Path, tmp_path: Path
) -> None:
    source = tmp_path / "people.csv"
    source.write_text("name,age,score\nMarie,30,1.5\nAna,,2\n")
    result = invoke("import", database, "people", source)
    assert result.exit_code == 0
    assert result.output.strip() == "OK (2 rows imported into people)"
    described = invoke("describe", database, "people", "--format", "csv").output.splitlines()
    assert described[1:] == [
        "0,name,TEXT,0,NULL,0",
        "1,age,INTEGER,0,NULL,0",
        "2,score,REAL,0,NULL,0",
    ]
    rows = invoke("show", database, "people", "--format", "csv").output.splitlines()
    assert rows[1:] == ["Marie,30,1.5", "Ana,NULL,2.0"]


def test_import_into_existing_table_and_json(
    invoke: Callable, database: Path, tmp_path: Path
) -> None:
    source = tmp_path / "rows.json"
    source.write_text('[{"x": "hello"}, {"x": null}]')
    result = invoke("import", database, "empty", source)
    assert result.exit_code == 0
    assert "2 rows imported into empty" in result.output
    assert invoke("show", database, "empty", "-f", "csv").output.splitlines() == [
        "x",
        "hello",
        "NULL",
    ]


def test_import_rolls_back_on_error(invoke: Callable, database: Path, tmp_path: Path) -> None:
    source = tmp_path / "bad.csv"
    source.write_text("x,y\n1,2\n")
    result = invoke("import", database, "empty", source)
    assert result.exit_code == 1
    assert "no column named y" in result.output
    assert "(no rows)" in invoke("show", database, "empty").output


def test_import_rejects_unknown_format(invoke: Callable, database: Path, tmp_path: Path) -> None:
    source = tmp_path / "data.xyz"
    source.write_text("x\n1\n")
    result = invoke("import", database, "t", source)
    assert result.exit_code == 1
    assert "pass --format" in result.output
    forced = invoke("import", database, "t", source, "--format", "csv")
    assert forced.exit_code == 0


def test_diff_identical_databases_exit_0(invoke: Callable, database: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy.db"
    copy.write_bytes(database.read_bytes())
    result = invoke("diff", database, copy)
    assert result.exit_code == 0
    assert result.output.strip() == "(no differences)"


def test_diff_reports_added_removed_changed_exit_1(
    invoke: Callable, database: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other.db"
    other.write_bytes(database.read_bytes())
    connection = sqlite3.connect(other)
    connection.executescript(
        "DROP INDEX users_name_idx; CREATE TABLE extra (y); ALTER TABLE empty ADD COLUMN z;"
    )
    connection.close()
    result = invoke("diff", database, other)
    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert "- index users_name_idx" in lines
    assert "+ table extra" in lines
    assert "~ table empty" in lines
    assert any(line.startswith("+") and "z" in line for line in lines)

"""Tests of the interactive shell, driven through stdin."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from sqlitexplorer.core import open_database
from sqlitexplorer.shell import Completer, history_path


def test_shell_runs_statement_from_stdin(invoke: Callable, database: Path) -> None:
    result = invoke("shell", database, input="SELECT name FROM users WHERE id = 1;\n")
    assert result.exit_code == 0
    assert "Marie" in result.output
    assert "sqlitexplorer>" not in result.output  # prompts are not echoed for piped input


def test_shell_accumulates_multiline_statement(invoke: Callable, database: Path) -> None:
    result = invoke("shell", database, input="SELECT name\nFROM users\nWHERE id = 2;\n")
    assert result.exit_code == 0
    assert "Joseph" in result.output


def test_shell_continues_after_error(invoke: Callable, database: Path) -> None:
    result = invoke("shell", database, input="SELECT * FROM nope;\nSELECT 1 AS one;\n")
    assert result.exit_code == 0
    assert "Error: no such table: nope" in result.output
    assert "one" in result.output


def test_shell_dot_commands(invoke: Callable, database: Path) -> None:
    script = ".tables\n.schema users\n.describe users\n.indexes users\n.stats empty\n.help\n"
    result = invoke("shell", database, input=script)
    assert result.exit_code == 0
    assert "adults" in result.output
    assert "CREATE TABLE users" in result.output
    assert "avatar" in result.output
    assert "users_name_idx" in result.output
    assert ".quit" in result.output


def test_shell_dot_format_null_and_truncate(invoke: Callable, database: Path) -> None:
    script = (
        ".format json\nSELECT age FROM users WHERE id = 2;\n"
        ".format csv\n.null -\n.truncate 3\nSELECT name, age FROM users WHERE id = 2;\n"
    )
    result = invoke("shell", database, input=script)
    assert result.exit_code == 0
    assert '"age": null' in result.output
    assert "Jo…,-" in result.output


def test_shell_rejects_bad_dot_commands(invoke: Callable, database: Path) -> None:
    result = invoke("shell", database, input=".nope\n.format xml\n.describe\n.truncate x\n")
    assert result.exit_code == 0
    assert "unknown command .nope" in result.output
    assert "unknown format: xml" in result.output
    assert "usage: .describe TABLE" in result.output
    assert "usage: .truncate N|off" in result.output


def test_shell_quit_stops_reading(invoke: Callable, database: Path) -> None:
    result = invoke("shell", database, input=".quit\nSELECT 'after' AS x;\n")
    assert result.exit_code == 0
    assert "after" not in result.output


def test_shell_eof_runs_pending_statement(invoke: Callable, database: Path) -> None:
    result = invoke("shell", database, input="SELECT 'pending' AS x")
    assert result.exit_code == 0
    assert "pending" in result.output


def test_shell_eof_exits_zero(invoke: Callable, database: Path) -> None:
    assert invoke("shell", database, input="").exit_code == 0


def test_shell_write_commits_each_statement(
    invoke: Callable, database: Path, count_users: Callable[[Path], int]
) -> None:
    result = invoke(
        "shell", database, "--write", input="INSERT INTO users (name) VALUES ('Zoe');\n"
    )
    assert result.exit_code == 0
    assert "OK (1 row affected)" in result.output
    assert count_users(database) == 4


def test_shell_is_read_only_by_default(
    invoke: Callable, database: Path, count_users: Callable[[Path], int]
) -> None:
    result = invoke("shell", database, input="DELETE FROM users;\n")
    assert result.exit_code == 0
    assert "--write" in result.output
    assert count_users(database) == 3


def test_shell_attach_option(invoke: Callable, database: Path, relational_database: Path) -> None:
    result = invoke(
        "shell",
        database,
        "--attach",
        f"o={relational_database}",
        input="SELECT title FROM o.posts WHERE id = 1;\n",
    )
    assert result.exit_code == 0
    assert "Hello world" in result.output


def test_shell_works_without_readline(
    invoke: Callable, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "readline", None)
    result = invoke("shell", database, input="SELECT 1 AS one;\n")
    assert result.exit_code == 0
    assert "one" in result.output


def test_completer_matches_tables_columns_keywords_and_dot_commands(database: Path) -> None:
    with open_database(database) as db:
        completer = Completer(db)
        assert completer("us", 0) == "users"
        assert completer("us", 1) is None
        assert completer("sel", 0) == "SELECT"
        assert completer("ava", 0) == "avatar"
        assert completer(".sc", 0) == ".schema"


def test_history_path_honours_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert history_path() == tmp_path / "sqlitexplorer" / "history"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert history_path() == Path.home() / ".local" / "state" / "sqlitexplorer" / "history"

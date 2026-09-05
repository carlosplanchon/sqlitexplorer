"""Interactive shell (REPL) on top of an open :class:`Explorer`.

Statements are accumulated until :func:`sqlite3.complete_statement` says they
are complete, then executed one by one. Lines starting with a dot are shell
commands. readline (history and Tab completion) is used when available and
the session is interactive.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import typer

from sqlitexplorer.core import Explorer, ExplorerError, split_statements, translate_error
from sqlitexplorer.render import OutputFormat, OutputOptions, emit, ok_message

__all__ = ["Completer", "DOT_COMMANDS", "SQL_KEYWORDS", "history_path", "run_shell"]

PROMPT = "sqlitexplorer> "
CONTINUATION = "        ...> "

SQL_KEYWORDS = (
    "ALL", "ALTER", "AND", "AS", "ASC", "ATTACH", "BEGIN", "BETWEEN", "BLOB", "BY",
    "CASE", "COMMIT", "COUNT", "CREATE", "DATABASE", "DELETE", "DESC", "DETACH",
    "DISTINCT", "DROP", "ELSE", "END", "EXISTS", "EXPLAIN", "FROM", "GROUP", "HAVING",
    "IN", "INDEX", "INNER", "INSERT", "INTEGER", "INTO", "IS", "JOIN", "KEY", "LEFT",
    "LIKE", "LIMIT", "MAX", "MIN", "NOT", "NULL", "OFFSET", "ON", "OR", "ORDER",
    "OUTER", "PLAN", "PRAGMA", "PRIMARY", "QUERY", "REAL", "ROLLBACK", "SELECT",
    "SET", "SUM", "TABLE", "TEXT", "THEN", "UNION", "UPDATE", "VALUES", "VIEW",
    "WHEN", "WHERE", "WITH",
)  # fmt: skip

DOT_COMMANDS = {
    ".tables": "list tables and views with their row counts",
    ".schema": "[NAME]  print CREATE statements",
    ".describe": "TABLE  show the columns of a table or view",
    ".indexes": "[TABLE]  list indexes",
    ".stats": "TABLE  per-column statistics",
    ".format": "table|csv|tsv|json|markdown  change the output format",
    ".null": "TEXT  text shown for NULL values",
    ".truncate": "N|off  truncate long values",
    ".help": "show this help",
    ".quit": "leave the shell (also .exit or Ctrl-D)",
}


class Completer:
    """readline completer over dot-commands, SQL keywords, table and column names."""

    def __init__(self, db: Explorer) -> None:
        self._db = db
        self._matches: list[str] = []

    def candidates(self) -> list[str]:
        names: list[str] = []
        try:
            for table in self._db.names():
                names.append(table)
                names.extend(self._db.column_names(table))
        except (ExplorerError, sqlite3.Error):
            pass
        return names

    def __call__(self, text: str, state: int) -> str | None:
        if state == 0:
            pool = (
                list(DOT_COMMANDS) if text.startswith(".") else [*SQL_KEYWORDS, *self.candidates()]
            )
            lowered = text.lower()
            matches = {item for item in pool if item.lower().startswith(lowered)}
            self._matches = sorted(matches, key=str.lower)
        return self._matches[state] if state < len(self._matches) else None


def history_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "sqlitexplorer" / "history"


def _setup_readline(completer: Completer) -> Callable[[], None] | None:
    """Configure readline if available; return a function that saves the history."""
    try:
        import readline
    except ImportError:
        return None
    readline.set_completer(completer)
    readline.set_completer_delims(" \t\n,;()")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    readline.set_history_length(1000)
    path = history_path()
    with suppress(OSError):
        readline.read_history_file(path)

    def save() -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(path)
        except OSError:
            pass

    return save


def _read_piped_line(prompt: str) -> str:
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\r\n")


def run_shell(
    db: Explorer,
    *,
    options: OutputOptions,
    write: bool,
    interactive: bool,
    report_error: Callable[[ExplorerError], None],
) -> None:
    """Read statements and dot-commands until EOF or ``.quit``."""
    save_history = _setup_readline(Completer(db)) if interactive else None
    read_line = input if interactive else _read_piped_line
    buffer = ""
    try:
        while True:
            try:
                line = read_line(CONTINUATION if buffer else PROMPT)
            except KeyboardInterrupt:
                if not interactive:
                    return
                typer.echo("")
                buffer = ""
                continue
            except EOFError:
                if buffer.strip():
                    _run_sql(db, buffer, options=options, write=write, report_error=report_error)
                if interactive:
                    typer.echo("")
                return
            stripped = line.strip()
            if not buffer and stripped.startswith("."):
                if not _dot_command(db, stripped, options=options, report_error=report_error):
                    return
                continue
            if not buffer and not stripped:
                continue
            buffer = f"{buffer}\n{line}" if buffer else line
            if sqlite3.complete_statement(buffer):
                _run_sql(db, buffer, options=options, write=write, report_error=report_error)
                buffer = ""
    finally:
        if save_history is not None:
            save_history()


def _run_sql(
    db: Explorer,
    sql: str,
    *,
    options: OutputOptions,
    write: bool,
    report_error: Callable[[ExplorerError], None],
) -> None:
    for statement in split_statements(sql):
        try:
            result = db.execute(statement)
        except sqlite3.Error as error:
            if write:
                db.rollback()
            report_error(translate_error(error, write=write))
            return
        try:
            if result.returns_rows:
                emit(result, options)
            else:
                typer.echo(ok_message(result))
        except ExplorerError as error:
            report_error(error)
        if write:
            db.commit()


def _dot_command(
    db: Explorer,
    line: str,
    *,
    options: OutputOptions,
    report_error: Callable[[ExplorerError], None],
) -> bool:
    """Run one dot-command. Returns False when the shell should exit."""
    command, _, argument = line.partition(" ")
    argument = argument.strip()
    try:
        if command in (".quit", ".exit"):
            return False
        if command == ".help":
            for name, description in DOT_COMMANDS.items():
                typer.echo(f"{name:<11}{description}")
        elif command == ".tables":
            emit(db.tables(), options, empty="(no tables)")
        elif command == ".schema":
            statements = db.schema(argument or None)
            typer.echo("\n\n".join(f"{s};" for s in statements) if statements else "(empty schema)")
        elif command == ".describe":
            emit(db.columns(_required(argument, command)), options)
        elif command == ".indexes":
            emit(db.indexes(argument or None), options, empty="(no indexes)")
        elif command == ".stats":
            emit(db.stats(_required(argument, command)), options)
        elif command == ".format":
            options.format = _parse_format(argument)
        elif command == ".null":
            options.null = argument
        elif command == ".truncate":
            options.truncate = _parse_truncate(argument)
        else:
            raise ExplorerError(f"unknown command {command}, try .help")
    except ExplorerError as error:
        report_error(error)
    except sqlite3.Error as error:
        report_error(translate_error(error, write=False))
    return True


def _required(argument: str, command: str) -> str:
    if not argument:
        raise ExplorerError(f"usage: {command} TABLE")
    return argument


def _parse_format(argument: str) -> OutputFormat:
    try:
        return OutputFormat(argument.lower())
    except ValueError:
        choices = ", ".join(fmt.value for fmt in OutputFormat)
        raise ExplorerError(
            f"unknown format: {argument or '(none)'}; choose from {choices}"
        ) from None


def _parse_truncate(argument: str) -> int | None:
    if argument.lower() in ("", "off"):
        return None
    try:
        return max(1, int(argument))
    except ValueError:
        raise ExplorerError("usage: .truncate N|off") from None

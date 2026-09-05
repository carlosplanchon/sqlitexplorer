"""SQLite access layer of sqlitexplorer.

The CLI never touches :mod:`sqlite3` directly: it opens a database with
:func:`open_database` and talks to the resulting :class:`Explorer`. Anything
that should reach the user as a plain message is raised as
:class:`ExplorerError`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Explorer",
    "ExplorerError",
    "ReadOnlyError",
    "ResultSet",
    "open_database",
    "quote_identifier",
]


class ExplorerError(Exception):
    """An error that should be shown to the user as a plain message."""


class ReadOnlyError(ExplorerError):
    """A write was attempted on a database that was opened read-only."""


@dataclass(frozen=True, slots=True)
class ResultSet:
    """Outcome of one SQL statement.

    ``columns`` is empty for statements that do not return rows (DDL, DML).
    ``rowcount`` is the number of rows changed by DML, or -1 when it does not
    apply.
    """

    columns: tuple[str, ...] = ()
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = -1

    @property
    def returns_rows(self) -> bool:
        """Whether the statement produced a result set, even an empty one."""
        return bool(self.columns)


def quote_identifier(name: str) -> str:
    """Quote *name* so it can be embedded in SQL as an identifier."""
    return '"' + name.replace('"', '""') + '"'


def _database_uri(path: Path, *, write: bool) -> str:
    # ``as_uri`` percent-encodes the characters that are special in URIs.
    mode = "rw" if write else "ro"
    return f"{path.resolve().as_uri()}?mode={mode}"


def _translate(error: sqlite3.Error, *, write: bool) -> ExplorerError:
    message = str(error)
    if not write and "readonly database" in message:
        return ReadOnlyError(message)
    return ExplorerError(message)


@contextmanager
def open_database(path: Path, *, write: bool = False) -> Iterator[Explorer]:
    """Open the database at *path* and yield an :class:`Explorer` bound to it.

    The file is opened read-only unless *write* is true, and it is never
    created. In write mode the transaction is committed when the ``with``
    block finishes normally and rolled back if it raises.
    """
    try:
        connection = sqlite3.connect(_database_uri(path, write=write), uri=True)
    except sqlite3.Error as error:
        raise ExplorerError(f"cannot open {path}: {error}") from error
    try:
        yield Explorer(connection)
        if write:
            connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise _translate(error, write=write) from error
    finally:
        connection.close()


class Explorer:
    """Explorer-oriented queries on top of an open :class:`sqlite3.Connection`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: tuple = ()) -> ResultSet:
        """Run a single SQL statement and collect its outcome."""
        cursor = self._connection.execute(sql, parameters)
        try:
            if cursor.description is None:
                return ResultSet(rowcount=cursor.rowcount)
            columns = tuple(name for name, *_ in cursor.description)
            return ResultSet(columns=columns, rows=cursor.fetchall(), rowcount=cursor.rowcount)
        finally:
            cursor.close()

    def tables(self, *, include_internal: bool = False) -> ResultSet:
        """List tables and views together with their row counts."""
        sql = "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view')"
        if not include_internal:
            sql += " AND name NOT LIKE 'sqlite_%'"
        sql += " ORDER BY type, name"
        rows = [(kind, name, self._count_rows(name)) for kind, name in self.execute(sql).rows]
        return ResultSet(columns=("type", "name", "rows"), rows=rows)

    def _count_rows(self, name: str) -> int | str:
        try:
            return self.execute(f"SELECT COUNT(*) FROM {quote_identifier(name)}").rows[0][0]
        except sqlite3.Error:
            # Typically a view whose underlying table no longer exists.
            return "?"

    def schema(self, name: str | None = None, *, include_internal: bool = False) -> list[str]:
        """Return the CREATE statements of the database.

        When *name* is given, only the table or view itself plus its indexes
        and triggers are returned. Otherwise SQLite's internal objects
        (``sqlite_*``) are skipped unless *include_internal* is true.
        """
        sql = "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
        parameters: tuple = ()
        if name is not None:
            sql += " AND (name = ? OR tbl_name = ?)"
            parameters = (name, name)
        elif not include_internal:
            sql += " AND name NOT LIKE 'sqlite_%'"
        # Creation order, like the sqlite3 shell: every object comes after
        # the ones it depends on, so the output can be replayed as a script.
        sql += " ORDER BY rowid"
        statements = [row[0] for row in self.execute(sql, parameters).rows]
        if name is not None and not statements:
            raise ExplorerError(f"no such table or view: {name}")
        return statements

    def columns(self, table: str) -> ResultSet:
        """Describe the columns of *table* as reported by ``PRAGMA table_info``."""
        result = self.execute(f"PRAGMA table_info({quote_identifier(table)})")
        if not result.rows:
            raise ExplorerError(f"no such table or view: {table}")
        return result

    def rows(self, table: str, *, limit: int | None = None, offset: int = 0) -> ResultSet:
        """Return the rows of *table*, optionally windowed by *limit* and *offset*."""
        sql = f"SELECT * FROM {quote_identifier(table)} LIMIT ? OFFSET ?"
        return self.execute(sql, (-1 if limit is None else limit, offset))

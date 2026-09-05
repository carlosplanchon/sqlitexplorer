"""SQLite access layer of sqlitexplorer.

The CLI never touches :mod:`sqlite3` directly: it opens a database with
:func:`open_database` and talks to the resulting :class:`Explorer`. Anything
that should reach the user as a plain message is raised as
:class:`ExplorerError`. Nothing in this module prints.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Explorer",
    "ExplorerError",
    "ReadOnlyError",
    "ResultSet",
    "open_database",
    "plan_tree",
    "quote_identifier",
    "split_statements",
    "translate_error",
]

Parameters = Sequence[object] | Mapping[str, object]


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


def split_statements(sql: str) -> list[str]:
    """Split *sql* into complete statements.

    Uses :func:`sqlite3.complete_statement`, so semicolons inside strings,
    comments and ``CREATE TRIGGER ... END`` blocks do not split. Trailing text
    without a semicolon is returned as a final statement.
    """
    statements: list[str] = []
    buffer = ""
    *parts, remainder = sql.split(";")
    for part in parts:
        buffer += part + ";"
        if sqlite3.complete_statement(buffer):
            if _has_content(buffer):
                statements.append(buffer.strip())
            buffer = ""
    leftover = buffer + remainder
    if _has_content(leftover):
        statements.append(leftover.strip())
    return statements


def _has_content(text: str) -> bool:
    return bool(text.strip().rstrip(";").strip())


def plan_tree(result: ResultSet) -> ResultSet:
    """Indent the ``detail`` column of an ``EXPLAIN QUERY PLAN`` result by depth."""
    parents = {row[0]: row[1] for row in result.rows}

    def depth(node: object) -> int:
        level, seen = 0, set()
        while node in parents and node not in seen and parents[node]:
            seen.add(node)
            node = parents[node]
            level += 1
        return level

    rows = [(row[0], row[1], "  " * depth(row[0]) + str(row[3])) for row in result.rows]
    return ResultSet(columns=("id", "parent", "detail"), rows=rows)


def _database_uri(path: Path, *, write: bool) -> str:
    # ``as_uri`` percent-encodes the characters that are special in URIs.
    mode = "rw" if write else "ro"
    return f"{path.resolve().as_uri()}?mode={mode}"


def translate_error(error: sqlite3.Error, *, write: bool) -> ExplorerError:
    """Turn a raw sqlite3 error into the ExplorerError shown to the user."""
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
        yield Explorer(connection, write=write)
        if write:
            connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise translate_error(error, write=write) from error
    finally:
        connection.close()


class Explorer:
    """Explorer-oriented queries on top of an open :class:`sqlite3.Connection`."""

    def __init__(self, connection: sqlite3.Connection, *, write: bool = False) -> None:
        self._connection = connection
        self._write = write

    # --- Statements -------------------------------------------------------

    def execute(self, sql: str, parameters: Parameters = ()) -> ResultSet:
        """Run a single SQL statement and collect its outcome."""
        cursor = self._connection.execute(sql, parameters)
        try:
            if cursor.description is None:
                return ResultSet(rowcount=cursor.rowcount)
            columns = tuple(name for name, *_ in cursor.description)
            return ResultSet(columns=columns, rows=cursor.fetchall(), rowcount=cursor.rowcount)
        finally:
            cursor.close()

    def explain(self, sql: str, parameters: Parameters = ()) -> ResultSet:
        """Return the query plan of *sql* as an indented tree."""
        return plan_tree(self.execute(f"EXPLAIN QUERY PLAN {sql}", parameters))

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def attach(self, alias: str, path: Path, *, write: bool | None = None) -> None:
        """Attach the database at *path* under *alias*, read-only unless *write*."""
        if write is None:
            write = self._write
        try:
            self._connection.execute(
                f"ATTACH DATABASE ? AS {quote_identifier(alias)}",
                (_database_uri(path, write=write),),
            )
        except sqlite3.Error as error:
            raise ExplorerError(f"cannot attach {path}: {error}") from error

    # --- Catalogue --------------------------------------------------------

    def names(
        self, *, kinds: Sequence[str] = ("table", "view"), include_internal: bool = False
    ) -> list[str]:
        """Names of the schema objects of the given *kinds*, sorted."""
        placeholders = ", ".join("?" for _ in kinds)
        sql = f"SELECT name FROM sqlite_master WHERE type IN ({placeholders})"
        if not include_internal:
            sql += " AND name NOT LIKE 'sqlite_%'"
        sql += " ORDER BY name"
        return [row[0] for row in self.execute(sql, tuple(kinds)).rows]

    def objects(self, *, include_internal: bool = False) -> list[tuple[str, str, str]]:
        """``(type, name, sql)`` of every object with a CREATE statement, in creation order."""
        sql = "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        if not include_internal:
            sql += " AND name NOT LIKE 'sqlite_%'"
        # Creation order, like the sqlite3 shell: every object comes after
        # the ones it depends on, so the output can be replayed as a script.
        sql += " ORDER BY rowid"
        return [(kind, name, statement) for kind, name, statement in self.execute(sql).rows]

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
        if name is None:
            return [
                statement for _, _, statement in self.objects(include_internal=include_internal)
            ]
        result = self.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
            " AND (name = ? OR tbl_name = ?) ORDER BY rowid",
            (name, name),
        )
        statements = [row[0] for row in result.rows]
        if not statements:
            raise ExplorerError(f"no such table or view: {name}")
        return statements

    def columns(self, table: str) -> ResultSet:
        """Describe the columns of *table* as reported by ``PRAGMA table_info``."""
        result = self.execute(f"PRAGMA table_info({quote_identifier(table)})")
        if not result.rows:
            raise ExplorerError(f"no such table or view: {table}")
        return result

    def column_names(self, table: str) -> list[str]:
        return [row[1] for row in self.columns(table).rows]

    def _resolve_column(self, table: str, name: str, known: Sequence[str]) -> str:
        for column in known:
            if column.casefold() == name.casefold():
                return column
        raise ExplorerError(f"no such column in {table}: {name}")

    def indexes(self, table: str | None = None) -> ResultSet:
        """Indexes of *table*, or of every table, with the columns they cover."""
        tables = self._tables_or_one(table)
        rows = []
        for name in tables:
            index_list = self.execute(f"PRAGMA index_list({quote_identifier(name)})").rows
            for _seq, index, unique, origin, partial in index_list:
                info = self.execute(f"PRAGMA index_info({quote_identifier(index)})").rows
                covered = ", ".join("<expr>" if column is None else column for _, _, column in info)
                rows.append((name, index, unique, origin, partial, covered))
        return ResultSet(
            columns=("table", "name", "unique", "origin", "partial", "columns"), rows=rows
        )

    def foreign_keys(self, table: str | None = None) -> ResultSet:
        """Foreign keys declared by *table*, or by every table."""
        tables = self._tables_or_one(table)
        rows = []
        for name in tables:
            fks = self.execute(f"PRAGMA foreign_key_list({quote_identifier(name)})").rows
            for fk_id, seq, referenced, source, target, on_update, on_delete, match in fks:
                rows.append(
                    (name, fk_id, seq, source, referenced, target, on_update, on_delete, match)
                )
        return ResultSet(
            columns=(
                "table",
                "id",
                "seq",
                "from",
                "references",
                "to",
                "on_update",
                "on_delete",
                "match",
            ),
            rows=rows,
        )

    def _tables_or_one(self, table: str | None) -> list[str]:
        if table is None:
            return self.names(kinds=("table",))
        self.columns(table)  # fail early with a clear message
        return [table]

    def info(self, *, check: bool = False) -> ResultSet:
        """Facts about the database file: size, pragmas and object counts."""
        databases = self.execute("PRAGMA database_list").rows
        path = next((file for _, name, file in databases if name == "main"), "")
        try:
            size: int | None = Path(path).stat().st_size if path else None
        except OSError:
            size = None
        entries: list[tuple[str, object]] = [
            ("path", path),
            ("size", size),
            ("sqlite_version", sqlite3.sqlite_version),
        ]
        pragmas = (
            "page_size",
            "page_count",
            "freelist_count",
            "journal_mode",
            "encoding",
            "user_version",
            "application_id",
        )
        for pragma in pragmas:
            entries.append((pragma, self.execute(f"PRAGMA {pragma}").rows[0][0]))
        counts = dict(
            self.execute(
                "SELECT type, COUNT(*) FROM sqlite_master"
                " WHERE name NOT LIKE 'sqlite_%' GROUP BY type"
            ).rows
        )
        plurals = {"table": "tables", "view": "views", "index": "indexes", "trigger": "triggers"}
        for kind, plural in plurals.items():
            entries.append((plural, counts.get(kind, 0)))
        if check:
            problems = self.execute("PRAGMA integrity_check").rows
            entries.append(("integrity_check", "; ".join(str(row[0]) for row in problems)))
        return ResultSet(columns=("key", "value"), rows=entries)

    # --- Data -------------------------------------------------------------

    def rows(
        self,
        table: str,
        *,
        columns: Sequence[str] | None = None,
        where: str | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> ResultSet:
        """Return the rows of *table*, optionally filtered, ordered and windowed.

        *where* is a raw SQL fragment; *columns* and *order_by* are validated
        against the table's columns (case-insensitively).
        """
        selection = "*"
        order_clause = ""
        if columns or order_by:
            known = self.column_names(table)
            if columns:
                selection = ", ".join(
                    quote_identifier(self._resolve_column(table, name, known)) for name in columns
                )
            if order_by:
                column = quote_identifier(self._resolve_column(table, order_by, known))
                order_clause = f" ORDER BY {column}{' DESC' if descending else ''}"
        sql = f"SELECT {selection} FROM {quote_identifier(table)}"
        if where:
            sql += f" WHERE ({where})"
        sql += f"{order_clause} LIMIT ? OFFSET ?"
        return self.execute(sql, (-1 if limit is None else limit, offset))

    def stats(self, table: str, *, top: int = 3) -> ResultSet:
        """Per-column summary of *table*: nulls, distinct values, min, max, top values."""
        q_table = quote_identifier(table)
        rows = []
        for _cid, name, declared, *_ in self.columns(table).rows:
            q_col = quote_identifier(name)
            nulls, distinct, minimum, maximum, min_type = self.execute(
                f"SELECT COUNT(*) - COUNT({q_col}), COUNT(DISTINCT {q_col}),"
                f" MIN({q_col}), MAX({q_col}), typeof(MIN({q_col})) FROM {q_table}"
            ).rows[0]
            frequent = self.execute(
                f"SELECT quote({q_col}), COUNT(*) AS n FROM {q_table}"
                f" WHERE {q_col} IS NOT NULL GROUP BY {q_col} ORDER BY n DESC, 1 LIMIT ?",
                (top,),
            ).rows
            summary = ", ".join(f"{value} ({count})" for value, count in frequent)
            rows.append((name, declared or min_type, nulls, distinct, minimum, maximum, summary))
        return ResultSet(
            columns=("column", "type", "nulls", "distinct", "min", "max", "top"), rows=rows
        )

    def search(
        self, text: str, *, tables: Sequence[str] | None = None, limit: int | None = None
    ) -> ResultSet:
        """Find *text* (case-insensitive substring) in every non-BLOB column.

        Each table is scanned once, whatever its number of columns; one row is
        reported per matching column.
        """
        if not text:
            raise ExplorerError("nothing to search for")
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        if tables is None:
            tables = self.names(kinds=("table",))
        remaining = limit
        rows = []
        for table in tables:
            if remaining is not None and remaining <= 0:
                break
            columns = self.column_names(table)
            for record in self._scan_table(table, columns, pattern, remaining):
                rowid, values, matched = (
                    record[0],
                    record[1:][: len(columns)],
                    record[1 + len(columns) :],
                )
                for column, value, hit in zip(columns, values, matched, strict=True):
                    if not hit:
                        continue
                    rows.append((table, column, rowid, value))
                    if remaining is not None:
                        remaining -= 1
                        if remaining <= 0:
                            break
                if remaining is not None and remaining <= 0:
                    break
        return ResultSet(columns=_SEARCH_COLUMNS, rows=rows)

    def _scan_table(
        self, table: str, columns: Sequence[str], pattern: str, limit: int | None
    ) -> list[tuple]:
        """One pass over *table* returning ``rowid, values..., matched flags...`` per hit row."""
        quoted = [quote_identifier(column) for column in columns]
        flags = [f'"__match_{index}"' for index in range(len(columns))]
        tests = ", ".join(
            f"(typeof({column}) <> 'blob' AND CAST({column} AS TEXT) LIKE ? ESCAPE '\\') AS {flag}"
            for column, flag in zip(quoted, flags, strict=True)
        )
        selection = ", ".join(quoted)
        parameters = (*[pattern] * len(columns), -1 if limit is None else limit)

        def sql(rowid: str) -> str:
            return (
                f'SELECT * FROM (SELECT {rowid} AS "__rowid", {selection}, {tests}'
                f" FROM {quote_identifier(table)}) WHERE {' OR '.join(flags)} LIMIT ?"
            )

        try:
            return self.execute(sql("rowid"), parameters).rows
        except sqlite3.OperationalError as error:
            if "no such column: rowid" not in str(error):
                raise
            # WITHOUT ROWID tables and views have no rowid to report.
            return self.execute(sql("NULL"), parameters).rows

    def dump(self) -> Iterator[str]:
        """The database as replayable SQL, one statement per item."""
        return self._connection.iterdump()

    def import_rows(
        self,
        table: str,
        columns: Sequence[tuple[str, str]],
        rows: Iterable[Sequence[object]],
        *,
        create: bool = True,
    ) -> int:
        """Insert *rows* into *table*, creating it from *columns* (``(name, type)``) if needed."""
        q_table = quote_identifier(table)
        exists = self.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).rows
        if not exists:
            if not create:
                raise ExplorerError(f"no such table: {table}")
            definition = ", ".join(f"{quote_identifier(name)} {kind}" for name, kind in columns)
            self._connection.execute(f"CREATE TABLE {q_table} ({definition})")
        names = ", ".join(quote_identifier(name) for name, _ in columns)
        placeholders = ", ".join("?" for _ in columns)
        cursor = self._connection.executemany(
            f"INSERT INTO {q_table} ({names}) VALUES ({placeholders})", rows
        )
        try:
            return cursor.rowcount
        finally:
            cursor.close()


_SEARCH_COLUMNS = ("table", "column", "rowid", "value")

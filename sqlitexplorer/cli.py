"""Command-line interface of sqlitexplorer, built with Typer."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, NoReturn

import outfancy.table
import typer

from sqlitexplorer import __version__
from sqlitexplorer.core import ExplorerError, ReadOnlyError, ResultSet, open_database

app = typer.Typer(
    help="Explore SQLite databases from the terminal.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_show_locals=False,
)

# Parameters shared by several commands.
DatabaseArg = Annotated[
    Path,
    typer.Argument(
        exists=True,
        dir_okay=False,
        readable=True,
        show_default=False,
        help="Path to the SQLite database file.",
    ),
]
TableArg = Annotated[str, typer.Argument(show_default=False, help="Name of a table or view.")]
AllOption = Annotated[
    bool,
    typer.Option("--all", "-a", help="Include SQLite's internal objects (sqlite_*)."),
]
ColorOption = Annotated[
    bool | None,
    typer.Option(
        "--color/--no-color",
        help="Force or disable ANSI colors. By default they are used only on a terminal "
        "and disabled when NO_COLOR is set.",
    ),
]
WidthOption = Annotated[
    int | None,
    typer.Option(
        "--width",
        "-W",
        min=1,
        help="Width in columns to fit the table into. Defaults to the terminal width.",
    ),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sqlitexplorer {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Explore SQLite databases from the terminal."""


def _fail(message: str) -> NoReturn:
    typer.secho(f"Error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(code=1)


@contextmanager
def _reporting_errors() -> Iterator[None]:
    """Turn :class:`ExplorerError` into a message on stderr and exit status 1."""
    try:
        yield
    except ReadOnlyError as error:
        _fail(f"{error}. Pass --write to allow changes.")
    except ExplorerError as error:
        _fail(str(error))


def _format_value(value: object) -> object:
    """Show NULL and BLOB values the way SQLite itself writes them."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"X'{value.hex().upper()}'"
    return value


def _natural_widths(columns: Sequence[str], rows: Sequence[tuple]) -> list[int]:
    """Width each column needs to show its label and every value in full."""
    widths = [len(label) for label in columns]
    for row in rows:
        for index, value in enumerate(row):
            longest = max((len(line) for line in str(value).splitlines()), default=0)
            widths[index] = max(widths[index], longest)
    return widths


def _render(result: ResultSet, *, width: int | None, empty: str) -> str:
    """Render *result* as an outfancy table.

    outfancy sizes columns from the data alone and clips labels that do not
    fit, so whenever the whole table fits on the screen the widths are passed
    explicitly to keep the labels intact. Otherwise outfancy decides which
    columns fit.
    """
    table = outfancy.table.Table()
    table.set_empty_string(empty)
    rows = [tuple(_format_value(value) for value in row) for row in result.rows]
    columns = list(result.columns)

    screen = width if width is not None else shutil.get_terminal_size().columns
    natural = _natural_widths(columns, rows)
    # outfancy keeps a two-column margin and one separator per column.
    fits = sum(natural) + len(natural) <= screen - 2
    return table.render(
        data=rows,
        label_list=columns,
        width=natural if fits else None,
        screen_x=width,
    )


def _echo_table(
    result: ResultSet,
    *,
    color: bool | None,
    width: int | None,
    empty: str = "(no rows)",
) -> None:
    if color is None and os.environ.get("NO_COLOR"):
        color = False
    typer.echo(_render(result, width=width, empty=empty), color=color)


@app.command()
def tables(
    database: DatabaseArg,
    include_internal: AllOption = False,
    color: ColorOption = None,
    width: WidthOption = None,
) -> None:
    """List the tables and views of the database with their row counts."""
    with _reporting_errors(), open_database(database) as db:
        result = db.tables(include_internal=include_internal)
    _echo_table(result, color=color, width=width, empty="(no tables)")


@app.command()
def schema(
    database: DatabaseArg,
    name: Annotated[
        str | None,
        typer.Argument(
            show_default=False,
            help="Show only this table or view, with its indexes and triggers.",
        ),
    ] = None,
    include_internal: AllOption = False,
) -> None:
    """Print the CREATE statements stored in the database."""
    with _reporting_errors(), open_database(database) as db:
        statements = db.schema(name, include_internal=include_internal)
    if not statements:
        typer.echo("(empty schema)")
        return
    typer.echo("\n\n".join(f"{statement};" for statement in statements))


@app.command()
def describe(
    database: DatabaseArg,
    table: TableArg,
    color: ColorOption = None,
    width: WidthOption = None,
) -> None:
    """Show the columns of a table or view: type, NOT NULL, default and primary key."""
    with _reporting_errors(), open_database(database) as db:
        result = db.columns(table)
    _echo_table(result, color=color, width=width)


@app.command()
def show(
    database: DatabaseArg,
    table: TableArg,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=0, help="Maximum number of rows to print."),
    ] = None,
    offset: Annotated[int, typer.Option("--offset", min=0, help="Number of rows to skip.")] = 0,
    color: ColorOption = None,
    width: WidthOption = None,
) -> None:
    """Print the rows of a table or view."""
    with _reporting_errors(), open_database(database) as db:
        result = db.rows(table, limit=limit, offset=offset)
    _echo_table(result, color=color, width=width)


@app.command()
def query(
    database: DatabaseArg,
    sql: Annotated[
        str,
        typer.Argument(
            show_default=False, help="SQL statement to run, or - to read it from stdin."
        ),
    ],
    write: Annotated[
        bool,
        typer.Option("--write", "-w", help="Open the database read-write and commit the changes."),
    ] = False,
    color: ColorOption = None,
    width: WidthOption = None,
) -> None:
    """Run a single SQL statement and print its result."""
    if sql == "-":
        sql = sys.stdin.read()
    if not sql.strip():
        _fail("no SQL statement given")
    with _reporting_errors(), open_database(database, write=write) as db:
        result = db.execute(sql)
    if result.returns_rows:
        _echo_table(result, color=color, width=width)
    elif result.rowcount >= 0:
        plural = "" if result.rowcount == 1 else "s"
        typer.echo(f"OK ({result.rowcount} row{plural} affected)")
    else:
        typer.echo("OK")


def main() -> None:
    """Entry point of the ``sqlitexplorer`` console script."""
    app()

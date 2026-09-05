"""Command-line interface of sqlitexplorer, built with Typer.

This module only declares commands and options; the SQL lives in
:mod:`sqlitexplorer.core`, the output formats in :mod:`sqlitexplorer.render`,
the charts in :mod:`sqlitexplorer.charts` and the REPL in
:mod:`sqlitexplorer.shell`.
"""

from __future__ import annotations

import difflib
import functools
import inspect
import shutil
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from sqlitexplorer import __version__
from sqlitexplorer.charts import (
    ChartKind,
    histogram_values,
    render_chart,
    render_histogram,
    series_from_result,
)
from sqlitexplorer.completion import complete_table
from sqlitexplorer.core import (
    Explorer,
    ExplorerError,
    ReadOnlyError,
    open_database,
    split_statements,
)
from sqlitexplorer.render import (
    OutputFormat,
    OutputOptions,
    coerce_rows,
    emit,
    infer_types,
    ok_message,
    parse_number,
    parse_rows,
    render,
    resolve_color,
    stdout_is_tty,
    strip_ansi,
)
from sqlitexplorer.shell import run_shell

app = typer.Typer(
    help="Explore SQLite databases from the terminal.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_show_locals=False,
)

# --- Parameters shared by several commands ------------------------------------

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
TableArg = Annotated[
    str,
    typer.Argument(
        show_default=False, help="Name of a table or view.", autocompletion=complete_table
    ),
]
OptionalTableArg = Annotated[
    str | None,
    typer.Argument(
        show_default=False,
        help="Name of a table (default: every table).",
        autocompletion=complete_table,
    ),
]
AllOption = Annotated[
    bool,
    typer.Option("--all", "-a", help="Include SQLite's internal objects (sqlite_*)."),
]
WriteOption = Annotated[
    bool,
    typer.Option("--write", "-w", help="Open the database read-write and commit the changes."),
]
ParamOption = Annotated[
    list[str] | None,
    typer.Option("--param", "-p", help="Bind a named parameter, as NAME=VALUE. Repeatable."),
]
AttachOption = Annotated[
    list[str] | None,
    typer.Option("--attach", help="Attach another database, as ALIAS=PATH. Repeatable."),
]

# Output options. Every command that prints a result set takes all of them.
FormatOption = Annotated[
    OutputFormat,
    typer.Option("--format", "-f", case_sensitive=False, help="Output format."),
]
NullOption = Annotated[
    str,
    typer.Option("--null", help="Text shown for NULL values (table, csv, tsv and markdown)."),
]
TruncateOption = Annotated[
    int | None,
    typer.Option("--truncate", min=1, help="Truncate values longer than N characters."),
]
PageOption = Annotated[
    int | None,
    typer.Option("--page", min=1, help="Print only page N of the rows."),
]
PageSizeOption = Annotated[
    int | None,
    typer.Option("--page-size", min=1, help="Rows per page (default: what fits on the screen)."),
]
PagerOption = Annotated[
    bool,
    typer.Option("--pager", help="Send the output to $PAGER (default: less -R) on a terminal."),
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
        help="Width in columns to fit the output into. Defaults to the terminal width.",
    ),
]

_EXTENSIONS = {
    OutputFormat.TABLE: "txt",
    OutputFormat.CSV: "csv",
    OutputFormat.TSV: "tsv",
    OutputFormat.JSON: "json",
    OutputFormat.MARKDOWN: "md",
}
_FORMAT_BY_SUFFIX = {".csv": OutputFormat.CSV, ".tsv": OutputFormat.TSV, ".json": OutputFormat.JSON}


# --- Helpers ------------------------------------------------------------------


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


def _error_message(error: ExplorerError) -> str:
    if isinstance(error, ReadOnlyError):
        return f"{error}. Pass --write to allow changes."
    return str(error)


def _report(error: ExplorerError) -> None:
    typer.secho(f"Error: {_error_message(error)}", err=True, fg=typer.colors.RED)


def _fail(message: str) -> NoReturn:
    typer.secho(f"Error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(code=1)


@contextmanager
def _reporting_errors() -> Iterator[None]:
    """Turn ExplorerError and OSError into a message on stderr and exit status 1."""
    try:
        yield
    except ExplorerError as error:
        _fail(_error_message(error))
    except OSError as error:
        _fail(str(error))


OUTPUT_PARAMETERS: dict[str, tuple[object, object]] = {
    "output_format": (FormatOption, OutputFormat.TABLE),
    "null": (NullOption, "NULL"),
    "truncate": (TruncateOption, None),
    "page": (PageOption, None),
    "page_size": (PageSizeOption, None),
    "pager": (PagerOption, False),
    "color": (ColorOption, None),
    "width": (WidthOption, None),
}


def with_output_options(command: Callable[..., None]) -> Callable[..., None]:
    """Expose the shared output options on *command* and hand them over as ``options``.

    Typer has no option groups, so instead of repeating the eight output
    parameters in every command this decorator appends them to the signature
    Typer inspects, and packs the values it receives into an
    :class:`OutputOptions` passed to the command as its ``options`` argument.
    """
    signature = inspect.signature(command, eval_str=True)
    own = [parameter for parameter in signature.parameters.values() if parameter.name != "options"]
    shared = [
        inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation
        )
        for name, (annotation, default) in OUTPUT_PARAMETERS.items()
    ]

    @functools.wraps(command)
    def wrapper(**arguments: object) -> None:
        values = {name: arguments.pop(name) for name in OUTPUT_PARAMETERS}
        values["format"] = values.pop("output_format")
        command(options=OutputOptions(**values), **arguments)

    wrapper.__signature__ = signature.replace(parameters=[*own, *shared])  # type: ignore[attr-defined]
    wrapper.__annotations__ = {
        parameter.name: parameter.annotation for parameter in [*own, *shared]
    }
    return wrapper


def _pairs(values: Sequence[str] | None, *, option: str) -> list[tuple[str, str]]:
    pairs = []
    for value in values or []:
        name, separator, rest = value.partition("=")
        if not separator or not name.strip():
            _fail(f"{option} expects NAME=VALUE, got {value!r}")
        pairs.append((name.strip(), rest))
    return pairs


def _parameters(values: Sequence[str] | None) -> dict[str, object]:
    return {name.lstrip(":@$"): parse_number(raw) for name, raw in _pairs(values, option="--param")}


def _attach_all(db: Explorer, values: Sequence[str] | None, *, write: bool) -> None:
    for alias, raw_path in _pairs(values, option="--attach"):
        path = Path(raw_path)
        if not path.is_file():
            _fail(f"--attach: no such file: {path}")
        db.attach(alias, path, write=write)


def _read_sql(sql: str | None, file: Path | None) -> list[str]:
    if (sql is None) == (file is None):
        _fail("give either an SQL statement or --file, not both")
    if file is not None:
        text = file.read_text(encoding="utf-8")
    elif sql == "-":
        text = sys.stdin.read()
    else:
        text = sql or ""
    statements = split_statements(text)
    if not statements:
        _fail("no SQL statement given")
    return statements


def _split_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


# --- Exploration --------------------------------------------------------------


@app.command()
@with_output_options
def tables(
    database: DatabaseArg,
    include_internal: AllOption = False,
    *,
    options: OutputOptions,
) -> None:
    """List the tables and views of the database with their row counts."""
    with _reporting_errors(), open_database(database) as db:
        emit(db.tables(include_internal=include_internal), options, empty="(no tables)")


@app.command()
def schema(
    database: DatabaseArg,
    name: Annotated[
        str | None,
        typer.Argument(
            show_default=False,
            help="Show only this table or view, with its indexes and triggers.",
            autocompletion=complete_table,
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
@with_output_options
def describe(
    database: DatabaseArg,
    table: TableArg,
    *,
    options: OutputOptions,
) -> None:
    """Show the columns of a table or view: type, NOT NULL, default and primary key."""
    with _reporting_errors(), open_database(database) as db:
        emit(db.columns(table), options)


@app.command()
@with_output_options
def indexes(
    database: DatabaseArg,
    table: OptionalTableArg = None,
    *,
    options: OutputOptions,
) -> None:
    """List the indexes of a table, or of every table, with the columns they cover."""
    with _reporting_errors(), open_database(database) as db:
        emit(db.indexes(table), options, empty="(no indexes)")


@app.command()
@with_output_options
def foreign_keys(
    database: DatabaseArg,
    table: OptionalTableArg = None,
    *,
    options: OutputOptions,
) -> None:
    """List the foreign keys declared by a table, or by every table."""
    with _reporting_errors(), open_database(database) as db:
        emit(db.foreign_keys(table), options, empty="(no foreign keys)")


@app.command()
@with_output_options
def info(
    database: DatabaseArg,
    check: Annotated[
        bool, typer.Option("--check", help="Run PRAGMA integrity_check (slow on big files).")
    ] = False,
    *,
    options: OutputOptions,
) -> None:
    """Show facts about the database file: size, pragmas and object counts."""
    with _reporting_errors(), open_database(database) as db:
        emit(db.info(check=check), options)


@app.command()
@with_output_options
def show(
    database: DatabaseArg,
    table: TableArg,
    columns: Annotated[
        str | None,
        typer.Option("--columns", "-c", help="Comma-separated list of columns to print."),
    ] = None,
    where: Annotated[
        str | None,
        typer.Option("--where", help="Condition to filter the rows (raw SQL)."),
    ] = None,
    order_by: Annotated[str | None, typer.Option("--order-by", help="Column to sort by.")] = None,
    descending: Annotated[bool, typer.Option("--desc", help="Sort in descending order.")] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=0, help="Maximum number of rows to print."),
    ] = None,
    offset: Annotated[int, typer.Option("--offset", min=0, help="Number of rows to skip.")] = 0,
    *,
    options: OutputOptions,
) -> None:
    """Print the rows of a table or view."""
    with _reporting_errors(), open_database(database) as db:
        result = db.rows(
            table,
            columns=_split_list(columns),
            where=where,
            order_by=order_by,
            descending=descending,
            limit=limit,
            offset=offset,
        )
        emit(result, options)


@app.command()
@with_output_options
def stats(
    database: DatabaseArg,
    table: TableArg,
    top: Annotated[
        int, typer.Option("--top", min=0, help="How many frequent values to list per column.")
    ] = 3,
    *,
    options: OutputOptions,
) -> None:
    """Per-column statistics: nulls, distinct values, min, max and most frequent values."""
    with _reporting_errors(), open_database(database) as db:
        emit(db.stats(table, top=top), options)


@app.command()
@with_output_options
def search(
    database: DatabaseArg,
    text: Annotated[
        str, typer.Argument(show_default=False, help="Text to look for (case-insensitive).")
    ],
    table: Annotated[
        list[str] | None,
        typer.Option(
            "--table",
            "-t",
            help="Only search these tables. Repeatable.",
            autocompletion=complete_table,
        ),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", min=1, help="Stop after N matches.")
    ] = None,
    *,
    options: OutputOptions,
) -> None:
    """Find a text in every column of every table."""
    with _reporting_errors(), open_database(database) as db:
        emit(db.search(text, tables=table, limit=limit), options, empty="(no matches)")


# --- Queries ------------------------------------------------------------------


@app.command()
@with_output_options
def query(
    database: DatabaseArg,
    sql: Annotated[
        str | None,
        typer.Argument(show_default=False, help="SQL to run, or - to read it from stdin."),
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            "-F",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Read the SQL from a file.",
        ),
    ] = None,
    write: WriteOption = False,
    explain: Annotated[
        bool, typer.Option("--explain", help="Show the query plan instead of running it.")
    ] = False,
    time_it: Annotated[
        bool, typer.Option("--time", help="Report rows and elapsed time on stderr.")
    ] = False,
    params: ParamOption = None,
    attach: AttachOption = None,
    watch: Annotated[
        float | None,
        typer.Option("--watch", min=0.1, help="Re-run every N seconds until Ctrl-C."),
    ] = None,
    *,
    options: OutputOptions,
) -> None:
    """Run SQL statements and print their results."""
    statements = _read_sql(sql, file)
    if watch is not None and (options.pager or write):
        _fail("--watch cannot be combined with --pager or --write")
    parameters = _parameters(params)
    with _reporting_errors(), open_database(database, write=write) as db:
        _attach_all(db, attach, write=write)
        try:
            first = True
            while True:
                if watch is not None:
                    if stdout_is_tty():
                        typer.echo("\x1b[2J\x1b[H", nl=False)
                    elif not first:
                        typer.echo("")
                for statement in statements:
                    _run_statement(
                        db, statement, parameters, options=options, explain=explain, time_it=time_it
                    )
                if watch is None:
                    return
                first = False
                time.sleep(watch)
        except KeyboardInterrupt:
            if watch is None:
                raise
            typer.echo("", err=True)


def _run_statement(
    db: Explorer,
    statement: str,
    parameters: dict[str, object],
    *,
    options: OutputOptions,
    explain: bool,
    time_it: bool,
) -> None:
    started = time.perf_counter()
    bound = parameters if parameters else ()
    result = db.explain(statement, bound) if explain else db.execute(statement, bound)
    elapsed = (time.perf_counter() - started) * 1000
    if result.returns_rows:
        emit(result, options)
    else:
        typer.echo(ok_message(result))
    if time_it:
        plural = "" if len(result.rows) == 1 else "s"
        typer.echo(f"{len(result.rows)} row{plural} in {elapsed:.1f} ms", err=True)


@app.command()
def chart(
    database: DatabaseArg,
    sql: Annotated[
        str,
        typer.Argument(
            show_default=False,
            help="Query whose first column is X and the other numeric columns are series.",
        ),
    ],
    kind: Annotated[
        ChartKind, typer.Option("--kind", "-k", case_sensitive=False, help="Kind of chart.")
    ] = ChartKind.LINE,
    height: Annotated[int, typer.Option("--height", min=3, help="Height in rows.")] = 15,
    bins: Annotated[int, typer.Option("--bins", min=1, help="Bins of a histogram.")] = 10,
    x_label: Annotated[str | None, typer.Option("--x-label", help="Label of the X axis.")] = None,
    y_label: Annotated[str | None, typer.Option("--y-label", help="Label of the Y axis.")] = None,
    params: ParamOption = None,
    width: WidthOption = None,
    color: ColorOption = None,
) -> None:
    """Draw the result of a query as a line chart, scatter plot or histogram."""
    parameters = _parameters(params)
    with _reporting_errors(), open_database(database) as db:
        text = sys.stdin.read() if sql == "-" else sql
        result = db.execute(text, parameters if parameters else ())
        if not result.returns_rows:
            _fail("the statement returned no rows")
        use_color = resolve_color(color)
        screen = width if width is not None else shutil.get_terminal_size().columns
        if kind is ChartKind.HIST:
            values, skipped = histogram_values(result)
            drawing = render_histogram(
                values,
                bins=bins,
                width=screen,
                height=height,
                color=use_color,
                x_label=x_label or result.columns[0],
                y_label=y_label or "count",
            )
        else:
            series, skipped = series_from_result(result)
            default_y = series[0].label if len(series) == 1 else "value"
            drawing = render_chart(
                series,
                kind=kind,
                width=screen,
                height=height,
                color=use_color,
                x_label=x_label or result.columns[0],
                y_label=y_label or default_y,
            )
        if skipped:
            plural = "" if skipped == 1 else "s"
            typer.echo(f"skipped {skipped} row{plural} with NULL values", err=True)
    typer.echo(drawing, color=use_color)


# --- Export / import ----------------------------------------------------------


@app.command()
def dump(
    database: DatabaseArg,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", dir_okay=False, help="Write to this file instead of stdout."
        ),
    ] = None,
) -> None:
    """Print the whole database as SQL, like the .dump command of the sqlite3 shell."""
    with _reporting_errors(), open_database(database) as db:
        text = "\n".join(db.dump())
        if output is None:
            typer.echo(text)
        else:
            output.write_text(text + "\n", encoding="utf-8")


@app.command()
def export(
    database: DatabaseArg,
    table: OptionalTableArg = None,
    every: Annotated[
        bool,
        typer.Option(
            "--all", "-a", help="Export every table and view, one file each, into --output."
        ),
    ] = False,
    output_format: FormatOption = OutputFormat.CSV,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="File to write, or directory with --all. Default: stdout."
        ),
    ] = None,
    null: NullOption = "NULL",
    truncate: TruncateOption = None,
) -> None:
    """Write the rows of a table (or of every table) to a file."""
    if (table is None) == (not every):
        _fail("give a table name or --all")
    if every and output is None:
        _fail("--all needs --output DIRECTORY")
    options = OutputOptions(format=output_format, null=null, truncate=truncate, color=False)
    with _reporting_errors(), open_database(database) as db:
        if every:
            assert output is not None
            output.mkdir(parents=True, exist_ok=True)
            for name in db.names():
                text = strip_ansi(render(db.rows(name), options))
                (output / f"{name}.{_EXTENSIONS[output_format]}").write_text(
                    text + "\n", encoding="utf-8"
                )
            return
        assert table is not None
        text = strip_ansi(render(db.rows(table), options))
        if output is None:
            typer.echo(text)
        else:
            output.write_text(text + "\n", encoding="utf-8")


@app.command("import")
def import_(
    database: DatabaseArg,
    table: TableArg,
    file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
            help="CSV, TSV or JSON file to load.",
        ),
    ],
    output_format: Annotated[
        OutputFormat | None,
        typer.Option(
            "--format",
            "-f",
            case_sensitive=False,
            help="Input format (default: from the file extension).",
        ),
    ] = None,
    delimiter: Annotated[
        str | None, typer.Option("--delimiter", help="Field delimiter for CSV/TSV.")
    ] = None,
) -> None:
    """Load a CSV, TSV or JSON file into a table, creating it if needed."""
    input_format = output_format or _FORMAT_BY_SUFFIX.get(file.suffix.lower())
    if input_format is None:
        _fail(f"cannot tell the format of {file.name}; pass --format")
    with _reporting_errors(), open_database(database, write=True) as db:
        headers, raw_rows = parse_rows(
            file.read_text(encoding="utf-8-sig"), input_format, delimiter=delimiter
        )
        types = infer_types(raw_rows, len(headers))
        count = db.import_rows(
            table, list(zip(headers, types, strict=True)), coerce_rows(raw_rows, types)
        )
    plural = "" if count == 1 else "s"
    typer.echo(f"OK ({count} row{plural} imported into {table})")


@app.command()
def diff(
    left: DatabaseArg,
    right: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
            help="Database to compare against.",
        ),
    ],
    include_internal: AllOption = False,
) -> None:
    """Compare the schemas of two databases. Exit status 1 when they differ."""
    with _reporting_errors():
        with open_database(left) as db:
            before = {(k, n): s for k, n, s in db.objects(include_internal=include_internal)}
        with open_database(right) as db:
            after = {(k, n): s for k, n, s in db.objects(include_internal=include_internal)}
    lines: list[str] = []
    for key in sorted(set(before) | set(after)):
        kind, name = key
        if key not in after:
            lines.append(f"- {kind} {name}")
        elif key not in before:
            lines.append(f"+ {kind} {name}")
        elif before[key] != after[key]:
            lines.append(f"~ {kind} {name}")
            lines.extend(
                difflib.unified_diff(
                    before[key].splitlines(),
                    after[key].splitlines(),
                    fromfile=f"{left}:{name}",
                    tofile=f"{right}:{name}",
                    lineterm="",
                )
            )
    if not lines:
        typer.echo("(no differences)")
        return
    typer.echo("\n".join(lines))
    raise typer.Exit(code=1)


# --- Shell --------------------------------------------------------------------


@app.command()
def shell(
    database: DatabaseArg,
    write: WriteOption = False,
    attach: AttachOption = None,
    output_format: FormatOption = OutputFormat.TABLE,
    null: NullOption = "NULL",
    truncate: TruncateOption = None,
    color: ColorOption = None,
    width: WidthOption = None,
) -> None:
    """Open an interactive shell: SQL statements, dot-commands, history and completion."""
    options = OutputOptions(
        format=output_format, null=null, truncate=truncate, color=color, width=width
    )
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    with _reporting_errors(), open_database(database, write=write) as db:
        _attach_all(db, attach, write=write)
        run_shell(db, options=options, write=write, interactive=interactive, report_error=_report)


def main() -> None:
    """Entry point of the ``sqlitexplorer`` console script."""
    app()

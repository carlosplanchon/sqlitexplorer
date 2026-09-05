"""Presentation layer: output formats, pagination, pager, colors and the shared emit path.

Every result set printed by the CLI goes through :func:`emit`, so that
``--format``, ``--null``, ``--truncate``, ``--page``, ``--pager``, ``--color``
and ``--width`` behave identically in every command. This is also the only
module that talks to outfancy.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

import outfancy.table
import typer

from sqlitexplorer.core import ExplorerError, ResultSet

__all__ = [
    "OutputFormat",
    "OutputOptions",
    "coerce_rows",
    "emit",
    "format_value",
    "infer_types",
    "ok_message",
    "paginate",
    "parse_number",
    "parse_rows",
    "render",
    "resolve_color",
    "stdout_is_tty",
    "strip_ansi",
]

ELLIPSIS = "…"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_INTEGER = re.compile(r"^[+-]?\d+$")
_REAL = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


class OutputFormat(str, Enum):
    TABLE = "table"
    CSV = "csv"
    TSV = "tsv"
    JSON = "json"
    MARKDOWN = "markdown"


@dataclass
class OutputOptions:
    """How a result set should be printed. Mutable: the shell changes it at runtime."""

    format: OutputFormat = OutputFormat.TABLE
    null: str = "NULL"
    truncate: int | None = None
    page: int | None = None
    page_size: int | None = None
    pager: bool = False
    color: bool | None = None
    width: int | None = None


# --- Values -------------------------------------------------------------------


def format_value(value: object, *, null: str = "NULL", truncate: int | None = None) -> object:
    """Show NULL and BLOB values the way SQLite writes them, optionally truncated."""
    if value is None:
        return null
    if isinstance(value, bytes):
        value = f"X'{value.hex().upper()}'"
    if truncate is not None and isinstance(value, str) and len(value) > truncate:
        return value[: max(truncate - 1, 0)] + ELLIPSIS
    return value


def _formatted_rows(result: ResultSet, *, null: str, truncate: int | None) -> list[tuple]:
    return [
        tuple(format_value(v, null=null, truncate=truncate) for v in row) for row in result.rows
    ]


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse_number(text: str) -> int | float | str:
    """Turn *text* into an int or a float when it looks like one, else keep it."""
    stripped = text.strip()
    if _INTEGER.match(stripped):
        return int(stripped)
    if _REAL.match(stripped):
        return float(stripped)
    return text


def ok_message(result: ResultSet) -> str:
    """What to print for a statement that returned no rows."""
    if result.rowcount >= 0:
        plural = "" if result.rowcount == 1 else "s"
        return f"OK ({result.rowcount} row{plural} affected)"
    return "OK"


# --- Renderers ----------------------------------------------------------------


def _natural_widths(columns: Sequence[str], rows: Sequence[tuple]) -> list[int]:
    """Width each column needs to show its label and every value in full."""
    widths = [len(label) for label in columns]
    for row in rows:
        for index, value in enumerate(row):
            longest = max((len(line) for line in str(value).splitlines()), default=0)
            widths[index] = max(widths[index], longest)
    return widths


def render_table(
    result: ResultSet,
    *,
    width: int | None = None,
    empty: str = "(no rows)",
    null: str = "NULL",
    truncate: int | None = None,
) -> str:
    """Render *result* as an outfancy table.

    outfancy sizes columns from the data alone and clips labels that do not
    fit, so whenever the whole table fits on the screen the widths are passed
    explicitly to keep the labels intact. Otherwise outfancy decides which
    columns fit.
    """
    table = outfancy.table.Table()
    table.set_empty_string(empty)
    rows = _formatted_rows(result, null=null, truncate=truncate)
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


def render_csv(
    result: ResultSet, *, delimiter: str = ",", null: str = "NULL", truncate: int | None = None
) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerow(result.columns)
    writer.writerows(_formatted_rows(result, null=null, truncate=truncate))
    return buffer.getvalue().rstrip("\n")


def _json_value(value: object) -> object:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value


def render_json(result: ResultSet) -> str:
    records = [dict(zip(result.columns, map(_json_value, row), strict=True)) for row in result.rows]
    return json.dumps(records, ensure_ascii=False, indent=2)


def render_markdown(result: ResultSet, *, null: str = "NULL", truncate: int | None = None) -> str:
    def cell(value: object) -> str:
        text = str(value).replace("|", "\\|")
        return " ".join(text.splitlines()) if "\n" in text or "\r" in text else text

    lines = [
        "| " + " | ".join(cell(column) for column in result.columns) + " |",
        "|" + "|".join(" --- " for _ in result.columns) + "|",
    ]
    for row in _formatted_rows(result, null=null, truncate=truncate):
        lines.append("| " + " | ".join(cell(value) for value in row) + " |")
    return "\n".join(lines)


def render(result: ResultSet, options: OutputOptions, *, empty: str = "(no rows)") -> str:
    """Render *result* in the format selected by *options*."""
    fmt = OutputFormat(options.format)
    if fmt is OutputFormat.TABLE:
        return render_table(
            result, width=options.width, empty=empty, null=options.null, truncate=options.truncate
        )
    if fmt is OutputFormat.CSV:
        return render_csv(result, null=options.null, truncate=options.truncate)
    if fmt is OutputFormat.TSV:
        return render_csv(result, delimiter="\t", null=options.null, truncate=options.truncate)
    if fmt is OutputFormat.JSON:
        return render_json(result)
    return render_markdown(result, null=options.null, truncate=options.truncate)


# --- Pagination, colors and pager --------------------------------------------


def default_page_size() -> int:
    """Rows that fit on the terminal, leaving room for the labels and the footer."""
    return max(1, shutil.get_terminal_size().lines - 4)


def paginate(
    result: ResultSet, *, page: int | None = None, page_size: int | None = None
) -> tuple[ResultSet, str | None]:
    """Slice *result* to one page. Returns the page and a footer, or the result untouched."""
    if page is None and page_size is None:
        return result, None
    size = page_size or default_page_size()
    number = page or 1
    total = max(1, -(-len(result.rows) // size))
    if number > total:
        raise ExplorerError(f"page {number} is out of range (1-{total})")
    start = (number - 1) * size
    sliced = ResultSet(
        columns=result.columns, rows=result.rows[start : start + size], rowcount=result.rowcount
    )
    return sliced, f"page {number} of {total} ({len(result.rows)} rows)"


def stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def resolve_color(color: bool | None) -> bool:
    """An explicit flag wins; otherwise color only on a terminal and unless NO_COLOR is set."""
    if color is not None:
        return color
    return not os.environ.get("NO_COLOR") and stdout_is_tty()


def page_through(text: str) -> bool:
    """Send *text* to ``$PAGER`` (default ``less -R``). Returns False when not paged."""
    if not stdout_is_tty():
        return False
    command = shlex.split(os.environ.get("PAGER") or "less -R")
    if not command:
        return False
    try:
        subprocess.run(command, input=text, text=True, check=False)
    except FileNotFoundError:
        return False
    return True


def emit(result: ResultSet, options: OutputOptions, *, empty: str = "(no rows)") -> None:
    """Print *result* honouring every output option. The single print path of the CLI."""
    page, footer = paginate(result, page=options.page, page_size=options.page_size)
    text = render(page, options, empty=empty)
    use_color = resolve_color(options.color)
    if not use_color:
        text = strip_ansi(text)
    if not (options.pager and page_through(text)):
        typer.echo(text, color=use_color)
    if footer:
        typer.echo(footer, err=True)


# --- Parsing (import) ---------------------------------------------------------


def parse_rows(
    text: str, fmt: OutputFormat, *, delimiter: str | None = None
) -> tuple[list[str], list[list[object]]]:
    """Read ``(headers, rows)`` from csv/tsv text or from a JSON list of objects."""
    if fmt is OutputFormat.JSON:
        return _parse_json_rows(text)
    if fmt not in (OutputFormat.CSV, OutputFormat.TSV):
        raise ExplorerError(f"cannot import from the {fmt.value} format")
    separator = delimiter or ("\t" if fmt is OutputFormat.TSV else ",")
    reader = csv.reader(io.StringIO(text), delimiter=separator)
    try:
        headers = next(reader)
    except StopIteration:
        raise ExplorerError("empty file") from None
    if not any(header.strip() for header in headers):
        raise ExplorerError("empty file")
    rows: list[list[object]] = []
    for number, record in enumerate(reader, start=2):
        if not record:
            continue
        if len(record) > len(headers):
            raise ExplorerError(f"row {number} has {len(record)} values, expected {len(headers)}")
        record = record + [""] * (len(headers) - len(record))
        rows.append([None if cell == "" else cell for cell in record])
    return headers, rows


def _parse_json_rows(text: str) -> tuple[list[str], list[list[object]]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ExplorerError(f"invalid JSON: {error}") from error
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ExplorerError("JSON input must be a list of objects")
    headers: list[str] = []
    for item in data:
        headers.extend(key for key in item if key not in headers)
    if not headers:
        raise ExplorerError("empty file")
    rows = [[_plain_json_value(item.get(key)) for key in headers] for item in data]
    return headers, rows


def _plain_json_value(value: object) -> object:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _classify(value: object) -> str:
    if isinstance(value, bool | int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    text = str(value).strip()
    if _INTEGER.match(text):
        return "INTEGER"
    if _REAL.match(text):
        return "REAL"
    return "TEXT"


def infer_types(rows: Sequence[Sequence[object]], count: int) -> list[str]:
    """Pick INTEGER, REAL or TEXT for each of the *count* columns from the values seen."""
    rank = {"INTEGER": 0, "REAL": 1, "TEXT": 2}
    types = ["INTEGER"] * count
    seen = [False] * count
    for row in rows:
        for index in range(count):
            value = row[index] if index < len(row) else None
            if value is None:
                continue
            seen[index] = True
            kind = _classify(value)
            if rank[kind] > rank[types[index]]:
                types[index] = kind
    return [kind if was_seen else "TEXT" for kind, was_seen in zip(types, seen, strict=True)]


def coerce_rows(rows: Sequence[Sequence[object]], types: Sequence[str]) -> list[list[object]]:
    """Convert textual values to int/float according to *types*."""
    converters = {"INTEGER": int, "REAL": float}
    coerced = []
    for row in rows:
        values: list[object] = []
        for value, kind in zip(row, types, strict=False):
            if isinstance(value, str) and kind in converters:
                value = converters[kind](value.strip())
            values.append(value)
        coerced.append(values)
    return coerced

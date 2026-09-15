"""Charts drawn with plotille from a :class:`ResultSet`.

The first column of the result is the X axis (numbers or ISO dates) and every
other column is a numeric series. Histograms use the first column only.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from itertools import islice
from typing import NamedTuple

import plotille
import plotilleresample

from sqlitexplorer.core import ExplorerError, ResultSet, RowStream
from sqlitexplorer.render import strip_ansi

__all__ = [
    "ChartKind",
    "Histogram",
    "Series",
    "render_chart",
    "render_histogram",
    "resample_series",
    "series_from_result",
    "stream_histogram",
    "stream_series",
]

PALETTE = ("red", "green", "yellow", "blue", "magenta", "cyan")
# plotille reserves this many characters for the Y axis label.
AXIS_LABEL_WIDTH = 8
# Characters the Y axis takes next to the canvas: ten for the tick, then
# " | ". plotille writes past the canvas too, which _fit takes care of.
AXIS_WIDTH = 13
# Narrowest canvas worth drawing on when the labels ask for too much room.
MIN_CANVAS = 10
# A tuple, not int | float: the union would be rebuilt on every call, and
# this runs once per value of the result. bool is an int.
_NUMERIC = (int, float)
# Rows read at a time when streaming, and how many reduced points may pile
# up before they are reduced again.
_CHUNK = 65536
_PILE = 8


class ChartKind(str, Enum):
    LINE = "line"
    SCATTER = "scatter"
    HIST = "hist"


@dataclass(frozen=True)
class Series:
    label: str
    x: list[float | datetime]
    y: list[float]


class Histogram(NamedTuple):
    """Counts of the first column of a query, in the bins plotille would draw."""

    counts: list[int]
    edges: list[float]
    column: str
    skipped: int


def _number(value: object) -> float | None:
    if isinstance(value, _NUMERIC):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _x_value(value: object) -> float | datetime | None:
    number = _number(value)
    if number is not None:
        return number
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def series_from_result(result: ResultSet, *, require_rows: bool = True) -> tuple[list[Series], int]:
    """Split *result* into one series per numeric column after the first.

    Rows with a NULL in the X column or in any series are skipped; the second
    item of the returned tuple counts them. With *require_rows* false an empty
    result yields empty series instead of raising, which is what the streaming
    reader needs for a chunk that holds nothing usable.
    """
    if len(result.columns) < 2:
        raise ExplorerError("need an X column and at least one numeric column")
    x_name, y_names = result.columns[0], result.columns[1:]
    xs: list[float | datetime] = []
    ys: list[list[float]] = [[] for _ in y_names]
    x_type: type | None = None
    skipped = 0
    keep_x = xs.append
    keep = [bucket.append for bucket in ys]
    for row in result.rows:
        if None in row:
            skipped += 1
            continue
        x = _x_value(row[0])
        if x is None:
            raise ExplorerError(f"column {x_name} is neither numeric nor an ISO date: {row[0]!r}")
        if x_type is None:
            x_type = type(x)
        elif not isinstance(x, x_type):
            raise ExplorerError(f"column {x_name} mixes numbers and dates")
        for name, value, append in zip(y_names, row[1:], keep, strict=True):
            number = _number(value)
            if number is None:
                raise ExplorerError(f"column {name} is not numeric: {value!r}")
            append(number)
        keep_x(x)
    if require_rows and not xs:
        raise ExplorerError("no rows to plot")
    return [
        Series(label=name, x=xs, y=bucket) for name, bucket in zip(y_names, ys, strict=True)
    ], skipped


def resample_series(
    series: Sequence[Series], *, kind: ChartKind, width: int, height: int
) -> list[Series]:
    """Reduce every series to the points the canvas can actually draw.

    min/max keeps the extremes of every bucket, so spikes survive, and it only
    indexes X, which the LTTB resamplers cannot do when X is a date. A scatter
    plot gets a uniform stride for its density plus those extremes.
    """
    budget = _canvas_width(width)
    reduce = (
        plotilleresample.resample_scatter_minmax
        if kind is ChartKind.SCATTER
        else plotilleresample.resample_plot_minmax
    )
    reduced = []
    for item in series:
        x, y = reduce(item.x, item.y, budget, height)
        reduced.append(Series(label=item.label, x=list(x), y=list(y)))
    return reduced


def stream_series(
    stream: RowStream, *, kind: ChartKind, width: int, height: int
) -> tuple[list[Series], int, int]:
    """Reduce *stream* to the canvas without ever holding every row.

    Rows are read in chunks, each chunk is reduced on its own and the reduced
    points are reduced again as they pile up, so the memory a chart needs stops
    growing with the size of the table. Returns the series, how many rows were
    skipped for their NULLs and how many were read.
    """
    reduced: list[Series] = []
    x_type: type | None = None
    skipped = rows_read = 0
    while True:
        rows = list(islice(stream.rows, _CHUNK))
        if not rows:
            break
        rows_read += len(rows)
        chunk, chunk_skipped = series_from_result(
            ResultSet(columns=stream.columns, rows=rows), require_rows=False
        )
        skipped += chunk_skipped
        if not chunk[0].x:
            continue
        if x_type is None:
            x_type = type(chunk[0].x[0])
        elif not isinstance(chunk[0].x[0], x_type):
            raise ExplorerError(f"column {stream.columns[0]} mixes numbers and dates")
        chunk = resample_series(chunk, kind=kind, width=width, height=height)
        reduced = (
            [
                Series(label=old.label, x=old.x + new.x, y=old.y + new.y)
                for old, new in zip(reduced, chunk, strict=True)
            ]
            if reduced
            else chunk
        )
        if len(reduced[0].x) > _PILE * len(chunk[0].x):
            reduced = resample_series(reduced, kind=kind, width=width, height=height)
    if not reduced or not reduced[0].x:
        raise ExplorerError("no rows to plot")
    return resample_series(reduced, kind=kind, width=width, height=height), skipped, rows_read


def stream_histogram(open_stream: Callable[[], RowStream], *, bins: int) -> Histogram:
    """Count the first column of a query into *bins* without holding its values.

    The query runs twice: the first pass validates the values and finds their
    range, the second counts them, so only the counts stay in memory. The
    bins are the ones plotille computes from the raw values: equal widths
    from the minimum to the maximum, the last one closed. ``skipped`` counts
    the rows whose first column was NULL.
    """
    first = open_stream()
    if not first.returns_rows:
        raise ExplorerError("the statement returned no rows")
    column = first.columns[0]
    low = high = None
    skipped = 0
    for row in first.rows:
        number = _histogram_value(row[0], column)
        if number is None:
            skipped += 1
        elif low is None or high is None:
            low = high = number
        else:
            low, high = min(low, number), max(high, number)
    if low is None or high is None:
        raise ExplorerError("no rows to plot")
    if low == high:
        low, high = low - 0.5, high + 0.5
    step = (high - low) / bins
    counts = [0] * bins
    for row in open_stream().rows:
        number = _histogram_value(row[0], column)
        if number is not None:
            # Clamped: a query that is not deterministic may not repeat its range.
            counts[max(0, min(bins - 1, int((number - low) // step)))] += 1
    edges = [low + index * step for index in range(bins + 1)]
    return Histogram(counts, edges, column, skipped)


def _histogram_value(value: object, column: str) -> float | None:
    if value is None:
        return None
    number = _number(value)
    if number is None:
        raise ExplorerError(f"column {column} is not numeric: {value!r}")
    return number


@contextmanager
def _color_environment(enabled: bool) -> Iterator[None]:
    """plotille checks FORCE_COLOR, NO_COLOR and isatty itself; make it follow *enabled*."""
    saved = {name: os.environ.get(name) for name in ("FORCE_COLOR", "NO_COLOR")}
    if enabled:
        os.environ["FORCE_COLOR"] = "1"
        os.environ.pop("NO_COLOR", None)
    else:
        os.environ["NO_COLOR"] = "1"
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _canvas_width(width: int) -> int:
    return max(MIN_CANVAS, width - AXIS_WIDTH)


def _fit(
    draw: Callable[[int], str], width: int, *, probe: Callable[[int], str] | None = None
) -> str:
    """Draw on the widest canvas whose longest line still fits in *width*.

    plotille writes the X label and the tick numbers past the end of the
    canvas, by an amount that depends on both, so the only way to know the
    room they take is to draw and measure. That room does not depend on the
    number of points, so *probe*, a drawing with the same labels and the same
    ranges but two points per series, finds the canvas cheaply and *draw*
    then runs once.
    """
    canvas = _canvas_width(width)
    if probe is not None:
        canvas = _narrow(probe, width, canvas)[1]
    return _narrow(draw, width, canvas)[0]


def _narrow(draw: Callable[[int], str], width: int, canvas: int) -> tuple[str, int]:
    """Shrink *canvas* until the drawing fits in *width*; return the drawing and its canvas."""
    while True:
        drawing = draw(canvas)
        excess = max(len(strip_ansi(line)) for line in drawing.splitlines()) - width
        if excess <= 0 or canvas <= MIN_CANVAS:
            return drawing, canvas
        canvas = max(MIN_CANVAS, canvas - excess)


def render_chart(
    series: Sequence[Series],
    *,
    kind: ChartKind,
    width: int,
    height: int,
    color: bool,
    x_label: str,
    y_label: str,
) -> str:
    """Draw *series* as a line chart or scatter plot, with a legend when there are several."""
    draw = functools.partial(
        _draw_series, kind=kind, height=height, color=color, x_label=x_label, y_label=y_label
    )
    return _fit(
        lambda canvas: draw(series, canvas=canvas),
        width,
        probe=lambda canvas: draw(_extremes(series), canvas=canvas),
    )


def _figure(
    *, canvas: int, height: int, color: bool, x_label: str, y_label: str
) -> plotille.Figure:
    figure = plotille.Figure()
    figure.width = canvas
    figure.height = max(3, height)
    figure.with_colors = color
    figure.color_mode = "names"
    figure.x_label = x_label
    figure.y_label = y_label[:AXIS_LABEL_WIDTH]
    return figure


def _draw_series(
    series: Sequence[Series],
    *,
    kind: ChartKind,
    canvas: int,
    height: int,
    color: bool,
    x_label: str,
    y_label: str,
) -> str:
    figure = _figure(canvas=canvas, height=height, color=color, x_label=x_label, y_label=y_label)
    for index, item in enumerate(series):
        line_color = PALETTE[index % len(PALETTE)] if color else None
        if kind is ChartKind.SCATTER:
            figure.scatter(item.x, item.y, lc=line_color, label=item.label)
        else:
            figure.plot(item.x, item.y, lc=line_color, label=item.label)
    with _color_environment(color):
        return figure.show(legend=len(series) > 1)


def _extremes(series: Sequence[Series]) -> list[Series]:
    """Two points per series, on the same ranges: enough to lay the axes out."""
    return [
        Series(label=item.label, x=[min(item.x), max(item.x)], y=[min(item.y), max(item.y)])
        for item in series
    ]


def render_histogram(
    histogram: Histogram, *, width: int, height: int, color: bool, x_label: str, y_label: str
) -> str:
    """Draw *histogram* the way plotille draws one from the raw values."""
    draw = functools.partial(
        _draw_histogram, histogram, height=height, color=color, x_label=x_label, y_label=y_label
    )
    return _fit(lambda canvas: draw(canvas=canvas), width)


def _draw_histogram(
    histogram: Histogram, *, canvas: int, height: int, color: bool, x_label: str, y_label: str
) -> str:
    figure = _figure(canvas=canvas, height=height, color=color, x_label=x_label, y_label=y_label)
    # plotille only bins raw values, but its Histogram plot keeps the counts
    # apart from them and draws from the counts alone: the two edges give it
    # the same range, hence the same bins, and the real counts then replace
    # the ones it took from those two values.
    figure.histogram(
        [histogram.edges[0], histogram.edges[-1]],
        bins=len(histogram.counts),
        lc=PALETTE[0] if color else None,
    )
    figure._plots[-1].frequencies = list(histogram.counts)
    with _color_environment(color):
        return figure.show()

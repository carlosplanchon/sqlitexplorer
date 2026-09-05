"""Charts drawn with plotille from a :class:`ResultSet`.

The first column of the result is the X axis (numbers or ISO dates) and every
other column is a numeric series. Histograms use the first column only.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import plotille

from sqlitexplorer.core import ExplorerError, ResultSet

__all__ = [
    "ChartKind",
    "Series",
    "histogram_values",
    "render_chart",
    "render_histogram",
    "series_from_result",
]

PALETTE = ("red", "green", "yellow", "blue", "magenta", "cyan")
# plotille reserves this many characters for the Y axis label.
AXIS_LABEL_WIDTH = 8
# Characters taken by the Y axis (ticks, label and separator) next to the canvas.
AXIS_WIDTH = 12


class ChartKind(str, Enum):
    LINE = "line"
    SCATTER = "scatter"
    HIST = "hist"


@dataclass(frozen=True)
class Series:
    label: str
    x: list[float | datetime]
    y: list[float]


def _number(value: object) -> float | None:
    if isinstance(value, bool | int | float):
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


def series_from_result(result: ResultSet) -> tuple[list[Series], int]:
    """Split *result* into one series per numeric column after the first.

    Rows with a NULL in the X column or in any series are skipped; the second
    item of the returned tuple counts them.
    """
    if len(result.columns) < 2:
        raise ExplorerError("need an X column and at least one numeric column")
    x_name, y_names = result.columns[0], result.columns[1:]
    xs: list[float | datetime] = []
    ys: list[list[float]] = [[] for _ in y_names]
    x_type: type | None = None
    skipped = 0
    for row in result.rows:
        if any(value is None for value in row):
            skipped += 1
            continue
        x = _x_value(row[0])
        if x is None:
            raise ExplorerError(f"column {x_name} is neither numeric nor an ISO date: {row[0]!r}")
        if x_type is None:
            x_type = type(x)
        elif not isinstance(x, x_type):
            raise ExplorerError(f"column {x_name} mixes numbers and dates")
        numbers = []
        for name, value in zip(y_names, row[1:], strict=True):
            number = _number(value)
            if number is None:
                raise ExplorerError(f"column {name} is not numeric: {value!r}")
            numbers.append(number)
        xs.append(x)
        for bucket, number in zip(ys, numbers, strict=True):
            bucket.append(number)
    if not xs:
        raise ExplorerError("no rows to plot")
    return [
        Series(label=name, x=xs, y=bucket) for name, bucket in zip(y_names, ys, strict=True)
    ], skipped


def histogram_values(result: ResultSet) -> tuple[list[float], int]:
    """Numeric values of the first column of *result*, and how many NULLs were skipped."""
    if not result.columns:
        raise ExplorerError("no rows to plot")
    name = result.columns[0]
    values: list[float] = []
    skipped = 0
    for row in result.rows:
        if row[0] is None:
            skipped += 1
            continue
        number = _number(row[0])
        if number is None:
            raise ExplorerError(f"column {name} is not numeric: {row[0]!r}")
        values.append(number)
    if not values:
        raise ExplorerError("no rows to plot")
    return values, skipped


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
    return max(10, width - AXIS_WIDTH)


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
    figure = plotille.Figure()
    figure.width = _canvas_width(width)
    figure.height = max(3, height)
    figure.with_colors = color
    figure.color_mode = "names"
    figure.x_label = x_label
    figure.y_label = y_label[:AXIS_LABEL_WIDTH]
    for index, item in enumerate(series):
        line_color = PALETTE[index % len(PALETTE)] if color else None
        if kind is ChartKind.SCATTER:
            figure.scatter(item.x, item.y, lc=line_color, label=item.label)
        else:
            figure.plot(item.x, item.y, lc=line_color, label=item.label)
    with _color_environment(color):
        return figure.show(legend=len(series) > 1)


def render_histogram(
    values: Sequence[float],
    *,
    bins: int,
    width: int,
    height: int,
    color: bool,
    x_label: str,
    y_label: str,
) -> str:
    """Draw the distribution of *values*."""
    with _color_environment(color):
        return plotille.histogram(
            list(values),
            bins=bins,
            width=_canvas_width(width),
            height=max(3, height),
            X_label=x_label,
            Y_label=y_label[:AXIS_LABEL_WIDTH],
            lc=PALETTE[0] if color else None,
        )

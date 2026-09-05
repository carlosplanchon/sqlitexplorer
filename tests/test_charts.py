"""Tests of the plotille-based charts."""

from __future__ import annotations

from datetime import datetime

import pytest

from sqlitexplorer.charts import (
    ChartKind,
    histogram_values,
    render_chart,
    render_histogram,
    series_from_result,
)
from sqlitexplorer.core import ExplorerError, ResultSet


def braille(text: str) -> bool:
    return any(0x2800 <= ord(char) <= 0x28FF for char in text)


def test_series_from_result_numeric_x_and_multiple_y() -> None:
    result = ResultSet(columns=("x", "a", "b"), rows=[(1, 2, 3), (2, 4, 6.5)])
    series, skipped = series_from_result(result)
    assert skipped == 0
    assert [item.label for item in series] == ["a", "b"]
    assert series[0].x == [1.0, 2.0]
    assert series[1].y == [3.0, 6.5]


def test_series_from_result_iso_dates() -> None:
    result = ResultSet(columns=("day", "v"), rows=[("2024-01-01", 1), ("2024-01-02T10:30:00", 2)])
    series, _ = series_from_result(result)
    assert series[0].x == [datetime(2024, 1, 1), datetime(2024, 1, 2, 10, 30)]


def test_series_skips_null_rows_and_counts_them() -> None:
    result = ResultSet(columns=("x", "y"), rows=[(1, None), (None, 2), (3, 4)])
    series, skipped = series_from_result(result)
    assert skipped == 2
    assert series[0].x == [3.0]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([(1, "abc")], "column y is not numeric"),
        ([("nope", 1)], "neither numeric nor an ISO date"),
        ([(1, 1), ("2024-01-01", 2)], "mixes numbers and dates"),
        ([(None, None)], "no rows to plot"),
    ],
)
def test_series_rejects_bad_data(rows: list[tuple], message: str) -> None:
    with pytest.raises(ExplorerError, match=message):
        series_from_result(ResultSet(columns=("x", "y"), rows=rows))


def test_series_requires_two_columns() -> None:
    with pytest.raises(ExplorerError, match="at least one numeric column"):
        series_from_result(ResultSet(columns=("x",), rows=[(1,)]))


def test_histogram_values_uses_first_column() -> None:
    result = ResultSet(columns=("v", "other"), rows=[(1, "a"), (None, "b"), (2.5, "c")])
    values, skipped = histogram_values(result)
    assert values == [1.0, 2.5]
    assert skipped == 1
    with pytest.raises(ExplorerError, match="not numeric"):
        histogram_values(ResultSet(columns=("v",), rows=[("x",)]))


def test_render_chart_prints_braille_without_colors() -> None:
    series, _ = series_from_result(ResultSet(columns=("x", "y"), rows=[(1, 1), (2, 3), (3, 2)]))
    text = render_chart(
        series, kind=ChartKind.LINE, width=60, height=8, color=False, x_label="x", y_label="y"
    )
    assert braille(text)
    assert "\x1b[" not in text
    assert "(x)" in text


def test_render_chart_with_colors_adds_ansi_and_legend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")  # an explicit request for color must win
    result = ResultSet(columns=("x", "a", "b"), rows=[(1, 1, 2), (2, 3, 1)])
    series, _ = series_from_result(result)
    text = render_chart(
        series, kind=ChartKind.SCATTER, width=60, height=8, color=True, x_label="x", y_label="y"
    )
    assert "\x1b[" in text
    assert "Legend" in text


def test_render_chart_single_point_does_not_crash() -> None:
    series, _ = series_from_result(ResultSet(columns=("x", "y"), rows=[(1, 1)]))
    text = render_chart(
        series, kind=ChartKind.LINE, width=40, height=5, color=False, x_label="x", y_label="y"
    )
    assert braille(text)


def test_render_histogram() -> None:
    text = render_histogram(
        [1.0, 2.0, 2.0, 3.0], bins=3, width=50, height=6, color=False, x_label="v", y_label="count"
    )
    assert braille(text)
    assert "(v)" in text
    assert "\x1b[" not in text

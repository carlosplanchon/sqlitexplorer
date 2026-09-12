"""Tests of the plotille-based charts."""

from __future__ import annotations

from datetime import datetime

import pytest

from sqlitexplorer.charts import (
    ChartKind,
    histogram_values,
    render_chart,
    render_histogram,
    resample_series,
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


def test_histogram_values_requires_a_column() -> None:
    with pytest.raises(ExplorerError, match="need a numeric column"):
        histogram_values(ResultSet())


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


def test_resample_series_reduces_and_keeps_the_extremes() -> None:
    rows: list[tuple] = [(i, 0.0) for i in range(5000)]
    rows[1234] = (1234, 999.0)
    rows[4321] = (4321, -999.0)
    series, _ = series_from_result(ResultSet(columns=("x", "y"), rows=rows))
    reduced = resample_series(series, kind=ChartKind.LINE, width=80, height=15)
    assert len(reduced) == 1
    assert len(reduced[0].x) == len(reduced[0].y) < 5000
    assert max(reduced[0].y) == 999.0
    assert min(reduced[0].y) == -999.0
    assert reduced[0].x == sorted(reduced[0].x)


def test_resample_series_handles_dates_on_the_x_axis() -> None:
    rows = [(f"2020-01-01T00:{i // 60:02d}:{i % 60:02d}", float(i)) for i in range(3000)]
    series, _ = series_from_result(ResultSet(columns=("t", "v"), rows=rows))
    reduced = resample_series(series, kind=ChartKind.LINE, width=80, height=15)
    assert len(reduced[0].x) < 3000
    assert all(isinstance(value, datetime) for value in reduced[0].x)


def test_resample_series_keeps_a_small_input_and_every_series() -> None:
    series, _ = series_from_result(
        ResultSet(columns=("x", "a", "b"), rows=[(1, 2, 3), (2, 4, 6.5)])
    )
    reduced = resample_series(series, kind=ChartKind.LINE, width=80, height=15)
    assert [item.label for item in reduced] == ["a", "b"]
    assert reduced[0].y == [2.0, 4.0]
    assert reduced[1].y == [3.0, 6.5]


def test_resample_series_scatter_keeps_more_points_than_a_line() -> None:
    series, _ = series_from_result(
        ResultSet(columns=("x", "y"), rows=[(i, float(i)) for i in range(20000)])
    )
    line = resample_series(series, kind=ChartKind.LINE, width=80, height=15)
    scatter = resample_series(series, kind=ChartKind.SCATTER, width=80, height=15)
    assert len(scatter[0].x) > len(line[0].x)

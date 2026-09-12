"""Unit tests of the presentation layer."""

from __future__ import annotations

import io
import json

import pytest

from sqlitexplorer.core import ExplorerError, ResultSet, RowStream
from sqlitexplorer.render import (
    OutputFormat,
    OutputOptions,
    coerce_rows,
    fit_widths,
    format_value,
    infer_types,
    ok_message,
    paginate,
    parse_number,
    parse_rows,
    render,
    strip_ansi,
    write_rows,
)

SAMPLE = ResultSet(
    columns=("id", "name", "blob"), rows=[(1, "Marie", b"\x01\x02"), (2, "a,b\nc|d", None)]
)


def test_format_value_null_blob_and_truncate() -> None:
    assert format_value(None) == "NULL"
    assert format_value(None, null="") == ""
    assert format_value(b"\x01\xab") == "X'01AB'"
    assert format_value("abcdef", truncate=4) == "abc…"
    assert format_value("abc", truncate=4) == "abc"
    assert format_value(42, truncate=1) == 42


def test_render_csv_quotes_commas_newlines_and_null_text() -> None:
    out = render(SAMPLE, OutputOptions(format=OutputFormat.CSV, null="-"))
    assert out.splitlines()[0] == "id,name,blob"
    assert '2,"a,b\nc|d",-' in out
    assert "1,Marie,X'0102'" in out


def test_render_tsv_uses_tabs() -> None:
    out = render(SAMPLE, OutputOptions(format=OutputFormat.TSV))
    assert out.splitlines()[0] == "id\tname\tblob"


def test_render_json_null_and_base64_blob() -> None:
    records = json.loads(render(SAMPLE, OutputOptions(format=OutputFormat.JSON)))
    assert records[0] == {"id": 1, "name": "Marie", "blob": "AQI="}
    assert records[1]["blob"] is None
    assert records[1]["name"] == "a,b\nc|d"


def test_render_markdown_escapes_pipes_and_newlines() -> None:
    lines = render(SAMPLE, OutputOptions(format=OutputFormat.MARKDOWN)).splitlines()
    assert lines[0] == "| id | name | blob |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[3] == "| 2 | a,b c\\|d | NULL |"


def test_render_table_keeps_labels_when_it_fits() -> None:
    result = ResultSet(columns=("identifier", "v"), rows=[(1, 2)])
    text = strip_ansi(render(result, OutputOptions(width=80)))
    assert text.splitlines()[0].split() == ["identifier", "v"]


def test_fit_widths_keeps_natural_widths_when_they_fit() -> None:
    assert fit_widths(["id", "name"], [1, 5], 20) == [2, 5]


def test_fit_widths_narrows_the_widest_columns_first() -> None:
    # Wide columns are cut down to a common cap; short ones keep their width.
    assert fit_widths(["id", "text", "note"], [2, 100, 30], 40) == [2, 19, 19]
    # Leftover characters go to the columns that still want them, widest first.
    assert fit_widths(["a", "b", "c"], [1, 50, 20], 30) == [1, 15, 14]
    # Labels are never narrower than their text.
    assert fit_widths(["identifier", "x"], [1, 100], 30) == [10, 20]


def test_fit_widths_returns_none_when_labels_do_not_fit() -> None:
    assert fit_widths(["identifier", "description"], [1, 1], 10) is None
    assert fit_widths([], [], 10) is None


def test_render_table_wraps_wide_values_instead_of_dropping_columns() -> None:
    result = ResultSet(columns=("id", "description", "flag"), rows=[(1, "x" * 200, "yes")])
    text = strip_ansi(render(result, OutputOptions(width=40)))
    lines = text.splitlines()
    assert lines[0].split() == ["id", "description", "flag"]
    assert "yes" in text
    assert all(len(line) <= 40 for line in lines)


def test_render_empty_results() -> None:
    empty = ResultSet(columns=("a", "b"))
    assert "(nothing)" in render(empty, OutputOptions(), empty="(nothing)")
    assert render(empty, OutputOptions(format=OutputFormat.CSV)) == "a,b"
    assert render(empty, OutputOptions(format=OutputFormat.JSON)) == "[]"
    markdown = render(empty, OutputOptions(format=OutputFormat.MARKDOWN)).splitlines()
    assert markdown == ["| a | b |", "| --- | --- |"]


def test_paginate_slices_rows_and_builds_footer() -> None:
    result = ResultSet(columns=("n",), rows=[(i,) for i in range(5)])
    page, footer = paginate(result, page=2, page_size=2)
    assert page.rows == [(2,), (3,)]
    assert footer == "page 2 of 3 (5 rows)"
    assert paginate(result) == (result, None)
    first, footer = paginate(result, page_size=10)
    assert first.rows == result.rows
    assert footer == "page 1 of 1 (5 rows)"
    blank, footer = paginate(ResultSet(columns=("n",)), page=1, page_size=3)
    assert blank.rows == []
    assert footer == "page 1 of 1 (0 rows)"


def test_paginate_out_of_range_raises() -> None:
    result = ResultSet(columns=("n",), rows=[(i,) for i in range(5)])
    with pytest.raises(ExplorerError, match=r"page 4 is out of range \(1-3\)"):
        paginate(result, page=4, page_size=2)


def test_paginate_uses_the_total_of_windowed_results() -> None:
    window = ResultSet(columns=("n",), rows=[(3,)], total=7)
    page, footer = paginate(window, page=4, page_size=1)
    assert page is window
    assert footer == "page 4 of 7 (7 rows)"
    with pytest.raises(ExplorerError, match=r"page 8 is out of range \(1-7\)"):
        paginate(window, page=8, page_size=1)


@pytest.mark.parametrize(
    "fmt", [OutputFormat.CSV, OutputFormat.TSV, OutputFormat.JSON, OutputFormat.MARKDOWN]
)
def test_write_rows_matches_render(fmt: OutputFormat) -> None:
    options = OutputOptions(format=fmt, null="-", truncate=5)
    for result in (SAMPLE, ResultSet(columns=("a", "b"))):
        buffer = io.StringIO()
        written = write_rows(RowStream(result.columns, iter(result.rows)), options, buffer)
        assert written == len(result.rows)
        assert buffer.getvalue() == render(result, options) + "\n"


def test_write_rows_table_format_loads_everything_and_strips_colors() -> None:
    buffer = io.StringIO()
    write_rows(RowStream(SAMPLE.columns, iter(SAMPLE.rows)), OutputOptions(width=80), buffer)
    assert "\x1b[" not in buffer.getvalue()
    assert buffer.getvalue().splitlines()[0].split() == ["id", "name", "blob"]


def test_ok_message() -> None:
    assert ok_message(ResultSet(rowcount=1)) == "OK (1 row affected)"
    assert ok_message(ResultSet(rowcount=3)) == "OK (3 rows affected)"
    assert ok_message(ResultSet()) == "OK"


def test_parse_number() -> None:
    assert parse_number("42") == 42
    assert parse_number("-1.5") == -1.5
    assert parse_number("1e3") == 1000.0
    assert parse_number("007") == 7
    assert parse_number("abc") == "abc"
    assert parse_number("1_000") == "1_000"


def test_parse_rows_csv_and_json() -> None:
    headers, rows = parse_rows("a,b\n1,x\n2,\n", OutputFormat.CSV)
    assert headers == ["a", "b"]
    assert rows == [["1", "x"], ["2", None]]
    headers, rows = parse_rows("a\tb\n1\tx\n", OutputFormat.TSV)
    assert rows == [["1", "x"]]
    headers, rows = parse_rows(
        '[{"a": 1, "b": true}, {"a": null, "c": {"k": 1}}]', OutputFormat.JSON
    )
    assert headers == ["a", "b", "c"]
    assert rows == [[1, 1, None], [None, None, '{"k": 1}']]


def test_parse_rows_rejects_bad_input() -> None:
    with pytest.raises(ExplorerError, match="empty file"):
        parse_rows("", OutputFormat.CSV)
    with pytest.raises(ExplorerError, match="row 2 has 3 values, expected 2"):
        parse_rows("a,b\n1,2,3\n", OutputFormat.CSV)
    with pytest.raises(ExplorerError, match="invalid JSON"):
        parse_rows("{", OutputFormat.JSON)
    with pytest.raises(ExplorerError, match="list of objects"):
        parse_rows("[1]", OutputFormat.JSON)
    with pytest.raises(ExplorerError, match="cannot import"):
        parse_rows("x", OutputFormat.MARKDOWN)


def test_infer_types_and_coerce_rows() -> None:
    rows = [["1", "1.5", "x", None], ["2", "2", "3", None]]
    types = infer_types(rows, 4)
    assert types == ["INTEGER", "REAL", "TEXT", "TEXT"]
    assert coerce_rows(rows, types) == [[1, 1.5, "x", None], [2, 2.0, "3", None]]


def test_infer_types_keeps_codes_and_huge_integers_as_text() -> None:
    rows = [["007", "0", "0.5", "99999999999999999999"]]
    types = infer_types(rows, 4)
    assert types == ["TEXT", "INTEGER", "REAL", "TEXT"]
    assert coerce_rows(rows, types) == [["007", 0, 0.5, "99999999999999999999"]]


def test_parse_rows_json_keeps_huge_integers_as_text() -> None:
    _, rows = parse_rows('[{"n": 99999999999999999999}, {"n": 1}]', OutputFormat.JSON)
    assert rows == [["99999999999999999999"], [1]]


def test_parse_rows_reports_a_bad_delimiter_and_a_malformed_file() -> None:
    with pytest.raises(ExplorerError, match="single character"):
        parse_rows("a,b\n1,2\n", OutputFormat.CSV, delimiter=";;")
    with pytest.raises(ExplorerError, match="cannot read the file"):
        parse_rows("a\n" + "x" * 200_000 + "\n", OutputFormat.CSV)


def test_page_footer_uses_the_singular_for_one_row() -> None:
    one = ResultSet(columns=("id",), rows=[(1,)])
    two = ResultSet(columns=("id",), rows=[(1,), (2,)])
    assert paginate(one, page_size=5)[1] == "page 1 of 1 (1 row)"
    assert paginate(two, page_size=5)[1] == "page 1 of 1 (2 rows)"
    # A page counted in SQL takes the same path.
    assert paginate(ResultSet(columns=("id",), total=1), page_size=5)[1] == "page 1 of 1 (1 row)"

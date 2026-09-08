"""Tests for the csv_plot series/aggregate helpers (2-PS-b1).

Pure — exercises the CSV parse / down-sample / aggregate / path
helpers directly against tmp_path files (no DB / no server). The
DB+ACL orchestration on :class:`SeriesService` is thin glue over
these + the b0-3 read path, covered by the b-6 regression.
"""

from __future__ import annotations

from pathlib import Path

from matofx.studies.series_service import (
    _aggregate,
    _csv_filename_for,
    _downsample_pairs,
    _extract_xy,
    _read_numeric_rows,
    _resolve_under_outputs,
)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_read_numeric_rows_skips_header(tmp_path: Path) -> None:
    p = _write(tmp_path, "d.csv", "time,temp\n0,20\n1,25\n2,30\n")
    assert _read_numeric_rows(p) == [[0.0, 20.0], [1.0, 25.0], [2.0, 30.0]]


def test_read_numeric_rows_no_header(tmp_path: Path) -> None:
    p = _write(tmp_path, "d.csv", "0,1\n2,3\n")
    assert _read_numeric_rows(p) == [[0.0, 1.0], [2.0, 3.0]]


def test_read_numeric_rows_non_numeric_cell_is_none(tmp_path: Path) -> None:
    p = _write(tmp_path, "d.csv", "t,v\n0,ok\n1,5\n")
    assert _read_numeric_rows(p) == [[0.0, None], [1.0, 5.0]]


def test_read_numeric_rows_empty(tmp_path: Path) -> None:
    assert _read_numeric_rows(_write(tmp_path, "d.csv", "")) == []


def test_extract_xy_skips_incomplete_rows(tmp_path: Path) -> None:
    p = _write(tmp_path, "d.csv", "x,y\n0,10\n1,\n2,30\n")
    xs, ys = _extract_xy(p, 0, 1, max_points=500)
    assert xs == [0.0, 2.0]
    assert ys == [10.0, 30.0]


def test_downsample_keeps_endpoints_and_caps() -> None:
    xs = [float(i) for i in range(1000)]
    ys = [float(i * 2) for i in range(1000)]
    dx, dy = _downsample_pairs(xs, ys, 100)
    assert len(dx) <= 100
    assert dx[0] == 0.0
    assert dx[-1] == 999.0
    assert len(dx) == len(dy)


def test_downsample_noop_when_small() -> None:
    xs, ys = [0.0, 1.0], [2.0, 3.0]
    assert _downsample_pairs(xs, ys, 500) == (xs, ys)


def test_aggregate_modes(tmp_path: Path) -> None:
    p = _write(tmp_path, "d.csv", "t,v\n0,10\n1,30\n2,20\n")
    assert _aggregate(p, 1, "last") == 20.0
    assert _aggregate(p, 1, "max") == 30.0
    assert _aggregate(p, 1, "min") == 10.0
    assert _aggregate(p, 1, "mean") == 20.0


def test_aggregate_empty_column_is_none(tmp_path: Path) -> None:
    p = _write(tmp_path, "d.csv", "t,v\n0,x\n1,y\n")
    assert _aggregate(p, 1, "mean") is None


def test_csv_filename_for_matches_kind_and_name() -> None:
    refs = [
        {"name": "report", "filename": "outputs/r.html", "kind": "html_plot"},
        {"name": "curve", "filename": "outputs/c.csv", "kind": "csv_plot"},
    ]
    assert _csv_filename_for(refs, "curve") == "outputs/c.csv"
    assert _csv_filename_for(refs, "report") is None  # not a csv_plot
    assert _csv_filename_for(refs, "missing") is None
    assert _csv_filename_for(None, "curve") is None


def test_resolve_under_outputs_strips_prefix(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "c.csv").write_text("x,y\n0,1\n", encoding="utf-8")
    assert _resolve_under_outputs(outputs, "outputs/c.csv") == (outputs / "c.csv").resolve()
    assert _resolve_under_outputs(outputs, "c.csv") == (outputs / "c.csv").resolve()


def test_resolve_under_outputs_blocks_traversal(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (tmp_path / "secret.csv").write_text("x", encoding="utf-8")
    assert _resolve_under_outputs(outputs, "../secret.csv") is None
    assert _resolve_under_outputs(outputs, "missing.csv") is None

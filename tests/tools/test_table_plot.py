"""Unit tests for the table-input plot planner (axis-assignment rules)."""

from __future__ import annotations

from matofx.tools.forms.table_plot import PlotColumn, build_figure, plan_table_plots


def _col(name: str, ctype: str = "float", unit: str | None = None) -> PlotColumn:
    return PlotColumn(name=name, label=name.title(), type=ctype, unit=unit)


def _shape(specs: list) -> list[tuple[list[str], list[str]]]:
    """(left labels, right labels) per plot."""
    return [([s.label for s in p.left], [s.label for s in p.right]) for p in specs]


def test_two_unlike_dependents_split_left_and_right() -> None:
    cols = [_col("x"), _col("stress", unit="MPa"), _col("disp", unit="mm")]
    rows = [
        {"x": 0, "stress": 1e8, "disp": 1e-3},
        {"x": 1, "stress": 2e8, "disp": 2e-3},
    ]
    specs = plan_table_plots(cols, rows)
    assert len(specs) == 1
    assert _shape(specs) == [(["Stress"], ["Disp"])]  # col2 left, col3 right
    assert specs[0].x == [0.0, 1.0]


def test_same_unit_and_magnitude_share_one_axis() -> None:
    cols = [_col("x"), _col("sx", unit="MPa"), _col("sy", unit="MPa")]
    rows = [{"x": 0, "sx": 100.0, "sy": 110.0}, {"x": 1, "sx": 120.0, "sy": 130.0}]
    specs = plan_table_plots(cols, rows)
    assert len(specs) == 1
    # Comparable scale + same unit -> both on the left axis, no secondary.
    assert _shape(specs) == [(["Sx", "Sy"], [])]


def test_same_unit_but_far_apart_magnitude_splits() -> None:
    cols = [_col("x"), _col("small", unit="Pa"), _col("big", unit="Pa")]
    rows = [{"x": 0, "small": 1.0, "big": 1e6}, {"x": 1, "small": 2.0, "big": 2e6}]
    specs = plan_table_plots(cols, rows)
    # Same unit, but >10x apart -> two axes.
    assert _shape(specs) == [(["Small"], ["Big"])]


def test_shared_cluster_plus_odd_one_stays_one_plot() -> None:
    # Two MPa columns of like scale share the left axis; the mm column is right.
    cols = [
        _col("x"),
        _col("sx", unit="MPa"),
        _col("disp", unit="mm"),
        _col("sy", unit="MPa"),
    ]
    rows = [{"x": 0, "sx": 100.0, "disp": 1e-3, "sy": 120.0}]
    specs = plan_table_plots(cols, rows)
    assert len(specs) == 1
    assert _shape(specs) == [(["Sx", "Sy"], ["Disp"])]


def test_three_unlike_dependents_make_two_plots() -> None:
    cols = [
        _col("x"),
        _col("a", unit="A"),
        _col("b", unit="B"),
        _col("c", unit="C"),
    ]
    rows = [{"x": 0, "a": 1.0, "b": 1e6, "c": 1e-3}]
    specs = plan_table_plots(cols, rows)
    assert len(specs) == 2
    assert _shape(specs) == [(["A"], ["B"]), (["C"], [])]  # last is single-axis


def test_non_numeric_dependent_is_skipped() -> None:
    cols = [_col("x"), _col("name", ctype="string"), _col("val", unit="kg")]
    rows = [{"x": 0, "name": "steel", "val": 7.8}]
    specs = plan_table_plots(cols, rows)
    assert _shape(specs) == [(["Val"], [])]


def test_string_first_column_is_categorical_x() -> None:
    cols = [_col("material", ctype="string"), _col("val", unit="kg")]
    rows = [{"material": "steel", "val": 7.8}, {"material": "alu", "val": 2.7}]
    specs = plan_table_plots(cols, rows)
    assert specs[0].x == ["steel", "alu"]
    assert _shape(specs) == [(["Val"], [])]


def test_nothing_to_plot_returns_empty() -> None:
    assert plan_table_plots([_col("x")], [{"x": 1}]) == []  # only one column
    assert plan_table_plots([_col("x"), _col("y")], []) == []  # no rows
    # A single non-numeric dependent -> no numeric series -> empty.
    assert plan_table_plots([_col("x"), _col("name", ctype="string")], [{"x": 1}]) == []


def test_build_figure_has_secondary_axis_when_right_present() -> None:
    cols = [_col("x"), _col("stress", unit="MPa"), _col("disp", unit="mm")]
    rows = [{"x": 0, "stress": 1e8, "disp": 1e-3}]
    fig = build_figure(plan_table_plots(cols, rows)[0])
    assert fig.layout.yaxis2 is not None
    assert fig.layout.yaxis2.overlaying == "y"
    assert fig.layout.yaxis2.side == "right"
    assert fig.layout.showlegend is True
    # Both y-axes are anchored at zero.
    assert fig.layout.yaxis.rangemode == "tozero"
    assert fig.layout.yaxis2.rangemode == "tozero"
    # Axis titles: x + a single-series axis shows the full label + unit.
    assert fig.layout.xaxis.title.text == "X"
    assert fig.layout.yaxis.title.text == "Stress (MPa)"
    assert fig.layout.yaxis2.title.text == "Disp (mm)"
    # Right-axis trace tagged, and bound to y2.
    y2 = [t for t in fig.data if t.yaxis == "y2"]
    assert len(y2) == 1
    assert "(right)" in y2[0].name

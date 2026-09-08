"""Unit tests for the warehouse-page plot helpers.

Pure-logic tests against ``matofx.ui.pages._results_plot`` — no
NiceGUI, no Plotly. The page shell wires the live widgets to
these helpers; they're the testable contract.
"""

from __future__ import annotations

from matofx.ui.pages._results_plot import (
    PlotDefaults,
    axis_label,
    build_scatter_data,
    build_timeseries_data,
    default_plot_state,
)

# ---- default_plot_state ----------------------------------------------


def test_default_state_no_kpis_means_no_plot() -> None:
    assert default_plot_state([]) == PlotDefaults(mode="scatter", x_kpi=None, y_kpi=None)


def test_default_state_one_kpi_defaults_to_timeseries() -> None:
    state = default_plot_state([{"kpi_name": "max_temperature"}])
    assert state.mode == "timeseries"
    assert state.y_kpi == "max_temperature"
    assert state.x_kpi is None


def test_default_state_two_kpis_defaults_to_scatter_first_two() -> None:
    state = default_plot_state(
        [
            {"kpi_name": "max_temperature"},
            {"kpi_name": "max_pressure"},
            {"kpi_name": "max_stress"},
        ]
    )
    assert state.mode == "scatter"
    assert state.x_kpi == "max_temperature"
    assert state.y_kpi == "max_pressure"


def test_default_state_garbage_entry_skipped() -> None:
    state = default_plot_state([{"label": "no name field"}])
    assert state.mode == "scatter"
    assert state.x_kpi is None


# ---- build_scatter_data ---------------------------------------------


def test_scatter_pivots_rows_into_aligned_lists() -> None:
    rows = [
        {"job_id": 1, "kpi_values": {"x": 1.0, "y": 10.0}},
        {"job_id": 2, "kpi_values": {"x": 2.0, "y": 20.0}},
    ]
    xs, ys, ids = build_scatter_data(rows, x_kpi="x", y_kpi="y")
    assert xs == [1.0, 2.0]
    assert ys == [10.0, 20.0]
    assert ids == [1, 2]


def test_scatter_drops_rows_with_missing_x_or_y() -> None:
    rows = [
        {"job_id": 1, "kpi_values": {"x": 1.0, "y": 10.0}},
        {"job_id": 2, "kpi_values": {"x": None, "y": 20.0}},
        {"job_id": 3, "kpi_values": {"x": 3.0, "y": None}},
        {"job_id": 4, "kpi_values": {"x": 4.0, "y": 40.0}},
    ]
    xs, ys, ids = build_scatter_data(rows, x_kpi="x", y_kpi="y")
    assert xs == [1.0, 4.0]
    assert ys == [10.0, 40.0]
    assert ids == [1, 4]


def test_scatter_drops_non_numeric_rows() -> None:
    rows = [
        {"job_id": 1, "kpi_values": {"x": "hot", "y": 10.0}},
        {"job_id": 2, "kpi_values": {"x": 2.0, "y": 20.0}},
    ]
    xs, ys, ids = build_scatter_data(rows, x_kpi="x", y_kpi="y")
    assert xs == [2.0]
    assert ids == [2]


def test_scatter_handles_empty_rows() -> None:
    assert build_scatter_data([], x_kpi="x", y_kpi="y") == ([], [], [])


# ---- build_timeseries_data ------------------------------------------


def test_timeseries_extracts_aligned_lists() -> None:
    points = [
        {"extracted_at": "2026-05-29T10:00:00+00:00", "value_numeric": 300.0, "job_id": 1},
        {"extracted_at": "2026-05-29T11:00:00+00:00", "value_numeric": 305.0, "job_id": 2},
    ]
    xs, ys, ids = build_timeseries_data(points)
    assert ys == [300.0, 305.0]
    assert ids == [1, 2]
    assert xs[0] == "2026-05-29T10:00:00+00:00"


def test_timeseries_drops_null_value_points() -> None:
    points = [
        {"extracted_at": "2026-05-29T10:00:00+00:00", "value_numeric": 300.0, "job_id": 1},
        {"extracted_at": "2026-05-29T10:30:00+00:00", "value_numeric": None, "job_id": 2},
        {"extracted_at": "2026-05-29T11:00:00+00:00", "value_numeric": 305.0, "job_id": 3},
    ]
    xs, ys, ids = build_timeseries_data(points)
    assert ys == [300.0, 305.0]
    assert ids == [1, 3]


# ---- axis_label -----------------------------------------------------


def test_axis_label_uses_kpi_label_and_unit() -> None:
    entries = [
        {"kpi_name": "max_temperature", "kpi_label": "Peak temp", "kpi_unit": "K"},
    ]
    assert axis_label(entries, "max_temperature") == "Peak temp (K)"


def test_axis_label_falls_back_to_kpi_name_without_label() -> None:
    entries = [{"kpi_name": "max_temperature", "kpi_unit": "K"}]
    assert axis_label(entries, "max_temperature") == "max_temperature (K)"


def test_axis_label_handles_missing_unit() -> None:
    entries = [{"kpi_name": "ratio", "kpi_label": "Aspect ratio"}]
    assert axis_label(entries, "ratio") == "Aspect ratio"


def test_axis_label_returns_raw_name_when_kpi_absent_from_catalog() -> None:
    """An unknown KPI name still gets a usable axis label."""
    entries = [{"kpi_name": "foo", "kpi_label": "Foo"}]
    assert axis_label(entries, "bar") == "bar"

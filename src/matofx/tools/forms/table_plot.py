"""Plan and build charts for a table input's rows (the "View as plot" modal).

The job form lets a user enter a ``table`` input as a grid of rows. This module
turns those rows into one or more line charts, following these rules:

* The **first column** is the shared x-axis.
* Every **numeric column after it** is a dependent series; non-numeric
  dependent columns are skipped.
* Dependent series that share a **unit and a comparable magnitude** (within
  :data:`_SCALE_RATIO`) sit together on one y-axis.
* Column 2 anchors the **left** axis, column 3 the **right** axis. A single
  plot holds at most two axis groups; with more independent scales than that,
  the extras spill onto **additional stacked plots**, each again capped at two
  axes — so no plot ever mixes more than two scales.

:func:`plan_table_plots` is pure (plain data in, dataclasses out) so the axis
logic is unit-tested without NiceGUI or Plotly. :func:`build_figure` turns one
plan entry into a Plotly figure for the modal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import plotly.graph_objects as go

# Two series share an axis only if their magnitudes are within this factor.
_SCALE_RATIO = 10.0
_NUMERIC_TYPES = frozenset({"integer", "float"})
# A plottable table needs the x column plus at least one dependent column.
_MIN_COLUMNS = 2

# Qualitative palette (distinct hues so overlaid series stay legible in both
# themes); cycles if a plot has more series than colours.
_PALETTE = (
    "#40A1A4",  # MATOFX teal
    "#E8833A",  # orange
    "#6C6CE0",  # indigo
    "#C0504D",  # red
    "#4E9A51",  # green
    "#B060B0",  # magenta
    "#3A7CA5",  # blue
    "#C9A227",  # gold
)


@dataclass(frozen=True)
class PlotColumn:
    """The subset of a descriptor ``TableColumn`` the planner needs."""

    name: str
    label: str
    type: str
    unit: str | None = None


@dataclass(frozen=True)
class Series:
    """One dependent series: its label, unit and per-row y values."""

    label: str
    unit: str | None
    y: list[float | None]


@dataclass(frozen=True)
class PlotSpec:
    """One chart: a shared x plus a left axis group and an optional right one."""

    x_label: str
    x_unit: str | None
    x: list[Any]
    left: list[Series]
    right: list[Series] = field(default_factory=list)


def _to_float(value: Any) -> float | None:
    """Coerce a cell to a finite float, or ``None`` (booleans never count)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _magnitude(ys: list[float | None]) -> float:
    """Representative scale of a series = the largest absolute finite value."""
    vals = [abs(y) for y in ys if y is not None]
    return max(vals) if vals else 0.0


@dataclass
class _Dep:
    index: int  # position in ``columns`` (>= 1)
    col: PlotColumn
    ys: list[float | None]
    magnitude: float


def _cluster_by_magnitude(deps: list[_Dep]) -> list[list[_Dep]]:
    """Split same-unit deps into groups of comparable magnitude.

    Sorted by magnitude, a new group starts at each jump larger than
    :data:`_SCALE_RATIO` (a zero-magnitude series — all blank/zero — only
    groups with other zero-magnitude series). Members keep column order.
    """
    ordered = sorted(deps, key=lambda d: d.magnitude)
    groups: list[list[_Dep]] = []
    for dep in ordered:
        if not groups:
            groups.append([dep])
            continue
        prev = groups[-1][-1].magnitude
        cur = dep.magnitude
        same_zeroness = (prev == 0.0) == (cur == 0.0)
        within = prev > 0.0 and cur / prev <= _SCALE_RATIO
        if same_zeroness and (prev == 0.0 or within):
            groups[-1].append(dep)
        else:
            groups.append([dep])
    # Restore column order inside each group for stable legend/plot order.
    return [sorted(g, key=lambda d: d.index) for g in groups]


def plan_table_plots(
    columns: list[PlotColumn], rows: list[dict[str, Any]]
) -> list[PlotSpec]:
    """Turn a table's columns + rows into an ordered list of chart specs.

    Returns an empty list when there is nothing plottable (fewer than two
    columns, no rows, or no numeric dependent column).
    """
    if len(columns) < _MIN_COLUMNS or not rows:
        return []

    x_col = columns[0]
    x_numeric = x_col.type in _NUMERIC_TYPES
    x_vals: list[Any] = []
    for r in rows:
        cell = r.get(x_col.name)
        x_vals.append(_to_float(cell) if x_numeric else ("" if cell is None else str(cell)))

    # Numeric dependents only, keeping descriptor column order.
    deps: list[_Dep] = []
    for idx, col in enumerate(columns[1:], start=1):
        if col.type not in _NUMERIC_TYPES:
            continue
        ys = [_to_float(r.get(col.name)) for r in rows]
        deps.append(_Dep(index=idx, col=col, ys=ys, magnitude=_magnitude(ys)))
    if not deps:
        return []

    # Cluster: same unit AND comparable magnitude share an axis. Group by unit
    # (in first-appearance / column order), then split each unit group by scale.
    clusters: list[list[_Dep]] = []
    seen_units: list[str | None] = []
    by_unit: dict[str | None, list[_Dep]] = {}
    for dep in deps:
        unit = dep.col.unit or None
        if unit not in by_unit:
            by_unit[unit] = []
            seen_units.append(unit)
        by_unit[unit].append(dep)
    for unit in seen_units:
        clusters.extend(_cluster_by_magnitude(by_unit[unit]))

    # Order clusters by their earliest column so column 2 anchors the first
    # left axis, column 3 the first right axis, etc.
    clusters.sort(key=lambda g: min(d.index for d in g))

    def _series(group: list[_Dep]) -> list[Series]:
        return [Series(label=d.col.label, unit=d.col.unit, y=d.ys) for d in group]

    # Pack two clusters (left + right) per plot; the last may be single-axis.
    specs: list[PlotSpec] = []
    for i in range(0, len(clusters), 2):
        left = _series(clusters[i])
        right = _series(clusters[i + 1]) if i + 1 < len(clusters) else []
        specs.append(
            PlotSpec(
                x_label=x_col.label,
                x_unit=x_col.unit,
                x=x_vals,
                left=left,
                right=right,
            )
        )
    return specs


def _axis_title(label: str, unit: str | None) -> str:
    return f"{label} ({unit})" if unit else label


def _group_unit(series: list[Series]) -> str | None:
    """The shared unit of an axis group, or None when it is mixed/absent."""
    units = {s.unit for s in series if s.unit}
    return next(iter(units)) if len(units) == 1 else None


def _axis_group_title(series: list[Series]) -> str:
    """Title for a y-axis: the single series' label+unit, else the shared unit."""
    if len(series) == 1:
        return _axis_title(series[0].label, series[0].unit)
    return _group_unit(series) or ""


def _legend_name(s: Series, *, right: bool) -> str:
    base = f"{s.label} ({s.unit})" if s.unit else s.label
    return f"{base} (right)" if right else base


def build_figure(spec: PlotSpec) -> go.Figure:
    """Build the Plotly line chart for one :class:`PlotSpec`.

    Left-axis series bind to ``y``; right-axis series to a secondary ``y2``
    overlaid on the same x. The legend is always shown, labelled by column
    label + unit, with right-axis series tagged ``(right)``.
    """
    fig = go.Figure()
    ci = 0
    for s in spec.left:
        fig.add_trace(
            go.Scatter(
                x=spec.x, y=s.y, mode="lines+markers", name=_legend_name(s, right=False),
                line={"color": _PALETTE[ci % len(_PALETTE)]}, yaxis="y",
            )
        )
        ci += 1
    for s in spec.right:
        fig.add_trace(
            go.Scatter(
                x=spec.x, y=s.y, mode="lines+markers", name=_legend_name(s, right=True),
                line={"color": _PALETTE[ci % len(_PALETTE)]}, yaxis="y2",
            )
        )
        ci += 1

    layout: dict[str, Any] = {
        "xaxis": {"title": {"text": _axis_title(spec.x_label, spec.x_unit)}},
        # ``rangemode="tozero"`` anchors the y-axis at zero (the axis always
        # starts from 0 for non-negative data, and includes 0 otherwise).
        "yaxis": {"title": {"text": _axis_group_title(spec.left)}, "rangemode": "tozero"},
        "showlegend": True,
        "legend": {"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
        "margin": {"l": 60, "r": 60, "t": 30, "b": 50},
        "height": 340,
    }
    if spec.right:
        layout["yaxis2"] = {
            "title": {"text": _axis_group_title(spec.right)},
            "overlaying": "y",
            "side": "right",
            "rangemode": "tozero",
        }
    fig.update_layout(**layout)
    return fig

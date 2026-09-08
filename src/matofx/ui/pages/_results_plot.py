"""Pure logic for the warehouse-page Plotly card.

The page shell wires the live widgets (X / Y dropdowns + the
time-series toggle) to these helpers; the helpers know nothing
about NiceGUI or Plotly's runtime — they just shape the data the
chart consumes. Unit-testable in isolation, like the selection
module next to it.

Two plot modes (design §8.1):

- **Scatter.** X = any KPI; Y = any KPI; optional series grouping
  (project / tool_version_id). Uses the warehouse page's already-
  loaded rows.
- **Time-series.** X = ``extracted_at``; Y = one KPI. Fed from the
  ``/results/timeseries`` API endpoint per-KPI.

Default axis selection (design §8.1): first two KPIs as X/Y;
when only one KPI exists, default to time-series with that KPI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlotDefaults:
    """The initial plot mode + axis picks for a tool's KPI set."""

    mode: str  # "scatter" or "timeseries"
    x_kpi: str | None
    y_kpi: str | None


def default_plot_state(kpi_entries: list[dict[str, Any]]) -> PlotDefaults:
    """Decide the plot card's initial mode + axes.

    Rules:

    - No KPIs declared → no plot at all (``mode='scatter'``, both
      axes ``None``); the page suppresses the card entirely.
    - One KPI → default to time-series on that KPI; the X picker
      isn't surfaced (only one trend to plot).
    - Two or more KPIs → default to scatter with the first two KPIs
      (kpi[0] on X, kpi[1] on Y).
    """
    if not kpi_entries:
        return PlotDefaults(mode="scatter", x_kpi=None, y_kpi=None)
    names = [e["kpi_name"] for e in kpi_entries if isinstance(e.get("kpi_name"), str)]
    if not names:
        return PlotDefaults(mode="scatter", x_kpi=None, y_kpi=None)
    if len(names) == 1:
        return PlotDefaults(mode="timeseries", x_kpi=None, y_kpi=names[0])
    return PlotDefaults(mode="scatter", x_kpi=names[0], y_kpi=names[1])


def build_scatter_data(
    rows: list[dict[str, Any]], *, x_kpi: str, y_kpi: str
) -> tuple[list[float], list[float], list[int]]:
    """Pivot warehouse rows into the (xs, ys, job_ids) shape Plotly needs.

    Rows whose X or Y KPI is missing / sentinel-``None`` are dropped
    — Plotly plots gappy series correctly but the discovery layer is
    cleaner without them. The triple's three lists stay aligned by
    index so hover-text / click-through can reference the same row
    by position.
    """
    xs: list[float] = []
    ys: list[float] = []
    job_ids: list[int] = []
    for r in rows:
        kpi_values = r.get("kpi_values", {}) or {}
        x = kpi_values.get(x_kpi)
        y = kpi_values.get(y_kpi)
        if x is None or y is None:
            continue
        try:
            xs.append(float(x))
            ys.append(float(y))
            job_ids.append(int(r.get("job_id", 0)))
        except (TypeError, ValueError):
            continue
    return xs, ys, job_ids


def build_timeseries_data(
    points: list[dict[str, Any]],
) -> tuple[list[Any], list[float], list[int]]:
    """Pivot timeseries-API points into (xs, ys, job_ids).

    ``xs`` are the raw ``extracted_at`` strings the API returns —
    Plotly accepts ISO-8601 dates without parsing. ``ys`` drops
    ``None`` value points so the chart doesn't render dummy zeros.
    """
    xs: list[Any] = []
    ys: list[float] = []
    job_ids: list[int] = []
    for p in points:
        v = p.get("value_numeric")
        if v is None:
            continue
        try:
            ys.append(float(v))
        except (TypeError, ValueError):
            continue
        xs.append(p.get("extracted_at"))
        job_ids.append(int(p.get("job_id", 0)))
    return xs, ys, job_ids


def axis_label(entries: list[dict[str, Any]], kpi_name: str) -> str:
    """Build the axis label ``kpi_label (kpi_unit)`` from the catalog."""
    for e in entries:
        if e.get("kpi_name") == kpi_name:
            label = e.get("kpi_label") or kpi_name
            unit = e.get("kpi_unit")
            return f"{label} ({unit})" if unit else label
    return kpi_name

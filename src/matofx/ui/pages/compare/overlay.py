"""CSV-overlay charts for the Compare workspace.

Locked design (2026-05-09): for each CSV output name shared by ≥2
selected jobs, render **one merged chart** with one trace per (job,
column). An "Show individual charts" expander offers the legacy
per-job view for users who want to inspect a single run in isolation.

The merged view saves vertical space — three jobs of the same tool
typically each emit two CSVs, so the legacy per-job-per-csv layout
needs six charts where this one needs two.

Theme integration: every figure is routed through
:func:`matofx.ui.theme.apply_theme_to_plotly` so backgrounds match the
active dark / light palette. Never pin ``template="plotly_white"``
here — that's the bug from the legacy code.
"""

from __future__ import annotations

import io
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go
from nicegui import ui

from matofx.ui.theme import export_plotly_html, themed_plotly

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobCsvOutput:
    """One CSV output produced by one selected job.

    The Compare page builds these by walking each selected job's
    output document, picking out ``type='file'`` outputs whose
    filename ends in ``.csv``, and downloading the bytes via
    :meth:`ApiClient.download_job_output`. Keeping the value object
    cheap (just ids + bytes) means the overlay module doesn't need
    to know how the bytes were fetched — easier to test, easier to
    reuse from the 3D-overlay code path later.
    """

    job_id: int
    job_label: str
    """Human-readable label for the legend (``"Job 0001234 · …"``)."""

    csv_name: str
    """Descriptor output name — the natural grouping key. Two jobs that
    emit the same descriptor output (even if their on-disk filenames
    diverge) belong on the same merged chart."""

    csv_filename: str
    """The actual filename under ``outputs/`` — only used in the
    individual-charts expander's title."""

    content: bytes
    """Raw CSV bytes; parsed lazily via pandas at render time."""

    plot_title: str | None = None
    """Descriptor's ``plot_title`` for this csv_plot output, when set —
    overrides the auto-generated chart title."""

    xaxis_title: str | None = None
    """Descriptor's ``xaxis_title`` — used as the X axis label so the
    user sees "Time (s)" instead of the raw CSV column name."""

    yaxis_title: str | None = None
    """Descriptor's ``yaxis_title`` — used as the Y axis label."""

    xaxis_column: int | None = None
    """0-indexed column to use for the X axis; ``None`` falls back to
    the first column in the CSV."""

    yaxis_column: int | None = None
    """0-indexed column to use for the Y axis; ``None`` falls back to
    every column after X."""


def render_overlay_section(
    outputs: Iterable[JobCsvOutput],
    *,
    job_order: list[int] | None = None,
    column_width_px: int | None = None,
) -> None:
    """Render the CSV-overlay block below the Compare grid.

    Layout:

    1. **Merged charts** — one chart per ``csv_name``, one trace per
       (job, column). Full-width because they're an "overall comparison"
       view rather than per-job.
    2. **"Show individual charts" expander** — when ``job_order`` is
       supplied, expands into a row of fixed-width columns matching
       the grid above (one column per job). Without ``job_order`` it
       falls back to a single-column stack — kept for legacy callers
       and tests, but the page passes the order so the visual line-up
       with the inputs/outputs grid is preserved.

    Bails silently when no outputs are supplied so callers can pipe
    every selected job's CSV bundle through unconditionally and let
    this function decide whether to render anything.
    """
    materialised = list(outputs)
    if not materialised:
        return

    by_csv_name: dict[str, list[JobCsvOutput]] = defaultdict(list)
    for entry in materialised:
        by_csv_name[entry.csv_name].append(entry)

    # Only merge CSVs shared by ≥2 jobs; a CSV that's only present in
    # one selection has nothing to overlay against, so it goes straight
    # into the individual-charts expander.
    merged_names = sorted(name for name, group in by_csv_name.items() if len(group) >= 2)

    if merged_names:
        with ui.column().classes("w-full gap-3"):
            ui.label("Merged charts").style("font-weight: 600;")
            for name in merged_names:
                _render_merged_chart(name, by_csv_name[name])

    # The expander always carries every (job, csv) pair, including the
    # ones already shown merged — gives the user the un-merged view
    # without making them deduce it.
    with ui.expansion("Show individual charts", icon="show_chart").classes("w-full q-mt-md"):
        if job_order:
            _render_individual_charts_per_column(materialised, job_order, column_width_px)
        else:
            with ui.column().classes("w-full gap-3"):
                for entry in materialised:
                    _render_individual_chart(entry)


_COLUMN_GUTTER_PX = 24
"""Card border + horizontal padding subtracted from the column width
when sizing each chart, so the plotly figure never overflows the
column it's rendered in."""


def _render_individual_charts_per_column(
    outputs: list[JobCsvOutput],
    job_order: list[int],
    column_width_px: int | None,
) -> None:
    """One column per job, charts stacked inside their own column.

    Mirrors the grid layout of the inputs / outputs cells above so the
    user reads the page as a coherent column-per-job structure rather
    than two unrelated stacks. Each chart is sized to ``column_width_px
    - _COLUMN_GUTTER_PX`` so plotly's intrinsic width matches the
    column — relying on ``responsive: true`` alone would cause the
    chart to render at its default ~700px width and overflow.
    """
    by_job: dict[int, list[JobCsvOutput]] = defaultdict(list)
    for entry in outputs:
        by_job[entry.job_id].append(entry)

    column_style = (
        f"flex: 0 0 {column_width_px}px; min-width: 0;"
        if column_width_px is not None
        else "flex: 1 1 0; min-width: 0;"
    )
    chart_width = column_width_px - _COLUMN_GUTTER_PX if column_width_px is not None else None
    with ui.row().classes("w-full items-start gap-3 no-wrap justify-center overflow-x-auto"):
        for job_id in job_order:
            entries = by_job.get(job_id, [])
            with ui.column().classes("gap-3").style(column_style):
                if not entries:
                    ui.label("(no CSV outputs)").style(
                        "color: var(--matofx-muted-text); font-size: 0.85rem;"
                    )
                    continue
                # Job label appears on the first entry — pull it out so
                # each column has a header and the per-chart titles can
                # drop the redundant prefix.
                ui.label(entries[0].job_label).style("font-weight: 600; font-size: 0.9rem;")
                for entry in entries:
                    _render_individual_chart(
                        entry,
                        show_job_in_title=False,
                        width_px=chart_width,
                    )


def _render_merged_chart(csv_name: str, group: list[JobCsvOutput]) -> None:
    """One merged chart for ``csv_name`` — one trace per (job, column).

    Axis + plot labels follow the descriptor's ``plot_title``,
    ``xaxis_title``, and ``yaxis_title`` when they're set (every job in
    the group emitted the same descriptor output, so taking those
    fields off the first entry is safe). When the descriptor doesn't
    declare them, fall back to the underlying CSV column names so the
    user never sees an unlabelled axis.
    """
    fig = go.Figure()
    any_trace = False
    first_x_name: str | None = None
    last_y_name: str | None = None
    for entry in group:
        df = _parse(entry)
        if df is None or df.empty or df.shape[1] < 2:
            continue
        x_idx, y_indices = _pick_axis_columns(df, entry)
        if x_idx is None or not y_indices:
            continue
        x_col = df.columns[x_idx]
        if first_x_name is None:
            first_x_name = str(x_col)
        for y_idx in y_indices:
            col_name = df.columns[y_idx]
            last_y_name = str(col_name)
            fig.add_trace(
                go.Scatter(
                    x=df[x_col],
                    y=df[col_name],
                    mode="lines",
                    name=f"{entry.job_label} · {col_name}",
                )
            )
            any_trace = True
    if not any_trace:
        ui.label(f"No usable rows in {csv_name} for any of the selected jobs.").style(
            "color: var(--matofx-muted-text);"
        )
        return

    # Descriptor-driven labels with sensible fallbacks. We look at the
    # first entry because every job in the group shares the same
    # descriptor output (that's how the grouping key was built).
    plot_title = group[0].plot_title or csv_name
    xaxis_title = group[0].xaxis_title or first_x_name or "X"
    yaxis_title = group[0].yaxis_title or last_y_name or "Y"

    fig.update_layout(
        title=plot_title,
        xaxis_title=xaxis_title,
        yaxis_title=yaxis_title,
        height=420,
        margin={"l": 60, "r": 20, "t": 50, "b": 50},
        legend={"orientation": "h", "y": -0.2},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")
    _render_plot_download(fig, filename=f"compare_{_slugify(csv_name)}.html")


def _pick_axis_columns(df: pd.DataFrame, entry: JobCsvOutput) -> tuple[int | None, list[int]]:
    """Resolve X + Y column indices for a (df, descriptor) pair.

    Falls back to (0, [1, 2, ...]) when the descriptor doesn't pin
    columns. Honours the 0-indexed ``xaxis_column`` / ``yaxis_column``
    when set. Out-of-range indices are ignored — better to render the
    chart with the wrong column than to silently skip the whole job's
    data because of a stale descriptor pin.
    """
    cols = df.shape[1]
    x_idx = entry.xaxis_column if entry.xaxis_column is not None else 0
    if not isinstance(x_idx, int) or x_idx < 0 or x_idx >= cols:
        x_idx = 0
    if entry.yaxis_column is not None:
        if 0 <= entry.yaxis_column < cols and entry.yaxis_column != x_idx:
            return x_idx, [entry.yaxis_column]
        return x_idx, []
    return x_idx, [i for i in range(cols) if i != x_idx]


def _render_individual_chart(
    entry: JobCsvOutput,
    *,
    show_job_in_title: bool = True,
    width_px: int | None = None,
) -> None:
    """One chart for one (job, csv) pair.

    ``show_job_in_title=False`` drops the job-label prefix from the
    chart title — used inside the per-column expander layout where the
    column already carries a job-label header above the charts.

    ``width_px`` pins the figure's intrinsic width. When set, the
    chart renders at exactly that width, which is the only reliable
    way to keep a plotly figure from overflowing a fixed-width flex
    column on first paint (the responsive ResizeObserver fires after
    the initial render). Leave it ``None`` for full-width contexts.
    """
    df = _parse(entry)
    label = f"{entry.job_label} · {entry.csv_filename}" if show_job_in_title else entry.csv_filename
    if df is None or df.empty or df.shape[1] < 2:
        ui.label(f"{label}: no plottable data.").style("color: var(--matofx-muted-text);")
        return
    x_idx, y_indices = _pick_axis_columns(df, entry)
    if x_idx is None or not y_indices:
        ui.label(f"{label}: no plottable data.").style("color: var(--matofx-muted-text);")
        return
    x_col = df.columns[x_idx]
    fig = go.Figure()
    for y_idx in y_indices:
        col_name = df.columns[y_idx]
        fig.add_trace(
            go.Scatter(
                x=df[x_col],
                y=df[col_name],
                mode="lines",
                name=str(col_name),
            )
        )
    last_y_name = str(df.columns[y_indices[-1]])
    layout: dict[str, object] = {
        "title": entry.plot_title or label,
        "xaxis_title": entry.xaxis_title or str(x_col),
        "yaxis_title": entry.yaxis_title or last_y_name,
        "height": 320,
        "margin": {"l": 55, "r": 15, "t": 40, "b": 45},
        "showlegend": True,
        "legend": {"orientation": "h", "y": -0.25, "font": {"size": 10}},
    }
    if width_px is not None:
        layout["width"] = width_px
        layout["autosize"] = False
    fig.update_layout(**layout)
    element = ui.plotly(themed_plotly(fig))
    if width_px is None:
        element.classes("w-full")
    else:
        element.style(f"width: {width_px}px; max-width: 100%;")
    _render_plot_download(
        fig,
        filename=f"job_{entry.job_id:07d}_{_slugify(entry.csv_filename)}.html",
    )


def _render_plot_download(fig: go.Figure, *, filename: str) -> None:
    """Small "Download as HTML" button under each chart.

    Uses plotly's CDN-embed mode so the downloaded file stays in the
    low-kB range; the user opens it in any browser and gets the same
    interactive chart they're looking at on screen.
    """
    ui.button(
        "Download as HTML",
        icon="download",
        on_click=lambda _e=None, _fig=fig, _name=filename: ui.download.content(
            export_plotly_html(_fig), _name
        ),
    ).props("flat dense color=primary size=sm").classes("self-end")


@dataclass(frozen=True)
class JobHtmlOutput:
    """One ``html_plot`` (or other ``.html``) output for one selected job.

    Same shape as :class:`JobCsvOutput` but carrying HTML bytes instead
    of CSV. The Compare page builds these by walking each selected
    job's output document and picking out file outputs whose filename
    ends in ``.html`` (typically a tool that wrote a plotly figure via
    ``write_html``). Kept distinct from :class:`JobCsvOutput` so the
    rendering paths don't have to switch on filename extension at
    every call site.
    """

    job_id: int
    job_label: str
    html_name: str
    html_filename: str
    content: bytes


def render_html_overlay_section(
    outputs: Iterable[JobHtmlOutput],
    *,
    job_order: list[int] | None = None,
    column_width_px: int | None = None,
) -> None:
    """Render the HTML-output block below the CSV overlays.

    Mirrors the CSV section's "Show individual charts" layout: one
    column per job, with each job's HTML files rendered as plotly
    figures (when the HTML carries a plotly figure JSON we can pluck
    out) or as iframes (the fallback). Bails silently when no outputs
    are supplied.
    """
    materialised = list(outputs)
    if not materialised:
        return

    with ui.expansion("Show HTML outputs", icon="html").classes("w-full q-mt-md"):
        if job_order:
            _render_html_per_column(materialised, job_order, column_width_px)
        else:
            with ui.column().classes("w-full gap-3"):
                for entry in materialised:
                    _render_individual_html(entry)


def _render_html_per_column(
    outputs: list[JobHtmlOutput],
    job_order: list[int],
    column_width_px: int | None,
) -> None:
    """One column per job, each column stacking the job's HTML outputs."""
    by_job: dict[int, list[JobHtmlOutput]] = defaultdict(list)
    for entry in outputs:
        by_job[entry.job_id].append(entry)

    column_style = (
        f"flex: 0 0 {column_width_px}px; min-width: 0;"
        if column_width_px is not None
        else "flex: 1 1 0; min-width: 0;"
    )
    chart_width = column_width_px - _COLUMN_GUTTER_PX if column_width_px is not None else None
    with ui.row().classes("w-full items-start gap-3 no-wrap justify-center overflow-x-auto"):
        for job_id in job_order:
            entries = by_job.get(job_id, [])
            with ui.column().classes("gap-3").style(column_style):
                if not entries:
                    ui.label("(no HTML outputs)").style(
                        "color: var(--matofx-muted-text); font-size: 0.85rem;"
                    )
                    continue
                ui.label(entries[0].job_label).style("font-weight: 600; font-size: 0.9rem;")
                for entry in entries:
                    _render_individual_html(entry, width_px=chart_width)


def _render_individual_html(entry: JobHtmlOutput, *, width_px: int | None = None) -> None:
    """Render a single HTML output — plotly figure first, srcdoc iframe second.

    Tries to extract the embedded plotly figure JSON (when the HTML was
    produced by ``plotly.io.write_html``) so the comparison view picks
    up the dashboard theme and matches the CSV charts above. Falls back
    to a self-contained ``<iframe srcdoc=...>`` so non-plotly HTML
    (legacy reports, hand-written summaries) still surfaces.
    """
    label = entry.html_filename
    try:
        html_text = entry.content.decode("utf-8", errors="replace")
    except Exception:
        ui.label(f"{label}: could not decode HTML bytes.").style("color: var(--matofx-muted-text);")
        return

    fig = _try_extract_plotly_figure(html_text)
    if fig is not None:
        ui.label(label).style("font-weight: 600; font-size: 0.85rem;")
        layout: dict[str, object] = {
            "height": 320,
            "margin": {"l": 45, "r": 15, "t": 30, "b": 35},
        }
        if width_px is not None:
            layout["width"] = width_px
            layout["autosize"] = False
        fig.update_layout(**layout)
        element = ui.plotly(themed_plotly(fig))
        if width_px is None:
            element.classes("w-full")
        else:
            element.style(f"width: {width_px}px; max-width: 100%;")
        return

    # Non-plotly HTML — embed inside an iframe via srcdoc so the bytes
    # don't have to round-trip through an authenticated GET (browser
    # iframes can't carry the bearer header). We escape quotes in the
    # srcdoc value; everything else in the HTML is the source page's
    # responsibility to escape.
    import html as _html  # noqa: PLC0415

    iframe_style = (
        "border:0; background:white; border-radius:4px;"
        + (f" width:{width_px}px;" if width_px is not None else " width:100%;")
        + " height:380px;"
    )
    ui.label(label).style("font-weight: 600; font-size: 0.85rem;")
    # ``sandbox="allow-scripts"`` (no ``allow-same-origin``) runs the
    # tool-produced HTML in an opaque origin — stored-XSS containment.
    ui.html(
        f'<iframe srcdoc="{_html.escape(html_text, quote=True)}" '
        f'sandbox="allow-scripts" style="{iframe_style}"></iframe>',
        sanitize=False,
    ).classes("w-full")


def _try_extract_plotly_figure(html_text: str) -> go.Figure | None:
    """Wrap the shared parser in a ``go.Figure`` constructor.

    Re-exported under the underscore-prefixed name because
    :mod:`matofx.ui.pages.compare.render` already imports it that way
    for its inline HTML rendering path. The real parser lives in
    :mod:`matofx.ui.plotly_extract` so the My Jobs viewer and the
    Compare workspace share one implementation (and one set of tests).
    """
    from matofx.ui.plotly_extract import extract_plotly_figure  # noqa: PLC0415

    payload = extract_plotly_figure(html_text)
    if payload is None:
        return None
    return go.Figure(data=payload["data"], layout=payload["layout"])


def _slugify(value: str) -> str:
    """Cheap, ASCII-safe slug for download filenames.

    Strips path components and replaces whitespace + non-word
    characters with single underscores; preserves dot-separated
    extensions where they exist.
    """
    import re  # noqa: PLC0415

    leaf = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf).strip("_")
    return cleaned or "chart"


def _parse(entry: JobCsvOutput) -> pd.DataFrame | None:
    """Parse the CSV bytes into a DataFrame, returning ``None`` on failure.

    A single bad CSV in a multi-job comparison shouldn't take the
    whole overlay section down — log and skip, the page renders the
    rest of the chart over the surviving jobs.
    """
    try:
        return pd.read_csv(io.BytesIO(entry.content))
    except Exception as exc:
        _log.warning(
            "compare.csv_parse_failed",
            extra={
                "job_id": entry.job_id,
                "csv_filename": entry.csv_filename,
                "error": str(exc),
            },
        )
        return None

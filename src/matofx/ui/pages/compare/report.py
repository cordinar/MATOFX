"""Self-contained HTML report for the Compare workspace.

Builds a single HTML document with the full side-by-side comparison:
header, per-column inputs / outputs / image cards (differing keys
highlighted), merged plotly charts, and per-column individual charts.
Plotly is included from CDN so the file stays small (~50 kB of HTML +
the figure JSON, vs. ~3 MB if plotly.js were inlined).

Two report shapes share one renderer:

- :func:`build_compare_report` — a **single** same-tool group (the Jobs
  mode and each tool/step in isolation). Header reads "Compare report —
  <tool>".
- :func:`build_multi_compare_report` — **several** groups under one
  document (the overall report for a Scenario or Workflow comparison —
  one section per matching tool / workflow step).

Both lay each group out as a CSS grid whose cards are **top-aligned**
(`align-items: start`), so the Inputs card of one column lines up with
the Inputs card of the next regardless of cell height — and a column a
participant doesn't have renders a top-aligned placeholder rather than
shifting the rest. Image outputs / inputs are embedded inline as
base64 data URLs (built by the page layer from the bytes it already
pre-fetched for the on-screen previews).

Two flavours of each:

- ``auto_print=False`` — plain HTML download.
- ``auto_print=True`` — same layout with a one-line script that calls
  ``window.print()`` once Plotly has finished rendering. Used by the
  "Download PDF" button. Bypasses kaleido entirely; the browser does
  the rendering.
"""

from __future__ import annotations

import datetime as _dt
import html
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import plotly.graph_objects as go
import plotly.io as pio

from matofx.ui._field_surrogate_viewer import (
    _field_animation_figure,
    _field_frame_figure,
)
from matofx.ui.pages.compare.field_overlay import JobFieldOutput
from matofx.ui.pages.compare.overlay import (
    JobCsvOutput,
    JobHtmlOutput,
    _try_extract_plotly_figure,
)
from matofx.ui.theme import (
    DARK_PALETTE,
    LIGHT_PALETTE,
    MATOFX_PLOTLY_CONFIG,
    Palette,
    apply_theme_to_plotly,
    palette_for,
)


@lru_cache(maxsize=1)
def _plotly_bundle() -> str:
    """Return the ``plotly.min.js`` body to inline into the report.

    Same approach as the per-job HTML report
    (:func:`matofx.reports.builder._plotly_bundle`), for the same two
    reasons.

    **Air-gap.** A downloaded report is a file that travels; it is
    opened on laptops, attached to emails, and read on sites with no
    internet. A ``<script src="https://cdn.plot.ly/…">`` gives a
    document that renders its text and shows blank rectangles where the
    charts should be, which is worse than refusing to produce it.

    **Version agreement.** This is the bug the pinned CDN URL actually
    had: the figure JSON is serialised by whichever ``plotly`` the
    install has (6.7.0 bundles plotly.js 3.5.0), while the pin named
    plotly.js 2.35.2 — a full major version behind, with no mechanism
    that would ever notice the drift. Taking the bundle from the same
    package that wrote the JSON makes the pair correct by construction
    and removes the "bump in lockstep" instruction nobody would
    remember. Reproducibility is preserved by the wheel's pinned
    ``plotly`` dependency, which is the honest place for it.

    Cached because the string is ~4.8 MB; behind a function so a report
    only pays the import when it is built.
    """
    from plotly.offline import get_plotlyjs  # noqa: PLC0415 — lazy on purpose.

    return str(get_plotlyjs())


@dataclass(frozen=True)
class ReportImage:
    """One embeddable image for a report column.

    ``data_url`` is a ready-to-render ``data:image/...;base64,...`` URL
    built by the page layer from bytes it already pre-fetched for the
    on-screen preview — the report module never touches the network.
    """

    caption: str
    data_url: str


@dataclass(frozen=True)
class ReportVideo:
    """One embeddable video for a report column.

    ``data_url`` is a ready-to-render ``data:video/...;base64,...`` URL
    built by the page layer from bytes it already pre-fetched for the
    on-screen preview. Videos ride only the **interactive HTML** report
    (``auto_print=False``) — the print/PDF flavour drops them, since a
    paper page can't play a video.
    """

    caption: str
    data_url: str


@dataclass(frozen=True)
class ReportJobEntry:
    """One column of a report group — a job, or a missing-participant slot.

    Mirrors :class:`compare.render.JobCellModel` (the report passes the
    descriptor once per group, not per column). ``present=False`` marks a
    participant (scenario / workflow run) that has no job for the group's
    tool; it renders as a top-aligned placeholder carrying
    ``missing_message`` so the surrounding columns stay aligned.
    """

    job_id: int
    job_label: str
    state: str
    participant_label: str = ""
    project_name: str = ""
    scenario_name: str = ""
    tool_name: str = ""
    tool_version: str = ""
    completed_display: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    output_values: dict[str, Any] = field(default_factory=dict)
    output_filenames: dict[str, str] = field(default_factory=dict)
    images: tuple[ReportImage, ...] = ()
    videos: tuple[ReportVideo, ...] = ()
    present: bool = True
    missing_message: str = ""


@dataclass(frozen=True)
class ReportGroup:
    """One same-tool comparison block within a report.

    ``heading`` names the section (a tool or a workflow step) in a
    multi-group report; the single-tool report leaves it empty.
    """

    descriptor: dict[str, Any]
    jobs: list[ReportJobEntry]
    differing_keys: set[str]
    csv_outputs: list[JobCsvOutput] = field(default_factory=list)
    html_outputs: list[JobHtmlOutput] = field(default_factory=list)
    field_outputs: list[JobFieldOutput] = field(default_factory=list)
    heading: str = ""


def build_compare_report(
    *,
    descriptor: dict[str, Any],
    jobs: list[ReportJobEntry],
    differing_keys: set[str],
    csv_outputs: list[JobCsvOutput],
    html_outputs: list[JobHtmlOutput] | None = None,
    field_outputs: list[JobFieldOutput] | None = None,
    theme: str = "dark",
    auto_print: bool = False,
) -> bytes:
    """Render a single same-tool comparison as a self-contained HTML document.

    Returns UTF-8 bytes ready to feed into
    :func:`nicegui.ui.download.content` (HTML button) or to surface via a
    blob URL (PDF button — the browser handles the PDF through its print
    dialog). ``field_outputs`` (time-resolved field animations) and any
    video columns ride the **HTML** flavour only; ``auto_print=True``
    (the PDF path) drops both — a paper page can't play them.
    """
    tool_name = str(descriptor.get("name") or descriptor.get("alias") or "tool")
    group = ReportGroup(
        descriptor=descriptor,
        jobs=jobs,
        differing_keys=differing_keys,
        csv_outputs=csv_outputs,
        html_outputs=html_outputs or [],
        field_outputs=field_outputs or [],
        heading="",
    )
    return _build_document(
        title="Compare report",
        subtitle=f"Tool: {tool_name} · {len(jobs)} columns",
        groups=[group],
        theme=theme,
        auto_print=auto_print,
    )


def build_multi_compare_report(
    *,
    title: str,
    subtitle: str,
    groups: list[ReportGroup],
    theme: str = "dark",
    auto_print: bool = False,
) -> bytes:
    """Render several comparison groups under one document — the overall report.

    Used by Scenario / Workflow comparisons: one :class:`ReportGroup` per
    matching tool (or workflow step), each its own section with a grid +
    charts, under a shared header. ``subtitle`` typically names the
    scenarios / runs being compared.
    """
    return _build_document(
        title=title,
        subtitle=subtitle,
        groups=groups,
        theme=theme,
        auto_print=auto_print,
    )


# ---------- document assembly ----------------------------------------------


def _build_document(
    *,
    title: str,
    subtitle: str,
    groups: list[ReportGroup],
    theme: str,
    auto_print: bool,
) -> bytes:
    palette = palette_for(theme)
    timestamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    multi = len(groups) > 1
    # Field animations + videos render only in the interactive HTML
    # download; the print/PDF flavour (auto_print) drops them — neither
    # plays on paper.
    interactive = not auto_print
    sections = "\n".join(
        _render_group_section(group, palette, with_heading=multi, interactive=interactive)
        for group in groups
    )
    body = "\n".join(
        [
            _render_header(title, subtitle, timestamp),
            sections,
            _render_footer(timestamp),
        ]
    )
    document = _DOCUMENT_TEMPLATE.format(
        title=html.escape(f"MATOFX — {title}"),
        css=_render_css(palette),
        plotly_js=_plotly_bundle(),
        head_extra=_AUTO_PRINT_SCRIPT if auto_print else "",
        body=body,
    )
    return document.encode("utf-8")


def _render_header(title: str, subtitle: str, timestamp: str) -> str:
    return (
        '<header class="report-header">'
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="meta">{html.escape(subtitle)} · Generated {html.escape(timestamp)}</p>'
        "</header>"
    )


def _render_footer(timestamp: str) -> str:
    return (
        '<footer class="report-footer">'
        f"<p>Generated by MATOFX · {html.escape(timestamp)}</p>"
        "</footer>"
    )


# ---------- one group: grid + charts ---------------------------------------


def _render_group_section(
    group: ReportGroup, palette: Palette, *, with_heading: bool, interactive: bool
) -> str:
    """A section: optional heading, the top-aligned card grid, then charts.

    ``interactive`` gates the field-animation section (and the per-column
    video cells inside the grid) — they're included in the HTML download
    but dropped from the print/PDF flavour.
    """
    heading_html = (
        f"<h2>{html.escape(group.heading)}</h2>" if with_heading and group.heading else ""
    )
    field_html = (
        _render_field_outputs(group.field_outputs, group.jobs, palette) if interactive else ""
    )
    return (
        '<section class="group-section">'
        f"{heading_html}"
        f"{_render_group_grid(group, interactive=interactive)}"
        f"{_render_merged_charts(group.csv_outputs, palette)}"
        f"{_render_individual_charts(group.csv_outputs, group.jobs, palette)}"
        f"{_render_html_outputs(group.html_outputs, group.jobs, palette)}"
        f"{field_html}"
        "</section>"
    )


def _render_group_grid(group: ReportGroup, *, interactive: bool) -> str:
    """One CSS grid: a row each for header / inputs / outputs / images.

    Every row emits exactly one cell per column (placeholder for a
    missing participant), and ``align-items: start`` keeps each row's
    cards top-aligned across columns — the report mirrors the on-screen
    grid's alignment.
    """
    descriptor = group.descriptor
    jobs = group.jobs
    n = max(len(jobs), 1)

    headers = "".join(_render_header_cell(job) for job in jobs)
    inputs = "".join(
        _render_io_cell(
            job,
            title="Inputs",
            descriptor_groups=descriptor.get("input_groups") or [],
            descriptor_fields=descriptor.get("inputs") or [],
            values=job.inputs,
            filenames={},
            differing_keys=group.differing_keys,
        )
        for job in jobs
    )
    outputs = "".join(
        _render_io_cell(
            job,
            title="Outputs",
            descriptor_groups=descriptor.get("output_groups") or [],
            descriptor_fields=descriptor.get("outputs") or [],
            values=job.output_values,
            filenames=job.output_filenames,
            differing_keys=group.differing_keys,
        )
        for job in jobs
    )
    cells = headers + inputs + outputs
    if any(job.images for job in jobs):
        cells += "".join(_render_images_cell(job) for job in jobs)
    # Video cells ride the interactive HTML only (a paper page can't play
    # them); each present column with videos gets a cell, the rest hold
    # the grid line-up with a placeholder.
    if interactive and any(job.videos for job in jobs):
        cells += "".join(_render_videos_cell(job) for job in jobs)
    return (
        f'<div class="grid" style="grid-template-columns: repeat({n}, minmax(0, 1fr));">'
        f"{cells}</div>"
    )


def _render_header_cell(job: ReportJobEntry) -> str:
    """One column's header: participant, label, tool/version, project, state, completion."""
    parts: list[str] = []
    if job.participant_label:
        parts.append(f'<div class="participant">{html.escape(job.participant_label)}</div>')
    if not job.present:
        message = job.missing_message or "Not available here."
        parts.append(f'<div class="missing">{html.escape(message)}</div>')
        return f'<div class="job-head">{"".join(parts)}</div>'
    parts.append(f'<div class="job-label">{html.escape(job.job_label)}</div>')
    if job.tool_name or job.tool_version:
        tool_line = job.tool_name
        if job.tool_version:
            tool_line = f"{tool_line} v{job.tool_version}".strip()
        parts.append(f'<div class="sub">{html.escape(tool_line)}</div>')
    context = " / ".join(part for part in (job.project_name, job.scenario_name) if part)
    if context:
        parts.append(f'<div class="sub">{html.escape(context)}</div>')
    parts.append(f'<div class="state">{html.escape(_state_label(job.state))}</div>')
    if job.completed_display:
        parts.append(f'<div class="sub">Completed {html.escape(job.completed_display)}</div>')
    return f'<div class="job-head">{"".join(parts)}</div>'


def _render_io_cell(
    job: ReportJobEntry,
    *,
    title: str,
    descriptor_groups: list[dict[str, Any]],
    descriptor_fields: list[dict[str, Any]],
    values: dict[str, Any],
    filenames: dict[str, str],
    differing_keys: set[str],
) -> str:
    """One Inputs or Outputs card — or a placeholder for a missing column."""
    if not job.present:
        return '<div class="placeholder"></div>'
    if not descriptor_fields:
        return (
            f'<div class="card"><h4>{html.escape(title)}</h4>'
            '<p class="muted">(none declared)</p></div>'
        )
    fields_by_name = {f["name"]: f for f in descriptor_fields if f.get("name")}
    sections: list[str] = []
    if descriptor_groups:
        for grp in descriptor_groups:
            group_name = grp.get("name")
            group_label = grp.get("label") or group_name or ""
            field_names = [
                f["name"]
                for f in descriptor_fields
                if f.get("name") and f.get("group") == group_name
            ]
            rows = "".join(
                _render_row(
                    fields_by_name.get(name, {"label": name}),
                    values.get(name),
                    filenames.get(name),
                    name in differing_keys,
                )
                for name in field_names
            )
            sections.append(
                f'<div class="group">'
                f'<div class="group-label">{html.escape(group_label)}</div>'
                f"{rows}</div>"
            )
    else:
        rows = "".join(
            _render_row(
                fields_by_name.get(name, {"label": name}),
                values.get(name),
                filenames.get(name),
                name in differing_keys,
            )
            for name in (f["name"] for f in descriptor_fields if f.get("name"))
        )
        sections.append(f'<div class="group">{rows}</div>')
    return f'<div class="card"><h4>{html.escape(title)}</h4>{"".join(sections)}</div>'


def _render_images_cell(job: ReportJobEntry) -> str:
    """One column's embedded images — or an empty placeholder to hold the grid cell."""
    if not job.present or not job.images:
        return '<div class="placeholder"></div>'
    figures = "".join(
        f'<figure class="report-image">'
        f'<img src="{img.data_url}" alt="{html.escape(img.caption)}">'
        f"<figcaption>{html.escape(img.caption)}</figcaption>"
        f"</figure>"
        for img in job.images
    )
    return f'<div class="card"><h4>Images</h4>{figures}</div>'


def _render_videos_cell(job: ReportJobEntry) -> str:
    """One column's embedded videos — or an empty placeholder to hold the grid cell.

    The bytes are inlined as a ``data:`` URL so the report stays a single
    self-contained file. ``preload="metadata"`` keeps the page from
    eagerly decoding every video at once.
    """
    if not job.present or not job.videos:
        return '<div class="placeholder"></div>'
    figures = "".join(
        f'<figure class="report-video">'
        f'<video src="{v.data_url}" controls preload="metadata"></video>'
        f"<figcaption>{html.escape(v.caption)}</figcaption>"
        f"</figure>"
        for v in job.videos
    )
    return f'<div class="card"><h4>Videos</h4>{figures}</div>'


def _render_row(
    spec: dict[str, Any],
    value: Any,
    filename: str | None,
    is_differing: bool,
) -> str:
    label = str(spec.get("label") or spec.get("name") or "?")
    unit = str(spec.get("unit") or "")
    field_type = str(spec.get("type") or "")
    cls = "row diff" if is_differing else "row"
    # Material inputs carry a frozen block (dict, or list for a
    # formulation), not a scalar — render it as a small card so the report
    # matches the on-screen view instead of dumping the raw dict.
    if field_type == "material" and isinstance(value, dict | list):
        return (
            f'<div class="{cls}">'
            f'<span class="row-label">{html.escape(label)}</span>'
            f'<span class="row-value">{_format_material_html(value)}</span>'
            "</div>"
        )
    if field_type == "file":
        display = filename or _format_value(value, field_type) or "—"
    else:
        display = _format_value(value, field_type) or "—"
        if unit:
            display = f"{display} {unit}"
    return (
        f'<div class="{cls}">'
        f'<span class="row-label">{html.escape(label)}</span>'
        f'<span class="row-value">{html.escape(str(display))}</span>'
        "</div>"
    )


def _format_material_html(value: Any) -> str:
    """Escaped HTML for a frozen material block (or list of blocks).

    A summary header (name + version + class) over the frozen properties
    as ``Property: value unit`` lines, one small card per component. Used
    by :func:`_render_row` so a material input reads cleanly in the report
    rather than as a raw ``{'material_id': …}`` dict.
    """
    blocks = [b for b in (value if isinstance(value, list) else [value]) if isinstance(b, dict)]
    if not blocks:
        return "—"
    cards: list[str] = []
    for block in blocks:
        name = str(block.get("name") or "(unnamed)")
        ordinal = block.get("version_ordinal")
        header = f"{name} v{ordinal}" if ordinal else name
        class_name = str(block.get("class") or "")
        if class_name:
            header = f"{header} — class: {class_name}"
        props_map = block.get("properties") or {}
        lines: list[str] = []
        for slug in sorted(props_map):
            prop = props_map[slug] or {}
            prop_name = str(prop.get("prop_name") or slug)
            val_text = _format_value(prop.get("value"), "")
            prop_unit = str(prop.get("unit") or "")
            if prop_unit:
                val_text = f"{val_text} {prop_unit}"
            lines.append(
                f'<div style="font-size:0.85em;">{html.escape(prop_name)}: '
                f"{html.escape(val_text)}</div>"
            )
        body = "".join(lines) or '<div style="font-size:0.85em;">(no properties)</div>'
        cards.append(
            '<div style="border:1px solid rgba(128,128,128,0.35);border-radius:6px;'
            'padding:4px 8px;margin:2px 0;">'
            f'<div style="font-weight:600;">{html.escape(header)}</div>{body}</div>'
        )
    return "".join(cards)


def _format_value(value: Any, field_type: str) -> str:
    if value is None or value == "":
        return ""
    if field_type == "file":
        text = str(value)
        return text.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list | tuple):
        if not value:
            return "(none)"
        return ", ".join(_format_value(v, "") for v in value)
    return str(value)


def _state_label(state: str) -> str:
    if state == "complete":
        return "Finished successfully"
    if state == "failed":
        return "Finished with errors"
    return state.title()


# ---------- charts ---------------------------------------------------------


def _render_merged_charts(csv_outputs: list[JobCsvOutput], palette: Palette) -> str:
    """One merged chart per CSV name shared by ≥2 jobs."""
    if not csv_outputs:
        return ""
    by_csv: dict[str, list[JobCsvOutput]] = defaultdict(list)
    for entry in csv_outputs:
        by_csv[entry.csv_name].append(entry)
    merged_names = sorted(name for name, group in by_csv.items() if len(group) >= 2)
    if not merged_names:
        return ""
    fragments = [_render_merged_chart_html(name, by_csv[name], palette) for name in merged_names]
    return f'<div class="charts"><h3>Merged charts</h3>{"".join(fragments)}</div>'


def _render_merged_chart_html(name: str, group: list[JobCsvOutput], palette: Palette) -> str:
    fig = go.Figure()
    any_trace = False
    for entry in group:
        df = _safe_read_csv(entry)
        if df is None or df.empty or df.shape[1] < 2:
            continue
        x_col = df.columns[0]
        for col_name in df.columns[1:]:
            fig.add_trace(
                go.Scatter(
                    x=df[x_col].tolist(),
                    y=df[col_name].tolist(),
                    mode="lines",
                    name=f"{entry.job_label} · {col_name}",
                )
            )
            any_trace = True
    if not any_trace:
        return f'<p class="muted">No usable rows in {html.escape(name)}.</p>'
    fig.update_layout(
        title=name,
        height=420,
        margin={"l": 50, "r": 20, "t": 50, "b": 40},
        legend={"orientation": "h", "y": -0.2},
    )
    apply_theme_to_plotly(fig, palette.name)
    return _figure_fragment(fig)


def _render_individual_charts(
    csv_outputs: list[JobCsvOutput],
    jobs: list[ReportJobEntry],
    palette: Palette,
) -> str:
    """Per-column individual charts, aligned to the group's column order."""
    if not csv_outputs:
        return ""
    by_job: dict[int, list[JobCsvOutput]] = defaultdict(list)
    for entry in csv_outputs:
        by_job[entry.job_id].append(entry)
    n = max(len(jobs), 1)
    columns: list[str] = []
    for job in jobs:
        entries = by_job.get(job.job_id, []) if job.present else []
        if not entries:
            columns.append('<div class="placeholder"></div>')
            continue
        charts = "".join(_render_individual_chart_html(e, palette) for e in entries)
        columns.append(f'<div class="card">{charts}</div>')
    return (
        '<div class="charts"><h3>Individual charts</h3>'
        f'<div class="grid" style="grid-template-columns: repeat({n}, minmax(0, 1fr));">'
        f"{''.join(columns)}</div></div>"
    )


def _render_individual_chart_html(entry: JobCsvOutput, palette: Palette) -> str:
    df = _safe_read_csv(entry)
    if df is None or df.empty or df.shape[1] < 2:
        return f'<p class="muted">{html.escape(entry.csv_filename)}: no plottable data.</p>'
    fig = go.Figure()
    x_col = df.columns[0]
    for col_name in df.columns[1:]:
        fig.add_trace(
            go.Scatter(
                x=df[x_col].tolist(),
                y=df[col_name].tolist(),
                mode="lines",
                name=str(col_name),
            )
        )
    fig.update_layout(
        title=entry.csv_filename,
        height=320,
        margin={"l": 45, "r": 15, "t": 40, "b": 35},
        showlegend=True,
        legend={"orientation": "h", "y": -0.25, "font": {"size": 10}},
    )
    apply_theme_to_plotly(fig, palette.name)
    return _figure_fragment(fig)


def _render_html_outputs(
    html_outputs: list[JobHtmlOutput],
    jobs: list[ReportJobEntry],
    palette: Palette,
) -> str:
    """Per-column HTML outputs (e.g. a plotly heatmap a tool wrote to disk).

    Mirrors the on-screen "HTML outputs" treatment: a plotly ``write_html``
    file has its figure extracted and re-rendered against the report's
    CDN plotly.js (so it's themed and crisp), and any other HTML embeds
    via ``<iframe srcdoc>``. Laid out as one column per job, aligned to
    the group's column order (placeholder for a missing participant).
    """
    if not html_outputs:
        return ""
    by_job: dict[int, list[JobHtmlOutput]] = defaultdict(list)
    for entry in html_outputs:
        by_job[entry.job_id].append(entry)
    n = max(len(jobs), 1)
    columns: list[str] = []
    for job in jobs:
        entries = by_job.get(job.job_id, []) if job.present else []
        if not entries:
            columns.append('<div class="placeholder"></div>')
            continue
        fragments = "".join(_render_html_output_fragment(e, palette) for e in entries)
        columns.append(f'<div class="card">{fragments}</div>')
    return (
        '<div class="charts"><h3>HTML outputs</h3>'
        f'<div class="grid" style="grid-template-columns: repeat({n}, minmax(0, 1fr));">'
        f"{''.join(columns)}</div></div>"
    )


def _render_html_output_fragment(entry: JobHtmlOutput, palette: Palette) -> str:
    """One HTML output: themed plotly figure if extractable, else an iframe."""
    try:
        html_text = entry.content.decode("utf-8", errors="replace")
    except Exception:
        return f'<p class="muted">{html.escape(entry.html_filename)}: could not decode.</p>'
    fig = _try_extract_plotly_figure(html_text)
    if fig is not None:
        fig.update_layout(height=360, margin={"l": 45, "r": 15, "t": 40, "b": 35})
        apply_theme_to_plotly(fig, palette.name)
        return _figure_fragment(fig)
    # Non-plotly HTML (a hand-written summary, a legacy DDP report) — embed
    # the document verbatim in a sandboxed iframe so it renders as the tool
    # wrote it without leaking styles into the report. ``allow-scripts``
    # (no ``allow-same-origin``) keeps any embedded script in an opaque
    # origin so it can't reach the surrounding report's origin — stored-XSS
    # containment for tool-produced HTML baked into the downloadable report.
    escaped = html.escape(html_text, quote=True)
    return (
        f'<figure class="report-image">'
        f'<iframe srcdoc="{escaped}" sandbox="allow-scripts" '
        f'style="border:0; background:white; border-radius:4px; '
        f'width:100%; height:380px;"></iframe>'
        f"<figcaption>{html.escape(entry.html_filename)}</figcaption>"
        f"</figure>"
    )


def _render_field_outputs(
    field_outputs: list[JobFieldOutput],
    jobs: list[ReportJobEntry],
    palette: Palette,
) -> str:
    """Per-column field animations (transient ``.vtp`` series etc.).

    Mirrors the on-screen field overlay: one self-contained Plotly
    animation per job, aligned to the group's column order (placeholder
    for a missing participant). The figure carries its own play/pause +
    step slider, so it plays straight from the downloaded HTML — the
    reason this section is HTML-only and dropped from the PDF.
    """
    if not field_outputs:
        return ""
    by_job: dict[int, list[JobFieldOutput]] = defaultdict(list)
    for entry in field_outputs:
        by_job[entry.job_id].append(entry)
    n = max(len(jobs), 1)
    columns: list[str] = []
    for job in jobs:
        entries = by_job.get(job.job_id, []) if job.present else []
        if not entries:
            columns.append('<div class="placeholder"></div>')
            continue
        fragments = "".join(_render_field_output_fragment(e, palette) for e in entries)
        columns.append(f'<div class="card">{fragments}</div>')
    return (
        '<div class="charts"><h3>Field animations</h3>'
        f'<div class="grid" style="grid-template-columns: repeat({n}, minmax(0, 1fr));">'
        f"{''.join(columns)}</div></div>"
    )


def _render_field_output_fragment(entry: JobFieldOutput, palette: Palette) -> str:
    """One field output as a themed, self-contained Plotly animation fragment."""
    frames: list[list[float]] = [list(f) for f in (entry.data.get("frames") or [])]
    label = entry.field_label or entry.field_name
    if not frames:
        return f'<p class="muted">{html.escape(label)}: no field data.</p>'
    all_values = [v for fr in frames for v in fr]
    c_range = (min(all_values), max(all_values)) if all_values else None
    if len(frames) > 1:
        fig = _field_animation_figure(entry.data, frames, label, c_range)
    else:
        fig = _field_frame_figure(entry.data, frames[0], label, c_range)
    apply_theme_to_plotly(fig, palette.name)
    return _figure_fragment(fig)


def _figure_fragment(fig: go.Figure) -> str:
    """Render one Plotly figure as an HTML fragment (no <html>, no plotly.js)."""
    fragment = pio.to_html(
        fig,
        include_plotlyjs=False,
        full_html=False,
        config=dict(MATOFX_PLOTLY_CONFIG),
    )
    return str(fragment)


def _safe_read_csv(entry: JobCsvOutput) -> Any:
    """Parse CSV bytes, returning ``None`` on failure (logged in overlay.py too)."""
    import io  # noqa: PLC0415

    import pandas as pd  # noqa: PLC0415

    try:
        return pd.read_csv(io.BytesIO(entry.content))
    except Exception:
        return None


# ---------- CSS + document templates ---------------------------------------


def _render_css(palette: Palette) -> str:
    """Print-friendly CSS scoped to the report shape.

    Every colour is a literal here (not a CSS variable referencing
    ``--matofx-*``) because the report file is read in isolation — no
    chrome-injected variables to fall back to.
    """
    is_dark = palette.name == "dark"
    card_bg = palette.card_bg if not is_dark else "rgba(255,255,255,0.02)"
    return f"""
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: {palette.page_bg};
      color: {palette.body_text};
      margin: 0;
      padding: 24px;
    }}
    h1 {{ font-size: 1.6rem; margin: 0 0 4px; }}
    h2 {{ font-size: 1.2rem; margin: 28px 0 12px; }}
    h3 {{ font-size: 1rem; margin: 16px 0 8px; }}
    h4 {{ font-size: 0.9rem; margin: 0 0 8px; }}
    .meta {{ color: {palette.muted_text}; font-size: 0.85rem; margin: 0 0 16px; }}
    .group-section {{ margin-bottom: 28px; }}
    .grid {{
      display: grid;
      gap: 12px;
      align-items: start;
      width: 100%;
    }}
    .job-head {{ min-width: 0; }}
    .participant {{ font-weight: 700; font-size: 0.95rem; }}
    .job-label {{ font-weight: 600; font-size: 0.9rem; }}
    .sub {{ color: {palette.muted_text}; font-size: 0.75rem; }}
    .state {{ color: {palette.muted_text}; font-size: 0.8rem; margin: 2px 0; }}
    .missing {{ color: {palette.muted_text}; font-size: 0.8rem; font-style: italic; }}
    .placeholder {{ min-width: 0; }}
    .card {{
      background: {card_bg};
      border: 1px solid {palette.card_border};
      border-radius: 4px;
      padding: 10px 12px;
      min-width: 0;
    }}
    .group + .group {{ margin-top: 8px; }}
    .group-label {{
      font-size: 0.8rem; font-weight: 600; color: {palette.muted_text};
      margin-bottom: 4px;
    }}
    .row {{
      display: flex; gap: 8px; align-items: baseline;
      font-size: 0.85rem; padding: 2px 0;
    }}
    .row-label {{ flex: 0 0 9rem; color: {palette.label_text}; }}
    .row-value {{ flex: 1 1 0; word-break: break-word; }}
    .row.diff .row-value {{ font-weight: 600; color: {palette.error_text}; }}
    .muted {{ color: {palette.muted_text}; font-size: 0.85rem; }}
    .report-image {{ margin: 0 0 10px; }}
    .report-image img {{
      width: 100%; height: auto; border: 1px solid {palette.card_border};
      border-radius: 4px; background: white;
    }}
    .report-image figcaption {{
      color: {palette.muted_text}; font-size: 0.75rem; margin-top: 2px;
      word-break: break-word;
    }}
    .report-video {{ margin: 0 0 10px; }}
    .report-video video {{
      width: 100%; height: auto; border: 1px solid {palette.card_border};
      border-radius: 4px; background: black;
    }}
    .report-video figcaption {{
      color: {palette.muted_text}; font-size: 0.75rem; margin-top: 2px;
      word-break: break-word;
    }}
    .charts {{ margin-top: 16px; }}
    .report-footer {{
      margin-top: 32px; padding-top: 12px;
      border-top: 1px solid {palette.card_border};
      color: {palette.muted_text}; font-size: 0.8rem;
    }}
    @media print {{
      body {{ background: white; color: black; padding: 0; }}
      .card {{ background: white; border-color: #999; }}
      .row.diff .row-value {{ color: #c0392b; }}
      .report-image img {{ border-color: #999; }}
      h2, h3 {{ page-break-after: avoid; }}
      .group-section {{ page-break-inside: auto; }}
      @page {{ size: A4; margin: 12mm; }}
    }}
    """


_AUTO_PRINT_SCRIPT = """
<script>
window.addEventListener('load', function () {
  // Defer until plotly has had a chance to lay out the figures —
  // 800 ms is generous for a typical comparison and avoids calling
  // print() before the charts have rendered.
  setTimeout(function () { window.print(); }, 800);
});
</script>
"""


_DOCUMENT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{css}</style>
<script>{plotly_js}</script>
{head_extra}
</head>
<body>
{body}
</body>
</html>
"""


__all__ = [
    "DARK_PALETTE",
    "LIGHT_PALETTE",
    "ReportGroup",
    "ReportImage",
    "ReportJobEntry",
    "ReportVideo",
    "build_compare_report",
    "build_multi_compare_report",
]

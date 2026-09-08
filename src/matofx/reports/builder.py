"""Per-job HTML report builder.

Renders ``templates/report.html.j2`` against a :class:`ReportContext`
the API endpoint has resolved (job + tool + branding + locale data).
Returns a self-contained HTML string ready to stream as ``text/html``.

The Jinja2 environment is set up once per process (template files are
shipped inside the package); autoescaping is on for the ``html``
namespace so any user-supplied string in the context is automatically
HTML-escaped — the template only marks the inline stylesheet block
as ``| safe`` since that's a constant we own.

Later slices extend this module with descriptor-grouped Inputs /
Outputs sections, interactive Plotly figures, embedded media, and
workflow context; the :func:`build_report` signature is the stable
contract the router depends on.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, PackageLoader, select_autoescape

from matofx.reports.assets import (
    is_3d_animation_file,
    is_html_file,
    is_pdf_file,
    is_previewable_image,
    is_video_file,
)
from matofx.summaries import SummaryText

MAX_CSV_TABLE_ROWS = 100
"""Per ADR-0004: CSV tables embed the first 100 rows; anything past
that surfaces a 'N more rows — available in MATOFX' truncation note."""


@dataclass(frozen=True)
class ReportContext:
    """Inputs to one :func:`build_report` call.

    Frozen dataclass so call sites can grow extra fields (inputs,
    outputs, workflow context) in later slices without breaking the
    existing call sites — every new attribute is added here with a
    default, and the router populates whichever ones it has resolved.
    """

    job_id: int
    """The job's primary key."""

    tool_alias: str
    """Stable machine alias, e.g. ``material_properties``."""

    tool_name: str
    """Human-readable tool name from the ``tool`` row, e.g.
    ``Material Properties``."""

    tool_version: str
    """Version string from the captured ``tool_version`` snapshot,
    e.g. ``1.4.2`` — pinned at submit time so a metadata edit on the
    live tool after submission can't drift the report."""

    submitted_by_username: str
    """Username of the actor who created the job."""

    submitted_at: _dt.datetime | None
    """When the job row was created (``job.created_at``)."""

    completed_at: _dt.datetime | None
    """When the runner stamped the terminal ``complete`` state
    (``job.completed_at``)."""

    report_generated_at: _dt.datetime
    """Wall-clock UTC at endpoint dispatch — the moment this download
    was produced."""

    app_display_title: str = "MATOFX"
    """Branded app title from
    :class:`matofx.ui.branding_cache.BrandingSnapshot`. Falls back to
    ``MATOFX`` when no override is set."""

    logo_data_uri: str | None = None
    """Base64 ``data:`` URI of the branded header icon, or ``None``
    when no header icon is configured. Built by
    :func:`matofx.reports.assets.inline_logo` from
    ``branding.icons_dir`` + the cached basename."""

    timezone: str = "UTC"
    """IANA timezone for rendered timestamps (e.g. ``Europe/London``).
    ``"UTC"`` or ``"auto"`` keep timestamps in UTC — the safe default
    when the requesting user hasn't picked a preference."""

    locale: str = "auto"
    """BCP-47 locale hint (e.g. ``en-GB``). Currently informational —
    timestamps use a locale-neutral ``%d %b %Y, %H:%M %Z`` format —
    but the slot is here so a future babel-based renderer can swap
    in without changing the call site."""

    descriptor: dict[str, Any] = field(default_factory=dict)
    """Captured tool descriptor (``tool_version.descriptor_snapshot``).
    Drives the descriptor-grouped Inputs section's layout, labels,
    units, and field types. Defaulted to ``{}`` so slice-1/2 unit
    tests that don't care about Inputs keep working."""

    job_inputs: dict[str, Any] = field(default_factory=dict)
    """The submitted ``job.inputs`` map keyed by descriptor field name."""

    input_image_previews: dict[str, str] = field(default_factory=dict)
    """Mapping from a file-input *value* (the absolute path stored on
    ``job.inputs``) to a base64 ``data:`` URI. Populated by the router
    via :func:`matofx.reports.assets.inline_input_image`. Entries are
    only present for files that resolved safely + were recognised as
    previewable images; everything else falls through to filename-only
    rendering inside the template."""

    outputs_doc: dict[str, Any] = field(default_factory=dict)
    """Parsed ``outputs/outputs.json`` (an :class:`OutputsDocument`
    converted to ``dict``). Carries the list of ``{name, type, value,
    unit, filename}`` entries the tool produced — used by the
    Outputs section to look up runtime values for each descriptor
    output."""

    output_image_previews: dict[str, str] = field(default_factory=dict)
    """``{filename: data:URI}`` for output files that are recognised
    images. Populated by the router from each file output's
    ``filename`` resolved against the job's working-dir; empty entries
    fall through to filename-only rendering."""

    output_csv_tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``{filename: payload}`` for ``file_type=csv_table`` outputs.
    Each payload carries ``{"columns": [...], "rows": [...],
    "total_rows": int, "truncated": bool}``: the first
    ``MAX_CSV_TABLE_ROWS`` of the file, the descriptor-declared column
    order, and a flag the template uses to surface the
    "N more rows — available in MATOFX" note."""

    output_csv_plots: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``{filename: payload}`` for ``file_type=csv_plot`` outputs.
    Each payload carries ``{"figure_json": str, "div_id": str}``: the
    pre-serialised ``{"data": [...], "layout": {...}}`` figure ready
    for ``Plotly.newPlot`` plus a unique div id. ``plotly.min.js`` is
    bundled once at the top of the document only when at least one
    chart is present, so a job with no plots doesn't pay the ~3.5 MB
    JS-bundle weight."""

    output_videos: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``{filename: {data_uri, size_bytes, mime}}`` for video outputs
    that fit under the ADR-0004 25 MB cap. Anything bigger appears in
    ``output_file_notes`` instead — filename + size + "over cap" note."""

    output_pdfs: dict[str, str] = field(default_factory=dict)
    """``{filename: data:URI}`` for PDF outputs the report embeds via
    ``<embed type="application/pdf">``."""

    output_html_embeds: dict[str, str] = field(default_factory=dict)
    """``{filename: data:URI}`` for HTML outputs the report embeds via
    ``<iframe sandbox src="data:text/html;...">``."""

    output_file_notes: dict[str, str] = field(default_factory=dict)
    """``{filename: note}`` overrides for the ``file_other`` branch
    so 3D models, oversized videos, etc. get a more informative
    inline note than the default "available in MATOFX"."""

    workflow_context: WorkflowContext | None = None
    """When the job is part of a workflow run, the parent workflow
    + sibling jobs (each with its own descriptor + inputs). ``None``
    suppresses the workflow card entirely so standalone jobs render
    as they do today. See ADR-0004 §5."""

    kpi_cards: list[dict[str, Any]] = field(default_factory=list)
    """Headline KPI cards (ADR-0012 §8.4). Each entry is
    ``{kpi_name, kpi_label, kpi_unit, value}`` — the executive-summary
    block reads from ``job_kpi_value`` not the bulk outputs document,
    so the report stays renderable even after ADR-0003 retention has
    dropped ``outputs.json``. Empty list → the section is suppressed
    entirely; flag-off callers pass ``[]`` so the block doesn't render
    when KPI surfaces are absent for the install."""

    summary: SummaryText | None = None
    """Auto-generated results summary (``designs/auto-summaries.md``).
    The deterministic templated narrative — or, when enrichment is enabled
    and the viewer hasn't opted out, the guard-passed LLM prose — for this
    job. ``None`` suppresses the Summary card entirely. Rendered as plain
    paragraphs (no assets) so the report stays self-contained."""

    provenance_svg: str | None = None
    """Inlined SVG of the job's lineage / provenance graph (ADR-0010
    slice 6). ``None`` suppresses the Provenance section entirely —
    the default, since the toggle is off by default to keep report
    sizes predictable. When the report endpoint is called with
    ``?include_provenance=true``, the router builds the lineage
    graph via :class:`matofx.lineage.LineageService` and renders it
    to SVG via :func:`matofx.lineage.graph_to_svg`. The string here
    is the raw ``<svg>...</svg>`` body, ready to inline into the
    template's "Provenance" section."""


def build_report(context: ReportContext) -> str:
    """Render the per-job report as a self-contained HTML document.

    Returns a UTF-8 string ready to stream as ``text/html``. The
    output carries an inline ``<style>`` block, the optional branded
    logo as a base64 data URI, the metadata card, the descriptor-
    grouped Inputs and Outputs sections, and — when at least one
    ``csv_plot`` output is present — a single inline copy of
    ``plotly.min.js`` plus one ``Plotly.newPlot`` call per chart. No
    external references at all.
    """
    template = _environment().get_template("report.html.j2")
    output_groups = _rendered_output_groups(context)
    plotly_js = _plotly_bundle() if _any_plot_field(output_groups) else ""
    return template.render(
        job_id=context.job_id,
        tool_alias=context.tool_alias,
        tool_name=context.tool_name,
        tool_version=context.tool_version,
        submitted_by_username=context.submitted_by_username,
        submitted_at_label=_format_timestamp(context.submitted_at, timezone=context.timezone),
        completed_at_label=_format_timestamp(context.completed_at, timezone=context.timezone),
        report_generated_at_label=_format_timestamp(
            context.report_generated_at, timezone=context.timezone
        ),
        app_display_title=context.app_display_title,
        logo_data_uri=context.logo_data_uri,
        input_groups=_rendered_input_groups(context),
        output_groups=output_groups,
        workflow_context=context.workflow_context,
        workflow_siblings=_rendered_workflow_siblings(context),
        plotly_js=plotly_js,
        provenance_svg=context.provenance_svg,
        kpi_cards=context.kpi_cards,
        summary_paragraphs=list(context.summary.paragraphs) if context.summary else [],
        stylesheet=_STYLESHEET,
    )


def _any_plot_field(groups: list[RenderedGroup]) -> bool:
    """``True`` when at least one rendered group carries a ``csv_plot`` field."""
    return any(field.kind == "csv_plot" for group in groups for field in group.fields)


@lru_cache(maxsize=1)
def _plotly_bundle() -> str:
    """Return the bundled ``plotly.min.js`` body for inline embedding.

    Cached so the ~3.5 MB string is loaded once per process. Lives
    behind a function (rather than a module-level constant) so the
    import only fires the first time a report needs plots — reports
    with zero plots never pay the load cost.
    """
    from plotly.offline import get_plotlyjs  # noqa: PLC0415 — lazy on purpose.

    return str(get_plotlyjs())


@dataclass(frozen=True)
class SiblingJob:
    """One sibling job's projection for the workflow context card.

    Carried only as data: the builder turns
    ``(descriptor, job_inputs)`` into a list of :class:`RenderedGroup`s
    at render time so the per-sibling layout reuses the same code
    path as the main Inputs section.
    """

    job_id: int
    tool_name: str
    tool_alias: str
    is_self: bool
    """``True`` for the job the report is *about* — lets the template
    label that sibling so the reader can spot it inside the run."""

    descriptor: dict[str, Any] = field(default_factory=dict)
    """Captured ``tool_version.descriptor_snapshot`` at submit time."""

    job_inputs: dict[str, Any] = field(default_factory=dict)
    """The sibling's submitted ``job.inputs`` map."""


@dataclass(frozen=True)
class WorkflowContext:
    """Parent workflow + sibling jobs for the report's workflow card.

    Sibling *outputs* are deliberately not pulled in (ADR-0004 §5) —
    that would explode report size and duplicate the main Outputs
    block; the workflow card is upstream-input traceability only.
    """

    workflow_name: str
    run_name: str
    siblings: list[SiblingJob] = field(default_factory=list)


@dataclass(frozen=True)
class RenderedSibling:
    """Template-facing view of one sibling — already projected to groups."""

    job_id: int
    tool_name: str
    tool_alias: str
    is_self: bool
    input_groups: list[RenderedGroup] = field(default_factory=list)


@dataclass(frozen=True)
class RenderedField:
    """One descriptor field's slot in the report (template-facing).

    Held as a frozen dataclass so the template stays declarative —
    Jinja2 reads ``.label`` / ``.kind`` / ``.value`` / etc. directly.
    Built by :func:`_rendered_input_groups` / :func:`_rendered_output_groups`
    for each populated field.

    ``kind`` is one of:

    - ``"scalar"`` — input string / number / boolean / select, OR
      output number / string. ``value`` is a human-readable string;
      ``unit`` may be set.
    - ``"file"`` — input file. ``value`` is the bare filename.
      ``image_preview`` is the base64 data URI when one was inlined;
      ``None`` falls through to filename-only rendering.
    - ``"image_file"`` — output file recognised as an image. Same
      shape as ``"file"`` but the template knows the preview will be
      present (or the report falls back to the filename-only path).
    - ``"csv_table"`` — output file declared as ``file_type=csv_table``.
      ``value`` is the filename; ``rows`` / ``table_columns`` carry the
      first :data:`MAX_CSV_TABLE_ROWS` rows + column order; ``total_rows``
      drives the truncation note. Wide content — group is marked
      ``wide`` to span the full grid row.
    - ``"csv_plot"`` — output file declared as ``file_type=csv_plot``.
      ``value`` is the filename; ``figure_json`` is the pre-serialised
      Plotly figure (``{"data": ..., "layout": ...}``); ``div_id`` is
      the unique target div. Renders as an interactive chart;
      ``plotly.min.js`` is bundled once near the top of the document.
    - ``"file_other"`` — any file we can't render in this slice
      (PDF / HTML / video / 3D for now, plus unrecognised types).
      ``value`` is the filename; the template surfaces an "Available
      in MATOFX" note.
    - ``"table"`` — inline ``table`` input or output whose value is
      a dict / list of dicts. ``rows`` / ``table_columns`` are
      populated; no truncation.
    - ``"list"`` — multiselect or other sequence; ``value`` is a
      comma-joined string.
    """

    label: str
    kind: str
    value: str = ""
    unit: str = ""
    image_preview: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    table_columns: list[str] = field(default_factory=list)
    total_rows: int = 0
    truncated: bool = False
    figure_json: str = ""
    div_id: str = ""
    media_data_uri: str = ""
    media_mime: str = ""
    note: str = "(available in MATOFX)"


@dataclass(frozen=True)
class RenderedGroup:
    """One descriptor group's slot, with its populated fields.

    ``wide`` flips the group's CSS class to ``group wide`` so the
    template can opt out of the auto-fit grid (``grid-column: 1 /
    -1``) for content that needs the full row width — CSV tables,
    plots, videos, embedded HTML. Inputs never set this; outputs do
    when at least one field is wide-natured.
    """

    label: str
    fields: list[RenderedField]
    wide: bool = False


def _rendered_input_groups(context: ReportContext) -> list[RenderedGroup]:
    """Walk the report's descriptor + job inputs into rendered groups."""
    return _project_input_groups(
        descriptor=context.descriptor,
        job_inputs=context.job_inputs,
        image_previews=context.input_image_previews,
    )


def _project_input_groups(
    *,
    descriptor: dict[str, Any],
    job_inputs: dict[str, Any],
    image_previews: dict[str, str],
) -> list[RenderedGroup]:
    """Pure projection: ``(descriptor, job_inputs)`` → rendered groups.

    Used by both the main Inputs section and the workflow context's
    per-sibling Inputs blocks. Skips groups whose fields are all
    unpopulated; falls through to a single synthetic "Inputs" group
    when the descriptor declares no ``input_groups``.
    """
    fields_by_name: dict[str, dict[str, Any]] = {
        f["name"]: f
        for f in (descriptor.get("inputs") or [])
        if isinstance(f, dict) and f.get("name")
    }
    declared_groups = list(descriptor.get("input_groups") or [])

    if not declared_groups:
        flat: list[RenderedField] = [
            _render_field(spec, job_inputs.get(name), image_previews)
            for name, spec in fields_by_name.items()
            if _has_value(job_inputs.get(name))
        ]
        # Also surface ad-hoc entries that aren't in the descriptor —
        # rare, but better than silently dropping them.
        for name, value in job_inputs.items():
            if name in fields_by_name or not _has_value(value):
                continue
            flat.append(_render_field({"label": name, "type": "string"}, value, image_previews))
        if not flat:
            return []
        return [RenderedGroup(label="Inputs", fields=flat)]

    out: list[RenderedGroup] = []
    for group in declared_groups:
        group_name = group.get("name")
        field_names_in_group = [
            f["name"]
            for f in (descriptor.get("inputs") or [])
            if isinstance(f, dict) and f.get("group") == group_name and f.get("name")
        ]
        rendered: list[RenderedField] = []
        for name in field_names_in_group:
            value = job_inputs.get(name)
            if not _has_value(value):
                continue
            rendered.append(_render_field(fields_by_name[name], value, image_previews))
        if rendered:
            out.append(
                RenderedGroup(label=str(group.get("label") or group_name or ""), fields=rendered)
            )
    return out


def _rendered_workflow_siblings(context: ReportContext) -> list[RenderedSibling]:
    """Project each sibling into a template-ready :class:`RenderedSibling`.

    Returns an empty list when the report isn't part of a workflow
    run, so the template's ``{% if rendered_siblings %}`` block stays
    hidden for standalone jobs.
    """
    if context.workflow_context is None:
        return []
    return [
        RenderedSibling(
            job_id=sibling.job_id,
            tool_name=sibling.tool_name,
            tool_alias=sibling.tool_alias,
            is_self=sibling.is_self,
            # Siblings never carry image previews — we don't reach
            # into their working dirs (ADR-0004 §5: upstream-inputs
            # only, no sibling outputs).
            input_groups=_project_input_groups(
                descriptor=sibling.descriptor,
                job_inputs=sibling.job_inputs,
                image_previews={},
            ),
        )
        for sibling in context.workflow_context.siblings
    ]


def _has_value(value: Any) -> bool:
    """``True`` when a job-input value is worth surfacing in the report.

    ``None`` / empty string / empty list collapse to "not populated"
    so an optional field the user left blank doesn't clutter the
    Inputs grid.
    """
    if value is None or value == "":
        return False
    return not (isinstance(value, list | tuple | dict) and not value)


def _render_field(
    spec: dict[str, Any],
    value: Any,
    image_previews: dict[str, str],
) -> RenderedField:
    """Project one descriptor + value pair into a :class:`RenderedField`."""
    label = str(spec.get("label") or spec.get("name") or "?")
    unit = str(spec.get("unit") or "")
    field_type = str(spec.get("type") or "")

    if field_type == "file":
        filename = _filename_from_path(value)
        preview = image_previews.get(str(value)) if is_previewable_image(filename) else None
        return RenderedField(
            label=label,
            kind="file",
            value=filename,
            image_preview=preview,
        )

    if field_type == "table":
        rows = list(value) if isinstance(value, list) else []
        columns = [
            str(col.get("name") or "")
            for col in (spec.get("columns") or [])
            if isinstance(col, dict) and col.get("name")
        ]
        if not columns and rows and isinstance(rows[0], dict):
            columns = list(rows[0].keys())
        return RenderedField(
            label=label,
            kind="table",
            rows=[r for r in rows if isinstance(r, dict)],
            table_columns=columns,
        )

    if isinstance(value, list | tuple):
        return RenderedField(
            label=label,
            kind="list",
            value=", ".join(str(v) for v in value),
            unit=unit,
        )

    if isinstance(value, bool):
        return RenderedField(
            label=label,
            kind="scalar",
            value="Yes" if value else "No",
            unit=unit,
        )

    return RenderedField(label=label, kind="scalar", value=str(value), unit=unit)


def _filename_from_path(value: Any) -> str:
    """Reduce a stored file-input value to its leaf filename.

    The runner stores file inputs as absolute paths; the report
    surfaces only the operator-meaningful basename.
    """
    text = str(value or "")
    return text.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


# ----- Outputs ---------------------------------------------------------------


def _rendered_output_groups(context: ReportContext) -> list[RenderedGroup]:
    """Walk the descriptor + outputs document and emit Output groups.

    Mirrors the Inputs counterpart but dispatches on output ``type``
    / ``file_type`` to choose between number / string / image / csv_table
    / file-other renderings. Groups with at least one wide-natured
    field (CSV table) get ``wide=True`` so the template spans them
    across the auto-fit grid.
    """
    output_fields = [
        f
        for f in (context.descriptor.get("outputs") or [])
        if isinstance(f, dict) and f.get("name")
    ]
    if not output_fields:
        return []

    declared_groups = list(context.descriptor.get("output_groups") or [])
    value_lookup = _outputs_value_lookup(context.outputs_doc)

    def _build_group(group_label: str, specs: list[dict[str, Any]]) -> RenderedGroup | None:
        rendered: list[RenderedField] = []
        wide = False
        for spec in specs:
            field_render = _render_output_field(spec, value_lookup, context)
            if field_render is None:
                continue
            rendered.append(field_render)
            if field_render.kind in _WIDE_OUTPUT_KINDS:
                wide = True
        if not rendered:
            return None
        return RenderedGroup(label=group_label, fields=rendered, wide=wide)

    if not declared_groups:
        group = _build_group("Outputs", output_fields)
        return [group] if group is not None else []

    out: list[RenderedGroup] = []
    for group in declared_groups:
        group_name = group.get("name")
        specs = [f for f in output_fields if f.get("group") == group_name]
        group_label = str(group.get("label") or group_name or "")
        built = _build_group(group_label, specs)
        if built is not None:
            out.append(built)
    return out


_WIDE_OUTPUT_KINDS: frozenset[str] = frozenset(
    {"csv_table", "csv_plot", "video", "pdf", "html_embed"}
)
"""Output kinds whose group spans the full grid row."""


def _outputs_value_lookup(outputs_doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{output_name: entry}`` indexed view of ``outputs.json``.

    The :class:`OutputsDocument` shape stores the list under
    ``outputs``; we index by ``name`` so each descriptor-declared
    output can find its runtime value cheaply.
    """
    raw = outputs_doc.get("outputs") if outputs_doc else None
    if not isinstance(raw, list):
        return {}
    return {entry["name"]: entry for entry in raw if isinstance(entry, dict) and entry.get("name")}


def _render_output_field(
    spec: dict[str, Any],
    value_lookup: dict[str, dict[str, Any]],
    context: ReportContext,
) -> RenderedField | None:
    """Project one descriptor + runtime-value pair into a :class:`RenderedField`.

    Returns ``None`` to suppress an output entirely — currently
    reserved for the "no value at all" case so a tool that declared
    an optional output but didn't emit one doesn't render an empty
    row.
    """
    name = str(spec.get("name") or "")
    label = str(spec.get("label") or name or "?")
    type_ = str(spec.get("type") or "")
    entry = value_lookup.get(name, {})

    if type_ == "number":
        return _render_number_output(label, spec, entry)
    if type_ == "string":
        value = entry.get("value")
        if not _has_value(value):
            return None
        return RenderedField(label=label, kind="scalar", value=str(value))
    if type_ == "table":
        return _render_inline_table_output(label, spec, entry)
    if type_ == "file":
        return _render_file_output(label, spec, context)
    # Unknown type — show the label so the operator knows something
    # was declared but couldn't be rendered, rather than dropping it.
    return RenderedField(label=label, kind="file_other", value=f"(unsupported type {type_!r})")


def _render_number_output(
    label: str,
    spec: dict[str, Any],
    entry: dict[str, Any],
) -> RenderedField | None:
    """``label · value · unit`` — value rounded to the descriptor's precision.

    Returns ``None`` when the runtime didn't emit a value for this
    output (rather than rendering a bare em-dash row).
    """
    value = entry.get("value")
    if not _has_value(value):
        return None
    formatted = _format_number(value, spec.get("precision"))
    return RenderedField(
        label=label,
        kind="scalar",
        value=formatted,
        unit=str(spec.get("unit") or ""),
    )


def _format_number(value: Any, precision: Any) -> str:
    """Round ``value`` to the decimal count implied by ``precision``."""
    import math  # noqa: PLC0415 — narrow scope, no module-level dep

    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    try:
        p = float(precision) if precision is not None else None
    except (TypeError, ValueError):
        p = None
    if p is None or p <= 0:
        return f"{f:g}"
    decimals = max(0, -math.floor(math.log10(p)))
    return f"{f:.{decimals}f}"


def _render_inline_table_output(
    label: str,
    spec: dict[str, Any],
    entry: dict[str, Any],
) -> RenderedField | None:
    """A ``type=table`` output whose value is an inline list-of-dicts."""
    raw = entry.get("value")
    rows: list[dict[str, Any]] = []
    if isinstance(raw, list):
        rows = [r for r in raw if isinstance(r, dict)]
    columns = [
        str(col.get("name") or "")
        for col in (spec.get("columns") or [])
        if isinstance(col, dict) and col.get("name")
    ]
    if not columns and rows:
        columns = list(rows[0].keys())
    if not rows:
        return None
    return RenderedField(label=label, kind="table", rows=rows, table_columns=columns)


def _render_file_output(
    label: str,
    spec: dict[str, Any],
    context: ReportContext,
) -> RenderedField:
    """Pick the right viewer for a ``type=file`` output.

    Walks a small dispatch table so each per-kind projection lives in
    its own helper. The table runs in order; the first helper that
    returns a non-``None`` :class:`RenderedField` wins. The final
    ``_render_file_listed_fallback`` always returns a field, so the
    dispatch always resolves.
    """
    filename = str(spec.get("filename") or "").strip()
    file_type = str(spec.get("file_type") or "").strip().lower()
    display_name = _filename_from_path(filename) or "(unnamed file)"

    for handler in _FILE_OUTPUT_HANDLERS:
        result = handler(label, display_name, filename, file_type, context)
        if result is not None:
            return result
    return _render_file_listed_fallback(label, display_name, filename, file_type, context)


_PLOT_FILE_TYPES: frozenset[str] = frozenset({"csv_plot", "html_plot"})
"""Descriptor ``file_type`` values whose payload lands in
``output_csv_plots`` (both render via the bundled plotly.js)."""


def _handle_csv_plot_field(
    label: str,
    display_name: str,
    filename: str,
    file_type: str,
    context: ReportContext,
) -> RenderedField | None:
    if file_type not in _PLOT_FILE_TYPES:
        return None
    payload = context.output_csv_plots.get(filename)
    if payload is None:
        # Declared as a plot but the collector couldn't extract the
        # figure (file missing, not a plotly-emitted HTML, etc.) —
        # surface the filename so the operator knows the artefact
        # exists rather than dropping it silently.
        return RenderedField(label=label, kind="file_other", value=display_name)
    return RenderedField(
        label=label,
        kind="csv_plot",
        value=display_name,
        figure_json=str(payload.get("figure_json") or ""),
        div_id=str(payload.get("div_id") or ""),
    )


def _handle_csv_table_field(
    label: str,
    display_name: str,
    filename: str,
    file_type: str,
    context: ReportContext,
) -> RenderedField | None:
    if file_type != "csv_table":
        return None
    payload = context.output_csv_tables.get(filename)
    if payload is None:
        return RenderedField(label=label, kind="file_other", value=display_name)
    return RenderedField(
        label=label,
        kind="csv_table",
        value=display_name,
        rows=list(payload.get("rows") or []),
        table_columns=list(payload.get("columns") or []),
        total_rows=int(payload.get("total_rows") or 0),
        truncated=bool(payload.get("truncated") or False),
    )


def _handle_image_field(
    label: str,
    display_name: str,
    filename: str,
    file_type: str,
    context: ReportContext,
) -> RenderedField | None:
    del file_type
    if not is_previewable_image(filename):
        return None
    preview = context.output_image_previews.get(filename)
    if preview is None:
        return None
    return RenderedField(
        label=label,
        kind="image_file",
        value=display_name,
        image_preview=preview,
    )


def _handle_video_field(
    label: str,
    display_name: str,
    filename: str,
    file_type: str,
    context: ReportContext,
) -> RenderedField | None:
    del file_type
    if not is_video_file(filename):
        return None
    payload = context.output_videos.get(filename)
    if payload is None:
        return None
    return RenderedField(
        label=label,
        kind="video",
        value=display_name,
        media_data_uri=str(payload.get("data_uri") or ""),
        media_mime=str(payload.get("mime") or ""),
    )


def _handle_pdf_field(
    label: str,
    display_name: str,
    filename: str,
    file_type: str,
    context: ReportContext,
) -> RenderedField | None:
    del file_type
    if not is_pdf_file(filename):
        return None
    data_uri = context.output_pdfs.get(filename)
    if not data_uri:
        return None
    return RenderedField(
        label=label,
        kind="pdf",
        value=display_name,
        media_data_uri=data_uri,
    )


def _handle_html_field(
    label: str,
    display_name: str,
    filename: str,
    file_type: str,
    context: ReportContext,
) -> RenderedField | None:
    del file_type
    if not is_html_file(filename):
        return None
    data_uri = context.output_html_embeds.get(filename)
    if not data_uri:
        return None
    return RenderedField(
        label=label,
        kind="html_embed",
        value=display_name,
        media_data_uri=data_uri,
    )


def _render_file_listed_fallback(
    label: str,
    display_name: str,
    filename: str,
    file_type: str,
    context: ReportContext,
) -> RenderedField:
    """Filename-only fallback for 3D, oversized video, or unrecognised files."""
    del file_type
    note = context.output_file_notes.get(filename)
    if note is None and is_3d_animation_file(filename):
        # Defensive: the collector normally pre-populates this, but
        # keep the message correct even if it ever drops the entry.
        note = "3D animation — not previewable in report"
    return RenderedField(
        label=label,
        kind="file_other",
        value=display_name,
        note=f"({note})" if note else "(available in MATOFX)",
    )


_FILE_OUTPUT_HANDLERS: tuple[
    Callable[[str, str, str, str, ReportContext], RenderedField | None],
    ...,
] = (
    _handle_csv_plot_field,
    _handle_csv_table_field,
    _handle_image_field,
    _handle_video_field,
    _handle_pdf_field,
    _handle_html_field,
)


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """Build the Jinja2 environment once per process.

    Cached to avoid re-parsing the template on every request. Loader
    points at ``matofx.reports`` package's ``templates`` directory so
    the file ships inside the wheel.
    """
    return Environment(
        loader=PackageLoader("matofx.reports", "templates"),
        autoescape=select_autoescape(enabled_extensions=("html", "j2")),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _format_timestamp(value: _dt.datetime | None, *, timezone: str) -> str:
    """Render a timestamp for the metadata card.

    Honors the caller's IANA ``timezone`` (e.g. ``Europe/London``)
    when valid; falls back to UTC for ``"auto"`` / ``"UTC"`` /
    unknown zones. Format is locale-neutral
    (``19 May 2026, 09:00 BST``) — enough for the operational record
    the report carries without pulling in :mod:`babel`. None values
    render as an em-dash so the table cell still has structure.
    """
    if value is None:
        return "—"
    tz = _resolve_zone(timezone)
    return value.astimezone(tz).strftime("%d %b %Y, %H:%M %Z")


def _resolve_zone(name: str) -> _dt.tzinfo:
    if name in ("", "auto", "UTC"):
        return _dt.UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return _dt.UTC


_STYLESHEET = (
    ":root { --report-bg: #ffffff; --report-fg: #1f2937; "
    "--report-muted: #6b7280; --report-card-bg: #f9fafb; "
    "--report-card-border: #e5e7eb; --report-accent: #0f766e; }"
    "* { box-sizing: border-box; }"
    "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', "
    "Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 2rem; "
    "background: var(--report-bg); color: var(--report-fg); "
    "line-height: 1.5; }"
    "header.report-header { display: flex; align-items: center; gap: 1.25rem; "
    "padding-bottom: 1.25rem; border-bottom: 2px solid var(--report-accent); "
    "margin-bottom: 1.5rem; }"
    "header.report-header .report-logo { height: 3rem; width: auto; }"
    "header.report-header h1 { font-size: 1.6rem; margin: 0; }"
    "header.report-header .report-subtitle { margin: 0.25rem 0 0 0; "
    "color: var(--report-muted); font-size: 0.95rem; }"
    ".card { background: var(--report-card-bg); "
    "border: 1px solid var(--report-card-border); border-radius: 0.5rem; "
    "padding: 1rem 1.25rem; margin-bottom: 1rem; }"
    ".card-heading { margin: 0 0 0.75rem 0; font-size: 1.05rem; "
    "color: var(--report-accent); }"
    ".metadata dl { display: grid; grid-template-columns: max-content 1fr; "
    "column-gap: 1.5rem; row-gap: 0.4rem; margin: 0; }"
    ".metadata dt { font-weight: 600; color: var(--report-muted); }"
    ".metadata dd { margin: 0; }"
    ".summary-paragraph { margin: 0 0 0.6rem 0; line-height: 1.5; "
    "max-width: 62ch; }"
    ".summary-paragraph:last-child { margin-bottom: 0; }"
    ".kpi-card-grid { display: grid; "
    "grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); "
    "gap: 0.75rem; }"
    ".kpi-card { background: #ffffff; border: 1px solid "
    "var(--report-card-border); border-radius: 0.4rem; padding: 0.75rem; }"
    ".kpi-card-label { color: var(--report-muted); font-size: 0.8rem; "
    "font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }"
    ".kpi-card-value { color: var(--report-accent); font-size: 1.5rem; "
    "font-weight: 600; margin-top: 0.25rem; "
    "font-variant-numeric: tabular-nums; }"
    ".kpi-card-unit { color: var(--report-muted); font-size: 0.75rem; "
    "margin-top: 0.1rem; }"
    ".muted { color: var(--report-muted); }"
    "section.section-heading { margin: 1.5rem 0 0.75rem 0; font-size: 1.2rem; "
    "font-weight: 600; color: var(--report-fg); }"
    ".groups-grid { display: grid; "
    "grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); "
    "grid-auto-flow: dense; gap: 1rem; }"
    ".group { background: var(--report-card-bg); "
    "border: 1px solid var(--report-card-border); border-radius: 0.5rem; "
    "padding: 1rem 1.25rem; }"
    ".group.wide { grid-column: 1 / -1; }"
    ".group .truncation-note { margin-top: 0.5rem; font-size: 0.85rem; "
    "color: var(--report-muted); }"
    ".group .available-note { font-size: 0.8rem; color: var(--report-muted); "
    "margin-left: 0.5rem; }"
    ".workflow-context p.workflow-line { margin: 0 0 0.75rem 0; }"
    ".workflow-context p.workflow-line strong { color: var(--report-fg); }"
    ".workflow-context details { border-top: 1px solid var(--report-card-border); "
    "padding: 0.6rem 0; }"
    ".workflow-context details[open] summary { margin-bottom: 0.5rem; }"
    ".workflow-context details summary { cursor: pointer; font-weight: 600; "
    "color: var(--report-fg); }"
    ".workflow-context details summary .muted-tool-id { color: var(--report-muted); "
    "font-weight: 400; margin-left: 0.5rem; font-size: 0.85rem; }"
    ".workflow-context details summary .self-badge { background: var(--report-accent); "
    "color: white; border-radius: 999px; padding: 0.05rem 0.5rem; margin-left: 0.5rem; "
    "font-size: 0.7rem; font-weight: 600; letter-spacing: 0.04em; "
    "text-transform: uppercase; }"
    ".workflow-context .sibling-empty { color: var(--report-muted); font-size: 0.85rem; "
    "padding: 0.25rem 0 0.5rem 0; }"
    ".plotly-chart { width: 100%; height: 420px; margin-top: 0.5rem; }"
    ".video-embed video { width: 100%; max-height: 480px; margin-top: 0.5rem; "
    "border-radius: 0.25rem; }"
    ".pdf-embed { width: 100%; height: 720px; margin-top: 0.5rem; "
    "border: 1px solid var(--report-card-border); border-radius: 0.25rem; }"
    ".html-embed { width: 100%; height: 480px; margin-top: 0.5rem; "
    "border: 1px solid var(--report-card-border); border-radius: 0.25rem; "
    "background: white; }"
    ".group .group-heading { margin: 0 0 0.75rem 0; font-size: 1rem; "
    "color: var(--report-accent); }"
    ".group dl.fields { display: grid; "
    "grid-template-columns: max-content 1fr; column-gap: 1rem; row-gap: 0.4rem; "
    "margin: 0; }"
    ".group dl.fields dt { font-weight: 600; color: var(--report-muted); }"
    ".group dl.fields dd { margin: 0; word-break: break-word; }"
    ".group .unit { color: var(--report-muted); margin-left: 0.25rem; }"
    ".group .filename { font-family: ui-monospace, SFMono-Regular, Menlo, "
    "Consolas, monospace; font-size: 0.9rem; }"
    ".group .image-preview { margin-top: 0.5rem; }"
    ".group .image-preview img { max-width: 100%; max-height: 240px; "
    "border-radius: 0.25rem; border: 1px solid var(--report-card-border); }"
    ".group .not-previewable { font-size: 0.8rem; color: var(--report-muted); "
    "margin-left: 0.5rem; }"
    ".value-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; "
    "margin-top: 0.25rem; }"
    ".value-table th, .value-table td { padding: 0.3rem 0.5rem; "
    "border-bottom: 1px solid var(--report-card-border); text-align: left; }"
    ".value-table th { background: rgba(15, 118, 110, 0.05); "
    "color: var(--report-muted); font-weight: 600; }"
    "@media print { body { padding: 0.5cm; } "
    ".card, .group { break-inside: avoid; box-shadow: none; } }"
)

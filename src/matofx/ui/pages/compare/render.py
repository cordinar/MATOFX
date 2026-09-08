"""Per-job-cell renderer for the Compare grid.

Each selected job becomes one column of the grid. The renderer walks
the descriptor's ``input_groups`` and ``output_groups`` to lay out the
cell in the same shape the job page uses, so a user familiar with
``/jobs/{id}`` recognises the layout immediately. Keys whose values
differ across the selected jobs render bold + accent-red so the
"what changed" question answers itself at a glance.

File-typed inputs *and* outputs render inline previews when their
bytes are pre-fetched by the page layer (locked 2026-05-14 — the
locked feedback was "everywhere I upload or generate a previewable
file I want to see it"). The cell renderer dispatches by descriptor
``file_type`` first (``html_plot`` / ``csv_table``) and falls back to
the value's filename extension for image / STL detection. CSV ``plot``
outputs ride the separate overlay-charts block below the grid; the
per-cell view doesn't duplicate that chart.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from nicegui import ui

from matofx.ui.pages._chrome import section_card

DownloadCallback = Callable[[str], Awaitable[None]]

#: Icon for each cell section's chip header, keyed by section title.
_SECTION_ICONS = {"Inputs": "input", "Outputs": "output"}
"""Per-cell hook called with a filename when the user clicks the
download icon next to a file output. The page layer wires this to
:meth:`ApiClient.download_job_output` + :func:`nicegui.ui.download.content`
so the renderer doesn't need to know about the API client."""


@dataclass(frozen=True)
class JobCellModel:
    """The data the cell renderer needs about one selected job.

    Built by the page from the descriptor + ``JobResponse`` + the
    parsed output document. Kept explicit (rather than a dict) so the
    renderer can't silently misread a missing field.
    """

    job_id: int
    job_label: str
    """E.g. ``"Job 0001234 · run name"``."""

    state: str
    """The job's terminal state (``complete`` / ``failed``)."""

    descriptor: dict[str, Any]
    """The tool's full descriptor — used for input/output group layout."""

    project_name: str = ""
    """Project the job's scenario belongs to — shown in the column header
    so a (possibly cross-project) comparison stays unambiguous."""

    scenario_name: str = ""
    """Scenario the job belongs to — shown in the column header."""

    tool_name: str = ""
    """Human tool name for the header (the descriptor's ``name``)."""

    tool_version: str = ""
    """The tool version this job actually ran (the pinned snapshot) — may
    differ from the tool's current released version."""

    vv_badge_kind: str = ""
    """V&V inheritance-aware badge label for the job's pinned tool version
    (ADR-0011 §8). One of ``verified_validated`` / ``verified_only`` /
    ``pending`` / ``none`` / ``retired``, or empty string when V&V is
    feature-off or the badge fetch failed. The header renders a coloured
    chip when this is non-empty."""

    completed_display: str = ""
    """Pre-formatted completion timestamp (via
    :func:`matofx.ui.format.format_user_datetime`); empty when the job
    hasn't completed."""

    inputs: dict[str, Any] = field(default_factory=dict)
    """The user's submitted form values, keyed by input ``name``."""

    output_values: dict[str, Any] = field(default_factory=dict)
    """Flattened output values: scalar value (or filename for file outputs)."""

    output_filenames: dict[str, str] = field(default_factory=dict)
    """For file-typed outputs: the filename under ``outputs/``."""

    output_preview_blobs: dict[str, bytes] = field(default_factory=dict)
    """Pre-fetched bytes for every previewable output, keyed by
    descriptor output ``name``. The page layer fills this in one
    pass for every ``file``-typed output declared with a recognised
    ``file_type`` (``html_plot`` / ``csv_table`` / ``image`` /
    ``cad_file``) — the cell renderer dispatches inline based on
    that ``file_type`` so the user sees the actual figure / table /
    image / 3-D scene in each column instead of a bare filename.
    Empty entry means the file isn't pre-loaded and the row falls
    back to the legacy filename + download icon."""

    field_frames: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Parsed ``field-frames`` payload per ``file_type='field'`` output
    name (ADR-0008): the shared mesh + per-timestep node values. The page
    fetches these from the field-frames endpoint (they're not a plain
    blob); the cell renderer draws the *same* self-contained Plotly
    animation the Job page shows, inline in the Outputs card — with a
    "Download animation (HTML)" button instead of a bare ``.vtp`` link."""

    input_preview_blobs: dict[str, bytes] = field(default_factory=dict)
    """Pre-fetched bytes for previewable ``file``-typed *inputs*, keyed
    by descriptor input ``name``. ``InputField`` has no ``file_type``
    (that's an output-side hint), so the page layer enumerates inputs
    whose value path ends in a recognised image / STL extension and
    fetches each via the authenticated file-preview endpoint. The
    cell renderer dispatches by extension and embeds an image / STL
    scene under the row, mirroring how the upload widget and the My
    Jobs page render the same files."""


def render_job_cell(
    model: JobCellModel,
    differing_keys: set[str],
    *,
    only_differences: bool = False,
    download_callback: DownloadCallback | None = None,
) -> None:
    """Render one job as a vertical cell in the Compare grid.

    ``differing_keys`` are the input + output names whose values aren't
    all equal across the selected jobs (computed by
    :func:`compare.diff.compute_differing_keys`). Rows whose key is in
    the set render bold + accent-red.

    ``only_differences=True`` collapses the cell to just the differing
    rows — wired to the "Show only differences" toggle on the Compare
    tab. Group headings whose members are all hidden render as a quiet
    "All values match" line so the cell doesn't go visually empty.

    ``download_callback`` (when supplied) renders a small download
    icon next to every file output's filename; clicking it invokes
    the callback with the filename so the page can stream the bytes
    to the browser. ``None`` keeps file rows display-only — the cell
    renderer stays usable in tests / static contexts that have no
    API client.
    """
    with ui.column().classes("w-full gap-3"):
        render_job_header(model)
        render_job_inputs_section(
            model,
            differing_keys=differing_keys,
            only_differences=only_differences,
        )
        render_job_outputs_section(
            model,
            differing_keys=differing_keys,
            only_differences=only_differences,
            download_callback=download_callback,
        )


def render_job_header(model: JobCellModel) -> None:
    """Render the per-job header: label, tool+version, project/scenario, state, completion.

    Split out so the Compare grid can place each job's header in its
    own CSS-grid row alongside the Inputs / Outputs cards below it —
    keeping every header at the same vertical position regardless of
    how tall the neighbouring cells grow. Beyond the job label + state,
    the header surfaces the context a reviewer needs to tell columns
    apart: the project / scenario the job lives in, the exact tool
    version it ran (which can differ across columns), and when it
    finished.
    """
    _meta = "color: var(--matofx-muted-text); font-size: 0.8rem;"
    ui.label(model.job_label).style("font-weight: 600;")
    if model.tool_name or model.tool_version:
        tool_line = model.tool_name
        if model.tool_version:
            tool_line = f"{tool_line} v{model.tool_version}".strip()
        ui.label(tool_line).style(_meta)
    if model.vv_badge_kind:
        _render_vv_badge_chip(model.vv_badge_kind)
    if model.project_name or model.scenario_name:
        context = " / ".join(part for part in (model.project_name, model.scenario_name) if part)
        ui.label(context).style(_meta)
    ui.label(_state_label(model.state)).style(
        "color: var(--matofx-muted-text); font-size: 0.85rem;"
    )
    if model.completed_display:
        ui.label(f"Completed {model.completed_display}").style(_meta)
    # Lineage link-out (ADR-0010 slice 5). Compare has no per-column
    # tab room for an inline provenance graph; we link out to the
    # admin standalone page in a new browser tab so the operator
    # keeps the comparison context.
    ui.button(
        "↗ Provenance",
        on_click=lambda jid=model.job_id: ui.navigate.to(f"/admin/lineage/job/{jid}", new_tab=True),
    ).props("flat dense").style("font-size: 0.75rem; padding: 0 0.4rem;")


_VV_BADGE_LABEL: dict[str, str] = {
    "verified_validated": "V&V",
    "verified_only": "V",
    "pending": "V&V pending",
    "none": "No V&V",
    "retired": "V&V retired",
}
_VV_BADGE_COLOR: dict[str, str] = {
    "verified_validated": "positive",
    "verified_only": "info",
    "pending": "warning",
    "none": "grey",
    "retired": "grey",
}


def _render_vv_badge_chip(kind: str) -> None:
    """Compact V&V badge under the tool-version line (ADR-0011 §8)."""
    label = _VV_BADGE_LABEL.get(kind, kind.title())
    color = _VV_BADGE_COLOR.get(kind, "grey")
    ui.chip(label, icon="verified").props(f"color={color} text-color=white dense size=sm")


def render_job_inputs_section(
    model: JobCellModel,
    *,
    differing_keys: set[str],
    only_differences: bool = False,
) -> None:
    """Render just the Inputs card for one job (no header, no Outputs)."""
    _render_section(
        "Inputs",
        descriptor_groups=model.descriptor.get("input_groups") or [],
        descriptor_fields=model.descriptor.get("inputs") or [],
        values=model.inputs,
        filenames={},
        preview_blobs=model.input_preview_blobs,
        section_kind="input",
        differing_keys=differing_keys,
        only_differences=only_differences,
        download_callback=None,
    )


def render_job_outputs_section(
    model: JobCellModel,
    *,
    differing_keys: set[str],
    only_differences: bool = False,
    download_callback: DownloadCallback | None = None,
) -> None:
    """Render just the Outputs card for one job (no header, no Inputs)."""
    _render_section(
        "Outputs",
        descriptor_groups=model.descriptor.get("output_groups") or [],
        descriptor_fields=model.descriptor.get("outputs") or [],
        values=model.output_values,
        filenames=model.output_filenames,
        preview_blobs=model.output_preview_blobs,
        field_frames=model.field_frames,
        section_kind="output",
        differing_keys=differing_keys,
        only_differences=only_differences,
        download_callback=download_callback,
    )


def _state_label(state: str) -> str:
    """User-facing wording for the job state — plain English, no jargon."""
    if state == "complete":
        return "Finished successfully"
    if state == "failed":
        return "Finished with errors"
    return state.title()


def _render_section(
    title: str,
    *,
    descriptor_groups: list[dict[str, Any]],
    descriptor_fields: list[dict[str, Any]],
    values: dict[str, Any],
    filenames: dict[str, str],
    preview_blobs: dict[str, bytes],
    section_kind: str,
    differing_keys: set[str],
    only_differences: bool,
    download_callback: DownloadCallback | None,
    field_frames: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Render the Inputs or Outputs section of one cell.

    ``preview_blobs`` is the unified ``{field-or-output name: bytes}``
    dict for this section — populated up-front by the Compare page so
    the row renderer can embed an image / table / plotly figure / STL
    scene without a per-row network call. ``section_kind`` is
    ``"input"`` or ``"output"`` and switches the row renderer's
    dispatch order (output rows prefer the descriptor's ``file_type``;
    input rows have no ``file_type`` and fall straight to the value's
    filename extension).
    """
    with section_card(_SECTION_ICONS.get(title, "list"), title):
        if not descriptor_fields:
            ui.label("(none declared)").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem;"
            )
            return

        frames = field_frames or {}
        fields_by_name: dict[str, dict[str, Any]] = {
            f["name"]: f for f in descriptor_fields if f.get("name")
        }
        groups = list(descriptor_groups)
        if not groups:
            _render_flat(
                fields_by_name=fields_by_name,
                ordered_names=[f["name"] for f in descriptor_fields if f.get("name")],
                values=values,
                filenames=filenames,
                preview_blobs=preview_blobs,
                field_frames=frames,
                section_kind=section_kind,
                differing_keys=differing_keys,
                only_differences=only_differences,
                download_callback=download_callback,
            )
            return

        for group in groups:
            group_name = group.get("name")
            group_label = group.get("label") or group_name or ""
            field_names = [
                f["name"]
                for f in descriptor_fields
                if f.get("name") and f.get("group") == group_name
            ]
            visible_names = [n for n in field_names if not only_differences or n in differing_keys]
            with ui.column().classes("w-full gap-1 q-mt-sm"):
                ui.label(group_label).style(
                    "font-size: 0.85rem; font-weight: 600; color: var(--matofx-muted-text);"
                )
                if not visible_names:
                    ui.label("All values match").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
                    continue
                for name in visible_names:
                    _render_row(
                        spec=fields_by_name.get(name, {"label": name}),
                        value=values.get(name),
                        filename=filenames.get(name),
                        preview_blob=preview_blobs.get(name),
                        field_data=frames.get(name),
                        section_kind=section_kind,
                        is_differing=name in differing_keys,
                        download_callback=download_callback,
                    )


def _render_flat(
    *,
    fields_by_name: dict[str, dict[str, Any]],
    ordered_names: Iterable[str],
    values: dict[str, Any],
    filenames: dict[str, str],
    preview_blobs: dict[str, bytes],
    field_frames: dict[str, dict[str, Any]],
    section_kind: str,
    differing_keys: set[str],
    only_differences: bool,
    download_callback: DownloadCallback | None,
) -> None:
    """Render rows without group headings — fallback for descriptors without groups."""
    any_row = False
    for name in ordered_names:
        if only_differences and name not in differing_keys:
            continue
        _render_row(
            spec=fields_by_name.get(name, {"label": name}),
            value=values.get(name),
            filename=filenames.get(name),
            preview_blob=preview_blobs.get(name),
            field_data=field_frames.get(name),
            section_kind=section_kind,
            is_differing=name in differing_keys,
            download_callback=download_callback,
        )
        any_row = True
    if not any_row:
        ui.label("All values match").style("color: var(--matofx-muted-text); font-size: 0.8rem;")


_IMAGE_PREVIEW_EXTENSIONS: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"}
)
_CAD_PREVIEW_EXTENSIONS: frozenset[str] = frozenset({"stl", "stp", "step", "iges", "igs", "obj"})
_PDF_PREVIEW_EXTENSIONS: frozenset[str] = frozenset({"pdf"})
_ANIM3D_PREVIEW_EXTENSIONS: frozenset[str] = frozenset({"gltf", "glb"})
_VIDEO_PREVIEW_EXTENSIONS: frozenset[str] = frozenset({"mp4", "webm", "ogg", "ogv", "mov", "m4v"})
_IMAGE_PREVIEW_MIME: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}


def _extension_of(path_or_filename: str) -> str:
    """Lowercase trailing extension (without dot), or empty string."""
    if not path_or_filename or "." not in path_or_filename:
        return ""
    return path_or_filename.rsplit(".", 1)[-1].lower()


def _render_row(
    *,
    spec: dict[str, Any],
    value: Any,
    filename: str | None,
    preview_blob: bytes | None,
    section_kind: str,
    is_differing: bool,
    download_callback: DownloadCallback | None,
    field_data: dict[str, Any] | None = None,
) -> None:
    """One row: ``label: value`` (with ``unit`` for non-file types).

    File rows additionally get an inline preview beneath the row when
    ``preview_blob`` was supplied by the page layer:

    - ``file_type == "html_plot"`` (output only) — native plotly figure
      extracted from the page, or ``<iframe srcdoc>`` fallback.
    - ``file_type == "csv_table"`` (output only) — inline ``ui.table``.
    - ``file_type == "field"`` (output only) — inline self-contained
      Plotly animation built from ``field_data`` (the field-frames
      payload), with a "Download animation (HTML)" button. No bare
      ``.vtp`` row — the animation *is* the output, same as the Job page.
    - extension ``.stl`` (input or output) — inline ``ui.scene`` 3-D
      viewer; same renderer the My Jobs file viewer uses.
    - image extensions (``.png`` / ``.jpg`` / ``.jpeg`` / ``.gif`` /
      ``.bmp`` / ``.webp`` / ``.svg``, input or output) — inline image
      via a base64 data URL with ``fit=contain`` so the whole image
      shows (the upload widget had the same crop-vs-contain trap).

    File outputs also get a download-icon button when
    ``download_callback`` is supplied.
    """
    label = str(spec.get("label") or spec.get("name") or "?")
    unit = str(spec.get("unit") or "")
    field_type = str(spec.get("type") or "")
    file_type = str(spec.get("file_type") or "")
    # Field outputs are an animation, not a "label: filename" row — render
    # the viewer in place of the value/download so the Outputs card shows
    # the same thing the Job page does.
    if field_type == "file" and file_type == "field" and section_kind == "output":
        _render_field_row(
            label,
            is_differing=is_differing,
            field_data=field_data,
            value=value,
            filename=filename,
            download_callback=download_callback,
        )
        return
    # Material inputs carry a frozen block (ADR-0007) — a dict (or list of
    # dicts for a multi-component formulation), not a scalar. Render it as a
    # compact card with the properties laid out, instead of dumping the raw
    # ``{'material_id': …}`` dict.
    if field_type == "material" and isinstance(value, dict | list):
        _render_material_row(label, value, is_differing=is_differing)
        return
    if field_type == "file":
        display = filename or _format_value(value, field_type) or "—"
    else:
        display = _format_value(value, field_type) or "—"
        if unit:
            display = f"{display} {unit}"

    style = "font-size: 0.9rem;"
    if is_differing:
        # Bold + accent-red so the eye picks out what changed. The
        # ``--matofx-error-text`` token reads as red on both themes;
        # if the design ever wants a separate "diff highlight" colour
        # we can promote it to its own token without touching the
        # call sites here.
        style += "font-weight: 600; color: var(--matofx-error-text);"
    with ui.column().classes("w-full gap-1"):
        with ui.row().classes("w-full items-baseline gap-2"):
            ui.label(label).style(
                "font-size: 0.85rem; min-width: 9rem; color: var(--matofx-label-text);"
            )
            ui.label(display).style(style)
            if field_type == "file" and download_callback is not None and filename:
                ui.button(
                    icon="download",
                    on_click=lambda _e=None, _cb=download_callback, _name=filename: _cb(_name),
                ).props("flat dense round size=sm color=primary").tooltip(f"Download {filename}")
        if field_type == "file" and preview_blob is not None:
            _dispatch_inline_preview(
                preview_blob,
                spec=spec,
                value=value,
                filename=filename,
                file_type=file_type,
                section_kind=section_kind,
                fallback_label=filename or label,
            )


def _render_material_row(label: str, value: Any, *, is_differing: bool) -> None:
    """Pretty-print a frozen material block (ADR-0007) instead of the raw dict.

    Mirrors the job page's material card in a compact, column-friendly
    form: a summary line (name + version + class) over the frozen
    properties as ``Property: value unit`` rows. ``value`` is a single
    block dict; a list (multi-component formulation) renders one card per
    component. Differing values tint to the accent-red diff colour.
    """
    label_style = "font-size: 0.85rem; color: var(--matofx-label-text);"
    if is_differing:
        label_style += " font-weight: 600; color: var(--matofx-error-text);"
    blocks = [b for b in (value if isinstance(value, list) else [value]) if isinstance(b, dict)]
    with ui.column().classes("w-full gap-1"):
        ui.label(label).style(label_style)
        if not blocks:
            ui.label("—").style("font-size: 0.9rem;")
            return
        for block in blocks:
            _render_material_card(block, is_differing=is_differing)


def _render_material_card(block: dict[str, Any], *, is_differing: bool) -> None:
    """One bordered material block: summary header + a property list."""
    name = str(block.get("name") or "(unnamed)")
    class_name = str(block.get("class") or "")
    ordinal = block.get("version_ordinal")
    header = f"{name} v{ordinal}" if ordinal else name
    header_colour = "var(--matofx-error-text)" if is_differing else "var(--matofx-body-text)"
    props_map = block.get("properties") or {}
    with ui.column().classes("w-full gap-0").style(
        "border: 1px solid var(--matofx-card-border); border-radius: 6px; padding: 0.4rem 0.55rem;"
    ):
        with ui.row().classes("items-baseline gap-2 no-wrap"):
            ui.label(header).style(f"font-size: 0.85rem; font-weight: 600; color: {header_colour};")
            if class_name:
                ui.label(f"class: {class_name}").style(
                    "font-size: 0.75rem; color: var(--matofx-muted-text);"
                )
        if not props_map:
            ui.label("(no properties on this version)").style(
                "font-size: 0.78rem; color: var(--matofx-muted-text);"
            )
            return
        for slug in sorted(props_map):
            prop = props_map[slug] or {}
            prop_name = str(prop.get("prop_name") or slug)
            val_text = _format_value(prop.get("value"), "")
            prop_unit = str(prop.get("unit") or "")
            if prop_unit:
                val_text = f"{val_text} {prop_unit}"
            with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
                ui.label(prop_name).style(
                    "font-size: 0.78rem; color: var(--matofx-label-text); min-width: 8rem;"
                )
                ui.label(val_text).style("font-size: 0.78rem; color: var(--matofx-body-text);")


def _render_field_row(
    label: str,
    *,
    is_differing: bool,
    field_data: dict[str, Any] | None,
    value: Any,
    filename: str | None,
    download_callback: DownloadCallback | None,
) -> None:
    """A ``file_type='field'`` output: label + inline animation, no ``.vtp`` link.

    Draws the same self-contained Plotly animation the Job page shows
    (built from the parsed ``field_data``). When the field frames couldn't
    be fetched, falls back to a muted note plus the raw-file download icon
    (if available) so the artefact is still reachable.
    """
    label_style = "font-size: 0.85rem; color: var(--matofx-label-text);"
    if is_differing:
        label_style += " font-weight: 600; color: var(--matofx-error-text);"
    with ui.column().classes("w-full gap-1"):
        ui.label(label).style(label_style)
        if field_data is not None:
            _render_field_animation_inline(
                field_data, label=label, filename=filename or str(value or "") or "field"
            )
            return
        ui.label("(animation unavailable)").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )
        if download_callback is not None and filename:
            ui.button(
                icon="download",
                on_click=lambda _e=None, _cb=download_callback, _name=filename: _cb(_name),
            ).props("flat dense round size=sm color=primary").tooltip(f"Download {filename}")


def _render_field_animation_inline(
    field_data: dict[str, Any], *, label: str, filename: str
) -> None:
    """Inline self-contained Plotly field animation + HTML download.

    Local imports keep plotly / the field builders off the renderer's
    import path for unit tests that don't exercise the field viewer
    (mirrors the html_plot / STL branches). A transient series (more than
    one frame) carries play/pause + a step slider in the figure itself and
    a "Download animation (HTML)" button matching the Job page; a
    single-frame field renders the static surface / line.
    """
    from matofx.ui._field_surrogate_viewer import (  # noqa: PLC0415
        _field_animation_figure,
        _field_animation_filename,
        _field_frame_figure,
    )
    from matofx.ui.theme import export_plotly_html, themed_plotly  # noqa: PLC0415

    frames: list[list[float]] = [list(f) for f in (field_data.get("frames") or [])]
    if not frames:
        ui.label("No field data to display.").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )
        return

    # Fixed colour / value range across the whole series so frames stay
    # comparable as the animation plays.
    all_values = [v for fr in frames for v in fr]
    c_range = (min(all_values), max(all_values)) if all_values else None

    if len(frames) > 1:
        fig = _field_animation_figure(field_data, frames, label, c_range)
        ui.plotly(themed_plotly(fig)).classes("w-full")
        anim_name = _field_animation_filename(filename)
        ui.button(
            "Download animation (HTML)",
            icon="movie",
            on_click=lambda _e=None, _fig=fig, _name=anim_name: ui.download.content(
                export_plotly_html(_fig), _name
            ),
        ).props("flat dense color=primary size=sm").classes("self-end")
    else:
        fig = _field_frame_figure(field_data, frames[0], label, c_range)
        ui.plotly(themed_plotly(fig)).classes("w-full")


def _dispatch_inline_preview(
    blob: bytes,
    *,
    spec: dict[str, Any],
    value: Any,
    filename: str | None,
    file_type: str,
    section_kind: str,
    fallback_label: str,
) -> None:
    """Pick the right inline renderer for a previewable file row.

    Output rows go through ``file_type`` first (the descriptor's
    explicit hint — ``html_plot`` / ``csv_table`` / ``image`` /
    ``cad_file``); both input and output rows fall back to the
    filename's extension so an input that wasn't given a
    ``file_type`` still shows its preview. Anything we don't know
    how to render silently no-ops — the row's filename + download
    icon is the user's fallback.
    """
    # Output dispatch by explicit descriptor hint first.
    if section_kind == "output":
        if file_type == "html_plot":
            _render_html_plot_inline(blob, fallback_label=fallback_label)
            return
        if file_type == "csv_table":
            _render_csv_table_inline(
                blob,
                declared_columns=spec.get("columns"),
                fallback_label=fallback_label,
            )
            return
        if file_type == "pdf":
            _render_pdf_inline(blob)
            return
        if file_type == "animation_3d":
            source_ext = _extension_of(str(value or "")) or _extension_of(str(filename or ""))
            _render_anim3d_inline(blob, extension=source_ext or "glb")
            return
        if file_type == "video":
            source_ext = _extension_of(str(value or "")) or _extension_of(str(filename or ""))
            _render_video_inline(blob, extension=source_ext or "mp4")
            return

    # Extension-based dispatch — works for inputs (no ``file_type``)
    # and for outputs whose hint we don't recognise.
    source = str(value or "") or str(filename or "")
    ext = _extension_of(source)
    if not ext and filename:
        ext = _extension_of(filename)
    if ext in _CAD_PREVIEW_EXTENSIONS:
        # ``blob`` carries STL bytes regardless of the source
        # extension — the page-level loader routes STP / STEP / IGES /
        # OBJ through the server-side cad-preview endpoint, which
        # tessellates to STL. STL files pass through unchanged. The
        # one ``ui.scene.stl`` viewer covers all five formats.
        #
        # Local import: ``ui.scene`` pulls in three.js machinery we
        # don't want loaded at module-import time for tests that
        # don't exercise the CAD path.
        from matofx.ui.cad_preview import render_stl_preview  # noqa: PLC0415

        render_stl_preview(blob, height=280)
        return
    if ext in _IMAGE_PREVIEW_EXTENSIONS:
        _render_image_inline(blob, ext)
        return
    if ext in _PDF_PREVIEW_EXTENSIONS:
        _render_pdf_inline(blob)
        return
    if ext in _ANIM3D_PREVIEW_EXTENSIONS:
        _render_anim3d_inline(blob, extension=ext)
        return
    if ext in _VIDEO_PREVIEW_EXTENSIONS:
        _render_video_inline(blob, extension=ext)


def _render_pdf_inline(content: bytes) -> None:
    """Embed a PDF preview below the row.

    Re-uses the shared :func:`matofx.ui.cad_preview.render_pdf_preview`
    helper so the iframe sizing / data-URL plumbing stays in one place
    and the job page + compare grid show the file the same way.
    """
    from matofx.ui.cad_preview import render_pdf_preview  # noqa: PLC0415

    render_pdf_preview(content, height=380)


def _render_anim3d_inline(content: bytes, *, extension: str) -> None:
    """Embed a GLTF/GLB preview below the row.

    Local import keeps the three.js plumbing off the import path for
    tests that don't exercise the 3-D scene viewer (mirrors how the
    STL branch above defers the ``ui.scene`` import).
    """
    from matofx.ui.cad_preview import render_gltf_preview  # noqa: PLC0415

    render_gltf_preview(content, extension=extension, height=300)


def _render_video_inline(content: bytes, *, extension: str) -> None:
    """Embed an HTML5 video preview below the row."""
    from matofx.ui.cad_preview import render_video_preview  # noqa: PLC0415

    render_video_preview(content, extension=extension, height=320)


def _render_image_inline(content: bytes, extension: str) -> None:
    """Embed image bytes as a data URL inline below the row.

    ``fit=contain`` so a non-square image renders in full rather than
    being cropped to fill the box — same lesson as the upload
    widget's preview pane (``ui.image`` defaults to Quasar
    ``fit=cover``).
    """
    import base64  # noqa: PLC0415

    mime = _IMAGE_PREVIEW_MIME.get(extension, "application/octet-stream")
    data_url = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
    ui.image(data_url).props("fit=contain").classes("w-full").style(
        "max-height: 280px; height: 280px; background: transparent;"
    )


def _render_csv_table_inline(
    content: bytes,
    *,
    declared_columns: Any | None,
    fallback_label: str,
) -> None:
    """Embed a ``csv_table`` output inline below its row.

    Mirrors :func:`matofx.ui.pages.job._render_csv_table`'s emit path
    — parses the CSV bytes via pandas, honours the descriptor's
    ``columns`` for header labels + visible order, and renders a
    compact ``ui.table``. Parse failures surface a friendly message
    rather than crashing the cell.
    """
    # Local imports: ``pandas`` is heavy and not needed by callers
    # that only emit scalar / file rows. Keeping the import lazy
    # leaves the renderer cheap for tests that don't exercise the
    # csv_table path.
    import io  # noqa: PLC0415

    import pandas as pd  # noqa: PLC0415

    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception as exc:  # pandas raises a broad family on parse errors
        ui.label(f"Couldn't parse {fallback_label}: {exc}").style(
            "color: var(--matofx-error-text); font-size: 0.85rem;"
        )
        return
    if df.empty:
        ui.label("(no rows)").style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        return

    if isinstance(declared_columns, list) and declared_columns:
        visible: list[tuple[str, str]] = []
        for col in declared_columns:
            if not isinstance(col, dict):
                continue
            field_name = str(col.get("name") or "")
            if not field_name or field_name not in df.columns:
                continue
            display_label = str(col.get("label") or field_name)
            visible.append((field_name, display_label))
        if not visible:
            visible = [(str(c), str(c)) for c in df.columns]
    else:
        visible = [(str(c), str(c)) for c in df.columns]

    columns = [{"name": f, "label": display, "field": f} for f, display in visible]
    rows = [
        {**{f: row.get(f) for f, _ in visible}, "_row": idx}
        for idx, row in enumerate(df.to_dict("records"))
    ]
    ui.table(columns=columns, rows=rows, row_key="_row").classes("w-full").props("dense flat")


def _render_html_plot_inline(content: bytes, *, fallback_label: str) -> None:
    """Embed an ``html_plot`` output inline below its row.

    Mirrors :func:`matofx.ui.pages.job._render_html_plot`. Tries to pull
    the plotly figure JSON out of the page (so theme + responsiveness
    track the rest of the dashboard); falls back to a self-contained
    ``<iframe srcdoc>`` for non-plotly HTML.

    Helpers (``_try_extract_plotly_figure`` + ``_bracket_balanced_end``)
    live in :mod:`matofx.ui.pages.compare.overlay`; the cell renderer
    re-uses them rather than re-implementing the parser, so future
    changes to plotly's ``write_html`` format only need one update.
    """
    # Local import: ``overlay`` pulls in plotly / pandas which we don't
    # want loaded at module-import time for unit tests of the renderer.
    from matofx.ui.pages.compare.overlay import (  # noqa: PLC0415
        _try_extract_plotly_figure,
    )
    from matofx.ui.theme import themed_plotly  # noqa: PLC0415

    try:
        html_text = content.decode("utf-8", errors="replace")
    except Exception:
        ui.label(f"{fallback_label}: could not decode HTML bytes.").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )
        return

    fig = _try_extract_plotly_figure(html_text)
    if fig is not None:
        fig.update_layout(
            height=340,
            margin={"l": 45, "r": 15, "t": 30, "b": 35},
        )
        ui.plotly(themed_plotly(fig)).classes("w-full")
        return

    import html as _html  # noqa: PLC0415

    # ``sandbox="allow-scripts"`` (no ``allow-same-origin``) isolates the
    # tool-produced HTML in an opaque origin so embedded scripts can't
    # touch the MATOFX session — stored-XSS containment.
    ui.html(
        f'<iframe srcdoc="{_html.escape(html_text, quote=True)}" '
        f'sandbox="allow-scripts" '
        f'style="border:0; background:white; border-radius:4px; '
        f'width:100%; height:380px;"></iframe>',
        sanitize=False,
    ).classes("w-full")


def _format_value(value: Any, field_type: str) -> str:
    """Turn a stored value into a compact display string.

    Mirrors :func:`matofx.ui.pages.job._format_input_value` so the
    Compare cells render values the same way the dedicated job page
    does — important for "looks the same place" recognition. We don't
    import the helper directly because :mod:`job` is a heavy module
    (plotly, pandas, etc.) and the compare cell renderer should stay
    cheap to import for unit tests.
    """
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

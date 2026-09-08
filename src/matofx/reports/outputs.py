"""Output-side asset collection for the per-job report.

Centralises the I/O the router needs to drive the Outputs section:

- read ``outputs/outputs.json`` from the job's working dir,
- embed each image-typed output file as a ``data:`` URI (with the
  same compression policy as input image previews),
- parse each ``file_type=csv_table`` output's first
  :data:`matofx.reports.builder.MAX_CSV_TABLE_ROWS` rows for inline
  rendering with a truncation note.

Plot / video / PDF / HTML / 3D outputs are out of scope for slice 4
and surface as filename-only via the ``file_other`` field kind.
Later slices replace those branches with real viewers.

Returns plain dicts so the builder stays pure-Python and the router
doesn't need to know about Pillow / CSV parsing internals.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from matofx.reports.assets import (
    VIDEO_SIZE_CAP_BYTES,
    human_size,
    inline_html_embed,
    inline_input_image,
    inline_pdf,
    inline_video,
    is_3d_animation_file,
    is_html_file,
    is_pdf_file,
    is_previewable_image,
    is_video_file,
    safe_resolve_under_mount,
)
from matofx.reports.builder import MAX_CSV_TABLE_ROWS

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutputPreviewBundle:
    """Everything the report builder needs from the job's working dir.

    Returned by :func:`collect_output_previews`. Each map is keyed by
    the descriptor's ``filename`` so the builder can look up the
    rendering for a given file output without re-deriving its
    extension / file_type.
    """

    outputs_doc: dict[str, Any] = field(default_factory=dict)
    """Parsed ``outputs/outputs.json``; ``{}`` when missing /
    malformed."""

    image_previews: dict[str, str] = field(default_factory=dict)
    """``{filename: data:URI}`` for image outputs."""

    csv_tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``{filename: {columns, rows, total_rows, truncated}}``."""

    csv_plots: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``{filename: {figure_json, div_id}}``."""

    videos: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``{filename: {data_uri, size_bytes, mime}}`` for embedded
    videos (≤ :data:`VIDEO_SIZE_CAP_BYTES`)."""

    pdfs: dict[str, str] = field(default_factory=dict)
    """``{filename: data:URI}`` for PDF outputs."""

    html_embeds: dict[str, str] = field(default_factory=dict)
    """``{filename: data:URI}`` for HTML outputs the report sandboxes
    in an iframe."""

    file_notes: dict[str, str] = field(default_factory=dict)
    """``{filename: note}`` overrides for the ``file_other`` branch.
    Surfaces messages like "Not embedded — 47 MB, over 25 MB cap" or
    "3D animation — not previewable in report"."""

    def __iter__(self) -> Iterator[Any]:
        """Yield the legacy 4-tuple shape for backwards compatibility.

        ``(outputs_doc, image_previews, csv_tables, csv_plots)`` —
        tests written before slice 6 destructure the result as a
        4-tuple; iteration preserves that shape so they keep working
        unchanged. The new video / pdf / html_embed / file_notes maps
        are reachable only via the named attributes.
        """
        yield self.outputs_doc
        yield self.image_previews
        yield self.csv_tables
        yield self.csv_plots


@dataclass
class _PreviewMaps:
    """Mutable working set the per-kind helpers append into."""

    image_previews: dict[str, str] = field(default_factory=dict)
    csv_tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    csv_plots: dict[str, dict[str, Any]] = field(default_factory=dict)
    videos: dict[str, dict[str, Any]] = field(default_factory=dict)
    pdfs: dict[str, str] = field(default_factory=dict)
    html_embeds: dict[str, str] = field(default_factory=dict)
    file_notes: dict[str, str] = field(default_factory=dict)


def collect_output_previews(
    *,
    descriptor: dict[str, Any],
    mount_root: Path,
    working_dir: Path,
) -> OutputPreviewBundle:
    """Build the :class:`OutputPreviewBundle` for the report's Outputs section.

    Every component defaults to empty so a missing / unparseable
    artefact silently collapses to "no data" in the report rather
    than failing the download — the report is best-effort and the
    raw files remain available in MATOFX.
    """
    outputs_doc = _read_outputs_document(working_dir)
    file_outputs = [
        f
        for f in (descriptor.get("outputs") or [])
        if isinstance(f, dict) and f.get("type") == "file" and f.get("filename")
    ]
    maps = _PreviewMaps()
    for spec in file_outputs:
        filename = str(spec["filename"]).strip()
        if not filename:
            continue
        _populate_for_file_output(spec, filename, mount_root, working_dir, maps)
    return OutputPreviewBundle(
        outputs_doc=outputs_doc,
        image_previews=maps.image_previews,
        csv_tables=maps.csv_tables,
        csv_plots=maps.csv_plots,
        videos=maps.videos,
        pdfs=maps.pdfs,
        html_embeds=maps.html_embeds,
        file_notes=maps.file_notes,
    )


def _populate_for_file_output(
    spec: dict[str, Any],
    filename: str,
    mount_root: Path,
    working_dir: Path,
    maps: _PreviewMaps,
) -> None:
    """Dispatch one descriptor file output into the right preview bucket.

    Output files live under ``<working_dir>/outputs/<filename>`` —
    same layout the API's ``/api/jobs/{id}/outputs/{filename}`` route
    serves from (see ``_resolve_job_paths`` in the jobs router).
    Earlier slices accidentally looked one directory up, which made
    every ``csv_plot`` / ``csv_table`` / image / HTML output miss the
    file silently and fall through to filename-only rendering.

    The dispatch happens in two passes: first the descriptor's
    declared ``file_type`` (the explicit hint), then the filename
    extension (the fallback). Either pass is allowed to fail to find
    a renderer — the file then surfaces as ``file_other`` in the
    builder.
    """
    file_type = str(spec.get("file_type") or "").strip().lower()
    absolute_str = str(working_dir / "outputs" / filename)
    if _populate_by_file_type(file_type, absolute_str, filename, mount_root, spec, maps):
        return
    _populate_by_extension(absolute_str, filename, mount_root, maps)


def _populate_by_file_type(
    file_type: str,
    absolute_str: str,
    filename: str,
    mount_root: Path,
    spec: dict[str, Any],
    maps: _PreviewMaps,
) -> bool:
    """Handle ``file_type=...`` descriptor hints. Returns ``True`` on a hit."""
    if file_type == "csv_plot":
        plot = _parse_csv_plot(absolute_str, mount_root, spec)
        if plot is not None:
            maps.csv_plots[filename] = plot
        return True
    if file_type == "html_plot":
        # html_plot rides on the same bundled plotly.js + figure JSON
        # path as csv_plot — the figure just came from a
        # ``plotly.write_html`` dump rather than a CSV.
        plot = _parse_html_plot(absolute_str, mount_root, spec)
        if plot is not None:
            maps.csv_plots[filename] = plot
        return True
    if file_type == "csv_table":
        payload = _parse_csv_table(absolute_str, mount_root, spec)
        if payload is not None:
            maps.csv_tables[filename] = payload
        return True
    return False


def _populate_by_extension(
    absolute_str: str,
    filename: str,
    mount_root: Path,
    maps: _PreviewMaps,
) -> None:
    """Fall-back dispatch when the descriptor didn't pin a ``file_type``."""
    if is_3d_animation_file(filename):
        maps.file_notes[filename] = "3D animation — not previewable in report"
        return
    if is_video_file(filename):
        _populate_video(absolute_str, mount_root, filename, maps)
        return
    if is_pdf_file(filename):
        data_uri = inline_pdf(value=absolute_str, mount_root=mount_root)
        if data_uri is not None:
            maps.pdfs[filename] = data_uri
        return
    if is_html_file(filename):
        data_uri = inline_html_embed(value=absolute_str, mount_root=mount_root)
        if data_uri is not None:
            maps.html_embeds[filename] = data_uri
        return
    if is_previewable_image(filename):
        data_uri = inline_input_image(value=absolute_str, mount_root=mount_root)
        if data_uri is not None:
            maps.image_previews[filename] = data_uri


def _populate_video(
    absolute_str: str,
    mount_root: Path,
    filename: str,
    maps: _PreviewMaps,
) -> None:
    """Embed a video that fits the cap, or stash an 'over-cap' note."""
    embedded = inline_video(value=absolute_str, mount_root=mount_root)
    if embedded is None:
        return
    if embedded.get("oversized"):
        size = int(embedded["size_bytes"])
        maps.file_notes[filename] = (
            f"Not embedded — {human_size(size)}, over the {human_size(VIDEO_SIZE_CAP_BYTES)} cap"
        )
        return
    maps.videos[filename] = embedded


def _read_outputs_document(working_dir: Path) -> dict[str, Any]:
    """Read + parse ``<wd>/outputs/outputs.json`` into a dict.

    Returns ``{}`` on any failure (missing file, malformed JSON, etc.)
    — the Outputs section will then render purely from the descriptor
    + on-disk files. Mirrors ``_read_output_document`` in the
    hierarchy service but stays synchronous + module-local so the
    report module has zero reach into other services.
    """
    path = working_dir / "outputs" / "outputs.json"
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, OSError):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.debug("outputs.json malformed at %s: %s", path, exc)
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _parse_csv_table(
    value: str,
    mount_root: Path,
    spec: dict[str, Any],
) -> dict[str, Any] | None:
    """Read a CSV output and project it into the template's row shape.

    Returns ``{"columns": [...], "rows": [...], "total_rows": N,
    "truncated": bool}`` or ``None`` when the file can't be read or
    refused the security gate. Reads at most
    ``MAX_CSV_TABLE_ROWS + 1`` rows so we know whether the file has
    more rows than the embedded slice (drives the truncation note)
    without buffering the whole file when it's huge.
    """
    path = safe_resolve_under_mount(value, mount_root)
    if path is None:
        return None
    try:
        # ``newline=""`` per csv-module docs; ``utf-8-sig`` swallows an
        # accidental BOM at the head of the file.
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                return {"columns": [], "rows": [], "total_rows": 0, "truncated": False}
            rows: list[dict[str, Any]] = []
            extra_rows = 0
            for raw_row in reader:
                row = {
                    header[i]: raw_row[i] if i < len(raw_row) else "" for i in range(len(header))
                }
                if len(rows) < MAX_CSV_TABLE_ROWS:
                    rows.append(row)
                else:
                    extra_rows += 1
    except (OSError, UnicodeDecodeError) as exc:
        _log.debug("csv_table %s: %s", path.name, exc)
        return None

    declared = _declared_columns(spec)
    columns = declared if declared else [str(h) for h in header]
    return {
        "columns": columns,
        "rows": rows,
        "total_rows": len(rows) + extra_rows,
        "truncated": extra_rows > 0,
    }


def _declared_columns(spec: dict[str, Any]) -> list[str]:
    """Pull the descriptor's declared column order, if any."""
    declared = spec.get("columns")
    if not isinstance(declared, list):
        return []
    return [
        str(col.get("name") or "") for col in declared if isinstance(col, dict) and col.get("name")
    ]


def _parse_csv_plot(
    value: str,
    mount_root: Path,
    spec: dict[str, Any],
) -> dict[str, Any] | None:
    """Read a CSV output and build an interactive Plotly figure for it.

    Returns ``{"figure_json": str, "div_id": str}`` ready for the
    template's ``Plotly.newPlot(div_id, fig.data, fig.layout)`` call,
    or ``None`` when the file refuses the security gate / can't be
    parsed / picks columns out of range.

    Column selection mirrors the dashboard's live renderer:
    ``xaxis_column`` / ``yaxis_column`` from the descriptor (0-indexed,
    defaulting to columns 0 and 1). Titles fall back to the
    descriptor's ``label`` / the CSV's header row when no overrides
    are set.
    """
    path = safe_resolve_under_mount(value, mount_root)
    if path is None:
        return None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                return None
            columns: list[list[float | None]] = [[] for _ in header]
            for raw_row in reader:
                for idx in range(len(header)):
                    cell = raw_row[idx] if idx < len(raw_row) else ""
                    columns[idx].append(_to_float_or_none(cell))
    except (OSError, UnicodeDecodeError) as exc:
        _log.debug("csv_plot %s: %s", path.name, exc)
        return None

    x_col = _column_index(spec.get("xaxis_column"), default=0)
    y_col = _column_index(spec.get("yaxis_column"), default=1)
    if x_col >= len(header) or y_col >= len(header):
        _log.debug(
            "csv_plot %s: column index out of range (x=%d, y=%d, have=%d)",
            path.name,
            x_col,
            y_col,
            len(header),
        )
        return None

    label = str(spec.get("label") or spec.get("name") or path.name)
    title = str(spec.get("plot_title") or label)
    xaxis_title = str(spec.get("xaxis_title") or header[x_col])
    yaxis_title = str(spec.get("yaxis_title") or header[y_col])

    figure = {
        "data": [
            {
                "type": "scatter",
                "mode": "lines",
                "x": columns[x_col],
                "y": columns[y_col],
                "name": yaxis_title,
            }
        ],
        "layout": {
            "title": {"text": title},
            "xaxis": {"title": {"text": xaxis_title}},
            "yaxis": {"title": {"text": yaxis_title}},
            "margin": {"l": 60, "r": 20, "t": 50, "b": 50},
            # Light theme for print fidelity regardless of how the
            # operator has their dashboard skin set.
            "paper_bgcolor": "white",
            "plot_bgcolor": "white",
            "font": {"color": "#1f2937"},
        },
    }
    # ``allow_nan=True`` so NaN cells survive — Plotly interprets
    # those as gaps in the trace, which is the right behaviour for
    # an interrupted scan / failed iteration.
    figure_json = json.dumps(figure, allow_nan=True, default=str)
    safe_name = str(spec.get("name") or path.stem)
    div_id = f"matofx-plot-{safe_name}"
    return {"figure_json": figure_json, "div_id": div_id}


def _parse_html_plot(
    value: str,
    mount_root: Path,
    spec: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract the figure JSON from a ``plotly.write_html`` dump.

    The legacy ``html_plot`` output kind is a complete HTML document
    with a ``Plotly.newPlot(...)`` call embedded by Plotly's HTML
    writer. The dashboard's renderer pulls the figure JSON out of
    that call and re-plays it against NiceGUI's bundled plotly
    element; the report does the same so the chart renders against
    its own inline ``plotly.min.js`` (the original document loads
    plotly from a CDN, which won't fire on an air-gapped machine).
    Returns ``None`` if the file isn't a Plotly-emitted HTML — the
    caller then falls back to ``file_other``.
    """
    from matofx.ui.plotly_extract import extract_plotly_figure  # noqa: PLC0415

    path = safe_resolve_under_mount(value, mount_root)
    if path is None:
        return None
    try:
        html_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log.debug("html_plot %s: %s", path.name, exc)
        return None
    figure = extract_plotly_figure(html_text)
    if figure is None:
        return None
    # Drop in the report's light-theme defaults so the chart prints
    # cleanly regardless of how the source HTML was generated.
    layout = dict(figure.get("layout") or {})
    layout.setdefault("paper_bgcolor", "white")
    layout.setdefault("plot_bgcolor", "white")
    layout.setdefault("font", {"color": "#1f2937"})
    figure_payload = {"data": figure.get("data") or [], "layout": layout}
    safe_name = str(spec.get("name") or path.stem)
    return {
        "figure_json": json.dumps(figure_payload, allow_nan=True, default=str),
        "div_id": f"matofx-plot-{safe_name}",
    }


def _to_float_or_none(cell: str) -> float | None:
    """Best-effort cell → float; non-numeric cells become ``None`` (Plotly gap)."""
    text = cell.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _column_index(value: Any, *, default: int) -> int:
    """Coerce a descriptor's column index to a non-negative ``int``."""
    if value is None:
        return default
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return default
    return max(idx, 0)

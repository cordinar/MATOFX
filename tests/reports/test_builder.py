"""Smoke tests for :mod:`matofx.reports.builder`.

The builder is a pure function (no DB, no I/O) so it's exercised here
in-memory. Pins down the self-containedness invariant — no external
CSS / JS / link references — and the headline metadata fields. Later
slices extend this file as the report grows.
"""

from __future__ import annotations

import datetime as _dt
import json
import re

import pytest

from matofx.reports import ReportContext, SiblingJob, WorkflowContext, build_report

_FIXED_NOW = _dt.datetime(2026, 5, 20, 14, 30, 0, tzinfo=_dt.UTC)


def _context(**overrides: object) -> ReportContext:
    """Construct a baseline :class:`ReportContext` for tests to tweak."""
    base = {
        "job_id": 123,
        "tool_alias": "material_properties",
        "tool_name": "Material Properties",
        "tool_version": "1.4.2",
        "submitted_by_username": "alice",
        "submitted_at": _dt.datetime(2026, 5, 19, 9, 0, 0, tzinfo=_dt.UTC),
        "completed_at": _dt.datetime(2026, 5, 19, 9, 7, 12, tzinfo=_dt.UTC),
        "report_generated_at": _FIXED_NOW,
        "app_display_title": "MATOFX",
    }
    base.update(overrides)
    return ReportContext(**base)  # type: ignore[arg-type]


def test_returns_well_formed_html_document() -> None:
    """The skeleton renders a valid-looking HTML5 document."""
    html = build_report(_context())
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    assert "<title>" in html
    assert '<meta charset="utf-8">' in html


def test_metadata_block_carries_every_field() -> None:
    """Every visible field on :class:`ReportContext` lands in the output."""
    html = build_report(_context())
    assert "Job ID" in html
    assert "123" in html
    assert "Material Properties" in html
    assert "material_properties" in html
    assert "v1.4.2" in html
    assert "alice" in html
    # Default timezone is UTC; format is "DD Mon YYYY, HH:MM TZ".
    assert "19 May 2026, 09:00 UTC" in html
    assert "19 May 2026, 09:07 UTC" in html
    assert "20 May 2026, 14:30 UTC" in html


def test_renders_em_dash_for_missing_timestamps() -> None:
    """When ``submitted_at`` / ``completed_at`` are None, cell uses an em-dash."""
    html = build_report(_context(submitted_at=None, completed_at=None))
    # The Unicode em-dash appears at least twice (submitted + completed
    # cells). The title tag uses the &mdash; entity separately.
    assert html.count("—") >= 2


def test_uses_branded_app_title() -> None:
    """``app_display_title`` flows into both the ``<title>`` tag and the ``<h1>``."""
    html = build_report(_context(app_display_title="Acme Tools"))
    assert "<title>Acme Tools" in html
    assert "<h1>Acme Tools</h1>" in html


def test_escapes_user_supplied_strings() -> None:
    """HTML-meaningful characters in tool / user fields get escaped (Jinja2 autoescape)."""
    html = build_report(
        _context(
            tool_name="Tool <script>alert(1)</script>",
            submitted_by_username="alice & bob",
        )
    )
    assert "<script>alert(1)</script>" not in html
    assert "Tool &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "alice &amp; bob" in html


_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    # External URL schemes inside HTML attributes (src=, href=,
    # poster=, action=, etc.). Scoped to attribute usage so URL
    # strings buried inside bundled JS (Plotly's source carries
    # links to its own docs/branding) don't false-positive.
    r"""(?:src|href|poster|action|formaction)\s*=\s*["']https?://""",
    # Stylesheet / module / script references that pull from disk
    # or network.
    r"<link\s+[^>]*rel=",
    r"<script\s+[^>]*src=",
    r"@import\s+",
)


def _assert_self_contained(html: str) -> None:
    """Strip ``<script>`` bodies, then assert no forbidden patterns remain.

    Bundled JS may legitimately contain URL string literals (Plotly
    builds branding links via DOM construction at runtime, not via
    load-time ``<a href="...">`` HTML). Those don't force a network
    fetch when the file is opened, so the check is scoped to the
    surrounding HTML only.
    """
    stripped = _SCRIPT_BLOCK_RE.sub("", html)
    for pattern in _FORBIDDEN_PATTERNS:
        match = re.search(pattern, stripped, flags=re.IGNORECASE)
        assert match is None, (
            f"Self-containedness violation: pattern {pattern!r} matched "
            f"at offset {match.start() if match else -1}"
        )


def test_is_fully_self_contained() -> None:
    """No external CSS / JS / font / image references in the document.

    The whole point of the per-job report is that a recipient can
    open it on an air-gapped machine. Anything that would force a
    network fetch breaks that promise.
    """
    _assert_self_contained(build_report(_context()))


def test_logo_data_uri_embeds_when_provided() -> None:
    """The ``logo_data_uri`` flag drops a base64 ``<img>`` into the header."""
    data_uri = "data:image/png;base64,iVBORw0K"
    html = build_report(_context(logo_data_uri=data_uri))
    assert 'class="report-logo"' in html
    assert data_uri in html


def test_logo_omitted_when_no_data_uri() -> None:
    """When ``logo_data_uri`` is ``None`` the template skips the ``<img>`` tag."""
    html = build_report(_context(logo_data_uri=None))
    assert 'class="report-logo"' not in html


def test_timestamps_render_in_user_timezone() -> None:
    """Non-UTC ``timezone`` flips timestamps into that zone with its abbreviation."""
    html = build_report(
        _context(
            submitted_at=_dt.datetime(2026, 5, 19, 9, 0, 0, tzinfo=_dt.UTC),
            completed_at=_dt.datetime(2026, 5, 19, 9, 7, 12, tzinfo=_dt.UTC),
            report_generated_at=_dt.datetime(2026, 5, 20, 14, 30, 0, tzinfo=_dt.UTC),
            timezone="Europe/London",
        )
    )
    # London is BST in May (UTC+1) — 09:00 UTC → 10:00 BST.
    assert "10:00 BST" in html


def test_unknown_timezone_falls_back_to_utc() -> None:
    """A bogus timezone string degrades gracefully to UTC."""
    html = build_report(_context(timezone="Not/A_Real_Zone"))
    assert "19 May 2026, 09:00 UTC" in html


@pytest.mark.parametrize("title", ["MATOFX", "Customer Brand", "Funis SPDM"])
def test_title_appears_consistently(title: str) -> None:
    """The branded app title appears in both ``<title>`` and ``<h1>``."""
    html = build_report(_context(app_display_title=title))
    assert f"<title>{title}" in html
    assert f"<h1>{title}</h1>" in html


# ----- Slice 3: Inputs section ----------------------------------------------


def _ambient_descriptor() -> dict[str, object]:
    """Two-group descriptor with one each of every non-table input type."""
    return {
        "inputs": [
            {
                "name": "temperature",
                "label": "Temperature",
                "type": "float",
                "unit": "K",
                "group": "ambient",
            },
            {
                "name": "pressure",
                "label": "Pressure",
                "type": "float",
                "unit": "bar",
                "group": "ambient",
            },
            {
                "name": "method",
                "label": "Method",
                "type": "select",
                "group": "model",
                "options": ["A", "B"],
            },
            {"name": "use_relaxation", "label": "Relax", "type": "boolean", "group": "model"},
            {"name": "phases", "label": "Phases", "type": "multiselect", "group": "model"},
        ],
        "input_groups": [
            {"name": "ambient", "label": "Ambient conditions", "column": 1, "row": 1},
            {"name": "model", "label": "Model settings", "column": 1, "row": 2},
        ],
    }


def test_renders_grouped_input_section() -> None:
    """Each populated descriptor group becomes a ``<section class="group">`` card."""
    html = build_report(
        _context(
            descriptor=_ambient_descriptor(),
            job_inputs={
                "temperature": 300.5,
                "pressure": 1.0,
                "method": "B",
                "use_relaxation": True,
                "phases": ["solid", "liquid"],
            },
        )
    )
    assert '<section class="section-heading">Inputs</section>' in html
    assert "Ambient conditions" in html
    assert "Model settings" in html
    assert "300.5" in html
    assert ">K<" in html  # unit rendered alongside value
    assert "Yes" in html  # boolean
    assert "solid, liquid" in html  # multiselect


def test_skips_empty_groups() -> None:
    """A group whose fields are all unpopulated does not render."""
    html = build_report(
        _context(
            descriptor=_ambient_descriptor(),
            job_inputs={"temperature": 300.5, "pressure": 1.0},
        )
    )
    assert "Ambient conditions" in html
    # Model group is unpopulated → not rendered.
    assert "Model settings" not in html


def test_flat_inputs_fallback_when_no_groups() -> None:
    """A descriptor with no ``input_groups`` collapses to a single Inputs card."""
    descriptor = {
        "inputs": [
            {"name": "x", "label": "X", "type": "string"},
            {"name": "y", "label": "Y", "type": "integer", "unit": "mm"},
        ],
    }
    html = build_report(_context(descriptor=descriptor, job_inputs={"x": "alpha", "y": 7}))
    # The synthetic group title is "Inputs".
    assert ">Inputs</h3>" in html
    assert "alpha" in html
    assert ">mm<" in html


def test_file_input_shows_filename_only_when_no_preview() -> None:
    """File inputs whose path didn't produce a preview render as a name + note."""
    descriptor = {
        "inputs": [{"name": "geom", "label": "Geometry", "type": "file"}],
    }
    html = build_report(
        _context(
            descriptor=descriptor,
            job_inputs={"geom": "/mount/jobs/job_0000001/inputs/uploaded/part.stp"},
        )
    )
    assert "part.stp" in html  # basename, not the absolute path
    assert "/mount/jobs/" not in html
    assert "not previewable in report" in html
    assert 'class="image-preview"' not in html


def test_file_input_embeds_image_preview_when_provided() -> None:
    """When the router supplies a data URI, the template embeds the image."""
    descriptor = {
        "inputs": [{"name": "thumb", "label": "Thumbnail", "type": "file"}],
    }
    data_uri = "data:image/png;base64,iVBORw0K"
    path = "/mount/jobs/job_0000001/inputs/uploaded/thumb.png"
    html = build_report(
        _context(
            descriptor=descriptor,
            job_inputs={"thumb": path},
            input_image_previews={path: data_uri},
        )
    )
    assert "thumb.png" in html
    assert data_uri in html
    assert 'class="image-preview"' in html
    assert "not previewable" not in html


def test_table_input_renders_as_html_table() -> None:
    """``table`` inputs become a ``<table>`` with descriptor-declared column order."""
    descriptor = {
        "inputs": [
            {
                "name": "components",
                "label": "Components",
                "type": "table",
                "columns": [
                    {"name": "material", "type": "string"},
                    {"name": "fraction", "type": "float"},
                ],
            }
        ],
    }
    html = build_report(
        _context(
            descriptor=descriptor,
            job_inputs={
                "components": [
                    {"material": "Steel", "fraction": 0.7},
                    {"material": "Copper", "fraction": 0.3},
                ]
            },
        )
    )
    assert 'class="value-table"' in html
    assert "<th>material</th>" in html
    assert "<th>fraction</th>" in html
    assert "Steel" in html
    assert "0.7" in html


def test_inputs_section_absent_when_no_groups_have_values() -> None:
    """No populated inputs at all → the Inputs section heading is omitted."""
    html = build_report(_context(descriptor=_ambient_descriptor(), job_inputs={}))
    # Match the actual tag, not the CSS selector inside the inline stylesheet.
    assert '<section class="section-heading">' not in html
    assert '<div class="groups-grid">' not in html


def test_adaptive_layout_uses_auto_fit_grid() -> None:
    """The Inputs container uses the auto-fit grid declared in ADR-0004."""
    html = build_report(
        _context(
            descriptor=_ambient_descriptor(),
            job_inputs={"temperature": 300.5},
        )
    )
    assert 'class="groups-grid"' in html
    # The stylesheet declares the auto-fit rule with minmax(360px, 1fr).
    assert "repeat(auto-fit, minmax(360px, 1fr))" in html


# ----- Slice 4: Outputs section ---------------------------------------------


def _two_group_output_descriptor() -> dict[str, object]:
    """Descriptor with grouped scalar / image / csv_table / file_other outputs."""
    return {
        "outputs": [
            {
                "name": "yield_value",
                "label": "Yield",
                "type": "number",
                "unit": "%",
                "precision": 0.01,
                "group": "scalars",
            },
            {
                "name": "status_message",
                "label": "Status",
                "type": "string",
                "group": "scalars",
            },
            {
                "name": "thumbnail",
                "label": "Thumbnail",
                "type": "file",
                "filename": "thumbnail.png",
                "group": "media",
            },
            {
                "name": "raw_log",
                "label": "Raw log",
                "type": "file",
                "filename": "run.log",
                "group": "media",
            },
            {
                "name": "trajectory",
                "label": "Trajectory",
                "type": "file",
                "filename": "trajectory.csv",
                "file_type": "csv_table",
                "group": "tables",
            },
        ],
        "output_groups": [
            {"name": "scalars", "label": "Key values", "column": 1, "row": 1},
            {"name": "media", "label": "Media", "column": 2, "row": 1},
            {"name": "tables", "label": "Tabular outputs", "column": 1, "row": 2},
        ],
    }


def _outputs_doc() -> dict[str, object]:
    return {
        "schema_version": 1,
        "tool_alias": "demo",
        "outputs": [
            {"name": "yield_value", "type": "number", "value": 87.4567, "unit": "%"},
            {"name": "status_message", "type": "string", "value": "Converged"},
        ],
    }


def test_outputs_section_renders_grouped() -> None:
    """The Outputs section appears with descriptor groups and a section heading."""
    html = build_report(
        _context(
            descriptor=_two_group_output_descriptor(),
            outputs_doc=_outputs_doc(),
        )
    )
    assert '<section class="section-heading">Outputs</section>' in html
    assert "Key values" in html
    assert "Tabular outputs" in html


def test_number_output_respects_precision_and_unit() -> None:
    """Numeric outputs are rounded to the descriptor's precision + carry the unit."""
    html = build_report(
        _context(
            descriptor=_two_group_output_descriptor(),
            outputs_doc=_outputs_doc(),
        )
    )
    assert "87.46" in html  # precision=0.01 → 2 decimals
    assert ">%<" in html


def test_string_output_renders_value() -> None:
    """String outputs render their value as-is in the metadata grid."""
    html = build_report(
        _context(
            descriptor=_two_group_output_descriptor(),
            outputs_doc=_outputs_doc(),
        )
    )
    assert "Converged" in html


def test_image_output_embeds_preview() -> None:
    """Output files with image extensions get the inline preview when one was inlined."""
    data_uri = "data:image/png;base64,iVBORw0K"
    html = build_report(
        _context(
            descriptor=_two_group_output_descriptor(),
            outputs_doc=_outputs_doc(),
            output_image_previews={"thumbnail.png": data_uri},
        )
    )
    assert "thumbnail.png" in html
    assert data_uri in html
    assert 'class="image-preview"' in html


def test_image_output_without_preview_falls_through_to_filename() -> None:
    """An image output the router couldn't preview falls back to file_other rendering."""
    html = build_report(
        _context(
            descriptor=_two_group_output_descriptor(),
            outputs_doc=_outputs_doc(),
            output_image_previews={},
        )
    )
    # Filename is shown but the inline <img> isn't.
    assert "thumbnail.png" in html
    assert 'class="image-preview"' not in html
    assert "(available in MATOFX)" in html


def test_csv_table_output_renders_as_table() -> None:
    """``file_type=csv_table`` outputs render their rows inside an HTML table."""
    csv_payload = {
        "columns": ["t", "value"],
        "rows": [{"t": "0.0", "value": "1.2"}, {"t": "0.1", "value": "1.3"}],
        "total_rows": 2,
        "truncated": False,
    }
    html = build_report(
        _context(
            descriptor=_two_group_output_descriptor(),
            outputs_doc=_outputs_doc(),
            output_csv_tables={"trajectory.csv": csv_payload},
        )
    )
    assert "trajectory.csv" in html
    assert "<th>t</th>" in html
    assert "<th>value</th>" in html
    assert "<td>0.0</td>" in html
    assert "<td>1.3</td>" in html


def test_csv_table_truncated_emits_note() -> None:
    """A truncated CSV table surfaces the 'N of M rows shown' line."""
    csv_payload = {
        "columns": ["t", "v"],
        "rows": [{"t": str(i), "v": str(i * 2)} for i in range(100)],
        "total_rows": 250,
        "truncated": True,
    }
    html = build_report(
        _context(
            descriptor=_two_group_output_descriptor(),
            outputs_doc=_outputs_doc(),
            output_csv_tables={"trajectory.csv": csv_payload},
        )
    )
    assert "truncation-note" in html
    assert "First 100 of 250 rows shown" in html


def test_csv_table_group_is_full_width() -> None:
    """A group containing a CSV table spans the full grid row via .wide."""
    html = build_report(
        _context(
            descriptor=_two_group_output_descriptor(),
            outputs_doc=_outputs_doc(),
            output_csv_tables={
                "trajectory.csv": {
                    "columns": ["t"],
                    "rows": [{"t": "0"}],
                    "total_rows": 1,
                    "truncated": False,
                }
            },
        )
    )
    assert '<section class="group wide">' in html


def test_unrendered_file_output_lists_filename() -> None:
    """A file output we don't recognise lists the filename + 'available in MATOFX'."""
    html = build_report(
        _context(
            descriptor=_two_group_output_descriptor(),
            outputs_doc=_outputs_doc(),
        )
    )
    # run.log has no recognised extension and no file_type → file_other.
    assert "run.log" in html
    assert "available in MATOFX" in html


def test_outputs_section_absent_when_no_outputs_declared() -> None:
    """A descriptor with no ``outputs`` skips the Outputs section entirely."""
    html = build_report(_context(descriptor={"inputs": []}, outputs_doc={}))
    assert '<section class="section-heading">Outputs</section>' not in html


def test_output_without_runtime_value_is_skipped() -> None:
    """A declared number output that the tool didn't emit is omitted, not blanked."""
    descriptor = {
        "outputs": [
            {"name": "yield_value", "label": "Yield", "type": "number", "unit": "%"},
            {"name": "missing", "label": "Missing", "type": "number", "unit": "%"},
        ],
    }
    doc = {"outputs": [{"name": "yield_value", "type": "number", "value": 42.0}]}
    html = build_report(_context(descriptor=descriptor, outputs_doc=doc))
    assert "Yield" in html
    assert "Missing" not in html


# ----- Slice 5: interactive Plotly for csv_plot outputs ---------------------


def _csv_plot_descriptor() -> dict[str, object]:
    return {
        "outputs": [
            {
                "name": "trajectory",
                "label": "Trajectory",
                "type": "file",
                "filename": "trajectory.csv",
                "file_type": "csv_plot",
                "plot_title": "Position vs Time",
                "xaxis_title": "Time [s]",
                "yaxis_title": "Position [m]",
            }
        ],
    }


def _plot_payload(name: str = "trajectory") -> dict[str, str]:
    figure = {
        "data": [{"type": "scatter", "x": [0.0, 1.0], "y": [10.0, 20.0]}],
        "layout": {"title": {"text": "Position vs Time"}},
    }
    return {"figure_json": json.dumps(figure), "div_id": f"matofx-plot-{name}"}


def test_csv_plot_emits_inline_plotly_div() -> None:
    """A csv_plot output renders the figure div + Plotly.newPlot call."""
    html = build_report(
        _context(
            descriptor=_csv_plot_descriptor(),
            output_csv_plots={"trajectory.csv": _plot_payload()},
        )
    )
    assert 'id="matofx-plot-trajectory"' in html
    assert "Plotly.newPlot(" in html
    # The figure JSON the builder hands over has the descriptor's title.
    assert "Position vs Time" in html


def test_csv_plot_bundles_plotly_js_when_a_plot_is_present() -> None:
    """plotly.min.js is inlined exactly once when at least one plot is used."""
    html = build_report(
        _context(
            descriptor=_csv_plot_descriptor(),
            output_csv_plots={"trajectory.csv": _plot_payload()},
        )
    )
    # The bundled JS body always declares the Plotly global at the top.
    assert html.count("Plotly=") + html.count("Plotly =") >= 1
    # And only one <script> tag in the <head> carries the bundle —
    # the per-plot calls live inside the <body>.
    head_section = html.split("</head>", 1)[0]
    assert head_section.count("<script>") == 1


def test_report_without_plots_does_not_bundle_plotly() -> None:
    """A report with no csv_plot outputs ships zero Plotly JS."""
    html = build_report(_context())
    assert "Plotly.newPlot(" not in html
    head_section = html.split("</head>", 1)[0]
    # Only the inline <style> block; no script tags before </head>.
    assert "<script>" not in head_section


def test_csv_plot_group_is_full_width() -> None:
    """A group containing a csv_plot spans the full grid row (wide class)."""
    html = build_report(
        _context(
            descriptor=_csv_plot_descriptor(),
            output_csv_plots={"trajectory.csv": _plot_payload()},
        )
    )
    assert '<section class="group wide">' in html


def test_csv_plot_without_payload_falls_through_to_filename() -> None:
    """A descriptor csv_plot with no figure ready falls back to file_other."""
    html = build_report(_context(descriptor=_csv_plot_descriptor(), output_csv_plots={}))
    assert "trajectory.csv" in html
    assert "available in MATOFX" in html
    assert "Plotly.newPlot(" not in html


def test_html_plot_file_type_renders_as_inline_plotly() -> None:
    """An ``html_plot`` output uses the same inline Plotly path as ``csv_plot``.

    The collector extracts the figure JSON out of a ``write_html``
    dump and stashes it in ``output_csv_plots`` keyed by filename;
    the builder must therefore accept ``file_type="html_plot"`` and
    render the chart inline (not fall through to the html_embed
    iframe path, which would try to render a CDN-loaded plotly.js
    inside a sandboxed frame and end up blank on an air-gapped
    machine).
    """
    descriptor = {
        "outputs": [
            {
                "name": "field",
                "label": "Temperature field",
                "type": "file",
                "filename": "temperature_field.html",
                "file_type": "html_plot",
            }
        ]
    }
    html = build_report(
        _context(
            descriptor=descriptor,
            output_csv_plots={"temperature_field.html": _plot_payload("field")},
        )
    )
    assert 'id="matofx-plot-field"' in html
    assert "Plotly.newPlot(" in html
    # Did NOT render as an html_embed iframe.
    assert '<iframe class="html-embed"' not in html
    # And did NOT degrade to file_other.
    assert "available in MATOFX" not in html


def test_html_plot_without_extractable_figure_falls_through() -> None:
    """An ``html_plot`` whose figure couldn't be extracted lists the filename only."""
    descriptor = {
        "outputs": [
            {
                "name": "field",
                "label": "Temperature field",
                "type": "file",
                "filename": "temperature_field.html",
                "file_type": "html_plot",
            }
        ]
    }
    html = build_report(_context(descriptor=descriptor, output_csv_plots={}))
    assert "temperature_field.html" in html
    assert "available in MATOFX" in html
    assert "Plotly.newPlot(" not in html


def test_multiple_csv_plots_share_one_plotly_bundle() -> None:
    """Two charts in one report still ship one ``<script>`` Plotly bundle."""
    descriptor = {
        "outputs": [
            {
                "name": "plot_one",
                "label": "Plot one",
                "type": "file",
                "filename": "p1.csv",
                "file_type": "csv_plot",
            },
            {
                "name": "plot_two",
                "label": "Plot two",
                "type": "file",
                "filename": "p2.csv",
                "file_type": "csv_plot",
            },
        ],
    }
    html = build_report(
        _context(
            descriptor=descriptor,
            output_csv_plots={
                "p1.csv": _plot_payload("plot_one"),
                "p2.csv": _plot_payload("plot_two"),
            },
        )
    )
    assert html.count('id="matofx-plot-plot_one"') == 1
    assert html.count('id="matofx-plot-plot_two"') == 1
    head_section = html.split("</head>", 1)[0]
    assert head_section.count("<script>") == 1


def test_report_with_plots_is_still_self_contained() -> None:
    """The Plotly-bundled report has no attribute-borne external references."""
    _assert_self_contained(
        build_report(
            _context(
                descriptor=_csv_plot_descriptor(),
                output_csv_plots={"trajectory.csv": _plot_payload()},
            )
        )
    )


# ----- Slice 6: video / PDF / HTML embed / 3D listed ------------------------


def _media_descriptor(filename: str, name: str = "media") -> dict[str, object]:
    return {
        "outputs": [
            {
                "name": name,
                "label": "Media",
                "type": "file",
                "filename": filename,
            }
        ]
    }


def test_video_under_cap_embeds_as_video_tag() -> None:
    """A descriptor video whose data URI is supplied renders a ``<video>`` element."""
    data_uri = "data:video/mp4;base64,AAA"
    html = build_report(
        _context(
            descriptor=_media_descriptor("render.mp4"),
            output_videos={"render.mp4": {"data_uri": data_uri, "mime": "video/mp4"}},
        )
    )
    assert "render.mp4" in html
    assert "<video controls" in html
    assert f'src="{data_uri}"' in html
    assert 'type="video/mp4"' in html
    # The containing group spans the full row for wide media.
    assert '<section class="group wide">' in html


def test_video_over_cap_falls_through_to_file_other_with_note() -> None:
    """An oversized video drops to file_other and shows the size-note in place."""
    html = build_report(
        _context(
            descriptor=_media_descriptor("huge.mp4"),
            output_videos={},
            output_file_notes={"huge.mp4": "Not embedded — 47.3 MB, over the 25.0 MB cap"},
        )
    )
    assert "huge.mp4" in html
    assert "<video" not in html
    assert "Not embedded" in html
    assert "47.3 MB" in html


def test_pdf_output_renders_inline_embed() -> None:
    """A PDF output renders as a base64 ``<embed type="application/pdf">``."""
    data_uri = "data:application/pdf;base64,JVBE"
    html = build_report(
        _context(
            descriptor=_media_descriptor("report.pdf"),
            output_pdfs={"report.pdf": data_uri},
        )
    )
    assert "report.pdf" in html
    assert 'type="application/pdf"' in html
    assert f'src="{data_uri}"' in html
    assert '<section class="group wide">' in html


def test_html_output_renders_sandboxed_iframe() -> None:
    """An HTML output renders inside a sandboxed iframe with the data URI."""
    data_uri = "data:text/html;base64,PGh0bWw+"
    html = build_report(
        _context(
            descriptor=_media_descriptor("summary.html"),
            output_html_embeds={"summary.html": data_uri},
        )
    )
    assert "summary.html" in html
    assert "<iframe" in html
    assert "sandbox" in html
    assert f'src="{data_uri}"' in html


def test_3d_listed_with_descriptor_note() -> None:
    """A .gltf / .glb output shows the descriptor-supplied 'Not previewable' note."""
    html = build_report(
        _context(
            descriptor=_media_descriptor("model.glb"),
            output_file_notes={"model.glb": "3D animation — not previewable in report"},
        )
    )
    assert "model.glb" in html
    assert "3D animation" in html
    assert "<video" not in html
    assert "<iframe" not in html


def test_3d_listed_defaults_message_when_no_note() -> None:
    """Defensive: a 3D output with no collector note still gets the right message."""
    html = build_report(_context(descriptor=_media_descriptor("model.gltf")))
    assert "model.gltf" in html
    assert "not previewable in report" in html


def test_pdf_without_data_uri_falls_through_to_file_other() -> None:
    """A PDF descriptor with no embed payload lists filename + 'available in MATOFX'."""
    html = build_report(_context(descriptor=_media_descriptor("report.pdf"), output_pdfs={}))
    assert "report.pdf" in html
    assert 'type="application/pdf"' not in html
    assert "available in MATOFX" in html


def test_video_kind_keeps_report_self_contained() -> None:
    """The video embed uses a data URI — no attribute-borne external refs."""
    _assert_self_contained(
        build_report(
            _context(
                descriptor=_media_descriptor("clip.mp4"),
                output_videos={
                    "clip.mp4": {
                        "data_uri": "data:video/mp4;base64,AAA",
                        "mime": "video/mp4",
                    }
                },
            )
        )
    )


# ----- Slice 7: workflow context --------------------------------------------


def _heat_descriptor() -> dict[str, object]:
    return {
        "inputs": [
            {"name": "temperature", "label": "Temperature", "type": "float", "unit": "K"},
        ]
    }


def _quench_descriptor() -> dict[str, object]:
    return {
        "inputs": [
            {"name": "rate", "label": "Quench rate", "type": "float", "unit": "K/s"},
        ]
    }


def _workflow_context() -> WorkflowContext:
    return WorkflowContext(
        workflow_name="Material loop",
        run_name="Run #4",
        siblings=[
            SiblingJob(
                job_id=901,
                tool_name="Heat treatment",
                tool_alias="heat",
                is_self=False,
                descriptor=_heat_descriptor(),
                job_inputs={"temperature": 1450},
            ),
            SiblingJob(
                job_id=902,
                tool_name="Quench",
                tool_alias="quench",
                is_self=True,
                descriptor=_quench_descriptor(),
                job_inputs={"rate": 60},
            ),
        ],
    )


def test_standalone_job_hides_workflow_context_block() -> None:
    """No ``workflow_context`` → no workflow card at all.

    The ``workflow-context`` CSS class lives in the stylesheet
    regardless; we check for the *structural* card tag instead so
    the test doesn't trip on the inline rules.
    """
    html = build_report(_context())
    assert '<section class="card workflow-context">' not in html
    # The "Workflow run" heading only appears inside the card.
    assert ">Workflow run</h2>" not in html


def test_workflow_card_shows_parent_and_run_name() -> None:
    """The card surfaces both the workflow name and a distinct run name."""
    html = build_report(_context(workflow_context=_workflow_context()))
    assert '<section class="card workflow-context">' in html
    assert "Material loop" in html
    assert "Run #4" in html


def test_workflow_card_lists_each_sibling_with_inputs() -> None:
    """Each sibling renders as a ``<details>`` with its tool name + inputs."""
    html = build_report(_context(workflow_context=_workflow_context()))
    assert "Heat treatment" in html
    assert "Quench" in html
    # Both siblings' input values appear (1450 K, 60 K/s).
    assert "1450" in html
    assert ">K<" in html
    assert "60" in html
    assert "K/s" in html


def test_self_sibling_is_marked_and_pre_opened() -> None:
    """The ``is_self`` sibling gets a badge and the ``<details>`` is open by default."""
    html = build_report(_context(workflow_context=_workflow_context()))
    # Two <details>; only the self one gets `open` on it.
    assert html.count("<details open>") == 1
    assert html.count("<details>") == 1
    assert "this job" in html
    assert "self-badge" in html


def test_workflow_card_renders_above_inputs_section() -> None:
    """Card sits between the metadata card and the main Inputs section."""
    html = build_report(
        _context(
            descriptor=_ambient_descriptor(),
            job_inputs={"temperature": 300.5},
            workflow_context=_workflow_context(),
        )
    )
    workflow_pos = html.find("workflow-context")
    inputs_pos = html.find('<section class="section-heading">Inputs')
    assert 0 < workflow_pos < inputs_pos


def test_sibling_with_no_inputs_shows_placeholder() -> None:
    """A sibling whose descriptor has no populated inputs reads as 'No inputs recorded'."""
    ctx = WorkflowContext(
        workflow_name="Demo",
        run_name="Demo",
        siblings=[
            SiblingJob(
                job_id=10,
                tool_name="Empty step",
                tool_alias="empty",
                is_self=False,
                descriptor={"inputs": []},
                job_inputs={},
            )
        ],
    )
    html = build_report(_context(workflow_context=ctx))
    assert "Empty step" in html
    assert "No inputs recorded" in html


def test_workflow_card_does_not_pull_in_sibling_outputs() -> None:
    """Per ADR-0004: workflow card carries inputs only, never sibling outputs."""
    ctx = WorkflowContext(
        workflow_name="Demo",
        run_name="Demo",
        siblings=[
            SiblingJob(
                job_id=11,
                tool_name="Stepwise",
                tool_alias="stepwise",
                is_self=False,
                descriptor={
                    "inputs": [{"name": "x", "label": "X", "type": "string"}],
                    "outputs": [{"name": "y", "label": "Y", "type": "number"}],
                },
                job_inputs={"x": "alpha"},
            )
        ],
    )
    html = build_report(_context(workflow_context=ctx))
    card_start = html.find('<section class="card workflow-context">')
    card_end = html.find("</section>", card_start)
    assert card_start >= 0
    card_body = html[card_start:card_end]
    assert "Outputs" not in card_body


def test_workflow_card_keeps_report_self_contained() -> None:
    """The workflow card uses no attribute-borne external references."""
    _assert_self_contained(build_report(_context(workflow_context=_workflow_context())))

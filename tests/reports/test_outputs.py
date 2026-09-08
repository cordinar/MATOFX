"""Tests for :mod:`matofx.reports.outputs` — the router's output collector.

Pins down the I/O contract the router relies on:

- ``outputs/outputs.json`` parsed into a plain dict (or ``{}`` when
  missing / malformed).
- Image output files embedded as ``data:`` URIs (via the same
  ``inline_input_image`` path the Inputs section uses).
- CSV tables read at most ``MAX_CSV_TABLE_ROWS`` rows; anything past
  that flips ``truncated=True`` and records the real ``total_rows``.
- Paths outside the storage mount are refused — defence in depth so
  a poisoned descriptor can't leak host files.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from matofx.reports.assets import VIDEO_SIZE_CAP_BYTES
from matofx.reports.builder import MAX_CSV_TABLE_ROWS
from matofx.reports.outputs import collect_output_previews


def _make_working_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Create a job-style ``<mount>/jobs/job_0000001/outputs/`` tree."""
    mount = tmp_path / "mount"
    working = mount / "jobs" / "job_0000001"
    (working / "outputs").mkdir(parents=True)
    return mount, working


def test_returns_empty_when_nothing_on_disk(tmp_path: Path) -> None:
    """Missing ``outputs.json`` + no file outputs → all-empty result."""
    mount, working = _make_working_dir(tmp_path)
    descriptor: dict = {"outputs": []}
    doc, images, tables, plots = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    assert doc == {}
    assert images == {}
    assert tables == {}
    assert plots == {}


def test_parses_outputs_document(tmp_path: Path) -> None:
    """A valid ``outputs.json`` is returned as a plain dict."""
    mount, working = _make_working_dir(tmp_path)
    (working / "outputs" / "outputs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tool_alias": "demo",
                "outputs": [{"name": "y", "type": "number", "value": 7.5}],
            }
        ),
        encoding="utf-8",
    )
    doc, _, _, _ = collect_output_previews(
        descriptor={"outputs": []}, mount_root=mount, working_dir=working
    )
    assert doc["tool_alias"] == "demo"
    assert doc["outputs"][0]["value"] == 7.5


def test_malformed_outputs_document_returns_empty(tmp_path: Path) -> None:
    """Unparseable JSON degrades gracefully to ``{}``."""
    mount, working = _make_working_dir(tmp_path)
    (working / "outputs" / "outputs.json").write_text("not-json-at-all", encoding="utf-8")
    doc, _, _, _ = collect_output_previews(
        descriptor={"outputs": []}, mount_root=mount, working_dir=working
    )
    assert doc == {}


def test_embeds_image_output(tmp_path: Path) -> None:
    """An image output file on disk produces a ``data:`` URI keyed by filename."""
    mount, working = _make_working_dir(tmp_path)
    image_path = working / "outputs" / "thumbnail.png"
    Image.new("RGB", (16, 16), color=(0, 0, 0)).save(image_path, format="PNG")
    descriptor = {
        "outputs": [
            {
                "name": "thumbnail",
                "label": "Thumbnail",
                "type": "file",
                "filename": "thumbnail.png",
            }
        ]
    }
    _, images, _, _ = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    assert "thumbnail.png" in images
    assert images["thumbnail.png"].startswith("data:image/png;base64,")


def test_csv_table_reads_rows_and_columns(tmp_path: Path) -> None:
    """A ``file_type=csv_table`` output parses its rows + column order."""
    mount, working = _make_working_dir(tmp_path)
    csv_path = working / "outputs" / "trajectory.csv"
    csv_path.write_text("t,value\n0.0,1.2\n0.1,1.3\n", encoding="utf-8")
    descriptor = {
        "outputs": [
            {
                "name": "trajectory",
                "label": "Trajectory",
                "type": "file",
                "filename": "trajectory.csv",
                "file_type": "csv_table",
            }
        ]
    }
    _, _, tables, _ = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    assert "trajectory.csv" in tables
    payload = tables["trajectory.csv"]
    assert payload["columns"] == ["t", "value"]
    assert payload["rows"] == [
        {"t": "0.0", "value": "1.2"},
        {"t": "0.1", "value": "1.3"},
    ]
    assert payload["total_rows"] == 2
    assert payload["truncated"] is False


def test_csv_table_truncates_past_limit(tmp_path: Path) -> None:
    """CSVs above the row limit return only the first chunk + ``truncated=True``."""
    mount, working = _make_working_dir(tmp_path)
    csv_path = working / "outputs" / "big.csv"
    total = MAX_CSV_TABLE_ROWS + 50
    lines = ["t,v"] + [f"{i},{i * 2}" for i in range(total)]
    csv_path.write_text("\n".join(lines), encoding="utf-8")
    descriptor = {
        "outputs": [
            {
                "name": "trajectory",
                "label": "Trajectory",
                "type": "file",
                "filename": "big.csv",
                "file_type": "csv_table",
            }
        ]
    }
    _, _, tables, _ = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    payload = tables["big.csv"]
    assert len(payload["rows"]) == MAX_CSV_TABLE_ROWS
    assert payload["total_rows"] == total
    assert payload["truncated"] is True


def test_csv_table_uses_declared_column_order(tmp_path: Path) -> None:
    """Descriptor-declared columns win over the CSV header when both are present."""
    mount, working = _make_working_dir(tmp_path)
    csv_path = working / "outputs" / "values.csv"
    csv_path.write_text("a,b\n1,2\n", encoding="utf-8")
    descriptor = {
        "outputs": [
            {
                "name": "v",
                "label": "Values",
                "type": "file",
                "filename": "values.csv",
                "file_type": "csv_table",
                "columns": [
                    {"name": "b", "type": "integer"},
                    {"name": "a", "type": "integer"},
                ],
            }
        ]
    }
    _, _, tables, _ = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    payload = tables["values.csv"]
    assert payload["columns"] == ["b", "a"]


def test_non_image_output_file_is_silently_skipped(tmp_path: Path) -> None:
    """A non-image file output isn't added to the image-previews map."""
    mount, working = _make_working_dir(tmp_path)
    (working / "outputs" / "report.txt").write_text("hello", encoding="utf-8")
    descriptor = {
        "outputs": [
            {
                "name": "report",
                "label": "Report",
                "type": "file",
                "filename": "report.txt",
            }
        ]
    }
    _, images, tables, _ = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    assert images == {}
    assert tables == {}


def test_missing_csv_file_is_skipped(tmp_path: Path) -> None:
    """A ``csv_table`` descriptor whose file is missing doesn't crash the collector."""
    mount, working = _make_working_dir(tmp_path)
    descriptor = {
        "outputs": [
            {
                "name": "missing",
                "type": "file",
                "filename": "nope.csv",
                "file_type": "csv_table",
            }
        ]
    }
    _, _, tables, _ = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    assert tables == {}


# ----- csv_plot --------------------------------------------------------------


def test_csv_plot_builds_figure_json(tmp_path: Path) -> None:
    """A csv_plot output produces ``{figure_json, div_id}`` ready for Plotly."""
    mount, working = _make_working_dir(tmp_path)
    csv_path = working / "outputs" / "trajectory.csv"
    csv_path.write_text("t,x\n0,0\n1,2\n2,4\n", encoding="utf-8")
    descriptor = {
        "outputs": [
            {
                "name": "trajectory",
                "label": "Position",
                "type": "file",
                "filename": "trajectory.csv",
                "file_type": "csv_plot",
                "plot_title": "Position vs Time",
                "xaxis_title": "Time [s]",
                "yaxis_title": "Position [m]",
            }
        ]
    }
    _, _, _, plots = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    assert "trajectory.csv" in plots
    payload = plots["trajectory.csv"]
    assert payload["div_id"] == "matofx-plot-trajectory"
    figure = json.loads(payload["figure_json"])
    assert figure["data"][0]["x"] == [0.0, 1.0, 2.0]
    assert figure["data"][0]["y"] == [0.0, 2.0, 4.0]
    assert figure["layout"]["title"]["text"] == "Position vs Time"
    assert figure["layout"]["xaxis"]["title"]["text"] == "Time [s]"
    assert figure["layout"]["yaxis"]["title"]["text"] == "Position [m]"


def test_csv_plot_defaults_to_columns_0_and_1(tmp_path: Path) -> None:
    """No ``xaxis_column`` / ``yaxis_column`` falls back to columns 0 and 1."""
    mount, working = _make_working_dir(tmp_path)
    csv_path = working / "outputs" / "trajectory.csv"
    csv_path.write_text("t,v,extra\n0,10,99\n1,20,99\n", encoding="utf-8")
    descriptor = {
        "outputs": [
            {
                "name": "t",
                "type": "file",
                "filename": "trajectory.csv",
                "file_type": "csv_plot",
            }
        ]
    }
    _, _, _, plots = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    figure = json.loads(plots["trajectory.csv"]["figure_json"])
    assert figure["data"][0]["x"] == [0.0, 1.0]
    assert figure["data"][0]["y"] == [10.0, 20.0]


def test_csv_plot_honours_explicit_column_indices(tmp_path: Path) -> None:
    """``xaxis_column`` / ``yaxis_column`` pin which CSV columns drive the plot."""
    mount, working = _make_working_dir(tmp_path)
    csv_path = working / "outputs" / "trajectory.csv"
    csv_path.write_text("t,a,b\n0,5,50\n1,6,60\n", encoding="utf-8")
    descriptor = {
        "outputs": [
            {
                "name": "t",
                "type": "file",
                "filename": "trajectory.csv",
                "file_type": "csv_plot",
                "xaxis_column": 0,
                "yaxis_column": 2,
            }
        ]
    }
    _, _, _, plots = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    figure = json.loads(plots["trajectory.csv"]["figure_json"])
    assert figure["data"][0]["y"] == [50.0, 60.0]


def test_csv_plot_skips_columns_out_of_range(tmp_path: Path) -> None:
    """A descriptor that points at columns the CSV doesn't have skips the chart."""
    mount, working = _make_working_dir(tmp_path)
    csv_path = working / "outputs" / "narrow.csv"
    csv_path.write_text("t\n0\n1\n", encoding="utf-8")
    descriptor = {
        "outputs": [
            {
                "name": "t",
                "type": "file",
                "filename": "narrow.csv",
                "file_type": "csv_plot",
            }
        ]
    }
    _, _, _, plots = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    assert plots == {}


def test_csv_plot_renders_non_numeric_cells_as_gaps(tmp_path: Path) -> None:
    """Cells that aren't numeric become ``None`` so Plotly draws gaps."""
    mount, working = _make_working_dir(tmp_path)
    csv_path = working / "outputs" / "with_gaps.csv"
    csv_path.write_text("t,v\n0,1.0\n1,\n2,3.0\n", encoding="utf-8")
    descriptor = {
        "outputs": [
            {
                "name": "t",
                "type": "file",
                "filename": "with_gaps.csv",
                "file_type": "csv_plot",
            }
        ]
    }
    _, _, _, plots = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    figure = json.loads(plots["with_gaps.csv"]["figure_json"])
    assert figure["data"][0]["y"] == [1.0, None, 3.0]


def test_html_plot_extracts_figure_json(tmp_path: Path) -> None:
    """A ``file_type=html_plot`` Plotly write_html dump becomes a csv_plots entry."""
    mount, working = _make_working_dir(tmp_path)
    html_path = working / "outputs" / "trajectory.html"
    # Minimal shape that mirrors what plotly.io.write_html emits.
    html_path.write_text(
        '<html><body><div id="abc"></div>'
        '<script>Plotly.newPlot("abc", '
        '[{"type":"scatter","x":[0,1,2],"y":[0,1,4]}], '
        '{"title":{"text":"Demo plot"}});'
        "</script></body></html>",
        encoding="utf-8",
    )
    descriptor = {
        "outputs": [
            {
                "name": "trajectory",
                "type": "file",
                "filename": "trajectory.html",
                "file_type": "html_plot",
            }
        ]
    }
    bundle = collect_output_previews(descriptor=descriptor, mount_root=mount, working_dir=working)
    # html_plot rides the same payload shape as csv_plot.
    assert "trajectory.html" in bundle.csv_plots
    payload = bundle.csv_plots["trajectory.html"]
    figure = json.loads(payload["figure_json"])
    assert figure["data"][0]["x"] == [0, 1, 2]
    assert figure["data"][0]["y"] == [0, 1, 4]
    assert figure["layout"]["title"]["text"] == "Demo plot"
    # Light theme dropped in regardless of source.
    assert figure["layout"]["paper_bgcolor"] == "white"


def test_html_plot_falls_back_when_not_plotly(tmp_path: Path) -> None:
    """An HTML file that isn't a Plotly dump returns None — falls through to file_other."""
    mount, working = _make_working_dir(tmp_path)
    html_path = working / "outputs" / "summary.html"
    html_path.write_text("<html><body>Just text, no Plotly.</body></html>", encoding="utf-8")
    descriptor = {
        "outputs": [
            {
                "name": "summary",
                "type": "file",
                "filename": "summary.html",
                "file_type": "html_plot",
            }
        ]
    }
    bundle = collect_output_previews(descriptor=descriptor, mount_root=mount, working_dir=working)
    assert "summary.html" not in bundle.csv_plots
    assert "summary.html" not in bundle.html_embeds


def test_csv_plot_missing_file_is_skipped(tmp_path: Path) -> None:
    """A csv_plot whose CSV isn't on disk silently drops the plot."""
    mount, working = _make_working_dir(tmp_path)
    descriptor = {
        "outputs": [
            {
                "name": "missing",
                "type": "file",
                "filename": "nowhere.csv",
                "file_type": "csv_plot",
            }
        ]
    }
    _, _, _, plots = collect_output_previews(
        descriptor=descriptor, mount_root=mount, working_dir=working
    )
    assert plots == {}


# ----- Slice 6: video / PDF / HTML / 3D listing -----------------------------


def test_video_under_cap_is_embedded(tmp_path: Path) -> None:
    """An mp4 below the 25 MB cap returns a ``data:video/mp4;base64,...`` URI."""
    mount, working = _make_working_dir(tmp_path)
    target = working / "outputs" / "render.mp4"
    target.write_bytes(b"fake-mp4-bytes" * 512)  # ~7 KB
    descriptor = {
        "outputs": [
            {"name": "render", "type": "file", "filename": "render.mp4"},
        ]
    }
    bundle = collect_output_previews(descriptor=descriptor, mount_root=mount, working_dir=working)
    assert "render.mp4" in bundle.videos
    entry = bundle.videos["render.mp4"]
    assert entry["data_uri"].startswith("data:video/mp4;base64,")
    assert entry["mime"] == "video/mp4"
    assert "render.mp4" not in bundle.file_notes


def test_video_over_cap_records_size_note(tmp_path: Path) -> None:
    """An oversized mp4 surfaces a 'Not embedded' note + size; not in videos."""
    mount, working = _make_working_dir(tmp_path)
    target = working / "outputs" / "huge.mp4"
    target.write_bytes(b"\x00" * (VIDEO_SIZE_CAP_BYTES + 1024))
    descriptor = {
        "outputs": [
            {"name": "huge", "type": "file", "filename": "huge.mp4"},
        ]
    }
    bundle = collect_output_previews(descriptor=descriptor, mount_root=mount, working_dir=working)
    assert "huge.mp4" not in bundle.videos
    note = bundle.file_notes["huge.mp4"]
    assert "Not embedded" in note
    assert "25.0 MB" in note  # the cap formatted as human size


def test_pdf_output_is_embedded(tmp_path: Path) -> None:
    """A .pdf output is base64-embedded as application/pdf."""
    mount, working = _make_working_dir(tmp_path)
    target = working / "outputs" / "report.pdf"
    target.write_bytes(b"%PDF-1.4 fake")
    descriptor = {
        "outputs": [
            {"name": "report", "type": "file", "filename": "report.pdf"},
        ]
    }
    bundle = collect_output_previews(descriptor=descriptor, mount_root=mount, working_dir=working)
    assert bundle.pdfs["report.pdf"].startswith("data:application/pdf;base64,")


def test_html_output_is_embedded(tmp_path: Path) -> None:
    """A .html output gets a ``data:text/html;base64,...`` URI for iframe embed."""
    mount, working = _make_working_dir(tmp_path)
    target = working / "outputs" / "summary.html"
    target.write_text("<html><body>Hello</body></html>", encoding="utf-8")
    descriptor = {
        "outputs": [
            {"name": "summary", "type": "file", "filename": "summary.html"},
        ]
    }
    bundle = collect_output_previews(descriptor=descriptor, mount_root=mount, working_dir=working)
    assert bundle.html_embeds["summary.html"].startswith("data:text/html;base64,")


def test_3d_animation_lists_filename_with_note(tmp_path: Path) -> None:
    """``.gltf`` / ``.glb`` outputs are filename-only with a note (no viewer bundled)."""
    mount, working = _make_working_dir(tmp_path)
    (working / "outputs" / "model.glb").write_bytes(b"fake-glb")
    descriptor = {
        "outputs": [
            {"name": "model", "type": "file", "filename": "model.glb"},
        ]
    }
    bundle = collect_output_previews(descriptor=descriptor, mount_root=mount, working_dir=working)
    assert bundle.videos == {}
    assert bundle.pdfs == {}
    assert "model.glb" in bundle.file_notes
    assert "3D animation" in bundle.file_notes["model.glb"]


def test_unknown_video_extension_falls_through(tmp_path: Path) -> None:
    """A .mkv (not in our MIME map) doesn't end up in any preview bucket."""
    mount, working = _make_working_dir(tmp_path)
    (working / "outputs" / "render.mkv").write_bytes(b"fake")
    descriptor = {
        "outputs": [
            {"name": "render", "type": "file", "filename": "render.mkv"},
        ]
    }
    bundle = collect_output_previews(descriptor=descriptor, mount_root=mount, working_dir=working)
    assert bundle.videos == {}
    assert "render.mkv" not in bundle.file_notes

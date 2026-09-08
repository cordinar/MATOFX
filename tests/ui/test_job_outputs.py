"""Unit tests for the helpers behind the descriptor-driven job result page.

The visual rendering itself runs inside a NiceGUI page context that the
ASGI test transport can't fully drive — but the helper functions are
pure and easy to exercise. A mis-classification, a wrong precision
rounding, or a broken value-lookup would surface here before the user
sees a blank panel.
"""

from __future__ import annotations

import inspect
from typing import Any

import plotly.graph_objects as go
import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

import matofx.ui.theme as theme_module
from matofx.ui.pages import job as job_page_module
from matofx.ui.pages.job import (
    _build_io_summary,
    _extract_plotly_figure,
    _field_animation_figure,
    _field_animation_filename,
    _flex_weights_from_layout,
    _format_input_value,
    _format_number,
    _format_timestamp,
    _normalise_output_path,
    _outputs_value_lookup,
    _render_typed_output,
    detect_output_kind,
    support_badge,
)
from matofx.ui.theme import export_plotly_html


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("plot.html", "html"),
        ("plot.HTML", "html"),
        ("report.htm", "html"),
        ("heatmap.png", "image"),
        ("photo.JPG", "image"),
        ("photo.jpeg", "image"),
        ("anim.gif", "image"),
        ("vector.svg", "image"),
        ("modern.webp", "image"),
        ("scan.bmp", "image"),
        ("report.pdf", "pdf"),
        ("REPORT.PDF", "pdf"),
        ("scene.gltf", "animation_3d"),
        ("scene.glb", "animation_3d"),
        ("clip.mp4", "video"),
        ("clip.WEBM", "video"),
        ("clip.ogg", "video"),
        ("clip.ogv", "video"),
        ("clip.mov", "video"),
        ("clip.m4v", "video"),
        ("results.csv", "download"),
        ("data.json", "download"),
        ("notes.txt", "download"),
        ("archive.tar.gz", "download"),
        ("README", "download"),
        ("noext.", "download"),
    ],
)
def test_detect_output_kind(filename: str, expected: str) -> None:
    assert detect_output_kind(filename) == expected


def test_normalise_output_path_bare_filename() -> None:
    """A bare filename (e.g. a video) is served + saved under its own name.

    Regression: the ``.mp4`` (and PDF/CAD/image) download links were bare
    ``<a href>`` to the bearer-only outputs endpoint and 401'd; every output
    download now goes through ``_output_download_link`` (fetch-then-
    ui.download.content), which normalises the path here.
    """
    assert _normalise_output_path("clip.mp4") == ("clip.mp4", "clip.mp4")


def test_normalise_output_path_drops_traversal_segments() -> None:
    """``..`` segments are stripped so the path can't escape the outputs dir."""
    assert _normalise_output_path("../etc/passwd") == ("etc/passwd", "passwd")


def test_normalise_output_path_strips_redundant_outputs_prefix() -> None:
    """The field tools' ``outputs/``-prefixed filename is normalised away."""
    assert _normalise_output_path("outputs/temperature_0000.vtp") == (
        "temperature_0000.vtp",
        "temperature_0000.vtp",
    )


def test_normalise_output_path_keeps_nested_separators() -> None:
    """Nested paths keep real ``/`` separators; the basename is the save name."""
    assert _normalise_output_path("outputs/sub/dir/file.csv") == (
        "sub/dir/file.csv",
        "file.csv",
    )


# ---------- transient field animation download -------------------------------


def test_field_animation_figure_has_one_frame_per_step_plus_controls() -> None:
    """The animation figure carries every timestep + play/pause + a step slider."""
    data = {"has_geometry": False}
    frames = [[1.0, 2.0, 3.0], [2.0, 3.0, 4.0], [3.0, 4.0, 5.0]]
    fig = _field_animation_figure(data, frames, "Temperature", (1.0, 5.0))
    assert len(fig.frames) == len(frames)
    labels = {b.label for b in fig.layout.updatemenus[0].buttons}
    assert labels == {"▶ Play", "⏸ Pause"}
    assert len(fig.layout.sliders[0].steps) == len(frames)
    # Controls sit in the bottom margin, not above the plot where they used to
    # overlap the title; the margin is roomy so step labels aren't clipped.
    assert fig.layout.updatemenus[0].y == 0
    assert fig.layout.sliders[0].y == 0
    assert fig.layout.margin.b >= 100


def test_field_animation_exports_to_self_contained_html() -> None:
    """The whole series exports as one openable HTML animation (frames embedded)."""
    data = {"has_geometry": False}
    frames = [[1.0, 2.0], [2.0, 3.0]]
    # Explicit theme so the export doesn't reach for NiceGUI's per-user storage.
    html = export_plotly_html(_field_animation_figure(data, frames, "T", None), theme="light")
    assert b"Plotly" in html
    assert len(html) > 500


def test_field_animation_filename_drops_trailing_frame_index() -> None:
    """The download name reads as the field, not 'frame zero'."""
    assert _field_animation_filename("outputs/temperature_0000.vtp") == "temperature_animation.html"
    assert _field_animation_filename("field.npy") == "field_animation.html"


# ---------- value lookup -----------------------------------------------------


def test_outputs_value_lookup_handles_empty_doc() -> None:
    assert _outputs_value_lookup(None) == {}
    assert _outputs_value_lookup({}) == {}
    assert _outputs_value_lookup({"outputs": "not a list"}) == {}  # type: ignore[arg-type]


def test_outputs_value_lookup_indexes_by_name() -> None:
    doc = {
        "outputs": [
            {"name": "T_core", "value": 18.5, "unit": "degC"},
            {"name": "plot", "filename": "temperature_field.html"},
        ]
    }
    lookup = _outputs_value_lookup(doc)
    assert lookup["T_core"]["value"] == 18.5
    assert lookup["plot"]["filename"] == "temperature_field.html"


def test_outputs_value_lookup_skips_entries_without_name() -> None:
    """Defensive: an entry missing ``name`` shouldn't crash the page."""
    doc = {"outputs": [{"value": 1}, {"name": "ok", "value": 2}]}
    assert _outputs_value_lookup(doc) == {"ok": {"name": "ok", "value": 2}}


# ---------- number formatting ------------------------------------------------


@pytest.mark.parametrize(
    ("value", "precision", "expected"),
    [
        (18.567, 0.01, "18.57"),
        (18.5, 0.1, "18.5"),
        (18.0, 1, "18"),
        (None, 0.01, "—"),
        ("not a number", 0.01, "not a number"),
        # No declared precision → 4 significant figures, not the raw
        # repr. That is deliberate ("sensible default" in `_format_number`)
        # and stops an undeclared output rendering as 0.30000000000000004.
        # These two rows previously expected passthrough and had therefore
        # never passed since the initial commit.
        (3.14159, None, "3.142"),
        (3.14159, 0, "3.142"),
    ],
)
def test_format_number(value: object, precision: float | None, expected: str) -> None:
    assert _format_number(value, precision) == expected


# ---------- input-value formatting ------------------------------------------


@pytest.mark.parametrize(
    ("value", "field_type", "expected"),
    [
        (None, "float", "—"),
        ("", "string", "—"),
        (True, "boolean", "Yes"),
        (False, "boolean", "No"),
        ([], "multiselect", "(none)"),
        (["a", "b"], "multiselect", "a, b"),
        ("/jobs/inputs/photo.png", "file", "photo.png"),
        ("C:\\Users\\x\\inputs\\photo.png", "file", "photo.png"),
        (42, "integer", "42"),
        (3.14, "float", "3.14"),
    ],
)
def test_format_input_value(value: object, field_type: str, expected: str) -> None:
    assert _format_input_value(value, field_type) == expected


# ---------- flex-weight calculation -----------------------------------------


def test_flex_weights_default_to_even_split() -> None:
    """No layout → equal weights."""
    assert _flex_weights_from_layout({}, 2) == [0.5, 0.5]
    assert _flex_weights_from_layout({}, 4) == [0.25] * 4


def test_flex_weights_honour_layout() -> None:
    layout = {"column_1": 0.2, "column_2": 0.8}
    assert _flex_weights_from_layout(layout, 2) == [0.2, 0.8]


def test_flex_weights_clamp_too_small() -> None:
    """A near-zero weight is clamped to keep the column visible."""
    weights = _flex_weights_from_layout({"column_1": 0.01, "column_2": 0.99}, 2)
    assert weights[0] >= 0.05


def test_flex_weights_single_column_fills_the_width() -> None:
    """One column always gets the whole row, whatever weight it carries.

    A tool whose output groups collapsed back to one column kept the
    stale ``column_2`` weight in its descriptor, so ``column_1`` still
    read ``0.5`` — and a lone flex item with grow 0.5 takes half the
    free space, leaving the Outputs panel half empty.
    """
    assert _flex_weights_from_layout({"column_1": 0.5, "column_2": 0.5}, 1) == [1.0]
    assert _flex_weights_from_layout({"column_1": 1.0}, 1) == [1.0]
    assert _flex_weights_from_layout({}, 1) == [1.0]


def test_flex_weights_normalise_to_fill_the_row() -> None:
    """Weights that don't sum to 1 keep their ratio but fill the width."""
    weights = _flex_weights_from_layout({"column_1": 2.0, "column_2": 1.0}, 2)
    assert weights == pytest.approx([2 / 3, 1 / 3])
    assert sum(weights) == pytest.approx(1.0)


# ---------- I/O summary -----------------------------------------------------


def test_build_io_summary_glues_descriptor_inputs_outputs() -> None:
    descriptor = {
        "inputs": [
            {"name": "T", "label": "Temperature", "unit": "degC", "type": "float"},
            {"name": "fuel", "label": "Fuel", "unit": "", "type": "string"},
        ],
        "outputs": [
            {
                "name": "T_core",
                "label": "Core temp",
                "type": "number",
                "unit": "degC",
                "precision": 0.01,
            },
            {
                "name": "plot",
                "label": "Plot",
                "type": "file",
                "file_type": "html_plot",
                "filename": "temperature_field.html",
            },
        ],
    }
    job_inputs = {"T": 25, "fuel": "diesel"}
    outputs_doc = {
        "outputs": [
            {"name": "T_core", "value": 18.567},
            {"name": "plot", "filename": "temperature_field.html"},
        ]
    }
    summary = _build_io_summary(descriptor, job_inputs, outputs_doc)
    assert summary == {
        "inputs": {"T": "25 degC", "fuel": "diesel"},
        "outputs": {"T_core": "18.57 degC", "plot": "temperature_field.html"},
    }


def test_build_io_summary_handles_missing_output_doc() -> None:
    """The summary still renders if outputs.json is missing — number values
    just fall through to an empty string instead of a rounded value."""
    descriptor = {
        "inputs": [{"name": "T", "label": "T", "unit": "degC", "type": "float"}],
        "outputs": [{"name": "x", "type": "number", "unit": "K", "precision": 1}],
    }
    summary = _build_io_summary(descriptor, {"T": 25}, None)
    assert summary["inputs"] == {"T": "25 degC"}
    # Number with no value → "—" (the default formatter result).
    assert summary["outputs"] == {"x": "— K"}


# ---------- plotly HTML extraction ------------------------------------------


def test_extract_plotly_figure_round_trips_a_real_write_html() -> None:
    """Generate a plotly HTML and prove we can pull data + layout back out."""
    fig = go.Figure(data=go.Heatmap(z=[[1, 2, 3], [4, 5, 6]]))
    fig.update_layout(title="Sample heatmap")
    html_text = fig.to_html(include_plotlyjs="cdn", full_html=True)

    extracted = _extract_plotly_figure(html_text)

    assert extracted is not None
    assert isinstance(extracted["data"], list)
    assert extracted["data"][0]["type"] == "heatmap"
    assert extracted["data"][0]["z"] == [[1, 2, 3], [4, 5, 6]]
    # Title round-trips. Plotly normalises the value into ``{'text': ...}``
    # — accept either shape so the test isn't pinned to a specific
    # plotly minor version's serialisation.
    title = extracted["layout"].get("title")
    title_text = title["text"] if isinstance(title, dict) else title
    assert title_text == "Sample heatmap"


def test_extract_plotly_figure_returns_none_for_non_plotly_html() -> None:
    """A page without a ``Plotly.newPlot(...)`` call → ``None`` (caller falls back)."""
    assert _extract_plotly_figure("<html><body>just a page</body></html>") is None
    assert _extract_plotly_figure("") is None
    # Truncated marker — newPlot call missing closing brackets.
    assert _extract_plotly_figure("Plotly.newPlot('id', [{") is None


# ---------- meta-panel timestamp formatting ----------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("2026-05-06T08:30:42.123456+00:00", "2026-05-06 08:30"),
        ("2026-05-06T08:30:42", "2026-05-06 08:30"),
        # Anything not in ISO 8601 falls through unchanged.
        ("2026-05-06", "2026-05-06"),
        ("yesterday", "yesterday"),
    ],
)
def test_format_timestamp(value: object, expected: str) -> None:
    assert _format_timestamp(value) == expected


# --------------------------------------------------------------------------
# The predict-time support badge (ADR-0020 §8)
# --------------------------------------------------------------------------


def test_string_outputs_have_a_renderer_at_all() -> None:
    """``string`` has been a legal ``OutputFieldType`` since ADR-0008, but the
    job page dispatched only number / table / file — so every string output
    rendered as "(unsupported output type 'string')".

    An exported surrogate declares ``support_status`` as exactly that, which
    meant the extrapolation guard's verdict displayed as an error message in
    the one place a prediction reaches a person.
    """
    source = inspect.getsource(_render_typed_output)
    assert 'type_ == "string"' in source


@pytest.mark.parametrize(
    ("status", "expected_label", "expected_colour"),
    [
        ("inside", "Well supported", "positive"),
        ("edge", "Near the edge", "warning"),
        ("outside", "Outside the trained range", "negative"),
    ],
)
def test_each_support_status_gets_its_own_badge(
    status: str, expected_label: str, expected_colour: str
) -> None:
    """The three verdicts are not interchangeable text — "outside the trained
    range" has to look different from "well supported" at a glance."""
    badge = support_badge({"value": status, "note": "because reasons"})
    assert badge is not None
    label, colour, note = badge
    assert label == expected_label
    assert colour == expected_colour
    assert note == "because reasons"


def test_the_badge_names_the_offending_input() -> None:
    """ "Outside the trained range" on its own is not something anybody can act
    on; the engine's reason names the input, and that is the part shown."""
    badge = support_badge(
        {
            "value": "outside",
            "note": "'thickness' is 42, outside the 1 to 8 range this model was "
            "trained to be trusted over.",
        }
    )
    assert badge is not None
    assert "thickness" in badge[2]


@pytest.mark.parametrize(
    "entry",
    [{}, {"value": None}, {"value": ""}],
    ids=["no-entry", "null-value", "empty-value"],
)
def test_no_verdict_draws_no_badge(entry: dict[str, object]) -> None:
    """A version fitted before coverage existed carries no verdict, and an
    empty badge is worse than none: it invites "blank means fine"."""
    assert support_badge(entry) is None


def test_a_status_from_a_newer_release_degrades_rather_than_misleading() -> None:
    """An unrecognised status must not borrow another one's colour."""
    badge = support_badge({"value": "something_new"})
    assert badge is not None
    label, colour, _note = badge
    assert label == "Not assessed"
    assert colour not in {"positive", "warning", "negative"}


def test_a_missing_reason_still_says_something_useful() -> None:
    """The runtime writes a reason, but a hand-rolled or older outputs
    document may not."""
    badge = support_badge({"value": "outside"})
    assert badge is not None
    assert badge[2].strip()


# --------------------------------------------------------------------------
# Every rendered file is a file you can take away
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "renderer",
    [
        job_page_module._render_html_plot,
        job_page_module._render_csv_plot,
        job_page_module._render_csv_table,
        job_page_module._render_image_file,
        job_page_module._render_cad_file_output,
        job_page_module._render_pdf_output,
        job_page_module._render_animation_3d_output,
        job_page_module._render_video_output,
        job_page_module._render_field_output,
        job_page_module._render_table_output,
    ],
    ids=lambda f: f.__name__,
)
def test_every_file_renderer_offers_the_file_itself(renderer: object) -> None:
    """A preview is not the file.

    Images rendered inline with no way to save them, and a CSV plot offered
    only as a re-exported chart — so the numbers behind the picture could be
    read on screen and never taken off it. Every ``file_type`` renderer now
    ends on :func:`_output_download_link`, the one bearer-aware download
    path; this pins that so the next renderer added doesn't quietly skip it.
    """
    source = inspect.getsource(renderer)  # type: ignore[arg-type]
    assert "_output_download_link" in source or "_field_download_link" in source


def test_csv_plot_offers_the_data_not_only_the_picture() -> None:
    """The chart is downloadable from Plotly's own camera button; what the
    page has to add is the CSV the chart was drawn from."""
    source = inspect.getsource(job_page_module._render_csv_plot)
    assert "_output_download_link(client, job_id, filename)" in source


def test_transient_field_offers_the_raw_file_beside_the_animation() -> None:
    """A time-resolved field used to offer only the exported HTML animation,
    which left the underlying field data with no way off the page."""
    source = inspect.getsource(job_page_module._render_field_output)
    animation_at = source.index("Download animation (HTML)")
    # The raw-file link has to sit in the same branch as the animation button,
    # not only in the single-frame ``else``.
    assert "_output_download_link(client, job_id, filename)" in source[:animation_at]


def test_input_image_preview_is_downloadable() -> None:
    """Inputs are rendered on this page too — an image input previewed inline
    is as much a rendered file as an image output."""
    source = inspect.getsource(job_page_module._render_input_row)
    assert "ui.download.content" in source


# --------------------------------------------------------------------------
# ...and a render pass proving the button is actually in the page
# --------------------------------------------------------------------------


class _StubClient:
    """Minimal stand-in for :class:`ApiClient` — just the one fetch."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def download_job_output(self, _job_id: int, _filename: str) -> bytes:
        return self.payload


@pytest.fixture
def render_slot() -> Any:
    """A container the renderers can build into, with no browser."""

    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__job_output_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


def _button_texts(container: Any) -> list[str]:
    return [
        child.text
        for child in container.descendants()
        if isinstance(child, ui.button) and getattr(child, "text", None)
    ]


async def test_an_image_output_renders_with_a_download_button(render_slot: Any) -> None:
    """The reported defect, at the level a user meets it: a picture on the page
    and no way to save it."""
    stub = _StubClient(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    with render_slot:
        await job_page_module._render_image_file(
            stub,  # type: ignore[arg-type]
            7,
            "outputs/temperature_map.png",
            "Temperature map",
        )
    assert any("temperature_map.png" in text for text in _button_texts(render_slot))


async def test_a_csv_plot_renders_with_both_the_data_and_the_chart(
    render_slot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chart is drawn *from* a file; that file has to be reachable."""
    # The theme lookup reads per-user storage a bare Client has not; the
    # buttons are what is under test, not the tint.
    monkeypatch.setattr(theme_module, "apply_theme_to_plotly", lambda figure, _theme=None: figure)
    stub = _StubClient(b"time,temperature\n0,20\n1,25\n2,31\n")
    with render_slot:
        await job_page_module._render_csv_plot(
            stub,  # type: ignore[arg-type]
            7,
            "history.csv",
            {"xaxis_column": 0, "yaxis_column": 1},
            "Temperature history",
        )
    texts = _button_texts(render_slot)
    assert any("history.csv" in text for text in texts)
    assert any("HTML" in text for text in texts)

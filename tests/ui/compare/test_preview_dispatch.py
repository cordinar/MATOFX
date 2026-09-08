"""Unit tests for the compare-grid preview pre-loader's classifier.

The actual three.js / iframe rendering needs a NiceGUI page context;
:func:`matofx.ui.pages.compare.page._is_previewable_output` is pure
and tells us up-front which outputs the page will pre-fetch — a
regression here would silently drop new PDF / animation / video
previews back to bare filenames.
"""

from __future__ import annotations

import pytest

from matofx.ui.pages.compare.page import (
    _RICH_PREVIEW_EXTENSIONS_FOR_PRELOAD,
    _is_previewable_output,
)


@pytest.mark.parametrize(
    ("file_type", "extension", "expected"),
    [
        # Explicit descriptor hints — always honoured.
        ("html_plot", "html", True),
        ("csv_table", "csv", True),
        ("image", "png", True),
        ("pdf", "pdf", True),
        ("video", "mp4", True),
        # cad_file / animation_3d still gate on extension because
        # unsupported sub-formats fall back to a placeholder.
        ("cad_file", "stl", True),
        ("cad_file", "x_t", False),
        ("animation_3d", "glb", True),
        ("animation_3d", "gltf", True),
        ("animation_3d", "fbx", False),
        # csv_plot rides the overlay path, not the cell pre-load.
        ("csv_plot", "csv", False),
        # No file_type → extension fallback covers every rich kind.
        ("", "png", True),
        ("", "pdf", True),
        ("", "glb", True),
        ("", "mp4", True),
        ("", "webm", True),
        ("", "stl", True),
        ("", "txt", False),
    ],
)
def test_is_previewable_output(file_type: str, extension: str, expected: bool) -> None:
    assert _is_previewable_output(file_type, extension) is expected


def test_rich_preview_union_covers_pdf_anim3d_video() -> None:
    """Sanity-check the union set so the input-preload path picks up
    every new extension when ``_load_input_preview_blobs`` filters
    against it."""
    assert {"pdf", "glb", "gltf", "mp4", "webm", "ogg", "mov"}.issubset(
        _RICH_PREVIEW_EXTENSIONS_FOR_PRELOAD
    )

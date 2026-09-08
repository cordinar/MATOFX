"""Contract tests for the rich-output renderer helper functions.

The PDF / GLTF / video helpers in :mod:`matofx.ui.cad_preview` all
accept either ``content`` (bytes) or ``src_url`` (str), exactly one
of the two. The actual rendering needs a NiceGUI page context, but
the parameter-validation branch can be exercised in plain Python —
a regression here would let callers silently pass both or neither
and get unpredictable output.
"""

from __future__ import annotations

import pytest

from matofx.ui.cad_preview import (
    render_gltf_preview,
    render_pdf_preview,
    render_video_preview,
)


@pytest.mark.parametrize(
    ("kwargs"),
    [
        # both missing
        {},
        # both provided
        {"content": b"x", "src_url": "/x"},
    ],
)
def test_render_pdf_preview_requires_exactly_one_source(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        render_pdf_preview(**kwargs)


@pytest.mark.parametrize(
    ("kwargs"),
    [
        {},
        {"content": b"x", "src_url": "/x"},
    ],
)
def test_render_gltf_preview_requires_exactly_one_source(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        render_gltf_preview(**kwargs)


@pytest.mark.parametrize(
    ("kwargs"),
    [
        {"extension": "mp4"},
        {"content": b"x", "src_url": "/x", "extension": "mp4"},
    ],
)
def test_render_video_preview_requires_exactly_one_source(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        render_video_preview(**kwargs)

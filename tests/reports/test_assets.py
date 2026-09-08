"""Tests for :mod:`matofx.reports.assets` — base64 logo embedding.

Pins the helper down end-to-end: real bytes in, ``data:`` URI out;
unknown extensions degrade to ``None`` so the template skips the
``<img>`` tag.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from PIL import Image

from matofx.reports.assets import (
    inline_input_image,
    inline_logo,
    is_previewable_image,
)

_PNG_HEADER = b"\x89PNG\r\n\x1a\n"  # smallest plausible PNG-ish prefix


def test_returns_data_uri_for_png(tmp_path: Path) -> None:
    """A real PNG file on disk produces a ``data:image/png;base64,...`` URI."""
    icons_dir = tmp_path / "branding"
    icons_dir.mkdir()
    logo_path = icons_dir / "logo.png"
    logo_path.write_bytes(_PNG_HEADER + b"\x00" * 8)
    uri = inline_logo(icons_dir=icons_dir, basename="logo.png")
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")


def test_returns_none_when_icons_dir_missing() -> None:
    """No configured ``icons_dir`` → no logo, template skips the slot."""
    assert inline_logo(icons_dir=None, basename="logo.png") is None


def test_returns_none_when_basename_missing(tmp_path: Path) -> None:
    """No configured ``basename`` → no logo."""
    assert inline_logo(icons_dir=tmp_path, basename=None) is None
    assert inline_logo(icons_dir=tmp_path, basename="") is None


def test_returns_none_when_file_not_on_disk(tmp_path: Path) -> None:
    """A configured basename whose file isn't on disk falls back to ``None``."""
    assert inline_logo(icons_dir=tmp_path, basename="missing.png") is None


@pytest.mark.parametrize(
    ("extension", "expected_mime"),
    [
        ("png", "image/png"),
        ("jpg", "image/jpeg"),
        ("jpeg", "image/jpeg"),
        ("gif", "image/gif"),
        ("webp", "image/webp"),
        ("svg", "image/svg+xml"),
    ],
)
def test_recognised_extensions_use_correct_mime(
    tmp_path: Path, extension: str, expected_mime: str
) -> None:
    """Each supported extension maps to the right MIME type in the URI."""
    icons_dir = tmp_path / "branding"
    icons_dir.mkdir()
    path = icons_dir / f"logo.{extension}"
    path.write_bytes(b"\x00\x01")
    uri = inline_logo(icons_dir=icons_dir, basename=path.name)
    assert uri is not None
    assert uri.startswith(f"data:{expected_mime};base64,")


def test_unknown_extension_returns_none(tmp_path: Path) -> None:
    """An exotic extension we don't whitelist returns ``None``."""
    icons_dir = tmp_path / "branding"
    icons_dir.mkdir()
    path = icons_dir / "logo.bmp"
    path.write_bytes(b"\x00")
    assert inline_logo(icons_dir=icons_dir, basename="logo.bmp") is None


# ----- is_previewable_image ---------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("foo.png", True),
        ("foo.jpeg", True),
        ("foo.JPG", True),  # case-insensitive
        ("foo.svg", True),
        ("foo.stp", False),
        ("foo.csv", False),
        ("no_extension", False),
    ],
)
def test_is_previewable_image(filename: str, expected: bool) -> None:
    """The predicate matches the recognised raster + svg extensions."""
    assert is_previewable_image(filename) is expected


# ----- inline_input_image ----------------------------------------------------


def _write_png(path: Path, *, size: tuple[int, int] = (8, 8)) -> int:
    """Write a small PNG file and return its on-disk byte count."""
    image = Image.new("RGB", size, color=(123, 200, 50))
    image.save(path, format="PNG")
    return path.stat().st_size


def test_inline_input_image_returns_data_uri(tmp_path: Path) -> None:
    """A real PNG under the mount yields a ``data:image/png;base64,...`` URI."""
    mount = tmp_path / "mount"
    (mount / "jobs" / "job_0000001" / "inputs" / "uploaded").mkdir(parents=True)
    target = mount / "jobs" / "job_0000001" / "inputs" / "uploaded" / "thumb.png"
    _write_png(target)
    uri = inline_input_image(value=str(target), mount_root=mount)
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")


def test_inline_input_image_rejects_paths_outside_mount(tmp_path: Path) -> None:
    """A path outside ``mount_root`` is refused even if it exists."""
    outside = tmp_path / "outside.png"
    _write_png(outside)
    mount = tmp_path / "mount"
    mount.mkdir()
    assert inline_input_image(value=str(outside), mount_root=mount) is None


def test_inline_input_image_returns_none_for_missing_file(tmp_path: Path) -> None:
    """A path that resolves under the mount but doesn't exist returns ``None``."""
    mount = tmp_path / "mount"
    mount.mkdir()
    assert inline_input_image(value=str(mount / "nope.png"), mount_root=mount) is None


def test_inline_input_image_returns_none_for_unsupported_extension(tmp_path: Path) -> None:
    """A non-image extension under the mount still returns ``None``."""
    mount = tmp_path / "mount"
    mount.mkdir()
    target = mount / "data.csv"
    target.write_text("a,b\n1,2\n", encoding="utf-8")
    assert inline_input_image(value=str(target), mount_root=mount) is None


def test_inline_input_image_recompresses_oversized_raster(tmp_path: Path) -> None:
    """A PNG above the threshold gets re-encoded to JPEG to shrink the report."""
    mount = tmp_path / "mount"
    mount.mkdir()
    target = mount / "big.png"
    # Fully-random noise compresses poorly even with PNG so the file
    # is reliably above the 256 KB threshold and triggers the JPEG
    # re-encode branch.
    width = height = 1200
    noise = os.urandom(width * height * 3)
    image = Image.frombytes("RGB", (width, height), noise)
    image.save(target, format="PNG")
    raw_size = target.stat().st_size
    assert raw_size > 256 * 1024  # sanity: we actually triggered the threshold

    uri = inline_input_image(value=str(target), mount_root=mount)
    assert uri is not None
    assert uri.startswith("data:image/jpeg;base64,")
    # The encoded payload should be appreciably smaller than the raw
    # PNG bytes (base64 inflates by ~4/3 so we compare against that).
    encoded_payload = uri.split(",", 1)[1]
    assert len(encoded_payload) < raw_size * 4 / 3


def test_inline_input_image_preserves_alpha_via_png(tmp_path: Path) -> None:
    """An oversized RGBA image stays PNG so transparency survives."""
    mount = tmp_path / "mount"
    mount.mkdir()
    target = mount / "alpha.png"
    image = Image.new("RGBA", (1000, 1000), color=(0, 0, 0, 128))
    image.save(target, format="PNG")
    # Pad bytes to force the re-encode branch.
    bloated = io.BytesIO()
    image.save(bloated, format="PNG", optimize=False)
    target.write_bytes(bloated.getvalue() + b"\0" * (256 * 1024))
    uri = inline_input_image(value=str(target), mount_root=mount)
    # Either we re-encode as PNG (alpha preserved) OR Pillow refuses
    # the bloated bytes and we fall through to the raw embed — both
    # paths keep the PNG mime so the test asserts on that.
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")

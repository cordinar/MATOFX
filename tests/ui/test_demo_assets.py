"""Unit tests for the demo-asset filesystem resolver.

The NiceGUI page handlers in :mod:`matofx.ui.pages.demo` need a NiceGUI
page context to drive; the resolver helpers are pure filesystem +
env-var glue and easy to exercise. Failures here would manifest as
silently-empty pickers, accidental traversal into a user's home
directory, or the env-var override not taking effect.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from matofx.ui.pages._demo_assets import (
    CATEGORY_ANIM3D,
    CATEGORY_CAD,
    CATEGORY_IMAGES,
    CATEGORY_PDF,
    DEMO_STATIC_URL_PREFIX,
    demo_file_url,
    list_files,
    mount_demo_static_route,
    read_file,
    resolve_demo_root,
)


def _seed_demo_root(tmp_path: Path) -> Path:
    """Populate a temp dir with the five subfolders and a couple of files."""
    root = tmp_path / "demo_assets"
    for sub in ("cad", "images", "pdf", "animation_3d", "video"):
        (root / sub).mkdir(parents=True)
    (root / "cad" / "cube.stl").write_bytes(b"solid cube\nendsolid cube\n")
    (root / "cad" / "ignored.txt").write_text("not a model")
    (root / "cad" / ".hidden.stl").write_bytes(b"hidden")
    (root / "images" / "logo.png").write_bytes(b"\x89PNG fake")
    (root / "images" / "photo.JPG").write_bytes(b"\xff\xd8 fake")
    (root / "pdf" / "report.pdf").write_bytes(b"%PDF-1.7 fake")
    return root


def test_resolve_uses_env_var_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _seed_demo_root(tmp_path)
    monkeypatch.setenv("MATOFX_DEMO_ASSETS_DIR", str(root))
    assert resolve_demo_root() == root.resolve()


def test_resolve_strips_whitespace_from_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user pasting a path with stray whitespace shouldn't break the resolver."""
    root = _seed_demo_root(tmp_path)
    monkeypatch.setenv("MATOFX_DEMO_ASSETS_DIR", f"  {root}  ")
    assert resolve_demo_root() == root.resolve()


def test_resolve_empty_env_var_falls_through_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty / whitespace-only env var = unset, not "use empty string"."""
    monkeypatch.setenv("MATOFX_DEMO_ASSETS_DIR", "   ")
    # Default path resolution is allowed to return any concrete path —
    # we just assert it didn't pick up the empty string and crash.
    result = resolve_demo_root()
    assert isinstance(result, Path)
    assert str(result) != ""


def test_list_files_skips_wrong_ext_and_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_demo_root(tmp_path)
    monkeypatch.setenv("MATOFX_DEMO_ASSETS_DIR", str(root))
    cad_files = list_files(CATEGORY_CAD)
    assert cad_files == ["cube.stl"], cad_files


def test_list_files_case_insensitive_extension_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.JPG`` (uppercase on disk) should still match the ``jpg`` extension."""
    root = _seed_demo_root(tmp_path)
    monkeypatch.setenv("MATOFX_DEMO_ASSETS_DIR", str(root))
    images = list_files(CATEGORY_IMAGES)
    assert sorted(images, key=str.lower) == ["logo.png", "photo.JPG"]


def test_list_files_returns_empty_for_missing_subfolder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty list, not an exception, when the subfolder doesn't exist —
    the page needs to render an empty-state."""
    root = tmp_path / "demo_assets"
    root.mkdir()
    # No subfolders created.
    monkeypatch.setenv("MATOFX_DEMO_ASSETS_DIR", str(root))
    assert list_files(CATEGORY_PDF) == []


def test_read_file_round_trips_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _seed_demo_root(tmp_path)
    monkeypatch.setenv("MATOFX_DEMO_ASSETS_DIR", str(root))
    content = read_file(CATEGORY_CAD, "cube.stl")
    assert content == b"solid cube\nendsolid cube\n"


@pytest.mark.parametrize(
    "bad_name",
    ["", "../etc/passwd", "..", ".", "sub/file.stl", "sub\\file.stl"],
)
def test_read_file_rejects_non_basenames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_name: str,
) -> None:
    root = _seed_demo_root(tmp_path)
    monkeypatch.setenv("MATOFX_DEMO_ASSETS_DIR", str(root))
    with pytest.raises(ValueError):
        read_file(CATEGORY_CAD, bad_name)


def test_read_file_raises_on_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _seed_demo_root(tmp_path)
    monkeypatch.setenv("MATOFX_DEMO_ASSETS_DIR", str(root))
    with pytest.raises(FileNotFoundError):
        read_file(CATEGORY_PDF, "missing.pdf")


def test_demo_file_url_url_quotes_filename() -> None:
    """Spaces / non-ASCII characters in the filename are URL-encoded."""
    url = demo_file_url(CATEGORY_PDF, "annual report (final).pdf")
    assert url == (f"{DEMO_STATIC_URL_PREFIX}/pdf/annual%20report%20%28final%29.pdf")


def test_demo_file_url_uses_root_relative_prefix() -> None:
    """URL must be absolute (leading slash) so it bypasses NiceGUI's
    ``/ui`` mount and hits the outer FastAPI static handler."""
    url = demo_file_url(CATEGORY_ANIM3D, "Cesium_Man.gltf")
    assert url.startswith("/")
    assert url == f"{DEMO_STATIC_URL_PREFIX}/animation_3d/Cesium_Man.gltf"


def test_mount_survives_an_uncreatable_demo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable demo root must not stop the API from starting.

    Found on ENV-A 2026-08-13, as a total startup failure on the exact
    production shape: an installed wheel makes ``resolve_demo_root`` fall
    back to ``cwd / "demo_assets"``; a systemd unit with no
    ``WorkingDirectory=`` has cwd ``/``; so a non-root service account
    tried to create ``/demo_assets``, got ``PermissionError``, and the
    exception propagated through ``mount_ui`` → ``make_app``. The whole
    product was taken down by an optional marketing surface.

    The mount is skipped rather than half-made, because ``StaticFiles``
    raises on a missing directory too — so mounting anyway would only
    move the crash.
    """
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("I am a file, so mkdir() under me cannot succeed.")
    monkeypatch.setenv("MATOFX_DEMO_ASSETS_DIR", str(blocked / "demo_assets"))

    app = FastAPI()
    mount_demo_static_route(app)  # must not raise

    assert not [r for r in app.routes if getattr(r, "name", None) == "demo-assets"], (
        "the demo mount should be skipped when its root cannot be created"
    )

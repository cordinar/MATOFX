"""Filesystem-backed source for the Demo group's sample files.

The four Demo pages in the sidebar (`/demo/cad`, `/demo/gltf`,
`/demo/pdf`, `/demo/web`) need a place a user can drop files into and
have them show up at runtime — no Tool/Job/Scenario plumbing
required. This module is that source: it resolves a configurable
on-disk folder, enumerates its five category subfolders, and reads
individual files back with a path-traversal guard.

Why a separate module rather than inlining into ``demo.py``:

- The three demo page handlers all need the same enumerate + read
  primitives, just against different subfolders.
- It's the only piece of demo-page logic that touches the filesystem,
  so the path-resolution rules live in one place and have their own
  unit tests (``tests/ui/test_demo_assets.py``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

_log = logging.getLogger(__name__)


_ENV_VAR = "MATOFX_DEMO_ASSETS_DIR"

DEMO_STATIC_URL_PREFIX = "/_demo-asset"
"""URL path the demo-asset folder is mounted at by
:func:`mount_demo_static_route`. The page handlers build per-file URLs
beneath this so the browser can fetch them with normal HTTP requests
— necessary for cases where a data URL doesn't work:

- ``.gltf`` files reference external ``.bin`` / texture buffers by
  relative path; the GLTFLoader needs a real base URL to resolve them
  against. Data URLs have no base path.
- Chrome's built-in PDF viewer (PDFium) sometimes refuses to render
  PDFs loaded from ``data:application/pdf`` URLs. A regular HTTP URL
  goes through the standard PDF embed path and always renders.

The mount is unauthenticated — files placed in
:func:`resolve_demo_root` are explicitly demo content the operator
curated. Don't enable this in a public-internet deployment without
fronting it with auth."""
"""Env var that overrides the default demo-asset root. Power users
can point it at any folder that has the five subfolders below."""


@dataclass(frozen=True)
class DemoCategory:
    """One demo-asset category — one sidebar page typically wires two."""

    subfolder: str
    """Name of the directory under the demo-asset root."""

    extensions: frozenset[str]
    """Lowercase file extensions (no dot) the category accepts."""

    label: str
    """Human-readable name shown above the file picker."""


CATEGORY_CAD = DemoCategory(
    subfolder="cad",
    extensions=frozenset({"stl", "stp", "step", "iges", "igs", "obj"}),
    label="CAD files",
)
CATEGORY_IMAGES = DemoCategory(
    subfolder="images",
    extensions=frozenset({"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"}),
    label="Images",
)
CATEGORY_PDF = DemoCategory(
    subfolder="pdf",
    extensions=frozenset({"pdf"}),
    label="PDF documents",
)
CATEGORY_ANIM3D = DemoCategory(
    subfolder="animation_3d",
    extensions=frozenset({"gltf", "glb"}),
    label="3-D animations",
)
CATEGORY_VIDEO = DemoCategory(
    subfolder="video",
    extensions=frozenset({"mp4", "webm", "ogg", "ogv", "mov", "m4v"}),
    label="Videos",
)


def resolve_demo_root() -> Path:
    """Resolve the demo-asset root folder.

    Order of precedence:

    1. ``MATOFX_DEMO_ASSETS_DIR`` env var (if set and non-empty).
    2. ``demo_assets/`` beside the working directory the site was
       launched from.

    Demo assets are **not** shipped with MATOFX: they are whatever
    sample CAD / images / video an install wants to showcase, so the
    env var is the supported way to point at them and the cwd-relative
    default is a convenience for a dev checkout.

    Returned path is :meth:`Path.resolve`'d but not validated for
    existence — the page-level code surfaces a friendly empty-state
    message when the folder doesn't exist, rather than crashing the
    sidebar.
    """
    override = os.environ.get(_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser().resolve(strict=False)
    return (Path.cwd() / "demo_assets").resolve(strict=False)


def list_files(category: DemoCategory) -> list[str]:
    """List files in ``<root>/<subfolder>/`` whose extension matches.

    Hidden files (``.`` prefix) and subdirectories are skipped. The
    return is sorted case-insensitively so the picker shows a stable
    order. Missing folder → empty list (the page handles the empty
    state, this isn't an error).
    """
    folder = resolve_demo_root() / category.subfolder
    if not folder.is_dir():
        return []
    out: list[str] = []
    for entry in folder.iterdir():
        if not entry.is_file():
            continue
        if entry.name.startswith("."):
            continue
        ext = entry.suffix.lstrip(".").lower()
        if ext in category.extensions:
            out.append(entry.name)
    out.sort(key=str.lower)
    return out


def demo_file_url(category: DemoCategory, filename: str) -> str:
    """Build the HTTP URL the browser uses to fetch a demo file.

    Format: ``/_demo-asset/<subfolder>/<URL-quoted filename>``. Pairs
    with the static mount registered by
    :func:`mount_demo_static_route`. We URL-quote ``filename`` so
    spaces / non-ASCII characters in the filename don't break the URL.
    """
    return f"{DEMO_STATIC_URL_PREFIX}/{category.subfolder}/{quote(filename, safe='')}"


def mount_demo_static_route(fastapi_app: Any | None = None) -> None:
    """Mount the demo-asset folder as a static HTTP route.

    The mount has to land on the OUTER matofx FastAPI app — NiceGUI's
    sub-app is served under ``/ui`` so a route added there appears at
    ``/ui/_demo-asset/...``, which would force every demo page to
    hard-code the NiceGUI mount path into its URL strings.

    Args:
        fastapi_app: The outer matofx FastAPI app. When ``None``,
            grabs it from :func:`matofx.ui._holder.get_app` — fine to
            call from a NiceGUI startup hook, which fires after
            :func:`matofx.ui.app.mount_ui` has set the app reference.

    Idempotent: calling twice with the same path replaces the
    previous mount. Captures the demo root at call time; if the
    operator sets ``MATOFX_DEMO_ASSETS_DIR`` after the app starts,
    this function would need to fire again to pick it up.
    """
    from starlette.staticfiles import StaticFiles  # noqa: PLC0415

    if fastapi_app is None:
        from matofx.ui._holder import get_app  # noqa: PLC0415

        fastapi_app = get_app()
    # ``StaticFiles`` raises on missing directories, so create the
    # folder eagerly. The mount survives even an empty folder — the
    # demo pages just show their empty-state hint.
    #
    # Best-effort, and that is load-bearing rather than defensive. On an
    # installed wheel :func:`resolve_demo_root` falls back to
    # ``cwd / "demo_assets"``; a systemd unit with no ``WorkingDirectory=``
    # runs with cwd ``/``, so a non-root service account tries to create
    # ``/demo_assets`` and gets ``PermissionError``. That used to
    # propagate through ``mount_ui`` → ``make_app`` and stop the whole
    # API from starting — the entire product taken down by an optional
    # marketing surface, on precisely the production shape (Linux, wheel,
    # service user, systemd). Found on ENV-A 2026-08-13.
    root = resolve_demo_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Not a warning-and-continue-anyway: StaticFiles would raise on
        # the missing directory too, so skip the mount entirely. The demo
        # pages already render an empty state when there is nothing to
        # list, and every other surface is unaffected.
        _log.warning(
            "Demo assets are unavailable and the demo page will show its empty "
            "state: could not create %s (%s). Set MATOFX_DEMO_ASSETS_DIR to a "
            "writable path if you want the demo surface.",
            root,
            exc,
        )
        return
    fastapi_app.mount(
        DEMO_STATIC_URL_PREFIX,
        StaticFiles(directory=str(root)),
        name="demo-assets",
    )


def read_file(category: DemoCategory, filename: str) -> bytes:
    """Read one file from the category's folder.

    ``filename`` must be a bare basename — no slashes, no ``..``
    segments. We re-validate that the resolved path lands under the
    category folder so a bug in a future caller can't read outside
    the demo-asset root. Raises :class:`FileNotFoundError` for
    missing files and :class:`ValueError` for traversal attempts.
    """
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise ValueError(f"Refusing to read {filename!r}: only basenames allowed.")
    folder = (resolve_demo_root() / category.subfolder).resolve(strict=False)
    target = (folder / filename).resolve(strict=False)
    try:
        target.relative_to(folder)
    except ValueError as exc:
        raise ValueError(
            f"Refusing to read {filename!r}: resolves outside the demo folder."
        ) from exc
    if not target.is_file():
        raise FileNotFoundError(f"No demo file at {target}")
    return target.read_bytes()

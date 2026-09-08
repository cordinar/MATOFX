"""Packaged UI static assets (the default brand logo) + their HTTP mount.

The login screen needs a logo that **always** shows — before any admin
has uploaded custom branding, and on a fresh install with nothing
configured. So a default logo ships *inside the package* under
``assets/``: hatchling bundles every file beneath ``src/matofx`` into the
wheel (see ``[tool.hatch.build.targets.wheel]`` in ``pyproject.toml``),
so the asset travels to every customer install automatically.

This is deliberately separate from the admin-uploadable branding /
favicon (served from ``/api/branding/...``, stored per deployment): that
is the *customer's* logo; this is the *shipped default*. To rebrand the
shipped default, drop a replacement into ``matofx/ui/assets/`` using one
of the recognised filenames in :data:`_LOGO_CANDIDATES` — no code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

ASSETS_URL_PREFIX = "/matofx-assets"
"""Absolute URL prefix the packaged assets are mounted under.

Absolute (not under ``/ui``) so pages on the NiceGUI sub-app can
reference ``/matofx-assets/...`` without hard-coding the mount path —
the same reasoning as the demo-asset mount in :mod:`._demo_assets`.
"""

# First match wins. Lets a customer drop in an ``.svg``, ``.png`` or
# ``.webp`` logo without touching code; the login page resolves the URL
# from whichever file is actually present.
_LOGO_CANDIDATES = ("matofx_logo.svg", "matofx_logo.png", "matofx_logo.webp")


def assets_root() -> Path:
    """Absolute path to the packaged ``assets/`` folder."""
    return Path(__file__).resolve().parent / "assets"


def logo_url() -> str | None:
    """URL for the shipped brand logo, or ``None`` if none is present.

    Checks the recognised filenames in order so the asset can be an
    SVG / PNG / WebP interchangeably. Returns ``None`` only if the
    folder was emptied — callers fall back to the text wordmark.
    """
    root = assets_root()
    for name in _LOGO_CANDIDATES:
        if (root / name).is_file():
            return f"{ASSETS_URL_PREFIX}/{name}"
    return None


def mount_static_assets(fastapi_app: FastAPI) -> None:
    """Mount the packaged ``assets/`` folder as a static HTTP route.

    Mirrors :func:`matofx.ui.pages._demo_assets.mount_demo_static_route`:
    the mount lands on the **outer** matofx FastAPI app (not NiceGUI's
    ``/ui`` sub-app) so the served URLs are absolute. Idempotent — a
    second call with the same path replaces the previous mount.
    """
    from starlette.staticfiles import StaticFiles  # noqa: PLC0415

    # StaticFiles raises on a missing directory; the folder is shipped
    # with a logo, but create it defensively so an emptied checkout
    # still boots (the login page just falls back to the wordmark).
    root = assets_root()
    root.mkdir(parents=True, exist_ok=True)
    fastapi_app.mount(
        ASSETS_URL_PREFIX,
        StaticFiles(directory=str(root)),
        name="matofx-assets",
    )

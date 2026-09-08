"""Module-level holder for the live FastAPI app the UI is mounted on.

NiceGUI page handlers don't have a clean way to receive the FastAPI
app reference; they're decorated callables registered at import time.
This holder is set once by :func:`matofx.ui.app.mount_ui` and read by
the pages when they need to construct an :class:`ApiClient`.

Single-process model: every UI request lands in the same Python
process as the API, so a module-level singleton is correct.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


_app: FastAPI | None = None


def set_app(app: FastAPI) -> None:
    """Stash the live app. Called from :func:`mount_ui`."""
    global _app  # noqa: PLW0603 — module-level singleton intentional
    _app = app


def get_app() -> FastAPI:
    """Return the live app. Raises if :func:`mount_ui` hasn't run yet."""
    if _app is None:
        raise RuntimeError(
            "matofx.ui hasn't been mounted yet — call matofx.ui.mount_ui(app) first."
        )
    return _app

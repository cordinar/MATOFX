"""NiceGUI shell for MATOFX (the ``/ui/...`` surface).

A minimal browser UI mounted onto the same FastAPI app the API runs
in — single ``matofx-api`` process serves both ``/api/*`` (REST) and
``/ui/*`` (NiceGUI). The UI talks to the API over an in-process
``httpx`` client, so the auth + access-control story is the same one
external clients get.

Public surface:

- :func:`mount_ui` — mount the NiceGUI pages onto a FastAPI app.
"""

from __future__ import annotations

from matofx.ui.app import mount_ui

__all__ = ["mount_ui"]

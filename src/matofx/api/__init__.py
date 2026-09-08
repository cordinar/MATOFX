"""HTTP API surface for MATOFX (the ``matofx-api`` process).

A FastAPI app that wraps the Phase 1 services — auth, projects /
scenarios / jobs hierarchy, installed-tools registry, job submission
+ status + logs + outputs + cancel — into a versioned REST surface.
The runner daemon (:mod:`matofx.runner.daemon`) is a separate process
that picks queued jobs up off the database; the API doesn't talk to
the runner directly.

Public surface:

- :func:`make_app` — build a fully wired :class:`fastapi.FastAPI`
  instance from a :class:`matofx.config.Settings`.
- :func:`run_api` — async entry point started by ``matofx-api`` (see
  ``pyproject.toml`` ``[project.scripts]``).
"""

from __future__ import annotations

from matofx.api.app import make_app
from matofx.api.daemon import run_api

__all__ = ["make_app", "run_api"]

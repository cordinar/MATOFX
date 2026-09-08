"""Observability primitives — structured logging, metrics, tracing.

Public surface:

- ``configure`` — call once at process startup to install the JSON-Lines
  formatter on the root logger and apply the configured level.
- ``get_logger`` — get a stdlib ``logging.Logger`` for a module. Always use
  this rather than ``logging.getLogger`` directly so context binding works.
- ``bind`` — context manager that attaches request-id / user-id / arbitrary
  fields to every log record emitted in its scope (and any awaited code).
- ``clear_context`` — reset the contextvars (test helper).

Phase 0 ships logging + context. Prometheus metrics, OpenTelemetry tracing,
and health-endpoint handlers land in later phases per the brief §I46.
"""

from __future__ import annotations

from matofx.obs.config import configure
from matofx.obs.context import bind, clear_context, current_context
from matofx.obs.logger import get_logger

__all__ = [
    "bind",
    "clear_context",
    "configure",
    "current_context",
    "get_logger",
]

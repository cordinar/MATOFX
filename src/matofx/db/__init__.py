"""Async data-access layer for MATOFX.

Public surface:

- :func:`make_engine` — build an :class:`AsyncEngine` from a
  :class:`matofx.config.DbSettings`. Call once at process startup.
- :func:`make_session_factory` — build the per-process async session factory.
- :func:`session_scope` — context manager that commits on success,
  rolls back on exception, always closes the session.
- :class:`DbConfigError` — raised when DB settings are internally
  inconsistent (e.g. dialect / URL mismatch); message is plain English.
"""

from __future__ import annotations

from matofx.db.engine import DbConfigError, make_engine, make_engine_scope
from matofx.db.session import SessionFactory, make_session_factory, session_scope

__all__ = [
    "DbConfigError",
    "SessionFactory",
    "make_engine",
    "make_engine_scope",
    "make_session_factory",
    "session_scope",
]

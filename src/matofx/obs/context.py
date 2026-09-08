"""Per-request context propagation for log records.

Backed by :mod:`contextvars` so values flow through ``await`` boundaries
and across :class:`asyncio.Task` instances within a single request.

Typical usage at a request boundary::

    with bind(request_id=req_id, user_id=user.id):
        await handle_request()

Anything logged within the ``with`` block — directly or transitively —
gets the bound fields attached to its log record.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

# ContextVar default is None rather than an empty dict — sharing one mutable
# default across contexts is a footgun (B039). ``current_context`` and ``bind``
# treat ``None`` as "no fields bound".
_context: ContextVar[dict[str, Any] | None] = ContextVar("matofx_obs_context", default=None)


def current_context() -> MappingProxyType[str, Any]:
    """Return a read-only view of the bound context fields."""
    return MappingProxyType(_context.get() or {})


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Attach fields to the current logging context for the duration of the block.

    Nested ``bind`` calls merge — inner fields override outer fields with the
    same key, and on exit the previous mapping is fully restored.

    Example:
        >>> with bind(request_id="abc"):
        ...     with bind(user_id=42):
        ...         get_logger(__name__).info("hello")
    """
    parent = _context.get() or {}
    merged = {**parent, **fields}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def clear_context() -> None:
    """Reset the context to empty. Intended for test isolation."""
    _context.set(None)

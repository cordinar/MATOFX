"""Fire-and-forget background tasks that survive long enough to finish.

``asyncio.create_task`` returns a task the loop holds only *weakly*. If the
caller drops the reference — which is exactly what a fire-and-forget UI write
does — the task can be garbage-collected mid-flight and the work is silently
lost. It is rare, load-dependent, and leaves no trace, which is the worst
combination to debug from a bug report.

:func:`spawn` keeps a strong reference until the task completes and then drops
it, so the set never grows without bound. Failures are logged rather than
raised: the caller has already moved on, and an unretrieved exception on a
discarded task would otherwise surface as a bare "Task exception was never
retrieved" at some unrelated point in the log.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

_log = logging.getLogger(__name__)

_TASKS: set[asyncio.Task[Any]] = set()
"""Strong references to in-flight background tasks — see the module docstring."""


def _on_done(task: asyncio.Task[Any]) -> None:
    """Drop the reference and log anything the task raised."""
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.warning("Background task %s failed: %s", task.get_name(), exc, exc_info=exc)


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    """Run ``coro`` in the background, holding a reference until it finishes.

    Use in place of a bare ``asyncio.create_task`` whenever nobody awaits the
    result — a preference write, a cache refresh, a reload triggered by a
    filter change.
    """
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_on_done)
    return task


__all__ = ["spawn"]

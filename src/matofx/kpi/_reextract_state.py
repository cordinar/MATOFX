"""In-memory lock + visibility for per-tool_version KPI re-extractions.

Per ``designs/kpi-extraction.md`` §5.4 the "warehouse refreshing"
banner on ``/results/{tool_alias}`` reads from a transient state
that lives in the runner daemon process — no new table.

The lock pattern is **per-tool_version_id**: two distinct versions
re-extract concurrently fine; the same version's sweep is
single-threaded so the rate-limit + audit-narrative stay coherent
(design §5.4 + ``KpiReextractInProgressError``).

This module is process-local — appropriate for v1.0 where the
runner is a single process. A multi-process runner (v1.x+) would
need a DB-backed advisory lock here; the surface
(:func:`is_reextract_in_progress`, :func:`acquire`) stays the
same.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from matofx.kpi.errors import KpiReextractInProgressError

_locks: dict[int, asyncio.Lock] = {}


def _get_lock(tool_version_id: int) -> asyncio.Lock:
    """Get (or lazily create) the per-version asyncio Lock."""
    lock = _locks.get(tool_version_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[tool_version_id] = lock
    return lock


def is_reextract_in_progress(tool_version_id: int) -> bool:
    """``True`` when a re-extraction sweep is currently running for the version.

    Surface for the per-tool ``/results/{tool_alias}`` page's banner
    (slice 6). Returns ``False`` for any version that has never
    re-extracted (no lock entry exists) and for any version whose
    lock entry exists but is currently unheld.
    """
    lock = _locks.get(tool_version_id)
    return lock is not None and lock.locked()


@asynccontextmanager
async def acquire(tool_version_id: int) -> AsyncIterator[None]:
    """Acquire the per-version re-extraction lock or raise.

    Raises :class:`KpiReextractInProgressError` immediately if the
    lock is already held — re-extractions are not queued, the caller
    must wait for the in-flight sweep to finish before requesting
    another. This is the design's explicit "serialised per
    ``tool_version_id``" contract (design §5.4).
    """
    lock = _get_lock(tool_version_id)
    if lock.locked():
        raise KpiReextractInProgressError(tool_version_id)
    async with lock:
        yield


def _reset_for_tests() -> None:
    """Clear the lock dict — for test isolation only.

    Each test process starts fresh, so this is needed only when a
    single process runs multiple re-extraction tests that would
    otherwise leak lock state via the dict. Production code never
    calls this.
    """
    _locks.clear()

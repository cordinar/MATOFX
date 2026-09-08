"""Local host capacity detection + runner resource-budget resolution.

The runner's pickup loop admits a local job only when the host actually
has the CPU + RAM the job's descriptor declares (``resource_request``),
not merely a free concurrency slot — so a ``local_concurrency`` of 10
can't start ten memory-hungry jobs that then OOM each other. This module
turns the admin-tunable budget settings into concrete numbers:

- ``local_cpu_cores`` — cores the runner may hand out (0 = auto-detect).
- ``local_memory_mb`` — memory the runner may hand out (0 = auto-detect
  the host total minus ``local_memory_reserve_mb``).
- ``local_memory_reserve_mb`` — RAM held back for the OS + MATOFX itself
  when the memory budget is auto-detected.

Host capacity is detected once via :mod:`psutil` and cached for the
process lifetime — the physical machine doesn't change under a running
daemon. The budget *settings* are read fresh on every call so an
admin's change hot-reloads without a restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matofx.runner.settings import RuntimeSettings

_log = logging.getLogger(__name__)

# Conservative fallback when psutil can't introspect the host (locked-down
# container, exotic platform). One core / 1 GiB keeps the runner admitting
# the default-sized job (1 core / 512 MB) rather than wedging the queue.
_FALLBACK_CORES: int = 1
_FALLBACK_MEMORY_MB: int = 1024


@dataclass(frozen=True)
class HostCapacity:
    """Detected physical capacity of the runner's local host."""

    cpu_cores: int
    """Logical CPU cores (hyperthreads included)."""

    memory_mb: int
    """Total physical RAM in whole megabytes."""


@dataclass(frozen=True)
class LocalBudget:
    """Effective resource budget the runner may hand out to local jobs.

    Derived from the admin budget settings, falling back to the detected
    host capacity when a setting is left at its ``0`` auto-detect value.
    """

    cpu_cores: int
    memory_mb: int


# Single-slot cache keyed by a constant. A dict (mutated in place) rather
# than a rebindable module global keeps the lint quiet and the intent clear:
# the host doesn't change under a live daemon, so we detect once.
_CACHE_KEY = "host"
_capacity_cache: dict[str, HostCapacity] = {}


def detect_host_capacity() -> HostCapacity:
    """Return the host's logical core count + total RAM, detected once.

    The result is cached for the process lifetime. Any psutil failure
    (missing on the platform, permission error) falls back to a
    conservative one-core / 1 GiB capacity and logs a warning rather
    than taking the runner down.
    """
    cached = _capacity_cache.get(_CACHE_KEY)
    if cached is not None:
        return cached

    cpu_cores = _FALLBACK_CORES
    memory_mb = _FALLBACK_MEMORY_MB
    try:
        import psutil  # noqa: PLC0415 — kept local so import cost is paid once, lazily

        cores = psutil.cpu_count(logical=True)
        if isinstance(cores, int) and cores > 0:
            cpu_cores = cores
        total_bytes = psutil.virtual_memory().total
        if isinstance(total_bytes, int) and total_bytes > 0:
            memory_mb = max(total_bytes // (1024 * 1024), 1)
    except Exception:  # pragma: no cover - platform-specific failure path
        _log.warning(
            "Host capacity detection failed; falling back to %d core / %d MB. "
            "Set explicit local CPU / memory budgets to silence this.",
            _FALLBACK_CORES,
            _FALLBACK_MEMORY_MB,
            exc_info=True,
        )

    capacity = HostCapacity(cpu_cores=cpu_cores, memory_mb=memory_mb)
    _capacity_cache[_CACHE_KEY] = capacity
    return capacity


def reset_capacity_cache() -> None:
    """Clear the cached host-capacity detection.

    Test hook only — the host doesn't change under a live daemon, so
    production never calls this.
    """
    _capacity_cache.clear()


def resolve_local_budget(settings: RuntimeSettings) -> LocalBudget:
    """Resolve the effective local CPU / memory budget from ``settings``.

    A non-zero ``local_cpu_cores`` / ``local_memory_mb`` is taken as the
    explicit budget; ``0`` means "use the detected host capacity" (RAM is
    reduced by ``local_memory_reserve_mb`` so the OS and MATOFX itself
    aren't starved). The reserve only applies to the auto-detected path —
    an explicit budget is honoured verbatim.
    """
    detected = detect_host_capacity()

    cpu_cores = settings.local_cpu_cores if settings.local_cpu_cores > 0 else detected.cpu_cores

    if settings.local_memory_mb > 0:
        memory_mb = settings.local_memory_mb
    else:
        memory_mb = detected.memory_mb - settings.local_memory_reserve_mb

    # A misconfigured reserve larger than total RAM (or a zero detected
    # value) must never produce a non-positive budget — that would wedge
    # the queue. Floor both at 1.
    return LocalBudget(cpu_cores=max(cpu_cores, 1), memory_mb=max(memory_mb, 1))


__all__ = [
    "HostCapacity",
    "LocalBudget",
    "detect_host_capacity",
    "reset_capacity_cache",
    "resolve_local_budget",
]

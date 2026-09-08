"""AOS GPU scheduler — Phase 1 placeholder.

Phase 1 doesn't ship GPU scheduling on Always-On-Server hosts. The
:class:`ToolDescriptor` validator refuses the combination at install
time (see ``ToolDescriptor._aos_gpu_refused``) so customers see a
clear error early rather than a confused failure later.

This module exists for two reasons:

- It's a single, named import path that future code can target when
  the AOS GPU scheduler lands (Phase 2). Anything that needs to
  reserve / share / queue GPU memory across AOS jobs goes through
  :class:`AosGpuScheduler`.
- :class:`AosGpuNotImplementedError` is a typed escape hatch the
  runner can raise if a job slips past the descriptor-time refusal
  (e.g. someone hand-edits a descriptor row in the DB to flip
  ``gpu_memory_mb`` after install). The runner catches this on
  pickup and fails the job with the friendly message.

Until Phase 2 ships the real implementation, every method on
:class:`AosGpuScheduler` raises :class:`AosGpuNotImplementedError`
with a plain-English message pointing the operator at the HPC path.
"""

from __future__ import annotations


class AosGpuNotImplementedError(NotImplementedError):
    """Raised when AOS GPU scheduling is requested in Phase 1.

    The descriptor validator should refuse this combination at
    install time; this error is the runtime escape hatch for
    descriptors that slip past validation (DB hand-edits, schema
    migrations from older releases). Plain-English message safe to
    surface to operators.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "GPU scheduling on always-on servers isn't available in this MATOFX release. "
            "Move the tool to an HPC backend so the cluster's scheduler can place the "
            "GPU job, or remove the GPU memory requirement from the descriptor."
        )


class AosGpuScheduler:
    """Phase 1 placeholder — every method raises :class:`AosGpuNotImplementedError`.

    Phase 2 lands the real implementation: a small allocator that
    tracks GPU memory across the AOS host's registered devices and
    queues / rejects requests that exceed availability. For now the
    class exists as a typed import path so the runner / API can
    reference it by name and the friendly error surfaces consistently.
    """

    def reserve(self, *, gpu_memory_mb: int, host_id: int) -> None:
        """Reserve ``gpu_memory_mb`` on AOS host ``host_id``."""
        del gpu_memory_mb, host_id
        raise AosGpuNotImplementedError

    def release(self, *, host_id: int, reservation_id: str) -> None:
        """Release a previously made reservation."""
        del host_id, reservation_id
        raise AosGpuNotImplementedError

    def available_memory_mb(self, *, host_id: int) -> int:
        """Return how much GPU memory is free on the host."""
        del host_id
        raise AosGpuNotImplementedError


__all__ = ["AosGpuNotImplementedError", "AosGpuScheduler"]

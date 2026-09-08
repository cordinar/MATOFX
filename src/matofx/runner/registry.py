"""Mapping from a tool's ``execution_target`` string to a target instance.

The runner's pickup loop reads the tool row, looks up the
:class:`ExecutionTarget` registered for the tool's
``execution_target``, and calls ``submit`` on it. Keeping the
registry small and explicit means a misconfigured tool surfaces as
:class:`UnsupportedTargetError` at submit time rather than a generic
``KeyError`` deep in the pickup loop.

Phase 1: only ``"local"`` ships. ``"aos"`` and ``"hpc"`` raise
:class:`UnsupportedTargetError` until the AOS / HPC sub-tasks land.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from matofx.tools.exec.base import ExecutionTarget, ExecutionTargetKind
from matofx.tools.exec.local import LocalTarget


class UnsupportedTargetError(RuntimeError):
    """Raised when a tool declares a target the runner can't service.

    The message is plain-English and surfaces directly to the
    operator on the failed-job audit trail.
    """


@dataclass
class TargetRegistry:
    """Holds the :class:`ExecutionTarget` instances the runner can dispatch to.

    Default-constructed registries include only the local target,
    which keeps unit tests independent of any AOS / HPC fixtures.
    Callers may register additional targets at runtime via
    :meth:`register`; tests use this to plug in mocks.
    """

    targets: dict[ExecutionTargetKind, ExecutionTarget] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Seed the local target as the default; tests may override."""
        self.targets.setdefault("local", LocalTarget())

    def register(self, kind: ExecutionTargetKind, target: ExecutionTarget) -> None:
        """Register or replace the target for ``kind``."""
        self.targets[kind] = target

    def resolve(self, kind: ExecutionTargetKind) -> ExecutionTarget:
        """Return the target for ``kind`` or raise :class:`UnsupportedTargetError`."""
        target = self.targets.get(kind)
        if target is None:
            raise UnsupportedTargetError(
                f"Execution target {kind!r} is not configured on this runner. "
                "Either install / enable the matching backend or change the "
                "tool's execution target."
            )
        return target

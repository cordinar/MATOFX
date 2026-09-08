"""User-facing errors for the lineage service (ADR-0010).

Plain English; safe to render directly to the end user. Shape parallels
:mod:`matofx.library.errors` / :mod:`matofx.hierarchy.errors` so the API
exception handler can register one umbrella handler
(:class:`LineageError`) and treat the domain uniformly.
"""

from __future__ import annotations


class LineageError(Exception):
    """Base class for lineage domain errors."""


class LineageNotFoundError(LineageError):
    """The lineage centre doesn't exist (or the caller can't see it).

    Existence-hiding: an unknown id and a visible-but-forbidden id
    collapse into the same error so a probing client can't enumerate
    private resources. The same collapse holds for nodes encountered
    *during* a walk — they drop from the rendered graph without
    distinguishing missing-from-deletion vs hidden-by-ACL.
    """

    def __init__(self, kind: str, obj_id: int) -> None:
        """Construct with the missing/invisible centre."""
        super().__init__("We couldn't find that.")
        self.kind = kind
        self.obj_id = obj_id


class LineageAccessDeniedError(LineageError):
    """The caller is authenticated but lacks ``lineage.view``.

    Distinct from :class:`LineageNotFoundError` because the surface
    exists and the caller can see it would be there — they just don't
    have the permission to read lineage at all. Raised by
    :meth:`LineageService.build_graph` *before* the centre is resolved.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "You don't have permission to view lineage. Ask an administrator if you need access."
        )


class LineageDepthOutOfRangeError(LineageError):
    """Upstream or downstream depth is outside the supported 0..20 range.

    The hard cap exists to keep build cost bounded; users who want
    deeper traversal expand individual edges from the UI rather than
    raising the global depth (design §4.5).
    """

    def __init__(self, depth: int) -> None:
        """Construct with the offending depth value."""
        super().__init__(
            f"Lineage depth {depth} is outside the supported range (0-20). "
            "Please choose a smaller upstream / downstream depth."
        )
        self.depth = depth


class LineageInvariantError(LineageError):
    """A built lineage graph fails a structural invariant.

    Development-time guard for the DAG-by-construction promise (design
    §3.3). Production should never see this — every edge kind has a
    schema-level direction that flows older → newer, so a back-edge
    indicates either a resolver bug or a forged edge tuple. The wrapped
    reason names the invariant that failed (e.g. ``"cycle through node
    (job, 42)"``).
    """

    def __init__(self, reason: str) -> None:
        """Construct with a developer-facing reason."""
        super().__init__(f"Lineage graph invariant failed: {reason}")
        self.reason = reason

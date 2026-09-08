"""Domain errors raised by :class:`matofx.tutorials.service.TutorialService`.

All carry plain-English ``detail`` strings safe to render to admins.
"""

from __future__ import annotations


class TutorialError(RuntimeError):
    """Base class for tutorial-domain errors. Carries a user-facing ``detail``."""

    def __init__(self, detail: str) -> None:
        """Construct with the user-facing reason."""
        super().__init__(detail)
        self.detail = detail


class TutorialNotFoundError(TutorialError):
    """The requested tutorial row doesn't exist."""

    def __init__(self, tutorial_id: int) -> None:
        """Construct for ``tutorial_id``."""
        super().__init__(f"We couldn't find tutorial #{tutorial_id}.")
        self.tutorial_id = tutorial_id


class ArchivedTutorialError(TutorialError):
    """The tutorial is an archived previous version and can't be changed.

    Raised when an operation that only makes sense on a *current* version
    (activate, deactivate, update) targets a row whose ``superseded_at``
    is set.
    """

    def __init__(self, tutorial_id: int) -> None:
        """Construct for ``tutorial_id``."""
        super().__init__(
            f"Tutorial #{tutorial_id} is an archived previous version, so it can't be "
            "changed. Update the current version instead."
        )
        self.tutorial_id = tutorial_id


class UnknownToolError(TutorialError):
    """The supplied ``tool_alias`` is not a known tool."""

    def __init__(self, tool_alias: str) -> None:
        """Construct for ``tool_alias``."""
        super().__init__(
            f"No tool called {tool_alias!r} is installed. Pick an existing tool, "
            "or upload as a general tutorial instead."
        )
        self.tool_alias = tool_alias


class UnknownWorkflowError(TutorialError):
    """The supplied ``workflow_alias`` is not a known workflow."""

    def __init__(self, workflow_alias: str) -> None:
        """Construct for ``workflow_alias``."""
        super().__init__(
            f"No workflow called {workflow_alias!r} is installed. Pick an "
            "existing workflow, or upload as a general tutorial instead."
        )
        self.workflow_alias = workflow_alias


class ScopeConflictError(TutorialError):
    """Both ``tool_alias`` and ``workflow_alias`` were supplied."""

    def __init__(self) -> None:
        """Construct with a fixed user-facing message."""
        super().__init__(
            "A tutorial can be scoped to a tool *or* a workflow, not both. Leave one blank."
        )


class InvalidMediaTypeError(TutorialError):
    """``media_type`` is not ``'document'`` or ``'video'``."""


class InvalidExtensionError(TutorialError):
    """Upload filename extension doesn't match the chosen media type."""


class UploadTooLargeError(TutorialError):
    """Upload size exceeds the per-file cap."""

"""User-facing errors for the file-library service (ADR-0006).

Plain English; safe to render directly to the end user. Shape parallels
:mod:`matofx.hierarchy.errors` / :mod:`matofx.approvals.errors` so the
API exception handler can register one umbrella handler
(:class:`LibraryError`) and treat the domain uniformly. Byte-storage
failures surface as :class:`matofx.library.storage.LibraryStorageError`,
which is a separate ``RuntimeError`` family the API maps alongside these.
"""

from __future__ import annotations


class LibraryError(Exception):
    """Base class for file-library domain errors."""


class LibraryFileNotFoundError(LibraryError):
    """No library file with that id (or the caller can't see it).

    The two cases collapse into one error to avoid leaking the
    existence of files the caller has no scope visibility on.
    """

    def __init__(self, file_id: int) -> None:
        """Construct with the missing/invisible file id."""
        super().__init__("We couldn't find that library file.")
        self.file_id = file_id


class LibraryVersionNotFoundError(LibraryError):
    """No library-file version with that id (or the caller can't see it)."""

    def __init__(self, version_id: int) -> None:
        """Construct with the missing/invisible version id."""
        super().__init__("We couldn't find that library file version.")
        self.version_id = version_id


class LibraryAccessDeniedError(LibraryError):
    """The caller is authenticated but not allowed to perform the action."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "You don't have permission to do that with this library file. "
            "Ask the file's owner, a project owner, or a library curator if you need access."
        )


class LibraryValidationError(LibraryError):
    """A library field failed a basic validation check.

    Covers a missing name or a missing provenance note — both required
    at the service layer. The message is already user-facing.
    """

    def __init__(self, message: str) -> None:
        """Construct with an already-user-facing message."""
        super().__init__(message)


class InvalidScopeError(LibraryError):
    """The supplied scope isn't one of the closed vocabulary."""

    def __init__(self, scope: str) -> None:
        """Construct with the offending scope."""
        super().__init__(
            f"'{scope}' isn't a recognised library scope. "
            "Expected one of: personal, project, global."
        )
        self.scope = scope


class ProjectScopeError(LibraryError):
    """The ``project_id`` doesn't line up with the chosen scope.

    Project scope requires a project; personal/global scope forbids
    one (the portable ``ck_library_file_project_scope`` CHECK enforces
    this in the DB — this is the friendly pre-check).
    """

    def __init__(self, message: str) -> None:
        """Construct with an already-user-facing message."""
        super().__init__(message)


class LibraryFileRetiredError(LibraryError):
    """A mutation was attempted on a retired file.

    Retired files are read-only; un-retiring (``state='active'``) is the
    only transition allowed, and only via :meth:`LibraryService.restore`.
    """

    def __init__(self, file_id: int) -> None:
        """Construct with the retired file id."""
        super().__init__(
            "This library file has been retired and can't be changed. "
            "Restore it first if you need to edit it."
        )
        self.file_id = file_id


class ToolAssignmentRequiredError(LibraryError):
    """A file was created with no tool assignment.

    Every library file must be usable by at least one tool, so
    :meth:`LibraryService.create_file` requires at least one
    ``(tool_alias[, field_name])`` mapping.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "A library file must be assigned to at least one tool so people can pick it in a form."
        )


class DuplicateToolAssignmentError(LibraryError):
    """The file is already assigned to this exact ``(tool, field)`` pair."""

    def __init__(self, tool_alias: str, field_name: str | None) -> None:
        """Construct with the conflicting tool alias and optional field."""
        where = f"{tool_alias!r} field {field_name!r}" if field_name else f"{tool_alias!r}"
        super().__init__(f"This file is already assigned to {where}.")
        self.tool_alias = tool_alias
        self.field_name = field_name


class ToolAssignmentNotFoundError(LibraryError):
    """The tool assignment to remove doesn't belong to this file."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__("That tool assignment doesn't exist on this library file.")


class ApproverRequiredError(LibraryError):
    """A global-publish gate is on but no approver was chosen.

    With the ``library_global_publish_approval`` toggle ON, landing a
    file in the global tier requires the submitter to name one curator
    to review it (ADR-0006 §5.3 — submit is blocked until an approver
    is chosen). Self-approval is rejected separately by the approvals
    service (:class:`matofx.approvals.errors.SelfApprovalNotAllowedError`).
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "Publishing to the global library needs a reviewer. "
            "Choose one curator (other than yourself) to approve this change."
        )


class GlobalPublishNotPermittedError(LibraryError):
    """A non-curator tried to publish straight to global with the gate off.

    With the approval toggle OFF, only a library curator
    (:attr:`matofx.users.permissions.Permission.LIBRARY_CURATE`) may
    publish directly to the global tier.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "Only a library curator can publish a file to the global library. "
            "Ask a curator to review and publish it, or keep it in your personal "
            "or a project library."
        )


class LibraryConcurrentModificationError(LibraryError):
    """Another writer changed this file since the caller loaded it.

    Raised when the optimistic-lock ``version`` on the row has moved on.
    The recovery is to reload and re-apply the change.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "This library file was changed by someone else while you were editing. "
            "Please reload and try again."
        )

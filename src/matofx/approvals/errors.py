"""User-facing errors for the approvals service.

Plain English; safe to render directly to the end user. Shape
parallels :mod:`matofx.tools.errors` / :mod:`matofx.favourites.errors`
so the API exception handler can treat the domain uniformly.
"""

from __future__ import annotations


class ApprovalError(Exception):
    """Base class for approval domain errors."""


class ApprovalRequestNotFoundError(ApprovalError):
    """No approval request with that id (or the caller can't see it)."""

    def __init__(self, request_id: int) -> None:
        """Construct with the missing request id."""
        super().__init__(
            f"We couldn't find approval request #{request_id}. "
            "It may have already been decided, or the link is stale."
        )
        self.request_id = request_id


class InvalidApprovalKindError(ApprovalError):
    """The supplied gate kind isn't ``pre`` or ``post``."""

    def __init__(self, kind: str) -> None:
        """Construct with the offending kind."""
        super().__init__(f"{kind!r} isn't a valid approval kind. Use 'pre' or 'post'.")
        self.kind = kind


class DuplicatePendingApprovalError(ApprovalError):
    """A pending request for this job + gate already exists.

    Defensive guard in front of the partial unique index
    ``uq_approval_request_pending_job_kind`` so a double-submit
    surfaces as a friendly message rather than an IntegrityError.
    """

    def __init__(self, job_id: int, kind: str) -> None:
        """Construct with the job id and gate kind."""
        super().__init__(
            f"Job {job_id:07d} already has a pending {kind}-approval "
            "request. It only needs to be decided once."
        )
        self.job_id = job_id
        self.kind = kind


class ApprovalAlreadyDecidedError(ApprovalError):
    """The request was already approved or denied by someone else.

    Expected in the batch page when two approvers act on overlapping
    inboxes — the second submit sees the row is no longer pending.
    """

    def __init__(self, request_id: int, status: str) -> None:
        """Construct with the request id and its current status."""
        super().__init__(
            f"Approval request #{request_id} has already been {status}. "
            "Refresh the Approvals page to see the current state."
        )
        self.request_id = request_id
        self.status = status


class SelfApprovalNotAllowedError(ApprovalError):
    """The submitter chose themselves as the directed approver.

    Directed-approver gates (ADR-0006/0007 — global library files,
    materials changes) require a *second* curator: the chooser is
    excluded from the eligible pool, so a real gate needs at least two
    curators. Self-approval would defeat the review.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "You can't choose yourself as the approver — pick another curator. "
            "A second person must review this change."
        )


class ApprovalNotPermittedError(ApprovalError):
    """The caller isn't an eligible approver for this request (and isn't an admin)."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "You're not an approver for this job. Only the approvers "
            "configured on the tool — or an administrator — can decide it."
        )


class DenialReasonRequiredError(ApprovalError):
    """A deny decision arrived without a reason.

    The requestor is told *why* their job was denied, so the reason is
    mandatory on every denial (it's optional on an approval).
    """

    def __init__(self, request_id: int) -> None:
        """Construct with the request id that was being denied."""
        super().__init__(
            f"Denying approval request #{request_id} needs a reason — "
            "it's sent to the requestor so they understand the decision."
        )
        self.request_id = request_id

"""User-facing errors for the hierarchy surface.

Plain English; safe to render directly to end-users (per §F30).
"""

from __future__ import annotations


class HierarchyError(Exception):
    """Base class for hierarchy errors."""


class NotFoundError(HierarchyError):
    """Resource doesn't exist (or the caller can't see it).

    The two cases collapse into one error to avoid leaking the
    existence of resources the caller can't access.
    """

    def __init__(self, resource_type: str, resource_id: int | str) -> None:
        """Construct with the resource type and id (already user-facing)."""
        super().__init__(f"We couldn't find that {resource_type}.")
        self.resource_type = resource_type
        self.resource_id = resource_id


class AccessDeniedError(HierarchyError):
    """The caller is authenticated but not allowed to perform the action."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "You don't have permission to do that. "
            "Ask the project owner or an administrator if you need access."
        )


class DuplicateNameError(HierarchyError):
    """A sibling resource already has this name."""

    def __init__(self, resource_type: str) -> None:
        """Construct with the conflicting resource type."""
        super().__init__(
            f"A {resource_type} with that name already exists here. Please choose a different name."
        )
        self.resource_type = resource_type


class InvalidJobStateError(HierarchyError):
    """The supplied job-state value isn't one of the closed vocabulary."""

    def __init__(self, state: str) -> None:
        """Construct with the offending state."""
        super().__init__(
            f"'{state}' isn't a recognised job state. "
            "Expected one of: created, queued, running, complete, failed."
        )
        self.state = state


class ConcurrentModificationError(HierarchyError):
    """Another user updated this resource while you were editing it.

    Raised when the optimistic-lock version on the row has moved on
    since the caller loaded it. The right recovery is to re-fetch
    and re-apply the change against the latest copy.
    """

    def __init__(self, resource_type: str) -> None:
        """Construct with the resource type."""
        super().__init__(
            f"This {resource_type} was changed by someone else while you were editing. "
            "Please reload and try again."
        )
        self.resource_type = resource_type


class JobAlreadyTerminalError(HierarchyError):
    """A cancel was requested on a job that has already finished.

    Surfaces when the user clicks Cancel on a job that completed or
    failed between the page load and the click. The right recovery is
    to refresh — there's nothing to cancel.
    """

    def __init__(self, state: str) -> None:
        """Construct with the job's terminal state."""
        super().__init__(
            f"This job has already finished (state: {state}), so it can't be cancelled."
        )
        self.state = state


class JobNotQueueableError(HierarchyError):
    """A queue-for-runner request was made on a job that isn't in 'created' state.

    A job moves through ``created → queued → running → complete | failed``;
    only the first transition is callable from the API. The runner owns
    everything after ``queued``.
    """

    def __init__(self, state: str) -> None:
        """Construct with the job's current state."""
        super().__init__(
            f"This job is in state {state!r}; only newly-created jobs can be queued for the runner."
        )
        self.state = state


class ResultsWithheldError(HierarchyError):
    """Outputs are gated by a post-approval and the caller may not see them.

    Raised by the output surfaces (§16.15) when a job is in
    ``pending_results`` (a post/both-policy run finished but an
    approver hasn't released the outputs yet) or ``results_withheld``
    (an approver denied release). Eligible approvers and admins are
    let through; everyone else — including the requestor — gets this.

    Carries ``state`` so the API can hand the UI a machine-readable
    discriminator (Compare degrades to a "results withheld" cell
    rather than aborting the whole comparison).
    """

    def __init__(self, state: str) -> None:
        """Construct with the job's current (gated) state."""
        if state == "pending_results":
            message = (
                "These results are pending approval and aren't available yet. "
                "An approver has been asked to review them; you'll be able to "
                "download them once they're released."
            )
        else:  # results_withheld
            message = (
                "These results were withheld by an approver and aren't "
                "available. Contact an administrator if you believe this "
                "is a mistake."
            )
        super().__init__(message)
        self.state = state


class StorageLocationUnavailableError(HierarchyError):
    """No active storage_location row to stage the job's working directory under.

    Surfaces when an admin hasn't registered a storage_location yet,
    or has retired the only active one. The fix is admin-side, not
    user-side — the message says so.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "There's no active storage location configured for this install. "
            "Ask an administrator to register or re-activate a storage location "
            "before submitting jobs."
        )


class DuplicateToolInScenarioError(HierarchyError):
    """The scenario already has a job for this tool — only one allowed.

    Locked by the user 2026-05-08: any given scenario can hold a job
    for any given tool **at most once**. Trying to create a second
    raises this error so the API + workspace can either redirect the
    user to the existing run or surface a clear "this tool already
    ran in this scenario; create a new scenario for a fresh attempt"
    message.

    Carries the existing job's id so the caller can navigate to it
    rather than dead-ending on the error.
    """

    def __init__(self, tool_alias: str, existing_job_id: int | None = None) -> None:
        """Construct with the conflicting tool alias and existing job id (if known)."""
        if existing_job_id is not None:
            message = (
                f"This scenario already has a job for {tool_alias!r}. "
                "Open the existing job, or create a new scenario for a fresh attempt."
            )
        else:
            message = (
                f"This scenario already has a job for {tool_alias!r}. "
                "Create a new scenario if you want to run this tool again."
            )
        super().__init__(message)
        self.tool_alias = tool_alias
        self.existing_job_id = existing_job_id


class ToolNotRunnableError(HierarchyError):
    """The tool a job references can't be run right now.

    Covers: archived tools, tools still in ``new`` / ``test`` state when
    the caller doesn't hold the matching see-state permission, tools
    whose bundle path no longer exists on disk.
    """

    def __init__(self, alias: str, reason: str) -> None:
        """Construct with the tool alias and a plain-English reason."""
        super().__init__(f"Tool {alias!r} can't be run right now: {reason}")
        self.alias = alias
        self.reason = reason


# ---- per-user ACL / state / metadata (ADR-0002) ----------------------------


class InvalidRoleError(HierarchyError):
    """The supplied project-ACL role isn't one of the closed vocabulary."""

    def __init__(self, role: str) -> None:
        """Construct with the offending role."""
        super().__init__(
            f"'{role}' isn't a recognised project role. Expected one of: owner, member, viewer."
        )
        self.role = role


class UserAlreadyGrantedError(HierarchyError):
    """The user already has a role on this project."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "That user is already on this project's access list. "
            "Change their role instead of adding them again."
        )


class UserNotGrantedError(HierarchyError):
    """The user doesn't currently have a role on this project."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__("That user isn't on this project's access list.")


class CreatorImmutableError(HierarchyError):
    """An attempt was made to grant/remove the Creator via the ACL.

    The Creator is ``project.creator_user_id`` — frozen for the life of
    the project and resolved to Owner-equivalent authority. It is never
    an ACL row, can't be removed, and can't be re-assigned.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "The project Creator can't be changed or removed. "
            "Add other Owners instead, or ask an administrator to transfer ownership."
        )


class InvalidStateError(HierarchyError):
    """An invalid or no-op project/scenario lifecycle transition."""

    def __init__(self, message: str) -> None:
        """Construct with an already-user-facing message."""
        super().__init__(message)


class MetadataError(HierarchyError):
    """Project metadata failed validation against the taxonomy.

    Covers: a required field left empty, a value that doesn't match the
    field's type, an unknown field, or a value that isn't (an active)
    allowed option for a select field. The message is already
    user-facing and names the field.
    """

    def __init__(self, message: str) -> None:
        """Construct with an already-user-facing message."""
        super().__init__(message)

"""Domain errors raised by :class:`matofx.workflows.service.WorkflowService`.

All carry plain-English ``detail`` strings safe to render directly to
admins via the UI.
"""

from __future__ import annotations


class WorkflowError(RuntimeError):
    """Base class for workflow-domain errors."""

    def __init__(self, detail: str) -> None:
        """Construct with the user-facing reason."""
        super().__init__(detail)
        self.detail = detail


class WorkflowNotFoundError(WorkflowError):
    """The requested workflow alias / id doesn't exist."""


class DuplicateWorkflowError(WorkflowError):
    """A workflow with the supplied alias is already installed."""

    def __init__(self, alias: str) -> None:
        """Construct for ``alias``."""
        super().__init__(
            f"A workflow with the alias {alias!r} already exists. Pick a different alias."
        )
        self.alias = alias


class EmptyWorkflowError(WorkflowError):
    """The workflow has no member tools — at least one is required."""

    def __init__(self) -> None:
        """Construct with a friendly default message."""
        super().__init__(
            "A workflow needs at least one tool. Add tools to the Tools list and save again."
        )


class UnknownToolMemberError(WorkflowError):
    """One of the member tool aliases isn't installed."""

    def __init__(self, tool_alias: str) -> None:
        """Construct for ``tool_alias``."""
        super().__init__(
            f"No tool called {tool_alias!r} is installed. Pick an "
            "existing tool, or install it first."
        )
        self.tool_alias = tool_alias


class ArchivedToolMemberError(WorkflowError):
    """One of the member tools is archived and can't be used."""

    def __init__(self, tool_alias: str) -> None:
        """Construct for the archived ``tool_alias``."""
        super().__init__(
            f"The tool {tool_alias!r} is archived and can't be added "
            "to a workflow. Unarchive it first, or pick another tool."
        )
        self.tool_alias = tool_alias


class DuplicateMemberError(WorkflowError):
    """A tool appears twice in the member list — not allowed."""

    def __init__(self, tool_alias: str) -> None:
        """Construct for the offending ``tool_alias``."""
        super().__init__(
            f"The tool {tool_alias!r} appears more than once in the "
            "workflow. Each tool can only be in a workflow once."
        )
        self.tool_alias = tool_alias


class UpstreamOrderingError(WorkflowError):
    """A downstream tool was placed *before* the upstream it depends on.

    Output→input wiring is auto-discovered from each tool's descriptor:
    a member tool whose descriptor declares an input as
    ``downstream_input=True, upstream_tool="<alias>"`` needs that
    upstream tool to run *first*. Putting the downstream tool before
    its upstream in the workflow's order makes the wiring impossible —
    we'd never get the output value before the downstream tab runs.
    """

    def __init__(
        self,
        *,
        downstream_tool_alias: str,
        downstream_tool_name: str,
        upstream_tool_alias: str,
        upstream_tool_name: str,
        input_field_name: str,
    ) -> None:
        """Construct with the specific offending pair + field."""
        super().__init__(
            f"'{downstream_tool_name}' needs an input from '{upstream_tool_name}' "
            f"(field '{input_field_name}'), so '{upstream_tool_name}' must run "
            f"before '{downstream_tool_name}'. Move '{upstream_tool_name}' "
            "earlier in the order, or drop one of the two from the workflow."
        )
        self.downstream_tool_alias = downstream_tool_alias
        self.downstream_tool_name = downstream_tool_name
        self.upstream_tool_alias = upstream_tool_alias
        self.upstream_tool_name = upstream_tool_name
        self.input_field_name = input_field_name


class WorkflowStateError(WorkflowError):
    """Promote / archive call refused because of the current state."""


class WorkflowArchivedError(WorkflowError):
    """The workflow is archived and can't be edited or run."""

    def __init__(self, alias: str) -> None:
        """Construct for ``alias``."""
        super().__init__(f"The workflow {alias!r} is archived. Unarchive it before making changes.")
        self.alias = alias


# ----- custom-workflow errors --------------------------------------------


class NotCustomWorkflowError(WorkflowError):
    """The requested operation only applies to ``kind='custom'`` rows.

    Raised when callers try to use a custom-workflow method on an
    admin-released workflow (or vice versa). The service treats the
    discriminator strictly so we don't accidentally let one path's
    privilege model leak into the other.
    """

    def __init__(self, alias: str) -> None:
        """Construct for ``alias``."""
        super().__init__(
            f"The workflow {alias!r} isn't a custom workflow. "
            "This operation only applies to user-owned custom workflows."
        )
        self.alias = alias


class NotOwnerError(WorkflowError):
    """Caller isn't the owner of this custom workflow.

    Edits, share-list changes, visibility toggles, and owner-driven
    archive all require the caller to *be* the row's ``owner_id``.
    Share recipients see + run only; admins go through the inspector
    surface, which has its own gate.
    """

    def __init__(self, alias: str) -> None:
        """Construct for ``alias``."""
        super().__init__(
            f"Only the owner of {alias!r} can change it. Ask the owner "
            "to make the edit, or duplicate the workflow into your own "
            "list and edit your copy."
        )
        self.alias = alias


class TooFewMembersError(WorkflowError):
    """A custom workflow needs at least 2 tools.

    Single-tool "workflows" don't earn their keep — the user could
    just launch the tool directly. The locked design is 2–10.
    """

    def __init__(self) -> None:
        """Construct with a friendly default message."""
        super().__init__(
            "A custom workflow needs at least 2 tools. Add another "
            "tool to the list, or launch the single tool directly "
            "from the Tools page."
        )


class TooManyMembersError(WorkflowError):
    """A custom workflow can have at most 10 tools.

    Cap from the locked design — the form scales nicely up to about
    that many tabs, and longer sequences usually want an admin-released
    workflow with proper version-pinning anyway.
    """

    def __init__(self, count: int) -> None:
        """Construct with the offending ``count``."""
        super().__init__(
            f"A custom workflow can hold at most 10 tools — you picked "
            f"{count}. Drop a few tools, or ask an admin to install a "
            "released workflow for this sequence."
        )
        self.count = count


class InvalidVisibilityError(WorkflowError):
    """Visibility must be ``'private'`` or ``'shared'``."""

    def __init__(self, value: str) -> None:
        """Construct for the offending ``value``."""
        super().__init__(
            f"{value!r} isn't a valid visibility for a custom workflow. "
            "Expected 'private' or 'shared'."
        )
        self.value = value


class InvalidShareRecipientError(WorkflowError):
    """A recipient on the share list is unusable.

    Triggers for: the owner trying to share with themselves, a missing
    user id, or a deactivated account. Sharing with the World wildcard
    is not allowed for custom workflows — that's an admin-released
    workflow concern.
    """

    def __init__(self, detail: str) -> None:
        """Construct with a plain-English ``detail``."""
        super().__init__(detail)


class ToolNotVisibleToRecipientError(WorkflowError):
    """A share recipient can't see one of the workflow's member tools.

    Raised at save time when the owner picks a recipient who doesn't
    pass the tool's state-and-team gate for at least one member tool.
    Per the locked design, we prompt the owner rather than silently
    hide — the message names the recipient + the tool so they can
    either drop the recipient, drop the tool, or contact an admin
    for a team grant.
    """

    def __init__(
        self,
        *,
        recipient_username: str,
        tool_alias: str,
        tool_name: str,
    ) -> None:
        """Construct for the (recipient, tool) pair that failed the gate."""
        super().__init__(
            f"{recipient_username} can't see the tool '{tool_name}' "
            f"({tool_alias}). Ask an admin to give them access, or "
            "remove the tool from the workflow before sharing."
        )
        self.recipient_username = recipient_username
        self.tool_alias = tool_alias
        self.tool_name = tool_name

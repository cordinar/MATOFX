"""Tool-management errors. Plain English, safe to render in the UI."""

from __future__ import annotations


class ToolError(Exception):
    """Base class for tool-related errors."""


class ToolNotFoundError(ToolError):
    """Tool doesn't exist or the actor can't see it (states leak nothing)."""

    def __init__(self, alias: str) -> None:
        """Construct with the missing tool's alias."""
        super().__init__(f"We couldn't find a tool called {alias!r}.")
        self.alias = alias


class ToolPermissionError(ToolError):
    """Actor is authenticated but not permitted to perform the action."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "You don't have permission to do that. "
            "Tool installation, edit, archival, and state changes are reserved for administrators."
        )


class DuplicateToolError(ToolError):
    """A tool with the same alias is already installed."""

    def __init__(self, alias: str) -> None:
        """Construct with the conflicting tool's alias."""
        super().__init__(
            f"A tool called {alias!r} is already installed. "
            "Edit the existing one or pick a different alias."
        )
        self.alias = alias


class InvalidStateTransitionError(ToolError):
    """Caller asked for a state that's not part of the closed vocabulary."""

    def __init__(self, target: str) -> None:
        """Construct with the rejected ``target`` state."""
        super().__init__(
            f"{target!r} is not a valid tool state. Valid states are 'new', 'test', and 'released'."
        )
        self.target = target


class InvalidPromotionError(ToolError):
    """Forward-promotion would skip a step.

    Forward order is ``new → test → released``. Rollback to any
    earlier state is permitted (broken release recovery), but
    skipping forwards from ``new`` straight to ``released`` is
    rejected — every tool has to go through ``test`` so beta testers
    actually get a chance to validate it.
    """

    def __init__(self, current: str, target: str) -> None:
        """Construct with the current and rejected target states."""
        super().__init__(
            f"Can't promote a tool from {current!r} directly to {target!r}. "
            "Promote to 'test' first, then to 'released'."
        )
        self.current = current
        self.target = target


class ToolArchivedError(ToolError):
    """Operation rejected because the tool is archived.

    Archived tools can be unarchived (admin only) but otherwise
    refuse promotions, descriptor edits, and new job submissions.
    """

    def __init__(self, alias: str) -> None:
        """Construct with the archived tool's alias."""
        super().__init__(f"Tool {alias!r} is archived. Unarchive it before making changes.")
        self.alias = alias


class AliasMismatchError(ToolError):
    """An update tried to change a tool's alias.

    The alias is the identity — every job carries it as a foreign
    key. Renaming would orphan existing jobs. Fix the alias on the
    descriptor before retrying.
    """

    def __init__(self, current: str, supplied: str) -> None:
        """Construct with the current and supplied aliases."""
        super().__init__(
            f"This tool's alias is {current!r}; the descriptor you supplied has {supplied!r}. "
            "Aliases can't be changed after install."
        )
        self.current = current
        self.supplied = supplied


class UnknownTeamError(ToolError):
    """The descriptor references a team that doesn't exist."""

    def __init__(self, team_names: list[str]) -> None:
        """Construct with the unknown team names."""
        names = ", ".join(repr(n) for n in team_names)
        super().__init__(
            f"These teams aren't on the install: {names}. "
            "Create the team first, or remove it from the tool's descriptor."
        )
        self.team_names = list(team_names)


class UnknownApproverError(ToolError):
    """The descriptor references an approver that isn't a real user."""

    def __init__(self, usernames: list[str]) -> None:
        """Construct with the unknown approver usernames."""
        names = ", ".join(repr(u) for u in usernames)
        super().__init__(
            f"These approvers aren't users on this install: {names}. "
            "Add the user first, or remove them from the tool's approver list."
        )
        self.usernames = list(usernames)


class ApproverNotEnabledError(ToolError):
    """The descriptor names a real user, but they aren't flagged as an approver."""

    def __init__(self, usernames: list[str]) -> None:
        """Construct with the affected usernames."""
        names = ", ".join(repr(u) for u in usernames)
        super().__init__(
            f"These users don't have the approver flag turned on: {names}. "
            "Enable each user's 'is approver' option, or pick different approvers."
        )


class UnknownMaterialClassError(ToolError):
    """A ``material`` input references a class that isn't an active material class.

    Raised at tool install/update (ADR-0007 §3) when the field's
    ``material_class`` doesn't resolve to an ``active`` class in the
    materials database — the curator must create the class first.
    """

    def __init__(self, material_class: str, field_name: str) -> None:
        """Construct with the offending class name and the input field."""
        super().__init__(
            f"Input {field_name!r} needs the material class {material_class!r}, which isn't an "
            "active class in the materials database. Create the class first, or fix the "
            "tool's descriptor."
        )
        self.material_class = material_class
        self.field_name = field_name


class UnknownMaterialPropertyError(ToolError):
    """A ``material`` input lists ``required_properties`` the class doesn't declare.

    Raised at tool install/update (ADR-0007 §3) so the mismatch surfaces
    before any job runs against the tool.
    """

    def __init__(self, material_class: str, prop_names: list[str], field_name: str) -> None:
        """Construct with the class, the unknown property names, and the field."""
        names = ", ".join(repr(p) for p in prop_names)
        super().__init__(
            f"Input {field_name!r} lists properties the {material_class!r} class doesn't "
            f"declare: {names}. Add them to the class schema, or remove them from the field."
        )
        self.material_class = material_class
        self.prop_names = list(prop_names)
        self.field_name = field_name


class InvalidBundlePathError(ToolError):
    """The supplied bundle path is empty, contains traversal segments, or otherwise invalid."""

    def __init__(self, path: str, reason: str) -> None:
        """Construct with the offending path and reason."""
        super().__init__(f"The bundle path {path!r} isn't acceptable: {reason}.")
        self.path = path
        self.reason = reason


class InvalidFileEditError(ToolError):
    """A file edit was rejected (path traversal, missing file, or non-text contents).

    The accompanying ``reason`` is plain English so the API can surface
    it to the operator without translation.
    """

    def __init__(self, path: str, reason: str) -> None:
        """Construct with the offending bundle-relative path and reason."""
        super().__init__(f"The file edit for {path!r} couldn't be applied: {reason}.")
        self.path = path
        self.reason = reason


class SyntaxValidationError(ToolError):
    """A file edit failed parser validation before any disk write happened.

    Carries the per-file list of issues so the API can return them as
    a structured response and the in-browser editor can render each
    one inline (line / column / message).
    """

    def __init__(self, path: str, issues: list[dict[str, int | str]]) -> None:
        """Construct with the bundle-relative path and a list of issue dicts."""
        previews = "; ".join(
            f"line {issue.get('line', '?')}: {issue.get('message', '')}" for issue in issues
        )
        super().__init__(f"The edit to {path!r} has syntax errors and wasn't saved: {previews}")
        self.path = path
        self.issues = list(issues)


class PythonEnvNotUsableError(ToolError):
    """The chosen Python env doesn't exist or has been deactivated.

    Raised when pinning a tool to a ``python_env`` row that isn't a
    usable target. Deactivated envs are deliberately rejected here:
    deactivation means "new tools can't pick this env" (see
    :class:`matofx.tools.paths_service.PythonEnvsService`), so binding
    a tool to one is exactly what we forbid.
    """

    def __init__(self, python_env_id: int) -> None:
        """Construct with the offending env id."""
        super().__init__(
            f"Python env #{python_env_id} isn't available — it may have been "
            "deactivated or removed. Pick an active env on the Execution "
            "environments page, or leave it unset to use the main MATOFX environment."
        )
        self.python_env_id = python_env_id


class SoleApproverError(ToolError):
    """Action would leave one or more tools with no approvers.

    Raised when deactivating a user OR turning off their approver
    flag would empty out a tool's ``approver_usernames`` list (which
    the descriptor invariant forbids when ``approvals_policy`` isn't
    ``"none"``).
    """

    def __init__(self, username: str, tool_aliases: list[tuple[str, str]]) -> None:
        """Construct with the username and the (alias, name) of affected tools."""
        listing = ", ".join(f"{name!r} ({alias})" for alias, name in tool_aliases)
        super().__init__(
            f"{username!r} is the only approver on these tools: {listing}. "
            "Add another approver to each affected tool first, then retry."
        )
        self.username = username
        self.tool_aliases = list(tool_aliases)

"""User-facing errors for the auth surface.

Per §F30 ("users are not modellers"), every error message that can
reach a UI surface is plain English with the *what* and *what to do
next*. The exception classes below carry messages safe to render
directly in the UI; they avoid leaking which check failed (e.g.
"unknown username" vs "wrong password") so an attacker can't probe
for valid usernames.

Class names follow PEP 8 (each ends in ``Error``) for consistency
with stdlib and the rest of the MATOFX codebase.
"""

from __future__ import annotations


class UserError(Exception):
    """Base class for user-facing auth errors."""


class InvalidCredentialsError(UserError):
    """Authentication failed — username unknown, password wrong, or account locked.

    Deliberately ambiguous: we do not reveal which check failed, to
    prevent username enumeration. The single failure mode is the only
    safe public outcome.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "We couldn't sign you in. Please check your username and password and try again."
        )


class IncorrectCurrentPasswordError(UserError):
    """Self-service change-password: the supplied current password is wrong.

    Distinct from :class:`InvalidCredentialsError` because the user is
    already authenticated — there's no enumeration risk to protect
    against, so we can name the failure plainly.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__("The current password you entered is incorrect. Please try again.")


class AccountLockedError(UserError):
    """The account is temporarily locked after repeated failures.

    Call sites that don't already know who the user is should usually
    surface :class:`InvalidCredentialsError` instead, to avoid
    confirming whether the username exists.
    """

    def __init__(self, minutes_remaining: int) -> None:
        """Construct with a message that names the remaining cooldown."""
        super().__init__(
            f"This account is temporarily locked after too many failed "
            f"sign-in attempts. Try again in {minutes_remaining} minute(s) "
            "or contact your administrator."
        )
        self.minutes_remaining = minutes_remaining


class AccountDisabledError(UserError):
    """The user authenticated but their account has been deactivated."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__("This account has been deactivated. Please contact your administrator.")


class TooManyLoginAttemptsError(UserError):
    """Too many failed logins from one source IP — the IP is rate-limited.

    Defence in depth over per-account lockout: an attacker spreading
    guesses across many usernames from one IP trips this before any
    single account locks. Deliberately names no username, so it leaks
    nothing about which accounts exist.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "Too many sign-in attempts from your network. Please wait a few "
            "minutes and try again, or contact your administrator."
        )


class WeakPasswordError(UserError):
    """Candidate password fails the configured password policy."""

    def __init__(self, reason: str) -> None:
        """Construct with the policy-failure ``reason`` (already user-facing)."""
        super().__init__(reason)


class InvalidEmailError(UserError):
    """The supplied email address fails validation."""

    def __init__(self, reason: str) -> None:
        """Construct with the underlying validation ``reason``."""
        super().__init__(f"That email address doesn't look right: {reason}")


class DuplicateUserError(UserError):
    """The username or email is already in use by another account."""

    def __init__(self, field: str) -> None:
        """Construct with the conflicting ``field`` ("username" or "email")."""
        super().__init__(f"That {field} is already in use. Please choose a different one.")
        self.field = field


class SessionExpiredError(UserError):
    """The session has timed out (idle or hard ceiling)."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__("Your session has expired. Please sign in again.")


class SessionInvalidError(UserError):
    """The session token is unknown or has been revoked."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__("Your session is no longer valid. Please sign in again.")


class RoleNotFoundError(UserError):
    """The requested role doesn't exist."""

    def __init__(self, role_name: str) -> None:
        """Construct with the missing role's name."""
        super().__init__(
            f"There's no role called {role_name!r}. "
            "Pick one of the built-in roles or create a custom role first."
        )
        self.role_name = role_name


class CannotEditBuiltInRoleError(UserError):
    """Tried to delete or rename a built-in role."""

    def __init__(self, role_name: str) -> None:
        """Construct with the protected role's name."""
        super().__init__(
            f"The {role_name!r} role is built in to MATOFX and cannot be "
            "deleted or renamed. You can still tune its permissions "
            "(except for super_user, which is always granted every permission)."
        )
        self.role_name = role_name


class DuplicateRoleError(UserError):
    """A role (base or capability) with that name already exists."""

    def __init__(self, name: str) -> None:
        """Construct with the conflicting name."""
        super().__init__(
            f"A role called {name!r} already exists. "
            "Pick a different name, or edit the existing group."
        )
        self.name = name


class NotACapabilityRoleError(UserError):
    """Tried to stack a non-capability (base) role as a capability role.

    Only ``capability``-kind roles can be granted to a user on top of
    their base role (ADR-0005). A user's single base role is set
    separately via :meth:`RoleService.assign_role_to_user`.
    """

    def __init__(self, role_name: str) -> None:
        """Construct with the offending role's name."""
        super().__init__(
            f"The {role_name!r} role isn't a capability role, so it can't be "
            "stacked on top of a user's base role. Set a user's base role "
            "separately."
        )
        self.role_name = role_name


class CannotEditSuperUserPermissionsError(UserError):
    """Tried to change the super_user role's permission set.

    The super_user role is the install's break-glass admin and always
    holds every permission in the catalog. The set is sentinel-managed
    in code and cannot be edited at runtime.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "The super_user role's permissions cannot be edited — it always "
            "holds every permission in the catalog."
        )


class LastSuperUserError(UserError):
    """The action would leave the install with no active super-user."""

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "This install must always have at least one active super-user. "
            "Promote another user to super_user first, or keep this one's "
            "role as super_user."
        )


class SuperUserRoleChangeRequiresSuperUserError(UserError):
    """Only a super-user may grant or remove the super_user role.

    Holding ``roles.manage`` (or ``users.create_with_role``) is enough
    to manage ordinary roles, but adding super_user to an account or
    taking it away is reserved to super-users themselves — an
    administrator can neither promote someone to super_user nor demote a
    super-user. Prevents privilege escalation via the role surface.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "Only a super-user can grant or remove the super_user role. "
            "Ask a super-user to make this change."
        )


class RoleInUseError(UserError):
    """Tried to delete a role that's still assigned to one or more users."""

    def __init__(self, role_name: str, user_count: int) -> None:
        """Construct with the role's name and the count of users still on it."""
        super().__init__(
            f"The {role_name!r} role is still assigned to {user_count} user(s). "
            "Reassign them to a different role before deleting this one."
        )
        self.role_name = role_name
        self.user_count = user_count


class RoleDeletionForbiddenError(UserError):
    """Tried to delete an access-rights group, which is never allowed.

    Teams and access-rights groups are permanent once created — they are
    never deleted, even when empty, so the audit trail can always resolve
    a historical membership reference back to a named group. Removing the
    last member leaves the group in place with zero members.
    """

    def __init__(self, role_name: str) -> None:
        """Construct with the group's name for the plain-English message."""
        super().__init__(
            f"The {role_name!r} access-rights group can't be deleted. Groups are "
            "kept permanently for audit purposes, even with no members — remove "
            "its members instead if it's no longer in use."
        )
        self.role_name = role_name


class UnknownPermissionError(UserError):
    """A permission slug not in the catalog was supplied."""

    def __init__(self, permission: str) -> None:
        """Construct with the offending slug."""
        super().__init__(
            f"{permission!r} is not a permission MATOFX knows about. "
            "Permissions are defined by the application; ask your supplier "
            "to add it in a future release if you need a new gate point."
        )
        self.permission = permission


class PermissionKindMismatchError(UserError):
    """A permission was granted to the wrong tier of role (ADR-0005).

    Base roles and capability roles grant strictly non-overlapping
    permission sets: a **capability** role may hold only capability-tier
    (``*.curate``) permissions, and a **base** role may hold only the
    rest. Mixing them is refused so the two tiers stay cleanly
    distinguishable.
    """

    def __init__(self, permission: str, kind: str) -> None:
        """Construct with the offending slug and the role's kind."""
        if kind == "capability":
            message = (
                f"{permission!r} is a base-role permission and can't be added to a "
                "capability. Capabilities only carry curator permissions (the "
                "'.curate' rights)."
            )
        else:
            message = (
                f"{permission!r} is a curator permission and belongs to a capability, "
                "not a base role. Create or assign a capability for it instead."
            )
        super().__init__(message)
        self.permission = permission
        self.kind = kind


class PermissionDeniedError(UserError):
    """The caller is authenticated but lacks the required permission."""

    def __init__(self, permission: str) -> None:
        """Construct with the missing permission slug."""
        super().__init__(
            "You don't have permission to do that. Ask an administrator if you need access."
        )
        self.permission = permission


class TeamNotFoundError(UserError):
    """The requested team doesn't exist."""

    def __init__(self, identifier: str | int) -> None:
        """Construct with the team's name or id."""
        super().__init__(f"We couldn't find a team called {identifier!r}.")
        self.identifier = identifier


class DuplicateTeamError(UserError):
    """A team with that name already exists."""

    def __init__(self, name: str) -> None:
        """Construct with the conflicting name."""
        super().__init__(
            f"A team called {name!r} already exists. "
            "Pick a different name or assign users to the existing team."
        )
        self.name = name


class TeamInUseError(UserError):
    """Tried to delete a team that still has members."""

    def __init__(self, name: str, member_count: int) -> None:
        """Construct with the team's name and member count."""
        super().__init__(
            f"The team {name!r} still has {member_count} member(s). "
            "Remove them first, then delete the team."
        )
        self.name = name
        self.member_count = member_count


class AlreadyInTeamError(UserError):
    """User is already a member of the team."""

    def __init__(self, username: str, team_name: str) -> None:
        """Construct with the user and team."""
        super().__init__(f"{username!r} is already a member of {team_name!r}.")
        self.username = username
        self.team_name = team_name


class NotInTeamError(UserError):
    """User isn't a member of the team."""

    def __init__(self, username: str, team_name: str) -> None:
        """Construct with the user and team."""
        super().__init__(
            f"{username!r} isn't a member of {team_name!r}, so there's nothing to remove."
        )
        self.username = username
        self.team_name = team_name

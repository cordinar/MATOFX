"""Built-in user roles.

MATOFX ships four default roles, organised into two **privilege tiers**
plus an orthogonal **tool-state capability**:

**Privileged tier** — see and modify every project, manage users:

1. ``super_user`` — full control. Manages users (any role), installs /
   removes tools, rotates the audit signing key. Specific differences
   between super-user and administrator rights are still being
   defined; for now the two share the same hierarchy / data access in
   Phase 1.
2. ``administrator`` — operational control. Same hierarchy access as
   ``super_user`` in Phase 1; the boundary between the two roles is
   sharpened when administrative surfaces (user management, tool
   install, licence install) land later.

**Standard tier** — see and modify only their own projects:

3. ``beta_tester`` — same hierarchy rights as ``regular_user``. The
   *only* practical difference is visibility of tools in the ``Test``
   state (validators of pre-release tool deployments). Does **not**
   gain any extra access to other users' projects, scenarios, or jobs.
4. ``regular_user`` — the default role for new accounts. Sees only
   their own projects, runs only ``Released`` tools.

The orthogonal **tool-state model** (New / Test / Released) lands with
:mod:`matofx.tools`. In summary: ``New`` is author-only / privileged,
``Test`` is visible to ``beta_tester`` plus the privileged tier, and
``Released`` is visible to everyone with access to the tool's project
context.

Custom / customer-defined roles are not in scope for v1.0; if a
customer needs them, that's a v1.x conversation.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

UserRole = Literal["super_user", "administrator", "beta_tester", "regular_user"]

USER_ROLES: Final[tuple[UserRole, ...]] = get_args(UserRole)

DEFAULT_ROLE: Final[UserRole] = "regular_user"
"""The role assigned to new accounts unless the caller specifies otherwise."""

# Privilege rank for hierarchy / data access only. Lower = more privileged.
# ``beta_tester`` and ``regular_user`` share rank because they have identical
# data-access rights; their only difference is the orthogonal tool-state
# capability — see :func:`can_see_test_tools`.
_ROLE_RANK: Final[dict[UserRole, int]] = {
    "super_user": 0,
    "administrator": 1,
    "beta_tester": 2,
    "regular_user": 2,
}


def has_role_at_least(actual: str, threshold: UserRole) -> bool:
    """``True`` when ``actual``'s privilege rank is at or above ``threshold``.

    Examples:
        >>> has_role_at_least("super_user", "administrator")
        True
        >>> has_role_at_least("regular_user", "administrator")
        False
        >>> has_role_at_least("beta_tester", "regular_user")
        True
        >>> has_role_at_least("regular_user", "beta_tester")
        True

    ``beta_tester`` and ``regular_user`` are equivalent for this check
    — they share the same hierarchy rights. Use
    :func:`can_see_test_tools` to test the orthogonal beta-visibility
    capability.

    Unknown role strings are treated as below every threshold so
    foreign / corrupted values can never satisfy a privilege check.
    """
    if actual not in _ROLE_RANK:
        return False
    return _ROLE_RANK[actual] <= _ROLE_RANK[threshold]


def can_see_test_tools(role: str) -> bool:
    """``True`` when ``role`` may see and run tools in the ``Test`` state.

    The privileged tier (``super_user``, ``administrator``) and
    ``beta_tester`` qualify. ``regular_user`` does not.

    This capability is **orthogonal** to the privilege rank — a beta
    tester is no more powerful than a regular user in any other
    respect.
    """
    return role in {"super_user", "administrator", "beta_tester"}


def is_known_role(role: str) -> bool:
    """``True`` if ``role`` is one of the built-in roles."""
    return role in USER_ROLES

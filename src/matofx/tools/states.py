"""Tool lifecycle states.

Every installed tool carries one of three states:

- ``new`` — author-only / privileged tier. Work in progress, not yet
  ready for beta validation. Visibility requires the
  ``tools.see_new_state`` permission (default: super_user, administrator).
- ``test`` — pre-release. Visibility requires the ``tools.see_test_state``
  permission (default: super_user, administrator, beta_tester).
- ``released`` — generally available. Visible to every authenticated user.

Promotion is one-directional in normal flow (``new → test → released``),
but rollback is allowed for anyone holding ``tools.promote_state`` when
something is discovered to be broken in production. The closed
vocabulary is enforced by a DB-level CHECK constraint so corrupted
values can't appear.

Because visibility is permission-driven (not role-name-driven), an
admin can grant ``tools.see_test_state`` to a custom role (e.g. a
"QA Lead" they've created) without touching the codebase.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, get_args

from matofx.users.permissions import Permission, user_has_permission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ToolState = Literal["new", "test", "released"]

TOOL_STATES: Final[tuple[ToolState, ...]] = get_args(ToolState)

DEFAULT_TOOL_STATE: Final[ToolState] = "new"
"""Newly-installed tools start in ``new`` and are promoted from there."""


async def user_can_see_state(session: AsyncSession, user_id: int, tool_state: str) -> bool:
    """``True`` when ``user_id`` may see tools in ``tool_state``.

    Permission mapping:

    - ``released`` — every authenticated user can see released tools.
    - ``test`` — requires ``tools.see_test_state``.
    - ``new`` — requires ``tools.see_new_state``.

    Unknown tool states return ``False``.
    """
    if tool_state == "released":
        # Released tools are visible to anyone who can be looked up at all.
        # We still confirm the user exists to avoid leaking the tool to
        # disabled or deleted users (look-up returns False for unknown ids).
        from matofx.users.orm import User  # noqa: PLC0415

        user = await session.get(User, user_id)
        return user is not None and user.is_active
    if tool_state == "test":
        return await user_has_permission(session, user_id, Permission.TOOLS_SEE_TEST_STATE)
    if tool_state == "new":
        return await user_has_permission(session, user_id, Permission.TOOLS_SEE_NEW_STATE)
    return False

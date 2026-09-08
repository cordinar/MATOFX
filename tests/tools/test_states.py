"""Tests for the tool-state visibility primitive.

Visibility is now permission-driven: ``user_can_see_state`` looks up
the user's role's permissions. The matrix below is what the *default*
seeded permission sets produce; an admin who grants
``tools.see_test_state`` to a custom role will, by design, find their
custom role passes the test for ``test``-state tools.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.tools import DEFAULT_TOOL_STATE, TOOL_STATES, user_can_see_state


def test_default_state_is_new() -> None:
    assert DEFAULT_TOOL_STATE == "new"


def test_three_built_in_states() -> None:
    assert TOOL_STATES == ("new", "test", "released")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role_name", "state", "expected"),
    [
        # released — every active user
        ("super_user", "released", True),
        ("administrator", "released", True),
        ("beta_tester", "released", True),
        ("regular_user", "released", True),
        # test — privileged tier and beta_tester
        ("super_user", "test", True),
        ("administrator", "test", True),
        ("beta_tester", "test", True),
        ("regular_user", "test", False),
        # new — privileged tier only
        ("super_user", "new", True),
        ("administrator", "new", True),
        ("beta_tester", "new", False),
        ("regular_user", "new", False),
    ],
)
async def test_default_visibility_matrix(
    tools_engine: AsyncEngine,
    seed_roles: dict[str, int],
    role_name: str,
    state: str,
    expected: bool,
) -> None:
    factory = make_session_factory(tools_engine)
    user_id = seed_roles[role_name]
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        result = await user_can_see_state(session, user_id, state)
    assert result is expected


@pytest.mark.asyncio
async def test_unknown_state_visible_to_nobody(
    tools_engine: AsyncEngine,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        for user_id in seed_roles.values():
            assert await user_can_see_state(session, user_id, "exploded") is False


@pytest.mark.asyncio
async def test_unknown_user_sees_nothing(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        for state in TOOL_STATES:
            assert await user_can_see_state(session, user_id=99999, tool_state=state) is False

"""Workflow member-visibility cascade.

A workflow must never be visible to a user who can't see one of its
member tools — whatever the reason the tool is hidden (pre-release
``new`` / ``test`` state, or a team the user doesn't belong to). This
is the cascade rule of :func:`matofx.workflows.service.can_user_see_workflow`,
exercised here through the public ``list_visible`` surface.

Regression guard: an earlier implementation only cascaded on ``new``
members, so a ``released`` workflow containing a ``test`` or
team-restricted tool leaked to users who couldn't see that tool.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.tools import ToolService
from matofx.users.team_orm import Team
from matofx.workflows import WorkflowService
from matofx.workflows.service import WorkflowDraft

from .conftest import make_tool_descriptor

_PROMOTE_PATH = {"new": (), "test": ("test",), "released": ("test", "released")}


async def _install_tool(
    factory,
    tool_service: ToolService,
    *,
    admin_id: int,
    alias: str,
    state: str = "released",
    teams: list[str] | None = None,
) -> str:
    """Install a tool and walk it to ``state`` (default ``released``)."""
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=teams or ["World"]),
            bundle_path=f"/tools/{alias}",
        )
    for target in _PROMOTE_PATH[state]:
        async with session_scope(factory) as session:
            await tool_service.promote_state(
                session, actor_user_id=admin_id, alias=alias, new_state=target
            )
    return alias


async def _install_released_workflow(
    factory,
    workflow_service: WorkflowService,
    *,
    admin_id: int,
    alias: str,
    tool_aliases: list[str],
    teams: list[str] | None = None,
) -> int:
    """Install an admin workflow and promote it to ``released``."""
    async with session_scope(factory) as session:
        wf = await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias=alias,
                name=alias.title(),
                description="",
                teams=teams or ["World"],
                tool_aliases=tool_aliases,
            ),
        )
        wf_id = wf.id
    for target in ("test", "released"):
        async with session_scope(factory) as session:
            await workflow_service.promote_state(
                session, actor_user_id=admin_id, alias=alias, new_state=target
            )
    return wf_id


async def _visible_aliases(
    factory, workflow_service: WorkflowService, *, actor_user_id: int
) -> set[str]:
    async with session_scope(factory) as session:
        rows = await workflow_service.list_visible(session, actor_user_id=actor_user_id)
        return {wf.alias for wf in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize("member_state", ["test", "new"])
async def test_released_workflow_with_prerelease_member_hidden_from_regular_user(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
    member_state: str,
) -> None:
    """A released workflow with a non-released member tool is hidden from
    a regular user who can't see that tool — even though the workflow's
    own state is ``released``."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    regular_id = seed_roles["regular_user"]

    await _install_tool(factory, tool_service, admin_id=admin_id, alias="visible")
    await _install_tool(
        factory, tool_service, admin_id=admin_id, alias="hidden", state=member_state
    )
    await _install_released_workflow(
        factory,
        workflow_service,
        admin_id=admin_id,
        alias="wf",
        tool_aliases=["visible", "hidden"],
    )

    assert "wf" not in await _visible_aliases(
        factory, workflow_service, actor_user_id=regular_id
    )
    # Admin (holds tools.see_new_state) sees everything regardless.
    assert "wf" in await _visible_aliases(
        factory, workflow_service, actor_user_id=admin_id
    )


@pytest.mark.asyncio
async def test_released_workflow_with_test_member_visible_to_beta_tester(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """A beta tester holds tools.see_test_state, so a workflow whose only
    pre-release member is in ``test`` is visible to them."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    beta_id = seed_roles["beta_tester"]

    await _install_tool(factory, tool_service, admin_id=admin_id, alias="visible")
    await _install_tool(
        factory, tool_service, admin_id=admin_id, alias="beta_only", state="test"
    )
    await _install_released_workflow(
        factory,
        workflow_service,
        admin_id=admin_id,
        alias="wf",
        tool_aliases=["visible", "beta_only"],
    )

    assert "wf" in await _visible_aliases(
        factory, workflow_service, actor_user_id=beta_id
    )


@pytest.mark.asyncio
async def test_released_workflow_with_team_restricted_member_hidden(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """A released, World-scoped workflow whose member tool is restricted
    to a team the user isn't in is hidden — the cascade covers the team
    gate, not just lifecycle state."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    regular_id = seed_roles["regular_user"]

    # A tool descriptor may only reference teams that exist on the install.
    async with session_scope(factory) as session:
        session.add(Team(name="Aero", description="", created_by_user_id=admin_id))

    await _install_tool(factory, tool_service, admin_id=admin_id, alias="open")
    await _install_tool(
        factory,
        tool_service,
        admin_id=admin_id,
        alias="aero_only",
        state="released",
        teams=["Aero"],
    )
    await _install_released_workflow(
        factory,
        workflow_service,
        admin_id=admin_id,
        alias="wf",
        tool_aliases=["open", "aero_only"],
        teams=["World"],
    )

    # Regular user belongs to no teams → can't see the Aero-only member.
    assert "wf" not in await _visible_aliases(
        factory, workflow_service, actor_user_id=regular_id
    )


@pytest.mark.asyncio
async def test_released_workflow_with_all_members_visible_is_shown(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Control: when every member is released + World, a regular user
    sees the released workflow."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    regular_id = seed_roles["regular_user"]

    await _install_tool(factory, tool_service, admin_id=admin_id, alias="alpha")
    await _install_tool(factory, tool_service, admin_id=admin_id, alias="beta")
    await _install_released_workflow(
        factory,
        workflow_service,
        admin_id=admin_id,
        alias="wf",
        tool_aliases=["alpha", "beta"],
    )

    assert "wf" in await _visible_aliases(
        factory, workflow_service, actor_user_id=regular_id
    )

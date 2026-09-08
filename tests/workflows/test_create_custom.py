"""Tests for :meth:`WorkflowService.create_custom`.

Covers the happy paths, permission gates, member-count limits, tool
visibility, alias synthesis collisions, and the share-recipient
validation matrix.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.tools import ToolService
from matofx.users import UserService
from matofx.users.team_orm import Team, UserTeam
from matofx.workflows import (
    CUSTOM_WORKFLOW_STATE,
    Workflow,
    WorkflowKind,
    WorkflowMember,
    WorkflowService,
    WorkflowShare,
    WorkflowVisibility,
)
from matofx.workflows.errors import (
    ArchivedToolMemberError,
    DuplicateMemberError,
    InvalidShareRecipientError,
    ToolNotVisibleToRecipientError,
    TooFewMembersError,
    TooManyMembersError,
    UnknownToolMemberError,
    WorkflowStateError,
)
from matofx.workflows.service import CustomWorkflowDraft

from .conftest import install_released_tool, make_tool_descriptor


@pytest.mark.asyncio
async def test_create_with_two_tools_no_share(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """The simplest possible custom workflow — owner only, no wiring."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]

    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )

    async with session_scope(factory) as session:
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="My pair",
                description="Two tools, no fuss.",
                tool_aliases=["alpha", "beta"],
            ),
        )

    assert wf.kind == WorkflowKind.CUSTOM.value
    assert wf.state == CUSTOM_WORKFLOW_STATE
    assert wf.owner_id == owner_id
    assert wf.installed_by_user_id == owner_id
    assert wf.visibility == WorkflowVisibility.PRIVATE.value
    assert wf.alias.startswith(f"custom_{owner_id}_")
    assert wf.name == "My pair"
    assert wf.archived_at is None

    async with session_scope(factory) as session:
        members = list(
            (
                await session.execute(
                    select(WorkflowMember)
                    .where(WorkflowMember.workflow_id == wf.id)
                    .order_by(WorkflowMember.position)
                )
            ).scalars()
        )
        shares = list(
            (
                await session.execute(
                    select(WorkflowShare).where(WorkflowShare.workflow_id == wf.id)
                )
            ).scalars()
        )

    assert [m.tool_alias for m in members] == ["alpha", "beta"]
    assert [m.position for m in members] == [1, 2]
    assert shares == []


@pytest.mark.asyncio
async def test_create_orders_via_descriptor_chain(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """A → B → C chain via downstream_tools — saved in dependency order."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]

    descriptors = [
        make_tool_descriptor(alias="tool_a", downstream_tools=["tool_b"], teams=["World"]),
        make_tool_descriptor(alias="tool_b", downstream_tools=["tool_c"], teams=["World"]),
        make_tool_descriptor(alias="tool_c", teams=["World"]),
    ]
    for d in descriptors:
        await install_released_tool(
            factory, tool_service, actor_user_id=admin_id, descriptor=d
        )

    async with session_scope(factory) as session:
        # Caller picks them in scrambled order — service should fix.
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Chain",
                description="Three tools wired into a chain.",
                tool_aliases=["tool_c", "tool_a", "tool_b"],
            ),
        )

    async with session_scope(factory) as session:
        members = list(
            (
                await session.execute(
                    select(WorkflowMember)
                    .where(WorkflowMember.workflow_id == wf.id)
                    .order_by(WorkflowMember.position)
                )
            ).scalars()
        )
    assert [m.tool_alias for m in members] == ["tool_a", "tool_b", "tool_c"]


@pytest.mark.asyncio
async def test_create_with_share_recipients(
    workflows_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Share list populated → visibility='shared' + share rows present."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]

    # Make two more regular users to receive the share.
    async with session_scope(factory) as session:
        bob = await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="securepassword1",
            full_name="Bob",
            role="regular_user",
        )
        carol = await user_service.create_user(
            session,
            username="carol",
            email="carol@example.com",
            password="securepassword1",
            full_name="Carol",
            role="regular_user",
        )

    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )

    async with session_scope(factory) as session:
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Shared pair",
                description="With recipients.",
                tool_aliases=["alpha", "beta"],
                share_with_user_ids=[bob.id, carol.id],
            ),
        )

    assert wf.visibility == WorkflowVisibility.SHARED.value
    async with session_scope(factory) as session:
        shares = list(
            (
                await session.execute(
                    select(WorkflowShare)
                    .where(WorkflowShare.workflow_id == wf.id)
                    .order_by(WorkflowShare.user_id)
                )
            ).scalars()
        )
    assert sorted(s.user_id for s in shares) == sorted([bob.id, carol.id])
    for share in shares:
        assert share.shared_by_user_id == owner_id


@pytest.mark.asyncio
async def test_create_alias_collision_bumps_suffix(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Same owner + same name twice → second alias gets a _2 suffix."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )

    async with session_scope(factory) as session:
        first = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Same name",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        second = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Same name",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    assert first.alias != second.alias
    assert second.alias.endswith("_2")


# ----- permission + count gates -----------------------------------------


@pytest.mark.asyncio
async def test_super_user_can_create(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """super_user gets CUSTOM_WORKFLOWS_CREATE via the catalog sentinel."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    su_id = seed_roles["super_user"]
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=su_id,
            draft=CustomWorkflowDraft(
                name="SU workflow",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    assert wf.owner_id == su_id


@pytest.mark.asyncio
async def test_too_few_members_rejected(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="solo", teams=["World"]),
    )
    async with session_scope(factory) as session:
        with pytest.raises(TooFewMembersError):
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="Lonely",
                    description="",
                    tool_aliases=["solo"],
                ),
            )


@pytest.mark.asyncio
async def test_too_many_members_rejected(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    aliases = [f"t{i}" for i in range(11)]
    for alias in aliases:
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        with pytest.raises(TooManyMembersError):
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="Too big",
                    description="",
                    tool_aliases=aliases,
                ),
            )


@pytest.mark.asyncio
async def test_duplicate_tool_in_selection_rejected(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateMemberError):
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="Dupes",
                    description="",
                    tool_aliases=["alpha", "alpha"],
                ),
            )


@pytest.mark.asyncio
async def test_unknown_tool_rejected(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="alpha", teams=["World"]),
    )
    async with session_scope(factory) as session:
        with pytest.raises(UnknownToolMemberError):
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="Missing tool",
                    description="",
                    tool_aliases=["alpha", "ghost"],
                ),
            )


@pytest.mark.asyncio
async def test_archived_tool_rejected(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        await tool_service.archive(
            session,
            actor_user_id=admin_id,
            alias="beta",
            reason="retired",
        )
    async with session_scope(factory) as session:
        with pytest.raises(ArchivedToolMemberError):
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="With archived",
                    description="",
                    tool_aliases=["alpha", "beta"],
                ),
            )


# ----- visibility + sharing validation -----------------------------------


@pytest.mark.asyncio
async def test_owner_cannot_pick_invisible_tool(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Tool restricted to a team the owner isn't in → reads as unknown."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]

    # Create a team that the owner is NOT a member of.
    async with session_scope(factory) as session:
        team = Team(name="Mechanical", description="")
        session.add(team)
        await session.flush()
        team_id = team.id

    for alias in ("alpha",):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    # Restricted tool: only visible to Mechanical team members.
    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="restricted", teams=["Mechanical"]),
    )
    async with session_scope(factory) as session:
        with pytest.raises(UnknownToolMemberError):
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="Mixed",
                    description="",
                    tool_aliases=["alpha", "restricted"],
                ),
            )
    # Sanity: with the owner *in* Mechanical, it should now succeed.
    async with session_scope(factory) as session:
        session.add(UserTeam(user_id=owner_id, team_id=team_id))
        await session.flush()
    async with session_scope(factory) as session:
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Mixed",
                description="",
                tool_aliases=["alpha", "restricted"],
            ),
        )
    assert wf.kind == WorkflowKind.CUSTOM.value


@pytest.mark.asyncio
async def test_share_to_self_rejected(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        with pytest.raises(InvalidShareRecipientError):
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="Self share",
                    description="",
                    tool_aliases=["alpha", "beta"],
                    share_with_user_ids=[owner_id],
                ),
            )


@pytest.mark.asyncio
async def test_share_to_missing_user_rejected(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        with pytest.raises(InvalidShareRecipientError):
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="Ghost share",
                    description="",
                    tool_aliases=["alpha", "beta"],
                    share_with_user_ids=[999_999],
                ),
            )


@pytest.mark.asyncio
async def test_share_to_deactivated_user_rejected(
    workflows_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]

    async with session_scope(factory) as session:
        bob = await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="securepassword1",
            full_name="Bob",
            role="regular_user",
        )
        bob_id = bob.id
    async with session_scope(factory) as session:
        await user_service.deactivate(
            session,
            user_id=bob_id,
            actor="ada",
            actor_user_id=admin_id,
        )

    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        with pytest.raises(InvalidShareRecipientError):
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="Deactivated share",
                    description="",
                    tool_aliases=["alpha", "beta"],
                    share_with_user_ids=[bob_id],
                ),
            )


@pytest.mark.asyncio
async def test_share_recipient_cannot_see_member_tool(
    workflows_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Owner is in Mechanical team; recipient isn't. Tool restricted to
    Mechanical. Share rejected with ToolNotVisibleToRecipientError."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]

    async with session_scope(factory) as session:
        bob = await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="securepassword1",
            full_name="Bob",
            role="regular_user",
        )
        bob_id = bob.id

    async with session_scope(factory) as session:
        team = Team(name="Mechanical", description="")
        session.add(team)
        await session.flush()
        session.add(UserTeam(user_id=owner_id, team_id=team.id))
        await session.flush()

    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="alpha", teams=["World"]),
    )
    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="restricted", teams=["Mechanical"]),
    )

    async with session_scope(factory) as session:
        with pytest.raises(ToolNotVisibleToRecipientError) as exc_info:
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="Bob can't see",
                    description="",
                    tool_aliases=["alpha", "restricted"],
                    share_with_user_ids=[bob_id],
                ),
            )
    assert exc_info.value.tool_alias == "restricted"
    assert exc_info.value.recipient_username == "bob"


# ----- permission gate ---------------------------------------------------


@pytest.mark.asyncio
async def test_user_without_create_permission_rejected(
    workflows_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """A user whose role lost CUSTOM_WORKFLOWS_CREATE can't create one.

    We construct this by deleting the default seed for regular_user so
    the gate has something to refuse against — the catalog says
    regular_user holds it by default.
    """
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]

    from sqlalchemy import delete as sa_delete  # noqa: PLC0415

    from matofx.users.role_orm import Role, RolePermission  # noqa: PLC0415

    async with session_scope(factory) as session:
        role_id = (
            await session.execute(
                select(Role.id).where(Role.name == "regular_user")
            )
        ).scalar_one()
        await session.execute(
            sa_delete(RolePermission).where(
                RolePermission.role_id == role_id,
                RolePermission.permission == "custom_workflows.create",
            )
        )

    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        with pytest.raises(WorkflowStateError, match="custom_workflows.create"):
            await workflow_service.create_custom(
                session,
                actor_user_id=owner_id,
                draft=CustomWorkflowDraft(
                    name="Should fail",
                    description="",
                    tool_aliases=["alpha", "beta"],
                ),
            )


# ----- DB-shape sanity ---------------------------------------------------


@pytest.mark.asyncio
async def test_created_row_passes_combined_check_constraint(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Custom row makes it through the kind/state/owner/visibility CHECK."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Sanity",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        loaded = await session.get(Workflow, wf.id)
    assert loaded is not None
    assert loaded.kind == "custom"
    assert loaded.state == "released"
    assert loaded.owner_id == owner_id
    assert loaded.visibility == "private"

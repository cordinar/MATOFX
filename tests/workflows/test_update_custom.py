"""Tests for :meth:`WorkflowService.update_custom`."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.tools import ToolService
from matofx.users import UserService
from matofx.workflows import (
    Workflow,
    WorkflowMember,
    WorkflowService,
    WorkflowShare,
    WorkflowVisibility,
)
from matofx.workflows.errors import (
    NotCustomWorkflowError,
    NotOwnerError,
    ToolNotVisibleToRecipientError,
    WorkflowArchivedError,
    WorkflowNotFoundError,
)
from matofx.workflows.service import CustomWorkflowDraft, WorkflowDraft

from .conftest import install_released_tool, make_tool_descriptor


async def _seed_simple_workflow(
    workflow_service: WorkflowService,
    factory,
    *,
    owner_id: int,
    name: str = "Initial",
    aliases: tuple[str, ...] = ("alpha", "beta"),
) -> Workflow:
    async with session_scope(factory) as session:
        return await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name=name,
                description="",
                tool_aliases=list(aliases),
            ),
        )


@pytest.mark.asyncio
async def test_owner_can_rename(
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
    wf = await _seed_simple_workflow(workflow_service, factory, owner_id=owner_id)
    before_version = wf.version

    async with session_scope(factory) as session:
        updated = await workflow_service.update_custom(
            session,
            actor_user_id=owner_id,
            workflow_id=wf.id,
            draft=CustomWorkflowDraft(
                name="Renamed",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    assert updated.name == "Renamed"
    # Metadata-only change bumps the patch component.
    assert updated.version != before_version


@pytest.mark.asyncio
async def test_member_change_bumps_minor(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    for alias in ("alpha", "beta", "gamma"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    wf = await _seed_simple_workflow(workflow_service, factory, owner_id=owner_id)
    initial_version = wf.version  # e.g. "0.1.0"

    async with session_scope(factory) as session:
        updated = await workflow_service.update_custom(
            session,
            actor_user_id=owner_id,
            workflow_id=wf.id,
            draft=CustomWorkflowDraft(
                name="Initial",
                description="",
                tool_aliases=["alpha", "beta", "gamma"],
            ),
        )

    # 0.1.0 → 0.2.0 on a minor bump (member list grew).
    assert updated.version.split(".")[1] != initial_version.split(".")[1]

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
    assert sorted(m.tool_alias for m in members) == ["alpha", "beta", "gamma"]


@pytest.mark.asyncio
async def test_share_list_reconciled(
    workflows_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Editing the share list adds new rows and deletes removed ones.

    Also flips ``visibility`` between ``private`` and ``shared`` based
    on whether any recipients remain.
    """
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
        carol = await user_service.create_user(
            session,
            username="carol",
            email="carol@example.com",
            password="securepassword1",
            full_name="Carol",
            role="regular_user",
        )
        bob_id, carol_id = bob.id, carol.id

    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )

    # Start private.
    wf = await _seed_simple_workflow(workflow_service, factory, owner_id=owner_id)
    assert wf.visibility == WorkflowVisibility.PRIVATE.value

    # Add Bob — flips to shared.
    async with session_scope(factory) as session:
        wf = await workflow_service.update_custom(
            session,
            actor_user_id=owner_id,
            workflow_id=wf.id,
            draft=CustomWorkflowDraft(
                name="Initial",
                description="",
                tool_aliases=["alpha", "beta"],
                share_with_user_ids=[bob_id],
            ),
        )
    assert wf.visibility == WorkflowVisibility.SHARED.value
    async with session_scope(factory) as session:
        shares = (
            await session.execute(
                select(WorkflowShare.user_id).where(WorkflowShare.workflow_id == wf.id)
            )
        ).scalars().all()
    assert sorted(shares) == [bob_id]

    # Swap Bob for Carol — Bob's row deleted, Carol's added.
    async with session_scope(factory) as session:
        wf = await workflow_service.update_custom(
            session,
            actor_user_id=owner_id,
            workflow_id=wf.id,
            draft=CustomWorkflowDraft(
                name="Initial",
                description="",
                tool_aliases=["alpha", "beta"],
                share_with_user_ids=[carol_id],
            ),
        )
    async with session_scope(factory) as session:
        shares = (
            await session.execute(
                select(WorkflowShare.user_id).where(WorkflowShare.workflow_id == wf.id)
            )
        ).scalars().all()
    assert sorted(shares) == [carol_id]

    # Empty list flips back to private + clears all share rows.
    async with session_scope(factory) as session:
        wf = await workflow_service.update_custom(
            session,
            actor_user_id=owner_id,
            workflow_id=wf.id,
            draft=CustomWorkflowDraft(
                name="Initial",
                description="",
                tool_aliases=["alpha", "beta"],
                share_with_user_ids=[],
            ),
        )
    assert wf.visibility == WorkflowVisibility.PRIVATE.value
    async with session_scope(factory) as session:
        shares = (
            await session.execute(
                select(WorkflowShare.user_id).where(WorkflowShare.workflow_id == wf.id)
            )
        ).scalars().all()
    assert shares == []


@pytest.mark.asyncio
async def test_non_owner_refused(
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
        intruder = await user_service.create_user(
            session,
            username="eve",
            email="eve@example.com",
            password="securepassword1",
            full_name="Eve",
            role="regular_user",
        )
        intruder_id = intruder.id

    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    wf = await _seed_simple_workflow(workflow_service, factory, owner_id=owner_id)

    async with session_scope(factory) as session:
        with pytest.raises(NotOwnerError):
            await workflow_service.update_custom(
                session,
                actor_user_id=intruder_id,
                workflow_id=wf.id,
                draft=CustomWorkflowDraft(
                    name="Stolen",
                    description="",
                    tool_aliases=["alpha", "beta"],
                ),
            )


@pytest.mark.asyncio
async def test_admin_workflow_refused(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """update_custom on an admin (kind='admin') workflow rejects."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        admin_wf = await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias="admin_wf",
                name="Admin WF",
                description="",
                teams=["World"],
                tool_aliases=["alpha", "beta"],
            ),
        )

    async with session_scope(factory) as session:
        with pytest.raises(NotCustomWorkflowError):
            await workflow_service.update_custom(
                session,
                actor_user_id=admin_id,
                workflow_id=admin_wf.id,
                draft=CustomWorkflowDraft(
                    name="Hijack",
                    description="",
                    tool_aliases=["alpha", "beta"],
                ),
            )


@pytest.mark.asyncio
async def test_archived_workflow_refused(
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
    wf = await _seed_simple_workflow(workflow_service, factory, owner_id=owner_id)

    # Archive via direct ORM (the owner-driven archive method comes
    # in task #10 — until then this is the minimum we need to drive
    # the test).
    async with session_scope(factory) as session:
        loaded = await session.get(Workflow, wf.id)
        assert loaded is not None
        from datetime import UTC, datetime  # noqa: PLC0415

        loaded.archived_at = datetime.now(tz=UTC)
        loaded.archived_by_user_id = owner_id
        await session.flush()

    async with session_scope(factory) as session:
        with pytest.raises(WorkflowArchivedError):
            await workflow_service.update_custom(
                session,
                actor_user_id=owner_id,
                workflow_id=wf.id,
                draft=CustomWorkflowDraft(
                    name="Still trying",
                    description="",
                    tool_aliases=["alpha", "beta"],
                ),
            )


@pytest.mark.asyncio
async def test_unknown_workflow_id_raises(
    workflows_engine: AsyncEngine,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    async with session_scope(factory) as session:
        with pytest.raises(WorkflowNotFoundError):
            await workflow_service.update_custom(
                session,
                actor_user_id=seed_roles["regular_user"],
                workflow_id=999_999,
                draft=CustomWorkflowDraft(
                    name="Phantom",
                    description="",
                    tool_aliases=["alpha", "beta"],
                ),
            )


@pytest.mark.asyncio
async def test_share_validation_runs_against_new_member_set(
    workflows_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Adding a restricted tool while sharing with a non-team member fails.

    Models: Bob is on the share list, can see the original tools.
    Owner edits the workflow to *add* a tool restricted to a team Bob
    isn't in. The update is rejected with
    :class:`ToolNotVisibleToRecipientError` — Bob's visibility is
    re-validated against the new member set.
    """
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

    # Owner is in Mechanical; Bob is not.
    async with session_scope(factory) as session:
        from matofx.users.team_orm import Team, UserTeam  # noqa: PLC0415

        team = Team(name="Mechanical", description="")
        session.add(team)
        await session.flush()
        session.add(UserTeam(user_id=owner_id, team_id=team.id))
        await session.flush()

    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="restricted", teams=["Mechanical"]),
    )

    # Initial workflow + share with Bob (all members visible to him).
    async with session_scope(factory) as session:
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Shared with Bob",
                description="",
                tool_aliases=["alpha", "beta"],
                share_with_user_ids=[bob_id],
            ),
        )

    # Now add the restricted tool — Bob can't see it, so the edit fails.
    async with session_scope(factory) as session:
        with pytest.raises(ToolNotVisibleToRecipientError):
            await workflow_service.update_custom(
                session,
                actor_user_id=owner_id,
                workflow_id=wf.id,
                draft=CustomWorkflowDraft(
                    name="Shared with Bob",
                    description="",
                    tool_aliases=["alpha", "beta", "restricted"],
                    share_with_user_ids=[bob_id],
                ),
            )


@pytest.mark.asyncio
async def test_reordered_members_via_descriptor_chain(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Replacing members keeps the position table in the computed order."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]

    for d in (
        make_tool_descriptor(
            alias="tool_a", downstream_tools=["tool_b"], teams=["World"]
        ),
        make_tool_descriptor(
            alias="tool_b", downstream_tools=["tool_c"], teams=["World"]
        ),
        make_tool_descriptor(alias="tool_c", teams=["World"]),
    ):
        await install_released_tool(
            factory, tool_service, actor_user_id=admin_id, descriptor=d
        )

    wf = await _seed_simple_workflow(
        workflow_service, factory, owner_id=owner_id, aliases=("tool_a", "tool_b")
    )

    async with session_scope(factory) as session:
        await workflow_service.update_custom(
            session,
            actor_user_id=owner_id,
            workflow_id=wf.id,
            draft=CustomWorkflowDraft(
                name="Initial",
                description="",
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

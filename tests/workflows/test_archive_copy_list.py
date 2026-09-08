"""Tests for archive_custom / copy_to_owner / list_for_user."""

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
    WorkflowError,
    WorkflowNotFoundError,
)
from matofx.workflows.service import CustomWorkflowDraft, WorkflowDraft

from .conftest import install_released_tool, make_tool_descriptor


# ----- archive_custom ----------------------------------------------------


@pytest.mark.asyncio
async def test_owner_archives_with_optional_reason(
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
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Archive me",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )

    async with session_scope(factory) as session:
        archived = await workflow_service.archive_custom(
            session,
            actor_user_id=owner_id,
            workflow_id=wf.id,
        )
    assert archived.archived_at is not None
    assert archived.archive_reason == "Archived by owner"
    assert archived.archived_by_user_id == owner_id


@pytest.mark.asyncio
async def test_admin_archives_requires_reason(
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
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Admin will archive",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )

    # Admin archive without reason → refused.
    async with session_scope(factory) as session:
        with pytest.raises(WorkflowError, match="reason"):
            await workflow_service.archive_custom(
                session,
                actor_user_id=admin_id,
                workflow_id=wf.id,
            )

    # Admin archive with reason → succeeds, reason recorded.
    async with session_scope(factory) as session:
        archived = await workflow_service.archive_custom(
            session,
            actor_user_id=admin_id,
            workflow_id=wf.id,
            reason="Investigating a stuck run.",
        )
    assert archived.archive_reason == "Investigating a stuck run."
    assert archived.archived_by_user_id == admin_id


@pytest.mark.asyncio
async def test_non_owner_non_inspector_refused(
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
        eve = await user_service.create_user(
            session,
            username="eve",
            email="eve@example.com",
            password="securepassword1",
            full_name="Eve",
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
                name="Mine",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        with pytest.raises(NotOwnerError):
            await workflow_service.archive_custom(
                session,
                actor_user_id=eve.id,
                workflow_id=wf.id,
            )


@pytest.mark.asyncio
async def test_archive_idempotent(
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
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Once",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        first = await workflow_service.archive_custom(
            session, actor_user_id=owner_id, workflow_id=wf.id
        )
    async with session_scope(factory) as session:
        again = await workflow_service.archive_custom(
            session, actor_user_id=owner_id, workflow_id=wf.id
        )
    # Both calls land on the same archived_at — comparing as naive
    # timestamps because SQLite drops tzinfo on the round-trip while
    # the first call still carries it. The wall-clock value is what
    # matters for idempotency.
    assert first.archived_at is not None
    assert again.archived_at is not None
    assert first.archived_at.replace(tzinfo=None) == again.archived_at.replace(
        tzinfo=None
    )


@pytest.mark.asyncio
async def test_owner_unarchives_and_state_returns(
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
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Round trip",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        await workflow_service.archive_custom(
            session, actor_user_id=owner_id, workflow_id=wf.id
        )
    async with session_scope(factory) as session:
        revived = await workflow_service.unarchive_custom(
            session, actor_user_id=owner_id, workflow_id=wf.id
        )
    assert revived.archived_at is None
    assert revived.archive_reason is None
    assert revived.archived_by_user_id is None


@pytest.mark.asyncio
async def test_unarchive_non_owner_refused(
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
        eve = await user_service.create_user(
            session,
            username="eve",
            email="eve@example.com",
            password="securepassword1",
            full_name="Eve",
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
                name="Mine",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        await workflow_service.archive_custom(
            session, actor_user_id=owner_id, workflow_id=wf.id
        )
    async with session_scope(factory) as session:
        with pytest.raises(NotOwnerError):
            await workflow_service.unarchive_custom(
                session, actor_user_id=eve.id, workflow_id=wf.id
            )


@pytest.mark.asyncio
async def test_unarchive_idempotent_on_active_row(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Calling unarchive on an already-active row is a no-op."""
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
                name="Active",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        result = await workflow_service.unarchive_custom(
            session, actor_user_id=owner_id, workflow_id=wf.id
        )
    assert result.archived_at is None


@pytest.mark.asyncio
async def test_archive_admin_workflow_refused(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """archive_custom on an admin workflow raises NotCustomWorkflowError."""
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
                alias="adm",
                name="Adm",
                description="",
                teams=["World"],
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        with pytest.raises(NotCustomWorkflowError):
            await workflow_service.archive_custom(
                session, actor_user_id=admin_id, workflow_id=admin_wf.id
            )


# ----- copy_to_owner -----------------------------------------------------


@pytest.mark.asyncio
async def test_share_recipient_copies_to_their_own_list(
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
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        source = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Alice's pair",
                description="",
                tool_aliases=["alpha", "beta"],
                share_with_user_ids=[bob_id],
            ),
        )

    async with session_scope(factory) as session:
        copy = await workflow_service.copy_to_owner(
            session,
            actor_user_id=bob_id,
            source_workflow_id=source.id,
        )

    assert copy.owner_id == bob_id
    assert copy.visibility == WorkflowVisibility.PRIVATE.value
    assert copy.name == "Alice's pair (copy)"
    assert copy.alias != source.alias
    assert isinstance(copy.descriptor, dict)
    assert copy.descriptor["copied_from_workflow_id"] == source.id

    # Source share list untouched.
    async with session_scope(factory) as session:
        copy_shares = (
            await session.execute(
                select(WorkflowShare.user_id).where(
                    WorkflowShare.workflow_id == copy.id
                )
            )
        ).scalars().all()
    assert copy_shares == []

    # Members preserved.
    async with session_scope(factory) as session:
        members = list(
            (
                await session.execute(
                    select(WorkflowMember)
                    .where(WorkflowMember.workflow_id == copy.id)
                    .order_by(WorkflowMember.position)
                )
            ).scalars()
        )
    assert [m.tool_alias for m in members] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_copy_name_collision_bumps_suffix(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Copying the same source twice yields '(copy)' then '(copy 2)'."""
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
        source = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Source",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        first = await workflow_service.copy_to_owner(
            session, actor_user_id=owner_id, source_workflow_id=source.id
        )
    async with session_scope(factory) as session:
        second = await workflow_service.copy_to_owner(
            session, actor_user_id=owner_id, source_workflow_id=source.id
        )
    assert first.name == "Source (copy)"
    assert second.name == "Source (copy 2)"


@pytest.mark.asyncio
async def test_outsider_cannot_copy_private(
    workflows_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """A non-share, non-owner regular user gets WorkflowNotFoundError."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    async with session_scope(factory) as session:
        eve = await user_service.create_user(
            session,
            username="eve",
            email="eve@example.com",
            password="securepassword1",
            full_name="Eve",
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
        source = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Private",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        with pytest.raises(WorkflowNotFoundError):
            await workflow_service.copy_to_owner(
                session,
                actor_user_id=eve.id,
                source_workflow_id=source.id,
            )


@pytest.mark.asyncio
async def test_admin_can_copy_for_troubleshooting(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Admin holding custom_workflows.inspect can fork any custom workflow."""
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
        source = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Bug repro",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        copy = await workflow_service.copy_to_owner(
            session,
            actor_user_id=admin_id,
            source_workflow_id=source.id,
        )
    assert copy.owner_id == admin_id


@pytest.mark.asyncio
async def test_copy_visible_admin_workflow(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """Any user who can see a released admin workflow can fork it."""
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
    # Promote to released so the regular user can see it.
    async with session_scope(factory) as session:
        await workflow_service.promote_state(
            session, actor_user_id=admin_id, alias="admin_wf", new_state="test"
        )
    async with session_scope(factory) as session:
        await workflow_service.promote_state(
            session, actor_user_id=admin_id, alias="admin_wf", new_state="released"
        )

    async with session_scope(factory) as session:
        copy = await workflow_service.copy_to_owner(
            session,
            actor_user_id=owner_id,
            source_workflow_id=admin_wf.id,
        )
    assert copy.kind == "custom"
    assert copy.owner_id == owner_id
    assert copy.descriptor["copied_from_workflow_id"] == admin_wf.id


# ----- list_for_user -----------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_owned_and_shared_only(
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
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )

    # Alice's: one private, one shared with Bob.
    async with session_scope(factory) as session:
        private = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Private",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        shared = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Shared",
                description="",
                tool_aliases=["alpha", "beta"],
                share_with_user_ids=[bob_id],
            ),
        )

    # Bob also owns one of his own.
    async with session_scope(factory) as session:
        bobs_own = await workflow_service.create_custom(
            session,
            actor_user_id=bob_id,
            draft=CustomWorkflowDraft(
                name="Bob's own",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )

    # Bob's view: his own + Alice's shared, NOT Alice's private.
    async with session_scope(factory) as session:
        rows = await workflow_service.list_for_user(
            session, actor_user_id=bob_id
        )
    ids = {wf.id for wf in rows}
    assert ids == {shared.id, bobs_own.id}
    assert private.id not in ids


@pytest.mark.asyncio
async def test_list_excludes_archived_by_default(
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
        active = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Active",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        archived = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="To archive",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        await workflow_service.archive_custom(
            session, actor_user_id=owner_id, workflow_id=archived.id
        )

    async with session_scope(factory) as session:
        active_only = await workflow_service.list_for_user(
            session, actor_user_id=owner_id
        )
        with_archived = await workflow_service.list_for_user(
            session, actor_user_id=owner_id, include_archived=True
        )
    assert {wf.id for wf in active_only} == {active.id}
    assert {wf.id for wf in with_archived} == {active.id, archived.id}


@pytest.mark.asyncio
async def test_list_excludes_admin_workflows(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """list_for_user is custom-only; admin workflows go through list_visible."""
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
        await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias="adm",
                name="Adm",
                description="",
                teams=["World"],
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        custom = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Mine",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        rows = await workflow_service.list_for_user(
            session, actor_user_id=owner_id
        )
    assert [wf.id for wf in rows] == [custom.id]
    assert all(wf.kind == "custom" for wf in rows)


# ----- list_for_admin ----------------------------------------------------


@pytest.mark.asyncio
async def test_list_for_admin_returns_every_custom_workflow(
    workflows_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    alice_id = seed_roles["regular_user"]
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
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        await workflow_service.create_custom(
            session,
            actor_user_id=alice_id,
            draft=CustomWorkflowDraft(
                name="Alice's",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
        await workflow_service.create_custom(
            session,
            actor_user_id=bob_id,
            draft=CustomWorkflowDraft(
                name="Bob's",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        rows = await workflow_service.list_for_admin(
            session, actor_user_id=admin_id
        )
    owners = {wf.owner_id for wf in rows}
    assert owners == {alice_id, bob_id}


@pytest.mark.asyncio
async def test_list_for_admin_filters_by_owner(
    workflows_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    alice_id = seed_roles["regular_user"]
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
    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )
    async with session_scope(factory) as session:
        await workflow_service.create_custom(
            session,
            actor_user_id=alice_id,
            draft=CustomWorkflowDraft(
                name="Alice's",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
        await workflow_service.create_custom(
            session,
            actor_user_id=bob_id,
            draft=CustomWorkflowDraft(
                name="Bob's",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
    async with session_scope(factory) as session:
        rows = await workflow_service.list_for_admin(
            session, actor_user_id=admin_id, owner_id=bob_id
        )
    assert [wf.owner_id for wf in rows] == [bob_id]


@pytest.mark.asyncio
async def test_list_for_admin_filters_by_contains_tool(
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
    async with session_scope(factory) as session:
        ab = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="AB",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
        bc = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="BC",
                description="",
                tool_aliases=["beta", "gamma"],
            ),
        )
    async with session_scope(factory) as session:
        rows = await workflow_service.list_for_admin(
            session, actor_user_id=admin_id, contains_tool_alias="gamma"
        )
    assert [wf.id for wf in rows] == [bc.id]
    async with session_scope(factory) as session:
        rows = await workflow_service.list_for_admin(
            session, actor_user_id=admin_id, contains_tool_alias="beta"
        )
    assert sorted(wf.id for wf in rows) == sorted([ab.id, bc.id])


@pytest.mark.asyncio
async def test_list_for_admin_search_matches_name_owner_and_tool(
    workflows_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    alice_id = seed_roles["regular_user"]
    async with session_scope(factory) as session:
        bob = await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="securepassword1",
            full_name="Bob Stress",
            role="regular_user",
        )
        bob_id = bob.id
    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="alpha", name="Alpha", teams=["World"]),
    )
    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="gamma", name="Thermal Soak", teams=["World"]),
    )
    async with session_scope(factory) as session:
        alice_wf = await workflow_service.create_custom(
            session,
            actor_user_id=alice_id,
            draft=CustomWorkflowDraft(
                name="Bridge Study",
                description="",
                tool_aliases=["alpha", "gamma"],
            ),
        )
        bob_wf = await workflow_service.create_custom(
            session,
            actor_user_id=bob_id,
            draft=CustomWorkflowDraft(
                name="Pump Casing",
                description="",
                tool_aliases=["alpha", "gamma"],
            ),
        )

    async def _search(term: str, **kw: object) -> list[int]:
        async with session_scope(factory) as session:
            rows = await workflow_service.list_for_admin(
                session, actor_user_id=admin_id, search=term, **kw
            )
        return sorted(wf.id for wf in rows)

    # by workflow name
    assert await _search("bridge") == [alice_wf.id]
    # by owner full name
    assert await _search("stress") == [bob_wf.id]
    # by tool name
    assert await _search("thermal") == sorted([alice_wf.id, bob_wf.id])
    # by tool alias
    assert await _search("alpha") == sorted([alice_wf.id, bob_wf.id])
    # additive with the owner filter
    assert await _search("alpha", owner_id=alice_id) == [alice_wf.id]
    # no match
    assert await _search("nonexistent-term") == []


@pytest.mark.asyncio
async def test_list_for_admin_refused_without_inspect_permission(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    from matofx.workflows.errors import WorkflowStateError  # noqa: PLC0415

    factory = make_session_factory(workflows_engine)
    owner_id = seed_roles["regular_user"]
    async with session_scope(factory) as session:
        with pytest.raises(WorkflowStateError, match="custom_workflows.inspect"):
            await workflow_service.list_for_admin(
                session, actor_user_id=owner_id
            )

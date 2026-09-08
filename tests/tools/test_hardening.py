"""Tests for the Phase 1 tool-hardening additions.

Covers:

- Forward-promotion graph (``new → test → released`` is enforced;
  ``new → released`` rejected; rollback always allowed).
- Soft-archive lifecycle (archived tools hide from listings, refuse
  promotion + descriptor edit).
- ``update_descriptor`` + ``update_bundle`` round-trips and
  optimistic-lock conflicts.
- Bundle path traversal guard.
- Descriptor reference validation (unknown team / unknown approver
  / approver flag off).
- Sole-approver guard on ``UserService.deactivate`` and
  ``set_approver(False)``, plus auto-removal cascade for
  multi-approver tools.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.errors import ConcurrentModificationError
from matofx.tools import (
    ApproverNotEnabledError,
    InvalidBundlePathError,
    InvalidPromotionError,
    SoleApproverError,
    ToolArchivedError,
    ToolDescriptor,
    ToolNotFoundError,
    ToolService,
    UnknownApproverError,
    UnknownTeamError,
)
from matofx.users import TeamService, UserService, make_team_service


@pytest.fixture
def team_service(audit_writer) -> TeamService:
    return make_team_service(audit_writer)


def _descriptor(
    alias: str = "demo-tool",
    *,
    teams: list[str] | None = None,
    approver_usernames: list[str] | None = None,
    approvals_policy: str = "none",
) -> ToolDescriptor:
    kwargs: dict[str, object] = {
        "alias": alias,
        "name": alias,
        "version": "1.0.0",
        "description": "test fixture",
        "main_script": "static/m.py",
        "approvals_policy": approvals_policy,
    }
    if teams is not None:
        kwargs["teams"] = teams
    if approver_usernames is not None:
        kwargs["approver_usernames"] = approver_usernames
    return ToolDescriptor.model_validate(kwargs)


# --- Forward-graph promotion -----------------------------------------


@pytest.mark.asyncio
async def test_new_to_released_is_blocked(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(),
            bundle_path="/tools/demo",
        )
    async with session_scope(factory) as session:
        with pytest.raises(InvalidPromotionError):
            await tool_service.promote_state(
                session,
                actor_user_id=seed_roles["super_user"],
                alias="demo-tool",
                new_state="released",
            )


@pytest.mark.asyncio
async def test_step_by_step_promotion_works(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(),
            bundle_path="/tools/demo",
        )
        await tool_service.promote_state(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            new_state="test",
        )
        result = await tool_service.promote_state(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            new_state="released",
        )
    assert result.state == "released"


# --- Archive lifecycle ----------------------------------------------


@pytest.mark.asyncio
async def test_archive_hides_from_list_visible(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(),
            bundle_path="/tools/demo",
        )
        await tool_service.archive(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            reason="superseded",
        )

    async with session_scope(factory) as session:
        listing = await tool_service.list_visible(session, actor_user_id=seed_roles["super_user"])
    assert listing == []

    async with session_scope(factory) as session:
        with_archived = await tool_service.list_visible(
            session, actor_user_id=seed_roles["super_user"], include_archived=True
        )
    assert [t.alias for t in with_archived] == ["demo-tool"]


@pytest.mark.asyncio
async def test_archived_tool_refuses_promotion(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(),
            bundle_path="/tools/demo",
        )
        await tool_service.archive(
            session, actor_user_id=seed_roles["super_user"], alias="demo-tool"
        )

    async with session_scope(factory) as session:
        with pytest.raises(ToolArchivedError):
            await tool_service.promote_state(
                session,
                actor_user_id=seed_roles["super_user"],
                alias="demo-tool",
                new_state="test",
            )


@pytest.mark.asyncio
async def test_unarchive_brings_tool_back(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(),
            bundle_path="/tools/demo",
        )
        await tool_service.archive(
            session, actor_user_id=seed_roles["super_user"], alias="demo-tool"
        )
        await tool_service.unarchive(
            session, actor_user_id=seed_roles["super_user"], alias="demo-tool"
        )

    async with session_scope(factory) as session:
        listing = await tool_service.list_visible(session, actor_user_id=seed_roles["super_user"])
    assert [t.alias for t in listing] == ["demo-tool"]


# --- update_descriptor + update_bundle ------------------------------


@pytest.mark.asyncio
async def test_update_descriptor_replaces_in_place(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(),
            bundle_path="/tools/demo",
        )

    # Server controls version from Session B on. The supplied
    # ``version`` is ignored — the service patch-bumps the existing
    # one. Initial install lands at 0.1.0 → first patch = 0.1.1.
    new_descriptor = _descriptor()
    payload = new_descriptor.model_dump()
    payload["version"] = "2.0.0"  # ignored by the service
    payload["short_description"] = "Refreshed for v2."
    new_descriptor = ToolDescriptor.model_validate(payload)

    async with session_scope(factory) as session:
        updated = await tool_service.update_descriptor(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=new_descriptor,
        )
    assert updated.version == "0.1.1"
    assert updated.descriptor["short_description"] == "Refreshed for v2."


@pytest.mark.asyncio
async def test_update_descriptor_alias_mismatch_rejected(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(alias="demo-tool"),
            bundle_path="/tools/demo",
        )

    other = _descriptor(alias="renamed-tool")
    async with session_scope(factory) as session:
        with pytest.raises(ToolNotFoundError):
            # alias='renamed-tool' is what we look up; the row doesn't exist.
            await tool_service.update_descriptor(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=other,
            )


@pytest.mark.asyncio
async def test_update_descriptor_optimistic_lock_rejects_stale(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        installed = await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(),
            bundle_path="/tools/demo",
        )
        seen_version = installed.row_version

    # Each candidate must change a real metadata field — Session B's
    # update_descriptor short-circuits no-op edits (no row_version
    # bump if nothing changed), so a version-only difference wouldn't
    # exercise the optimistic lock.
    new_a = ToolDescriptor.model_validate(
        {**_descriptor().model_dump(), "short_description": "First concurrent edit"}
    )
    new_b = ToolDescriptor.model_validate(
        {**_descriptor().model_dump(), "short_description": "Second concurrent edit"}
    )

    async with session_scope(factory) as session:
        await tool_service.update_descriptor(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=new_a,
            expected_row_version=seen_version,
        )

    async with session_scope(factory) as session:
        with pytest.raises(ConcurrentModificationError):
            await tool_service.update_descriptor(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=new_b,
                expected_row_version=seen_version,
            )


@pytest.mark.asyncio
async def test_update_bundle_replaces_path(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(),
            bundle_path="/tools/demo",
        )
    async with session_scope(factory) as session:
        result = await tool_service.update_bundle(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            bundle_path="/tools/demo-v2",
        )
    assert result.bundle_path == "/tools/demo-v2"


# --- Bundle path traversal guard ------------------------------------


@pytest.mark.asyncio
async def test_install_rejects_bundle_with_dotdot(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidBundlePathError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=_descriptor(),
                bundle_path="/tools/../etc/secrets",
            )


@pytest.mark.asyncio
async def test_install_rejects_empty_bundle(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidBundlePathError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=_descriptor(),
                bundle_path="   ",
            )


# --- Descriptor reference validation --------------------------------


@pytest.mark.asyncio
async def test_install_rejects_unknown_team(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        with pytest.raises(UnknownTeamError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=_descriptor(teams=["does-not-exist"]),
                bundle_path="/tools/demo",
            )


@pytest.mark.asyncio
async def test_install_accepts_known_team(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    team_service: TeamService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await team_service.create_team(
            session, actor_user_id=seed_roles["super_user"], name="thermal"
        )
    async with session_scope(factory) as session:
        tool = await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(teams=["thermal"]),
            bundle_path="/tools/demo",
        )
    assert tool.descriptor["teams"] == ["thermal"]


@pytest.mark.asyncio
async def test_install_rejects_unknown_approver(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        with pytest.raises(UnknownApproverError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=_descriptor(approver_usernames=["ghost"], approvals_policy="pre"),
                bundle_path="/tools/demo",
            )


@pytest.mark.asyncio
async def test_install_rejects_known_user_without_approver_flag(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    """The user 'alice' (regular_user) exists but isn't flagged as an approver."""
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        with pytest.raises(ApproverNotEnabledError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=_descriptor(approver_usernames=["alice"], approvals_policy="pre"),
                bundle_path="/tools/demo",
            )


# --- Sole-approver guard --------------------------------------------


async def _enable_alice_as_approver(
    factory, user_service: UserService, super_user_id: int, alice_id: int
) -> None:
    async with session_scope(factory) as session:
        await user_service.set_approver(
            session,
            user_id=alice_id,
            is_approver=True,
            actor=f"user:{super_user_id}",
        )


@pytest.mark.asyncio
async def test_set_approver_false_when_sole_approver_blocks(
    tools_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    await _enable_alice_as_approver(
        factory, user_service, seed_roles["super_user"], seed_roles["regular_user"]
    )

    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(approver_usernames=["alice"], approvals_policy="pre"),
            bundle_path="/tools/demo",
        )

    async with session_scope(factory) as session:
        with pytest.raises(SoleApproverError) as exc_info:
            await user_service.set_approver(
                session,
                user_id=seed_roles["regular_user"],
                is_approver=False,
                actor=f"user:{seed_roles['super_user']}",
            )
    assert ("demo-tool", "demo-tool") in exc_info.value.tool_aliases


@pytest.mark.asyncio
async def test_deactivate_when_sole_approver_blocks(
    tools_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    await _enable_alice_as_approver(
        factory, user_service, seed_roles["super_user"], seed_roles["regular_user"]
    )

    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(approver_usernames=["alice"], approvals_policy="pre"),
            bundle_path="/tools/demo",
        )

    async with session_scope(factory) as session:
        with pytest.raises(SoleApproverError):
            await user_service.deactivate(
                session,
                user_id=seed_roles["regular_user"],
                actor=f"user:{seed_roles['super_user']}",
            )


@pytest.mark.asyncio
async def test_deactivate_with_other_approvers_cascades_remove(
    tools_engine: AsyncEngine,
    user_service: UserService,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    """When the user is one of multiple approvers, removal is automatic."""
    factory = make_session_factory(tools_engine)
    await _enable_alice_as_approver(
        factory, user_service, seed_roles["super_user"], seed_roles["regular_user"]
    )
    await _enable_alice_as_approver(
        factory, user_service, seed_roles["super_user"], seed_roles["beta_tester"]
    )

    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor(approver_usernames=["alice", "beth"], approvals_policy="pre"),
            bundle_path="/tools/demo",
        )

    async with session_scope(factory) as session:
        await user_service.deactivate(
            session,
            user_id=seed_roles["regular_user"],
            actor=f"user:{seed_roles['super_user']}",
        )

    async with session_scope(factory) as session:
        tool = await tool_service.get(
            session, actor_user_id=seed_roles["super_user"], alias="demo-tool"
        )
    assert tool.descriptor["approver_usernames"] == ["beth"]

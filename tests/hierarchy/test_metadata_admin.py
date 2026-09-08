"""Admin metadata-taxonomy management (ADR-0002, task 8).

Gated by ``projects.manage_metadata`` — super_user holds it via the
catalog sentinel; regular_user does not.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import (
    AccessDeniedError,
    DuplicateNameError,
    MetadataError,
    NotFoundError,
    ProjectService,
)


@pytest.mark.asyncio
async def test_admin_can_create_field_and_value(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, _, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        field = await project_service.admin_create_metadata_field(
            session,
            actor_user_id=admin_id,
            key="programme",
            label="Programme",
            field_type="single_select",
            required=True,
            show_in_table=True,
        )
        await project_service.admin_create_metadata_value(
            session, actor_user_id=admin_id, field_id=field.id, value="Alpha"
        )

    async with session_scope(factory) as session:
        active = await project_service.list_active_metadata_fields(session)
    assert len(active) == 1
    f, values = active[0]
    assert f.key == "programme"
    assert f.required
    assert f.show_in_table
    assert [v.value for v in values] == ["Alpha"]


@pytest.mark.asyncio
async def test_regular_user_cannot_manage_taxonomy(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        with pytest.raises(AccessDeniedError):
            await project_service.admin_create_metadata_field(
                session,
                actor_user_id=alice_id,
                key="x",
                label="X",
                field_type="number",
            )
        with pytest.raises(AccessDeniedError):
            await project_service.admin_list_metadata_fields(session, actor_user_id=alice_id)


@pytest.mark.asyncio
async def test_invalid_field_type_and_duplicate_key(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, _, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        with pytest.raises(MetadataError):
            await project_service.admin_create_metadata_field(
                session,
                actor_user_id=admin_id,
                key="bad",
                label="Bad",
                field_type="freetext",
            )
    async with session_scope(factory) as session:
        await project_service.admin_create_metadata_field(
            session,
            actor_user_id=admin_id,
            key="dup",
            label="Dup",
            field_type="number",
        )
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateNameError):
            await project_service.admin_create_metadata_field(
                session,
                actor_user_id=admin_id,
                key="dup",
                label="Dup2",
                field_type="number",
            )


@pytest.mark.asyncio
async def test_value_on_non_select_rejected(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, _, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        f = await project_service.admin_create_metadata_field(
            session,
            actor_user_id=admin_id,
            key="budget",
            label="Budget",
            field_type="number",
        )
    async with session_scope(factory) as session:
        with pytest.raises(MetadataError):
            await project_service.admin_create_metadata_value(
                session, actor_user_id=admin_id, field_id=f.id, value="nope"
            )


@pytest.mark.asyncio
async def test_retire_field_hides_from_active_list(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, _, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        f = await project_service.admin_create_metadata_field(
            session,
            actor_user_id=admin_id,
            key="disc",
            label="Discipline",
            field_type="single_select",
        )
        fid = f.id
    async with session_scope(factory) as session:
        await project_service.admin_update_metadata_field(
            session, actor_user_id=admin_id, field_id=fid, active=False
        )
    async with session_scope(factory) as session:
        active = await project_service.list_active_metadata_fields(session)
        assert active == []
        full = await project_service.admin_list_metadata_fields(session, actor_user_id=admin_id)
    assert len(full) == 1
    assert full[0][0].active is False


@pytest.mark.asyncio
async def test_retire_value_hides_from_active_only(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, _, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        f = await project_service.admin_create_metadata_field(
            session,
            actor_user_id=admin_id,
            key="prog",
            label="Prog",
            field_type="single_select",
        )
        v = await project_service.admin_create_metadata_value(
            session, actor_user_id=admin_id, field_id=f.id, value="Keep"
        )
        gone = await project_service.admin_create_metadata_value(
            session, actor_user_id=admin_id, field_id=f.id, value="Gone"
        )
        gone_id = gone.id
        assert v.id != gone_id
    async with session_scope(factory) as session:
        await project_service.admin_update_metadata_value(
            session, actor_user_id=admin_id, value_id=gone_id, active=False
        )
    async with session_scope(factory) as session:
        active = await project_service.list_active_metadata_fields(session)
    _, values = active[0]
    assert [vv.value for vv in values] == ["Keep"]


@pytest.mark.asyncio
async def test_reorder_values_sets_display_order(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, _, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        f = await project_service.admin_create_metadata_field(
            session,
            actor_user_id=admin_id,
            key="disc",
            label="Discipline",
            field_type="single_select",
        )
        fid = f.id
        ids = [
            (
                await project_service.admin_create_metadata_value(
                    session, actor_user_id=admin_id, field_id=fid, value=v
                )
            ).id
            for v in ("A", "B", "C")
        ]
    async with session_scope(factory) as session:
        await project_service.admin_reorder_metadata_values(
            session,
            actor_user_id=admin_id,
            field_id=fid,
            ordered_value_ids=[ids[2], ids[0], ids[1]],
        )
    async with session_scope(factory) as session:
        active = await project_service.list_active_metadata_fields(session)
    _, values = active[0]
    assert [v.value for v in values] == ["C", "A", "B"]


@pytest.mark.asyncio
async def test_reorder_values_rejects_incomplete_list(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, _, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        f = await project_service.admin_create_metadata_field(
            session,
            actor_user_id=admin_id,
            key="disc",
            label="Discipline",
            field_type="single_select",
        )
        fid = f.id
        v1 = (
            await project_service.admin_create_metadata_value(
                session, actor_user_id=admin_id, field_id=fid, value="A"
            )
        ).id
        await project_service.admin_create_metadata_value(
            session, actor_user_id=admin_id, field_id=fid, value="B"
        )
    async with session_scope(factory) as session:
        with pytest.raises(MetadataError):
            await project_service.admin_reorder_metadata_values(
                session, actor_user_id=admin_id, field_id=fid, ordered_value_ids=[v1]
            )


@pytest.mark.asyncio
async def test_update_missing_field_not_found(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, _, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await project_service.admin_update_metadata_field(
                session, actor_user_id=admin_id, field_id=99999, label="x"
            )

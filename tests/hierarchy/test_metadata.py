"""Typed project-metadata taxonomy validation (ADR-0002)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import (
    AccessDeniedError,
    MetadataError,
    ProjectService,
)
from matofx.hierarchy.orm import ProjectMetadataField, ProjectMetadataValue


async def _seed_taxonomy(factory, creator_id: int) -> dict[str, int]:
    """Programme (single_select, required) + Budget (number) + Due (date)."""
    async with session_scope(factory) as session:
        prog = ProjectMetadataField(
            key="programme",
            label="Programme",
            field_type="single_select",
            required=True,
            created_by_user_id=creator_id,
        )
        budget = ProjectMetadataField(
            key="budget",
            label="Budget",
            field_type="number",
            created_by_user_id=creator_id,
        )
        due = ProjectMetadataField(
            key="due",
            label="Due",
            field_type="date",
            created_by_user_id=creator_id,
        )
        session.add_all([prog, budget, due])
        await session.flush()
        v_alpha = ProjectMetadataValue(field_id=prog.id, value="Alpha")
        v_beta = ProjectMetadataValue(field_id=prog.id, value="Beta", active=False)
        session.add_all([v_alpha, v_beta])
        await session.flush()
        return {
            "prog": prog.id,
            "budget": budget.id,
            "due": due.id,
            "v_alpha": v_alpha.id,
            "v_beta": v_beta.id,
        }


@pytest.mark.asyncio
async def test_set_and_get_metadata_round_trip(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    ids = await _seed_taxonomy(factory, alice_id)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        pid = project.id

    async with session_scope(factory) as session:
        await project_service.set_metadata(
            session,
            project_id=pid,
            actor_user_id=alice_id,
            selections=[
                {"field_id": ids["prog"], "value_id": ids["v_alpha"]},
                {"field_id": ids["budget"], "value_num": 1200},
                {"field_id": ids["due"], "value_date": "2026-09-01"},
            ],
        )
    async with session_scope(factory) as session:
        rows = await project_service.get_metadata(session, project_id=pid, actor_user_id=alice_id)
    by_field = {r.field_id: r for r in rows}
    assert by_field[ids["prog"]].value_id == ids["v_alpha"]
    assert float(by_field[ids["budget"]].value_num) == 1200.0
    assert by_field[ids["due"]].value_date.isoformat() == "2026-09-01"


@pytest.mark.asyncio
async def test_required_field_enforced(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    await _seed_taxonomy(factory, alice_id)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        pid = project.id
    async with session_scope(factory) as session:
        with pytest.raises(MetadataError):
            await project_service.set_metadata(
                session, project_id=pid, actor_user_id=alice_id, selections=[]
            )


@pytest.mark.asyncio
async def test_inactive_value_rejected(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    ids = await _seed_taxonomy(factory, alice_id)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        pid = project.id
    async with session_scope(factory) as session:
        with pytest.raises(MetadataError):
            await project_service.set_metadata(
                session,
                project_id=pid,
                actor_user_id=alice_id,
                selections=[{"field_id": ids["prog"], "value_id": ids["v_beta"]}],
            )


@pytest.mark.asyncio
async def test_bad_number_rejected(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    ids = await _seed_taxonomy(factory, alice_id)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        pid = project.id
    async with session_scope(factory) as session:
        with pytest.raises(MetadataError):
            await project_service.set_metadata(
                session,
                project_id=pid,
                actor_user_id=alice_id,
                selections=[
                    {"field_id": ids["prog"], "value_id": ids["v_alpha"]},
                    {"field_id": ids["budget"], "value_num": "not-a-number"},
                ],
            )


@pytest.mark.asyncio
async def test_integer_format_rejects_fraction(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        field = await project_service.admin_create_metadata_field(
            session,
            actor_user_id=admin_id,
            key="count",
            label="Count",
            field_type="number",
            number_format="integer",
        )
        fid = field.id
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        pid = project.id
    async with session_scope(factory) as session:
        await project_service.set_metadata(
            session,
            project_id=pid,
            actor_user_id=alice_id,
            selections=[{"field_id": fid, "value_num": 5}],
        )
    async with session_scope(factory) as session:
        rows = await project_service.get_metadata(session, project_id=pid, actor_user_id=alice_id)
        assert float(rows[0].value_num) == 5.0
    async with session_scope(factory) as session:
        with pytest.raises(MetadataError):
            await project_service.set_metadata(
                session,
                project_id=pid,
                actor_user_id=alice_id,
                selections=[{"field_id": fid, "value_num": 1.5}],
            )


@pytest.mark.asyncio
async def test_pattern_format_round_trip_and_rejection(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        field = await project_service.admin_create_metadata_field(
            session,
            actor_user_id=admin_id,
            key="part",
            label="Part",
            field_type="number",
            number_format="pattern",
            format_mask="AA-000-0000",
        )
        fid = field.id
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        pid = project.id
    async with session_scope(factory) as session:
        await project_service.set_metadata(
            session,
            project_id=pid,
            actor_user_id=alice_id,
            selections=[{"field_id": fid, "value_text": "AB-123-4567"}],
        )
    async with session_scope(factory) as session:
        rows = await project_service.get_metadata(session, project_id=pid, actor_user_id=alice_id)
        assert rows[0].value_text == "AB-123-4567"
        assert rows[0].value_num is None
    async with session_scope(factory) as session:
        with pytest.raises(MetadataError):
            await project_service.set_metadata(
                session,
                project_id=pid,
                actor_user_id=alice_id,
                selections=[{"field_id": fid, "value_text": "abc-12-99"}],
            )


@pytest.mark.asyncio
async def test_pattern_field_requires_mask_with_token(
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
                field_type="number",
                number_format="pattern",
                format_mask="----",
            )


@pytest.mark.asyncio
async def test_member_cannot_set_metadata(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    ids = await _seed_taxonomy(factory, alice_id)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        pid = project.id
        await project_service.add_acl_entry(
            session, project_id=pid, user_id=bob_id, role="member", actor_user_id=alice_id
        )
    async with session_scope(factory) as session:
        with pytest.raises(AccessDeniedError):
            await project_service.set_metadata(
                session,
                project_id=pid,
                actor_user_id=bob_id,
                selections=[{"field_id": ids["prog"], "value_id": ids["v_alpha"]}],
            )

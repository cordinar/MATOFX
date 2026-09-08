"""Tests for the V&V submit-time gate + metadata-field bootstrap (slice 7).

Covers:

- :func:`ensure_vv_required_for_submit_field` — first-call insert,
  subsequent-call idempotency, value-row repair (missing values get
  re-inserted without disturbing existing ones).
- :func:`check_submit_policy` — silent pass when field unseeded /
  selection is ``no`` / selection is ``not_set`` / no selection; allow
  on ``yes`` + ``verified_validated`` badge; **block** on ``yes`` +
  badge anything else (the core ADR-0011 §6.4 invariant).
"""

from __future__ import annotations

import datetime as _dt

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import ProjectMetadataField
from matofx.vv import VVPolicyError, ensure_vv_required_for_submit_field
from matofx.vv.policy import check_submit_policy

from .conftest import Basics


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


async def _project_id(session: sa.ext.asyncio.AsyncSession) -> int:
    """Return the seeded project's id (the conftest's lone project)."""
    return int(
        (
            await session.execute(sa.text("SELECT id FROM project ORDER BY id ASC LIMIT 1"))
        ).scalar_one()
    )


async def _set_project_metadata(
    session: sa.ext.asyncio.AsyncSession,
    *,
    project_id: int,
    field_id: int,
    value: str,
) -> None:
    """Insert one ``project_metadata`` row selecting ``value`` for this project."""
    value_id = (
        await session.execute(
            sa.text(
                "SELECT id FROM project_metadata_value WHERE field_id = :fid AND value = :v"
            ).bindparams(fid=field_id, v=value)
        )
    ).scalar_one()
    await session.execute(
        sa.text(
            "INSERT INTO project_metadata (project_id, field_id, value_id) "
            "VALUES (:pid, :fid, :vid)"
        ).bindparams(pid=project_id, fid=field_id, vid=value_id)
    )


# ---- Bootstrap idempotency -----------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_inserts_field_and_three_values_first_call(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """First call seeds the field + its three canonical values."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        field_id = await ensure_vv_required_for_submit_field(
            session, created_by_user_id=basics.user_id
        )
        field = await session.get(ProjectMetadataField, field_id)
        assert field is not None
        assert field.key == "vv_required_for_submit"
        assert field.field_type == "single_select"
        values = {
            (v,)[0]
            for v in (
                await session.execute(
                    sa.text(
                        "SELECT value FROM project_metadata_value WHERE field_id = :f"
                    ).bindparams(f=field_id)
                )
            ).scalars()
        }
        assert values == {"yes", "no", "not_set"}


@pytest.mark.asyncio
async def test_bootstrap_idempotent_on_repeat_call(vv_engine: AsyncEngine, basics: Basics) -> None:
    """Subsequent calls return the same field id; no duplicate rows."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        first = await ensure_vv_required_for_submit_field(
            session, created_by_user_id=basics.user_id
        )

    async with session_scope(factory) as session:
        second = await ensure_vv_required_for_submit_field(
            session, created_by_user_id=basics.user_id
        )
        assert first == second
        field_count = (
            await session.execute(
                sa.text(
                    "SELECT COUNT(*) FROM project_metadata_field "
                    "WHERE key = 'vv_required_for_submit'"
                )
            )
        ).scalar_one()
        value_count = (
            await session.execute(
                sa.text(
                    "SELECT COUNT(*) FROM project_metadata_value WHERE field_id = :f"
                ).bindparams(f=first)
            )
        ).scalar_one()
    assert field_count == 1
    assert value_count == 3


@pytest.mark.asyncio
async def test_bootstrap_repairs_missing_values(vv_engine: AsyncEngine, basics: Basics) -> None:
    """A subsequently-deleted value row is re-inserted on the next bootstrap call."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        field_id = await ensure_vv_required_for_submit_field(
            session, created_by_user_id=basics.user_id
        )
        # Curator deletes one value row by mistake (or via the admin UI).
        await session.execute(
            sa.text(
                "DELETE FROM project_metadata_value WHERE field_id = :f AND value = 'yes'"
            ).bindparams(f=field_id)
        )

    async with session_scope(factory) as session:
        await ensure_vv_required_for_submit_field(session, created_by_user_id=basics.user_id)
        values = {
            v
            for (v,) in (
                await session.execute(
                    sa.text(
                        "SELECT value FROM project_metadata_value WHERE field_id = :f"
                    ).bindparams(f=field_id)
                )
            ).all()
        }
    assert values == {"yes", "no", "not_set"}


# ---- Policy gate ---------------------------------------------------


@pytest.mark.asyncio
async def test_policy_silent_pass_when_field_unseeded(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """No field → no gate → no exception."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        project_id = await _project_id(session)
        # Should not raise even with no V&V records on the tool.
        await check_submit_policy(
            session,
            project_id=project_id,
            tool_alias="demo",
            tool_version="1.0.0",
            tool_version_id=basics.tool_version_id_a,
        )


@pytest.mark.asyncio
async def test_policy_silent_pass_when_selection_is_not_set(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Selection ``not_set`` → no gate → no exception."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        project_id = await _project_id(session)
        field_id = await ensure_vv_required_for_submit_field(
            session, created_by_user_id=basics.user_id
        )
        await _set_project_metadata(
            session, project_id=project_id, field_id=field_id, value="not_set"
        )

    async with session_scope(factory) as session:
        await check_submit_policy(
            session,
            project_id=project_id,
            tool_alias="demo",
            tool_version="1.0.0",
            tool_version_id=basics.tool_version_id_a,
        )


@pytest.mark.asyncio
async def test_policy_silent_pass_when_selection_is_no(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Selection ``no`` → no gate → no exception."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        project_id = await _project_id(session)
        field_id = await ensure_vv_required_for_submit_field(
            session, created_by_user_id=basics.user_id
        )
        await _set_project_metadata(session, project_id=project_id, field_id=field_id, value="no")

    async with session_scope(factory) as session:
        await check_submit_policy(
            session,
            project_id=project_id,
            tool_alias="demo",
            tool_version="1.0.0",
            tool_version_id=basics.tool_version_id_a,
        )


@pytest.mark.asyncio
async def test_policy_blocks_when_yes_and_badge_is_none(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Selection ``yes`` + no V&V records → :class:`VVPolicyError`."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        project_id = await _project_id(session)
        field_id = await ensure_vv_required_for_submit_field(
            session, created_by_user_id=basics.user_id
        )
        await _set_project_metadata(session, project_id=project_id, field_id=field_id, value="yes")

    async with session_scope(factory) as session:
        with pytest.raises(VVPolicyError) as exc_info:
            await check_submit_policy(
                session,
                project_id=project_id,
                tool_alias="demo",
                tool_version="1.0.0",
                tool_version_id=basics.tool_version_id_a,
            )
    assert exc_info.value.tool_alias == "demo"
    assert exc_info.value.tool_version == "1.0.0"


@pytest.mark.asyncio
async def test_policy_blocks_when_yes_and_only_verification_approved(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``yes`` + ``verified_only`` (no validation) still blocks."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        project_id = await _project_id(session)
        field_id = await ensure_vv_required_for_submit_field(
            session, created_by_user_id=basics.user_id
        )
        await _set_project_metadata(session, project_id=project_id, field_id=field_id, value="yes")
        # Approved verification only — no validation. Badge will be
        # ``verified_only``, which does NOT satisfy the gate.
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, "
                " decided_by_user_id, decided_at, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'verification', 'approved', "
                "        :did, :now, :uid, :now, 1)"
            ).bindparams(
                tv=basics.tool_version_id_a,
                did=basics.other_user_id,
                uid=basics.user_id,
                now=_utcnow(),
            )
        )

    async with session_scope(factory) as session:
        with pytest.raises(VVPolicyError):
            await check_submit_policy(
                session,
                project_id=project_id,
                tool_alias="demo",
                tool_version="1.0.0",
                tool_version_id=basics.tool_version_id_a,
            )


@pytest.mark.asyncio
async def test_policy_allows_when_yes_and_verified_validated(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``yes`` + ``verified_validated`` → no exception."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        project_id = await _project_id(session)
        field_id = await ensure_vv_required_for_submit_field(
            session, created_by_user_id=basics.user_id
        )
        await _set_project_metadata(session, project_id=project_id, field_id=field_id, value="yes")
        for kind in ("verification", "validation"):
            await session.execute(
                sa.text(
                    "INSERT INTO tool_vv_record "
                    "(tool_version_id, kind, state, "
                    " decided_by_user_id, decided_at, "
                    " recorded_by_user_id, recorded_at, version) "
                    "VALUES (:tv, :k, 'approved', "
                    "        :did, :now, :uid, :now, 1)"
                ).bindparams(
                    tv=basics.tool_version_id_a,
                    k=kind,
                    did=basics.other_user_id,
                    uid=basics.user_id,
                    now=_utcnow(),
                )
            )

    async with session_scope(factory) as session:
        await check_submit_policy(
            session,
            project_id=project_id,
            tool_alias="demo",
            tool_version="1.0.0",
            tool_version_id=basics.tool_version_id_a,
        )

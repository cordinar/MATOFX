"""Schema tests for the ``approval_request`` widening (slice 2 / migration 0055).

After 0055, ``approval_request`` accepts two additional target FKs
(``tool_vv_record_id`` and ``job_vv_record_id``) and the
``ck_approval_request_exactly_one_target`` CHECK widens from "one of 4"
(post-0049 — job, study, library_file_version, material_version) to
"one of 6". Two pending-only partial unique indexes prevent duplicate
in-flight gates on the same V&V record.

These tests exercise the schema directly (raw SQL via SQLAlchemy Core),
not the approvals service — the service layer that *uses* the new
columns lands with slice 6, but the schema must hold the invariants
from the moment migration 0055 ships so the slice-6 service can rely
on them.
"""

from __future__ import annotations

import datetime as _dt

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.vv import JobVVRecord, ToolVVRecord

from .conftest import Basics


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


async def _seed_tool_vv_record(session: sa.ext.asyncio.AsyncSession, basics: Basics) -> int:
    """Insert one tool V&V record; return its id."""
    rec = ToolVVRecord(
        tool_version_id=basics.tool_version_id_a,
        kind="verification",
        state="draft",
        recorded_by_user_id=basics.user_id,
    )
    session.add(rec)
    await session.flush()
    return rec.id


async def _seed_job_vv_record(session: sa.ext.asyncio.AsyncSession, basics: Basics) -> int:
    """Insert one job V&V record; return its id."""
    rec = JobVVRecord(
        job_id=basics.job_id,
        kind="validated_against",
        state="draft",
        recorded_by_user_id=basics.user_id,
    )
    session.add(rec)
    await session.flush()
    return rec.id


async def _insert_approval(
    session: sa.ext.asyncio.AsyncSession,
    *,
    requested_by_user_id: int,
    kind: str = "pre",
    status: str = "pending",
    tool_vv_record_id: int | None = None,
    job_vv_record_id: int | None = None,
    job_id: int | None = None,
    library_file_version_id: int | None = None,
) -> None:
    """Insert one ``approval_request`` row via core SQL.

    Bypasses the ORM so the test surface is the schema's CHECK + the
    partial unique indexes, not any service-layer invariant. Auto-
    populates ``decided_*`` when ``status`` is non-pending so the
    pre-existing ``ck_approval_request_decided_coherent`` invariant is
    satisfied (decided rows MUST carry the decider + timestamp + via).
    """
    now = _utcnow()
    is_decided = status != "pending"
    await session.execute(
        sa.text(
            "INSERT INTO approval_request "
            "(job_id, parametric_study_id, library_file_version_id, "
            " material_version_id, tool_vv_record_id, job_vv_record_id, "
            " kind, status, requested_at, requested_by_user_id, "
            " eligible_approvers, justification, "
            " decided_at, decided_by_user_id, decided_via) "
            "VALUES (:job_id, NULL, :lfv_id, NULL, :tool_vv_id, :job_vv_id, "
            "        :kind, :status, :requested_at, :requested_by, '[]', '', "
            "        :decided_at, :decided_by, :decided_via)"
        ).bindparams(
            job_id=job_id,
            lfv_id=library_file_version_id,
            tool_vv_id=tool_vv_record_id,
            job_vv_id=job_vv_record_id,
            kind=kind,
            status=status,
            requested_at=now,
            requested_by=requested_by_user_id,
            decided_at=now if is_decided else None,
            decided_by=requested_by_user_id if is_decided else None,
            decided_via="approver" if is_decided else None,
        )
    )


# ---- The new targets are accepted on their own ----------------------


@pytest.mark.asyncio
async def test_approval_with_only_tool_vv_target_accepted(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """An approval_request with only ``tool_vv_record_id`` set passes the CHECK."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        record_id = await _seed_tool_vv_record(session, basics)
        await _insert_approval(
            session,
            requested_by_user_id=basics.user_id,
            tool_vv_record_id=record_id,
        )
        # If we reach here without IntegrityError the CHECK accepted it.


@pytest.mark.asyncio
async def test_approval_with_only_job_vv_target_accepted(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """An approval_request with only ``job_vv_record_id`` set passes the CHECK."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        record_id = await _seed_job_vv_record(session, basics)
        await _insert_approval(
            session,
            requested_by_user_id=basics.user_id,
            job_vv_record_id=record_id,
        )


# ---- Two targets at once are rejected ------------------------------


@pytest.mark.asyncio
async def test_approval_with_both_vv_targets_rejected(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Setting both ``tool_vv_record_id`` and ``job_vv_record_id`` violates the CHECK."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        tool_id = await _seed_tool_vv_record(session, basics)
        job_id = await _seed_job_vv_record(session, basics)
        with pytest.raises(IntegrityError):
            await _insert_approval(
                session,
                requested_by_user_id=basics.user_id,
                tool_vv_record_id=tool_id,
                job_vv_record_id=job_id,
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_approval_with_vv_and_job_targets_rejected(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Setting a V&V target alongside a job target violates the CHECK."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        tool_vv_id = await _seed_tool_vv_record(session, basics)
        with pytest.raises(IntegrityError):
            await _insert_approval(
                session,
                requested_by_user_id=basics.user_id,
                tool_vv_record_id=tool_vv_id,
                job_id=basics.job_id,
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_approval_with_vv_and_library_targets_rejected(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Setting a V&V target alongside a library target violates the CHECK."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        job_vv_id = await _seed_job_vv_record(session, basics)
        with pytest.raises(IntegrityError):
            await _insert_approval(
                session,
                requested_by_user_id=basics.user_id,
                job_vv_record_id=job_vv_id,
                library_file_version_id=basics.library_file_version_id,
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_approval_with_no_targets_rejected(vv_engine: AsyncEngine, basics: Basics) -> None:
    """An approval_request with **no** targets set (all NULL) violates the CHECK."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await _insert_approval(session, requested_by_user_id=basics.user_id)
        await session.rollback()


# ---- Pending-only partial unique index ------------------------------


@pytest.mark.asyncio
async def test_two_pending_gates_on_same_tool_vv_record_rejected(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``uq_approval_request_pending_tool_vv_kind`` blocks duplicate pending gates."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        record_id = await _seed_tool_vv_record(session, basics)
        await _insert_approval(
            session,
            requested_by_user_id=basics.user_id,
            tool_vv_record_id=record_id,
            kind="pre",
            status="pending",
        )
        with pytest.raises(IntegrityError):
            await _insert_approval(
                session,
                requested_by_user_id=basics.user_id,
                tool_vv_record_id=record_id,
                kind="pre",
                status="pending",
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_two_pending_gates_on_same_job_vv_record_rejected(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``uq_approval_request_pending_job_vv_kind`` blocks duplicate pending gates."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        record_id = await _seed_job_vv_record(session, basics)
        await _insert_approval(
            session,
            requested_by_user_id=basics.user_id,
            job_vv_record_id=record_id,
            kind="pre",
            status="pending",
        )
        with pytest.raises(IntegrityError):
            await _insert_approval(
                session,
                requested_by_user_id=basics.user_id,
                job_vv_record_id=record_id,
                kind="pre",
                status="pending",
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_non_pending_duplicates_allowed_on_tool_vv(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """The partial index is pending-only — multiple decided gates coexist.

    A V&V record may legitimately accumulate history: a rejected attempt
    + a fresh draft + an eventual approval. The partial unique index
    must NOT block that — only ONE pending gate is forbidden.
    """
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        record_id = await _seed_tool_vv_record(session, basics)
        await _insert_approval(
            session,
            requested_by_user_id=basics.user_id,
            tool_vv_record_id=record_id,
            kind="pre",
            status="approved",
        )
        await _insert_approval(
            session,
            requested_by_user_id=basics.user_id,
            tool_vv_record_id=record_id,
            kind="pre",
            status="denied",
        )
        # Both decided rows persist; one pending row on top is OK.
        await _insert_approval(
            session,
            requested_by_user_id=basics.user_id,
            tool_vv_record_id=record_id,
            kind="pre",
            status="pending",
        )


# ---- The pre-existing targets still work ----------------------------


@pytest.mark.asyncio
async def test_widening_does_not_break_existing_targets(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """A library-only approval_request still passes the widened CHECK.

    Sanity check that the 4 → 6 widening didn't accidentally tighten
    one of the pre-existing targets — every prior-migration combination
    must remain valid.
    """
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _insert_approval(
            session,
            requested_by_user_id=basics.user_id,
            library_file_version_id=basics.library_file_version_id,
        )


# ---- CASCADE — deleting the V&V record removes its approval rows ---


@pytest.mark.asyncio
async def test_tool_vv_record_delete_cascades_to_approval(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Hard-deleting a ``tool_vv_record`` cascades to its approval rows.

    Soft-delete (``state='retired'``) is the normal path; this test
    exercises the *schema-level* CASCADE so the slice-6 service knows
    it can rely on the cleanup happening automatically if a record is
    ever hard-deleted.
    """
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        record_id = await _seed_tool_vv_record(session, basics)
        await _insert_approval(
            session,
            requested_by_user_id=basics.user_id,
            tool_vv_record_id=record_id,
            kind="pre",
            status="pending",
        )

    async with session_scope(factory) as session:
        # Enforce FK constraints for SQLite.
        await session.execute(sa.text("PRAGMA foreign_keys=ON"))
        await session.execute(
            sa.text("DELETE FROM tool_vv_record WHERE id = :rid").bindparams(rid=record_id)
        )

    async with session_scope(factory) as session:
        rows = await session.execute(
            sa.text(
                "SELECT COUNT(*) FROM approval_request WHERE tool_vv_record_id = :rid"
            ).bindparams(rid=record_id)
        )
        count = rows.scalar_one()
        assert count == 0, "CASCADE should have removed the approval row"

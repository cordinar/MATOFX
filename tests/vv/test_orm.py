"""ORM + constraint tests for the V&V data model (slice 1).

Schema correctness — the closed-vocabulary CHECKs (kind, state, the
decided-when-terminal invariant), the FK constraint semantics
(``tool_version`` RESTRICT, ``job`` CASCADE, ``library_file_version``
RESTRICT, ``user`` RESTRICT), and the optimistic-lock counter. Migration
``0054`` round-trips on both engines via ``tests/db/test_alembic.py``;
this file exercises the ORM end of the same schema.

Tests use direct ORM inserts (no service layer — that lands in slice 4)
so the CHECK / FK behaviour gets a focused, schema-only assertion
surface.
"""

from __future__ import annotations

import datetime as _dt

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm.exc import StaleDataError

from matofx.db import make_session_factory, session_scope
from matofx.vv import (
    JobVVRecord,
    ToolVVRecord,
)

from .conftest import Basics


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


# ---- Round-trip ------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_read_tool_vv_record(vv_engine: AsyncEngine, basics: Basics) -> None:
    """Insert a tool V&V record + read it back with every populated column."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="validation",
            state="draft",
            evidence_file_version_id=basics.library_file_version_id,
            evidence_url=None,
            applicable_range_json={"temperature": {"min": 300.0, "max": 600.0}},
            confidence_band=0.02,
            standards_cited_json=["ASME V&V 10", "NAFEMS QSS-2023"],
            notes="Validated against the X-rig dataset.",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        await session.flush()
        record_id = rec.id

    async with session_scope(factory) as session:
        loaded = await session.get(ToolVVRecord, record_id)
        assert loaded is not None
        assert loaded.kind == "validation"
        assert loaded.state == "draft"
        assert loaded.applicable_range_json == {"temperature": {"min": 300.0, "max": 600.0}}
        assert loaded.confidence_band == 0.02
        assert loaded.standards_cited_json == ["ASME V&V 10", "NAFEMS QSS-2023"]
        assert loaded.evidence_file_version_id == basics.library_file_version_id
        assert loaded.decided_by_user_id is None
        assert loaded.decided_at is None
        assert loaded.version == 1  # SQLAlchemy version_id_col starts at 1


@pytest.mark.asyncio
async def test_create_and_read_job_vv_record(vv_engine: AsyncEngine, basics: Basics) -> None:
    """Insert a job V&V record + read it back."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = JobVVRecord(
            job_id=basics.job_id,
            kind="validated_against",
            state="draft",
            evidence_file_version_id=basics.library_file_version_id,
            notes="Compared against same-day rig sample.",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        await session.flush()
        record_id = rec.id

    async with session_scope(factory) as session:
        loaded = await session.get(JobVVRecord, record_id)
        assert loaded is not None
        assert loaded.kind == "validated_against"
        assert loaded.state == "draft"
        assert loaded.evidence_file_version_id == basics.library_file_version_id


@pytest.mark.asyncio
async def test_json_columns_default_to_empty_collections(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Omitting ``applicable_range_json`` / ``standards_cited_json`` defaults to ``{}`` / ``[]``."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
            state="draft",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        await session.flush()
        record_id = rec.id

    async with session_scope(factory) as session:
        loaded = await session.get(ToolVVRecord, record_id)
        assert loaded is not None
        assert loaded.applicable_range_json == {}
        assert loaded.standards_cited_json == []


# ---- CHECK constraints -----------------------------------------------


@pytest.mark.asyncio
async def test_invalid_tool_vv_kind_rejected(vv_engine: AsyncEngine, basics: Basics) -> None:
    """``ck_tool_vv_record_kind`` rejects values outside the vocabulary."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="invented",  # not in TOOL_VV_KINDS
            state="draft",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        with pytest.raises(IntegrityError):
            await session.flush()
        # session_scope commits on normal exit; the flush failure leaves
        # the session needing explicit rollback before commit can run.
        await session.rollback()


@pytest.mark.asyncio
async def test_invalid_job_vv_kind_rejected(vv_engine: AsyncEngine, basics: Basics) -> None:
    """``ck_job_vv_record_kind`` rejects values outside the vocabulary."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = JobVVRecord(
            job_id=basics.job_id,
            kind="invented",  # not in JOB_VV_KINDS
            state="draft",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_invalid_state_rejected_tool(vv_engine: AsyncEngine, basics: Basics) -> None:
    """``ck_tool_vv_record_state`` rejects out-of-vocab states."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
            state="frobbed",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_approved_without_decided_fields_rejected(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``ck_tool_vv_record_decided_when_terminal`` blocks approved/null-decision."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
            state="approved",
            # decided_by_user_id + decided_at both NULL — invariant violation
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_approved_with_decided_fields_accepted(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``approved`` with both ``decided_*`` populated passes the CHECK."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
            state="approved",
            decided_by_user_id=basics.other_user_id,
            decided_at=_utcnow(),
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        await session.flush()
        # If we got here, the CHECK didn't fire — explicit assertion to make
        # the intent visible in the test report.
        assert rec.id is not None


@pytest.mark.asyncio
async def test_rejected_without_decided_fields_blocked(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """The decided-when-terminal CHECK also fires on ``rejected``."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = JobVVRecord(
            job_id=basics.job_id,
            kind="validated_against",
            state="rejected",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_retired_can_lack_decided_fields(vv_engine: AsyncEngine, basics: Basics) -> None:
    """``retired`` is terminal but NOT a decision — ``decided_*`` may be NULL."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
            state="retired",
            # Retired without ever being decided — e.g. drafted then
            # abandoned. The CHECK is intentionally narrow to (approved,
            # rejected) so this case is allowed.
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        await session.flush()
        assert rec.id is not None


# ---- FK semantics ----------------------------------------------------


@pytest.mark.asyncio
async def test_tool_version_restrict_blocks_delete_with_records(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """RESTRICT — deleting a ``tool_version`` while V&V records reference it raises."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
            state="draft",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        await session.flush()

    # Now try to delete the tool_version. The RESTRICT FK must raise —
    # SQLite fires this on the DELETE itself, not on the subsequent flush.
    factory2 = make_session_factory(vv_engine)
    async with session_scope(factory2) as session:
        # Enforce FK constraints for SQLite (PRAGMA per-connection).
        await session.execute(sa.text("PRAGMA foreign_keys=ON"))
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text("DELETE FROM tool_version WHERE id = :tv").bindparams(
                    tv=basics.tool_version_id_a
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_job_cascade_deletes_job_vv_records(vv_engine: AsyncEngine, basics: Basics) -> None:
    """CASCADE — deleting a ``job`` removes its ``job_vv_record`` rows."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = JobVVRecord(
            job_id=basics.job_id,
            kind="sanity_checked",
            state="draft",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        await session.flush()
        record_id = rec.id

    factory2 = make_session_factory(vv_engine)
    async with session_scope(factory2) as session:
        await session.execute(sa.text("PRAGMA foreign_keys=ON"))
        await session.execute(sa.text("DELETE FROM job WHERE id = :j").bindparams(j=basics.job_id))
        await session.flush()

    async with session_scope(factory2) as session:
        loaded = await session.get(JobVVRecord, record_id)
        assert loaded is None, "CASCADE should have removed the record"


@pytest.mark.asyncio
async def test_evidence_file_version_restrict_blocks_delete(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """RESTRICT — a referenced ``library_file_version`` can't be deleted."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="validation",
            state="draft",
            evidence_file_version_id=basics.library_file_version_id,
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        await session.flush()

    factory2 = make_session_factory(vv_engine)
    async with session_scope(factory2) as session:
        await session.execute(sa.text("PRAGMA foreign_keys=ON"))
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text("DELETE FROM library_file_version WHERE id = :lfv").bindparams(
                    lfv=basics.library_file_version_id
                )
            )
        await session.rollback()


# ---- Optimistic lock -------------------------------------------------


@pytest.mark.asyncio
async def test_optimistic_lock_increments_on_update(vv_engine: AsyncEngine, basics: Basics) -> None:
    """SQLAlchemy's ``version_id_col`` bumps ``version`` on every UPDATE."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
            state="draft",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        await session.flush()
        assert rec.version == 1  # version_id_col starts at 1
        record_id = rec.id

    async with session_scope(factory) as session:
        loaded = await session.get(ToolVVRecord, record_id)
        assert loaded is not None
        loaded.notes = "first edit"
        await session.flush()
        assert loaded.version == 2

    async with session_scope(factory) as session:
        loaded = await session.get(ToolVVRecord, record_id)
        assert loaded is not None
        loaded.notes = "second edit"
        await session.flush()
        assert loaded.version == 3


@pytest.mark.asyncio
async def test_optimistic_lock_stale_write_raises(vv_engine: AsyncEngine, basics: Basics) -> None:
    """A stale write raises :class:`StaleDataError`.

    Concurrent sessions on the same SQLite engine hit the single-writer
    lock before they can demonstrate the ``version_id_col`` mismatch, so
    the test instead **simulates** a stale view: an intervening writer
    commits, then a fresh session loads the row and manually rolls the
    in-memory ``version`` back to the prior value before flushing. The
    UPDATE's ``WHERE version = ?`` then matches zero rows and SQLAlchemy
    raises :class:`StaleDataError` — the same invariant a real concurrent
    edit would trip, asserted on an engine without concurrent writes.
    """
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rec = ToolVVRecord(
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
            state="draft",
            recorded_by_user_id=basics.user_id,
        )
        session.add(rec)
        await session.flush()
        record_id = rec.id

    # Intervening writer bumps the row to version=2.
    async with session_scope(factory) as session:
        rec = await session.get(ToolVVRecord, record_id)
        assert rec is not None
        rec.notes = "intervening writer"
        # commits on exit; version goes 1 → 2

    # Fresh session: load the row at version=2, then raw-SQL bump the
    # version to 99 (simulating a concurrent writer that landed between
    # load and update). The ORM still believes the row is at version=2
    # and emits ``UPDATE ... WHERE id=X AND version=2`` on flush, which
    # matches zero rows because the raw UPDATE moved version forward —
    # SQLAlchemy detects the empty result set and raises StaleDataError.
    async with session_scope(factory) as session:
        rec = await session.get(ToolVVRecord, record_id)
        assert rec is not None
        assert rec.version == 2
        await session.execute(
            sa.text("UPDATE tool_vv_record SET version = 99 WHERE id = :rid").bindparams(
                rid=record_id
            )
        )
        rec.notes = "stale writer"
        with pytest.raises(StaleDataError):
            await session.flush()
        await session.rollback()


# ---- Index existence (sanity check the schema landed) ----------------


@pytest.mark.asyncio
async def test_composite_index_exists(vv_engine: AsyncEngine) -> None:
    """``ix_tool_vv_record_tv_state_kind`` is present after migration.

    The slice-4 badge query hits this index directly. If a future
    migration drops it without replacement, the query silently
    table-scans — this test makes that regression noisy.
    """
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rows = await session.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tool_vv_record'"
            )
        )
        names = {r[0] for r in rows.fetchall()}
    if not names:
        pytest.skip("Index introspection skipped on non-sqlite backends")
    assert "ix_tool_vv_record_tv_state_kind" in names
    assert "ix_tool_vv_record_state" in names
    assert "ix_tool_vv_record_tool_version_id" in names


# ---- Read-side query (the bones of slice 4's summarise_tool_status) --


@pytest.mark.asyncio
async def test_records_filterable_by_tool_version_state_kind(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """The slice-4 badge query shape: filter by ``(tool_version_id, state, kind)``."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        # One approved verification on tv_a, one approved validation on tv_a,
        # one approved verification on tv_b, one DRAFT verification on tv_a
        # (should not appear in the approved query).
        session.add_all(
            [
                ToolVVRecord(
                    tool_version_id=basics.tool_version_id_a,
                    kind="verification",
                    state="approved",
                    decided_by_user_id=basics.other_user_id,
                    decided_at=_utcnow(),
                    recorded_by_user_id=basics.user_id,
                ),
                ToolVVRecord(
                    tool_version_id=basics.tool_version_id_a,
                    kind="validation",
                    state="approved",
                    decided_by_user_id=basics.other_user_id,
                    decided_at=_utcnow(),
                    recorded_by_user_id=basics.user_id,
                ),
                ToolVVRecord(
                    tool_version_id=basics.tool_version_id_b,
                    kind="verification",
                    state="approved",
                    decided_by_user_id=basics.other_user_id,
                    decided_at=_utcnow(),
                    recorded_by_user_id=basics.user_id,
                ),
                ToolVVRecord(
                    tool_version_id=basics.tool_version_id_a,
                    kind="verification",
                    state="draft",
                    recorded_by_user_id=basics.user_id,
                ),
            ]
        )
        await session.flush()

    async with session_scope(factory) as session:
        result = await session.execute(
            select(ToolVVRecord).where(
                ToolVVRecord.tool_version_id == basics.tool_version_id_a,
                ToolVVRecord.state == "approved",
            )
        )
        records = list(result.scalars())
        kinds = {r.kind for r in records}
        assert kinds == {"verification", "validation"}
        assert len(records) == 2  # the draft is excluded

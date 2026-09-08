"""ORM + constraint tests for the KPI data model (slice 2).

Schema correctness — the composite UNIQUE on ``(job_id, kpi_name)``,
the two FK semantics (``job`` CASCADE, ``tool_version`` RESTRICT),
the ``Decimal | None`` precision round-trip, and the sentinel-NULL
semantics. Migration ``0057`` round-trips on both engines via
``tests/db/test_alembic.py``; this file exercises the ORM end of the
same schema.

Tests use direct ORM inserts (no service layer — that lands in
slice 3) so the CHECK / FK / UNIQUE behaviour gets a focused,
schema-only assertion surface.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.kpi import JobKpiValue

from .conftest import Basics


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


# ---- Round-trip ------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_read_job_kpi_value(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """Insert a populated KPI row + read it back with the captured value intact."""
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        row = JobKpiValue(
            job_id=basics.job_id_a,
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id_a,
            kpi_name="max_temperature",
            value_numeric=Decimal("372.150000000000"),
            unit="K",
        )
        session.add(row)
        await session.flush()
        row_id = row.id

    async with session_scope(factory) as session:
        loaded = await session.get(JobKpiValue, row_id)
        assert loaded is not None
        assert loaded.job_id == basics.job_id_a
        assert loaded.tool_alias == basics.tool_alias
        assert loaded.tool_version_id == basics.tool_version_id_a
        assert loaded.kpi_name == "max_temperature"
        assert loaded.value_numeric == Decimal("372.150000000000")
        assert loaded.unit == "K"
        # ``extracted_at`` is populated by the column's ``default=_utcnow``.
        # SQLite strips tzinfo on round-trip (known dialect quirk); the
        # ``DateTime(timezone=True)`` column type matters on Postgres, so
        # only the populated-ness is asserted here.
        assert loaded.extracted_at is not None


@pytest.mark.asyncio
async def test_sentinel_null_value_round_trip(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """A row with ``value_numeric = NULL`` (extraction failure sentinel) round-trips."""
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        row = JobKpiValue(
            job_id=basics.job_id_a,
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id_a,
            kpi_name="max_temperature",
            value_numeric=None,
            unit="K",
        )
        session.add(row)
        await session.flush()
        row_id = row.id

    async with session_scope(factory) as session:
        loaded = await session.get(JobKpiValue, row_id)
        assert loaded is not None
        assert loaded.value_numeric is None
        # Row exists despite the NULL value — answers "did this job
        # complete with this KPI populated?" without a join back to
        # ``outputs.json``.


@pytest.mark.asyncio
async def test_decimal_precision_preserved(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """A small Decimal survives the round-trip without binary-float drift.

    The audit-truth contract (design §15) requires bit-equal compare
    across re-extractions; a Float-typed column would lose the last
    decimal places here.
    """
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        row = JobKpiValue(
            job_id=basics.job_id_a,
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id_a,
            kpi_name="parts_per_million",
            value_numeric=Decimal("0.000000123456"),
            unit="ppm",
        )
        session.add(row)
        await session.flush()
        row_id = row.id

    async with session_scope(factory) as session:
        loaded = await session.get(JobKpiValue, row_id)
        assert loaded is not None
        assert loaded.value_numeric == Decimal("0.000000123456")


# ---- UNIQUE on (job_id, kpi_name) ------------------------------------


@pytest.mark.asyncio
async def test_unique_job_kpi_violation(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """Two rows for the same ``(job_id, kpi_name)`` violate the composite UNIQUE.

    Re-extraction (slice 4) is an UPSERT against this key; the
    constraint is what guarantees a job never has two rows for the
    same KPI even under concurrent writes.
    """
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        session.add(
            JobKpiValue(
                job_id=basics.job_id_a,
                tool_alias=basics.tool_alias,
                tool_version_id=basics.tool_version_id_a,
                kpi_name="max_temperature",
                value_numeric=Decimal("372"),
                unit="K",
            )
        )
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(
                JobKpiValue(
                    job_id=basics.job_id_a,
                    tool_alias=basics.tool_alias,
                    tool_version_id=basics.tool_version_id_a,
                    kpi_name="max_temperature",
                    value_numeric=Decimal("400"),
                    unit="K",
                )
            )
            await session.flush()


@pytest.mark.asyncio
async def test_same_kpi_name_across_different_jobs_allowed(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """Two jobs may both have a row for the same ``kpi_name`` — the warehouse pivot.

    The UNIQUE is composite on ``(job_id, kpi_name)``; a tool with N
    KPIs and J jobs has N×J rows by design (one per cell of the
    warehouse table).
    """
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        session.add(
            JobKpiValue(
                job_id=basics.job_id_a,
                tool_alias=basics.tool_alias,
                tool_version_id=basics.tool_version_id_a,
                kpi_name="max_temperature",
                value_numeric=Decimal("372"),
                unit="K",
            )
        )
        session.add(
            JobKpiValue(
                job_id=basics.job_id_b,
                tool_alias=basics.tool_alias,
                tool_version_id=basics.tool_version_id_a,
                kpi_name="max_temperature",
                value_numeric=Decimal("388"),
                unit="K",
            )
        )
        await session.flush()

    async with session_scope(factory) as session:
        result = await session.execute(
            sa.select(JobKpiValue).where(JobKpiValue.kpi_name == "max_temperature")
        )
        rows = list(result.scalars())
        assert len(rows) == 2
        assert {r.job_id for r in rows} == {basics.job_id_a, basics.job_id_b}


# ---- FK semantics ----------------------------------------------------


@pytest.mark.asyncio
async def test_cascade_on_job_delete(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """Deleting a job removes its KPI rows (``job_id`` FK is CASCADE).

    A deleted job leaves no semantic claim about its KPIs.
    """
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        session.add(
            JobKpiValue(
                job_id=basics.job_id_a,
                tool_alias=basics.tool_alias,
                tool_version_id=basics.tool_version_id_a,
                kpi_name="max_temperature",
                value_numeric=Decimal("372"),
                unit="K",
            )
        )
        await session.flush()

    # Confirm CASCADE actually fires (SQLite enforces FK CASCADE only
    # with PRAGMA foreign_keys = ON; ``make_engine`` already sets it).
    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        await session.delete(job)
        await session.flush()

    async with session_scope(factory) as session:
        result = await session.execute(
            sa.select(JobKpiValue).where(JobKpiValue.job_id == basics.job_id_a)
        )
        assert list(result.scalars()) == []


@pytest.mark.asyncio
async def test_restrict_on_tool_version_delete(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """Deleting a tool_version that owns KPI rows fails (``tool_version_id`` FK is RESTRICT).

    Retiring a tool version while it still owns KPI rows should fail
    loudly rather than silently orphan them.
    """
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        session.add(
            JobKpiValue(
                job_id=basics.job_id_a,
                tool_alias=basics.tool_alias,
                tool_version_id=basics.tool_version_id_a,
                kpi_name="max_temperature",
                value_numeric=Decimal("372"),
                unit="K",
            )
        )
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            tv = await session.get(ToolVersion, basics.tool_version_id_a)
            assert tv is not None
            await session.delete(tv)
            await session.flush()


# ---- Index existence (smoke) -----------------------------------------


@pytest.mark.asyncio
async def test_indexes_present(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """Confirm the two performance indexes were created by migration ``0057``.

    The warehouse query path's range scan and the time-series chart's
    date-window scan both depend on these being present; the smoke
    check guards against an accidental schema-rewrite migration
    dropping them.
    """
    async with kpi_engine.connect() as conn:
        index_names = await conn.run_sync(
            lambda sync_conn: {
                idx["name"] for idx in sa.inspect(sync_conn).get_indexes("job_kpi_value")
            }
        )
    assert "ix_job_kpi_value_alias_kpi" in index_names
    assert "ix_job_kpi_value_extracted_at" in index_names

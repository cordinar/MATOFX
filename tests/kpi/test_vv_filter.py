"""Tests for the V&V status filter wiring on the warehouse query (slice 9)."""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.kpi import (
    JobKpiValue,
    WarehouseFilters,
    list_warehouse_rows,
)
from matofx.kpi.service import _vv_status_passes

from .conftest import Basics


# ---- _vv_status_passes (pure logic) ----------------------------------


def test_vv_passes_any_always_true() -> None:
    for badge in ("none", "pending", "verified_only", "verified_validated", "retired"):
        assert _vv_status_passes(badge, "any") is True


def test_vv_passes_verified_only_requires_verification_or_both() -> None:
    assert _vv_status_passes("none", "verified_only") is False
    assert _vv_status_passes("pending", "verified_only") is False
    assert _vv_status_passes("retired", "verified_only") is False
    assert _vv_status_passes("verified_only", "verified_only") is True
    assert _vv_status_passes("verified_validated", "verified_only") is True


def test_vv_passes_verified_validated_strict() -> None:
    assert _vv_status_passes("verified_only", "verified_validated") is False
    assert _vv_status_passes("verified_validated", "verified_validated") is True


def test_vv_passes_unknown_filter_value_does_not_constrain() -> None:
    """Defensive — an unrecognised filter value passes everything."""
    assert _vv_status_passes("none", "garbage") is True


# ---- list_warehouse_rows with vv_status -----------------------------


async def _seed_completed_job_with_kpi(
    engine: AsyncEngine, basics: Basics, *, project_idx: int = 0
) -> int:
    """Seed a completed job + 1 KPI row in a fresh project/scenario.

    Returns the job id. Uses the basics' scenario_a for the first
    call; callers needing multiples can pass distinct ``project_idx``
    values to get a fresh project + scenario row each time.
    """
    from matofx.hierarchy.orm import Job, Project, Scenario  # noqa: PLC0415

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        project = Project(
            name=f"P_vv_{project_idx}",
            creator_user_id=basics.user_id,
            state="active",
        )
        session.add(project)
        await session.flush()
        scenario = Scenario(
            project_id=project.id,
            name=f"S_vv_{project_idx}",
            state="active",
        )
        session.add(scenario)
        await session.flush()
        now = _dt.datetime.now(tz=_dt.UTC)
        future = now + _dt.timedelta(seconds=1)
        job = Job(
            scenario_id=scenario.id,
            name=f"Jvv{project_idx}",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id_a,
            state="complete",
            inputs={},
            created_by_user_id=basics.user_id,
            created_at=now,
            started_at=now,
            completed_at=future,
        )
        session.add(job)
        await session.flush()
        session.add(
            JobKpiValue(
                job_id=job.id,
                tool_alias=basics.tool_alias,
                tool_version_id=basics.tool_version_id_a,
                kpi_name="max_temperature",
                value_numeric=Decimal("300.0"),
                unit="K",
            )
        )
        await session.flush()
        return int(job.id)


@pytest.mark.asyncio
async def test_warehouse_vv_status_any_returns_all_rows(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """With ``vv_status='any'`` no V&V join happens; new row surfaces."""
    new_job_id = await _seed_completed_job_with_kpi(kpi_engine, basics)
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        page = await list_warehouse_rows(
            session,
            actor_user_id=basics.user_id,
            tool_alias=basics.tool_alias,
            filters=WarehouseFilters(vv_status="any"),
        )
    # The basics fixture seeds 2 completed jobs (no KPI rows of their
    # own); the new seed adds one with a KPI row. With vv_status=any
    # all three surface.
    assert page.total >= 1
    assert any(r.job_id == new_job_id for r in page.rows)


@pytest.mark.asyncio
async def test_warehouse_vv_status_verified_only_filters_unverified(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """With ``vv_status='verified_only'`` and no V&V records, every row is hidden."""
    await _seed_completed_job_with_kpi(kpi_engine, basics)
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        page = await list_warehouse_rows(
            session,
            actor_user_id=basics.user_id,
            tool_alias=basics.tool_alias,
            filters=WarehouseFilters(vv_status="verified_only"),
        )
    # No V&V records seeded → badge is "none" → filter rejects.
    assert page.total == 0
    assert page.rows == ()


@pytest.mark.asyncio
async def test_warehouse_row_carries_vv_status_badge(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """Per-row ``vv_status`` is populated (defaults to ``'none'``)."""
    await _seed_completed_job_with_kpi(kpi_engine, basics)
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        page = await list_warehouse_rows(
            session,
            actor_user_id=basics.user_id,
            tool_alias=basics.tool_alias,
            filters=WarehouseFilters(),
        )
    assert len(page.rows) >= 1
    # No V&V records seeded → badge is "none" for every row.
    assert all(r.vv_status == "none" for r in page.rows)

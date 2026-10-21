"""Catalog tests for :func:`matofx.kpi.list_kpis_for_tool`.

The catalog decides which columns the results warehouse draws, which
KPI tiles the job page shows, and which names the CSV / PDF exports
carry. Two defects lived here:

1. **Duplicate columns after un-tick / re-tick.** The dedup key was
   ``(kpi_name, kpi_label, kpi_unit)``. Un-ticking a KPI sweeps
   ``kpi_label`` (``clear_kpi_overrides``), so the re-tick's effective
   label falls back to the output's own ``label`` and the key diverges
   — two catalog entries for one output, which the AG-Grid builder
   turns into two columns with the same ``field`` and the same values.

2. **Columns resurrected from versions nobody ran.** The catalog was a
   union over *every* version, so a KPI un-ticked long ago kept its
   column forever — ``bracket_stress`` showed "2 KPI(s)" and two
   all-``—`` columns over 650 runs whose pinned descriptors declared
   none.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.kpi import JobKpiValue, list_kpis_for_tool

from .conftest import Basics


def _outputs(*, kpi_label: str | None, kpi: bool = True) -> dict:
    """A one-output descriptor snapshot for ``t_max``."""
    field: dict = {
        "name": "t_max",
        "label": "Peak temperature",
        "type": "number",
        "unit": "degC",
    }
    if kpi:
        field["kpi"] = True
        if kpi_label is not None:
            field["kpi_label"] = kpi_label
    return {"alias": "heat_transfer", "outputs": [field]}


async def _set_snapshots(
    engine: AsyncEngine, basics: Basics, *, version_a: dict, version_b: dict
) -> None:
    """Stamp descriptor snapshots on the fixture's two tool versions.

    Version A is the older one, B the newer — the fixture creates them
    in that order, and the catalog orders by ``created_at`` desc.
    """
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        tv_a = await session.get(ToolVersion, basics.tool_version_id_a)
        tv_b = await session.get(ToolVersion, basics.tool_version_id_b)
        assert tv_a is not None
        assert tv_b is not None
        tv_a.descriptor_snapshot = version_a
        tv_b.descriptor_snapshot = version_b
        # Force a strict ordering so "newest" is unambiguous on a fast
        # clock — both fixture rows are stamped with datetime.now().
        now = _dt.datetime.now(tz=_dt.UTC)
        tv_a.created_at = now - _dt.timedelta(hours=1)
        tv_b.created_at = now


async def _capture(engine: AsyncEngine, basics: Basics, *, kpi_name: str) -> None:
    """Store one captured KPI value against the fixture's first job."""
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        session.add(
            JobKpiValue(
                job_id=basics.job_id_a,
                tool_alias=basics.tool_alias,
                tool_version_id=basics.tool_version_id_a,
                kpi_name=kpi_name,
                value_numeric=Decimal("372.15"),
                unit="degC",
                extracted_at=_dt.datetime.now(tz=_dt.UTC),
            )
        )


@pytest.mark.asyncio
async def test_retick_after_untick_does_not_duplicate_the_column(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """Un-tick then re-tick yields ONE entry, not two.

    The re-tick loses the ``kpi_label`` override, so the two versions
    disagree on the effective label. They still describe one output and
    must collapse to one column — the newest label wins.
    """
    await _set_snapshots(
        kpi_engine,
        basics,
        # Originally ticked with an override label...
        version_a=_outputs(kpi_label="Max temperature"),
        # ...un-ticked, then re-ticked without re-entering the override.
        version_b=_outputs(kpi_label=None),
    )
    await _capture(kpi_engine, basics, kpi_name="t_max")

    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        entries = await list_kpis_for_tool(session, tool_alias=basics.tool_alias)

    assert [e.kpi_name for e in entries] == ["t_max"]
    # Newest-version-wins: the label the author sees in the wizard today.
    assert entries[0].kpi_label == "Peak temperature"


@pytest.mark.asyncio
async def test_kpi_dropped_by_current_version_with_no_values_is_gone(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """A KPI no version in force declares, and no job captured, has no column."""
    await _set_snapshots(
        kpi_engine,
        basics,
        version_a=_outputs(kpi_label="Max temperature"),
        version_b=_outputs(kpi_label=None, kpi=False),
    )

    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        entries = await list_kpis_for_tool(session, tool_alias=basics.tool_alias)

    assert entries == []


@pytest.mark.asyncio
async def test_kpi_dropped_but_with_captured_values_keeps_its_column(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """Un-ticking a KPI must not orphan the values already captured.

    The label / unit come from the newest version that declared it, so
    the recovered column reads the way it did when the data was written.
    """
    await _set_snapshots(
        kpi_engine,
        basics,
        version_a=_outputs(kpi_label="Max temperature"),
        version_b=_outputs(kpi_label=None, kpi=False),
    )
    await _capture(kpi_engine, basics, kpi_name="t_max")

    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        entries = await list_kpis_for_tool(session, tool_alias=basics.tool_alias)

    assert [(e.kpi_name, e.kpi_label, e.kpi_unit) for e in entries] == [
        ("t_max", "Max temperature", "degC")
    ]


@pytest.mark.asyncio
async def test_version_pinned_catalog_is_that_version_alone(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """Pinning a version answers for that version, ignoring the others.

    This is what the job page asks for: a run must be judged against
    the descriptor it actually ran, never the tool-wide catalog.
    """
    await _set_snapshots(
        kpi_engine,
        basics,
        version_a=_outputs(kpi_label="Max temperature"),
        version_b=_outputs(kpi_label=None, kpi=False),
    )
    await _capture(kpi_engine, basics, kpi_name="t_max")

    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        older = await list_kpis_for_tool(
            session,
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id_a,
        )
        newer = await list_kpis_for_tool(
            session,
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id_b,
        )

    assert [e.kpi_name for e in older] == ["t_max"]
    # The captured value does NOT resurrect a column for a version that
    # never declared the KPI — that would be the warehouse's rule, not
    # the per-run one.
    assert newer == []


@pytest.mark.asyncio
async def test_unknown_tool_returns_empty_catalog(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """A tool with no versions at all short-circuits rather than raising."""
    del basics
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        assert await list_kpis_for_tool(session, tool_alias="no_such_tool") == []

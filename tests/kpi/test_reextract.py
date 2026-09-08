"""Re-extraction worker + signature differ tests (slice 4).

Covers :func:`compute_kpi_signature` /
:func:`should_reextract_after_descriptor_change` (pure logic, no DB);
:func:`reextract_tool_version` (the worker — walks completed jobs,
loads outputs via the injected stub, re-runs extraction, audits);
the per-tool-version lock from :mod:`matofx.kpi._reextract_state`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.audit.orm import AuditLog
from matofx.db import make_session_factory, session_scope
from matofx.kpi import (
    JobKpiValue,
    KpiReextractInProgressError,
    compute_kpi_signature,
    is_reextract_in_progress,
    reextract_tool_version,
    should_reextract_after_descriptor_change,
)
from matofx.kpi import _reextract_state
from matofx.tools.exec.contracts import OutputsDocument, OutputValue

from .conftest import Basics


@pytest.fixture(autouse=True)
def _reset_reextract_locks() -> None:
    """Each test starts with no in-flight sweeps recorded."""
    _reextract_state._reset_for_tests()


# ---- Signature differ -------------------------------------------------


def test_signature_empty_when_no_kpis() -> None:
    descriptor = {"outputs": [{"name": "x", "type": "number"}]}
    assert compute_kpi_signature(descriptor) == frozenset()


def test_signature_includes_kpi_label_and_unit() -> None:
    descriptor = {
        "outputs": [
            {
                "name": "t_max",
                "type": "number",
                "unit": "K",
                "kpi": True,
                "kpi_label": "Peak",
                "kpi_unit": "K",
            },
            {"name": "x", "type": "number"},  # non-KPI; ignored
        ]
    }
    assert compute_kpi_signature(descriptor) == frozenset({("t_max", "Peak", "K")})


def test_signature_uses_unit_fallback_when_kpi_unit_absent() -> None:
    """When ``kpi_unit`` is absent, ``unit`` fills in — matches the captured row."""
    descriptor = {
        "outputs": [
            {"name": "t_max", "type": "number", "unit": "K", "kpi": True},
        ]
    }
    assert compute_kpi_signature(descriptor) == frozenset({("t_max", None, "K")})


def test_signature_handles_garbage_descriptor() -> None:
    """Empty / malformed descriptors → empty signature, not a crash."""
    assert compute_kpi_signature(None) == frozenset()
    assert compute_kpi_signature("not a dict") == frozenset()
    assert compute_kpi_signature({"outputs": "not a list"}) == frozenset()
    assert compute_kpi_signature({"outputs": [42, "junk"]}) == frozenset()


def test_should_reextract_added_kpi() -> None:
    prev = {"outputs": [{"name": "x", "type": "number"}]}
    new = {"outputs": [{"name": "x", "type": "number", "kpi": True}]}
    assert should_reextract_after_descriptor_change(
        previous_descriptor=prev, new_descriptor=new
    )


def test_should_reextract_removed_kpi() -> None:
    prev = {"outputs": [{"name": "x", "type": "number", "kpi": True}]}
    new = {"outputs": [{"name": "x", "type": "number"}]}
    assert should_reextract_after_descriptor_change(
        previous_descriptor=prev, new_descriptor=new
    )


def test_should_reextract_changed_unit() -> None:
    prev = {"outputs": [{"name": "x", "type": "number", "unit": "K", "kpi": True}]}
    new = {"outputs": [{"name": "x", "type": "number", "unit": "C", "kpi": True}]}
    assert should_reextract_after_descriptor_change(
        previous_descriptor=prev, new_descriptor=new
    )


def test_should_not_reextract_when_unchanged() -> None:
    prev = {"outputs": [{"name": "x", "type": "number", "unit": "K", "kpi": True}]}
    new = {"outputs": [{"name": "x", "type": "number", "unit": "K", "kpi": True}]}
    assert not should_reextract_after_descriptor_change(
        previous_descriptor=prev, new_descriptor=new
    )


def test_should_not_reextract_when_only_kpi_category_changed() -> None:
    """``kpi_category`` is metadata-only — doesn't change the captured row."""
    prev = {
        "outputs": [
            {
                "name": "x",
                "type": "number",
                "unit": "K",
                "kpi": True,
                "kpi_category": "thermal",
            }
        ]
    }
    new = {
        "outputs": [
            {
                "name": "x",
                "type": "number",
                "unit": "K",
                "kpi": True,
                "kpi_category": "diagnostic",
            }
        ]
    }
    assert not should_reextract_after_descriptor_change(
        previous_descriptor=prev, new_descriptor=new
    )


# ---- Re-extraction worker --------------------------------------------


def _kpi_descriptor() -> dict:
    return {
        "alias": "heat_transfer",
        "outputs": [
            {
                "name": "max_temperature",
                "label": "Max temperature",
                "type": "number",
                "unit": "K",
                "kpi": True,
                "kpi_unit": "K",
            },
        ],
    }


def _outputs_for_temp(value: float) -> OutputsDocument:
    return OutputsDocument(
        tool_alias="heat_transfer",
        outputs=[
            OutputValue(
                name="max_temperature", label="Max temperature", type="number", value=value
            )
        ],
    )


async def _set_descriptor_and_staging(
    engine: AsyncEngine,
    tool_version_id: int,
    descriptor: dict,
    job_ids: list[int],
    storage_location_id: int,
) -> None:
    """Patch the snapshot + give jobs durable staging metadata for the resolver."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        tv = await session.get(ToolVersion, tool_version_id)
        assert tv is not None
        tv.descriptor_snapshot = descriptor
        for job_id in job_ids:
            job = await session.get(Job, job_id)
            assert job is not None
            job.storage_location_id = storage_location_id
            job.working_dir_relative = f"jobs/job_{job_id:07}"
        await session.flush()


async def _seed_storage_location(engine: AsyncEngine) -> int:
    """Seed a StorageLocation row so jobs can carry storage_location_id."""
    from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        loc = StorageLocation(name="test_share", mount_path="/share")
        session.add(loc)
        await session.flush()
        return loc.id


@pytest.mark.asyncio
async def test_reextract_walks_completed_jobs(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """The sweep extracts KPIs for every completed job of the version."""
    loc_id = await _seed_storage_location(kpi_engine)
    await _set_descriptor_and_staging(
        kpi_engine,
        basics.tool_version_id_a,
        _kpi_descriptor(),
        [basics.job_id_a, basics.job_id_b],
        loc_id,
    )

    # Inject loader stubs so each job gets a distinct value.
    docs = {
        f"jobs/job_{basics.job_id_a:07}": _outputs_for_temp(372.15),
        f"jobs/job_{basics.job_id_b:07}": _outputs_for_temp(388.50),
    }

    async def _resolver(_loc_id: int) -> str:
        return "/share"

    def _loader(path):  # type: ignore[no-untyped-def]
        # path = /share/jobs/job_NNN/outputs/outputs.json — pull the job
        # directory name back out for the per-job document lookup.
        rel = "/".join(path.parts[2:4])  # "jobs/job_NNN"
        return docs.get(rel)

    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        result = await reextract_tool_version(
            audit,
            session,
            tool_version_id=basics.tool_version_id_a,
            triggered_by_user_id=basics.user_id,
            storage_resolver=_resolver,
            outputs_loader=_loader,
        )

    assert result.job_count == 2
    assert result.extracted == 2
    assert result.skipped_no_outputs == 0

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    sa.select(JobKpiValue).order_by(JobKpiValue.job_id)
                )
            ).scalars()
        )
        by_job = {r.job_id: r for r in rows}
        assert by_job[basics.job_id_a].value_numeric == Decimal("372.15")
        assert by_job[basics.job_id_b].value_numeric == Decimal("388.50")


@pytest.mark.asyncio
async def test_reextract_skips_jobs_with_missing_outputs(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """A job whose ``outputs.json`` is gone is counted as skipped, not crashed."""
    loc_id = await _seed_storage_location(kpi_engine)
    await _set_descriptor_and_staging(
        kpi_engine,
        basics.tool_version_id_a,
        _kpi_descriptor(),
        [basics.job_id_a, basics.job_id_b],
        loc_id,
    )

    async def _resolver(_loc_id: int) -> str:
        return "/share"

    def _loader(_path):  # type: ignore[no-untyped-def]
        return None  # every job's outputs.json is "missing"

    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        result = await reextract_tool_version(
            audit,
            session,
            tool_version_id=basics.tool_version_id_a,
            triggered_by_user_id=basics.user_id,
            storage_resolver=_resolver,
            outputs_loader=_loader,
        )

    assert result.job_count == 2
    assert result.extracted == 0
    assert result.skipped_no_outputs == 2

    async with session_scope(factory) as session:
        rows = list(
            (await session.execute(sa.select(JobKpiValue))).scalars()
        )
        assert rows == []


@pytest.mark.asyncio
async def test_reextract_no_completed_jobs_is_no_op(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """A version with no completed jobs (tool_version_id_b) yields a zero tally."""
    await _set_descriptor_and_staging(
        kpi_engine,
        basics.tool_version_id_b,
        _kpi_descriptor(),
        job_ids=[],
        storage_location_id=0,
    )
    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        result = await reextract_tool_version(
            audit,
            session,
            tool_version_id=basics.tool_version_id_b,
            triggered_by_user_id=basics.user_id,
        )

    assert result.job_count == 0
    assert result.extracted == 0


@pytest.mark.asyncio
async def test_reextract_audit_recorded(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """A ``kpi.reextracted`` audit event lands with the sweep tally + trigger tag."""
    loc_id = await _seed_storage_location(kpi_engine)
    await _set_descriptor_and_staging(
        kpi_engine,
        basics.tool_version_id_a,
        _kpi_descriptor(),
        [basics.job_id_a],
        loc_id,
    )

    async def _resolver(_loc_id: int) -> str:
        return "/share"

    def _loader(_path):  # type: ignore[no-untyped-def]
        return _outputs_for_temp(300.0)

    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        await reextract_tool_version(
            audit,
            session,
            tool_version_id=basics.tool_version_id_a,
            triggered_by_user_id=basics.user_id,
            trigger="admin_manual",
            storage_resolver=_resolver,
            outputs_loader=_loader,
        )

    async with session_scope(factory) as session:
        events = list(
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.action == "kpi.reextracted")
                )
            ).scalars()
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["tool_version_id"] == basics.tool_version_id_a
        assert payload["trigger"] == "admin_manual"
        # Both seeded jobs are on tool_version_id_a; only job_a has
        # staging metadata + a loader stub here, so job_b counts as a
        # skip (no-outputs sentinel).
        assert payload["job_count"] == 2
        assert payload["extracted"] == 1
        assert payload["missing"] == 0
        assert payload["skipped_no_outputs"] == 1
        assert events[0].actor == f"user:{basics.user_id}"


@pytest.mark.asyncio
async def test_reextract_system_trigger_uses_system_actor(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """When ``triggered_by_user_id is None``, the audit actor is ``system:runner``."""
    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        await reextract_tool_version(
            audit,
            session,
            tool_version_id=basics.tool_version_id_b,
            triggered_by_user_id=None,
            trigger="descriptor_promote",
        )

    async with session_scope(factory) as session:
        event = (
            await session.execute(
                sa.select(AuditLog).where(AuditLog.action == "kpi.reextracted")
            )
        ).scalar_one()
        assert event.actor == "system:runner"
        assert event.actor_type == "system"
        assert event.payload["trigger"] == "descriptor_promote"


@pytest.mark.asyncio
async def test_reextract_lock_rejects_concurrent_same_version(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """Two concurrent sweeps for the same tool_version: the second raises."""
    import asyncio  # noqa: PLC0415

    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_resolver(_loc_id: int) -> str:
        started.set()
        await release.wait()
        return "/share"

    async def _runner_a() -> None:
        async with session_scope(factory) as session:
            await reextract_tool_version(
                audit,
                session,
                tool_version_id=basics.tool_version_id_a,
                triggered_by_user_id=basics.user_id,
                storage_resolver=_slow_resolver,
                outputs_loader=lambda _p: None,
            )

    loc_id = await _seed_storage_location(kpi_engine)
    await _set_descriptor_and_staging(
        kpi_engine,
        basics.tool_version_id_a,
        _kpi_descriptor(),
        [basics.job_id_a],
        loc_id,
    )

    task_a = asyncio.create_task(_runner_a())
    await started.wait()
    assert is_reextract_in_progress(basics.tool_version_id_a) is True

    # Second concurrent sweep on the same version → raise.
    with pytest.raises(KpiReextractInProgressError):
        async with session_scope(factory) as session:
            await reextract_tool_version(
                audit,
                session,
                tool_version_id=basics.tool_version_id_a,
                triggered_by_user_id=basics.user_id,
            )

    release.set()
    await task_a
    assert is_reextract_in_progress(basics.tool_version_id_a) is False


@pytest.mark.asyncio
async def test_lock_is_per_tool_version() -> None:
    """The lock primitive is keyed per ``tool_version_id`` — distinct ids don't interfere.

    Exercised against ``_reextract_state`` directly (no DB, no audit
    writer) — the audit-chain serialisation under concurrent writers
    is a Postgres-only guarantee (SELECT FOR UPDATE is a no-op on
    SQLite), and verifying the lock invariant doesn't require us to
    exercise that.
    """
    async with _reextract_state.acquire(1):
        assert is_reextract_in_progress(1) is True
        # Acquiring a different version's lock at the same time is fine.
        async with _reextract_state.acquire(2):
            assert is_reextract_in_progress(2) is True
            # Acquiring the *same* version's lock raises.
            with pytest.raises(KpiReextractInProgressError):
                async with _reextract_state.acquire(1):
                    pass
        assert is_reextract_in_progress(2) is False
    assert is_reextract_in_progress(1) is False

"""Service tests for KPI extraction (slice 3).

Covers :func:`extract_kpis_for_job` and the value classifier
:func:`_classify_value`. The runner-hook integration is exercised in
``tests/runner/`` via the supervisor's post-completion path (those
tests are pre-existing and ride on the extractor without needing
KPI-specific edits — see the runner suite's smoke test below).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.audit.orm import AuditLog
from matofx.db import make_session_factory, session_scope
from matofx.kpi import JobKpiValue, extract_kpis_for_job
from matofx.kpi.service import _classify_value
from matofx.tools.exec.contracts import OutputsDocument, OutputValue

from .conftest import Basics

# ---- _classify_value -------------------------------------------------


def test_classify_normal_int_returns_decimal() -> None:
    value, reason = _classify_value(372)
    assert value == Decimal("372")
    assert reason == "ok"


def test_classify_normal_float_returns_decimal_string_mediated() -> None:
    """Float→Decimal goes via str() to preserve the digits the tool wrote."""
    value, reason = _classify_value(372.15)
    # str(372.15) → "372.15" — the binary-float drift Decimal(372.15)
    # would introduce (372.1499999...) is what the string-mediated
    # path explicitly avoids.
    assert value == Decimal("372.15")
    assert reason == "ok"


def test_classify_decimal_passes_through_unchanged() -> None:
    """An already-Decimal value bypasses the str() round-trip."""
    value, reason = _classify_value(Decimal("0.000000123456"))
    assert value == Decimal("0.000000123456")
    assert reason == "ok"


def test_classify_missing_returns_null_with_reason() -> None:
    value, reason = _classify_value(None)
    assert value is None
    assert reason == "missing"


def test_classify_bool_rejected_explicitly() -> None:
    """``bool`` is a subclass of ``int`` — explicit reject avoids type confusion."""
    value, reason = _classify_value(True)
    assert value is None
    assert "bool" in reason


def test_classify_string_rejected() -> None:
    value, reason = _classify_value("hot")
    assert value is None
    assert "non-numeric" in reason
    assert "str" in reason


def test_classify_nan_rejected() -> None:
    value, reason = _classify_value(float("nan"))
    assert value is None
    assert reason == "NaN"


def test_classify_positive_inf_rejected() -> None:
    value, reason = _classify_value(float("inf"))
    assert value is None
    assert reason == "+Inf"


def test_classify_negative_inf_rejected() -> None:
    value, reason = _classify_value(float("-inf"))
    assert value is None
    assert reason == "-Inf"


def test_classify_decimal_nan_rejected() -> None:
    """A ``Decimal('NaN')`` is rejected the same way a float NaN is."""
    value, reason = _classify_value(Decimal("NaN"))
    assert value is None
    assert reason == "NaN"


# ---- extract_kpis_for_job (happy path) -------------------------------


def _kpi_descriptor() -> dict:
    """A minimal descriptor with two KPI outputs + one non-KPI numeric + one non-numeric."""
    return {
        "alias": "heat_transfer",
        "outputs": [
            {
                "name": "max_temperature",
                "label": "Max temperature",
                "type": "number",
                "unit": "K",
                "kpi": True,
                "kpi_label": "Peak temp",
                "kpi_unit": "K",
                "kpi_category": "thermal",
            },
            {
                "name": "max_pressure",
                "label": "Max pressure",
                "type": "number",
                "unit": "Pa",
                "kpi": True,
            },
            {
                "name": "iteration_count",
                "label": "Iterations",
                "type": "number",
                # No `kpi` flag — should NOT land in the warehouse.
            },
            {
                "name": "status_text",
                "label": "Status",
                "type": "string",
                # Not numeric — would fail descriptor validation if
                # `kpi=true`; included here to confirm the extractor
                # filters non-numeric outputs out defensively.
            },
        ],
    }


async def _set_descriptor(engine: AsyncEngine, tool_version_id: int, descriptor: dict) -> None:
    """Update the seeded ToolVersion's snapshot to the KPI descriptor."""
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        tv = await session.get(ToolVersion, tool_version_id)
        assert tv is not None
        tv.descriptor_snapshot = descriptor
        await session.flush()


def _outputs(*pairs: tuple[str, object]) -> OutputsDocument:
    """Build an OutputsDocument with the given (name, value) pairs as numeric outputs."""
    return OutputsDocument(
        tool_alias="heat_transfer",
        outputs=[
            OutputValue(name=name, label=name, type="number", value=value) for name, value in pairs
        ],
    )


@pytest.mark.asyncio
async def test_extract_writes_one_row_per_declared_kpi(
    kpi_engine: AsyncEngine, basics: Basics
) -> None:
    """A successful job with two KPIs writes exactly two ``JobKpiValue`` rows."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    await _set_descriptor(kpi_engine, basics.tool_version_id_a, _kpi_descriptor())
    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)

    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        result = await extract_kpis_for_job(
            audit,
            session,
            job=job,
            outputs_doc=_outputs(
                ("max_temperature", 372.15),
                ("max_pressure", 101325),
                ("iteration_count", 200),
                ("status_text", "converged"),
            ),
        )

    assert result.extracted == 2
    assert result.missing == 0
    assert result.total == 2

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    sa.select(JobKpiValue).where(JobKpiValue.job_id == basics.job_id_a)
                )
            ).scalars()
        )
        assert {r.kpi_name for r in rows} == {"max_temperature", "max_pressure"}
        by_name = {r.kpi_name: r for r in rows}
        assert by_name["max_temperature"].value_numeric == Decimal("372.15")
        assert by_name["max_temperature"].unit == "K"
        assert by_name["max_pressure"].value_numeric == Decimal("101325")
        # max_pressure has no `kpi_unit` override; falls back to `unit`.
        assert by_name["max_pressure"].unit == "Pa"


@pytest.mark.asyncio
async def test_extract_audit_recorded(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """A ``kpi.extracted`` audit event is appended with the per-KPI tally."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    await _set_descriptor(kpi_engine, basics.tool_version_id_a, _kpi_descriptor())
    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)

    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        await extract_kpis_for_job(
            audit,
            session,
            job=job,
            outputs_doc=_outputs(("max_temperature", 372.15), ("max_pressure", 101325)),
        )

    async with session_scope(factory) as session:
        events = list(
            (
                await session.execute(sa.select(AuditLog).where(AuditLog.action == "kpi.extracted"))
            ).scalars()
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["tool_alias"] == basics.tool_alias
        assert payload["tool_version_id"] == basics.tool_version_id_a
        assert payload["extracted"] == 2
        assert payload["missing"] == 0
        assert payload["total"] == 2


# ---- Sentinel handling -----------------------------------------------


@pytest.mark.asyncio
async def test_missing_kpi_value_writes_null(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """An ``outputs.json`` missing a declared KPI lands as ``value_numeric = NULL``."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    await _set_descriptor(kpi_engine, basics.tool_version_id_a, _kpi_descriptor())
    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)

    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        result = await extract_kpis_for_job(
            audit,
            session,
            job=job,
            outputs_doc=_outputs(("max_temperature", 372.15)),  # max_pressure absent
        )

    assert result.extracted == 1
    assert result.missing == 1
    assert result.total == 2

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    sa.select(JobKpiValue).where(JobKpiValue.job_id == basics.job_id_a)
                )
            ).scalars()
        )
        by_name = {r.kpi_name: r for r in rows}
        assert by_name["max_pressure"].value_numeric is None
        assert by_name["max_temperature"].value_numeric == Decimal("372.15")


@pytest.mark.asyncio
async def test_nan_kpi_value_writes_null(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """A NaN runtime value lands as ``value_numeric = NULL``."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    await _set_descriptor(kpi_engine, basics.tool_version_id_a, _kpi_descriptor())
    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)

    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        await extract_kpis_for_job(
            audit,
            session,
            job=job,
            outputs_doc=_outputs(("max_temperature", float("nan")), ("max_pressure", 101325)),
        )

    async with session_scope(factory) as session:
        row = (
            await session.execute(
                sa.select(JobKpiValue).where(
                    JobKpiValue.job_id == basics.job_id_a,
                    JobKpiValue.kpi_name == "max_temperature",
                )
            )
        ).scalar_one()
        assert row.value_numeric is None


# ---- No-op / edge cases ----------------------------------------------


@pytest.mark.asyncio
async def test_no_outputs_doc_writes_no_rows(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """The failure-path case — ``outputs_doc=None`` writes nothing."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    await _set_descriptor(kpi_engine, basics.tool_version_id_a, _kpi_descriptor())
    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)

    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        result = await extract_kpis_for_job(audit, session, job=job, outputs_doc=None)

    assert result == result.__class__(extracted=0, missing=0, total=0)

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    sa.select(JobKpiValue).where(JobKpiValue.job_id == basics.job_id_a)
                )
            ).scalars()
        )
        assert rows == []


@pytest.mark.asyncio
async def test_no_kpis_declared_writes_no_rows(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """A tool with no ``kpi = true`` outputs doesn't touch the warehouse."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    descriptor_no_kpis = {
        "alias": "heat_transfer",
        "outputs": [
            {"name": "x", "label": "X", "type": "number"},
            {"name": "y", "label": "Y", "type": "string"},
        ],
    }
    await _set_descriptor(kpi_engine, basics.tool_version_id_a, descriptor_no_kpis)
    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)

    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        result = await extract_kpis_for_job(
            audit, session, job=job, outputs_doc=_outputs(("x", 1.0))
        )

    assert result.total == 0

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    sa.select(JobKpiValue).where(JobKpiValue.job_id == basics.job_id_a)
                )
            ).scalars()
        )
        assert rows == []


# ---- Idempotence / UPSERT --------------------------------------------


@pytest.mark.asyncio
async def test_re_run_replaces_existing_row(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """A second extract for the same (job, kpi) updates the existing row, no duplicates."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    await _set_descriptor(kpi_engine, basics.tool_version_id_a, _kpi_descriptor())
    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)

    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        await extract_kpis_for_job(
            audit,
            session,
            job=job,
            outputs_doc=_outputs(("max_temperature", 300.0), ("max_pressure", 100.0)),
        )

    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        # Re-fire — simulates the supervisor heartbeat-loop retry on a
        # recovered-from-stalled job; same (job, kpi) UPSERTs the value.
        await extract_kpis_for_job(
            audit,
            session,
            job=job,
            outputs_doc=_outputs(("max_temperature", 400.0), ("max_pressure", 200.0)),
        )

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    sa.select(JobKpiValue).where(JobKpiValue.job_id == basics.job_id_a)
                )
            ).scalars()
        )
        assert len(rows) == 2  # No duplicates.
        by_name = {r.kpi_name: r for r in rows}
        assert by_name["max_temperature"].value_numeric == Decimal("400.0")
        assert by_name["max_pressure"].value_numeric == Decimal("200.0")


# ---- Never-raise contract --------------------------------------------


@pytest.mark.asyncio
async def test_never_raises_on_bad_descriptor(kpi_engine: AsyncEngine, basics: Basics) -> None:
    """A descriptor snapshot that isn't a dict short-circuits cleanly."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        tv = await session.get(ToolVersion, basics.tool_version_id_a)
        assert tv is not None
        tv.descriptor_snapshot = ["not", "a", "dict"]  # type: ignore[assignment]
        await session.flush()

    audit = AuditWriter(signing_key=None, key_id=None)
    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        result = await extract_kpis_for_job(
            audit, session, job=job, outputs_doc=_outputs(("x", 1.0))
        )

    # No raise; sentinel result.
    assert result.total == 0


@pytest.mark.asyncio
async def test_never_raises_when_inner_logic_explodes(
    kpi_engine: AsyncEngine, basics: Basics, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the inner body raises unexpectedly, the wrapper swallows + returns a zero tally."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415
    from matofx.kpi import service as _svc  # noqa: PLC0415

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic explosion")

    monkeypatch.setattr(_svc, "_extract_kpis_for_job_inner", _boom)

    audit = AuditWriter(signing_key=None, key_id=None)
    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        job = await session.get(Job, basics.job_id_a)
        assert job is not None
        # Should NOT raise — the never-raise wrapper catches it.
        result = await extract_kpis_for_job(
            audit, session, job=job, outputs_doc=_outputs(("x", 1.0))
        )
        assert result.total == 0
        # Outer transaction can still flush — extractor failure didn't poison it.
        await session.flush()

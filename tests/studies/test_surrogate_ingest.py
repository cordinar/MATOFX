"""Tests for source-agnostic surrogate ingestion (ADR-0008 §4.1, Phase 3).

Confirms ``collect_training_data`` pulls aligned ``(X, y)`` from the
unified ``study_result`` store for any source, that the result fits via
the unchanged ``fit_surrogate`` (carrying a ``source_ref`` tag +
``training_inputs``), that the ``FactorFilter`` subsets a source, that a
``field`` response is rejected (Phase 4), and — non-negotiable #10 —
that raw-column parametric callers are unchanged (no ``source_ref``).
"""

from __future__ import annotations

import pytest

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job
from matofx.studies import (
    FactorFilter,
    IngestError,
    ResponseSpec,
    collect_training_data,
    fit_surrogate,
)
from matofx.studies.orm import ParametricStudy, StudyResult
from tests.studies.conftest import Basics


def _study(basics: Basics) -> ParametricStudy:
    return ParametricStudy(
        project_id=basics.project_id,
        name="ingest",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        mode="grid",
        factor_spec={},
        sampling_params={},
        run_cap=100,
        created_by_user_id=basics.user_id,
    )


async def _seed_run(
    session,
    basics: Basics,
    study_id: int,
    *,
    x: float,
    y: float | None,
    source_kind: str = "parametric",
    source_id: int | None = None,
    validity: str = "valid",
    validity_reasons: list[dict[str, object]] | None = None,
) -> Job:
    """A completed study child + its ``study_result`` row (inputs x + label)."""
    job = Job(
        parametric_study_id=study_id,
        scenario_id=None,  # study child — no uq_job_scenario_tool collision
        name="run",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        state="complete",
        inputs={"x": x, "label": "a"},  # 'label' is non-numeric → not a factor
        created_by_user_id=basics.user_id,
    )
    session.add(job)
    await session.flush()
    session.add(
        StudyResult(
            source_kind=source_kind,
            source_id=source_id if source_id is not None else study_id,
            job_id=job.id,
            scalars={} if y is None else {"y": y},
            retained_files=[],
            validity=validity,
            validity_reasons=validity_reasons or [],
        )
    )
    await session.flush()
    return job


async def test_collect_scalar_and_fit(studies_engine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        for x in range(1, 7):  # 6 complete runs, x varies → fittable
            await _seed_run(session, basics, study.id, x=float(x), y=2.0 * x)

        data = await collect_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            response_spec=ResponseSpec(kind="scalar", name="y"),
        )
        # Numeric input 'x' is a factor; the string 'label' is excluded.
        assert "x" in data.factor_columns
        assert "label" not in data.factor_columns
        assert data.factor_columns["x"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        assert data.response == [2.0, 4.0, 6.0, 8.0, 10.0, 12.0]
        assert data.n_rows == 6
        assert data.n_complete == 6
        assert data.source_ref is not None
        assert data.source_ref.source_kind == "parametric"
        # 'label' was identical across every run → captured as a held
        # constant (ADR-0008 §7.6); the varied 'x' is not.
        assert data.input_constants == {"label": "a"}

        surr = fit_surrogate(data.factor_columns, data.response, source_ref=data.source_ref)
        assert surr.fitted
        assert surr.source_ref is not None
        assert surr.source_ref.source_id == study.id
        assert surr.training_inputs["x"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


async def test_collect_excludes_invalid_runs(studies_engine, basics: Basics) -> None:
    """Runs flagged invalid (ADR-0017) never train; exclusions are counted by code."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        for x in range(1, 5):  # four valid runs
            await _seed_run(session, basics, study.id, x=float(x), y=2.0 * x)
        for x in (9.0, 10.0):  # two invalid runs (out-of-bounds response)
            await _seed_run(
                session,
                basics,
                study.id,
                x=x,
                y=1.0e6,
                validity="invalid",
                validity_reasons=[{"output": "y", "code": "above_max"}],
            )

        data = await collect_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            response_spec=ResponseSpec(kind="scalar", name="y"),
        )
        assert data.n_rows == 4  # only the valid runs are kept
        assert data.factor_columns["x"] == [1.0, 2.0, 3.0, 4.0]
        assert data.n_excluded_invalid == 2
        assert data.excluded_reason_counts == {"above_max": 2}


async def test_collect_from_cohort_source(studies_engine, basics: Basics) -> None:
    """Ingestion is source-agnostic — a ``cohort`` source reads identically."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        for x in range(1, 6):
            await _seed_run(
                session,
                basics,
                study.id,
                x=float(x),
                y=float(x),
                source_kind="cohort",
                source_id=77,
            )
        data = await collect_training_data(
            session,
            source_kind="cohort",
            source_id=77,
            response_spec=ResponseSpec(kind="scalar", name="y"),
        )
        assert data.n_rows == 5
        assert data.source_ref is not None
        assert data.source_ref.source_kind == "cohort"
        assert data.source_ref.source_id == 77


async def test_factor_filter_subsets_source(studies_engine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        for x in range(1, 7):
            await _seed_run(session, basics, study.id, x=float(x), y=float(x))
        data = await collect_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            response_spec=ResponseSpec(kind="scalar", name="y"),
            factor_filter=FactorFilter(input_ranges={"x": {"min": 3, "max": 5}}),
        )
        assert data.factor_columns["x"] == [3.0, 4.0, 5.0]


async def test_output_filter_subsets_source(studies_engine, basics: Basics) -> None:
    """On-the-fly output-range filter drops runs by their scalar output value."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        for x in range(1, 7):  # output y == x, so filter y ∈ [2, 4] keeps x 2..4
            await _seed_run(session, basics, study.id, x=float(x), y=float(x))
        data = await collect_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            response_spec=ResponseSpec(kind="scalar", name="y"),
            factor_filter=FactorFilter(output_ranges={"y": {"min": 2, "max": 4}}),
        )
        assert data.factor_columns["x"] == [2.0, 3.0, 4.0]
        assert data.response == [2.0, 3.0, 4.0]


async def test_missing_scalar_counts_as_incomplete(studies_engine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        await _seed_run(session, basics, study.id, x=1.0, y=10.0)
        await _seed_run(session, basics, study.id, x=2.0, y=None)  # no response
        data = await collect_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            response_spec=ResponseSpec(kind="scalar", name="y"),
        )
        assert data.n_rows == 2
        assert data.n_complete == 1
        assert data.response == [10.0, None]


async def test_field_response_rejected(studies_engine) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(IngestError):
            await collect_training_data(
                session,
                source_kind="parametric",
                source_id=1,
                response_spec=ResponseSpec(kind="field", name="temperature"),
            )


def test_raw_column_caller_unchanged() -> None:
    """Non-negotiable #10: a raw-column fit has no source_ref (additive only)."""
    cols = {"x": [float(i) for i in range(1, 7)]}
    resp = [2.0 * i for i in range(1, 7)]
    surr = fit_surrogate(cols, resp)
    assert surr.fitted
    assert surr.source_ref is None
    assert surr.training_inputs["x"] == cols["x"]

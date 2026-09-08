"""Cohort-preview coverage (ADR-0020 §12, slice 3).

The question cohort preview asks is *"is this fit to learn from?"* — **before**
anything is committed, before a response is chosen and before any GP exists.
This is the service-level path that answers it: resolve the cohort, take the
training set ``ingest`` would produce, and run the response-agnostic design
pass over it.

Seeding is through the ORM (``feedback_test_seeding_via_orm``).
"""

from __future__ import annotations

import itertools

import pytest

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import CoverageSettings
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import make_scenario_service
from matofx.hierarchy.orm import Job
from matofx.studies.cohort import CohortService
from matofx.studies.coverage import CoverageReport, FactorOverride
from matofx.studies.orm import StudyResult
from tests.studies.conftest import Basics

_NAME_SEQ = itertools.count(1)


def _service() -> CohortService:
    return CohortService(AuditWriter(signing_key=None, key_id=None))


async def _scenario(session, basics: Basics) -> int:
    scenario = await make_scenario_service(AuditWriter(signing_key=None, key_id=None)).create(
        session,
        actor_user_id=basics.user_id,
        project_id=basics.project_id,
        name=f"CP-{next(_NAME_SEQ)}",
    )
    return int(scenario.id)


async def _member(
    session,
    basics: Basics,
    cohort_id: int,
    *,
    inputs: dict,
    scalars: dict | None = None,
    validity: str = "valid",
) -> Job:
    """A completed member run plus its ``study_result`` row."""
    job = Job(
        scenario_id=await _scenario(session, basics),
        name="run",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        state="complete",
        inputs=inputs,
        created_by_user_id=basics.user_id,
    )
    session.add(job)
    await session.flush()
    session.add(
        StudyResult(
            source_kind="cohort",
            source_id=cohort_id,
            job_id=job.id,
            scalars=scalars if scalars is not None else {"y": 1.0},
            retained_files=[],
            validity=validity,
            validity_reasons=[],
        )
    )
    await session.flush()
    return job


async def _cohort_with(session, basics: Basics, rows: list[dict]) -> int:
    """A fixed-membership cohort over ``rows``, with results populated."""
    svc = _service()
    cohort = await svc.create_cohort(
        session,
        actor_user_id=basics.user_id,
        name=f"C-{next(_NAME_SEQ)}",
        tool_alias=basics.tool_alias,
        tolerated_tool_version_ids=[basics.tool_version_id],
    )
    for row in rows:
        await _member(session, basics, int(cohort.id), inputs=row)
    return int(cohort.id)


# --- the preview answers without a response or a model -------------------


async def test_preview_returns_a_report_with_no_response_chosen(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rows = [
            {"thermal_conductivity": float(a), "thickness": float(b)}
            for a in (1.0, 3.0, 5.0, 7.0)
            for b in (1.0, 3.0, 5.0, 7.0)
        ]
        cohort_id = await _cohort_with(session, basics, rows)

        report = await _service().coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )

    assert isinstance(report, CoverageReport)
    assert report.per_response == ()  # nothing fitted, nothing claimed
    assert report.design.criterion == "model_free"
    assert report.verdict in ("good", "thin", "poor", "insufficient")
    assert report.source_kind == "cohort"


async def test_a_space_filling_cohort_reads_well(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rows = [
            {"thermal_conductivity": float(a), "thickness": float(b)}
            for a in (1.0, 3.0, 5.0, 7.0, 9.0)
            for b in (1.0, 3.0, 5.0, 7.0, 9.0)
        ]
        cohort_id = await _cohort_with(session, basics, rows)
        report = await _service().coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )
    assert report.verdict == "good"


async def test_a_clustered_cohort_reads_poor(
    studies_engine,
    basics: Basics,
) -> None:
    # The failure the whole ADR exists to catch: a tight cluster that would
    # report an excellent Q2 with nothing to contradict it.
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rows = [
            {"thermal_conductivity": 1.0 + i * 0.02, "thickness": 1.0 + j * 0.02}
            for i in range(5)
            for j in range(5)
        ]
        rows.append({"thermal_conductivity": 9.0, "thickness": 9.0})
        cohort_id = await _cohort_with(session, basics, rows)
        report = await _service().coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )
    assert report.verdict == "poor"
    assert report.design.message


# --- §1.1 the point set is the training set ------------------------------


async def test_invalid_runs_are_excluded_and_counted(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc = _service()
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name=f"C-{next(_NAME_SEQ)}",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        for i in range(10):
            await _member(
                session,
                basics,
                int(cohort.id),
                inputs={"thermal_conductivity": float(i + 1), "thickness": float(10 - i)},
                validity="invalid" if i >= 7 else "valid",
            )
        report = await svc.coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=int(cohort.id)
        )

    assert report.design.diagnostics.n_points == 7
    assert report.design.diagnostics.n_excluded_invalid == 3
    assert any(w.code == "excluded_runs" for w in report.design.warnings)


async def test_a_constant_input_shows_as_pinned_in_the_preview(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rows = [
            {"thermal_conductivity": float(i), "thickness": 2.0, "mesh_density": "fine"}
            for i in range(1, 9)
        ]
        cohort_id = await _cohort_with(session, basics, rows)
        report = await _service().coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )

    pinned = {f.name for f in report.envelope.factors if f.kind == "pinned"}
    assert {"thickness", "mesh_density"} <= pinned


# --- the envelope is declared, and editable ------------------------------


async def test_the_envelope_defaults_to_the_observed_spread(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rows = [
            {"thermal_conductivity": float(a), "thickness": float(b)}
            for a in (2.0, 4.0, 6.0)
            for b in (1.0, 5.0, 9.0)
        ]
        cohort_id = await _cohort_with(session, basics, rows)
        report = await _service().coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )

    axis = report.envelope.by_name("thermal_conductivity")
    assert (axis.lo, axis.hi) == (2.0, 6.0)


async def test_narrowing_the_envelope_is_honoured_and_counted(
    studies_engine,
    basics: Basics,
) -> None:
    # "We ran 1-9 but only trust 3-7."
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rows = [
            {"thermal_conductivity": float(a), "thickness": float(b)}
            for a in (1.0, 3.0, 5.0, 7.0, 9.0)
            for b in (1.0, 5.0, 9.0)
        ]
        cohort_id = await _cohort_with(session, basics, rows)
        report = await _service().coverage_preview(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort_id,
            overrides={"thermal_conductivity": FactorOverride(lo=3.0, hi=7.0)},
        )

    axis = report.envelope.by_name("thermal_conductivity")
    assert (axis.lo, axis.hi) == (3.0, 7.0)
    assert report.design.diagnostics.n_outside_envelope == 6  # the 1.0 and 9.0 columns


async def test_an_impossible_envelope_is_refused_with_a_plain_message(
    studies_engine,
    basics: Basics,
) -> None:
    from matofx.studies.coverage import CoverageError  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rows = [
            {"thermal_conductivity": float(a), "thickness": float(b)}
            for a in (1.0, 5.0, 9.0)
            for b in (1.0, 5.0, 9.0)
        ]
        cohort_id = await _cohort_with(session, basics, rows)
        with pytest.raises(CoverageError):
            await _service().coverage_preview(
                session,
                actor_user_id=basics.user_id,
                cohort_id=cohort_id,
                overrides={"thermal_conductivity": FactorOverride(lo=9.0, hi=1.0)},
            )


# --- degenerate cohorts --------------------------------------------------


async def test_an_empty_cohort_returns_insufficient_not_an_error(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort_id = await _cohort_with(session, basics, [])
        report = await _service().coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )
    assert report.verdict == "insufficient"
    assert report.design.message


async def test_the_analysis_floor_comes_from_config(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rows = [
            {"thermal_conductivity": float(a), "thickness": float(b)}
            for a in (1.0, 5.0, 9.0)
            for b in (1.0, 5.0, 9.0)
        ]
        cohort_id = await _cohort_with(session, basics, rows)
        svc = _service()
        strict = await svc.coverage_preview(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort_id,
            settings=CoverageSettings(min_points_for_analysis=50),
        )
        lax = await svc.coverage_preview(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort_id,
            settings=CoverageSettings(min_points_for_analysis=4),
        )
    assert strict.verdict == "insufficient"
    assert lax.verdict != "insufficient"


# --- the preview is read-only --------------------------------------------


async def test_preview_is_open_to_any_caller_who_can_see_the_cohort(
    studies_engine,
    basics: Basics,
) -> None:
    # The read-only analysis stays open; committing runs is a separate
    # right, gated in slice 4.
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rows = [
            {"thermal_conductivity": float(a), "thickness": float(b)}
            for a in (1.0, 5.0, 9.0)
            for b in (1.0, 5.0, 9.0)
        ]
        cohort_id = await _cohort_with(session, basics, rows)
        report = await _service().coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )
    assert report is not None

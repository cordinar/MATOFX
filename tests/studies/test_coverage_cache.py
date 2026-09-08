"""Cohort-preview caching (ADR-0020 §9.1, slice 5).

An ``auto_grow`` cohort re-resolves its query on **every read**. Recomputing
coverage per page load would violate ``feedback_performance_and_security_equal``
— so the report is cached against a key of the cohort, its resolved membership
and the declared envelope, and recomputed only when that key changes.

The cache must be **invisible**: a stale verdict after new runs arrive would be
worse than no cache at all, because the whole point of the feature is to tell
you when your archive has stopped being fit to learn from.
"""

from __future__ import annotations

import itertools

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import make_scenario_service
from matofx.hierarchy.orm import Job
from matofx.studies.cohort import CohortService
from matofx.studies.coverage import FactorOverride
from matofx.studies.orm import StudyResult
from tests.studies.conftest import Basics

_SEQ = itertools.count(1)


def _audit() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


def _service() -> CohortService:
    return CohortService(_audit())


async def _scenario(session, basics: Basics) -> int:
    scenario = await make_scenario_service(_audit()).create(
        session,
        actor_user_id=basics.user_id,
        project_id=basics.project_id,
        name=f"CC-{next(_SEQ)}",
    )
    return int(scenario.id)


async def _job(session, basics: Basics, row: dict) -> int:
    """One completed run of the cohort's tool."""
    job = Job(
        scenario_id=await _scenario(session, basics),
        name="run",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        state="complete",
        inputs=row,
        created_by_user_id=basics.user_id,
    )
    session.add(job)
    await session.flush()
    return int(job.id)


async def _result(session, cohort_id: int, job_id: int) -> None:
    session.add(
        StudyResult(
            source_kind="cohort",
            source_id=cohort_id,
            job_id=job_id,
            scalars={"y": 1.0},
            retained_files=[],
            validity="valid",
            validity_reasons=[],
        )
    )
    await session.flush()


async def _member(session, basics: Basics, cohort_id: int, row: dict) -> None:
    """Add a run to an ``auto_grow`` cohort — it matches by query."""
    await _result(session, cohort_id, await _job(session, basics, row))


async def _cohort(session, basics: Basics, rows: list[dict]) -> int:
    """A live (``auto_grow``) cohort over ``rows``."""
    cohort = await _service().create_cohort(
        session,
        actor_user_id=basics.user_id,
        name=f"CCH-{next(_SEQ)}",
        tool_alias=basics.tool_alias,
        tolerated_tool_version_ids=[basics.tool_version_id],
    )
    for row in rows:
        await _member(session, basics, int(cohort.id), row)
    return int(cohort.id)


async def _fixed_cohort(session, basics: Basics, rows: list[dict]) -> int:
    """A cohort pinned to exactly ``rows``.

    Two ``auto_grow`` cohorts over the same tool resolve to the **same** runs —
    each query matches the other's jobs — so comparing two designs in one
    database needs fixed membership to keep them distinct.
    """
    job_ids = [await _job(session, basics, row) for row in rows]
    cohort = await _service().create_cohort(
        session,
        actor_user_id=basics.user_id,
        name=f"CFX-{next(_SEQ)}",
        tool_alias=basics.tool_alias,
        tolerated_tool_version_ids=[basics.tool_version_id],
        auto_grow=False,
        member_job_ids=job_ids,
    )
    for job_id in job_ids:
        await _result(session, int(cohort.id), job_id)
    return int(cohort.id)


def _clustered() -> list[dict]:
    return [
        {"thermal_conductivity": 1.0 + i * 0.1, "thickness": 1.0 + j * 0.1}
        for i in range(4)
        for j in range(4)
    ] + [{"thermal_conductivity": 9.0, "thickness": 9.0}]


# --- the cache is a cache ------------------------------------------------


async def test_an_unchanged_read_does_not_recompute(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc = _service()
        cohort_id = await _cohort(session, basics, _clustered())

        first = await svc.coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )
        before = svc.coverage_cache_computations
        second = await svc.coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )

    assert svc.coverage_cache_computations == before  # no second analysis
    assert second is first  # and the same object, not an equal copy


async def test_the_cached_report_is_identical_not_merely_similar(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc = _service()
        cohort_id = await _cohort(session, basics, _clustered())
        a = await svc.coverage_preview(session, actor_user_id=basics.user_id, cohort_id=cohort_id)
        b = await svc.coverage_preview(session, actor_user_id=basics.user_id, cohort_id=cohort_id)
    assert a.verdict == b.verdict
    assert a.computed_at == b.computed_at


# --- and it invalidates on everything that changes the answer ------------


async def test_new_runs_invalidate_the_cache(
    studies_engine,
    basics: Basics,
) -> None:
    # An auto_grow cohort that grew must not keep reporting yesterday's
    # verdict — that is the exact failure the feature exists to prevent.
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc = _service()
        cohort_id = await _cohort(session, basics, _clustered())
        first = await svc.coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )
        before = svc.coverage_cache_computations

        for value in (3.0, 5.0, 7.0):
            await _member(
                session,
                basics,
                cohort_id,
                {"thermal_conductivity": value, "thickness": value},
            )
        second = await svc.coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id
        )

    assert svc.coverage_cache_computations == before + 1
    assert second.design.diagnostics.n_points > first.design.diagnostics.n_points


async def test_a_changed_envelope_invalidates_the_cache(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc = _service()
        cohort_id = await _cohort(session, basics, _clustered())
        await svc.coverage_preview(session, actor_user_id=basics.user_id, cohort_id=cohort_id)
        before = svc.coverage_cache_computations

        narrowed = await svc.coverage_preview(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort_id,
            overrides={"thickness": FactorOverride(lo=2.0, hi=6.0)},
        )

    assert svc.coverage_cache_computations == before + 1
    axis = narrowed.envelope.by_name("thickness")
    assert (axis.lo, axis.hi) == (2.0, 6.0)


async def test_two_cohorts_do_not_share_a_cache_entry(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc = _service()
        spread = [
            {"thermal_conductivity": float(a), "thickness": float(b)}
            for a in (1.0, 3.0, 5.0, 7.0, 9.0)
            for b in (1.0, 3.0, 5.0, 7.0, 9.0)
        ]
        clustered_id = await _fixed_cohort(session, basics, _clustered())
        spread_id = await _fixed_cohort(session, basics, spread)

        poor = await svc.coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=clustered_id
        )
        good = await svc.coverage_preview(
            session, actor_user_id=basics.user_id, cohort_id=spread_id
        )

    assert poor.verdict == "poor"
    assert good.verdict == "good"


async def test_settings_are_part_of_the_key(
    studies_engine,
    basics: Basics,
) -> None:
    # Thresholds are config-tunable; a cached verdict computed under different
    # floors is a different answer to a different question.
    from matofx.config import CoverageSettings  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc = _service()
        cohort_id = await _cohort(session, basics, _clustered())
        await svc.coverage_preview(session, actor_user_id=basics.user_id, cohort_id=cohort_id)
        before = svc.coverage_cache_computations
        strict = await svc.coverage_preview(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort_id,
            settings=CoverageSettings(min_points_for_analysis=100),
        )

    assert svc.coverage_cache_computations == before + 1
    assert strict.verdict == "insufficient"


async def test_the_cache_is_bounded(
    studies_engine,
    basics: Basics,
) -> None:
    # A per-process dict keyed on membership would grow without limit on an
    # install with many cohorts and an auto_grow query that keeps moving.
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc = _service()
        cohort_id = await _cohort(session, basics, _clustered())
        for i in range(40):
            await svc.coverage_preview(
                session,
                actor_user_id=basics.user_id,
                cohort_id=cohort_id,
                overrides={"thickness": FactorOverride(lo=1.0, hi=9.0 + i)},
            )
    assert svc.coverage_cache_size <= 32

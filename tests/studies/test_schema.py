"""Slice-1 schema-constraint tests (run on SQLite + Postgres via 0032).

These prove the non-negotiable data-model invariants from
designs/parametric-studies.md §2 / §13: the study CHECKs, the
deliberately-not-unique study name, and the job home-exactly-one XOR
that lets N same-tool study children coexist while the existing
one-tool-per-scenario rule for standalone jobs is untouched.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job
from matofx.studies.orm import ParametricStudy, ParametricStudyResult
from tests.studies.conftest import Basics


def _study(b: Basics, **overrides: object) -> ParametricStudy:
    kwargs: dict[str, object] = {
        "project_id": b.project_id,
        "name": "sweep",
        "tool_alias": b.tool_alias,
        "tool_version_id": b.tool_version_id,
        "mode": "grid",
        "factor_spec": {},
        "sampling_params": {},
        "run_cap": 200,
        "created_by_user_id": b.user_id,
    }
    kwargs.update(overrides)
    return ParametricStudy(**kwargs)


def _job(b: Basics, **overrides: object) -> Job:
    kwargs: dict[str, object] = {
        "name": "child",
        "tool_alias": b.tool_alias,
        "tool_version_id": b.tool_version_id,
        "created_by_user_id": b.user_id,
        "inputs": {},
    }
    kwargs.update(overrides)
    return Job(**kwargs)


async def test_insert_valid_study(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _study(basics)
        session.add(study)
        await session.flush()
        assert study.id is not None
        assert study.state == "draft"


async def test_bad_state_rejected(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(_study(basics, state="bogus"))
        try:
            await session.flush()
            raise AssertionError("expected ck_parametric_study_state to reject")
        except IntegrityError:
            await session.rollback()


async def test_bad_mode_rejected(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(_study(basics, mode="d_optimal"))
        try:
            await session.flush()
            raise AssertionError("expected ck_parametric_study_mode to reject")
        except IntegrityError:
            await session.rollback()


async def test_negative_count_rejected(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(_study(basics, skipped_invalid_count=-1))
        try:
            await session.flush()
            raise AssertionError("expected counts CHECK to reject")
        except IntegrityError:
            await session.rollback()


async def test_study_name_not_unique_within_project(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """Design §2: a study name is deliberately NOT project-unique."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(_study(basics, name="dup"))
        session.add(_study(basics, name="dup"))
        await session.flush()  # must not raise


async def test_job_scenario_home_ok(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(_job(basics, scenario_id=basics.scenario_id))
        await session.flush()


async def test_job_study_home_ok(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _study(basics)
        session.add(study)
        await session.flush()
        session.add(_job(basics, parametric_study_id=study.id))
        await session.flush()


async def test_job_no_home_rejected(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(_job(basics))  # neither scenario nor study
        try:
            await session.flush()
            raise AssertionError("expected ck_job_exactly_one_home to reject")
        except IntegrityError:
            await session.rollback()


async def test_job_both_homes_rejected(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _study(basics)
        session.add(study)
        await session.flush()
        session.add(_job(basics, scenario_id=basics.scenario_id, parametric_study_id=study.id))
        try:
            await session.flush()
            raise AssertionError("expected ck_job_exactly_one_home to reject")
        except IntegrityError:
            await session.rollback()


async def test_two_study_children_same_tool_ok(studies_engine: AsyncEngine, basics: Basics) -> None:
    """The whole point of NULL-scenario children (design §2): N
    same-tool jobs under one study must NOT collide on
    uq_job_scenario_tool_standalone."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _study(basics)
        session.add(study)
        await session.flush()
        session.add(_job(basics, parametric_study_id=study.id, name="c1"))
        session.add(_job(basics, parametric_study_id=study.id, name="c2"))
        await session.flush()  # must not raise


async def test_standalone_one_tool_per_scenario_still_enforced(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """Regression guard: the 0032 job recreate must preserve the
    existing partial unique index."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(_job(basics, scenario_id=basics.scenario_id, name="a"))
        session.add(_job(basics, scenario_id=basics.scenario_id, name="b"))
        try:
            await session.flush()
            raise AssertionError("expected uq_job_scenario_tool_standalone to reject")
        except IntegrityError:
            await session.rollback()


async def test_permission_seeded_for_builtin_roles(
    studies_engine: AsyncEngine,
) -> None:
    """The 0032 perm seed runs during ``upgrade head`` (roles are
    seeded by bootstrap migration 0006), independent of ``basics``."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT r.name FROM role_permission rp "
                    "JOIN role r ON r.id = rp.role_id "
                    "WHERE rp.permission = :p"
                ).bindparams(p="parametric_studies.create")
            )
        ).fetchall()
    granted = {row[0] for row in rows}
    assert {"administrator", "beta_tester", "regular_user"} <= granted


# --- study_result schema (b0-2 / migration 0041; renamed by 0058) --------


def _result(study_id: int, job_id: int, **overrides: object) -> ParametricStudyResult:
    kwargs: dict[str, object] = {
        "source_kind": "parametric",
        "source_id": study_id,
        "job_id": job_id,
        "scalars": {"temperature": 42.0, "status": "ok"},
        "retained_files": [{"filename": "outputs/results.csv", "kind": "csv_plot"}],
    }
    kwargs.update(overrides)
    return ParametricStudyResult(**kwargs)


async def test_insert_valid_result(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        row = _result(study.id, job.id)
        session.add(row)
        await session.flush()
        assert row.id is not None
        assert row.scalars == {"temperature": 42.0, "status": "ok"}
        assert row.retained_files == [{"filename": "outputs/results.csv", "kind": "csv_plot"}]


async def test_unique_source_job_rejects_duplicate(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """One row per ``(source_kind, source_id, job_id)`` (migration 0060).

    A second row for the *same* source + job is rejected, but the same
    job under a *different* source (e.g. a cohort over a parametric
    child) is allowed — that is the point of the composite UNIQUE.
    """
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        session.add(_result(study.id, job.id))
        await session.flush()
        # Same job under a different source — allowed.
        session.add(_result(study.id, job.id, source_kind="cohort", source_id=999))
        await session.flush()
        # Same source + job again — rejected.
        session.add(_result(study.id, job.id, scalars={"x": 1}))
        try:
            await session.flush()
            raise AssertionError("expected uq_study_result_source_job to reject")
        except IntegrityError:
            await session.rollback()


async def test_result_cascades_on_job_delete(studies_engine: AsyncEngine, basics: Basics) -> None:
    """``ON DELETE CASCADE`` from ``job`` removes the row (draft reaping).

    Migration 0060 dropped the ``parametric_study_id`` FK; the ``job_id``
    cascade (0041) is now the sole reaping path — a draft child's removal
    takes its results row with it.
    """
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        session.add(_result(study.id, job.id))
        await session.flush()
        jid = job.id
        # Direct SQL DELETE — bypasses ORM/service layers (D20 is a
        # service-layer rule; this verifies the DB constraint shape).
        await session.execute(sa.text("DELETE FROM job WHERE id = :i").bindparams(i=jid))
        await session.flush()
        remaining = (
            await session.execute(
                sa.text("SELECT COUNT(*) FROM study_result WHERE job_id = :i").bindparams(i=jid)
            )
        ).scalar()
        assert remaining == 0


async def test_result_defaults_source_kind_and_rejects_unknown_kind(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """``source_kind`` defaults to ``parametric`` (studies-engine §6) and
    the CHECK rejects a value outside the closed vocabulary."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        # source_kind omitted → ORM default "parametric"; source_id set.
        row = ParametricStudyResult(
            source_id=study.id,
            job_id=job.id,
            scalars={},
            retained_files=[],
        )
        session.add(row)
        await session.flush()
        assert row.source_kind == "parametric"
        assert row.source_id == study.id

    async with session_scope(factory) as session:
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        session.add(_result(study.id, job.id, source_kind="bogus"))
        try:
            await session.flush()
            raise AssertionError("expected ck_study_result_source_kind to reject 'bogus'")
        except IntegrityError:
            await session.rollback()

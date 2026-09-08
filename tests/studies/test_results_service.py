"""Tests for the ``parametric_study_result`` write hook + read service (b0-3).

Pure SQLite (no Postgres gate): exercises ``record_study_result``
(the runner hook from ``Runner._post_complete``) and the read-side
``ResultsService`` directly via the studies engine fixture, without
spinning up the full supervisor / runner.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.studies import (
    ParametricStudyResult,
    ResultsService,
    StudyService,
    record_study_result,
)
from matofx.tools.exec.contracts import (
    OUTPUTS_DOCUMENT_SCHEMA_VERSION,
    OutputsDocument,
    OutputValue,
)
from matofx.tools.orm import Tool
from tests.studies.conftest import Basics
from tests.studies.test_schema import _job, _study


def _audit() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


def _study_service() -> StudyService:
    return StudyService(audit=_audit())


def _outputs(*outs: OutputValue, tool_alias: str = "demo") -> OutputsDocument:
    return OutputsDocument(
        schema_version=OUTPUTS_DOCUMENT_SCHEMA_VERSION,
        tool_alias=tool_alias,
        outputs=list(outs),
    )


def _scalar(name: str, type_: str, value: Any) -> OutputValue:
    return OutputValue(name=name, label=name.title(), type=type_, value=value)  # type: ignore[arg-type]


def _file_out(name: str, filename: str) -> OutputValue:
    return OutputValue(name=name, label=name.title(), type="file", filename=filename)


async def _tool_for(session: Any, b: Basics, descriptor: dict[str, Any]) -> Tool:
    """Patch the seeded tool's descriptor for this test's purpose."""
    tool = (await session.execute(select(Tool).where(Tool.alias == b.tool_alias))).scalar_one()
    tool.descriptor = descriptor
    await session.flush()
    return tool


# --- record_study_result write hook --------------------------------------


async def test_record_writes_scalars_and_file_refs(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """Success path: numeric + string scalars persist; file outputs
    are referenced with descriptor ``file_type`` as the kind."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        tool = await _tool_for(
            session,
            basics,
            {
                "outputs": [
                    {"name": "temperature", "type": "number"},
                    {"name": "status", "type": "string"},
                    {"name": "report", "type": "file", "file_type": "html_plot"},
                    {"name": "curve", "type": "file", "file_type": "csv_plot"},
                ]
            },
        )
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        doc = _outputs(
            _scalar("temperature", "number", 42.5),
            _scalar("status", "string", "ok"),
            _file_out("report", "outputs/report.html"),
            _file_out("curve", "outputs/curve.csv"),
        )

        await record_study_result(
            _audit(),
            session,
            job=job,
            tool=tool,
            outputs_doc=doc,
            succeeded=True,
        )

        row = (
            await session.execute(
                select(ParametricStudyResult).where(ParametricStudyResult.job_id == job.id)
            )
        ).scalar_one()
        assert row.scalars == {"temperature": 42.5, "status": "ok"}
        # Files in descriptor order; kind = file_type or "file" fallback.
        assert row.retained_files == [
            {"name": "report", "filename": "outputs/report.html", "kind": "html_plot"},
            {"name": "curve", "filename": "outputs/curve.csv", "kind": "csv_plot"},
        ]
        # Discriminator tagged for the unified store (studies-engine §6).
        assert row.source_kind == "parametric"
        assert row.source_id == study.id


async def test_record_stamps_valid_when_in_bounds(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """A finite, in-range scalar yields validity='valid' with no reasons (ADR-0017)."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        tool = await _tool_for(
            session,
            basics,
            {"outputs": [{"name": "fi_max", "type": "number", "min": 0.0, "max": 10.0}]},
        )
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        await record_study_result(
            _audit(),
            session,
            job=job,
            tool=tool,
            outputs_doc=_outputs(_scalar("fi_max", "number", 2.5)),
            succeeded=True,
        )
        row = (
            await session.execute(
                select(ParametricStudyResult).where(ParametricStudyResult.job_id == job.id)
            )
        ).scalar_one()
        assert row.validity == "valid"
        assert row.validity_reasons == []


async def test_record_flags_non_finite_and_out_of_bounds(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """NaN trips the finiteness floor; an over-max scalar trips its bound."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        tool = await _tool_for(
            session,
            basics,
            {
                "outputs": [
                    {"name": "fi_max", "type": "number", "min": 0.0, "max": 10.0},
                    {"name": "mass", "type": "number"},
                ]
            },
        )
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        await record_study_result(
            _audit(),
            session,
            job=job,
            tool=tool,
            outputs_doc=_outputs(
                _scalar("fi_max", "number", 1.0e6),  # above max
                _scalar("mass", "number", float("nan")),  # non-finite
            ),
            succeeded=True,
        )
        row = (
            await session.execute(
                select(ParametricStudyResult).where(ParametricStudyResult.job_id == job.id)
            )
        ).scalar_one()
        assert row.validity == "invalid"
        codes = {(r["output"], r["code"]) for r in row.validity_reasons}
        assert ("fi_max", "above_max") in codes
        assert ("mass", "non_finite") in codes


async def test_record_allow_non_finite_stays_valid(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """An output that opts into allow_non_finite is not flagged for Inf/NaN."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        tool = await _tool_for(
            session,
            basics,
            {"outputs": [{"name": "x", "type": "number", "allow_non_finite": True}]},
        )
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        await record_study_result(
            _audit(),
            session,
            job=job,
            tool=tool,
            outputs_doc=_outputs(_scalar("x", "number", float("inf"))),
            succeeded=True,
        )
        row = (
            await session.execute(
                select(ParametricStudyResult).where(ParametricStudyResult.job_id == job.id)
            )
        ).scalar_one()
        assert row.validity == "valid"
        assert row.validity_reasons == []


async def test_backfill_validity_flips_existing_rows(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """backfill_validity re-evaluates pre-existing rows against current bounds."""
    from matofx.studies.results_service import backfill_validity  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        tool = await _tool_for(
            session,
            basics,
            {"outputs": [{"name": "fi_max", "type": "number", "min": 0.0, "max": 10.0}]},
        )
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        # A pre-0085-style row: stored as valid, but its scalar is out of bounds.
        session.add(
            ParametricStudyResult(
                source_kind="parametric",
                source_id=study.id,
                job_id=job.id,
                scalars={"fi_max": 1.0e6},
                retained_files=[],
                validity="valid",
                validity_reasons=[],
            )
        )
        await session.flush()
        assert tool is not None  # descriptor patched in

        scanned, updated = await backfill_validity(session)
        assert scanned >= 1
        assert updated == 1
        row = (
            await session.execute(
                select(ParametricStudyResult).where(ParametricStudyResult.job_id == job.id)
            )
        ).scalar_one()
        assert row.validity == "invalid"
        assert any(r["code"] == "above_max" for r in row.validity_reasons)


async def test_record_failure_path_writes_empty_row(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """Failed / cancelled children get a row with empty scalars +
    empty retained_files (D3 / partial-data banner; never dropped)."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        tool = await _tool_for(session, basics, {})
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()

        await record_study_result(
            _audit(),
            session,
            job=job,
            tool=tool,
            outputs_doc=None,
            succeeded=False,
        )

        row = (
            await session.execute(
                select(ParametricStudyResult).where(ParametricStudyResult.job_id == job.id)
            )
        ).scalar_one()
        assert row.scalars == {}
        assert row.retained_files == []


async def test_record_is_noop_for_non_study_children(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """Non-study children (workflow / standalone jobs) get no row."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        tool = await _tool_for(session, basics, {})
        job = _job(basics, scenario_id=basics.scenario_id)  # scenario child, not study
        session.add(job)
        await session.flush()

        await record_study_result(
            _audit(),
            session,
            job=job,
            tool=tool,
            outputs_doc=_outputs(_scalar("x", "number", 1)),
            succeeded=True,
        )

        count = (
            (
                await session.execute(
                    select(ParametricStudyResult).where(ParametricStudyResult.job_id == job.id)
                )
            )
            .scalars()
            .all()
        )
        assert count == []


async def test_record_is_idempotent_under_retry(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """A second call for the same job is a no-op (UNIQUE on job_id;
    runner retries / multi-pickup must not fail)."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        tool = await _tool_for(session, basics, {})
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(basics, parametric_study_id=study.id, scenario_id=None)
        session.add(job)
        await session.flush()
        doc = _outputs(_scalar("x", "number", 1.0))
        await record_study_result(
            _audit(), session, job=job, tool=tool, outputs_doc=doc, succeeded=True
        )
        # Second call would otherwise raise IntegrityError (UNIQUE
        # on job_id) — the existence check makes it a clean no-op.
        await record_study_result(
            _audit(), session, job=job, tool=tool, outputs_doc=doc, succeeded=True
        )
        rows = (
            (
                await session.execute(
                    select(ParametricStudyResult).where(ParametricStudyResult.job_id == job.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1


# --- ResultsService read side --------------------------------------------


async def test_results_service_returns_matrix(studies_engine: AsyncEngine, basics: Basics) -> None:
    """Two children, one with a result row and one without (still
    running). Both surface in the matrix; the un-completed child has
    empty scalars + retained_files."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        tool = await _tool_for(session, basics, {"outputs": [{"name": "y", "type": "number"}]})
        study = _study(basics)
        session.add(study)
        await session.flush()
        job_a = _job(
            basics,
            parametric_study_id=study.id,
            scenario_id=None,
            name="a",
            inputs={"x": 1},
        )
        job_b = _job(
            basics,
            parametric_study_id=study.id,
            scenario_id=None,
            name="b",
            inputs={"x": 2},
        )
        session.add(job_a)
        session.add(job_b)
        await session.flush()
        await record_study_result(
            _audit(),
            session,
            job=job_a,
            tool=tool,
            outputs_doc=_outputs(_scalar("y", "number", 10.0)),
            succeeded=True,
        )
        # job_b has no result row yet.

        view = await ResultsService(_study_service()).get_results_for_study(
            session, actor_user_id=basics.user_id, study_id=study.id
        )

    assert view.study_id == study.id
    assert [c.job_id for c in view.children] == [job_a.id, job_b.id]
    assert view.children[0].scalars == {"y": 10.0}
    assert view.children[0].inputs == {"x": 1}
    assert view.children[1].scalars == {}  # not yet recorded
    assert view.children[1].retained_files == []


async def test_results_matrix_ignores_cohort_result_rows(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """A parametric child that also joined a Job Cohort carries a second
    ``study_result`` row (``source_kind='cohort'``). The matrix join must
    pin to this study's ``parametric`` rows so the job isn't fanned out
    into a duplicate run — and must show the study's own scalars, not the
    cohort row's."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        tool = await _tool_for(session, basics, {"outputs": [{"name": "y", "type": "number"}]})
        study = _study(basics)
        session.add(study)
        await session.flush()
        job = _job(
            basics, parametric_study_id=study.id, scenario_id=None, name="a", inputs={"x": 1}
        )
        session.add(job)
        await session.flush()
        await record_study_result(
            _audit(),
            session,
            job=job,
            tool=tool,
            outputs_doc=_outputs(_scalar("y", "number", 10.0)),
            succeeded=True,
        )
        # The lazy cohort populate (ADR-0008 §3.3) writes a second result row
        # for the same job once it joins a cohort.
        session.add(
            ParametricStudyResult(
                source_kind="cohort",
                source_id=999,
                job_id=job.id,
                scalars={"y": 99.0},
                retained_files=[],
            )
        )
        await session.flush()

        view = await ResultsService(_study_service()).get_results_for_study(
            session, actor_user_id=basics.user_id, study_id=study.id
        )

    assert [c.job_id for c in view.children] == [job.id]  # one row, not duplicated
    assert view.children[0].scalars == {"y": 10.0}  # the parametric row, not the cohort's

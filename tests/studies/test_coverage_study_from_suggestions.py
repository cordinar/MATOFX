"""Suggestions become a governed study (ADR-0020 §5, §11.5, slice 4).

The output of "suggest runs to fill the gaps" lands as a **parametric study**
(ADR-0001) — an auditable batch inside the platform, not a CSV emailed around.

Two rights, deliberately different: the read-only analysis is open to anyone
who can see the cohort, while **committing** the runs spawns real solver jobs
and spends real compute, so it is gated on the existing study-create
permission. Seeding through the ORM (``feedback_test_seeding_via_orm``).
"""

from __future__ import annotations

import itertools

import pytest

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import make_scenario_service
from matofx.hierarchy.errors import AccessDeniedError
from matofx.hierarchy.orm import Job, ProjectAcl
from matofx.studies.cohort import CohortService
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.users import make_password_hasher, make_user_service
from tests.studies._coverage_fixtures import descriptor, num
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
        name=f"SS-{next(_SEQ)}",
    )
    return int(scenario.id)


async def _cohort_with(session, basics: Basics, rows: list[dict]) -> int:
    svc = _service()
    cohort = await svc.create_cohort(
        session,
        actor_user_id=basics.user_id,
        name=f"SC-{next(_SEQ)}",
        tool_alias=basics.tool_alias,
        tolerated_tool_version_ids=[basics.tool_version_id],
    )
    for row in rows:
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
        session.add(
            StudyResult(
                source_kind="cohort",
                source_id=int(cohort.id),
                job_id=job.id,
                scalars={"y": 1.0},
                retained_files=[],
                validity="valid",
                validity_reasons=[],
            )
        )
    await session.flush()
    return int(cohort.id)


def _clustered() -> list[dict]:
    """A design with a large, obvious hole — so there is something to suggest."""
    rows = [
        {"thermal_conductivity": 1.0 + i * 0.2, "thickness": 1.0 + j * 0.2}
        for i in range(4)
        for j in range(4)
    ]
    rows.append({"thermal_conductivity": 9.0, "thickness": 9.0})
    return rows


# --- suggesting ----------------------------------------------------------


async def test_a_cohort_can_suggest_runs_with_no_model_fitted(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort_id = await _cohort_with(session, basics, _clustered())
        batch = await _service().coverage_suggest(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id, n=4
        )

    assert len(batch.points) == 4
    assert all({"thermal_conductivity", "thickness"} <= set(p) for p in batch.points)


async def test_suggesting_is_open_and_commits_nothing(
    studies_engine,
    basics: Basics,
) -> None:
    # Read-only: no study, no jobs, no compute.
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort_id = await _cohort_with(session, basics, _clustered())
        await _service().coverage_suggest(
            session, actor_user_id=basics.user_id, cohort_id=cohort_id, n=3
        )
        studies = (await session.execute(_all_studies())).all()
    assert studies == []


def _all_studies():
    from sqlalchemy import select  # noqa: PLC0415

    return select(ParametricStudy.id)


# --- creating the study --------------------------------------------------


async def test_the_suggestions_land_as_a_running_parametric_study(
    studies_engine,
    basics: Basics,
) -> None:
    """The batch is run, not parked (ADR-0020 §11.5).

    It used to stop at ``draft``, which read as "Not submitted yet" on the page
    the user was sent to expressly to watch it run — after they had priced the
    batch, seen every proposed run, and pressed a button naming the cohort it
    feeds. The governance gate is unchanged: `submit` still honours the tool's
    ``approvals_policy`, and this fixture's tool requires none.
    """
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort_id = await _cohort_with(session, basics, _clustered())
        study = await _service().create_study_from_coverage(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort_id,
            project_id=basics.project_id,
            name="Fill the gaps",
            n=4,
        )

    assert study.state == "running"  # submitted for us; approvals still gate it
    assert study.mode == "table"  # exact rows, no cross-product
    assert study.tool_alias == basics.tool_alias
    assert len(study.sampling_params["runs"]) == 4


async def test_every_varied_factor_is_a_run_table_column(
    studies_engine,
    basics: Basics,
) -> None:
    # `table` mode requires it: values come from the rows, not a domain.
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort_id = await _cohort_with(session, basics, _clustered())
        study = await _service().create_study_from_coverage(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort_id,
            project_id=basics.project_id,
            name="cols",
            n=3,
        )

    varied = {n: f for n, f in study.factor_spec.items() if f.get("role") == "vary"}
    assert varied
    assert all(f["vary"]["kind"] == "column" for f in varied.values())


async def test_the_generated_study_actually_generates(
    studies_engine,
    basics: Basics,
) -> None:
    # The whole point of handing them to ADR-0001 is that the batch is real.
    from matofx.studies.factors import FactorSpec  # noqa: PLC0415
    from matofx.studies.generate import SamplingParams, generate  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort_id = await _cohort_with(session, basics, _clustered())
        study = await _service().create_study_from_coverage(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort_id,
            project_id=basics.project_id,
            name="real",
            n=4,
        )
        spec = FactorSpec.from_obj(study.factor_spec)
        result = generate(
            descriptor(
                num("thermal_conductivity", 0.0, 20.0),
                num("thickness", 0.0, 20.0),
            ),
            spec,
            study.mode,
            SamplingParams.model_validate(study.sampling_params),
            run_cap=100,
        )
        rows = result.points

    assert len(rows) == 4
    for row in rows:
        assert "thermal_conductivity" in row
        assert "thickness" in row


async def test_a_pinned_input_is_carried_as_a_constant_not_a_column(
    studies_engine,
    basics: Basics,
) -> None:
    # It never varied, so it is context — every suggested run must reproduce
    # it or the batch isn't comparable with what it is filling in.
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rows = [{**r, "mesh_density": "fine"} for r in _clustered()]
        cohort_id = await _cohort_with(session, basics, rows)
        study = await _service().create_study_from_coverage(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort_id,
            project_id=basics.project_id,
            name="pinned",
            n=3,
        )

    assert study.factor_spec["mesh_density"] == {"role": "constant", "value": "fine"}


async def test_the_study_records_where_it_came_from(
    studies_engine,
    basics: Basics,
) -> None:
    # A batch spawned by coverage should say so — otherwise the runs look
    # like someone's hand-authored table a month later.
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort_id = await _cohort_with(session, basics, _clustered())
        study = await _service().create_study_from_coverage(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort_id,
            project_id=basics.project_id,
            name="provenance",
            n=3,
        )

    assert str(cohort_id) in study.description
    assert "coverage" in study.description.lower()


async def test_nothing_to_suggest_is_refused_rather_than_creating_an_empty_study(
    studies_engine,
    basics: Basics,
) -> None:
    from matofx.studies.coverage import CoverageError  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        # Every input constant → nothing a new run could vary.
        rows = [{"thermal_conductivity": 2.0, "thickness": 3.0} for _ in range(8)]
        cohort_id = await _cohort_with(session, basics, rows)
        with pytest.raises(CoverageError, match=r"(?i)vary|suggest"):
            await _service().create_study_from_coverage(
                session,
                actor_user_id=basics.user_id,
                cohort_id=cohort_id,
                project_id=basics.project_id,
                name="empty",
                n=3,
            )


# --- §11.5 committing runs is a separate right ---------------------------


async def test_a_user_without_the_study_permission_cannot_commit_runs(
    studies_engine,
    basics: Basics,
    _auth_settings,  # noqa: PT019
) -> None:
    # Every built-in role *except* the most restricted holds
    # parametric_studies.create, so the gate is exercised through a custom
    # role — which is how a customer would actually restrict it
    # ([[project_four_default_user_roles]]: roles are runtime data).
    from matofx.users import RoleService  # noqa: PLC0415
    from matofx.users.permissions import Permission  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort_id = await _cohort_with(session, basics, _clustered())
        hasher = make_password_hasher(_auth_settings)
        users = make_user_service(_auth_settings, _audit(), hasher=hasher)
        role = await RoleService(_audit()).create_role(
            session,
            actor_user_id=basics.user_id,
            name=f"reader-{next(_SEQ)}",
            description="Can read, cannot commit compute",
            permissions=[Permission.LIVE_JOBS_VIEW],
        )
        plain = await users.create_user(
            session,
            username=f"plain{next(_SEQ)}",
            email=f"plain{next(_SEQ)}@example.com",
            password="Correct-Horse-Battery-9",
            full_name="Plain User",
            role=role.name,
        )
        # They can see the cohort's project. Without this the runs are hidden
        # from them (D7), the cohort is empty, and there is nothing to suggest.
        session.add(
            ProjectAcl(
                project_id=basics.project_id,
                user_id=plain.id,
                role="viewer",
                granted_by_user_id=basics.user_id,
            )
        )
        await session.flush()

        # The read-only analysis stays available to them...
        batch = await _service().coverage_suggest(
            session, actor_user_id=plain.id, cohort_id=cohort_id, n=3
        )
        assert batch.points

        # ...but spending compute is not theirs to do.
        with pytest.raises(AccessDeniedError, match=r"(?i)permission"):
            await _service().create_study_from_coverage(
                session,
                actor_user_id=plain.id,
                cohort_id=cohort_id,
                project_id=basics.project_id,
                name="not mine",
                n=3,
            )

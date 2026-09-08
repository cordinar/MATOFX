"""OptimisationService lifecycle + the job 1-of-3 home CHECK (ADR-0009 §12/§14).

Runs on SQLite + Postgres via migration 0066 (the ``studies_db_settings``
fixture is parametrised over both engines, so the schema changes — the
``optimisation_study`` table, the widened ``ck_job_exactly_one_home``,
and the two ``study_result`` columns — are exercised on each).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.errors import NotFoundError
from matofx.hierarchy.orm import Job
from matofx.studies.optimisation import (
    OptimisationConfigError,
    OptimisationStateError,
    OptimisationStudy,
    make_optimisation_service,
)
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.tools.descriptor import InputField, OutputField, ToolDescriptor
from matofx.tools.orm import Tool, ToolVersion
from tests.studies.conftest import Basics


def _descriptor_snapshot() -> dict[str, Any]:
    """A heat-sink descriptor snapshot (float/int/select inputs + 2 outputs)."""
    return ToolDescriptor(
        alias="heat_sink",
        name="Heat Sink",
        version="1.0.0",
        description="thermal study",
        main_script="static/main.py",
        inputs=[
            InputField(
                name="fin_height", label="Fin height", type="float", minimum=5.0, maximum=40.0
            ),
            InputField(name="fin_count", label="Fin count", type="integer", minimum=4, maximum=60),
            InputField(
                name="material", label="Material", type="select", options=["al", "cu", "steel"]
            ),
        ],
        outputs=[
            OutputField(name="mass", label="Mass", type="number"),
            OutputField(name="max_temperature", label="Max temperature", type="number"),
        ],
    ).model_dump()


@pytest.fixture
async def opt_tool(studies_engine: AsyncEngine, basics: Basics) -> int:
    """Seed a ``heat_sink`` tool whose version snapshot is a real descriptor.

    Returns the ``tool_version.id``; ``basics`` carries the user/project ids.
    """
    snapshot = _descriptor_snapshot()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias="heat_sink",
                name="Heat Sink",
                version="1.0.0",
                state="released",
                descriptor=snapshot,
                bundle_path="bundle/heat_sink",
                installed_by_user_id=basics.user_id,
            )
        )
        tv = ToolVersion(
            alias="heat_sink",
            version="1.0.0",
            descriptor_snapshot=snapshot,
            bundle_path="bundle/heat_sink",
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=basics.user_id,
            change_summary="seed",
            changes={},
        )
        session.add(tv)
        await session.flush()
        return int(tv.id)


def _config_steps() -> dict[str, Any]:
    """The non-target config a draft accrues before ``configure``."""
    return {
        "factors": [
            {"name": "fin_height", "type": "float", "min": 10.0, "max": 30.0},
            {"name": "fin_count", "type": "int", "min": 8, "max": 40},
        ],
        # The required ``material`` input isn't varied, so it's held constant (§3.5).
        "fixed_inputs": {"material": "al"},
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "constraints": [{"kind": "output", "response": "mass", "op": "<=", "value": 5.0}],
        "strategy": "bo",
        "run_mode": "real",
        "batch_size": 4,
        "concurrency_cap": 4,
        "seed": {"kind": "lhs", "n": 20},
        "stopping_criteria": [{"kind": "max_runs", "value": 100}],
    }


# --- lifecycle --------------------------------------------------------------


async def test_full_lifecycle_create_update_configure_archive(
    studies_engine: AsyncEngine, basics: Basics, opt_tool: int
) -> None:
    audit = AuditWriter(signing_key=None, key_id=None)
    service = make_optimisation_service(audit)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = await service.create_draft(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="Lightweighting",
            tool_alias="heat_sink",
            tool_version_id=opt_tool,
        )
        assert study.state == "draft"
        assert study.config_json["target"]["tool_alias"] == "heat_sink"

        await service.update_draft(
            session,
            actor_user_id=basics.user_id,
            study_id=study.id,
            config_patch=_config_steps(),
        )
        configured = await service.configure(
            session, actor_user_id=basics.user_id, study_id=study.id
        )
        assert configured.state == "running"
        assert configured.state_changed_at is not None

        archived = await service.archive(session, actor_user_id=basics.user_id, study_id=study.id)
        assert archived.archived_at is not None

        listed = await service.list_for_project(
            session, actor_user_id=basics.user_id, project_id=basics.project_id
        )
        assert study.id not in {s.id for s in listed}
        listed_all = await service.list_for_project(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            include_archived=True,
        )
        assert study.id in {s.id for s in listed_all}


async def test_create_draft_rejects_mismatched_tool_version(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    audit = AuditWriter(signing_key=None, key_id=None)
    service = make_optimisation_service(audit)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(OptimisationConfigError, match="belongs to"):
            await service.create_draft(
                session,
                actor_user_id=basics.user_id,
                project_id=basics.project_id,
                name="x",
                tool_alias="heat_sink",
                tool_version_id=basics.tool_version_id,  # the 'demo' version
            )


async def test_configure_invalid_config_rejected(
    studies_engine: AsyncEngine, basics: Basics, opt_tool: int
) -> None:
    """A draft with no factors/responses can't be configured."""
    audit = AuditWriter(signing_key=None, key_id=None)
    service = make_optimisation_service(audit)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = await service.create_draft(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="empty",
            tool_alias="heat_sink",
            tool_version_id=opt_tool,
        )
        with pytest.raises(OptimisationConfigError):
            await service.configure(session, actor_user_id=basics.user_id, study_id=study.id)
        # Still a draft — a failed configure doesn't advance state.
        refetched = await service.get(session, actor_user_id=basics.user_id, study_id=study.id)
        assert refetched.state == "draft"


async def test_update_after_configure_rejected(
    studies_engine: AsyncEngine, basics: Basics, opt_tool: int
) -> None:
    audit = AuditWriter(signing_key=None, key_id=None)
    service = make_optimisation_service(audit)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = await service.create_draft(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="x",
            tool_alias="heat_sink",
            tool_version_id=opt_tool,
        )
        await service.update_draft(
            session, actor_user_id=basics.user_id, study_id=study.id, config_patch=_config_steps()
        )
        await service.configure(session, actor_user_id=basics.user_id, study_id=study.id)
        with pytest.raises(OptimisationStateError, match="only a draft"):
            await service.update_draft(
                session,
                actor_user_id=basics.user_id,
                study_id=study.id,
                config_patch={"batch_size": 8},
            )


async def test_update_draft_cannot_change_target(
    studies_engine: AsyncEngine, basics: Basics, opt_tool: int
) -> None:
    audit = AuditWriter(signing_key=None, key_id=None)
    service = make_optimisation_service(audit)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = await service.create_draft(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="x",
            tool_alias="heat_sink",
            tool_version_id=opt_tool,
        )
        with pytest.raises(OptimisationConfigError, match="target"):
            await service.update_draft(
                session,
                actor_user_id=basics.user_id,
                study_id=study.id,
                config_patch={"target": {"kind": "tool"}},
            )


async def test_outsider_cannot_list(
    studies_engine: AsyncEngine, basics: Basics, outsider_user_id: int
) -> None:
    audit = AuditWriter(signing_key=None, key_id=None)
    service = make_optimisation_service(audit)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        # A zero-access caller is existence-hidden — the project ACL
        # raises NotFoundError, not AccessDeniedError (studies-engine §7).
        with pytest.raises(NotFoundError):
            await service.list_for_project(
                session, actor_user_id=outsider_user_id, project_id=basics.project_id
            )


async def test_get_missing_study_raises(studies_engine: AsyncEngine, basics: Basics) -> None:
    audit = AuditWriter(signing_key=None, key_id=None)
    service = make_optimisation_service(audit)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await service.get(session, actor_user_id=basics.user_id, study_id=999999)


# --- job home 1-of-3 CHECK (migration 0066) ---------------------------------


def _opt_study(b: Basics, **overrides: object) -> OptimisationStudy:
    kwargs: dict[str, object] = {
        "project_id": b.project_id,
        "name": "opt",
        "target_kind": "tool",
        "tool_alias": b.tool_alias,
        "tool_version_id": b.tool_version_id,
        "config_json": {"target": {"kind": "tool"}},
        "state": "running",
        "created_by_user_id": b.user_id,
    }
    kwargs.update(overrides)
    return OptimisationStudy(**kwargs)


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


async def test_job_optimisation_home_ok(studies_engine: AsyncEngine, basics: Basics) -> None:
    """A job homed solely under an optimisation study satisfies the 1-of-3 CHECK."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _opt_study(basics)
        session.add(study)
        await session.flush()
        session.add(_job(basics, optimisation_study_id=study.id))
        await session.flush()  # must not raise


async def test_two_optimisation_children_same_tool_ok(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """NULL-scenario optimisation children don't collide on the standalone index."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _opt_study(basics)
        session.add(study)
        await session.flush()
        session.add(_job(basics, optimisation_study_id=study.id, name="c1"))
        session.add(_job(basics, optimisation_study_id=study.id, name="c2"))
        await session.flush()  # must not raise


async def test_job_scenario_plus_optimisation_rejected(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = _opt_study(basics)
        session.add(study)
        await session.flush()
        session.add(_job(basics, scenario_id=basics.scenario_id, optimisation_study_id=study.id))
        try:
            await session.flush()
            raise AssertionError("expected ck_job_exactly_one_home to reject two homes")
        except IntegrityError:
            await session.rollback()


async def test_job_parametric_plus_optimisation_rejected(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        opt = _opt_study(basics)
        para = ParametricStudy(
            project_id=basics.project_id,
            name="p",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            mode="grid",
            factor_spec={},
            sampling_params={},
            run_cap=10,
            created_by_user_id=basics.user_id,
        )
        session.add_all([opt, para])
        await session.flush()
        session.add(_job(basics, parametric_study_id=para.id, optimisation_study_id=opt.id))
        try:
            await session.flush()
            raise AssertionError("expected ck_job_exactly_one_home to reject two homes")
        except IntegrityError:
            await session.rollback()


async def test_job_no_home_still_rejected(studies_engine: AsyncEngine, basics: Basics) -> None:
    """Regression: widening to 1-of-3 must still reject a homeless job."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(_job(basics))  # no scenario, no study of either kind
        try:
            await session.flush()
            raise AssertionError("expected ck_job_exactly_one_home to reject no home")
        except IntegrityError:
            await session.rollback()


# --- study_result optimisation columns (migration 0066) ---------------------


async def test_study_result_optimisation_columns_present(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """``evaluated_on_surrogate`` defaults false; ``feasible`` is nullable."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        opt = _opt_study(basics)
        session.add(opt)
        await session.flush()
        job = _job(basics, optimisation_study_id=opt.id)
        session.add(job)
        await session.flush()
        row = StudyResult(
            source_kind="optimisation",
            source_id=opt.id,
            job_id=job.id,
            scalars={"mass": 4.2},
            retained_files=[],
            feasible=True,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        assert row.evaluated_on_surrogate in (False, None)
        assert row.feasible is True


# --- continue / extend budget (§14) -----------------------------------------


async def _complete_study(
    factory: Any, basics: Basics, opt_tool: int, *, state: str = "complete"
) -> int:
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = OptimisationStudy(
            project_id=basics.project_id,
            name="continue me",
            target_kind="tool",
            tool_alias="heat_sink",
            tool_version_id=opt_tool,
            config_json={
                "target": {"kind": "tool", "tool_alias": "heat_sink", "tool_version_id": opt_tool},
                "stopping_criteria": [{"kind": "max_runs", "value": 40}],
            },
            state=state,
            stop_reason="budget_exhausted" if state == "complete" else None,
            created_by_user_id=basics.user_id,
        )
        session.add(study)
        await session.flush()
        return int(study.id)


async def test_extend_budget_continues_a_completed_study(
    studies_engine: AsyncEngine, basics: Basics, opt_tool: int
) -> None:
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    factory = make_session_factory(studies_engine)
    sid = await _complete_study(factory, basics, opt_tool)
    async with session_scope(factory) as session:
        result = await service.extend_budget(
            session, actor_user_id=basics.user_id, study_id=sid, additional_runs=15
        )
        assert result.state == "running"
        assert result.stop_reason is None
        budget = next(
            c["value"]
            for c in result.config_json["stopping_criteria"]
            if c["kind"] == "max_runs"
        )
        assert budget == 55  # 40 + 15


async def test_extend_budget_rejects_a_running_study(
    studies_engine: AsyncEngine, basics: Basics, opt_tool: int
) -> None:
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    factory = make_session_factory(studies_engine)
    sid = await _complete_study(factory, basics, opt_tool, state="running")
    async with session_scope(factory) as session:
        with pytest.raises(OptimisationStateError):
            await service.extend_budget(
                session, actor_user_id=basics.user_id, study_id=sid, additional_runs=10
            )


async def test_extend_budget_rejects_non_positive(
    studies_engine: AsyncEngine, basics: Basics, opt_tool: int
) -> None:
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    factory = make_session_factory(studies_engine)
    sid = await _complete_study(factory, basics, opt_tool)
    async with session_scope(factory) as session:
        with pytest.raises(OptimisationConfigError):
            await service.extend_budget(
                session, actor_user_id=basics.user_id, study_id=sid, additional_runs=0
            )

"""Slice-4 execution wiring tests.

Covers the JobService scenario->study project-resolution generalisation
(study children are now fetchable through JobService where they used
to 404) and StudyService.submit / retry_child orchestration.

``queue_for_runner`` itself (tool/bundle/storage validation + working-
dir staging) is exercised by the large hierarchy/api/runner job suites;
here it's faked so the tests pin StudyService's own contract — "fan a
child per point, delegate each to queue_for_runner, roll the study to
running" — at the right boundary.
"""

from __future__ import annotations

import pytest

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.errors import NotFoundError
from matofx.hierarchy.orm import Job
from matofx.hierarchy.service import make_job_service
from matofx.studies import StudyService, StudyStateError
from tests.studies.conftest import Basics


def _svc() -> StudyService:
    from matofx.audit import AuditWriter  # noqa: PLC0415

    return StudyService(audit=AuditWriter(signing_key=None, key_id=None), environment="test")


def _list_spec(a: list[float], b: list[float]) -> dict[str, object]:
    return {
        "a": {"role": "vary", "type": "numeric", "vary": {"kind": "list", "values": a}},
        "b": {"role": "vary", "type": "numeric", "vary": {"kind": "list", "values": b}},
    }


# `fake_queue` (faked JobService.queue_for_runner) lives in conftest.py
# — slice-4 submit/retry tests here and the slice-3 submit-success
# tests in test_study_service.py both use it.


# --- JobService project-resolution generalisation -------------------------


async def test_jobservice_get_resolves_study_child(
    studies_engine, basics: Basics, valid_tool: str, outsider_user_id: int
) -> None:
    """A scenario_id-NULL study child is fetchable via JobService (it
    used to 404 because the project was resolved only via scenario)."""
    factory = make_session_factory(studies_engine)
    from matofx.studies.orm import ParametricStudy  # noqa: PLC0415

    async with session_scope(factory) as session:
        study = ParametricStudy(
            project_id=basics.project_id,
            name="s",
            tool_alias=valid_tool,
            tool_version_id=basics.tool_version_id,
            mode="list",
            factor_spec={},
            sampling_params={},
            run_cap=200,
            state="running",
            created_by_user_id=basics.user_id,
        )
        session.add(study)
        await session.flush()
        child = Job(
            scenario_id=None,
            parametric_study_id=study.id,
            name="child",
            tool_alias=valid_tool,
            tool_version_id=basics.tool_version_id,
            state="created",
            inputs={},
            created_by_user_id=basics.user_id,
        )
        session.add(child)
        await session.flush()
        child_id = child.id

    job_service = make_job_service(_svc().audit, environment="test")
    async with session_scope(factory) as session:
        got = await job_service.get(session, job_id=child_id, actor_user_id=basics.user_id)
        assert got.id == child_id
        assert got.parametric_study_id is not None
        with pytest.raises(NotFoundError):  # outsider: NONE -> existence-hiding
            await job_service.get(session, job_id=child_id, actor_user_id=outsider_user_id)


# --- submit enqueues every child + rolls the study to running -------------


async def test_submit_enqueues_children(
    studies_engine, basics: Basics, valid_tool: str, fake_queue: list[int]
) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    async with session_scope(factory) as session:
        study = await svc.create(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="sweep",
            tool_alias=valid_tool,
            mode="list",
            factor_spec=_list_spec([40, 60], [60, 40]),
        )
        sid = study.id
    async with session_scope(factory) as session:
        study = await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        assert study.state == "running"
        assert study.generated_count == 2
        children = await svc.list_children(session, actor_user_id=basics.user_id, study_id=sid)
    assert sorted(fake_queue) == sorted(c.id for c in children)
    assert all(c.state == "queued" for c in children)


# --- retry_child ----------------------------------------------------------


async def test_retry_child_requeues_failed_child(
    studies_engine, basics: Basics, valid_tool: str, fake_queue: list[int]
) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    async with session_scope(factory) as session:
        study = await svc.create(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="sweep",
            tool_alias=valid_tool,
            mode="list",
            factor_spec=_list_spec([40], [60]),
        )
        sid = study.id
    async with session_scope(factory) as session:
        await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        child = (await svc._list_children(session, sid))[0]
        child.state = "failed"
        child.completed_at = child.started_at = child.created_at
        await session.flush()
        child_id = child.id
    fake_queue.clear()
    async with session_scope(factory) as session:
        retried = await svc.retry_child(
            session, actor_user_id=basics.user_id, study_id=sid, job_id=child_id
        )
        assert retried.state == "queued"  # faked queue_for_runner flipped it
        study = await svc.get(session, actor_user_id=basics.user_id, study_id=sid)
        assert study.state == "running"
    assert fake_queue == [child_id]


async def test_retry_child_rejects_non_failed(
    studies_engine,
    basics: Basics,
    valid_tool: str,
    fake_queue: list[int],  # noqa: ARG001 — needed so submit()'s enqueue is faked
) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    async with session_scope(factory) as session:
        study = await svc.create(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="sweep",
            tool_alias=valid_tool,
            mode="list",
            factor_spec=_list_spec([40], [60]),
        )
        sid = study.id
    async with session_scope(factory) as session:
        await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        child_id = (await svc._list_children(session, sid))[0].id
    async with session_scope(factory) as session:
        with pytest.raises(StudyStateError, match="failed run can be retried"):
            await svc.retry_child(
                session, actor_user_id=basics.user_id, study_id=sid, job_id=child_id
            )
        await session.rollback()

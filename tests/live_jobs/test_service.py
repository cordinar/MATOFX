"""Unit tests for :class:`matofx.live_jobs.LiveJobsService`.

Exercises the install-wide in-flight listing (the three row kinds, the
cancellable flag, and the kind / submitter / window filters) and the
bulk-cancel path (per-item success, skip-with-reason, run / study
cascade, and the project-access bypass).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest
from sqlalchemy import select

from matofx.audit import AuditWriter
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.live_jobs import make_live_jobs_service
from matofx.studies.orm import ParametricStudy
from matofx.tools.orm import ToolVersion
from matofx.users import make_password_hasher, make_user_service
from matofx.workflows.orm import Workflow, WorkflowRun

pytestmark = pytest.mark.asyncio


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


async def _tool_version_id(factory: SessionFactory, alias: str) -> int:
    async with session_scope(factory) as session:
        row = (
            (
                await session.execute(
                    select(ToolVersion)
                    .where(ToolVersion.alias == alias)
                    .order_by(ToolVersion.id.desc())
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        return int(row.id)


async def _make_user(
    factory: SessionFactory,
    settings: Any,
    audit: AuditWriter,
    *,
    username: str,
    role: str,
) -> int:
    svc = make_user_service(settings.auth, audit, hasher=make_password_hasher(settings.auth))
    async with session_scope(factory) as session:
        user = await svc.create_user(
            session,
            username=username,
            email=f"{username}@example.com",
            password="password12345",
            full_name=f"{username.title()} User",
            role=role,
        )
        return int(user.id)


async def _new_project_scenario(
    factory: SessionFactory, *, owner_id: int, suffix: str
) -> tuple[int, int]:
    async with session_scope(factory) as session:
        project = Project(name=f"LJ project {suffix}", creator_user_id=owner_id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name=f"LJ scenario {suffix}")
        session.add(scenario)
        await session.flush()
        return int(project.id), int(scenario.id)


async def _add_standalone_job(
    factory: SessionFactory,
    *,
    scenario_id: int,
    tool_alias: str,
    tool_version_id: int,
    owner_id: int,
    state: str,
    created_at: _dt.datetime | None = None,
    name: str = "standalone",
) -> int:
    async with session_scope(factory) as session:
        job = Job(
            scenario_id=scenario_id,
            name=name,
            tool_alias=tool_alias,
            tool_version_id=tool_version_id,
            state=state,
            inputs={},
            created_by_user_id=owner_id,
            created_at=created_at or _utcnow(),
        )
        session.add(job)
        await session.flush()
        return int(job.id)


async def _add_workflow_run_with_child(
    factory: SessionFactory,
    *,
    scenario_id: int,
    tool_alias: str,
    tool_version_id: int,
    owner_id: int,
    child_state: str,
) -> tuple[int, int]:
    async with session_scope(factory) as session:
        wf = Workflow(
            alias="wf-demo",
            name="Demo Workflow",
            version="1.0",
            kind="admin",
            state="released",
            descriptor={"description": "demo"},
            installed_by_user_id=owner_id,
        )
        session.add(wf)
        await session.flush()
        run = WorkflowRun(
            workflow_id=wf.id,
            workflow_version_snapshot="1.0",
            scenario_id=scenario_id,
            name="Demo run",
            submitted_by_user_id=owner_id,
        )
        session.add(run)
        await session.flush()
        child = Job(
            scenario_id=scenario_id,
            workflow_run_id=run.id,
            workflow_position=1,
            name="wf-child",
            tool_alias=tool_alias,
            tool_version_id=tool_version_id,
            state=child_state,
            inputs={},
            created_by_user_id=owner_id,
        )
        session.add(child)
        await session.flush()
        return int(run.id), int(child.id)


async def _add_study_with_child(
    factory: SessionFactory,
    *,
    project_id: int,
    tool_alias: str,
    tool_version_id: int,
    owner_id: int,
    study_state: str,
    child_state: str = "running",
) -> tuple[int, int]:
    async with session_scope(factory) as session:
        study = ParametricStudy(
            project_id=project_id,
            name="Demo Study",
            tool_alias=tool_alias,
            tool_version_id=tool_version_id,
            mode="grid",
            factor_spec={},
            sampling_params={},
            run_cap=100,
            state=study_state,
            created_by_user_id=owner_id,
        )
        session.add(study)
        await session.flush()
        child = Job(
            parametric_study_id=study.id,
            name="study-child",
            tool_alias=tool_alias,
            tool_version_id=tool_version_id,
            state=child_state,
            inputs={},
            created_by_user_id=owner_id,
        )
        session.add(child)
        await session.flush()
        return int(study.id), int(child.id)


async def test_list_standalone_jobs_by_state_and_cancellable(
    api_factory: SessionFactory, api_audit: AuditWriter, seeded: dict[str, Any]
) -> None:
    """Only in-flight standalone jobs appear; cancellable tracks the state."""
    alias = seeded["tool_alias"]
    tv = await _tool_version_id(api_factory, alias)
    owner = seeded["user_id"]
    _proj, scen_q = await _new_project_scenario(api_factory, owner_id=owner, suffix="q")
    _proj2, scen_r = await _new_project_scenario(api_factory, owner_id=owner, suffix="r")
    _proj3, scen_p = await _new_project_scenario(api_factory, owner_id=owner, suffix="p")
    _proj4, scen_c = await _new_project_scenario(api_factory, owner_id=owner, suffix="c")
    qid = await _add_standalone_job(
        api_factory,
        scenario_id=scen_q,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        state="queued",
    )
    rid = await _add_standalone_job(
        api_factory,
        scenario_id=scen_r,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        state="running",
    )
    pid = await _add_standalone_job(
        api_factory,
        scenario_id=scen_p,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        state="pending_approval",
    )
    await _add_standalone_job(
        api_factory,
        scenario_id=scen_c,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        state="complete",
    )

    service = make_live_jobs_service(api_audit)
    async with session_scope(api_factory) as session:
        view = await service.list_inflight(session)

    by_id = {(i.kind, i.id): i for i in view.items}
    assert ("job", qid) in by_id
    assert ("job", rid) in by_id
    assert ("job", pid) in by_id  # approval-waiting shown for visibility
    assert all(i.state != "complete" for i in view.items)  # terminal excluded
    assert by_id[("job", qid)].cancellable is True
    assert by_id[("job", rid)].cancellable is True
    assert by_id[("job", pid)].cancellable is False  # at an approval gate
    # Names + hierarchy resolve.
    assert by_id[("job", qid)].name  # tool display name, not blank
    assert by_id[("job", qid)].scenario_name == "LJ scenario q"
    assert by_id[("job", qid)].project_name == "LJ project q"
    # The seeded super-user is offered as a submitter filter option.
    assert owner in {s.user_id for s in view.submitters}


async def test_workflow_and_study_children_are_not_listed_as_jobs(
    api_factory: SessionFactory, api_audit: AuditWriter, seeded: dict[str, Any]
) -> None:
    """A run / study is one row each; their child jobs are not separate rows."""
    alias = seeded["tool_alias"]
    tv = await _tool_version_id(api_factory, alias)
    owner = seeded["user_id"]
    _proj, scen = await _new_project_scenario(api_factory, owner_id=owner, suffix="wf")
    proj_s, _scen_s = await _new_project_scenario(api_factory, owner_id=owner, suffix="st")
    run_id, child_job_id = await _add_workflow_run_with_child(
        api_factory,
        scenario_id=scen,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        child_state="running",
    )
    study_id, study_child_id = await _add_study_with_child(
        api_factory,
        project_id=proj_s,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        study_state="running",
    )

    service = make_live_jobs_service(api_audit)
    async with session_scope(api_factory) as session:
        view = await service.list_inflight(session)

    kinds_ids = {(i.kind, i.id) for i in view.items}
    assert ("workflow", run_id) in kinds_ids
    assert ("study", study_id) in kinds_ids
    # No child job leaks in as its own "job" row.
    assert ("job", child_job_id) not in kinds_ids
    assert ("job", study_child_id) not in kinds_ids
    wf_item = next(i for i in view.items if i.kind == "workflow")
    assert wf_item.name == "Demo Workflow"
    assert wf_item.child_count == 1
    assert wf_item.cancellable is True
    study_item = next(i for i in view.items if i.kind == "study")
    assert study_item.name == "Demo Study"
    assert study_item.scenario_name is None  # studies are project-level
    assert study_item.project_name == "LJ project st"


async def test_filters_kind_submitter_and_window(
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    api_settings: Any,
    seeded: dict[str, Any],
) -> None:
    """Kind, submitter, and submitted-window filters each narrow the list."""
    alias = seeded["tool_alias"]
    tv = await _tool_version_id(api_factory, alias)
    root = seeded["user_id"]
    other = await _make_user(
        api_factory, api_settings, api_audit, username="bea", role="regular_user"
    )
    _p1, scen_root = await _new_project_scenario(api_factory, owner_id=root, suffix="root")
    _p2, scen_other = await _new_project_scenario(api_factory, owner_id=root, suffix="other")
    old = _utcnow() - _dt.timedelta(days=40)
    root_job = await _add_standalone_job(
        api_factory,
        scenario_id=scen_root,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=root,
        state="running",
    )
    other_job = await _add_standalone_job(
        api_factory,
        scenario_id=scen_other,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=other,
        state="running",
        created_at=old,
    )

    service = make_live_jobs_service(api_audit)
    async with session_scope(api_factory) as session:
        # Submitter filter → only the other user's job.
        only_other = await service.list_inflight(session, submitter_user_ids=(other,))
        assert {(i.kind, i.id) for i in only_other.items} == {("job", other_job)}
        # Kind filter for studies → nothing here.
        only_studies = await service.list_inflight(session, kinds=("study",))
        assert only_studies.items == []
        # Window excludes the 40-day-old job.
        recent = await service.list_inflight(
            session, since=_utcnow() - _dt.timedelta(days=7), until=_utcnow()
        )
        recent_ids = {(i.kind, i.id) for i in recent.items}
        assert ("job", root_job) in recent_ids
        assert ("job", other_job) not in recent_ids


async def test_cancel_many_jobs_success_and_skip(
    api_factory: SessionFactory, api_audit: AuditWriter, seeded: dict[str, Any]
) -> None:
    """Queued/running jobs cancel; an approval-gate job is skipped with a reason."""
    alias = seeded["tool_alias"]
    tv = await _tool_version_id(api_factory, alias)
    owner = seeded["user_id"]
    _p1, scen_run = await _new_project_scenario(api_factory, owner_id=owner, suffix="run")
    _p2, scen_hold = await _new_project_scenario(api_factory, owner_id=owner, suffix="hold")
    running = await _add_standalone_job(
        api_factory,
        scenario_id=scen_run,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        state="running",
    )
    pending = await _add_standalone_job(
        api_factory,
        scenario_id=scen_hold,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        state="pending_approval",
    )

    service = make_live_jobs_service(api_audit)
    async with session_scope(api_factory) as session:
        result = await service.cancel_many(session, actor_user_id=owner, job_ids=(running, pending))

    outcomes = {o.id: o for o in result.outcomes}
    assert result.cancelled_count == 1
    assert outcomes[running].cancelled is True
    assert outcomes[pending].cancelled is False
    assert "Approvals" in outcomes[pending].reason

    async with session_scope(api_factory) as session:
        running_row = await session.get(Job, running)
        pending_row = await session.get(Job, pending)
        assert running_row is not None
        assert running_row.cancel_requested_at is not None
        assert pending_row is not None
        assert pending_row.cancel_requested_at is None


async def test_cancel_run_and_study_cascade(
    api_factory: SessionFactory, api_audit: AuditWriter, seeded: dict[str, Any]
) -> None:
    """Cancelling a run / study stamps the parent and its in-flight children."""
    alias = seeded["tool_alias"]
    tv = await _tool_version_id(api_factory, alias)
    owner = seeded["user_id"]
    _p, scen = await _new_project_scenario(api_factory, owner_id=owner, suffix="wfc")
    proj_s, _ = await _new_project_scenario(api_factory, owner_id=owner, suffix="stc")
    run_id, child_id = await _add_workflow_run_with_child(
        api_factory,
        scenario_id=scen,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        child_state="running",
    )
    study_id, study_child = await _add_study_with_child(
        api_factory,
        project_id=proj_s,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        study_state="running",
    )

    service = make_live_jobs_service(api_audit)
    async with session_scope(api_factory) as session:
        result = await service.cancel_many(
            session, actor_user_id=owner, run_ids=(run_id,), study_ids=(study_id,)
        )
    assert result.cancelled_count == 2
    assert all(o.cancelled for o in result.outcomes)

    async with session_scope(api_factory) as session:
        run = await session.get(WorkflowRun, run_id)
        run_child = await session.get(Job, child_id)
        study = await session.get(ParametricStudy, study_id)
        study_child_row = await session.get(Job, study_child)
        assert run is not None
        assert run.cancel_requested_at is not None
        assert run_child is not None
        assert run_child.cancel_requested_at is not None
        assert study is not None
        assert study.state == "cancelled"
        assert study_child_row is not None
        assert study_child_row.cancel_requested_at is not None


async def test_cancel_bypasses_project_access(
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    api_settings: Any,
    seeded: dict[str, Any],
) -> None:
    """An actor with no ACL on the project still cancels (router gates the perm)."""
    alias = seeded["tool_alias"]
    tv = await _tool_version_id(api_factory, alias)
    owner = seeded["user_id"]
    outsider = await _make_user(
        api_factory, api_settings, api_audit, username="otto", role="regular_user"
    )
    _p, scen = await _new_project_scenario(api_factory, owner_id=owner, suffix="acl")
    job_id = await _add_standalone_job(
        api_factory,
        scenario_id=scen,
        tool_alias=alias,
        tool_version_id=tv,
        owner_id=owner,
        state="running",
    )

    service = make_live_jobs_service(api_audit)
    async with session_scope(api_factory) as session:
        result = await service.cancel_many(session, actor_user_id=outsider, job_ids=(job_id,))
    assert result.cancelled_count == 1

    async with session_scope(api_factory) as session:
        row = await session.get(Job, job_id)
        assert row is not None
        assert row.cancel_requested_at is not None


async def test_cancel_unknown_id_reports_failure(
    api_factory: SessionFactory, api_audit: AuditWriter, seeded: dict[str, Any]
) -> None:
    """An unknown id comes back as a non-fatal per-item failure."""
    service = make_live_jobs_service(api_audit)
    async with session_scope(api_factory) as session:
        result = await service.cancel_many(
            session, actor_user_id=seeded["user_id"], job_ids=(999_999,)
        )
    assert result.cancelled_count == 0
    assert result.failures[0].id == 999_999
    assert "not found" in result.failures[0].reason.lower()

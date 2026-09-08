"""Tests for JobService."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import (
    InvalidJobStateError,
    JobService,
    NotFoundError,
    ProjectService,
    ScenarioService,
)


async def _make_scenario(
    factory,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    owner_id: int,
) -> int:
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=owner_id, name="P")
        scenario = await scenario_service.create(
            session, actor_user_id=owner_id, project_id=project.id, name="S"
        )
        return scenario.id


@pytest.mark.asyncio
async def test_create_and_get_job(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="run-1",
            tool_alias="demo",
            inputs={"foo": "bar"},
        )

    async with session_scope(factory) as session:
        loaded = await job_service.get(session, job_id=job.id, actor_user_id=alice_id)
    assert loaded.name == "run-1"
    assert loaded.state == "created"
    assert loaded.tool_alias == "demo"
    assert loaded.inputs == {"foo": "bar"}


@pytest.mark.asyncio
async def test_list_jobs_in_scenario(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    # One scenario can hold at most one job per tool (locked
    # 2026-05-08), so to list multiple jobs in a scenario the test
    # needs distinct tool aliases. We seed two extras alongside
    # ``demo`` to land three rows in one scenario without tripping
    # the unique constraint.
    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)

    # Seed two more tools so the scenario can hold three jobs. Tool
    # install requires admin / super_user; we use the seeded admin.
    from matofx.audit import AuditWriter  # noqa: PLC0415
    from matofx.tools.descriptor import InputField, ToolDescriptor  # noqa: PLC0415
    from matofx.tools.service import make_tool_service  # noqa: PLC0415

    extra_aliases = ("demo_b", "demo_c")
    async with session_scope(factory) as session:
        tool_service = make_tool_service(AuditWriter(signing_key=None, key_id=None))
        for alias in extra_aliases:
            await tool_service.install(
                session,
                actor_user_id=admin_id,
                descriptor=ToolDescriptor(
                    alias=alias,
                    name=alias,
                    version="1.0.0",
                    description="x",
                    main_script="m.py",
                    inputs=[InputField(name="x", label="X", type="string")],
                ),
                bundle_path=f"bundle/{alias}",
            )
            await tool_service.promote_state(
                session, actor_user_id=admin_id, alias=alias, new_state="test"
            )
            await tool_service.promote_state(
                session, actor_user_id=admin_id, alias=alias, new_state="released"
            )

    aliases = ("demo", *extra_aliases)
    async with session_scope(factory) as session:
        for i, alias in enumerate(aliases):
            await job_service.create(
                session,
                actor_user_id=alice_id,
                scenario_id=scenario_id,
                name=f"j{i}",
                tool_alias=alias,
            )
    async with session_scope(factory) as session:
        jobs = await job_service.list_for_scenario(
            session, scenario_id=scenario_id, actor_user_id=alice_id
        )
    assert {j.name for j in jobs} == {"j0", "j1", "j2"}


@pytest.mark.asyncio
async def test_surrogate_tool_job_carries_model_version_tag(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- the 'demo' tool is the no-pin control
) -> None:
    """A synthesised surrogate-tool's job inherits the pinned model version (§7.4).

    The pin lives in the tool version's ``descriptor_snapshot`` (set by
    ``synthesise_descriptor``); ``JobService.create`` reads it through and
    tags ``Job.surrogate_model_version_id`` for lineage. A hand-authored
    tool leaves the tag ``None``.
    """
    import datetime as _dt  # noqa: PLC0415

    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)

    # Seed a surrogate-tool: its descriptor_snapshot pins version 99.
    descriptor = {"alias": "surrogate_demo_5", "surrogate_model_version_id": 99}
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias="surrogate_demo_5",
                name="Surrogate of demo",
                version="1.0",
                state="released",
                descriptor=descriptor,
                bundle_path="bundle/surrogate_demo_5",
                installed_by_user_id=admin_id,
            )
        )
        session.add(
            ToolVersion(
                alias="surrogate_demo_5",
                version="1.0",
                descriptor_snapshot=descriptor,
                bundle_path="bundle/surrogate_demo_5",
                created_at=_dt.datetime.now(tz=_dt.UTC),
                created_by_user_id=admin_id,
                change_summary="seed",
                changes={},
            )
        )

    async with session_scope(factory) as session:
        surrogate_job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="pred",
            tool_alias="surrogate_demo_5",
        )
        control_job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="real",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        loaded = await job_service.get(session, job_id=surrogate_job.id, actor_user_id=alice_id)
        control = await job_service.get(session, job_id=control_job.id, actor_user_id=alice_id)
    assert loaded.surrogate_model_version_id == 99
    assert control.surrogate_model_version_id is None


@pytest.mark.asyncio
async def test_other_user_cannot_create_job(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await job_service.create(
                session,
                actor_user_id=bob_id,
                scenario_id=scenario_id,
                name="x",
                tool_alias="demo",
            )


@pytest.mark.asyncio
async def test_state_transitions_set_timestamps(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        running = await job_service._transition_state(
            session, job_id=job.id, actor_user_id=alice_id, new_state="running"
        )
    assert running.state == "running"
    assert running.started_at is not None
    assert running.completed_at is None

    async with session_scope(factory) as session:
        done = await job_service._transition_state(
            session, job_id=job.id, actor_user_id=alice_id, new_state="complete"
        )
    assert done.state == "complete"
    assert done.completed_at is not None


@pytest.mark.asyncio
async def test_unknown_state_rejected(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )
    async with session_scope(factory) as session:
        with pytest.raises(InvalidJobStateError):
            await job_service._transition_state(
                session, job_id=job.id, actor_user_id=alice_id, new_state="exploded"
            )


@pytest.mark.asyncio
async def test_cascading_delete_via_project(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    """Direct DELETE on project removes its scenarios and jobs (FK cascades)."""
    from sqlalchemy import delete  # noqa: PLC0415

    from matofx.hierarchy import Job, Project, Scenario  # noqa: PLC0415

    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        scenario = await scenario_service.create(
            session, actor_user_id=alice_id, project_id=project.id, name="S"
        )
        await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario.id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        await session.execute(delete(Project).where(Project.id == project.id))

    async with session_scope(factory) as session:
        from sqlalchemy import select  # noqa: PLC0415

        scenarios = (await session.execute(select(Scenario))).scalars().all()
        jobs = (await session.execute(select(Job))).scalars().all()
        assert len(scenarios) == 0
        assert len(jobs) == 0


# ---- search_by_inputs / list_distinct_input_values ------------------------


async def _seed_completed_jobs(
    factory,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    *,
    owner_id: int,
    project_name: str,
    inputs_per_job: list[dict],
) -> list[int]:
    """Helper: one project, N scenarios, one ``demo`` job each, all → complete.

    The unique-tool-per-scenario constraint means every job needs its
    own scenario; we sidestep the multi-tool seeding dance by reusing
    the ``demo`` alias across distinct scenarios.
    """
    async with session_scope(factory) as session:
        project = await project_service.create(
            session, actor_user_id=owner_id, name=project_name
        )
        proj_id = project.id

    job_ids: list[int] = []
    for i, inputs in enumerate(inputs_per_job):
        async with session_scope(factory) as session:
            scenario = await scenario_service.create(
                session, actor_user_id=owner_id, project_id=proj_id, name=f"s{i}"
            )
            sid = scenario.id
        async with session_scope(factory) as session:
            job = await job_service.create(
                session,
                actor_user_id=owner_id,
                scenario_id=sid,
                name=f"j{i}",
                tool_alias="demo",
                inputs=inputs,
            )
            jid = job.id
        async with session_scope(factory) as session:
            await job_service._transition_state(
                session, job_id=jid, actor_user_id=owner_id, new_state="running"
            )
        async with session_scope(factory) as session:
            await job_service._transition_state(
                session, job_id=jid, actor_user_id=owner_id, new_state="complete"
            )
        job_ids.append(jid)
    return job_ids


@pytest.mark.asyncio
async def test_search_by_inputs_no_filters_returns_all_complete(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    await _seed_completed_jobs(
        factory,
        project_service,
        scenario_service,
        job_service,
        owner_id=alice_id,
        project_name="P",
        inputs_per_job=[
            {"material": "Steel", "temperature": 25.0},
            {"material": "Aluminium", "temperature": 100.0},
            {"material": "Steel", "temperature": 30.0},
        ],
    )

    async with session_scope(factory) as session:
        results = await job_service.search_by_inputs(
            session, actor_user_id=alice_id, tool_alias="demo"
        )
    assert len(results) == 3


@pytest.mark.asyncio
async def test_search_by_inputs_equality_filter(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    await _seed_completed_jobs(
        factory,
        project_service,
        scenario_service,
        job_service,
        owner_id=alice_id,
        project_name="P",
        inputs_per_job=[
            {"material": "Steel"},
            {"material": "Aluminium"},
            {"material": "Steel"},
        ],
    )

    async with session_scope(factory) as session:
        results = await job_service.search_by_inputs(
            session,
            actor_user_id=alice_id,
            tool_alias="demo",
            equality_filters={"material": "Steel"},
        )
    assert {tuple(sorted(r.inputs.items())) for r in results} == {
        (("material", "Steel"),),
        (("material", "Steel"),),
    }
    assert len(results) == 2


@pytest.mark.asyncio
async def test_search_by_inputs_numeric_tolerance(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    await _seed_completed_jobs(
        factory,
        project_service,
        scenario_service,
        job_service,
        owner_id=alice_id,
        project_name="P",
        inputs_per_job=[
            {"temperature": 80.0},  # +/-25% of 100 ⇒ [75, 125] — IN
            {"temperature": 70.0},  # +/-25% of 100 ⇒ [75, 125] — OUT
            {"temperature": 125.0},  # boundary IN
            {"temperature": 126.0},  # boundary OUT
        ],
    )

    async with session_scope(factory) as session:
        results = await job_service.search_by_inputs(
            session,
            actor_user_id=alice_id,
            tool_alias="demo",
            numeric_filters={"temperature": 100.0},
            numeric_tolerance_pct=25.0,
        )
    found = sorted(r.inputs["temperature"] for r in results)
    assert found == [80.0, 125.0]


@pytest.mark.asyncio
async def test_search_by_inputs_combined_equality_and_numeric(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    await _seed_completed_jobs(
        factory,
        project_service,
        scenario_service,
        job_service,
        owner_id=alice_id,
        project_name="P",
        inputs_per_job=[
            {"material": "Steel", "temperature": 95.0},  # both match
            {"material": "Steel", "temperature": 200.0},  # mat OK, temp out
            {"material": "Aluminium", "temperature": 100.0},  # mat off
        ],
    )

    async with session_scope(factory) as session:
        results = await job_service.search_by_inputs(
            session,
            actor_user_id=alice_id,
            tool_alias="demo",
            equality_filters={"material": "Steel"},
            numeric_filters={"temperature": 100.0},
            numeric_tolerance_pct=25.0,
        )
    assert len(results) == 1
    assert results[0].inputs == {"material": "Steel", "temperature": 95.0}


@pytest.mark.asyncio
async def test_search_by_inputs_excludes_non_complete(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
) -> None:
    """A draft + queued + running job in the mix; only complete returns."""
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        proj_id = project.id

    # Scenario A: complete job (will match).
    async with session_scope(factory) as session:
        s = await scenario_service.create(
            session, actor_user_id=alice_id, project_id=proj_id, name="sA"
        )
        sa = s.id
    async with session_scope(factory) as session:
        ja = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=sa,
            name="ja",
            tool_alias="demo",
            inputs={"k": "v"},
        )
        ja_id = ja.id
    async with session_scope(factory) as session:
        await job_service._transition_state(
            session, job_id=ja_id, actor_user_id=alice_id, new_state="running"
        )
    async with session_scope(factory) as session:
        await job_service._transition_state(
            session, job_id=ja_id, actor_user_id=alice_id, new_state="complete"
        )

    # Scenario B: a draft (state=created) job that should NOT show.
    async with session_scope(factory) as session:
        s = await scenario_service.create(
            session, actor_user_id=alice_id, project_id=proj_id, name="sB"
        )
        sb = s.id
    async with session_scope(factory) as session:
        await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=sb,
            name="jb",
            tool_alias="demo",
            inputs={"k": "v"},
        )

    async with session_scope(factory) as session:
        results = await job_service.search_by_inputs(
            session, actor_user_id=alice_id, tool_alias="demo"
        )
    assert [r.id for r in results] == [ja_id]


@pytest.mark.asyncio
async def test_search_by_inputs_respects_acl(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
) -> None:
    """Bob (no access) sees nothing of alice's jobs."""
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    await _seed_completed_jobs(
        factory,
        project_service,
        scenario_service,
        job_service,
        owner_id=alice_id,
        project_name="alice-P",
        inputs_per_job=[{"k": "v"}, {"k": "w"}],
    )
    async with session_scope(factory) as session:
        bob_results = await job_service.search_by_inputs(
            session, actor_user_id=bob_id, tool_alias="demo"
        )
    assert bob_results == []


@pytest.mark.asyncio
async def test_search_by_inputs_zero_target_collapses_to_exact(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    await _seed_completed_jobs(
        factory,
        project_service,
        scenario_service,
        job_service,
        owner_id=alice_id,
        project_name="P",
        inputs_per_job=[{"x": 0.0}, {"x": 0.001}, {"x": -0.0}],
    )
    async with session_scope(factory) as session:
        results = await job_service.search_by_inputs(
            session,
            actor_user_id=alice_id,
            tool_alias="demo",
            numeric_filters={"x": 0.0},
            numeric_tolerance_pct=25.0,
        )
    found = sorted(r.inputs["x"] for r in results)
    assert found == [-0.0, 0.0]


@pytest.mark.asyncio
async def test_search_by_inputs_non_numeric_value_no_match(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
) -> None:
    """A numeric filter on a string value yields no match (type-guard)."""
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    await _seed_completed_jobs(
        factory,
        project_service,
        scenario_service,
        job_service,
        owner_id=alice_id,
        project_name="P",
        inputs_per_job=[{"x": "100"}, {"x": 100.0}],
    )
    async with session_scope(factory) as session:
        results = await job_service.search_by_inputs(
            session,
            actor_user_id=alice_id,
            tool_alias="demo",
            numeric_filters={"x": 100.0},
            numeric_tolerance_pct=25.0,
        )
    assert len(results) == 1
    assert results[0].inputs["x"] == 100.0


@pytest.mark.asyncio
async def test_list_distinct_input_values_for_file_field(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
) -> None:
    """File-input dropdown: distinct paths from Job.inputs[<file_key>]."""
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    await _seed_completed_jobs(
        factory,
        project_service,
        scenario_service,
        job_service,
        owner_id=alice_id,
        project_name="P",
        inputs_per_job=[
            {"geometry": "/share/a.step"},
            {"geometry": "/share/b.step"},
            {"geometry": "/share/a.step"},  # dup
            {"geometry": ""},  # skip
            {"other": 1},  # missing field, skip
        ],
    )
    async with session_scope(factory) as session:
        values = await job_service.list_distinct_input_values(
            session, actor_user_id=alice_id, tool_alias="demo", field="geometry"
        )
    assert sorted(values) == ["/share/a.step", "/share/b.step"]

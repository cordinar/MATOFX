"""Workflow-target optimisation runtime (ADR-0009 §3.4, build Phase 11).

The candidate runner for a workflow target (:class:`WorkflowRunRunner`)
drives real headless runs, which need the runner daemon's cascade — too
heavy for a unit test. So these cover the two seams that make the
workflow target work in the outer loop:

- :meth:`OptimisationRuntime._select_runner` picks the workflow runner
  for a ``workflow``-target config (no DB).
- ``run_study`` loads a ``workflow``-target study (the ``_load_runnable``
  workflow branch, member-qualified factor names) and drives it to a
  terminal state with a fake runner injected in place of the real one.
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job, Scenario
from matofx.studies.optimisation import OptimisationStudy, make_optimisation_service
from matofx.studies.optimisation.runtime import (
    Evaluation,
    OptimisationRuntime,
    StudyRef,
    WorkflowRunRunner,
    _candidate_open_inputs,
    _split_member_inputs,
)
from matofx.tools.orm import Tool, ToolVersion
from matofx.workflows.orm import Workflow, WorkflowMember, WorkflowRun
from tests.studies.conftest import Basics


def test_split_member_inputs_groups_by_member() -> None:
    # Held-constant inputs keyed member.input → grouped per member (§3.5).
    fixed = {"mat.coolant": "42", "mat.note": "x", "heat.mesh_seed": 3}
    assert _split_member_inputs(fixed) == {
        "mat": {"coolant": "42", "note": "x"},
        "heat": {"mesh_seed": 3},
    }
    # A key without a member prefix is ignored, not crashed on.
    assert _split_member_inputs({"loose": 1}) == {}


def test_candidate_open_inputs_merges_fixed_then_factors() -> None:
    fixed_by_member = {"mat": {"coolant": "42"}}
    factor_map = {"mat.thickness": ("mat", "thickness"), "heat.mesh": ("heat", "mesh_size")}
    cand = {"mat.thickness": 2.5, "heat.mesh": 0.8}
    # The held material rides alongside this candidate's varied factors.
    assert _candidate_open_inputs(cand, fixed_by_member, factor_map) == {
        "mat": {"coolant": "42", "thickness": 2.5},
        "heat": {"mesh_size": 0.8},
    }


def test_candidate_open_inputs_factor_overrides_fixed() -> None:
    # If a key is somehow both held and varied, the search value wins.
    fixed_by_member = {"mat": {"thickness": 9.9}}
    factor_map = {"mat.thickness": ("mat", "thickness")}
    assert _candidate_open_inputs({"mat.thickness": 2.5}, fixed_by_member, factor_map) == {
        "mat": {"thickness": 2.5}
    }


def test_select_runner_picks_workflow_runner() -> None:
    """A ``workflow``-target config routes to the WorkflowRunRunner (no DB)."""
    runtime = OptimisationRuntime(
        factory=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        runner=object(),  # type: ignore[arg-type]
        environment="test",
    )
    runner = runtime._select_runner({"target": {"kind": "workflow"}, "run_mode": "real"})
    assert isinstance(runner, WorkflowRunRunner)
    # The tool/real path still returns the injected runner.
    tool_runner = runtime._select_runner({"target": {"kind": "tool"}, "run_mode": "real"})
    assert tool_runner is runtime._runner


class _WorkflowFakeRunner:
    """In-memory stand-in for the real WorkflowRunRunner.

    Scores each candidate as the sum of its (member-qualified) factor
    values — a simple bowl the optimiser can minimise — and reports it
    under every declared response name.
    """

    async def run_batch(
        self,
        *,
        ref: StudyRef,
        candidates: Any,
        batch: int,  # noqa: ARG002
        rationales: Any,  # noqa: ARG002
    ) -> list[Evaluation]:
        evals: list[Evaluation] = []
        for c in candidates:
            total = float(sum(v for v in c.values() if isinstance(v, int | float)))
            responses = dict.fromkeys(ref.response_names, total)
            evals.append(
                Evaluation(candidate=c, responses=responses, feasible=True, succeeded=True)
            )
        return evals


def _workflow_study(b: Basics) -> OptimisationStudy:
    config: dict[str, Any] = {
        "target": {"kind": "workflow", "workflow_alias": "mat_heat", "workflow_version_id": 1},
        "factors": [
            {
                "name": "heat.mesh",
                "member": "heat",
                "input": "mesh_size",
                "type": "float",
                "min": 0.0,
                "max": 10.0,
            },
            {
                "name": "mat.thickness",
                "member": "mat",
                "input": "thickness",
                "type": "float",
                "min": 0.0,
                "max": 5.0,
            },
        ],
        "responses": [
            {
                "name": "temp",
                "goal": "minimise",
                "spec": {"kind": "scalar", "member": "heat", "name": "max_temp"},
            }
        ],
        "strategy": "bo",
        "run_mode": "real",
        "batch_size": 2,
        "concurrency_cap": 2,
        "seed": {"kind": "lhs", "n": 4},
        "stopping_criteria": [{"kind": "max_runs", "value": 10}],
        "random_seed": 7,
    }
    return OptimisationStudy(
        project_id=b.project_id,
        name="wf-opt",
        target_kind="workflow",
        workflow_alias="mat_heat",
        workflow_version_id=1,
        scenario_id=b.scenario_id,  # stands in for the study's dedicated scenario
        config_json=config,
        state="running",
        created_by_user_id=b.user_id,
    )


def _member_descriptor(alias: str, *, inputs: list[dict], outputs: list[dict]) -> dict[str, Any]:
    return {
        "alias": alias,
        "name": alias.title(),
        "version": "1.0.0",
        "description": f"{alias} fixture",
        "main_script": "static/main.py",
        "inputs": inputs,
        "outputs": outputs,
    }


async def _install_chain(factory: Any, *, user_id: int) -> None:
    """Install ``mat`` → ``heat`` (heat consumes mat's youngs_modulus) + the workflow."""
    mat = _member_descriptor(
        "mat",
        inputs=[
            {
                "name": "thickness",
                "label": "Thickness",
                "type": "float",
                "minimum": 0.0,
                "maximum": 5.0,
            }
        ],
        outputs=[{"name": "youngs_modulus", "label": "E", "type": "number"}],
    )
    heat = _member_descriptor(
        "heat",
        inputs=[
            {"name": "mesh_size", "label": "Mesh", "type": "float", "minimum": 0.1, "maximum": 5.0},
            {
                "name": "youngs_modulus",
                "label": "E",
                "type": "float",
                "downstream_input": True,
                "upstream_tool": "mat",
            },
        ],
        outputs=[{"name": "max_temp", "label": "Max temp", "type": "number"}],
    )
    async with session_scope(factory) as session:
        for desc in (mat, heat):
            session.add(
                Tool(
                    alias=desc["alias"],
                    name=desc["name"],
                    version="1.0.0",
                    state="released",
                    descriptor=desc,
                    bundle_path=f"bundle/{desc['alias']}",
                    installed_by_user_id=user_id,
                )
            )
            session.add(
                ToolVersion(
                    alias=desc["alias"],
                    version="1.0.0",
                    descriptor_snapshot=desc,
                    bundle_path=f"bundle/{desc['alias']}",
                    created_at=_dt.datetime.now(tz=_dt.UTC),
                    created_by_user_id=user_id,
                    change_summary="seed",
                    changes={},
                )
            )
        workflow = Workflow(
            alias="mat_heat",
            name="Mat Heat",
            version="1.0.0",
            descriptor={"description": ""},
            installed_by_user_id=user_id,
        )
        session.add(workflow)
        await session.flush()
        session.add(WorkflowMember(workflow_id=workflow.id, tool_alias="mat", position=1))
        session.add(WorkflowMember(workflow_id=workflow.id, tool_alias="heat", position=2))


async def test_configure_workflow_target_creates_dedicated_scenario(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """configure() validates the workflow config + spins up the study's scenario (§3.4)."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
    await _install_chain(factory, user_id=basics.user_id)

    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        study = await service.create_workflow_draft(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="Chain opt",
            workflow_alias="mat_heat",
            workflow_version_id=1,
        )
        sid = study.id
        await service.update_draft(
            session,
            actor_user_id=basics.user_id,
            study_id=sid,
            config_patch={
                "factors": [
                    {
                        "name": "heat.mesh",
                        "member": "heat",
                        "input": "mesh_size",
                        "type": "float",
                        "min": 0.5,
                        "max": 5.0,
                    },
                    {
                        "name": "mat.thick",
                        "member": "mat",
                        "input": "thickness",
                        "type": "float",
                        "min": 0.0,
                        "max": 5.0,
                    },
                ],
                "responses": [
                    {
                        "name": "temp",
                        "goal": "minimise",
                        "spec": {"kind": "scalar", "member": "heat", "name": "max_temp"},
                    }
                ],
                "strategy": "bo",
                "run_mode": "real",
                "batch_size": 2,
                "seed": {"kind": "lhs", "n": 4},
                "stopping_criteria": [{"kind": "max_runs", "value": 20}],
            },
        )
        configured = await service.configure(session, actor_user_id=basics.user_id, study_id=sid)
        assert configured.state == "running"
        assert configured.scenario_id is not None
        scenario = await session.get(Scenario, configured.scenario_id)
        assert scenario is not None
        assert scenario.project_id == basics.project_id
        assert "optimisation runs" in scenario.name


async def test_run_study_workflow_target_reaches_terminal(
    studies_engine: AsyncEngine, basics: Basics, monkeypatch: Any
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _workflow_study(basics)
        session.add(study)
        await session.flush()
        study_id = study.id

    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=_WorkflowFakeRunner()
    )
    # Swap the real (daemon-dependent) workflow runner for the in-memory one.
    monkeypatch.setattr(OptimisationRuntime, "_select_runner", lambda self, _cfg: self._runner)
    await runtime.run_study(study_id)

    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.state == "complete"
        assert done.stop_reason == "budget_exhausted"
        assert done.current_best_json is not None
        assert "temp" in done.current_best_json
        assert done.convergence_trace


async def _make_run(
    factory: Any, b: Basics, *, mat_state: str, heat_state: str
) -> int:
    """One headless workflow run of the mat→heat chain with the given child states."""
    async with session_scope(factory) as session:
        workflow_id = int(
            (await session.execute(select(Workflow.id).where(Workflow.alias == "mat_heat")))
            .scalar_one()
        )
        version_id = {
            alias: int(
                (
                    await session.execute(
                        select(ToolVersion.id).where(ToolVersion.alias == alias)
                    )
                ).scalar_one()
            )
            for alias in ("mat", "heat")
        }
        run = WorkflowRun(
            workflow_id=workflow_id,
            workflow_version_snapshot="1.0.0",
            scenario_id=b.scenario_id,
            name="opt run",
            submitted_by_user_id=b.user_id,
        )
        session.add(run)
        await session.flush()
        for position, (alias, state) in enumerate(
            (("mat", mat_state), ("heat", heat_state)), start=1
        ):
            session.add(
                Job(
                    scenario_id=b.scenario_id,
                    workflow_run_id=run.id,
                    workflow_position=position,
                    name=f"{alias}-child",
                    tool_alias=alias,
                    tool_version_id=version_id[alias],
                    state=state,
                    inputs={},
                    created_by_user_id=b.user_id,
                )
            )
        await session.flush()
        return int(run.id)


async def test_await_runs_terminal_does_not_hang_on_failed_member(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """A failed upstream leaves the downstream ``created``; the wait must not block.

    Regression: ``_await_runs_terminal`` used to require *every* child job
    terminal, so a failed member (downstream stuck ``created``) blocked the
    batch until ``batch_timeout_s``. It now keys off the run-level state.
    """
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
    await _install_chain(factory, user_id=basics.user_id)
    run_id = await _make_run(factory, basics, mat_state="failed", heat_state="created")

    runner = WorkflowRunRunner(
        factory,
        AuditWriter(signing_key=None, key_id=None),
        poll_interval_s=0.02,
        batch_timeout_s=10.0,
    )
    started = time.monotonic()
    await runner._await_runs_terminal([run_id])
    elapsed = time.monotonic() - started
    # Returns on the first poll, nowhere near the 10 s timeout.
    assert elapsed < 2.0


async def _study_and_run(factory: Any, b: Basics) -> tuple[int, int, list[int]]:
    """Install the chain, add a workflow study, and one run with complete children."""
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
    await _install_chain(factory, user_id=b.user_id)
    async with session_scope(factory) as session:
        study = _workflow_study(b)
        session.add(study)
        await session.flush()
        study_id = int(study.id)
    run_id = await _make_run(factory, b, mat_state="complete", heat_state="complete")
    async with session_scope(factory) as session:
        child_ids = [
            int(j)
            for j in (
                await session.execute(
                    select(Job.id).where(Job.workflow_run_id == run_id).order_by(Job.id)
                )
            )
            .scalars()
            .all()
        ]
    return study_id, run_id, child_ids


async def test_record_run_result_writes_one_row_keyed_on_final_member(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    from matofx.hierarchy.orm import Job  # noqa: PLC0415
    from matofx.studies.orm import StudyResult  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    study_id, run_id, _ = await _study_and_run(factory, basics)
    runner = WorkflowRunRunner(factory, AuditWriter(signing_key=None, key_id=None))

    candidate = {"heat.mesh": 1.5, "mat.thickness": 2.0}
    outputs = {"heat": {"max_temp": 42.0}, "mat": {"youngs_modulus": 200.0}}
    for _ in range(2):  # twice → idempotent (one row)
        async with session_scope(factory) as session:
            children = list(
                (await session.execute(select(Job).where(Job.workflow_run_id == run_id)))
                .scalars()
                .all()
            )
            await runner._record_run_result(
                session,
                study_id=study_id,
                batch=3,
                children=children,
                candidate=candidate,
                outputs_by_member=outputs,
            )

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    select(StudyResult).where(
                        StudyResult.source_kind == "optimisation",
                        StudyResult.source_id == study_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        # Member-qualified scalars from every member's outputs.
        assert rows[0].scalars == {"heat.max_temp": 42.0, "mat.youngs_modulus": 200.0}
        anchor = await session.get(Job, rows[0].job_id)
        assert anchor is not None
        assert anchor.tool_alias == "heat"  # the final member (position 2)
        assert anchor.optimisation_metadata_json == {"batch": 3, "candidate": candidate}


async def test_run_results_reads_workflow_rows(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    study_id, run_id, _ = await _study_and_run(factory, basics)
    runner = WorkflowRunRunner(factory, AuditWriter(signing_key=None, key_id=None))
    candidate = {"heat.mesh": 1.5, "mat.thickness": 2.0}
    async with session_scope(factory) as session:
        children = list(
            (await session.execute(select(Job).where(Job.workflow_run_id == run_id)))
            .scalars()
            .all()
        )
        await runner._record_run_result(
            session,
            study_id=study_id,
            batch=1,
            children=children,
            candidate=candidate,
            outputs_by_member={"heat": {"max_temp": 42.0}},
        )

    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        results = await service.run_results(
            session, actor_user_id=basics.user_id, study_id=study_id
        )
    assert len(results) == 1
    row = results[0]
    assert row["batch"] == 1
    assert row["inputs"] == candidate  # the candidate, not the member job's inputs
    assert row["scalars"] == {"heat.max_temp": 42.0}
    assert row["state"] == "complete"


async def test_workflow_descriptor_maps_member_qualified_labels(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    # The auto-summary resolves a workflow study's member-qualified names to
    # "Tool name · field label" instead of degrading to aliases.
    from matofx.summaries.collectors._descriptor import workflow_descriptor_maps  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
    await _install_chain(factory, user_id=basics.user_id)
    async with session_scope(factory) as session:
        maps = await workflow_descriptor_maps(session, "mat_heat")
    assert maps.output_label("heat.max_temp") == "Heat · Max temp"
    assert maps.output_label("mat.youngs_modulus") == "Mat · E"
    assert maps.input_label("mat.thickness") == "Mat · Thickness"
    # Unknown keys degrade to the key itself (never crash a summary).
    assert maps.output_label("nope.x") == "nope.x"


async def test_await_runs_terminal_returns_when_all_complete(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """The happy path (every member complete) still resolves promptly."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
    await _install_chain(factory, user_id=basics.user_id)
    run_id = await _make_run(factory, basics, mat_state="complete", heat_state="complete")

    runner = WorkflowRunRunner(
        factory,
        AuditWriter(signing_key=None, key_id=None),
        poll_interval_s=0.02,
        batch_timeout_s=10.0,
    )
    started = time.monotonic()
    await runner._await_runs_terminal([run_id])
    assert time.monotonic() - started < 2.0

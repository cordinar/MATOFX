"""End-to-end outer-loop tests (ADR-0009 §4, build Phase 2).

A fast in-memory :class:`FakeRunner` stands in for real job execution
(mirroring the parametric suite's ``fake_queue``), so the loop, seed,
strategies, and termination are exercised without standing up the
supervisor + staging. A ``running`` study is driven to a terminal state
on each of ``random`` / ``grid`` / ``bo`` and the convergence trace,
best-so-far, and terminal state are asserted.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job
from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.studies.optimisation.runtime import Evaluation, OptimisationRuntime, StudyRef
from matofx.studies.orm import ParametricStudy, StudyResult
from tests.studies.conftest import Basics


class FakeRunner:
    """Evaluates candidates in-memory; ``mass = sum of numeric factors``."""

    def __init__(self) -> None:
        self.total = 0

    async def run_batch(
        self,
        *,
        ref: StudyRef,
        candidates: Any,
        batch: int,  # noqa: ARG002 — Protocol arg
        rationales: Any,  # noqa: ARG002 — Protocol arg
    ) -> list[Evaluation]:
        evals: list[Evaluation] = []
        for c in candidates:
            mass = float(sum(v for v in c.values() if isinstance(v, int | float)))
            responses = dict.fromkeys(ref.response_names, mass)
            evals.append(
                Evaluation(candidate=c, responses=responses, feasible=True, succeeded=True)
            )
        self.total += len(candidates)
        return evals


def _running_study(b: Basics, strategy: str, **cfg_overrides: Any) -> OptimisationStudy:
    config: dict[str, Any] = {
        "target": {
            "kind": "tool",
            "tool_alias": b.tool_alias,
            "tool_version_id": b.tool_version_id,
        },
        "factors": [
            {"name": "x", "type": "float", "min": 0.0, "max": 10.0},
            {"name": "n", "type": "int", "min": 0, "max": 5},
        ],
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": strategy,
        "run_mode": "real",
        "batch_size": 3,
        "concurrency_cap": 3,
        "seed": {"kind": "lhs", "n": 4},
        "stopping_criteria": [{"kind": "max_runs", "value": 12}],
        "random_seed": 42,
    }
    config.update(cfg_overrides)
    return OptimisationStudy(
        project_id=b.project_id,
        name=f"opt-{strategy}",
        target_kind="tool",
        tool_alias=b.tool_alias,
        tool_version_id=b.tool_version_id,
        config_json=config,
        state="running",
        created_by_user_id=b.user_id,
    )


async def test_hypervolume_reference_point_survives_continue(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """A continued multi-objective study keeps the same hypervolume reference.

    Otherwise the metric would re-anchor to the (now better) front and appear to
    drop at the resume boundary even though the Pareto front only improved.
    """
    from matofx.studies.optimisation import make_optimisation_service  # noqa: PLC0415
    from matofx.studies.optimisation.state import load_optimiser_state  # noqa: PLC0415

    config: dict[str, Any] = {
        "target": {
            "kind": "tool",
            "tool_alias": basics.tool_alias,
            "tool_version_id": basics.tool_version_id,
        },
        "factors": [
            {"name": "x", "type": "float", "min": 0.0, "max": 10.0},
            {"name": "n", "type": "int", "min": 0, "max": 5},
        ],
        "responses": [
            {"name": "q", "spec": {"kind": "scalar"}, "goal": "maximise"},
            {"name": "eff", "spec": {"kind": "scalar"}, "goal": "maximise"},
        ],
        "strategy": "nsga2",
        "run_mode": "real",
        "batch_size": 4,
        "stopping_criteria": [{"kind": "max_runs", "value": 8}],
        "random_seed": 1,
    }
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = OptimisationStudy(
            project_id=basics.project_id,
            name="mo-continue",
            target_kind="tool",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            config_json=config,
            state="running",
            created_by_user_id=basics.user_id,
        )
        session.add(study)
        await session.flush()
        study_id = study.id

    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=FakeRunner()
    )
    await runtime.run_study(study_id)
    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        ref_before = (load_optimiser_state(done) or {}).get("ref_point")
    assert isinstance(ref_before, list)
    assert ref_before  # a reference was fixed

    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        await service.extend_budget(
            session, actor_user_id=basics.user_id, study_id=study_id, additional_runs=8
        )
    await runtime.run_study(study_id)
    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        ref_after = (load_optimiser_state(done) or {}).get("ref_point")
    # The reference point is unchanged across the continue — not re-anchored.
    assert ref_after == ref_before


async def test_continue_extends_budget_and_runs_more(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """Continuing a budget-exhausted study resumes it and runs more (§14)."""
    from matofx.studies.optimisation import make_optimisation_service  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _running_study(basics, "bo", stopping_criteria=[{"kind": "max_runs", "value": 12}])
        session.add(study)
        await session.flush()
        study_id = study.id

    runner = FakeRunner()
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=runner
    )
    await runtime.run_study(study_id)
    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.state == "complete"
    first_total = runner.total
    assert first_total <= 12

    # Continue with 12 more runs → resumes from the persisted state.
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        await service.extend_budget(
            session, actor_user_id=basics.user_id, study_id=study_id, additional_runs=12
        )
    await runtime.run_study(study_id)

    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.state == "complete"
        assert done.last_batch_number >= 2
    # It evaluated more runs (without re-seeding from scratch) up to the new budget.
    assert runner.total > first_total
    assert runner.total <= 24


@pytest.mark.parametrize("strategy", ["random", "grid", "bo", "cmaes", "de", "pso", "ga"])
async def test_run_study_reaches_terminal(
    studies_engine: AsyncEngine, basics: Basics, strategy: str
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _running_study(basics, strategy)
        session.add(study)
        await session.flush()
        study_id = study.id

    runner = FakeRunner()
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=runner
    )
    await runtime.run_study(study_id)

    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.state == "complete"
        assert done.stop_reason in {"budget_exhausted", "grid_exhausted"}
        assert done.last_batch_number >= 1
        assert done.convergence_trace  # at least one point
        assert done.current_best_json is not None
        assert "mass" in done.current_best_json
    # Budget honoured — never more than max_runs evaluations.
    assert runner.total <= 12


async def test_run_study_budget_exhausted_random(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """random has no natural end, so it stops exactly on the budget."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _running_study(basics, "random")
        session.add(study)
        await session.flush()
        study_id = study.id

    runner = FakeRunner()
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=runner
    )
    await runtime.run_study(study_id)
    assert runner.total == 12

    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.stop_reason == "budget_exhausted"


async def test_dedupe_stops_when_integer_space_exhausted(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """A tiny integer-only space can't fill a generous budget — once every
    distinct design has run, the dedupe stops the study (``space_exhausted``)
    rather than re-running identical jobs (ADR-0009 amendment 2026-06-15)."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _running_study(
            basics,
            "random",
            factors=[{"name": "n", "type": "int", "min": 0, "max": 2}],  # 3 designs only
            stopping_criteria=[{"kind": "max_runs", "value": 50}],  # far more than 3
        )
        session.add(study)
        await session.flush()
        study_id = study.id

    runner = FakeRunner()
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=runner
    )
    await runtime.run_study(study_id)

    # Only the three distinct integer points ever ran — no identical re-runs.
    assert runner.total == 3
    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.stop_reason == "space_exhausted"
        assert done.state == "complete"


async def test_halt_stops_the_loop(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _running_study(basics, "random", **{"halt": True})
        # halt_requested is a column, not config — set it on the row.
        study.halt_requested = True
        session.add(study)
        await session.flush()
        study_id = study.id

    runner = FakeRunner()
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=runner
    )
    await runtime.run_study(study_id)

    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.state == "cancelled"
        assert done.stop_reason == "user_halted"


async def _seed_cohort(factory: Any, basics: Basics, cohort_id: int, n: int = 5) -> None:
    """Materialise ``n`` prior runs as ``study_result`` rows under a cohort source."""
    async with session_scope(factory) as session:
        para = ParametricStudy(
            project_id=basics.project_id,
            name="prior",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            mode="grid",
            factor_spec={},
            sampling_params={},
            run_cap=100,
            created_by_user_id=basics.user_id,
        )
        session.add(para)
        await session.flush()
        for i in range(n):
            job = Job(
                scenario_id=None,
                parametric_study_id=para.id,
                name=f"prior-{i}",
                tool_alias=basics.tool_alias,
                tool_version_id=basics.tool_version_id,
                state="complete",
                inputs={"x": float(i * 2), "n": i % 5},
                created_by_user_id=basics.user_id,
            )
            session.add(job)
            await session.flush()
            session.add(
                StudyResult(
                    source_kind="cohort",
                    source_id=cohort_id,
                    job_id=job.id,
                    scalars={"mass": float(i * 2 + i % 5)},
                    retained_files=[],
                )
            )
        await session.flush()


async def test_cohort_seeded_study_runs_only_new_budget(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """A cohort-seeded `bo` study imports prior runs (no budget) then runs max_runs."""
    factory = make_session_factory(studies_engine)
    cohort_id = 4242
    await _seed_cohort(factory, basics, cohort_id, n=5)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _running_study(
            basics,
            "bo",
            seed={"kind": "cohort", "cohort_id": cohort_id},
            stopping_criteria=[{"kind": "max_runs", "value": 6}],
        )
        session.add(study)
        await session.flush()
        study_id = study.id

    runner = FakeRunner()
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=runner
    )
    await runtime.run_study(study_id)

    # The 5 prior runs seeded the surrogate but cost no budget — only the
    # 6 max_runs were actually run.
    assert runner.total == 6
    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.state == "complete"
        # The first trace point is the cohort import (5 prior points, no new runs).
        assert done.convergence_trace[0]["n_total"] == 5


async def test_trace_counts_rule_dropped_candidates(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """Candidates dropped for breaking a rule are counted in the trace.

    A study-level cross-field constraint (the same shape descriptor
    validity rules compile to) rejects proposals where ``n > 3``; each
    persisted batch records how many it dropped in ``n_dropped`` so the
    results page can sum them.
    """
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _running_study(
            basics,
            "random",
            constraints=[{"kind": "cross_field", "expr": "n <= 3"}],
            stopping_criteria=[{"kind": "max_runs", "value": 6}],
        )
        session.add(study)
        await session.flush()
        study_id = study.id

    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=FakeRunner()
    )
    await runtime.run_study(study_id)

    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        trace = done.convergence_trace
        assert trace
        # Every trace point carries the count (shape), and with a constraint
        # that rejects a third of the integer range at least one proposal was
        # dropped over the run.
        assert all("n_dropped" in p for p in trace)
        assert sum(int(p["n_dropped"]) for p in trace) >= 1


async def test_trace_no_drops_without_constraints(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """With no input rules, nothing is dropped — n_dropped sums to zero."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _running_study(
            basics, "random", stopping_criteria=[{"kind": "max_runs", "value": 6}]
        )
        session.add(study)
        await session.flush()
        study_id = study.id

    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=FakeRunner()
    )
    await runtime.run_study(study_id)
    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert sum(int(p.get("n_dropped") or 0) for p in done.convergence_trace) == 0


async def test_scan_picks_up_running_study(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _running_study(basics, "random")
        session.add(study)
        await session.flush()
        study_id = study.id

    runner = FakeRunner()
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=runner
    )
    spawned = await runtime.scan()
    assert spawned == 1
    # Let the spawned per-study task finish.
    for _ in range(500):
        if not runtime._active:
            break
        await asyncio.sleep(0.01)
    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.state == "complete"

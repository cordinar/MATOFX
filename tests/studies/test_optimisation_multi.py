"""Phase 4 tests — multi-objective pymoo strategies + output constraints (ADR-0009 §5/§7).

Pure-unit coverage of the output-constraint helpers, the nsga2/moea_d
Pareto search, and the hypervolume helpers; DB-backed coverage of a
2-objective study reaching a non-trivial Pareto front and of
output-constraint infeasibility being excluded from it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.studies.optimisation import OptimisationStudy
from matofx.studies.optimisation.constraints import output_constraint_g, output_feasible
from matofx.studies.optimisation.runtime import Evaluation, OptimisationRuntime, StudyRef
from matofx.studies.optimisation.strategies import build_context, get
from matofx.studies.optimisation.strategies.multi import hypervolume, reference_point
from tests.studies.conftest import Basics


def _multi_config(strategy: str = "nsga2", **extra: Any) -> dict[str, Any]:
    config = {
        "factors": [
            {"name": "x", "type": "float", "min": 0.0, "max": 10.0},
            {"name": "y", "type": "float", "min": 0.0, "max": 10.0},
        ],
        "responses": [
            {"name": "f1", "spec": {"kind": "scalar"}, "goal": "minimise"},
            {"name": "f2", "spec": {"kind": "scalar"}, "goal": "minimise"},
        ],
        "strategy": strategy,
        "batch_size": 8,
        "random_seed": 1,
    }
    config.update(extra)
    return config


def _objs(c: dict[str, Any]) -> dict[str, float]:
    """Two conflicting objectives over x → a genuine trade-off front."""
    return {"f1": float(c["x"]), "f2": float(10.0 - c["x"])}


# --- output-constraint helpers ---------------------------------------------


def test_output_feasible() -> None:
    config = {"constraints": [{"kind": "output", "response": "t", "op": "<=", "value": 85.0}]}
    assert output_feasible({"t": 80.0}, config) is True
    assert output_feasible({"t": 90.0}, config) is False
    assert output_feasible({}, config) is False  # missing response = violation


def test_output_constraint_g_sign() -> None:
    config = {"constraints": [{"kind": "output", "response": "t", "op": "<=", "value": 85.0}]}
    assert output_constraint_g({"t": 80.0}, config) == [80.0 - 85.0]  # feasible → g<0
    assert output_constraint_g({"t": 90.0}, config) == [90.0 - 85.0]  # infeasible → g>0


def test_output_constraint_g_geq() -> None:
    config = {"constraints": [{"kind": "output", "response": "t", "op": ">=", "value": 10.0}]}
    assert output_constraint_g({"t": 12.0}, config) == [10.0 - 12.0]  # feasible → g<0


# --- pymoo strategies -------------------------------------------------------


def _drive(strategy_name: str, generations: int = 12) -> Any:
    strat = get(strategy_name).bind(build_context(_multi_config(strategy_name)), None)
    for _ in range(generations):
        cands = strat.ask(8)
        responses = [_objs(c) for c in cands]
        strat.tell(cands, responses, [True] * len(cands))
    return strat


def test_nsga2_finds_non_trivial_pareto_front() -> None:
    front = _drive("nsga2").pareto_front()
    assert len(front) > 1
    # The two objectives conflict, so the front spans a range of f1.
    f1s = sorted(p["f1"] for p in front)
    assert f1s[-1] - f1s[0] > 1.0


def test_moea_d_finds_non_trivial_pareto_front() -> None:
    front = _drive("moea_d").pareto_front()
    assert len(front) > 1


def test_nsga3_finds_non_trivial_pareto_front() -> None:
    front = _drive("nsga3").pareto_front()
    assert len(front) > 1
    f1s = sorted(p["f1"] for p in front)
    assert f1s[-1] - f1s[0] > 1.0


def test_pareto_excludes_infeasible_points() -> None:
    strat = get("nsga2").bind(build_context(_multi_config("nsga2")), None)
    cands = strat.ask(8)
    responses = [_objs(c) for c in cands]
    # Mark the low-f1 half infeasible (e.g. an output constraint they break).
    feasible = [r["f1"] >= 5.0 for r in responses]
    strat.tell(cands, responses, feasible)
    front = strat.pareto_front()
    assert front  # something survived
    assert all(p["f1"] >= 5.0 for p in front)  # no infeasible point in the front


def test_hypervolume_and_reference_point() -> None:
    objectives = build_context(_multi_config()).objectives
    pareto = [{"f1": 1.0, "f2": 9.0}, {"f1": 5.0, "f2": 5.0}, {"f1": 9.0, "f2": 1.0}]
    ref = reference_point(pareto, objectives)
    assert len(ref) == 2
    hv = hypervolume(pareto, objectives, ref)
    assert hv > 0.0
    assert hypervolume([], objectives, ref) == 0.0


# --- multi-objective runtime (DB) ------------------------------------------


class MultiFakeRunner:
    """In-memory 2-objective evaluator with conflicting f1 / f2."""

    async def run_batch(
        self,
        *,
        ref: StudyRef,  # noqa: ARG002 — Protocol arg
        candidates: Any,
        batch: int,  # noqa: ARG002 — Protocol arg
        rationales: Any,  # noqa: ARG002 — Protocol arg
    ) -> list[Evaluation]:
        return [
            Evaluation(candidate=c, responses=_objs(c), feasible=True, succeeded=True)
            for c in candidates
        ]


def _multi_study(b: Basics, **cfg: Any) -> OptimisationStudy:
    config = _multi_config(
        "nsga2",
        target={"kind": "tool", "tool_alias": b.tool_alias, "tool_version_id": b.tool_version_id},
        run_mode="real",
        stopping_criteria=[{"kind": "max_runs", "value": 24}],
        **cfg,
    )
    return OptimisationStudy(
        project_id=b.project_id,
        name="multi",
        target_kind="tool",
        tool_alias=b.tool_alias,
        tool_version_id=b.tool_version_id,
        config_json=config,
        state="running",
        created_by_user_id=b.user_id,
    )


async def test_runtime_multi_objective_pareto(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _multi_study(basics)
        session.add(study)
        await session.flush()
        study_id = study.id

    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=MultiFakeRunner()
    )
    await runtime.run_study(study_id)

    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.state == "complete"
        assert done.current_best_json is not None
        front = done.current_best_json["pareto_front"]
        assert len(front) > 1  # a real trade-off front
        # The trace metric is the hypervolume (a number), not a scalar best.
        assert done.convergence_trace[-1]["metric"] is not None


async def test_runtime_output_constraint_excludes_infeasible(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _multi_study(
            basics,
            constraints=[{"kind": "output", "response": "f1", "op": "<=", "value": 5.0}],
        )
        session.add(study)
        await session.flush()
        study_id = study.id

    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=MultiFakeRunner()
    )
    await runtime.run_study(study_id)

    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        front = done.current_best_json["pareto_front"]
        assert front
        # Every Pareto point satisfies the output constraint f1 <= 5.
        assert all(p["f1"] <= 5.0 for p in front)


# --- proposed designs respect the tool's declared resolution ---------------


def test_a_float_factor_is_snapped_to_its_declared_precision() -> None:
    """The optimiser must not propose a design the tool cannot build.

    A tool declaring ``precision = 0.1`` has one decimal of resolution, so a
    candidate of ``10.055135393891423`` asks for something it cannot set and
    records digits the run never had. Integer factors have always been snapped;
    float ones were not, so every optimisation job's inputs carried full binary
    float noise.
    """
    config = _multi_config()
    config["factors"] = [
        {"name": "x", "type": "float", "min": 0.0, "max": 10.0, "precision": 0.1},
        {"name": "y", "type": "float", "min": 0.0, "max": 10.0},  # none declared
    ]
    strategy = get("nsga2").bind(build_context(config), None)
    for candidate in strategy.ask(8):
        x = candidate["x"]
        assert x == round(x, 1), f"{x} is not on the declared 0.1 step"
        assert 0.0 <= x <= 10.0


def test_a_factor_without_a_declared_precision_is_left_alone() -> None:
    """No declared step means the tool has not told us its resolution."""
    from matofx.studies.optimisation.strategies.base import FactorSpec  # noqa: PLC0415

    loose = FactorSpec(name="x", kind="float", low=0.0, high=10.0)
    assert loose.snap_float(3.14159265) == 3.14159265


def test_snapping_still_respects_the_factor_bounds() -> None:
    """A snapped value must never leave the search box."""
    from matofx.studies.optimisation.strategies.base import FactorSpec  # noqa: PLC0415

    tight = FactorSpec(name="x", kind="float", low=2.05, high=9.97, precision=0.1)
    assert tight.snap_float(2.0) >= 2.05
    assert tight.snap_float(99.0) <= 9.97

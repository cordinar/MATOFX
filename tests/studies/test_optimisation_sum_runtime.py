"""Runtime: a sum-checked study's evaluated candidates all satisfy the sum (§3.6).

The repair runs in ``run_study`` before each candidate is evaluated (and told
back to the strategy), so every point the runner sees — seed and proposed —
must total the group's value exactly. Covered for the non-generational
strategies (``bo`` / ``random``); the pymoo generational strategies add an
in-strategy projection (tested with the strategy tests).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.studies.optimisation.runtime import Evaluation, OptimisationRuntime, StudyRef
from matofx.studies.optimisation.strategies import build_context
from matofx.studies.optimisation.strategies import registry as strategy_registry
from tests.studies.conftest import Basics


class _RecordingRunner:
    """Records every candidate it's asked to run; scores by the numeric sum."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def run_batch(
        self,
        *,
        ref: StudyRef,
        candidates: Any,
        batch: int,  # noqa: ARG002 — Protocol arg
        rationales: Any,  # noqa: ARG002 — Protocol arg
    ) -> list[Evaluation]:
        evals: list[Evaluation] = []
        for cand in candidates:
            self.seen.append(dict(cand))
            mass = float(sum(v for v in cand.values() if isinstance(v, int | float)))
            evals.append(
                Evaluation(
                    candidate=cand,
                    responses=dict.fromkeys(ref.response_names, mass),
                    feasible=True,
                    succeeded=True,
                )
            )
        return evals


def _sum_study(b: Basics, strategy: str) -> OptimisationStudy:
    config: dict[str, Any] = {
        "target": {"kind": "tool", "tool_alias": b.tool_alias, "tool_version_id": b.tool_version_id},
        "factors": [
            {"name": "a", "type": "float", "min": 0.0, "max": 100.0},
            {"name": "b", "type": "float", "min": 0.0, "max": 100.0},
            {"name": "c", "type": "float", "min": 0.0, "max": 100.0},
        ],
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": strategy,
        "run_mode": "real",
        "batch_size": 3,
        "seed": {"kind": "lhs", "n": 6},
        "stopping_criteria": [{"kind": "max_runs", "value": 18}],
        "random_seed": 7,
        "sum_groups": [
            {
                "value": 100.0,
                "members": [
                    {"factor": "a", "input_key": "a", "role": "varied", "fixed_value": None},
                    {"factor": "b", "input_key": "b", "role": "varied", "fixed_value": None},
                    {"factor": "c", "input_key": "c", "role": "varied", "fixed_value": None},
                ],
                "balance": "c",
                "balance_kind": "float",
                "balance_low": 0.0,
                "balance_high": 100.0,
            }
        ],
    }
    return OptimisationStudy(
        project_id=b.project_id,
        name=f"sum-{strategy}",
        target_kind="tool",
        tool_alias=b.tool_alias,
        tool_version_id=b.tool_version_id,
        config_json=config,
        state="running",
        created_by_user_id=b.user_id,
    )


@pytest.mark.parametrize("strategy", ["bo", "random", "ga"])
async def test_sum_checked_candidates_all_total_value(
    studies_engine: AsyncEngine, basics: Basics, strategy: str
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _sum_study(basics, strategy)
        session.add(study)
        await session.flush()
        sid = study.id

    runner = _RecordingRunner()
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=runner
    )
    await runtime.run_study(sid)

    assert runner.seen, "no candidates were evaluated"
    for cand in runner.seen:
        assert abs(cand["a"] + cand["b"] + cand["c"] - 100.0) < 1e-6
        # The balance member stays within its own box.
        assert 0.0 <= cand["c"] <= 100.0


def _ctx_config(strategy: str) -> dict[str, Any]:
    # Bounds chosen so the balance (``c``, the widest range) can always absorb
    # the residual: a+b ∈ [0, 60] → c = 100-a-b ∈ [40, 100] ⊆ c's box. So every
    # proposal is feasible and must land on the plane (no infeasible drops).
    return {
        "factors": [
            {"name": "a", "type": "float", "min": 0.0, "max": 30.0},
            {"name": "b", "type": "float", "min": 0.0, "max": 30.0},
            {"name": "c", "type": "float", "min": 40.0, "max": 100.0},
        ],
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": strategy,
        "batch_size": 4,
        "random_seed": 3,
        "sum_groups": [
            {
                "value": 100.0,
                "members": [
                    {"factor": "a", "input_key": "a", "role": "varied", "fixed_value": None},
                    {"factor": "b", "input_key": "b", "role": "varied", "fixed_value": None},
                    {"factor": "c", "input_key": "c", "role": "varied", "fixed_value": None},
                ],
                "balance": "c",
                "balance_kind": "float",
                "balance_low": 40.0,
                "balance_high": 100.0,
            }
        ],
    }


@pytest.mark.parametrize("strategy", ["bo", "ga"])
def test_strategy_decode_projects_onto_sum_plane(strategy: str) -> None:
    """A strategy's own proposals already sum to the value (no runtime needed).

    For pymoo this is what keeps the told point identical to the asked one, so
    the generational match succeeds instead of penalising the whole batch.
    """
    ctx = build_context(_ctx_config(strategy))
    assert ctx.sum_groups  # carried onto the context
    strat = strategy_registry.get(strategy).bind(ctx, None)
    proposals = strat.ask(4)
    assert proposals
    for cand in proposals:
        assert abs(cand["a"] + cand["b"] + cand["c"] - 100.0) < 1e-6
    # Telling those exact points back advances cleanly + the next ask stays on-plane.
    strat.tell(proposals, [{"mass": c["a"]} for c in proposals], [True] * len(proposals))
    for cand in strat.ask(4):
        assert abs(cand["a"] + cand["b"] + cand["c"] - 100.0) < 1e-6

"""Service-level MOP/CoP analysis over a study's runs (ADR-0009 §5.7, Phase 12b).

Seeds a tool-target ``bo`` study with completed children + ``study_result``
rows (``mass = 10*x1 + 0.001*x2``) and checks ``compute_mop`` fits the
best metamodel, attributes CoP to the driver, and flags the inert factor
for variable reduction — persisting it all on the study.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job
from matofx.studies.optimisation import (
    OptimisationConfigError,
    OptimisationStudy,
    make_optimisation_service,
)
from matofx.studies.optimisation.runtime import Evaluation, OptimisationRuntime
from matofx.studies.orm import StudyResult
from tests.studies.conftest import Basics


def _study(b: Basics) -> OptimisationStudy:
    config: dict[str, Any] = {
        "target": {
            "kind": "tool",
            "tool_alias": b.tool_alias,
            "tool_version_id": b.tool_version_id,
        },
        "factors": [
            {"name": "x1", "type": "float", "min": 0.0, "max": 1.0},
            {"name": "x2", "type": "float", "min": 0.0, "max": 1.0},
        ],
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": "bo",
        "run_mode": "real",
        "mop": {"enabled": True, "cop_cutoff": 0.01},
        "batch_size": 2,
        "seed": {"kind": "lhs", "n": 4},
        "stopping_criteria": [{"kind": "max_runs", "value": 40}],
    }
    return OptimisationStudy(
        project_id=b.project_id,
        name="mop-study",
        target_kind="tool",
        tool_alias=b.tool_alias,
        tool_version_id=b.tool_version_id,
        config_json=config,
        state="running",
        created_by_user_id=b.user_id,
    )


async def test_compute_mop_attributes_cop_and_flags_inert_factor(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        study_id = study.id
        # 40 completed children: mass driven by x1, x2 nearly inert.
        for i in range(40):
            x1 = (i % 8) / 7.0
            x2 = (i // 8) / 4.0
            job = Job(
                scenario_id=None,
                optimisation_study_id=study_id,
                name=f"c{i}",
                tool_alias=basics.tool_alias,
                tool_version_id=basics.tool_version_id,
                state="complete",
                inputs={"x1": x1, "x2": x2},
                created_by_user_id=basics.user_id,
            )
            session.add(job)
            await session.flush()
            session.add(
                StudyResult(
                    source_kind="optimisation",
                    source_id=study_id,
                    job_id=job.id,
                    scalars={"mass": 10.0 * x1 + 0.001 * x2},
                    retained_files=[],
                    feasible=True,
                )
            )

    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        state = await service.compute_mop(session, actor_user_id=basics.user_id, study_id=study_id)

    assert state["best_kind"] in {"gp", "gp_matern", "poly", "gbm"}
    assert state["n_runs"] == 40
    assert state["objective"] == "mass"
    cop = {row["factor"]: row["cop"] for row in state["cop"]}
    assert cop["x1"] > cop["x2"]
    assert "x2" in state["suggested_prune"]
    assert "x2" in state["frozen_factors"]
    assert "x1" not in state["frozen_factors"]


class _RecordingRunner:
    """Records every candidate it's asked to run (to assert the search space)."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def run_batch(
        self,
        *,
        ref: Any,
        candidates: Any,
        batch: int,  # noqa: ARG002
        rationales: Any,  # noqa: ARG002
    ) -> list[Evaluation]:
        evals: list[Evaluation] = []
        for c in candidates:
            self.seen.append(dict(c))
            evals.append(
                Evaluation(
                    candidate=dict(c),
                    responses=dict.fromkeys(ref.response_names, float(c.get("x1", 0.0))),
                    feasible=True,
                    succeeded=True,
                )
            )
        return evals


async def test_run_study_freezes_pruned_factor_at_nominal(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """A MOP-frozen factor is held at its box midpoint; the search varies the rest (§5.7)."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        # x2 frozen out by a prior MOP analysis; gp chosen as best metamodel.
        study.mop_state_json = {"frozen_factors": ["x2"], "best_kind": "gp", "cop": []}
        session.add(study)
        await session.flush()
        study_id = study.id

    runner = _RecordingRunner()
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=runner
    )
    await runtime.run_study(study_id)

    assert runner.seen  # something ran
    # x2 (frozen) is pinned to the midpoint of its [0, 1] box on every candidate;
    # x1 (active) is genuinely searched.
    assert all(c["x2"] == 0.5 for c in runner.seen)
    assert len({c["x1"] for c in runner.seen}) > 1


async def test_set_mop_frozen_factors_overrides(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        study.mop_state_json = {"frozen_factors": ["x2"], "cop": []}
        session.add(study)
        await session.flush()
        study_id = study.id

    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        state = await service.set_mop_frozen_factors(
            session, actor_user_id=basics.user_id, study_id=study_id, frozen_factors=["x1"]
        )
    assert state["frozen_factors"] == ["x1"]

    # An unknown factor is rejected.
    async with session_scope(factory) as session:
        with pytest.raises(OptimisationConfigError, match="aren't factors"):
            await service.set_mop_frozen_factors(
                session, actor_user_id=basics.user_id, study_id=study_id, frozen_factors=["ghost"]
            )

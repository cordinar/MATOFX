"""Phase 3 tests — stopping criteria, durable state, resume, crash recovery (ADR-0009 §8/§10).

Pure-unit coverage of ``evaluate_stopping``, the state round-trip, and
idempotent ``tell``; DB-backed coverage of resume-from-persisted-state,
the ``target`` stop, and the corrupt-state park (via the studies
fixtures, so both engines are exercised).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.studies.optimisation import OptimisationStudy
from matofx.studies.optimisation.errors import OptimiserStateCorruptError
from matofx.studies.optimisation.runtime import Evaluation, OptimisationRuntime, StudyRef
from matofx.studies.optimisation.state import load_optimiser_state, persist_optimiser_state
from matofx.studies.optimisation.stopping import evaluate_stopping
from matofx.studies.optimisation.strategies import build_context, get
from tests.studies.conftest import Basics


class FakeRunner:
    """In-memory evaluator: ``mass = sum of numeric factors``."""

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
        evals = [
            Evaluation(
                candidate=c,
                responses={
                    n: float(sum(v for v in c.values() if isinstance(v, int | float)))
                    for n in ref.response_names
                },
                feasible=True,
                succeeded=True,
            )
            for c in candidates
        ]
        self.total += len(candidates)
        return evals


# --- evaluate_stopping ------------------------------------------------------


def _stop_config(*criteria: dict[str, Any]) -> dict[str, Any]:
    return {
        "responses": [{"name": "mass", "goal": "minimise"}],
        "stopping_criteria": list(criteria),
    }


def test_stopping_max_runs() -> None:
    config = _stop_config({"kind": "max_runs", "value": 10})
    assert evaluate_stopping(config, total=9, best=None, trace=[], elapsed_s=0) is None
    assert (
        evaluate_stopping(config, total=10, best=None, trace=[], elapsed_s=0) == "budget_exhausted"
    )


def test_stopping_target_met() -> None:
    config = _stop_config(
        {"kind": "max_runs", "value": 100},
        {"kind": "target", "response": "mass", "op": "<=", "value": 5.0},
    )
    assert evaluate_stopping(config, total=3, best={"mass": 9.0}, trace=[], elapsed_s=0) is None
    assert (
        evaluate_stopping(config, total=3, best={"mass": 4.0}, trace=[], elapsed_s=0)
        == "target_met"
    )


def test_stopping_wall_clock() -> None:
    config = _stop_config({"kind": "max_runs", "value": 100}, {"kind": "wall_clock", "hours": 1})
    assert evaluate_stopping(config, total=1, best=None, trace=[], elapsed_s=3599) is None
    assert evaluate_stopping(config, total=1, best=None, trace=[], elapsed_s=3601) == "wall_clock"


def test_stopping_convergence() -> None:
    config = _stop_config(
        {"kind": "max_runs", "value": 100},
        {"kind": "convergence", "metric": "best_so_far", "window": 3, "epsilon": 0.01},
    )
    # Improving — not converged.
    improving = [{"metric": m} for m in (10.0, 8.0, 6.0, 4.0)]
    assert evaluate_stopping(config, total=4, best=None, trace=improving, elapsed_s=0) is None
    # Flat over the window — converged.
    flat = [{"metric": m} for m in (5.0, 5.0, 5.0, 5.0)]
    assert evaluate_stopping(config, total=4, best=None, trace=flat, elapsed_s=0) == "converged"


def test_stopping_order_first_match_wins() -> None:
    config = _stop_config(
        {"kind": "max_runs", "value": 3},
        {"kind": "target", "response": "mass", "op": "<=", "value": 5.0},
    )
    # Both fire; max_runs is declared first.
    assert (
        evaluate_stopping(config, total=3, best={"mass": 1.0}, trace=[], elapsed_s=0)
        == "budget_exhausted"
    )


# --- durable state round-trip ----------------------------------------------


def _study_stub(study_id: int = 1) -> OptimisationStudy:
    study = OptimisationStudy()
    study.id = study_id
    study.optimiser_state_json = None
    study.optimiser_state_blob_path = None
    study.optimiser_state_sha256 = None
    return study


def test_state_inline_json_round_trip() -> None:
    study = _study_stub()
    payload = {"strategy": {"history": [{"x": {"a": 1}, "y": {"mass": 2.0}, "feasible": True}]}}
    persist_optimiser_state(study, payload, blob_root=None)
    assert study.optimiser_state_json == payload
    assert study.optimiser_state_blob_path is None
    assert load_optimiser_state(study) == payload


def test_state_blob_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    study = _study_stub(7)
    payload = {"strategy": {"history": [{"x": {"a": 1}, "y": {"mass": 2.0}, "feasible": True}]}}
    # Force the blob path by lowering the inline threshold.
    monkeypatch.setattr("matofx.studies.optimisation.state._MAX_INLINE_BYTES", 0)
    persist_optimiser_state(study, payload, blob_root=tmp_path)
    assert study.optimiser_state_json is None
    assert study.optimiser_state_blob_path is not None
    assert study.optimiser_state_sha256 is not None
    assert load_optimiser_state(study) == payload


def test_state_corrupt_blob_raises(tmp_path: Path) -> None:
    study = _study_stub(9)
    blob = tmp_path / "state.pkl"
    blob.write_bytes(b"not a real joblib payload")
    study.optimiser_state_blob_path = str(blob)
    study.optimiser_state_sha256 = "0" * 64  # deliberately wrong
    with pytest.raises(OptimiserStateCorruptError, match="integrity check"):
        load_optimiser_state(study)


def test_state_none_when_unwritten() -> None:
    assert load_optimiser_state(_study_stub()) is None


# --- idempotent tell --------------------------------------------------------


def _factor_config(strategy: str) -> dict[str, Any]:
    return {
        "factors": [{"name": "x", "type": "float", "min": 0.0, "max": 10.0}],
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": strategy,
        "random_seed": 1,
    }


def test_tell_is_idempotent_random() -> None:
    strat = get("random").bind(build_context(_factor_config("random")), None)
    point = [{"x": 3.0}]
    strat.tell(point, [{"mass": 3.0}], [True])
    strat.tell(point, [{"mass": 3.0}], [True])  # re-told — no-op
    assert len(strat._history) == 1


def test_tell_is_idempotent_bo() -> None:
    strat = get("bo").bind(build_context(_factor_config("bo")), None)
    point = [{"x": 3.0}]
    strat.tell(point, [{"mass": 3.0}], [True])
    strat.tell(point, [{"mass": 3.0}], [True])  # re-told — no-op
    assert len(strat._history) == 1


# --- resume + corrupt park (DB) ---------------------------------------------


def _resume_config() -> dict[str, Any]:
    return {
        "target": {"kind": "tool"},
        "factors": [
            {"name": "x", "type": "float", "min": 0.0, "max": 10.0},
            {"name": "n", "type": "int", "min": 0, "max": 5},
        ],
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": "random",
        "run_mode": "real",
        "batch_size": 3,
        "stopping_criteria": [{"kind": "max_runs", "value": 12}],
        "random_seed": 1,
    }


async def test_resume_continues_from_persisted_state(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """A study with 6 points already persisted runs only the remaining 6."""
    factory = make_session_factory(studies_engine)
    history = [
        {"x": {"x": float(i), "n": i % 3}, "y": {"mass": float(i)}, "feasible": True}
        for i in range(6)
    ]
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = OptimisationStudy(
            project_id=basics.project_id,
            name="resume",
            target_kind="tool",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            config_json=_resume_config(),
            state="running",
            created_by_user_id=basics.user_id,
            last_batch_number=2,
            convergence_trace=[{"batch": 1, "metric": 5.0}, {"batch": 2, "metric": 3.0}],
            optimiser_state_json={
                "strategy": {"history": history},
                "total": 6,
                "any_success": True,
            },
        )
        session.add(study)
        await session.flush()
        study_id = study.id

    runner = FakeRunner()
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=runner
    )
    await runtime.run_study(study_id)

    assert runner.total == 6  # only the remaining budget ran, not all 12
    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.state == "complete"
        assert done.stop_reason == "budget_exhausted"
        assert done.last_batch_number == 4  # resumed at 2, ran two more batches


async def test_target_stop_via_runtime(studies_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    config = _resume_config()
    config["stopping_criteria"] = [
        {"kind": "max_runs", "value": 99},
        {"kind": "target", "response": "mass", "op": "<=", "value": 1000.0},
    ]
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = OptimisationStudy(
            project_id=basics.project_id,
            name="target",
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
        assert done is not None
        assert done.state == "complete"
        assert done.stop_reason == "target_met"


async def test_corrupt_state_parks(
    studies_engine: AsyncEngine, basics: Basics, tmp_path: Path
) -> None:
    factory = make_session_factory(studies_engine)
    blob = tmp_path / "state.pkl"
    blob.write_bytes(b"garbage")
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = OptimisationStudy(
            project_id=basics.project_id,
            name="corrupt",
            target_kind="tool",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            config_json=_resume_config(),
            state="running",
            created_by_user_id=basics.user_id,
            last_batch_number=1,
            optimiser_state_blob_path=str(blob),
            optimiser_state_sha256="0" * 64,  # wrong digest
        )
        session.add(study)
        await session.flush()
        study_id = study.id

    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=FakeRunner()
    )
    # run_study surfaces the corruption...
    with pytest.raises(OptimiserStateCorruptError):
        await runtime.run_study(study_id)
    # ...and the tracked entrypoint parks it (stays running, no re-pickup).
    await runtime._run_tracked(study_id)
    assert study_id in runtime._parked
    async with session_scope(factory) as session:
        parked = await session.get(OptimisationStudy, study_id)
        assert parked is not None
        assert parked.state == "running"  # parked, not failed

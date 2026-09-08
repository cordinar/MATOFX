"""Phase 5 tests — the ``surrogate`` run mode + rule-7 attachment validation (ADR-0009 §6.2/§3.3).

A real (tiny) model bundle + version is seeded so the in-process
:class:`SurrogateRunner` and the surrogate-mode runtime are exercised
end-to-end with zero real jobs. Rule-7 attachment validation is covered
via ``OptimisationService.configure``; ``surrogate_assisted_real`` is
confirmed deferred.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job
from matofx.models.bundle import write_scalar_bundle
from matofx.models.orm import SurrogateModel, SurrogateModelVersion
from matofx.models.service import make_model_service
from matofx.studies.optimisation import (
    OptimisationConfigError,
    OptimisationStudy,
    make_optimisation_service,
    validate_config,
)
from matofx.studies.optimisation.runtime import (
    OptimisationRuntime,
    StudyRef,
    SurrogateRunner,
    ToolJobRunner,
)
from matofx.studies.orm import StudyResult
from matofx.studies.surrogate import fit_surrogate
from matofx.tools.descriptor import InputField, OutputField, ToolDescriptor
from matofx.tools.orm import Tool, ToolVersion
from tests.studies.conftest import Basics

_FACTORS = [
    {"name": "x", "type": "float", "unit": "", "min": 0.0, "max": 10.0},
    {"name": "n", "type": "int", "unit": "", "min": 0, "max": 5},
]
_RESPONSES = [{"name": "mass", "kind": "scalar", "unit": "kg"}]


async def _seed_surrogate_version(
    factory: Any, basics: Basics, bundle_dir: Path, *, tool_alias: str | None = None
) -> int:
    """Fit a tiny ``mass`` surrogate, write its bundle, persist model + version."""
    cols = {"x": [0.0, 2.0, 4.0, 6.0, 8.0, 10.0], "n": [0, 1, 2, 3, 4, 5]}
    mass_values = [x + n for x, n in zip(cols["x"], cols["n"], strict=True)]
    surrogate = fit_surrogate(cols, mass_values, kind="gp")
    sha = write_scalar_bundle(
        bundle_dir, {"mass": surrogate}, factors=_FACTORS, responses=_RESPONSES
    )
    async with session_scope(factory) as session:
        model = SurrogateModel(
            name="mass-model",
            tool_alias=tool_alias or basics.tool_alias,
            scope="personal",
            owner_user_id=basics.user_id,
            created_by_user_id=basics.user_id,
        )
        session.add(model)
        await session.flush()
        version = SurrogateModelVersion(
            surrogate_model_id=model.id,
            ordinal=1,
            kind="scalar",
            factors=_FACTORS,
            responses=_RESPONSES,
            source_kind="cohort",
            source_id=1,
            bundle_path=str(bundle_dir),
            bundle_sha256=sha,
            fitted_by_user_id=basics.user_id,
            publish_state="published",
        )
        session.add(version)
        await session.flush()
        return int(version.id)


def _surrogate_config(version_id: int, b: Basics, **extra: Any) -> dict[str, Any]:
    config = {
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
        "strategy": "bo",
        "run_mode": "surrogate",
        "surrogate_model_version_id": version_id,
        "batch_size": 3,
        "stopping_criteria": [{"kind": "max_runs", "value": 9}],
        "random_seed": 1,
    }
    config.update(extra)
    return config


# --- SurrogateRunner + surrogate-mode runtime (DB) -------------------------


async def test_surrogate_runner_predicts(
    studies_engine: AsyncEngine, basics: Basics, tmp_path: Path
) -> None:
    factory = make_session_factory(studies_engine)
    version_id = await _seed_surrogate_version(factory, basics, tmp_path)
    runner = SurrogateRunner(factory, version_id)
    ref = StudyRef(
        study_id=1,
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        created_by_user_id=basics.user_id,
        response_names=("mass",),
    )
    evals = await runner.run_batch(
        ref=ref, candidates=[{"x": 3.0, "n": 1}, {"x": 7.0, "n": 4}], batch=0, rationales=[{}, {}]
    )
    assert len(evals) == 2
    assert all("mass" in e.responses and e.feasible and e.succeeded for e in evals)


async def test_runtime_surrogate_mode_runs_zero_jobs(
    studies_engine: AsyncEngine, basics: Basics, tmp_path: Path
) -> None:
    factory = make_session_factory(studies_engine)
    version_id = await _seed_surrogate_version(factory, basics, tmp_path)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = OptimisationStudy(
            project_id=basics.project_id,
            name="surrogate",
            target_kind="tool",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            config_json=_surrogate_config(version_id, basics),
            state="running",
            created_by_user_id=basics.user_id,
        )
        session.add(study)
        await session.flush()
        study_id = study.id

    # The injected ToolJobRunner must never be used in surrogate mode.
    runtime = OptimisationRuntime(
        factory,
        AuditWriter(signing_key=None, key_id=None),
        runner=ToolJobRunner(factory, AuditWriter(signing_key=None, key_id=None)),
    )
    await runtime.run_study(study_id)

    async with session_scope(factory) as session:
        done = await session.get(OptimisationStudy, study_id)
        assert done is not None
        assert done.state == "complete"
        assert done.stop_reason == "budget_exhausted"
        assert done.convergence_trace
        assert done.current_best_json is not None
        # Zero real child jobs — every evaluation was in-process.
        job_count = await session.scalar(
            select(func.count()).select_from(Job).where(Job.optimisation_study_id == study_id)
        )
        assert job_count == 0


# --- rule-7 attachment validation (via configure) --------------------------


async def _xn_tool(factory: Any, basics: Basics) -> int:
    """Seed an ``xn_tool`` whose descriptor matches the surrogate factors."""
    descriptor = ToolDescriptor(
        alias="xn_tool",
        name="XN",
        version="1.0.0",
        description="x/n tool",
        main_script="static/main.py",
        inputs=[
            InputField(name="x", label="X", type="float", minimum=0.0, maximum=10.0),
            InputField(name="n", label="N", type="integer", minimum=0, maximum=5),
        ],
        outputs=[OutputField(name="mass", label="Mass", type="number")],
    ).model_dump()
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias="xn_tool",
                name="XN",
                version="1.0.0",
                state="released",
                descriptor=descriptor,
                bundle_path="bundle/xn",
                installed_by_user_id=basics.user_id,
            )
        )
        tv = ToolVersion(
            alias="xn_tool",
            version="1.0.0",
            descriptor_snapshot=descriptor,
            bundle_path="bundle/xn",
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=basics.user_id,
            change_summary="seed",
            changes={},
        )
        session.add(tv)
        await session.flush()
        return int(tv.id)


async def _draft_surrogate_study(
    factory: Any, basics: Basics, tv_id: int, version_id: int, **patch: Any
) -> int:
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = await service.create_draft(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="s",
            tool_alias="xn_tool",
            tool_version_id=tv_id,
        )
        config = {
            "factors": [
                {"name": "x", "type": "float", "min": 0.0, "max": 10.0},
                {"name": "n", "type": "int", "min": 0, "max": 5},
            ],
            "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
            "strategy": "bo",
            "run_mode": "surrogate",
            "surrogate_model_version_id": version_id,
            "batch_size": 3,
            "stopping_criteria": [{"kind": "max_runs", "value": 9}],
        }
        config.update(patch)
        await service.update_draft(
            session, actor_user_id=basics.user_id, study_id=study.id, config_patch=config
        )
        return int(study.id)


async def test_configure_accepts_valid_surrogate(
    studies_engine: AsyncEngine, basics: Basics, tmp_path: Path
) -> None:
    factory = make_session_factory(studies_engine)
    tv_id = await _xn_tool(factory, basics)
    version_id = await _seed_surrogate_version(factory, basics, tmp_path, tool_alias="xn_tool")
    study_id = await _draft_surrogate_study(factory, basics, tv_id, version_id)
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        configured = await service.configure(
            session, actor_user_id=basics.user_id, study_id=study_id
        )
        assert configured.state == "running"


async def test_configure_rejects_missing_surrogate_version(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(studies_engine)
    tv_id = await _xn_tool(factory, basics)
    study_id = await _draft_surrogate_study(factory, basics, tv_id, version_id=999999)
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        with pytest.raises(OptimisationConfigError, match="doesn't exist"):
            await service.configure(session, actor_user_id=basics.user_id, study_id=study_id)


async def test_configure_rejects_surrogate_missing_factor(
    studies_engine: AsyncEngine, basics: Basics, tmp_path: Path
) -> None:
    """A study factor the surrogate wasn't fit over is rejected (rule 7)."""
    factory = make_session_factory(studies_engine)
    tv_id = await _xn_tool(factory, basics)
    version_id = await _seed_surrogate_version(factory, basics, tmp_path, tool_alias="xn_tool")
    # Drop factor 'n' from the surrogate-covered set by adding an extra study
    # factor 'z' the version doesn't have — simulated by patching factors.
    study_id = await _draft_surrogate_study(
        factory,
        basics,
        tv_id,
        version_id,
        factors=[
            {"name": "x", "type": "float", "min": 0.0, "max": 10.0},
            {"name": "n", "type": "int", "min": 0, "max": 5},
        ],
    )
    # Tamper the version so it no longer covers 'n'.
    async with session_scope(factory) as session:
        version = await session.get(SurrogateModelVersion, version_id)
        version.factors = [{"name": "x", "type": "float", "min": 0.0, "max": 10.0}]
        await session.flush()
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        with pytest.raises(OptimisationConfigError, match="doesn't cover factor"):
            await service.configure(session, actor_user_id=basics.user_id, study_id=study_id)


def test_validate_config_defers_surrogate_assisted_real() -> None:
    descriptor = ToolDescriptor(
        alias="xn_tool",
        name="XN",
        version="1.0.0",
        description="d",
        main_script="static/main.py",
        inputs=[InputField(name="x", label="X", type="float", minimum=0.0, maximum=10.0)],
        outputs=[OutputField(name="mass", label="Mass", type="number")],
    )
    config = {
        "target": {"kind": "tool", "tool_alias": "xn_tool", "tool_version_id": 1},
        "factors": [{"name": "x", "type": "float", "min": 0.0, "max": 10.0}],
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": "bo",
        "run_mode": "surrogate_assisted_real",
        "surrogate_model_version_id": 1,
        "batch_size": 3,
        "seed": {"kind": "lhs", "n": 5},
        "stopping_criteria": [{"kind": "max_runs", "value": 10}],
    }
    with pytest.raises(OptimisationConfigError, match="isn't available yet"):
        validate_config(config, descriptor)


# --- publish-current-surrogate handoff (amendment) -------------------------


async def _seed_optimisation_runs(factory: Any, basics: Basics, n: int = 6) -> int:
    """An optimisation study with ``n`` completed real runs (study_result rows)."""
    config = {
        "target": {
            "kind": "tool",
            "tool_alias": basics.tool_alias,
            "tool_version_id": basics.tool_version_id,
        },
        "factors": [
            {"name": "x", "type": "float", "min": 0.0, "max": 10.0},
            {"name": "n", "type": "int", "min": 0, "max": 5},
        ],
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": "bo",
        "run_mode": "real",
        "stopping_criteria": [{"kind": "max_runs", "value": n}],
    }
    async with session_scope(factory) as session:
        study = OptimisationStudy(
            project_id=basics.project_id,
            name="optrun",
            target_kind="tool",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            config_json=config,
            state="complete",
            created_by_user_id=basics.user_id,
        )
        session.add(study)
        await session.flush()
        for i in range(n):
            job = Job(
                scenario_id=None,
                optimisation_study_id=study.id,
                name=f"run-{i}",
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
                    source_kind="optimisation",
                    source_id=study.id,
                    job_id=job.id,
                    scalars={"mass": float(i * 2 + i % 5)},
                    retained_files=[],
                )
            )
        await session.flush()
        return int(study.id)


async def test_publish_current_surrogate(
    studies_engine: AsyncEngine, basics: Basics, tmp_path: Path
) -> None:
    factory = make_session_factory(studies_engine)
    study_id = await _seed_optimisation_runs(factory, basics)
    audit = AuditWriter(signing_key=None, key_id=None)
    service = make_optimisation_service(audit)
    model_service = make_model_service(audit, models_root=tmp_path, environment="test")
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        version = await service.publish_current_surrogate(
            session,
            actor_user_id=basics.user_id,
            study_id=study_id,
            model_service=model_service,
        )
        # A governed registry version was fit from the study's own runs.
        assert version.id is not None
        assert version.source_kind == "optimisation"
        assert version.source_id == study_id

    async with session_scope(factory) as session:
        models = (await session.execute(select(SurrogateModel))).scalars().all()
        assert any(m.tool_alias == basics.tool_alias for m in models)

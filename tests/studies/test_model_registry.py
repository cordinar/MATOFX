"""Tests for the surrogate Model registry — ModelService core (ADR-0008 §5, Phase 5a).

Schema via the shared ``studies`` conftest (Alembic head includes 0061).
Covers create (personal/project) + bundle write + sha256-verified load +
runtime predict, the corrupt-bundle guard, the picker + retire visibility,
refit ordinals, and the global-scope deferral.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

# Register every ORM whose table ``approval_request`` carries an FK to, so
# its mapper resolves when this suite runs in isolation (the studies
# conftest does the same for studies/workflows; the full CI registers
# these transitively via their own suites).
import matofx.library.orm
import matofx.materials.orm
import matofx.vv.orm  # noqa: F401  -- registers tool/job_vv_record
from matofx.approvals import make_approval_service
from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job
from matofx.models import (
    ModelBundleCorruptError,
    ModelStateError,
    SurrogateModel,
    SurrogateModelVersion,
    make_model_service,
    register_as_tool,
)
from matofx.studies.orm import ParametricStudy
from matofx.tools import make_tool_service
from matofx.tools.orm import Tool
from matofx.users import make_password_hasher, make_user_service
from tests.studies.conftest import Basics


def _audit() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


def _service(tmp_path: Path):
    return make_model_service(_audit(), models_root=tmp_path / "store", environment="test")


def _study(basics: Basics) -> ParametricStudy:
    return ParametricStudy(
        project_id=basics.project_id,
        name="src",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        mode="grid",
        factor_spec={},
        sampling_params={},
        run_cap=100,
        created_by_user_id=basics.user_id,
    )


async def _seed_source(session, basics: Basics) -> int:
    """A parametric study + 6 completed children (x → y=2x, z=x²). Returns its id."""
    from matofx.studies.orm import StudyResult  # noqa: PLC0415

    study = _study(basics)
    session.add(study)
    await session.flush()
    for x in range(1, 7):
        job = Job(
            parametric_study_id=study.id,
            scenario_id=None,
            name="run",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            state="complete",
            inputs={"x": float(x)},
            created_by_user_id=basics.user_id,
        )
        session.add(job)
        await session.flush()
        session.add(
            StudyResult(
                source_kind="parametric",
                source_id=study.id,
                job_id=job.id,
                scalars={"y": 2.0 * x, "z": float(x * x)},
                retained_files=[],
            )
        )
    await session.flush()
    return int(study.id)


async def test_create_personal_writes_verified_bundle(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        model, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="Heat sink GP",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=source_id,
            response_names=["y", "z"],
        )
        assert model.scope == "personal"
        assert model.published_version_id == version.id
        assert version.ordinal == 1
        assert version.publish_state == "published"
        assert version.n_training_points == 6
        assert version.bundle_sha256 not in ("", "(pending)")
        assert (Path(version.bundle_path) / "manifest.json").is_file()

        # Load + sha256 verify + predict at a training point.
        runtime = svc.load_for_runtime(version, version.bundle_sha256)
        pred = runtime.predict({"x": 3.0})
        assert abs(pred["y"] - 6.0) < 1.0  # y = 2x
        assert abs(pred["z"] - 9.0) < 2.0  # z = x²


async def test_load_with_wrong_sha_raises(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        _, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="m",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=source_id,
            response_names=["y"],
        )
        with pytest.raises(ModelBundleCorruptError):
            svc.load_for_runtime(version, "0" * 64)


async def test_picker_and_retire(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        model, _, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="m",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=source_id,
            response_names=["y"],
        )
        picked = await svc.list_for_picker(
            session, actor_user_id=basics.user_id, tool_alias=basics.tool_alias
        )
        assert model.id in {m.id for m in picked}

        await svc.retire(session, actor_user_id=basics.user_id, model_id=model.id)
        after = await svc.list_for_picker(
            session, actor_user_id=basics.user_id, tool_alias=basics.tool_alias
        )
        assert model.id not in {m.id for m in after}  # retired → hidden


async def test_refit_mints_new_ordinal(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        model, v1, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="m",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=source_id,
            response_names=["y"],
        )
        v2 = await svc.refit(session, actor_user_id=basics.user_id, model_id=model.id)
        assert v2.ordinal == v1.ordinal + 1
        assert v2.id != v1.id
        await session.refresh(model)
        assert model.published_version_id == v2.id
        # Both versions persist (append-only).
        from sqlalchemy import select  # noqa: PLC0415

        rows = (
            (
                await session.execute(
                    select(SurrogateModelVersion).where(
                        SurrogateModelVersion.surrogate_model_id == model.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {r.ordinal for r in rows} == {1, 2}


async def test_global_scope_deferred(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        with pytest.raises(ModelStateError, match="global"):
            await svc.create_model(
                session,
                actor_user_id=basics.user_id,
                name="m",
                tool_alias=basics.tool_alias,
                scope="global",
                source_kind="parametric",
                source_id=source_id,
                response_names=["y"],
            )


async def _personal_model(svc, session, basics):
    source_id = await _seed_source(session, basics)
    model, _, _ = await svc.create_model(
        session,
        actor_user_id=basics.user_id,
        name="m",
        tool_alias=basics.tool_alias,
        scope="personal",
        source_kind="parametric",
        source_id=source_id,
        response_names=["y"],
    )
    return model


async def test_update_metadata(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        model = await _personal_model(svc, session, basics)
        updated = await svc.update_metadata(
            session,
            actor_user_id=basics.user_id,
            model_id=model.id,
            name="  Renamed  ",
            tags=["thermal", "v1"],
        )
        assert updated.name == "Renamed"
        assert updated.tags == ["thermal", "v1"]


async def test_set_scope_personal_to_project_and_back(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        model = await _personal_model(svc, session, basics)
        promoted = await svc.set_scope(
            session,
            actor_user_id=basics.user_id,
            model_id=model.id,
            new_scope="project",
            project_id=basics.project_id,
        )
        assert promoted.scope == "project"
        assert promoted.project_id == basics.project_id
        back = await svc.set_scope(
            session, actor_user_id=basics.user_id, model_id=model.id, new_scope="personal"
        )
        assert back.scope == "personal"
        assert back.project_id is None


async def test_set_scope_global_rejected(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        model = await _personal_model(svc, session, basics)
        with pytest.raises(ModelStateError, match="global"):
            await svc.set_scope(
                session, actor_user_id=basics.user_id, model_id=model.id, new_scope="global"
            )


async def test_approval_target_surrogate_model_version(studies_engine, basics) -> None:
    """The §5.5 widening: approval_request accepts a surrogate-model-version target."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        model = SurrogateModel(
            name="global m",
            tags=[],
            tool_alias=basics.tool_alias,
            scope="global",
            project_id=None,
            owner_user_id=basics.user_id,
            state="pending",
            created_by_user_id=basics.user_id,
        )
        session.add(model)
        await session.flush()
        version = SurrogateModelVersion(
            surrogate_model_id=model.id,
            ordinal=1,
            kind="scalar",
            factors=[],
            responses=[{"name": "y", "kind": "scalar"}],
            source_kind="parametric",
            source_id=1,
            tool_version_ids_at_fit=[basics.tool_version_id],
            n_training_points=6,
            bundle_path="x",
            bundle_sha256="x",
            quality={},
            publish_state="in_review",
            fitted_by_user_id=basics.user_id,
        )
        session.add(version)
        await session.flush()

        approvals = make_approval_service(_audit())
        request = await approvals.create_request(
            session,
            surrogate_model_version_id=version.id,
            kind="pre",
            requested_by_user_id=basics.user_id,
            eligible_approvers=[],
        )
        assert request.surrogate_model_version_id == version.id
        assert request.status == "pending"

        # Exactly-one-target: a second target alongside it is rejected.
        with pytest.raises(ValueError, match="exactly one"):
            await approvals.create_request(
                session,
                surrogate_model_version_id=version.id,
                job_id=999,
                kind="pre",
                requested_by_user_id=basics.user_id,
                eligible_approvers=[],
            )


async def _make_user(session, username: str):
    auth = AuthSettings(
        argon2_time_cost=1, argon2_memory_cost_kib=8192, argon2_parallelism=1, password_min_length=8
    )
    svc = make_user_service(auth, _audit(), hasher=make_password_hasher(auth))
    return await svc.create_user(
        session,
        username=username,
        email=f"{username}@example.com",
        password="checkerpass",
        full_name=username.title(),
        role="regular_user",
    )


async def _global_draft(svc, session, basics):
    source_id = await _seed_source(session, basics)
    model, version, _ = await svc.create_model(
        session,
        actor_user_id=basics.user_id,
        name="global m",
        tool_alias=basics.tool_alias,
        scope="global",
        source_kind="parametric",
        source_id=source_id,
        response_names=["y"],
        is_curator=True,  # builder holds MODELS_CURATE
    )
    return model, version


async def test_global_create_requires_curator(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        with pytest.raises(ModelStateError, match="Model Curator"):
            await svc.create_model(
                session,
                actor_user_id=basics.user_id,
                name="m",
                tool_alias=basics.tool_alias,
                scope="global",
                source_kind="parametric",
                source_id=source_id,
                response_names=["y"],
                is_curator=False,
            )


async def test_two_gate_global_release(studies_engine, basics, tmp_path) -> None:
    """draft → in_review → (checker) checked → (su/admin) published (§6.2/§6.3)."""
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        checker = await _make_user(session, "carol")
        model, version = await _global_draft(svc, session, basics)
        assert model.state == "pending"
        assert version.publish_state == "draft"
        assert model.published_version_id is None
        # A pending global model isn't pickable.
        picked = await svc.list_for_picker(
            session, actor_user_id=basics.user_id, tool_alias=basics.tool_alias
        )
        assert model.id not in {m.id for m in picked}

        # Gate 1 — builder submits, nominating the checker.
        await svc.submit_for_review(
            session,
            actor_user_id=basics.user_id,
            version_id=version.id,
            approval_required=True,
            checker_user_id=checker.id,
            checker_username=checker.username,
        )
        await session.refresh(version)
        assert version.publish_state == "in_review"

        # The checker approves → checked.
        from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

        req = (
            (
                await session.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.surrogate_model_version_id == version.id
                    )
                )
            )
            .scalars()
            .one()
        )
        _, checked = await svc.decide_check(
            session,
            request_id=req.id,
            decider_user_id=checker.id,
            decider_username=checker.username,
            decision="approve",
            decider_is_curator=True,
            decider_is_admin=False,
        )
        assert checked.publish_state == "checked"

        # Gate 2 — su/admin releases → published + pickable.
        released = await svc.release_version(
            session, actor_user_id=basics.user_id, version_id=version.id, is_admin=True
        )
        assert released.publish_state == "published"
        await session.refresh(model)
        assert model.state == "active"
        assert model.published_version_id == version.id
        picked2 = await svc.list_for_picker(
            session, actor_user_id=basics.user_id, tool_alias=basics.tool_alias
        )
        assert model.id in {m.id for m in picked2}


async def test_release_requires_admin(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        _, version = await _global_draft(svc, session, basics)
        # Toggle OFF path: builder (curator) checks directly.
        await svc.submit_for_review(
            session,
            actor_user_id=basics.user_id,
            version_id=version.id,
            approval_required=False,
            builder_is_curator=True,
        )
        await session.refresh(version)
        assert version.publish_state == "checked"
        # A non-admin can't release.
        with pytest.raises(ModelStateError, match="super-user"):
            await svc.release_version(
                session, actor_user_id=basics.user_id, version_id=version.id, is_admin=False
            )


async def test_register_as_tool(studies_engine, basics, tmp_path) -> None:
    """A fitted version exports to an installed tool in the ``test`` state (§7.6)."""
    svc = _service(tmp_path)
    tool_service = make_tool_service(_audit())
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        model, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="Heat sink GP",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=source_id,
            response_names=["y"],
        )
        tool = await register_as_tool(
            session,
            tool_service=tool_service,
            audit=_audit(),
            model=model,
            version=version,
            actor_user_id=basics.user_id,
            models_root=tmp_path / "store",
            environment="test",
        )
        assert tool.alias == f"surrogate_{basics.tool_alias}_{model.id}"

        fetched = await session.get(Tool, tool.id)
        assert fetched is not None
        assert fetched.state == "test"  # promoted new → test
        desc = fetched.descriptor
        assert [i["name"] for i in desc["inputs"]] == ["x"]
        out_names = [o["name"] for o in desc["outputs"]]
        assert "y" in out_names
        assert "y_std" in out_names  # GP uncertainty sibling
        # The orchestrator was written to the bundle dir.
        bundle = tmp_path / "store" / "exported_tools" / "test" / tool.alias
        assert (bundle / "surrogate_orchestrator.py").is_file()


async def test_register_as_tool_carries_source_labels(studies_engine, basics, tmp_path) -> None:
    """The exported tool's input/output *labels* come from the source tool (§7.6).

    Names are identifiers (the predict mapping); labels are UX. A user
    reading the surrogate's form should see "Fin height", not "fin_h".
    """
    svc = _service(tmp_path)
    tool_service = make_tool_service(_audit())
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        # Give the source 'demo' tool real labels for the factor + response.
        demo = (
            await session.execute(select(Tool).where(Tool.alias == basics.tool_alias))
        ).scalar_one()
        demo.descriptor = {
            "alias": basics.tool_alias,
            "inputs": [{"name": "x", "label": "X value", "type": "float", "unit": "mm"}],
            "outputs": [{"name": "y", "label": "Y output", "type": "number", "unit": "kg"}],
        }
        await session.flush()

        model, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="Labelled GP",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=source_id,
            response_names=["y"],
        )
        tool = await register_as_tool(
            session,
            tool_service=tool_service,
            audit=_audit(),
            model=model,
            version=version,
            actor_user_id=basics.user_id,
            models_root=tmp_path / "store",
            environment="test",
        )
        desc = (await session.get(Tool, tool.id)).descriptor
        x_in = next(i for i in desc["inputs"] if i["name"] == "x")
        assert x_in["label"] == "X value"
        assert x_in["unit"] == "mm"
        y_out = next(o for o in desc["outputs"] if o["name"] == "y")
        assert y_out["label"] == "Y output"
        assert y_out["unit"] == "kg"
        y_std = next(o for o in desc["outputs"] if o["name"] == "y_std")
        assert y_std["label"] == "Y output (1-sigma)"


async def test_gbm_model_records_method_and_omits_std_outputs(
    studies_engine, basics, tmp_path
) -> None:
    """A non-GP fit records its method + CV pairs, and exports without ± outputs.

    ``gbm`` (and ``poly``) have no uncertainty, so the exported tool must
    not declare ``<response>_std`` outputs that would never be filled
    (ADR-0008 §4). The CV ``actual``/``predicted`` pairs are stored for the
    detail page's parity plot regardless of method.
    """
    svc = _service(tmp_path)
    tool_service = make_tool_service(_audit())
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        model, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="GBM model",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=source_id,
            response_names=["y"],
            surrogate_kind="gbm",
        )
        assert version.surrogate_method == "gbm"
        # CV pairs captured for the parity plot.
        assert version.quality["y"]["cv_actual"]
        assert version.quality["y"]["cv_predicted"]

        tool = await register_as_tool(
            session,
            tool_service=tool_service,
            audit=_audit(),
            model=model,
            version=version,
            actor_user_id=basics.user_id,
            models_root=tmp_path / "store",
            environment="test",
        )
        out_names = [o["name"] for o in (await session.get(Tool, tool.id)).descriptor["outputs"]]
        assert "y" in out_names
        assert "y_std" not in out_names  # gbm has no uncertainty → no ± output


async def test_register_as_tool_splits_varied_and_held_constant_inputs(
    studies_engine, basics, tmp_path
) -> None:
    """The exported tool's form splits varied factors from held-constant inputs (§7.6)."""
    from matofx.studies.orm import StudyResult  # noqa: PLC0415

    svc = _service(tmp_path)
    tool_service = make_tool_service(_audit())
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        # A study where 'x' varies but 'air_velocity' is held at 3.0.
        study = _study(basics)
        session.add(study)
        await session.flush()
        for x in range(1, 7):
            job = Job(
                parametric_study_id=study.id,
                scenario_id=None,
                name="run",
                tool_alias=basics.tool_alias,
                tool_version_id=basics.tool_version_id,
                state="complete",
                inputs={"x": float(x), "air_velocity": 3.0},
                created_by_user_id=basics.user_id,
            )
            session.add(job)
            await session.flush()
            session.add(
                StudyResult(
                    source_kind="parametric",
                    source_id=study.id,
                    job_id=job.id,
                    scalars={"y": 2.0 * x},
                    retained_files=[],
                )
            )
        await session.flush()

        model, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="Held GP",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=study.id,
            response_names=["y"],
        )
        # The held constant was captured on the version.
        assert version.input_constants == {"air_velocity": 3.0}

        tool = await register_as_tool(
            session,
            tool_service=tool_service,
            audit=_audit(),
            model=model,
            version=version,
            actor_user_id=basics.user_id,
            models_root=tmp_path / "store",
            environment="test",
        )
        desc = (await session.get(Tool, tool.id)).descriptor
        groups = {g["name"] for g in desc.get("input_groups", [])}
        assert groups == {"varied", "held_constant"}
        x_in = next(i for i in desc["inputs"] if i["name"] == "x")
        assert x_in["group"] == "varied"
        held = next(i for i in desc["inputs"] if i["name"] == "air_velocity")
        assert held["group"] == "held_constant"
        assert held["default"] == 3.0


async def test_register_as_tool_rejects_double_export(studies_engine, basics, tmp_path) -> None:
    """A second export of the same model is a clear error (re-export is a follow-up)."""
    svc = _service(tmp_path)
    tool_service = make_tool_service(_audit())
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        model, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="m",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=source_id,
            response_names=["y"],
        )
        kwargs = {
            "tool_service": tool_service,
            "audit": _audit(),
            "model": model,
            "version": version,
            "actor_user_id": basics.user_id,
            "models_root": tmp_path / "store",
            "environment": "test",
        }
        await register_as_tool(session, **kwargs)
        with pytest.raises(ModelStateError, match="already exported"):
            await register_as_tool(session, **kwargs)


async def test_model_curator_role_seeded(studies_engine) -> None:
    """Migration 0062 seeds the model_curator capability role + models.curate."""
    from sqlalchemy import text  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        rid = (
            await session.execute(text("SELECT id, kind FROM role WHERE name = 'model_curator'"))
        ).fetchone()
        assert rid is not None
        assert rid[1] == "capability"
        perms = {
            r[0]
            for r in (
                await session.execute(
                    text("SELECT permission FROM role_permission WHERE role_id = :rid").bindparams(
                        rid=rid[0]
                    )
                )
            ).fetchall()
        }
        assert "models.curate" in perms

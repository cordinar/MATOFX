"""Field surrogates through the Model registry (ADR-0008 §4.3/§5, Phase 4).

End-to-end at the service level: a parametric study whose children retain
nodal-field ``.npy`` snapshots is fitted into a governed ``kind='field'``
``surrogate_model_version`` (POD bundle written + sha-verified), the
predicted field is read back through ``field_predict``, refit mints a new
field ordinal, and ``resolve_field_spec`` routes by descriptor.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import numpy as np
import pytest

# Register ORMs whose tables approval_request FKs to (isolation-run safety),
# mirroring test_model_registry.
import matofx.library.orm
import matofx.materials.orm
import matofx.vv.orm  # noqa: F401
from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job
from matofx.models import make_model_service
from matofx.models.errors import ModelConfigError
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.studies.surrogate.ingest import FieldSpec
from matofx.tools.exec_orm import StorageLocation
from matofx.tools.orm import ToolVersion
from tests.studies.conftest import Basics

_N_NODES = 18


def _service(tmp_path: Path):
    return make_model_service(
        AuditWriter(signing_key=None, key_id=None),
        models_root=tmp_path / "store",
        environment="test",
    )


def _modes() -> tuple[np.ndarray, np.ndarray]:
    t = np.linspace(0.0, np.pi, _N_NODES)
    return np.sin(t), np.cos(t)


def _field(x: float) -> np.ndarray:
    mode1, mode2 = _modes()
    return x * mode1 + 0.5 * x * mode2


async def _seed_field_study(session, basics: Basics, mount: Path) -> int:
    """A study + 6 children each retaining one ``.npy`` field snapshot."""
    loc = StorageLocation(name="share", mount_path=str(mount))
    session.add(loc)
    await session.flush()
    study = ParametricStudy(
        project_id=basics.project_id,
        name="field-src",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        mode="grid",
        factor_spec={},
        sampling_params={},
        run_cap=100,
        created_by_user_id=basics.user_id,
    )
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
            storage_location_id=int(loc.id),
            working_dir_relative=f"job_{x:05}",
            created_by_user_id=basics.user_id,
        )
        session.add(job)
        await session.flush()
        outputs_dir = mount / job.working_dir_relative / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        np.save(outputs_dir / "temperature.npy", _field(float(x)))
        session.add(
            StudyResult(
                source_kind="parametric",
                source_id=study.id,
                job_id=job.id,
                scalars={},
                retained_files=[
                    {"kind": "field", "name": "temperature", "filename": "outputs/temperature.npy"}
                ],
            )
        )
    await session.flush()
    return int(study.id)


async def test_create_field_model_writes_field_bundle(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)
    mount = tmp_path / "share"
    mount.mkdir()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_field_study(session, basics, mount)
        model, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="Temperature field",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=source_id,
            response_names=["temperature"],
            field_spec=FieldSpec(response_name="temperature", file_format="npy"),
        )
        assert version.kind == "field"
        assert version.n_training_points == 6
        assert version.bundle_sha256 not in ("", "(pending)")
        assert (Path(version.bundle_path) / "field" / "mu.npy").is_file()
        assert version.responses[0]["name"] == "temperature"
        assert version.responses[0]["kind"] == "field"

        # Predict the field back through the service (sha-verified load).
        result = await svc.field_predict(
            session, actor_user_id=basics.user_id, version_id=version.id, point={"x": 3.0}
        )
        assert result.n_nodes == _N_NODES
        assert len(result.field) == _N_NODES
        assert result.field_std is not None  # GP-backed
        assert result.has_geometry is False  # .npy has no mesh geometry
        expected = _field(3.0)
        rel = float(np.linalg.norm(np.array(result.field) - expected) / (np.linalg.norm(expected)))
        assert rel < 0.1

        # Refit mints a second field ordinal.
        v2 = await svc.refit(session, actor_user_id=basics.user_id, model_id=model.id)
        assert v2.kind == "field"
        assert v2.ordinal == 2


async def test_create_combined_model_writes_combined_bundle(
    studies_engine, basics, tmp_path
) -> None:
    """A study with a scalar + a field output fits one ``combined`` version (ADR-0018)."""
    from matofx.models.bundle import load_combined_bundle  # noqa: PLC0415

    svc = _service(tmp_path)
    mount = tmp_path / "share"
    mount.mkdir()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        loc = StorageLocation(name="share", mount_path=str(mount))
        session.add(loc)
        await session.flush()
        study = ParametricStudy(
            project_id=basics.project_id,
            name="combined-src",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            mode="grid",
            factor_spec={},
            sampling_params={},
            run_cap=100,
            created_by_user_id=basics.user_id,
        )
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
                storage_location_id=int(loc.id),
                working_dir_relative=f"job_{x:05}",
                created_by_user_id=basics.user_id,
            )
            session.add(job)
            await session.flush()
            outputs_dir = mount / job.working_dir_relative / "outputs"
            outputs_dir.mkdir(parents=True, exist_ok=True)
            np.save(outputs_dir / "temperature.npy", _field(float(x)))
            session.add(
                StudyResult(
                    source_kind="parametric",
                    source_id=study.id,
                    job_id=job.id,
                    scalars={"peak": 2.0 * x},
                    retained_files=[
                        {
                            "kind": "field",
                            "name": "temperature",
                            "filename": "outputs/temperature.npy",
                        }
                    ],
                )
            )
        await session.flush()

        model, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="Combined model",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=study.id,
            response_names=["peak"],
            field_specs=[FieldSpec(response_name="temperature", file_format="npy")],
        )
        assert version.kind == "combined"
        assert {r["name"] for r in version.responses} == {"peak", "temperature"}
        bundle = Path(version.bundle_path)
        assert (bundle / "scalar").is_dir()
        assert (bundle / "field" / "temperature" / "mu.npy").is_file()
        # The single combined sha covers both subtrees and round-trips.
        scalars, fields = load_combined_bundle(bundle, version.bundle_sha256)
        assert set(scalars) == {"peak"}
        assert set(fields) == {"temperature"}

        # field_predict works on the combined version (ADR-0018 slice 4).
        result = await svc.field_predict(
            session,
            actor_user_id=basics.user_id,
            version_id=version.id,
            point={"x": 3.0},
            response_name="temperature",
        )
        assert result.n_nodes == _N_NODES
        assert len(result.field) == _N_NODES

        # Refit reproduces the combined shape.
        v2 = await svc.refit(session, actor_user_id=basics.user_id, model_id=model.id)
        assert v2.kind == "combined"
        assert v2.ordinal == 2


async def test_field_predict_rejects_scalar_version(studies_engine, basics, tmp_path) -> None:
    """``field_predict`` refuses a scalar version with a clear message."""
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = ParametricStudy(
            project_id=basics.project_id,
            name="scalar-src",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            mode="grid",
            factor_spec={},
            sampling_params={},
            run_cap=100,
            created_by_user_id=basics.user_id,
        )
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
                    scalars={"y": 2.0 * x},
                    retained_files=[],
                )
            )
        await session.flush()
        _, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="scalar",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=int(study.id),
            response_names=["y"],
        )
        with pytest.raises(ModelConfigError, match="not a field surrogate"):
            await svc.field_predict(
                session, actor_user_id=basics.user_id, version_id=version.id, point={"x": 3.0}
            )


async def test_resolve_field_spec_reads_descriptor(studies_engine, basics, tmp_path) -> None:
    """``resolve_field_spec`` returns a spec for a field output, None for scalar."""
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        descriptor = {
            "alias": "demo",
            "outputs": [
                {
                    "name": "temperature",
                    "type": "file",
                    "file_type": "field",
                    "filename": "outputs/t.vtu",
                    "field_format": "vtu",
                    "field_node_array_name": "Temperature",
                    "field_timesteps": "outputs/t_*.vtu",
                },
                {"name": "t_max", "type": "number", "file_type": None},
            ],
        }
        # A newer ToolVersion for the same alias the basics fixture seeded.
        session.add(
            ToolVersion(
                alias=basics.tool_alias,
                version="2.0",
                descriptor_snapshot=descriptor,
                bundle_path="bundle/demo2",
                created_at=_dt.datetime.now(tz=_dt.UTC),
                created_by_user_id=basics.user_id,
                change_summary="field output",
                changes={},
            )
        )
        await session.flush()

        spec = await svc.resolve_field_spec(
            session, tool_alias=basics.tool_alias, response_name="temperature"
        )
        assert spec is not None
        assert spec.file_format == "vtu"
        assert spec.node_array_name == "Temperature"
        assert spec.timesteps_glob == "outputs/t_*.vtu"
        assert spec.time_resolved

        assert (
            await svc.resolve_field_spec(
                session, tool_alias=basics.tool_alias, response_name="t_max"
            )
            is None
        )


async def test_vtp_field_model_has_3d_geometry(studies_engine, basics, tmp_path) -> None:
    """A geometry-bearing (.vtp) ensemble fits, captures a reference mesh, and the
    viewer geometry endpoint returns an aligned surface (exercises the pyvista
    ingest path end-to-end)."""
    import pyvista as pv  # noqa: PLC0415

    svc = _service(tmp_path)
    mount = tmp_path / "share"
    mount.mkdir()
    base = pv.Sphere(theta_resolution=10, phi_resolution=10)
    z = np.asarray(base.points[:, 2], dtype=float)  # per-node pattern
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        loc = StorageLocation(name="share", mount_path=str(mount))
        session.add(loc)
        await session.flush()
        study = ParametricStudy(
            project_id=basics.project_id,
            name="vtp-src",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            mode="grid",
            factor_spec={},
            sampling_params={},
            run_cap=100,
            created_by_user_id=basics.user_id,
        )
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
                storage_location_id=int(loc.id),
                working_dir_relative=f"job_{x:05}",
                created_by_user_id=basics.user_id,
            )
            session.add(job)
            await session.flush()
            outputs_dir = mount / job.working_dir_relative / "outputs"
            outputs_dir.mkdir(parents=True, exist_ok=True)
            mesh = base.copy()
            mesh.point_data["temperature"] = float(x) * z  # same geometry, varying field
            mesh.save(outputs_dir / "temperature.vtp")
            session.add(
                StudyResult(
                    source_kind="parametric",
                    source_id=study.id,
                    job_id=job.id,
                    scalars={},
                    retained_files=[
                        {
                            "kind": "field",
                            "name": "temperature",
                            "filename": "outputs/temperature.vtp",
                        }
                    ],
                )
            )
        await session.flush()

        _, version, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="Sphere temperature",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=int(study.id),
            response_names=["temperature"],
            field_spec=FieldSpec(
                response_name="temperature", file_format="vtp", node_array_name="temperature"
            ),
        )
        assert version.kind == "field"
        # The bundle captured a reference mesh (geometry-bearing format).
        assert (Path(version.bundle_path) / "field").is_dir()

        predict = await svc.field_predict(
            session, actor_user_id=basics.user_id, version_id=version.id, point={"x": 3.0}
        )
        assert predict.has_geometry is True
        assert predict.n_nodes == base.n_points

        geo = await svc.field_geometry(session, actor_user_id=basics.user_id, version_id=version.id)
        assert geo.has_geometry is True
        assert len(geo.points) == base.n_points
        assert len(geo.i) == len(geo.j) == len(geo.k) > 0
        assert max([*geo.i, *geo.j, *geo.k]) < len(geo.points)

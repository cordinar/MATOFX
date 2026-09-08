"""Tests for field-snapshot ingestion (ADR-0008 §4.1/§4.3, Phase 4).

``collect_field_training_data`` joins each ``study_result`` to its member
job, locates the retained field snapshot(s) on disk, loads them, and
returns ``(factor_columns, snapshots)`` in ``fit_field_surrogate`` shape —
including the time-resolved ``(run, timestep)`` expansion with a reserved
``time`` factor. The snapshots are staged as real ``.npy`` files under a
StorageLocation mount so the on-disk resolver is exercised end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.studies.surrogate.field import fit_field_surrogate
from matofx.studies.surrogate.ingest import (
    TIME_FACTOR,
    FieldSpec,
    IngestError,
    collect_field_training_data,
)
from matofx.tools.exec_orm import StorageLocation
from tests.studies.conftest import Basics

_N_NODES = 16


def _modes() -> tuple[np.ndarray, np.ndarray]:
    t = np.linspace(0.0, np.pi, _N_NODES)
    return np.sin(t), np.cos(t)


def _field(x: float, scale: float = 1.0) -> np.ndarray:
    mode1, mode2 = _modes()
    return scale * (x * mode1 + 0.5 * x * mode2)


def _study(basics: Basics) -> ParametricStudy:
    return ParametricStudy(
        project_id=basics.project_id,
        name="field-ingest",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        mode="grid",
        factor_spec={},
        sampling_params={},
        run_cap=100,
        created_by_user_id=basics.user_id,
    )


async def _storage(session, mount: Path) -> int:
    loc = StorageLocation(name="t", mount_path=str(mount))
    session.add(loc)
    await session.flush()
    return int(loc.id)


async def _seed_field_run(
    session,
    basics: Basics,
    study_id: int,
    storage_id: int,
    mount: Path,
    *,
    x: float,
    snapshots: list[np.ndarray],
    filename: str = "outputs/temperature.npy",
    response_name: str = "temperature",
    validity: str = "valid",
    validity_reasons: list[dict[str, object]] | None = None,
) -> Job:
    """A completed child + its staged snapshot file(s) + study_result row.

    ``snapshots`` of length 1 is a steady-state field at ``outputs/...``;
    a longer list is a transient series written as ``outputs/temperature_NNNN.npy``.
    """
    job = Job(
        parametric_study_id=study_id,
        scenario_id=None,
        name="run",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        state="complete",
        inputs={"x": x, "label": "a"},
        storage_location_id=storage_id,
        working_dir_relative=f"job_{int(x):05}",
        created_by_user_id=basics.user_id,
    )
    session.add(job)
    await session.flush()
    outputs_dir = mount / job.working_dir_relative / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    if len(snapshots) == 1:
        np.save(mount / job.working_dir_relative / filename, snapshots[0])
    else:
        for i, snap in enumerate(snapshots):
            np.save(outputs_dir / f"temperature_{i:04}.npy", snap)
    session.add(
        StudyResult(
            source_kind="parametric",
            source_id=study_id,
            job_id=job.id,
            scalars={},
            retained_files=[{"kind": "field", "name": response_name, "filename": filename}],
            validity=validity,
            validity_reasons=validity_reasons or [],
        )
    )
    await session.flush()
    return job


async def test_collect_field_skips_invalid_result(
    studies_engine, basics: Basics, tmp_path: Path
) -> None:
    """A run flagged invalid (ADR-0017) is skipped before its mesh is loaded."""
    mount = tmp_path / "share"
    mount.mkdir()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        storage_id = await _storage(session, mount)
        for x in (1.0, 2.0, 3.0):
            await _seed_field_run(
                session, basics, study.id, storage_id, mount, x=x, snapshots=[_field(x)]
            )
        await _seed_field_run(
            session,
            basics,
            study.id,
            storage_id,
            mount,
            x=9.0,
            snapshots=[_field(9.0)],
            validity="invalid",
            validity_reasons=[{"output": "fi_max", "code": "above_max"}],
        )
        data = await collect_field_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            field_spec=FieldSpec(response_name="temperature", file_format="npy"),
        )
        assert data.n_complete == 3
        assert data.n_excluded_invalid == 1
        assert data.excluded_reason_counts == {"above_max": 1}


async def test_collect_field_drops_out_of_bounds_snapshot(
    studies_engine, basics: Basics, tmp_path: Path
) -> None:
    """A snapshot with a per-node value past value_max is dropped at ingest."""
    mount = tmp_path / "share"
    mount.mkdir()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        storage_id = await _storage(session, mount)
        for x in (1.0, 2.0, 3.0):
            await _seed_field_run(
                session, basics, study.id, storage_id, mount, x=x, snapshots=[_field(x)]
            )
        # A run whose field has an absurd node well past value_max.
        blown = _field(4.0)
        blown[0] = 1.0e9
        await _seed_field_run(
            session, basics, study.id, storage_id, mount, x=4.0, snapshots=[blown]
        )
        data = await collect_field_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            field_spec=FieldSpec(
                response_name="temperature", file_format="npy", value_min=-100.0, value_max=100.0
            ),
        )
        assert data.n_complete == 3  # the blown run is dropped whole
        assert data.n_excluded_invalid == 1
        assert data.excluded_reason_counts == {"field_out_of_bounds": 1}


async def test_collect_steady_field_and_fit(studies_engine, basics: Basics, tmp_path: Path) -> None:
    mount = tmp_path / "share"
    mount.mkdir()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        storage_id = await _storage(session, mount)
        study = _study(basics)
        session.add(study)
        await session.flush()
        xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        for x in xs:
            await _seed_field_run(
                session, basics, study.id, storage_id, mount, x=x, snapshots=[_field(x)]
            )

        data = await collect_field_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            field_spec=FieldSpec(response_name="temperature", file_format="npy"),
        )
        assert data.n_rows == 6
        assert data.n_complete == 6
        assert data.n_nodes == _N_NODES
        assert not data.time_resolved
        assert data.factor_columns["x"] == xs
        assert "label" not in data.factor_columns  # non-numeric input dropped
        assert TIME_FACTOR not in data.factor_columns
        assert len(data.snapshots) == 6
        assert data.reference_snapshot is not None
        # The collected (X, snapshots) fit a real field surrogate.
        surr = fit_field_surrogate(data.snapshots, data.factor_columns, mode_energy_cutoff=0.999)
        pred = surr.predict({"x": 4.0})
        expected = _field(4.0)
        rel = float(np.sqrt(np.mean((pred.field - expected) ** 2)) / (np.std(expected) + 1e-9))
        assert rel < 0.1


async def test_run_missing_snapshot_is_dropped(
    studies_engine, basics: Basics, tmp_path: Path
) -> None:
    mount = tmp_path / "share"
    mount.mkdir()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        storage_id = await _storage(session, mount)
        study = _study(basics)
        session.add(study)
        await session.flush()
        await _seed_field_run(
            session, basics, study.id, storage_id, mount, x=1.0, snapshots=[_field(1.0)]
        )
        # A run whose retained_files point at a file that was never written.
        job = Job(
            parametric_study_id=study.id,
            scenario_id=None,
            name="run",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            state="complete",
            inputs={"x": 2.0, "label": "a"},
            storage_location_id=storage_id,
            working_dir_relative="job_missing",
            created_by_user_id=basics.user_id,
        )
        session.add(job)
        await session.flush()
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

        data = await collect_field_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            field_spec=FieldSpec(response_name="temperature", file_format="npy"),
        )
        assert data.n_rows == 2  # both considered
        assert data.n_complete == 1  # the missing-file run dropped
        assert len(data.snapshots) == 1


async def test_time_resolved_field_adds_time_factor(
    studies_engine, basics: Basics, tmp_path: Path
) -> None:
    mount = tmp_path / "share"
    mount.mkdir()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        storage_id = await _storage(session, mount)
        study = _study(basics)
        session.add(study)
        await session.flush()
        # Each run is a 3-step transient: field grows linearly with the step.
        xs = [1.0, 2.0, 3.0, 4.0]
        for x in xs:
            series = [_field(x, scale=1.0 + 0.5 * step) for step in range(3)]
            await _seed_field_run(
                session,
                basics,
                study.id,
                storage_id,
                mount,
                x=x,
                snapshots=series,
                filename="outputs/temperature_0000.npy",
            )

        data = await collect_field_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            field_spec=FieldSpec(
                response_name="temperature",
                file_format="npy",
                timesteps_glob="outputs/temperature_*.npy",
            ),
        )
        assert data.time_resolved
        assert TIME_FACTOR in data.factor_columns
        # 4 runs by 3 timesteps = 12 aligned rows.
        assert len(data.snapshots) == 12
        assert data.factor_columns[TIME_FACTOR] == [0.0, 1.0, 2.0] * 4
        assert len(data.factor_columns["x"]) == 12

        surr = fit_field_surrogate(data.snapshots, data.factor_columns, mode_energy_cutoff=0.999)
        # 'time' is a fitted factor — prediction varies with t.
        early = surr.predict({"x": 2.0, TIME_FACTOR: 0.0}).field
        late = surr.predict({"x": 2.0, TIME_FACTOR: 2.0}).field
        assert float(np.abs(late - early).max()) > 1e-3


async def test_reserved_time_input_name_clashes(
    studies_engine, basics: Basics, tmp_path: Path
) -> None:
    mount = tmp_path / "share"
    mount.mkdir()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        storage_id = await _storage(session, mount)
        study = _study(basics)
        session.add(study)
        await session.flush()
        # A run whose inputs include a numeric 'time' — clashes with the reserved factor.
        job = Job(
            parametric_study_id=study.id,
            scenario_id=None,
            name="run",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            state="complete",
            inputs={"time": 5.0},
            storage_location_id=storage_id,
            working_dir_relative="job_00001",
            created_by_user_id=basics.user_id,
        )
        session.add(job)
        await session.flush()
        outputs_dir = mount / "job_00001" / "outputs"
        outputs_dir.mkdir(parents=True)
        np.save(outputs_dir / "temperature_0000.npy", _field(1.0))
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
                        "filename": "outputs/temperature_0000.npy",
                    }
                ],
            )
        )
        await session.flush()

        with pytest.raises(IngestError, match="reserved factor"):
            await collect_field_training_data(
                session,
                source_kind="parametric",
                source_id=study.id,
                field_spec=FieldSpec(
                    response_name="temperature",
                    file_format="npy",
                    timesteps_glob="outputs/temperature_*.npy",
                ),
            )

"""Tests for the AOS / HPC dispatch targets — routing by transport / scheduler."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.config import DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.secrets import InMemorySecretsStore
from matofx.tools.exec.aos_ssh import AosTargetError, SshAosTarget
from matofx.tools.exec.aos_winrm import WinRmAosTarget
from matofx.tools.exec.base import TargetContext
from matofx.tools.exec.dispatch import AosDispatchTarget, HpcDispatchTarget
from matofx.tools.exec.hpc_mshpc import MsHpcHpcTarget
from matofx.tools.exec.hpc_slurm import HpcTargetError, SlurmHpcTarget
from matofx.tools.exec_orm import AosHost, HpcCluster
from tests._migrated_db import sqlite_db_settings


def _alembic_config(url: str) -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "src" / "matofx" / "db" / "migrations"))
    cfg.cmd_opts = type("Args", (), {"x": [f"url={url}"]})()
    return cfg


@pytest.fixture
def db_settings(tmp_path: Path, migrated_template: Path) -> DbSettings:
    # Byte copy of the session-scoped migrated template rather than
    # running 90 migrations per test (see tests/_migrated_db.py).
    return sqlite_db_settings(migrated_template, tmp_path, "dispatch.db")


@pytest.fixture
async def engine(db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(db_settings)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _seed_cluster(factory, *, scheduler: str, transport: str) -> int:
    async with session_scope(factory) as session:
        cluster = HpcCluster(
            name=f"cluster-{scheduler}",
            scheduler=scheduler,
            head_node="head.invalid",
            transport=transport,
            port=22 if transport == "ssh" else 5986,
            username="svc",
            secret_ref="ref",
        )
        session.add(cluster)
        await session.flush()
        return int(cluster.id)


async def _seed_host(factory, *, os_name: str, transport: str) -> int:
    async with session_scope(factory) as session:
        host = AosHost(
            name=f"host-{transport}",
            hostname="host.invalid",
            os=os_name,
            transport=transport,
            port=22 if transport == "ssh" else 5986,
            username="svc",
            secret_ref="ref",
        )
        session.add(host)
        await session.flush()
        return int(host.id)


def _hpc(engine: AsyncEngine) -> HpcDispatchTarget:
    return HpcDispatchTarget(
        session_factory=make_session_factory(engine), secrets_store=InMemorySecretsStore()
    )


def _aos(engine: AsyncEngine) -> AosDispatchTarget:
    return AosDispatchTarget(
        session_factory=make_session_factory(engine), secrets_store=InMemorySecretsStore()
    )


# ----- HPC routing -----------------------------------------------------


@pytest.mark.asyncio
async def test_hpc_routes_slurm(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    cid = await _seed_cluster(factory, scheduler="slurm", transport="ssh")
    target = await _hpc(engine)._select(TargetContext(hpc_cluster_id=cid))
    assert isinstance(target, SlurmHpcTarget)


@pytest.mark.asyncio
async def test_hpc_routes_mshpc(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    cid = await _seed_cluster(factory, scheduler="mshpc", transport="winrm")
    target = await _hpc(engine)._select(TargetContext(hpc_cluster_id=cid))
    assert isinstance(target, MsHpcHpcTarget)


@pytest.mark.asyncio
async def test_hpc_missing_cluster_raises(engine: AsyncEngine) -> None:
    with pytest.raises(HpcTargetError, match="not found"):
        await _hpc(engine)._select(TargetContext(hpc_cluster_id=9999))


@pytest.mark.asyncio
async def test_hpc_no_cluster_id_raises(engine: AsyncEngine) -> None:
    with pytest.raises(HpcTargetError, match="no cluster assigned"):
        await _hpc(engine)._select(TargetContext())


# ----- AOS routing -----------------------------------------------------


@pytest.mark.asyncio
async def test_aos_routes_ssh(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    hid = await _seed_host(factory, os_name="linux", transport="ssh")
    target = await _aos(engine)._select(TargetContext(aos_host_id=hid))
    assert isinstance(target, SshAosTarget)


@pytest.mark.asyncio
async def test_aos_routes_winrm(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    hid = await _seed_host(factory, os_name="windows", transport="winrm")
    target = await _aos(engine)._select(TargetContext(aos_host_id=hid))
    assert isinstance(target, WinRmAosTarget)


@pytest.mark.asyncio
async def test_aos_missing_host_raises(engine: AsyncEngine) -> None:
    with pytest.raises(AosTargetError, match="not found"):
        await _aos(engine)._select(TargetContext(aos_host_id=9999))


@pytest.mark.asyncio
async def test_aos_no_host_id_raises(engine: AsyncEngine) -> None:
    with pytest.raises(AosTargetError, match="no host"):
        await _aos(engine)._select(TargetContext())


def test_dispatchers_have_correct_target_kind(engine: AsyncEngine) -> None:
    assert _hpc(engine).target_kind == "hpc"
    assert _aos(engine).target_kind == "aos"

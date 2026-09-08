"""Tests for the AosHostsService + HpcClustersService admin CRUD."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings, DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.tools.exec_orm import AosHost, HpcCluster
from matofx.tools.hosts_service import make_aos_hosts_service, make_hpc_clusters_service
from matofx.tools.paths_service import DuplicateNameError, NotFoundError
from matofx.users import make_password_hasher, make_user_service
from matofx.users.errors import PermissionDeniedError
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
    return sqlite_db_settings(migrated_template, tmp_path, "hosts.db")


@pytest.fixture
async def engine(db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(db_settings)
    try:
        yield eng
    finally:
        await eng.dispose()


def _audit() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


async def _seed_user(factory, *, role: str = "super_user", username: str = "root") -> int:
    auth = AuthSettings(
        argon2_time_cost=1, argon2_memory_cost_kib=8192, argon2_parallelism=1, password_min_length=8
    )
    service = make_user_service(auth, _audit(), hasher=make_password_hasher(auth))
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await service.create_user(
            session,
            username=username,
            email=f"{username}@example.com",
            password="securepassword1",
            full_name="Test User",
            role=role,
        )
        return user.id


# ----- AosHostsService -------------------------------------------------


@pytest.mark.asyncio
async def test_create_aos_host_derives_transport_and_default_port(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_aos_hosts_service(_audit())
    async with session_scope(factory) as session:
        row = await service.create(
            session,
            actor_user_id=uid,
            name="winbox",
            hostname="win.example",
            os="windows",
            username="svc",
            secret_ref="aos:winbox",
        )
        assert row.transport == "winrm"
        assert row.port == 5986
        assert row.max_concurrency == 1
        assert row.cpu_cores is None


@pytest.mark.asyncio
async def test_create_aos_host_linux_ssh(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_aos_hosts_service(_audit())
    async with session_scope(factory) as session:
        row = await service.create(
            session,
            actor_user_id=uid,
            name="linbox",
            hostname="lin.example",
            os="linux",
            username="svc",
            secret_ref="ref",
            max_concurrency=4,
            cpu_cores=16,
            memory_mb=32000,
        )
        assert row.transport == "ssh"
        assert row.port == 22
        assert (row.max_concurrency, row.cpu_cores, row.memory_mb) == (4, 16, 32000)


@pytest.mark.asyncio
async def test_create_aos_host_duplicate_name_raises(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_aos_hosts_service(_audit())
    async with session_scope(factory) as session:
        await service.create(
            session, actor_user_id=uid, name="dup", hostname="h", os="linux",
            username="u", secret_ref="r",
        )
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateNameError):
            await service.create(
                session, actor_user_id=uid, name="dup", hostname="h2", os="linux",
                username="u", secret_ref="r",
            )


@pytest.mark.asyncio
async def test_update_aos_host_changes_fields_and_transport(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_aos_hosts_service(_audit())
    async with session_scope(factory) as session:
        row = await service.create(
            session, actor_user_id=uid, name="h", hostname="old", os="linux",
            username="u", secret_ref="r",
        )
        rid = row.id
    async with session_scope(factory) as session:
        updated = await service.update(
            session, actor_user_id=uid, row_id=rid, hostname="new", os="windows",
            username="u2", secret_ref="r2", port=5986, max_concurrency=2,
            cpu_cores=8, memory_mb=None,
        )
        assert updated.hostname == "new"
        assert updated.transport == "winrm"  # re-derived from os
        assert updated.max_concurrency == 2
        assert updated.cpu_cores == 8


@pytest.mark.asyncio
async def test_set_active_toggles(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_aos_hosts_service(_audit())
    async with session_scope(factory) as session:
        row = await service.create(
            session, actor_user_id=uid, name="h", hostname="h", os="linux",
            username="u", secret_ref="r",
        )
        rid = row.id
    async with session_scope(factory) as session:
        await service.set_active(session, actor_user_id=uid, row_id=rid, is_active=False)
    async with session_scope(factory) as session:
        row2 = await session.get(AosHost, rid)
        assert row2 is not None
        assert row2.is_active is False


@pytest.mark.asyncio
async def test_update_missing_aos_host_raises(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_aos_hosts_service(_audit())
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await service.update(
                session, actor_user_id=uid, row_id=9999, hostname="x", os="linux",
                username="u", secret_ref="r", port=22, max_concurrency=1,
                cpu_cores=None, memory_mb=None,
            )


@pytest.mark.asyncio
async def test_aos_create_denied_for_regular_user(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    await _seed_user(factory)  # super_user (the first user must be super)
    regular = await _seed_user(factory, role="regular_user", username="bob")
    service = make_aos_hosts_service(_audit())
    async with session_scope(factory) as session:
        with pytest.raises(PermissionDeniedError):
            await service.create(
                session, actor_user_id=regular, name="h", hostname="h", os="linux",
                username="u", secret_ref="r",
            )


# ----- HpcClustersService ----------------------------------------------


@pytest.mark.asyncio
async def test_create_hpc_cluster_slurm_and_mshpc(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_hpc_clusters_service(_audit())
    async with session_scope(factory) as session:
        slurm = await service.create(
            session, actor_user_id=uid, name="slurm1", head_node="h", scheduler="slurm",
            username="u", secret_ref="r", default_queue="batch",
        )
        assert (slurm.transport, slurm.port, slurm.default_queue) == ("ssh", 22, "batch")
        mshpc = await service.create(
            session, actor_user_id=uid, name="mshpc1", head_node="h", scheduler="mshpc",
            username="u", secret_ref="r",
        )
        assert (mshpc.transport, mshpc.port) == ("winrm", 5986)


@pytest.mark.asyncio
async def test_update_hpc_cluster(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_hpc_clusters_service(_audit())
    async with session_scope(factory) as session:
        row = await service.create(
            session, actor_user_id=uid, name="c", head_node="old", scheduler="slurm",
            username="u", secret_ref="r",
        )
        rid = row.id
    async with session_scope(factory) as session:
        updated = await service.update(
            session, actor_user_id=uid, row_id=rid, head_node="new", scheduler="mshpc",
            username="u2", secret_ref="r2", port=5986, default_queue="gpu",
        )
        assert updated.head_node == "new"
        assert updated.transport == "winrm"
        assert updated.default_queue == "gpu"


@pytest.mark.asyncio
async def test_hpc_cluster_accounting_fields_round_trip(engine: AsyncEngine) -> None:
    """``account`` / ``wckey`` persist, and blank-out to NULL rather than "".

    They feed ``sbatch`` flags, so a whitespace-only value cleared from
    the admin form must not survive as a string — it would emit a flag
    with no argument.
    """
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_hpc_clusters_service(_audit())
    async with session_scope(factory) as session:
        row = await service.create(
            session, actor_user_id=uid, name="c", head_node="h", scheduler="slurm",
            username="u", secret_ref="r", account="cfd-team", wckey="matofx",
        )
        assert (row.account, row.wckey) == ("cfd-team", "matofx")
        rid = row.id
    async with session_scope(factory) as session:
        updated = await service.update(
            session, actor_user_id=uid, row_id=rid, head_node="h", scheduler="slurm",
            username="u", secret_ref="r", port=22, default_queue=None,
            account="   ", wckey=None,
        )
        assert updated.account is None
        assert updated.wckey is None


@pytest.mark.asyncio
async def test_hpc_cluster_accounting_defaults_to_none(engine: AsyncEngine) -> None:
    """Unset means unset — the submission then carries no accounting flag."""
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_hpc_clusters_service(_audit())
    async with session_scope(factory) as session:
        row = await service.create(
            session, actor_user_id=uid, name="plain", head_node="h", scheduler="slurm",
            username="u", secret_ref="r",
        )
        assert row.account is None
        assert row.wckey is None


@pytest.mark.asyncio
async def test_hpc_set_active_and_list(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    uid = await _seed_user(factory)
    service = make_hpc_clusters_service(_audit())
    async with session_scope(factory) as session:
        row = await service.create(
            session, actor_user_id=uid, name="c", head_node="h", scheduler="slurm",
            username="u", secret_ref="r",
        )
        rid = row.id
        await service.set_active(session, actor_user_id=uid, row_id=rid, is_active=False)
    async with session_scope(factory) as session:
        rows = await service.list_all(session)
        assert len(rows) == 1
        stored = await session.get(HpcCluster, rid)
        assert stored is not None
        assert stored.is_active is False

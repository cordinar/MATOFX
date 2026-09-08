"""Schema-level tests for the execution-backend additions (migration 0012).

Verifies:

- The five new registry tables exist and admit valid rows.
- ``aos_host`` / ``hpc_cluster`` CHECK constraints reject the bad
  combinations (invalid os, mismatched os/transport, out-of-range port).
- The ``tool`` cross-target CHECK constraints accept each valid
  combination and reject the disallowed ones (e.g. AOS-targeted tool
  must have ``aos_host_id`` set and ``hpc_cluster_id`` null).
- The ``job`` heartbeat-state CHECK pins the closed vocabulary.
- Default runtime settings are seeded with the agreed values.
- The ``administrator`` role gained the five admin-managed permissions
  and did NOT gain ``jobs.cancel_any`` (super-user-only).
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope

# ---------------------------------------------------------------------------
# Registry-table positive cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_location_round_trip(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO storage_location "
                "(name, mount_path, is_active, row_version, created_at, updated_at) "
                "VALUES (:n, :p, :a, :v, :c, :u)"
            ),
            {
                "n": "fsxdrv1",
                "p": r"\\fsxdrv1\matofx",
                "a": True,
                "v": 1,
                "c": "2026-05-04T00:00:00+00:00",
                "u": "2026-05-04T00:00:00+00:00",
            },
        )
        rows = (
            await session.execute(sa.text("SELECT mount_path FROM storage_location"))
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == r"\\fsxdrv1\matofx"


@pytest.mark.asyncio
async def test_python_env_round_trip(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO python_env "
                "(name, interpreter_path, is_active, row_version, created_at, updated_at) "
                "VALUES (:n, :p, :a, :v, :c, :u)"
            ),
            {
                "n": "matofx_core",
                "p": r"envs\matofx_core\Scripts\python.exe",
                "a": True,
                "v": 1,
                "c": "2026-05-04T00:00:00+00:00",
                "u": "2026-05-04T00:00:00+00:00",
            },
        )
        rows = (await session.execute(sa.text("SELECT name FROM python_env"))).fetchall()
        assert rows[0][0] == "matofx_core"


# ---------------------------------------------------------------------------
# AOS host CHECK constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aos_host_accepts_linux_ssh(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO aos_host "
                "(name, hostname, os, transport, port, username, secret_ref, is_active, "
                "row_version, created_at, updated_at) "
                "VALUES (:n, :h, :os, :t, :p, :u, :s, :a, :v, :c, :upd)"
            ),
            {
                "n": "aos1",
                "h": "aos1.local",
                "os": "linux",
                "t": "ssh",
                "p": 22,
                "u": "matofx",
                "s": "secrets://aos1-key",
                "a": True,
                "v": 1,
                "c": "2026-05-04T00:00:00+00:00",
                "upd": "2026-05-04T00:00:00+00:00",
            },
        )


_INSERT_AOS_HOST = sa.text(
    "INSERT INTO aos_host "
    "(name, hostname, os, transport, port, username, secret_ref, is_active, "
    "row_version, created_at, updated_at) "
    "VALUES (:n,'h',:os,:t,:p,'u','s',1,1,"
    "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
)


@pytest.mark.asyncio
async def test_aos_host_rejects_linux_winrm(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                _INSERT_AOS_HOST, {"n": "aos2", "os": "linux", "t": "winrm", "p": 22}
            )


@pytest.mark.asyncio
async def test_aos_host_rejects_unknown_os(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                _INSERT_AOS_HOST, {"n": "aos3", "os": "solaris", "t": "ssh", "p": 22}
            )


@pytest.mark.asyncio
async def test_aos_host_rejects_out_of_range_port(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                _INSERT_AOS_HOST, {"n": "aos4", "os": "linux", "t": "ssh", "p": 70_000}
            )


# ---------------------------------------------------------------------------
# HPC cluster CHECK constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hpc_cluster_accepts_slurm_ssh(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO hpc_cluster "
                "(name, scheduler, head_node, transport, port, username, secret_ref, "
                "is_active, row_version, created_at, updated_at) "
                "VALUES ('cluster1','slurm','login1.hpc',"
                "'ssh',22,'matofx','secrets://hpc1',1,1,"
                "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
            )
        )


@pytest.mark.asyncio
async def test_hpc_cluster_rejects_slurm_winrm(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    stmt = sa.text(
        "INSERT INTO hpc_cluster "
        "(name, scheduler, head_node, transport, port, username, secret_ref, "
        "is_active, row_version, created_at, updated_at) "
        "VALUES ('c2','slurm','h','winrm',5985,'u','s',1,1,"
        "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
    )
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await session.execute(stmt)


# ---------------------------------------------------------------------------
# Tool cross-target CHECK constraints
# ---------------------------------------------------------------------------


async def _seed_aos_and_hpc(session, user_id: int) -> tuple[int, int, int]:
    """Insert an AOS host and an HPC cluster; return (user_id, aos_id, hpc_id).

    ``user_id`` is provided by the standard ``seed_roles`` fixture, so
    we avoid touching the dense ``user`` table directly.
    """
    await session.execute(
        sa.text(
            "INSERT INTO aos_host "
            "(name, hostname, os, transport, port, username, secret_ref, is_active, "
            "row_version, created_at, updated_at) "
            "VALUES ('a','h','linux','ssh',22,'u','s',1,1,"
            "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
        )
    )
    aos_id = (await session.execute(sa.text("SELECT id FROM aos_host WHERE name='a'"))).scalar_one()
    await session.execute(
        sa.text(
            "INSERT INTO hpc_cluster "
            "(name, scheduler, head_node, transport, port, username, secret_ref, "
            "is_active, row_version, created_at, updated_at) "
            "VALUES ('c','slurm','h','ssh',22,'u','s',1,1,"
            "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
        )
    )
    hpc_id = (
        await session.execute(sa.text("SELECT id FROM hpc_cluster WHERE name='c'"))
    ).scalar_one()
    return user_id, aos_id, hpc_id


def _insert_tool_sql() -> str:
    return (
        "INSERT INTO tool "
        "(alias, name, version, state, descriptor, bundle_path, "
        "installed_by_user_id, execution_target, aos_host_id, hpc_cluster_id, "
        "python_env_id, requires_gpu, row_version, created_at, updated_at) "
        "VALUES (:alias, :alias, '1.0.0', 'new', :desc, '/tmp/x', :uid, "
        ":target, :aos, :hpc, NULL, 0, 1, "
        "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
    )


@pytest.mark.asyncio
async def test_tool_local_accepts_no_target_fks(
    tools_engine: AsyncEngine, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        user_id, _, _ = await _seed_aos_and_hpc(session, seed_roles["super_user"])
        await session.execute(
            sa.text(_insert_tool_sql()),
            {
                "alias": "local-tool",
                "desc": json.dumps({"alias": "local-tool"}),
                "uid": user_id,
                "target": "local",
                "aos": None,
                "hpc": None,
            },
        )


def _tool_params(
    alias: str, *, uid: int, target: str, aos: int | None, hpc: int | None
) -> dict[str, object]:
    return {
        "alias": alias,
        "desc": json.dumps({"alias": alias}),
        "uid": uid,
        "target": target,
        "aos": aos,
        "hpc": hpc,
    }


@pytest.mark.asyncio
async def test_tool_local_rejects_aos_fk(
    tools_engine: AsyncEngine, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        user_id, aos_id, _ = await _seed_aos_and_hpc(session, seed_roles["super_user"])
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text(_insert_tool_sql()),
                _tool_params("local-bad", uid=user_id, target="local", aos=aos_id, hpc=None),
            )


@pytest.mark.asyncio
async def test_tool_aos_requires_aos_fk(
    tools_engine: AsyncEngine, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        user_id, _, _ = await _seed_aos_and_hpc(session, seed_roles["super_user"])
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text(_insert_tool_sql()),
                _tool_params("aos-bad", uid=user_id, target="aos", aos=None, hpc=None),
            )


@pytest.mark.asyncio
async def test_tool_aos_rejects_hpc_fk(
    tools_engine: AsyncEngine, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        user_id, aos_id, hpc_id = await _seed_aos_and_hpc(session, seed_roles["super_user"])
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text(_insert_tool_sql()),
                _tool_params("aos-mixed", uid=user_id, target="aos", aos=aos_id, hpc=hpc_id),
            )


@pytest.mark.asyncio
async def test_tool_hpc_requires_hpc_fk(
    tools_engine: AsyncEngine, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        user_id, _, _ = await _seed_aos_and_hpc(session, seed_roles["super_user"])
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text(_insert_tool_sql()),
                _tool_params("hpc-bad", uid=user_id, target="hpc", aos=None, hpc=None),
            )


@pytest.mark.asyncio
async def test_tool_unknown_target_rejected(
    tools_engine: AsyncEngine, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        user_id, _, _ = await _seed_aos_and_hpc(session, seed_roles["super_user"])
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text(_insert_tool_sql()),
                _tool_params("weird", uid=user_id, target="rescale", aos=None, hpc=None),
            )


# ---------------------------------------------------------------------------
# Job heartbeat-state CHECK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_heartbeat_state_rejects_unknown(
    tools_engine: AsyncEngine, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        user_id, _, _ = await _seed_aos_and_hpc(session, seed_roles["super_user"])
        await session.execute(
            sa.text(_insert_tool_sql()),
            _tool_params("hb-tool", uid=user_id, target="local", aos=None, hpc=None),
        )
        await session.execute(
            sa.text(
                "INSERT INTO project (name, description, creator_user_id, state, version, "
                "created_at, updated_at) VALUES "
                "('p','',:uid,'active',1,'2026-05-04T00:00:00+00:00',"
                "'2026-05-04T00:00:00+00:00')"
            ),
            {"uid": user_id},
        )
        project_id = (
            await session.execute(sa.text("SELECT id FROM project WHERE name='p'"))
        ).scalar_one()
        await session.execute(
            sa.text(
                "INSERT INTO scenario (project_id, name, description, version, "
                "created_at, updated_at) VALUES (:p,'s','',1,"
                "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00')"
            ),
            {"p": project_id},
        )
        scenario_id = (
            await session.execute(sa.text("SELECT id FROM scenario WHERE name='s'"))
        ).scalar_one()

    failing_insert = sa.text(
        "INSERT INTO job (scenario_id, name, tool_alias, state, inputs, "
        "created_by_user_id, version, created_at, updated_at, heartbeat_state) "
        "VALUES (:s,'j','hb-tool','created','{}',:u,1,"
        "'2026-05-04T00:00:00+00:00','2026-05-04T00:00:00+00:00','banana')"
    )
    async with session_scope(factory) as session:
        with pytest.raises(IntegrityError):
            await session.execute(failing_insert, {"s": scenario_id, "u": user_id})


# ---------------------------------------------------------------------------
# Runtime settings seeded with the agreed defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_settings_seeded(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        rows = (
            await session.execute(sa.text("SELECT key, value_json FROM runtime_setting"))
        ).fetchall()
        as_dict = {row[0]: json.loads(row[1]) for row in rows}
    assert as_dict == {
        "local_concurrency": 2,
        "heartbeat_local_aos_interval_s": 2,
        "heartbeat_hpc_running_interval_s": 10,
        "heartbeat_hpc_queued_interval_s": 30,
        "cancel_grace_s": 30,
        # Seeded by migration 0037 (per-study concurrency cap, design §6,
        # 2-PS-a). The vocab guard wasn't updated when 0037 shipped — added
        # in lockstep here (b0-1 regression surfaced the drift).
        "study_concurrency_cap": 10,
        # Seeded by migration 0077 (local GPU queue + resource-aware
        # admission, ARCHITECTURE §16.32). The same drift as 0037 above,
        # a second time: the migration shipped and this list did not move.
        # That recurrence is the argument for keeping the expectation
        # spelled out by hand — deriving it from the migration would make
        # the guard agree with whatever was seeded and notice nothing.
        "gpu_concurrency": 1,
        "local_cpu_cores": 0,
        "local_memory_mb": 0,
        "local_memory_reserve_mb": 2048,
    }


# ---------------------------------------------------------------------------
# Permission seeding for the administrator role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_role_gained_new_permissions(tools_engine: AsyncEngine) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT permission FROM role_permission "
                    "WHERE role_id = (SELECT id FROM role WHERE name='administrator')"
                )
            )
        ).fetchall()
    granted = {row[0] for row in rows}
    assert "runtime_settings.manage" in granted
    assert "aos_hosts.manage" in granted
    assert "hpc_clusters.manage" in granted
    assert "storage_locations.manage" in granted
    assert "python_envs.manage" in granted
    # Super-user-only sentinel; admin must NOT have it as a row.
    assert "jobs.cancel_any" not in granted

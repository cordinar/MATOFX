"""Retention cleanup sweeper: drafts, logs, completed/failed working dirs.

Seeds jobs (old + recent) with real on-disk working directories, sets
the retention windows via General Settings, runs one pass, and asserts
what was deleted and what survived. Mirrors the ORM-seed posture of
``test_supervisor.py`` (datetimes written through the ORM so SQLite's
lexicographic CHECK comparisons stay happy).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings, make_general_settings_service
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.runner.retention_sweeper import run_retention_pass
from matofx.tools.exec import LOGS_DIRNAME
from matofx.tools.exec_orm import StorageLocation
from matofx.tools.orm import Tool, ToolVersion
from matofx.users import make_password_hasher, make_user_service


async def _seed(api_engine: AsyncEngine, tmp_path: Path) -> dict[str, Any]:
    factory = make_session_factory(api_engine)
    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    audit = AuditWriter(signing_key=None, key_id=None)
    us = make_user_service(auth, audit, hasher=make_password_hasher(auth))
    now = _dt.datetime.now(tz=_dt.UTC)
    old = now - _dt.timedelta(days=40)
    older = now - _dt.timedelta(days=41)

    def _mkdir(rel: str) -> str:
        d = tmp_path / rel
        (d / LOGS_DIRNAME).mkdir(parents=True)
        (d / "out.txt").write_text("x")
        (d / LOGS_DIRNAME / "stdout.log").write_text("log")
        return rel

    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await us.create_user(
            session,
            username="root",
            email="root@example.com",
            password="securepassword1",
            full_name="Root User",
            role="super_user",
        )
        uid = user.id
        descriptor = {"alias": "demo", "retain_files": [], "delete_io_files": []}
        session.add(
            Tool(
                alias="demo",
                name="Demo",
                version="1.0",
                state="released",
                descriptor=descriptor,
                bundle_path="/x",
                installed_by_user_id=uid,
            )
        )
        tv = ToolVersion(
            alias="demo",
            version="1.0",
            descriptor_snapshot=descriptor,
            bundle_path="/x",
            created_at=now,
            created_by_user_id=uid,
            change_summary="seed",
            changes={},
        )
        session.add(tv)
        loc = StorageLocation(name="local", mount_path=str(tmp_path))
        session.add(loc)
        await session.flush()
        project = Project(name="p", description="", creator_user_id=uid)
        session.add(project)
        await session.flush()

        # One tool per scenario is a UNIQUE constraint (migration 0017),
        # so each job gets its own scenario.
        specs: list[tuple[str, str, dict[str, Any]]] = [
            ("draft_old", "created", {"created_at": older}),
            ("draft_new", "created", {"created_at": now}),
            (
                "comp_old",
                "complete",
                {
                    "created_at": older,
                    "started_at": older,
                    "completed_at": old,
                    "storage_location_id": loc.id,
                    "working_dir_relative": _mkdir("jobs/job_c"),
                },
            ),
            (
                "fail_old",
                "failed",
                {
                    "created_at": older,
                    "started_at": older,
                    "completed_at": old,
                    "storage_location_id": loc.id,
                    "working_dir_relative": _mkdir("jobs/job_f"),
                },
            ),
            (
                "comp_new",
                "complete",
                {
                    "created_at": now,
                    "started_at": now,
                    "completed_at": now,
                    "storage_location_id": loc.id,
                    "working_dir_relative": _mkdir("jobs/job_cn"),
                },
            ),
        ]
        names: list[str] = []
        for name, state, kw in specs:
            scenario = Scenario(project_id=project.id, name=f"s_{name}", description="")
            session.add(scenario)
            await session.flush()
            session.add(
                Job(
                    scenario_id=scenario.id,
                    name=name,
                    tool_alias="demo",
                    tool_version_id=tv.id,
                    state=state,
                    inputs={},
                    created_by_user_id=uid,
                    **kw,
                )
            )
            names.append(name)
        await session.flush()
    return {"factory": factory, "audit": audit, "uid": uid, "tmp": tmp_path, "jobs": names}


async def _set(seed: dict[str, Any], key: str, value: object) -> None:
    async with session_scope(seed["factory"]) as session:
        await make_general_settings_service(seed["audit"]).set(
            session,
            actor_user_id=seed["uid"],
            actor_role="administrator",
            key=key,
            value=value,
        )


async def _remaining_job_names(seed: dict[str, Any]) -> set[str]:
    async with session_scope(seed["factory"]) as session:
        return set((await session.execute(sa.select(Job.name))).scalars().all())


@pytest.mark.asyncio
async def test_sweep_deletes_old_drafts_and_workdirs(
    runner_engine: AsyncEngine, tmp_path: Path
) -> None:
    seed = await _seed(runner_engine, tmp_path)
    await _set(seed, "retain_draft_jobs_days", 30)
    await _set(seed, "retain_completed_workdirs_days", 30)
    await _set(seed, "retain_failed_workdirs_days", 30)

    async with session_scope(seed["factory"]) as session:
        result = await run_retention_pass(session, audit=seed["audit"])

    assert result.drafts_deleted == 1
    assert result.completed_workdirs_deleted == 1
    assert result.failed_workdirs_deleted == 1

    names = await _remaining_job_names(seed)
    assert "draft_old" not in names
    assert "draft_new" in names  # recent draft kept

    assert not (tmp_path / "jobs" / "job_c").exists()  # old completed dir gone
    assert not (tmp_path / "jobs" / "job_f").exists()  # old failed dir gone
    assert (tmp_path / "jobs" / "job_cn").exists()  # recent completed kept


@pytest.mark.asyncio
async def test_sweep_deletes_old_logs_keeps_workdir(
    runner_engine: AsyncEngine, tmp_path: Path
) -> None:
    seed = await _seed(runner_engine, tmp_path)
    await _set(seed, "retain_job_logs_days", 30)
    # Workdir windows left at their 0 default → only the logs/ subdir goes.

    async with session_scope(seed["factory"]) as session:
        result = await run_retention_pass(session, audit=seed["audit"])

    assert result.log_dirs_deleted == 2  # comp_old + fail_old
    assert not (tmp_path / "jobs" / "job_c" / LOGS_DIRNAME).exists()
    assert (tmp_path / "jobs" / "job_c" / "out.txt").exists()  # rest of workdir kept
    assert (tmp_path / "jobs" / "job_cn" / LOGS_DIRNAME).exists()  # recent logs kept


@pytest.mark.asyncio
async def test_zero_window_keeps_drafts(runner_engine: AsyncEngine, tmp_path: Path) -> None:
    seed = await _seed(runner_engine, tmp_path)
    await _set(seed, "retain_draft_jobs_days", 0)  # 0 = keep forever

    async with session_scope(seed["factory"]) as session:
        result = await run_retention_pass(session, audit=seed["audit"])

    assert result.drafts_deleted == 0
    assert "draft_old" in await _remaining_job_names(seed)

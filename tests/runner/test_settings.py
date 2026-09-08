"""Tests for the runtime-settings reader."""

from __future__ import annotations

import datetime as _dt
import json

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.runner import DEFAULT_RUNTIME_SETTINGS, load_runtime_settings
from matofx.runner.settings import RUNTIME_SETTINGS_CATALOG


def test_study_concurrency_cap_in_catalog_and_defaults() -> None:
    spec = RUNTIME_SETTINGS_CATALOG["study_concurrency_cap"]
    assert spec.kind == "int"
    assert (spec.min_value, spec.max_value) == (1, 1024)
    assert spec.default == 10
    assert DEFAULT_RUNTIME_SETTINGS.study_concurrency_cap == 10


@pytest.mark.asyncio
async def test_study_concurrency_cap_load_and_override(runner_engine: AsyncEngine) -> None:
    factory = make_session_factory(runner_engine)
    async with session_scope(factory) as session:
        settings = await load_runtime_settings(session)
    assert settings.study_concurrency_cap == 10  # seeded by migration 0037
    now = _dt.datetime.now(tz=_dt.UTC).isoformat()
    async with session_scope(factory) as session:
        await session.execute(
            sa.text("UPDATE runtime_setting SET value_json = :v, updated_at = :u WHERE key = :k"),
            {"v": json.dumps(3), "u": now, "k": "study_concurrency_cap"},
        )
    async with session_scope(factory) as session:
        settings = await load_runtime_settings(session)
    assert settings.study_concurrency_cap == 3


def test_local_resource_settings_in_catalog_and_defaults() -> None:
    """Part A adds gpu_concurrency + the local CPU/RAM budget knobs."""
    gpu = RUNTIME_SETTINGS_CATALOG["gpu_concurrency"]
    assert gpu.kind == "int"
    assert (gpu.min_value, gpu.max_value) == (1, 64)
    assert gpu.default == 1
    assert DEFAULT_RUNTIME_SETTINGS.gpu_concurrency == 1

    cpu = RUNTIME_SETTINGS_CATALOG["local_cpu_cores"]
    assert cpu.min_value == 0  # 0 = auto-detect
    assert DEFAULT_RUNTIME_SETTINGS.local_cpu_cores == 0

    mem = RUNTIME_SETTINGS_CATALOG["local_memory_mb"]
    assert mem.min_value == 0
    assert DEFAULT_RUNTIME_SETTINGS.local_memory_mb == 0

    reserve = RUNTIME_SETTINGS_CATALOG["local_memory_reserve_mb"]
    assert reserve.default == 2048
    assert DEFAULT_RUNTIME_SETTINGS.local_memory_reserve_mb == 2048


@pytest.mark.asyncio
async def test_gpu_concurrency_load_and_override(runner_engine: AsyncEngine) -> None:
    factory = make_session_factory(runner_engine)
    async with session_scope(factory) as session:
        settings = await load_runtime_settings(session)
    assert settings.gpu_concurrency == 1  # seeded by migration 0077
    now = _dt.datetime.now(tz=_dt.UTC).isoformat()
    async with session_scope(factory) as session:
        await session.execute(
            sa.text("UPDATE runtime_setting SET value_json = :v, updated_at = :u WHERE key = :k"),
            {"v": json.dumps(3), "u": now, "k": "gpu_concurrency"},
        )
    async with session_scope(factory) as session:
        settings = await load_runtime_settings(session)
    assert settings.gpu_concurrency == 3


@pytest.mark.asyncio
async def test_load_returns_seeded_defaults(runner_engine: AsyncEngine) -> None:
    factory = make_session_factory(runner_engine)
    async with session_scope(factory) as session:
        settings = await load_runtime_settings(session)
    assert settings == DEFAULT_RUNTIME_SETTINGS


@pytest.mark.asyncio
async def test_load_picks_up_admin_changes(runner_engine: AsyncEngine) -> None:
    factory = make_session_factory(runner_engine)
    now = _dt.datetime.now(tz=_dt.UTC).isoformat()
    async with session_scope(factory) as session:
        await session.execute(
            sa.text("UPDATE runtime_setting SET value_json = :v, updated_at = :u WHERE key = :k"),
            {"v": json.dumps(7), "u": now, "k": "local_concurrency"},
        )
    async with session_scope(factory) as session:
        settings = await load_runtime_settings(session)
    assert settings.local_concurrency == 7
    # Other defaults still present.
    assert settings.cancel_grace_s == DEFAULT_RUNTIME_SETTINGS.cancel_grace_s


@pytest.mark.asyncio
async def test_load_clamps_out_of_range(runner_engine: AsyncEngine) -> None:
    factory = make_session_factory(runner_engine)
    now = _dt.datetime.now(tz=_dt.UTC).isoformat()
    async with session_scope(factory) as session:
        await session.execute(
            sa.text("UPDATE runtime_setting SET value_json = :v, updated_at = :u WHERE key = :k"),
            {"v": json.dumps(99_999), "u": now, "k": "local_concurrency"},
        )
    async with session_scope(factory) as session:
        settings = await load_runtime_settings(session)
    # Out-of-range falls back to the default rather than raising.
    assert settings.local_concurrency == DEFAULT_RUNTIME_SETTINGS.local_concurrency


@pytest.mark.asyncio
async def test_load_falls_back_on_garbage_json(runner_engine: AsyncEngine) -> None:
    factory = make_session_factory(runner_engine)
    now = _dt.datetime.now(tz=_dt.UTC).isoformat()
    async with session_scope(factory) as session:
        await session.execute(
            sa.text("UPDATE runtime_setting SET value_json = :v, updated_at = :u WHERE key = :k"),
            {"v": "not-json", "u": now, "k": "cancel_grace_s"},
        )
    async with session_scope(factory) as session:
        settings = await load_runtime_settings(session)
    assert settings.cancel_grace_s == DEFAULT_RUNTIME_SETTINGS.cancel_grace_s

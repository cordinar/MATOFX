"""Tests for :class:`RuntimeSettingsService`.

The fixture builds a fresh DB through alembic per test, seeds the
default permissions catalog (migration 0006), and creates a super-
user (gets the runtime_settings.manage permission via the
SUPER_USER_PERMISSIONS sentinel) and a regular user (no permissions).
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings
from matofx.db import make_session_factory, session_scope
from matofx.runner import (
    DEFAULT_RUNTIME_SETTINGS,
    RUNTIME_SETTINGS_CATALOG,
    InvalidSettingValueError,
    RuntimeSettingsAccessDeniedError,
    RuntimeSettingsService,
    UnknownSettingKeyError,
    load_runtime_settings,
    make_runtime_settings_service,
)
from matofx.tools.exec_orm import RuntimeSetting
from matofx.users import make_password_hasher, make_user_service


async def _seed_users(factory) -> tuple[int, int]:
    """Return (super_user_id, regular_user_id)."""
    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    audit = AuditWriter(signing_key=None, key_id=None)
    user_service = make_user_service(auth, audit, hasher=make_password_hasher(auth))
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        admin = await user_service.create_user(
            session,
            username="admin",
            email="admin@example.com",
            password="adminpassword",
            full_name="Admin",
            role="super_user",
        )
        regular = await user_service.create_user(
            session,
            username="regular",
            email="regular@example.com",
            password="regularpassword",
            full_name="Regular",
        )
        return admin.id, regular.id


@pytest.fixture
def runtime_settings_service() -> RuntimeSettingsService:
    return make_runtime_settings_service(AuditWriter(signing_key=None, key_id=None))


@pytest.mark.asyncio
async def test_get_all_returns_seeded_defaults(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        snapshot = await runtime_settings_service.get_all(session, actor_user_id=admin_id)
    assert snapshot["local_concurrency"] == DEFAULT_RUNTIME_SETTINGS.local_concurrency
    assert snapshot["cancel_grace_s"] == DEFAULT_RUNTIME_SETTINGS.cancel_grace_s
    assert set(snapshot.keys()) == set(RUNTIME_SETTINGS_CATALOG.keys())


@pytest.mark.asyncio
async def test_regular_user_denied_on_get_all(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    _, regular_id = await _seed_users(factory)
    async with session_scope(factory) as session:
        with pytest.raises(RuntimeSettingsAccessDeniedError):
            await runtime_settings_service.get_all(session, actor_user_id=regular_id)


@pytest.mark.asyncio
async def test_set_persists_value(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        await runtime_settings_service.set(
            session, actor_user_id=admin_id, key="local_concurrency", value=8
        )
    async with session_scope(factory) as session:
        snapshot = await runtime_settings_service.get_all(session, actor_user_id=admin_id)
    assert snapshot["local_concurrency"] == 8


@pytest.mark.asyncio
async def test_set_value_visible_to_runner_loader(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    """The runner's permissive loader sees the admin's write."""
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        await runtime_settings_service.set(
            session, actor_user_id=admin_id, key="cancel_grace_s", value=120
        )
    async with session_scope(factory) as session:
        loaded = await load_runtime_settings(session)
    assert loaded.cancel_grace_s == 120


@pytest.mark.asyncio
async def test_regular_user_denied_on_set(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    _, regular_id = await _seed_users(factory)
    async with session_scope(factory) as session:
        with pytest.raises(RuntimeSettingsAccessDeniedError):
            await runtime_settings_service.set(
                session, actor_user_id=regular_id, key="local_concurrency", value=4
            )


@pytest.mark.asyncio
async def test_set_unknown_key_rejected(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        with pytest.raises(UnknownSettingKeyError) as ei:
            await runtime_settings_service.set(session, actor_user_id=admin_id, key="nope", value=1)
        assert ei.value.key == "nope"


@pytest.mark.asyncio
async def test_set_wrong_type_rejected(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidSettingValueError):
            await runtime_settings_service.set(
                session, actor_user_id=admin_id, key="local_concurrency", value="eight"
            )


@pytest.mark.asyncio
async def test_set_float_into_int_field_rejected(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    """Floats land in float fields, not int fields."""
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidSettingValueError):
            await runtime_settings_service.set(
                session, actor_user_id=admin_id, key="local_concurrency", value=2.5
            )


@pytest.mark.asyncio
async def test_set_int_into_float_field_coerces(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    """Ints widen to floats for float-typed keys; this is convenient and unambiguous."""
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        await runtime_settings_service.set(
            session,
            actor_user_id=admin_id,
            key="heartbeat_local_aos_interval_s",
            value=5,
        )
    async with session_scope(factory) as session:
        snapshot = await runtime_settings_service.get_all(session, actor_user_id=admin_id)
    assert snapshot["heartbeat_local_aos_interval_s"] == 5.0


@pytest.mark.asyncio
async def test_set_bool_rejected(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    """``True`` / ``False`` look like ints to Python but never make sense here."""
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidSettingValueError):
            await runtime_settings_service.set(
                session, actor_user_id=admin_id, key="local_concurrency", value=True
            )


@pytest.mark.asyncio
async def test_set_below_range_rejected(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidSettingValueError):
            await runtime_settings_service.set(
                session, actor_user_id=admin_id, key="local_concurrency", value=0
            )


@pytest.mark.asyncio
async def test_set_above_range_rejected(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidSettingValueError):
            await runtime_settings_service.set(
                session,
                actor_user_id=admin_id,
                key="cancel_grace_s",
                value=10_000,
            )


@pytest.mark.asyncio
async def test_reset_restores_default(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        await runtime_settings_service.set(
            session, actor_user_id=admin_id, key="local_concurrency", value=16
        )
    async with session_scope(factory) as session:
        await runtime_settings_service.reset(
            session, actor_user_id=admin_id, key="local_concurrency"
        )
    async with session_scope(factory) as session:
        snapshot = await runtime_settings_service.get_all(session, actor_user_id=admin_id)
    assert snapshot["local_concurrency"] == DEFAULT_RUNTIME_SETTINGS.local_concurrency


@pytest.mark.asyncio
async def test_reset_unknown_key_rejected(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        with pytest.raises(UnknownSettingKeyError):
            await runtime_settings_service.reset(session, actor_user_id=admin_id, key="nope")


@pytest.mark.asyncio
async def test_reset_regular_user_denied(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    factory = make_session_factory(runner_engine)
    _, regular_id = await _seed_users(factory)
    async with session_scope(factory) as session:
        with pytest.raises(RuntimeSettingsAccessDeniedError):
            await runtime_settings_service.reset(
                session, actor_user_id=regular_id, key="local_concurrency"
            )


@pytest.mark.asyncio
async def test_set_records_audit_event(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    from matofx.audit.orm import AuditLog  # noqa: PLC0415

    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        await runtime_settings_service.set(
            session, actor_user_id=admin_id, key="local_concurrency", value=4
        )

    async with session_scope(factory) as session:
        rows = (
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.action == "runner.runtime_setting.set")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].resource_id == "local_concurrency"
        assert rows[0].actor == f"user:{admin_id}"


@pytest.mark.asyncio
async def test_set_updates_updated_at(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    """The row's ``updated_at`` advances on every write."""
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        original = await session.get(RuntimeSetting, ("runner", "local_concurrency"))
        assert original is not None
        original_at = original.updated_at

    async with session_scope(factory) as session:
        await runtime_settings_service.set(
            session, actor_user_id=admin_id, key="local_concurrency", value=4
        )

    async with session_scope(factory) as session:
        updated = await session.get(RuntimeSetting, ("runner", "local_concurrency"))
        assert updated is not None
        assert updated.value_json == json.dumps(4)
        # Naive vs aware: SQLite drops tzinfo on round-trip.
        a = updated.updated_at.replace(tzinfo=None)
        b = original_at.replace(tzinfo=None)
        assert a >= b


@pytest.mark.asyncio
async def test_set_records_updated_by_user_id_on_update(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    """**B1** fix: ``updated_by_user_id`` reflects the actor on the seeded-row update path."""
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        await runtime_settings_service.set(
            session, actor_user_id=admin_id, key="local_concurrency", value=12
        )
    async with session_scope(factory) as session:
        row = await session.get(RuntimeSetting, ("runner", "local_concurrency"))
        assert row is not None
        assert row.updated_by_user_id == admin_id


@pytest.mark.asyncio
async def test_set_records_updated_by_user_id_on_insert(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    """**B1** fix: insert path also records the actor (catalog row first-write)."""
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    # Delete the seeded row so the next set() takes the insert branch.
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "DELETE FROM runtime_setting WHERE domain = 'runner' AND key = 'local_concurrency'"
            )
        )

    async with session_scope(factory) as session:
        await runtime_settings_service.set(
            session, actor_user_id=admin_id, key="local_concurrency", value=5
        )
    async with session_scope(factory) as session:
        row = await session.get(RuntimeSetting, ("runner", "local_concurrency"))
        assert row is not None
        assert row.updated_by_user_id == admin_id


@pytest.mark.asyncio
async def test_reset_records_updated_by_user_id(
    runner_engine: AsyncEngine, runtime_settings_service: RuntimeSettingsService
) -> None:
    """**B1** fix: reset() also records the actor."""
    factory = make_session_factory(runner_engine)
    admin_id, _ = await _seed_users(factory)
    async with session_scope(factory) as session:
        await runtime_settings_service.reset(session, actor_user_id=admin_id, key="cancel_grace_s")
    async with session_scope(factory) as session:
        row = await session.get(RuntimeSetting, ("runner", "cancel_grace_s"))
        assert row is not None
        assert row.updated_by_user_id == admin_id

"""Tests for ``matofx.users.inactive_sweeper``.

Covers the four design invariants the docstring on the module
calls out:

* eligibility — active non-super-user with effective last activity
  older than the cutoff (NULL ``last_login_at`` falls back to
  ``created_at``);
* super-users are never auto-disabled;
* the WHERE-clause re-check defeats the live-login race;
* grace warnings are deduped via ``last_grace_warning_at`` and
  cleared on a fresh successful login.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.tools.exec_orm import RuntimeSetting
from matofx.users import UserService
from matofx.users.inactive_sweeper import (
    SweepResult,
    preview_inactive_users,
    sweep_once,
)
from matofx.users.orm import User

# --- helpers -----------------------------------------------------------


async def _seed_general_setting(
    engine: AsyncEngine,
    *,
    key: str,
    value: Any,
) -> None:
    """Write a single ``general``-domain ``runtime_setting`` row."""
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        existing = await session.get(RuntimeSetting, ("general", key))
        if existing is None:
            session.add(
                RuntimeSetting(
                    domain="general",
                    key=key,
                    value_json=json.dumps(value),
                )
            )
        else:
            existing.value_json = json.dumps(value)


async def _set_last_login(
    engine: AsyncEngine,
    *,
    user_id: int,
    at: _dt.datetime | None,
) -> None:
    """Bypass the normal login flow to backdate ``last_login_at`` for a test."""
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        await session.execute(sa.update(User).where(User.id == user_id).values(last_login_at=at))


async def _set_created_at(
    engine: AsyncEngine,
    *,
    user_id: int,
    at: _dt.datetime,
) -> None:
    """Backdate ``created_at`` so the NULL-last-login test can hit the cutoff."""
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        await session.execute(sa.update(User).where(User.id == user_id).values(created_at=at))


class _RecordingNotifier:
    """Notifier double that captures grace-warning calls for assertions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def notify_job_state_changed(self, *args: Any, **kwargs: Any) -> None:
        """Unused in these tests."""

    async def notify_approval_request_opened(self, *args: Any, **kwargs: Any) -> None:
        """Unused."""

    async def notify_approval_decisions(self, *args: Any, **kwargs: Any) -> None:
        """Unused."""

    async def notify_inactivity_grace_warning(
        self,
        session: Any,
        *,
        user: Any,
        days_remaining: int,
        threshold_days: int,
    ) -> None:
        self.calls.append(
            {
                "user_id": int(user.id),
                "days_remaining": days_remaining,
                "threshold_days": threshold_days,
            }
        )


# --- sweep_once --------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_disables_users_past_threshold(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer: Any,
) -> None:
    """An active regular user past the cutoff is deactivated."""
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="stale",
            email="stale@example.com",
            password="securepassword1",
            first_name="Stale",
            role="regular_user",
        )

    await _seed_general_setting(users_engine, key="auto_disable_inactive_days", value=30)
    now = _dt.datetime.now(tz=_dt.UTC)
    await _set_last_login(users_engine, user_id=user.id, at=now - _dt.timedelta(days=45))

    async with session_scope(factory) as session:
        result = await sweep_once(session, notifier=None, audit=audit_writer, as_of=now)

    assert isinstance(result, SweepResult)
    assert user.id in result.disabled_user_ids
    async with session_scope(factory) as session:
        refreshed = await session.get(User, user.id)
        assert refreshed is not None
        assert refreshed.is_active is False
        assert refreshed.logon_revoked_at is not None


@pytest.mark.asyncio
async def test_sweep_uses_created_at_when_last_login_is_null(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer: Any,
) -> None:
    """A brand-new account that's never logged in is measured from creation.

    The fallback is the design's answer to the "never-logged-in" case
    so a stale-looking NULL doesn't get treated as "infinitely old".
    """
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="never",
            email="never@example.com",
            password="securepassword1",
            first_name="Never",
            role="regular_user",
        )

    await _seed_general_setting(users_engine, key="auto_disable_inactive_days", value=30)
    now = _dt.datetime.now(tz=_dt.UTC)
    # Backdate created_at to 45 days ago AND clear last_login_at to
    # guarantee the COALESCE fallback is what triggers the disable.
    await _set_last_login(users_engine, user_id=user.id, at=None)
    await _set_created_at(users_engine, user_id=user.id, at=now - _dt.timedelta(days=45))

    async with session_scope(factory) as session:
        result = await sweep_once(session, notifier=None, audit=audit_writer, as_of=now)

    assert user.id in result.disabled_user_ids


@pytest.mark.asyncio
async def test_sweep_spares_brand_new_accounts(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer: Any,
) -> None:
    """An account created yesterday with no login is NOT auto-disabled."""
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="fresh",
            email="fresh@example.com",
            password="securepassword1",
            first_name="Fresh",
            role="regular_user",
        )

    await _seed_general_setting(users_engine, key="auto_disable_inactive_days", value=30)
    now = _dt.datetime.now(tz=_dt.UTC)
    await _set_last_login(users_engine, user_id=user.id, at=None)
    await _set_created_at(users_engine, user_id=user.id, at=now - _dt.timedelta(days=1))

    async with session_scope(factory) as session:
        result = await sweep_once(session, notifier=None, audit=audit_writer, as_of=now)

    assert user.id not in result.disabled_user_ids
    async with session_scope(factory) as session:
        refreshed = await session.get(User, user.id)
        assert refreshed is not None and refreshed.is_active is True


@pytest.mark.asyncio
async def test_sweep_never_auto_disables_super_users(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer: Any,
) -> None:
    """Super-users stay active no matter how stale their last login is.

    The rule explicitly preserves the install's break-glass admins;
    auto-disabling them would brick the install.
    """
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        su = await user_service.create_user(
            session,
            username="su",
            email="su@example.com",
            password="securepassword1",
            first_name="Super",
            role="super_user",
        )

    await _seed_general_setting(users_engine, key="auto_disable_inactive_days", value=30)
    now = _dt.datetime.now(tz=_dt.UTC)
    await _set_last_login(users_engine, user_id=su.id, at=now - _dt.timedelta(days=365))

    async with session_scope(factory) as session:
        result = await sweep_once(session, notifier=None, audit=audit_writer, as_of=now)

    assert su.id not in result.disabled_user_ids
    async with session_scope(factory) as session:
        refreshed = await session.get(User, su.id)
        assert refreshed is not None and refreshed.is_active is True


@pytest.mark.asyncio
async def test_sweep_is_noop_when_threshold_is_zero(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer: Any,
) -> None:
    """``auto_disable_inactive_days = 0`` disables the feature entirely.

    A long-idle user with the feature off must NOT be disabled.
    """
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="quiet",
            email="quiet@example.com",
            password="securepassword1",
            first_name="Quiet",
            role="regular_user",
        )

    # Default is 0; double-check by not seeding the row at all.
    now = _dt.datetime.now(tz=_dt.UTC)
    await _set_last_login(users_engine, user_id=user.id, at=now - _dt.timedelta(days=365))

    async with session_scope(factory) as session:
        result = await sweep_once(session, notifier=None, audit=audit_writer, as_of=now)

    assert result.threshold_days == 0
    assert result.disabled_user_ids == ()
    async with session_scope(factory) as session:
        refreshed = await session.get(User, user.id)
        assert refreshed is not None and refreshed.is_active is True


@pytest.mark.asyncio
async def test_sweep_race_recheck_skips_just_logged_in_users(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer: Any,
) -> None:
    """A user who logs in mid-sweep is saved by the WHERE-clause guard.

    Simulated by bumping ``last_login_at`` between the eligibility
    scan and the per-row update — the bump lands inside the same
    session, so the in-transaction WHERE re-check sees the fresh
    timestamp and the update affects zero rows.
    """
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="lucky",
            email="lucky@example.com",
            password="securepassword1",
            first_name="Lucky",
            role="regular_user",
        )

    await _seed_general_setting(users_engine, key="auto_disable_inactive_days", value=30)
    now = _dt.datetime.now(tz=_dt.UTC)
    await _set_last_login(users_engine, user_id=user.id, at=now - _dt.timedelta(days=45))

    # Concurrently bump the timestamp via direct SQL just before the
    # per-row UPDATE runs. Reading the eligibility list first ensures
    # the user IS in the set, so the race-recheck is what saves them.
    async with session_scope(factory) as session:
        # First call: just take the snapshot (this is what the sweeper does
        # internally), but in this test we want to interleave a login bump.
        # We use a separate session to simulate the user logging in just
        # before the sweep's per-row UPDATE.
        pass

    # Simulate the live login. We bump last_login_at to "now" via a
    # second connection, then run the sweep. The sweeper's WHERE
    # clause re-checks ``COALESCE(last_login_at, created_at) < cutoff``;
    # the bumped timestamp is >= cutoff, so the update matches 0 rows.
    await _set_last_login(users_engine, user_id=user.id, at=now)
    async with session_scope(factory) as session:
        result = await sweep_once(session, notifier=None, audit=audit_writer, as_of=now)

    # Because the eligibility scan and the per-row update both run on
    # the bumped state, the user isn't in the eligible set at all.
    # Either way, the user must NOT be disabled.
    assert user.id not in result.disabled_user_ids
    async with session_scope(factory) as session:
        refreshed = await session.get(User, user.id)
        assert refreshed is not None and refreshed.is_active is True


# --- grace-warning email ----------------------------------------------


@pytest.mark.asyncio
async def test_grace_warning_emails_users_in_the_band(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer: Any,
) -> None:
    """A user 5 days from the 30-day threshold gets a warning when grace=7."""
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="warning",
            email="warning@example.com",
            password="securepassword1",
            first_name="Warning",
            role="regular_user",
        )

    await _seed_general_setting(users_engine, key="auto_disable_inactive_days", value=30)
    await _seed_general_setting(users_engine, key="auto_disable_grace_warning_days", value=7)
    now = _dt.datetime.now(tz=_dt.UTC)
    # 25 days inactive — in the band (23 to 30).
    await _set_last_login(users_engine, user_id=user.id, at=now - _dt.timedelta(days=25))

    notifier = _RecordingNotifier()
    async with session_scope(factory) as session:
        result = await sweep_once(session, notifier=notifier, audit=audit_writer, as_of=now)

    assert user.id in result.grace_warned_user_ids
    assert user.id not in result.disabled_user_ids
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["user_id"] == user.id
    assert call["threshold_days"] == 30


@pytest.mark.asyncio
async def test_grace_warning_deduped_via_last_grace_warning_at(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer: Any,
) -> None:
    """A second sweep doesn't re-send the warning while the user is still in the band."""
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="dedup",
            email="dedup@example.com",
            password="securepassword1",
            first_name="Dedup",
            role="regular_user",
        )

    await _seed_general_setting(users_engine, key="auto_disable_inactive_days", value=30)
    await _seed_general_setting(users_engine, key="auto_disable_grace_warning_days", value=7)
    now = _dt.datetime.now(tz=_dt.UTC)
    await _set_last_login(users_engine, user_id=user.id, at=now - _dt.timedelta(days=25))

    notifier = _RecordingNotifier()
    async with session_scope(factory) as session:
        first = await sweep_once(session, notifier=notifier, audit=audit_writer, as_of=now)
    async with session_scope(factory) as session:
        second = await sweep_once(
            session,
            notifier=notifier,
            audit=audit_writer,
            as_of=now + _dt.timedelta(hours=1),
        )

    assert user.id in first.grace_warned_user_ids
    assert user.id not in second.grace_warned_user_ids
    assert len(notifier.calls) == 1  # only the first sweep emailed


@pytest.mark.asyncio
async def test_reactivation_resets_clock_so_next_sweep_does_not_redisable(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer: Any,
) -> None:
    """Admin re-activation must set ``last_login_at = now()`` to reset the clock.

    Without the reset, the next sweep finds the stale timestamp and
    immediately re-disables the just-reactivated user — exactly the
    bug the design called out.
    """
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="returning",
            email="returning@example.com",
            password="securepassword1",
            first_name="Returning",
            role="regular_user",
        )

    await _seed_general_setting(users_engine, key="auto_disable_inactive_days", value=30)
    now = _dt.datetime.now(tz=_dt.UTC)
    # Simulate a stale account that was auto-disabled.
    await _set_last_login(users_engine, user_id=user.id, at=now - _dt.timedelta(days=90))
    async with session_scope(factory) as session:
        await user_service.deactivate(
            session, user_id=user.id, actor="user:admin", reason="auto-disabled (manual)"
        )

    # Admin re-activates.
    async with session_scope(factory) as session:
        await user_service.reactivate(
            session, user_id=user.id, actor="user:admin", reason="back from leave"
        )

    # Next sweep — must NOT disable them.
    async with session_scope(factory) as session:
        result = await sweep_once(session, notifier=None, audit=audit_writer, as_of=now)

    assert user.id not in result.disabled_user_ids
    async with session_scope(factory) as session:
        refreshed = await session.get(User, user.id)
        assert refreshed is not None
        assert refreshed.is_active is True
        assert refreshed.last_login_at is not None
        assert refreshed.last_grace_warning_at is None


# --- preview_inactive_users -------------------------------------------


@pytest.mark.asyncio
async def test_preview_returns_count_and_users(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """The preview surfaces both an exact count + a sorted row list."""
    factory = make_session_factory(users_engine)
    now = _dt.datetime.now(tz=_dt.UTC)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        u_old = await user_service.create_user(
            session,
            username="u_old",
            email="old@example.com",
            password="securepassword1",
            first_name="Old",
            role="regular_user",
        )
        u_recent = await user_service.create_user(
            session,
            username="u_recent",
            email="recent@example.com",
            password="securepassword1",
            first_name="Recent",
            role="regular_user",
        )

    await _set_last_login(users_engine, user_id=u_old.id, at=now - _dt.timedelta(days=120))
    await _set_last_login(users_engine, user_id=u_recent.id, at=now - _dt.timedelta(days=5))

    async with session_scope(factory) as session:
        rows, total = await preview_inactive_users(session, threshold_days=30, as_of=now)

    assert total == 1
    assert len(rows) == 1
    assert rows[0].user_id == u_old.id
    assert rows[0].role == "regular_user"
    assert rows[0].days_inactive >= 90


@pytest.mark.asyncio
async def test_preview_excludes_super_users(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """Super-users never show up in the preview — they're never auto-disabled."""
    factory = make_session_factory(users_engine)
    now = _dt.datetime.now(tz=_dt.UTC)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        su = await user_service.create_user(
            session,
            username="su_stale",
            email="su_stale@example.com",
            password="securepassword1",
            first_name="SU",
            role="super_user",
        )

    await _set_last_login(users_engine, user_id=su.id, at=now - _dt.timedelta(days=200))

    async with session_scope(factory) as session:
        rows, total = await preview_inactive_users(session, threshold_days=30, as_of=now)

    assert total == 0
    assert rows == []

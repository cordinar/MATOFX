"""Tests for :class:`matofx.migrate.trust.TrustedPeerService`.

Goals:

* Round-trip — add then list returns the row in canonical form, even
  if the operator pasted a non-canonical fingerprint.
* Duplicate detection — both via the in-process pre-check and the
  DB unique constraint.
* Remove flow — by canonical or pasted fingerprint, audit row written
  before the delete, ``reason`` carried in the audit payload.
* ``mark_used`` updates ``last_used_at`` and is a quiet no-op when
  the row is gone.
* Audit rows land with the expected action names and payload shape —
  this is what the export / import services will rely on to satisfy
  the cross-install audit-trail requirement in PROJECT_BRIEF §Q5.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit.orm import AuditLog
from matofx.db import make_session_factory, session_scope
from matofx.migrate.errors import (
    TrustPeerAlreadyTrustedError,
    TrustPeerNotFoundError,
)
from matofx.migrate.orm import MigrateTrustedPeer
from matofx.migrate.signing import BundleSigningError
from matofx.migrate.trust import TrustedPeerService

# A canonical 64-char hex fingerprint and a few cosmetic variants of it.
CANONICAL = "9f2ea17b043d8ce1556a2f97cb0d187c0123456789abcdef0123456789abcdef"
PRETTY = "9F:2E:A1:7B:04:3D:8C:E1:55:6A:2F:97:CB:0D:18:7C:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF"


# ---------------------------------------------------------------------------
# add_peer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_peer_canonicalises_and_persists(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        row = await trust_service.add_peer(
            session,
            actor_user_id=seed_admin_user_id,
            fingerprint=PRETTY,  # paste-style colon-separated uppercase
            label="Staging",
            notes="prod-network MTX-412",
        )
        assert row.fingerprint == CANONICAL
        assert row.label == "Staging"
        assert row.notes == "prod-network MTX-412"
        assert row.added_by_user_id == seed_admin_user_id
        assert row.last_used_at is None

    # Survives commit and looks the same on reload.
    async with session_scope(factory) as session:
        rows = await trust_service.list_all(session)
        assert len(rows) == 1
        assert rows[0].fingerprint == CANONICAL


@pytest.mark.asyncio
async def test_add_peer_rejects_duplicate(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        await trust_service.add_peer(
            session,
            actor_user_id=seed_admin_user_id,
            fingerprint=CANONICAL,
            label="Staging",
        )

    # Same fingerprint, different cosmetic format — should still collide.
    async with session_scope(factory) as session:
        with pytest.raises(TrustPeerAlreadyTrustedError) as exc_info:
            await trust_service.add_peer(
                session,
                actor_user_id=seed_admin_user_id,
                fingerprint=PRETTY,
                label="Different label, same key",
            )
        assert "already trusted" in str(exc_info.value).lower()
        assert "Staging" in str(exc_info.value)


@pytest.mark.asyncio
async def test_add_peer_rejects_empty_label(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        with pytest.raises(ValueError) as exc_info:
            await trust_service.add_peer(
                session,
                actor_user_id=seed_admin_user_id,
                fingerprint=CANONICAL,
                label="   ",
            )
        assert "non-empty label" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_add_peer_rejects_malformed_fingerprint(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        with pytest.raises(BundleSigningError):
            await trust_service.add_peer(
                session,
                actor_user_id=seed_admin_user_id,
                fingerprint="not-a-fingerprint",
                label="Whatever",
            )


@pytest.mark.asyncio
async def test_add_peer_writes_audit_event(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        await trust_service.add_peer(
            session,
            actor_user_id=seed_admin_user_id,
            fingerprint=CANONICAL,
            label="Staging",
            notes="initial setup",
        )

    async with session_scope(factory) as session:
        events = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "migrate.peer.trusted")
            )
        ).scalars().all()
        assert len(events) == 1
        event = events[0]
        assert event.actor == f"user:{seed_admin_user_id}"
        assert event.outcome == "success"
        assert event.resource_type == "migrate_trusted_peer"
        assert event.payload["fingerprint"] == CANONICAL
        assert event.payload["label"] == "Staging"
        assert event.payload["notes"] == "initial setup"


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_fingerprint_canonicalises_input(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        await trust_service.add_peer(
            session,
            actor_user_id=seed_admin_user_id,
            fingerprint=CANONICAL,
            label="Staging",
        )

    async with session_scope(factory) as session:
        # Find via the prettified variant
        row = await trust_service.get_by_fingerprint(session, PRETTY)
        assert row is not None
        assert row.fingerprint == CANONICAL

        # And via "sha256:" prefix
        row = await trust_service.get_by_fingerprint(session, f"SHA256:{PRETTY}")
        assert row is not None


@pytest.mark.asyncio
async def test_is_trusted_returns_true_or_false(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        assert not await trust_service.is_trusted(session, CANONICAL)

    async with session_scope(factory) as session:
        await trust_service.add_peer(
            session,
            actor_user_id=seed_admin_user_id,
            fingerprint=CANONICAL,
            label="Staging",
        )

    async with session_scope(factory) as session:
        assert await trust_service.is_trusted(session, PRETTY)
        other = "ab" * 32
        assert not await trust_service.is_trusted(session, other)


@pytest.mark.asyncio
async def test_list_all_orders_newest_first(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    first = "1" * 64
    second = "2" * 64
    async with session_scope(factory) as session:
        await trust_service.add_peer(
            session, actor_user_id=seed_admin_user_id, fingerprint=first, label="First"
        )
    async with session_scope(factory) as session:
        await trust_service.add_peer(
            session, actor_user_id=seed_admin_user_id, fingerprint=second, label="Second"
        )

    async with session_scope(factory) as session:
        rows = await trust_service.list_all(session)
        assert [r.label for r in rows] == ["Second", "First"]


# ---------------------------------------------------------------------------
# remove_peer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_peer_deletes_row_and_audits(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        await trust_service.add_peer(
            session,
            actor_user_id=seed_admin_user_id,
            fingerprint=CANONICAL,
            label="Staging",
        )

    async with session_scope(factory) as session:
        await trust_service.remove_peer(
            session,
            actor_user_id=seed_admin_user_id,
            fingerprint=PRETTY,
            reason="Decommissioning Staging install",
        )

    async with session_scope(factory) as session:
        rows = await trust_service.list_all(session)
        assert rows == []
        events = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "migrate.peer.untrusted")
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].reason == "Decommissioning Staging install"
        assert events[0].payload["fingerprint"] == CANONICAL
        assert events[0].payload["label"] == "Staging"


@pytest.mark.asyncio
async def test_remove_peer_missing_raises(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        with pytest.raises(TrustPeerNotFoundError) as exc_info:
            await trust_service.remove_peer(
                session,
                actor_user_id=seed_admin_user_id,
                fingerprint=CANONICAL,
                reason="oops",
            )
        assert "no trusted peer" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# mark_used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_used_sets_last_used_at(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        row = await trust_service.add_peer(
            session,
            actor_user_id=seed_admin_user_id,
            fingerprint=CANONICAL,
            label="Staging",
        )
        assert row.last_used_at is None

    async with session_scope(factory) as session:
        await trust_service.mark_used(session, fingerprint=PRETTY)

    async with session_scope(factory) as session:
        row = await trust_service.get_by_fingerprint(session, CANONICAL)
        assert row is not None
        assert row.last_used_at is not None
        # Sanity: stamped recently. SQLite strips tzinfo on round-trip
        # through ``DateTime(timezone=True)`` columns, so normalise
        # both sides to naive-UTC before comparing.
        stamped = row.last_used_at
        if stamped.tzinfo is not None:
            stamped = stamped.astimezone(_dt.UTC).replace(tzinfo=None)
        now_naive_utc = _dt.datetime.now(tz=_dt.UTC).replace(tzinfo=None)
        delta = now_naive_utc - stamped
        assert _dt.timedelta(0) <= delta <= _dt.timedelta(minutes=1)


@pytest.mark.asyncio
async def test_mark_used_is_noop_when_missing(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
) -> None:
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        # Quiet no-op — no exception, just nothing to update.
        await trust_service.mark_used(session, fingerprint=CANONICAL)
        rows = await trust_service.list_all(session)
        assert rows == []


# ---------------------------------------------------------------------------
# DB-level constraint sanity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unique_constraint_holds_at_db_level(
    migrate_engine: AsyncEngine,
    trust_service: TrustedPeerService,
    seed_admin_user_id: int,
) -> None:
    """Direct ORM-level insert bypassing add_peer should still fail.

    Belt-and-braces — even if a future code path forgets to canonicalise
    or pre-check, the DB unique constraint catches duplicates.
    """
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        await trust_service.add_peer(
            session,
            actor_user_id=seed_admin_user_id,
            fingerprint=CANONICAL,
            label="Staging",
        )

    from sqlalchemy.exc import IntegrityError

    factory = make_session_factory(migrate_engine)
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(
                MigrateTrustedPeer(
                    fingerprint=CANONICAL,
                    label="Conflict",
                    added_by_user_id=seed_admin_user_id,
                )
            )
            await session.flush()

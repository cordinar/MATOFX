"""Security-hardening tests for matofx.audit."""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import (
    ChainHeadMissingError,
    RecordInput,
    ensure_chain_head,
    generate_signing_key,
    load_signing_key,
    make_audit_writer,
    verify,
)
from matofx.audit.orm import AuditChainHead, AuditLog
from matofx.config import AuditSettings
from matofx.db import make_session_factory, session_scope


def _input(**overrides: object) -> RecordInput:
    base: dict[str, object] = {
        "actor": "user:42",
        "actor_type": "user",
        "action": "login.success",
        "resource_type": "session",
        "resource_id": "s-1",
        "outcome": "success",
    }
    base.update(overrides)
    return RecordInput(**base)  # type: ignore[arg-type]


# --- Verifier loud about missing keys -----------------------------------


@pytest.mark.asyncio
async def test_verify_signed_records_with_no_keys_supplied_returns_failures(
    audit_engine: AsyncEngine,
    tmp_signing_key_path: Path,
) -> None:
    """A signed chain verified with no public keys must NOT silently pass."""
    writer = make_audit_writer(AuditSettings(signing_key_path=tmp_signing_key_path))
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await writer.record(session, _input())
        await writer.record(session, _input(action="job.submit"))

    async with session_scope(factory) as session:
        result = await verify(session, public_keys=None)
    assert not result.ok
    assert any("no public keys supplied" in f.reason for f in result.failures)


@pytest.mark.asyncio
async def test_verify_signed_records_with_empty_keys_dict_returns_failures(
    audit_engine: AsyncEngine,
    tmp_signing_key_path: Path,
) -> None:
    writer = make_audit_writer(AuditSettings(signing_key_path=tmp_signing_key_path))
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await writer.record(session, _input())

    async with session_scope(factory) as session:
        result = await verify(session, public_keys={})
    assert not result.ok


@pytest.mark.asyncio
async def test_verify_unsigned_records_pass_without_keys(
    audit_engine: AsyncEngine,
) -> None:
    """Unsigned chains verify fine without any public keys (the legacy path)."""
    writer = make_audit_writer(AuditSettings())  # no signing key
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await writer.record(session, _input())
        await writer.record(session, _input(action="job.submit"))

    async with session_scope(factory) as session:
        result = await verify(session)
    assert result.ok


# --- Chain-head guard ---------------------------------------------------


@pytest.mark.asyncio
async def test_chain_head_missing_with_existing_records_raises(
    audit_engine: AsyncEngine,
) -> None:
    """If the head row is deleted but records exist, the next append refuses."""
    writer = make_audit_writer(AuditSettings())
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await writer.record(session, _input())

    # Operator (or attacker) deletes the head row directly via SQL.
    async with session_scope(factory) as session:
        await session.execute(delete(AuditChainHead).where(AuditChainHead.id == 1))

    async with session_scope(factory) as session:
        with pytest.raises(ChainHeadMissingError):
            await writer.record(session, _input(action="another"))


@pytest.mark.asyncio
async def test_ensure_chain_head_with_records_but_no_head_raises(
    audit_engine: AsyncEngine,
) -> None:
    """The bootstrap path also refuses to fabricate a head row over existing data."""
    writer = make_audit_writer(AuditSettings())
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await writer.record(session, _input())

    async with session_scope(factory) as session:
        await session.execute(delete(AuditChainHead).where(AuditChainHead.id == 1))

    async with session_scope(factory) as session:
        with pytest.raises(ChainHeadMissingError):
            await ensure_chain_head(session)


@pytest.mark.asyncio
async def test_ensure_chain_head_creates_on_fresh_install(
    audit_engine: AsyncEngine,
) -> None:
    """When there are no records, ``ensure_chain_head`` creates the head row."""
    factory = make_session_factory(audit_engine)
    # Migration seeded the head row; remove it so we can test re-creation
    # on a hypothetical install where the migration didn't seed.
    async with session_scope(factory) as session:
        await session.execute(delete(AuditChainHead).where(AuditChainHead.id == 1))
    async with session_scope(factory) as session:
        head = await ensure_chain_head(session)
        assert head.id == 1


# --- Unique constraint on prior_hash ------------------------------------


@pytest.mark.asyncio
async def test_direct_insert_with_duplicate_prior_hash_rejected(
    audit_engine: AsyncEngine,
) -> None:
    """A direct INSERT bypassing the writer can't fork the chain."""
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    writer = make_audit_writer(AuditSettings())
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await writer.record(session, _input())
        # prior_hash of the second record == hash of the first
        await writer.record(session, _input(action="job.submit"))

    # Attempt to forge a fork: insert a third record claiming the same
    # prior_hash as record 2 (== hash of record 1). Use a raw session
    # outside ``session_scope`` because we expect the flush to fail and
    # need to handle the rollback ourselves rather than have the
    # context manager attempt to commit a poisoned transaction.
    from sqlalchemy import select  # noqa: PLC0415

    session = factory()
    try:
        first = (await session.execute(select(AuditLog).where(AuditLog.id == 1))).scalar_one()
        forged = AuditLog(
            timestamp=first.timestamp,
            actor="forged",
            actor_type="user",
            action="forged",
            resource_type="x",
            resource_id="y",
            outcome="success",
            prior_hash=first.hash,  # collide with record 2's prior_hash
            hash="ff" * 32,
            schema_version=1,
            payload={},
        )
        session.add(forged)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
    finally:
        await session.close()


# --- Permission warning on signing key ----------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_loose_key_permissions_warn(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    generate_signing_key(keyfile)
    keyfile.chmod(0o644)  # other-readable
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        load_signing_key(keyfile)
    assert any("readable by other users" in str(w.message) for w in captured)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_owner_only_key_does_not_warn(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    generate_signing_key(keyfile)
    keyfile.chmod(0o600)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        load_signing_key(keyfile)
    msgs = [str(w.message) for w in captured]
    assert not any("readable by other users" in m for m in msgs)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_key_perm_warning_suppressed_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyfile = tmp_path / "signing.key"
    generate_signing_key(keyfile)
    keyfile.chmod(0o644)
    monkeypatch.setenv("MATOFX_SUPPRESS_PERM_WARNING", "1")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        load_signing_key(keyfile)
    msgs = [str(w.message) for w in captured]
    assert not any("readable by other users" in m for m in msgs)

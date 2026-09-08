"""Integrity tests: the chain walks clean, tampering is detected."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import (
    RecordInput,
    compute_key_id,
    ensure_chain_head,
    load_signing_key,
    make_audit_writer,
    verify,
)
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


@pytest.mark.asyncio
async def test_empty_chain_verifies(audit_engine: AsyncEngine) -> None:
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
    async with session_scope(factory) as session:
        result = await verify(session)
    assert result.ok
    assert result.records_checked == 0


@pytest.mark.asyncio
async def test_clean_chain_verifies(audit_engine: AsyncEngine) -> None:
    writer = make_audit_writer(AuditSettings())
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        for i in range(5):
            await writer.record(session, _input(action=f"act.{i}"))

    async with session_scope(factory) as session:
        result = await verify(session)
    assert result.ok, result.failures
    assert result.records_checked == 5


@pytest.mark.asyncio
async def test_tampered_payload_detected(audit_engine: AsyncEngine) -> None:
    writer = make_audit_writer(AuditSettings())
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await writer.record(session, _input())
        await writer.record(session, _input(action="job.submit"))

    # Forge: rewrite a stored record's reason without recomputing the hash.
    async with session_scope(factory) as session:
        await session.execute(text("UPDATE audit_log SET reason = 'tamper' WHERE id = 1"))

    async with session_scope(factory) as session:
        result = await verify(session)
    assert not result.ok
    assert any("hash mismatch" in f.reason for f in result.failures)


@pytest.mark.asyncio
async def test_broken_chain_link_detected(audit_engine: AsyncEngine) -> None:
    writer = make_audit_writer(AuditSettings())
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await writer.record(session, _input())
        await writer.record(session, _input(action="job.submit"))

    # Forge: replace the second record's prior_hash with a wrong value.
    async with session_scope(factory) as session:
        await session.execute(
            text("UPDATE audit_log SET prior_hash = '" + ("f" * 64) + "' WHERE id = 2")
        )

    async with session_scope(factory) as session:
        result = await verify(session)
    assert not result.ok
    assert any("prior_hash" in f.reason for f in result.failures)


@pytest.mark.asyncio
async def test_signed_chain_verifies_with_correct_key(
    audit_engine: AsyncEngine,
    tmp_signing_key_path: Path,
) -> None:
    writer = make_audit_writer(AuditSettings(signing_key_path=tmp_signing_key_path))
    factory = make_session_factory(audit_engine)

    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await writer.record(session, _input())
        await writer.record(session, _input(action="job.submit"))

    pub = load_signing_key(tmp_signing_key_path).public_key()
    public_keys = {compute_key_id(pub): pub}
    async with session_scope(factory) as session:
        result = await verify(session, public_keys=public_keys)
    assert result.ok, result.failures


@pytest.mark.asyncio
async def test_signed_chain_fails_with_wrong_key(
    audit_engine: AsyncEngine,
    tmp_signing_key_path: Path,
    tmp_path: Path,
) -> None:
    from matofx.audit import generate_signing_key  # noqa: PLC0415

    writer = make_audit_writer(AuditSettings(signing_key_path=tmp_signing_key_path))
    factory = make_session_factory(audit_engine)

    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await writer.record(session, _input())

    other = generate_signing_key(tmp_path / "other.key").public_key()
    other_id = compute_key_id(other)
    async with session_scope(factory) as session:
        result = await verify(session, public_keys={other_id: other})
    assert not result.ok
    assert any("unknown key_id" in f.reason for f in result.failures)


@pytest.mark.asyncio
async def test_subrange_verification(audit_engine: AsyncEngine) -> None:
    writer = make_audit_writer(AuditSettings())
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        ids = [await writer.record(session, _input(action=f"act.{i}")) for i in range(4)]

    async with session_scope(factory) as session:
        result = await verify(session, start_id=ids[1], end_id=ids[2])
    assert result.ok, result.failures
    assert result.records_checked == 2

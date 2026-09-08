"""Tests for the audit writer / chain head behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import (
    AuditConfigError,
    RecordInput,
    ensure_chain_head,
    make_audit_writer,
)
from matofx.audit.hashing import GENESIS_HASH
from matofx.audit.orm import AuditChainHead, AuditLog
from matofx.config import AuditSettings
from matofx.db import make_session_factory, session_scope


def _settings(path: Path | None = None, *, require_signing: bool = False) -> AuditSettings:
    return AuditSettings(signing_key_path=path, require_signing=require_signing)


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
async def test_unsigned_writer_constructs_when_no_key() -> None:
    writer = make_audit_writer(_settings())
    assert not writer.is_signed
    assert writer.key_id is None


@pytest.mark.asyncio
async def test_signed_writer_loads_key(tmp_signing_key_path: Path) -> None:
    writer = make_audit_writer(_settings(tmp_signing_key_path))
    assert writer.is_signed
    assert writer.key_id is not None
    assert len(writer.key_id) == 16


def test_require_signing_without_key_raises() -> None:
    with pytest.raises(AuditConfigError) as exc_info:
        make_audit_writer(_settings(require_signing=True))
    assert "require_signing" in str(exc_info.value)


@pytest.mark.asyncio
async def test_first_record_uses_genesis_prior_hash(audit_engine: AsyncEngine) -> None:
    writer = make_audit_writer(_settings())
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rec_id = await writer.record(session, _input())

    async with session_scope(factory) as session:
        rec = await session.get(AuditLog, rec_id)
        assert rec is not None
        assert rec.prior_hash == GENESIS_HASH
        head = await session.get(AuditChainHead, 1)
        assert head is not None
        assert head.last_record_id == rec.id
        assert head.last_hash == rec.hash


@pytest.mark.asyncio
async def test_subsequent_records_link(audit_engine: AsyncEngine) -> None:
    writer = make_audit_writer(_settings())
    factory = make_session_factory(audit_engine)

    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        first_id = await writer.record(session, _input(action="login.success"))
        second_id = await writer.record(session, _input(action="job.submit"))

    async with session_scope(factory) as session:
        first = await session.get(AuditLog, first_id)
        second = await session.get(AuditLog, second_id)
        assert first is not None
        assert second is not None
        assert second.prior_hash == first.hash
        assert second.id > first.id


@pytest.mark.asyncio
async def test_signed_records_carry_signature(
    audit_engine: AsyncEngine,
    tmp_signing_key_path: Path,
) -> None:
    writer = make_audit_writer(_settings(tmp_signing_key_path))
    factory = make_session_factory(audit_engine)

    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rec_id = await writer.record(session, _input())

    async with session_scope(factory) as session:
        rec = await session.get(AuditLog, rec_id)
        assert rec is not None
        assert rec.signature is not None
        assert len(rec.signature) == 128  # 64 bytes hex
        assert rec.key_id == writer.key_id


@pytest.mark.asyncio
async def test_unsigned_records_have_null_signature(audit_engine: AsyncEngine) -> None:
    writer = make_audit_writer(_settings())
    factory = make_session_factory(audit_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        rec_id = await writer.record(session, _input())
    async with session_scope(factory) as session:
        rec = await session.get(AuditLog, rec_id)
        assert rec is not None
        assert rec.signature is None
        assert rec.key_id is None

"""Fixtures for the licence-subsystem tests (ADR-0019 slice 2).

Provides a licence-file writer that can produce every shape the loader
has to cope with: valid, unsigned, signed by an untrusted key, edited
after signing, bound to another machine, and expired.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from matofx.audit import generate_signing_key
from matofx.audit.keys import compute_key_id
from matofx.compliance.canonical import canonical_bytes
from matofx.config import (
    AuditSettings,
    ComplianceSettings,
    DbSettings,
    LicenceSettings,
    Settings,
)
from matofx.db import make_engine, make_session_factory
from tests._migrated_db import postgres_db_settings, sqlite_db_settings

ISSUER_KEY_ID = "funis-test-2026"


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def licence_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    """Per-dialect DbSettings against a migrated database.

    SQLite copies the session-scoped migrated template (milliseconds);
    Postgres migrates per test exactly as before. See
    :mod:`tests._migrated_db` for why.
    """
    if request.param == "sqlite":
        yield sqlite_db_settings(migrated_template, tmp_path, "licence.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def licence_engine(licence_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(licence_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(licence_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = make_session_factory(licence_engine)
    async with factory() as db:
        yield db
        await db.rollback()


@pytest.fixture
def issuer() -> Ed25519PrivateKey:
    """A stand-in for the Funis YubiKey."""
    return Ed25519PrivateKey.generate()


def public_hex(private: Ed25519PrivateKey) -> str:
    """Hex of a key's public half."""
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def write_licence(
    path: Path,
    *,
    issuer_private: Ed25519PrivateKey | None,
    issuer_key_id: str = ISSUER_KEY_ID,
    licence_type: str = "csl",
    n_slots: int = 10,
    issued_at: str = "2026-01-14T00:00:00Z",
    expires_at: str = "2099-01-14T00:00:00Z",
    grace_period_days: int = 30,
    features: dict[str, bool] | None = None,
    machine_binding: str | None = None,
    with_report_key: bool = True,
    install_id: str | None = "inst_TEST0001",
    tamper: bool = False,
    overrides: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> Path:
    """Write a licence file, optionally broken in a specific way."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "licence_id": "LIC-TEST-0001",
        "customer": "Northwind Aerospace Ltd",
        "environment": "production",
        "install_id": install_id,
        "licence_type": licence_type,
        "n_slots": n_slots,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "grace_period_days": grace_period_days,
        "features": features if features is not None else {"parametric_studies": True},
        "machine_binding": machine_binding,
        "banner_text": None,
    }
    if with_report_key:
        report_key = Ed25519PrivateKey.generate()
        payload["report_key_seed"] = report_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ).hex()
        payload["report_key_id"] = compute_key_id(report_key.public_key())
    if overrides:
        payload.update(overrides)
    for key in drop:
        payload.pop(key, None)

    document: dict[str, Any] = {"licence": payload}
    if issuer_private is not None:
        document["issuer"] = {
            "key_id": issuer_key_id,
            "alg": "ed25519",
            "signature": issuer_private.sign(canonical_bytes({"licence": payload})).hex(),
        }
    if tamper:
        payload["n_slots"] = n_slots * 100
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def licence_file(tmp_path: Path, issuer: Ed25519PrivateKey) -> Path:
    """A valid, issuer-signed, non-expiring CSL licence."""
    return write_licence(tmp_path / "matofx-licence.json", issuer_private=issuer)


@pytest.fixture
def settings(
    licence_db_settings: DbSettings,
    tmp_path: Path,
    licence_file: Path,
    issuer: Ed25519PrivateKey,
) -> Settings:
    """A Settings tree wired to the temp DB, licence and audit key."""
    signing_key_path = tmp_path / "audit-signing.key"
    generate_signing_key(signing_key_path)
    return Settings(
        environment="test",
        db=licence_db_settings,
        audit=AuditSettings(signing_key_path=signing_key_path, require_signing=True),
        compliance=ComplianceSettings(issuer_public_keys={ISSUER_KEY_ID: public_hex(issuer)}),
        licence=LicenceSettings(file=licence_file),
    )


def utcnow() -> _dt.datetime:
    """Timezone-aware now."""
    return _dt.datetime.now(tz=_dt.UTC)

"""Fixtures for the compliance-attestation tests (ADR-0019).

Mirrors ``tests/audit/conftest.py``: a freshly-migrated DB per test, plus
the two pieces of key material the subsystem needs — an audit signing key
(so the chain is signed and self-verifiable) and a Funis-issued
install-identity file signed by a *test* issuer key.

The issuer key is generated per-test and passed through
``[compliance].issuer_public_keys``. The build-time trusted map
(:mod:`matofx.compliance.issuer`) is deliberately empty in the source
tree, so tests must supply their own — exactly as a self-hosting
customer would.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from matofx.audit import generate_signing_key
from matofx.audit.keys import compute_key_id
from matofx.compliance.canonical import canonical_bytes
from matofx.config import AuditSettings, ComplianceSettings, DbSettings, Settings
from matofx.db import make_engine, make_session_factory
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def compliance_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    """Per-dialect DbSettings against a migrated database.

    SQLite copies the session-scoped migrated template (milliseconds);
    Postgres migrates per test exactly as before. See
    :mod:`tests._migrated_db` for why.
    """
    if request.param == "sqlite":
        yield sqlite_db_settings(migrated_template, tmp_path, "compliance.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def compliance_engine(compliance_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(compliance_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(compliance_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A committing session — the report writes across several tables."""
    factory = make_session_factory(compliance_engine)
    async with factory() as db:
        yield db
        await db.rollback()


@pytest.fixture
def issuer_key() -> tuple[str, Ed25519PrivateKey]:
    """A stand-in for the Funis YubiKey: ``(key_id, private key)``."""
    return ("funis-test-2026", Ed25519PrivateKey.generate())


def write_identity_file(
    path: Path,
    *,
    issuer_key_id: str,
    issuer_private: Ed25519PrivateKey | None,
    install_id: str = "inst_TEST0001",
    customer: str = "Northwind Aerospace Ltd",
    environment: str = "production",
    seed: str | None = None,
    tamper: bool = False,
) -> Path:
    """Write a Funis-issued install-identity file (optionally broken).

    ``issuer_private=None`` writes an unsigned file; ``tamper=True``
    signs it and then edits the customer name, which is what a customer
    forging their own identity would produce.
    """
    report_key = (
        Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
        if seed
        else Ed25519PrivateKey.generate()
    )
    raw_seed = report_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()
    identity = {
        "install_id": install_id,
        "customer": customer,
        "environment": environment,
        "issued_at": "2026-01-14T00:00:00Z",
        "report_key_seed": raw_seed,
        "report_key_id": compute_key_id(report_key.public_key()),
    }
    document: dict[str, object] = {"identity": identity}
    if issuer_private is not None:
        signature = issuer_private.sign(canonical_bytes({"identity": identity}))
        document["issuer"] = {
            "key_id": issuer_key_id,
            "alg": "ed25519",
            "signature": signature.hex(),
        }
    if tamper:
        identity["customer"] = "Someone Else Ltd"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def issuer_public_hex(private: Ed25519PrivateKey) -> str:
    """Hex of an issuer key's public half, for ``issuer_public_keys``."""
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


@pytest.fixture
def identity_file(tmp_path: Path, issuer_key: tuple[str, Ed25519PrivateKey]) -> Path:
    """A valid, issuer-signed identity file."""
    key_id, private = issuer_key
    return write_identity_file(
        tmp_path / "funis-install-identity.json",
        issuer_key_id=key_id,
        issuer_private=private,
    )


@pytest.fixture
def settings(
    compliance_db_settings: DbSettings,
    tmp_path: Path,
    identity_file: Path,
    issuer_key: tuple[str, Ed25519PrivateKey],
) -> Settings:
    """A full Settings tree wired to the temp DB + keys."""
    key_id, private = issuer_key
    signing_key_path = tmp_path / "audit-signing.key"
    generate_signing_key(signing_key_path)
    return Settings(
        environment="test",
        db=compliance_db_settings,
        audit=AuditSettings(signing_key_path=signing_key_path, require_signing=True),
        compliance=ComplianceSettings(
            identity_file=identity_file,
            issuer_public_keys={key_id: issuer_public_hex(private)},
        ),
    )


def utcnow() -> _dt.datetime:
    """Timezone-aware now, for tests that stamp their own timestamps."""
    return _dt.datetime.now(tz=_dt.UTC)

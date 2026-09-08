"""Install-identity adoption + issuer trust (ADR-0019 §A1).

The identity file carries the key that signs every compliance report, so
these tests are about one question: can an install end up signing reports
with material Funis did not issue?
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.compliance import (
    InstallIdentityError,
    adopt_identity,
    current_identity,
    load_identity_file,
    next_report_counter,
)
from tests.compliance.conftest import issuer_public_hex, utcnow, write_identity_file


def _keys(issuer_key: tuple[str, Ed25519PrivateKey]) -> dict[str, str]:
    key_id, private = issuer_key
    return {key_id: issuer_public_hex(private)}


def test_valid_identity_loads_and_verifies(
    identity_file: Path, issuer_key: tuple[str, Ed25519PrivateKey]
) -> None:
    issued = load_identity_file(identity_file, extra_issuer_keys=_keys(issuer_key))
    assert issued.install_id == "inst_TEST0001"
    assert issued.customer == "Northwind Aerospace Ltd"
    assert issued.issuer_verified is True
    assert len(issued.report_key_id) == 16
    assert len(issued.public_key_hex()) == 64


def test_tampered_identity_is_rejected(
    tmp_path: Path, issuer_key: tuple[str, Ed25519PrivateKey]
) -> None:
    """Edit the customer name after signing — the classic forge attempt."""
    key_id, private = issuer_key
    path = write_identity_file(
        tmp_path / "id.json", issuer_key_id=key_id, issuer_private=private, tamper=True
    )
    with pytest.raises(InstallIdentityError, match="altered since Funis signed it"):
        load_identity_file(path, extra_issuer_keys=_keys(issuer_key))


def test_identity_signed_by_untrusted_key_is_rejected(
    tmp_path: Path, issuer_key: tuple[str, Ed25519PrivateKey]
) -> None:
    """A customer who mints their own issuer key gets nowhere."""
    rogue = Ed25519PrivateKey.generate()
    path = write_identity_file(
        tmp_path / "id.json", issuer_key_id="rogue-key", issuer_private=rogue
    )
    with pytest.raises(InstallIdentityError, match="does not trust"):
        load_identity_file(path, extra_issuer_keys=_keys(issuer_key))


def test_unsigned_identity_rejected_when_required(tmp_path: Path) -> None:
    path = write_identity_file(tmp_path / "id.json", issuer_key_id="", issuer_private=None)
    with pytest.raises(InstallIdentityError, match="no issuer signature"):
        load_identity_file(path, require_issuer=True)


def test_unsigned_identity_allowed_outside_prod_but_flagged(tmp_path: Path) -> None:
    """Dev installs work, but the report can never claim a verified identity."""
    path = write_identity_file(tmp_path / "id.json", issuer_key_id="", issuer_private=None)
    issued = load_identity_file(path, require_issuer=False)
    assert issued.issuer_verified is False


def test_key_id_must_match_the_seed(
    tmp_path: Path, issuer_key: tuple[str, Ed25519PrivateKey]
) -> None:
    key_id, private = issuer_key
    path = write_identity_file(tmp_path / "id.json", issuer_key_id=key_id, issuer_private=private)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["identity"]["report_key_id"] = "0" * 16
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(InstallIdentityError, match="does not match the key seed"):
        load_identity_file(path, extra_issuer_keys=_keys(issuer_key), require_issuer=False)


def test_missing_file_names_the_setting(tmp_path: Path) -> None:
    with pytest.raises(InstallIdentityError, match=r"\[compliance\].identity_file"):
        load_identity_file(tmp_path / "nope.json")


@pytest.mark.asyncio
async def test_adopt_then_reissue_keeps_the_counter(
    session: AsyncSession,
    tmp_path: Path,
    identity_file: Path,
    issuer_key: tuple[str, Ed25519PrivateKey],
) -> None:
    """A re-issued identity must not restart the report sequence.

    Funis's ledger is keyed on the counter; restarting it would look
    exactly like a withheld period.
    """
    key_id, private = issuer_key
    issued = load_identity_file(identity_file, extra_issuer_keys=_keys(issuer_key))
    await adopt_identity(session, issued, now=utcnow())
    assert await next_report_counter(session) == 1
    assert await next_report_counter(session) == 2

    rotated_path = write_identity_file(
        tmp_path / "reissued.json",
        issuer_key_id=key_id,
        issuer_private=private,
        install_id="inst_TEST0001",
        customer="Northwind Aerospace Limited",
    )
    rotated = load_identity_file(rotated_path, extra_issuer_keys=_keys(issuer_key))
    row = await adopt_identity(session, rotated, now=utcnow())

    assert row.report_counter == 2
    assert row.customer == "Northwind Aerospace Limited"
    assert row.report_key_id == rotated.report_key_id


@pytest.mark.asyncio
async def test_adopting_a_different_install_is_refused(
    session: AsyncSession,
    tmp_path: Path,
    identity_file: Path,
    issuer_key: tuple[str, Ed25519PrivateKey],
) -> None:
    """A cloned database must not be re-badged as a new install."""
    key_id, private = issuer_key
    first = load_identity_file(identity_file, extra_issuer_keys=_keys(issuer_key))
    await adopt_identity(session, first, now=utcnow())

    other_path = write_identity_file(
        tmp_path / "other.json",
        issuer_key_id=key_id,
        issuer_private=private,
        install_id="inst_OTHER999",
    )
    other = load_identity_file(other_path, extra_issuer_keys=_keys(issuer_key))
    with pytest.raises(InstallIdentityError, match="Refusing to re-badge"):
        await adopt_identity(session, other, now=utcnow())


@pytest.mark.asyncio
async def test_counter_without_identity_is_a_clear_error(session: AsyncSession) -> None:
    assert await current_identity(session) is None
    with pytest.raises(InstallIdentityError, match="compliance adopt"):
        await next_report_counter(session)

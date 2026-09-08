"""Install identity: adopt Funis-issued material, sign reports with it (ADR-0019).

The install-identity file is the bridge that lets slice 1 ship before the
licence subsystem exists. Funis issues it out-of-band, signed with the
same key infrastructure as licences and releases (§C16); slice 2 folds
these fields into the licence manifest and this file becomes legacy.

Why a Funis-issued key rather than the install's own: an install that
generates its own signing key can regenerate its whole history and
re-sign it, which is precisely the forgery a compliance report is meant
to rule out (ADR-0019 §A1 — this supersedes brief §B11's derived key).

## File shape

```json
{
  "identity": {
    "install_id": "inst_7Q2M4KJ9",
    "customer": "Northwind Aerospace Ltd",
    "environment": "production",
    "issued_at": "2026-01-14T00:00:00Z",
    "report_key_seed": "<64 hex — Ed25519 private seed>",
    "report_key_id": "<16 hex — derived from the public half>"
  },
  "issuer": { "key_id": "funis-2026-01", "alg": "ed25519", "signature": "<hex>" }
}
```

The issuer signature covers the canonical bytes of the ``identity``
object alone, so adding envelope fields later doesn't invalidate issued
files.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from matofx.audit.keys import compute_key_id
from matofx.compliance.canonical import canonical_bytes
from matofx.compliance.errors import InstallIdentityError
from matofx.compliance.issuer import TRUSTED_ISSUER_KEYS
from matofx.compliance.orm import SINGLETON_ID, InstallIdentity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_SEED_HEX_LEN = 64
_REQUIRED_IDENTITY_FIELDS = (
    "install_id",
    "customer",
    "environment",
    "issued_at",
    "report_key_seed",
    "report_key_id",
)


@dataclass(frozen=True)
class IssuedIdentity:
    """A parsed, structurally-valid install-identity file."""

    install_id: str
    customer: str
    environment: str
    issued_at: _dt.datetime
    report_key_seed: str
    report_key_id: str
    issuer_key_id: str
    issuer_verified: bool
    file_sha256: str

    def signing_key(self) -> Ed25519PrivateKey:
        """The Ed25519 private key this install signs reports with."""
        try:
            return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.report_key_seed))
        except ValueError as exc:  # pragma: no cover - guarded at parse time
            msg = f"Report key seed is not a valid Ed25519 key: {exc}"
            raise InstallIdentityError(msg) from exc

    def public_key_hex(self) -> str:
        """Hex of the raw public key — what Funis pins in its ledger."""
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

        return (
            self.signing_key()
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            .hex()
        )


def load_identity_file(
    path: Path,
    *,
    extra_issuer_keys: dict[str, str] | None = None,
    require_issuer: bool = True,
) -> IssuedIdentity:
    """Parse + validate an install-identity file.

    Args:
        path: Location of the Funis-issued JSON file.
        extra_issuer_keys: Additional trusted issuer keys (key id → hex
            public key) from ``[compliance].issuer_public_keys``, merged
            over the build-time map.
        require_issuer: When true (production), an unverifiable issuer
            signature is fatal. When false, the identity is accepted with
            ``issuer_verified = False`` and every report says so.

    Raises:
        InstallIdentityError: missing / unreadable / malformed file, a
            key id the build doesn't trust, a bad issuer signature, or a
            ``report_key_id`` that doesn't match the seed.
    """
    raw = _read_bytes(path)
    document = _parse_json(raw, path)

    identity = document.get("identity")
    if not isinstance(identity, dict):
        raise InstallIdentityError(
            f"{path} has no 'identity' object. This does not look like a MATOFX "
            "install-identity file — ask Funis to re-issue it."
        )
    missing = [f for f in _REQUIRED_IDENTITY_FIELDS if not identity.get(f)]
    if missing:
        raise InstallIdentityError(
            f"{path} is missing required identity field(s): {', '.join(missing)}. "
            "Ask Funis to re-issue the file."
        )

    seed = str(identity["report_key_seed"])
    if len(seed) != _SEED_HEX_LEN:
        raise InstallIdentityError(
            f"{path}: report_key_seed must be {_SEED_HEX_LEN} hex characters, got {len(seed)}."
        )
    try:
        private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
    except ValueError as exc:
        msg = f"{path}: report_key_seed is not valid hex / key bytes."
        raise InstallIdentityError(msg) from exc

    derived_key_id = compute_key_id(private.public_key())
    declared_key_id = str(identity["report_key_id"])
    if derived_key_id != declared_key_id:
        raise InstallIdentityError(
            f"{path}: report_key_id {declared_key_id} does not match the key seed "
            f"(derives {derived_key_id}). The file has been edited — ask Funis to re-issue."
        )

    issuer_key_id, issuer_verified = _check_issuer(
        document,
        identity,
        path=path,
        extra_issuer_keys=extra_issuer_keys or {},
        require_issuer=require_issuer,
    )

    return IssuedIdentity(
        install_id=str(identity["install_id"]),
        customer=str(identity["customer"]),
        environment=str(identity["environment"]),
        issued_at=_parse_timestamp(identity["issued_at"], path=path),
        report_key_seed=seed,
        report_key_id=derived_key_id,
        issuer_key_id=issuer_key_id,
        issuer_verified=issuer_verified,
        file_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise InstallIdentityError(
            f"No install-identity file at {path}. Set [compliance].identity_file to the "
            "file Funis issued for this install, or ask Funis for one."
        ) from exc
    except OSError as exc:
        raise InstallIdentityError(f"Could not read {path}: {exc}") from exc


def _parse_json(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallIdentityError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise InstallIdentityError(f"{path} must contain a JSON object at the top level.")
    return document


def _check_issuer(
    document: dict[str, Any],
    identity: dict[str, Any],
    *,
    path: Path,
    extra_issuer_keys: dict[str, str],
    require_issuer: bool,
) -> tuple[str, bool]:
    """Verify the Funis signature over the identity object.

    Returns ``(issuer_key_id, verified)``. Raises when verification is
    required and fails — an unsigned or badly-signed identity in
    production means someone minted their own install identity.
    """
    trusted = {**TRUSTED_ISSUER_KEYS, **extra_issuer_keys}
    issuer = document.get("issuer")
    issuer_key_id = str(issuer.get("key_id", "")) if isinstance(issuer, dict) else ""
    signature = str(issuer.get("signature", "")) if isinstance(issuer, dict) else ""

    if not issuer_key_id or not signature:
        if require_issuer:
            raise InstallIdentityError(
                f"{path} carries no issuer signature. A production install will not adopt "
                "an unsigned identity — ask Funis to re-issue it."
            )
        return (issuer_key_id or "unsigned", False)

    public_hex = trusted.get(issuer_key_id)
    if public_hex is None:
        if require_issuer:
            raise InstallIdentityError(
                f"{path} is signed by issuer key {issuer_key_id!r}, which this build does not "
                "trust. Either the file is not from Funis, or this build predates the key — "
                "add it to [compliance].issuer_public_keys if you know it to be genuine."
            )
        return (issuer_key_id, False)

    try:
        public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        public.verify(bytes.fromhex(signature), canonical_bytes({"identity": identity}))
    except (ValueError, InvalidSignature) as exc:
        raise InstallIdentityError(
            f"{path}: the issuer signature is not valid for issuer key {issuer_key_id!r}. "
            "The file has been altered since Funis signed it."
        ) from exc
    return (issuer_key_id, True)


def _parse_timestamp(value: Any, *, path: Path) -> _dt.datetime:
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise InstallIdentityError(f"{path}: issued_at {value!r} is not an ISO-8601 time.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)


async def current_identity(session: AsyncSession) -> InstallIdentity | None:
    """The adopted identity row, or ``None`` when nothing has been adopted."""
    return await session.get(InstallIdentity, SINGLETON_ID)


#: Length of the random part of a self-generated install id, in Crockford
#: base32 characters. 12 characters is ~60 bits — far beyond any chance of
#: collision across a customer base, and short enough to read down a phone.
_INSTALL_ID_CHARS = 12
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_install_id() -> str:
    """Mint a fresh ``inst_…`` identifier for this install.

    Crockford base32 (no I, L, O or U) because this string is read aloud,
    typed into an email and pasted into an issuing tool. Generated with
    :mod:`secrets` rather than :mod:`random`: it is not a secret, but a
    predictable id would let someone request a licence for an install
    that does not exist yet.
    """
    import secrets  # noqa: PLC0415

    body = "".join(secrets.choice(_CROCKFORD) for _ in range(_INSTALL_ID_CHARS))
    return f"inst_{body}"


async def ensure_install_identity(
    session: AsyncSession, *, now: _dt.datetime | None = None
) -> InstallIdentity:
    """Return this install's identity, minting one on first boot.

    **The install names itself, and Funis issues licences against that
    name** (ADR-0019 §A3, revised 2026-08-11). The reverse — Funis
    inventing the id and shipping it inside the licence — made the id an
    identifier for the *licence*: feed one licence to two deployments and
    both legitimately claim it, so binding proved nothing. Worse, an
    install that never ran ``compliance adopt`` had no identity at all
    and was exempt from binding entirely, which is precisely what a
    second instance would do.

    Minting locally fixes both. A second deployment generates a different
    id, so a licence naming the first one is refused at startup, and
    there is no "no identity yet" state to hide in.

    Idempotent, and safe to call on every boot. The row is created with
    no customer, no report key and no issuer — a licence supplies those
    later (:func:`bind_identity_to_licence`).
    """
    existing = await session.get(InstallIdentity, SINGLETON_ID)
    if existing is not None:
        return existing

    now = now or _dt.datetime.now(tz=_dt.UTC)
    row = InstallIdentity(
        id=SINGLETON_ID,
        install_id=new_install_id(),
        report_counter=0,
        adopted_at=now,
        self_generated=True,
    )
    session.add(row)
    await session.flush()
    return row


async def bind_identity_to_licence(
    session: AsyncSession,
    *,
    install_id: str,
    customer: str,
    environment: str,
    issued_at: _dt.datetime,
    report_key_id: str | None,
    issuer_key_id: str | None,
    manifest_sha256: str,
) -> InstallIdentity:
    """Fill in the Funis-supplied half of a self-generated identity.

    Called when a licence is installed. The licence is the thing that
    says who the customer is and which report key they were issued, so
    those fields arrive here rather than at first boot.

    Never changes ``install_id``: the licence has already been checked
    against it (:func:`matofx.licence.verify_install_binding`), and an
    identity that could be renamed by a file would be no identity at all.
    """
    row = await ensure_install_identity(session)
    if row.install_id != install_id:  # pragma: no cover - guarded upstream
        from matofx.compliance.errors import InstallIdentityError  # noqa: PLC0415

        raise InstallIdentityError(
            f"Refusing to bind a licence for {install_id!r} to install {row.install_id!r}."
        )
    row.customer = customer
    row.environment = environment
    row.issued_at = issued_at
    row.report_key_id = report_key_id
    row.issuer_key_id = issuer_key_id
    row.identity_sha256 = manifest_sha256
    await session.flush()
    return row


async def adopt_identity(
    session: AsyncSession,
    issued: IssuedIdentity,
    *,
    now: _dt.datetime,
) -> InstallIdentity:
    """Record ``issued`` as this install's identity, or refresh it in place.

    Three cases:

    - **Nothing adopted yet** — insert the singleton row, counter at 0.
    - **Same ``install_id``** — a re-issue (key rotation, corrected
      customer name). Update the key ids and file digest, **keep the
      counter**: the report sequence Funis holds must not restart.
    - **Different ``install_id``** — refuse. Either this database was
      cloned from another install, or the wrong identity file was
      supplied; both need a human, and silently re-badging a database
      would destroy the anchoring history that makes reports meaningful.
    """
    existing = await current_identity(session)
    if existing is None:
        row = InstallIdentity(
            id=SINGLETON_ID,
            install_id=issued.install_id,
            customer=issued.customer,
            environment=issued.environment,
            issued_at=issued.issued_at,
            report_key_id=issued.report_key_id,
            issuer_key_id=issued.issuer_key_id,
            identity_sha256=issued.file_sha256,
            report_counter=0,
            adopted_at=now,
        )
        session.add(row)
        await session.flush()
        return row

    if existing.install_id != issued.install_id:
        raise InstallIdentityError(
            f"This database already belongs to install {existing.install_id!r} "
            f"(adopted {existing.adopted_at}), but the supplied identity file is for "
            f"{issued.install_id!r}. Refusing to re-badge an existing install: if this "
            "database was copied from another deployment, Funis must issue a fresh "
            "identity for a fresh chain."
        )

    existing.customer = issued.customer
    existing.environment = issued.environment
    existing.issued_at = issued.issued_at
    existing.report_key_id = issued.report_key_id
    existing.issuer_key_id = issued.issuer_key_id
    existing.identity_sha256 = issued.file_sha256
    await session.flush()
    return existing


async def next_report_counter(session: AsyncSession) -> int:
    """Atomically increment and return the install's report counter.

    Uses a conditional UPDATE rather than read-modify-write so two
    concurrent report generations can't both claim the same counter —
    duplicate counters would look, to Funis, exactly like a withheld
    period.
    """
    result = await session.execute(
        sa.update(InstallIdentity)
        .where(InstallIdentity.id == SINGLETON_ID)
        .values(report_counter=InstallIdentity.report_counter + 1)
        .returning(InstallIdentity.report_counter)
    )
    counter = result.scalar_one_or_none()
    if counter is None:
        raise InstallIdentityError(
            "No install identity has been adopted yet. Run "
            "'matofx compliance adopt <identity-file>' first."
        )
    return int(counter)

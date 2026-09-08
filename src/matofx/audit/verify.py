"""Verification: walk the chain, recompute hashes, validate signatures.

Verification is the source of truth for audit-log integrity. The
application code never *trusts* the chain; it verifies it.

A walk that succeeds proves three things:

1. Every record's stored ``hash`` equals the SHA-256 of its content.
2. Every record's ``prior_hash`` equals the previous record's ``hash``
   (or :data:`GENESIS_HASH` for the first record).
3. Every record's ``signature``, when present, is a valid Ed25519
   signature of ``hash`` under the public key matching its ``key_id``.

Failure produces a :class:`VerifyResult` whose ``ok`` field is ``False``
and whose ``failures`` list names the offending record(s) and the reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select

from matofx.audit.hashing import GENESIS_HASH, compute_hash
from matofx.audit.orm import AuditLog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class VerifyFailure:
    """One thing that's wrong with the chain."""

    record_id: int
    reason: str


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a chain walk.

    ``ok`` is true only when every record passes hash and (if present)
    signature checks AND the chain links unbroken from genesis.
    """

    ok: bool
    records_checked: int
    failures: list[VerifyFailure] = field(default_factory=list)


async def verify(
    session: AsyncSession,
    *,
    public_keys: Mapping[str, Ed25519PublicKey] | None = None,
    start_id: int | None = None,
    end_id: int | None = None,
) -> VerifyResult:
    """Walk the audit chain and verify integrity.

    Args:
        session: A read-capable session bound to the audit DB.
        public_keys: Map of ``key_id`` → public key. Records whose
            ``key_id`` isn't in the map are reported as a verification
            failure (signed by an unknown key). When ``None`` or empty,
            signatures are not checked — useful in dev / for unsigned
            chains. Hash-chain integrity is always checked.
        start_id: Inclusive lower bound on row id. ``None`` starts from
            the very first record.
        end_id: Inclusive upper bound. ``None`` walks to the latest.

    Returns:
        A :class:`VerifyResult`. Successful walks have empty ``failures``.
    """
    stmt = select(AuditLog).order_by(AuditLog.id)
    if start_id is not None:
        stmt = stmt.where(AuditLog.id >= start_id)
    if end_id is not None:
        stmt = stmt.where(AuditLog.id <= end_id)

    result = await session.execute(stmt)
    records = list(result.scalars())

    failures: list[VerifyFailure] = []
    expected_prior = (
        await _initial_prior(session, start_id) if start_id is not None else GENESIS_HASH
    )

    for row in records:
        if row.prior_hash != expected_prior:
            failures.append(
                VerifyFailure(
                    record_id=row.id,
                    reason=(
                        f"prior_hash {row.prior_hash} does not match expected {expected_prior}"
                    ),
                )
            )

        recomputed = compute_hash(_record_to_dict(row))
        if recomputed != row.hash:
            failures.append(
                VerifyFailure(
                    record_id=row.id,
                    reason=f"hash mismatch: stored {row.hash}, computed {recomputed}",
                )
            )

        if row.signature is not None:
            if not public_keys:
                # A signed record with no public keys to verify against
                # is a real failure — silently passing here means an
                # operator could rotate keys without realising old
                # records can't be verified.
                failures.append(
                    VerifyFailure(
                        record_id=row.id,
                        reason=(
                            "signature present but no public keys supplied — "
                            "pass the install's public key set to verify, "
                            "or document this chain as unverifiable"
                        ),
                    )
                )
            else:
                failures.extend(_check_signature(row, public_keys))

        expected_prior = row.hash

    return VerifyResult(ok=not failures, records_checked=len(records), failures=failures)


def _check_signature(
    row: AuditLog,
    public_keys: Mapping[str, Ed25519PublicKey],
) -> list[VerifyFailure]:
    if row.key_id is None:
        return [
            VerifyFailure(
                record_id=row.id,
                reason="signature present but key_id is missing",
            )
        ]
    public_key = public_keys.get(row.key_id)
    if public_key is None:
        return [
            VerifyFailure(
                record_id=row.id,
                reason=f"signed by unknown key_id {row.key_id}",
            )
        ]
    try:
        public_key.verify(bytes.fromhex(row.signature or ""), bytes.fromhex(row.hash))
    except InvalidSignature:
        return [VerifyFailure(record_id=row.id, reason="invalid signature")]
    return []


def _record_to_dict(row: AuditLog) -> dict[str, object]:
    return {
        "schema_version": row.schema_version,
        "timestamp": row.timestamp,
        "actor": row.actor,
        "actor_type": row.actor_type,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "outcome": row.outcome,
        "request_id": row.request_id,
        "session_id": row.session_id,
        "source_ip": row.source_ip,
        "payload": row.payload,
        "reason": row.reason,
        "correlation_id": row.correlation_id,
        "key_id": row.key_id,
        "prior_hash": row.prior_hash,
    }


async def _initial_prior(session: AsyncSession, start_id: int) -> str:
    """When verifying a sub-range, the expected prior_hash is the previous record's hash."""
    stmt = select(AuditLog.hash).where(AuditLog.id < start_id).order_by(AuditLog.id.desc()).limit(1)
    earlier = (await session.execute(stmt)).scalar_one_or_none()
    return earlier if earlier is not None else GENESIS_HASH

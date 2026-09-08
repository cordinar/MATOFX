"""Append API for the audit chain.

:class:`AuditWriter` is the one object code uses to append records.
Build it once at process startup with :func:`make_audit_writer` (which
loads the configured signing key, or runs unsigned if none is
configured), then pass it into anything that needs to record an event.

The append flow:

1. Lock the singleton ``audit_chain_head`` row (``SELECT … FOR UPDATE``).
2. Read the latest hash from the head row.
3. Build the canonical record bytes (per :mod:`matofx.audit.hashing`).
4. Hash and (optionally) sign the bytes.
5. INSERT into ``audit_log``; UPDATE the head row.

Steps 1-5 happen inside the caller's session/transaction so the audit
record either commits with the work it audits or doesn't commit at all.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from matofx.audit.hashing import GENESIS_HASH, canonical_bytes, compute_hash
from matofx.audit.keys import compute_key_id, load_signing_key
from matofx.audit.orm import AuditChainHead, AuditLog
from matofx.audit.schema import CURRENT_SCHEMA_VERSION, RecordInput

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.config import AuditSettings


class AuditConfigError(Exception):
    """Raised when audit configuration is internally inconsistent."""


@dataclass(frozen=True)
class AuditWriter:
    """Holds the per-process signing state for audit appends.

    Construct via :func:`make_audit_writer`.
    """

    signing_key: Ed25519PrivateKey | None
    key_id: str | None

    @property
    def is_signed(self) -> bool:
        """``True`` when a signing key is loaded."""
        return self.signing_key is not None

    async def record(self, session: AsyncSession, payload: RecordInput) -> int:
        """Append a record to the audit chain. Returns its row id."""
        head = await _lock_head(session)
        prior_hash = head.last_hash
        timestamp = payload.timestamp or _now_utc()

        record_dict = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "timestamp": timestamp,
            "actor": payload.actor,
            "actor_type": payload.actor_type,
            "action": payload.action,
            "resource_type": payload.resource_type,
            "resource_id": payload.resource_id,
            "outcome": payload.outcome,
            "request_id": payload.request_id,
            "session_id": payload.session_id,
            "source_ip": payload.source_ip,
            "payload": payload.payload,
            "reason": payload.reason,
            "correlation_id": payload.correlation_id,
            "key_id": self.key_id,
            "prior_hash": prior_hash,
        }
        record_hash = compute_hash(record_dict)

        signature: str | None = None
        if self.signing_key is not None:
            signature = self.signing_key.sign(bytes.fromhex(record_hash)).hex()

        row = AuditLog(
            timestamp=timestamp,
            actor=payload.actor,
            actor_type=payload.actor_type,
            action=payload.action,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            outcome=payload.outcome,
            request_id=payload.request_id,
            session_id=payload.session_id,
            source_ip=payload.source_ip,
            prior_hash=prior_hash,
            hash=record_hash,
            signature=signature,
            key_id=self.key_id,
            schema_version=CURRENT_SCHEMA_VERSION,
            payload=payload.payload,
            reason=payload.reason,
            correlation_id=payload.correlation_id,
        )
        session.add(row)
        await session.flush()
        head.last_record_id = row.id
        head.last_hash = record_hash
        await session.flush()
        return row.id


def make_audit_writer(settings: AuditSettings) -> AuditWriter:
    """Build the per-process audit writer from the validated settings.

    - If ``signing_key_path`` is set, the key is loaded and signing is
      enabled.
    - If ``signing_key_path`` is unset and ``require_signing`` is true,
      raises :class:`AuditConfigError` with actionable guidance.
    - Otherwise returns an unsigned writer (chain is still hashed).
    """
    if settings.signing_key_path is not None:
        key = load_signing_key(settings.signing_key_path)
        return AuditWriter(signing_key=key, key_id=compute_key_id(key.public_key()))

    if settings.require_signing:
        raise AuditConfigError(
            "audit.require_signing = true but audit.signing_key_path is not set. "
            "Either configure a signing key (recommended for production) or "
            "set require_signing = false (acceptable only in dev / test)."
        )

    return AuditWriter(signing_key=None, key_id=None)


class ChainHeadMissingError(Exception):
    """Raised when the audit chain head is missing but records already exist.

    The chain has been tampered with; refuse to append rather than
    silently start a fresh chain. The plain-English message points
    the operator at ``matofx audit verify`` to investigate.
    """

    def __init__(self) -> None:
        """Construct with the canonical operator-facing message."""
        super().__init__(
            "Audit chain head row is missing but audit_log already has records. "
            "The chain has been tampered with — refusing to append. "
            "Run `matofx audit verify` to investigate, then restore the "
            "audit_chain_head row from a known-good backup."
        )


async def ensure_chain_head(session: AsyncSession) -> AuditChainHead:
    """Make sure the singleton chain-head row exists; create it if not.

    Called once during install / first-run; idempotent so re-running is
    safe. Returns the head row.

    Refuses to (re)create the head row if ``audit_log`` already has
    records — that combination indicates tampering, not a fresh
    install. Raises :class:`ChainHeadMissingError` in that case.
    """
    head = await session.get(AuditChainHead, 1)
    if head is None:
        await _refuse_if_records_exist(session)
        head = AuditChainHead(id=1, last_record_id=None, last_hash=GENESIS_HASH)
        session.add(head)
        await session.flush()
    return head


async def _lock_head(session: AsyncSession) -> AuditChainHead:
    stmt = select(AuditChainHead).where(AuditChainHead.id == 1).with_for_update()
    head = (await session.execute(stmt)).scalar_one_or_none()
    if head is None:
        # First-ever append on a fresh install OR the head row has been
        # tampered with. Refuse the second case loudly.
        await _refuse_if_records_exist(session)
        head = AuditChainHead(id=1, last_record_id=None, last_hash=GENESIS_HASH)
        session.add(head)
        await session.flush()
        head = (await session.execute(stmt)).scalar_one()
    return head


async def _refuse_if_records_exist(session: AsyncSession) -> None:
    """Raise if ``audit_log`` has records but the head row is missing."""
    existing = (await session.execute(select(AuditLog.id).limit(1))).scalar_one_or_none()
    if existing is not None:
        raise ChainHeadMissingError


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


# Re-export for convenience.
__all__ = [
    "AuditConfigError",
    "AuditWriter",
    "ChainHeadMissingError",
    "ensure_chain_head",
    "make_audit_writer",
]


def _used() -> tuple[type, ...]:
    # Keep canonical_bytes importable at module level for the verifier.
    return (canonical_bytes,)  # type: ignore[return-value]

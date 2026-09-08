"""TrustedPeerService — manage the destination's list of source-install fingerprints.

The trust list is the destination admin's statement of which source
installs may have bundles imported here. Adding and removing entries
is audit-logged; the actual verify-and-decide loop lives in the
import service (which queries :meth:`is_trusted` after the bundle's
signature is cryptographically verified).

Mutations are sole-source-of-truth audited via the same
:class:`AuditWriter` every other service uses. Action namespace:

* ``migrate.peer.trusted`` — a fingerprint was added to the trust list.
* ``migrate.peer.untrusted`` — a fingerprint was removed.

Both carry the canonical fingerprint, the human label, and the
free-text reason / notes in the audit payload. The audit row is
written **before** the destructive DB action so the chain captures
the deletion even if the row vanishes mid-request — same convention
as :class:`matofx.tutorials.service.TutorialService.delete`.

:meth:`mark_used` updates ``last_used_at`` on every successful import
that verifies against a given peer. Not audit-logged: the import
itself writes a ``migrate.bundle.imported`` event that carries the
source fingerprint, so a separate "peer used" row would be redundant.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from matofx.audit.schema import RecordInput
from matofx.migrate.errors import (
    TrustPeerAlreadyTrustedError,
    TrustPeerNotFoundError,
)
from matofx.migrate.orm import MigrateTrustedPeer
from matofx.migrate.signing import canonicalise_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


@dataclass(frozen=True)
class TrustedPeerService:
    """CRUD on :class:`MigrateTrustedPeer`, fully audited."""

    audit: AuditWriter

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_all(self, session: AsyncSession) -> list[MigrateTrustedPeer]:
        """Return every trust entry, most-recently-added first."""
        stmt = select(MigrateTrustedPeer).order_by(
            MigrateTrustedPeer.added_at.desc(), MigrateTrustedPeer.id.desc()
        )
        return list((await session.execute(stmt)).scalars())

    async def get_by_fingerprint(
        self, session: AsyncSession, fingerprint: str
    ) -> MigrateTrustedPeer | None:
        """Look up one row by canonical fingerprint. ``None`` if absent.

        Caller-supplied input is canonicalised before the lookup so
        any of the formats :func:`canonicalise_fingerprint` accepts
        works here. Returns ``None`` rather than raising so the import
        path can branch on absence without a try/except.
        """
        canonical = canonicalise_fingerprint(fingerprint)
        stmt = select(MigrateTrustedPeer).where(MigrateTrustedPeer.fingerprint == canonical)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def is_trusted(self, session: AsyncSession, fingerprint: str) -> bool:
        """``True`` when ``fingerprint`` (canonicalised) is in the trust list.

        Convenience predicate for the import path. Equivalent to
        ``get_by_fingerprint(...) is not None``.
        """
        return (await self.get_by_fingerprint(session, fingerprint)) is not None

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def add_peer(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        fingerprint: str,
        label: str,
        notes: str = "",
    ) -> MigrateTrustedPeer:
        """Add a new trust entry. Fingerprint canonicalised on the way in.

        Raises :class:`TrustPeerAlreadyTrustedError` if the canonical
        fingerprint is already present (the unique constraint would
        catch it too, but we translate the DB error into the
        plain-English form the UI quotes). Empty label is rejected by
        the DB CHECK; we reject it here too for a better message.
        """
        canonical = canonicalise_fingerprint(fingerprint)
        cleaned_label = label.strip()
        if not cleaned_label:
            raise _empty_label_error()
        cleaned_notes = notes.strip()

        existing = await self.get_by_fingerprint(session, canonical)
        if existing is not None:
            raise TrustPeerAlreadyTrustedError(canonical, existing.label)

        now = _utcnow()
        row = MigrateTrustedPeer(
            fingerprint=canonical,
            label=cleaned_label,
            added_by_user_id=actor_user_id,
            added_at=now,
            last_used_at=None,
            notes=cleaned_notes,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            # Race: someone added the same fingerprint between our
            # lookup and our flush. Re-fetch so the error message
            # mentions whoever beat us to it.
            await session.rollback()
            existing = await self.get_by_fingerprint(session, canonical)
            label_for_msg = existing.label if existing else cleaned_label
            raise TrustPeerAlreadyTrustedError(canonical, label_for_msg) from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="migrate.peer.trusted",
                resource_type="migrate_trusted_peer",
                resource_id=str(row.id),
                outcome="success",
                payload={
                    "fingerprint": canonical,
                    "label": cleaned_label,
                    "notes": cleaned_notes,
                },
            ),
        )
        return row

    async def remove_peer(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        fingerprint: str,
        reason: str,
    ) -> None:
        """Delete a trust entry by fingerprint.

        Raises :class:`TrustPeerNotFoundError` if the fingerprint
        isn't in the trust list (caller should validate the
        canonicalisation result against the list before calling for
        better UX; this is the safety net). The mandatory ``reason``
        rides in the audit payload so a removal is always
        explainable later.
        """
        canonical = canonicalise_fingerprint(fingerprint)
        row = await self.get_by_fingerprint(session, canonical)
        if row is None:
            raise TrustPeerNotFoundError(canonical)

        # Audit BEFORE delete so the chain captures the event regardless
        # of any flush-time failure. Mirror of the tutorial-delete pattern.
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="migrate.peer.untrusted",
                resource_type="migrate_trusted_peer",
                resource_id=str(row.id),
                outcome="success",
                reason=reason.strip() or None,
                payload={
                    "fingerprint": canonical,
                    "label": row.label,
                },
            ),
        )
        await session.delete(row)
        await session.flush()

    async def mark_used(self, session: AsyncSession, *, fingerprint: str) -> None:
        """Stamp ``last_used_at`` on the matching trust row.

        Called by the import path after a successful signature
        verification + trust-list match. Quiet no-op if the row has
        been removed since the verification (unlikely race; not worth
        an error). Not audit-logged — the import itself records the
        bundle-import event with the source fingerprint.
        """
        canonical = canonicalise_fingerprint(fingerprint)
        await session.execute(
            update(MigrateTrustedPeer)
            .where(MigrateTrustedPeer.fingerprint == canonical)
            .values(last_used_at=_utcnow())
        )


def make_trusted_peer_service(audit: AuditWriter) -> TrustedPeerService:
    """Wire up a :class:`TrustedPeerService` from a configured audit writer."""
    return TrustedPeerService(audit=audit)


def _empty_label_error() -> ValueError:
    """Internal helper — raised when ``add_peer`` gets an empty label.

    Kept out-of-class so the message stays alongside the others in the
    plain-English catalogue and so the trust-error class hierarchy
    isn't padded with one-off variants.
    """
    return ValueError(
        "A trusted-peer entry needs a non-empty label. The label is "
        "for your own future reference — typically the name of the "
        "source install, e.g. 'Staging' or 'Funis Dev'."
    )

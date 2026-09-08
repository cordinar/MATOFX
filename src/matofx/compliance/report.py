"""Generate, sign and archive a compliance report (ADR-0019 §B).

The artefact answers one question for the vendor: *is this install's
history the same history it previously attested to, and did it stay
inside its licence?* Three parts do that work:

1. **Anchoring** — a Merkle tree over the audit chain, plus an RFC 6962
   **consistency proof** from the tree size of *every* report this
   install has generated. Funis holds one of those sizes; the matching
   proof demonstrates that today's log is an append-only extension of
   the one they recorded. A rewind, a rebuild or a deletion cannot
   produce a valid proof. This is the load-bearing part.

   Proving from every past size, rather than only from the last report,
   is what makes an undelivered report harmless: the install cannot know
   which report the vendor actually received, so it proves from all of
   them and lets the vendor pick.
2. **Self-verification** — a full chain walk embedded verbatim,
   including failing record ids.
3. **Usage** — both licence types' figures, with the authoritative one
   named (:mod:`matofx.compliance.schema`).

Report generation never fails closed (ADR-0019 §B9): a broken chain, a
missing licence or a breach must still produce an artefact *saying so*.
Refusing to emit would destroy the evidence being asked for.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from matofx.audit import RecordInput, make_audit_writer, verify
from matofx.audit.keys import collect_public_keys
from matofx.audit.orm import AuditLog
from matofx.compliance.canonical import canonical_bytes, content_sha256, format_timestamp
from matofx.compliance.identity import (
    IssuedIdentity,
    current_identity,
    next_report_counter,
)
from matofx.compliance.integrity import current_status
from matofx.compliance.merkle import consistency_proof, hex_proof, tree_hash
from matofx.compliance.orm import ComplianceReport, InstallIdentity
from matofx.compliance.schema import (
    FAL,
    LICENCE_EVENT_ACTIONS,
    NOTABLE_EVENT_ACTIONS,
    NOTABLE_EVENT_CAP,
    PROOF_CAP,
    REPORT_SCHEMA_VERSION,
    LicenceType,
    UsageBlock,
    select_proof_sizes,
)
from matofx.compliance.usage import csl_usage, event_counts, fal_usage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.config.models import Settings


@dataclass(frozen=True)
class LicenceSnapshot:
    """What the licence subsystem knows, when it exists (slice 2+).

    Slice 1 passes ``None`` and the report says ``state: not_installed``
    — honest, and the document keeps its shape either way.
    """

    licence_id: str
    licence_type: LicenceType
    n_slots: int
    issued_at: _dt.datetime
    expires_at: _dt.datetime
    state: str
    manifest_sha256: str
    features_enabled: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Render as the report's ``licence`` block."""
        return {
            "state": self.state,
            "licence_id": self.licence_id,
            "licence_type": self.licence_type,
            "n_slots": self.n_slots,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "manifest_sha256": self.manifest_sha256,
            "features_enabled": sorted(self.features_enabled),
        }


_LICENCE_NOT_INSTALLED: dict[str, Any] = {
    "state": "not_installed",
    "licence_id": None,
    "licence_type": None,
    "n_slots": None,
    "issued_at": None,
    "expires_at": None,
    "manifest_sha256": None,
    "features_enabled": None,
}


@dataclass(frozen=True)
class GeneratedReport:
    """A signed report plus the row it was archived as."""

    document: dict[str, Any]
    counter: int
    content_sha256: str


async def generate_report(
    session: AsyncSession,
    *,
    settings: Settings,
    issued: IssuedIdentity,
    actor: str,
    now: _dt.datetime | None = None,
    period_from: _dt.datetime | None = None,
    licence: LicenceSnapshot | None = None,
    prove_from: int | None = None,
) -> GeneratedReport:
    """Build, sign and archive a compliance report.

    Args:
        session: Write-capable session; the counter increment, the
            archive row and the audit event share its transaction, so a
            failure part-way cannot burn a counter value.
        settings: For the audit signing keys used to self-verify.
        issued: Parsed identity file — supplies the signing key.
        actor: Who asked (``user:3``, ``cli``), recorded in the chain.
        now: Report timestamp; defaults to the current UTC time.
        period_from: Start of the reporting window. Defaults to the
            previous report's ``generated_at`` — so consecutive reports
            tile the timeline with no gap and no overlap — or to the
            first audit record for report 1.
        licence: Live licence state once slice 2 exists.
        prove_from: Force a consistency proof from this tree size, on top
            of the usual selection. For answering a vendor whose ledger
            entry falls in a gap left by proof thinning.
    """
    now = now or _dt.datetime.now(tz=_dt.UTC)
    identity_row = await current_identity(session)
    if identity_row is None:  # pragma: no cover - guarded by the CLI
        from matofx.compliance.errors import InstallIdentityError  # noqa: PLC0415

        raise InstallIdentityError(
            "No install identity adopted. Run 'matofx compliance adopt <identity-file>'."
        )

    previous = await _previous_report(session)
    period_start = period_from or await _default_period_start(session, previous, identity_row)
    # A period must never run backwards. Report 1 on a chain whose first
    # record post-dates `now` (or on an empty chain) would otherwise
    # produce from > to, and the window between adoption and the first
    # report would be covered by no report at all.
    period_start = min(period_start, now)

    genesis = await _record_summary(session, first=True)
    head = await _record_summary(session, first=False)
    head_id = int(head["id"]) if head["id"] is not None else 0

    leaves = await _chain_leaves(session)
    tree_size = len(leaves)
    tree_root = tree_hash(leaves).hex()
    proofs, proofs_truncated = await _consistency_proofs(session, leaves, prove_from=prove_from)
    integrity = await current_status(session, now=now)

    public_keys = (
        collect_public_keys(settings.audit.signing_key_path)
        if settings.audit.signing_key_path is not None
        else {}
    )
    chain = await verify(session, public_keys=public_keys or None, end_id=head_id or None)

    counts = await event_counts(
        session,
        actions=LICENCE_EVENT_ACTIONS,
        period_from=period_start,
        period_to=now,
    )
    usage = UsageBlock(
        licence_type=licence.licence_type if licence else None,
        n_slots=licence.n_slots if licence else None,
        csl=await csl_usage(session, period_from=period_start, period_to=now, counts=counts),
        # Enforced only when a FAL licence is actually in force —
        # otherwise the figure is a count, not a metered assignment.
        fal=await fal_usage(
            session, counts=counts, enforced=bool(licence and licence.licence_type == FAL)
        ),
    )
    notable, truncated = await _notable_events(session, period_from=period_start, period_to=now)

    counter = await next_report_counter(session)

    document: dict[str, Any] = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": now,
        "counter": counter,
        "install": {
            "install_id": identity_row.install_id,
            "customer": identity_row.customer,
            "environment": identity_row.environment,
            "app_version": _app_version(),
            "db_dialect": settings.db.dialect,
            "identity_sha256": identity_row.identity_sha256,
            "issuer_key_id": identity_row.issuer_key_id,
            "issuer_verified": issued.issuer_verified,
        },
        "licence": licence.to_dict() if licence else dict(_LICENCE_NOT_INSTALLED),
        "period": {"from": period_start, "to": now},
        "anchor": {
            "genesis_record": genesis,
            "previous_report": (
                None
                if previous is None
                else {
                    "counter": previous.counter,
                    "generated_at": previous.generated_at,
                    "head_id": previous.head_id,
                    "head_hash": previous.head_hash,
                }
            ),
            # Retained for human reading and for the HTML twin. It is no
            # longer what anchoring turns on — it is a self-assertion,
            # whereas the consistency proof below is checkable.
            "previous_head_rehashed_now": (
                None if previous is None else await _hash_at(session, previous.head_id)
            ),
            "head": head,
            "tree": {"algorithm": "rfc6962-sha256", "size": tree_size, "root": tree_root},
            "consistency_proofs": proofs,
            "consistency_proofs_truncated": proofs_truncated,
        },
        "chain_verification": {
            "range": {"start_id": 1 if head_id else None, "end_id": head_id or None},
            "records_checked": chain.records_checked,
            "ok": chain.ok,
            # When the install last verified itself *outside* this report.
            # An install that stops checking — a runner that died, an
            # API-only deployment — is otherwise invisible to the vendor,
            # who would keep reading "ok: True" from a report that
            # verified on demand and never routinely.
            "last_verified_at": integrity.checked_at,
            "last_verified_age_hours": (
                None if integrity.age_hours is None else round(integrity.age_hours, 1)
            ),
            "verification_is_current": not integrity.stale,
            "signing_keys_configured": sorted(public_keys),
            "failures": [
                {"record_id": f.record_id, "reason": f.reason}
                for f in chain.failures[:NOTABLE_EVENT_CAP]
            ],
            "failures_truncated": len(chain.failures) > NOTABLE_EVENT_CAP,
        },
        "usage": usage.to_dict(),
        "licence_events": counts,
        "notable_events": notable,
        "notable_events_truncated": truncated,
    }

    signature = issued.signing_key().sign(canonical_bytes(document))
    document["signature"] = {
        "alg": "ed25519",
        "key_id": issued.report_key_id,
        "value": signature.hex(),
        "covers": "every field except 'signature', canonicalised per matofx.compliance.canonical",
    }

    digest = content_sha256(document)
    session.add(
        ComplianceReport(
            counter=counter,
            generated_at=now,
            period_from=period_start,
            period_to=now,
            head_id=head_id,
            head_hash=str(head["hash"] or ""),
            tree_size=tree_size,
            tree_root=tree_root,
            chain_ok=chain.ok,
            licence_type=licence.licence_type if licence else None,
            content_sha256=digest,
            generated_by=actor,
            document=_jsonable(document),
        )
    )
    await session.flush()

    # Written *after* the head is captured, so this event belongs to the
    # next report's period — the chain records that a report was taken
    # without the report having to predict its own hash.
    writer = make_audit_writer(settings.audit)
    await writer.record(
        session,
        RecordInput(
            actor=actor,
            actor_type="user" if actor.startswith("user:") else "system",
            action="compliance.report.generated",
            resource_type="compliance_report",
            resource_id=str(counter),
            outcome="success",
            payload={
                "counter": counter,
                "head_id": head_id,
                "head_hash": head["hash"],
                "chain_ok": chain.ok,
                "content_sha256": digest,
            },
        ),
    )

    return GeneratedReport(document=_jsonable(document), counter=counter, content_sha256=digest)


async def _previous_report(session: AsyncSession) -> ComplianceReport | None:
    stmt = sa.select(ComplianceReport).order_by(ComplianceReport.counter.desc()).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def _default_period_start(
    session: AsyncSession,
    previous: ComplianceReport | None,
    identity: InstallIdentity,
) -> _dt.datetime:
    """Consecutive reports tile the timeline; report 1 starts at adoption.

    Report 1 deliberately reaches back to whichever is earlier of the
    first audit record and the moment the identity was adopted, so that
    commissioning activity — the first logins, the licence install — lands
    inside a reporting period instead of falling down the gap between
    "install exists" and "first report taken".
    """
    if previous is not None:
        return _aware(previous.generated_at)
    first = (
        await session.execute(sa.select(AuditLog.timestamp).order_by(AuditLog.id).limit(1))
    ).scalar_one_or_none()
    adopted = _aware(identity.adopted_at)
    return min(_aware(first), adopted) if first is not None else adopted


async def _chain_leaves(session: AsyncSession) -> list[bytes]:
    """Every audit record hash, in id order, as Merkle leaf data.

    One leaf per record, so tree size and record count are the same
    number — which is what lets a ledger entry written before this
    existed be backfilled from ``head_id`` (migration 0091).

    A hash that isn't valid hex is passed through as raw bytes rather
    than dropped: a corrupt row must change the tree, not silently
    vanish from it.
    """
    rows = (await session.execute(sa.select(AuditLog.hash).order_by(AuditLog.id))).scalars()
    leaves: list[bytes] = []
    for value in rows:
        text = str(value)
        try:
            leaves.append(bytes.fromhex(text))
        except ValueError:
            leaves.append(text.encode("utf-8"))
    return leaves


async def _consistency_proofs(
    session: AsyncSession,
    leaves: list[bytes],
    *,
    prove_from: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """A proof from each past report's tree size to the current one.

    The install cannot know which of its reports the vendor holds — one
    may have been previewed and never sent — so it proves from all of
    them. Each proof is ``O(log n)``; at the agreed one-or-two reports a
    year the whole block stays a few kilobytes.

    Past ``PROOF_CAP`` distinct sizes the list is thinned by
    :func:`select_proof_sizes`, which keeps the oldest and the newest and
    strides the middle. ``prove_from`` forces a specific size into the
    selection, so a vendor whose ledger entry falls in a thinned gap can
    always ask for a report that anchors to it.

    A past size larger than the current tree is emitted with a ``null``
    proof rather than omitted. The install is then *stating* that its log
    has shrunk, which is exactly the admission the verifier needs, and
    hiding it would look like a missing proof instead of a rewind.
    """
    stmt = (
        sa.select(ComplianceReport.counter, ComplianceReport.tree_size)
        .where(ComplianceReport.tree_size > 0)
        .order_by(ComplianceReport.counter.asc())
    )
    rows = list((await session.execute(stmt)).all())

    # One proof per distinct size: the verifier looks proofs up by tree
    # size, so a repeated size would be a duplicate answer to the same
    # question. Keep the earliest counter that attested each size.
    by_size: dict[int, int] = {}
    for counter, size in rows:
        by_size.setdefault(int(size), int(counter))

    ordered_sizes = sorted(by_size)
    kept = set(select_proof_sizes(ordered_sizes, PROOF_CAP))
    if prove_from is not None and prove_from in by_size:
        kept.add(prove_from)
    truncated = len(kept) < len(ordered_sizes)

    proofs: list[dict[str, Any]] = []
    for size in sorted(kept):
        entry: dict[str, Any] = {"counter": by_size[size], "tree_size": size}
        try:
            entry["proof"] = hex_proof(consistency_proof(leaves, size))
        except ValueError:
            entry["proof"] = None
        proofs.append(entry)
    return proofs, truncated


async def _record_summary(session: AsyncSession, *, first: bool) -> dict[str, Any]:
    """The first or last chain record as ``{id, hash, timestamp}``."""
    order = AuditLog.id.asc() if first else AuditLog.id.desc()
    row = (
        await session.execute(
            sa.select(AuditLog.id, AuditLog.hash, AuditLog.timestamp).order_by(order).limit(1)
        )
    ).first()
    if row is None:
        return {"id": None, "hash": None, "timestamp": None}
    return {"id": int(row[0]), "hash": str(row[1]), "timestamp": _aware(row[2])}


async def _hash_at(session: AsyncSession, record_id: int) -> str | None:
    """The hash currently stored at ``record_id`` — ``None`` if it's gone.

    This single value is what catches a rebuilt chain: Funis compares it
    against the head hash it recorded from the previous report.
    """
    return (
        await session.execute(sa.select(AuditLog.hash).where(AuditLog.id == record_id))
    ).scalar_one_or_none()


async def _notable_events(
    session: AsyncSession,
    *,
    period_from: _dt.datetime,
    period_to: _dt.datetime,
) -> tuple[list[dict[str, Any]], bool]:
    """Individually-listed events (capped), newest last."""
    stmt = (
        sa.select(AuditLog)
        .where(
            AuditLog.action.in_(sorted(NOTABLE_EVENT_ACTIONS)),
            AuditLog.timestamp >= period_from,
            AuditLog.timestamp <= period_to,
        )
        .order_by(AuditLog.id)
        .limit(NOTABLE_EVENT_CAP + 1)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    truncated = len(rows) > NOTABLE_EVENT_CAP
    return (
        [
            {
                "record_id": row.id,
                "timestamp": _aware(row.timestamp),
                "action": row.action,
                "actor": row.actor,
                "outcome": row.outcome,
                "reason": row.reason,
            }
            for row in rows[:NOTABLE_EVENT_CAP]
        ],
        truncated,
    )


def _app_version() -> str:
    from matofx import __version__  # noqa: PLC0415

    return __version__


def _jsonable(value: Any) -> Any:
    """Recursively convert datetimes so the document is JSON-native.

    Uses the same timestamp shape as the canonical bytes, so what is
    written to disk is byte-identical to what was signed.
    """
    if isinstance(value, _dt.datetime):
        return format_timestamp(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _aware(value: _dt.datetime) -> _dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=_dt.UTC)

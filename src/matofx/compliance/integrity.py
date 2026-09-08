"""Chain-integrity state and the acknowledgement that lifts it (ADR-0019 §C14).

A failed audit-chain verification degrades the install to **read-only**
until a Super User acknowledges it. That was the maintainer's call over
my recommendation, and it comes with two mitigations that are part of
the decision rather than optional extras:

1. **Verification at startup is incremental** — the head plus a recent
   window (:func:`verify_recent`) — so boot time doesn't scale with a
   90,000-record chain. The full walk runs on the runner's hourly tick
   and on demand (``matofx audit verify``).
2. **The acknowledgement is itself a chain event**, so lifting read-only
   shows up in the next compliance report. Without a liftable path a
   single disk fault would brick a customer permanently; without the
   event, lifting it would be invisible to the vendor. Both halves are
   needed.
3. **The attestation expires.** A recorded verdict says "the chain was
   intact when it was checked", so it cannot stand for ever: an install
   whose runner stopped would keep serving on an arbitrarily old verdict
   with nothing to say so. Past :data:`UNVERIFIED_AFTER_HOURS` the site
   degrades exactly as a failure would. Absence of checking is thereby
   self-enforcing, instead of something a separate watchdog has to
   notice — the OCSP ``nextUpdate`` pattern.

A restore from backup and a disk fault look identical to fraud from
here, which is exactly why the state is *recorded and reported* rather
than judged locally.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from matofx.audit import RecordInput
from matofx.compliance.orm import SINGLETON_ID, IntegrityState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter, VerifyResult

RECENT_WINDOW_RECORDS = 500
"""How many trailing records the *startup* check walks.

Enough to catch a truncated or rewritten tail — the shape a crashed
write or a partial restore leaves — without making boot time a function
of chain length. The hourly walk covers everything.
"""

STALE_AFTER_HOURS = 3
"""When a recorded verification stops counting as current.

Past this, administrators are told the chain is no longer being checked.
The hourly walk means a healthy install re-attests well inside the
window; only a checker that has actually stopped drifts past it.
"""

UNVERIFIED_AFTER_HOURS = 24
"""When a stale attestation degrades the site to read-only.

The integrity claim is "this chain was verified recently", and an
attestation that never expires cannot express that: an install whose
runner died would keep serving on a verdict from an arbitrarily long
time ago, and nothing would say so. Making the attestation *expire*
means the absence of checking is self-enforcing — the same reasoning as
an OCSP response's ``nextUpdate`` or a short-lived certificate, rather
than trying to detect the checker's death from outside.

Deliberately far longer than :data:`STALE_AFTER_HOURS`: a banner is
cheap and a read-only site is not, so there is a long window in which
somebody can notice and fix the runner before anything stops working.
Lifted by the same Super User acknowledgement as a failed check, so a
planned maintenance window is never a brick.
"""


@dataclass(frozen=True)
class IntegrityStatus:
    """Whether the chain is trusted, and whether that's been acknowledged."""

    ok: bool
    acknowledged: bool
    failed_at: _dt.datetime | None = None
    acknowledged_at: _dt.datetime | None = None
    acknowledged_by: str | None = None
    detail: str | None = None
    checked_at: _dt.datetime | None = None
    #: Hours since the last recorded verification. ``None`` when nothing
    #: has ever been checked, which is a *fresh install*, not staleness —
    #: an empty chain has nothing to contradict.
    age_hours: float | None = None

    @property
    def stale(self) -> bool:
        """Whether the last verification is too old to still stand for much."""
        return self.age_hours is not None and self.age_hours >= STALE_AFTER_HOURS

    @property
    def unverified(self) -> bool:
        """Whether the attestation has expired outright."""
        return self.age_hours is not None and self.age_hours >= UNVERIFIED_AFTER_HOURS

    @property
    def read_only(self) -> bool:
        """Read-only while a failure — or an expired attestation — stands.

        Staleness is gated on the acknowledgement too, so an operator who
        has to run without the checker for a while can say so once rather
        than being locked out repeatedly.
        """
        if self.acknowledged:
            return False
        return not self.ok or self.unverified

    @property
    def banner(self) -> str | None:
        """What administrators are told, or ``None`` when all is well."""
        if self.ok and not self.stale:
            return None
        if self.ok:
            return self._staleness_banner()
        if self.acknowledged:
            return (
                "The audit-record integrity check has failed and was acknowledged by a "
                "Super User. The site is writable again, but the failure is permanent in "
                "the record and will appear in the next compliance report."
            )
        return (
            "The audit-record integrity check has FAILED. The site is read-only until a "
            f"Super User acknowledges it. {self.detail or ''} "
            "This is most often a restore from backup or a disk fault — contact your "
            "supplier with the compliance report rather than clearing it blindly."
        ).strip()

    def _staleness_banner(self) -> str:
        """Told to administrators when checking has stopped, before it bites."""
        hours = int(self.age_hours or 0)
        if self.unverified:
            return (
                f"The audit-record integrity check has not run for {hours} hours, so the "
                "site can no longer attest that its records are intact and is read-only. "
                "This almost always means the MATOFX runner has stopped — start it, and "
                "the site recovers on its own at the next check. A Super User can "
                "acknowledge this to keep working in the meantime."
            )
        return (
            f"The audit-record integrity check last ran {hours} hours ago. It should run "
            "hourly, so the MATOFX runner has probably stopped. Records are not being "
            f"checked; after {UNVERIFIED_AFTER_HOURS} hours the site becomes read-only."
        )


async def current_status(
    session: AsyncSession, *, now: _dt.datetime | None = None
) -> IntegrityStatus:
    """Read the recorded integrity state, and how old it is.

    A missing row means *never checked*, which is a fresh install rather
    than a stale one — ``age_hours`` stays ``None`` so a site that has
    not yet had its first hourly tick is not degraded on day one.
    """
    row = await session.get(IntegrityState, SINGLETON_ID)
    if row is None:
        return IntegrityStatus(ok=True, acknowledged=False)
    now = now or _dt.datetime.now(tz=_dt.UTC)
    checked_at = _aware(row.checked_at)
    age_hours = None if checked_at is None else max(0.0, (now - checked_at).total_seconds() / 3600)
    return IntegrityStatus(
        ok=bool(row.chain_ok),
        acknowledged=row.acknowledged_at is not None,
        failed_at=row.failed_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        detail=row.detail,
        checked_at=checked_at,
        age_hours=age_hours,
    )


def _aware(value: _dt.datetime | None) -> _dt.datetime | None:
    """Treat a naive stored timestamp as UTC (SQLite drops the tzinfo)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=_dt.UTC)


async def verify_recent(
    session: AsyncSession,
    *,
    settings: object,
    audit: AuditWriter,
    window: int = RECENT_WINDOW_RECORDS,
    now: _dt.datetime | None = None,
) -> IntegrityStatus:
    """Walk the head plus a trailing window, and record the outcome.

    The cheap half of ADR-0019 §C14's cadence. A full walk is ``O(chain)``
    and unacceptable in a boot path or on a request-serving process, but
    the failure shapes that matter most in practice — a truncated tail, a
    partially-restored backup, a crashed write — all disturb the *end* of
    the chain, which a bounded window catches immediately.

    This exists because the full hourly walk lives in the runner, and an
    install whose runner is not running was therefore never checking its
    chain at all. API-only deployments now attest for themselves.
    """
    import sqlalchemy as sa  # noqa: PLC0415

    from matofx.audit import verify  # noqa: PLC0415
    from matofx.audit.keys import collect_public_keys  # noqa: PLC0415
    from matofx.audit.orm import AuditLog  # noqa: PLC0415

    head_id = (await session.execute(sa.select(sa.func.max(AuditLog.id)))).scalar_one_or_none()
    if head_id is None:
        # Nothing recorded yet: an empty chain is trivially intact, and
        # stamping a verification keeps a fresh install out of staleness.
        return await record_verify_result(
            session,
            result=await verify(session, public_keys=None),
            audit=audit,
            now=now,
        )

    audit_settings = getattr(settings, "audit", None)
    key_path = getattr(audit_settings, "signing_key_path", None)
    public_keys = collect_public_keys(key_path) if key_path is not None else {}

    start_id = max(1, int(head_id) - window + 1)
    result = await verify(
        session, public_keys=public_keys or None, start_id=start_id, end_id=int(head_id)
    )
    return await record_verify_result(session, result=result, audit=audit, now=now)


async def record_verify_result(
    session: AsyncSession,
    *,
    result: VerifyResult,
    audit: AuditWriter,
    now: _dt.datetime | None = None,
    actor: str = "system",
) -> IntegrityStatus:
    """Persist a verification outcome and audit any *transition*.

    Only transitions are written to the chain — an hourly ``failed``
    event for a month would bury the one that matters. A fresh failure
    clears any previous acknowledgement: acknowledging one problem must
    not silently absolve the next.
    """
    now = now or _dt.datetime.now(tz=_dt.UTC)
    row = await session.get(IntegrityState, SINGLETON_ID)
    if row is None:
        row = IntegrityState(id=SINGLETON_ID, chain_ok=True, checked_at=now)
        session.add(row)
        await session.flush()

    was_ok = bool(row.chain_ok)
    row.checked_at = now
    row.records_checked = result.records_checked
    row.chain_ok = result.ok

    if result.ok:
        if not was_ok:
            row.failed_at = None
            row.acknowledged_at = None
            row.acknowledged_by = None
            row.detail = None
        await session.flush()
        return await current_status(session)

    detail = _summarise(result)
    if was_ok:
        # New failure: stamp it and clear any stale acknowledgement.
        row.failed_at = now
        row.acknowledged_at = None
        row.acknowledged_by = None
    row.detail = detail
    await session.flush()

    if was_ok:
        await audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="system",
                action="audit.verify.failed",
                resource_type="audit_chain",
                resource_id="chain",
                outcome="failure",
                payload={
                    "records_checked": result.records_checked,
                    "failures": [
                        {"record_id": f.record_id, "reason": f.reason} for f in result.failures[:20]
                    ],
                },
                reason=detail,
            ),
        )
    return await current_status(session)


async def acknowledge(
    session: AsyncSession,
    *,
    audit: AuditWriter,
    actor: str,
    note: str,
    now: _dt.datetime | None = None,
) -> IntegrityStatus:
    """Lift read-only after a failed check — Super User only, and recorded.

    The caller enforces the role. ``note`` is required by the API: an
    acknowledgement with no explanation is worth very little to whoever
    reads the compliance report six months later.
    """
    now = now or _dt.datetime.now(tz=_dt.UTC)
    row = await session.get(IntegrityState, SINGLETON_ID)
    if row is None or row.chain_ok:
        return await current_status(session)
    row.acknowledged_at = now
    row.acknowledged_by = actor
    row.acknowledgement_note = note
    await session.flush()
    await audit.record(
        session,
        RecordInput(
            actor=actor,
            actor_type="user",
            action="audit.verify.acknowledged",
            resource_type="audit_chain",
            resource_id="chain",
            outcome="success",
            payload={"failed_at": row.failed_at.isoformat() if row.failed_at else None},
            reason=note,
        ),
    )
    return await current_status(session)


def _summarise(result: VerifyResult) -> str:
    """One line an operator can act on: how many, and the first one."""
    count = len(result.failures)
    if not count:
        return "Verification failed with no recorded reason."
    first = result.failures[0]
    tail = f" (and {count - 1} more)" if count > 1 else ""
    return f"Record {first.record_id}: {first.reason}{tail}."

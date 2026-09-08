"""CSL concurrent-session slot accounting (ADR-0019 slice 4, brief §C16).

A **Concurrent Session Licence** grants N sessions at once. A slot is
consumed at login and released at logout, idle timeout, hard timeout, or
admin revocation — which is why the idle-logout behaviour is load-bearing
here: without it, a user who walks away holds a place indefinitely.

Per §C16 the licence module is the single authority, so the auth layer
asks :func:`admit_session` and acts on the answer; it never counts
sessions or reads a manifest itself.

## The four outcomes

``granted``      a slot was free
``breakglass``   at cap, but the user is a Super User — admitted anyway
``denied``       at cap, and the user is not a Super User
``unmetered``    no cap applies (no CSL licence, or read-only mode)

**Break-glass** (ADR-0019 §C13) exists because the alternative is an
administrator locked out of their own install exactly when the licence
needs fixing. It is recorded as its own event and broken out in the
compliance report, so an excess over ``n_slots`` is *explained* rather
than hidden — the report says "peak 11/10, 1 break-glass".

**Read-only mode is unmetered** (ADR-0019 §C12): there is no metered work
to protect when nothing can be submitted, and "every registered user may
sign in and view results" cannot hold if the cap still turned people
away.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa

from matofx.audit import RecordInput
from matofx.licence.models import CSL
from matofx.users.orm import AuthSession
from matofx.users.settings_overlay import effective_session_policy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.config.models import Settings
    from matofx.licence.service import LicenceResolution
    from matofx.users.orm import User

SlotOutcome = Literal["granted", "breakglass", "denied", "unmetered"]

SUPER_USER_ROLE = "super_user"


@dataclass(frozen=True)
class SlotDecision:
    """Whether a login may proceed, and what it cost."""

    outcome: SlotOutcome
    in_use: int
    n_slots: int | None

    @property
    def admitted(self) -> bool:
        """``True`` for every outcome except ``denied``."""
        return self.outcome != "denied"

    @property
    def message(self) -> str | None:
        """Plain-English refusal text, or ``None`` when admitted.

        Written for the person staring at a login form: what happened,
        why, and what they can do — not "quota exceeded".
        """
        if self.outcome != "denied":
            return None
        return (
            f"All {self.n_slots} MATOFX places are in use right now. Someone needs to sign "
            "out before you can sign in — or ask an administrator, who can free a place "
            "from Active sessions."
        )


async def count_live_sessions(
    session: AsyncSession,
    *,
    settings: Settings,
    now: _dt.datetime | None = None,
) -> int:
    """Sessions currently holding a slot.

    A row holds a slot while it is un-revoked **and** still within both
    timeouts. Using the same liveness rule as
    :meth:`matofx.users.service.SessionService.validate` matters: if this
    counted rows that ``validate`` would reject, the licence would refuse
    logins on behalf of sessions that are already dead.
    """
    now = now or _dt.datetime.now(tz=_dt.UTC)
    policy = await effective_session_policy(session, settings.auth)
    idle_cutoff = now - _dt.timedelta(minutes=policy.idle_timeout_minutes)
    hard_cutoff = now - _dt.timedelta(hours=policy.hard_timeout_hours)
    stmt = sa.select(sa.func.count(AuthSession.id)).where(
        AuthSession.revoked_at.is_(None),
        AuthSession.last_activity_at >= idle_cutoff,
        AuthSession.created_at >= hard_cutoff,
    )
    return int((await session.execute(stmt)).scalar_one())


async def admit_session(
    session: AsyncSession,
    *,
    settings: Settings,
    resolution: LicenceResolution,
    user: User,
    role_name: str,
    audit: AuditWriter,
    ip_address: str | None = None,
    now: _dt.datetime | None = None,
) -> SlotDecision:
    """Provisionally decide whether ``user`` may take a session slot.

    Called *after* credentials are verified and *before* the session row
    is created, so a refused login never leaves a session behind.

    **This decision is provisional.** Counting live sessions and
    inserting the new one are two statements, and concurrent logins at
    the cap boundary can all pass this check — so the caller must follow
    a successful decision with :func:`confirm_admission` once the row
    exists. Only a *refusal* is recorded here; a provisional grant is
    recorded on confirmation, so a login that gets bounced never leaves
    a ``granted`` event behind claiming otherwise.
    """
    now = now or _dt.datetime.now(tz=_dt.UTC)
    decision = await _decide(
        session, settings=settings, resolution=resolution, role_name=role_name, now=now
    )
    if decision.outcome == "denied":
        await _record(
            session,
            audit=audit,
            user=user,
            decision=decision,
            ip_address=ip_address,
            resolution=resolution,
        )
    return decision


async def confirm_admission(
    session: AsyncSession,
    *,
    settings: Settings,
    resolution: LicenceResolution,
    user: User,
    decision: SlotDecision,
    session_id: int,
    audit: AuditWriter,
    ip_address: str | None = None,
    now: _dt.datetime | None = None,
) -> SlotDecision:
    """Re-check the cap now the session row exists; revoke if we went over.

    :func:`admit_session` counts and the caller inserts — two statements
    with no lock between them, so N concurrent logins at the boundary can
    all be told there is room. This closes that window without a
    dialect-specific lock (the first client runs SQLite, which has no row
    locking to take).

    The loser is chosen by **rank, not by re-count**: this session's
    position among live sessions ordered by ``(created_at, id)`` is
    compared against ``n_slots``. Bouncing everyone who merely *observes*
    an over-cap count would over-correct — five simultaneous logins into
    the last place would all withdraw and leave it empty. Ranking bounces
    exactly the excess, and does so identically whichever request asks
    first.

    Break-glass admissions are never bounced: a Super User admitted over
    the cap by design (§C13) is the mechanism for recovering an install,
    and racing it away would defeat the point.
    """
    now = now or _dt.datetime.now(tz=_dt.UTC)
    if decision.outcome in ("unmetered", "denied"):
        return decision

    if decision.outcome == "granted" and decision.n_slots is not None:
        rank = await _rank_of(session, session_id=session_id, settings=settings, now=now)
        if rank > decision.n_slots:
            await _revoke(session, session_id=session_id, now=now)
            final = SlotDecision(outcome="denied", in_use=rank - 1, n_slots=decision.n_slots)
            await _record(
                session,
                audit=audit,
                user=user,
                decision=final,
                ip_address=ip_address,
                resolution=resolution,
                reason=(
                    "admitted provisionally, then withdrawn: a concurrent login took the last place"
                ),
            )
            return final

    if decision.outcome == "breakglass":
        # Mark the row, not just the chain event: the Active sessions
        # page needs to point at the over-cap sessions, and the session
        # policy needs to time-box them.
        await session.execute(
            sa.update(AuthSession).where(AuthSession.id == session_id).values(breakglass=True)
        )
        await session.flush()

    await _record(
        session,
        audit=audit,
        user=user,
        decision=decision,
        ip_address=ip_address,
        resolution=resolution,
    )
    return decision


async def _rank_of(
    session: AsyncSession,
    *,
    session_id: int,
    settings: Settings,
    now: _dt.datetime,
) -> int:
    """This session's 1-based position among live sessions by age.

    Ordered by ``(created_at, id)`` so the ordering is total even when
    two rows share a timestamp — which they will, on a fast machine
    under exactly the burst this exists to handle.
    """
    row = (
        await session.execute(
            sa.select(AuthSession.created_at, AuthSession.id).where(AuthSession.id == session_id)
        )
    ).first()
    if row is None:  # pragma: no cover - the caller just inserted it
        return 1
    created_at, ident = row

    policy = await effective_session_policy(session, settings.auth)
    idle_cutoff = now - _dt.timedelta(minutes=policy.idle_timeout_minutes)
    hard_cutoff = now - _dt.timedelta(hours=policy.hard_timeout_hours)
    ahead = sa.select(sa.func.count(AuthSession.id)).where(
        AuthSession.revoked_at.is_(None),
        AuthSession.last_activity_at >= idle_cutoff,
        AuthSession.created_at >= hard_cutoff,
        sa.or_(
            AuthSession.created_at < created_at,
            sa.and_(AuthSession.created_at == created_at, AuthSession.id < ident),
        ),
    )
    return int((await session.execute(ahead)).scalar_one()) + 1


async def _revoke(session: AsyncSession, *, session_id: int, now: _dt.datetime) -> None:
    """Withdraw a provisionally-created session row."""
    await session.execute(
        sa.update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.flush()


async def _decide(
    session: AsyncSession,
    *,
    settings: Settings,
    resolution: LicenceResolution,
    role_name: str,
    now: _dt.datetime,
) -> SlotDecision:
    status = resolution.status
    if status is None or status.manifest.licence_type != CSL or resolution.read_only:
        # No CSL licence, or read-only mode: nothing to meter.
        return SlotDecision(outcome="unmetered", in_use=0, n_slots=None)

    n_slots = status.manifest.n_slots
    in_use = await count_live_sessions(session, settings=settings, now=now)
    if in_use < n_slots:
        return SlotDecision(outcome="granted", in_use=in_use, n_slots=n_slots)
    if role_name == SUPER_USER_ROLE:
        return SlotDecision(outcome="breakglass", in_use=in_use, n_slots=n_slots)
    return SlotDecision(outcome="denied", in_use=in_use, n_slots=n_slots)


_ACTIONS: dict[str, str] = {
    "granted": "licence.slot.granted",
    "breakglass": "licence.slot.breakglass",
    "denied": "licence.slot.denied",
}


async def _record(
    session: AsyncSession,
    *,
    audit: AuditWriter,
    user: User,
    decision: SlotDecision,
    ip_address: str | None,
    resolution: LicenceResolution,
    reason: str | None = None,
) -> None:
    licence_id = resolution.status.manifest.licence_id if resolution.status is not None else "none"
    await audit.record(
        session,
        RecordInput(
            actor=f"user:{user.id}",
            actor_type="user",
            action=_ACTIONS[decision.outcome],
            resource_type="licence",
            resource_id=licence_id,
            outcome="success" if decision.admitted else "denied",
            source_ip=ip_address,
            payload={
                "in_use": decision.in_use,
                "n_slots": decision.n_slots,
                "username": user.username,
            },
            reason=reason
            or (
                "admitted over the cap as a Super User"
                if decision.outcome == "breakglass"
                else None
            ),
        ),
    )


async def release_slot(
    session: AsyncSession,
    *,
    audit: AuditWriter,
    resolution: LicenceResolution,
    user_id: int,
    session_id: int,
    actor: str,
    reason: str,
) -> None:
    """Record that a slot went back to the pool.

    Called on logout and on admin revocation. Only meaningful under a
    CSL licence — under FAL the places are assignments, not sessions —
    so it is a no-op otherwise rather than filling the chain with events
    that mean nothing.
    """
    status = resolution.status
    if status is None or status.manifest.licence_type != CSL:
        return
    await audit.record(
        session,
        RecordInput(
            actor=actor,
            actor_type="user" if actor.startswith("user:") else "system",
            action="licence.slot.released",
            resource_type="session",
            resource_id=str(session_id),
            outcome="success",
            payload={"user_id": user_id, "licence_id": status.manifest.licence_id},
            reason=reason,
        ),
    )

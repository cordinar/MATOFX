"""FAL floating-assignment accounting (ADR-0019 slice 5, brief §C16).

A **Floating Access Licence** grants N *access-rights assignments*.
Unlike CSL's minute-to-minute churn, this counter moves only when an
administrator acts — which is the whole point of the type: a settled
group of named users, and a number that doesn't surprise you.

## What a "floating assignment" is in this codebase

It already exists: **a user account that can sign in**. MATOFX has
carried a logon-access lifecycle since migration 0009 —
``user.is_active`` with ``logon_granted_at`` / ``logon_revoked_at`` and
an append-only ``user_logon_event`` history. §C16's FAL description
("users without an assignment exist in the system — their account,
projects and settings persist — but cannot log in") is a precise
description of a deactivated user.

So this slice adds no new concept and no new column. It puts a **cap**
on the one that was already there, and makes the compliance report's
``assignments_current`` authoritative rather than the
``enabled_accounts`` proxy slice 1 shipped with.

## Where the gate sits

Granting is the metered action, so the gate runs when an admin
*activates* a user — creating an active one, or reactivating a
deactivated one. Revoking is free and always allowed: an install must
always be able to get back under its cap.

Read-only mode is unmetered, for the same reason as CSL (§C12): a site
that can't submit work shouldn't also be unable to re-enable an account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa

from matofx.audit import RecordInput
from matofx.licence.models import FAL
from matofx.users.orm import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.licence.service import LicenceResolution

AssignmentOutcome = Literal["granted", "denied", "unmetered"]


@dataclass(frozen=True)
class AssignmentDecision:
    """Whether an account may be given access, and what it costs."""

    outcome: AssignmentOutcome
    in_use: int
    n_slots: int | None

    @property
    def admitted(self) -> bool:
        """``True`` unless the licence has no assignment left."""
        return self.outcome != "denied"

    @property
    def message(self) -> str | None:
        """Plain-English refusal for the administrator, or ``None``.

        Names the cap and the remedy, because the person reading it is
        mid-task trying to onboard a colleague.
        """
        if self.outcome != "denied":
            return None
        return (
            f"Your licence covers {self.n_slots} people with access, and all "
            f"{self.n_slots} are assigned. Remove access from someone who no longer "
            "needs it, or ask your supplier to extend the licence."
        )


async def count_assignments(session: AsyncSession) -> int:
    """Accounts that currently hold access (i.e. can sign in)."""
    stmt = sa.select(sa.func.count(User.id)).where(User.is_active.is_(True))
    return int((await session.execute(stmt)).scalar_one())


async def admit_assignment(
    session: AsyncSession,
    *,
    resolution: LicenceResolution,
    audit: AuditWriter,
    actor: str,
    target_username: str,
    target_user_id: int | None = None,
) -> AssignmentDecision:
    """Decide whether one more account may be given access, and record it.

    Call **before** flipping ``is_active`` so a refusal leaves the
    account exactly as it was.
    """
    status = resolution.status
    if status is None or status.manifest.licence_type != FAL or resolution.read_only:
        return AssignmentDecision(outcome="unmetered", in_use=0, n_slots=None)

    n_slots = status.manifest.n_slots
    in_use = await count_assignments(session)
    outcome: AssignmentOutcome = "granted" if in_use < n_slots else "denied"
    decision = AssignmentDecision(outcome=outcome, in_use=in_use, n_slots=n_slots)

    await audit.record(
        session,
        RecordInput(
            actor=actor,
            actor_type="user" if actor.startswith("user:") else "system",
            action=(
                "licence.assignment.granted"
                if decision.admitted
                else "licence.assignment.denied"
            ),
            resource_type="licence",
            resource_id=status.manifest.licence_id,
            outcome="success" if decision.admitted else "denied",
            payload={
                "in_use": in_use,
                "n_slots": n_slots,
                "username": target_username,
                "user_id": target_user_id,
            },
        ),
    )
    return decision


async def release_assignment(
    session: AsyncSession,
    *,
    resolution: LicenceResolution,
    audit: AuditWriter,
    actor: str,
    target_username: str,
    target_user_id: int,
) -> None:
    """Record that an assignment went back to the pool.

    Never gated — getting back *under* a cap must always be possible.
    A no-op unless a FAL licence is in force, so the chain doesn't fill
    with events that mean nothing on a CSL install.
    """
    status = resolution.status
    if status is None or status.manifest.licence_type != FAL:
        return
    await audit.record(
        session,
        RecordInput(
            actor=actor,
            actor_type="user" if actor.startswith("user:") else "system",
            action="licence.assignment.revoked",
            resource_type="licence",
            resource_id=status.manifest.licence_id,
            outcome="success",
            payload={"username": target_username, "user_id": target_user_id},
        ),
    )

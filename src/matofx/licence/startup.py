"""Startup licence check + the periodic re-check body (ADR-0019 slice 3).

§C16's enforcement cadence is "at app startup; every N minutes; at every
login". This module owns the first two; the login check arrives with slot
enforcement in slice 4.

The startup check is where **fail-closed** actually bites (§C11): a
tampered, untrusted or wrong-machine licence stops the process before it
serves a request. Everything else — a missing licence, an expired one —
is logged loudly and left to read-only mode, because §C16 forbids a
lapsed renewal taking a customer offline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from matofx.licence.errors import LicenceInvalidError
from matofx.licence.service import LicenceResolution, record_validation, resolve_licence

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.config.models import Settings

_log = logging.getLogger(__name__)


def check_licence_at_startup(settings: Settings) -> LicenceResolution:
    """Resolve the licence during boot, refusing to start on tampering.

    Returns the resolution so the caller can apply feature gating and
    (slice 6) put the site into read-only mode.

    Raises:
        LicenceInvalidError: The licence material is invalid — edited,
            signed by an untrusted key, or issued for another machine.
            Deliberately fatal: continuing would mean honouring terms
            nobody granted.
    """
    resolution = resolve_licence(settings)
    if resolution.fail_closed:
        _log.error("Refusing to start: %s", resolution.problem)
        raise LicenceInvalidError(resolution.problem or "The licence is not valid.")
    _log_state(resolution, at="startup")
    return resolution


async def recheck_licence(
    session: AsyncSession,
    *,
    settings: Settings,
    previous: LicenceResolution | None = None,
) -> LicenceResolution:
    """One periodic re-check tick: resolve, audit, log any transition.

    Never raises — a running install must not be killed mid-flight by a
    licence file that changed underneath it. A licence that has become
    invalid is recorded as a failed validation and (slice 6) flips the
    site read-only; the operator sees it in the banner and the next
    compliance report, which is the intended escalation path.

    Every tick is recorded, not just changes. §C16 asks for it, and the
    steady drum of ``licence.validated.ok`` is itself evidence: a gap in
    the run, or a sudden switch to ``failed``, is exactly the signal a
    compliance report should carry.
    """
    resolution = resolve_licence(settings)
    await record_validation(session, settings=settings, resolution=resolution)
    if previous is not None:
        await _record_transitions(session, settings=settings, before=previous, after=resolution)
    if previous is None or _summary(previous) != _summary(resolution):
        _log_state(resolution, at="re-check")
    return resolution


async def _record_transitions(
    session: AsyncSession,
    *,
    settings: Settings,
    before: LicenceResolution,
    after: LicenceResolution,
) -> None:
    """Audit the *changes* between two re-checks (ADR-0019 slice 7).

    Transitions only. The per-tick ``licence.validated.ok`` already
    proves the check ran; what a compliance report needs on top is the
    handful of moments the site's posture actually changed — the day it
    entered grace, the day it went read-only, the day a renewal lifted
    it again. Those are the rows a reader scans for.
    """
    from matofx.audit import RecordInput, make_audit_writer  # noqa: PLC0415

    events: list[tuple[str, str, dict[str, object]]] = []

    before_state = before.status.state if before.status else None
    after_state = after.status.state if after.status else None
    if before_state != after_state and after_state is not None:
        events.append(
            (
                "licence.expiry.transition",
                f"{before_state or 'unusable'} -> {after_state}",
                {"from": before_state, "to": after_state},
            )
        )

    if before.read_only != after.read_only:
        action = "licence.readonly.entered" if after.read_only else "licence.readonly.left"
        events.append(
            (
                action,
                after.banner or after.problem or "",
                {"state": after_state, "fail_closed": after.fail_closed},
            )
        )

    if not events:
        return
    writer = make_audit_writer(settings.audit)
    licence_id = after.status.manifest.licence_id if after.status else "none"
    for action, reason, payload in events:
        await writer.record(
            session,
            RecordInput(
                actor="system",
                actor_type="system",
                action=action,
                resource_type="licence",
                resource_id=licence_id,
                outcome="success",
                payload=payload,
                reason=reason or None,
            ),
        )


def _summary(resolution: LicenceResolution) -> tuple[str, str | None]:
    """What counts as a change worth logging again."""
    if resolution.status is not None:
        return (resolution.status.state, resolution.status.manifest.licence_id)
    return ("unusable", resolution.problem)


def _log_state(resolution: LicenceResolution, *, at: str) -> None:
    if resolution.status is None:
        _log.warning("Licence %s: %s", at, resolution.problem)
        return
    status = resolution.status
    manifest = status.manifest
    _log.info(
        "Licence %s: %s — %s (%s, %d slot(s)), expires %s",
        at,
        status.state,
        manifest.licence_id,
        manifest.licence_type.upper(),
        manifest.n_slots,
        manifest.expires_at.date().isoformat(),
    )
    if status.banner:
        _log.warning("%s", status.banner)

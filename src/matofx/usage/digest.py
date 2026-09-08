"""Email the Usage Statistics report to chosen recipients.

The digest reuses the export builders (:mod:`matofx.usage.export`) so a
recipient's email is exactly the printable report the page produces —
the caller renders the HTML and hands it here; this service only fans it
out over SMTP.

Scope (this slice): **send now**. Recurring delivery needs the Arq cron
worker, which isn't wired in v2 yet; when it lands it will read a stored
schedule and call :meth:`DigestService.send` with the same arguments, so
the compose-and-send path is already the durable seam. Until then an
admin triggers a send from the page on demand.

Send is best-effort + per-recipient isolated, mirroring
:class:`matofx.notify.BroadcastService`: one SMTP connection is reused
for the whole list, a per-recipient failure is recorded and the fan-out
continues, and the outcome is audited.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from matofx.audit.schema import RecordInput
from matofx.notify.smtp import (
    SmtpSession,
    assemble_message,
    resolve_effective_email_settings,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.config import EmailSettings

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DigestResult:
    """Outcome summary for one digest send."""

    disabled: bool
    """True when the email subsystem is off (``email.enabled = false``);
    nothing was attempted."""

    attempted: int
    sent: int
    failed: list[dict[str, str]] = field(default_factory=list)
    """``[{"email": ..., "reason": ...}]`` for each recipient that errored."""


@dataclass(frozen=True)
class DigestService:
    """Send a pre-rendered usage report to a list of recipients over SMTP."""

    audit: AuditWriter
    settings: EmailSettings

    async def send(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        recipients: list[str],
        subject: str,
        html_body: str,
        text_body: str,
    ) -> DigestResult:
        """Send ``subject`` / ``html_body`` to each address in ``recipients``."""
        from matofx.config.general_service import (  # noqa: PLC0415
            load_general_setting_overrides,
        )

        addresses = _dedupe(recipients)
        if not self.settings.enabled:
            await self._audit(
                session,
                actor_user_id=actor_user_id,
                outcome="success",
                payload={"reason": "email subsystem disabled", "attempted": 0},
            )
            return DigestResult(disabled=True, attempted=0, sent=0)
        if not addresses:
            return DigestResult(disabled=False, attempted=0, sent=0)

        overrides = await load_general_setting_overrides(session)
        effective = resolve_effective_email_settings(self.settings, overrides)

        failed: list[dict[str, str]] = []
        sent = 0
        try:
            async with SmtpSession(effective) as smtp:
                for address in addresses:
                    message = assemble_message(
                        from_address=effective["from_address"],
                        to_address=address,
                        subject=subject,
                        text_body=text_body,
                        html_body=html_body,
                    )
                    try:
                        await smtp.send(message)
                        sent += 1
                    except Exception as exc:  # per-recipient isolation
                        _log.warning("usage.digest.recipient_failed")
                        failed.append({"email": address, "reason": str(exc)})
        except Exception as exc:  # connection-level failure
            _log.exception("usage.digest.connection_failed")
            failed = [{"email": address, "reason": str(exc)} for address in addresses]
            sent = 0

        await self._audit(
            session,
            actor_user_id=actor_user_id,
            outcome="success" if not failed else "failure",
            payload={
                "subject": subject,
                "attempted": len(addresses),
                "sent": sent,
                "failed": failed,
            },
        )
        return DigestResult(disabled=False, attempted=len(addresses), sent=sent, failed=failed)

    async def _audit(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        outcome: Literal["success", "failure"],
        payload: dict[str, object],
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="usage.digest.sent",
                resource_type="usage_digest",
                resource_id="-",
                outcome=outcome,
                payload=payload,
            ),
        )


def _dedupe(recipients: list[str]) -> list[str]:
    """Trim, drop blanks, and de-duplicate (case-insensitive) addresses."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in recipients:
        address = (raw or "").strip()
        if address and address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
    return out


def make_digest_service(audit: AuditWriter, settings: EmailSettings) -> DigestService:
    """Factory: build a :class:`DigestService`."""
    return DigestService(audit=audit, settings=settings)

"""Admin broadcast email — one composed message fanned out to many users.

Distinct from the job-state :class:`~matofx.notify.email.EmailNotifier`:

* **Trigger** — an administrator on ``Admin → Send Notifications``,
  not the runner. Gated by ``Permission.NOTIFICATIONS_SEND``.
* **Recipients** — every registered user, or only those whose account
  is currently enabled. The per-user ``email_notifications_enabled``
  opt-out is *job-state only* (migration 0016) and deliberately does
  not suppress admin broadcasts — these are announcements (planned
  downtime, policy changes) every user is expected to receive. The
  job-notification master switch is likewise irrelevant here.
* **Transport** — one SMTP connection reused for the whole audience
  (:class:`~matofx.notify.smtp.SmtpSession`); relays rate-limit logins.
* **Body** — HTML wrapped in the persisted header/footer template
  (text + optional inline images), with a plain-text fallback, plus
  any uploaded file attachments.

Send is best-effort and synchronous: a per-recipient failure is
recorded and the fan-out continues; the caller gets a
:class:`BroadcastResult` summarising sent vs failed.
"""

from __future__ import annotations

import html as _html
import logging
import mimetypes
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.notify.smtp import (
    Attachment,
    InlineImage,
    SmtpSession,
    assemble_message,
    resolve_effective_email_settings,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.config import EmailSettings

_log = logging.getLogger(__name__)

Audience = Literal["all", "active"]
"""The base recipient scope.

* ``all`` — every registered user.
* ``active`` — only ``user.is_active`` users.

An optional :data:`GroupKind` filter narrows this scope to the members
of specific teams / access-rights groups / curator categories.
"""

GroupKind = Literal["teams", "roles", "curators"]
"""Optional restriction layered on top of the base :data:`Audience`.

* ``teams`` — members of the selected teams (``selection`` = team ids).
* ``roles`` — holders of the selected roles, base **or** capability
  (``selection`` = role ids).
* ``curators`` — holders of the selected curate/approve permissions
  (``selection`` = category keys, see :data:`PERMISSION_GROUPS`).

The chosen entities arrive as a ``selection`` list. Recipients are
always resolved server-side (the browser never supplies an email list),
**intersected** with the base scope (so ``active`` + a team yields only
that team's currently-enabled members), and deduped.
"""

_GROUP_AUDIENCES: frozenset[str] = frozenset({"teams", "roles", "curators"})

PERMISSION_GROUPS: dict[str, str] = {
    "library": "library.curate",
    "materials": "materials.curate",
    "vv": "vv.curate",
    "models": "models.curate",
    "cohorts": "cohorts.curate",
    "optimisation_approvers": "optimisation.approve",
}
"""Curator/approver category key → the permission slug it stands for.

A "curator" or "approver" is not a table row but a permission holder
(ADR-0005: these permissions ride stackable *capability* roles, plus
the ``super_user`` sentinel). Selecting a category broadcasts to
everyone who effectively holds that permission. Backs the ``curators``
:data:`GroupKind`.
"""

PERMISSION_GROUP_LABELS: dict[str, str] = {
    "library": "Library curators",
    "materials": "Materials curators",
    "vv": "V&V curators",
    "models": "Models curators",
    "cohorts": "Cohort curators",
    "optimisation_approvers": "Optimisation approvers",
}
"""User-facing label for each curator/approver category (drives the dropdown)."""

_HEADER_CID = "broadcast-header"
_FOOTER_CID = "broadcast-footer"


@dataclass(frozen=True)
class BroadcastAttachmentInput:
    """One file the admin attached to the broadcast (raw upload bytes)."""

    filename: str
    data: bytes
    content_type: str


@dataclass(frozen=True)
class BroadcastResult:
    """Outcome summary the admin sees immediately after sending."""

    disabled: bool
    """True when the email subsystem is off (``email.enabled = false``);
    nothing was attempted."""

    attempted: int
    sent: int
    failed: list[dict[str, str]] = field(default_factory=list)
    """``[{"email": ..., "reason": ...}]`` for each recipient that errored."""


@dataclass(frozen=True)
class BroadcastService:
    """Resolve audience, render the templated HTML, fan out, summarise.

    Stateless beyond the injected :class:`AuditWriter`, the process
    :class:`EmailSettings` baseline, and the directory broadcast
    template images are stored in (the same ``branding.icons_dir``
    :class:`~matofx.config.GeneralSettingsService.set_image` writes to).
    """

    audit: AuditWriter
    settings: EmailSettings
    branding_dir: Path

    async def send_broadcast(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        audience: Audience,
        subject: str,
        body: str,
        attachments: list[BroadcastAttachmentInput],
        group_kind: str = "",
        selection: list[str] | None = None,
    ) -> BroadcastResult:
        """Send ``subject``/``body`` (wrapped in the saved template) to the audience.

        ``audience`` is the base scope (``all`` / ``active``).
        ``group_kind`` optionally narrows it to a team / role / curator
        selection; ``selection`` then carries the chosen team ids, role
        ids, or curator category keys. An empty ``group_kind`` sends to
        the whole base scope.
        """
        from matofx.config.general_service import (  # noqa: PLC0415
            load_general_setting_overrides,
            load_general_settings,
        )

        if not self.settings.enabled:
            await self._audit(
                session,
                actor_user_id=actor_user_id,
                action="notify.broadcast.skipped",
                outcome="success",
                payload={"reason": "email subsystem disabled", "audience": audience},
            )
            return BroadcastResult(disabled=True, attempted=0, sent=0)

        overrides = await load_general_setting_overrides(session)
        effective = resolve_effective_email_settings(self.settings, overrides)
        snapshot = await load_general_settings(session)

        recipients = await self._resolve_recipients(session, audience, group_kind, selection or [])
        text_body, html_body, inline_images = self._render(body=body, snapshot=snapshot)
        file_attachments = [_to_attachment(a) for a in attachments]

        failed: list[dict[str, str]] = []
        sent = 0
        if recipients:
            try:
                async with SmtpSession(effective) as smtp:
                    for address in recipients:
                        message = assemble_message(
                            from_address=effective["from_address"],
                            to_address=address,
                            subject=subject,
                            text_body=text_body,
                            html_body=html_body,
                            inline_images=inline_images,
                            attachments=file_attachments,
                        )
                        try:
                            await smtp.send(message)
                            sent += 1
                        except Exception as exc:  # per-recipient isolation
                            _log.warning("notify.broadcast.recipient_failed")
                            failed.append({"email": address, "reason": str(exc)})
            except Exception as exc:  # connection-level failure
                # SmtpSession.__aenter__ failed (connect / login):
                # nothing was sent. Per-recipient send errors are
                # caught inside the loop, so reaching here means the
                # whole audience is undeliverable with one shared
                # reason. Audited as a failure outcome below.
                _log.exception("notify.broadcast.connection_failed")
                failed = [{"email": address, "reason": str(exc)} for address in recipients]
                sent = 0

        await self._audit(
            session,
            actor_user_id=actor_user_id,
            action="notify.broadcast.sent",
            outcome="success" if not failed else "failure",
            payload={
                "audience": audience,
                "group_kind": group_kind,
                "selection": list(selection or []),
                "subject": subject,
                "attempted": len(recipients),
                "sent": sent,
                "failed": failed,
                "attachments": len(file_attachments),
            },
        )
        return BroadcastResult(disabled=False, attempted=len(recipients), sent=sent, failed=failed)

    # ----- helpers ------------------------------------------------------

    async def _resolve_recipients(
        self,
        session: AsyncSession,
        audience: Audience,
        group_kind: str,
        selection: list[str],
    ) -> list[str]:
        """Distinct, non-empty email addresses for the chosen audience.

        Resolves the base scope (``all`` / ``active``), then — when a
        ``group_kind`` is given — intersects it with the selected group's
        members so the scope still applies (e.g. ``active`` + a team
        yields that team's currently-enabled members only). An empty
        ``group_kind`` returns the whole base scope.
        """
        from matofx.users.orm import User  # noqa: PLC0415

        scope_stmt = select(User.email)
        if audience == "active":
            scope_stmt = scope_stmt.where(User.is_active.is_(True))
        base = _dedupe_emails(list((await session.execute(scope_stmt)).scalars().all()))

        if group_kind not in _GROUP_AUDIENCES:
            return base

        group = _dedupe_emails(await self._resolve_group_emails(session, group_kind, selection))
        in_scope = {e.lower() for e in base}
        return [e for e in group if e.lower() in in_scope]

    async def _resolve_group_emails(
        self, session: AsyncSession, audience: str, selection: list[str]
    ) -> list[str]:
        """Emails for a team / role / curator selection (membership join)."""
        from matofx.users.orm import User  # noqa: PLC0415
        from matofx.users.permissions import Permission, user_has_permission  # noqa: PLC0415
        from matofx.users.role_orm import UserCapabilityRole  # noqa: PLC0415
        from matofx.users.team_orm import UserTeam  # noqa: PLC0415

        if audience == "teams":
            ids = _as_int_ids(selection)
            if not ids:
                return []
            stmt = (
                select(User.email)
                .join(UserTeam, UserTeam.user_id == User.id)
                .where(UserTeam.team_id.in_(ids))
            )
            return list((await session.execute(stmt)).scalars().all())

        if audience == "roles":
            ids = _as_int_ids(selection)
            if not ids:
                return []
            # A role can be a user's base role (User.role_id) or a
            # stacked capability role (user_role join) — union both.
            base_stmt = select(User.email).where(User.role_id.in_(ids))
            cap_stmt = (
                select(User.email)
                .join(UserCapabilityRole, UserCapabilityRole.user_id == User.id)
                .where(UserCapabilityRole.role_id.in_(ids))
            )
            base = list((await session.execute(base_stmt)).scalars().all())
            cap = list((await session.execute(cap_stmt)).scalars().all())
            return base + cap

        # audience == "curators": map each category key to its curate/
        # approve permission and collect everyone who effectively holds it
        # (covers capability-role grants and the super_user sentinel).
        perms = [
            Permission(slug)
            for key in selection
            if (slug := PERMISSION_GROUPS.get(key)) is not None
        ]
        if not perms:
            return []
        rows = list((await session.execute(select(User.id, User.email))).all())
        out: list[str] = []
        for uid, email in rows:
            for perm in perms:
                if await user_has_permission(session, uid, perm):
                    out.append(email)
                    break
        return out

    def _render(self, *, body: str, snapshot: dict[str, Any]) -> tuple[str, str, list[InlineImage]]:
        """Build (plain-text, HTML, inline-images) from body + saved template."""
        header_text = str(snapshot.get("broadcast_header_text") or "")
        footer_text = str(snapshot.get("broadcast_footer_text") or "")
        header_img = self._load_image(snapshot.get("broadcast_header_image"), _HEADER_CID)
        footer_img = self._load_image(snapshot.get("broadcast_footer_image"), _FOOTER_CID)

        inline_images = [img for img in (header_img, footer_img) if img is not None]

        # Plain-text fallback: template text + body, no markup.
        text_parts = [p for p in (header_text, body, footer_text) if p.strip()]
        text_body = "\n\n".join(text_parts) + "\n"

        # HTML: escape every admin-supplied string, newlines → <br>.
        # The header (image + text) and footer (text + image) are each a
        # single unit with no divider *inside* them; the <hr/> rules only
        # separate header | body | footer.
        header_parts: list[str] = []
        if header_img is not None:
            header_parts.append(
                f'<img src="cid:{header_img.cid}" '
                f'alt="" style="max-width:100%;height:auto;display:block;" />'
            )
        if header_text.strip():
            header_parts.append(f"<div>{_html_block(header_text)}</div>")

        footer_parts: list[str] = []
        if footer_text.strip():
            footer_parts.append(
                f'<div style="color:#666;font-size:0.9em;">{_html_block(footer_text)}</div>'
            )
        if footer_img is not None:
            footer_parts.append(
                f'<img src="cid:{footer_img.cid}" '
                f'alt="" style="max-width:100%;height:auto;display:block;" />'
            )

        blocks: list[str] = []
        if header_parts:
            blocks.append("".join(header_parts))
        blocks.append(f"<div>{_html_block(body)}</div>")
        if footer_parts:
            blocks.append("".join(footer_parts))
        html_body = (
            "<html><body "
            'style="font-family:Arial,Helvetica,sans-serif;'
            'color:#222;line-height:1.5;">' + "<hr/>".join(blocks) + "</body></html>"
        )
        return text_body, html_body, inline_images

    def _load_image(self, basename: object, cid: str) -> InlineImage | None:
        """Read a saved template image off disk into an :class:`InlineImage`.

        Missing file / no override → ``None`` (the block is simply
        omitted; a deleted image must never break a broadcast).
        """
        if not isinstance(basename, str) or not basename:
            return None
        path = self.branding_dir / basename
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        guessed, _ = mimetypes.guess_type(str(path))
        maintype, _, subtype = (guessed or "image/png").partition("/")
        if maintype != "image" or not subtype:
            maintype, subtype = "image", "png"
        return InlineImage(cid=cid, data=data, maintype=maintype, subtype=subtype)

    async def _audit(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        action: str,
        outcome: Literal["success", "failure"],
        payload: dict[str, Any],
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=action,
                resource_type="broadcast",
                resource_id="-",
                outcome=outcome,
                payload=payload,
            ),
        )


def _to_attachment(a: BroadcastAttachmentInput) -> Attachment:
    maintype, _, subtype = (a.content_type or "application/octet-stream").partition("/")
    if not maintype or not subtype:
        maintype, subtype = "application", "octet-stream"
    return Attachment(
        filename=a.filename or "attachment",
        data=a.data,
        maintype=maintype,
        subtype=subtype,
    )


def _dedupe_emails(emails: Sequence[str | None]) -> list[str]:
    """Distinct, non-empty, order-preserving addresses (case-insensitive key)."""
    seen: set[str] = set()
    out: list[str] = []
    for email in emails:
        address = (email or "").strip()
        if address and address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
    return out


def _as_int_ids(selection: list[str]) -> list[int]:
    """Parse a selection of id strings to ints, dropping anything non-numeric."""
    out: list[int] = []
    for raw in selection:
        try:
            out.append(int(str(raw).strip()))
        except (TypeError, ValueError):
            continue
    return out


def _html_block(text: str) -> str:
    """HTML-escape admin text and turn newlines into ``<br>``."""
    return _html.escape(text).replace("\n", "<br/>")


def make_broadcast_service(
    audit: AuditWriter, settings: EmailSettings, branding_dir: Path
) -> BroadcastService:
    """Factory: build a :class:`BroadcastService`."""
    return BroadcastService(audit=audit, settings=settings, branding_dir=branding_dir)

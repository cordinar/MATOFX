"""Email notifier — async SMTP via :mod:`aiosmtplib`.

Phase 1: a single channel (email) and a single recipient model
(notify the user who created the job). Per-user / per-tool / per-customer
preferences land in Phase 2 alongside the in-app + chat channels.

The notifier is fire-and-forget from the runner's point of view: every
``notify_job_state_changed`` call is wrapped in a try-except so a relay
hiccup never breaks the canonical state transition. Failures are
audited via ``notify.email.failed`` so operators see them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import sqlalchemy as sa

from matofx.audit.schema import RecordInput
from matofx.notify.smtp import (
    assemble_message,
    resolve_effective_email_settings,
    send_one,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.config import EmailSettings
    from matofx.hierarchy.orm import Job

_log = logging.getLogger(__name__)


JobEvent = Literal["running", "complete", "failed"]
"""Closed vocabulary for the runner state transitions that fire notifications."""


@dataclass(frozen=True)
class ApprovalDecisionNotice:
    """One decided request, for the consolidated requestor email.

    ``kind`` ∈ ``pre`` / ``post``; ``decision`` ∈ ``approve`` / ``deny``;
    ``reason`` is the approver's verbatim text (always present on a
    deny — the service enforces it; ``None`` on an approve).
    """

    job_id: int
    job_name: str
    tool_name: str
    requestor_user_id: int
    kind: str
    decision: str
    reason: str | None = None


class Notifier(Protocol):
    """Minimal interface the runner + API call for notifications."""

    async def notify_job_state_changed(
        self,
        session: AsyncSession,
        *,
        job: Job,
        event: JobEvent,
    ) -> None:
        """Send a notification for ``job`` reaching ``event``.

        Implementations swallow their own errors; the runner never
        sees an exception from this call. Failures are audited so
        operators can find them.
        """

    async def notify_approval_request_opened(
        self,
        session: AsyncSession,
        *,
        request: Any,
    ) -> None:
        """Email every eligible approver that ``request`` is waiting (§16.15).

        ``request`` is an :class:`~matofx.approvals.orm.ApprovalRequest`
        (typed ``Any`` to keep this module importable before
        ``matofx.approvals``). Self-guarded + audited like the
        job-state path.
        """

    async def notify_approval_decisions(
        self,
        session: AsyncSession,
        *,
        notices: list[ApprovalDecisionNotice],
    ) -> None:
        """Email each requestor a consolidated summary of a decision batch.

        One email per distinct ``requestor_user_id`` — approved items
        ("now queued" / "results released") and denied items (with the
        reason verbatim) in a single message.
        """

    async def notify_licence_alert(
        self,
        session: AsyncSession,
        *,
        kind: str,
        summary: str,
        detail: str,
    ) -> None:
        """Tell administrators about a licence event as it happens.

        The only thing that makes a licence problem visible before the
        next compliance report, which at one or two reports a year could
        be months away.
        """

    async def notify_inactivity_grace_warning(
        self,
        session: AsyncSession,
        *,
        user: Any,
        days_remaining: int,
        threshold_days: int,
    ) -> None:
        """Warn ``user`` that their account is about to be auto-disabled.

        Sent from the inactivity sweeper (see
        :mod:`matofx.users.inactive_sweeper`) for users whose
        idle time has crossed the warning band but not yet the
        disable threshold. Failures are swallowed by the sweeper
        so the loop survives a flaky relay.
        """


class NoopNotifier:
    """Drops every send. Used when ``EmailSettings.enabled`` is ``False``.

    Honours the :class:`Notifier` Protocol so callers don't need a
    None-check at every site.
    """

    async def notify_job_state_changed(
        self,
        session: AsyncSession,
        *,
        job: Job,
        event: JobEvent,
    ) -> None:
        """No-op."""
        del session, job, event

    async def notify_approval_request_opened(
        self,
        session: AsyncSession,
        *,
        request: Any,
    ) -> None:
        """No-op."""
        del session, request

    async def notify_approval_decisions(
        self,
        session: AsyncSession,
        *,
        notices: list[ApprovalDecisionNotice],
    ) -> None:
        """No-op."""
        del session, notices

    async def notify_licence_alert(
        self,
        session: AsyncSession,
        *,
        kind: str,
        summary: str,
        detail: str,
    ) -> None:
        """No-op."""
        del session, kind, summary, detail

    async def notify_inactivity_grace_warning(
        self,
        session: AsyncSession,
        *,
        user: Any,
        days_remaining: int,
        threshold_days: int,
    ) -> None:
        """No-op."""
        del session, user, days_remaining, threshold_days


@dataclass(frozen=True)
class _RenderedMessage:
    """Subject + body the notifier built before handing to SMTP."""

    subject: str
    body: str


class EmailNotifier:
    """Async SMTP notifier backed by :mod:`aiosmtplib`.

    Constructed once per process by :func:`make_notifier`. Holds the
    :class:`EmailSettings` and the :class:`AuditWriter`; resolves the
    recipient address per-call by looking up the job's
    ``created_by_user_id``.
    """

    def __init__(self, settings: EmailSettings, audit: AuditWriter) -> None:
        """Construct from validated settings + the audit writer the process shares."""
        self._settings = settings
        self._audit = audit

    async def notify_job_state_changed(
        self,
        session: AsyncSession,
        *,
        job: Job,
        event: JobEvent,
    ) -> None:
        """Resolve the recipient + tool flag, render, send. Audits success / failure."""
        from matofx.config.general_service import (  # noqa: PLC0415
            load_general_setting_overrides,
            load_general_settings,
        )
        from matofx.users.orm import User  # noqa: PLC0415

        # Resolve the effective SMTP + branding settings ONCE per call.
        # Every admin-tunable knob (host/port/credentials/TLS/from/public_url)
        # comes from the general-settings catalog where the admin saved
        # something, otherwise falls back to the TOML baseline on
        # ``self._settings``. The master kill-switch is the one knob that
        # uses the back-filled snapshot (its catalog default is True, and
        # tests + ops both rely on that default).
        snapshot = await load_general_settings(session)
        if not bool(snapshot.get("job_email_notifications_enabled", False)):
            await self._audit_skip(
                session, job=job, event=event, reason="job notifications disabled"
            )
            return
        # Overrides-only for SMTP — unset keys fall through to the TOML
        # baseline, not the catalog default (e.g. "localhost").
        overrides = await load_general_setting_overrides(session)
        effective = resolve_effective_email_settings(self._settings, overrides)

        tool = (await session.execute(_select_tool_by_alias(job.tool_alias))).scalar_one_or_none()
        if tool is not None and not _tool_email_enabled(tool):
            # Per-tool opt-out (descriptor.email_notifications=False).
            return

        recipient = await session.get(User, job.created_by_user_id)
        if recipient is None or not recipient.email:
            await self._audit_skip(session, job=job, event=event, reason="no recipient")
            return

        if not recipient.email_notifications_enabled:
            # Per-user opt-out — the recipient has switched job-state
            # emails off on the Account page. Audit so operators can
            # see why no email arrived without scraping the user table.
            await self._audit_skip(session, job=job, event=event, reason="recipient opted out")
            return

        message = _render(job=job, event=event, public_url=effective["public_url"])
        # Never propagate SMTP failures into the runner; audit + log instead.
        try:
            await send_one(
                effective,
                assemble_message(
                    from_address=effective["from_address"],
                    to_address=recipient.email,
                    subject=message.subject,
                    text_body=message.body,
                ),
            )
        except Exception as exc:
            _log.exception("notify.email.failed")
            await self._audit_failure(
                session, job=job, event=event, recipient=recipient.email, reason=str(exc)
            )
            return

        await self._audit_success(session, job=job, event=event, recipient=recipient.email)

    async def _gated_effective(
        self,
        session: AsyncSession,
        *,
        gate_key: str,
        per_type_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Shared preamble: switch gate(s) + resolved SMTP settings.

        Returns ``None`` (caller does nothing) when the master
        ``gate_key`` is off, or — for the per-type approval emails —
        when the ``per_type_key`` switch for that kind of approval is
        off. Each switch's catalog default is ``True``, so a fresh
        install with no admin edits sends.
        """
        from matofx.config.general_service import (  # noqa: PLC0415
            load_general_setting_overrides,
            load_general_settings,
        )

        snapshot = await load_general_settings(session)
        if not bool(snapshot.get(gate_key, False)):
            return None
        if per_type_key is not None and not bool(snapshot.get(per_type_key, False)):
            return None
        overrides = await load_general_setting_overrides(session)
        return resolve_effective_email_settings(self._settings, overrides)

    async def notify_approval_request_opened(
        self,
        session: AsyncSession,
        *,
        request: Any,
    ) -> None:
        """Email every eligible approver/curator that a request is waiting.

        Works for any approval target — job, parametric study,
        optimisation study, library file, material, surrogate model, or
        V&V record. The per-target wording + the per-type switch that
        gates it come from :func:`_describe_approval_target`.
        """
        from sqlalchemy import select  # noqa: PLC0415

        from matofx.users.orm import User  # noqa: PLC0415

        target = await _describe_approval_target(session, request)
        # Master approvals switch + the per-type switch for this target.
        effective = await self._gated_effective(
            session,
            gate_key="approval_email_notifications_enabled",
            per_type_key=target.per_type_key,
        )
        if effective is None:
            await self._audit_approval(
                session,
                action="notify.approval.skipped",
                request_id=request.id,
                outcome="success",
                payload={"kind": request.kind, "reason": "approval notifications disabled"},
            )
            return

        eligible = [str(u).strip().lower() for u in (request.eligible_approvers or [])]
        if not eligible:
            return
        approvers = list(
            (await session.execute(select(User).where(User.username.in_(eligible)))).scalars()
        )

        base = effective["public_url"].rstrip("/")
        link = f"{base}/ui/approvals"
        subject = target.subject
        body = f"{target.body}\n\nReview and decide here: {link}\n"

        for approver in approvers:
            if not approver.email or not approver.email_notifications_enabled:
                continue
            try:
                await send_one(
                    effective,
                    assemble_message(
                        from_address=effective["from_address"],
                        to_address=approver.email,
                        subject=subject,
                        text_body=body,
                    ),
                )
            except Exception as exc:  # never break the caller
                _log.exception("notify.approval.failed")
                await self._audit_approval(
                    session,
                    action="notify.approval.failed",
                    request_id=request.id,
                    outcome="failure",
                    payload={
                        "kind": request.kind,
                        "recipient": approver.email,
                        "reason": str(exc),
                    },
                )
                continue
            await self._audit_approval(
                session,
                action="notify.approval.sent",
                request_id=request.id,
                outcome="success",
                payload={"kind": request.kind, "recipient": approver.email},
            )

    async def notify_approval_decisions(
        self,
        session: AsyncSession,
        *,
        notices: list[ApprovalDecisionNotice],
    ) -> None:
        """One consolidated email per requestor for a decision batch."""
        from matofx.users.orm import User  # noqa: PLC0415

        if not notices:
            return
        effective = await self._gated_effective(
            session, gate_key="approval_email_notifications_enabled"
        )
        if effective is None:
            return

        by_requestor: dict[int, list[ApprovalDecisionNotice]] = {}
        for n in notices:
            by_requestor.setdefault(n.requestor_user_id, []).append(n)

        base = effective["public_url"].rstrip("/")
        link = f"{base}/ui/my-jobs"
        for user_id, items in by_requestor.items():
            recipient = await session.get(User, user_id)
            if (
                recipient is None
                or not recipient.email
                or not recipient.email_notifications_enabled
            ):
                continue
            lines: list[str] = []
            for it in items:
                tag = f"Job {it.job_id:07d} — {it.tool_name}"
                if it.decision == "approve":
                    outcome = (
                        "results released" if it.kind == "post" else "approved — now queued to run"
                    )
                    lines.append(f"  {tag}: {outcome}")
                else:
                    what = "results withheld" if it.kind == "post" else "denied"
                    lines.append(f"  {tag}: {what}. Reason: {it.reason or '(none given)'}")
            body = (
                "An approver has reviewed your simulation request(s):\n\n"
                + "\n".join(lines)
                + f"\n\nView your jobs here: {link}\n"
            )
            try:
                await send_one(
                    effective,
                    assemble_message(
                        from_address=effective["from_address"],
                        to_address=recipient.email,
                        subject="Your simulation approval results",
                        text_body=body,
                    ),
                )
            except Exception as exc:  # never break the caller
                _log.exception("notify.approval.failed")
                await self._audit_approval(
                    session,
                    action="notify.approval.failed",
                    request_id=items[0].job_id,
                    outcome="failure",
                    payload={"recipient": recipient.email, "reason": str(exc)},
                )
                continue
            await self._audit_approval(
                session,
                action="notify.approval.sent",
                request_id=items[0].job_id,
                outcome="success",
                payload={
                    "recipient": recipient.email,
                    "decisions": len(items),
                },
            )

    async def notify_licence_alert(
        self,
        session: AsyncSession,
        *,
        kind: str,
        summary: str,
        detail: str,
    ) -> None:
        """Email administrators about a licence event as it happens.

        Recipients are everyone holding ``audit.read`` — the right that
        already means "trusted with this install's compliance evidence",
        and which administrators hold by default. Deliberately not the
        whole user base: an ordinary user can do nothing about a full
        licence except be alarmed by it.

        Never raises. An alert that broke the login it was reporting on
        would be worse than no alert, which is why the caller that
        matters most — break-glass admission — treats this as a
        best-effort side effect of admitting the Super User.
        """
        from matofx.users.orm import User  # noqa: PLC0415
        from matofx.users.permissions import Permission, user_has_permission  # noqa: PLC0415

        effective = await self._gated_effective(
            session, gate_key="licence_alert_emails_enabled"
        )
        if effective is None:
            return

        rows = list(
            (
                await session.execute(
                    sa.select(User.id, User.email).where(
                        User.email.is_not(None), User.is_active.is_(True)
                    )
                )
            ).all()
        )
        recipients: list[str] = []
        for user_id, email in rows:
            if email and await user_has_permission(session, int(user_id), Permission.AUDIT_READ):
                recipients.append(str(email))
        if not recipients:
            return

        base = str(effective["public_url"]).rstrip("/")
        subject = f"MATOFX licence alert: {summary}"
        body = (
            f"{summary}\n\n"
            f"{detail}\n\n"
            "This is recorded in the audit log and will appear in your next "
            "compliance report whether or not you act on it.\n\n"
            f"Licence and compliance: {base}/ui/admin/compliance\n"
            f"Active sessions: {base}/ui/admin/sessions\n\n"
            "To stop these emails, turn off 'Send licence alerts to "
            "administrators' on Admin -> General settings. The events are "
            "still recorded either way."
        )
        for address in sorted(set(recipients)):
            try:
                await send_one(
                    effective,
                    assemble_message(
                        from_address=effective["from_address"],
                        to_address=address,
                        subject=subject,
                        text_body=body,
                    ),
                )
            except Exception:  # never break the caller
                _log.exception("notify.licence_alert.failed")
        del kind

    async def notify_inactivity_grace_warning(
        self,
        session: AsyncSession,
        *,
        user: Any,
        days_remaining: int,
        threshold_days: int,
    ) -> None:
        """Tell ``user`` their account is about to be auto-disabled.

        Riding the same kill-switch as job/approval emails — when
        site-wide job notifications are off the warning is skipped
        + audited so ops see why a user got no notice. The
        recipient's per-user opt-out is **not** honoured here:
        these warnings tell the user "your account is about to
        stop working" — security-relevant, not promotional.
        """
        from matofx.users.orm import User as _User  # noqa: PLC0415

        del _User  # type annotation hint only — keeps mypy aware of ``user``

        # Account-lifecycle warning: rides the job-state switch (not the
        # approvals switch) — it's the same "operational email" class.
        effective = await self._gated_effective(
            session, gate_key="job_email_notifications_enabled"
        )
        if effective is None:
            await self._audit_grace(
                session,
                user_id=int(user.id),
                outcome="skipped",
                payload={"reason": "job notifications disabled"},
            )
            return
        if not user.email:
            await self._audit_grace(
                session,
                user_id=int(user.id),
                outcome="skipped",
                payload={"reason": "no recipient email"},
            )
            return

        plural = "day" if days_remaining == 1 else "days"
        base = effective["public_url"].rstrip("/")
        link = f"{base}/ui/login"
        subject = (
            f"Your MATOFX account is inactive — sign in within {days_remaining} {plural} to keep it"
        )
        body = (
            f"Hello {user.full_name or user.username},\n\n"
            f"Your MATOFX account has not been used in a while. This install "
            f"automatically disables accounts that have been inactive for "
            f"{threshold_days} days. Sign in within the next {days_remaining} "
            f"{plural} to keep your account active.\n\n"
            f"Sign in here: {link}\n\n"
            "If you no longer need access, no action is required — your "
            "account will be deactivated automatically and an administrator "
            "can re-enable it later if you change your mind."
        )
        try:
            await send_one(
                effective,
                assemble_message(
                    from_address=effective["from_address"],
                    to_address=user.email,
                    subject=subject,
                    text_body=body,
                ),
            )
        except Exception as exc:  # never break the caller
            _log.exception("notify.inactivity.failed")
            await self._audit_grace(
                session,
                user_id=int(user.id),
                outcome="failure",
                payload={"recipient": user.email, "reason": str(exc)},
            )
            # Re-raise so the caller knows not to set
            # last_grace_warning_at — next sweep will retry.
            raise
        await self._audit_grace(
            session,
            user_id=int(user.id),
            outcome="success",
            payload={
                "recipient": user.email,
                "days_remaining": days_remaining,
                "threshold_days": threshold_days,
            },
        )

    async def _audit_grace(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        outcome: str,
        payload: dict[str, Any],
    ) -> None:
        await self._audit.record(
            session,
            RecordInput(
                actor="system:inactive_sweeper",
                actor_type="system",
                action="notify.inactivity_grace_warning",
                resource_type="user",
                resource_id=str(user_id),
                outcome=outcome,  # type: ignore[arg-type]
                payload=payload,
            ),
        )

    async def _audit_approval(
        self,
        session: AsyncSession,
        *,
        action: str,
        request_id: int,
        outcome: str,
        payload: dict[str, Any],
    ) -> None:
        await self._audit.record(
            session,
            RecordInput(
                actor="system:approvals",
                actor_type="system",
                action=action,
                resource_type="approval_request",
                resource_id=str(request_id),
                outcome=outcome,  # type: ignore[arg-type]
                payload=payload,
            ),
        )

    async def _audit_success(
        self,
        session: AsyncSession,
        *,
        job: Job,
        event: JobEvent,
        recipient: str,
    ) -> None:
        await self._audit.record(
            session,
            RecordInput(
                actor="system:runner",
                actor_type="system",
                action="notify.email.sent",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={"event": event, "recipient": recipient},
            ),
        )

    async def _audit_failure(
        self,
        session: AsyncSession,
        *,
        job: Job,
        event: JobEvent,
        recipient: str,
        reason: str,
    ) -> None:
        await self._audit.record(
            session,
            RecordInput(
                actor="system:runner",
                actor_type="system",
                action="notify.email.failed",
                resource_type="job",
                resource_id=str(job.id),
                outcome="failure",
                payload={"event": event, "recipient": recipient, "reason": reason},
            ),
        )

    async def _audit_skip(
        self,
        session: AsyncSession,
        *,
        job: Job,
        event: JobEvent,
        reason: str,
    ) -> None:
        await self._audit.record(
            session,
            RecordInput(
                actor="system:runner",
                actor_type="system",
                action="notify.email.skipped",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={"event": event, "reason": reason},
            ),
        )


def make_notifier(settings: EmailSettings, audit: AuditWriter) -> Notifier:
    """Pick :class:`EmailNotifier` or :class:`NoopNotifier` from settings.

    ``EmailSettings.enabled=False`` → :class:`NoopNotifier`. Production
    installs flip the flag and supply real SMTP details; dev / test
    installs leave it off.
    """
    if not settings.enabled:
        return NoopNotifier()
    return EmailNotifier(settings=settings, audit=audit)


# ---------- helpers ------------------------------------------------------


def _select_tool_by_alias(alias: str) -> Any:
    """Lazy SELECT helper — keeps this module importable before matofx.tools."""
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.tools.orm import Tool  # noqa: PLC0415

    return select(Tool).where(Tool.alias == alias)


def _person_name(user: Any) -> str:
    """Friendly requestor label for the approver email."""
    if user is None:
        return "(unknown)"
    full = (getattr(user, "full_name", "") or "").strip()
    return full or getattr(user, "username", "") or "(unknown)"


@dataclass(frozen=True)
class _ApprovalTargetInfo:
    """Per-target wording + the General Settings switch that gates it.

    ``per_type_key`` names the ``approval_email_*`` boolean the
    notifier checks (subordinate to the master approvals switch).
    ``body`` is the lead + one-line detail; the caller appends the
    "Review and decide here" link.
    """

    per_type_key: str
    subject: str
    body: str


async def _name_for(
    session: AsyncSession, model: Any, ident: int | None, attr: str = "name"
) -> str:
    """Best-effort display name for a target row; never raises.

    Returns the row's ``attr`` (usually ``name``) when found, else a
    ``#<id>`` placeholder. A lookup failure degrades to the placeholder
    so an email still goes out with a usable label.
    """
    if ident is None:
        return "(unknown)"
    try:
        row = await session.get(model, ident)
    except Exception:
        return f"#{ident}"
    if row is None:
        return f"#{ident}"
    value = getattr(row, attr, None)
    return str(value) if value else f"#{ident}"


async def _describe_approval_target(  # noqa: PLR0911 — one return per target kind
    session: AsyncSession, request: Any
) -> _ApprovalTargetInfo:
    """Resolve per-target email wording + the per-type gate for ``request``.

    Inspects whichever target FK the request carries and returns a
    target-appropriate subject + body plus the ``approval_email_*``
    switch that governs it. Falls back to a generic description for an
    unrecognised target so the notifier always has something to send.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.users.orm import User  # noqa: PLC0415

    requestor = await session.get(User, request.requested_by_user_id)
    by = f"requested by {_person_name(requestor)}"

    if request.job_id is not None:
        from matofx.tools.orm import Tool  # noqa: PLC0415

        tool = (
            await session.execute(select(Tool).where(Tool.alias == request.tool_alias))
        ).scalar_one_or_none()
        tool_name = tool.name if tool is not None else (request.tool_alias or "a tool")
        if request.kind == "post":
            subject = f"Simulation results awaiting your release: {tool_name}"
            lead = (
                "A simulation has finished and its results are held for "
                "your review before they're released to the requestor."
            )
        else:
            subject = f"Simulation awaiting your approval: {tool_name}"
            lead = "A simulation needs your approval before it can run."
        detail = f"Job {request.job_id:07d} — {tool_name} — {by}"
        return _ApprovalTargetInfo("approval_email_jobs", subject, f"{lead}\n\n{detail}")

    if request.parametric_study_id is not None:
        from matofx.studies.orm import ParametricStudy  # noqa: PLC0415

        name = await _name_for(session, ParametricStudy, request.parametric_study_id)
        return _ApprovalTargetInfo(
            "approval_email_studies",
            f"Parametric study awaiting your approval: {name}",
            "A parametric study needs your approval before it can run."
            f"\n\nStudy — {name} — {by}",
        )

    if request.optimisation_study_id is not None:
        from matofx.studies.optimisation.orm import OptimisationStudy  # noqa: PLC0415

        name = await _name_for(session, OptimisationStudy, request.optimisation_study_id)
        return _ApprovalTargetInfo(
            "approval_email_optimisation",
            f"Optimisation study awaiting your approval: {name}",
            "An optimisation study is held at its start gate and needs "
            f"your approval before it can run.\n\nStudy — {name} — {by}",
        )

    if request.library_file_version_id is not None:
        from matofx.library.orm import LibraryFile, LibraryFileVersion  # noqa: PLC0415

        version = await session.get(LibraryFileVersion, request.library_file_version_id)
        file_id = getattr(version, "library_file_id", None) if version is not None else None
        name = await _name_for(session, LibraryFile, file_id)
        return _ApprovalTargetInfo(
            "approval_email_library",
            f"Library file awaiting your approval: {name}",
            "A file has been submitted for publishing to the global file "
            f"library and needs a curator's approval.\n\nFile — {name} — {by}",
        )

    if request.material_version_id is not None:
        from matofx.materials.orm import Material, MaterialVersion  # noqa: PLC0415

        version = await session.get(MaterialVersion, request.material_version_id)
        mat_id = getattr(version, "material_id", None) if version is not None else None
        name = await _name_for(session, Material, mat_id)
        return _ApprovalTargetInfo(
            "approval_email_materials",
            f"Material change awaiting your approval: {name}",
            "A material change has been submitted to the materials "
            f"database and needs a curator's approval.\n\nMaterial — {name} — {by}",
        )

    if request.surrogate_model_version_id is not None:
        from matofx.models.orm import SurrogateModel, SurrogateModelVersion  # noqa: PLC0415

        version = await session.get(SurrogateModelVersion, request.surrogate_model_version_id)
        model_id = getattr(version, "surrogate_model_id", None) if version is not None else None
        name = await _name_for(session, SurrogateModel, model_id)
        return _ApprovalTargetInfo(
            "approval_email_models",
            f"Surrogate model awaiting your check: {name}",
            "A surrogate model has been submitted for checking before "
            f"global release.\n\nModel — {name} — {by}",
        )

    if request.tool_vv_record_id is not None or request.job_vv_record_id is not None:
        return _ApprovalTargetInfo(
            "approval_email_vv",
            "V&V record awaiting your approval",
            "A verification & validation record has been submitted for "
            f"promotion and needs a curator's approval.\n\nV&V record — {by}",
        )

    # Unrecognised target — gate on the master switch only (per-type
    # key falls back to the master) so it still notifies.
    return _ApprovalTargetInfo(
        "approval_email_notifications_enabled",
        "An item is awaiting your approval",
        f"An item needs your approval.\n\n{by}",
    )


def _tool_email_enabled(tool: Any) -> bool:
    """Return True only when the tool's descriptor opted *in* to email notifications.

    Opt-in, matching the descriptor schema default: a descriptor that
    doesn't mention ``email_notifications`` — a hand-written or imported
    bundle — sends nothing until an admin turns it on for that tool.
    Every descriptor authored through the app stores the flag
    explicitly, so this fallback only decides the silent-by-omission
    case, and silence is the safe answer there.
    """
    descriptor = tool.descriptor or {}
    if not isinstance(descriptor, dict):
        return False
    return descriptor.get("email_notifications") is True


_SUBJECT_TEMPLATES: dict[JobEvent, str] = {
    "running": "MATOFX job started: {name}",
    "complete": "MATOFX job complete: {name}",
    "failed": "MATOFX job failed: {name}",
}

_BODY_TEMPLATES: dict[JobEvent, str] = {
    "running": (
        "Your MATOFX job has started running.\n"
        "\n"
        "Job: {name}\n"
        "Tool: {tool_alias}\n"
        "\n"
        "Watch progress here: {url}\n"
    ),
    "complete": (
        "Your MATOFX job finished successfully.\n"
        "\n"
        "Job: {name}\n"
        "Tool: {tool_alias}\n"
        "Exit code: {exit_code}\n"
        "\n"
        "View the results here: {url}\n"
    ),
    "failed": (
        "Your MATOFX job failed.\n"
        "\n"
        "Job: {name}\n"
        "Tool: {tool_alias}\n"
        "Exit code: {exit_code}\n"
        "\n"
        "Open the logs to see what went wrong: {url}\n"
    ),
}


def _render(*, job: Job, event: JobEvent, public_url: str) -> _RenderedMessage:
    """Render the (subject, body) pair for one job event.

    Plain English, no jargon — the recipient may not know what an
    "exit code" is, but the surrounding sentence makes it obvious
    when it matters (failed run).
    """
    base = public_url.rstrip("/")
    # Point straight at the canonical unified workspace. /ui/jobs/{id}
    # is only a thin redirect alias (see ui.pages.job.job_page); linking
    # the alias adds a navigate hop that can drop the job seed when the
    # recipient isn't logged in yet. The workspace reads ?job=<id>.
    deep_link = f"{base}/ui/my-jobs?job={job.id}"
    fmt_args = {
        "name": job.name,
        "tool_alias": job.tool_alias,
        "exit_code": job.exit_code if job.exit_code is not None else "(unknown)",
        "url": deep_link,
    }
    return _RenderedMessage(
        subject=_SUBJECT_TEMPLATES[event].format(**fmt_args),
        body=_BODY_TEMPLATES[event].format(**fmt_args),
    )

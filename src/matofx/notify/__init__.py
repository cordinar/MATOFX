"""Notification surface — Phase 1 ships email only.

Public surface:

- :class:`Notifier` — minimal Protocol every notifier honours.
- :class:`EmailNotifier` — async SMTP-backed notifier built on
  :mod:`aiosmtplib`.
- :class:`NoopNotifier` — drops every send; used when ``EmailSettings.enabled``
  is False or when a caller needs an explicit no-op (tests, headless installs).
- :class:`JobEvent` — closed vocabulary for the runner state transitions
  that fire notifications.
- :func:`make_notifier` — pick :class:`EmailNotifier` or :class:`NoopNotifier`
  from :class:`EmailSettings`.

Phase 2 lands the three-layer notification preferences (per-customer
default + per-tool default + per-user override) and the in-app /
Slack / Teams channels.
"""

from __future__ import annotations

from matofx.notify.broadcast import (
    Audience,
    BroadcastAttachmentInput,
    BroadcastResult,
    BroadcastService,
    make_broadcast_service,
)
from matofx.notify.email import (
    ApprovalDecisionNotice,
    EmailNotifier,
    JobEvent,
    NoopNotifier,
    Notifier,
    make_notifier,
)
from matofx.notify.smtp import (
    Attachment,
    InlineImage,
    SmtpSession,
    assemble_message,
    close_smtp_pool,
    resolve_effective_email_settings,
    send_one,
)

__all__ = [
    "ApprovalDecisionNotice",
    "Attachment",
    "Audience",
    "BroadcastAttachmentInput",
    "BroadcastResult",
    "BroadcastService",
    "EmailNotifier",
    "InlineImage",
    "JobEvent",
    "NoopNotifier",
    "Notifier",
    "SmtpSession",
    "assemble_message",
    "close_smtp_pool",
    "make_broadcast_service",
    "make_notifier",
    "resolve_effective_email_settings",
    "send_one",
]

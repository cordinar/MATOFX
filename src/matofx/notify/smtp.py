"""Shared SMTP plumbing for the notify package.

Two consumers share this module so the SMTP mechanics live in exactly
one place:

* :class:`matofx.notify.email.EmailNotifier` — one email per job-state
  transition, sent over a shared pooled connection (:func:`send_one`).
* :class:`matofx.notify.broadcast.BroadcastService` — one admin
  broadcast fanned out to many recipients over a single connection
  (:class:`SmtpSession`). Gmail and most relays rate-limit **logins**, so
  re-connecting per message throttles either path — which is why the
  job-state notifier pools its connection too.

Both resolve effective settings identically (TOML baseline overlaid
with admin DB overrides — :func:`resolve_effective_email_settings`),
both honour the ``email.tls_verify`` escape hatch, and both assemble
MIME the same way (:func:`assemble_message`).
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

import aiosmtplib

if TYPE_CHECKING:
    from matofx.config import EmailSettings


@dataclass(frozen=True)
class InlineImage:
    """An image embedded in the HTML body via ``cid:`` reference.

    ``cid`` is the bare token (no angle brackets); the HTML references
    it as ``<img src="cid:{cid}">`` and the part's ``Content-ID``
    header is written as ``<{cid}>``.
    """

    cid: str
    data: bytes
    maintype: str
    subtype: str


@dataclass(frozen=True)
class Attachment:
    """A file attached to the email (broadcast attachments)."""

    filename: str
    data: bytes
    maintype: str
    subtype: str


def resolve_effective_email_settings(
    settings: EmailSettings, overrides: dict[str, Any]
) -> dict[str, Any]:
    """Merge admin-saved DB overrides on top of the TOML baseline.

    ``overrides`` is the dict returned by
    :func:`matofx.config.general_service.load_general_setting_overrides`
    — only the keys with an explicit DB row. Any key not in
    ``overrides`` falls through to the TOML
    :class:`~matofx.config.EmailSettings` baseline, NOT the catalog
    default: an operator who set ``smtp_host`` in ``matofx.toml``
    keeps that value until an admin explicitly saves something else.

    ``use_tls`` and ``tls_verify`` are TOML-only by design (no catalog
    row) so transport security cannot be downgraded from the web UI.
    """

    def _str(key: str, baseline: str) -> str:
        value = overrides.get(key)
        return value if isinstance(value, str) and value else baseline

    def _bool(key: str, baseline: bool) -> bool:
        value = overrides.get(key)
        return value if isinstance(value, bool) else baseline

    def _int(key: str, baseline: int) -> int:
        value = overrides.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else baseline

    baseline_pw = (
        settings.smtp_password.get_secret_value() if settings.smtp_password is not None else None
    )
    snapshot_pw = overrides.get("smtp_password")
    password = snapshot_pw if isinstance(snapshot_pw, str) and snapshot_pw else baseline_pw

    return {
        "smtp_host": _str("smtp_host", settings.smtp_host),
        "smtp_port": _int("smtp_port", settings.smtp_port),
        "smtp_username": _str("smtp_username", settings.smtp_username or ""),
        "smtp_password": password,
        "use_tls": settings.use_tls,  # TOML-only (no catalog row)
        "tls_verify": settings.tls_verify,  # TOML-only (no catalog row)
        "use_starttls": _bool("use_starttls", settings.use_starttls),
        "from_address": _str("smtp_from_address", settings.from_address),
        "public_url": _str("public_url", settings.public_url),
        "timeout_seconds": settings.timeout_seconds,
    }


def _connection_kwargs(effective: dict[str, Any]) -> dict[str, Any]:
    """Build the aiosmtplib connection kwargs shared by both send paths.

    When ``tls_verify`` is False we pass a permissive SSL context —
    the escape hatch for hosts whose outbound TLS is intercepted by an
    antivirus / proxy presenting a CA cert OpenSSL 3.x rejects. It
    disables peer-cert + hostname verification for the SMTP TLS layer
    ONLY and is never the default (gated on the TOML-only
    ``email.tls_verify = false`` — see EmailSettings).
    """
    kwargs: dict[str, Any] = {
        "hostname": effective["smtp_host"],
        "port": effective["smtp_port"],
        "use_tls": effective["use_tls"],
        "start_tls": effective["use_starttls"] and not effective["use_tls"],
        "timeout": effective["timeout_seconds"],
    }
    if not effective["tls_verify"]:
        insecure_ctx = ssl.create_default_context()
        insecure_ctx.check_hostname = False
        insecure_ctx.verify_mode = ssl.CERT_NONE
        kwargs["tls_context"] = insecure_ctx
    return kwargs


def assemble_message(
    *,
    from_address: str,
    to_address: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    inline_images: list[InlineImage] | None = None,
    attachments: list[Attachment] | None = None,
) -> EmailMessage:
    """Build an :class:`EmailMessage`.

    Plain-text only → ``text/plain``. With ``html_body`` →
    ``multipart/alternative`` (plain fallback + HTML). With inline
    images the HTML part becomes ``multipart/related``. With
    attachments the whole thing is wrapped in ``multipart/mixed``.
    :class:`EmailMessage` performs the restructuring; we only place
    the parts in the right order.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address
    msg.set_content(text_body)

    if html_body is not None:
        msg.add_alternative(html_body, subtype="html")
        # ``get_payload()`` is typed as str | Message | Any because a
        # non-multipart message returns its body; this one is multipart by
        # construction (``add_alternative`` above), so the payload is the
        # parts list and each part is an EmailMessage.
        parts = cast("list[EmailMessage]", msg.get_payload())
        html_part = parts[-1]
        for img in inline_images or []:
            html_part.add_related(img.data, maintype=img.maintype, subtype=img.subtype)
            related_part = cast("list[EmailMessage]", html_part.get_payload())[-1]
            related_part["Content-ID"] = f"<{img.cid}>"

    for att in attachments or []:
        msg.add_attachment(
            att.data,
            maintype=att.maintype,
            subtype=att.subtype,
            filename=att.filename,
        )
    return msg


_POOL_IDLE_SECONDS = 60.0
"""Reopen rather than reuse a connection that has been idle this long.

Relays close idle sessions without telling the client, so a long-idle socket
looks alive and fails on first use. Reconnecting is cheap next to an email;
holding a socket open for minutes to save one connect is not the trade.
"""


class _SmtpPool:
    """One authenticated connection, reused across single sends.

    ``send_one`` used to connect, authenticate, send and quit for **every**
    message — one login per email. Relays rate-limit logins, so a study
    emitting a job-state email per transition walks straight into that limit:
    on the demo box a 400-run optimisation had every send failing with
    ``454 4.7.0 Too many login attempts``, and once hard-throttled the server
    stopped answering ``AUTH`` at all (a bare ``SMTPServerDisconnected``,
    which reads like a network fault and is not one).

    This is the same reasoning :class:`SmtpSession` was built on, applied to
    a *burst* of unrelated sends rather than one fan-out. The difference is
    that a burst has no enclosing scope, so the connection is held here and
    revalidated per send.

    Sends are serialised by a lock: one SMTP connection cannot interleave two
    transactions. Job-state emails are far slower to *produce* than to send,
    so the lock is not a bottleneck.
    """

    def __init__(self) -> None:
        """Start empty; the connection opens on the first send."""
        self._lock = asyncio.Lock()
        self._client: aiosmtplib.SMTP | None = None
        self._effective: dict[str, Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_used = 0.0

    async def send(self, effective: dict[str, Any], message: EmailMessage) -> None:
        """Send one message, opening or reusing the pooled connection."""
        async with self._lock:
            reused = await self._ensure(effective)
            try:
                await self._require_client().send_message(message)
            except Exception:
                await self._discard()
                # A *reused* connection is the one that goes stale — the relay
                # closed it while we thought it was open. Retry once on a fresh
                # one. A connection we just opened failing is a real failure,
                # and retrying it would only risk delivering twice.
                if not reused:
                    raise
                await self._ensure(effective)
                await self._require_client().send_message(message)
            self._last_used = time.monotonic()

    async def aclose(self) -> None:
        """Close the pooled connection, if any. Safe to call repeatedly."""
        async with self._lock:
            await self._discard()

    def _require_client(self) -> aiosmtplib.SMTP:
        """The open client — :meth:`_ensure` guarantees one, this narrows it."""
        if self._client is None:  # pragma: no cover - _ensure just set it
            raise RuntimeError("SMTP pool used before a connection was opened.")
        return self._client

    async def _ensure(self, effective: dict[str, Any]) -> bool:
        """Open a connection if needed. ``True`` if an existing one was reused."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A client belongs to the loop that created it; a different loop
            # (a test, a re-created runner loop) must not touch or close it.
            self._client, self._effective, self._loop = None, None, None
        elif (
            self._client is not None
            and self._effective == effective
            and self._client.is_connected
            and time.monotonic() - self._last_used <= _POOL_IDLE_SECONDS
        ):
            return True
        await self._discard()

        client = aiosmtplib.SMTP(**_connection_kwargs(effective))
        await client.connect()
        if effective["smtp_username"]:
            await client.login(effective["smtp_username"], effective["smtp_password"])
        # Copy the settings: an admin changing the host or credentials must
        # reopen rather than keep sending over the connection they replaced.
        self._client, self._effective, self._loop = client, dict(effective), loop
        self._last_used = time.monotonic()
        return False

    async def _discard(self) -> None:
        """Drop the current connection; teardown errors are non-fatal."""
        client, self._client, self._effective = self._client, None, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.quit()


_pool = _SmtpPool()


async def send_one(effective: dict[str, Any], message: EmailMessage) -> None:
    """Send a single email over the shared, reused SMTP connection.

    Used by the job-state notifier, where each call is one email but the calls
    arrive in bursts. Connecting and authenticating per message trips relay
    login rate limits (see :class:`_SmtpPool`), so the connection is pooled.

    Exceptions propagate; the caller audits + swallows them.
    """
    await _pool.send(effective, message)


async def close_smtp_pool() -> None:
    """Close the pooled connection — call on process shutdown."""
    await _pool.aclose()


class SmtpSession:
    """One connection, many messages — the broadcast fan-out path.

    Opens a single authenticated SMTP connection on ``__aenter__`` and
    reuses it for every :meth:`send`. A per-recipient send failure
    raises out of :meth:`send` (the caller records it and continues);
    the connection itself stays open for the remaining recipients.
    """

    def __init__(self, effective: dict[str, Any]) -> None:
        """Capture the resolved settings; the connection opens on enter."""
        self._effective = effective
        self._client: aiosmtplib.SMTP | None = None

    async def __aenter__(self) -> SmtpSession:
        """Open + authenticate one SMTP connection for the fan-out."""
        client = aiosmtplib.SMTP(**_connection_kwargs(self._effective))
        await client.connect()
        username = self._effective["smtp_username"] or None
        password = self._effective["smtp_password"]
        if username:
            await client.login(username, password)
        self._client = client
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection; teardown errors are non-fatal."""
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.quit()
            self._client = None

    async def send(self, message: EmailMessage) -> None:
        """Send one already-assembled message over the open connection."""
        if self._client is None:
            raise RuntimeError("SmtpSession.send called outside the context manager.")
        await self._client.send_message(message)

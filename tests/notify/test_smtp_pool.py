"""``send_one`` reuses one authenticated connection (2026-09-02 regression).

It used to connect, authenticate, send and quit for **every** message — one
login per email. Relays rate-limit logins, not messages, so a study emitting a
job-state email per transition walks into that limit: on the demo box a 400-run
optimisation had every send failing with ``454 4.7.0 Too many login attempts``,
and once hard-throttled the relay stopped answering ``AUTH`` at all, surfacing
as a bare ``SMTPServerDisconnected`` that reads like a network fault.

These tests pin the property that fixes it — *N messages, one login* — and the
two things a pooled connection must get right that a one-shot never had to:
noticing the relay closed the socket, and noticing an admin changed the server.
"""

from __future__ import annotations

import time
from email.message import EmailMessage
from typing import Any, ClassVar

import pytest

from matofx.notify import smtp as smtp_module
from matofx.notify.smtp import _SmtpPool

_SETTINGS: dict[str, Any] = {
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "smtp_username": "sender@example.com",
    "smtp_password": "secret",
    "use_tls": False,
    "use_starttls": True,
    "tls_verify": True,
    "timeout_seconds": 10,
}


class FakeSmtp:
    """Records what the pool actually does to a connection."""

    instances: ClassVar[list[FakeSmtp]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.logins: list[str] = []
        self.sent: list[str] = []
        self.quits = 0
        self.is_connected = False
        self.fail_next_send = False
        FakeSmtp.instances.append(self)

    async def connect(self) -> None:
        self.is_connected = True

    async def login(self, username: str, _password: str) -> None:
        self.logins.append(username)

    async def send_message(self, message: EmailMessage) -> None:
        if self.fail_next_send:
            self.fail_next_send = False
            self.is_connected = False
            raise ConnectionResetError("relay closed the connection")
        self.sent.append(str(message["Subject"]))

    async def quit(self) -> None:
        self.quits += 1
        self.is_connected = False


@pytest.fixture(autouse=True)
def _fake_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSmtp.instances = []
    monkeypatch.setattr(smtp_module.aiosmtplib, "SMTP", FakeSmtp)


def _message(subject: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "sender@example.com"
    msg["To"] = "someone@example.com"
    msg.set_content("body")
    return msg


async def test_a_burst_of_emails_costs_exactly_one_login() -> None:
    """The whole point: relays rate-limit logins, and this used to be 20."""
    pool = _SmtpPool()
    for i in range(20):
        await pool.send(_SETTINGS, _message(f"job {i}"))

    assert len(FakeSmtp.instances) == 1
    assert FakeSmtp.instances[0].logins == ["sender@example.com"]
    assert len(FakeSmtp.instances[0].sent) == 20


async def test_every_message_is_still_delivered_once() -> None:
    """Pooling must not drop or duplicate anything."""
    pool = _SmtpPool()
    for subject in ("a", "b", "c"):
        await pool.send(_SETTINGS, _message(subject))

    assert FakeSmtp.instances[0].sent == ["a", "b", "c"]


async def test_a_relay_with_no_username_is_never_asked_to_log_in() -> None:
    """An open internal relay has no credentials to rate-limit."""
    pool = _SmtpPool()
    await pool.send({**_SETTINGS, "smtp_username": ""}, _message("x"))

    assert FakeSmtp.instances[0].logins == []


async def test_a_closed_connection_is_reopened_before_the_next_send() -> None:
    """Relays close idle sessions silently; a pool must notice."""
    pool = _SmtpPool()
    await pool.send(_SETTINGS, _message("first"))
    FakeSmtp.instances[0].is_connected = False

    await pool.send(_SETTINGS, _message("second"))

    assert len(FakeSmtp.instances) == 2
    assert FakeSmtp.instances[1].sent == ["second"]


async def test_a_send_that_dies_on_a_reused_connection_is_retried_once() -> None:
    """The failure mode pooling introduces: the socket looked open and was not.

    A one-shot send never had this problem, so the retry is the price of the
    fix — and it must actually deliver, not just reconnect.
    """
    pool = _SmtpPool()
    await pool.send(_SETTINGS, _message("first"))
    FakeSmtp.instances[0].fail_next_send = True

    await pool.send(_SETTINGS, _message("second"))

    assert len(FakeSmtp.instances) == 2
    assert FakeSmtp.instances[1].sent == ["second"]


async def test_a_failure_on_a_fresh_connection_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection we just opened failing is a real failure. Retrying it
    would risk delivering the same email twice for no gain."""

    def _fail_on_open(**kwargs: Any) -> FakeSmtp:
        client = FakeSmtp(**kwargs)
        client.fail_next_send = True
        return client

    monkeypatch.setattr(smtp_module.aiosmtplib, "SMTP", _fail_on_open)
    with pytest.raises(ConnectionResetError):
        await _SmtpPool().send(_SETTINGS, _message("only"))

    assert len(FakeSmtp.instances) == 1, "a fresh failure must not reconnect"


async def test_changing_the_server_reopens_rather_than_reusing() -> None:
    """An admin who repoints SMTP must not keep sending to the old host."""
    pool = _SmtpPool()
    await pool.send(_SETTINGS, _message("old host"))
    await pool.send({**_SETTINGS, "smtp_host": "smtp.other.com"}, _message("new host"))

    assert len(FakeSmtp.instances) == 2
    assert FakeSmtp.instances[1].kwargs["hostname"] == "smtp.other.com"
    assert FakeSmtp.instances[0].quits == 1, "the old connection must be closed"


async def test_changed_credentials_reopen_too() -> None:
    """Same reasoning, and the one an admin is most likely to hit: a rotated
    password kept working until the process restarted."""
    pool = _SmtpPool()
    await pool.send(_SETTINGS, _message("before"))
    await pool.send({**_SETTINGS, "smtp_password": "rotated"}, _message("after"))

    assert len(FakeSmtp.instances) == 2


async def test_a_long_idle_connection_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the idle window the socket is assumed dead even if it claims not
    to be — reconnecting is cheap next to a failed notification."""
    pool = _SmtpPool()
    await pool.send(_SETTINGS, _message("first"))

    real_monotonic = time.monotonic
    monkeypatch.setattr(
        smtp_module.time,
        "monotonic",
        lambda: real_monotonic() + smtp_module._POOL_IDLE_SECONDS + 1.0,
    )
    await pool.send(_SETTINGS, _message("second"))

    assert len(FakeSmtp.instances) == 2


async def test_closing_the_pool_quits_the_connection() -> None:
    pool = _SmtpPool()
    await pool.send(_SETTINGS, _message("x"))
    await pool.aclose()

    assert FakeSmtp.instances[0].quits == 1


async def test_closing_an_unused_pool_is_harmless() -> None:
    """Shutdown runs whether or not anything was ever emailed."""
    await _SmtpPool().aclose()
    assert FakeSmtp.instances == []

"""Conftest for the notify tests — re-uses the shared API fixtures."""

from __future__ import annotations

from typing import Any

import pytest

from matofx.notify import smtp as smtp_module
from tests._api_fixtures import *  # noqa: F403  -- re-export for fixture discovery


def patch_smtp(monkeypatch: pytest.MonkeyPatch, send: Any) -> None:
    """Fake the pooled SMTP client, driving an ``aiosmtplib.send``-shaped fake.

    ``send_one`` no longer calls ``aiosmtplib.send``: it holds one
    authenticated connection open and reuses it across sends, because relays
    rate-limit **logins** and a burst of job-state emails was tripping that
    limit. Tests still describe what they expect as "one message, sent with
    these connection kwargs", which is the clearer thing to assert — so this
    adapts the class-based client back to that shape rather than rewriting
    every expectation.

    ``send`` is called as ``await send(message, **connection_kwargs)``; raising
    from it surfaces exactly as a failed send.
    """

    class _Client:
        """Minimal stand-in for ``aiosmtplib.SMTP``, always healthy."""

        def __init__(self, **kwargs: Any) -> None:
            self._kwargs = kwargs

        async def connect(self) -> None:
            """Nothing to open."""

        async def login(self, _username: str, _password: str) -> None:
            """Credentials are the real client's business, not the fake's."""

        @property
        def is_connected(self) -> bool:
            """Always usable, so the pool exercises its reuse path."""
            return True

        async def send_message(self, message: Any) -> None:
            await send(message, **self._kwargs)

        async def quit(self) -> None:
            """Nothing to close."""

    monkeypatch.setattr("matofx.notify.smtp.aiosmtplib.SMTP", _Client)
    # The pool is module state and outlives a single test; give each one its
    # own so a connection opened here can never be reused over there.
    monkeypatch.setattr(smtp_module, "_pool", smtp_module._SmtpPool())

"""A licence alert must never own the sign-in's latency.

Found by stress-testing on 2026-08-12. `notify_licence_alert` walks its
recipients sequentially and SMTP's own timeout defaults to 30s, so an
unreachable mail relay blocked the login for 30s *per administrator*.

The case that makes it serious is break-glass: that alert fires when a
Super User is signing in to a site whose licence is already broken —
plausibly the same outage that took the mail relay out. Making them wait
minutes to get in, in order to send mail about the fact that they are
getting in, inverts the priority completely. A hang also reads to a user
as an outage, where a missing email reads as a missing email.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from matofx.api.routers import auth


class _StalledNotifier:
    """SMTP that accepts the connection and never answers."""

    def __init__(self, delay: float = 30.0) -> None:
        self.delay = delay
        self.calls = 0

    async def notify_licence_alert(
        self, session: Any, *, kind: str, summary: str, detail: str
    ) -> None:
        del session, kind, summary, detail  # signature matches the protocol
        self.calls += 1
        await asyncio.sleep(self.delay)


class _ExplodingNotifier:
    async def notify_licence_alert(
        self, session: Any, *, kind: str, summary: str, detail: str
    ) -> None:
        del session, kind, summary, detail
        raise RuntimeError("relay refused the connection")


@pytest.mark.asyncio
async def test_a_stalled_relay_cannot_hold_the_sign_in() -> None:
    notifier = _StalledNotifier(delay=30.0)

    started = time.monotonic()
    await auth._alert_licence(notifier, None, kind="breakglass", summary="s", detail="d")
    elapsed = time.monotonic() - started

    assert notifier.calls == 1
    assert elapsed < auth._ALERT_BUDGET_SECONDS + 2.0, (
        f"sign-in waited {elapsed:.1f}s on a stalled mail relay"
    )


@pytest.mark.asyncio
async def test_a_refusing_relay_does_not_fail_the_sign_in() -> None:
    """The event is still audited and reported; only the email is lost."""
    await auth._alert_licence(
        _ExplodingNotifier(), None, kind="slots_exhausted", summary="s", detail="d"
    )


@pytest.mark.asyncio
async def test_a_healthy_relay_is_awaited_normally() -> None:
    """The honest case: a working notifier is still actually called."""
    seen: list[str] = []

    class _Working:
        async def notify_licence_alert(
            self, session: Any, *, kind: str, summary: str, detail: str
        ) -> None:
            del session, summary, detail
            seen.append(kind)

    await auth._alert_licence(_Working(), None, kind="breakglass", summary="s", detail="d")
    assert seen == ["breakglass"]

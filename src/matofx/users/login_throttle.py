"""In-process per-IP login-attempt throttle.

Per-account lockout (``User.failed_login_count`` + ``locked_until``) stops
a brute force against a single username, but an attacker can spread
guesses across many usernames from one source IP without ever tripping
it. This adds an IP-level backstop: too many *failed* logins from one IP
within a sliding window blocks further attempts from that IP for a
cooldown, independent of which username is targeted.

State is in-process — a dict guarded by a lock. The ``matofx-api`` daemon
runs as a single uvicorn process (see :mod:`matofx.api.daemon`), so one
instance sees every login. A future multi-worker / clustered deployment
would need a shared store (Redis / DB); the per-account lockout remains
the durable, cross-process guarantee, so this is defence in depth, not
the primary control.

Time is measured with :func:`time.monotonic` (immune to wall-clock
adjustments). Callers may inject ``now`` for deterministic tests.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from matofx.config import AuthSettings
from matofx.users.errors import TooManyLoginAttemptsError


@dataclass
class IpLoginThrottle:
    """Sliding-window failed-login counter keyed by source IP."""

    max_failures: int
    window_seconds: float
    block_seconds: float
    _failures: dict[str, deque[float]] = field(default_factory=dict)
    _blocked_until: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, ip: str, *, now: float | None = None) -> None:
        """Raise :class:`TooManyLoginAttemptsError` if ``ip`` is blocked.

        A lapsed block is cleared on the way through, so the next attempt
        after the cooldown starts from a clean slate.
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            blocked_until = self._blocked_until.get(ip)
            if blocked_until is None:
                return
            if blocked_until > moment:
                raise TooManyLoginAttemptsError
            del self._blocked_until[ip]
            self._failures.pop(ip, None)

    def record_failure(self, ip: str, *, now: float | None = None) -> None:
        """Record one failed login from ``ip``; block the IP if it tips over."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            attempts = self._failures.setdefault(ip, deque())
            attempts.append(moment)
            cutoff = moment - self.window_seconds
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if len(attempts) >= self.max_failures:
                self._blocked_until[ip] = moment + self.block_seconds
                # Counter reset folds into the block window — the IP can't
                # be re-armed until the cooldown lapses in ``check``.
                del self._failures[ip]

    def record_success(self, ip: str) -> None:
        """Clear an IP's failure history after a successful login."""
        with self._lock:
            self._failures.pop(ip, None)
            self._blocked_until.pop(ip, None)


def make_login_throttle(auth: AuthSettings) -> IpLoginThrottle:
    """Build an :class:`IpLoginThrottle` from the auth settings."""
    return IpLoginThrottle(
        max_failures=auth.login_ip_rate_limit_max_failures,
        window_seconds=auth.login_ip_rate_limit_window_minutes * 60,
        block_seconds=auth.login_ip_rate_limit_block_minutes * 60,
    )

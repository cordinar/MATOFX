"""Unit tests for the per-IP login throttle (defence-in-depth rate limit)."""

from __future__ import annotations

import pytest

from matofx.config import AuthSettings
from matofx.users.errors import TooManyLoginAttemptsError
from matofx.users.login_throttle import IpLoginThrottle, make_login_throttle


def _throttle(
    max_failures: int = 3, window: float = 100.0, block: float = 200.0
) -> IpLoginThrottle:
    return IpLoginThrottle(max_failures=max_failures, window_seconds=window, block_seconds=block)


def test_under_threshold_is_allowed() -> None:
    t = _throttle(max_failures=3)
    t.record_failure("1.2.3.4", now=0.0)
    t.record_failure("1.2.3.4", now=1.0)
    # Two failures < threshold of 3 → still allowed.
    t.check("1.2.3.4", now=2.0)


def test_blocks_after_threshold_then_releases_after_cooldown() -> None:
    t = _throttle(max_failures=3, window=100.0, block=200.0)
    for i in range(3):
        t.record_failure("1.2.3.4", now=float(i))
    # Now blocked.
    with pytest.raises(TooManyLoginAttemptsError):
        t.check("1.2.3.4", now=5.0)
    # Still blocked just before the cooldown elapses (blocked at t=2 + 200).
    with pytest.raises(TooManyLoginAttemptsError):
        t.check("1.2.3.4", now=201.0)
    # After the cooldown the IP is allowed again and its history is cleared.
    t.check("1.2.3.4", now=203.0)


def test_window_evicts_stale_failures() -> None:
    t = _throttle(max_failures=3, window=100.0)
    t.record_failure("1.2.3.4", now=0.0)
    t.record_failure("1.2.3.4", now=10.0)
    # This failure is 150s after the first, so the first has aged out of the
    # 100s window — only two live failures, below the threshold.
    t.record_failure("1.2.3.4", now=150.0)
    t.check("1.2.3.4", now=151.0)


def test_failures_are_per_ip() -> None:
    t = _throttle(max_failures=2)
    t.record_failure("1.1.1.1", now=0.0)
    t.record_failure("1.1.1.1", now=1.0)
    with pytest.raises(TooManyLoginAttemptsError):
        t.check("1.1.1.1", now=2.0)
    # A different IP is unaffected.
    t.check("2.2.2.2", now=2.0)


def test_record_success_clears_history() -> None:
    t = _throttle(max_failures=3)
    t.record_failure("1.2.3.4", now=0.0)
    t.record_failure("1.2.3.4", now=1.0)
    t.record_success("1.2.3.4")
    # History wiped — a fresh failure starts the count from one.
    t.record_failure("1.2.3.4", now=2.0)
    t.check("1.2.3.4", now=3.0)


def test_make_login_throttle_reads_settings() -> None:
    auth = AuthSettings(
        login_ip_rate_limit_max_failures=7,
        login_ip_rate_limit_window_minutes=2,
        login_ip_rate_limit_block_minutes=3,
    )
    t = make_login_throttle(auth)
    assert t.max_failures == 7
    assert t.window_seconds == 120.0
    assert t.block_seconds == 180.0

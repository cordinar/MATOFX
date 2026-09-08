"""The attestation expires (ADR-0019 §C14, staleness).

A recorded verification says "the chain was intact **when it was
checked**". Treating that as permanent was the hole behind ADR-0019
§6.3: the full walk lives in the runner, so an install whose runner never
started — or quietly died — kept serving indefinitely on a verdict from
an arbitrarily long time ago, and nothing anywhere said so.

Making the attestation expire turns that into a self-enforcing condition
rather than something a separate watchdog must detect. The tests below
pin the three states and, importantly, the two that must **not** degrade:
a fresh install that has never been checked, and an operator who has
acknowledged the situation deliberately.
"""

from __future__ import annotations

import datetime as _dt

from matofx.compliance.integrity import (
    STALE_AFTER_HOURS,
    UNVERIFIED_AFTER_HOURS,
    IntegrityStatus,
)


def _checked(hours_ago: float, **overrides: object) -> IntegrityStatus:
    now = _dt.datetime.now(tz=_dt.UTC)
    fields: dict[str, object] = {
        "ok": True,
        "acknowledged": False,
        "checked_at": now - _dt.timedelta(hours=hours_ago),
        "age_hours": hours_ago,
    }
    fields.update(overrides)
    return IntegrityStatus(**fields)  # type: ignore[arg-type]


def test_a_recent_check_is_silent() -> None:
    status = _checked(0.5)
    assert not status.stale
    assert not status.unverified
    assert not status.read_only
    assert status.banner is None


def test_a_stale_check_warns_but_keeps_the_site_writable() -> None:
    """A banner is cheap; a read-only site is not. Warn long before biting."""
    status = _checked(STALE_AFTER_HOURS + 1)
    assert status.stale
    assert not status.unverified
    assert not status.read_only
    banner = status.banner or ""
    assert "runner" in banner, "the banner must name the likely cause"
    assert str(UNVERIFIED_AFTER_HOURS) in banner, "and what happens if it is ignored"


def test_an_expired_attestation_degrades_to_read_only() -> None:
    """The §6.3 regression: checking stops, and the site notices by itself."""
    status = _checked(UNVERIFIED_AFTER_HOURS + 1)
    assert status.unverified
    assert status.read_only
    assert "read-only" in (status.banner or "")


def test_a_never_checked_install_is_not_stale() -> None:
    """A fresh install has nothing to contradict — it must not be degraded."""
    status = IntegrityStatus(ok=True, acknowledged=False)
    assert status.age_hours is None
    assert not status.stale
    assert not status.unverified
    assert not status.read_only
    assert status.banner is None


def test_acknowledgement_lifts_an_expired_attestation() -> None:
    """Planned maintenance without the runner must not brick the site."""
    status = _checked(UNVERIFIED_AFTER_HOURS * 10, acknowledged=True)
    assert status.unverified
    assert not status.read_only


def test_a_failure_still_outranks_freshness() -> None:
    """A recent check that FAILED is read-only regardless of its age."""
    status = _checked(0.1, ok=False, detail="Record 12: hash mismatch.")
    assert status.read_only
    assert "FAILED" in (status.banner or "")

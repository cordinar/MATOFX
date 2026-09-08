"""Tests for ``matofx.ui.format``.

Covers the contract every UI page consumes:

- :func:`format_user_datetime` produces the user-locale + UTC-bracket
  shape per the user's §G38 instruction.
- :func:`format_job_id` zero-pads to seven digits.
- :func:`format_job_state` maps every closed-vocabulary state to a
  friendly label and falls back gracefully on unknown values.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from matofx.ui.format import (
    format_job_id,
    format_job_state,
    format_number,
    format_user_datetime,
)


@pytest.mark.parametrize(
    ("value", "precision", "expected"),
    [
        # No precision → 5 significant figures (raw doubles read cleanly).
        (3.141592653589793, None, "3.1416"),
        (12345.6789, None, "12346"),
        (0.000123456, None, "0.00012346"),
        # Whole numbers read as integers, not "1000.0".
        (1000.0, None, "1000"),
        (42, None, "42"),
        # Declared descriptor precision wins (a 0.01 step → 2 decimals).
        (3.14159, 0.01, "3.14"),
        (3.14159, 0.1, "3.1"),
        (2.0, 1.0, "2"),
        # Non-numeric / None pass through / placeholder.
        (None, None, "—"),
        ("n/a", None, "n/a"),
        (float("nan"), None, "nan"),
    ],
)
def test_format_number(value: object, precision: float | None, expected: str) -> None:
    assert format_number(value, precision=precision) == expected


@pytest.fixture(autouse=True)
def _clear_default_tz_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts without ``MATOFX_DEFAULT_TIMEZONE`` so cases are explicit."""
    monkeypatch.delenv("MATOFX_DEFAULT_TIMEZONE", raising=False)


# --- format_user_datetime ------------------------------------------------


def test_summer_london_shows_bst_with_utc_bracket() -> None:
    ts = datetime(2026, 5, 8, 13, 32, 11, tzinfo=UTC)
    assert (
        format_user_datetime(ts, install_tz="Europe/London")
        == "08 May 2026 14:32:11 BST (13:32:11 UTC)"
    )


def test_winter_london_shows_gmt_with_utc_bracket() -> None:
    # GMT and UTC happen to share the same wall-clock time, but the
    # labels are genuinely different — keeping the bracket clarifies
    # which one the user is reading.
    ts = datetime(2026, 1, 15, 13, 32, 11, tzinfo=UTC)
    assert (
        format_user_datetime(ts, install_tz="Europe/London")
        == "15 Jan 2026 13:32:11 GMT (13:32:11 UTC)"
    )


def test_tokyo_renders_jst() -> None:
    ts = datetime(2026, 5, 8, 13, 32, 11, tzinfo=UTC)
    assert (
        format_user_datetime(ts, install_tz="Asia/Tokyo")
        == "08 May 2026 22:32:11 JST (13:32:11 UTC)"
    )


def test_utc_zone_collapses_bracket() -> None:
    # When the display tz is UTC the bracket is just noise — collapse.
    ts = datetime(2026, 5, 8, 13, 32, 11, tzinfo=UTC)
    assert format_user_datetime(ts, install_tz="UTC") == "08 May 2026 13:32:11 UTC"


def test_default_tz_falls_back_to_utc() -> None:
    ts = datetime(2026, 5, 8, 13, 32, 11, tzinfo=UTC)
    assert format_user_datetime(ts) == "08 May 2026 13:32:11 UTC"


def test_default_tz_env_var_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATOFX_DEFAULT_TIMEZONE", "Asia/Tokyo")
    ts = datetime(2026, 5, 8, 13, 32, 11, tzinfo=UTC)
    assert format_user_datetime(ts) == "08 May 2026 22:32:11 JST (13:32:11 UTC)"


def test_default_timezone_setting_drives_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admin-set ``default_timezone`` General Setting wins over the env var."""
    import dataclasses  # noqa: PLC0415

    from matofx.ui import branding_cache  # noqa: PLC0415

    # Setting present → used even though the env var says Tokyo.
    monkeypatch.setenv("MATOFX_DEFAULT_TIMEZONE", "Asia/Tokyo")
    monkeypatch.setattr(
        branding_cache,
        "_current",
        dataclasses.replace(branding_cache._DEFAULTS, default_timezone="America/New_York"),
    )
    ts = datetime(2026, 5, 8, 17, 0, 0, tzinfo=UTC)
    out = format_user_datetime(ts)
    assert "EDT" in out  # May → US Eastern Daylight Time
    assert "(17:00:00 UTC)" in out


def test_user_timezone_preference_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signed-in user's own timezone beats the install default."""
    import dataclasses  # noqa: PLC0415

    from matofx.ui import branding_cache, state  # noqa: PLC0415

    monkeypatch.setattr(
        branding_cache,
        "_current",
        dataclasses.replace(branding_cache._DEFAULTS, default_timezone="America/New_York"),
    )
    monkeypatch.setattr(state, "current_timezone", lambda: "Asia/Tokyo")
    ts = datetime(2026, 5, 8, 13, 32, 11, tzinfo=UTC)
    out = format_user_datetime(ts)
    assert "JST" in out  # user's Tokyo wins over the NY default
    assert "(13:32:11 UTC)" in out


def test_user_timezone_auto_falls_through_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """``auto`` (no personal choice) falls through to the install default."""
    import dataclasses  # noqa: PLC0415

    from matofx.ui import branding_cache, state  # noqa: PLC0415

    monkeypatch.setattr(
        branding_cache,
        "_current",
        dataclasses.replace(branding_cache._DEFAULTS, default_timezone="America/New_York"),
    )
    monkeypatch.setattr(state, "current_timezone", lambda: "auto")
    ts = datetime(2026, 5, 8, 17, 0, 0, tzinfo=UTC)
    out = format_user_datetime(ts)
    assert "EDT" in out  # default NY applies


def test_naive_datetime_treated_as_utc() -> None:
    # §G38 mandates UTC storage; naive values from the DB should be
    # interpreted as UTC, not as the local tz.
    naive = datetime(2026, 5, 8, 13, 32, 11)
    assert (
        format_user_datetime(naive, install_tz="Europe/London")
        == "08 May 2026 14:32:11 BST (13:32:11 UTC)"
    )


def test_iso_8601_string_with_z_suffix_parses() -> None:
    assert (
        format_user_datetime("2026-05-08T13:32:11Z", install_tz="Europe/London")
        == "08 May 2026 14:32:11 BST (13:32:11 UTC)"
    )


def test_iso_8601_string_with_offset_parses() -> None:
    assert (
        format_user_datetime("2026-05-08T13:32:11+00:00", install_tz="Europe/London")
        == "08 May 2026 14:32:11 BST (13:32:11 UTC)"
    )


def test_iso_8601_with_microseconds_round_trips() -> None:
    # API responses commonly include microseconds — make sure they
    # don't break parsing.
    out = format_user_datetime("2026-05-08T13:32:11.123456+00:00", install_tz="Europe/London")
    assert out == "08 May 2026 14:32:11 BST (13:32:11 UTC)"


def test_aware_non_utc_datetime_converts_correctly() -> None:
    # An already-aware datetime in a non-UTC zone must round-trip via
    # UTC (canonical) before re-converting to the display zone.
    aware = datetime(2026, 5, 8, 22, 32, 11, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert (
        format_user_datetime(aware, install_tz="Europe/London")
        == "08 May 2026 14:32:11 BST (13:32:11 UTC)"
    )


def test_unknown_tz_silently_falls_back_to_utc() -> None:
    ts = datetime(2026, 5, 8, 13, 32, 11, tzinfo=UTC)
    assert format_user_datetime(ts, install_tz="Mars/Olympus") == "08 May 2026 13:32:11 UTC"


def test_none_returns_em_dash() -> None:
    assert format_user_datetime(None) == "—"


def test_empty_string_returns_em_dash() -> None:
    assert format_user_datetime("") == "—"


def test_unparseable_string_returns_em_dash() -> None:
    assert format_user_datetime("not-a-timestamp") == "—"


# --- format_job_id -------------------------------------------------------


def test_job_id_zero_pads_to_seven_digits() -> None:
    assert format_job_id(1234) == "0001234"


def test_job_id_accepts_string_input() -> None:
    assert format_job_id("5678") == "0005678"


def test_job_id_handles_zero() -> None:
    assert format_job_id(0) == "0000000"


def test_job_id_does_not_truncate_eight_digits() -> None:
    # Above 9_999_999 the legacy convention can't pad — leave it intact.
    assert format_job_id(12345678) == "12345678"


def test_job_id_none_returns_em_dash() -> None:
    assert format_job_id(None) == "—"


def test_job_id_garbage_returns_em_dash() -> None:
    assert format_job_id("xyz") == "—"


# --- format_job_state ----------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("created", "New"),
        ("queued", "Queued"),
        ("running", "Running"),
        ("complete", "Complete"),
        ("failed", "Failed"),
        ("pending_approval", "Pending approval"),
        ("denied", "Denied"),
        ("running_repeat", "Re-running"),
        ("cancelled", "Cancelled"),
    ],
)
def test_known_states_render_friendly_labels(state: str, expected: str) -> None:
    assert format_job_state(state) == expected


def test_unknown_state_falls_back_to_titlecase() -> None:
    # A state added in a later phase before the formatter catches up
    # must still render — title-case + space-substitution gets us a
    # legible fallback ("future_state" → "Future state").
    assert format_job_state("future_state") == "Future state"


def test_empty_state_returns_em_dash() -> None:
    assert format_job_state(None) == "—"
    assert format_job_state("") == "—"

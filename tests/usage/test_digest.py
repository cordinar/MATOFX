"""Unit tests for the pure helpers in :mod:`matofx.usage.digest`.

The send path (SMTP fan-out, disabled-subsystem, per-recipient
isolation) is covered end-to-end by the digest router tests.
"""

from __future__ import annotations

from matofx.usage.digest import _dedupe


def test_dedupe_trims_drops_blanks_and_deduplicates() -> None:
    assert _dedupe([" Alice@x.com ", "alice@x.com", "", "  ", "bob@y.com"]) == [
        "Alice@x.com",
        "bob@y.com",
    ]

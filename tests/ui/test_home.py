"""Unit tests for the pure helpers in ``matofx.ui.pages.home``.

The page handler itself runs inside a NiceGUI page context the ASGI
test transport can't fully drive (per ``test_mount.py``'s comment),
so we stick to the pure data-shaping helpers — the table-row builders,
formatting, and truncation. The async data-collectors take an
ApiClient and do API roundtrips; those are exercised end-to-end via
the api_client tests in ``tests/ui/test_api_client.py`` (Postgres-only).
"""

from __future__ import annotations

import pytest

from matofx.ui.pages.home import _short_timestamp, _truncate


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("2026-05-06T08:30:42.123456+00:00", "2026-05-06 08:30"),
        ("2026-05-06T08:30:42", "2026-05-06 08:30"),
        # Anything that doesn't look like ISO 8601 falls through unchanged
        # — the user's locale text appears verbatim instead of getting eaten.
        ("2026-05-06", "2026-05-06"),
        ("yesterday", "yesterday"),
    ],
)
def test_short_timestamp(value: object, expected: str) -> None:
    assert _short_timestamp(value) == expected


@pytest.mark.parametrize(
    ("text", "max_len", "expected"),
    [
        ("short", 80, "short"),
        ("", 80, ""),
        # An exact-length string is left alone (no ellipsis).
        ("abcde", 5, "abcde"),
        # One over → ellipsis appended (and the last char kept is shifted to fit).
        ("abcdef", 5, "abcd…"),
        # Trailing whitespace is stripped before the ellipsis so the result
        # doesn't read like ``abc …`` with a stray space before it.
        ("ab cdef", 5, "ab c…"),
    ],
)
def test_truncate(text: str, max_len: int, expected: str) -> None:
    assert _truncate(text, max_len) == expected

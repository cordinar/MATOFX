"""Mask helpers for ``pattern``-formatted project metadata fields.

A mask describes a fixed-shape alphanumeric code (e.g. a part or
serial number). Two token characters are special; every other
character is a literal that must appear verbatim:

- ``0`` matches a single digit ``[0-9]``.
- ``A`` matches a single letter ``[A-Za-z]`` (either case).

So the mask ``AA-000-0000`` accepts ``AB-123-4567`` and rejects
``abc-12-99``. The mask is stored on
:attr:`matofx.hierarchy.orm.ProjectMetadataField.format_mask`; the
service validates entered values against it on save.
"""

from __future__ import annotations

import re

_TOKENS: dict[str, str] = {"0": r"[0-9]", "A": r"[A-Za-z]"}


def mask_has_token(mask: str) -> bool:
    """Return whether ``mask`` has at least one ``0`` or ``A`` token.

    A mask of only literals (no token) would accept exactly one fixed
    string, which is never a useful field — the caller rejects it.
    """
    return any(ch in _TOKENS for ch in mask)


def mask_to_regex(mask: str) -> re.Pattern[str]:
    """Compile ``mask`` into an anchored, full-match regular expression."""
    body = "".join(_TOKENS.get(ch, re.escape(ch)) for ch in mask)
    return re.compile(f"^{body}$")


def matches_mask(mask: str, value: str) -> bool:
    """Return whether ``value`` matches ``mask`` exactly (full match)."""
    return mask_to_regex(mask).fullmatch(value) is not None

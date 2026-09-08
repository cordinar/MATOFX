"""Coverage errors — plain-English, safe to surface in the UI.

Per ``feedback_users_are_not_modellers`` every message here is written for
an engineer who is not a modeller: what went wrong, in their words, and what
they can do about it.
"""

from __future__ import annotations


class CoverageError(Exception):
    """A coverage request couldn't be satisfied (plain-English, UI-safe).

    Deliberately narrow. A *small* or *degenerate* point set is **not** an
    error — it returns the ``insufficient`` verdict, because "we cannot tell"
    is an answer and raising would deny the user one. This is for genuinely
    malformed input: ragged columns, an inverted envelope, a log scale over a
    span that includes zero.
    """


__all__ = ["CoverageError"]

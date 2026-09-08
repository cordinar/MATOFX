"""Exceptions for the auto-generated results-summaries engine.

The collect/template path raises only on programmer error (a non-terminal
result handed to a collector, an unknown surface kind). The *enrichment*
path never raises — it falls back to the templated baseline — so there is
no enrichment exception type here by design.
"""

from __future__ import annotations


class SummaryError(Exception):
    """Base class for summary-engine programmer errors."""


class UnknownSurfaceKindError(SummaryError):
    """A surface kind outside the closed vocabulary was requested."""


class NonTerminalResultError(SummaryError):
    """A summary was requested for a result that is not in a terminal state.

    Summaries are only produced for ``complete`` / ``failed`` / ``partial`` /
    ``cancelled`` results; running or queued results show live status instead.
    """

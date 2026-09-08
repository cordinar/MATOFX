"""Error hierarchy for job cohorts (ADR-0008 §3).

All messages are plain-English (F30) — they surface verbatim in the API
and the cohort UI, never a raw expression or a stack-trace fragment.
"""

from __future__ import annotations


class CohortError(Exception):
    """Base for every run-cohort failure."""


class CohortConfigError(CohortError):
    """The cohort definition is malformed or self-inconsistent.

    Raised at create / update — e.g. an empty ``tolerated_tool_version_ids``
    list (§3.2 requires at least one), a tolerated version that doesn't
    belong to the cohort's tool, an ``input_ranges`` entry with
    ``max < min``, or a ``cross_field_predicate`` that won't compile under
    ``safe_expr``.
    """


class CohortStateError(CohortError):
    """A cohort action was attempted from the wrong state.

    Plain-English (F30); safe to surface directly — e.g. editing an
    already-archived cohort.
    """

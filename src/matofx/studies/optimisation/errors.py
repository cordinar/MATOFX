"""Error hierarchy for optimisation studies (ADR-0009 §3 / §10).

All messages are plain-English (F30) — they surface verbatim in the API
and the optimisation UI, never a raw expression or a stack-trace
fragment. ``OptimisationConfigError`` mirrors the cohort family's
``CohortConfigError``: a single structured failure raised on the first
broken rule in :func:`matofx.studies.optimisation.config.validate_config`.
"""

from __future__ import annotations


class OptimisationError(Exception):
    """Base for every optimisation-study failure."""


class OptimisationConfigError(OptimisationError):
    """The study configuration is malformed or self-inconsistent.

    Raised at ``create_draft`` / ``update_draft`` / ``configure`` — e.g.
    a factor that isn't in the pinned descriptor, factor bounds that
    widen past the descriptor's box, no optimised response, a
    ``cross_field`` constraint that references an undeclared factor, a
    missing mandatory ``max_runs`` budget, or a strategy whose objective
    shape doesn't match the responses (§3.3).
    """


class OptimisationStateError(OptimisationError):
    """A study action was attempted from the wrong state.

    Plain-English (F30); safe to surface directly — e.g. editing a study
    that has already left ``draft``, or configuring one that is not in
    ``draft``.
    """


class OptimiserStateCorruptError(OptimisationError):
    """The persisted optimiser state failed its integrity check (§10).

    Raised on rehydrate when the disk-blob's sha256 doesn't match the
    recorded digest, or the blob can't be deserialised. The runtime parks
    the study (leaves it ``running`` with the error surfaced) rather than
    silently continuing on bad state — the durable ``study_result``
    history is the source of truth, and recovery is an explicit admin
    action (non-negotiable #3).
    """

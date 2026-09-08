"""Error hierarchy for the parametric-studies sampling-plan generator.

All messages are plain-English (F30) — they surface verbatim in the
pre-submit confirmation and the API, never a raw expression or a
stack-trace fragment.
"""

from __future__ import annotations


class StudyGenerationError(Exception):
    """Base for every sampling-plan generation failure."""


class FactorSpecError(StudyGenerationError):
    """The factor specification is malformed or self-inconsistent.

    Raised while parsing the ``factor_spec`` JSON (design §3) — e.g. a
    vary block whose ``kind`` doesn't match the factor ``type``, a
    range with ``max <= min``, an empty value list, or a study with no
    varied factor at all.
    """


class ModeNotApplicableError(StudyGenerationError):
    """The chosen sampling mode is incompatible with the factors.

    Carries the plain-English reason the UI shows when it greys the
    mode out (design §9 step 3) — e.g. "Response-surface designs need
    every varied input to be a continuous numeric range; 'mesh density'
    is a list of options." The raw mode/factor identifiers are not
    exposed to the end user beyond the human label.
    """


class NoValidPointsError(StudyGenerationError):
    """Generation produced points but every one failed cross-field validation.

    A study with zero runnable points is degenerate — refuse rather
    than create an empty "running" study. The message names the
    constraint(s) that ate the matrix (design §4 step 5 / §9).
    """

    def __init__(self, raw_count: int, reasons: dict[str, int]) -> None:
        """Build the plain-English refusal from the skip tally."""
        self.raw_count = raw_count
        self.reasons = reasons
        if reasons:
            top = max(reasons, key=lambda k: reasons[k])
            detail = f" All {raw_count} were skipped because '{top}'."
        else:
            detail = ""
        super().__init__(
            f"This study has no runnable points.{detail} Widen the ranges "
            f"or relax the factor specification, then try again."
        )


class StudyStateError(Exception):
    """A study lifecycle action was attempted from the wrong state.

    Plain-English (F30); safe to surface directly — e.g. submitting a
    study that isn't a ``draft``, or an unknown sampling mode at
    creation.
    """


class RunCapExceededError(StudyGenerationError):
    """The plan would exceed the study's frozen run cap (design §4 step 4).

    ``generated`` survived cross-field filtering; ``cap`` is the
    install setting frozen on the study row. The message is the exact
    F30 copy from the design.
    """

    def __init__(self, generated: int, cap: int) -> None:
        """Build the plain-English refusal."""
        self.generated = generated
        self.cap = cap
        super().__init__(
            f"This study would run {generated} simulations; the limit is "
            f"{cap}. Reduce the ranges or ask your administrator to raise "
            f"the limit."
        )

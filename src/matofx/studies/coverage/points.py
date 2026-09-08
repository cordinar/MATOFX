"""Point-set resolution — which points, in which space (ADR-0020 §1).

Everything downstream depends on answering that precisely. Getting it wrong
would overstate coverage, which is the exact failure ADR-0020 exists to
prevent, so this module is deliberately literal about its three rules:

- the points are the **training set**, not cohort membership (§1.1);
- the factors are **inferred**, not declared — an ad-hoc cohort has no
  ``FactorSpec`` (§1.2);
- an input that never varied is **pinned**, not absent (§1.2), and the
  reserved ``time`` axis is **derived** — a real predictor nobody can
  commission (§1.2.1).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from matofx.studies.coverage.errors import CoverageError
from matofx.studies.surrogate.ingest import TIME_FACTOR, FieldTrainingData, TrainingData

if TYPE_CHECKING:
    from collections.abc import Mapping

    from matofx.tools.descriptor import ToolDescriptor

_MIN_VARIED_DISTINCT = 2
"""Distinct values an input needs to count as an axis — ``analysis.
varied_columns``' rule, restated here because coverage applies it to
categorical columns too."""


@dataclass(frozen=True, slots=True)
class PointSet:
    """The training points coverage measures, and where they came from.

    ``rows`` is one mapping per training row over the **varied** factors
    only; ``constants`` holds the inputs that never varied (they become
    ``pinned`` factors in the envelope). Exclusion counts travel with the
    points so the report can explain "62 of 100" rather than presenting 62
    as though it were the whole cohort.
    """

    rows: tuple[Mapping[str, Any], ...] = ()
    factor_names: tuple[str, ...] = ()
    constants: dict[str, Any] = field(default_factory=dict)
    derived: tuple[str, ...] = ()
    numeric_names: frozenset[str] = frozenset()
    unusable_names: tuple[str, ...] = ()
    """Inputs present in the archive with no usable value in any run.

    They can be neither a factor (nothing varies) nor a pinned constant
    (there is no value to pin), so they have no axis. Recorded so the report
    can *say so*: an input that vanished silently would leave a user hunting
    a row that is not there, and it is precisely the input a model will later
    be asked to extrapolate along."""
    n_excluded_invalid: int = 0
    n_excluded_filtered: int = 0
    excluded_reason_counts: dict[str, int] = field(default_factory=dict)
    source_kind: str = "cohort"
    descriptor: ToolDescriptor | None = None

    @property
    def n_points(self) -> int:
        """How many training points there are (never ``TrainingData.n_rows``).

        The two collectors disagree on what ``n_rows`` counts — post-validity
        on the scalar path, pre-validity on the field one — so coverage
        derives its own count from the rows it actually holds.
        """
        return len(self.rows)

    @property
    def actionable_names(self) -> tuple[str, ...]:
        """Varied factors a suggested design point could actually move."""
        return tuple(n for n in self.factor_names if n not in self.derived)

    def values(self, name: str) -> list[Any]:
        """Every row's value for ``name`` (``None`` where a row lacks it).

        **A constant input falls back to its constant.** ``rows`` carries only
        the *varied* factors — an input that never changed lives once in
        :attr:`constants` rather than repeated on every row — so a plain
        ``row.get`` returns ``None`` for it, which reads as "no value
        recorded" when the truth is "this value, on every run".

        That distinction had no consequence while a pinned axis was excluded
        from every measurement. Once one can be opened up and swept (§11.5) it
        becomes a real column, and without this fallback its column normalises
        to all-``NaN``: the axis is dropped from the joint radius and every
        pair containing it is silently unjudged — six of twenty-one on the
        demo archive, reported as "15 of 21 examined" with no reason given.

        **Per row, not all-or-nothing.** The fallback used to apply only when
        *no* row carried the name, which is true of the archive on its own and
        false the moment it is projected: the suggestion planner appends
        proposed runs that do carry the value, so one row having it switched
        the fallback off for the other twenty and they came back ``None``.

        That produced the reported "20 runs patchy, 40 well covered, 80
        patchy" — the twenty unknowns were counted as a level of their own, so
        the balance measure got *worse* the more runs were proposed.
        """
        if name in self.constants:
            constant = self.constants[name]
            return [row.get(name, constant) for row in self.rows]
        return [row.get(name) for row in self.rows]


def resolve_points(
    training: TrainingData | FieldTrainingData,
    *,
    descriptor: ToolDescriptor | None = None,
    factor_spec: object | None = None,  # noqa: ARG001 — domains are read in envelope.py
) -> PointSet:
    """Build the :class:`PointSet` for one training set (§1).

    Consumes the ``TrainingData`` ``ingest`` produced — post ADR-0017 validity
    gate and post ``FactorFilter`` — never raw cohort membership. Factors are
    inferred from the columns that actually vary; the rest are carried as
    constants. Raises :class:`CoverageError` only for a genuinely malformed
    training set (ragged columns), never for a small or degenerate one — "too
    few runs to tell" is a verdict, not an error.
    """
    numeric = dict(training.factor_columns)
    categorical = dict(training.categorical_columns)
    n = _row_count(numeric, categorical)

    varied_numeric = [
        name for name, col in numeric.items() if _n_distinct(col) >= _MIN_VARIED_DISTINCT
    ]
    varied_categorical = [
        name for name, col in categorical.items() if _n_distinct(col) >= _MIN_VARIED_DISTINCT
    ]
    factor_names = tuple(sorted(varied_numeric) + sorted(varied_categorical))

    constants = dict(training.input_constants)
    unusable: list[str] = []
    for name, col in list(numeric.items()) + list(categorical.items()):
        if name not in factor_names and name not in constants and n:
            # A *usable* value, not merely a non-None one. An all-NaN column
            # would otherwise be pinned at NaN — and since `NaN != NaN`, the
            # envelope could never contain anything again, so the tool's own
            # suggestions failed its own guard. An input with nothing usable
            # in it constrains nothing, and is left out.
            values = [v for v in col if _usable(v)]
            if values:
                constants[name] = values[0]
            else:
                unusable.append(name)

    rows: tuple[Mapping[str, Any], ...] = tuple(
        {name: (numeric.get(name) or categorical[name])[i] for name in factor_names}
        for i in range(n)
    )

    return PointSet(
        rows=rows,
        factor_names=factor_names,
        constants=constants,
        derived=_derived_axes(training, factor_names),
        numeric_names=frozenset(varied_numeric),
        unusable_names=tuple(unusable),
        n_excluded_invalid=training.n_excluded_invalid,
        n_excluded_filtered=training.n_excluded_filtered,
        excluded_reason_counts=dict(training.excluded_reason_counts),
        source_kind=training.source_ref.source_kind if training.source_ref else "cohort",
        descriptor=descriptor,
    )


def _derived_axes(
    training: TrainingData | FieldTrainingData, factor_names: tuple[str, ...]
) -> tuple[str, ...]:
    """The axes that predict but cannot be commissioned (§1.2.1).

    Only the reserved ``time`` column of a **time-resolved field** training
    set qualifies. ``ingest`` reserves the name on that path alone, so a
    scalar cohort with an input genuinely called ``time`` is a real design
    variable and must keep its suggestions.
    """
    is_time_resolved = isinstance(training, FieldTrainingData) and training.time_resolved
    if is_time_resolved and TIME_FACTOR in factor_names:
        return (TIME_FACTOR,)
    return ()


def _row_count(numeric: Mapping[str, list[Any]], categorical: Mapping[str, list[Any]]) -> int:
    """The shared row count, or raise if the columns are ragged."""
    lengths = {len(col) for col in numeric.values()} | {len(col) for col in categorical.values()}
    if not lengths:
        return 0
    if len(lengths) > 1:
        raise CoverageError(
            "These runs don't all describe the same inputs, so they can't be "
            "compared as a set. This usually means the cohort mixes tools or "
            "tool versions whose inputs changed."
        )
    return lengths.pop()


def _usable(value: Any) -> bool:
    """Whether a value can stand for anything — not ``None``, not non-finite."""
    if value is None:
        return False
    return not (isinstance(value, float) and not math.isfinite(value))


def _n_distinct(column: list[Any]) -> int:
    """Distinct usable values in a column (``None`` / non-finite ignored).

    Keyed by a canonical form rather than by the value itself: a ``material``
    input (ADR-0007) arrives as a **dict**, which is unhashable, and putting
    one in a set raised ``TypeError`` — taking the whole analysis down for
    every tool that uses the materials database.
    """
    seen: set[Any] = set()
    for value in column:
        if value is None:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        seen.add(canonical_key(value))
    return len(seen)


def canonical_key(value: Any) -> str:
    """A stable, hashable identity for any input value.

    Tool inputs are whatever the descriptor allows, which includes ``table``
    (a list of rows) and ``material`` (a dict). Neither is hashable, so
    anything that groups or de-duplicates input values has to go through this
    rather than through the value itself.
    """
    if isinstance(value, str | int | float | bool) or value is None:
        return repr(value)
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


__all__ = ["PointSet", "resolve_points"]

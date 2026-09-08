"""Coverage diagnostics (ADR-0020 §6).

The two diseases a sparsity metric cannot see, plus the bookkeeping that
explains the point count.

- **Confounding.** Engineers rarely vary one factor at a time, so a cohort can
  be beautifully space-filling *along a diagonal ridge* and still be unable to
  separate two effects. Reported independently of the sparsity verdict —
  collapsing them would let that cohort pass as healthy.
- **Categorical imbalance.** 40 CFRP runs and 2 GFRP looks perfectly covered
  on every continuous axis, and the surrogate will then confidently predict a
  material it has effectively never seen. This one is **silent**, which is why
  it is a first-class verdict input rather than a footnote.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from matofx.studies.coverage.envelope import level_label
from matofx.studies.coverage.metrics import finite_rows
from matofx.studies.coverage.points import canonical_key

if TYPE_CHECKING:
    from collections.abc import Mapping

    from matofx.config import CoverageSettings
    from matofx.studies.coverage.envelope import Envelope
    from matofx.studies.coverage.points import PointSet

GroupingMode = Literal["split", "pooled", "n/a"]

_MIN_FACTORS_FOR_CORRELATION = 2
_LEVEL_TUPLE_WIDTH = 3
"""An underpopulated-level entry is (factor, level, count)."""


@dataclass(frozen=True, slots=True)
class CoverageDiagnostics:
    """Everything about the point set that is not a distance."""

    n_points: int = 0
    n_excluded_invalid: int = 0
    n_excluded_filtered: int = 0
    n_outside_envelope: int = 0
    n_unmeasurable: int = 0
    """Runs carrying a missing or non-finite value on a measured axis.

    They cannot be placed, so they cannot be measured — and they are counted
    rather than quietly parked at the axis minimum, which would have them
    "cover" the emptiest part of the range."""
    n_near_duplicates: int = 0
    factor_correlations: dict[tuple[str, str], float] | None = None
    max_abs_correlation: float | None = None
    confounded: bool = False
    condition_number: float | None = None
    level_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    underpopulated_levels: list[tuple[str, str, int]] = field(default_factory=list)
    grouping_mode: GroupingMode = "n/a"

    def to_json_obj(self) -> dict[str, Any]:
        """A JSON-safe view (§9 persists the report as JSON).

        ``factor_correlations`` is tuple-keyed — the natural shape for a
        pairwise matrix, and one JSON cannot express — so it flattens to
        ``"a|b"`` here, at the boundary, rather than distorting the in-memory
        type for the sake of storage.
        """
        return {
            "n_points": self.n_points,
            "n_excluded_invalid": self.n_excluded_invalid,
            "n_excluded_filtered": self.n_excluded_filtered,
            "n_outside_envelope": self.n_outside_envelope,
            "n_unmeasurable": self.n_unmeasurable,
            "n_near_duplicates": self.n_near_duplicates,
            "factor_correlations": (
                None
                if self.factor_correlations is None
                else {f"{a}|{b}": v for (a, b), v in sorted(self.factor_correlations.items())}
            ),
            "max_abs_correlation": self.max_abs_correlation,
            "confounded": self.confounded,
            "condition_number": self.condition_number,
            "level_counts": self.level_counts,
            "underpopulated_levels": [list(t) for t in self.underpopulated_levels],
            "grouping_mode": self.grouping_mode,
        }

    @classmethod
    def from_json_obj(cls, raw: Mapping[str, Any]) -> CoverageDiagnostics:
        """Rebuild diagnostics from their stored form (ADR-0020 §9).

        ``factor_correlations`` comes back from its flattened ``"a|b"`` keys.
        ``None`` stays ``None``: "we could not tell" and "we checked and found
        nothing" are different answers and must not merge on the way through
        storage.
        """
        flat = raw.get("factor_correlations")
        correlations: dict[tuple[str, str], float] | None = None
        if isinstance(flat, dict):
            correlations = {}
            for key, value in flat.items():
                a, _, b = str(key).partition("|")
                correlations[(a, b)] = float(value)
        return cls(
            n_points=int(raw.get("n_points") or 0),
            n_excluded_invalid=int(raw.get("n_excluded_invalid") or 0),
            n_excluded_filtered=int(raw.get("n_excluded_filtered") or 0),
            n_outside_envelope=int(raw.get("n_outside_envelope") or 0),
            n_unmeasurable=int(raw.get("n_unmeasurable") or 0),
            n_near_duplicates=int(raw.get("n_near_duplicates") or 0),
            factor_correlations=correlations,
            max_abs_correlation=_opt_float(raw.get("max_abs_correlation")),
            confounded=bool(raw.get("confounded", False)),
            condition_number=_opt_float(raw.get("condition_number")),
            level_counts={
                str(k): {str(kk): int(vv) for kk, vv in (v or {}).items()}
                for k, v in (raw.get("level_counts") or {}).items()
            },
            underpopulated_levels=[
                (str(t[0]), str(t[1]), int(t[2]))
                for t in (raw.get("underpopulated_levels") or [])
                if len(t) == _LEVEL_TUPLE_WIDTH
            ],
            grouping_mode=raw.get("grouping_mode", "n/a"),
        )


def _opt_float(value: Any) -> float | None:
    """``float(value)`` or ``None`` — a stored ``null`` means "not computed"."""
    return None if value is None else float(value)


def diagnose(
    points: PointSet, envelope: Envelope, *, settings: CoverageSettings
) -> CoverageDiagnostics:
    """Confounding, duplicates, balance and the exclusion bookkeeping."""
    x = envelope.normalise(points)
    correlations, max_abs, condition = _confounding(x, envelope)
    level_counts = _level_counts(points, envelope)

    return CoverageDiagnostics(
        n_points=points.n_points,
        n_excluded_invalid=points.n_excluded_invalid,
        n_excluded_filtered=points.n_excluded_filtered,
        n_outside_envelope=_n_outside(x),
        n_unmeasurable=_n_unmeasurable(x, points, envelope),
        n_near_duplicates=_n_near_duplicates(points, envelope, x, settings=settings),
        factor_correlations=correlations,
        max_abs_correlation=max_abs,
        confounded=max_abs is not None and max_abs > settings.max_abs_correlation,
        condition_number=condition,
        level_counts=level_counts,
        underpopulated_levels=_underpopulated(level_counts, settings=settings),
        grouping_mode=_grouping_mode(points, level_counts, settings=settings),
    )


# --- confounding ----------------------------------------------------------


def _confounding(
    x: np.ndarray, envelope: Envelope
) -> tuple[dict[tuple[str, str], float] | None, float | None, float | None]:
    """Pearson correlations + design-matrix condition number, or all ``None``.

    Omitted rather than zero-filled below two continuous/integer factors:
    "we cannot tell" and "they are uncorrelated" are different answers, and
    ``0.0`` would assert the second while meaning the first.
    """
    axes = envelope.metric_axes
    x = finite_rows(x)
    if len(axes) < _MIN_FACTORS_FOR_CORRELATION or x.shape[0] < _MIN_FACTORS_FOR_CORRELATION:
        return None, None, None
    if np.ptp(x, axis=0).min() == 0.0:
        return None, None, None  # a constant column makes correlation undefined

    matrix = np.corrcoef(x, rowvar=False)
    correlations: dict[tuple[str, str], float] = {}
    for i in range(len(axes)):
        for j in range(i + 1, len(axes)):
            value = float(matrix[i, j])
            correlations[tuple(sorted((axes[i], axes[j])))] = (  # type: ignore[index]
                0.0 if math.isnan(value) else value
            )
    max_abs = max(abs(v) for v in correlations.values())
    return correlations, max_abs, _condition_number(x)


def _condition_number(x: np.ndarray) -> float | None:
    """Condition number of the centred design matrix (``None`` if singular)."""
    centred = x - x.mean(axis=0)
    try:
        value = float(np.linalg.cond(centred))
    except np.linalg.LinAlgError:
        return None
    return None if not math.isfinite(value) else value


# --- duplicates -----------------------------------------------------------


def _n_near_duplicates(
    points: PointSet, envelope: Envelope, x: np.ndarray, *, settings: CoverageSettings
) -> int:
    """Points that add no information beyond one already counted.

    A duplicate is **exact equality on every discrete axis** (integer,
    categorical, pinned) **and** within ``duplicate_eps`` on the continuous
    ones. Epsilon must never touch an integer axis: on ``[1,5]`` the
    normalised spacing is 0.25, so a generously tuned epsilon would flag
    genuinely distinct adjacent ply counts as the same run.
    """
    if points.n_points < _MIN_FACTORS_FOR_CORRELATION:
        return 0
    axes = envelope.metric_axes
    continuous_cols = [i for i, a in enumerate(axes) if envelope.by_name(a).kind == "continuous"]
    discrete_names = [a for a in axes if envelope.by_name(a).kind == "integer"] + list(
        envelope.categorical_axes
    )

    eps = settings.duplicate_eps
    seen: list[int] = []
    duplicates = 0
    for i in range(points.n_points):
        if any(_same_point(points, x, i, j, discrete_names, continuous_cols, eps) for j in seen):
            duplicates += 1
        else:
            seen.append(i)
    return duplicates


def _same_point(
    points: PointSet,
    x: np.ndarray,
    i: int,
    j: int,
    discrete_names: list[str],
    continuous_cols: list[int],
    eps: float,
) -> bool:
    """Whether rows ``i`` and ``j`` are the same design point."""
    row_i, row_j = points.rows[i], points.rows[j]
    if any(
        canonical_key(row_i.get(name)) != canonical_key(row_j.get(name)) for name in discrete_names
    ):
        return False
    if not continuous_cols:
        return True
    delta = x[i, continuous_cols] - x[j, continuous_cols]
    return bool(math.sqrt(float((delta * delta).sum())) <= eps)


# --- categorical balance --------------------------------------------------


def _level_counts(points: PointSet, envelope: Envelope) -> dict[str, dict[str, int]]:
    """Runs per level, per categorical axis — every declared level included.

    A level the archive never ran counts **zero** rather than being absent: it
    is the 40/2 failure one step further on, and it must be visible.
    """
    counts: dict[str, dict[str, int]] = {}
    for name in envelope.categorical_axes:
        levels = envelope.by_name(name).levels or ()
        per_level = dict.fromkeys(levels, 0)
        for value in points.values(name):
            key = level_label(value)
            per_level[key] = per_level.get(key, 0) + 1
        counts[name] = per_level
    return counts


def _underpopulated(
    level_counts: dict[str, dict[str, int]], *, settings: CoverageSettings
) -> list[tuple[str, str, int]]:
    """``(factor, level, count)`` for every level below the configured floor."""
    return [
        (factor, level, count)
        for factor, per_level in sorted(level_counts.items())
        for level, count in sorted(per_level.items())
        if count < settings.min_level_points
    ]


def _grouping_mode(
    points: PointSet, level_counts: dict[str, dict[str, int]], *, settings: CoverageSettings
) -> GroupingMode:
    """Split the analysis per level-group, or pool it (§4.8).

    Splitting is correct only while the groups stay populated. Two selects of
    three levels is nine groups; three is twenty-seven, and a perfectly
    reasonable 40-run design becomes 1.5 runs per group with **every** group
    reading ``poor`` — a false alarm, since a GP with encoded categoricals
    shares information across levels. Balance is reported either way; only the
    continuous analysis changes.
    """
    if not level_counts:
        return "n/a"
    n_groups = 1
    for per_level in level_counts.values():
        n_groups *= max(len(per_level), 1)
    if not n_groups:
        return "n/a"
    return "split" if points.n_points / n_groups >= settings.min_points_per_group else "pooled"


def _n_outside(x: np.ndarray) -> int:
    """Training points outside the declared envelope (§1.3 — counted, kept).

    Only rows that can actually be placed: a row with a missing coordinate is
    not "outside the range", it is unmeasurable, and conflating the two would
    tell a user to widen an envelope when the real problem is missing data.
    """
    if x.shape[0] == 0 or x.shape[1] == 0:
        return 0
    usable = finite_rows(x)
    if usable.shape[0] == 0:
        return 0
    return int((~((usable >= 0.0) & (usable <= 1.0)).all(axis=1)).sum())


def _n_unmeasurable(x: np.ndarray, points: PointSet, envelope: Envelope) -> int:
    """Rows that cannot be placed — on **any** axis, numeric or categorical.

    The numeric half is the normalised matrix: a missing or non-finite
    coordinate leaves the row unplaceable, and it is counted rather than
    parked at the axis minimum where it would "cover" the emptiest part of
    the range.

    The categorical half was missed at first, and the two surfaces disagreed
    because of it. A categorical column contributes no column to ``x``, so a
    run whose ``material`` is ``null`` was measured as though nothing were
    wrong — while the predict-time guard called that same run **outside**,
    ``None`` not being a level the model ever saw. One real run in the demo
    archive is exactly this. A run that cannot be placed is unmeasurable on
    whichever kind of axis defeats it.
    """
    unplaceable: set[int] = set()
    if x.shape[0] and x.shape[1]:
        finite = np.isfinite(x).all(axis=1)
        unplaceable.update(int(i) for i, ok in enumerate(finite) if not ok)

    levels_by_axis = {
        factor.name: set(factor.levels or ())
        for factor in envelope.factors
        if factor.kind == "categorical"
    }
    for name, levels in levels_by_axis.items():
        for i, row in enumerate(points.rows):
            if level_label(row.get(name)) not in levels:
                unplaceable.add(i)
    return len(unplaceable)


__all__ = ["CoverageDiagnostics", "GroupingMode", "diagnose"]

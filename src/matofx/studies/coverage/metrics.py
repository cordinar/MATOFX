"""Model-free coverage metrics (ADR-0020 §4).

The criterion cohort preview and every cold-start case depend on. That is not
a fallback for an edge case: the customer with forty scattered ad-hoc jobs
wondering whether they have something trainable **is the modal case**, so
these have to work exactly when the data is worst.

All of them operate on the *normalised* continuous + integer sub-space —
``derived`` and ``pinned`` axes are measured apart (§4.1) — and normalisation
honours each axis's scale, so a log-sampled factor is measured in log space.
"""

from __future__ import annotations

import math
import statistics
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from matofx.studies.coverage.envelope import Envelope, FactorEnvelope
    from matofx.studies.coverage.points import PointSet

_MIN_PAIR = 2


def reference_radius(
    envelope: Envelope,
    n_points: int,
    *,
    n_candidates: int = 4096,
    seed: int = 0,
    replicates: int = 5,
) -> float:
    """The covering radius an evenly-spread design of ``n_points`` would achieve.

    **The yardstick a coverage score has to be measured against.** An earlier
    version scored against the unit hypercube's half-diagonal, `sqrt(d)/2` —
    the radius a single centred point leaves. That looks dimension-aware and
    is not: an achievable covering radius grows far faster with dimension than
    `sqrt(d)` does, so the score collapsed as factors were added regardless of
    how good the design was. Measured against a *perfect* 254-point Latin
    hypercube, it read ``good`` at 2 factors and ``poor`` at 8 — a verdict
    driven by the factor count rather than by the design, and a red light that
    is always red is one users learn to ignore (§7.1).

    So the reference is the same machinery laying out the same number of
    points evenly over the same envelope. The score then answers a question
    with a fixed meaning rather than one that drifts with the archive.

    **Averaged over ``replicates`` layouts, not taken from one.** A single
    stratified sample is itself a random draw, and its covering radius is
    noisy enough to matter: sweeping the standard, the bar came out 0.3283 at
    ten runs per input and 0.2443 at twelve -- a *larger* ideal design
    producing a 26% smaller bar, which is impossible in expectation and pure
    sampling noise in practice. Every score is divided by this number, so
    that noise was landing directly on the verdict. The median of a handful
    of layouts is stable and still cheap, because the result depends only on
    the envelope's shape and the point count -- never on the archive.
    """
    if n_points <= 0 or not envelope.metric_axes:
        return 0.0
    key = (_shape_signature(envelope), n_points, n_candidates, seed, max(1, replicates))
    cached = _REFERENCE_CACHE.get(key)
    if cached is not None:
        return cached

    radii = [
        covering_radius(
            envelope.sample(n_points, seed=seed + 2 * i),
            envelope,
            n_candidates=n_candidates,
            seed=seed + 1,
        )
        for i in range(max(1, replicates))
    ]
    value = float(statistics.median(radii))

    _REFERENCE_CACHE[key] = value
    while len(_REFERENCE_CACHE) > _REFERENCE_CACHE_MAX:
        _REFERENCE_CACHE.pop(next(iter(_REFERENCE_CACHE)))
    return value


_REFERENCE_CACHE: dict[tuple[Any, ...], float] = {}
_REFERENCE_CACHE_MAX = 512
"""Memoised reference radii.

The reference depends on the envelope's **shape** and a point count -- never
on the archive -- so the same answer is asked for repeatedly: once for the
joint pass and once per pair, and most pairs of a tool are the same shape as
each other. Caching turns the pairwise sweep's fifteen reference layouts into
two or three, which is what makes averaging over replicates affordable.

Bounded and FIFO-evicted; an unbounded dict keyed on a shape nobody controls
has no ceiling."""


def _shape_signature(envelope: Envelope) -> tuple[Any, ...]:
    """What actually determines a reference layout, and nothing else.

    Normalisation maps every metric axis to ``[0,1]``, so an axis's units and
    bounds do not reach the sample -- **except** on an integer axis, where
    ``lo``/``hi`` fix how many lattice points there are and where they fall.
    Two envelopes agreeing on this tuple produce identical reference designs.
    """
    return tuple(
        (f.kind, f.lo, f.hi) if f.kind == "integer" else (f.kind,)
        for f in envelope.factors
        if f.kind in ("continuous", "integer")
    )


def candidate_resolution(n_candidates: int, n_axes: int) -> float:
    """Effective candidate points per axis — how finely the space is probed.

    ``n_candidates ** (1 / n_axes)``. Below a handful, a covering radius over
    that sample is a coarse estimate rather than a measurement: 4096
    candidates is 64 per axis in two dimensions and **two** per axis in
    twelve. Callers surface this rather than presenting a noisy number as
    exact.
    """
    if n_axes <= 0:
        return float("inf")
    return float(n_candidates ** (1.0 / n_axes))


def finite_rows(x: np.ndarray) -> np.ndarray:
    """Rows with a usable value on every axis.

    A row carrying a missing or non-finite coordinate is not a design point:
    it cannot be placed, so it cannot be measured, and it must not be placed
    *somewhere* just to keep the matrix rectangular.
    """
    if x.shape[0] == 0 or x.shape[1] == 0:
        return x
    kept: np.ndarray = x[np.isfinite(x).all(axis=1)]
    return kept


def measured_rows(x: np.ndarray, envelope: Envelope) -> int:
    """How many rows :func:`covering_radius` actually measures over.

    **The count any yardstick has to be built for.** ``covering_radius``
    silently discards two kinds of row — one that cannot be placed (a missing
    or non-finite coordinate) and one that sits outside the declared envelope
    — so the number of rows a cohort *holds* is not the number its radius was
    achieved by. Scoring a 291-point radius against a 531-point reference
    made every archive with missing data look worse than it was.

    Defined by **calling the same filter** rather than by restating its rule:
    a second copy of "which rows count" is exactly the drift §3's F1 is about.
    """
    del envelope  # the filter is envelope-independent; kept for call-site clarity
    return int(_inside_unit_cube(x).shape[0])


def maximin_distance(x: np.ndarray) -> float | None:
    """Smallest pairwise distance — how tightly the points crowd.

    ``None`` below two points: with nothing to compare, "we cannot tell" is
    the honest answer and 0.0 would read as "maximally clustered".
    """
    x = finite_rows(x)
    if x.shape[0] < _MIN_PAIR:
        return None
    return float(_pairwise(x)[np.triu_indices(x.shape[0], k=1)].min())


def covering_radius(
    x: np.ndarray,
    envelope: Envelope,
    *,
    n_candidates: int = 4096,
    seed: int = 0,
) -> float:
    """Largest distance from anywhere in the envelope to the nearest point.

    **The headline sparsity number**: the biggest empty hole. Evaluated over a
    stratified candidate sample from ``envelope.sample`` rather than a full
    grid, whose cost explodes past ~4 factors — and that sample is generated
    on the integer lattice (§3.3), so a fully occupied lattice contributes no
    spurious gap at 1.5 or 2.5.

    Training points **outside** the declared envelope are dropped rather than
    clamped (§1.3): the measure is only meaningful inside the region being
    claimed, and clamping would manufacture false duplicates on the boundary.
    """
    candidates = envelope.sample(n_candidates, seed=seed)
    if candidates.shape[1] == 0:
        return 0.0
    inside = _inside_unit_cube(x)
    if inside.shape[0] == 0:
        # No points inside the claimed region at all — the whole envelope is
        # a hole, so the radius is its own diameter.
        return float(math.sqrt(candidates.shape[1]))
    return float(_cdist(candidates, inside).min(axis=1).max())


def centered_l2_discrepancy(x: np.ndarray) -> float | None:
    """Deviation from a uniform spread over the unit cube (lower is better).

    The standard centered-L2 formula (Hickernell). Complements the covering
    radius: the radius finds the single worst hole, discrepancy scores the
    overall evenness, and a design can be mediocre on one and fine on the
    other.
    """
    x = finite_rows(x)
    if x.shape[0] == 0 or x.shape[1] == 0:
        return None
    n, d = x.shape
    z = _inside_unit_cube(x, keep_all=True)

    term1 = (13.0 / 12.0) ** d
    a = 1.0 + 0.5 * np.abs(z - 0.5) - 0.5 * (z - 0.5) ** 2
    term2 = (2.0 / n) * float(np.prod(a, axis=1).sum())

    zi = z[:, None, :]
    zj = z[None, :, :]
    b = 1.0 + 0.5 * np.abs(zi - 0.5) + 0.5 * np.abs(zj - 0.5) - 0.5 * np.abs(zi - zj)
    term3 = (1.0 / n**2) * float(np.prod(b, axis=2).sum())
    return float(math.sqrt(max(term1 - term2 + term3, 0.0)))


def nn_distance_distribution(x: np.ndarray) -> tuple[float, ...]:
    """Each point's distance to its nearest neighbour — drives the histogram."""
    x = finite_rows(x)
    if x.shape[0] < _MIN_PAIR:
        return ()
    d = _pairwise(x)
    np.fill_diagonal(d, np.inf)
    return tuple(float(v) for v in d.min(axis=1))


def lattice_occupancy(points: PointSet, factor: FactorEnvelope) -> tuple[float, tuple[str, ...]]:
    """Fraction of an integer axis's attainable values present, and which are not.

    The measure that makes an integer axis honest (§3.3): an envelope of
    ``[1,5]`` with all five values run is **completely covered**, whereas a
    continuous reading would report holes at 1.5 / 2.5 and go on to propose
    running ply count 2.7 — not a valid input at all. The missing values are
    named because they *are* the suggestions.
    """
    lattice = factor.lattice()
    if not lattice:
        return 1.0, ()
    present = {
        round(v)
        for v in (_as_float(value) for value in points.values(factor.name))
        if v is not None
    }
    missing = tuple(str(v) for v in lattice if v not in present)
    return (len(lattice) - len(missing)) / len(lattice), missing


def trained_span(
    points: PointSet, factor: FactorEnvelope
) -> tuple[float, tuple[float | None, float | None]]:
    """Fraction of a declared axis the training data actually spans, and the span.

    The right measure for a ``derived`` axis (§4.1): it tells a user their
    model covers snapshots 0-23 of each run, and it is exactly what the guard
    needs — a prediction beyond the trained span is extrapolation whatever the
    envelope says.
    """
    numbers = [n for n in (_as_float(v) for v in points.values(factor.name)) if n is not None]
    if not numbers:
        return 0.0, (None, None)
    lo, hi = min(numbers), max(numbers)
    if factor.lo is None or factor.hi is None or factor.hi == factor.lo:
        return 1.0, (lo, hi)
    return (hi - lo) / (factor.hi - factor.lo), (lo, hi)


# --- internals ------------------------------------------------------------


def _pairwise(x: np.ndarray) -> np.ndarray:
    """Full euclidean distance matrix."""
    return _cdist(x, x)


def _cdist(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Euclidean distances between every row of ``a`` and every row of ``b``."""
    diff = a[:, None, :] - b[None, :, :]
    out: np.ndarray = np.sqrt((diff * diff).sum(axis=2))
    return out


def _inside_unit_cube(x: np.ndarray, *, keep_all: bool = False) -> np.ndarray:
    """Rows whose every coordinate lies in ``[0,1]`` (§1.3 — dropped, not clamped)."""
    if x.shape[0] == 0 or x.shape[1] == 0:
        return x
    x = finite_rows(x)
    if keep_all:
        clipped: np.ndarray = np.clip(x, 0.0, 1.0)
        return clipped
    mask = ((x >= 0.0) & (x <= 1.0)).all(axis=1)
    return x[mask]


def _as_float(value: object) -> float | None:
    """``float(value)`` for a real number (not bool / string), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


__all__ = [
    "candidate_resolution",
    "centered_l2_discrepancy",
    "covering_radius",
    "finite_rows",
    "lattice_occupancy",
    "maximin_distance",
    "measured_rows",
    "nn_distance_distribution",
    "reference_radius",
    "trained_span",
]

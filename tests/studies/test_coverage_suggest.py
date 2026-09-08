"""Suggested runs — greedy sequential selection (ADR-0020 §5, §5.1, slice 2).

Because posterior sigma is `y`-free, a whole batch of *N* points can be
planned without running any of them: pick the max-sigma point, condition on
it as though it had returned a value, recompute, pick again. Naive top-*N*
would return *N* adjacent points in the same hole.

`designs/integer-parameter-handling.md` states the non-negotiable these must
obey: *whenever a parameter is sampled automatically, an integer parameter is
always respected*.
"""

from __future__ import annotations

import math

import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import (
    FactorOverride,
    ResponseModel,
    build_envelope,
    resolve_points,
)
from matofx.studies.coverage.gp import suggest_points, variance_field
from tests.studies._coverage_fixtures import (
    descriptor,
    field_training,
    fit,
    integer,
    num,
    training,
    two_factor_rows,
)


def _prepared(rows, *, response=None, kind="gp", **kwargs):
    """``(model, points, envelope)`` for ``rows``."""
    resolve_kwargs = {k: kwargs.pop(k) for k in ("descriptor", "factor_spec") if k in kwargs}
    y = response if response is not None else [sum(v for v in r.values()) for r in rows]
    model = ResponseModel.scalar("y", fit(rows, y, kind=kind))
    points = resolve_points(training(rows), **resolve_kwargs)
    return model, points, build_envelope(points, **resolve_kwargs, **kwargs)


# --- greedy, never top-N --------------------------------------------------


def test_greedy_spreads_where_top_n_would_cluster() -> None:
    # One large hole in the middle: top-N returns N neighbours inside it,
    # greedy conditions after each pick and moves on.
    rows = [
        {"thickness": 1.0 + i * 0.15, "heat_load": 1.0 + j * 0.15}
        for i in range(4)
        for j in range(4)
    ] + [
        {"thickness": 9.0 - i * 0.15, "heat_load": 9.0 - j * 0.15}
        for i in range(4)
        for j in range(4)
    ]
    model, _, envelope = _prepared(rows)

    greedy = suggest_points(model, envelope, n=5, seed=7).points
    field = variance_field(model, envelope, n_candidates=2048, seed=7)
    order = sorted(range(len(field.sigma)), key=lambda i: -field.sigma[i])
    top_n = [field.points[i] for i in order[:5]]

    assert _min_separation(greedy) > _min_separation(top_n)


def test_a_batch_is_planned_with_no_solver_calls_and_no_response_values() -> None:
    rows = two_factor_rows(4, 4)
    model, _, envelope = _prepared(rows)
    batch = suggest_points(model, envelope, n=4, seed=1)

    assert len(batch.points) == 4
    # Each suggestion is a complete, submittable input set.
    for point in batch.points:
        assert set(point) >= set(envelope.metric_axes)
        assert envelope.contains(point)


def test_suggestions_land_in_the_emptiest_region() -> None:
    rows = [
        {"thickness": 1.0 + i * 0.2, "heat_load": 1.0 + j * 0.2} for i in range(5) for j in range(5)
    ] + [{"thickness": 9.0, "heat_load": 9.0}]
    model, _, envelope = _prepared(rows)

    # The cluster sits in the low corner, so the emptiest region is
    # anywhere far from it — an off-diagonal corner counts, not only the
    # middle.
    (first,) = suggest_points(model, envelope, n=1, seed=2).points
    assert max(first["thickness"], first["heat_load"]) > 4.0


def test_suggestions_name_the_response_they_improve() -> None:
    # "These runs improve *peak temperature*" is actionable; an
    # unattributed list is not.
    rows = two_factor_rows(4, 4)
    model = ResponseModel.scalar("peak_temperature", fit(rows, [r["thickness"] for r in rows]))
    points = resolve_points(training(rows))
    batch = suggest_points(model, build_envelope(points), n=3, seed=1)
    assert batch.response_name == "peak_temperature"


def test_suggest_is_deterministic_for_a_given_seed() -> None:
    rows = two_factor_rows(4, 4)
    model, _, envelope = _prepared(rows)
    assert (
        suggest_points(model, envelope, n=4, seed=9).points
        == suggest_points(model, envelope, n=4, seed=9).points
    )


def test_a_model_free_response_still_produces_usable_suggestions() -> None:
    # It loses ARD weighting, not the feature.
    rows = two_factor_rows(4, 4)
    model, _, envelope = _prepared(rows, kind="gbm")
    batch = suggest_points(model, envelope, n=3, seed=4)

    assert len(batch.points) == 3
    assert all(envelope.contains(p) for p in batch.points)


# --- §5.1 the integer invariant ------------------------------------------


def _integer_setup(present, lo=1, hi=5, **kwargs):
    # Three runs per lattice point, thickness varying within each, so both
    # axes clear varied_columns' two-distinct-values floor and the fit has
    # enough complete rows.
    rows = [
        {"ply_count": float(n), "thickness": 1.0 + n * 0.7 + j * 0.4}
        for n in present
        for j in range(3)
    ]
    desc = descriptor(integer("ply_count", lo, hi), num("thickness", 0.0, 12.0))
    return _prepared(
        rows,
        descriptor=desc,
        overrides={"ply_count": FactorOverride(lo=float(lo), hi=float(hi))},
        **kwargs,
    )


def test_every_suggested_integer_value_is_integral() -> None:
    # Assert no 2.7 escapes.
    model, _, envelope = _integer_setup([1, 2, 5])
    for point in suggest_points(model, envelope, n=4, seed=3).points:
        assert float(point["ply_count"]).is_integer(), point


def test_a_fully_occupied_lattice_yields_zero_suggestions_on_that_axis() -> None:
    # The correct answer is that the axis is finished.
    model, _, envelope = _integer_setup([1, 2, 3, 4, 5])
    batch = suggest_points(model, envelope, n=3, seed=3)
    existing = {1, 2, 3, 4, 5}
    assert all(int(p["ply_count"]) in existing for p in batch.points)


def test_a_repeated_integer_value_is_allowed_beside_a_continuous_axis() -> None:
    """Revised 2026-09-03. This asserted the opposite, and that was the bug.

    A duplicate *run* wastes solver time; a duplicate ply count does not. Two
    points at three plies and different thicknesses are different designs, and
    reaching between two clusters of ply count requires exactly that. Treating
    the lattice key as the point's identity rejected every candidate on a
    cohort whose integer values were all used, and the panel then reported
    there was nothing left to suggest at all.

    What must still hold is that no proposal repeats an existing run *whole*.
    """
    model, points, envelope = _integer_setup([1, 2, 5], lo=1, hi=8)
    batch = suggest_points(model, envelope, n=3, seed=3)

    assert len(batch.points) == 3, "the batch must not be starved by a busy lattice"
    existing = {
        (round(a, 6), round(b, 6))
        for a, b in zip(points.values("ply_count"), points.values("thickness"), strict=True)
    }
    proposed = {
        (round(float(p["ply_count"]), 6), round(float(p["thickness"]), 6)) for p in batch.points
    }
    assert not (existing & proposed), "a whole run was proposed twice"


def test_a_busy_lattice_no_longer_shrinks_a_mixed_design() -> None:
    """The other half of the same correction.

    ``[1, 2]`` out of ``1..3`` used to shrink an eight-run batch to one and
    warn that the axis was finished. With a continuous axis alongside it, the
    axis is not finished — there is a whole thickness range to explore at
    every ply count.
    """
    model, _, envelope = _integer_setup([1, 2], lo=1, hi=3)
    batch = suggest_points(model, envelope, n=8, seed=3)

    assert len(batch.points) == 8
    assert not any(w.code == "lattice_exhausted" for w in batch.warnings)


def test_a_dense_integer_axis_still_snaps_but_is_not_treated_as_exhaustible() -> None:
    model, _, envelope = _integer_setup(
        list(range(1, 12)),
        lo=1,
        hi=1000,
    )
    batch = suggest_points(
        model, envelope, n=5, seed=3, settings=CoverageSettings(max_lattice_cardinality=20)
    )
    assert len(batch.points) == 5
    assert all(float(p["ply_count"]).is_integer() for p in batch.points)
    assert not any(w.code == "lattice_exhausted" for w in batch.warnings)


def test_suggested_integer_values_stay_inside_the_declared_bounds() -> None:
    model, _, envelope = _integer_setup([1, 2, 5], lo=1, hi=5)
    for point in suggest_points(model, envelope, n=3, seed=3).points:
        assert 1 <= int(point["ply_count"]) <= 5


# --- pinned and derived axes ---------------------------------------------


def test_a_pinned_factor_is_emitted_at_its_pinned_value_and_never_varied() -> None:
    rows = [
        {"thickness": r["thickness"], "heat_load": r["heat_load"], "mesh_density": "fine"}
        for r in two_factor_rows(4, 4)
    ]
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] + r["heat_load"] for r in rows]))
    points = resolve_points(training(rows))
    envelope = build_envelope(points)

    for point in suggest_points(model, envelope, n=3, seed=1).points:
        assert point["mesh_density"] == "fine"


def test_no_suggested_point_ever_carries_a_time_coordinate() -> None:
    # Nobody can run a job at a snapshot index; proposing one would be
    # meaningless output (§1.2.1).
    rows = [
        {"thickness": float(t), "time": float(s)} for t in (1.0, 3.0, 5.0, 7.0) for s in range(4)
    ]
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] + r["time"] for r in rows]))
    points = resolve_points(field_training(rows))
    envelope = build_envelope(points)

    batch = suggest_points(model, envelope, n=3, seed=1)
    assert batch.points
    for point in batch.points:
        assert "time" not in point


def test_a_design_with_only_derived_and_pinned_axes_suggests_nothing() -> None:
    rows = [{"time": float(s), "mesh": "fine"} for s in range(8)]
    model = ResponseModel.scalar("y", fit(rows, [float(r["time"]) for r in rows]))
    points = resolve_points(field_training([{"time": float(s)} for s in range(8)]))
    envelope = build_envelope(points)

    batch = suggest_points(model, envelope, n=3, seed=1)
    assert batch.points == ()
    assert any(w.code == "nothing_to_suggest" for w in batch.warnings)


# --- degenerate requests --------------------------------------------------


@pytest.mark.parametrize("n", [0, -1])
def test_a_non_positive_batch_returns_nothing_without_raising(n: int) -> None:
    rows = two_factor_rows(4, 4)
    model, _, envelope = _prepared(rows)
    assert suggest_points(model, envelope, n=n, seed=1).points == ()


def test_an_unfitted_model_suggests_from_geometry_rather_than_raising() -> None:
    rows = two_factor_rows(2, 2)
    model, _, envelope = _prepared(rows)
    assert not model.surrogate.fitted
    batch = suggest_points(model, envelope, n=2, seed=1)
    assert len(batch.points) == 2


# --- helpers --------------------------------------------------------------


def _min_separation(points) -> float:
    """Smallest distance between any two suggested points (raw units)."""
    keys = sorted(set(points[0]) & {"thickness", "heat_load"})
    best = math.inf
    for i, a in enumerate(points):
        for b in points[i + 1 :]:
            d = math.sqrt(sum((float(a[k]) - float(b[k])) ** 2 for k in keys))
            best = min(best, d)
    return best

"""Model-free coverage metrics (ADR-0020 §4, slice 1).

Known designs, known answers. These are the criterion the cold-start path
and every cohort preview depend on — the modal case is a customer with forty
scattered ad-hoc jobs, so the metrics must work exactly when the data is
worst.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from matofx.studies.coverage import (
    build_envelope,
    resolve_points,
)
from matofx.studies.coverage.metrics import (
    centered_l2_discrepancy,
    covering_radius,
    lattice_occupancy,
    maximin_distance,
    nn_distance_distribution,
    trained_span,
)
from tests.studies._coverage_fixtures import descriptor, grid, integer, num, training


def _points_and_envelope(rows, **kwargs):
    resolve_kwargs = {k: kwargs.pop(k) for k in ("descriptor", "factor_spec") if k in kwargs}
    points = resolve_points(training(rows), **resolve_kwargs)
    return points, build_envelope(points, **resolve_kwargs, **kwargs)


def _matrix(rows, **kwargs) -> np.ndarray:
    points, env = _points_and_envelope(rows, **kwargs)
    return env.normalise(points)


# --- maximin distance -----------------------------------------------------


def test_maximin_is_the_smallest_pairwise_distance() -> None:
    x = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    assert maximin_distance(x) == pytest.approx(1.0)


def test_maximin_spots_a_tight_cluster() -> None:
    spread = _matrix(grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0]))
    clustered = _matrix(
        [{"thickness": 1.0 + i * 0.01, "heat_load": 1.0 + i * 0.01} for i in range(9)]
        + [{"thickness": 9.0, "heat_load": 9.0}]
    )
    assert maximin_distance(clustered) < maximin_distance(spread)


def test_maximin_is_invariant_to_factor_ordering() -> None:
    rows = grid([1.0, 3.0, 7.0], [2.0, 4.0, 8.0])
    swapped = [{"heat_load": r["heat_load"], "thickness": r["thickness"]} for r in rows]
    assert maximin_distance(_matrix(rows)) == pytest.approx(maximin_distance(_matrix(swapped)))


def test_maximin_is_invariant_to_unit_scaling_after_normalisation() -> None:
    mm = grid([1.0, 3.0, 7.0], [2.0, 4.0, 8.0])
    metres = [{"thickness": r["thickness"] / 1000, "heat_load": r["heat_load"]} for r in mm]
    assert maximin_distance(_matrix(mm)) == pytest.approx(maximin_distance(_matrix(metres)))


def test_maximin_of_fewer_than_two_points_is_none() -> None:
    assert maximin_distance(np.zeros((1, 2))) is None
    assert maximin_distance(np.zeros((0, 2))) is None


# --- covering radius — the headline sparsity number ----------------------


def test_covering_radius_is_smaller_for_a_grid_than_for_a_corner_cluster() -> None:
    grid_points, grid_env = _points_and_envelope(grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0]))
    corner_rows = [
        {"thickness": 1.0 + i * 0.05, "heat_load": 1.0 + j * 0.05}
        for i in range(3)
        for j in range(3)
    ]
    # Same declared envelope so the two are comparable.
    corner_points, corner_env = _points_and_envelope(
        [*corner_rows, {"thickness": 9.0, "heat_load": 9.0}]
    )
    assert covering_radius(grid_env.normalise(grid_points), grid_env, seed=1) < covering_radius(
        corner_env.normalise(corner_points), corner_env, seed=1
    )


def test_covering_radius_shrinks_monotonically_as_points_are_added() -> None:
    base = grid([1.0, 9.0], [1.0, 9.0])
    radii = []
    for extra in (
        [],
        [{"thickness": 5.0, "heat_load": 5.0}],
        [{"thickness": 5.0, "heat_load": 5.0}, {"thickness": 3.0, "heat_load": 7.0}],
    ):
        points, env = _points_and_envelope([*base, *extra])
        radii.append(covering_radius(env.normalise(points), env, seed=11))
    assert radii[0] >= radii[1] >= radii[2]


def test_covering_radius_is_bounded_by_the_unit_hypercube_diagonal() -> None:
    points, env = _points_and_envelope(grid([1.0, 9.0], [1.0, 9.0]))
    r = covering_radius(env.normalise(points), env, seed=5)
    assert 0.0 <= r <= math.sqrt(2)


def test_covering_radius_uses_a_candidate_sample_not_a_full_grid() -> None:
    # Grid cost explodes past ~4 factors; the candidate count is the knob.
    rows = [{f"x{k}": float(i + k) for k in range(6)} for i in range(12)]
    points, env = _points_and_envelope(rows)
    r = covering_radius(env.normalise(points), env, n_candidates=256, seed=2)
    assert 0.0 <= r <= math.sqrt(6)


def test_covering_radius_is_deterministic_for_a_given_seed() -> None:
    points, env = _points_and_envelope(grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0]))
    x = env.normalise(points)
    assert covering_radius(x, env, seed=42) == covering_radius(x, env, seed=42)


def test_covering_radius_ignores_points_outside_the_declared_envelope() -> None:
    from matofx.studies.coverage import FactorOverride  # noqa: PLC0415

    inside_only = grid([3.0, 5.0], [3.0, 5.0])
    points_a, env_a = _points_and_envelope(
        inside_only,
        overrides={
            "thickness": FactorOverride(lo=3.0, hi=5.0),
            "heat_load": FactorOverride(lo=3.0, hi=5.0),
        },
    )
    with_outliers = [*inside_only, {"thickness": 40.0, "heat_load": 40.0}]
    points_b, env_b = _points_and_envelope(
        with_outliers,
        overrides={
            "thickness": FactorOverride(lo=3.0, hi=5.0),
            "heat_load": FactorOverride(lo=3.0, hi=5.0),
        },
    )
    assert covering_radius(env_a.normalise(points_a), env_a, seed=9) == pytest.approx(
        covering_radius(env_b.normalise(points_b), env_b, seed=9)
    )


# --- discrepancy + nearest-neighbour distribution ------------------------


def test_discrepancy_prefers_a_uniform_design_over_a_clustered_one() -> None:
    uniform = _matrix(grid([1.0, 3.0, 5.0, 7.0, 9.0], [1.0, 3.0, 5.0, 7.0, 9.0]))
    clustered = _matrix(
        [
            {"thickness": 1.0 + i * 0.1, "heat_load": 1.0 + j * 0.1}
            for i in range(5)
            for j in range(5)
        ]
        + [{"thickness": 9.0, "heat_load": 9.0}]
    )
    assert centered_l2_discrepancy(uniform) < centered_l2_discrepancy(clustered)


def test_discrepancy_is_invariant_to_row_order() -> None:
    x = _matrix(grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0]))
    shuffled = x[::-1]
    assert centered_l2_discrepancy(x) == pytest.approx(centered_l2_discrepancy(shuffled))


def test_nn_distances_has_one_entry_per_point() -> None:
    x = _matrix(grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0]))
    distances = nn_distance_distribution(x)
    assert len(distances) == 9
    assert min(distances) == pytest.approx(maximin_distance(x))


def test_nn_distances_of_a_single_point_is_empty() -> None:
    assert nn_distance_distribution(np.zeros((1, 2))) == ()


# --- §3.3 integer lattice occupancy --------------------------------------


def test_a_fully_occupied_lattice_is_completely_covered() -> None:
    # The test that fails under a naive continuous treatment, which would
    # report holes at 1.5 / 2.5 and propose ply count 2.7.
    rows = [{"ply_count": float(n), "thickness": float(n)} for n in (1, 2, 3, 4, 5)]
    points, env = _points_and_envelope(
        rows, descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 9.0))
    )
    occupancy, missing = lattice_occupancy(points, env.by_name("ply_count"))
    assert occupancy == pytest.approx(1.0)
    assert missing == ()


def test_a_partially_occupied_lattice_names_the_missing_values_exactly() -> None:
    rows = [{"ply_count": float(n), "thickness": float(n)} for n in (1, 3, 5)]
    points, env = _points_and_envelope(
        rows, descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 9.0))
    )
    occupancy, missing = lattice_occupancy(points, env.by_name("ply_count"))
    assert occupancy == pytest.approx(3 / 5)
    assert missing == ("2", "4")


def test_lattice_ordering_survives_ply_count_3_is_near_4_and_far_from_12() -> None:
    # Ordered discrete, not unordered levels — the GP's ARD length scales
    # genuinely use that ordering.
    rows = [{"ply_count": float(n), "thickness": 1.0 + n} for n in (3, 4, 12)]
    points, env = _points_and_envelope(
        rows, descriptor=descriptor(integer("ply_count", 1, 12), num("thickness", 0.0, 20.0))
    )
    col = env.column_index("ply_count")
    values = [row[col] for row in env.normalise(points).tolist()]
    near = abs(values[0] - values[1])
    far = abs(values[0] - values[2])
    assert far > near


def test_occupancy_only_counts_lattice_points_inside_the_declared_envelope() -> None:
    from matofx.studies.coverage import FactorOverride  # noqa: PLC0415

    rows = [{"ply_count": float(n), "thickness": float(n)} for n in (1, 2, 3, 4, 5)]
    points = resolve_points(
        training(rows),
        descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 9.0)),
    )
    env = build_envelope(
        points,
        descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 9.0)),
        overrides={"ply_count": FactorOverride(lo=2.0, hi=4.0)},
    )
    occupancy, missing = lattice_occupancy(points, env.by_name("ply_count"))
    assert occupancy == pytest.approx(1.0)  # 2, 3, 4 all present
    assert missing == ()


# --- §4.1 trained span for derived axes ----------------------------------


def test_trained_span_reports_observed_min_max_against_the_declared_bounds() -> None:
    from matofx.studies.coverage import FactorOverride  # noqa: PLC0415

    rows = [{"soak_time": float(s), "thickness": 1.0 + s} for s in range(0, 7)]
    points = resolve_points(training(rows))
    env = build_envelope(points, overrides={"soak_time": FactorOverride(lo=0.0, hi=12.0)})

    fraction, (lo, hi) = trained_span(points, env.by_name("soak_time"))
    assert (lo, hi) == (0.0, 6.0)
    assert fraction == pytest.approx(0.5)


def test_trained_span_of_a_fully_spanned_axis_is_one() -> None:
    rows = [{"soak_time": float(s), "thickness": 1.0 + s} for s in range(0, 7)]
    points, env = _points_and_envelope(rows)
    fraction, _ = trained_span(points, env.by_name("soak_time"))
    assert fraction == pytest.approx(1.0)


# --- degenerate inputs ----------------------------------------------------


def test_metrics_return_none_rather_than_raising_on_an_empty_matrix() -> None:
    empty = np.zeros((0, 2))
    assert maximin_distance(empty) is None
    assert centered_l2_discrepancy(empty) is None
    assert nn_distance_distribution(empty) == ()


def test_metrics_tolerate_a_zero_width_factor_without_dividing_by_zero() -> None:
    # A factor whose declared lo == hi should not produce nan/inf.
    from matofx.studies.coverage import FactorOverride  # noqa: PLC0415

    rows = grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0])
    points = resolve_points(training(rows))
    env = build_envelope(points, overrides={"heat_load": FactorOverride(lo=5.0, hi=5.0)})
    x = env.normalise(points)
    assert np.isfinite(x).all()


# --- dimensionality: the two defects the real NAFEMS archive exposed -------


def _lhs_like(n: int, d: int, seed: int) -> np.ndarray:
    """A stratified sample — a well-spread design of ``n`` points in ``d`` dims."""
    rng = np.random.default_rng(seed)
    return np.column_stack([(rng.permutation(n) + rng.random(n)) / n for _ in range(d)])


@pytest.mark.parametrize("n_axes", [2, 4, 8, 12])
def test_a_well_spread_design_scores_well_at_every_dimension(n_axes: int) -> None:
    """The verdict must track the design, not the number of factors.

    Referencing against the hypercube half-diagonal made a **perfect** 254-point
    Latin hypercube read `poor` from eight factors up — the covering radius
    grows with dimension far faster than `sqrt(d)`, so the score collapsed
    however good the design was. A light that is always red is one users learn
    to ignore.
    """
    from matofx.studies.coverage import analyse_design  # noqa: PLC0415

    names = [f"x{i}" for i in range(n_axes)]
    x = _lhs_like(200, n_axes, seed=1)
    rows = [{n: float(x[i, j]) for j, n in enumerate(names)} for i in range(200)]
    points = resolve_points(training(rows))
    design = analyse_design(points, build_envelope(points))

    assert design.verdict == "good", (n_axes, design.coverage_score)
    assert design.coverage_score > 0.7


@pytest.mark.parametrize("n_axes", [2, 4, 8, 12])
def test_a_clustered_design_still_scores_badly_at_every_dimension(n_axes: int) -> None:
    # The other half of the same guarantee: making the score dimension-fair
    # must not make it uncritical.
    from matofx.studies.coverage import analyse_design  # noqa: PLC0415

    rng = np.random.default_rng(3)
    names = [f"x{i}" for i in range(n_axes)]
    x = np.vstack([rng.random((199, n_axes)) * 0.15, np.ones((1, n_axes)) * 0.99])
    rows = [{n: float(x[i, j]) for j, n in enumerate(names)} for i in range(200)]
    points = resolve_points(training(rows))
    design = analyse_design(points, build_envelope(points))

    assert design.verdict == "poor", (n_axes, design.coverage_score)


def test_the_reference_radius_is_what_this_many_points_could_achieve() -> None:
    from matofx.studies.coverage.metrics import reference_radius  # noqa: PLC0415

    points, env = _points_and_envelope(grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0]))
    _ = points
    sparse = reference_radius(env, 4, n_candidates=1024, seed=1)
    dense = reference_radius(env, 400, n_candidates=1024, seed=1)
    # More runs must be able to cover more tightly, or it is not a yardstick.
    assert dense < sparse


def test_candidate_resolution_says_how_finely_the_space_is_probed() -> None:
    from matofx.studies.coverage.metrics import candidate_resolution  # noqa: PLC0415

    # 4096 candidates is 64 per axis in two dimensions and two in twelve —
    # the same number means very different things.
    assert candidate_resolution(4096, 2) == pytest.approx(64.0)
    assert candidate_resolution(4096, 12) == pytest.approx(2.0, abs=0.05)


def test_a_high_dimensional_report_says_its_headline_is_an_estimate() -> None:
    from matofx.studies.coverage import analyse_design  # noqa: PLC0415

    rng = np.random.default_rng(5)
    names = [f"x{i}" for i in range(12)]
    rows = [{n: float(v) for n, v in zip(names, rng.random(12), strict=True)} for _ in range(60)]
    points = resolve_points(training(rows))
    design = analyse_design(points, build_envelope(points))

    assert any(w.code == "coarse_estimate" for w in design.warnings)


def test_a_low_dimensional_report_makes_no_such_apology() -> None:
    from matofx.studies.coverage import analyse_design  # noqa: PLC0415

    points = resolve_points(training(grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0])))
    design = analyse_design(points, build_envelope(points))
    assert not any(w.code == "coarse_estimate" for w in design.warnings)

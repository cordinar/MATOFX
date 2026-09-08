"""Model-free suggested runs, from the design pass alone (ADR-0020 §5, slice 4).

§11.5 puts the "suggest runs to fill the gaps" button on the **cohort preview**
panel — where, by construction, no model has been fitted and no response has
been chosen (§2.1). ``suggest_points`` needs a ``ResponseModel``; this path
needs none, and works from the geometry the design pass already measured.

It loses ARD weighting, which is exactly the trade §2.3 describes for every
model-free case. It does not lose the integer invariant, the derived-axis
skip, or the pinned passthrough.
"""

from __future__ import annotations

import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import (
    FactorOverride,
    build_envelope,
    resolve_points,
    suggest_design_points,
)
from tests.studies._coverage_fixtures import (
    descriptor,
    field_training,
    integer,
    num,
    training,
    two_factor_rows,
)


def _prepared(rows, **kwargs):
    resolve_kwargs = {k: kwargs.pop(k) for k in ("descriptor", "factor_spec") if k in kwargs}
    points = resolve_points(training(rows), **resolve_kwargs)
    return points, build_envelope(points, **resolve_kwargs, **kwargs)


# --- it works with no model at all ---------------------------------------


def test_suggestions_need_no_fitted_model() -> None:
    points, envelope = _prepared(two_factor_rows(4, 4))
    batch = suggest_design_points(points, envelope, n=4, seed=1)

    assert len(batch.points) == 4
    assert all(envelope.contains(p) for p in batch.points)


def test_suggestions_land_in_the_empty_region() -> None:
    rows = [
        {"thickness": 1.0 + i * 0.2, "heat_load": 1.0 + j * 0.2} for i in range(5) for j in range(5)
    ] + [{"thickness": 9.0, "heat_load": 9.0}]
    points, envelope = _prepared(rows)

    (first,) = suggest_design_points(points, envelope, n=1, seed=2).points
    assert max(first["thickness"], first["heat_load"]) > 4.0


def test_a_batch_spreads_rather_than_crowding_one_hole() -> None:
    rows = [
        {"thickness": 1.0 + i * 0.15, "heat_load": 1.0 + j * 0.15}
        for i in range(4)
        for j in range(4)
    ]
    points, envelope = _prepared([*rows, {"thickness": 9.0, "heat_load": 9.0}])
    batch = suggest_design_points(points, envelope, n=5, seed=3)

    separations = [
        ((a["thickness"] - b["thickness"]) ** 2 + (a["heat_load"] - b["heat_load"]) ** 2) ** 0.5
        for i, a in enumerate(batch.points)
        for b in batch.points[i + 1 :]
    ]
    assert min(separations) > 0.5


def test_suggest_is_deterministic_for_a_given_seed() -> None:
    points, envelope = _prepared(two_factor_rows(4, 4))
    assert (
        suggest_design_points(points, envelope, n=4, seed=9).points
        == suggest_design_points(points, envelope, n=4, seed=9).points
    )


def test_the_batch_is_attributed_to_the_design_not_a_response() -> None:
    # "These runs improve *peak temperature*" is a claim this path cannot
    # make — no response has been chosen — so it must not pretend to.
    points, envelope = _prepared(two_factor_rows(4, 4))
    assert suggest_design_points(points, envelope, n=2, seed=1).response_name == ""


# --- §5.1 the invariants still hold --------------------------------------


def _integer_setup(present, lo=1, hi=5):
    rows = [
        {"ply_count": float(n), "thickness": 1.0 + n * 0.7 + j * 0.4}
        for n in present
        for j in range(3)
    ]
    return _prepared(
        rows,
        descriptor=descriptor(integer("ply_count", lo, hi), num("thickness", 0.0, 12.0)),
        overrides={"ply_count": FactorOverride(lo=float(lo), hi=float(hi))},
    )


def test_every_suggested_integer_value_is_integral() -> None:
    points, envelope = _integer_setup([1, 2, 5])
    for point in suggest_design_points(points, envelope, n=4, seed=3).points:
        assert float(point["ply_count"]).is_integer(), point


def test_already_run_lattice_points_are_never_proposed() -> None:
    points, envelope = _integer_setup([1, 2, 5], lo=1, hi=8)
    already = {int(v) for v in points.values("ply_count")}
    batch = suggest_design_points(points, envelope, n=3, seed=3)
    assert not (already & {int(p["ply_count"]) for p in batch.points})


def test_a_batch_larger_than_the_remaining_lattice_shrinks_with_a_warning() -> None:
    points, envelope = _integer_setup([1, 2], lo=1, hi=3)
    batch = suggest_design_points(points, envelope, n=8, seed=3)

    assert len(batch.points) <= 1
    assert any(w.code == "lattice_exhausted" for w in batch.warnings)


def test_pinned_factors_ride_along_at_their_pinned_value() -> None:
    rows = [
        {"thickness": r["thickness"], "heat_load": r["heat_load"], "mesh_density": "fine"}
        for r in two_factor_rows(4, 4)
    ]
    points, envelope = _prepared(rows)
    for point in suggest_design_points(points, envelope, n=3, seed=1).points:
        assert point["mesh_density"] == "fine"


def test_no_suggested_point_carries_a_snapshot_index() -> None:
    rows = [
        {"thickness": float(t), "time": float(s)} for t in (1.0, 3.0, 5.0, 7.0) for s in range(4)
    ]
    points = resolve_points(field_training(rows))
    envelope = build_envelope(points)

    batch = suggest_design_points(points, envelope, n=3, seed=1)
    assert batch.points
    assert all("time" not in p for p in batch.points)


def test_a_design_with_nothing_to_vary_suggests_nothing() -> None:
    points = resolve_points(field_training([{"time": float(s)} for s in range(8)]))
    envelope = build_envelope(points)

    batch = suggest_design_points(points, envelope, n=3, seed=1)
    assert batch.points == ()
    assert any(w.code == "nothing_to_suggest" for w in batch.warnings)


@pytest.mark.parametrize("n", [0, -1])
def test_a_non_positive_batch_returns_nothing(n: int) -> None:
    points, envelope = _prepared(two_factor_rows(4, 4))
    assert suggest_design_points(points, envelope, n=n, seed=1).points == ()


def test_the_candidate_count_comes_from_config() -> None:
    points, envelope = _prepared(two_factor_rows(4, 4))
    batch = suggest_design_points(
        points, envelope, n=3, seed=1, settings=CoverageSettings(n_candidates=512)
    )
    assert len(batch.points) == 3


def test_suggestions_respect_a_narrowed_envelope() -> None:
    rows = [{"thickness": float(t), "heat_load": float(t)} for t in range(1, 10)]
    points = resolve_points(training(rows))
    envelope = build_envelope(points, overrides={"thickness": FactorOverride(lo=3.0, hi=6.0)})
    for point in suggest_design_points(points, envelope, n=3, seed=1).points:
        assert 3.0 <= point["thickness"] <= 6.0


# --- the criterion: minimax, not maximin ---------------------------------


def _normalised(envelope, batch):
    import numpy as np  # noqa: PLC0415

    axes = list(envelope.metric_axes)
    lo = np.array([envelope.by_name(a).lo for a in axes])
    hi = np.array([envelope.by_name(a).hi for a in axes])
    raw = np.array([[float(p[a]) for a in axes] for p in batch.points])
    return (raw - lo) / np.maximum(hi - lo, 1e-12)


@pytest.mark.parametrize("n_axes", [2, 6, 12])
def test_suggestions_beat_a_random_batch_of_the_same_size(n_axes: int) -> None:
    """The control that catches a plausible-but-wrong criterion.

    Maximin — "take the point furthest from everything so far" — is the
    obvious walk and the wrong one: the number this feature reports is the
    **covering radius**, a minimax quantity. In high dimensions maximin drives
    every pick onto a corner, and the earlier implementation lost to *every*
    one of 200 random batches at twelve factors while winning at two. The
    greedy now minimises the resulting radius directly.
    """
    import numpy as np  # noqa: PLC0415

    from matofx.studies.coverage.metrics import covering_radius  # noqa: PLC0415

    rng = np.random.default_rng(0)
    names = [f"x{i}" for i in range(n_axes)]
    x = np.vstack([rng.random((60, n_axes)) * 0.25, np.ones((1, n_axes)) * 0.99])
    rows = [{n: float(x[i, j]) for j, n in enumerate(names)} for i in range(len(x))]
    points = resolve_points(training(rows))
    envelope = build_envelope(points)
    existing = envelope.normalise(points)

    proposed = _normalised(envelope, suggest_design_points(points, envelope, n=8, seed=20_020))
    # Measured on an *independent* candidate sample: filling the holes of the
    # set you planned against would be marking your own homework.
    ours = covering_radius(np.vstack([existing, proposed]), envelope, n_candidates=4096, seed=777)
    beaten = sum(
        1
        for _ in range(40)
        if covering_radius(
            np.vstack([existing, rng.random((8, n_axes))]),
            envelope,
            n_candidates=4096,
            seed=777,
        )
        > ours
    )
    assert beaten >= 36, (n_axes, beaten)


def test_suggestions_reduce_the_covering_radius_they_are_measured_by() -> None:
    import numpy as np  # noqa: PLC0415

    from matofx.studies.coverage.metrics import covering_radius  # noqa: PLC0415

    rows = [
        {"thickness": 1.0 + i * 0.2, "heat_load": 1.0 + j * 0.2} for i in range(5) for j in range(5)
    ] + [{"thickness": 9.0, "heat_load": 9.0}]
    points = resolve_points(training(rows))
    envelope = build_envelope(points)
    existing = envelope.normalise(points)

    before = covering_radius(existing, envelope, n_candidates=4096, seed=20_020)
    proposed = _normalised(envelope, suggest_design_points(points, envelope, n=6, seed=20_020))
    after = covering_radius(
        np.vstack([existing, proposed]), envelope, n_candidates=4096, seed=20_020
    )
    assert after < before

"""The declared design envelope (ADR-0020 §3, slice 1).

Factor-kind resolution across all six vary kinds, the integer lattice, the
log-scale rule, and the normalise / contains / sample contract. The envelope
is the object the guard later enforces, so its bounds are declared — never
inferred from a bounding box and never silently widened.
"""

from __future__ import annotations

import math

import pytest

from matofx.studies.coverage import (
    FactorOverride,
    build_envelope,
    resolve_points,
)
from tests.studies._coverage_fixtures import (
    boolean,
    descriptor,
    integer,
    num,
    select,
    spec,
    training,
)


def _envelope(rows, **kwargs):
    """``build_envelope(resolve_points(training(rows)), **kwargs)``."""
    resolve_kwargs = {k: kwargs.pop(k) for k in ("descriptor", "factor_spec") if k in kwargs}
    points = resolve_points(training(rows), **resolve_kwargs)
    return build_envelope(points, **resolve_kwargs, **kwargs)


# --- §3 bounds default to the observed spread, then are user-editable ----


def test_bounds_default_to_the_training_sets_observed_spread() -> None:
    env = _envelope([{"thickness": t, "heat_load": t * 3} for t in (1.0, 4.0, 7.5)])
    thickness = env.by_name("thickness")
    assert (thickness.lo, thickness.hi) == (1.0, 7.5)


def test_a_user_override_narrows_the_envelope() -> None:
    # The declared-envelope decision: narrowing is the expected action.
    env = _envelope(
        [{"thickness": float(t), "heat_load": float(t)} for t in range(1, 9)],
        overrides={"thickness": FactorOverride(lo=2.0, hi=6.0)},
    )
    assert (env.by_name("thickness").lo, env.by_name("thickness").hi) == (2.0, 6.0)


def test_a_user_override_may_also_widen_beyond_the_observed_spread() -> None:
    env = _envelope(
        [{"thickness": float(t), "heat_load": float(t)} for t in range(1, 5)],
        overrides={"thickness": FactorOverride(lo=0.0, hi=20.0)},
    )
    assert (env.by_name("thickness").lo, env.by_name("thickness").hi) == (0.0, 20.0)


def test_an_inverted_override_is_rejected_with_a_plain_english_message() -> None:
    from matofx.studies.coverage import CoverageError  # noqa: PLC0415

    with pytest.raises(CoverageError, match=r"(?i)lowest.*highest|greater than"):
        _envelope(
            [{"thickness": float(t), "heat_load": float(t)} for t in range(1, 5)],
            overrides={"thickness": FactorOverride(lo=6.0, hi=2.0)},
        )


# --- §3.1 factor-kind resolution across the six vary kinds ---------------


def test_range_vary_on_a_continuous_numeric_is_continuous_with_declared_bounds() -> None:
    fs = spec(
        {
            "thickness": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "range", "min": 0.5, "max": 9.5},
            },
            "heat_load": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "range", "min": 1.0, "max": 2.0},
            },
        }
    )
    env = _envelope(
        [{"thickness": t, "heat_load": 1.0 + t / 10} for t in (1.0, 2.0, 3.0)],
        factor_spec=fs,
    )
    thickness = env.by_name("thickness")
    assert thickness.kind == "continuous"
    # The *declared* domain wins over the observed spread.
    assert (thickness.lo, thickness.hi) == (0.5, 9.5)


def test_list_vary_on_a_numeric_is_continuous_not_bucketed_into_levels() -> None:
    # A sparse continuous sample is not four balanced levels (§3.4's warning
    # applies to any numeric domain, not just run-table columns).
    fs = spec(
        {
            "thickness": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "list", "values": [1.0, 2.0, 3.5, 4.0]},
            },
            "heat_load": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "range", "min": 1.0, "max": 2.0},
            },
        }
    )
    env = _envelope(
        [{"thickness": t, "heat_load": 1.5} for t in (1.0, 2.0, 3.5, 4.0)], factor_spec=fs
    )
    assert env.by_name("thickness").kind == "continuous"
    assert env.by_name("thickness").levels is None


def test_subset_vary_on_a_select_is_categorical_with_the_declared_options() -> None:
    fs = spec(
        {
            "mesh_density": {
                "role": "vary",
                "type": "select",
                "vary": {"kind": "subset", "options": ["coarse", "medium", "fine"]},
            },
            "thickness": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "range", "min": 1.0, "max": 4.0},
            },
        }
    )
    env = _envelope(
        [
            {"mesh_density": "coarse", "thickness": 1.0},
            {"mesh_density": "fine", "thickness": 2.0},
        ],
        factor_spec=fs,
    )
    axis = env.by_name("mesh_density")
    assert axis.kind == "categorical"
    # Declared options, including the level nobody happened to run.
    assert axis.levels == ("coarse", "medium", "fine")


def test_a_varied_bool_is_categorical_over_two_levels() -> None:
    fs = spec(
        {
            "use_radiation": {"role": "vary", "type": "bool"},
            "thickness": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "range", "min": 1.0, "max": 4.0},
            },
        }
    )
    env = _envelope(
        [
            {"use_radiation": True, "thickness": 1.0},
            {"use_radiation": False, "thickness": 2.0},
        ],
        factor_spec=fs,
    )
    axis = env.by_name("use_radiation")
    assert axis.kind == "categorical"
    assert set(axis.levels or ()) == {"True", "False"}


def test_file_list_vary_is_categorical_over_the_cas_hashes() -> None:
    fs = spec(
        {
            "geometry_file": {
                "role": "vary",
                "type": "file",
                "vary": {"kind": "file_list", "cas_hashes": ["aaa", "bbb"]},
            },
            "thickness": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "range", "min": 1.0, "max": 4.0},
            },
        }
    )
    env = _envelope(
        [
            {"geometry_file": "aaa", "thickness": 1.0},
            {"geometry_file": "bbb", "thickness": 2.0},
        ],
        factor_spec=fs,
    )
    axis = env.by_name("geometry_file")
    assert axis.kind == "categorical"
    assert axis.levels == ("aaa", "bbb")


# --- §3.4 ColumnVary resolves by the underlying factor type --------------


def test_column_vary_on_a_select_is_categorical() -> None:
    fs = spec(
        {
            "mesh_density": {"role": "vary", "type": "select", "vary": {"kind": "column"}},
            "thickness": {"role": "vary", "type": "numeric", "vary": {"kind": "column"}},
        }
    )
    env = _envelope(
        [
            {"mesh_density": "coarse", "thickness": 1.0},
            {"mesh_density": "fine", "thickness": 2.0},
            {"mesh_density": "fine", "thickness": 3.5},
        ],
        factor_spec=fs,
    )
    axis = env.by_name("mesh_density")
    assert axis.kind == "categorical"
    # No declared domain — recovered from observed values.
    assert set(axis.levels or ()) == {"coarse", "fine"}


def test_column_vary_on_a_continuous_numeric_is_not_bucketed_into_levels() -> None:
    # A hand-built run table with 1.0 / 2.0 / 3.5 / 4.0 is a sparse
    # continuous sample; calling it four balanced levels would report perfect
    # coverage on an axis that is barely sampled.
    fs = spec(
        {
            "thickness": {"role": "vary", "type": "numeric", "vary": {"kind": "column"}},
            "heat_load": {"role": "vary", "type": "numeric", "vary": {"kind": "column"}},
        }
    )
    env = _envelope(
        [{"thickness": t, "heat_load": 10.0 + t} for t in (1.0, 2.0, 3.5, 4.0)], factor_spec=fs
    )
    axis = env.by_name("thickness")
    assert axis.kind == "continuous"
    assert axis.levels is None


def test_column_vary_on_a_descriptor_integer_is_an_ordered_lattice() -> None:
    fs = spec(
        {
            "ply_count": {"role": "vary", "type": "numeric", "vary": {"kind": "column"}},
            "thickness": {"role": "vary", "type": "numeric", "vary": {"kind": "column"}},
        }
    )
    env = _envelope(
        [{"ply_count": float(n), "thickness": float(n) / 2} for n in (2, 4, 6)],
        factor_spec=fs,
        descriptor=descriptor(integer("ply_count", 1, 8), num("thickness", 0.0, 5.0)),
    )
    axis = env.by_name("ply_count")
    assert axis.kind == "integer"
    # A run-table column has no declared domain (§3.4), so the bounds come from
    # the observed values — not from the descriptor's [1, 8], which is what the
    # *tool* accepts rather than what this design claims to cover.
    assert (axis.lo, axis.hi) == (2.0, 6.0)
    assert env.lattice("ply_count") == (2, 3, 4, 5, 6)


def test_column_vary_on_a_bool_is_categorical() -> None:
    fs = spec(
        {
            "use_radiation": {"role": "vary", "type": "bool", "vary": {"kind": "column"}},
            "thickness": {"role": "vary", "type": "numeric", "vary": {"kind": "column"}},
        }
    )
    env = _envelope(
        [
            {"use_radiation": True, "thickness": 1.0},
            {"use_radiation": False, "thickness": 2.0},
        ],
        factor_spec=fs,
    )
    assert env.by_name("use_radiation").kind == "categorical"


# --- §3.2 log-scaled axes -------------------------------------------------


def test_loguniform_is_inferred_as_a_log_scaled_axis() -> None:
    fs = spec(
        {
            "conductivity": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "distribution", "dist": "loguniform", "low": 1.0, "high": 10000.0},
            },
            "thickness": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "range", "min": 1.0, "max": 4.0},
            },
        }
    )
    env = _envelope(
        [{"conductivity": c, "thickness": 2.0} for c in (1.0, 10.0, 100.0)], factor_spec=fs
    )
    assert env.by_name("conductivity").scale == "log"


def test_a_normal_distribution_stays_linear() -> None:
    # Unbounded in principle, but the envelope supplies real bounds and a
    # linear measure over those bounds is correct.
    fs = spec(
        {
            "conductivity": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "distribution", "dist": "normal", "mean": 5.0, "std": 1.0},
            },
            "thickness": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "range", "min": 1.0, "max": 4.0},
            },
        }
    )
    env = _envelope(
        [{"conductivity": c, "thickness": 2.0} for c in (4.0, 5.0, 6.0)], factor_spec=fs
    )
    assert env.by_name("conductivity").scale == "linear"


def test_an_adhoc_factor_defaults_to_linear_and_honours_a_user_override() -> None:
    # An archive of ad-hoc runs carries no record of how someone chose values.
    rows = [{"conductivity": c, "thickness": 2.0 + c} for c in (1.0, 10.0, 100.0, 1000.0)]
    assert _envelope(rows).by_name("conductivity").scale == "linear"

    overridden = _envelope(rows, overrides={"conductivity": FactorOverride(scale="log")})
    assert overridden.by_name("conductivity").scale == "log"


def test_log_normalisation_maps_decades_evenly() -> None:
    rows = [{"conductivity": c, "thickness": 2.0 + c} for c in (1.0, 10.0, 100.0, 1000.0, 10000.0)]
    points = resolve_points(training(rows))
    env = build_envelope(points, overrides={"conductivity": FactorOverride(scale="log")})

    col = env.column_index("conductivity")
    values = sorted(row[col] for row in env.normalise(points).tolist())
    assert values == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])


def test_a_log_scale_override_is_refused_for_a_non_positive_range() -> None:
    from matofx.studies.coverage import CoverageError  # noqa: PLC0415

    rows = [{"offset": v, "thickness": 1.0 + v} for v in (-5.0, 0.0, 5.0)]
    with pytest.raises(CoverageError, match=r"(?i)greater than zero|positive"):
        _envelope(rows, overrides={"offset": FactorOverride(scale="log")})


# --- normalise / contains -------------------------------------------------


def test_normalise_puts_the_declared_bounds_at_zero_and_one() -> None:
    rows = [{"thickness": t, "heat_load": h} for t, h in ((1.0, 10.0), (5.0, 30.0))]
    points = resolve_points(training(rows))
    env = build_envelope(points)

    matrix = env.normalise(points).tolist()
    assert matrix[0] == pytest.approx([0.0, 0.0])
    assert matrix[1] == pytest.approx([1.0, 1.0])


def test_normalise_excludes_pinned_and_categorical_axes() -> None:
    rows = [
        {"thickness": 1.0, "heat_load": 10.0, "material": "cfrp", "mesh": "fine"},
        {"thickness": 5.0, "heat_load": 30.0, "material": "gfrp", "mesh": "fine"},
    ]
    points = resolve_points(training(rows))
    env = build_envelope(points)

    assert set(env.metric_axes) == {"thickness", "heat_load"}
    assert env.normalise(points).shape == (2, 2)


def test_normalisation_makes_distance_unit_free() -> None:
    # thickness in mm and load in N must contribute comparably.
    mm = [{"thickness": t, "heat_load": h} for t, h in ((1.0, 10.0), (2.0, 20.0), (3.0, 30.0))]
    metres = [
        {"thickness": t / 1000.0, "heat_load": h}
        for t, h in ((1.0, 10.0), (2.0, 20.0), (3.0, 30.0))
    ]
    a_points = resolve_points(training(mm))
    b_points = resolve_points(training(metres))
    a = build_envelope(a_points).normalise(a_points).ravel().tolist()
    b = build_envelope(b_points).normalise(b_points).ravel().tolist()
    assert a == pytest.approx(b)


def test_contains_is_inclusive_of_the_declared_bounds() -> None:
    env = _envelope([{"thickness": 1.0, "heat_load": 1.0}, {"thickness": 5.0, "heat_load": 9.0}])
    assert env.contains({"thickness": 1.0, "heat_load": 1.0})
    assert env.contains({"thickness": 5.0, "heat_load": 9.0})
    assert not env.contains({"thickness": 5.001, "heat_load": 9.0})


def test_contains_rejects_an_unknown_categorical_level() -> None:
    rows = [
        {"thickness": 1.0, "material": "cfrp"},
        {"thickness": 2.0, "material": "gfrp"},
        {"thickness": 3.0, "material": "cfrp"},
    ]
    env = build_envelope(
        resolve_points(training(rows)),
    )
    assert env.contains({"thickness": 2.0, "material": "gfrp"})
    assert not env.contains({"thickness": 2.0, "material": "titanium"})


def test_contains_refuses_a_point_missing_a_factor_rather_than_guessing() -> None:
    env = _envelope([{"thickness": 1.0, "heat_load": 1.0}, {"thickness": 5.0, "heat_load": 9.0}])
    assert not env.contains({"thickness": 2.0})


# --- sample() honours the lattice (§3.3 / §5.1) --------------------------


def test_sample_emits_integral_values_on_an_integer_axis() -> None:
    # Candidate generation, not just suggestion emission: a covering radius
    # measured against 2.7 ply counts would report a hole nothing can fill.
    rows = [{"ply_count": float(n), "thickness": float(n) / 2} for n in (1, 3, 5)]
    points = resolve_points(
        training(rows),
        descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 5.0)),
    )
    env = build_envelope(points)

    col = env.column_index("ply_count")
    lo, hi = env.by_name("ply_count").lo, env.by_name("ply_count").hi
    for row in env.sample(64, seed=7).tolist():
        raw = lo + row[col] * (hi - lo)
        assert math.isclose(raw, round(raw), abs_tol=1e-9), raw


def test_sample_is_deterministic_for_a_given_seed() -> None:
    env = _envelope([{"thickness": 1.0, "heat_load": 1.0}, {"thickness": 5.0, "heat_load": 9.0}])
    assert env.sample(32, seed=3).tolist() == env.sample(32, seed=3).tolist()


def test_lattice_names_every_attainable_value() -> None:
    rows = [{"ply_count": float(n), "thickness": float(n)} for n in (1, 3, 5)]
    points = resolve_points(
        training(rows),
        descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 9.0)),
    )
    env = build_envelope(points)
    assert env.lattice("ply_count") == (1, 2, 3, 4, 5)


def test_lattice_is_none_for_a_continuous_axis() -> None:
    env = _envelope([{"thickness": 1.0, "heat_load": 1.0}, {"thickness": 5.0, "heat_load": 9.0}])
    assert env.lattice("thickness") is None


def test_by_name_raises_for_an_unknown_factor() -> None:
    from matofx.studies.coverage import CoverageError  # noqa: PLC0415

    env = _envelope([{"thickness": 1.0, "heat_load": 1.0}, {"thickness": 5.0, "heat_load": 9.0}])
    with pytest.raises(CoverageError):
        env.by_name("no_such_input")


def test_the_envelope_factor_order_is_stable() -> None:
    rows = [{"z": 1.0, "a": 2.0, "m": 3.0}, {"z": 4.0, "a": 5.0, "m": 6.0}]
    first = _envelope(rows)
    second = _envelope(list(reversed(rows)))
    assert [f.name for f in first.factors] == [f.name for f in second.factors]


def test_select_options_from_the_descriptor_include_unrun_levels() -> None:
    # 40 CFRP / 2 GFRP is the silent failure; a level nobody ran at all is
    # the same disease one step further on, so the descriptor's options are
    # the domain even when the archive never touched them.
    rows = [{"thickness": float(i), "material": "cfrp"} for i in range(1, 4)]
    rows.append({"thickness": 4.0, "material": "gfrp"})
    points = resolve_points(
        training(rows),
        descriptor=descriptor(
            num("thickness", 0.0, 9.0), select("material", "cfrp", "gfrp", "titanium")
        ),
    )
    env = build_envelope(points)
    assert env.by_name("material").levels == ("cfrp", "gfrp", "titanium")


def test_a_boolean_descriptor_input_carries_both_levels_even_if_one_was_never_run() -> None:
    rows = [{"thickness": float(i), "use_radiation": True} for i in range(1, 4)]
    rows.append({"thickness": 4.0, "use_radiation": False})
    points = resolve_points(
        training(rows), descriptor=descriptor(num("thickness", 0.0, 9.0), boolean("use_radiation"))
    )
    assert set(build_envelope(points).by_name("use_radiation").levels or ()) == {"True", "False"}

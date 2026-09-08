"""Point-set resolution for design-space coverage (ADR-0020 §1, slice 1).

The whole feature rests on answering *which points, in which space* — get it
wrong and coverage is overstated, which is the exact failure ADR-0020 exists
to prevent. Pure unit tests: no DB, no fitted model.
"""

from __future__ import annotations

import pytest

from matofx.studies.coverage import (
    FactorOverride,
    analyse_design,
    build_envelope,
    resolve_points,
)
from matofx.studies.surrogate.ingest import TIME_FACTOR
from tests.studies._coverage_fixtures import (
    boolean,
    descriptor,
    field_training,
    integer,
    num,
    select,
    training,
)

# --- §1.1 the points are the training set, not the cohort ----------------


def test_measures_the_training_set_not_cohort_membership() -> None:
    # A cohort of 100 that trained on 62: 30 dropped by the ADR-0017 validity
    # gate, 8 by the user's FactorFilter. Measuring 100 would overstate
    # coverage by exactly the runs the platform judged unfit to learn from.
    td = training(
        [{"thickness": 1.0 + i * 0.1, "heat_load": 5.0 + i} for i in range(62)],
        n_excluded_invalid=30,
        n_excluded_filtered=8,
        excluded_reason_counts={"above_max": 27, "non_finite": 3},
    )
    points = resolve_points(td)

    assert points.n_points == 62
    assert points.n_excluded_invalid == 30
    assert points.n_excluded_filtered == 8
    assert points.excluded_reason_counts == {"above_max": 27, "non_finite": 3}


def test_exclusion_counts_reach_the_report_so_62_of_100_is_never_a_surprise() -> None:
    td = training(
        [{"thickness": float(i), "heat_load": float(10 - i)} for i in range(10)],
        n_excluded_invalid=4,
        n_excluded_filtered=1,
    )
    points = resolve_points(td)
    design = analyse_design(points, build_envelope(points))

    assert design.diagnostics.n_points == 10
    assert design.diagnostics.n_excluded_invalid == 4
    assert design.diagnostics.n_excluded_filtered == 1


# --- §1.2 factors are inferred, not declared -----------------------------


def test_factors_inferred_for_a_cohort_with_no_factor_spec() -> None:
    # An ad-hoc cohort has no FactorSpec — nobody authored a study.
    td = training(
        [
            {"thickness": 1.0, "heat_load": 10.0, "mesh_density": "fine"},
            {"thickness": 2.0, "heat_load": 20.0, "mesh_density": "fine"},
            {"thickness": 3.0, "heat_load": 15.0, "mesh_density": "coarse"},
            {"thickness": 4.0, "heat_load": 25.0, "mesh_density": "coarse"},
        ]
    )
    points = resolve_points(td)
    assert set(points.factor_names) == {"thickness", "heat_load", "mesh_density"}


def test_an_input_with_one_distinct_value_is_not_a_factor() -> None:
    # varied_columns' rule: >= 2 distinct non-null values to be an axis.
    td = training(
        [
            {"thickness": 1.0, "ambient_temp": 20.0},
            {"thickness": 2.0, "ambient_temp": 20.0},
            {"thickness": 3.0, "ambient_temp": 20.0},
        ]
    )
    points = resolve_points(td)
    assert points.factor_names == ("thickness",)
    assert points.constants == {"ambient_temp": 20.0}


def test_a_constant_input_becomes_a_pinned_factor_in_the_envelope() -> None:
    td = training(
        [
            {"thickness": 1.0, "mesh_density": "fine"},
            {"thickness": 2.0, "mesh_density": "fine"},
            {"thickness": 3.0, "mesh_density": "fine"},
        ]
    )
    envelope = build_envelope(resolve_points(td))

    pinned = envelope.by_name("mesh_density")
    assert pinned.kind == "pinned"
    assert pinned.pinned_value == "fine"


def test_a_pinned_factor_supplied_at_a_different_value_is_outside() -> None:
    # Someone reuses the model on a job with a different mesh density.
    # Predicting along a never-varied input is pure extrapolation, and
    # silence there would be the dangerous answer.
    td = training(
        [
            {"thickness": 1.0, "mesh_density": "fine"},
            {"thickness": 2.0, "mesh_density": "fine"},
            {"thickness": 3.0, "mesh_density": "fine"},
        ]
    )
    envelope = build_envelope(resolve_points(td))

    assert envelope.contains({"thickness": 2.0, "mesh_density": "fine"})
    assert not envelope.contains({"thickness": 2.0, "mesh_density": "coarse"})


def test_a_pinned_factor_gets_no_coverage_measure() -> None:
    td = training([{"thickness": float(i), "mesh_density": "fine"} for i in range(1, 9)])
    points = resolve_points(td)
    design = analyse_design(points, build_envelope(points))

    pinned = next(f for f in design.per_factor if f.name == "mesh_density")
    assert pinned.kind == "pinned"
    assert pinned.measure == "pinned"
    assert pinned.actionable is False


# --- §1.2.1 derived axes — real predictors you cannot commission ---------


def test_the_reserved_time_factor_is_derived_not_a_design_variable() -> None:
    # Two runs x four snapshots; `time` is the 0-based snapshot index.
    ftd = field_training(
        [{"thickness": t, "time": float(step)} for t in (1.0, 4.0) for step in range(4)]
    )
    envelope = build_envelope(resolve_points(ftd))

    axis = envelope.by_name(TIME_FACTOR)
    assert axis.kind == "derived"
    assert (axis.lo, axis.hi) == (0.0, 3.0)


def test_a_derived_axis_is_measured_and_guarded_but_not_actionable() -> None:
    ftd = field_training(
        [{"thickness": t, "time": float(s)} for t in (1.0, 2.0, 3.0, 4.0) for s in range(6)]
    )
    points = resolve_points(ftd)
    envelope = build_envelope(points)
    design = analyse_design(points, envelope)

    axis = next(f for f in design.per_factor if f.name == TIME_FACTOR)
    assert axis.measure == "span"  # §4.1 — trained min/max, not a joint radius
    assert axis.actionable is False  # no design point can fill it

    # It still counts toward `contains`: a prediction beyond the trained span
    # is extrapolation and must read outside.
    assert envelope.contains({"thickness": 2.0, "time": 3.0})
    assert not envelope.contains({"thickness": 2.0, "time": 40.0})


def test_a_derived_axis_is_excluded_from_the_joint_covering_radius() -> None:
    # Folding it in would report a hole no suggestion can address, and the
    # headline number's whole job is to be actionable (§4.1).
    # Enough thicknesses that the comparison set clears the analysis floor on
    # its own — otherwise it reads `insufficient` and the radii aren't
    # comparable at all.
    thicknesses = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)
    ftd = field_training(
        [{"thickness": t, "time": float(s)} for t in thicknesses for s in range(4)]
    )
    with_time_points = resolve_points(ftd)
    with_time = analyse_design(with_time_points, build_envelope(with_time_points))

    plain_points = resolve_points(training([{"thickness": t} for t in thicknesses]))
    without_time = analyse_design(plain_points, build_envelope(plain_points))

    assert with_time.covering_radius == pytest.approx(without_time.covering_radius, abs=1e-9)


def test_a_scalar_input_actually_named_time_is_not_treated_as_derived() -> None:
    # ingest reserves the name only on the time-resolved *field* path; a
    # scalar cohort with an input called `time` is a real design variable.
    td = training([{"time": float(i), "thickness": float(i)} for i in range(6)])
    envelope = build_envelope(resolve_points(td))
    assert envelope.by_name("time").kind == "continuous"


def test_a_field_training_set_that_is_not_time_resolved_has_no_derived_axis() -> None:
    ftd = field_training(
        [{"thickness": float(t), "heat_load": float(t * 2)} for t in range(1, 6)],
        time_resolved=False,
    )
    envelope = build_envelope(resolve_points(ftd))
    assert all(f.kind != "derived" for f in envelope.factors)


# --- §1.3 points outside a narrowed envelope -----------------------------


def test_points_outside_a_narrowed_envelope_are_counted_and_still_train() -> None:
    # "We ran 1-8 mm but only trust 2-6."
    td = training([{"thickness": float(t)} for t in range(1, 9)])
    points = resolve_points(td)
    envelope = build_envelope(points, overrides={"thickness": FactorOverride(lo=2.0, hi=6.0)})
    design = analyse_design(points, envelope)

    assert design.diagnostics.n_outside_envelope == 3  # 1, 7 and 8
    assert design.diagnostics.n_points == 8  # they still trained the model


def test_out_of_envelope_points_are_never_clamped_onto_the_boundary() -> None:
    # Clamping would pile them on the edge and manufacture false duplicates.
    td = training([{"thickness": t} for t in (1.0, 1.2, 1.4, 3.0, 5.0)])
    points = resolve_points(td)
    envelope = build_envelope(points, overrides={"thickness": FactorOverride(lo=2.0, hi=6.0)})

    rows = envelope.normalise(points).tolist()
    assert len(rows) == 5  # every training row is still represented
    inside = [r for r in rows if 0.0 <= r[0] <= 1.0]
    assert len(inside) == 2  # 3.0 and 5.0 only
    assert not any(r[0] == 0.0 for r in rows)  # the low three are not stacked at the edge


# --- §1.4 degenerate point sets ------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 2])
def test_too_few_runs_is_insufficient_never_poor(n: int) -> None:
    td = training([{"thickness": float(i), "heat_load": float(i)} for i in range(n)])
    points = resolve_points(td)
    design = analyse_design(points, build_envelope(points))

    assert design.verdict == "insufficient"
    assert design.message  # plain-English, per feedback_users_are_not_modellers
    assert "runs" in design.message.lower()


def test_every_factor_constant_is_insufficient_with_its_own_message() -> None:
    td = training([{"thickness": 2.0, "heat_load": 10.0} for _ in range(20)])
    points = resolve_points(td)
    design = analyse_design(points, build_envelope(points))

    assert design.verdict == "insufficient"
    assert "same design point" in design.message.lower()


def test_degenerate_sets_never_raise() -> None:
    for rows in ([], [{"thickness": 1.0}], [{"a": "x"}, {"a": "y"}]):
        points = resolve_points(training(rows))
        design = analyse_design(points, build_envelope(points))
        assert design.verdict == "insufficient"


# --- descriptor / factor-spec provenance ---------------------------------


def test_descriptor_supplies_integer_ness_that_the_factor_spec_cannot() -> None:
    # FactorSpec only knows numeric | select | bool | file; whether a numeric
    # input is an integer is a descriptor fact (InputFieldType).
    td = training([{"ply_count": float(n), "thickness": float(n)} for n in (1, 2, 3, 4, 5)])
    points = resolve_points(
        td, descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 10.0))
    )
    envelope = build_envelope(points)

    assert envelope.by_name("ply_count").kind == "integer"
    assert envelope.by_name("thickness").kind == "continuous"


def test_without_a_descriptor_a_whole_valued_numeric_stays_continuous() -> None:
    # Guessing integer-ness from observed whole numbers would misclassify a
    # thickness that happens to have been run at 1, 2, 3 mm.
    td = training([{"thickness": float(n), "heat_load": float(n)} for n in (1, 2, 3, 4, 5)])
    envelope = build_envelope(resolve_points(td))
    assert envelope.by_name("thickness").kind == "continuous"


def test_bool_and_select_inputs_resolve_to_categorical() -> None:
    td = training(
        [
            {"thickness": 1.0, "material": "cfrp", "use_radiation": True},
            {"thickness": 2.0, "material": "gfrp", "use_radiation": False},
            {"thickness": 3.0, "material": "cfrp", "use_radiation": True},
            {"thickness": 4.0, "material": "gfrp", "use_radiation": False},
        ]
    )
    points = resolve_points(
        td,
        descriptor=descriptor(
            num("thickness", 0.0, 10.0),
            select("material", "cfrp", "gfrp"),
            boolean("use_radiation"),
        ),
    )
    envelope = build_envelope(points)

    assert envelope.by_name("material").kind == "categorical"
    assert envelope.by_name("use_radiation").kind == "categorical"
    assert set(envelope.by_name("use_radiation").levels or ()) == {"True", "False"}

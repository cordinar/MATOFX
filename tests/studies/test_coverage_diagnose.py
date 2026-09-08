"""Coverage diagnostics (ADR-0020 §6, slice 1).

Three diseases hide in an ad-hoc cohort and they need different cures:
sparsity, confounding, and categorical imbalance. These tests pin the two
that sparsity metrics cannot see — and the near-duplicate rule that must
never swallow adjacent lattice points.
"""

from __future__ import annotations

import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import build_envelope, resolve_points
from matofx.studies.coverage.diagnose import diagnose
from tests.studies._coverage_fixtures import (
    boolean,
    descriptor,
    grid,
    integer,
    num,
    select,
    training,
)


def _diagnose(rows, *, settings=None, **kwargs):
    resolve_kwargs = {k: kwargs.pop(k) for k in ("descriptor", "factor_spec") if k in kwargs}
    points = resolve_points(training(rows), **resolve_kwargs)
    env = build_envelope(points, **resolve_kwargs, **kwargs)
    return diagnose(points, env, settings=settings or CoverageSettings())


# --- confounding ----------------------------------------------------------


def test_a_diagonal_ridge_is_flagged_as_confounded_even_though_it_is_space_filling() -> None:
    # Engineers rarely vary one factor at a time; thickness and load move
    # together. No amount of coverage fixes collinearity.
    rows = [{"thickness": float(i), "heat_load": float(i) * 3.0} for i in range(1, 21)]
    d = _diagnose(rows)

    assert d.max_abs_correlation == pytest.approx(1.0, abs=1e-6)
    assert d.condition_number is not None
    assert d.condition_number > 1.0


def test_confounding_and_sparsity_are_reported_separately() -> None:
    # A cohort beautifully space-filling *along a diagonal* must not pass as
    # healthy just because its points are evenly spread.
    from matofx.studies.coverage import analyse_design  # noqa: PLC0415

    rows = [{"thickness": float(i), "heat_load": float(i) * 3.0} for i in range(1, 41)]
    points = resolve_points(training(rows))
    design = analyse_design(points, build_envelope(points))

    assert design.diagnostics.max_abs_correlation == pytest.approx(1.0, abs=1e-6)
    assert any(w.code == "confounding" for w in design.warnings)


def test_an_orthogonal_grid_is_not_flagged_as_confounded() -> None:
    d = _diagnose(grid([1.0, 3.0, 5.0, 7.0], [2.0, 4.0, 6.0, 8.0]))
    assert d.max_abs_correlation == pytest.approx(0.0, abs=1e-9)


def test_correlation_is_omitted_not_zero_filled_below_two_continuous_factors() -> None:
    # "We cannot tell" and "they are uncorrelated" are different answers.
    d = _diagnose(
        [{"thickness": float(i), "material": "cfrp" if i % 2 else "gfrp"} for i in range(1, 11)]
    )
    assert d.factor_correlations is None
    assert d.max_abs_correlation is None
    assert d.condition_number is None


def test_correlation_keys_are_ordered_pairs_of_factor_names() -> None:
    d = _diagnose(grid([1.0, 3.0, 5.0], [2.0, 4.0, 6.0]))
    assert d.factor_correlations is not None
    assert set(d.factor_correlations) == {("heat_load", "thickness")}


def test_the_confounding_warning_is_plain_english() -> None:
    from matofx.studies.coverage import analyse_design  # noqa: PLC0415

    rows = [{"thickness": float(i), "heat_load": float(i) * 3.0} for i in range(1, 41)]
    points = resolve_points(training(rows))
    design = analyse_design(points, build_envelope(points))

    message = next(w.message for w in design.warnings if w.code == "confounding")
    assert "VIF" not in message
    assert "move together" in message.lower()


def test_the_correlation_threshold_comes_from_config() -> None:
    # Correlated but not perfectly so (|r| ~ 0.86) — the interesting band,
    # where the threshold is what decides the answer.
    rows = [
        {"thickness": float(i), "heat_load": float(i) * 3.0 + (20.0 if i % 2 else -20.0)}
        for i in range(1, 41)
    ]
    strict = _diagnose(rows, settings=CoverageSettings(max_abs_correlation=0.5))
    lax = _diagnose(rows, settings=CoverageSettings(max_abs_correlation=0.999))
    assert strict.confounded is True
    assert lax.confounded is False


# --- near-duplicates ------------------------------------------------------


def test_near_duplicates_are_counted_on_continuous_axes() -> None:
    rows = [
        {"thickness": 1.0, "heat_load": 10.0},
        {"thickness": 1.0000001, "heat_load": 10.0000001},
        {"thickness": 5.0, "heat_load": 30.0},
        {"thickness": 9.0, "heat_load": 50.0},
    ]
    d = _diagnose(rows, settings=CoverageSettings(duplicate_eps=1e-3))
    assert d.n_near_duplicates == 1


def test_duplicate_eps_never_applies_to_an_integer_axis() -> None:
    # On [1,5] the normalised spacing is 0.25; a generously tuned epsilon
    # must not flag genuinely distinct adjacent lattice points.
    rows = [{"ply_count": float(n), "thickness": 1.0 + n / 100} for n in (1, 2, 3, 4, 5)]
    d = _diagnose(
        rows,
        settings=CoverageSettings(duplicate_eps=0.4),
        descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 9.0)),
    )
    assert d.n_near_duplicates == 0


def test_a_duplicate_needs_exact_equality_on_every_discrete_axis() -> None:
    rows = [
        {"thickness": 1.0, "material": "cfrp"},
        {"thickness": 1.0, "material": "gfrp"},  # same continuous, different level
        {"thickness": 5.0, "material": "cfrp"},
        {"thickness": 9.0, "material": "gfrp"},
    ]
    d = _diagnose(rows, settings=CoverageSettings(duplicate_eps=0.1))
    assert d.n_near_duplicates == 0


def test_exact_repeats_across_every_axis_are_duplicates() -> None:
    rows = [
        {"thickness": 1.0, "material": "cfrp"},
        {"thickness": 1.0, "material": "cfrp"},
        {"thickness": 5.0, "material": "cfrp"},
        {"thickness": 9.0, "material": "gfrp"},
    ]
    d = _diagnose(rows, settings=CoverageSettings(duplicate_eps=1e-9))
    assert d.n_near_duplicates == 1


# --- categorical balance — the silent failure ----------------------------


def test_a_forty_two_split_flags_the_underpopulated_level() -> None:
    # 40 CFRP and 2 GFRP looks perfectly covered on every continuous axis,
    # and the surrogate will confidently predict a material it has
    # effectively never seen.
    rows = [{"thickness": 1.0 + i * 0.2, "material": "cfrp"} for i in range(40)]
    rows += [{"thickness": 3.0, "material": "gfrp"}, {"thickness": 7.0, "material": "gfrp"}]
    d = _diagnose(rows, settings=CoverageSettings(min_level_points=5))

    assert d.level_counts["material"] == {"cfrp": 40, "gfrp": 2}
    assert ("material", "gfrp", 2) in d.underpopulated_levels


def test_imbalance_is_flagged_even_with_excellent_continuous_coverage() -> None:
    from matofx.studies.coverage import analyse_design  # noqa: PLC0415

    rows = [{"thickness": 1.0 + i * 0.2, "material": "cfrp"} for i in range(40)]
    rows += [{"thickness": 3.0, "material": "gfrp"}, {"thickness": 7.0, "material": "gfrp"}]
    points = resolve_points(training(rows))
    design = analyse_design(
        points, build_envelope(points), settings=CoverageSettings(min_level_points=5)
    )

    assert any(w.code == "categorical_imbalance" for w in design.warnings)
    material = next(f for f in design.per_factor if f.name == "material")
    assert material.verdict in ("thin", "poor")


def test_a_level_the_archive_never_ran_counts_as_zero_not_as_absent() -> None:
    rows = [{"thickness": float(i), "material": "cfrp" if i % 2 else "gfrp"} for i in range(1, 21)]
    d = _diagnose(
        rows,
        settings=CoverageSettings(min_level_points=3),
        descriptor=descriptor(
            num("thickness", 0.0, 30.0), select("material", "cfrp", "gfrp", "titanium")
        ),
    )
    assert d.level_counts["material"]["titanium"] == 0
    assert ("material", "titanium", 0) in d.underpopulated_levels


def test_a_balanced_design_flags_nothing() -> None:
    rows = [{"thickness": float(i), "material": "cfrp" if i % 2 else "gfrp"} for i in range(1, 21)]
    d = _diagnose(rows, settings=CoverageSettings(min_level_points=5))
    assert d.underpopulated_levels == []


def test_boolean_levels_are_reported_as_levels() -> None:
    rows = [{"thickness": float(i), "use_radiation": i > 18} for i in range(1, 21)]
    d = _diagnose(
        rows,
        settings=CoverageSettings(min_level_points=5),
        descriptor=descriptor(num("thickness", 0.0, 30.0), boolean("use_radiation")),
    )
    assert d.level_counts["use_radiation"] == {"False": 18, "True": 2}
    assert ("use_radiation", "True", 2) in d.underpopulated_levels


# --- §4.2 split vs pool ---------------------------------------------------


def test_split_mode_is_chosen_when_the_groups_stay_populated() -> None:
    rows = [
        {"thickness": float(i), "heat_load": float(i * 2), "material": m}
        for m in ("cfrp", "gfrp")
        for i in range(1, 21)
    ]
    d = _diagnose(rows, settings=CoverageSettings(min_points_per_group=8))
    assert d.grouping_mode == "split"


def test_pooled_mode_is_chosen_when_groups_would_fragment() -> None:
    # Three selects of three levels is 27 groups; a perfectly reasonable
    # 40-run design becomes 1.5 runs per group.
    levels = ("a", "b", "c")
    rows = []
    for i in range(40):
        rows.append(
            {
                "thickness": 1.0 + i * 0.2,
                "heat_load": 2.0 + i * 0.3,
                "m1": levels[i % 3],
                "m2": levels[(i // 3) % 3],
                "m3": levels[(i // 9) % 3],
            }
        )
    d = _diagnose(rows, settings=CoverageSettings(min_points_per_group=8))
    assert d.grouping_mode == "pooled"


def test_balance_is_reported_in_both_grouping_modes() -> None:
    levels = ("a", "b", "c")
    rows = [
        {
            "thickness": 1.0 + i * 0.2,
            "heat_load": 2.0 + i * 0.3,
            "m1": levels[i % 3],
            "m2": levels[(i // 3) % 3],
            "m3": levels[(i // 9) % 3],
        }
        for i in range(40)
    ]
    pooled = _diagnose(rows, settings=CoverageSettings(min_points_per_group=8))
    split = _diagnose(rows, settings=CoverageSettings(min_points_per_group=1))

    assert pooled.grouping_mode == "pooled"
    assert split.grouping_mode == "split"
    assert set(pooled.level_counts) == set(split.level_counts) == {"m1", "m2", "m3"}


def test_grouping_mode_is_not_applicable_without_a_categorical_factor() -> None:
    d = _diagnose(grid([1.0, 3.0, 5.0], [2.0, 4.0, 6.0]))
    assert d.grouping_mode == "n/a"


def test_a_forty_run_design_over_twenty_seven_groups_is_not_all_poor() -> None:
    # The false alarm: a GP with encoded categoricals shares information
    # across levels, so fragmenting into 27 groups of 1.5 runs would mark
    # every group poor for no real reason.
    from matofx.studies.coverage import analyse_design  # noqa: PLC0415

    # The continuous axes are deliberately *decorrelated* here — this test is
    # about the grouping rule, and a diagonal ridge would read `poor` on
    # sparsity for reasons that have nothing to do with 27 groups.
    points = resolve_points(training(_twenty_seven_group_rows()))
    design = analyse_design(
        points, build_envelope(points), settings=CoverageSettings(min_points_per_group=8)
    )
    assert design.diagnostics.grouping_mode == "pooled"
    assert design.verdict != "poor"


def _twenty_seven_group_rows() -> list[dict[str, object]]:
    """40 runs over three 3-level selects, with a well-spread continuous pair."""
    levels = ("a", "b", "c")
    return [
        {
            "thickness": 1.0 + (i % 7) * 1.3,
            "heat_load": 2.0 + ((i * 3) % 11) * 0.9,
            "m1": levels[i % 3],
            "m2": levels[(i // 3) % 3],
            "m3": levels[(i // 9) % 3],
        }
        for i in range(40)
    ]

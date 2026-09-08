"""A projected archive keeps its held-constant values (2026-09-03 regression).

Reported from the UI: on a cohort with the material opened up, the suggestion
ladder read **20 runs patchy, 40 well covered, 80 patchy** — a bigger batch
rating worse, twice.

Two defects compounded.

``PointSet.values`` fell back to a held constant only when *no* row carried the
name. That is true of an archive on its own and false the moment it is
projected: the planner appends proposed runs that **do** carry the value, so
one row having it switched the fallback off for the other twenty, which came
back ``None``.

``_categorical_row`` then counted those ``None``s as a level of their own —
``counts.get(key, 0) + 1`` invents one — which inflated ``len(counts)`` so the
even share fell from a half to a third, and put a fixed count of twenty into
the ``min()``. The balance therefore *decreased* as real runs were added,
which is what made the ladder non-monotone.

The observed values reproduce exactly: (6/40)/(1/3) = 0.45, (19/60)/(1/3) =
0.95, (20/100)/(1/3) = 0.60 — patchy, well covered, patchy.
"""

from __future__ import annotations

from dataclasses import replace

from matofx.config import CoverageSettings
from matofx.studies.coverage.envelope import Envelope, FactorEnvelope
from matofx.studies.coverage.points import PointSet
from matofx.studies.coverage.report import analyse_design

_MATERIALS = ("Aluminium", "Magnesium")


def _archive(n: int = 20) -> PointSet:
    """Rows that vary one number, with the material held constant."""
    return PointSet(
        rows=tuple({"fin_height": 15.0 + i * 0.5} for i in range(n)),
        factor_names=("fin_height",),
        constants={"material": "Aluminium"},
        numeric_names=frozenset({"fin_height"}),
    )


def _promoted() -> Envelope:
    return Envelope(
        factors=(
            FactorEnvelope(name="fin_height", kind="continuous", lo=5.0, hi=45.0),
            FactorEnvelope(
                name="material",
                kind="categorical",
                levels=_MATERIALS,
                declared_levels=_MATERIALS,
                pinned_value="Aluminium",
                varying_kind="categorical",
            ),
        )
    )


# --- the constants fallback -----------------------------------------------


def test_a_held_constant_resolves_on_every_row() -> None:
    assert _archive(3).values("material") == ["Aluminium"] * 3


def test_it_still_resolves_once_one_row_carries_the_value() -> None:
    """The bug in one assertion: appending a row that has the value must not
    take it away from the rows that do not."""
    projected = replace(
        _archive(3), rows=(*_archive(3).rows, {"fin_height": 30.0, "material": "Magnesium"})
    )
    assert projected.values("material") == ["Aluminium"] * 3 + ["Magnesium"]


def test_a_row_that_states_none_is_left_as_none() -> None:
    """An explicit ``None`` is a missing value, not a request for the
    constant — the fallback fills a *gap*, it does not overwrite."""
    projected = replace(_archive(1), rows=({"fin_height": 1.0, "material": None},))
    assert projected.values("material") == [None]


def test_a_name_with_no_constant_is_unaffected() -> None:
    assert _archive(2).values("nothing_like_this") == [None, None]


# --- an unknown value is not a level --------------------------------------


def _balance(rows: tuple[dict, ...]) -> tuple[str, float]:
    points = replace(_archive(20), rows=(*_archive(20).rows, *rows))
    design = analyse_design(points, _promoted(), settings=CoverageSettings())
    material = next(f for f in design.per_factor if f.name == "material")
    return material.verdict, material.value


def test_balance_improves_as_the_minority_level_is_filled() -> None:
    """The property the ladder depends on. It used to fall."""
    _, few = _balance(tuple({"fin_height": 20.0, "material": "Magnesium"} for _ in range(5)))
    _, more = _balance(tuple({"fin_height": 20.0, "material": "Magnesium"} for _ in range(20)))
    assert more > few


def test_adding_runs_never_makes_the_balance_worse() -> None:
    """Directly the reported symptom: 20 → 40 → 80 must not go good → patchy.

    The proposed runs alternate levels, so every prefix is close to even and
    the balance can only climb towards it.
    """
    proposed = [{"fin_height": 10.0 + i, "material": _MATERIALS[i % 2]} for i in range(80)]
    values = [_balance(tuple(proposed[:n]))[1] for n in (20, 40, 80)]
    assert values == sorted(values), f"balance fell as runs were added: {values}"


def test_a_value_outside_the_declared_levels_is_not_a_new_level() -> None:
    """A phantom level shrinks the even share for everybody and competes for
    the minimum — which is how twenty unknowns became a third material."""
    _verdict, value = _balance(
        tuple({"fin_height": 20.0, "material": "Magnesium"} for _ in range(20))
    )
    _stray, stray_value = _balance(
        (
            *({"fin_height": 20.0, "material": "Magnesium"} for _ in range(20)),
            {"fin_height": 20.0, "material": "Unobtanium"},
        )
    )
    assert stray_value == value, "a stray value must not move the balance"


def test_runs_with_no_value_are_reported_rather_than_swallowed() -> None:
    """Dropped from the measure, but the reader is told."""
    points = replace(
        _archive(20),
        rows=(
            *_archive(20).rows,
            *({"fin_height": 20.0, "material": "Magnesium"} for _ in range(20)),
            {"fin_height": 21.0, "material": None},
        ),
    )
    design = analyse_design(points, _promoted(), settings=CoverageSettings())
    material = next(f for f in design.per_factor if f.name == "material")
    assert any("no value recorded" in str(m) for m in material.missing_values)

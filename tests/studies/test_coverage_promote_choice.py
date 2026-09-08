"""A held-constant *choice* can be opened up, not only a held-constant number.

Reported from the UI: the "Also vary the inputs held constant so far" switch
never appeared on a cohort whose only held-constant input was the plate
material. The control was implemented and correct; it renders only when
something is promotable, and the promotable test was ``is it a number``.

That rule was too narrow in two ways. It excluded genuine choices — a
material, a solver, a flag — which is the whole reason the switch was
invisible. Less obviously, it also excluded a *discrete numeric* input
authored as a ``select`` of ``6 / 8 / 10``, whose content is perfectly
numeric.

The test is now whether the descriptor **declares a domain the form would
accept**: bounds for a number, options for a choice. These tests pin which
types qualify, what promotion produces, and the two directions it must refuse.
"""

from __future__ import annotations

from typing import get_args

import pytest

from matofx.api.schemas import CoverageEnvelopeFactor
from matofx.studies.coverage.envelope import (
    Envelope,
    FactorEnvelope,
    FactorOverride,
    _apply_override,
    _declared_levels,
    _varying_kind,
)


class _Field:
    """The slice of a descriptor input the envelope actually reads."""

    def __init__(self, type_: str, options: list[str] | None = None) -> None:
        self.type = type_
        self.options = options


# --- which types can be opened up -----------------------------------------


@pytest.mark.parametrize(
    ("field_type", "expected"),
    [
        pytest.param("float", "continuous", id="float-to-continuous"),
        pytest.param("integer", "integer", id="integer-stays-integer"),
        pytest.param("select", "categorical", id="select-to-categorical"),
        pytest.param("boolean", "categorical", id="boolean-to-categorical"),
    ],
)
def test_a_declared_domain_can_be_opened_up(field_type: str, expected: str) -> None:
    fields = {"x": _Field(field_type, ["a", "b"])}
    assert _varying_kind("x", fields) == expected


@pytest.mark.parametrize(
    "field_type",
    [
        pytest.param("material", id="material-may-not-enumerate-its-options"),
        pytest.param("multiselect", id="multiselect-domain-is-a-power-set"),
        pytest.param("string", id="string-is-open"),
        pytest.param("file", id="file-is-open"),
        pytest.param("table", id="table-is-open"),
    ],
)
def test_an_undeclared_domain_is_refused(field_type: str) -> None:
    """Each for its own reason, but the same consequence: there is nothing to
    sweep, and inventing one would propose runs the form rejects."""
    assert _varying_kind("x", {"x": _Field(field_type, ["a", "b"])}) is None


def test_an_input_the_descriptor_does_not_mention_is_refused() -> None:
    assert _varying_kind("ghost", {}) is None


# --- what promotion produces ----------------------------------------------


def _pinned_choice(levels: tuple[str, ...] = ("Aluminium", "Magnesium")) -> FactorEnvelope:
    return FactorEnvelope(
        name="material",
        kind="pinned",
        pinned_value="Aluminium",
        varying_kind="categorical",
        declared_levels=levels,
        label="Plate material",
    )


def test_giving_a_pinned_choice_its_options_makes_it_a_real_axis() -> None:
    promoted = _apply_override(_pinned_choice(), FactorOverride(levels=("Aluminium", "Magnesium")))
    assert promoted.kind == "categorical"
    assert promoted.levels == ("Aluminium", "Magnesium")
    assert promoted.actionable, "a promoted axis must be one suggestions can move"


def test_promotion_keeps_the_label_and_the_declared_options() -> None:
    """An override changes the domain, never what the input is called nor what
    the tool itself allows."""
    promoted = _apply_override(_pinned_choice(), FactorOverride(levels=("Aluminium", "Magnesium")))
    assert promoted.label == "Plate material"
    assert promoted.declared_levels == ("Aluminium", "Magnesium")


def test_one_level_is_not_a_promotion() -> None:
    """One option is what the axis already was. Promoting to it would report
    an axis as varying that no suggestion can move."""
    unchanged = _apply_override(_pinned_choice(), FactorOverride(levels=("Aluminium",)))
    assert unchanged.kind == "pinned"


def test_a_choice_is_not_promoted_by_bounds() -> None:
    """Bounds on a set of options are meaningless; the axis must stay pinned
    rather than becoming a number line between two materials."""
    unchanged = _apply_override(_pinned_choice(), FactorOverride(lo=0.0, hi=1.0))
    assert unchanged.kind == "pinned"


def test_a_number_is_not_promoted_by_levels() -> None:
    """The mirror image, and the one that would quietly destroy a range."""
    pinned_number = FactorEnvelope(
        name="rise", kind="pinned", pinned_value=60.0, varying_kind="continuous"
    )
    unchanged = _apply_override(pinned_number, FactorOverride(levels=("10", "120")))
    assert unchanged.kind == "pinned"


def test_a_choice_with_no_declared_domain_stays_pinned() -> None:
    """A material whose options the descriptor never enumerates."""
    material = FactorEnvelope(name="alloy", kind="pinned", pinned_value="6061", varying_kind=None)
    unchanged = _apply_override(material, FactorOverride(levels=("6061", "7075")))
    assert unchanged.kind == "pinned"


# --- and the suggestions actually move it ---------------------------------


def test_a_promoted_choice_is_swept_across_the_batch() -> None:
    """The point of promoting it. The batch has to spread over the options,
    not sit on one of them — an imbalanced categorical axis is not fixed by
    more runs at the same level.
    """
    envelope = Envelope(
        factors=(
            FactorEnvelope(name="fin_height", kind="continuous", lo=5.0, hi=45.0),
            _apply_override(_pinned_choice(), FactorOverride(levels=("Aluminium", "Magnesium"))),
        )
    )
    points = envelope.denormalise(envelope.sample(6, seed=0))

    assert len(points) == 6
    used = {str(p["material"]) for p in points}
    assert used == {"Aluminium", "Magnesium"}, "both options must appear"


def test_every_suggested_run_keeps_a_held_choice_constant() -> None:
    """The default is unchanged: without the override every suggested run
    keeps the value the archive used."""
    envelope = Envelope(
        factors=(
            FactorEnvelope(name="fin_height", kind="continuous", lo=5.0, hi=45.0),
            _pinned_choice(),
        )
    )
    points = envelope.denormalise(envelope.sample(4, seed=0))
    assert {str(p["material"]) for p in points} == {"Aluminium"}


def test_a_promoted_choice_is_inside_the_envelope_it_came_from() -> None:
    """A suggested run nobody could submit is the failure the level-cycling
    exists to prevent, so the envelope's own check has to accept its output."""
    envelope = Envelope(
        factors=(
            FactorEnvelope(name="fin_height", kind="continuous", lo=5.0, hi=45.0),
            _apply_override(_pinned_choice(), FactorOverride(levels=("Aluminium", "Magnesium"))),
        )
    )
    points = envelope.denormalise(envelope.sample(5, seed=0))
    assert all(envelope.contains(point) for point in points)


def test_a_boolean_enumerates_itself() -> None:
    """It has no ``options`` in the descriptor, but it does not need any."""
    assert _declared_levels("flag", {"flag": _Field("boolean")}) == ("False", "True")


def test_boolean_levels_match_what_the_runs_store() -> None:
    """Levels are compared as strings everywhere downstream, so they have to
    be the strings ``str()`` produces for the stored values."""
    levels = _declared_levels("flag", {"flag": _Field("boolean")})
    assert {str(True), str(False)} == set(levels)


def test_a_numeric_input_still_has_no_options() -> None:
    assert _declared_levels("x", {"x": _Field("float")}) == ()


# --- the API has to be able to say what the engine produces ----------------


def test_varying_kind_contract() -> None:
    """Every kind the engine can promote to must survive the response schema.

    The literal on ``CoverageEnvelopeFactor`` silently mirrored the engine's
    old numeric-only rule. Widening the rule therefore made the whole coverage
    endpoint raise a 500 for any cohort with a held-constant choice — which is
    every cohort this feature was built for. The page hung on "Measuring
    coverage..." because the failure was a validation error on the way *out*,
    long after the analysis had succeeded.

    Comparing the two sets is the only check that would have caught it: the
    engine tests passed, the UI tests passed, and nothing crossed the boundary
    where the mismatch lived.
    """
    fields = {
        "f": _Field("float"),
        "i": _Field("integer"),
        "s": _Field("select", ["a", "b"]),
        "b": _Field("boolean"),
        "m": _Field("material"),
    }
    produced = {_varying_kind(name, fields) for name in fields}
    annotation = CoverageEnvelopeFactor.model_fields["varying_kind"].annotation
    accepted = set(get_args(get_args(annotation)[0]))

    assert produced - {None} <= accepted, (
        f"the engine can emit {sorted(produced - {None} - accepted)}, which the API rejects"
    )


def test_a_promoted_choice_survives_the_response_schema() -> None:
    """The end-to-end shape of the 500: build what the router builds."""
    factor = _pinned_choice()
    row = CoverageEnvelopeFactor(
        name=factor.name,
        kind=factor.kind,
        label=factor.label,
        levels=list(factor.levels or ()),
        declared_levels=list(factor.declared_levels),
        varying_kind=factor.varying_kind,
        actionable=factor.actionable,
    )
    assert row.varying_kind == "categorical"

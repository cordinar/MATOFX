"""An exported surrogate keeps the source tool's input rules (ADR-0008 §7.1).

The synthesised descriptor used to carry only ``label`` and ``unit`` from the
source tool — the two things the form *reads* — and dropped everything it
*enforces*. A tool whose fin count is a whole number on a step of 1, bounded
6 to 28, exported as an unbounded float on no step; a rule like "these three
fractions must total 100" vanished entirely. Both produced a stand-in that
accepted designs the tool it stands in for would refuse.

These tests run a **real descriptor** through the synthesis and compare the
rules field by field, rather than asserting against a hand-written fixture that
would share the synthesiser's blind spot.
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.models.tool_synthesis import synthesise_descriptor
from matofx.tools.descriptor import ToolDescriptor

# A trimmed stand-in for the Finned Cold Plate showcase tool: an integer count,
# two precision floats (one a slider), and a radio-button material.
SOURCE_INPUTS: dict[str, dict[str, Any]] = {
    "fin_count": {
        "name": "fin_count",
        "label": "Number of fins",
        "type": "integer",
        "minimum": 6.0,
        "maximum": 28.0,
        "precision": 1.0,
        "widget": "field",
        "help_text": "Fins are 1 mm thick.",
    },
    "fin_height": {
        "name": "fin_height",
        "label": "Fin height",
        "type": "float",
        "unit": "mm",
        "minimum": 5.0,
        "maximum": 45.0,
        "precision": 0.1,
        "widget": "slider",
    },
    "material": {
        "name": "material",
        "label": "Plate material",
        "type": "select",
        "options": ["Aluminium", "Magnesium"],
        "choice_widget": "radio",
    },
}

FACTORS: list[dict[str, Any]] = [
    {"name": "fin_count", "min": 8.0, "max": 24.0, "label": "Number of fins"},
    {"name": "fin_height", "min": 6.4, "max": 41.2, "label": "Fin height", "unit": "mm"},
    {"name": "material", "levels": ["Aluminium", "Magnesium"], "label": "Plate material"},
]
RESPONSES: list[dict[str, Any]] = [{"name": "t_source", "kind": "scalar"}]


def _descriptor(**overrides: Any) -> ToolDescriptor:
    kwargs: dict[str, Any] = {
        "source_tool_alias": "finned_plate",
        "model_id": 21,
        "version_id": 22,
        "name": "Cold plate surrogate",
        "description": "",
        "factors": FACTORS,
        "responses": RESPONSES,
        "version_ordinal": 1,
        "source_inputs": SOURCE_INPUTS,
    }
    kwargs.update(overrides)
    return synthesise_descriptor(**kwargs)


def _field(descriptor: ToolDescriptor, name: str) -> Any:
    return next(i for i in descriptor.inputs if i.name == name)


def test_integer_factor_stays_an_integer() -> None:
    """A whole-number input exported as a plain float accepted 17.4 fins."""
    fin_count = _field(_descriptor(), "fin_count")
    assert fin_count.type == "integer"
    assert fin_count.precision == 1.0


def test_precision_and_widget_carry_over() -> None:
    fin_height = _field(_descriptor(), "fin_height")
    assert fin_height.type == "float"
    assert fin_height.precision == 0.1
    assert fin_height.widget == "slider"
    assert fin_height.unit == "mm"


def test_source_bounds_are_enforced_even_when_extrapolation_is_advisory() -> None:
    """The tool's own min/max is a *validity* limit, not a coverage one.

    Advisory extrapolation is about the range the runs covered; a fin count of
    -3 was never a legal design, surrogate or not.
    """
    d = _descriptor(block_extrapolation=False)
    assert (_field(d, "fin_count").minimum, _field(d, "fin_count").maximum) == (6.0, 28.0)
    assert (_field(d, "fin_height").minimum, _field(d, "fin_height").maximum) == (5.0, 45.0)


def test_block_extrapolation_intersects_with_the_source_bounds() -> None:
    """Blocking tightens to the trained range, which is inside the tool's own."""
    d = _descriptor(block_extrapolation=True)
    assert (_field(d, "fin_count").minimum, _field(d, "fin_count").maximum) == (8.0, 24.0)
    assert (_field(d, "fin_height").minimum, _field(d, "fin_height").maximum) == (6.4, 41.2)


def test_integer_bounds_are_pulled_inward_to_whole_numbers() -> None:
    """The descriptor refuses a fractional bound on an integer input.

    Rounding *outward* would also admit a value the source tool rejects, so a
    trained range of 8.3 to 23.7 becomes 9 to 23, never 8 to 24.
    """
    d = _descriptor(
        factors=[{"name": "fin_count", "min": 8.3, "max": 23.7}, *FACTORS[1:]],
        block_extrapolation=True,
    )
    assert (_field(d, "fin_count").minimum, _field(d, "fin_count").maximum) == (9.0, 23.0)


def test_slider_falls_back_to_a_textbox_without_a_fixed_range() -> None:
    """A slider needs both bounds; an unbounded factor has nothing to span."""
    d = _descriptor(
        source_inputs={
            **SOURCE_INPUTS,
            "fin_height": {"name": "fin_height", "type": "float", "widget": "slider"},
        }
    )
    assert _field(d, "fin_height").widget == "field"


def test_categorical_keeps_its_radio_treatment_and_source_help() -> None:
    material = _field(_descriptor(), "material")
    assert material.type == "select"
    assert material.choice_widget == "radio"
    assert material.options == ["Aluminium", "Magnesium"]


def test_help_text_keeps_the_source_help_and_adds_the_trained_range() -> None:
    fin_count = _field(_descriptor(), "fin_count")
    assert fin_count.help_text is not None
    assert "Fins are 1 mm thick." in fin_count.help_text
    assert "8.0 to 24.0" in fin_count.help_text


def test_held_constant_numeric_keeps_the_source_integer_type() -> None:
    """A held constant is still an envelope axis the guard checks.

    Its default has to match its type, so an integer input held at 16 must
    export as an integer default, not ``16.0``.
    """
    d = _descriptor(
        factors=FACTORS[1:],
        constants=[{"name": "fin_count", "value": 16, "label": "Number of fins"}],
    )
    fin_count = _field(d, "fin_count")
    assert fin_count.type == "integer"
    assert fin_count.default == 16
    assert fin_count.group == "held_constant"


# --- cross-field sum checks -------------------------------------------------

SUM_INPUTS: dict[str, dict[str, Any]] = {
    "part_a": {
        "name": "part_a",
        "type": "float",
        "sum_check": {"value": 100.0, "sum_parameters": ["part_a", "part_b", "part_c"]},
    },
    "part_b": {"name": "part_b", "type": "float"},
    "part_c": {"name": "part_c", "type": "float"},
}


def _mixture(**overrides: Any) -> ToolDescriptor:
    kwargs: dict[str, Any] = {
        "source_tool_alias": "mix",
        "model_id": 1,
        "version_id": 1,
        "name": "Mixture surrogate",
        "description": "",
        "factors": [
            {"name": "part_a", "min": 0.0, "max": 100.0},
            {"name": "part_b", "min": 0.0, "max": 100.0},
            {"name": "part_c", "min": 0.0, "max": 100.0},
        ],
        "responses": RESPONSES,
        "version_ordinal": 1,
        "source_inputs": SUM_INPUTS,
    }
    kwargs.update(overrides)
    return synthesise_descriptor(**kwargs)


def test_sum_check_survives_the_export() -> None:
    check = _field(_mixture(), "part_a").sum_check
    assert check is not None
    assert check.value == 100.0
    assert check.sum_parameters == ["part_a", "part_b", "part_c"]


def test_sum_check_spans_a_varied_factor_and_a_held_constant() -> None:
    """A member held fixed during training is still on the exported form."""
    d = _mixture(
        factors=[
            {"name": "part_a", "min": 0.0, "max": 100.0},
            {"name": "part_b", "min": 0.0, "max": 100.0},
        ],
        constants=[{"name": "part_c", "value": 20.0}],
    )
    check = _field(d, "part_a").sum_check
    assert check is not None
    assert check.sum_parameters == ["part_a", "part_b", "part_c"]


def test_sum_check_is_dropped_when_a_member_did_not_survive() -> None:
    """A partial check names an input the descriptor doesn't declare.

    Carrying it anyway would make the whole export unbuildable, which is a far
    worse outcome than an unchecked form.
    """
    d = _mixture(
        factors=[
            {"name": "part_a", "min": 0.0, "max": 100.0},
            {"name": "part_b", "min": 0.0, "max": 100.0},
        ]
    )
    assert {i.name for i in d.inputs} == {"part_a", "part_b"}
    assert _field(d, "part_a").sum_check is None


# --- back-compat ------------------------------------------------------------


def test_without_source_inputs_the_old_rule_free_shape_is_unchanged() -> None:
    """An export whose source tool can no longer be read must still build."""
    d = _descriptor(source_inputs=None)
    fin_count = _field(d, "fin_count")
    assert fin_count.type == "float"
    assert (fin_count.minimum, fin_count.maximum) == (None, None)
    assert fin_count.precision is None
    # The categorical is a property of the *model*, not the source descriptor,
    # so it survives regardless.
    assert _field(d, "material").type == "select"


@pytest.mark.parametrize("missing", ["fin_count", "fin_height"])
def test_an_input_the_source_no_longer_declares_keeps_the_plain_treatment(
    missing: str,
) -> None:
    d = _descriptor(source_inputs={k: v for k, v in SOURCE_INPUTS.items() if k != missing})
    assert _field(d, missing).type == "float"

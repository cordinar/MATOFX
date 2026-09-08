"""The model detail page shows labels, never frozen identifiers.

``SurrogateModelVersion.factors`` / ``.responses`` freeze *names*; the labels
live in the source tool's descriptor and have to be resolved and threaded in.
The resolver used to cover only ``type == "number"`` outputs, so a **field**
output — declared as a ``file`` — fell through and the field panels, the viewer
title and its axes all read ``temperature_field``. Inputs weren't resolved at
all, so every slider in the predicted-field viewer read ``fin_height``.
"""

from __future__ import annotations

from matofx.api.schemas import FieldPredictRequest
from matofx.ui.pages.models import _display_labels

DESCRIPTOR_OUTPUTS = [
    {"name": "t_source", "label": "Peak component temperature", "type": "number", "unit": "degC"},
    {"name": "mass", "label": "Plate + fin mass", "type": "number", "unit": "g"},
    {
        "name": "temperature_field",
        "label": "Plate temperature field",
        "type": "file",
        "file_type": "field",
    },
]

DESCRIPTOR_INPUTS = [
    {"name": "fin_height", "label": "Fin height", "type": "float", "unit": "mm"},
    {"name": "material", "label": "Plate material", "type": "select"},
]


def test_field_outputs_get_a_label_too() -> None:
    """A 3-D field is declared as a ``file``, which the number-only filter skipped."""
    labels = _display_labels(DESCRIPTOR_OUTPUTS)
    assert labels["temperature_field"] == "Plate temperature field"


def test_units_are_appended_and_the_identifier_is_not() -> None:
    labels = _display_labels(DESCRIPTOR_OUTPUTS)
    assert labels["t_source"] == "Peak component temperature (degC)"
    assert labels["mass"] == "Plate + fin mass (g)"
    # The bare name must not trail the label — the rule is labels, not both.
    assert "t_source" not in labels["t_source"]


def test_inputs_resolve_through_the_same_map() -> None:
    labels = _display_labels(DESCRIPTOR_INPUTS)
    assert labels == {"fin_height": "Fin height (mm)", "material": "Plate material"}


def test_a_field_without_a_label_falls_back_to_its_name() -> None:
    assert _display_labels([{"name": "x", "type": "number"}]) == {"x": "x"}


def test_junk_rows_are_skipped_rather_than_raising() -> None:
    """A descriptor is a stored JSON blob that outlives the release that wrote it."""
    assert _display_labels([None, {}, "nope", {"name": "y", "label": "Y"}]) == {"y": "Y"}
    assert _display_labels(None) == {}


def test_predict_point_accepts_a_categorical_level() -> None:
    """A varied ``select`` is a real factor, named by its level, not a number.

    ``dict[str, float]`` could not carry one at all, so the viewer had no way to
    ask for the Magnesium version of a design.
    """
    body = FieldPredictRequest(point={"fin_height": 20.0, "material": "Magnesium"})
    assert body.point == {"fin_height": 20.0, "material": "Magnesium"}

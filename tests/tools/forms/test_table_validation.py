"""Submit-time per-cell validation for ``table`` inputs (bounds + types)."""

from __future__ import annotations

import pytest

from matofx.tools.descriptor import InputField, TableColumn, ToolDescriptor
from matofx.tools.forms.errors import FormValidationError
from matofx.tools.forms.model import validate_inputs


def _descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        alias="t",
        name="T",
        version="1.0.0",
        description="d",
        main_script="m.py",
        inputs=[
            InputField(
                name="layers",
                label="Layers",
                type="table",
                required=False,
                columns=[
                    TableColumn(name="n", label="Count", type="integer", minimum=1, maximum=10),
                    TableColumn(
                        name="thk", label="Thickness", type="float", unit="mm", minimum=0.0
                    ),
                    TableColumn(name="mat", label="Material", type="string"),
                ],
            )
        ],
    )


def _first_error(rows: list[dict]) -> str:
    with pytest.raises(FormValidationError) as exc:
        validate_inputs(_descriptor(), {"layers": rows})
    return exc.value.field_errors[0].message


def test_valid_rows_pass() -> None:
    out = validate_inputs(
        _descriptor(),
        {"layers": [{"n": 3, "thk": 2.5, "mat": "steel"}, {"n": 5, "thk": 0.0, "mat": "alu"}]},
    )
    assert out["layers"][0]["n"] == 3


def test_blank_cells_are_allowed() -> None:
    # Empty/None cells count as "not filled" and don't trip bounds.
    out = validate_inputs(_descriptor(), {"layers": [{"n": None, "thk": "", "mat": ""}]})
    assert out["layers"] == [{"n": None, "thk": "", "mat": ""}]


def test_above_maximum_rejected() -> None:
    assert _first_error([{"n": 20, "thk": 1.0}]) == "Layers: Count in row 1 must be at most 10."


def test_below_minimum_rejected_with_unit() -> None:
    assert _first_error([{"n": 3, "thk": -1.0}]) == (
        "Layers: Thickness in row 1 must be at least 0 mm."
    )


def test_non_whole_integer_rejected() -> None:
    assert _first_error([{"n": 2.5, "thk": 1.0}]) == (
        "Layers: Count in row 1 must be a whole number."
    )


def test_non_numeric_rejected() -> None:
    assert _first_error([{"n": "abc", "thk": 1.0}]) == "Layers: Count in row 1 must be a number."


def test_row_number_is_reported() -> None:
    # The offending row's 1-based index appears in the message.
    assert "row 2" in _first_error([{"n": 3, "thk": 1.0}, {"n": 99, "thk": 1.0}])

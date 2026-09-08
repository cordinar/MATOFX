"""Install-time validation of §F30 cross-field constraints."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from matofx.tools import CrossFieldConstraint, InputField, ToolDescriptor


def _desc(inputs: list[InputField], constraints: list[Any]) -> ToolDescriptor:
    return ToolDescriptor(
        alias="demo",
        name="Demo",
        version="1.0.0",
        description="Demo tool.",
        main_script="static/main.py",
        inputs=inputs,
        cross_field_constraints=constraints,
    )


def _num(name: str) -> InputField:
    return InputField(name=name, label=name.upper(), type="integer", required=False)


def test_valid_constraint_round_trips() -> None:
    d = _desc(
        [_num("a"), _num("b")],
        [
            CrossFieldConstraint(
                expression="a + b == 100",
                error_message="A and B must add up to 100.",
            )
        ],
    )
    assert ToolDescriptor.model_validate(d.model_dump()) == d


def test_bad_grammar_rejected() -> None:
    with pytest.raises(ValidationError, match="expression is invalid"):
        _desc(
            [_num("a")],
            [CrossFieldConstraint(expression="a.attr == 1", error_message="no")],
        )


def test_non_sibling_reference_rejected() -> None:
    with pytest.raises(ValidationError, match="not an input of this tool"):
        _desc(
            [_num("a")],
            [CrossFieldConstraint(expression="a <= ghost", error_message="bad")],
        )


def test_empty_message_rejected() -> None:
    with pytest.raises(ValidationError):
        CrossFieldConstraint(expression="a == 1", error_message="")

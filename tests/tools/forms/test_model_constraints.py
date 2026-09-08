"""Submit-time evaluation of §F30 cross-field constraints."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from matofx.tools import (
    CrossFieldConstraint,
    Dependency,
    HideEffect,
    InputField,
    SetValueEffect,
    ToolDescriptor,
)
from matofx.tools.forms import FormValidationError, clear_cache, validate_inputs


@pytest.fixture(autouse=True)
def _isolate_cache() -> Iterator[None]:
    clear_cache()
    yield
    clear_cache()


def _desc(
    inputs: list[InputField],
    constraints: list[CrossFieldConstraint],
    deps: list[Dependency] | None = None,
) -> ToolDescriptor:
    return ToolDescriptor(
        alias="cf",
        name="CF",
        version="1.0.0",
        description="Constraint tool.",
        main_script="m.py",
        inputs=inputs,
        cross_field_constraints=constraints,
        dependencies=deps or [],
    )


def _num(name: str, **kw: Any) -> InputField:
    kw.setdefault("required", False)
    return InputField(name=name, label=name.upper(), type="integer", **kw)


def test_constraint_pass_returns_values() -> None:
    desc = _desc(
        [_num("a"), _num("b")],
        [CrossFieldConstraint(expression="a <= b", error_message="A must be ≤ B.")],
    )
    assert validate_inputs(desc, {"a": 1, "b": 5}) == {"a": 1, "b": 5}


def test_constraint_fail_uses_custom_message_verbatim() -> None:
    desc = _desc(
        [_num("a"), _num("b")],
        [
            CrossFieldConstraint(
                expression="a <= b",
                error_message="Operating temperature can't exceed the max.",
            )
        ],
    )
    with pytest.raises(FormValidationError) as ei:
        validate_inputs(desc, {"a": 9, "b": 1})
    (fe,) = ei.value.field_errors
    assert fe.message == "Operating temperature can't exceed the max."
    # Raw expression is never leaked to the user (§F30 clarifier 3).
    assert "a <= b" not in fe.message


def test_failure_anchors_first_participant_and_lists_all() -> None:
    desc = _desc(
        [_num("a"), _num("b"), _num("c")],
        [
            CrossFieldConstraint(
                expression="a + b + c == 100",
                error_message="A, B and C must total 100.",
            )
        ],
    )
    with pytest.raises(FormValidationError) as ei:
        validate_inputs(desc, {"a": 10, "b": 20, "c": 5})
    (fe,) = ei.value.field_errors
    assert fe.name == "a"  # first sorted participant — deterministic anchor
    assert fe.sum_parameters == ("a", "b", "c")


def test_multiple_constraints_all_reported() -> None:
    desc = _desc(
        [_num("a"), _num("b")],
        [
            CrossFieldConstraint(expression="a > 0", error_message="A must be > 0."),
            CrossFieldConstraint(expression="b > 0", error_message="B must be > 0."),
        ],
    )
    with pytest.raises(FormValidationError) as ei:
        validate_inputs(desc, {"a": -1, "b": -2})
    assert {fe.message for fe in ei.value.field_errors} == {
        "A must be > 0.",
        "B must be > 0.",
    }


def test_skipped_when_participant_unfilled() -> None:
    desc = _desc(
        [_num("a"), _num("b")],
        [CrossFieldConstraint(expression="a <= b", error_message="A ≤ B.")],
    )
    # b unfilled (None) → constraint can't be judged → skipped, no nag.
    assert validate_inputs(desc, {"a": 5}) == {"a": 5, "b": None}


def test_skipped_when_participant_hidden_dropped() -> None:
    desc = _desc(
        [_num("a"), _num("b")],
        [CrossFieldConstraint(expression="a <= b", error_message="A ≤ B.")],
        deps=[Dependency(when="a == 9", effects=[HideEffect(fields=["b"])])],
    )
    # a=9 hides+drops b → b absent from resolved values → constraint
    # skipped even though 9 <= None would otherwise be a problem.
    out = validate_inputs(desc, {"a": 9, "b": 1})
    assert "b" not in out


def test_evaluated_against_forced_value() -> None:
    desc = _desc(
        [_num("a"), _num("b")],
        [CrossFieldConstraint(expression="b <= 50", error_message="B too big.")],
        deps=[Dependency(when="a == 1", effects=[SetValueEffect(field="b", value=99)])],
    )
    # b forced to 99 by the rule → constraint sees the resolved value.
    with pytest.raises(FormValidationError) as ei:
        validate_inputs(desc, {"a": 1, "b": 0})
    assert ei.value.field_errors[0].message == "B too big."


def test_no_constraints_is_unchanged() -> None:
    desc = _desc([_num("a")], [])
    assert validate_inputs(desc, {"a": 3}) == {"a": 3}

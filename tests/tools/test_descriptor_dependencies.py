"""Install-time validation of §16.16 conditional dependencies."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from matofx.tools import (
    Dependency,
    DisableEffect,
    InputField,
    SetMinMaxEffect,
    SetOptionsEffect,
    SetValueEffect,
    ShowEffect,
    ToolDescriptor,
)


def _desc(inputs: list[InputField], deps: list[Any]) -> ToolDescriptor:
    return ToolDescriptor(
        alias="demo",
        name="Demo",
        version="1.0.0",
        description="Demo tool.",
        main_script="static/main.py",
        inputs=inputs,
        dependencies=deps,
    )


def _num(name: str, **kw: Any) -> InputField:
    return InputField(name=name, label=name.upper(), type="integer", required=False, **kw)


# --- Happy path -----------------------------------------------------------


def test_valid_dependency_round_trips() -> None:
    d = _desc(
        [_num("a"), _num("b"), _num("c")],
        [
            Dependency(when="a == 1", effects=[SetValueEffect(field="b", value=2)]),
            Dependency(
                when="a == 1 and b == 2",
                effects=[SetValueEffect(field="c", value=3)],
            ),
        ],
    )
    assert ToolDescriptor.model_validate(d.model_dump()) == d


# --- when expression lint -------------------------------------------------


def test_when_unknown_name_rejected() -> None:
    with pytest.raises(ValidationError, match="not an input of this tool"):
        _desc(
            [_num("a")],
            [Dependency(when="ghost == 1", effects=[ShowEffect(fields=["a"])])],
        )


def test_when_bad_grammar_rejected() -> None:
    with pytest.raises(ValidationError, match="'when' is invalid"):
        _desc(
            [_num("a")],
            [Dependency(when="a.attr == 1", effects=[ShowEffect(fields=["a"])])],
        )


def test_when_disallowed_call_rejected() -> None:
    with pytest.raises(ValidationError):
        _desc(
            [_num("a")],
            [Dependency(when="open('x')", effects=[ShowEffect(fields=["a"])])],
        )


# --- Effect target checks -------------------------------------------------


def test_effect_targets_unknown_input_rejected() -> None:
    with pytest.raises(ValidationError, match="not an input of this tool"):
        _desc(
            [_num("a")],
            [Dependency(when="a == 1", effects=[ShowEffect(fields=["nope"])])],
        )


def test_set_options_on_non_select_rejected() -> None:
    with pytest.raises(ValidationError, match="only select / multiselect"):
        _desc(
            [_num("a"), _num("b")],
            [
                Dependency(
                    when="a == 1",
                    effects=[SetOptionsEffect(field="b", options=["x", "y"])],
                )
            ],
        )


def test_set_min_max_on_non_numeric_rejected() -> None:
    with pytest.raises(ValidationError, match="only integer / float"):
        _desc(
            [
                _num("a"),
                InputField(name="s", label="S", type="string", required=False),
            ],
            [
                Dependency(
                    when="a == 1",
                    effects=[SetMinMaxEffect(field="s", minimum=1)],
                )
            ],
        )


def test_set_value_type_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="whole number for an integer"):
        _desc(
            [_num("a"), _num("b")],
            [
                Dependency(
                    when="a == 1",
                    effects=[SetValueEffect(field="b", value="not-an-int")],
                )
            ],
        )


def test_set_value_type_match_accepted() -> None:
    _desc(
        [_num("a"), _num("b")],
        [Dependency(when="a == 1", effects=[SetValueEffect(field="b", value=4)])],
    )


# --- Effect-level invariants ---------------------------------------------


def test_set_min_max_requires_a_bound() -> None:
    with pytest.raises(ValidationError, match="at least one of minimum"):
        SetMinMaxEffect(field="x")


def test_set_min_max_min_above_max_rejected() -> None:
    with pytest.raises(ValidationError, match="above maximum"):
        SetMinMaxEffect(field="x", minimum=10, maximum=1)


# --- Cycle rejection ------------------------------------------------------


def test_cyclic_rule_graph_rejected() -> None:
    with pytest.raises(ValidationError, match="form a cycle"):
        _desc(
            [_num("x"), _num("y")],
            [
                Dependency(when="y == 1", effects=[SetValueEffect(field="x", value=1)]),
                Dependency(when="x == 1", effects=[SetValueEffect(field="y", value=1)]),
            ],
        )


def test_self_referential_rule_rejected() -> None:
    with pytest.raises(ValidationError, match="form a cycle"):
        _desc(
            [_num("x")],
            [Dependency(when="x == 1", effects=[SetValueEffect(field="x", value=2)])],
        )


def test_long_acyclic_chain_accepted() -> None:
    # a → b → c → d is a DAG; must not be flagged as a cycle.
    _desc(
        [_num("a"), _num("b"), _num("c"), _num("d")],
        [
            Dependency(when="a == 1", effects=[SetValueEffect(field="b", value=1)]),
            Dependency(when="b == 1", effects=[SetValueEffect(field="c", value=1)]),
            Dependency(when="c == 1", effects=[DisableEffect(field="d")]),
        ],
    )

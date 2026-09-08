"""Submit-time validation under §16.16 conditional dependencies.

These exercise the relaxed-model + resolver post-pass path
(`descriptor.dependencies` non-empty). The strict no-deps path is
covered by ``test_model.py`` and must stay byte-identical — a couple
of cases here pin that it still is.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from matofx.tools import (
    Dependency,
    DisableEffect,
    HideEffect,
    InputField,
    SetMinMaxEffect,
    SetOptionsEffect,
    SetRequiredEffect,
    SetValueEffect,
    ToolDescriptor,
)
from matofx.tools.forms import FormValidationError, clear_cache, validate_inputs


@pytest.fixture(autouse=True)
def _isolate_cache() -> Iterator[None]:
    clear_cache()
    yield
    clear_cache()


def _desc(inputs: list[InputField], deps: list[Dependency]) -> ToolDescriptor:
    return ToolDescriptor(
        alias="dep",
        name="Dep",
        version="1.0.0",
        description="Conditional tool.",
        main_script="m.py",
        inputs=inputs,
        dependencies=deps,
    )


def _num(name: str, **kw: Any) -> InputField:
    kw.setdefault("required", False)
    return InputField(name=name, label=name.upper(), type="integer", **kw)


def _messages(exc: FormValidationError) -> list[str]:
    return [fe.message for fe in exc.field_errors]


# --- Hidden value-state matrix -------------------------------------------


def test_hidden_drop_omits_and_skips_required() -> None:
    desc = _desc(
        [_num("a"), _num("b", required=True)],
        [Dependency(when="a == 0", effects=[HideEffect(fields=["b"])])],
    )
    # b is required but hidden+drop → not required-checked, omitted.
    out = validate_inputs(desc, {"a": 0})
    assert "b" not in out
    assert out == {"a": 0}


def test_hidden_keep_default_writes_default() -> None:
    desc = _desc(
        [
            _num("a"),
            InputField(name="b", label="B", type="integer", required=False, default=42),
        ],
        [
            Dependency(
                when="a == 0",
                effects=[HideEffect(fields=["b"], on_hide="keep_default")],
            )
        ],
    )
    out = validate_inputs(desc, {"a": 0, "b": 9})
    assert out["b"] == 42


def test_hidden_keep_last_preserves_submitted() -> None:
    desc = _desc(
        [_num("a"), _num("b")],
        [
            Dependency(
                when="a == 0",
                effects=[HideEffect(fields=["b"], on_hide="keep_last")],
            )
        ],
    )
    out = validate_inputs(desc, {"a": 0, "b": 7})
    assert out["b"] == 7


# --- set_value force ------------------------------------------------------


def test_set_value_overrides_user_input_and_skips_required() -> None:
    desc = _desc(
        [_num("a"), _num("b", required=True)],
        [
            Dependency(
                when="a == 1",
                effects=[SetValueEffect(field="b", value=2), DisableEffect(field="b")],
            )
        ],
    )
    # User sent b=999; rule forces 2. No "b required" even though b had
    # no value of its own.
    out = validate_inputs(desc, {"a": 1, "b": 999})
    assert out["b"] == 2


# --- Effective required / options / bounds -------------------------------


def test_effective_required_rule_makes_field_mandatory() -> None:
    desc = _desc(
        [_num("a"), _num("b", required=False)],
        [
            Dependency(
                when="a == 1",
                effects=[SetRequiredEffect(field="b", required=True)],
            )
        ],
    )
    with pytest.raises(FormValidationError) as ei:
        validate_inputs(desc, {"a": 1})
    assert _messages(ei.value) == ["B is required."]
    # Rule not triggered → b stays optional.
    assert validate_inputs(desc, {"a": 0}) == {"a": 0, "b": None}


def test_effective_options_membership() -> None:
    desc = _desc(
        [
            _num("premium"),
            InputField(
                name="finish",
                label="Finish",
                type="select",
                required=False,
                options=["raw", "matte"],
            ),
        ],
        [
            Dependency(
                when="premium == 1",
                effects=[SetOptionsEffect(field="finish", options=["anodised", "painted"])],
            )
        ],
    )
    # Value valid under the rule-changed options.
    assert validate_inputs(desc, {"premium": 1, "finish": "anodised"})["finish"] == ("anodised")
    # "raw" was valid in the declared options but not the effective set.
    with pytest.raises(FormValidationError) as ei:
        validate_inputs(desc, {"premium": 1, "finish": "raw"})
    assert "must be one of" in _messages(ei.value)[0]


def test_effective_bounds_from_set_min_max() -> None:
    desc = _desc(
        [_num("mode"), _num("speed", minimum=0, maximum=100)],
        [
            Dependency(
                when="mode == 1",
                effects=[SetMinMaxEffect(field="speed", minimum=10)],
            )
        ],
    )
    with pytest.raises(FormValidationError) as ei:
        validate_inputs(desc, {"mode": 1, "speed": 5})
    assert _messages(ei.value) == ["SPEED can't be less than 10."]
    assert validate_inputs(desc, {"mode": 1, "speed": 50})["speed"] == 50


# --- Relaxed model still type-checks; default membership still enforced ---


def test_relaxed_model_still_reports_type_errors() -> None:
    desc = _desc(
        [_num("a"), _num("b")],
        [Dependency(when="a == 1", effects=[DisableEffect(field="b")])],
    )
    with pytest.raises(FormValidationError) as ei:
        validate_inputs(desc, {"a": "not-a-number"})
    assert _messages(ei.value) == ["A must be a whole number."]


def test_declared_options_enforced_when_no_rule_touches_them() -> None:
    desc = _desc(
        [
            _num("a"),
            InputField(
                name="finish",
                label="Finish",
                type="select",
                required=False,
                options=["raw", "matte"],
            ),
        ],
        [Dependency(when="a == 1", effects=[DisableEffect(field="finish")])],
    )
    with pytest.raises(FormValidationError):
        validate_inputs(desc, {"a": 0, "finish": "chartreuse"})


# --- Sum-check interaction ------------------------------------------------


def test_sum_check_skipped_when_participant_hidden_dropped() -> None:
    desc = _desc(
        [
            _num("a"),
            InputField(
                name="p1",
                label="P1",
                type="float",
                required=False,
                sum_check={"value": 100, "sum_parameters": ["p1", "p2"]},
            ),
            InputField(name="p2", label="P2", type="float", required=False),
        ],
        [Dependency(when="a == 0", effects=[HideEffect(fields=["p2"])])],
    )
    # p2 hidden+drop → the p1+p2==100 constraint loses a participant and
    # is skipped, so p1=30 alone does not fail.
    out = validate_inputs(desc, {"a": 0, "p1": 30})
    assert "p2" not in out
    assert out["p1"] == 30

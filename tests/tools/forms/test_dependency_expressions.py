"""Expression-driven effects + ``require`` validity rules (§16.16).

Covers the unified per-input dependency model that lets an effect's
value/bound be a formula over other inputs (e.g. *B's minimum is A*) and
adds a ``require`` (must-be-true-or-block) outcome — both honoured in the
resolver/submit path, the studies generator, and the optimiser compiler.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from matofx.tools import (
    Dependency,
    InputField,
    RequireEffect,
    SetDefaultEffect,
    SetMinMaxEffect,
    SetValueEffect,
    ToolDescriptor,
)
from matofx.tools.forms import (
    FormValidationError,
    clear_cache,
    resolve,
    resolve_for_generation,
    selection_constraint_exprs,
    validate_inputs,
)


@pytest.fixture(autouse=True)
def _isolate_cache() -> Iterator[None]:
    clear_cache()
    yield
    clear_cache()


def _num(name: str, label: str, **kw: Any) -> InputField:
    kw.setdefault("required", False)
    kw.setdefault("minimum", 0.0)
    kw.setdefault("maximum", 100.0)
    return InputField(name=name, label=label, type="float", **kw)


def _desc(deps: list[Dependency], inputs: list[InputField] | None = None) -> ToolDescriptor:
    return ToolDescriptor(
        alias="demo",
        name="Demo",
        version="1.0.0",
        description="Demo tool.",
        main_script="m.py",
        inputs=inputs or [_num("a", "Inner"), _num("b", "Outer")],
        dependencies=deps,
    )


def _messages(exc: FormValidationError) -> list[str]:
    return [fe.message for fe in exc.field_errors]


# --- Formula-driven bounds (the "B's minimum is A" case) ------------------


def test_formula_minimum_resolves_to_other_input() -> None:
    desc = _desc([Dependency(when="True", effects=[SetMinMaxEffect(field="b", minimum_expr="a")])])
    assert resolve(desc, {"a": 10, "b": 5}).fields["b"].minimum == 10.0


def test_formula_minimum_blocks_submit_below_it() -> None:
    desc = _desc([Dependency(when="True", effects=[SetMinMaxEffect(field="b", minimum_expr="a")])])
    with pytest.raises(FormValidationError) as exc:
        validate_inputs(desc, {"a": 10.0, "b": 5.0})
    assert "Outer" in _messages(exc.value)[0]
    assert validate_inputs(desc, {"a": 10.0, "b": 20.0}) == {"a": 10.0, "b": 20.0}


def test_formula_minimum_with_offset_for_strictly_greater() -> None:
    desc = _desc(
        [Dependency(when="True", effects=[SetMinMaxEffect(field="b", minimum_expr="a + 1")])]
    )
    assert resolve(desc, {"a": 10, "b": 11}).fields["b"].minimum == 11.0


def test_unevaluable_formula_bound_is_skipped() -> None:
    # ``a`` unfilled → the bound can't compute, so it simply isn't applied
    # this pass (the rule bites once ``a`` is well-typed).
    desc = _desc([Dependency(when="True", effects=[SetMinMaxEffect(field="b", minimum_expr="a")])])
    assert resolve(desc, {"a": None, "b": 5}).fields["b"].minimum == 0.0


def test_conditional_formula_bound_only_when_gate_holds() -> None:
    desc = _desc(
        [
            Dependency(
                when="a > 50",
                effects=[SetMinMaxEffect(field="b", minimum_expr="a")],
            )
        ]
    )
    assert resolve(desc, {"a": 10, "b": 5}).fields["b"].minimum == 0.0  # gate off
    assert resolve(desc, {"a": 60, "b": 5}).fields["b"].minimum == 60.0  # gate on


# --- Formula-driven values (set_value / set_default) ----------------------


def test_set_value_formula_forces_at_submit() -> None:
    desc = _desc([Dependency(when="True", effects=[SetValueEffect(field="b", value_expr="a * 2")])])
    assert validate_inputs(desc, {"a": 5.0, "b": 999.0}) == {"a": 5.0, "b": 10.0}


def test_set_default_formula_resolves() -> None:
    desc = _desc(
        [Dependency(when="True", effects=[SetDefaultEffect(field="b", value_expr="a + 3")])]
    )
    assert resolve(desc, {"a": 7}).fields["b"].effective_default == 10.0


# --- require (validity) outcome -------------------------------------------


def test_require_blocks_with_auto_message() -> None:
    desc = _desc([Dependency(when="True", effects=[RequireEffect(field="b", expr="b > a")])])
    with pytest.raises(FormValidationError) as exc:
        validate_inputs(desc, {"a": 10.0, "b": 5.0})
    # Auto message uses labels (Inner/Outer), not raw names (a/b).
    assert _messages(exc.value)[0] == "Not allowed: Outer > Inner must be true."
    assert validate_inputs(desc, {"a": 10.0, "b": 20.0}) == {"a": 10.0, "b": 20.0}


def test_require_uses_author_message_when_given() -> None:
    desc = _desc(
        [
            Dependency(
                when="True",
                effects=[
                    RequireEffect(field="b", expr="b > a", message="Outer must exceed inner.")
                ],
            )
        ]
    )
    with pytest.raises(FormValidationError) as exc:
        validate_inputs(desc, {"a": 10.0, "b": 5.0})
    assert _messages(exc.value)[0] == "Outer must exceed inner."


def test_require_gated_by_when() -> None:
    desc = _desc(
        [Dependency(when="a > 50", effects=[RequireEffect(field="b", expr="b > a")])]
    )
    # Gate off → no requirement.
    assert validate_inputs(desc, {"a": 10.0, "b": 5.0}) == {"a": 10.0, "b": 5.0}
    # Gate on → enforced.
    with pytest.raises(FormValidationError):
        validate_inputs(desc, {"a": 60.0, "b": 5.0})


# --- Studies generation path ---------------------------------------------


def test_resolve_for_generation_drops_and_keeps() -> None:
    desc = _desc([Dependency(when="True", effects=[SetMinMaxEffect(field="b", minimum_expr="a")])])
    _, errors = resolve_for_generation(desc, {"a": 10.0, "b": 5.0})
    assert errors
    assert "Outer" in errors[0].message
    point2, errors2 = resolve_for_generation(desc, {"a": 10.0, "b": 20.0})
    assert not errors2
    assert point2 == {"a": 10.0, "b": 20.0}


def test_resolve_for_generation_applies_forces() -> None:
    desc = _desc([Dependency(when="True", effects=[SetValueEffect(field="b", value_expr="a * 2")])])
    point, errors = resolve_for_generation(desc, {"a": 5.0, "b": 999.0})
    assert not errors
    assert point["b"] == 10.0


# --- Optimiser compiler ---------------------------------------------------


def test_selection_constraint_exprs_compiles_bounds_and_require() -> None:
    desc = _desc(
        [
            Dependency(when="True", effects=[SetMinMaxEffect(field="b", minimum_expr="a")]),
            Dependency(when="mode == 'hp'", effects=[RequireEffect(field="b", expr="b > a")]),
        ],
        inputs=[
            _num("a", "Inner"),
            _num("b", "Outer"),
            InputField(
                name="mode",
                label="Mode",
                type="select",
                options=["hp", "lp"],
                required=False,
            ),
        ],
    )
    compiled = dict(selection_constraint_exprs(desc))
    assert "(not (True)) or (b >= (a))" in compiled
    assert "(not (mode == 'hp')) or (b > a)" in compiled


# --- Install-time lint ----------------------------------------------------


def test_formula_referencing_unknown_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="not an input"):
        _desc([Dependency(when="True", effects=[SetMinMaxEffect(field="b", minimum_expr="ghost")])])


def test_value_and_formula_both_set_is_rejected() -> None:
    with pytest.raises(ValueError, match="not both"):
        SetValueEffect(field="b", value=1.0, value_expr="a")


def test_formula_value_cycle_is_rejected() -> None:
    # b's value reads a, a's value reads b → a genuine value loop.
    with pytest.raises(ValueError, match="cycle"):
        _desc(
            [
                Dependency(when="True", effects=[SetValueEffect(field="b", value_expr="a")]),
                Dependency(when="True", effects=[SetValueEffect(field="a", value_expr="b")]),
            ]
        )

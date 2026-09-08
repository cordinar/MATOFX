"""Tests for the §16.16 conditional-dependency resolver."""

from __future__ import annotations

from typing import Any

from matofx.tools import (
    Dependency,
    DisableEffect,
    HideEffect,
    InputField,
    SetDefaultEffect,
    SetMinMaxEffect,
    SetOptionsEffect,
    SetRequiredEffect,
    SetValueEffect,
    ShowEffect,
    ToolDescriptor,
)
from matofx.tools.forms import resolve


def _desc(inputs: list[InputField], deps: list[Dependency]) -> ToolDescriptor:
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
    kw.setdefault("required", False)
    return InputField(name=name, label=name.upper(), type="integer", **kw)


# --- Chaining -------------------------------------------------------------


def test_chain_a_sets_b_then_b_drives_c() -> None:
    desc = _desc(
        [_num("a"), _num("b"), _num("c")],
        [
            Dependency(
                when="a == 1",
                effects=[SetValueEffect(field="b", value=2), DisableEffect(field="b")],
            ),
            Dependency(
                when="a == 1 and b == 2",
                effects=[SetValueEffect(field="c", value=3)],
            ),
        ],
    )
    ef = resolve(desc, {"a": 1})
    b = ef.for_field("b")
    c = ef.for_field("c")
    assert b is not None
    assert b.forced is True
    assert b.forced_value == 2
    assert b.disabled is True
    assert c is not None
    assert c.forced is True
    assert c.forced_value == 3
    assert ef.resolved_values["b"] == 2
    assert ef.resolved_values["c"] == 3


def test_chain_does_not_fire_when_condition_false() -> None:
    desc = _desc(
        [_num("a"), _num("b"), _num("c")],
        [
            Dependency(when="a == 1", effects=[SetValueEffect(field="b", value=2)]),
            Dependency(when="a == 1 and b == 2", effects=[SetValueEffect(field="c", value=3)]),
        ],
    )
    ef = resolve(desc, {"a": 0})
    assert ef.for_field("b").forced is False  # type: ignore[union-attr]
    assert ef.for_field("c").forced is False  # type: ignore[union-attr]


def test_chain_resolves_regardless_of_declaration_order() -> None:
    # Rule that *reads* b is declared before the rule that *writes* b;
    # the topological order still settles b before evaluating c.
    desc = _desc(
        [_num("a"), _num("b"), _num("c")],
        [
            Dependency(when="b == 2", effects=[SetValueEffect(field="c", value=3)]),
            Dependency(when="a == 1", effects=[SetValueEffect(field="b", value=2)]),
        ],
    )
    ef = resolve(desc, {"a": 1})
    assert ef.for_field("c").forced_value == 3  # type: ignore[union-attr]


# --- Last-wins ------------------------------------------------------------


def test_last_declared_rule_wins_on_conflict() -> None:
    desc = _desc(
        [_num("a"), _num("e")],
        [
            Dependency(when="a == 1", effects=[SetValueEffect(field="e", value=2)]),
            Dependency(when="a == 1", effects=[SetValueEffect(field="e", value=5)]),
        ],
    )
    ef = resolve(desc, {"a": 1})
    assert ef.for_field("e").forced_value == 5  # type: ignore[union-attr]


def test_later_show_overrides_earlier_hide() -> None:
    desc = _desc(
        [_num("a"), _num("b")],
        [
            Dependency(when="a == 1", effects=[HideEffect(fields=["b"])]),
            Dependency(when="a == 1", effects=[ShowEffect(fields=["b"])]),
        ],
    )
    ef = resolve(desc, {"a": 1})
    assert ef.for_field("b").visible is True  # type: ignore[union-attr]


# --- Individual effects ---------------------------------------------------


def test_hide_records_on_hide_policy() -> None:
    desc = _desc(
        [_num("a"), _num("b")],
        [
            Dependency(
                when="a == 1",
                effects=[HideEffect(fields=["b"], on_hide="keep_default")],
            )
        ],
    )
    ef = resolve(desc, {"a": 1})
    b = ef.for_field("b")
    assert b is not None
    assert b.visible is False
    assert b.on_hide == "keep_default"


def test_set_options_replaces_choices() -> None:
    desc = _desc(
        [
            _num("grade_is_premium"),
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
                when="grade_is_premium == 1",
                effects=[SetOptionsEffect(field="finish", options=["anodised", "painted"])],
            )
        ],
    )
    ef = resolve(desc, {"grade_is_premium": 1})
    assert ef.for_field("finish").options == ["anodised", "painted"]  # type: ignore[union-attr]
    # Untriggered → declared options stand.
    ef0 = resolve(desc, {"grade_is_premium": 0})
    assert ef0.for_field("finish").options == ["raw", "matte"]  # type: ignore[union-attr]


def test_set_min_max_overrides_only_given_bound() -> None:
    desc = _desc(
        [_num("mode"), _num("speed", minimum=0, maximum=10)],
        [
            Dependency(
                when="mode == 1",
                effects=[SetMinMaxEffect(field="speed", minimum=5)],
            )
        ],
    )
    ef = resolve(desc, {"mode": 1})
    speed = ef.for_field("speed")
    assert speed is not None
    assert speed.minimum == 5
    assert speed.maximum == 10


def test_set_required_and_set_default() -> None:
    desc = _desc(
        [_num("a"), _num("b", required=False)],
        [
            Dependency(
                when="a == 1",
                effects=[
                    SetRequiredEffect(field="b", required=True),
                    SetDefaultEffect(field="b", value=7),
                ],
            )
        ],
    )
    ef = resolve(desc, {"a": 1})
    b = ef.for_field("b")
    assert b is not None
    assert b.required is True
    assert b.effective_default == 7


def test_no_dependencies_is_identity() -> None:
    desc = _desc([_num("a", required=True)], [])
    ef = resolve(desc, {"a": 4})
    a = ef.for_field("a")
    assert a is not None
    assert a.visible
    assert a.required
    assert not a.disabled
    assert ef.resolved_values == {"a": 4}


def test_missing_values_seeded_from_default() -> None:
    desc = _desc(
        [InputField(name="a", label="A", type="integer", required=False, default=1)],
        [Dependency(when="a == 1", effects=[ShowEffect(fields=["a"])])],
    )
    # `a` not supplied; seeded from default 1 so the rule still fires.
    ef = resolve(desc, {})
    assert ef.resolved_values["a"] == 1
    assert ef.for_field("a").visible is True  # type: ignore[union-attr]

"""Live §16.16 reactivity in the rendered form.

Drives the resolver-backed re-resolve deterministically via
``FormHandle.refresh()`` (the same callback the live ``on_value_change``
fires) and inspects the resulting widget/container state.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from nicegui import Client
from nicegui.page import page

from matofx.tools import (
    Dependency,
    DisableEffect,
    HideEffect,
    InputField,
    SetOptionsEffect,
    SetValueEffect,
    ToolDescriptor,
)
from matofx.tools.forms import render_form


@pytest.fixture
def nicegui_client() -> Iterator[Client]:
    client = Client(page("/"))
    with client:
        yield client


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


# --- Rule-free tools are untouched ---------------------------------------


def test_rule_free_tool_has_no_reapply(nicegui_client: Client) -> None:  # noqa: ARG001
    d = _desc([_num("a")], [])
    handle = render_form(d)
    assert handle._reapply is None
    handle.refresh()  # no-op, must not raise
    assert set(handle.field_containers) == {"a"}
    assert handle.auto_notes["a"].visible is False


# --- Initial resolve + show/hide -----------------------------------------


def test_initial_resolve_hides_then_shows(nicegui_client: Client) -> None:  # noqa: ARG001
    d = _desc(
        [_num("mode", default=0), _num("extra")],
        [Dependency(when="mode == 0", effects=[HideEffect(fields=["extra"])])],
    )
    handle = render_form(d)
    # mode seeds to 0 → rule fires on the initial pass.
    assert handle.field_containers["extra"].visible is False

    handle.state["mode"] = 1
    handle.refresh()
    assert handle.field_containers["extra"].visible is True


# --- set_value + disable --------------------------------------------------


def test_set_value_and_disable_forces_and_locks(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    d = _desc(
        [_num("a"), _num("b")],
        [
            Dependency(
                when="a == 1",
                effects=[SetValueEffect(field="b", value=2), DisableEffect(field="b")],
            )
        ],
    )
    handle = render_form(d)
    handle.state["a"] = 1
    handle.refresh()

    assert handle.state["b"] == 2
    assert handle.widgets["b"].value == 2
    assert handle.widgets["b"].enabled is False
    note = handle.auto_notes["b"]
    assert note.visible is True
    assert note.text == "Set automatically based on your other answers."


def test_disable_only_sets_locked_note(nicegui_client: Client) -> None:  # noqa: ARG001
    d = _desc(
        [_num("a"), _num("b")],
        [Dependency(when="a == 1", effects=[DisableEffect(field="b")])],
    )
    handle = render_form(d)
    handle.state["a"] = 1
    handle.refresh()

    assert handle.widgets["b"].enabled is False
    assert handle.auto_notes["b"].text == "Locked based on your other answers."


# --- set_options ----------------------------------------------------------


def test_set_options_repopulates_and_clears_stale(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    d = _desc(
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
    handle = render_form(d)
    handle.state["finish"] = "raw"
    handle.state["premium"] = 1
    handle.refresh()

    assert handle.widgets["finish"].options == ["anodised", "painted"]
    # "raw" is not in the new set → cleared to unselected.
    assert handle.state["finish"] is None
    assert handle.auto_notes["finish"].text == ("Options updated based on your other answers.")


# --- seed re-resolves -----------------------------------------------------


def test_seed_triggers_resolve(nicegui_client: Client) -> None:  # noqa: ARG001
    d = _desc(
        [_num("a"), _num("b")],
        [Dependency(when="a == 1", effects=[HideEffect(fields=["b"])])],
    )
    handle = render_form(d)
    assert handle.field_containers["b"].visible is True

    handle.seed({"a": 1})
    assert handle.state["a"] == 1
    assert handle.field_containers["b"].visible is False

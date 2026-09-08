"""Live cross-field validation + field locking in the rendered form.

Two behaviours land here, both exercised through the same headless
``Client(page('/'))`` fixture the other render tests use:

1. **Live cross-field validation** — a sum-check (e.g. component
   percentages totalling 100) or a §F30 ``cross_field_constraints``
   rule flags inline *as the values are entered*, not only on Submit.
   The live pass is driven deterministically via
   ``FormHandle.validate_cross_field()`` — the very callback the live
   ``on_value_change`` fires — and via ``seed()``.

2. **Field locking** — ``FormHandle.lock(...)`` disables the named
   widgets (workflow ``downstream_input`` fields fed from an upstream
   tool) and keeps them disabled even when a §16.16 re-resolve would
   otherwise enable them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from nicegui import Client
from nicegui.page import page

from matofx.tools import Dependency, HideEffect
from matofx.tools.descriptor import (
    CrossFieldConstraint,
    InputField,
    SumCheck,
    ToolDescriptor,
)
from matofx.tools.forms import FieldError, FormValidationError, render_form


def _descriptor(**fields: Any) -> ToolDescriptor:
    base: dict[str, Any] = {
        "alias": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "description": "Demo.",
        "main_script": "main.py",
    }
    base.update(fields)
    return ToolDescriptor(**base)


def _percent(name: str, *, sum_check: SumCheck | None = None) -> InputField:
    return InputField(
        name=name,
        label=name.upper(),
        type="float",
        unit="%",
        required=False,
        sum_check=sum_check,
    )


def _three_percent_descriptor() -> ToolDescriptor:
    """Mirror the Material Properties Calculator: three percents → 100."""
    constraint = SumCheck(value=100.0, sum_parameters=["a", "b", "c"])
    return _descriptor(
        inputs=[_percent("a", sum_check=constraint), _percent("b"), _percent("c")],
    )


@pytest.fixture
def nicegui_client() -> Iterator[Client]:
    client = Client(page("/"))
    with client:
        yield client


# --- Live sum-check -------------------------------------------------------


def test_live_sum_check_flags_every_participant_when_total_wrong(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    handle = render_form(_three_percent_descriptor())
    # 50 + 30 + 10 = 90 → constraint violated.
    handle.state.update({"a": 50, "b": 30, "c": 10})
    handle.validate_cross_field()
    for name in ("a", "b", "c"):
        assert handle.error_labels[name].visible is True
        assert "must add up to 100" in handle.error_labels[name].text


def test_live_sum_check_clears_when_total_becomes_correct(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    handle = render_form(_three_percent_descriptor())
    handle.state.update({"a": 50, "b": 30, "c": 10})
    handle.validate_cross_field()
    assert handle.error_labels["a"].visible is True

    # The user fixes the last field — 50 + 30 + 20 = 100.
    handle.state["c"] = 20
    handle.validate_cross_field()
    for name in ("a", "b", "c"):
        assert handle.error_labels[name].visible is False
        assert handle.error_labels[name].text == ""


def test_live_sum_check_skips_while_a_participant_is_unfilled(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # Don't-nag: an unfilled participant means the total can't be judged
    # yet, so nothing is flagged until the user has entered every part.
    handle = render_form(_three_percent_descriptor())
    handle.state.update({"a": 50, "b": 30, "c": None})
    handle.validate_cross_field()
    for name in ("a", "b", "c"):
        assert handle.error_labels[name].visible is False


def test_live_validation_leaves_non_participant_errors_intact(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # A per-field error surfaced by show_errors on a field that takes no
    # part in any cross-field rule must survive a later live pass.
    constraint = SumCheck(value=100.0, sum_parameters=["a", "b"])
    d = _descriptor(
        inputs=[
            _percent("a", sum_check=constraint),
            _percent("b"),
            InputField(name="other", label="Other", type="float", required=False),
        ],
    )
    handle = render_form(d)
    handle.show_errors(
        FormValidationError([FieldError("other", "Other", "Other is required.")])
    )
    handle.state.update({"a": 50, "b": 50})  # valid sum
    handle.validate_cross_field()
    assert handle.error_labels["other"].visible is True
    assert handle.error_labels["other"].text == "Other is required."


def test_seed_triggers_live_cross_field(nicegui_client: Client) -> None:  # noqa: ARG001
    # Loading an upstream / preset snapshot that doesn't satisfy the
    # constraint flags immediately, without waiting for a keystroke.
    handle = render_form(_three_percent_descriptor())
    handle.seed({"a": 50, "b": 30, "c": 10})
    for name in ("a", "b", "c"):
        assert handle.error_labels[name].visible is True


# --- Live §F30 cross-field constraint -------------------------------------


def test_live_cross_field_constraint_flags_and_clears(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    d = _descriptor(
        inputs=[
            InputField(name="lo", label="Lo", type="float", required=False),
            InputField(name="hi", label="Hi", type="float", required=False),
        ],
        cross_field_constraints=[
            CrossFieldConstraint(expression="lo <= hi", error_message="Lo must not exceed Hi."),
        ],
    )
    handle = render_form(d)
    handle.state.update({"lo": 10, "hi": 5})
    handle.validate_cross_field()
    for name in ("lo", "hi"):
        assert handle.error_labels[name].visible is True
        assert handle.error_labels[name].text == "Lo must not exceed Hi."

    handle.state["hi"] = 20
    handle.validate_cross_field()
    for name in ("lo", "hi"):
        assert handle.error_labels[name].visible is False


# --- Constraint-free tools are untouched ----------------------------------


def test_constraint_free_tool_has_no_live_runner(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    handle = render_form(_descriptor(inputs=[_percent("a")]))
    assert handle._run_cross_field is None
    handle.validate_cross_field()  # no-op, must not raise


# --- Field locking --------------------------------------------------------


def test_lock_disables_named_widgets_and_records_them(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    d = _descriptor(inputs=[_percent("a"), _percent("b")])
    handle = render_form(d)
    handle.lock(["a"])
    assert handle.locked_fields == {"a"}
    assert handle.widgets["a"].enabled is False
    assert handle.widgets["b"].enabled is True


def test_seed_updates_locked_field_value_but_keeps_it_disabled(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    handle = render_form(_descriptor(inputs=[_percent("a")]))
    handle.lock(["a"])
    handle.seed({"a": 42})
    assert handle.state["a"] == 42
    assert handle.widgets["a"].value == 42
    assert handle.widgets["a"].enabled is False


def test_locked_field_is_not_re_enabled_by_a_resolve(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # A rule-bearing tool wires the live re-resolve; a field that no rule
    # disables would normally resolve to "enabled" — the lock must win.
    d = _descriptor(
        inputs=[
            InputField(name="mode", label="Mode", type="integer", default=0, required=False),
            _percent("b"),
        ],
        dependencies=[Dependency(when="mode == 1", effects=[HideEffect(fields=["b"])])],
    )
    handle = render_form(d)
    handle.lock(["b"])
    assert handle.widgets["b"].enabled is False
    handle.refresh()  # resolve runs; no rule disables b
    assert handle.widgets["b"].enabled is False

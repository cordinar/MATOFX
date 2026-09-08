"""Parametric Studies names every factor the way the user knows it.

Descriptor *names* (``inlet_velocity_ms``) are an authoring detail; the
person filling in a study knows the *label* and unit (``Inlet velocity
(m/s)``). The factor list, the preview grid and the scatter axes already
went through :func:`_input_header`; the run-table columns and every
validation message did not, so a study built in Run-table mode showed
raw aliases as column headings and told a user who fumbled a range
``'inlet_velocity_ms': max must be greater than min.``

These tests hold the surfaces *against each other*: the run-table header
must say exactly what the factor list says, because a rule implemented
twice drifts, and a single-surface test cannot see a disagreement.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

from matofx.ui.pages.studies import (
    _build_design_vary,
    _build_file_list_vary,
    _build_grid_vary,
    _build_list_vary,
    _build_sampling_vary,
    _factor_label,
    _input_header,
    _rebuild_run_table,
    _render_factor_row,
)

_INP: dict[str, Any] = {
    "name": "inlet_velocity_ms",
    "label": "Inlet velocity",
    "unit": "m/s",
    "type": "float",
}


@pytest.fixture
def slot() -> Any:
    """A container the render functions can build into, with no browser."""

    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__studies_label_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


def _w(value: Any) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def _texts(node: Any) -> list[str]:
    """Every label/aria text in the built element tree."""
    out: list[str] = []
    for child in node.descendants():
        text = getattr(child, "text", None)
        if text:
            out.append(str(text))
        aria = child.props.get("aria-label")
        if aria:
            out.append(str(aria))
    return out


# ---- the helper -----------------------------------------------------


def test_factor_label_prefers_the_stashed_label() -> None:
    assert _factor_label("inlet_velocity_ms", {"label": "Inlet velocity (m/s)"}) == (
        "Inlet velocity (m/s)"
    )


def test_factor_label_falls_back_to_the_name() -> None:
    """Hand-built state dicts (the pure builder tests) still work."""
    assert _factor_label("inlet_velocity_ms", {}) == "inlet_velocity_ms"


# ---- the factor row stashes what the run table reads -----------------


def test_factor_row_stashes_exactly_what_the_factor_list_shows(slot: Any) -> None:
    """The two surfaces must not compute the label independently."""
    factors: dict[str, dict[str, Any]] = {}
    with slot:
        _render_factor_row(_INP, factors, "table", lambda: None, lambda: None)
    assert factors["inlet_velocity_ms"]["label"] == _input_header(_INP, "inlet_velocity_ms")
    assert factors["inlet_velocity_ms"]["label"] == "Inlet velocity (m/s)"


# ---- the run table ---------------------------------------------------


def test_run_table_heads_its_column_with_the_label(slot: Any) -> None:
    factors = {
        "inlet_velocity_ms": {
            "type": "numeric",
            "ftype": "float",
            "label": "Inlet velocity (m/s)",
            "role": "Vary",
        }
    }
    with slot:
        box = ui.column()
    _rebuild_run_table({}, factors, box, "table", lambda: None)
    texts = _texts(box)
    assert "Inlet velocity (m/s)" in texts
    assert "inlet_velocity_ms" not in texts


def test_run_table_cells_carry_the_label_for_screen_readers(slot: Any) -> None:
    """The visible label moved to the header; the accessible one must not."""
    factors = {
        "inlet_velocity_ms": {
            "type": "numeric",
            "ftype": "float",
            "label": "Inlet velocity (m/s)",
            "role": "Vary",
        }
    }
    ctx: dict[str, Any] = {}
    with slot:
        box = ui.column()
    _rebuild_run_table(ctx, factors, box, "table", lambda: None)
    # One starter row is added, and its cell is named.
    assert len(ctx["run_rows"]) == 1
    cell = ctx["run_rows"][0]["inlet_velocity_ms"]
    assert cell.props.get("aria-label") == "Inlet velocity (m/s)"


def test_run_table_renders_every_factor_type(slot: Any) -> None:
    """Numeric, bool and string columns all build and all get named."""
    factors = {
        "n_cells": {"type": "numeric", "ftype": "integer", "label": "Cell count", "role": "Vary"},
        "adiabatic": {"type": "bool", "ftype": "bool", "label": "Adiabatic walls", "role": "Vary"},
        "scheme": {"type": "select", "ftype": "select", "label": "Scheme", "role": "Vary"},
    }
    ctx: dict[str, Any] = {}
    with slot:
        box = ui.column()
    _rebuild_run_table(ctx, factors, box, "table", lambda: None)
    texts = _texts(box)
    for label in ("Cell count", "Adiabatic walls", "Scheme"):
        assert label in texts


# ---- validation messages --------------------------------------------


def _st(**over: Any) -> dict[str, Any]:
    st: dict[str, Any] = {"ftype": "float", "label": "Inlet velocity (m/s)"}
    st.update(over)
    return st


@pytest.mark.parametrize(
    ("call", "fragment"),
    [
        (lambda: _build_list_vary(
            "inlet_velocity_ms", "numeric", _st(values_text=_w(""))
        ), "add at least one value"),
        (lambda: _build_list_vary(
            "inlet_velocity_ms", "select", _st(subset=_w([]))
        ), "pick at least one option"),
        (lambda: _build_file_list_vary(
            "inlet_velocity_ms", _st(file_refs=_w(""))
        ), "file reference"),
        (lambda: _build_grid_vary(
            "inlet_velocity_ms", "numeric", _st(by_select=_w("By range"), min=_w(5.0), max=_w(1.0))
        ), "max must be greater than min"),
        (lambda: _build_design_vary(
            "inlet_velocity_ms", "select", _st()
        ), "continuous numeric range"),
        (lambda: _build_design_vary(
            "inlet_velocity_ms", "numeric", _st(min=_w(5.0), max=_w(1.0))
        ), "max must be greater than min"),
        (lambda: _build_sampling_vary(
            "inlet_velocity_ms",
            "numeric",
            _st(by_select=_w("By range"), dist_kind=_w("normal"), mean=_w(None), std=_w(0)),
        ), "normal needs a mean"),
        (lambda: _build_sampling_vary(
            "inlet_velocity_ms",
            "numeric",
            _st(by_select=_w("By range"), dist_kind=_w("loguniform"), low=_w(0), high=_w(1)),
        ), "log-uniform needs low > 0"),
        (lambda: _build_sampling_vary(
            "inlet_velocity_ms", "file", _st()
        ), "aren't sampleable"),
    ],
)
def test_validation_messages_name_the_factor_by_label(call: Any, fragment: str) -> None:
    with pytest.raises(ValueError) as exc:
        call()
    message = str(exc.value)
    assert fragment in message
    assert "Inlet velocity (m/s)" in message
    assert "inlet_velocity_ms" not in message


def test_validation_messages_still_work_without_a_stashed_label() -> None:
    """Defensive: a state dict built by hand degrades to the name, not a crash."""
    with pytest.raises(ValueError, match="inlet_velocity_ms"):
        _build_design_vary("inlet_velocity_ms", "numeric", {"min": _w(5.0), "max": _w(1.0)})


# ---- the same rule, one page over -----------------------------------
#
# Optimisation's factor editor already ticked the box with the input's
# label, then named the factor by its descriptor alias the moment
# anything was wrong with it — the same defect as the run table, in the
# place a user is most likely to meet it.


def test_optimisation_validation_messages_use_the_label() -> None:
    from matofx.ui.pages.optimisation import _FactorWidgets

    widgets = _FactorWidgets([])
    widgets._rows = {
        "inlet_velocity_ms": {
            "include": _w(True),
            "type": "float",
            "name": "inlet_velocity_ms",
            "label": "Inlet velocity",
            "min": _w(5.0),
            "max": _w(1.0),
        }
    }
    with pytest.raises(ValueError) as exc:
        widgets.collect()
    assert "Inlet velocity" in str(exc.value)
    assert "inlet_velocity_ms" not in str(exc.value)


def test_optimisation_falls_back_to_the_name_without_a_label() -> None:
    from matofx.ui.pages.optimisation import _FactorWidgets

    widgets = _FactorWidgets([])
    widgets._rows = {
        "inlet_velocity_ms": {
            "include": _w(True),
            "type": "categorical",
            "name": "inlet_velocity_ms",
            "choices": _w([]),
        }
    }
    with pytest.raises(ValueError, match="inlet_velocity_ms"):
        widgets.collect()

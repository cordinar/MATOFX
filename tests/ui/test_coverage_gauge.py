"""Coverage-gauge helper test (ADR-0008 §7.3).

Per house style the live NiceGUI rendering is exercised through the API
client path (the gauge mounts on the My Jobs surrogate-tool form); this
unit test pins the pure-Python guard that keeps the component off when
there's nothing numeric to classify. The classification maths itself is
covered by tests/studies/test_tool_synthesis.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

from matofx.ui._coverage_gauge import render_coverage_gauge


def test_render_skips_when_no_ranged_factors() -> None:
    """Factors lacking a numeric min/max → the component returns before
    touching any NiceGUI slot (safe to call outside a client context)."""
    # No exception despite no active UI slot: the early return fires.
    render_coverage_gauge(
        [{"name": "x"}, {"name": "y", "min": None, "max": None}],
        values={},
        widgets={},
    )


@pytest.fixture
def slot() -> Any:
    """A container the gauge can build into, with no browser."""

    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__coverage_gauge_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


def _chip_text(element: Any) -> list[str]:
    return [
        child.text
        for child in element.descendants()
        if isinstance(getattr(child, "text", None), str) and ":" in child.text
    ]


_FACTORS = [
    {"name": "fan_duty", "min": 22.0, "max": 100.0},
    {"name": "fin_count", "min": 6.0, "max": 26.0},
]


def test_the_gauge_names_inputs_by_their_label(slot: Any) -> None:
    """Chips must read like the form they sit on ([[feedback_use_labels_not_names]]).

    A model version's ``factors`` record only ``{name, min, max}`` — the label
    is a descriptor fact — so the gauge showed ``fan_duty: far extrapolating``
    on a form whose own field is labelled "Fan speed", leaving the reader to
    match the two up.
    """
    with slot:
        render_coverage_gauge(
            _FACTORS,
            values={"fan_duty": 50.0, "fin_count": 200.0},
            widgets={},
            labels={"fan_duty": "Fan speed", "fin_count": "Number of fins"},
        )

    text = " | ".join(_chip_text(slot))
    assert "Fan speed:" in text
    assert "Number of fins:" in text
    assert "fan_duty" not in text
    assert "fin_count" not in text


def test_the_gauge_falls_back_to_the_name_without_a_label(slot: Any) -> None:
    """A missing label is worse than a label, but better than drawing nothing."""
    with slot:
        render_coverage_gauge(_FACTORS, values={"fan_duty": 50.0}, widgets={})

    text = " | ".join(_chip_text(slot))
    assert "fan_duty:" in text

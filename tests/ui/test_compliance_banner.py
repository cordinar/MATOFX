"""The compliance-report reminder banner (chrome).

Three things this banner must get right, and each is a test:

1. It appears when a report is due, at the top of every page, in amber.
2. It appears **only** for someone who can actually generate one. The
   API decides that on ``audit.read`` — the same right the compliance
   page is gated on — and the chrome trusts it, so the banner and the
   page it points at can never disagree about who is allowed in.
3. It never implies anything has stopped working. It sits in the same
   strip as the read-only and integrity banners, which do mean the site
   is degraded; this one does not, and an administrator who misreads it
   will not produce the report their supplier asked for.
"""

from __future__ import annotations

from typing import Any

import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

from matofx.ui.pages._chrome import _render_compliance_banner


@pytest.fixture
def slot() -> Any:
    """A container the render functions can build into, with no browser."""

    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__compliance_banner_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


DUE_TEXT = (
    "A compliance report was due 4 days ago — the last one was generated on 10 June 2026. "
    "Generating one takes a few seconds and sends nothing anywhere. "
    "Nothing stops working until you do."
)


def _mode(**over: Any) -> dict[str, object]:
    payload: dict[str, object] = {
        "read_only": False,
        "compliance_due": True,
        "compliance_banner": DUE_TEXT,
    }
    payload.update(over)
    return payload


def _texts(node: Any) -> list[str]:
    return [str(e.text) for e in node.descendants() if getattr(e, "text", None)]


def test_a_due_report_draws_the_banner(slot: Any) -> None:
    _render_compliance_banner(_mode())
    assert DUE_TEXT in _texts(slot)


def test_the_banner_offers_the_way_to_act_on_it(slot: Any) -> None:
    """A reminder with no route to the page is just nagging."""
    _render_compliance_banner(_mode())
    assert "Generate report" in _texts(slot)


def test_nothing_is_drawn_when_no_report_is_due(slot: Any) -> None:
    _render_compliance_banner(_mode(compliance_due=False, compliance_banner=None))
    assert _texts(slot) == []


def test_nothing_is_drawn_for_a_user_who_cannot_generate_one(slot: Any) -> None:
    """The API zeroes this for anyone without ``audit.read``."""
    _render_compliance_banner({"compliance_due": False, "compliance_banner": None})
    assert _texts(slot) == []


def test_a_due_flag_with_no_wording_draws_nothing(slot: Any) -> None:
    """Defensive: an empty banner bar reads as a rendering fault."""
    _render_compliance_banner(_mode(compliance_banner=""))
    assert _texts(slot) == []


def test_an_older_server_without_the_field_draws_nothing(slot: Any) -> None:
    """The chrome must survive a payload that predates the reminder."""
    _render_compliance_banner({"read_only": False, "banner": None})
    assert _texts(slot) == []


def test_the_banner_is_amber_not_the_red_of_a_blocked_site(slot: Any) -> None:
    """Red is reserved for read-only. This one blocks nothing."""
    _render_compliance_banner(_mode())
    row = next(e for e in slot.descendants() if isinstance(e, ui.row))
    style = row._style
    assert style.get("background-color") == "#fff3cd"
    assert style.get("color") == "#856404"

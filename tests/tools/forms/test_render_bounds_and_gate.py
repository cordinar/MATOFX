"""Live numeric-bound messaging and the shared Submit gate.

Two behaviours land here, both exercised through the same headless
``Client(page('/'))`` fixture the other render tests use:

1. **Live bound messaging** — a manual numeric entry outside the
   field's min/max is *flagged* with a red message under the field
   rather than silently clamped back to the bound. The widget no longer
   carries ``min``/``max`` props (which drove NiceGUI's sanitise-on-blur
   clamp); the bound is surfaced live and at submit instead.

2. **Shared Submit gate** — :meth:`FormHandle.submit_blockers` is the
   single gate both the standalone (My Jobs) and the workflow (Workflow
   Run) submit buttons consult, so every gate applies identically
   wherever a tool is run.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from nicegui import Client
from nicegui.page import page

from matofx.tools.descriptor import InputField, InputGroup, ToolDescriptor
from matofx.tools.forms import FieldError, FormValidationError, render_form


def _descriptor(**fields: Any) -> ToolDescriptor:
    base: dict[str, Any] = {
        "alias": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "description": "Demo.",
        "main_script": "main.py",
        # Default off so tests that aren't about the section-review gate
        # aren't tripped by the synthetic "Other" group (ungrouped inputs
        # start collapsed + unopened). The section test overrides this.
        "require_all_groups_opened": False,
    }
    base.update(fields)
    return ToolDescriptor(**base)


def _bounded(name: str, *, required: bool = False) -> InputField:
    return InputField(
        name=name,
        label=name.upper(),
        type="float",
        unit="K",
        minimum=0.0,
        maximum=100.0,
        required=required,
    )


@pytest.fixture
def nicegui_client() -> Iterator[Client]:
    client = Client(page("/"))
    with client:
        yield client


# --- Live bound messaging -------------------------------------------------


def test_bounded_number_widget_carries_no_min_max_props(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # The clamp lever: without min/max props NiceGUI's blur-sanitize is a
    # no-op, so an out-of-range entry stays put long enough to be flagged.
    handle = render_form(_descriptor(inputs=[_bounded("t")]))
    widget = handle.widgets["t"]
    assert "min" not in widget._props
    assert "max" not in widget._props


def test_out_of_range_value_is_flagged_not_cleared(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    handle = render_form(_descriptor(inputs=[_bounded("t")]))

    handle.state["t"] = 150  # above max
    handle.validate_cross_field()
    assert handle.error_labels["t"].visible is True
    assert "can't be more than 100" in handle.error_labels["t"].text

    handle.state["t"] = -5  # below min
    handle.validate_cross_field()
    assert handle.error_labels["t"].visible is True
    assert "can't be less than 0" in handle.error_labels["t"].text

    handle.state["t"] = 50  # back in range
    handle.validate_cross_field()
    assert handle.error_labels["t"].visible is False
    assert handle.error_labels["t"].text == ""


def test_bound_free_number_has_no_live_runner(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # A numeric field with no declared bound and no constraint can't
    # produce a live message — no runner is wired (constraint-free tools
    # stay untouched).
    handle = render_form(
        _descriptor(inputs=[InputField(name="x", label="X", type="float")]),
    )
    assert handle._run_cross_field is None


def test_live_pass_preserves_submit_error_on_unmanaged_field(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # A submit-time error on a bound-free field must survive a later live
    # pass driven by editing a *bounded* sibling.
    handle = render_form(
        _descriptor(
            inputs=[
                _bounded("t"),
                InputField(name="note", label="Note", type="string"),
            ],
        ),
    )
    handle.show_errors(
        FormValidationError([FieldError("note", "Note", "Note is required.")]),
    )
    handle.state["t"] = 50  # valid edit to a managed field
    handle.validate_cross_field()
    assert handle.error_labels["note"].visible is True
    assert handle.error_labels["note"].text == "Note is required."


# --- Shared Submit gate ---------------------------------------------------


def test_gate_reports_name_and_required(nicegui_client: Client) -> None:  # noqa: ARG001
    handle = render_form(
        _descriptor(inputs=[InputField(name="t", label="T", type="float", required=True)]),
    )
    reasons = handle.submit_blockers(require_job_name=True, job_name="")
    assert "Enter a job name." in reasons
    assert any("Fill in required fields: T" in r for r in reasons)

    # Workflow surface: job name is auto-generated, so it isn't gated.
    reasons_wf = handle.submit_blockers(require_job_name=False)
    assert "Enter a job name." not in reasons_wf


def test_gate_reports_out_of_range_value(nicegui_client: Client) -> None:  # noqa: ARG001
    handle = render_form(_descriptor(inputs=[_bounded("t")]))
    handle.state["t"] = 150
    reasons = handle.submit_blockers(require_job_name=False)
    assert any("can't be more than 100" in r for r in reasons)

    handle.state["t"] = 50
    assert handle.submit_blockers(require_job_name=False) == []


def test_gate_excludes_locked_fields(nicegui_client: Client) -> None:  # noqa: ARG001
    # A locked upstream-fed field the user can't edit must not block
    # Submit on either required-ness or an out-of-range upstream value.
    handle = render_form(_descriptor(inputs=[_bounded("up", required=True)]))
    handle.lock(["up"])
    handle.state["up"] = 150  # out of range, but locked
    assert handle.submit_blockers(require_job_name=False) == []


def test_gate_reports_unreviewed_sections(nicegui_client: Client) -> None:  # noqa: ARG001
    d = _descriptor(
        require_all_groups_opened=True,
        input_groups=[
            InputGroup(name="cond", label="Conditions", column=1, row=1),
            InputGroup(name="mat", label="Material", column=1, row=2),
        ],
        inputs=[
            InputField(name="t", label="T", type="float", required=False, group="cond"),
            InputField(name="m", label="M", type="float", required=False, group="mat"),
        ],
    )
    handle = render_form(d)
    reasons = handle.submit_blockers(require_job_name=False)
    assert any("Open and review 2 sections" in r for r in reasons)

    handle.opened_groups.update({"cond", "mat"})
    assert handle.submit_blockers(require_job_name=False) == []

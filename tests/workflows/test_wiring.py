"""Unit tests for descriptor-level workflow input wiring (pure).

Covers the open-vs-wired determination and the upstream→downstream value
transfer the headless orchestration loop relies on (ADR-0009 §3.4 / the
workflow-orchestration prerequisite).
"""

from __future__ import annotations

from typing import Any

from matofx.workflows.wiring import (
    open_input_fields,
    ready_member_inputs,
    resolve_wired_values,
    wired_inputs,
)

# A two-member chain: `mat` produces `youngs_modulus`; `heat` consumes it
# (wired) plus has its own free inputs `mesh_size` (open) and a
# `legacy_field` wired from a tool NOT in this workflow (so: open here).
_HEAT_DESCRIPTOR: dict[str, Any] = {
    "inputs": [
        {"name": "mesh_size", "type": "float", "minimum": 0.1, "maximum": 5.0},
        {
            "name": "youngs_modulus",
            "type": "float",
            "downstream_input": True,
            "upstream_tool": "mat",
        },
        {"name": "ambient_temp", "type": "float"},
        {
            "name": "legacy_field",
            "type": "float",
            "downstream_input": True,
            "upstream_tool": "some_other_tool",
        },
    ]
}
_MEMBERS = ["mat", "heat"]


def test_wired_inputs_only_in_chain() -> None:
    wired = wired_inputs(_HEAT_DESCRIPTOR, _MEMBERS)
    # youngs_modulus is fed by `mat` (in the chain); legacy_field's upstream
    # isn't a member, so it's open, not wired. With no explicit
    # upstream_output the binding falls back to the input's own name.
    assert wired == {"youngs_modulus": ("mat", "youngs_modulus")}


# A chain where the downstream input name differs from the upstream output
# name and the binding is made explicit via `upstream_output`.
_EXPLICIT_DESCRIPTOR: dict[str, Any] = {
    "inputs": [
        {
            "name": "stiffness",
            "type": "float",
            "downstream_input": True,
            "upstream_tool": "mat",
            "upstream_output": "youngs_modulus",
        },
    ]
}


def test_wired_inputs_uses_explicit_output_name() -> None:
    wired = wired_inputs(_EXPLICIT_DESCRIPTOR, _MEMBERS)
    # The input is named `stiffness` but is fed by `mat`'s `youngs_modulus`.
    assert wired == {"stiffness": ("mat", "youngs_modulus")}


def test_resolve_wired_values_uses_explicit_output() -> None:
    upstream = {"mat": {"youngs_modulus": 210.0}}
    resolved = resolve_wired_values(_EXPLICIT_DESCRIPTOR, _MEMBERS, upstream)
    # Pulled by the upstream output name, written under the input name.
    assert resolved == {"stiffness": 210.0}


def test_ready_member_inputs_honours_explicit_output() -> None:
    descriptors = {"mat": _MAT_DESCRIPTOR, "heat": _EXPLICIT_DESCRIPTOR}
    ready = ready_member_inputs(
        member_states={"mat": "complete", "heat": "created"},
        descriptors=descriptors,
        open_inputs={"mat": {}, "heat": {}},
        completed_outputs={"mat": {"youngs_modulus": 7.0}},
    )
    assert ready == {"heat": {"stiffness": 7.0}}


def test_open_input_fields_excludes_wired() -> None:
    open_fields = open_input_fields(_HEAT_DESCRIPTOR, _MEMBERS)
    names = {f["name"] for f in open_fields}
    assert names == {"mesh_size", "ambient_temp", "legacy_field"}
    # The full field dicts are returned (type/bounds preserved for the picker).
    mesh = next(f for f in open_fields if f["name"] == "mesh_size")
    assert mesh["minimum"] == 0.1


def test_resolve_wired_values_transfers_by_name() -> None:
    upstream = {"mat": {"youngs_modulus": 210.0, "density": 7800.0}}
    resolved = resolve_wired_values(_HEAT_DESCRIPTOR, _MEMBERS, upstream)
    assert resolved == {"youngs_modulus": 210.0}  # only the wired, like-named output


def test_resolve_skips_missing_upstream_value() -> None:
    # Upstream completed but didn't produce the named output → skipped, not None.
    resolved = resolve_wired_values(_HEAT_DESCRIPTOR, _MEMBERS, {"mat": {"density": 7800.0}})
    assert resolved == {}
    resolved_none = resolve_wired_values(
        _HEAT_DESCRIPTOR, _MEMBERS, {"mat": {"youngs_modulus": None}}
    )
    assert resolved_none == {}


def test_no_inputs_descriptor() -> None:
    assert wired_inputs({}, _MEMBERS) == {}
    assert open_input_fields({}, _MEMBERS) == []
    assert resolve_wired_values({}, _MEMBERS, {}) == {}


# --- ready_member_inputs (the headless orchestration planner) ---------------

_MAT_DESCRIPTOR: dict[str, Any] = {
    "inputs": [{"name": "alloy", "type": "categorical", "options": ["al", "cu"]}]
}
_DESCRIPTORS = {"mat": _MAT_DESCRIPTOR, "heat": _HEAT_DESCRIPTOR}
_OPEN_INPUTS = {
    "mat": {"alloy": "cu"},
    "heat": {"mesh_size": 1.5, "ambient_temp": 20.0},
}


def test_ready_only_source_when_nothing_complete() -> None:
    # Both created; `heat` depends on `mat` (wired youngs_modulus) → only
    # `mat` (the source, no in-chain upstream) is runnable first.
    ready = ready_member_inputs(
        member_states={"mat": "created", "heat": "created"},
        descriptors=_DESCRIPTORS,
        open_inputs=_OPEN_INPUTS,
        completed_outputs={},
    )
    assert set(ready) == {"mat"}
    assert ready["mat"] == {"alloy": "cu"}  # its open inputs


def test_downstream_ready_with_merged_inputs_after_upstream_completes() -> None:
    ready = ready_member_inputs(
        member_states={"mat": "complete", "heat": "created"},
        descriptors=_DESCRIPTORS,
        open_inputs=_OPEN_INPUTS,
        completed_outputs={"mat": {"youngs_modulus": 210.0}},
    )
    assert set(ready) == {"heat"}  # mat already done, heat now unblocked
    # open inputs + the wired youngs_modulus pulled from mat's output
    assert ready["heat"] == {"mesh_size": 1.5, "ambient_temp": 20.0, "youngs_modulus": 210.0}


def test_already_queued_members_are_not_replanned() -> None:
    # `mat` queued (not created) → not re-queued; `heat` still gated.
    ready = ready_member_inputs(
        member_states={"mat": "queued", "heat": "created"},
        descriptors=_DESCRIPTORS,
        open_inputs=_OPEN_INPUTS,
        completed_outputs={},
    )
    assert ready == {}


def test_failed_upstream_blocks_downstream_forever() -> None:
    ready = ready_member_inputs(
        member_states={"mat": "failed", "heat": "created"},
        descriptors=_DESCRIPTORS,
        open_inputs=_OPEN_INPUTS,
        completed_outputs={},
    )
    assert ready == {}  # heat never becomes ready off a failed upstream

"""Descriptor-level input wiring for workflow members (pure, no I/O).

A workflow member's input is one of two kinds:

- **wired** — fed by an upstream member's output: the input field declares
  ``downstream_input=True`` with an ``upstream_tool`` that is itself a
  member of this workflow. Its value is *not* a free parameter; it is the
  upstream member's like-named output, transferred at run time.
- **open** — a free entry: either not a ``downstream_input`` at all, or a
  ``downstream_input`` whose ``upstream_tool`` is **not** part of this
  workflow (so nothing in the chain feeds it). These are the inputs a
  user fills in — and, for an optimisation study with a *workflow* target
  (ADR-0009 §3.4), the only inputs eligible to be search **factors**.

This is the headless counterpart to the rule the workflow-run UI applies
per tab (``_all_upstream_ready`` / the locked-field seeding in
``ui/pages/workflow_run.py``): same descriptor convention, no Streamlit.
The orchestration loop (``WorkflowRunService.advance``) uses
:func:`resolve_wired_values` to populate a downstream member's wired
inputs from the upstream outputs before queueing it; the optimisation
factor picker uses :func:`open_input_fields`.

The wiring convention is that a downstream input names *which* upstream
output feeds it via ``upstream_output``. When ``upstream_output`` is
absent (descriptors authored before explicit output selection), the
binding falls back to the legacy rule — a downstream input's **name
equals the upstream output's name**.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def wired_inputs(
    descriptor: Mapping[str, Any], member_aliases: Iterable[str]
) -> dict[str, tuple[str, str]]:
    """Map ``{input_name: (upstream_alias, upstream_output)}`` for wired inputs.

    An input counts as wired only when its ``upstream_tool`` is itself a
    member of the workflow (``member_aliases``); a ``downstream_input``
    pointing at a tool outside the chain is treated as open (nothing feeds
    it here), matching the UI's readiness rule.

    ``upstream_output`` is the name of the upstream output that feeds the
    input; it falls back to the input's own ``name`` when the descriptor
    doesn't declare one (the legacy name-equality convention), so
    pre-existing descriptors keep working unchanged.
    """
    aliases = set(member_aliases)
    wired: dict[str, tuple[str, str]] = {}
    for field in descriptor.get("inputs") or []:
        if not isinstance(field, Mapping) or not field.get("downstream_input"):
            continue
        upstream = field.get("upstream_tool")
        name = field.get("name")
        if isinstance(upstream, str) and upstream in aliases and isinstance(name, str) and name:
            output = field.get("upstream_output")
            output_name = output if isinstance(output, str) and output else name
            wired[name] = (upstream, output_name)
    return wired


def open_input_fields(
    descriptor: Mapping[str, Any], member_aliases: Iterable[str]
) -> list[dict[str, Any]]:
    """The member's input field dicts that are *open* (free entry).

    Returns the full field dicts (name / type / bounds / options) so the
    optimisation factor picker can offer them with their descriptor
    metadata. Wired inputs are excluded — they aren't free parameters.
    """
    wired = wired_inputs(descriptor, member_aliases)
    return [
        dict(field)
        for field in descriptor.get("inputs") or []
        if isinstance(field, Mapping)
        and isinstance(field.get("name"), str)
        and field["name"] not in wired
    ]


def ready_member_inputs(
    *,
    member_states: Mapping[str, str],
    descriptors: Mapping[str, Mapping[str, Any]],
    open_inputs: Mapping[str, Mapping[str, Any]],
    completed_outputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Decide which not-yet-queued members are runnable, and with what inputs.

    Pure planner for the headless orchestrator
    (:meth:`matofx.workflows.run_service.WorkflowRunService.advance`): no
    DB, no disk. A member is *ready* when it is still ``created`` and
    every member that feeds one of its wired inputs is ``complete``. Its
    resolved input vector is the member's open-input values merged with
    the wired values pulled from the completed upstreams' outputs (wired
    keys win on the rare overlap — they are authoritative).

    Args:
        member_states: ``{tool_alias: job_state}`` for every member.
        descriptors: ``{tool_alias: descriptor}`` for every member.
        open_inputs: ``{tool_alias: {input_name: value}}`` — the run's
            open-input values (e.g. an optimisation candidate vector).
        completed_outputs: ``{tool_alias: {output_name: value}}`` for the
            members that have completed (the others are simply absent).

    Returns:
        ``{tool_alias: resolved_inputs}`` for each ready member — the set
        the caller should queue this tick. Empty when nothing is newly
        runnable.
    """
    member_aliases = set(member_states)
    ready: dict[str, dict[str, Any]] = {}
    for alias, state in member_states.items():
        if state != "created":
            continue
        descriptor = descriptors.get(alias) or {}
        wired = wired_inputs(descriptor, member_aliases)
        upstream_aliases = {alias for alias, _output in wired.values()}
        if any(member_states.get(u) != "complete" for u in upstream_aliases):
            continue  # an upstream this member depends on hasn't finished
        resolved = resolve_wired_values(descriptor, member_aliases, completed_outputs)
        ready[alias] = {**dict(open_inputs.get(alias) or {}), **resolved}
    return ready


def resolve_wired_values(
    descriptor: Mapping[str, Any],
    member_aliases: Iterable[str],
    upstream_outputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Pull each wired input's value from its upstream member's outputs.

    ``upstream_outputs`` is ``{upstream_alias: {output_name: value}}`` —
    the completed upstream members' outputs. A wired input takes the
    upstream output named by its ``upstream_output`` (falling back to the
    input's own name for legacy descriptors — see :func:`wired_inputs`).
    A missing / ``None`` upstream value is skipped rather than written as
    ``None`` — the downstream job's own input validation then surfaces a
    genuinely-absent dependency, exactly as the manual UI path does.
    """
    values: dict[str, Any] = {}
    for name, (upstream, output_name) in wired_inputs(descriptor, member_aliases).items():
        produced = upstream_outputs.get(upstream) or {}
        value = produced.get(output_name)
        if value is not None:
            values[name] = value
    return values

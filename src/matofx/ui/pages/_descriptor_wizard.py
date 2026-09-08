"""Descriptor wizard — six-tab editor for input/output structure (Session C C3).

Used by both ``admin_tool_new.py`` (alternative to uploading
``descriptor.json``) and ``admin_tool_edit.py`` (a third tab next to
Metadata / Files — the only way to edit ``inputs[]`` / ``outputs[]``
post-install since descriptor.json is excluded from the file editor).

The wizard owns the *body* of the descriptor — input/output groups +
fields, layout, retain/delete glob lists, execution backend,
duration class, **and tool-author-supplied presets**. Identity, teams,
resources, licences, approvals, notifications, downstream tools stay
on the surrounding metadata form: the wizard renders read-only context
cards above the tabs so the operator sees the full descriptor at once
but mutates the metadata via the existing widgets.

Six tabs, in operator workflow order:

1. **Input Groups** — author the visual layout cards first.
2. **Inputs** — add fields; the group dropdown reads from tab 1. Each
   input's **Dependencies** card authors the §16.16 rules that affect
   it — including expression-driven effects (e.g. *B's minimum tracks
   A*) and per-input **validity (must-be-true)** rules, both of which
   are honoured in the live form, at submit, and when studies /
   optimisation generate candidate points.
3. **Output Groups**.
4. **Outputs** — same shape as Inputs, output-specific field set.
5. **Presets** — author named ``{name, description, values}`` sets
   over the inputs from tab 2. Surfaces as "Use this preset" picks
   in the My Jobs workspace. Replaces the legacy presets-JSON
   upload on the Bundle files card.
6. **Advanced** — retain/delete file globs, execution backend,
   duration class.

State model: a single :class:`dict` mutated in place by user
interactions. The page calls :func:`build_descriptor_payload` on
Save to extract the wizard's contribution to the descriptor; the
metadata form supplies the rest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field as _dc_field
from typing import Any, cast

from nicegui import ui

from matofx.tools import FLOAT_PRECISION_CHOICES
from matofx.ui._features import materials_database_enabled
from matofx.ui.units import (
    UNIT_CUSTOM_VALUE,
    UNIT_NONE_VALUE,
    UNIT_OPTIONS,
    is_known_unit,
)

# --- Upstream wiring context --------------------------------------------
#
# The Inputs tab lets an author mark a field "auto-populated from an
# upstream tool" and pick (a) which tool and (b) which of that tool's
# outputs feeds the field. Per the two-sided handshake, the *Upstream
# tool* dropdown only lists tools that already declare the tool being
# edited in their ``downstream_tools`` (``candidates``). A separate
# "make another tool a source" affordance lets the author pull in any
# other tool on demand — that selection is reconciled back into the
# source tool's ``downstream_tools`` when the edited tool is saved.


@dataclass
class UpstreamWiringContext:
    """Everything the Inputs tab needs to wire downstream inputs.

    ``current_alias`` is the tool being edited (empty string during the
    New Tool flow until the alias is known — reconciliation then uses the
    final alias). ``all_tools`` / ``candidates`` are ``{alias: name}``
    maps. ``outputs`` is ``{alias: [{"name", "label"}, ...]}`` — prefetched
    for the candidate set (+ any aliases already wired in the descriptor)
    and grown on demand via ``fetch_outputs`` when the author makes a new
    tool a source.
    """

    current_alias: str = ""
    all_tools: dict[str, str] = _dc_field(default_factory=dict)
    candidates: dict[str, str] = _dc_field(default_factory=dict)
    outputs: dict[str, list[dict[str, str]]] = _dc_field(default_factory=dict)
    fetch_outputs: Callable[[str], Awaitable[list[dict[str, str]]]] | None = None


def outputs_summary_from_descriptor(descriptor: dict[str, Any]) -> list[dict[str, str]]:
    """Reduce a descriptor's ``outputs`` to ``[{"name", "label"}, ...]``.

    Shared by both admin pages when populating an
    :class:`UpstreamWiringContext`. Skips entries without a usable name;
    falls back to the name when a label is absent.
    """
    summary: list[dict[str, str]] = []
    for out in descriptor.get("outputs") or []:
        if not isinstance(out, dict):
            continue
        name = str(out.get("name") or "").strip()
        if not name:
            continue
        label = str(out.get("label") or "").strip() or name
        summary.append({"name": name, "label": label})
    return summary


def upstream_candidate_map(existing_tools: list[dict[str, Any]], current_alias: str) -> dict[str, str]:
    """``{alias: name}`` of non-archived tools that declare ``current_alias`` downstream.

    The two-sided handshake: a tool only appears as a selectable upstream
    when *it* lists the tool being edited in its ``downstream_tools``.
    """
    return {
        str(t["alias"]): str(t.get("name") or t["alias"])
        for t in sorted(existing_tools, key=lambda t: str(t.get("name") or ""))
        if not t.get("archived_at")
        and str(t["alias"]) != current_alias
        and current_alias
        and current_alias in (t.get("downstream_tools") or [])
    }


def all_tools_map(existing_tools: list[dict[str, Any]], current_alias: str) -> dict[str, str]:
    """``{alias: name}`` of every non-archived tool except ``current_alias``."""
    return {
        str(t["alias"]): str(t.get("name") or t["alias"])
        for t in sorted(existing_tools, key=lambda t: str(t.get("name") or ""))
        if not t.get("archived_at") and str(t["alias"]) != current_alias
    }


def wired_upstream_aliases(state: dict[str, Any]) -> set[str]:
    """Upstream aliases referenced by this wizard state's downstream inputs.

    Used both to prefetch the outputs of tools an existing descriptor
    already wires to (so their output dropdowns show real options) and,
    at save time, to reconcile each source tool's ``downstream_tools``.
    """
    aliases: set[str] = set()
    for field in state.get("inputs") or []:
        if not isinstance(field, dict) or not field.get("downstream_input"):
            continue
        upstream = field.get("upstream_tool")
        if isinstance(upstream, str) and upstream:
            aliases.add(upstream)
    return aliases


async def build_upstream_wiring(
    *,
    existing_tools: list[dict[str, Any]],
    current_alias: str,
    fetch_detail: Callable[[str], Awaitable[dict[str, Any]]],
    extra_prefetch_aliases: Iterable[str] = (),
) -> UpstreamWiringContext:
    """Assemble an :class:`UpstreamWiringContext` for the Inputs tab.

    ``fetch_detail`` is ``ApiClient.get_admin_tool`` (alias → detail dict
    with a ``descriptor``); the wizard module stays client-agnostic by
    taking it as a callable. Outputs are prefetched for the candidate set
    plus ``extra_prefetch_aliases`` (typically the upstreams an existing
    descriptor already wires to); everything else is fetched lazily when
    the author makes a new tool a source.
    """
    candidates = upstream_candidate_map(existing_tools, current_alias)
    all_tools = all_tools_map(existing_tools, current_alias)

    async def _fetch_outputs(alias: str) -> list[dict[str, str]]:
        detail = await fetch_detail(alias)
        return outputs_summary_from_descriptor(dict(detail.get("descriptor") or {}))

    outputs: dict[str, list[dict[str, str]]] = {}
    to_prefetch = set(candidates) | {a for a in extra_prefetch_aliases if a}
    for alias in to_prefetch:
        try:
            outputs[alias] = await _fetch_outputs(alias)
        except Exception:  # noqa: BLE001 — a missing source degrades to no-outputs
            outputs[alias] = []
    return UpstreamWiringContext(
        current_alias=current_alias,
        all_tools=all_tools,
        candidates=candidates,
        outputs=outputs,
        fetch_outputs=_fetch_outputs,
    )


async def reconcile_downstream_registrations(
    *,
    state: dict[str, Any],
    current_alias: str,
    register: Callable[[str, str], Awaitable[bool]],
) -> list[str]:
    """Ensure each wired source tool lists ``current_alias`` downstream.

    Additive only — never removes ``current_alias`` from a tool that's no
    longer wired (that stays a manual edit on the source tool). ``register``
    is ``ApiClient.register_downstream_tool`` partially applied; returns
    the list of source aliases that were actually patched (for surfacing
    "also updated tool X" feedback). Best-effort: a failure to patch one
    source is swallowed so the primary save still reports success.
    """
    patched: list[str] = []
    for upstream in sorted(wired_upstream_aliases(state)):
        if upstream == current_alias:
            continue
        try:
            if await register(upstream, current_alias):
                patched.append(upstream)
        except Exception:  # noqa: BLE001 — source patch is best-effort
            continue
    return patched


# --- Boolean-default tri-state sentinels --------------------------------
#
# NiceGUI's ui.select serialises option keys as JSON strings, which makes
# native ``True`` / ``False`` / ``None`` keys round-trip awkwardly. We
# expose three string sentinels in the dropdown and translate between
# the sentinel and the typed value at the edges (state load + payload
# build + on_change handler).

_BOOL_DEFAULT_NONE = "__none__"
_BOOL_DEFAULT_TRUE = "__true__"
_BOOL_DEFAULT_FALSE = "__false__"

_BOOL_DEFAULT_OPTIONS: dict[str, str] = {
    _BOOL_DEFAULT_NONE: "(no default)",
    _BOOL_DEFAULT_TRUE: "True",
    _BOOL_DEFAULT_FALSE: "False",
}


def _bool_default_to_sentinel(value: Any) -> str:
    """Map a stored default (``True`` / ``False`` / ``None``) to its dropdown sentinel."""
    if value is True:
        return _BOOL_DEFAULT_TRUE
    if value is False:
        return _BOOL_DEFAULT_FALSE
    return _BOOL_DEFAULT_NONE


def _sentinel_to_bool_default(sentinel: str) -> bool | None:
    """Reverse of :func:`_bool_default_to_sentinel`."""
    if sentinel == _BOOL_DEFAULT_TRUE:
        return True
    if sentinel == _BOOL_DEFAULT_FALSE:
        return False
    return None


# --- Float precision dropdown choices -----------------------------------

_FLOAT_PRECISION_OPTIONS: dict[float, str] = {
    p: (f"{p:g} (whole numbers)" if p >= 1 else f"{p:g} ({i} dp)")
    for i, p in enumerate(FLOAT_PRECISION_CHOICES)
}
"""Decimal-step dropdown shown for ``float`` inputs. Keys are stored as-is."""

# --- Vocabulary lists used by the dropdowns -----------------------------

_INPUT_TYPES: dict[str, str] = {
    "string": "string — free text",
    "integer": "integer — whole number",
    "float": "float — decimal number",
    "boolean": "boolean — true / false",
    "select": "select — pick one option",
    "multiselect": "multiselect — pick several",
    "file": "file — upload",
    "table": "table — fixed columns, user adds rows",
}

_MATERIAL_TYPE_LABEL = "material — pick from the materials database"


def _input_type_options(field: dict[str, Any]) -> dict[str, str]:
    """Input-type choices for the picker.

    The ``material`` type (ADR-0007) appears only when the
    ``materials_database`` feature is on — *or* the field is already a
    material input, so editing an existing tool never silently drops the
    selected type when the feature happens to be off on this install.
    """
    opts = dict(_INPUT_TYPES)
    if materials_database_enabled() or field.get("type") == "material":
        opts["material"] = _MATERIAL_TYPE_LABEL
    return opts


_OUTPUT_TYPES: dict[str, str] = {
    "number": "number — numeric scalar",
    "string": "string — text scalar",
    "file": "file — written to the working dir",
    "table": "table — tabular grid output",
}

_EXECUTION_KINDS: dict[str, str] = {
    "subprocess": "subprocess — Phase 1 default",
    "hpc_batch": "HPC batch — Slurm / HPC Pack queue",
    "aos_calculator": "AOS calculator — always-on server",
    "aos_repeat_calculator": "AOS repeat calculator — looping AOS",
    "web_app": "web app — long-running web service",
}

_FILE_TYPE_HINTS: dict[str, str] = {
    "html_plot": "html_plot — embed an HTML report",
    "csv_plot": "csv_plot — render a 2-D line chart from CSV columns",
    "csv_table": "csv_table — render a CSV as an inline table",
    "image": "image — embed a PNG/JPG inline",
    "cad_file": "cad_file — 3-D CAD preview (STL/STP/STEP/IGES/OBJ)",
    "pdf": "pdf — inline PDF viewer (.pdf)",
    "animation_3d": "animation_3d — 3-D animated scene (.gltf / .glb)",
    "video": "video — inline HTML5 video player (.mp4 / .webm / .ogg / .mov)",
    "field": "field — nodal field for a surrogate / 3-D field viewer",
    "report": "report — downloadable artefact only",
}

# Snapshot formats a ``file_type == "field"`` output may declare (ADR-0008
# §4.4; mirrors ``matofx.tools.descriptor._FIELD_FORMATS``).
_FIELD_FORMAT_OPTIONS: dict[str, str] = {
    "vtu": "vtu — VTK unstructured grid (3-D solid / volume)",
    "vtp": "vtp — VTK polydata (surface)",
    "vtk": "vtk — legacy VTK",
    "npy": "npy — bare NumPy array",
    "cgns": "cgns — CGNS",
    "case": "case — EnSight",
}


# --- Helpers used everywhere -------------------------------------------


_NoArgsCallable = Callable[[], None]
"""Standard handler shape — no args, no return — used for re-render + delete callbacks."""


def _then(*actions: _NoArgsCallable) -> _NoArgsCallable:
    """Compose ``actions`` into a single zero-arg handler.

    Used to chain "mutate the state" + "re-render the list" into one
    on_click callback. Replaces the ``(items.append(...), render())``
    tuple-as-statement pattern that mypy --strict refuses.
    """

    def composed() -> None:
        for action in actions:
            action()

    return composed


# --- Empty-item factories -----------------------------------------------


def empty_input_group() -> dict[str, Any]:
    """Blank input-group with sensible defaults (column 1, row 1, 1 sub-column)."""
    return {"name": "", "label": "", "column": 1, "row": 1, "sub_columns": 1}


def empty_output_group() -> dict[str, Any]:
    """Blank output-group (no sub_columns — output groups don't support them)."""
    return {"name": "", "label": "", "column": 1, "row": 1}


def empty_preset() -> dict[str, Any]:
    """Blank preset — empty name + description + empty values map.

    ``values`` is a fresh dict the renderer mutates as the operator
    fills in per-input editors. Inputs left blank are omitted from the
    final descriptor so the runtime resolver falls back to descriptor
    defaults for those.
    """
    return {"name": "", "description": "", "values": {}}


def empty_input_field() -> dict[str, Any]:
    """Blank input field — string, required, no unit, no group."""
    return {
        "name": "",
        "label": "",
        "type": "string",
        "unit_choice": UNIT_NONE_VALUE,
        "unit_custom": "",
        "required": True,
        "default": None,
        "options": [],
        "minimum": None,
        "maximum": None,
        "precision": None,
        "help_text": "",
        "group": None,
        "sub_column": None,
        "downstream_input": False,
        "upstream_tool": None,
        "upstream_output": None,
        "sum_check_value": None,
        "sum_check_targets": [],
        "columns": [],
        "default_rows": None,
        "plot_enabled": True,  # table inputs only: offer the "View as plot" button
        # New per-field rendering hints (see descriptor.py docs):
        "widget": "field",  # "field" | "slider" — numeric only
        "choice_widget": "dropdown",  # "dropdown" | "radio" — select/multiselect only
        "accepted_file_types": [],  # file inputs only; empty = any
        "max_file_size_mb": None,  # file inputs only; None = no cap
        "expected_field_array": None,  # file inputs only; field-array filter for the picker
        "material_class": None,  # material inputs only (ADR-0007)
        "required_properties": [],  # material inputs only; CSV-edited
        "required_properties_csv": "",  # editor mirror of required_properties
        # §16.16 conditional rules that *affect this input*. Each entry
        # is one ``when`` + one effect on this field; the payload
        # builder aggregates every input's list into the descriptor's
        # top-level ``dependencies``.
        "dependencies": [],
    }


# §16.16 effect kinds the per-input dependency editor offers, in the
# order they appear in the dropdown. Keys are descriptor effect
# ``kind``s; values are the plain-English label the author sees.
DEPENDENCY_EFFECTS: dict[str, str] = {
    "show": "Show this field",
    "hide": "Hide this field",
    "disable": "Lock this field (read-only)",
    "set_value": "Set this field's value",
    "set_default": "Change this field's default",
    "set_required": "Make this field required / optional",
    "set_min_max": "Change this field's min / max",
    "set_options": "Change this field's options",
    "require": "Require something to be true (else block)",
}

# Per-bound / per-value source: a fixed number/value the author types, or a
# formula over other inputs (the locked DSL) evaluated at run time.
_VALUE_SOURCE_CHOICES: dict[str, str] = {
    "fixed": "a fixed value",
    "formula": "a formula (from other inputs)",
}

_ON_HIDE_CHOICES: dict[str, str] = {
    "drop": "Discard the value (don't submit it)",
    "keep_default": "Reset to the field's default",
    "keep_last": "Keep what the user last entered",
}


def _empty_outcome() -> dict[str, Any]:
    """One effect inside a rule: the effect kind + its effect-specific params.

    A rule fires every outcome it carries, in order, when its ``when``
    is true. All outcomes act on the input whose editor the rule lives
    in (same-field only — an outcome on a *different* field is authored
    under that other field's editor).
    """
    return {
        "effect": "show",
        "on_hide": "drop",  # effect == hide
        "value": "",  # effect in {set_value, set_default}, source == fixed
        "value_source": "fixed",  # {set_value, set_default}: "fixed" | "formula"
        "value_expr": "",  # {set_value, set_default}, source == formula
        "required": True,  # effect == set_required
        "minimum": None,  # effect == set_min_max, min source == fixed
        "maximum": None,  # effect == set_min_max, max source == fixed
        "minimum_source": "fixed",  # set_min_max: "fixed" | "formula"
        "maximum_source": "fixed",
        "minimum_expr": "",  # set_min_max, min source == formula
        "maximum_expr": "",  # set_min_max, max source == formula
        "require_expr": "",  # effect == require
        "require_message": "",  # effect == require (optional; auto-generated if blank)
        "options_csv": "",  # effect == set_options
    }


def empty_dependency_rule() -> dict[str, Any]:
    """Blank per-input rule: one ``when`` → one or more effects on this field."""
    return {"when": "", "outcomes": [_empty_outcome()]}


_OUTCOME_KEYS = (
    "effect",
    "on_hide",
    "value",
    "value_source",
    "value_expr",
    "required",
    "minimum",
    "maximum",
    "minimum_source",
    "maximum_source",
    "minimum_expr",
    "maximum_expr",
    "require_expr",
    "require_message",
    "options_csv",
)


def _rule_outcomes(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a rule's outcome list, tolerating the pre-multi flat shape.

    New rules carry ``outcomes: [{effect, ...}, ...]``. A rule that
    still has a top-level ``effect`` key (older in-memory state, or a
    hand-built test dict) is read as a single-outcome rule so the
    round-trip never silently drops it.
    """
    outcomes = rule.get("outcomes")
    if isinstance(outcomes, list) and outcomes:
        return outcomes
    if rule.get("effect") is not None:
        return [{k: rule.get(k) for k in _OUTCOME_KEYS}]
    return []


def empty_output_field() -> dict[str, Any]:
    """Blank output field — number, no unit, no group, no plot wiring."""
    return {
        "name": "",
        "label": "",
        "type": "number",
        "unit_choice": UNIT_NONE_VALUE,
        "unit_custom": "",
        "precision": None,
        "group": None,
        "filename": "",
        "file_type": None,
        "xaxis_column": None,
        "yaxis_column": None,
        "plot_title": "",
        "xaxis_title": "",
        "yaxis_title": "",
        # Field-output attributes (file_type == "field"; ADR-0008 §4.4).
        "field_format": None,
        "field_node_array_name": "",
        "field_timesteps": "",
        "field_remap_reference_mesh": "",
        # Output-validity bounds (ADR-0017): min/max for a numeric output,
        # value_min/value_max per-node for a field, and the finiteness opt-out.
        "min": None,
        "max": None,
        "value_min": None,
        "value_max": None,
        "allow_non_finite": False,
        "columns": [],
    }


def empty_table_column() -> dict[str, Any]:
    """Blank table column — string-typed by default.

    The numeric / unit / default keys are kept on every column dict so
    type-conditional UI rows can flip between rendering them populated
    or hidden without ever popping keys.
    """
    return {
        "name": "",
        "label": "",
        "type": "string",
        "unit_choice": UNIT_NONE_VALUE,
        "unit_custom": "",
        "minimum": None,
        "maximum": None,
        "precision": None,
        "default": None,
    }


_TABLE_COLUMN_TYPES: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "float": "float",
    "boolean": "boolean",
}


# --- Round-trip from existing ToolDescriptor JSON to wizard state -------


def state_from_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Translate a stored descriptor dict into the wizard's internal state shape.

    Used by the Edit Tool wizard to pre-populate the tabs from the
    tool's existing ``ToolDescriptor`` row. The reverse direction is
    :func:`build_descriptor_payload`. Round-trip is exact for fields
    the wizard owns; metadata fields (alias, name, …) are left to
    the surrounding form.

    Unknown unit strings (custom units the operator typed at create
    time) are mapped to the ``UNIT_CUSTOM_VALUE`` choice with the
    literal value preserved in ``unit_custom``.
    """
    state: dict[str, Any] = {
        "input_groups": [dict(g) for g in (descriptor.get("input_groups") or [])],
        "inputs": [_input_from_descriptor(f) for f in (descriptor.get("inputs") or [])],
        "cross_field_constraints": [
            {
                "expression": str(c.get("expression") or ""),
                "error_message": str(c.get("error_message") or ""),
            }
            for c in (descriptor.get("cross_field_constraints") or [])
            if isinstance(c, dict)
        ],
        "output_groups": [dict(g) for g in (descriptor.get("output_groups") or [])],
        "outputs": [_output_from_descriptor(f) for f in (descriptor.get("outputs") or [])],
        "presets": [_preset_from_descriptor(p) for p in (descriptor.get("presets") or [])],
        "input_column_layout": dict(descriptor.get("input_column_layout") or {}),
        "output_column_layout": dict(descriptor.get("output_column_layout") or {}),
        "retain_files": list(descriptor.get("retain_files") or []),
        "delete_io_files": list(descriptor.get("delete_io_files") or []),
        "execution_backend": dict(descriptor.get("execution_backend") or {"kind": "subprocess"}),
        "duration_class": str(descriptor.get("duration_class") or "medium"),
        # Default True so descriptors authored before these flags
        # existed adopt the system default (collapsed sections + the
        # review gate), matching the ToolDescriptor model defaults.
        "input_groups_start_collapsed": bool(descriptor.get("input_groups_start_collapsed", True)),
        "require_all_groups_opened": bool(descriptor.get("require_all_groups_opened", True)),
    }
    # Fan the top-level dependency rules back out onto the inputs they
    # affect so each input's editor shows the rules that concern it.
    _distribute_dependencies(descriptor.get("dependencies") or [], state["inputs"])
    return state


def _unit_to_state(unit: str | None) -> tuple[str, str]:
    """Map a descriptor unit string to ``(unit_choice, unit_custom)``."""
    if unit is None or unit == "":
        return (UNIT_NONE_VALUE, "")
    if is_known_unit(unit):
        return (unit, "")
    return (UNIT_CUSTOM_VALUE, unit)


def _input_from_descriptor(field: dict[str, Any]) -> dict[str, Any]:
    base = empty_input_field()
    base.update({k: v for k, v in field.items() if k not in ("unit", "sum_check", "columns")})
    choice, custom = _unit_to_state(field.get("unit"))
    base["unit_choice"] = choice
    base["unit_custom"] = custom
    sum_check = field.get("sum_check") or {}
    if sum_check:
        base["sum_check_value"] = sum_check.get("value")
        base["sum_check_targets"] = list(sum_check.get("sum_parameters") or [])
    base["columns"] = [_table_column_from_descriptor(c) for c in (field.get("columns") or [])]
    base["accepted_file_types"] = list(field.get("accepted_file_types") or [])
    base["required_properties"] = list(field.get("required_properties") or [])
    base["required_properties_csv"] = ", ".join(base["required_properties"])
    return base


def _output_from_descriptor(field: dict[str, Any]) -> dict[str, Any]:
    base = empty_output_field()
    base.update({k: v for k, v in field.items() if k not in ("unit", "columns")})
    choice, custom = _unit_to_state(field.get("unit"))
    base["unit_choice"] = choice
    base["unit_custom"] = custom
    base["columns"] = [_table_column_from_descriptor(c) for c in (field.get("columns") or [])]
    return base


def _preset_from_descriptor(preset: dict[str, Any]) -> dict[str, Any]:
    """Hydrate a stored preset dict into the wizard's authoring shape.

    The descriptor stores ``{name, description, values}`` flat; the
    wizard state uses the same shape so the only translation is
    defensive copying (so editing one preset's values dict in the
    wizard doesn't aliassign with the loaded source).
    """
    base = empty_preset()
    base["name"] = str(preset.get("name") or "")
    base["description"] = str(preset.get("description") or "")
    raw_values = preset.get("values")
    base["values"] = dict(raw_values) if isinstance(raw_values, dict) else {}
    return base


def _preset_to_descriptor(preset: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Emit the descriptor-shaped preset dict.

    The wizard's per-input value editors write into
    ``preset["values"][input_name]`` directly. On serialise we:

    * Strip empty / unset values (``None``, empty string, empty list)
      so the runtime resolver falls back to descriptor defaults for
      inputs the preset doesn't explicitly override.
    * Drop entries that name inputs no longer present in the wizard
      (the operator deleted the input after writing the preset).
      Tolerant rather than strict — the legacy file loader had the
      same behaviour.
    """
    known_input_names = {str(f.get("name") or "") for f in state.get("inputs") or []}
    raw_values = preset.get("values") or {}
    cleaned: dict[str, Any] = {}
    for name, value in raw_values.items():
        if name not in known_input_names:
            continue
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        if isinstance(value, list) and not value:
            continue
        cleaned[str(name)] = value
    return {
        "name": str(preset.get("name") or "").strip(),
        "description": str(preset.get("description") or "").strip(),
        "values": cleaned,
    }


def _table_column_from_descriptor(col: dict[str, Any]) -> dict[str, Any]:
    """Hydrate a stored table-column dict with the wizard's extra editor keys."""
    base = empty_table_column()
    base.update({k: v for k, v in col.items() if k != "unit"})
    choice, custom = _unit_to_state(col.get("unit"))
    base["unit_choice"] = choice
    base["unit_custom"] = custom
    return base


def _table_column_to_descriptor(col: dict[str, Any]) -> dict[str, Any]:
    """Emit the descriptor-shaped column dict, dropping wizard-only state keys."""
    out: dict[str, Any] = {
        "name": str(col.get("name") or ""),
        "label": str(col.get("label") or ""),
        "type": str(col.get("type") or "string"),
    }
    unit = _resolve_unit(col)
    if unit is not None:
        out["unit"] = unit
    if col.get("minimum") is not None:
        out["minimum"] = col["minimum"]
    if col.get("maximum") is not None:
        out["maximum"] = col["maximum"]
    if col.get("precision") is not None:
        out["precision"] = col["precision"]
    if col.get("default") is not None:
        out["default"] = col["default"]
    return out


# --- §16.16 dependency round-trip --------------------------------------


def _coerce_rule_value(raw: Any, field_type: str) -> Any:
    """Coerce an editor string to the target input's value type.

    The editor stores ``set_value`` / ``set_default`` literals as text.
    A coercion that fails is left as the raw string so the descriptor's
    own type validator surfaces a precise error at save rather than the
    wizard guessing.
    """
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    text = str(raw).strip()
    if field_type == "integer":
        try:
            return int(float(text))
        except ValueError:
            return raw
    if field_type == "float":
        try:
            return float(text)
        except ValueError:
            return raw
    if field_type == "boolean":
        low = text.lower()
        if low in {"true", "yes", "1"}:
            return True
        if low in {"false", "no", "0"}:
            return False
        return raw
    if field_type == "multiselect":
        return [p.strip() for p in text.split(",") if p.strip()]
    return text


def _rule_value_to_text(value: Any) -> str:
    """Inverse of :func:`_coerce_rule_value` for the editor field."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _outcome_to_effect(
    outcome: dict[str, Any], name: str, field_type: str
) -> dict[str, Any] | None:
    """Build the descriptor effect dict for one outcome of a per-input rule.

    Returns ``None`` for an outcome that can't form a valid effect (an
    empty option list, a min/max with neither bound) so the wizard
    doesn't emit a descriptor the model would reject.
    """
    effect = str(outcome.get("effect") or "show")
    if effect == "show":
        return {"kind": "show", "fields": [name]}
    if effect == "hide":
        return {"kind": "hide", "fields": [name], "on_hide": outcome.get("on_hide") or "drop"}
    if effect == "disable":
        return {"kind": "disable", "field": name}
    if effect in ("set_value", "set_default"):
        out: dict[str, Any] = {"kind": effect, "field": name}
        if str(outcome.get("value_source") or "fixed") == "formula":
            expr = str(outcome.get("value_expr") or "").strip()
            if not expr:
                return None
            out["value_expr"] = expr
        else:
            out["value"] = _coerce_rule_value(outcome.get("value"), field_type)
        return out
    if effect == "set_required":
        return {
            "kind": "set_required",
            "field": name,
            "required": bool(outcome.get("required", True)),
        }
    if effect == "set_min_max":
        out = {"kind": "set_min_max", "field": name}
        if str(outcome.get("minimum_source") or "fixed") == "formula":
            expr = str(outcome.get("minimum_expr") or "").strip()
            if expr:
                out["minimum_expr"] = expr
        elif outcome.get("minimum") is not None:
            out["minimum"] = outcome.get("minimum")
        if str(outcome.get("maximum_source") or "fixed") == "formula":
            expr = str(outcome.get("maximum_expr") or "").strip()
            if expr:
                out["maximum_expr"] = expr
        elif outcome.get("maximum") is not None:
            out["maximum"] = outcome.get("maximum")
        # Need at least one bound (literal or formula) for a valid effect.
        if not any(k in out for k in ("minimum", "maximum", "minimum_expr", "maximum_expr")):
            return None
        return out
    if effect == "require":
        expr = str(outcome.get("require_expr") or "").strip()
        if not expr:
            return None
        req: dict[str, Any] = {"kind": "require", "field": name, "expr": expr}
        message = str(outcome.get("require_message") or "").strip()
        if message:
            req["message"] = message
        return req
    if effect == "set_options":
        opts = [p.strip() for p in str(outcome.get("options_csv") or "").split(",") if p.strip()]
        if not opts:
            return None
        return {"kind": "set_options", "field": name, "options": opts}
    return None


def _collect_dependencies(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Aggregate every input's per-input rules into top-level ``dependencies``.

    One descriptor :class:`Dependency` per authored rule: a single
    ``when`` plus the rule's list of effects, all on that input.
    Outcomes that can't form a valid effect are dropped; a rule with a
    blank ``when`` or no formable effect at all is skipped entirely.
    """
    deps: list[dict[str, Any]] = []
    for field in state.get("inputs", []):
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        field_type = str(field.get("type") or "string")
        for rule in field.get("dependencies") or []:
            # A blank condition means "always" — store it as the DSL literal
            # ``True`` so the rule (e.g. an unconditional "B's min is A")
            # always fires. The reverse map turns ``True`` back into blank.
            when = str(rule.get("when") or "").strip() or "True"
            effects: list[dict[str, Any]] = []
            for outcome in _rule_outcomes(rule):
                effect = _outcome_to_effect(outcome, name, field_type)
                if effect is not None:
                    effects.append(effect)
            if not effects:
                continue
            deps.append({"when": when, "effects": effects})
    return deps


def _distribute_dependencies(
    descriptor_deps: list[dict[str, Any]],
    inputs_state: list[dict[str, Any]],
) -> None:
    """Attribute a loaded top-level ``dependencies`` to per-input editors.

    Each descriptor rule becomes, per target field it touches, one
    per-input rule whose ``outcomes`` are that field's effects in
    declaration order — the exact inverse of :func:`_collect_dependencies`.
    A hand-authored rule whose effects span several fields is split
    one-per-field (each field's editor showing only its own outcomes):
    behaviourally identical under the resolver (topological + last-wins),
    though not byte-identical. The wizard is the primary authoring
    surface, so this is acceptable.
    """
    by_name = {f["name"]: f for f in inputs_state if f.get("name")}
    for rule in descriptor_deps or []:
        # ``True`` is how an "always" rule is stored; show it as a blank
        # condition in the editor (the inverse of :func:`_collect_dependencies`).
        when_raw = str(rule.get("when") or "")
        when = "" if when_raw.strip() == "True" else when_raw
        # Effects in this rule, bucketed by the field they target so a
        # field's multiple outcomes collapse back into one editor rule.
        per_field: dict[str, dict[str, Any]] = {}
        for effect in rule.get("effects") or []:
            kind = str(effect.get("kind") or "")
            if kind in ("show", "hide"):
                targets = list(effect.get("fields") or [])
            else:
                targets = [effect.get("field")]
            for target in targets:
                field = by_name.get(target)
                if field is None:
                    continue
                entry = per_field.get(target)
                if entry is None:
                    entry = {"when": when, "outcomes": []}
                    per_field[target] = entry
                    field.setdefault("dependencies", []).append(entry)
                outcome = _empty_outcome()
                outcome["effect"] = kind
                if kind == "hide":
                    outcome["on_hide"] = effect.get("on_hide") or "drop"
                elif kind in ("set_value", "set_default"):
                    if effect.get("value_expr") is not None:
                        outcome["value_source"] = "formula"
                        outcome["value_expr"] = str(effect.get("value_expr") or "")
                    else:
                        outcome["value"] = _rule_value_to_text(effect.get("value"))
                elif kind == "set_required":
                    outcome["required"] = bool(effect.get("required", True))
                elif kind == "set_min_max":
                    if effect.get("minimum_expr") is not None:
                        outcome["minimum_source"] = "formula"
                        outcome["minimum_expr"] = str(effect.get("minimum_expr") or "")
                    else:
                        outcome["minimum"] = effect.get("minimum")
                    if effect.get("maximum_expr") is not None:
                        outcome["maximum_source"] = "formula"
                        outcome["maximum_expr"] = str(effect.get("maximum_expr") or "")
                    else:
                        outcome["maximum"] = effect.get("maximum")
                elif kind == "require":
                    outcome["require_expr"] = str(effect.get("expr") or "")
                    outcome["require_message"] = str(effect.get("message") or "")
                elif kind == "set_options":
                    outcome["options_csv"] = ", ".join(
                        str(o) for o in (effect.get("options") or [])
                    )
                entry["outcomes"].append(outcome)


# --- Round-trip from wizard state back to ToolDescriptor JSON ----------


def _prune_column_layout(
    layout: dict[str, float] | None, groups: list[dict[str, Any]] | None
) -> dict[str, float]:
    """Drop ``column_N`` weights for columns the groups no longer use."""
    live = {int(g.get("column") or 1) for g in (groups or [])}
    return {
        key: value
        for key, value in (layout or {}).items()
        if key.startswith("column_")
        and key[len("column_") :].isdigit()
        and int(key[len("column_") :]) in live
    }


def build_descriptor_payload(state: dict[str, Any]) -> dict[str, Any]:
    """Assemble the wizard's contribution to the descriptor JSON.

    The metadata form supplies identity / teams / resources / etc.;
    the page merges the two dicts before submitting. Unit choices
    are translated back to the literal string the descriptor model
    expects: ``UNIT_NONE_VALUE`` → ``None``, ``UNIT_CUSTOM_VALUE`` →
    the operator's typed custom string.
    """
    return {
        "input_groups": [dict(g) for g in state.get("input_groups", [])],
        "inputs": [_input_to_descriptor(f) for f in state.get("inputs", [])],
        "dependencies": _collect_dependencies(state),
        # Top-level cross-field constraints aren't authored in the wizard
        # (the per-input Dependencies card's "must be true" outcome is the
        # authoring surface now); carry any that already exist on the
        # descriptor through untouched so editing a tool never drops them.
        "cross_field_constraints": [
            dict(c) for c in (state.get("cross_field_constraints") or [])
        ],
        "output_groups": [dict(g) for g in state.get("output_groups", [])],
        "outputs": [_output_to_descriptor(f) for f in state.get("outputs", [])],
        "presets": [_preset_to_descriptor(p, state) for p in state.get("presets", [])],
        # Weights for columns that no longer exist are dropped on save.
        # A stale ``column_2`` left behind when the groups collapsed back
        # to one column used to render that column at a fraction of the
        # width (see ``job._flex_weights_from_layout``); the renderers
        # normalise now, but there's no reason to keep carrying dead keys.
        "input_column_layout": _prune_column_layout(
            state.get("input_column_layout"), state.get("input_groups")
        ),
        "output_column_layout": _prune_column_layout(
            state.get("output_column_layout"), state.get("output_groups")
        ),
        "retain_files": list(state.get("retain_files") or []),
        "delete_io_files": list(state.get("delete_io_files") or []),
        "execution_backend": dict(state.get("execution_backend") or {"kind": "subprocess"}),
        "duration_class": str(state.get("duration_class") or "medium"),
        "input_groups_start_collapsed": bool(state.get("input_groups_start_collapsed", True)),
        "require_all_groups_opened": bool(state.get("require_all_groups_opened", True)),
    }


def _resolve_unit(field: dict[str, Any]) -> str | None:
    choice = field.get("unit_choice", UNIT_NONE_VALUE)
    if choice == UNIT_NONE_VALUE:
        return None
    if choice == UNIT_CUSTOM_VALUE:
        custom = str(field.get("unit_custom") or "").strip()
        return custom or None
    return str(choice)


def _input_to_descriptor(field: dict[str, Any]) -> dict[str, Any]:
    field_type = str(field.get("type") or "string")
    out: dict[str, Any] = {
        "name": str(field.get("name") or ""),
        "label": str(field.get("label") or ""),
        "type": field_type,
        "unit": _resolve_unit(field),
        "required": bool(field.get("required", True)),
        "default": field.get("default"),
        "options": list(field.get("options") or []) or None,
        "minimum": field.get("minimum"),
        "maximum": field.get("maximum"),
        "precision": field.get("precision"),
        "help_text": str(field.get("help_text") or "") or None,
        "group": field.get("group") or None,
        "sub_column": field.get("sub_column"),
        "downstream_input": bool(field.get("downstream_input", False)),
        "upstream_tool": field.get("upstream_tool") or None,
        # Only meaningful for a downstream input; the explicit output the
        # author chose (falls back to name-match at runtime when None).
        "upstream_output": (
            field.get("upstream_output") or None if field.get("downstream_input") else None
        ),
    }
    # Numeric: persist widget kind only when relevant; default ("field")
    # is omitted to keep descriptor JSON tidy and round-trip cleanly.
    if field_type in ("integer", "float"):
        widget = str(field.get("widget") or "field")
        if widget == "slider":
            out["widget"] = "slider"
    if field_type in ("select", "multiselect"):
        choice_widget = str(field.get("choice_widget") or "dropdown")
        if choice_widget == "radio":
            out["choice_widget"] = "radio"
    if field_type == "file":
        accepted = [str(x) for x in (field.get("accepted_file_types") or []) if str(x).strip()]
        if accepted:
            out["accepted_file_types"] = accepted
        if field.get("max_file_size_mb") is not None:
            out["max_file_size_mb"] = float(field["max_file_size_mb"])
        expected_field = str(field.get("expected_field_array") or "").strip()
        if expected_field:
            out["expected_field_array"] = expected_field
    sum_value = field.get("sum_check_value")
    sum_targets = list(field.get("sum_check_targets") or [])
    if sum_value is not None and sum_targets:
        out["sum_check"] = {"value": sum_value, "sum_parameters": sum_targets}
    if field_type == "table":
        columns = [
            _table_column_to_descriptor(c) for c in field.get("columns") or [] if c.get("name")
        ]
        if columns:
            out["columns"] = columns
        if field.get("default_rows") is not None:
            out["default_rows"] = int(field["default_rows"])
        # Default is True (model default); only persist the off state so
        # existing descriptors stay clean.
        if not field.get("plot_enabled", True):
            out["plot_enabled"] = False
    if field_type == "material":
        # A material field declares a *requirement*, not options/default —
        # descriptor._validate_material_constraints forbids both.
        out["options"] = None
        out["default"] = None
        out["material_class"] = str(field.get("material_class") or "") or None
        req = [str(x) for x in (field.get("required_properties") or []) if str(x).strip()]
        if req:
            out["required_properties"] = req
    return out


def _output_to_descriptor(field: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": str(field.get("name") or ""),
        "label": str(field.get("label") or ""),
        "type": str(field.get("type") or "number"),
        "unit": _resolve_unit(field),
        "precision": field.get("precision"),
        "group": field.get("group") or None,
        "filename": field.get("filename") or None,
        "file_type": field.get("file_type") or None,
        "xaxis_column": field.get("xaxis_column"),
        "yaxis_column": field.get("yaxis_column"),
        "plot_title": str(field.get("plot_title") or "") or None,
        "xaxis_title": str(field.get("xaxis_title") or "") or None,
        "yaxis_title": str(field.get("yaxis_title") or "") or None,
    }
    # The uncertainty link (ADR-0008 §7.1). This allowlist previously dropped
    # it, so ANY edit through this editor — adding a group, renaming a label —
    # silently unpaired a surrogate's ``value ± std`` reporting and turned every
    # std into an ordinary row. Same failure the field-output keys below already
    # had once; the fix is the same.
    if str(field.get("type") or "") == "number" and str(field.get("uncertainty_for") or "").strip():
        out["uncertainty_for"] = str(field["uncertainty_for"]).strip()
    if str(field.get("type") or "") == "table":
        columns = [
            _table_column_to_descriptor(c) for c in field.get("columns") or [] if c.get("name")
        ]
        if columns:
            out["columns"] = columns
    # Field outputs (file_type == "field") carry the snapshot format + array
    # name + optional transient glob / remap mesh (ADR-0008 §4.4). The
    # descriptor validator forbids these keys on any non-field output, so they
    # are emitted only here — and dropping them is what previously broke the
    # round-trip of a field output through the editor.
    if str(field.get("file_type") or "") == "field":
        out["field_format"] = field.get("field_format") or None
        if str(field.get("field_node_array_name") or "").strip():
            out["field_node_array_name"] = str(field["field_node_array_name"]).strip()
        if str(field.get("field_timesteps") or "").strip():
            out["field_timesteps"] = str(field["field_timesteps"]).strip()
        if str(field.get("field_remap_reference_mesh") or "").strip():
            out["field_remap_reference_mesh"] = str(field["field_remap_reference_mesh"]).strip()
    # Output-validity bounds (ADR-0017): min/max on a numeric output;
    # value_min/value_max per-node on a field; allow_non_finite on either.
    # The descriptor validator scopes these by type, so emit only where valid.
    otype = str(field.get("type") or "")
    is_field = str(field.get("file_type") or "") == "field"
    if otype == "number":
        if field.get("min") is not None:
            out["min"] = float(field["min"])
        if field.get("max") is not None:
            out["max"] = float(field["max"])
    if is_field:
        if field.get("value_min") is not None:
            out["value_min"] = float(field["value_min"])
        if field.get("value_max") is not None:
            out["value_max"] = float(field["value_max"])
    if bool(field.get("allow_non_finite")) and (otype == "number" or is_field):
        out["allow_non_finite"] = True
    # KPI extraction (ADR-0012 §3.1): the "Mark as KPI" flag and its optional
    # label / unit / category overrides live on numeric outputs only. These are
    # written into the field's state by the KPI editor row but were previously
    # never serialised here, so ticking "Mark as KPI" was silently lost on Save.
    # The descriptor validator (_kpi_consistency) rejects kpi=true on non-number
    # types and rejects the overrides unless kpi=true, so scope both to a
    # numeric, flagged output and emit each override only when non-empty.
    if otype == "number" and bool(field.get("kpi")):
        out["kpi"] = True
        for key in ("kpi_label", "kpi_unit", "kpi_category"):
            raw = field.get(key)
            text = str(raw).strip() if raw is not None else ""
            if text:
                out[key] = text
    return out


# --- Helpers used by every list-and-edit pattern -----------------------


def _bind_text(
    item: dict[str, Any],
    key: str,
    *,
    label: str,
    placeholder: str = "",
    multiline: bool = False,
    width_class: str = "w-full",
    on_change_extra: _NoArgsCallable | None = None,
) -> Any:
    """A text input wired to ``item[key]`` plus an optional follow-up callback.

    ``on_change_extra`` runs after the bound field is updated — used to
    refresh the summary header / re-evaluate visibility on a single
    change. Keeps name + label + type bindings reactive without
    duplicating event-handler boilerplate at every callsite.
    """
    factory = ui.textarea if multiline else ui.input

    def _handler(e: Any) -> None:
        item[key] = e.value
        if on_change_extra is not None:
            on_change_extra()

    elem = factory(
        label,
        value=str(item.get(key) or ""),
        placeholder=placeholder,
        on_change=_handler,
    ).classes(width_class)
    if not multiline:
        elem.props("dense")
    else:
        elem.props("dense outlined")
    return elem


def _bind_number(
    item: dict[str, Any],
    key: str,
    *,
    label: str,
    min_value: float | None = None,
    max_value: float | None = None,
    step: float | None = None,
    width_class: str = "flex-1",
    on_change_extra: _NoArgsCallable | None = None,
) -> Any:
    """A numeric input wired to ``item[key]`` with optional bounds + step."""
    kwargs: dict[str, Any] = {"label": label, "value": item.get(key)}
    if min_value is not None:
        kwargs["min"] = min_value
    if max_value is not None:
        kwargs["max"] = max_value
    if step is not None:
        kwargs["step"] = step

    def _handler(e: Any) -> None:
        item[key] = e.value
        if on_change_extra is not None:
            on_change_extra()

    elem = ui.number(**kwargs).classes(width_class).props("dense")
    elem.on_value_change(_handler)
    return elem


def _bind_int_number(
    item: dict[str, Any],
    key: str,
    *,
    label: str,
    min_value: float | None = None,
    max_value: float | None = None,
    width_class: str = "flex-1",
    on_change_extra: _NoArgsCallable | None = None,
) -> Any:
    """A numeric input that stores a Python ``int`` (or ``None``).

    Step is fixed to 1. ``ui.number(step=1)`` only enforces the step on
    arrow-up/arrow-down clicks, so the operator can still type ``1.5``
    by hand. We catch that here: every on_change coerces via
    ``int(...)`` (truncation toward zero), writes the truncated value
    *back to the widget* so what's displayed matches what's stored,
    and fires a one-shot warning toast so the operator notices the
    silent fixup. Used for ``integer`` inputs' min / max / default and
    for integer columns inside the table sub-editor.
    """
    kwargs: dict[str, Any] = {
        "label": label,
        "value": item.get(key),
        "step": 1,
    }
    if min_value is not None:
        kwargs["min"] = int(min_value)
    if max_value is not None:
        kwargs["max"] = int(max_value)

    elem = ui.number(**kwargs).classes(width_class).props("dense")

    def _handler(e: Any) -> None:
        v = e.value
        if v is None or v == "":
            item[key] = None
        else:
            try:
                coerced = int(v)
            except (TypeError, ValueError):
                # Anything we can't coerce gets blanked out; the widget
                # state will still hold the bad input so the operator
                # can fix the typo. Don't notify here — the browser's
                # number widget already rejects garbled text.
                item[key] = None
                if on_change_extra is not None:
                    on_change_extra()
                return
            if coerced != v:
                # The user typed (or pasted) a fractional value.
                # Snap the widget back to the truncated int so the
                # display agrees with the persisted state, and surface
                # a warning so the change isn't silent.
                elem.set_value(coerced)
                ui.notify(
                    f"{label}: integer fields can't have decimals — truncated to {coerced}.",
                    type="warning",
                    position="top-right",
                )
            item[key] = coerced
        if on_change_extra is not None:
            on_change_extra()

    elem.on_value_change(_handler)
    return elem


def _bind_checkbox(
    item: dict[str, Any],
    key: str,
    *,
    label: str,
    on_change_extra: _NoArgsCallable | None = None,
) -> Any:
    """A checkbox wired to ``item[key]``."""

    def _handler(e: Any) -> None:
        item[key] = bool(e.value)
        if on_change_extra is not None:
            on_change_extra()

    return ui.checkbox(label, value=bool(item.get(key, False)), on_change=_handler)


def _bind_select(
    item: dict[str, Any],
    key: str,
    *,
    label: str,
    options: dict[Any, str] | list[str],
    with_input: bool = False,
    width_class: str = "flex-1",
    multiple: bool = False,
    on_change_extra: _NoArgsCallable | None = None,
) -> Any:
    """A select wired to ``item[key]``; ``options`` may be dict (label map) or list.

    When ``multiple=True`` the widget renders as a multi-select with
    chips; the bound value is a list. The default empty value is
    ``[]`` when multi, ``None`` otherwise — matches NiceGUI's own
    convention so the caller doesn't have to special-case empty state.
    """

    def _handler(e: Any) -> None:
        if multiple:
            item[key] = list(e.value or [])
        else:
            item[key] = e.value
        if on_change_extra is not None:
            on_change_extra()

    current = item.get(key)
    if multiple and not isinstance(current, list):
        current = []
    # NiceGUI's ChoiceElement rejects a preset value that isn't in the
    # option set and raises ValueError, which 500s the whole page. That
    # happens whenever a tool's descriptor carries a value the wizard's
    # hint dict doesn't enumerate (e.g. a file_type added later, or a
    # legacy value the new app doesn't know). Coerce to None so the
    # picker renders blank instead of crashing the Edit Tool page.
    keys = options if isinstance(options, list) else list(options.keys())
    if not multiple and current is not None and current not in keys:
        current = None
    elem = ui.select(
        options,
        label=label,
        value=current,
        with_input=with_input,
        multiple=multiple,
        on_change=_handler,
    ).classes(width_class)
    if multiple:
        elem.props("dense use-chips")
    else:
        elem.props("dense")
    return elem


def _move(items: list[Any], index: int, delta: int) -> None:
    """Swap ``items[index]`` with its ``index+delta`` sibling, clamped to bounds."""
    target = index + delta
    if 0 <= target < len(items):
        items[index], items[target] = items[target], items[index]


# --- Row toolbar (up / down / delete) used by every list ---------------


def _row_toolbar(
    items: list[Any],
    index: int,
    rerender: _NoArgsCallable,
) -> None:
    """Render up / down / delete buttons for one row in a list."""

    def _move_up() -> None:
        _move(items, index, -1)

    def _move_down() -> None:
        _move(items, index, +1)

    def _delete() -> None:
        items.pop(index)

    with ui.row().classes("gap-1 items-center"):
        ui.button(
            icon="keyboard_arrow_up",
            on_click=_then(_move_up, rerender),
        ).props("flat round dense").bind_visibility_from(items, "__len__", lambda _len: index > 0)
        ui.button(
            icon="keyboard_arrow_down",
            on_click=_then(_move_down, rerender),
        ).props("flat round dense").bind_visibility_from(
            items, "__len__", lambda _len: index < len(items) - 1
        )
        ui.button(
            icon="delete",
            on_click=_then(_delete, rerender),
        ).props("flat round dense color=negative")


# --- Drag-and-drop reordering (shared by every ordered list) -----------
#
# Native HTML5 drag-and-drop, the same idiom as the Project Metadata
# values editor — no JS library, so nothing new to vendor for an
# air-gapped install. Three pieces per list:
#
# - ``_new_drag_state()`` — one holder per *render pass*, closed over by
#   that pass's handles and drop targets. It only has to survive from
#   ``dragstart`` to ``drop``, and a re-render happens after the drop.
# - ``_drag_handle()`` — the grab affordance. Only the handle carries
#   ``draggable``, never the whole card: a draggable ancestor breaks
#   text selection inside the row's own inputs.
# - ``_drop_target()`` — the row container. Highlighting is done in
#   ``js_handler`` (client-side only, no emit) because ``dragover``
#   fires continuously and a server round-trip per pixel would flood
#   the socket.
#
# The up/down arrows in ``_row_toolbar`` stay: they're the keyboard-
# reachable path, and dragging a tall expansion across a long list is
# fiddly however it's built.

_DRAG_OUTLINE = "2px dashed var(--matofx-accent, #40A1A4)"


def _new_drag_state() -> dict[str, int | None]:
    """A fresh ``{"index": None}`` holder for one list's render pass."""
    return {"index": None}


def _drag_handle(drag: dict[str, int | None], index: int) -> None:
    """The grab handle that starts a drag for row ``index``."""
    handle = (
        ui.element("div")
        .props("draggable=true")
        .style("cursor: grab; display: flex; align-items: center; padding: 0 0.15rem;")
    )
    with handle:
        ui.icon("drag_indicator").style("color: var(--matofx-muted-text, #888);")
    handle.on("dragstart", lambda _e=None, _i=index: drag.update(index=_i))
    handle.tooltip("Drag to reorder")


def _drop_target(
    container: Any,
    drag: dict[str, int | None],
    items: list[Any],
    index: int,
    rerender: _NoArgsCallable,
) -> None:
    """Make ``container`` accept a dragged sibling, landing it at ``index``.

    Dropping *on* a row inserts the dragged row after it when dragging
    down and before it when dragging up, so both ends of the list stay
    reachable — pop-then-insert-at-``index`` gives exactly that in both
    directions.
    """

    def _handler() -> None:
        source = drag["index"]
        drag["index"] = None
        if source is None or source == index or not 0 <= source < len(items):
            return
        items.insert(index, items.pop(source))
        rerender()

    container.on(
        "dragover",
        js_handler=(
            "(...a) => { a[0].preventDefault();"
            f" a[0].currentTarget.style.outline = '{_DRAG_OUTLINE}';"
            " a[0].currentTarget.style.outlineOffset = '2px'; }"
        ),
    )
    container.on(
        "dragleave",
        js_handler="(...a) => { a[0].currentTarget.style.outline = ''; }",
    )
    container.on(
        "drop",
        _handler,
        js_handler="(...a) => { a[0].currentTarget.style.outline = ''; emit(); }",
    )


def _reorderable_row(
    drag: dict[str, int | None],
    items: list[Any],
    index: int,
    rerender: _NoArgsCallable,
) -> Any:
    """A drop-target row carrying a leading drag handle.

    For the expansion-card lists (inputs / outputs), where the handle
    can't live in the card's own toolbar: that toolbar is inside the
    expansion *body* and so is invisible while the row is collapsed —
    which is exactly when you want to reorder.

    Returns the row so the caller can ``with`` it and add the card
    itself right after the handle.
    """
    row = ui.row().classes("w-full items-start gap-1 no-wrap")
    with row:
        _drag_handle(drag, index)
    _drop_target(row, drag, items, index, rerender)
    return row


# --- Tab 1: Input Groups -----------------------------------------------


def _render_input_groups_tab(state: dict[str, Any]) -> None:
    """Authoring tab for ``input_groups`` (visual cards in the input form)."""
    groups: list[dict[str, Any]] = state["input_groups"]
    container = ui.column().classes("w-full gap-2")

    def render() -> None:
        container.clear()
        with container:
            with ui.card().classes("w-full"):
                ui.label("Section behaviour in the job form").style("font-weight: 600;")
                ui.label(
                    "Controls how this tool's sections behave when a user fills in a new job."
                ).style("color: #888; font-size: 0.85rem;")

                collapsed_cb = ui.checkbox(
                    "Start every section collapsed",
                    value=bool(state.get("input_groups_start_collapsed", True)),
                )
                ui.label(
                    "On: the new-job form opens with all sections collapsed "
                    "and the user expands the ones they need. Off: every "
                    "section starts expanded."
                ).style("color: #888; font-size: 0.8rem;")

                require_cb = ui.checkbox(
                    "Require the user to open every section before Submit activates",
                    value=bool(state.get("require_all_groups_opened", True)),
                )
                require_hint = ui.label(
                    "Only applies when sections start collapsed — the user "
                    "must open each section at least once (re-collapsing is "
                    "fine) before the Submit button enables."
                ).style("color: #888; font-size: 0.8rem;")

                def _on_collapsed(e: Any) -> None:
                    state["input_groups_start_collapsed"] = bool(e.value)
                    # The review gate is meaningless when sections start
                    # expanded (everything's already revealed); disable +
                    # grey the control so the operator isn't misled.
                    if e.value:
                        require_cb.enable()
                        require_hint.style("color: #888; font-size: 0.8rem;")
                    else:
                        require_cb.disable()
                        require_hint.style("color: #888; font-size: 0.8rem; opacity: 0.5;")

                collapsed_cb.on_value_change(_on_collapsed)
                require_cb.on_value_change(
                    lambda e: state.__setitem__("require_all_groups_opened", bool(e.value))
                )
                _on_collapsed(cast(Any, type("E", (), {"value": collapsed_cb.value})()))

            ui.label(
                "Input groups are the visual cards your tool's input form is "
                "divided into. Define them first; the Inputs tab will let "
                "each field pick which group it belongs to. Drag a row by "
                "its handle to reorder."
            ).style("color: #888; font-size: 0.85rem;")
            drag = _new_drag_state()
            for index, group in enumerate(groups):
                card = ui.card().classes("w-full")
                with card, ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    _drag_handle(drag, index)
                    _bind_text(group, "name", label="Name *", width_class="flex-1 min-w-32")
                    _bind_text(group, "label", label="Label *", width_class="flex-1 min-w-32")
                    _bind_number(group, "column", label="Column *", min_value=1, step=1)
                    _bind_number(group, "row", label="Row *", min_value=1, step=1)
                    _bind_number(
                        group,
                        "sub_columns",
                        label="Sub-columns",
                        min_value=1,
                        max_value=4,
                        step=1,
                    )
                    _row_toolbar(groups, index, render)
                _drop_target(card, drag, groups, index, render)
            ui.button(
                "+ Add input group",
                on_click=_then(lambda: groups.append(empty_input_group()), render),
            ).props("flat color=primary")

    render()


# --- Tab 3: Output Groups ----------------------------------------------


def _render_output_groups_tab(state: dict[str, Any]) -> None:
    """Authoring tab for ``output_groups`` (no sub_columns)."""
    groups: list[dict[str, Any]] = state["output_groups"]
    container = ui.column().classes("w-full gap-2")

    def render() -> None:
        container.clear()
        with container:
            ui.label(
                "Output groups are the visual cards your tool's results panel is "
                "divided into. Define them first; the Outputs tab will let "
                "each field pick which group it belongs to. Drag a row by its "
                "handle to reorder."
            ).style("color: #888; font-size: 0.85rem;")
            drag = _new_drag_state()
            for index, group in enumerate(groups):
                card = ui.card().classes("w-full")
                with card, ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    _drag_handle(drag, index)
                    _bind_text(group, "name", label="Name *", width_class="flex-1 min-w-32")
                    _bind_text(group, "label", label="Label *", width_class="flex-1 min-w-32")
                    _bind_number(group, "column", label="Column *", min_value=1, step=1)
                    _bind_number(group, "row", label="Row *", min_value=1, step=1)
                    _row_toolbar(groups, index, render)
                _drop_target(card, drag, groups, index, render)
            ui.button(
                "+ Add output group",
                on_click=_then(lambda: groups.append(empty_output_group()), render),
            ).props("flat color=primary")

    render()


# --- Unit picker (shared by Inputs + Outputs editors) -------------------


def _render_unit_picker(field: dict[str, Any]) -> None:
    """Categorised unit dropdown + reveal-on-Custom text input."""
    custom_input: ui.input | None = None

    def _on_choice_change(e: Any) -> None:
        field["unit_choice"] = e.value
        if custom_input is not None:
            custom_input.set_visibility(e.value == UNIT_CUSTOM_VALUE)

    with ui.row().classes("flex-1 gap-2 items-center"):
        ui.select(
            UNIT_OPTIONS,
            label="Unit",
            value=field.get("unit_choice", UNIT_NONE_VALUE),
            with_input=True,
            on_change=_on_choice_change,
        ).classes("flex-1").props("dense")
        custom_input = (
            ui.input(
                "Custom unit",
                value=str(field.get("unit_custom") or ""),
                on_change=lambda e: field.__setitem__("unit_custom", e.value),
            )
            .classes("flex-1")
            .props("dense")
        )
        custom_input.set_visibility(field.get("unit_choice") == UNIT_CUSTOM_VALUE)


# --- Tab 2: Inputs ------------------------------------------------------


def _render_inputs_tab(
    state: dict[str, Any],
    wiring: UpstreamWiringContext,
    material_classes_with_props: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    """Authoring tab for ``inputs[]``. Group dropdown is fed by tab 1's state."""
    inputs: list[dict[str, Any]] = state["inputs"]
    container = ui.column().classes("w-full gap-3")

    def render() -> None:
        container.clear()
        # Show just the human-friendly label in the picker. Fall back to
        # the internal name only when an author hasn't filled in a label
        # yet — otherwise the dropdown reads as plain English.
        group_options = {
            g["name"]: str(g.get("label") or g["name"])
            for g in state["input_groups"]
            if g.get("name")
        }
        with container:
            _render_layout_widget(
                state, "input_column_layout", state["input_groups"], heading="Input column layout"
            )
            ui.separator()
            ui.label(
                "Each entry below is one form field your tool's input form will "
                "show. Pick a type, a unit, optional bounds + default, and the "
                "group it belongs to. Drag a field by its handle to reorder — "
                "the order here is the order it appears in, within its group."
            ).style("color: #888; font-size: 0.85rem;")
            drag = _new_drag_state()
            for index, field in enumerate(inputs):
                if index > 0:
                    # Visual divider between input cards so the operator
                    # can scan a long list at a glance.
                    ui.separator().style("opacity: 0.5;")
                _render_one_input(
                    field,
                    index=index,
                    inputs=inputs,
                    rerender=render,
                    drag=drag,
                    group_options=group_options,
                    wiring=wiring,
                    input_groups=state["input_groups"],
                    material_classes_with_props=material_classes_with_props,
                )
            ui.button(
                "+ Add input",
                on_click=_then(lambda: inputs.append(empty_input_field()), render),
            ).props("flat color=primary")

    render()


def _input_summary(field: dict[str, Any]) -> str:
    """Heading text for one input's expansion card."""
    return (
        f"{field.get('label') or '(unlabelled)'} "
        f"({field.get('name') or 'no name'}) — {field.get('type', 'string')}"
    )


def _subcard(title: str, caption: str = "") -> Any:
    """A bordered, shadow-free section card with a heading.

    Returns the card so the caller can re-enter it with ``with card:``
    to add the body. ``flat`` drops the Quasar shadow (durable "no
    drop shadow" rule); ``bordered`` gives the thin outline.
    """
    card = ui.card().classes("w-full gap-2").props("flat bordered")
    with card:
        ui.label(title).style("font-weight: 600; font-size: 0.85rem; color: #bbb;")
        if caption:
            ui.label(caption).style("color: #888; font-size: 0.78rem;")
    return card


def _render_one_input(
    field: dict[str, Any],
    *,
    index: int,
    inputs: list[dict[str, Any]],
    rerender: _NoArgsCallable,
    group_options: dict[str, str],
    wiring: UpstreamWiringContext,
    input_groups: list[dict[str, Any]],
    drag: dict[str, int | None],
    material_classes_with_props: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    """Render one input as a bordered card of grouped sub-cards.

    Each input parameter is a card-bordered expansion; its body is
    split into labelled sub-cards so the (many) properties read as
    coherent groups rather than one long row:

    - **Identity** — name + label (placeholders spell out the
      difference and the name rules) + help text.
    - **Type & value** — type + unit + the type-conditional editor
      (min/max/precision/options/default/radio/slider/accepted file
      types/default rows/columns); repainted on a type switch.
    - **Layout** — group + sub-column.
    - **Upstream** — auto-populated toggle + upstream tool.
    - **Dependencies** — the §16.16 rules that affect *this* field
      (repeatable) plus the cross-field sum check.
    - **Required** — the required toggle.

    The card sits in a drop-target row behind a drag handle, so a field
    can be reordered while collapsed (the row toolbar's arrows live in
    the expansion *body*, which a collapsed row hides).
    """
    with _reorderable_row(drag, inputs, index, rerender):
        expansion = (
            ui.expansion(_input_summary(field), value=not field.get("name"))
            .classes("flex-1 min-w-0")
            .style(
                "border: 1px solid rgba(255,255,255,0.30); border-radius: 6px; "
                "padding: 0.25rem 0.5rem;"
            )
        )
    refs: dict[str, Any] = {}

    def _refresh_summary() -> None:
        expansion.set_text(_input_summary(field))

    def _refresh_visibility() -> None:
        ftype = str(field.get("type") or "string")
        is_numeric = ftype in {"integer", "float"}
        is_choice = ftype in {"select", "multiselect"}
        # Unit applies to numeric and choice scalars only — string,
        # boolean, file and table inputs aren't unit-y.
        refs["unit_row"].set_visibility(is_numeric or is_choice)
        _render_per_type_editor()
        refs["sum_check_row"].set_visibility(is_numeric)
        _refresh_upstream_enabled()

    def _refresh_sub_column_max() -> None:
        # Look up the chosen group's ``sub_columns`` and clamp the
        # sub-column input's max accordingly. When no group is picked
        # or the group has only 1 sub-column the input is disabled —
        # picking 1-of-1 is meaningless.
        chosen = field.get("group") or ""
        group = next((g for g in input_groups if g.get("name") == chosen), None)
        max_value = int(group.get("sub_columns", 1)) if group else 1
        sub_col = refs["sub_column_input"]
        sub_col.props(f"max={max_value}")
        if max_value <= 1:
            sub_col.disable()
            field["sub_column"] = None
            sub_col.set_value(None)
        else:
            sub_col.enable()

    def _upstream_output_options(alias: str) -> dict[str, str]:
        """``{output_name: "Label (name)"}`` for the chosen upstream tool.

        Preserves a stored value that's no longer among the tool's outputs
        (renamed / removed) as an explicit "no longer exists" entry so the
        author sees the stale binding instead of a silent reset.
        """
        options: dict[str, str] = {}
        for out in wiring.outputs.get(alias, []):
            name = str(out.get("name") or "").strip()
            if not name:
                continue
            label = str(out.get("label") or "").strip() or name
            options[name] = f"{label} ({name})"
        current = field.get("upstream_output")
        if isinstance(current, str) and current and current not in options:
            options[current] = f"{current} — no longer exists"
        return options

    def _refresh_output_select() -> None:
        output = refs.get("upstream_output_select")
        if output is None:
            return
        alias = field.get("upstream_tool") or ""
        options = _upstream_output_options(alias) if alias else {}
        current = field.get("upstream_output")
        if current not in options:
            current = None
            field["upstream_output"] = None
        output.set_options(options, value=current)

    def _on_upstream_tool_change(event: Any) -> None:
        field["upstream_tool"] = event.value or None
        # A different tool means the previously-chosen output no longer
        # applies — reset and let the author pick from the new tool.
        field["upstream_output"] = None
        _refresh_output_select()

    def _on_output_change(event: Any) -> None:
        field["upstream_output"] = event.value or None

    def _add_source_options() -> dict[str, str]:
        return {
            alias: name
            for alias, name in wiring.all_tools.items()
            if alias not in wiring.candidates and alias != wiring.current_alias
        }

    def _reveal_add_source() -> None:
        # Show the second picker only on demand — keeping a single
        # tool selector visible at rest avoids the two-dropdown confusion.
        row = refs.get("add_source_row")
        button = refs.get("add_source_button")
        if row is not None:
            row.visible = True
        if button is not None:
            button.visible = False

    async def _on_add_source_pick(event: Any) -> None:
        picked = event.value
        if not picked:
            return
        # Pull the chosen tool's outputs in on demand, then treat it like
        # any other candidate. The actual write to its ``downstream_tools``
        # is reconciled when the edited tool is saved.
        if picked not in wiring.outputs and wiring.fetch_outputs is not None:
            try:
                wiring.outputs[picked] = await wiring.fetch_outputs(picked)
            except Exception:  # noqa: BLE001 — degrade to no-outputs, never crash the form
                wiring.outputs[picked] = []
        wiring.candidates[picked] = wiring.all_tools.get(picked, picked)
        field["upstream_tool"] = picked
        field["upstream_output"] = None
        upstream = refs.get("upstream_select")
        if upstream is not None:
            upstream.set_options({"": "(none)"} | dict(wiring.candidates), value=picked)
        _refresh_output_select()
        add_select = refs.get("add_source_select")
        if add_select is not None:
            add_select.set_value(None)
            add_select.set_options(_add_source_options())
        row = refs.get("add_source_row")
        button = refs.get("add_source_button")
        if row is not None:
            row.visible = False
        if button is not None:
            button.visible = True

    def _refresh_upstream_enabled() -> None:
        # The upstream controls are meaningful only when the input is
        # flagged as auto-populated. Disable rather than hide so the
        # operator can still see what was previously chosen on toggle.
        upstream = refs.get("upstream_select")
        output = refs.get("upstream_output_select")
        button = refs.get("add_source_button")
        row = refs.get("add_source_row")
        if upstream is None:
            return
        if field.get("downstream_input"):
            upstream.enable()
            if output is not None:
                output.enable()
            if button is not None:
                button.enable()
            _refresh_output_select()
        else:
            field["upstream_tool"] = None
            field["upstream_output"] = None
            upstream.disable()
            upstream.set_value("")
            if output is not None:
                output.set_options({}, value=None)
                output.disable()
            if button is not None:
                button.disable()
            if row is not None:
                row.visible = False

    def _render_per_type_editor() -> None:
        """Repaint row 2 — type-conditional widgets including the default."""
        container = refs["per_type_container"]
        container.clear()
        ftype = str(field.get("type") or "string")
        with container, ui.row().classes("w-full gap-2 items-center flex-wrap"):
            if ftype in {"integer", "float"}:
                _render_numeric_row2(field, ftype)
            elif ftype in {"select", "multiselect"}:
                _render_choice_row2(field, ftype)
            elif ftype == "file":
                _render_file_row2(field)
            elif ftype == "material":
                _render_material_row2(field, material_classes_with_props)
            elif ftype == "boolean":
                _render_default_widget(field, ftype)
            elif ftype == "table":
                # Tables don't fit on a single horizontal row — fall
                # through to a vertical sub-editor below the row.
                pass
            else:  # string
                _render_default_widget(field, ftype)
        if str(field.get("type") or "") == "table":
            with container:
                _render_table_editor(field, with_default_rows=True)

    def _live_siblings() -> list[str]:
        return [i["name"] for i in inputs if i.get("name") and i.get("name") != field.get("name")]

    def _populate_sum_check_options() -> None:
        """Re-paint the sum-check body with up-to-date sibling names."""
        body = refs["sum_check_body"]
        body.clear()
        live_siblings = _live_siblings()
        with body, ui.row().classes("w-full gap-2 items-center"):
            _bind_number(field, "sum_check_value", label="Required sum value", step=0.001)
            _bind_select(
                field,
                "sum_check_targets",
                label="Siblings that must sum to this *",
                options={n: n for n in live_siblings},
                multiple=True,
            )
        if not live_siblings:
            with body:
                ui.label("Add at least one other named input above before picking siblings.").style(
                    "color: #888; font-size: 0.85rem;"
                )

    def _formula_hint() -> str:
        sibs = _live_siblings()
        return (
            "A formula over other inputs. "
            f"Available: {', '.join(sibs) if sibs else '(none yet)'}. "
            "Operators: + - * / == != < <= > >= and or not in ( ). "
            "Example: inner_diameter + 1"
        )

    def _paint_outcome_params(outcome: dict[str, Any], holder: Any) -> None:
        """Repaint one outcome's effect-specific settings."""
        holder.clear()
        effect = str(outcome.get("effect") or "show")

        def _repaint() -> None:
            _paint_outcome_params(outcome, holder)

        with holder, ui.column().classes("w-full gap-2"):
            if effect == "hide":
                _bind_select(
                    outcome,
                    "on_hide",
                    label="If hidden, the value should…",
                    options=_ON_HIDE_CHOICES,
                )
            elif effect in ("set_value", "set_default"):
                _bind_select(
                    outcome,
                    "value_source",
                    label="Set it to…",
                    options=_VALUE_SOURCE_CHOICES,
                    on_change_extra=_repaint,
                )
                if str(outcome.get("value_source") or "fixed") == "formula":
                    _bind_text(
                        outcome,
                        "value_expr",
                        label="Formula",
                        placeholder="e.g. base_length / 2",
                        width_class="w-full",
                    )
                    ui.label(_formula_hint()).style("color: #888; font-size: 0.72rem;")
                else:
                    _bind_text(
                        outcome,
                        "value",
                        label="Value",
                        placeholder="entered for this field's type (e.g. 0, true, low)",
                        width_class="flex-1 min-w-48",
                    )
            elif effect == "set_required":
                _bind_checkbox(outcome, "required", label="Make required (untick = make optional)")
            elif effect == "set_min_max":
                _paint_bound(outcome, "minimum", "Minimum", _repaint)
                _paint_bound(outcome, "maximum", "Maximum", _repaint)
            elif effect == "require":
                _bind_text(
                    outcome,
                    "require_expr",
                    label="This must be true *",
                    placeholder="e.g. outer_diameter > inner_diameter",
                    width_class="w-full",
                )
                ui.label(_formula_hint()).style("color: #888; font-size: 0.72rem;")
                _bind_text(
                    outcome,
                    "require_message",
                    label="Message if not (optional)",
                    placeholder="Leave blank to auto-generate from the inputs' labels.",
                    width_class="w-full",
                )
            elif effect == "set_options":
                _bind_text(
                    outcome,
                    "options_csv",
                    label="New options (comma-separated)",
                    placeholder="e.g. low, medium, high",
                    width_class="flex-1 min-w-48",
                )
            else:  # show / disable — nothing else to configure
                ui.label("No extra settings for this effect.").style(
                    "color: #888; font-size: 0.8rem;"
                )

    def _paint_bound(outcome: dict[str, Any], which: str, label: str, repaint: Any) -> None:
        """One bound row of a set_min_max outcome: fixed number or formula."""
        source_key = f"{which}_source"
        with ui.row().classes("w-full gap-2 items-center flex-wrap"):
            _bind_select(
                outcome,
                source_key,
                label=f"{label} is…",
                options=_VALUE_SOURCE_CHOICES,
                on_change_extra=repaint,
            )
            if str(outcome.get(source_key) or "fixed") == "formula":
                _bind_text(
                    outcome,
                    f"{which}_expr",
                    label=f"{label} formula",
                    placeholder="e.g. inner_diameter",
                    width_class="flex-1 min-w-48",
                )
            else:
                _bind_number(outcome, which, label=label, step=0.001)

    def _render_dependencies() -> None:
        """Rebuild the per-input rule list inside the Dependencies card.

        A rule is ``when`` + an ordered list of outcomes; every outcome
        fires (in order) when the condition is true, so an author can
        say "if a == 1 then set b = 2 *and* lock b" as one rule.
        """
        container = refs["deps_container"]
        container.clear()
        rules: list[dict[str, Any]] = field.setdefault("dependencies", [])
        siblings = _live_siblings()
        hint = (
            "Refers to other inputs by name. Leave blank to always apply. "
            f"Available: {', '.join(siblings) if siblings else '(none yet)'}. "
            "Operators: == != < <= > >= and or not in ( ). "
            "Example: mode == 'auto' and pressure > 10"
        )
        with container:
            if not rules:
                ui.label(
                    "No rules yet — add one to show/hide/lock this field, change "
                    "its value, default, options or bounds (a fixed value or a "
                    "formula from other inputs), or require something to be true. "
                    "Bounds, values and validity rules are honoured live, at "
                    "submit, and in studies / optimisation."
                ).style("color: #888; font-size: 0.82rem;")
            for r_index, rule in enumerate(rules):
                with ui.card().classes("w-full gap-2").props("flat bordered"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label(f"Rule {r_index + 1}").style(
                            "font-weight: 600; font-size: 0.8rem; color: #aaa;"
                        )
                        _row_toolbar(rules, r_index, _render_dependencies)
                    _bind_text(
                        rule,
                        "when",
                        label="When this is true (blank = always)",
                        placeholder="e.g. cooling_type == 'forced' — or leave blank",
                        width_class="w-full",
                    )
                    ui.label(hint).style("color: #888; font-size: 0.75rem;")
                    outcomes: list[dict[str, Any]] = rule.setdefault("outcomes", [])
                    if not outcomes:
                        outcomes.append(_empty_outcome())
                    ui.label("Then, for this field — apply every outcome below:").style(
                        "color: #aaa; font-size: 0.78rem; font-weight: 600;"
                    )
                    for o_index, outcome in enumerate(outcomes):
                        with ui.card().classes("w-full gap-2 ml-2").props("flat bordered"):
                            with ui.row().classes("w-full items-center justify-between"):
                                ui.label(f"Outcome {o_index + 1}").style(
                                    "font-size: 0.75rem; color: #888;"
                                )
                                _row_toolbar(outcomes, o_index, _render_dependencies)
                            params_holder = ui.column().classes("w-full")

                            def _on_effect_change(
                                _o: dict[str, Any] = outcome, _h: Any = params_holder
                            ) -> None:
                                _paint_outcome_params(_o, _h)

                            _bind_select(
                                outcome,
                                "effect",
                                label="Effect",
                                options=DEPENDENCY_EFFECTS,
                                on_change_extra=_on_effect_change,
                            )
                            _paint_outcome_params(outcome, params_holder)

                    def _add_outcome(_o: list[dict[str, Any]] = outcomes) -> None:
                        _o.append(_empty_outcome())

                    ui.button(
                        "+ Add outcome",
                        on_click=_then(_add_outcome, _render_dependencies),
                    ).props("flat color=primary dense")
            ui.button(
                "+ Add rule",
                on_click=_then(
                    lambda: field.setdefault("dependencies", []).append(empty_dependency_rule()),
                    _render_dependencies,
                ),
            ).props("flat color=primary")

    with expansion, ui.column().classes("w-full gap-3"):
        with ui.row().classes("w-full justify-end"):
            _row_toolbar(inputs, index, rerender)

        # --- Identity ----------------------------------------------
        identity = _subcard("Identity")
        with identity, ui.row().classes("w-full gap-2 items-center flex-wrap"):
            _bind_text(
                field,
                "name",
                label="Name *",
                placeholder="lowercase id, no spaces — e.g. inlet_pressure",
                width_class="flex-1 min-w-32",
                on_change_extra=_refresh_summary,
            )
            _bind_text(
                field,
                "label",
                label="Label *",
                placeholder="what the user sees — e.g. Inlet pressure",
                width_class="flex-1 min-w-32",
                on_change_extra=_refresh_summary,
            )
        with identity:
            ui.label(
                "Name is the programmatic identifier the tool reads: lowercase "
                "letters, digits and underscores only, no spaces (e.g. "
                "inlet_pressure). Label is the human text shown on the form "
                "(e.g. Inlet pressure)."
            ).style("color: #888; font-size: 0.78rem;")
            _bind_text(field, "help_text", label="Help text", multiline=True)

        # --- Type & value ------------------------------------------
        type_card = _subcard("Type & value")
        with type_card, ui.row().classes("w-full gap-2 items-center flex-wrap"):
            _bind_select(
                field,
                "type",
                label="Type *",
                options=_input_type_options(field),
                on_change_extra=_then(
                    lambda: _normalise_type_dependent_state(field),
                    _refresh_summary,
                    _refresh_visibility,
                ),
            )
            with ui.row().classes("flex-1 min-w-48 gap-2 items-center") as unit_row:
                _render_unit_picker(field)
            refs["unit_row"] = unit_row
        with type_card:
            per_type_container = ui.column().classes("w-full gap-2")
            refs["per_type_container"] = per_type_container

        # --- Layout ------------------------------------------------
        layout_card = _subcard("Layout")
        with layout_card, ui.row().classes("w-full gap-2 items-center flex-wrap"):
            _bind_select(
                field,
                "group",
                label="Group",
                options=({"": "(none)"} | group_options),
                on_change_extra=_refresh_sub_column_max,
            )
            sub_col = _bind_number(field, "sub_column", label="Sub-column", min_value=1, step=1)
            refs["sub_column_input"] = sub_col

        # --- Upstream ----------------------------------------------
        upstream_card = _subcard(
            "Upstream",
            "Fill this field automatically from another tool's output. "
            "Only tools that list this tool as a downstream tool are offered; "
            "use “Make another tool a source” to add one (it's wired up "
            "when you save).",
        )
        with upstream_card:
            _bind_checkbox(
                field,
                "downstream_input",
                label="Auto-populated from upstream tool",
                on_change_extra=_refresh_upstream_enabled,
            )
            # Ensure an already-wired upstream stays visible even if it no
            # longer declares this tool downstream (or was added this
            # session) — otherwise the select would drop the stored value.
            stored_upstream = field.get("upstream_tool")
            candidate_options = dict(wiring.candidates)
            if isinstance(stored_upstream, str) and stored_upstream:
                candidate_options.setdefault(
                    stored_upstream, wiring.all_tools.get(stored_upstream, stored_upstream)
                )
            with ui.row().classes("w-full gap-2 items-center flex-wrap"):
                upstream = ui.select(
                    {"": "(none)"} | candidate_options,
                    label="Upstream tool",
                    value=stored_upstream or "",
                    on_change=_on_upstream_tool_change,
                ).classes("flex-1 min-w-48").props("dense")
                refs["upstream_select"] = upstream
                output_select = ui.select(
                    _upstream_output_options(stored_upstream or ""),
                    label="Upstream output",
                    value=field.get("upstream_output") or None,
                    on_change=_on_output_change,
                ).classes("flex-1 min-w-48").props("dense")
                refs["upstream_output_select"] = output_select
            add_source_button = ui.button(
                "+ Make another tool a source", on_click=_reveal_add_source
            ).props("flat dense color=primary")
            refs["add_source_button"] = add_source_button
            add_source_row = ui.row().classes("w-full gap-2 items-center flex-wrap")
            add_source_row.visible = False
            refs["add_source_row"] = add_source_row
            with add_source_row:
                add_source_select = ui.select(
                    _add_source_options(),
                    label="Pick a tool to make available as a source",
                    with_input=True,
                    on_change=_on_add_source_pick,
                ).classes("flex-1 min-w-64").props("dense")
                refs["add_source_select"] = add_source_select
                ui.label(
                    "Saving updates that tool to offer its output here "
                    "(a small version bump)."
                ).style("color: #9a7d0a; font-size: 0.78rem;")

        # --- Dependencies ------------------------------------------
        deps_card = _subcard(
            "Dependencies",
            "Make this field react to other inputs, and constrain sibling sums.",
        )
        with deps_card:
            deps_container = ui.column().classes("w-full gap-2")
            refs["deps_container"] = deps_container
            sum_check_expansion = ui.expansion(
                "Cross-field sum check",
                caption="Constrain a set of inputs to a fixed total",
            ).classes("w-full")
            with sum_check_expansion:
                ui.label(
                    "Use this when several sibling inputs must sum to a fixed "
                    "value (e.g. percentages totalling 100). Pick two or more "
                    "siblings."
                ).style("color: #888; font-size: 0.85rem;")
                sum_check_body = ui.column().classes("w-full")
                refs["sum_check_body"] = sum_check_body
            sum_check_expansion.on_value_change(
                lambda e: _populate_sum_check_options() if e.value else None
            )
            refs["sum_check_row"] = sum_check_expansion

        # --- Required ----------------------------------------------
        required_card = _subcard("Required")
        with required_card:
            _bind_checkbox(
                field,
                "required",
                label="The user must fill this in before submitting",
            )

    _render_dependencies()
    _populate_sum_check_options()
    _refresh_visibility()
    _refresh_sub_column_max()
    _refresh_upstream_enabled()


def _render_numeric_row2(field: dict[str, Any], ftype: str) -> None:
    """Row 2 for numeric inputs: min, max, precision, default, slider tickbox."""
    is_integer = ftype == "integer"
    if is_integer:
        _bind_int_number(field, "minimum", label="Minimum")
        _bind_int_number(field, "maximum", label="Maximum")
        field["precision"] = 1.0
        elem = ui.number("Precision", value=1, step=1).classes("flex-1").props("dense readonly")
        elem.disable()
    else:
        _bind_number(field, "minimum", label="Minimum", step=0.001)
        _bind_number(field, "maximum", label="Maximum", step=0.001)
        _bind_select(
            field,
            "precision",
            label="Precision (step) *",
            options=_FLOAT_PRECISION_OPTIONS,
        )
    _render_default_widget(field, ftype)

    def _on_slider_toggle() -> None:
        field["widget"] = "slider" if field.get("widget_is_slider") else "field"

    field["widget_is_slider"] = field.get("widget") == "slider"
    _bind_checkbox(
        field,
        "widget_is_slider",
        label="Show as slider",
        on_change_extra=_on_slider_toggle,
    )


def _render_choice_row2(field: dict[str, Any], _ftype: str) -> None:
    """Row 2 for select / multiselect: options CSV, default-from-options, radio tickbox."""
    _sync_options_csv(field)

    _bind_text(
        field,
        "options_csv",
        label="Options (comma-separated) *",
        placeholder="e.g. low, medium, high",
        width_class="flex-1 min-w-32",
        on_change_extra=lambda: _flush_options_csv(field),
    )
    _render_default_widget(field, _ftype)

    def _on_radio_toggle() -> None:
        field["choice_widget"] = "radio" if field.get("choice_is_radio") else "dropdown"

    field["choice_is_radio"] = field.get("choice_widget") == "radio"
    _bind_checkbox(
        field,
        "choice_is_radio",
        label="Show as radio buttons",
        on_change_extra=_on_radio_toggle,
    )


def _render_file_row2(field: dict[str, Any]) -> None:
    """Row 2 for file inputs: accepted types CSV, max size MB, no-max tickbox."""
    _sync_accepted_file_types_csv(field)
    field["no_max_file_size"] = field.get("max_file_size_mb") is None

    size_widget_holder: dict[str, Any] = {}

    def _on_no_max_change() -> None:
        widget = size_widget_holder.get("widget")
        if widget is None:
            return
        if field.get("no_max_file_size"):
            field["max_file_size_mb"] = None
            widget.set_value(None)
            widget.disable()
        else:
            widget.enable()

    _bind_text(
        field,
        "accepted_file_types_csv",
        label="Accepted file types (comma-separated)",
        placeholder="e.g. csv, xlsx, json",
        width_class="flex-1 min-w-32",
        on_change_extra=lambda: _flush_accepted_file_types_csv(field),
    )
    size_widget = _bind_number(
        field,
        "max_file_size_mb",
        label="Max size (MB)",
        min_value=0,
        step=1,
    )
    size_widget_holder["widget"] = size_widget
    _bind_checkbox(
        field,
        "no_max_file_size",
        label="No maximum",
        on_change_extra=_on_no_max_change,
    )
    _on_no_max_change()
    _bind_text(
        field,
        "expected_field_array",
        label="Expected field array (optional)",
        placeholder="e.g. temperature",
        width_class="flex-1 min-w-32",
    )


def _normalise_type_dependent_state(field: dict[str, Any]) -> None:
    """Coerce stored fields to match a freshly-picked type.

    Switching ``string → integer`` (etc.) leaves stale values behind in
    ``default`` / ``precision`` / ``options`` / ``columns``. This
    helper resets the keys that don't make sense for the new type so
    the per-type re-render starts clean and the descriptor payload
    doesn't carry junk.
    """
    ftype = str(field.get("type") or "string")
    if ftype != "integer":
        # Leave alone for now; integer's rule is enforced when integer.
        pass
    if ftype == "integer":
        field["precision"] = 1.0
    elif ftype == "float":
        # Float keeps user-set precision if it's already in the menu;
        # otherwise reset to None so the dropdown shows nothing picked.
        if field.get("precision") not in FLOAT_PRECISION_CHOICES:
            field["precision"] = None
    else:
        field["precision"] = None
    if ftype not in {"integer", "float"}:
        field["minimum"] = None
        field["maximum"] = None
        field["widget"] = "field"
        field["sum_check_value"] = None
        field["sum_check_targets"] = []
    if ftype not in {"select", "multiselect"}:
        field["choice_widget"] = "dropdown"
        field["options"] = []
        field["options_csv"] = ""
    if ftype != "file":
        field["accepted_file_types"] = []
        field["max_file_size_mb"] = None
        field["expected_field_array"] = None
    if ftype != "material":
        field["material_class"] = None
        field["required_properties"] = []
        field["required_properties_csv"] = ""
    if ftype != "table":
        field["columns"] = []
        field["default_rows"] = None
    # Reset default — the widget that edits it is about to be repainted.
    field["default"] = None


def _render_default_widget(field: dict[str, Any], ftype: str) -> None:
    """Render the type-aware default editor for one input field.

    Each branch produces a single widget that writes through to
    ``field["default"]`` in the type the descriptor expects (string,
    int, float, bool/None, str-from-options, list-of-str-from-options).
    """
    if ftype == "string":
        _bind_text(
            field,
            "default",
            label="Default",
            placeholder="Pre-filled string (leave blank for none)",
            width_class="w-full",
        )
        return
    if ftype == "integer":
        # Reuse the int-coercing helper so a typed-in fractional value
        # gets truncated + the operator gets the warning toast — same
        # behaviour as the min/max widgets above.
        _bind_int_number(
            field,
            "default",
            label="Default",
            min_value=field.get("minimum"),
            max_value=field.get("maximum"),
            width_class="w-full",
        )
        return
    if ftype == "float":

        def _on_float_change(e: Any) -> None:
            field["default"] = float(e.value) if e.value is not None and e.value != "" else None

        elem = (
            ui.number(
                "Default",
                value=field.get("default"),
                step=field.get("precision") or 0.001,
                min=field.get("minimum"),
                max=field.get("maximum"),
            )
            .classes("w-full")
            .props("dense")
        )
        elem.on_value_change(_on_float_change)
        return
    if ftype == "boolean":

        def _on_bool_change(e: Any) -> None:
            field["default"] = _sentinel_to_bool_default(str(e.value))

        ui.select(
            _BOOL_DEFAULT_OPTIONS,
            label="Default",
            value=_bool_default_to_sentinel(field.get("default")),
            on_change=_on_bool_change,
        ).classes("w-full").props("dense")
        return
    if ftype == "select":
        opts = list(field.get("options") or [])
        choice_options = {"": "(no default)", **{o: o for o in opts}}

        def _on_select_change(e: Any) -> None:
            v = e.value
            field["default"] = v if v else None

        current = field.get("default") if field.get("default") in opts else ""
        ui.select(
            choice_options,
            label="Default",
            value=current,
            on_change=_on_select_change,
        ).classes("w-full").props("dense")
        return
    if ftype == "multiselect":
        opts = list(field.get("options") or [])
        opt_map = {o: o for o in opts}
        current_default = field.get("default")
        if not isinstance(current_default, list):
            current_default = []
        # Drop any stale entries that don't belong to the current options.
        current_default = [v for v in current_default if v in opts]
        field["default"] = current_default if current_default else None

        def _on_ms_change(e: Any) -> None:
            v = list(e.value or [])
            field["default"] = v if v else None

        ui.select(
            opt_map,
            label="Default",
            value=current_default,
            multiple=True,
            on_change=_on_ms_change,
        ).classes("w-full").props("dense use-chips")
        return


def _sync_accepted_file_types_csv(field: dict[str, Any]) -> None:
    """Initialise the CSV-shaped representation of ``accepted_file_types``."""
    items = list(field.get("accepted_file_types") or [])
    if "accepted_file_types_csv" not in field:
        field["accepted_file_types_csv"] = ", ".join(str(o) for o in items)


def _flush_accepted_file_types_csv(field: dict[str, Any]) -> None:
    """Re-parse the visible CSV string into ``accepted_file_types``."""
    text = str(field.get("accepted_file_types_csv") or "")
    field["accepted_file_types"] = [
        item.strip().lstrip(".").lower() for item in text.split(",") if item.strip()
    ]


def _flush_options_csv(field: dict[str, Any]) -> None:
    """Re-parse the visible CSV string into the underlying list field."""
    text = str(field.get("options_csv") or "")
    field["options"] = [item.strip() for item in text.split(",") if item.strip()]


def _sync_required_properties_csv(field: dict[str, Any]) -> None:
    """Initialise the CSV-shaped representation of ``required_properties``."""
    if "required_properties_csv" not in field:
        items = list(field.get("required_properties") or [])
        field["required_properties_csv"] = ", ".join(str(o) for o in items)


def _flush_required_properties_csv(field: dict[str, Any]) -> None:
    """Re-parse the visible CSV string into ``required_properties``."""
    text = str(field.get("required_properties_csv") or "")
    field["required_properties"] = [item.strip() for item in text.split(",") if item.strip()]


def _render_material_row2(
    field: dict[str, Any],
    material_classes_with_props: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    """Row 2 for material inputs: the required class + optional required props.

    A ``material`` field declares a *requirement* (its class + the
    properties the tool needs), not options or a default — the user
    picks a concrete material at submit, which is frozen into
    ``inputs.json`` (ADR-0007 §6).

    When ``material_classes_with_props`` is provided (a
    ``{class_name: [prop_name, ...]}`` map the admin page pre-fetches
    from ``/api/materials/classes``), the class becomes a select over
    the live catalogue and required-properties becomes a multiselect
    filtered by the chosen class. Without it (e.g. tests, or the
    materials feature off), the editor falls back to plain text inputs
    so the wizard always works.
    """
    _sync_required_properties_csv(field)
    if not material_classes_with_props:
        _bind_text(
            field,
            "material_class",
            label="Material class *",
            placeholder="e.g. Fluid",
            width_class="flex-1 min-w-32",
        )
        _bind_text(
            field,
            "required_properties_csv",
            label="Required properties (comma-separated, optional)",
            placeholder="e.g. thermal_conductivity, density",
            width_class="flex-1 min-w-48",
            on_change_extra=lambda: _flush_required_properties_csv(field),
        )
        ui.label(
            "The class must already exist in the materials database. Leave properties "
            "blank to require the class's own required properties."
        ).style("color: #888; font-size: 0.78rem;")
        return

    # Live catalogue: class select + properties multiselect filtered by class.
    class_opts: dict[str, str] = {name: name for name in sorted(material_classes_with_props.keys())}
    current_class = str(field.get("material_class") or "")
    # If a previously-saved class isn't in the catalogue (renamed /
    # retired), surface it as a one-off so editing doesn't drop it.
    if current_class and current_class not in class_opts:
        class_opts = {current_class: f"{current_class} (not in catalogue)", **class_opts}

    props_holder: dict[str, Any] = {"box": None}

    def _rebuild_props_select() -> None:
        box = props_holder["box"]
        if box is None:
            return
        box.clear()
        cls = str(field.get("material_class") or "")
        # Map of {slug: display_label} for the chosen class.
        avail = {
            p["slug"]: p.get("prop_name") or p["slug"]
            for p in material_classes_with_props.get(cls, [])
        }
        # Drop saved entries (slugs) that aren't valid for the new class.
        kept = [s for s in (field.get("required_properties") or []) if s in avail]
        if kept != list(field.get("required_properties") or []):
            field["required_properties"] = kept
            field["required_properties_csv"] = ", ".join(kept)
        with box:
            if not cls:
                ui.label("Pick a class to choose its required properties.").style(
                    "color: #888; font-size: 0.78rem;"
                )
                return
            if not avail:
                ui.label(
                    f"'{cls}' has no properties yet — declare some in Materials → Manage classes."
                ).style("color: #888; font-size: 0.78rem;")
                return
            # Options dict is {slug: "Display Name (slug)"} so the
            # operator sees the friendly label *and* the immutable
            # identifier the descriptor will record.
            options = {slug: f"{label} ({slug})" for slug, label in avail.items()}
            ms = (
                ui.select(
                    options,
                    label="Required properties (optional)",
                    multiple=True,
                    value=kept,
                )
                .props("dense use-chips")
                .classes("w-full")
            )

            def _on_ms(e: Any) -> None:
                v = list(e.value or [])
                field["required_properties"] = v
                field["required_properties_csv"] = ", ".join(v)

            ms.on_value_change(_on_ms)

    _bind_select(
        field,
        "material_class",
        label="Material class *",
        options=class_opts,
        with_input=True,
        width_class="flex-1 min-w-32",
        on_change_extra=_rebuild_props_select,
    )
    props_holder["box"] = ui.column().classes("flex-1 min-w-48 gap-1")
    _rebuild_props_select()
    ui.label(
        "Class options come from the live materials catalogue. The user picks a "
        "concrete material of this class at submit time."
    ).style("color: #888; font-size: 0.78rem;")


def _render_table_editor(field: dict[str, Any], *, with_default_rows: bool) -> None:
    """Sub-editor for ``type == "table"`` inputs / outputs.

    Operator declares the column shape up front; at job-submit time the
    user fills in rows. ``with_default_rows`` adds a "Default rows"
    number above the column list — used by inputs (so the form starts
    with N empty rows) but not outputs (the runner controls rows).

    Each column carries the same authoring rules as a scalar input:
    unit + min/max/precision for numerics (integer precision locked
    to 1, float precision picked from :data:`FLOAT_PRECISION_CHOICES`),
    a type-aware default cell. String / boolean columns omit the
    unit + numeric rows.
    """
    columns: list[dict[str, Any]] = field["columns"]
    container = ui.column().classes("w-full gap-1")

    def render() -> None:
        container.clear()
        with container:
            if with_default_rows:
                with ui.row().classes("w-full items-center gap-4"):
                    _bind_number(
                        field,
                        "default_rows",
                        label="Default rows",
                        min_value=0,
                        max_value=1024,
                        step=1,
                    )
                    _bind_checkbox(
                        field,
                        "plot_enabled",
                        label="Show 'view as plot' button",
                    )
                ui.label(
                    "When on, the job form offers a plot of the entered rows: the "
                    "first column is the x-axis, each numeric column after it a series."
                ).style("color: #888; font-size: 0.8rem;")
            ui.label("Columns *").style("font-weight: 500;")
            ui.label(
                "At least one column is required. The user adds rows when "
                "submitting a job; each cell uses the column's type. Drag a "
                "column by its handle to reorder."
            ).style("color: #888; font-size: 0.8rem;")
            drag = _new_drag_state()
            for index, column in enumerate(columns):
                _render_one_table_column(columns, index, column, render, drag=drag)
            ui.button(
                "+ Add column",
                on_click=_then(lambda: columns.append(empty_table_column()), render),
            ).props("flat color=primary")

    render()


def _render_one_table_column(
    columns: list[dict[str, Any]],
    index: int,
    column: dict[str, Any],
    rerender: _NoArgsCallable,
    *,
    drag: dict[str, int | None],
) -> None:
    """One column card inside the table sub-editor.

    Layout mirrors the scalar-input card's first two rows.

    Row 1: drag handle + name + label (with placeholder explanations) +
    type + unit + toolbar. Unit is hidden for ``string`` / ``boolean``.
    Row 2: per-type widgets. For ``integer`` / ``float`` columns:
    minimum + maximum + precision + default. For ``string`` /
    ``boolean``: default. Integer columns force min / max / default
    to whole numbers (step = 1, coerced on change).

    The whole row is repainted on a type change because the widget
    shapes inside differ.
    """
    refs: dict[str, Any] = {}

    def _render_row1_unit_block() -> None:
        unit_block = refs["unit_block"]
        unit_block.clear()
        ctype = str(column.get("type") or "string")
        with unit_block:
            if ctype not in {"string", "boolean"}:
                _render_unit_picker(column)

    def _render_row2() -> None:
        row2 = refs["row2"]
        row2.clear()
        ctype = str(column.get("type") or "string")
        if ctype not in {"integer", "float", "string", "boolean"}:
            return
        with row2, ui.row().classes("w-full gap-2 items-center flex-wrap"):
            if ctype == "integer":
                _bind_int_number(column, "minimum", label="Minimum")
                _bind_int_number(column, "maximum", label="Maximum")
                column["precision"] = 1.0
                elem = (
                    ui.number("Precision", value=1, step=1)
                    .classes("flex-1")
                    .props("dense readonly")
                )
                elem.disable()
                _render_default_widget(column, ctype)
            elif ctype == "float":
                _bind_number(column, "minimum", label="Minimum", step=0.001)
                _bind_number(column, "maximum", label="Maximum", step=0.001)
                _bind_select(
                    column,
                    "precision",
                    label="Precision *",
                    options=_FLOAT_PRECISION_OPTIONS,
                )
                _render_default_widget(column, ctype)
            else:  # string / boolean
                _render_default_widget(column, ctype)

    card = ui.card().classes("w-full gap-2")
    with card:
        # Row 1 — identity.
        with ui.row().classes("w-full gap-2 items-center flex-wrap"):
            _drag_handle(drag, index)
            _bind_text(
                column,
                "name",
                label="Name *",
                placeholder="programmatic identifier (e.g. material)",
                width_class="flex-1 min-w-32",
            )
            _bind_text(
                column,
                "label",
                label="Label *",
                placeholder="human-readable label shown to the user",
                width_class="flex-1 min-w-32",
            )
            _bind_select(
                column,
                "type",
                label="Type",
                options=_TABLE_COLUMN_TYPES,
                on_change_extra=_then(
                    lambda: _normalise_table_column_state(column),
                    _render_row1_unit_block,
                    _render_row2,
                ),
            )
            unit_block = ui.row().classes("flex-1 min-w-48 gap-2 items-center")
            refs["unit_block"] = unit_block
            _row_toolbar(columns, index, rerender)
        # Row 2 — type-conditional widgets.
        row2 = ui.column().classes("w-full")
        refs["row2"] = row2
    _drop_target(card, drag, columns, index, rerender)

    _render_row1_unit_block()
    _render_row2()


def _normalise_table_column_state(column: dict[str, Any]) -> None:
    """Reset type-incompatible cells when a table column's type changes."""
    ctype = str(column.get("type") or "string")
    if ctype not in {"integer", "float"}:
        column["minimum"] = None
        column["maximum"] = None
        column["precision"] = None
    elif ctype == "integer":
        column["precision"] = 1.0
    elif ctype == "float":
        if column.get("precision") not in FLOAT_PRECISION_CHOICES:
            column["precision"] = None
    if ctype in {"string", "boolean"}:
        column["unit_choice"] = UNIT_NONE_VALUE
        column["unit_custom"] = ""
    column["default"] = None


def _sync_options_csv(field: dict[str, Any]) -> None:
    """Initialise + listen on the CSV-shaped representation of ``options``.

    The wizard exposes ``options`` to the operator as a comma-separated
    string for ergonomics; this helper keeps the underlying list field
    in sync with the visible string. Trailing/leading whitespace and
    empty entries are stripped on each change.
    """
    options_list = list(field.get("options") or [])
    if "options_csv" not in field:
        field["options_csv"] = ", ".join(str(o) for o in options_list)

    def _on_change(value: str) -> None:
        items = [item.strip() for item in str(value).split(",") if item.strip()]
        field["options_csv"] = value
        field["options"] = items

    # Re-bind ``options_csv`` to call our sync function on every keystroke.
    field["__sync_options_csv"] = _on_change


# --- Layout widget (column-ratio editor) shared by Inputs + Outputs ----


def _render_layout_widget(
    state: dict[str, Any],
    layout_key: str,
    groups: list[dict[str, Any]],
    *,
    heading: str,
) -> None:
    """Render the ``column_layout`` editor — visible only when there's >1 column."""
    layout: dict[str, float] = state[layout_key]
    columns = sorted({int(g.get("column") or 1) for g in groups})
    if len(columns) <= 1:
        return
    ui.label(heading).style("font-weight: 600;")
    ui.label(
        "Relative widths for each column of groups. Pick numbers that "
        "feel natural — they're normalised on render. Leave blank for "
        "equal widths."
    ).style("color: #888; font-size: 0.85rem;")
    with ui.row().classes("w-full gap-2"):
        for col in columns:
            key = f"column_{col}"

            def _on_change(e: Any, _k: str = key) -> None:
                if e.value is None or e.value == "":
                    layout.pop(_k, None)
                else:
                    layout[_k] = float(e.value)

            ui.number(
                f"Column {col} weight",
                value=layout.get(key),
                step=0.1,
                min=0,
                on_change=_on_change,
            ).classes("flex-1").props("dense")


# --- Tab 4: Outputs -----------------------------------------------------


def _render_outputs_tab(state: dict[str, Any]) -> None:
    """Authoring tab for ``outputs[]``."""
    outputs: list[dict[str, Any]] = state["outputs"]
    container = ui.column().classes("w-full gap-3")

    def render() -> None:
        container.clear()
        group_options = {
            g["name"]: str(g.get("label") or g["name"])
            for g in state["output_groups"]
            if g.get("name")
        }
        with container:
            _render_layout_widget(
                state,
                "output_column_layout",
                state["output_groups"],
                heading="Output column layout",
            )
            ui.separator()
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(
                    "Each entry below is one output your tool produces — a "
                    "scalar number / string, a file written to the working "
                    "directory, or a tabular grid. Drag an output by its "
                    "handle to reorder — the order here is the order it "
                    "appears in, within its group."
                ).style("color: #888; font-size: 0.85rem;")
                from matofx.ui.pages._kpi_wizard_helpers import (  # noqa: PLC0415
                    kpis_declared_badge_text,
                )

                ui.chip(kpis_declared_badge_text(outputs)).props(
                    "color=primary text-color=white dense"
                )
            drag = _new_drag_state()
            for index, field in enumerate(outputs):
                if index > 0:
                    ui.separator().style("opacity: 0.5;")
                _render_one_output(
                    field,
                    index=index,
                    outputs=outputs,
                    rerender=render,
                    group_options=group_options,
                    drag=drag,
                )
            ui.button(
                "+ Add output",
                on_click=_then(lambda: outputs.append(empty_output_field()), render),
            ).props("flat color=primary")

    render()


def _output_summary(field: dict[str, Any]) -> str:
    """Heading text for one output's expansion card."""
    return (
        f"{field.get('label') or '(unlabelled)'} "
        f"({field.get('name') or 'no name'}) — {field.get('type', 'number')}"
    )


def _render_one_output(
    field: dict[str, Any],
    *,
    index: int,
    outputs: list[dict[str, Any]],
    rerender: _NoArgsCallable,
    group_options: dict[str, str],
    drag: dict[str, int | None],
) -> None:
    """Render the editor card for one output field with reactive type-conditional rows.

    Mirrors :func:`_render_one_input` — the expansion title updates
    live, type-specific rows toggle visibility on the type dropdown's
    change, the plot-wiring fields appear only when the file_type is
    ``csv_plot``, and the card sits behind a drag handle in a
    drop-target row so it can be reordered while collapsed.
    """
    with _reorderable_row(drag, outputs, index, rerender):
        expansion = ui.expansion(_output_summary(field), value=not field.get("name")).classes(
            "flex-1 min-w-0"
        )
    refs: dict[str, Any] = {}

    def _refresh_summary() -> None:
        expansion.set_text(_output_summary(field))

    def _refresh_visibility() -> None:
        ftype = str(field.get("type") or "number")
        is_number = ftype == "number"
        # An uncertainty is not an independent output: it is folded into its
        # parent as ``value ± std`` and never rendered as its own row. So the
        # rows that describe an independent output are hidden rather than
        # offered and then ignored — a control that silently does nothing is
        # worse than no control. Unit is inherited (a 1-sigma of a mass in
        # grams is in grams); KPI is a measure of the *design*, not of the
        # model's confidence in it. Precision and the validity bounds stay:
        # both act on the number this output actually writes.
        is_uncertainty = bool(str(field.get("uncertainty_for") or "").strip())
        if is_uncertainty:
            refs["unit_row"].set_visibility(False)
            refs["precision_input"].set_visibility(True)
            refs["file_column"].set_visibility(False)
            refs["table_row"].set_visibility(False)
            if "kpi_row" in refs:
                refs["kpi_row"].set_visibility(False)
            if "number_bounds_row" in refs:
                refs["number_bounds_row"].set_visibility(True)
            return
        is_file = ftype == "file"
        is_table = ftype == "table"
        # Unit only meaningful for numeric scalar outputs.
        refs["unit_row"].set_visibility(is_number)
        # Precision only meaningful for numeric outputs.
        refs["precision_input"].set_visibility(is_number)
        # File rows + plot wiring only for files.
        refs["file_column"].set_visibility(is_file)
        # Table sub-editor only for tables.
        refs["table_row"].set_visibility(is_table)
        # KPI row only meaningful for numeric outputs (slice 1's
        # validator rejects ``kpi=true`` on every other type).
        if "kpi_row" in refs:
            refs["kpi_row"].set_visibility(is_number)
        # Numeric min/max bounds only for numeric outputs (ADR-0017); the field
        # value bounds live inside file_column and follow the field renderer.
        if "number_bounds_row" in refs:
            refs["number_bounds_row"].set_visibility(is_number)
        # Plot wiring is gated *additionally* on file_type == csv_plot;
        # _refresh_plot_visibility owns that.
        _refresh_plot_visibility()

    def _refresh_plot_visibility() -> None:
        is_csv_plot = field.get("file_type") == "csv_plot"
        refs["plot_row"].set_visibility(is_csv_plot)
        refs["plot_titles_row"].set_visibility(is_csv_plot)
        # Field-output rows show only for the "field" renderer hint.
        if "field_column" in refs:
            refs["field_column"].set_visibility(field.get("file_type") == "field")

    with expansion, ui.column().classes("w-full gap-2"):
        with ui.row().classes("w-full gap-2 items-center"):
            _bind_text(
                field,
                "name",
                label="Name *",
                width_class="flex-1",
                on_change_extra=_refresh_summary,
            )
            _bind_text(
                field,
                "label",
                label="Label *",
                width_class="flex-1",
                on_change_extra=_refresh_summary,
            )
            _row_toolbar(outputs, index, rerender)

        with ui.row().classes("w-full gap-2 items-center"):
            _bind_select(
                field,
                "type",
                label="Type *",
                options=_OUTPUT_TYPES,
                on_change_extra=_then(_refresh_summary, _refresh_visibility),
            )
            with ui.row().classes("flex-1 gap-2 items-center") as unit_row:
                _render_unit_picker(field)
            refs["unit_row"] = unit_row
            # Precision is the same fixed menu as on the input side
            # (1 / 0.1 / 0.01 / 0.001 / …) so authors get the same
            # plain-English choices rather than a free-form numeric box.
            # The selection survives the input/output switch because the
            # underlying value is the same float either way.
            precision_input = _bind_select(
                field,
                "precision",
                label="Precision (step)",
                options=_FLOAT_PRECISION_OPTIONS,
            )
            refs["precision_input"] = precision_input
            if str(field.get("uncertainty_for") or "").strip():
                # Shown, not omitted: the author should see that the control
                # exists and why it is unavailable, rather than wonder where it
                # went. The uncertainty follows whatever group its parent is in.
                ui.select(
                    {"": "Group not assignable"},
                    value="",
                    label="Group",
                ).props("outlined dense readonly").tooltip(
                    f"Shown as ± on {field['uncertainty_for']}, so it follows that "
                    "output's group."
                )
            else:
                _bind_select(
                    field,
                    "group",
                    label="Group",
                    options=({"": "(none)"} | group_options),
                )

        # Output-validity bounds for a numeric output (ADR-0017): physical
        # min / max, plus the finiteness opt-out. A run whose value falls
        # outside (or is NaN/Inf) is flagged invalid and excluded from
        # surrogates / sensitivity / optimisation. Hidden for non-numeric types.
        with ui.row().classes("w-full gap-2 items-center") as number_bounds_row:
            _bind_number(field, "min", label="Min (physical)")
            _bind_number(field, "max", label="Max (physical)")
            _bind_checkbox(field, "allow_non_finite", label="Allow non-finite (NaN/Inf)")
        refs["number_bounds_row"] = number_bounds_row

        with ui.column().classes("w-full gap-2") as file_column:
            with ui.row().classes("w-full gap-2 items-center"):
                _bind_text(
                    field,
                    "filename",
                    label="Filename (relative to working dir) *",
                    placeholder="results/summary.csv",
                    width_class="flex-1",
                )
                _bind_select(
                    field,
                    "file_type",
                    label="Renderer hint",
                    options=({"": "(none)"} | _FILE_TYPE_HINTS),
                    on_change_extra=_refresh_plot_visibility,
                )
            with ui.row().classes("w-full gap-2") as plot_row:
                _bind_number(
                    field,
                    "xaxis_column",
                    label="X-axis column (0-indexed)",
                    min_value=0,
                    step=1,
                )
                _bind_number(
                    field,
                    "yaxis_column",
                    label="Y-axis column (0-indexed)",
                    min_value=0,
                    step=1,
                )
            ui.label(
                "Column numbers are 0-indexed — the first column in the CSV is 0, "
                "the second is 1, and so on. Leave empty to default to column 0 (X) "
                "and column 1 (Y)."
            ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
            with ui.row().classes("w-full gap-2") as plot_titles_row:
                _bind_text(field, "plot_title", label="Plot title")
                _bind_text(field, "xaxis_title", label="X-axis title")
                _bind_text(field, "yaxis_title", label="Y-axis title")
            refs["plot_row"] = plot_row
            refs["plot_titles_row"] = plot_titles_row
            # Field-output controls (file_type == "field"; ADR-0008 §4.4).
            with ui.column().classes("w-full gap-2") as field_column:
                with ui.row().classes("w-full gap-2"):
                    _bind_select(
                        field,
                        "field_format",
                        label="Field format *",
                        options=({"": "(choose)"} | _FIELD_FORMAT_OPTIONS),
                    )
                    _bind_text(
                        field,
                        "field_node_array_name",
                        label="Node array name",
                        placeholder="temperature",
                    )
                with ui.row().classes("w-full gap-2"):
                    _bind_text(
                        field,
                        "field_timesteps",
                        label="Timestep glob (transient, optional)",
                        placeholder="outputs/temperature_step_*.vtu",
                    )
                    _bind_text(
                        field,
                        "field_remap_reference_mesh",
                        label="Remap reference mesh (optional)",
                        placeholder="static/reference.vtu",
                    )
                # Per-node output-validity bounds for the field (ADR-0017): a
                # snapshot with any node outside [min, max] (or NaN/Inf) is
                # dropped at surrogate ingest so it can't poison a POD field fit.
                with ui.row().classes("w-full gap-2 items-center"):
                    _bind_number(field, "value_min", label="Field min (per node)")
                    _bind_number(field, "value_max", label="Field max (per node)")
                    _bind_checkbox(field, "allow_non_finite", label="Allow non-finite (NaN/Inf)")
                ui.label(
                    "A field output is a whole nodal field (a temperature map, a stress "
                    "field) for the field-surrogate viewer. Choose the snapshot format; "
                    "the node array name is the point-data array carrying the field; the "
                    "timestep glob makes it time-resolved. Per-node min/max bound the "
                    "physical range (tip: also bound the companion peak scalar so a bad "
                    "field is caught at the study level)."
                ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
            refs["field_column"] = field_column
        refs["file_column"] = file_column

        with ui.column().classes("w-full gap-2") as table_row:
            _render_table_editor(field, with_default_rows=False)
        refs["table_row"] = table_row

        # KPI editor row (ADR-0012 §3.1) — slice 11. Numeric outputs
        # get a "Mark as KPI" checkbox + popover with kpi_label /
        # kpi_unit / kpi_category. Un-ticking sweeps the override
        # values to keep the slice-1 validator happy on save.
        with ui.row().classes("w-full gap-2 items-center") as kpi_row:
            _render_kpi_editor(field)
        refs["kpi_row"] = kpi_row

    _refresh_visibility()


def _render_kpi_editor(field: dict[str, Any]) -> None:
    """One-row KPI editor: checkbox + (when ticked) label/unit/category inputs."""
    from matofx.ui.pages._kpi_wizard_helpers import (  # noqa: PLC0415
        clear_kpi_overrides,
    )

    overrides_row = ui.row().classes("flex-1 gap-2 items-center")

    def _refresh_overrides() -> None:
        overrides_row.clear()
        if not field.get("kpi"):
            return
        with overrides_row:
            _bind_text(
                field,
                "kpi_label",
                label="KPI label",
                placeholder="Defaults to the output label",
                width_class="flex-1",
            )
            _bind_text(
                field,
                "kpi_unit",
                label="KPI unit",
                placeholder="Defaults to unit",
            )
            _bind_text(
                field,
                "kpi_category",
                label="KPI category",
                placeholder="e.g. thermal / structural",
            )

    def _on_toggle(value: bool) -> None:
        field["kpi"] = bool(value)
        if not field["kpi"]:
            clear_kpi_overrides(field)
        _refresh_overrides()

    checkbox = ui.checkbox(
        "Mark as KPI", value=bool(field.get("kpi"))
    ).on_value_change(lambda e: _on_toggle(e.value))
    with checkbox:
        ui.tooltip(
            "Headline KPI for the per-tool results warehouse "
            "(/results/{alias})."
        )
    _refresh_overrides()


# --- Tab 5: Presets -----------------------------------------------------


def _preset_value_in_state(values: dict[str, Any], input_name: str) -> Any:
    """Read a preset's stored value for ``input_name``, or ``None`` when absent."""
    return values.get(input_name)


def _preset_set_value(values: dict[str, Any], input_name: str, value: Any) -> None:
    """Write a preset's value, dropping the key when ``value`` is the empty sentinel.

    Mirrors :func:`_preset_to_descriptor`'s strip-empties logic so the
    live state and the serialised payload agree on what counts as
    "the preset overrides this input".
    """
    if value is None or value == "" or value == []:
        values.pop(input_name, None)
        return
    values[input_name] = value


def _render_preset_value_editor(values: dict[str, Any], field: dict[str, Any]) -> None:
    """Render one (input → value) row inside a preset's group sub-card.

    Layout per input:

    * **Persistent label row** — the input's display label plus its unit
      in parens, always visible above the editor (no NiceGUI floating
      label). Bold label; muted unit. Mirrors how the operator sees the
      input in the descriptor wizard's Inputs tab and how the end-user
      sees it on the live submit form.
    * **Editor widget** — the value control matching ``field["type"]``,
      stripped of its own ``label=`` prop so the persistent label above
      stays the only label and doesn't get covered by a floating one
      when the field is filled.

    Inputs whose type can't be sensibly captured in a preset (``file``,
    ``table``, ``material``) render as a muted "(not supported in
    presets)" hint under the persistent label so the operator still
    sees the input exists in its proper group, with a clear explanation
    of why they can't override it from here. Empty values surface as
    "not overridden" and are stripped at save time by
    :func:`_preset_to_descriptor`.
    """
    input_name = str(field.get("name") or "")
    if not input_name:
        return
    field_type = str(field.get("type") or "string")
    current = _preset_value_in_state(values, input_name)

    with ui.column().classes("w-full gap-1"):
        _render_preset_label(field)

        if field_type in ("file", "table", "material"):
            ui.label("(not supported in presets)").style(
                "color: #888; font-style: italic; font-size: 0.8rem;"
            )
            return

        if field_type == "string":
            ui.input(
                value="" if current is None else str(current),
                on_change=lambda e, n=input_name: _preset_set_value(values, n, e.value),
            ).classes("w-full").props("dense clearable")
            return

        if field_type == "integer":
            ui.number(
                value=current if isinstance(current, (int, float)) else None,
                min=field.get("minimum"),
                max=field.get("maximum"),
                step=1,
                on_change=lambda e, n=input_name: _preset_set_value(
                    values, n, None if e.value is None else int(e.value)
                ),
            ).classes("w-full").props("dense clearable")
            return

        if field_type == "float":
            precision = field.get("precision")
            ui.number(
                value=current if isinstance(current, (int, float)) else None,
                min=field.get("minimum"),
                max=field.get("maximum"),
                step=precision if isinstance(precision, (int, float)) else None,
                on_change=lambda e, n=input_name: _preset_set_value(
                    values, n, None if e.value is None else float(e.value)
                ),
            ).classes("w-full").props("dense clearable")
            return

        if field_type == "boolean":
            ui.select(
                _BOOL_DEFAULT_OPTIONS,
                value=_bool_default_to_sentinel(current),
                on_change=lambda e, n=input_name: _preset_set_value(
                    values, n, _sentinel_to_bool_default(str(e.value))
                ),
            ).classes("w-full").props("dense")
            return

        if field_type == "select":
            options = [str(o) for o in (field.get("options") or [])]
            # Build {key: label} so the empty sentinel reads as "—"
            # (i.e. "preset doesn't override this input"). Use the
            # option value itself as both key and label since the
            # descriptor's current select widget already does that.
            choices: dict[str, str] = {"": "— (not overridden)"}
            choices.update({opt: opt for opt in options})
            current_str = "" if current is None else str(current)
            ui.select(
                choices,
                value=current_str if current_str in choices else "",
                on_change=lambda e, n=input_name: _preset_set_value(
                    values, n, None if not e.value else str(e.value)
                ),
            ).classes("w-full").props("dense")
            return

        if field_type == "multiselect":
            options = [str(o) for o in (field.get("options") or [])]
            choices = {opt: opt for opt in options}
            current_list = list(current) if isinstance(current, list) else []
            ui.select(
                choices,
                value=[v for v in current_list if v in choices],
                multiple=True,
                on_change=lambda e, n=input_name: _preset_set_value(
                    values, n, [str(v) for v in (e.value or [])]
                ),
            ).classes("w-full").props("dense use-chips")
            return

        # Unknown type — render as plain text so the operator can still
        # author *something*, but flag it visibly under the label.
        ui.label(f"(unknown type: {field_type})").style(
            "color: #b58900; font-size: 0.75rem;"
        )
        ui.input(
            value="" if current is None else str(current),
            on_change=lambda e, n=input_name: _preset_set_value(values, n, e.value),
        ).classes("w-full").props("dense clearable")


def _render_preset_label(field: dict[str, Any]) -> None:
    """The persistent label line shown above every preset value editor.

    Splits the label text from the unit so the unit can render in a
    muted weight — easier to scan a column of editors when each row's
    name jumps out. No widget-level ``label`` prop is used downstream,
    so this stays the sole label and never gets hidden by a floating
    label inside the editor.
    """
    label = _preset_input_label_text(field)
    unit = _preset_input_unit_str(field)
    with ui.row().classes("items-baseline gap-1 w-full no-wrap"):
        ui.label(label).style(
            "font-weight: 600; font-size: 0.85rem; "
            "overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
        )
        if unit:
            ui.label(f"({unit})").style(
                "color: #888; font-size: 0.78rem; flex-shrink: 0;"
            )


def _preset_input_label_text(field: dict[str, Any]) -> str:
    """Return the input's display label only — no unit.

    Falls back to the input name when the label is empty (e.g. a
    half-authored input the operator hasn't named yet). Kept
    separate from :func:`_preset_input_unit_str` so the renderer
    can style label + unit independently.
    """
    label = str(field.get("label") or "").strip()
    if label:
        return label
    return str(field.get("name") or "(unnamed input)")


def _preset_input_unit_str(field: dict[str, Any]) -> str | None:
    """Return the descriptor unit string, or ``None`` when the input has no unit.

    Honours the wizard's ``unit_choice`` / ``unit_custom`` state shape:
    known-unit choice → that string; custom choice → the operator's
    free-form text; ``UNIT_NONE_VALUE`` → ``None``.
    """
    unit_choice = field.get("unit_choice")
    unit_custom = field.get("unit_custom")
    if unit_choice == UNIT_CUSTOM_VALUE and isinstance(unit_custom, str):
        custom_stripped = unit_custom.strip()
        return custom_stripped or None
    if (
        isinstance(unit_choice, str)
        and unit_choice
        and unit_choice not in (UNIT_NONE_VALUE, UNIT_CUSTOM_VALUE)
    ):
        return unit_choice
    return None


def _preset_input_label(field: dict[str, Any]) -> str:
    """Human-readable label + unit combined: ``"Label (unit)"`` or just ``"Label"``.

    Used for plain-text contexts (auto-summary blurbs, the "Use this
    preset" picker on the live submit form). The wizard's persistent
    label renderer uses :func:`_preset_input_label_text` and
    :func:`_preset_input_unit_str` separately so it can style them
    independently.
    """
    label = _preset_input_label_text(field)
    unit = _preset_input_unit_str(field)
    return f"{label} ({unit})" if unit else label


def _group_inputs_for_presets(
    state: dict[str, Any],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Bucket the wizard's inputs into ``(group_label, [field, ...])`` blocks.

    Mirrors :func:`matofx.tools.forms.render._plan_layout` so the
    presets editor groups inputs the same way the live submit form
    does. Groups are sorted by ``(column, row)`` ascending — the same
    order the form renders them — so the operator authoring a preset
    sees the inputs in the same sequence the end-user will see them.
    Inputs with no ``group`` (or whose group doesn't exist in the
    wizard's ``input_groups`` list) collect into a final ``"Ungrouped"``
    block so they're still authorable from this tab.
    """
    inputs = list(state.get("inputs") or [])
    groups = list(state.get("input_groups") or [])
    if not inputs:
        return []

    def _group_sort_key(g: dict[str, Any]) -> tuple[int, int]:
        try:
            col = int(g.get("column") or 1)
        except (TypeError, ValueError):
            col = 1
        try:
            row = int(g.get("row") or 1)
        except (TypeError, ValueError):
            row = 1
        return (col, row)

    ordered_groups = sorted(groups, key=_group_sort_key)
    known_group_names = {str(g.get("name") or "") for g in ordered_groups if g.get("name")}

    blocks: list[tuple[str, list[dict[str, Any]]]] = []
    for group in ordered_groups:
        name = str(group.get("name") or "")
        label = str(group.get("label") or name or "(unnamed group)")
        members = [f for f in inputs if str(f.get("group") or "") == name]
        if not members:
            continue
        blocks.append((label, members))

    # Ungrouped — also catches inputs that reference a group name that
    # no longer exists (e.g. the operator deleted the group on the
    # Input Groups tab after assigning an input to it). Either way,
    # the operator still gets a path to override the input from here.
    ungrouped = [
        f
        for f in inputs
        if not f.get("group") or str(f.get("group")) not in known_group_names
    ]
    if ungrouped:
        blocks.append(("Ungrouped", ungrouped))

    # Edge case: groups exist but every input is ungrouped (or all
    # group members are empty). The fallthrough above already handles
    # this — ``blocks`` ends up with just the Ungrouped entry. No
    # special case needed; the caller renders it as the only sub-card.
    return blocks


def _render_presets_tab(state: dict[str, Any]) -> None:
    """Authoring tab for tool-author-supplied presets.

    A **preset** is a named bundle of ``{input_name: value}`` overrides
    surfaced as a "Use this preset" pick in the My Jobs workspace —
    the user can still over-write any value before submitting. This
    tab replaces the legacy presets-JSON upload on the Bundle files
    card: operators author named sets here, the wizard persists them
    on the descriptor, and the runtime loader
    (:func:`matofx.tools.forms.seeders.load_presets`) prefers them
    over any bundle file.

    The per-input value editors read from ``state["inputs"]`` live —
    edits in the Inputs tab (rename / delete / add an input) reflect
    immediately when the operator returns to this tab. Stale entries
    that name an input the descriptor no longer has are dropped
    silently at save time so the persisted descriptor is always clean.
    """
    presets: list[dict[str, Any]] = state["presets"]
    container = ui.column().classes("w-full gap-2")

    def render() -> None:
        container.clear()
        with container:
            ui.label(
                "Presets are named starting points users pick from the "
                "input form. Each preset can override any subset of the "
                "tool's inputs; users can still edit every value before "
                "submitting. Inputs left blank below aren't overridden — "
                "the form keeps the descriptor's default for those."
            ).style("color: #888; font-size: 0.85rem;")
            inputs_now = list(state.get("inputs") or [])
            if not inputs_now:
                ui.label(
                    "Define at least one input on the Inputs tab before "
                    "authoring presets."
                ).style("color: #b58900; font-size: 0.85rem;")
            grouped_inputs = _group_inputs_for_presets(state) if inputs_now else []
            for index, preset in enumerate(presets):
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                        _bind_text(
                            preset,
                            "name",
                            label="Preset name *",
                            placeholder="e.g. Glass at 200°C",
                            width_class="flex-1 min-w-48",
                        )
                        _bind_text(
                            preset,
                            "description",
                            label="Description",
                            placeholder=(
                                "Optional — shown alongside the preset name in the picker."
                            ),
                            width_class="flex-2 min-w-64",
                        )
                        _row_toolbar(presets, index, render)
                    if inputs_now:
                        ui.label("Values").style(
                            "font-weight: 600; margin-top: 0.5rem;"
                        )
                        # One sub-card per input group, in the same
                        # ``(column, row)`` order the live submit form
                        # renders them. Inputs without a group land in
                        # a final "Ungrouped" sub-card so they're still
                        # authorable from here.
                        for group_label, fields in grouped_inputs:
                            with _subcard(group_label):
                                with ui.grid(columns=2).classes("w-full gap-3"):
                                    for field in fields:
                                        _render_preset_value_editor(
                                            preset["values"], field
                                        )
            ui.button(
                "+ Add preset",
                on_click=_then(lambda: presets.append(empty_preset()), render),
            ).props("flat color=primary")

    render()


# --- Tab 6: Advanced ----------------------------------------------------


def _render_advanced_tab(state: dict[str, Any]) -> None:
    """retain_files (keep-list) + execution_backend.

    ``duration_class`` is still carried in ``state`` (preserved on save so
    stored descriptors round-trip untouched) but is no longer editable here:
    no scheduler reads it today, so exposing it as a control misled authors.
    """
    with ui.column().classes("w-full gap-4"):
        with ui.card().classes("w-full"):
            ui.label("Files to keep after a successful run").style("font-weight: 600;")
            ui.label(
                "MATOFX always keeps — you never list these — the job's inputs, "
                "every declared output, and the run's logs and records. Everything "
                "else the tool wrote to the working directory is deleted, unless a "
                "pattern below keeps it."
            ).style("color: #888; font-size: 0.85rem;")
            ui.label(
                "Patterns are globs relative to the working directory; use e.g. "
                "outputs/*.vtk for a file inside outputs/. Leave the list empty to "
                "keep all of the tool's working files."
            ).style("color: #888; font-size: 0.85rem;")
            _render_glob_list(state, "retain_files", placeholder="outputs/*.vtk")

        with ui.card().classes("w-full"):
            ui.label("Execution backend").style("font-weight: 600;")
            backend = state["execution_backend"]
            kind_select = (
                ui.select(
                    _EXECUTION_KINDS,
                    label="Kind *",
                    value=backend.get("kind") or "subprocess",
                )
                .classes("w-full")
                .props("dense")
            )

            # Optional: clusters without partitions have no queue to name.
            queue_input = _bind_text(
                backend,
                "queue",
                label="HPC queue / partition  (optional — blank uses the cluster's default)",
                placeholder="cpu_short",
            )
            server_input = _bind_text(
                backend, "server", label="AOS server", placeholder="server-01"
            )
            url_input = _bind_text(backend, "url", label="Web app URL")
            port_input = _bind_text(backend, "port", label="Web app port")

            def _on_kind_change(e: Any) -> None:
                backend["kind"] = e.value
                queue_input.set_visibility(e.value == "hpc_batch")
                server_input.set_visibility(e.value in {"aos_calculator", "aos_repeat_calculator"})
                url_input.set_visibility(e.value == "web_app")
                port_input.set_visibility(e.value == "web_app")

            kind_select.on_value_change(_on_kind_change)
            _on_kind_change(
                cast(Any, type("E", (), {"value": backend.get("kind") or "subprocess"})())
            )


def _render_glob_list(state: dict[str, Any], key: str, *, placeholder: str) -> None:
    """List-of-strings editor for ``retain_files`` / ``delete_io_files``."""
    items: list[str] = state[key]
    container = ui.column().classes("w-full gap-1")

    def render() -> None:
        container.clear()
        with container:
            for index, pattern in enumerate(items):

                def _on_change(e: Any, _i: int = index) -> None:
                    items[_i] = str(e.value)

                with ui.row().classes("w-full gap-2 items-center"):
                    ui.input(
                        value=pattern,
                        placeholder=placeholder,
                        on_change=_on_change,
                    ).classes("flex-1").props("dense")
                    _row_toolbar(items, index, render)
            ui.button(
                "+ Add pattern",
                on_click=_then(lambda: items.append(""), render),
            ).props("flat color=primary")

    render()


# --- Public entry point -------------------------------------------------


def render_descriptor_wizard(
    state: dict[str, Any],
    *,
    wiring: UpstreamWiringContext,
    material_classes_with_props: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    """Render the descriptor wizard's tabs inside the current NiceGUI container.

    The caller (page) sets up the container; this function paints the
    tabs into it and wires them to ``state`` (mutated in place).
    ``wiring`` carries the upstream-tool candidate list (the two-sided
    handshake), each candidate tool's outputs, and an on-demand fetcher
    so the Inputs tab can offer a per-field "auto-populated from upstream
    tool" + "which output" pair. The dropdown *labels* show
    human-readable names while the stored *values* are the alias /
    output name.

    ``material_classes_with_props`` is an optional pre-fetched map of
    ``{material_class: [prop_name, ...]}`` (the admin page fetches it
    via :meth:`ApiClient.list_material_classes` when the
    ``materials_database`` feature is on). When provided, a ``material``
    input renders a class select + a class-scoped multiselect for
    required properties; otherwise the wizard falls back to text inputs
    so it works without the catalogue (tests, feature off).
    """
    with (
        ui.tabs()
        .classes("w-full")
        .props("align=left active-color=primary indicator-color=primary")
        .style(
            "background-color: transparent;"
            " border-bottom: 1px solid var(--matofx-card-border);"
        ) as tabs
    ):
        ig = ui.tab("Input Groups", icon="folder").props("no-caps")
        i = ui.tab("Inputs", icon="input").props("no-caps")
        og = ui.tab("Output Groups", icon="folder_special").props("no-caps")
        o = ui.tab("Outputs", icon="output").props("no-caps")
        pr = ui.tab("Presets", icon="bookmark").props("no-caps")
        adv = ui.tab("Advanced", icon="settings").props("no-caps")
    with (
        ui.tab_panels(tabs, value=ig)
        .classes("w-full")
        .style("background-color: transparent;")
    ):
        with ui.tab_panel(ig):
            _render_input_groups_tab(state)
        with ui.tab_panel(i):
            _render_inputs_tab(state, wiring, material_classes_with_props)
        with ui.tab_panel(og):
            _render_output_groups_tab(state)
        with ui.tab_panel(o):
            _render_outputs_tab(state)
        with ui.tab_panel(pr):
            _render_presets_tab(state)
        with ui.tab_panel(adv):
            _render_advanced_tab(state)


def empty_state() -> dict[str, Any]:
    """Blank-slate wizard state for the New Tool flow."""
    return {
        "input_groups": [],
        "inputs": [],
        "cross_field_constraints": [],
        "output_groups": [],
        "outputs": [],
        "presets": [],
        "input_column_layout": {},
        "output_column_layout": {},
        "retain_files": [],
        "delete_io_files": [],
        "execution_backend": {"kind": "subprocess"},
        "duration_class": "medium",
        "input_groups_start_collapsed": True,
        "require_all_groups_opened": True,
    }

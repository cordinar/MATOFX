"""Surrogate-as-exportable-tool synthesis (ADR-0008 §7).

Publishing a ``surrogate_model_version`` "as a tool" generates a normal
``Tool`` whose descriptor + orchestrator are *derived* from the model, so
it inherits the whole install / ACL / submit-form / workflow machinery.

This module is the dependency-light synthesis **core** — three pure
functions:

- :func:`synthesise_descriptor` (§7.1) — the generated :class:`ToolDescriptor`
  (one numeric input per factor with the training-range bounds, one numeric
  output per response, plus a ``<response>_std`` sibling for GP-backed
  uncertainty). When ``block_extrapolation`` is on, the input bounds are
  *tightened* to the training range so the existing form-validation layer
  enforces it with no special path. The **source tool's own input rules**
  ride along via ``source_inputs`` — whole-number inputs stay whole numbers,
  decimal precision and the slider / radio widget are preserved, the tool's
  declared min/max is enforced, and a cross-field sum check (three fractions
  that must total 100) is re-attached — so the exported form asks for a valid
  design, not merely a numeric one.
- :func:`synthesise_orchestrator` (§7.2) — the generated orchestrator
  Python source, with the model version id + bundle sha256 baked in.
- :func:`classify_coverage` / :func:`worst_coverage` (§7.3) — the in-form
  training-data coverage classification the gauge renders.

``register_as_tool`` (wiring this into ``ToolService.install`` so the
synthesised version lands in ``test``), the orchestrator runtime entry
point, and the ``Job.surrogate_model_version_id`` write at submit are the
following slice.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, Literal

from matofx.tools.descriptor import InputField, InputGroup, OutputField, ToolDescriptor

CoverageClass = Literal["in_range", "near_edge", "extrapolation", "far_extrapolation"]

_NEAR_EDGE_FRACTION = 0.10
"""Within 10% of either training-range edge → ``near_edge`` (§7.3)."""

_FAR_FRACTION = 0.50
"""More than 50% of the training span outside the range → ``far_extrapolation``."""

_MIN_SUM_CHECK_MEMBERS = 2
"""A sum check needs at least two participants — the descriptor enforces the same."""


def surrogate_tool_alias(source_tool_alias: str, model_id: int) -> str:
    """The stable alias of a model's exported tool (``surrogate_<tool>_<id>``)."""
    return f"surrogate_{source_tool_alias}_{model_id}"


def classify_coverage(value: float, training_min: float, training_max: float) -> CoverageClass:
    """Classify an input value against the factor's training range (§7.3).

    ``in_range`` (green) · ``near_edge`` (within 10% of an edge, yellow) ·
    ``extrapolation`` (outside but within ±50% of the span, orange) ·
    ``far_extrapolation`` (beyond that, red).
    """
    span = training_max - training_min
    if span <= 0:
        return "in_range" if value == training_min else "far_extrapolation"
    if value < training_min:
        return (
            "far_extrapolation"
            if (training_min - value) > _FAR_FRACTION * span
            else "extrapolation"
        )
    if value > training_max:
        return (
            "far_extrapolation"
            if (value - training_max) > _FAR_FRACTION * span
            else "extrapolation"
        )
    edge = _NEAR_EDGE_FRACTION * span
    if value <= training_min + edge or value >= training_max - edge:
        return "near_edge"
    return "in_range"


_COVERAGE_RANK: dict[CoverageClass, int] = {
    "in_range": 0,
    "near_edge": 1,
    "extrapolation": 2,
    "far_extrapolation": 3,
}


def worst_coverage(classes: list[CoverageClass]) -> CoverageClass:
    """The worst coverage class across factors (the submit-button summary, §7.3)."""
    if not classes:
        return "in_range"
    return max(classes, key=lambda c: _COVERAGE_RANK[c])


def _declared_bounds(
    coverage_report: dict[str, Any] | None,
    factor_name: str,
    lo: Any,
    hi: Any,
) -> tuple[Any, Any]:
    """The declared envelope's bounds for one factor, else the training range.

    ADR-0020 §10 turns the envelope from an assertion into the thing the guard
    enforces. ``block_extrapolation`` predates that and tightened inputs to the
    raw training spread; once a user has narrowed the range they trust, the
    hard gate has to honour the same range the guard reports against.
    """
    if not coverage_report:
        return lo, hi
    for axis in coverage_report.get("envelope") or []:
        if axis.get("name") != factor_name:
            continue
        if axis.get("kind") not in ("continuous", "integer"):
            return lo, hi
        return (
            axis.get("lo") if axis.get("lo") is not None else lo,
            axis.get("hi") if axis.get("hi") is not None else hi,
        )
    return lo, hi


def _support_outputs(coverage_report: dict[str, Any] | None, taken: set[str]) -> list[OutputField]:
    """The ADR-0020 §8.1 support outputs, or ``[]`` without a report.

    Omitted entirely for a version fitted before coverage existed: an output
    a descriptor declares but the orchestrator never writes is worse than no
    output at all, and existing exports keep working untouched until they are
    re-exported through the normal ``test -> released`` gate.

    ``support_sigma`` is **numeric** on purpose. ADR-0017 output validity
    operates on numeric scalars and field arrays, not on status strings, so
    only the number can carry a declared maximum and flag a runaway
    extrapolation in a workflow nobody is watching. ``support_status`` is the
    readable half.

    A source tool whose own response is already called ``support_status`` keeps
    it; duplicating an output name would produce an invalid descriptor.
    """
    if not coverage_report:
        return []
    out: list[OutputField] = []
    if "support_status" not in taken:
        out.append(
            OutputField(
                name="support_status",
                label="Prediction support",
                type="string",
            )
        )
    if "support_sigma" not in taken:
        out.append(
            OutputField(
                name="support_sigma",
                label="Prediction uncertainty (relative)",
                type="number",
            )
        )
    return out


def _spec(source_inputs: Mapping[str, Mapping[str, Any]] | None, name: str) -> Mapping[str, Any]:
    """The source tool's declaration for one input, or an empty mapping.

    A model outlives the tool it was fitted from, so an input the descriptor
    no longer declares (renamed, dropped) simply yields no rules rather than
    failing the export — the surrogate is still valid, it just loses the
    cosmetic and validation trimmings for that one field.
    """
    spec = (source_inputs or {}).get(name)
    return spec if isinstance(spec, Mapping) else {}


def _hard_bounds(
    spec: Mapping[str, Any],
    lo: Any,
    hi: Any,
    *,
    block_extrapolation: bool,
    integer: bool,
) -> tuple[float | None, float | None]:
    """The bounds the exported form **enforces** for one numeric factor.

    Two different limits meet here and they mean different things. The
    training / declared range is a statement about *coverage* — advisory by
    default (§7.3 / non-negotiable #8) so a user can knowingly extrapolate and
    the gauge merely warns — and becomes a hard gate only under
    ``block_extrapolation``. The source tool's own ``minimum`` / ``maximum`` is
    a statement about *validity*: a negative fin count was never a legal
    design, surrogate or not, so it is enforced either way. The enforced range
    is the intersection of whichever of those apply.

    Integer bounds are pulled inward to whole numbers, since the descriptor
    refuses a fractional bound on an integer input (and rounding outward would
    admit a value the source tool itself rejects).
    """
    low = float(lo) if (block_extrapolation and lo is not None) else None
    high = float(hi) if (block_extrapolation and hi is not None) else None
    src_lo, src_hi = spec.get("minimum"), spec.get("maximum")
    if src_lo is not None:
        low = float(src_lo) if low is None else max(low, float(src_lo))
    if src_hi is not None:
        high = float(src_hi) if high is None else min(high, float(src_hi))
    if integer:
        low = float(math.ceil(low)) if low is not None else None
        high = float(math.floor(high)) if high is not None else None
    return low, high


def _factor_help(spec: Mapping[str, Any], lo: Any, hi: Any) -> str | None:
    """The source tool's help for a factor, plus the range the surrogate saw."""
    bits: list[str] = []
    source_help = str(spec.get("help_text") or "").strip()
    if source_help:
        bits.append(source_help)
    if lo is not None and hi is not None:
        bits.append(f"Surrogate trained over {lo} to {hi}.")
    return " ".join(bits) or None


def _numeric_factor(
    factor: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    block_extrapolation: bool,
    group: str | None,
) -> InputField:
    """One varied numeric factor as an input, carrying the source tool's rules.

    A tool whose fin count is an ``integer`` on a step of 1 exported as a plain
    ``float``, so the surrogate form accepted 17.4 fins and handed it to a
    model that had only ever seen whole ones. Type, decimal precision and the
    slider / textbox choice come from the source declaration; the enforced
    range is :func:`_hard_bounds`.
    """
    fname = str(factor["name"])
    lo, hi = factor.get("min"), factor.get("max")
    integer = spec.get("type") == "integer"
    minimum, maximum = _hard_bounds(
        spec, lo, hi, block_extrapolation=block_extrapolation, integer=integer
    )
    precision = 1.0 if integer else spec.get("precision")
    # A slider with no fixed range has nothing to span, so an unbounded
    # (advisory-extrapolation) factor falls back to the numeric textbox.
    widget = str(spec.get("widget") or "field")
    if widget == "slider" and (minimum is None or maximum is None):
        widget = "field"
    return InputField(
        name=fname,
        label=str(factor.get("label") or fname),
        type="integer" if integer else "float",
        unit=factor.get("unit"),
        minimum=minimum,
        maximum=maximum,
        precision=float(precision) if precision is not None else None,
        widget=widget,  # type: ignore[arg-type]
        group=group,
        help_text=_factor_help(spec, lo, hi),
    )


def _held_constant(
    constant: Mapping[str, Any], spec: Mapping[str, Any], *, help_text: str
) -> InputField | None:
    """One held-fixed training input as a defaulted, read-only-in-spirit field.

    A numeric value keeps the source tool's integer-ness (and therefore an
    integer default, which the descriptor requires); a non-numeric one — a
    held material, a fixed mesh file — becomes a single-option select:
    unchangeable, which is what "held constant" means, and present, which is
    what the predict-time guard needs.
    """
    value = constant.get("value")
    name = str(constant["name"])
    label = str(constant.get("label") or name)
    if not isinstance(value, bool) and isinstance(value, int | float):
        integer = spec.get("type") == "integer" and float(value).is_integer()
        precision = 1.0 if integer else spec.get("precision")
        return InputField(
            name=name,
            label=label,
            type="integer" if integer else "float",
            unit=constant.get("unit"),
            required=False,
            default=int(value) if integer else float(value),
            precision=float(precision) if precision is not None else None,
            group="held_constant",
            help_text=help_text,
        )
    if value is None:
        return None
    level = _level_label(value)
    return InputField(
        name=name,
        label=label,
        type="select",
        options=[level],
        required=False,
        default=level,
        choice_widget=str(spec.get("choice_widget") or "dropdown"),  # type: ignore[arg-type]
        group="held_constant",
        help_text=help_text,
    )


def _apply_sum_checks(
    inputs: list[InputField], source_inputs: Mapping[str, Mapping[str, Any]] | None
) -> list[InputField]:
    """Re-attach the source tool's cross-field sum checks (§7.1).

    A descriptor rule like "these three fractions must total 100" is as much
    a part of a valid design as a range is, and the exported tool dropped it —
    so the surrogate form happily accepted 40/40/40 and asked the model to
    predict a mixture that cannot exist. Carried only when **every** member
    survived into the exported form (as a varied factor or as a held
    constant): a partial check would reference an input the descriptor does
    not declare, which the descriptor validator rightly refuses.
    """
    if not source_inputs:
        return inputs
    present = {i.name for i in inputs}
    out: list[InputField] = []
    for field in inputs:
        check = _spec(source_inputs, field.name).get("sum_check")
        if not isinstance(check, Mapping):
            out.append(field)
            continue
        members = [str(m) for m in (check.get("sum_parameters") or ())]
        if (
            len(members) < _MIN_SUM_CHECK_MEMBERS
            or check.get("value") is None
            or not present.issuperset(members)
        ):
            out.append(field)
            continue
        out.append(
            InputField(
                **{
                    **field.model_dump(),
                    "sum_check": {"value": float(check["value"]), "sum_parameters": members},
                }
            )
        )
    return out


def _modelled_categorical(
    factor: Mapping[str, Any], spec: Mapping[str, Any], *, group: str | None
) -> InputField | None:
    """The picker for a categorical the model was **fitted on**, or ``None``.

    Kept out of the factor loop so that loop stays within its branch budget —
    and because the two cases really are different inputs: this one changes the
    prediction, the envelope-only one below merely lets the guard judge it.
    Renders with the same dropdown / radio treatment the source tool chose, so
    a two-option material picker keeps reading as two radio buttons.
    """
    levels = [str(v) for v in (factor.get("levels") or []) if str(v)]
    if not levels:
        return None
    name = str(factor["name"])
    fitted_note = "The surrogate was fitted on this choice, so the prediction changes with it."
    source_help = str(spec.get("help_text") or "").strip()
    return InputField(
        name=name,
        label=str(factor.get("label") or name),
        type="select",
        options=levels,
        required=True,
        default=levels[0],
        choice_widget=str(spec.get("choice_widget") or "dropdown"),  # type: ignore[arg-type]
        group=group,
        help_text=f"{source_help} {fitted_note}".strip(),
    )


def _envelope_axes(coverage_report: dict[str, Any] | None, *, kind: str) -> list[dict[str, Any]]:
    """Envelope axes of one kind from a stored report, tolerantly.

    A stored report is a database row that outlives the release that wrote it,
    so anything malformed yields no axes rather than raising inside export.
    """
    if not isinstance(coverage_report, dict):
        return []
    axes = coverage_report.get("envelope")
    if not isinstance(axes, list | tuple):
        return []
    return [a for a in axes if isinstance(a, dict) and a.get("kind") == kind]


def _level_label(value: Any) -> str:
    """A non-numeric held value as the string the envelope keys it by."""
    from matofx.studies.coverage.envelope import level_label  # noqa: PLC0415

    return level_label(value)


def _envelope_only_categoricals(
    coverage_report: dict[str, Any] | None, *, taken: set[str], group: str | None
) -> list[InputField]:
    """Pickers for declared categorical axes the model was **not** fitted on.

    The legacy case since categoricals became real factors: a bundle written
    before that, or an axis the envelope declares and the training set never
    varied. Still offered, because the predict-time guard (§8) checks every
    prediction against the envelope and an incomplete point cannot be claimed
    as inside — every prediction from such a tool read `outside`, naming an
    input the form did not have. ``taken`` is what the factor loop already
    added, so a *modelled* categorical is never re-added with this weaker
    wording.
    """
    out: list[InputField] = []
    for axis in _envelope_axes(coverage_report, kind="categorical"):
        levels = [str(v) for v in (axis.get("levels") or []) if str(v)]
        name = str(axis.get("name") or "")
        if not levels or not name or name in taken:
            continue
        out.append(
            InputField(
                name=name,
                label=name,
                type="select",
                options=levels,
                required=False,
                default=levels[0],
                group=group,
                help_text=(
                    "The surrogate was fitted on runs using these options. It "
                    "doesn't predict differences between them — this is here so "
                    "the model can tell you when you've asked outside what it saw."
                ),
            )
        )
    return out


def synthesise_descriptor(
    *,
    source_tool_alias: str,
    model_id: int,
    version_id: int,
    name: str,
    description: str,
    factors: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    version_ordinal: int,
    constants: list[dict[str, Any]] | None = None,
    block_extrapolation: bool = False,
    with_std: bool = True,
    coverage_report: dict[str, Any] | None = None,
    source_inputs: Mapping[str, Mapping[str, Any]] | None = None,
) -> ToolDescriptor:
    """Build the exported tool's :class:`ToolDescriptor` (§7.1).

    ``factors`` are the frozen ``{name, min, max, label?, unit?}`` entries and
    ``responses`` the ``{name, kind, label?, unit?}`` entries from the model
    version (the caller enriches ``label``/``unit`` from the source tool so the
    exported form reads like the original — names are identifiers, labels are
    UX). Each factor becomes a numeric input bounded by its training range
    (tightened-only when ``block_extrapolation``); each response a ``number``
    output, with a ``<response>_std`` sibling when ``with_std`` (GP
    uncertainty). The descriptor pins ``version_id`` (§7.4) so the submit path
    can tag the child job and render the coverage gauge.

    ``source_inputs`` maps each input *name* to the **source tool's own
    declaration** for it, and is what keeps the exported form asking for a
    valid design rather than merely a numeric one: an ``integer`` input stays
    an integer, decimal precision and the slider / radio widget survive, the
    tool's declared min/max is enforced alongside the coverage range, and a
    cross-field sum check ("these three must total 100") is re-attached
    whenever every member survived into the exported form. Omitting it keeps
    the older, rule-free shape.

    ``constants`` (``{name, value, label?, unit?}`` per held-fixed input,
    §7.6) splits the form into two groups: "Inputs you can vary" (the
    factors the surrogate learned) and "Held constant during training" (the
    context it was fitted in, defaulted + read-only-in-spirit — the runtime
    ignores them, but they tell the user the surrogate's validity envelope).
    With no constants the form stays flat / ungrouped (back-compat).
    """
    constants = constants or []
    varied_group = "varied" if constants else None
    inputs: list[InputField] = []
    for factor in factors:
        fname = str(factor["name"])
        spec = _spec(source_inputs, fname)
        # A categorical the model was actually **fitted on** (one-hot encoded).
        # It renders as the same picker the source tool had, and the prediction
        # genuinely depends on it.
        modelled = _modelled_categorical(factor, spec, group=varied_group)
        if modelled is not None:
            inputs.append(modelled)
            continue
        # Advisory by default (§7.3 / non-negotiable #8): leave the *coverage*
        # range unbounded so the user can extrapolate and the gauge merely
        # warns. Only ``block_extrapolation`` turns it into a hard gate,
        # letting the ordinary form-validation layer reject out-of-range
        # submissions with no special path. (The source tool's own min/max is
        # a validity limit rather than a coverage one, so ``_hard_bounds``
        # enforces that either way.)
        # Where a coverage report exists, the **declared envelope** is the
        # range — not the raw training spread. Narrowing the envelope is the
        # expected edit (ADR-0020 §1.3), and two extrapolation controls
        # disagreeing about what "the range" means would let the form accept a
        # point the guard then reports as `outside`.
        lo, hi = _declared_bounds(coverage_report, fname, factor.get("min"), factor.get("max"))
        inputs.append(
            _numeric_factor(
                {**factor, "min": lo, "max": hi},
                spec,
                block_extrapolation=block_extrapolation,
                group=varied_group,
            )
        )

    # A categorical in the **declared envelope** that the model was not fitted
    # on. Since categoricals became real factors this is the legacy case — a
    # bundle written before that, or an axis the envelope declares and the
    # training set never varied. It is still added to the form, because the
    # predict-time guard (§8) checks every prediction against the envelope and
    # an incomplete point cannot be claimed as inside: every prediction from
    # such a tool read `outside`, naming an input the form did not have. The
    # ``any(i.name == aname …)`` guard below is what keeps a *modelled*
    # categorical from being added twice, with the weaker wording.
    inputs.extend(
        _envelope_only_categoricals(
            coverage_report, taken={i.name for i in inputs}, group=varied_group
        )
    )

    # Held-constant inputs. A numeric one keeps the source tool's type +
    # precision; a non-numeric one (a held material, a fixed mesh file) becomes
    # a single-option select, because it is still an envelope axis the guard
    # checks and dropping it produced the same always-outside failure as above.
    chelp = "Held constant during training — the surrogate doesn't vary this."
    taken = {i.name for i in inputs}
    constant_inputs: list[InputField] = []
    for c in constants:
        cname = str(c["name"])
        if cname in taken:
            continue
        held = _held_constant(c, _spec(source_inputs, cname), help_text=chelp)
        if held is not None:
            constant_inputs.append(held)

    outputs: list[OutputField] = []
    for response in responses:
        rname = str(response["name"])
        unit = response.get("unit")
        base_label = str(response.get("label") or rname)
        # A field response (ADR-0018) becomes a field-file output the
        # orchestrator writes (.vtu on the baked mesh, else .npy); the array it
        # writes is named after the response, so the descriptor's
        # ``field_node_array_name`` matches.
        if response.get("kind") == "field":
            fmt = str(response.get("file_format") or "npy").lower().lstrip(".")
            outputs.append(
                OutputField(
                    name=rname,
                    label=base_label,
                    type="file",
                    file_type="field",
                    filename=f"outputs/{rname}.{fmt}",
                    field_format=fmt,
                    field_node_array_name=rname,
                )
            )
            continue
        outputs.append(OutputField(name=rname, label=base_label, type="number", unit=unit))
        # ``has_std`` is per result (ADR-0022: a version can mix methods).
        if with_std and response.get("has_std", True):
            outputs.append(
                OutputField(
                    name=f"{rname}_std",
                    label=f"{base_label} (1-sigma)",
                    type="number",
                    unit=unit,
                    uncertainty_for=rname,
                )
            )

    outputs.extend(_support_outputs(coverage_report, {o.name for o in outputs}))

    # Group membership is by each ``InputField.group``; the InputGroup rows
    # just declare the section labels + grid position.
    input_groups: list[InputGroup] = []
    if constant_inputs:
        input_groups = [
            InputGroup(name="varied", label="Inputs you can vary", column=1, row=1),
            InputGroup(
                name="held_constant",
                label="Held constant during training",
                column=1,
                row=2,
            ),
        ]

    return ToolDescriptor(
        alias=surrogate_tool_alias(source_tool_alias, model_id),
        name=name,
        version=f"1.{version_ordinal}.0",
        short_description=f"Surrogate of {source_tool_alias} (v{version_ordinal}).",
        long_description=(description.strip() or None),
        main_script="surrogate_orchestrator.py",
        # Sum checks go on last, once every member the form will offer is
        # known — a check may span a varied factor and a held constant.
        inputs=_apply_sum_checks(inputs + constant_inputs, source_inputs),
        input_groups=input_groups,
        # Show both groups expanded (no "open every section" gate) — a
        # surrogate form is short and the held-constant group is read-only
        # context the user shouldn't have to click through.
        input_groups_start_collapsed=False,
        outputs=outputs,
        surrogate_model_version_id=version_id,
    )


def synthesise_orchestrator(
    *,
    version_id: int,
    bundle_path: str,
    bundle_sha256: str,
    tool_alias: str,
) -> str:
    """The generated orchestrator Python source (§7.2, v2 tool contract).

    A normal v2 tool main script: reads ``inputs/inputs.json``, then hands the
    point to :func:`matofx.models.runtime.run_surrogate_tool`, which loads the
    pinned model bundle **from disk** (sha256-verified — the run subprocess has
    no DB, so the canonical ``bundle_path`` + sha + version id are baked in at
    synthesis), predicts every scalar response (+ ``<response>_std`` for GP
    uncertainty), reconstructs + writes any **field** outputs (ADR-0018), and
    writes ``outputs/outputs.json``. A refit re-export mints a new tool version
    with a new orchestrator, never mutating this one.
    """
    return (
        '"""Surrogate tool orchestrator — auto-generated, do not edit."""\n'
        "from __future__ import annotations\n\n"
        "import json\n"
        "from pathlib import Path\n\n"
        "from matofx.models.runtime import run_surrogate_tool\n\n"
        f"_VERSION_ID = {version_id}\n"
        f"_BUNDLE_PATH = {json.dumps(bundle_path)}\n"
        f"_BUNDLE_SHA256 = {json.dumps(bundle_sha256)}\n"
        f"_TOOL_ALIAS = {json.dumps(tool_alias)}\n\n\n"
        "def main() -> int:\n"
        '    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))\n'
        "    point = {}\n"
        '    for entry in payload["inputs"]:\n'
        "        value = entry.get('value')\n"
        "        if isinstance(value, bool):\n"
        "            # A boolean is not a number here, matching the\n"
        "            # guard's own rule: float(True) would silently\n"
        "            # become 1.0 and be range-checked as one.\n"
        "            point[entry['name']] = value\n"
        "            continue\n"
        "        try:\n"
        "            # A number stays a number; a categorical level or a\n"
        "            # held material passes through untouched. Blanket\n"
        "            # float() raised on the first non-numeric input, and\n"
        "            # leaving those inputs out instead handed the support\n"
        "            # guard an incomplete point, which it can only report\n"
        "            # as unsupported.\n"
        "            point[entry['name']] = float(value)\n"
        "        except (TypeError, ValueError):\n"
        "            point[entry['name']] = value\n"
        "    return run_surrogate_tool(\n"
        "        bundle_path=_BUNDLE_PATH,\n"
        "        expected_sha256=_BUNDLE_SHA256,\n"
        "        version_id=_VERSION_ID,\n"
        "        tool_alias=_TOOL_ALIAS,\n"
        "        point=point,\n"
        '        out_dir="outputs",\n'
        "    )\n\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n"
    )


__all__ = [
    "CoverageClass",
    "classify_coverage",
    "surrogate_tool_alias",
    "synthesise_descriptor",
    "synthesise_orchestrator",
    "worst_coverage",
]

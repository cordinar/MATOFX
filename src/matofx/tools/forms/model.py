"""Descriptor → Pydantic v2 input model factory.

Given a :class:`~matofx.tools.descriptor.ToolDescriptor`, produce the
runtime model that validates a job's submitted inputs against that
tool's contract. The descriptor is the *authoring surface* (what the
tool author writes); the model is the *runtime engine* (what the
submit endpoint and tool SDK use). This module is the bridge.

Schemas are frozen per ``(tool_alias, tool_version)`` per §F30 Q5: the
factory caches a single model class per pair so every render and
every submit for the same version goes through the same code path,
and an in-flight job started before a tool upgrade keeps validating
against its pinned version's frozen schema.

## What this module does NOT do

- **It does not render the form.** That's :mod:`matofx.tools.forms.render`.
- **It does not seed inputs from presets / previous jobs / defaults.**
  That's :mod:`matofx.tools.forms.seeders`.
- **It does not handle browser file uploads.** Pass 1 accepts a
  ``file`` input as a path string (with optional extension allow-list);
  CAS upload + magic-byte sniffing are pass 2 alongside §F31's CAS
  primitive.
- **Cross-field rules run here.** Both the fixed-shape
  :class:`SumCheck` and arbitrary §F30 ``cross_field_constraints``
  (the locked DSL via :mod:`matofx.tools.forms.safe_expr`) are
  evaluated at submit, after per-field validation and the §16.16
  conditional post-pass, against the resolved values.

## Field-type mapping (pass 1)

| Descriptor type | Python annotation | Pydantic constraints |
|---|---|---|
| ``string`` | ``str`` | min/max ignored (no length on legacy) |
| ``integer`` | ``int`` | ``ge=minimum``, ``le=maximum`` (rounded to int) |
| ``float`` | ``float`` | ``ge=minimum``, ``le=maximum`` |
| ``boolean`` | ``bool`` | — |
| ``select`` | ``Annotated[str, AfterValidator]`` | membership in ``options`` |
| ``multiselect`` | ``Annotated[list[str], AfterValidator]`` | every entry in ``options`` |
| ``file`` | ``Annotated[str, AfterValidator?]`` | optional extension allow-list |
| ``table`` | ``list[dict[str, Any]]`` | shape pass-through (validated in pass 2) |

Required inputs use ``Field(...)`` (Pydantic's "required" sentinel);
optional inputs default to ``None`` (or the descriptor's ``default``)
and accept ``T | None``.

## Plain-English error translation

Pydantic v2 raises :class:`ValidationError` with technical error
codes (``int_parsing``, ``greater_than_equal``, ``literal_error``,
...). :func:`validate_inputs` catches that, walks the per-field
errors, and produces a :class:`~matofx.tools.forms.errors.FieldError`
per offending input with a friendly message — the §F30 / §F32 /
F30-clarifier-3 user UX rule. Custom validators raise ``ValueError``
with the friendly message directly so no extra translation is
needed for them.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from pydantic.functional_validators import AfterValidator
from pydantic_core import ErrorDetails

from matofx.tools.descriptor import (
    InputField,
    RequireEffect,
    SetMinMaxEffect,
    ToolDescriptor,
)
from matofx.tools.forms import safe_expr
from matofx.tools.forms.errors import FieldError, FormValidationError
from matofx.tools.forms.resolver import EffectiveForm, FieldState, resolve

_SUM_CHECK_TOLERANCE = 1e-9
"""Floating-point tolerance for sum-check equality.

Tighter than user-perceptible (``9e-10`` is below any meaningful
engineering precision) but loose enough to absorb the float-arithmetic
noise of summing six or seven percentages.
"""

# --- Cache ----------------------------------------------------------------

# Per §F29 / §F30 Q5: the schema is frozen for a given (alias, version).
# Building the dynamic Pydantic class is cheap-but-not-free, so cache by
# the pair. Tests call :func:`clear_cache` between cases that exercise
# the same alias/version with mutated descriptors.
_MODEL_CACHE: dict[tuple[str, str], type[BaseModel]] = {}


def clear_cache() -> None:
    """Empty the model cache. Tests call this between cases."""
    _MODEL_CACHE.clear()


# --- Public API -----------------------------------------------------------


def build_input_model(descriptor: ToolDescriptor) -> type[BaseModel]:
    """Return the Pydantic v2 model that validates ``descriptor``'s inputs.

    Cached per ``(alias, version)``. Re-calling with the same descriptor
    returns the same class object so equality checks ``model is other``
    hold across requests for the same tool version.

    Two shapes, decided by whether the descriptor carries §16.16
    conditional rules:

    - **No dependencies** — the strict model (unchanged): required-ness,
      numeric ``ge`` / ``le``, and option membership are baked in.
    - **Has dependencies** — a *relaxed* model: types / numeric-parse /
      file-extension only, every field optional, no bounds or option
      membership. Those are conditional (a rule can change required-ness,
      bounds, or the option list) so they move into the resolver-driven
      post-pass in :func:`validate_inputs`. The shape is still
      deterministic per ``(alias, version)`` — the descriptor is frozen
      per version (§F30 Q5) — so the cache key is unchanged.
    """
    cache_key = (descriptor.alias, descriptor.version)
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    relaxed = bool(descriptor.dependencies)
    fields: dict[str, tuple[Any, Any]] = {}
    for input_field in descriptor.inputs:
        fields[input_field.name] = _build_field(input_field, relaxed=relaxed)

    # Names like ``JobInputs_heat_transfer_1_0_0`` — readable in stack
    # traces while staying a valid Python identifier (Pydantic uses the
    # name in error messages).
    model_name = _sanitise_model_name(descriptor.alias, descriptor.version)
    model_class: type[BaseModel] = create_model(  # type: ignore[call-overload]
        model_name,
        __config__=ConfigDict(extra="forbid", str_strip_whitespace=True),
        **fields,
    )
    _MODEL_CACHE[cache_key] = model_class
    return model_class


def validate_inputs(
    descriptor: ToolDescriptor,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a submitted-inputs payload against the descriptor's contract.

    On success returns a dict of validated, type-coerced values keyed
    by input ``name`` — the shape that lands in ``inputs.json``.

    On any kind of failure raises
    :class:`~matofx.tools.forms.errors.FormValidationError` whose
    :attr:`field_errors` lists one entry per offending input with the
    plain-English message the renderer surfaces under that field.
    """
    model_cls = build_input_model(descriptor)
    try:
        instance = model_cls.model_validate(dict(payload))
    except ValidationError as exc:
        raise FormValidationError(_translate_errors(descriptor, exc)) from exc

    values = instance.model_dump(mode="python")

    if descriptor.dependencies:
        # Relaxed model only checked types/parse/file-ext. The resolver
        # computes the *effective* form for these submitted values and
        # the post-pass enforces effective required-ness / options /
        # bounds, applies set_value forces and the on_hide value-state
        # matrix, and decides which fields land in inputs.json.
        values, effective_errors = _enforce_effective_form(descriptor, values)
        if effective_errors:
            raise FormValidationError(effective_errors)

    # Cross-field rules run *after* per-field validation — they need
    # every participant to have parsed cleanly first. Both kinds attach
    # to the first participant and carry the full participant list so
    # the renderer highlights the whole group; a rule that has lost a
    # participant (optional-unfilled, or hidden-and-dropped by §16.16)
    # is skipped rather than nagging the user.
    cross_field_errors = _check_sum_checks(descriptor, values)
    cross_field_errors += _check_cross_field_constraints(descriptor, values)
    # §16.16 validity (require) outcomes: gated by their rule's ``when`` and
    # evaluated against the resolved values, like the cross-field rules.
    cross_field_errors += _check_requires(descriptor, values)
    if cross_field_errors:
        raise FormValidationError(cross_field_errors)

    return values


def check_cross_field(
    descriptor: ToolDescriptor,
    values: Mapping[str, Any],
) -> list[FieldError]:
    """Run *only* the cross-field layer on an already-built value set.

    Exactly the :class:`SumCheck` + §F30 ``cross_field_constraints``
    pass :func:`validate_inputs` performs after per-field validation —
    same evaluator, same don't-nag tolerance, same plain-English
    :class:`~matofx.tools.forms.errors.FieldError` messages — but with
    no per-field coercion and no §16.16 conditional resolution.

    The parametric-studies generator uses this to drop generated points
    that violate the tool's cross-field rules (designs/parametric-
    studies.md §4 step 3): every point is produced inside each factor's
    declared domain, so per-field validation is satisfied by
    construction and only the cross-field verdict is needed. Returns an
    empty list when the point is acceptable.
    """
    vals = dict(values)
    errors = _check_sum_checks(descriptor, vals)
    errors += _check_cross_field_constraints(descriptor, vals)
    return errors


def live_messages(
    descriptor: ToolDescriptor,
    values: Mapping[str, Any],
) -> dict[str, str]:
    """Per-field problems for the *current* (possibly partial) values.

    The live counterpart to :func:`validate_inputs`, built for the
    form's as-you-type feedback and the Submit gate
    (:meth:`matofx.tools.forms.render.FormHandle.submit_blockers`). For
    every visible field that currently holds a value it reports an
    effective option-membership or numeric-bound problem, then layers
    the cross-field verdict on top (a per-field bound message wins over
    a group message anchored to the same field). Returns
    ``{field_name: message}`` — the same plain-English strings
    :func:`validate_inputs` would raise.

    Deliberately *excludes* required-missing: a pristine field the user
    hasn't reached yet shouldn't show a red error, and empty fields are
    skipped for the same reason — required-ness is surfaced by the gate
    as a friendly "fill in …" line, not a per-field error. Effective
    bounds / options are resolved through :func:`resolve` so a §16.16
    rule that tightened a bound is honoured exactly as submit will.

    Because this shares the effective-bounds / cross-field code with
    :func:`validate_inputs`, the live verdict can't disagree with what
    Submit accepts.
    """
    vals = dict(values)
    ef = resolve(descriptor, vals)
    out: dict[str, str] = {}
    for spec in descriptor.inputs:
        st = ef.fields.get(spec.name)
        if st is None or not st.visible:
            continue
        value = vals.get(spec.name)
        if spec.type == "table":
            # Per-cell type / bound problems, keyed to the table field so the
            # grid's error label + the Submit gate both surface them live.
            if isinstance(value, list) and value:
                cell_err = first_table_cell_error(spec, value)
                if cell_err is not None:
                    out[spec.name] = cell_err
            continue
        if value is None or (isinstance(value, str | list) and len(value) == 0):
            continue
        try:
            err = _membership_error(spec, st, value) or _bounds_error(spec, st, value)
        except TypeError:
            # A non-numeric value in a numeric field (e.g. a stray seed)
            # can't be bound-compared — leave it for submit-time parse
            # validation to flag rather than crashing the live pass.
            continue
        if err is not None:
            out[spec.name] = err.message
    for fe in check_cross_field(descriptor, vals):
        for target in fe.sum_parameters or (fe.name,):
            out.setdefault(target, fe.message)
    # §16.16 validity (require) outcomes — surfaced live like cross-field.
    for fe in _eval_requires(descriptor, ef):
        for target in fe.sum_parameters or (fe.name,):
            out.setdefault(target, fe.message)
    return out


# --- Per-field annotation builders ---------------------------------------


def _build_field(spec: InputField, *, relaxed: bool = False) -> tuple[Any, Any]:
    """Compute (type annotation, FieldInfo) for one descriptor input.

    ``relaxed`` (descriptor has §16.16 rules): drop the bound and
    option-membership constraints and make every field optional —
    those are conditional and the resolver post-pass enforces the
    *effective* ones. Type/parse and file-extension checks stay (a
    rule can't change a field's type or a file input's allow-list).
    """
    if relaxed:
        annotation: Any = _relaxed_annotation(spec)
        return annotation | None, Field(default=None, description=spec.help_text or spec.label)

    annotation = _annotation_for(spec)
    field_kwargs: dict[str, Any] = {"description": spec.help_text or spec.label}

    # Numeric bounds map directly to Pydantic ``ge`` / ``le``.
    if spec.type == "integer":
        if spec.minimum is not None:
            field_kwargs["ge"] = int(spec.minimum)
        if spec.maximum is not None:
            field_kwargs["le"] = int(spec.maximum)
    elif spec.type == "float":
        if spec.minimum is not None:
            field_kwargs["ge"] = spec.minimum
        if spec.maximum is not None:
            field_kwargs["le"] = spec.maximum

    # Required vs optional. Pydantic uses ``...`` for "no default".
    # When a descriptor declares an explicit default we use it even if
    # the field is required — the user's submitted value still has to
    # arrive, but the form pre-fills with the default.
    if spec.default is not None:
        field_kwargs["default"] = spec.default
    elif spec.required:
        # Required + no default → mandatory at submit time.
        field_kwargs["default"] = ...
    else:
        # Optional + no default → ``None`` is the resting value.
        field_kwargs["default"] = None
        annotation = annotation | None

    return annotation, Field(**field_kwargs)


def _relaxed_annotation(spec: InputField) -> Any:
    """Type/parse annotation only — no option-membership constraint.

    ``select`` / ``multiselect`` collapse to ``str`` / ``list[str]``
    (membership is now resolver-effective and checked in the post-pass).
    ``file`` keeps its extension allow-list (not rule-controlled).
    Scalars and ``table`` are unchanged from the strict mapping.
    """
    if spec.type == "select":
        return str
    if spec.type == "multiselect":
        return list[str]
    return _annotation_for(spec)


_SCALAR_ANNOTATION: dict[str, Any] = {
    "string": str,
    "integer": int,
    "float": float,
    "boolean": bool,
}


def _annotation_for(spec: InputField) -> Any:
    """Map descriptor type → Python annotation, including custom validators.

    Scalar types use :data:`_SCALAR_ANNOTATION` directly; types that
    need a per-input closure validator (``select``, ``multiselect``,
    ``file`` with extension allow-list) build an ``Annotated[...]``
    wrapping the closure.
    """
    scalar = _SCALAR_ANNOTATION.get(spec.type)
    if scalar is not None:
        return scalar
    builder = _NON_SCALAR_BUILDERS.get(spec.type)
    if builder is not None:
        return builder(spec)
    # Defensive default — the descriptor's own validators reject
    # unknown types at install time, so this branch is unreachable
    # in normal flows. Kept for forward-compatibility when a new
    # descriptor type lands but the form layer hasn't caught up yet.
    return Any  # pragma: no cover


def _select_annotation(spec: InputField) -> Any:
    return Annotated[str, AfterValidator(_membership_validator(spec))]


def _multiselect_annotation(spec: InputField) -> Any:
    return Annotated[list[str], AfterValidator(_multimembership_validator(spec))]


def _file_annotation(spec: InputField) -> Any:
    """Path-string in pass 1; optional extension allow-list via custom validator."""
    if spec.accepted_file_types:
        return Annotated[str, AfterValidator(_file_extension_validator(spec))]
    return str


def _table_annotation(spec: InputField) -> Any:
    """Each row is a dict; every cell is checked against its column's rules."""
    return Annotated[list[dict[str, Any]], AfterValidator(_table_validator(spec))]


_NON_SCALAR_BUILDERS: dict[str, Callable[[InputField], Any]] = {
    "select": _select_annotation,
    "multiselect": _multiselect_annotation,
    "file": _file_annotation,
    "table": _table_annotation,
}


# --- Custom validator closures -------------------------------------------


def _membership_validator(spec: InputField) -> Callable[[str], str]:
    """Closure that validates ``select`` inputs against ``options``."""
    label = spec.label
    options = list(spec.options or [])
    options_set = set(options)

    def _check(value: str) -> str:
        if value not in options_set:
            choices = ", ".join(repr(o) for o in options)
            raise ValueError(f"{label} must be one of: {choices}.")
        return value

    return _check


def _multimembership_validator(spec: InputField) -> Callable[[list[str]], list[str]]:
    """Closure that validates every entry of a ``multiselect`` against ``options``."""
    label = spec.label
    options = list(spec.options or [])
    options_set = set(options)

    def _check(value: list[str]) -> list[str]:
        bad = [v for v in value if v not in options_set]
        if bad:
            choices = ", ".join(repr(o) for o in options)
            shown = ", ".join(repr(b) for b in bad)
            raise ValueError(
                f"{label} contains values not in the allowed list: {shown}. Pick from: {choices}."
            )
        return value

    return _check


def _file_extension_validator(spec: InputField) -> Callable[[str], str]:
    """Closure that validates a file-input path against the descriptor's extension list."""
    from matofx.library.refs import is_library_ref  # noqa: PLC0415 — leaf util; keep forms light

    label = spec.label
    allowed = sorted({e.lower() for e in (spec.accepted_file_types or [])})
    allowed_set = set(allowed)

    def _check(value: str) -> str:
        # Empty string for an optional file is fine; required-ness is
        # already enforced by the field's ``...`` / ``None`` default.
        if value == "":
            return value
        # A "Pick from library" selection carries a string sentinel, not
        # a path — its extension was already vetted against the field at
        # pick time (the picker filters by accepted_file_types) and the
        # bytes are resolved at submit (ADR-0006 §6). Let it through.
        if is_library_ref(value):
            return value
        ext = value.rsplit(".", 1)[-1].lower() if "." in value else ""
        if ext not in allowed_set:
            choices = ", ".join(allowed)
            raise ValueError(f"{label} must be a file with one of these extensions: {choices}.")
        return value

    return _check


def _table_validator(
    spec: InputField,
) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Closure validating each table cell against its column's type + bounds.

    Numeric cells must parse to a finite number inside the column's
    ``minimum`` / ``maximum``; integer columns must be whole; boolean
    columns must be true/false. Blank cells (``None`` / ``""``) count as
    "not filled" and are skipped — per-cell requiredness isn't enforced.
    Raises a friendly :class:`ValueError` naming the column and row on the
    first offending cell (the same UX as scalar field errors).
    """

    def _check(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Pydantic has already validated the ``list[dict]`` shape before this
        # AfterValidator runs, so only cell-level checks remain.
        err = first_table_cell_error(spec, rows)
        if err is not None:
            raise ValueError(err)
        return rows

    return _check


def first_table_cell_error(  # noqa: PLR0911 — one return per distinct cell-rule violation
    spec: InputField, rows: list[dict[str, Any]]
) -> str | None:
    """First per-cell type / bound problem in a table's rows, or ``None``.

    Shared by the submit-time validator (which raises it) and the live layer
    :func:`live_messages` / the grid's own error label (which display it), so
    a table cell's min/max is surfaced live and gates Submit exactly like a
    scalar field — never silently clamped. Blank cells are skipped.
    """
    label = spec.label
    columns = {c.name: c for c in (spec.columns or [])}
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        for cname, col in columns.items():
            cell = row.get(cname)
            if cell is None or cell == "":
                continue  # blank cell — treated as not filled
            where = f"{label}: {col.label} in row {i}"
            if col.type in ("integer", "float"):
                try:
                    num = float(cell)
                except (TypeError, ValueError):
                    return f"{where} must be a number."
                if not math.isfinite(num):
                    return f"{where} must be a number."
                if col.type == "integer" and not num.is_integer():
                    return f"{where} must be a whole number."
                unit = f" {col.unit}" if col.unit else ""
                if col.minimum is not None and num < col.minimum:
                    return f"{where} must be at least {col.minimum:g}{unit}."
                if col.maximum is not None and num > col.maximum:
                    return f"{where} must be at most {col.maximum:g}{unit}."
            elif col.type == "boolean" and not isinstance(cell, bool):
                return f"{where} must be true or false."
    return None


# --- Sum-check post-validation -------------------------------------------


def _check_sum_checks(descriptor: ToolDescriptor, values: dict[str, Any]) -> list[FieldError]:
    """Run every distinct sum-check on the validated values.

    A sum-check belongs to one input but references siblings; the
    descriptor allows the same constraint to be declared on every
    participating input. We deduplicate so an "A + B + C = 100"
    constraint declared three times only fails once.
    """
    label_by_name = {f.name: f.label for f in descriptor.inputs}
    seen_constraints: set[tuple[float, tuple[str, ...]]] = set()
    errors: list[FieldError] = []

    for spec in descriptor.inputs:
        if spec.sum_check is None:
            continue
        target_value = float(spec.sum_check.value)
        # Sort so the dedupe key is order-independent — declaring the
        # constraint on field A vs field C is the same constraint.
        key_names = tuple(sorted(spec.sum_check.sum_parameters))
        constraint_key = (target_value, key_names)
        if constraint_key in seen_constraints:
            continue
        seen_constraints.add(constraint_key)

        total = 0.0
        skip = False
        for name in spec.sum_check.sum_parameters:
            v = values.get(name)
            if v is None:
                # A missing participant means we can't evaluate the
                # constraint — Pydantic should already have raised on
                # the missing field if it was required. Skip silently
                # to avoid a confusing double error.
                skip = True
                break
            try:
                total += float(v)
            except (TypeError, ValueError):
                skip = True
                break

        if skip or math.isnan(total):
            continue

        if abs(total - target_value) > _SUM_CHECK_TOLERANCE:
            participating_labels = ", ".join(
                label_by_name.get(n, n) for n in spec.sum_check.sum_parameters
            )
            # Attach to the first listed parameter so the UI has a
            # deterministic anchor; carry the full participant list
            # so the renderer can also highlight the rest.
            anchor_name = spec.sum_check.sum_parameters[0]
            anchor_label = label_by_name.get(anchor_name, anchor_name)
            errors.append(
                FieldError(
                    name=anchor_name,
                    label=anchor_label,
                    message=(
                        f"{participating_labels} must add up to {_format_sum_target(target_value)}."
                    ),
                    sum_parameters=tuple(spec.sum_check.sum_parameters),
                )
            )

    return errors


def _format_sum_target(value: float) -> str:
    """Render a sum-check target value without unnecessary decimals."""
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


# --- Cross-field constraints (§F30) --------------------------------------


def _check_cross_field_constraints(
    descriptor: ToolDescriptor,
    values: dict[str, Any],
) -> list[FieldError]:
    """Evaluate the descriptor's arbitrary cross-field rules.

    Each constraint's ``expression`` is the locked DSL (already
    grammar/sibling-checked at install). It is evaluated against the
    resolved ``values``; a falsy result yields one
    :class:`FieldError` carrying the author's ``error_message`` (never
    the raw expression — §F30 clarifier 3), anchored to the first
    participant and listing all of them in ``sum_parameters`` so the
    renderer highlights the whole group.

    A constraint is **skipped** (not failed) when any participant is
    absent from ``values`` (hidden-and-dropped by a §16.16 rule) or
    still ``None`` (optional, unfilled), or when evaluation hits a
    type/zero-division/name issue — the same don't-nag tolerance the
    sum-check uses.
    """
    constraints = descriptor.cross_field_constraints
    if not constraints:
        return []

    input_names = {f.name for f in descriptor.inputs}
    label_by_name = {f.name: f.label for f in descriptor.inputs}
    errors: list[FieldError] = []

    for constraint in constraints:
        try:
            tree = safe_expr.parse(constraint.expression)
        except safe_expr.UnsafeExpressionError:
            # An installed descriptor passed the install-time lint, so
            # this is unreachable in practice; skip rather than 500.
            continue  # pragma: no cover
        participants = sorted(safe_expr.referenced_names(tree) & input_names)
        if any(p not in values or values.get(p) is None for p in participants):
            continue
        try:
            result = safe_expr.evaluate(constraint.expression, values)
        except (TypeError, ZeroDivisionError, NameError):
            continue
        if not result:
            anchor = participants[0] if participants else ""
            errors.append(
                FieldError(
                    name=anchor,
                    label=label_by_name.get(anchor, anchor),
                    message=constraint.error_message,
                    sum_parameters=tuple(participants),
                )
            )
    return errors


# --- Conditional-dependency post-pass (§16.16) ---------------------------


def _enforce_effective_form(
    descriptor: ToolDescriptor,
    values: dict[str, Any],
) -> tuple[dict[str, Any], list[FieldError]]:
    """Apply the resolved effective form to type-checked ``values``.

    Returns ``(resolved_values, errors)``. ``resolved_values`` is what
    lands in ``inputs.json``: ``set_value`` forces applied, the
    ``on_hide`` value-state matrix applied (drop / keep_default /
    keep_last), everything else passed through. ``errors`` carries
    per-field plain-English failures for *effective* required-ness,
    option membership, and numeric bounds — the conditional-sensitive
    checks the relaxed model deliberately skipped.

    A field hidden by a rule is never required-checked; a forced field
    is authoritative (its value wins over the user's and over a hide).
    """
    ef: EffectiveForm = resolve(descriptor, values)
    resolved: dict[str, Any] = {}
    errors: list[FieldError] = []

    for spec in descriptor.inputs:
        st = ef.fields[spec.name]
        if st.forced:
            # set_value: machine-authoritative. Wins over the user's
            # input and over a concurrent hide — the forced value is
            # the intended value, so it is written out, not dropped.
            resolved[spec.name] = st.forced_value
            continue
        if not st.visible:
            _apply_hidden(spec, st, values, resolved)
            continue
        value = values.get(spec.name)
        field_error = _effective_field_error(spec, st, value)
        if field_error is not None:
            errors.append(field_error)
        resolved[spec.name] = value

    return resolved, errors


def _apply_hidden(
    spec: InputField,
    st: FieldState,
    values: dict[str, Any],
    resolved: dict[str, Any],
) -> None:
    """Write (or omit) a hidden field per its ``on_hide`` policy."""
    if st.on_hide == "drop":
        return  # omitted from inputs.json; never required-checked
    if st.on_hide == "keep_default":
        resolved[spec.name] = st.effective_default
        return
    # keep_last — whatever the user last had, never required-checked.
    resolved[spec.name] = values.get(spec.name)


def _effective_field_error(
    spec: InputField,
    st: FieldState,
    value: Any,
) -> FieldError | None:
    """Effective required / membership / bound check for a visible field."""
    if st.required and value is None:
        return FieldError(name=spec.name, label=spec.label, message=f"{spec.label} is required.")
    if value is None:
        return None
    return _membership_error(spec, st, value) or _bounds_error(spec, st, value)


def _membership_error(
    spec: InputField,
    st: FieldState,
    value: Any,
) -> FieldError | None:
    """Effective option-membership check (select / multiselect)."""
    if spec.type == "select" and st.options is not None and value not in st.options:
        choices = ", ".join(repr(o) for o in st.options)
        return FieldError(
            name=spec.name,
            label=spec.label,
            message=f"{spec.label} must be one of: {choices}.",
        )
    if spec.type == "multiselect" and st.options is not None:
        bad = [v for v in value if v not in st.options]
        if bad:
            choices = ", ".join(repr(o) for o in st.options)
            shown = ", ".join(repr(b) for b in bad)
            return FieldError(
                name=spec.name,
                label=spec.label,
                message=(
                    f"{spec.label} contains values not in the allowed list: "
                    f"{shown}. Pick from: {choices}."
                ),
            )
    return None


def _bounds_error(
    spec: InputField,
    st: FieldState,
    value: Any,
) -> FieldError | None:
    """Effective numeric-bound check."""
    if spec.type not in {"integer", "float"}:
        return None
    unit = f" {spec.unit}" if spec.unit else ""
    if st.minimum is not None and value < st.minimum:
        return FieldError(
            name=spec.name,
            label=spec.label,
            message=f"{spec.label} can't be less than {_format_bound(st.minimum)}{unit}.",
        )
    if st.maximum is not None and value > st.maximum:
        return FieldError(
            name=spec.name,
            label=spec.label,
            message=f"{spec.label} can't be more than {_format_bound(st.maximum)}{unit}.",
        )
    return None


def _format_bound(value: float) -> str:
    """Render a numeric bound without a spurious ``.0`` (plain-English rule)."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


# --- Validity (require) rules + generation-time enforcement --------------


def _render_expr_with_labels(expr: str, descriptor: ToolDescriptor) -> str:
    """Rewrite a DSL expression with input *labels* in place of names.

    For an auto-generated message: ``outer_d > inner_d`` becomes e.g.
    ``Outer diameter > Inner diameter``. Whole-word substitution only, so
    a name that's a substring of another isn't mangled. Best-effort — a
    name with no label keeps its own text.
    """
    import re  # noqa: PLC0415

    label_by_name = {f.name: f.label or f.name for f in descriptor.inputs}
    if not label_by_name:
        return expr.strip()

    def _sub(match: re.Match[str]) -> str:
        return label_by_name.get(match.group(0), match.group(0))

    pattern = r"\b(" + "|".join(re.escape(n) for n in label_by_name) + r")\b"
    return re.sub(pattern, _sub, expr).strip()


def _auto_require_message(descriptor: ToolDescriptor, expr: str) -> str:
    """Readable fallback for a require rule with no author message."""
    return f"Not allowed: {_render_expr_with_labels(expr, descriptor)} must be true."


def _check_requires(
    descriptor: ToolDescriptor,
    values: dict[str, Any],
) -> list[FieldError]:
    """Evaluate the active ``require`` (validity) outcomes (§16.16).

    Resolves the form for ``values`` to learn which validity rules' ``when``
    held, then evaluates each one's expression against the resolved values.
    A falsy result yields one :class:`FieldError` anchored to the rule's
    input, carrying the author's message (or an auto-generated one). Uses
    the same don't-nag tolerance as the cross-field check: a rule whose
    expression references an unfilled / missing input is skipped, not
    failed.
    """
    if not descriptor.dependencies:
        return []
    return _eval_requires(descriptor, resolve(descriptor, values))


def _eval_requires(
    descriptor: ToolDescriptor,
    ef: EffectiveForm,
) -> list[FieldError]:
    """Evaluate an already-resolved form's active require outcomes.

    Shared by submit (:func:`_check_requires`) and the live form
    (:func:`live_messages`) so both surface the same verdicts.
    """
    if not ef.requires:
        return []
    input_names = {f.name for f in descriptor.inputs}
    label_by_name = {f.name: f.label for f in descriptor.inputs}
    resolved = ef.resolved_values
    errors: list[FieldError] = []
    for req in ef.requires:
        try:
            tree = safe_expr.parse(req.expr)
        except safe_expr.UnsafeExpressionError:  # pragma: no cover - linted at install
            continue
        participants = sorted(safe_expr.referenced_names(tree) & input_names)
        if any(p not in resolved or resolved.get(p) is None for p in participants):
            continue
        try:
            result = safe_expr.evaluate(req.expr, resolved)
        except (TypeError, ZeroDivisionError, NameError):
            continue
        if not result:
            anchor = (
                req.field if req.field in input_names else (participants[0] if participants else "")
            )
            errors.append(
                FieldError(
                    name=anchor,
                    label=label_by_name.get(anchor, anchor),
                    message=req.message or _auto_require_message(descriptor, req.expr),
                    sum_parameters=tuple(participants),
                )
            )
    return errors


def resolve_for_generation(
    descriptor: ToolDescriptor,
    point: Mapping[str, Any],
) -> tuple[dict[str, Any], list[FieldError]]:
    """Apply §16.16 rules to a generated candidate point (studies path).

    Parametric studies (and any other generate-then-check caller) build a
    point inside each factor's declared box, then call this to apply the
    tool's conditional rules: ``set_value`` forces are written into the
    returned point; effective (possibly formula-driven) numeric bounds and
    ``require`` validity rules are enforced; the §F30 cross-field
    constraints and sum-checks are checked too. Returns
    ``(resolved_point, errors)`` — an empty ``errors`` list means the
    point is feasible and ``resolved_point`` is what to run. A non-empty
    list means drop it; ``errors[0].message`` is the plain-English reason.

    This is the generator-side twin of the live form + submit, so a
    relationship like "B's minimum tracks A" is honoured identically
    whether a human or a study produced the inputs.
    """
    base = dict(point)
    if not descriptor.dependencies:
        return base, check_cross_field(descriptor, base)

    ef = resolve(descriptor, base)
    resolved: dict[str, Any] = dict(base)
    errors: list[FieldError] = []
    for spec in descriptor.inputs:
        st = ef.fields.get(spec.name)
        if st is None:
            continue
        if st.forced:
            resolved[spec.name] = st.forced_value
            continue
        if spec.name not in resolved or not st.visible:
            continue
        value = resolved[spec.name]
        if value is None:
            continue
        err = _membership_error(spec, st, value) or _bounds_error(spec, st, value)
        if err is not None:
            errors.append(err)

    # Reuse the form we already resolved (its ``requires`` were collected and
    # its ``resolved_values`` carry the forces) rather than resolving again.
    errors += _eval_requires(descriptor, ef)
    errors += check_cross_field(descriptor, resolved)
    return resolved, errors


def selection_constraint_exprs(descriptor: ToolDescriptor) -> list[tuple[str, str]]:
    """Compile every input relationship into ``(boolean_expr, message)`` pairs.

    For the optimiser, whose candidate filter (``constraints_ok``)
    evaluates plain boolean DSL strings over a *complete* candidate
    (every input present). Each pair holds an expression that must be
    **true** for the candidate to be kept, plus the plain-English reason
    to show if it isn't. Sources, all reduced to the same shape:

    - §F30 ``cross_field_constraints`` — used verbatim.
    - ``require`` outcomes — ``(not (when)) or (expr)`` (the rule only
      bites where its ``when`` holds).
    - ``set_min_max`` outcomes (literal or formula bound) —
      ``(not (when)) or (field >= bound)`` / ``<= bound``, so a
      formula-driven bound like "B's minimum is A" enforces ``B >= A``.

    ``set_value`` forces aren't compiled here: a forced field isn't a
    free variable the optimiser should vary, so there's nothing to drop.
    """
    out: list[tuple[str, str]] = [
        (c.expression, c.error_message) for c in descriptor.cross_field_constraints
    ]
    label_by_name = {f.name: f.label or f.name for f in descriptor.inputs}
    for dep in descriptor.dependencies:
        gate = f"not ({dep.when})"
        for effect in dep.effects:
            if isinstance(effect, RequireEffect):
                ce = f"({gate}) or ({effect.expr})"
                msg = effect.message or _auto_require_message(descriptor, effect.expr)
                out.append((ce, msg))
            elif isinstance(effect, SetMinMaxEffect):
                label = label_by_name.get(effect.field, effect.field)
                for rhs, op, word in _bound_terms(effect):
                    ce = f"({gate}) or ({effect.field} {op} ({rhs}))"
                    pretty = _render_expr_with_labels(rhs, descriptor)
                    out.append((ce, f"{label} must be {word} {pretty}."))
    return out


def _bound_terms(effect: SetMinMaxEffect) -> list[tuple[str, str, str]]:
    """``(rhs_expr, operator, words)`` for each bound an effect sets."""
    terms: list[tuple[str, str, str]] = []
    if effect.minimum_expr is not None:
        terms.append((effect.minimum_expr, ">=", "at least"))
    elif effect.minimum is not None:
        terms.append((_format_bound(effect.minimum), ">=", "at least"))
    if effect.maximum_expr is not None:
        terms.append((effect.maximum_expr, "<=", "at most"))
    elif effect.maximum is not None:
        terms.append((_format_bound(effect.maximum), "<=", "at most"))
    return terms


# --- Pydantic error translation ------------------------------------------


def _translate_errors(
    descriptor: ToolDescriptor,
    exc: ValidationError,
) -> list[FieldError]:
    """Walk a Pydantic ``ValidationError`` and produce per-field FieldErrors."""
    by_name = {f.name: f for f in descriptor.inputs}
    out: list[FieldError] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        if not loc:
            # Top-level error with no field anchor — unusual. Keep the
            # raw message but anchor to an empty name so the UI shows
            # it as a form-level banner.
            out.append(FieldError(name="", label="", message=err.get("msg", "Invalid input.")))
            continue
        field_name = str(loc[0])
        spec = by_name.get(field_name)
        if spec is None:
            # ``extra="forbid"`` violation — payload had a key the
            # descriptor doesn't declare. Surface it but keep the
            # phrasing safe (don't echo the value).
            out.append(
                FieldError(
                    name=field_name,
                    label=field_name,
                    message=f"{field_name!r} isn't a recognised input for this tool.",
                )
            )
            continue
        out.append(
            FieldError(
                name=field_name,
                label=spec.label,
                message=_friendly_message(err, spec),
            )
        )
    return out


# Pydantic error codes that map to the same fixed phrase regardless of
# the descriptor field. Bound-aware codes (``greater_than_equal`` etc.)
# are handled by :data:`_BOUND_FORMATTERS` instead.
_FIXED_PHRASE: dict[str, str] = {
    "missing": "{label} is required.",
    "int_parsing": "{label} must be a whole number.",
    "int_type": "{label} must be a whole number.",
    "int_from_float": "{label} must be a whole number.",
    "float_parsing": "{label} must be a number.",
    "float_type": "{label} must be a number.",
    "bool_parsing": "{label} must be Yes or No.",
    "bool_type": "{label} must be Yes or No.",
    "string_type": "{label} must be text.",
    "string_too_short": "{label} must be text.",
    "list_type": "{label} must be a list of values.",
    "extra_forbidden": "{label!r} isn't a recognised input for this tool.",
}

# Bound codes share a shape: ``"{label} <comparator> {bound}{unit}.``"
# The first tuple element is the ``ctx`` key Pydantic uses for the
# bound value; the second is the comparator phrase.
_BOUND_FORMATTERS: dict[str, tuple[str, str]] = {
    "greater_than_equal": ("ge", "can't be less than"),
    "greater_than": ("gt", "must be greater than"),
    "less_than_equal": ("le", "can't be more than"),
    "less_than": ("lt", "must be less than"),
}


def _friendly_message(err: ErrorDetails, spec: InputField) -> str:
    """Translate one Pydantic error into a per-field plain-English message.

    The mapping below covers the codes Pydantic v2 emits for the
    descriptor types we support. Anything unrecognised falls back to
    the raw ``msg`` with the leading ``"Value error, "`` prefix
    stripped — that prefix is Pydantic boilerplate around any
    ``ValueError`` raised inside a custom validator, which is exactly
    where our friendly messages already live.
    """
    code = str(err.get("type", ""))
    label = spec.label
    unit_suffix = f" {spec.unit}" if spec.unit else ""

    fixed = _FIXED_PHRASE.get(code)
    if fixed is not None:
        return fixed.format(label=label)

    bound_format = _BOUND_FORMATTERS.get(code)
    if bound_format is not None:
        ctx_key, comparator = bound_format
        bound = (err.get("ctx") or {}).get(ctx_key)
        return f"{label} {comparator} {bound}{unit_suffix}."

    # Fallback: messages from our own ``AfterValidator`` closures end
    # up here with the ``"Value error, "`` Pydantic boilerplate prefix
    # — strip it so the final string is exactly what the closure
    # raised.
    msg = str(err.get("msg") or "Invalid value.")
    boilerplate = "Value error, "
    if msg.startswith(boilerplate):
        msg = msg[len(boilerplate) :]
    return msg


# --- Internal helpers ----------------------------------------------------


def _sanitise_model_name(alias: str, version: str) -> str:
    """``"heat_transfer" + "1.0.0"`` → ``"JobInputs_heat_transfer_1_0_0"``."""
    safe_alias = "".join(c if c.isalnum() or c == "_" else "_" for c in alias)
    safe_version = "".join(c if c.isalnum() or c == "_" else "_" for c in version)
    return f"JobInputs_{safe_alias}_{safe_version}"

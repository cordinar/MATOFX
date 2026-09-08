"""Conditional-dependency resolver (§16.16).

One pure function — :func:`resolve` — turns a
:class:`~matofx.tools.descriptor.ToolDescriptor` plus the current input
values into an :class:`EffectiveForm`: per-field visibility, required-
ness, disabled-state, effective options / bounds / default, and any
forced value. The same function backs three surfaces with no drift:

- the live NiceGUI form (re-resolve on every change),
- the authoritative submit-time post-pass (the client is never trusted
  about what was visible), and
- the SDK launch safety-net.

## Evaluation model

Rules chain — a rule's ``when`` may read a field another rule's
``set_value`` writes. The descriptor validators already guaranteed the
rule graph is acyclic, so a topological order of the rules exists.
:func:`resolve` builds that order (Kahn's algorithm, ties broken by
declaration order so that **the last-declared rule wins** when two
independent rules write the same field) and applies each rule once,
in that order, against the running value map. Because the graph is a
DAG every rule sees the settled values of everything it depends on, so
a single ordered pass is exact — no fixpoint iteration needed.

This module does **not** apply the value-state matrix (drop /
keep_default / keep_last for hidden fields, required-ness enforcement,
stale-option clearing). It *describes* the effective form; enforcing it
against a submitted payload is the Phase-2 job of
:mod:`matofx.tools.forms.model`. Keeping the resolver side-effect-free
and policy-free is what lets the live form and the submit path share it.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from matofx.tools.descriptor import (
    Dependency,
    DisableEffect,
    Effect,
    HideEffect,
    InputField,
    OnHidePolicy,
    RequireEffect,
    SetDefaultEffect,
    SetMinMaxEffect,
    SetOptionsEffect,
    SetRequiredEffect,
    SetValueEffect,
    ShowEffect,
    ToolDescriptor,
    effect_expr_strings,
)
from matofx.tools.forms import safe_expr

__all__ = ["ActiveRequire", "EffectiveForm", "FieldState", "resolve"]

_UNSET: Any = object()
"""Sentinel: a formula that couldn't be evaluated this pass (a referenced
input is still unfilled / ill-typed). The effect is skipped rather than
applied with a bad value — the rule simply doesn't bite until its inputs
are well-typed, mirroring :func:`safe_expr.evaluate_condition`."""


def _try_eval(expr: str, vals: Mapping[str, Any]) -> Any:
    """Evaluate a formula, returning :data:`_UNSET` on any data-shaped error."""
    try:
        return safe_expr.evaluate(expr, vals)
    except (TypeError, ZeroDivisionError, NameError, safe_expr.UnsafeExpressionError):
        return _UNSET


@dataclass
class FieldState:
    """The resolved state of one input after all rules have run.

    Defaults mirror the descriptor; a rule whose condition held may
    have overridden any of them. ``forced`` records that a
    ``set_value`` effect fired — ``forced_value`` is then authoritative
    and the user's own input for this field is ignored at submit.
    """

    name: str
    visible: bool = True
    required: bool = True
    disabled: bool = False
    options: list[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    effective_default: Any = None
    forced: bool = False
    forced_value: Any = None
    on_hide: OnHidePolicy = "drop"
    """Only meaningful when ``visible`` is False — how the submit-time
    post-pass treats the hidden field's value (set by the
    :class:`~matofx.tools.descriptor.HideEffect` that hid it; the
    field's resting state is the harmless default ``"drop"``)."""


@dataclass
class ActiveRequire:
    """A validity (``require``) outcome whose owning rule's ``when`` held.

    Carries the must-be-true ``expr``, the ``field`` it's anchored to
    (for error highlighting), and the author's optional ``message``.
    Collected by :func:`resolve` so the submit path and the live form
    can evaluate it against the resolved values without re-deriving which
    rules fired.
    """

    field: str
    expr: str
    message: str | None = None


@dataclass
class EffectiveForm:
    """Resolved per-field state plus the value map rules evaluated against.

    ``resolved_values`` is the externally-supplied values overlaid with
    every ``set_value`` force, in evaluation order — i.e. exactly what
    the rule chain "saw". The submit post-pass starts from this when it
    applies the value-state matrix. ``requires`` lists the validity
    outcomes whose condition held, for the cross-field check to evaluate.
    """

    fields: dict[str, FieldState] = field(default_factory=dict)
    resolved_values: dict[str, Any] = field(default_factory=dict)
    requires: list[ActiveRequire] = field(default_factory=list)

    def for_field(self, name: str) -> FieldState | None:
        """Return the resolved state of ``name``, or ``None`` if unknown."""
        return self.fields.get(name)

    def visible_field_names(self) -> list[str]:
        """Names of currently-visible fields, in descriptor order."""
        return [n for n, s in self.fields.items() if s.visible]


def resolve(
    descriptor: ToolDescriptor,
    values: Mapping[str, Any],
) -> EffectiveForm:
    """Resolve ``descriptor``'s conditional rules against ``values``.

    Args:
        descriptor: The (already-validated) tool contract. Its
            ``dependencies`` are assumed grammar-clean and acyclic —
            that is the descriptor validators' guarantee, not this
            function's responsibility.
        values: Current input values keyed by input name. Missing
            inputs are seeded from their descriptor default so the
            result is deterministic regardless of how complete the
            caller's map is.

    Returns:
        The :class:`EffectiveForm` describing every input's resolved
        state and the value map the rule chain evaluated against.
    """
    base_fields = {f.name: f for f in descriptor.inputs}
    vals: dict[str, Any] = {
        name: values.get(name, _seed_value(spec)) for name, spec in base_fields.items()
    }
    states: dict[str, FieldState] = {name: _base_state(spec) for name, spec in base_fields.items()}
    requires: list[ActiveRequire] = []

    for dep in _ordered_rules(descriptor.dependencies, base_fields):
        if safe_expr.evaluate_condition(dep.when, vals):
            for effect in dep.effects:
                if isinstance(effect, RequireEffect):
                    # A validity outcome changes no form state; it's a
                    # constraint the submit / generation paths evaluate.
                    requires.append(ActiveRequire(effect.field, effect.expr, effect.message))
                else:
                    _apply_effect(effect, states, vals)

    return EffectiveForm(fields=states, resolved_values=vals, requires=requires)


# --- Base state + seeding -------------------------------------------------


def _base_state(spec: InputField) -> FieldState:
    return FieldState(
        name=spec.name,
        visible=True,
        required=spec.required,
        disabled=False,
        options=list(spec.options) if spec.options is not None else None,
        minimum=spec.minimum,
        maximum=spec.maximum,
        effective_default=spec.default,
    )


def _seed_value(spec: InputField) -> Any:
    """Resting value for an input the caller didn't supply.

    Matches the renderer's initial-value rule so live and submit
    resolution agree on a half-filled form.
    """
    if spec.default is not None:
        return spec.default
    if spec.type == "boolean":
        return False
    if spec.type == "multiselect":
        return []
    return None


# --- Rule ordering (Kahn, declaration-order tiebreak) ---------------------


def _ordered_rules(
    dependencies: list[Dependency],
    base_fields: dict[str, InputField],
) -> list[Dependency]:
    """Topologically order the rules; ties broken by declaration order.

    Edge ``A → B`` when rule ``A`` writes a field rule ``B``'s
    condition reads, so a rule is only evaluated once everything it
    depends on has settled. Among rules with nothing left blocking
    them the **lowest declaration index is taken first**, so a
    later-declared rule that writes the same field is applied later and
    therefore wins (the locked last-wins policy).
    """
    n = len(dependencies)
    if n <= 1:
        return list(dependencies)

    reads: list[set[str]] = []
    writes: list[set[str]] = []
    for dep in dependencies:
        # A rule reads every input named in its ``when`` and in any of its
        # effect formulas (set_min_max/set_value/set_default expr, require
        # expr) — all observe values, so a set_value upstream must run
        # first.
        read_names = safe_expr.referenced_names(safe_expr.parse(dep.when))
        for expr_str in (s for e in dep.effects for s in effect_expr_strings(e)):
            read_names |= safe_expr.referenced_names(safe_expr.parse(expr_str))
        reads.append(read_names & base_fields.keys())
        # Only set_value changes what a later rule's condition / formula
        # observes, so only set_value targets create an ordering edge.
        writes.append({e.field for e in dep.effects if isinstance(e, SetValueEffect)})

    indegree = [0] * n
    successors: list[list[int]] = [[] for _ in range(n)]
    for a in range(n):
        for b in range(n):
            if a != b and writes[a] & reads[b]:
                successors[a].append(b)
                indegree[b] += 1

    # Ready set kept sorted so the smallest declaration index is always
    # taken next — that is the last-wins tiebreak.
    ready = sorted(i for i in range(n) if indegree[i] == 0)
    order: list[Dependency] = []
    while ready:
        i = ready.pop(0)
        order.append(dependencies[i])
        for j in successors[i]:
            indegree[j] -= 1
            if indegree[j] == 0:
                bisect.insort(ready, j)

    # A leftover (indegree never hit 0) would mean a cycle — impossible
    # for an installed descriptor (validator rejects cyclic rule graphs),
    # but fall back to declaration order rather than silently dropping
    # rules if a caller hand-built an unvalidated descriptor.
    if len(order) != n:
        return list(dependencies)
    return order


# --- Effect application ---------------------------------------------------


def _apply_show(e: ShowEffect, st: dict[str, FieldState], _v: dict[str, Any]) -> None:
    for name in e.fields:
        if name in st:
            st[name].visible = True


def _apply_hide(e: HideEffect, st: dict[str, FieldState], _v: dict[str, Any]) -> None:
    for name in e.fields:
        if name in st:
            st[name].visible = False
            st[name].on_hide = e.on_hide


def _apply_set_options(e: SetOptionsEffect, st: dict[str, FieldState], _v: dict[str, Any]) -> None:
    if e.field in st:
        st[e.field].options = list(e.options)


def _apply_set_required(
    e: SetRequiredEffect, st: dict[str, FieldState], _v: dict[str, Any]
) -> None:
    if e.field in st:
        st[e.field].required = e.required


def _apply_set_default(e: SetDefaultEffect, st: dict[str, FieldState], v: dict[str, Any]) -> None:
    if e.field not in st:
        return
    if e.value_expr is not None:
        result = _try_eval(e.value_expr, v)
        if result is not _UNSET:
            st[e.field].effective_default = result
        return
    st[e.field].effective_default = e.value


def _coerce_bound(value: Any) -> float | None:
    """A formula bound is usable only if it evaluates to a real number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _apply_set_min_max(e: SetMinMaxEffect, st: dict[str, FieldState], v: dict[str, Any]) -> None:
    if e.field not in st:
        return
    if e.minimum_expr is not None:
        bound = _coerce_bound(_try_eval(e.minimum_expr, v))
        if bound is not None:
            st[e.field].minimum = bound
    elif e.minimum is not None:
        st[e.field].minimum = e.minimum
    if e.maximum_expr is not None:
        bound = _coerce_bound(_try_eval(e.maximum_expr, v))
        if bound is not None:
            st[e.field].maximum = bound
    elif e.maximum is not None:
        st[e.field].maximum = e.maximum


def _apply_set_value(e: SetValueEffect, st: dict[str, FieldState], v: dict[str, Any]) -> None:
    if e.field not in st:
        return
    if e.value_expr is not None:
        result = _try_eval(e.value_expr, v)
        if result is _UNSET:
            # A formula that can't be computed yet (an input it reads is
            # still blank) doesn't force this pass — the field stays the
            # user's, and the rule bites once its inputs are well-typed.
            return
        value = result
    else:
        value = e.value
    st[e.field].forced = True
    st[e.field].forced_value = value
    # Feed the forced value forward so a later rule that reads this
    # field sees what was forced, not the user's input.
    v[e.field] = value


def _apply_disable(e: DisableEffect, st: dict[str, FieldState], _v: dict[str, Any]) -> None:
    if e.field in st:
        st[e.field].disabled = True


_EFFECT_HANDLERS: dict[
    type[object],
    Callable[[Any, dict[str, FieldState], dict[str, Any]], None],
] = {
    ShowEffect: _apply_show,
    HideEffect: _apply_hide,
    SetOptionsEffect: _apply_set_options,
    SetRequiredEffect: _apply_set_required,
    SetDefaultEffect: _apply_set_default,
    SetMinMaxEffect: _apply_set_min_max,
    SetValueEffect: _apply_set_value,
    DisableEffect: _apply_disable,
}


def _apply_effect(
    effect: Effect,
    states: dict[str, FieldState],
    vals: dict[str, Any],
) -> None:
    """Dispatch one effect to its handler.

    A target that isn't a known field is ignored by the handler — the
    descriptor validator already rejects unknown targets at install, so
    this only guards a hand-built unvalidated descriptor.
    """
    _EFFECT_HANDLERS[type(effect)](effect, states, vals)

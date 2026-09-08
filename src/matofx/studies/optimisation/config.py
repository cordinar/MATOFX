"""Configuration shape + the ``configure``-time validation gate (ADR-0009 §3.2/§3.3).

An optimisation study carries one frozen ``config_json`` (the §3.2
shape). :func:`validate_config` is the single gate run when a study
leaves ``draft``: it raises :class:`OptimisationConfigError` on the
**first** broken rule with a plain-English message (the house pattern —
the cohort family's ``validate_filters`` does the same).

The §5 strategy catalogue lives here as a frozen
``STRATEGY_OBJECTIVE_SHAPES`` map so configure-time shape validation
works before the full pluggable strategy *registry* lands (build Phase
2). The registry will consume this same map rather than redefine it, so
the catalogue has one home.

**Phase 1 scope.** Rules 1, 2, 4, 5, 6, 8 (factors, responses,
constraints, output-constraint resolution, stopping/budget, batch/seed)
plus strategy-shape compatibility (rule 3) and the basic run-mode shape
are enforced. The deeper rule-7 surrogate checks (version visible,
factor-superset) need the Model registry and land with the surrogate
run-mode build (Phase 5); rule 9 (workflow-target resolution) lands with
the workflow-target build (Phase 11) — until then a ``workflow`` target
is refused with a plain-English "not available yet". These deferrals are
called out inline so the gate never *silently* skips a rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from matofx.studies.optimisation.errors import OptimisationConfigError
from matofx.studies.optimisation.sumcheck import pick_balance_heuristic

if TYPE_CHECKING:
    from matofx.tools.descriptor import InputField, ToolDescriptor

# Floating-point slack for "the totals match" / "the residual fits" — see
# sumcheck._EPS; factor values here are O(1)..O(1e3).
_SUM_EPS = 1e-9

# --- closed vocabularies ----------------------------------------------------

RUN_MODES: frozenset[str] = frozenset({"real", "surrogate", "surrogate_assisted_real"})
"""``config.run_mode`` values (§6). ``surrogate`` / ``surrogate_assisted_real``
additionally require an attached surrogate version (rule 7)."""

_SURROGATE_RUN_MODES: frozenset[str] = frozenset({"surrogate", "surrogate_assisted_real"})

RESPONSE_GOALS: frozenset[str] = frozenset({"minimise", "maximise", "track"})
"""``response.goal`` values (§3.2). ``track`` is recorded/plotted but not
optimised; at least one ``minimise`` / ``maximise`` is required to leave
``draft`` (except for the ``info_gain`` strategies)."""

_OPTIMISED_GOALS: frozenset[str] = frozenset({"minimise", "maximise"})

# config factor type → the descriptor ``InputFieldType`` it must map to
# (§3.3 rule 1: "int / categorical shapes match the descriptor type").
_FACTOR_TYPE_TO_DESCRIPTOR: dict[str, str] = {
    "float": "float",
    "int": "integer",
    "categorical": "select",
}

_CONSTRAINT_KINDS: frozenset[str] = frozenset({"box", "cross_field", "output"})

_CONVERGENCE_METRICS: frozenset[str] = frozenset({"best_so_far", "hypervolume"})

_MIN_MULTI_OBJECTIVE = 2
"""Optimised-response count at/above which a study is multi-objective —
the threshold for the ``hypervolume`` convergence metric (§8)."""

STRATEGY_OBJECTIVE_SHAPES: dict[str, frozenset[str]] = {
    # Surrogate-driven single-objective BO on MATOFX's own surrogate engine
    # (ADR-0009 amendment 2026-06-08). The acquisition *mode* (optimise /
    # balanced / explore) is a separate config knob, not a separate strategy.
    "bo": frozenset({"single"}),
    # Single-objective classical / evolutionary spectrum (pymoo; build Phase 9).
    # All generational ask/tell over the same encoded real vector as the
    # multi-objective EAs — zero new dependency (§5.2/§5).
    "cmaes": frozenset({"single"}),
    "de": frozenset({"single"}),
    "pso": frozenset({"single"}),
    "ga": frozenset({"single"}),
    # Multi-objective evolutionary (pymoo)
    "nsga2": frozenset({"multi"}),
    "moea_d": frozenset({"multi"}),
    "nsga3": frozenset({"multi"}),
    # Baselines
    "random": frozenset({"single", "multi"}),
    "grid": frozenset({"single", "multi"}),
}
"""The strategy catalogue: name → supported objective shapes.

The *implemented* set (ADR-0009 amendment 2026-06-08 + build Phase 9):
the MATOFX-surrogate ``bo`` replaced the Optuna `bo_ei`/`bo_ucb`/`hyperband`;
the pymoo classical spectrum (`cmaes` / `de` / `pso` / `ga` / `nsga3`) lands
in Phase 9. The scipy local/annealing strategies (`nelder_mead` / `slsqp`
/ `dual_annealing`) and MOP/CoP re-enter this map when their later build
phases land. :func:`validate_config` checks shape compatibility against it,
and the registry cross-checks it."""

ACQUISITION_MODES: frozenset[str] = frozenset({"optimise", "balanced", "explore"})
"""How the ``bo`` strategy turns the surrogate into the next run(s)."""

_SINGLE_ONLY_STRATEGIES: frozenset[str] = frozenset(
    k for k, v in STRATEGY_OBJECTIVE_SHAPES.items() if v == frozenset({"single"})
)
"""Strategies that optimise exactly one objective. Choosing one of these with
two or more optimised responses turns the study into a **weighted-sum** search
(the objectives are scalarised via their weights)."""

WEIGHTED_SUM_KEY: str = "__weighted_sum__"
"""Synthetic objective name the runtime feeds a weighted-sum study's strategy."""


def is_weighted_sum(config: Mapping[str, Any]) -> bool:
    """True when the study scalarises several objectives into one weighted sum.

    That's the case when a single-objective strategy (``bo`` / ``cmaes`` / ``de``
    / ``pso`` / ``ga``) is paired with two or more optimised responses — the
    user has opted into a weighted-sum search rather than a Pareto front.
    """
    strategy = config.get("strategy")
    optimised = sum(
        1
        for r in (config.get("responses") or [])
        if isinstance(r, Mapping) and r.get("goal") in _OPTIMISED_GOALS
    )
    return strategy in _SINGLE_ONLY_STRATEGIES and optimised >= _MIN_MULTI_OBJECTIVE


def weighted_sum_value(responses: Mapping[str, Any], config: Mapping[str, Any]) -> float | None:
    """Scalarise a run's responses into ``Σ wᵢ·signᵢ·fᵢ`` (minimise → +, maximise → −).

    Minimising the result optimises every objective in its own direction. Returns
    ``None`` when any optimised response is missing from ``responses`` (a failed
    run), so the caller can skip it. The response key matches the runner's stored
    output name (``spec.name`` for a scalar response, else the response name).
    """
    total = 0.0
    seen = False
    for r in config.get("responses") or []:
        if not isinstance(r, Mapping) or r.get("goal") not in _OPTIMISED_GOALS:
            continue
        spec = r.get("spec") if isinstance(r.get("spec"), Mapping) else {}
        key = str(spec.get("name") or r.get("name")) if spec.get("kind") == "scalar" else str(
            r.get("name")
        )
        value = responses.get(key)
        if not isinstance(value, int | float) or isinstance(value, bool):
            return None
        weight = r.get("weight", 1.0)
        weight = float(weight) if isinstance(weight, int | float) and not isinstance(weight, bool) else 1.0
        sign = 1.0 if r.get("goal") == "minimise" else -1.0
        total += weight * sign * float(value)
        seen = True
    return total if seen else None

# Strategies that bootstrap their own initial design and need no external
# LHS seed batch (§11) — used only to relax the rule-8 seed check. The
# baselines are space-filling; the pymoo EAs (`cmaes`/`de`/`pso`/`ga` +
# the multi-objective `nsga2`/`moea_d`/`nsga3`) sample their own first
# generation. Only `bo` consumes an external seed (it has no population to
# bootstrap from — the surrogate needs initial points to fit).
_SELF_SEEDING_STRATEGIES: frozenset[str] = frozenset(
    {"grid", "random", "cmaes", "de", "pso", "ga", "nsga2", "moea_d", "nsga3"}
)


def validate_config(
    config: Mapping[str, Any], descriptor: ToolDescriptor
) -> list[dict[str, Any]]:
    """Validate a study config against its pinned tool descriptor (§3.3).

    Raises :class:`OptimisationConfigError` on the first failure with a
    plain-English message safe to surface verbatim. Returns the derived
    **sum-check groups** (§3.6) when the configuration is launch-ready — the
    caller (``configure``) persists them into ``config_json`` so the runtime
    can hold each group's total exact without reloading the descriptor.
    ``descriptor`` is the parsed
    :class:`~matofx.tools.descriptor.ToolDescriptor` of the study's pinned
    tool version.
    """
    target = config.get("target")
    if not isinstance(target, Mapping):
        raise OptimisationConfigError("The study has no evaluation target configured.")
    target_kind = target.get("kind")
    if target_kind == "workflow":
        raise OptimisationConfigError(
            "Workflow-target optimisation isn't available yet. Pick a single tool to optimise."
        )
    if target_kind != "tool":
        raise OptimisationConfigError("The evaluation target must be a tool.")

    inputs_by_name = {f.name: f for f in descriptor.inputs}
    output_names = {o.name for o in descriptor.outputs}

    factor_names = _validate_factors(config, inputs_by_name)
    optimised_count, response_names = _validate_responses(config, output_names)
    strategy = _validate_strategy_shape(config, optimised_count)
    _validate_acquisition(config, strategy)
    _validate_surrogate_pool(config)
    _validate_mop(config, strategy)
    _validate_constraints(config, factor_names, response_names)
    _validate_run_mode(config)
    _validate_stopping(config, optimised_count)
    _validate_batch_and_seed(config, strategy)
    required = {
        name: f"'{f.label or name}'"
        for name, f in inputs_by_name.items()
        if f.required and name not in factor_names
    }
    _validate_fixed_inputs(config, required)
    # §3.6 — a descriptor sum-check whose members are varied must be held exact
    # by the optimiser (derive one balance member). Tool axis key == input name.
    return _collect_sum_groups(
        config, descriptor.inputs, member=None, varied_keys=factor_names
    )


def validate_workflow_config(
    config: Mapping[str, Any], member_descriptors: Mapping[str, ToolDescriptor]
) -> list[dict[str, Any]]:
    """Validate a ``workflow``-target study config (§3.3 rule 9 / §3.4).

    The tool-target :func:`validate_config` can't serve this: a workflow
    candidate spans many members, so factors and responses are
    **member-qualified**. ``member_descriptors`` maps each member tool's
    alias to its parsed :class:`ToolDescriptor`.

    - Each **factor** names an *open* input of one member (an input not
      wired from an upstream member) — ``{name, member, input, type,
      ...}`` with ``name`` a unique key (the strategy axis) and ``input``
      the member's descriptor field. Wired inputs are never factors
      (rule 12).
    - Each **response** names a real ``{member, output}`` in the chain.

    Strategy / acquisition / constraints / run-mode / stopping / batch /
    seed are validated exactly as for a tool target (shared helpers).
    Raises :class:`OptimisationConfigError` on the first broken rule.
    """
    target = config.get("target")
    if not isinstance(target, Mapping) or target.get("kind") != "workflow":
        raise OptimisationConfigError("The evaluation target must be a workflow.")
    member_aliases = set(member_descriptors)
    if not member_aliases:
        raise OptimisationConfigError("The workflow has no member tools to optimise.")

    open_by_member = {
        alias: {f.name: f for f in _open_member_inputs(desc, member_aliases)}
        for alias, desc in member_descriptors.items()
    }
    outputs_by_member = {
        alias: {o.name for o in desc.outputs} for alias, desc in member_descriptors.items()
    }

    factor_names = _validate_workflow_factors(config, open_by_member)
    optimised_count, response_names = _validate_workflow_responses(config, outputs_by_member)
    strategy = _validate_strategy_shape(config, optimised_count)
    _validate_acquisition(config, strategy)
    _validate_surrogate_pool(config)
    _validate_mop(config, strategy)
    _validate_constraints(config, factor_names, response_names)
    _validate_run_mode(config)
    _validate_stopping(config, optimised_count)
    _validate_batch_and_seed(config, strategy)
    # Open required inputs of every member that aren't varied must be held
    # constant (rule 9b / §3.5). Wired inputs are excluded — they're filled
    # from upstream — and ``open_by_member`` already drops them. A factor's
    # ``name`` is an arbitrary key, so identify the varied inputs by their
    # (member, input) pair, not the name.
    varied = {
        (str(f.get("member")), str(f.get("input")))
        for f in (config.get("factors") or [])
        if isinstance(f, Mapping)
    }
    required = {
        f"{member}.{name}": f"'{f.label or name}' ({member})"
        for member, fields in open_by_member.items()
        for name, f in fields.items()
        if f.required and (member, name) not in varied
    }
    _validate_fixed_inputs(config, required)
    # §3.6 — sum-checks are declared per member descriptor; collect each
    # member's groups with member-qualified axis keys.
    varied_by_member_input = {
        (str(f["member"]), str(f["input"])): str(f["name"])
        for f in (config.get("factors") or [])
        if isinstance(f, Mapping) and {"member", "input", "name"} <= f.keys()
    }
    groups: list[dict[str, Any]] = []
    for alias, desc in member_descriptors.items():
        groups.extend(
            _collect_sum_groups(
                config,
                desc.inputs,
                member=alias,
                varied_keys=None,
                varied_by_member_input=varied_by_member_input,
            )
        )
    return groups


def _validate_fixed_inputs(
    config: Mapping[str, Any], required_labels: Mapping[str, str]
) -> None:
    """Rule 9b (§3.5) — every required, non-varied input is held constant.

    ``required_labels`` maps each input key that must carry a fixed value
    (a tool input name, or ``member.input`` for a workflow) to a friendly
    label for the error. A value is "set" when it isn't ``None`` or an
    empty string — ``0`` / ``False`` are legitimate held values. Materials
    are held as their non-empty ``material_version_id`` string.
    """
    raw = config.get("fixed_inputs")
    fixed = raw if isinstance(raw, Mapping) else {}
    missing = [
        label for key, label in required_labels.items() if fixed.get(key) in (None, "")
    ]
    if missing:
        joined = ", ".join(sorted(missing))
        raise OptimisationConfigError(
            "These required inputs aren't being varied, so they need a fixed value "
            f"before the study can start: {joined}."
        )


# --- sum-checks (§3.6) ------------------------------------------------------


@dataclass(frozen=True)
class _Bounds:
    """The factor facts the sum-check repair needs (``_Bounded`` in sumcheck)."""

    kind: str
    low: float | None
    high: float | None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _factor_specs(config: Mapping[str, Any]) -> dict[str, _Bounds]:
    """Numeric varied factors → bounds, keyed by axis key (the factor ``name``)."""
    specs: dict[str, _Bounds] = {}
    for factor in config.get("factors") or []:
        if not isinstance(factor, Mapping):
            continue
        name, ftype = factor.get("name"), factor.get("type")
        if ftype in ("float", "int") and isinstance(name, str):
            try:
                specs[name] = _Bounds(str(ftype), float(factor["min"]), float(factor["max"]))
            except (KeyError, TypeError, ValueError):
                continue
    return specs


def _resolve_sum_member(
    pname: str,
    *,
    member: str | None,
    varied_keys: set[str] | None,
    varied_by_member_input: Mapping[tuple[str, str], str] | None,
) -> tuple[str, str]:
    """Map a descriptor input name to its ``(axis_key, role)`` for the active target.

    Tool: axis key == input name; varied iff it's a factor. Workflow: a varied
    member's axis key is its factor ``name`` (arbitrary key), matched by the
    ``(member, input)`` pair; a held member's key is ``member.input`` (the
    ``fixed_inputs`` convention).
    """
    if member is None:  # tool target
        if varied_keys is not None and pname in varied_keys:
            return pname, "varied"
        return pname, "fixed"
    if varied_by_member_input and (member, pname) in varied_by_member_input:
        return varied_by_member_input[(member, pname)], "varied"
    return f"{member}.{pname}", "fixed"


def _collect_sum_groups(
    config: Mapping[str, Any],
    inputs: Sequence[InputField],
    *,
    member: str | None,
    varied_keys: set[str] | None,
    varied_by_member_input: Mapping[tuple[str, str], str] | None = None,
) -> list[dict[str, Any]]:
    """Build + validate the §3.6 sum-group records for one descriptor's inputs.

    A sum-check is declared on every participating input, so the same group is
    seen once per member input — de-duplicated by ``(member, value, params)``.
    Returns one record per group that has ≥1 varied member; a fully-held group
    is validated (must already total the value) and contributes no record.
    """
    specs = _factor_specs(config)
    fixed_raw = config.get("fixed_inputs")
    fixed = fixed_raw if isinstance(fixed_raw, Mapping) else {}
    field_by_name = {f.name: f for f in inputs}
    seen: set[tuple[Any, ...]] = set()
    groups: list[dict[str, Any]] = []
    for field in inputs:
        sc = field.sum_check
        if sc is None:
            continue
        signature = (member, float(sc.value), tuple(sc.sum_parameters))
        if signature in seen:
            continue
        seen.add(signature)
        members: list[dict[str, Any]] = []
        for pname in sc.sum_parameters:
            axis_key, role = _resolve_sum_member(
                pname,
                member=member,
                varied_keys=varied_keys,
                varied_by_member_input=varied_by_member_input,
            )
            if role == "varied":
                members.append(
                    {
                        "factor": axis_key,
                        "input_key": axis_key,
                        "role": "varied",
                        "fixed_value": None,
                    }
                )
            else:
                members.append(
                    _fixed_sum_member(
                        axis_key, pname, fixed.get(axis_key), field_by_name.get(pname), sc.value
                    )
                )
        group = _finalise_sum_group(float(sc.value), members, specs)
        if group is not None:
            groups.append(group)
    return groups


def _fixed_sum_member(
    axis_key: str, input_name: str, fixed_value: Any, field: InputField | None, value: float
) -> dict[str, Any]:
    """A held sum-member record — value from ``fixed_inputs`` or the descriptor default."""
    if _is_number(fixed_value):
        held = float(fixed_value)
    elif field is not None and _is_number(field.default):
        held = float(field.default)
    else:
        raise OptimisationConfigError(
            f"Input '{input_name}' is part of a group that must sum to {value:g}, but it isn't "
            "being varied and has no fixed value to hold it at."
        )
    return {"factor": axis_key, "input_key": axis_key, "role": "fixed", "fixed_value": held}


def _finalise_sum_group(
    value: float, members: list[dict[str, Any]], specs: Mapping[str, _Bounds]
) -> dict[str, Any] | None:
    """Pick the heuristic balance + check feasibility; ``None`` for a fully-held group."""
    varied = [m["factor"] for m in members if m["role"] == "varied"]
    if not varied:
        total = sum(float(m["fixed_value"]) for m in members)
        if abs(total - value) > _SUM_EPS:
            raise OptimisationConfigError(
                f"The inputs that must sum to {value:g} are all held at fixed values that "
                f"total {total:g} instead."
            )
        return None
    balance = pick_balance_heuristic(varied, specs)
    _validate_sum_feasible(value, members, balance, specs)
    # Store the derived member's bounds on the record so the runtime can fill
    # it in without a factor-spec map (the member is removed from the search
    # space at study load, so its spec isn't on the strategy context).
    bspec = specs.get(balance) if balance is not None else None
    return {
        "value": value,
        "members": members,
        "balance": balance,
        "balance_kind": bspec.kind if bspec is not None else "float",
        "balance_low": bspec.low if bspec is not None else None,
        "balance_high": bspec.high if bspec is not None else None,
    }


def _validate_sum_feasible(
    value: float, members: list[dict[str, Any]], balance: str | None, specs: Mapping[str, _Bounds]
) -> None:
    """Reject a group whose balance member can never land in range (empty overlap)."""
    bal = specs.get(balance) if balance is not None else None
    if bal is None or bal.low is None or bal.high is None:
        return
    lo_sum = hi_sum = 0.0
    for member in members:
        if member["role"] == "fixed":
            lo_sum += float(member["fixed_value"])
            hi_sum += float(member["fixed_value"])
        elif member["factor"] != balance:
            spec = specs.get(member["factor"])
            if spec is None or spec.low is None or spec.high is None:
                return
            lo_sum += spec.low
            hi_sum += spec.high
    residual_lo, residual_hi = value - hi_sum, value - lo_sum
    if residual_hi < bal.low - _SUM_EPS or residual_lo > bal.high + _SUM_EPS:
        raise OptimisationConfigError(
            f"The inputs that must sum to {value:g} can't be satisfied within their ranges — "
            "widen a range or change a fixed value."
        )


def _open_member_inputs(descriptor: ToolDescriptor, member_aliases: set[str]) -> list[Any]:
    """A member's *open* input fields — not wired from another member (rule 12)."""
    return [
        field
        for field in descriptor.inputs
        if not (field.downstream_input and field.upstream_tool in member_aliases)
    ]


def _validate_workflow_factors(
    config: Mapping[str, Any], open_by_member: Mapping[str, Mapping[str, Any]]
) -> set[str]:
    """Rule 1+9 (workflow) — ≥1 factor, each an open input of a named member."""
    factors = config.get("factors")
    if not isinstance(factors, Sequence) or not factors:
        raise OptimisationConfigError("An optimisation study needs at least one factor to vary.")

    names: set[str] = set()
    for factor in factors:
        if not isinstance(factor, Mapping):
            raise OptimisationConfigError("Each factor must be a {name, member, input, ...} entry.")
        name = factor.get("name")
        if not isinstance(name, str) or not name:
            raise OptimisationConfigError("Every factor needs a name.")
        if name in names:
            raise OptimisationConfigError(f"Factor '{name}' is listed more than once.")
        names.add(name)

        member = factor.get("member")
        if not isinstance(member, str) or member not in open_by_member:
            raise OptimisationConfigError(
                f"Factor '{name}' names member {member!r}, which isn't a tool in this workflow."
            )
        input_name = factor.get("input")
        field = open_by_member[member].get(input_name) if isinstance(input_name, str) else None
        if field is None:
            raise OptimisationConfigError(
                f"Factor '{name}' maps to {member}.{input_name}, which isn't an *open* "
                "input of that member (it may be wired from an upstream tool)."
            )
        ftype = factor.get("type")
        expected = _FACTOR_TYPE_TO_DESCRIPTOR.get(ftype) if isinstance(ftype, str) else None
        if expected is None:
            raise OptimisationConfigError(
                f"Factor '{name}' has an unsupported type {ftype!r}; "
                "use 'float', 'int', or 'categorical'."
            )
        if field.type != expected:
            raise OptimisationConfigError(
                f"Factor '{name}' is declared {ftype!r} but {member}.{input_name} is "
                f"a {field.type!r} field."
            )
        if ftype == "categorical":
            _validate_categorical_factor(name, factor, field)
        else:
            _validate_numeric_factor(name, factor, field)
    return names


def _validate_workflow_responses(
    config: Mapping[str, Any], outputs_by_member: Mapping[str, set[str]]
) -> tuple[int, set[str]]:
    """Rule 2+9 (workflow) — responses name a real ``{member, output}``."""
    responses = config.get("responses")
    if not isinstance(responses, Sequence) or not responses:
        raise OptimisationConfigError("An optimisation study needs at least one response.")

    names: set[str] = set()
    optimised = 0
    for response in responses:
        if not isinstance(response, Mapping):
            raise OptimisationConfigError("Each response must be a {name, spec, goal} entry.")
        name = response.get("name")
        if not isinstance(name, str) or not name:
            raise OptimisationConfigError("Every response needs a name.")
        if name in names:
            raise OptimisationConfigError(f"Response '{name}' is listed more than once.")
        names.add(name)

        goal = response.get("goal")
        if goal not in RESPONSE_GOALS:
            raise OptimisationConfigError(
                f"Response '{name}' has goal {goal!r}; use 'minimise', 'maximise', or 'track'."
            )
        if goal in _OPTIMISED_GOALS:
            optimised += 1
            _validate_weight(name, response)

        spec = response.get("spec")
        if not isinstance(spec, Mapping) or spec.get("kind") != "scalar":
            raise OptimisationConfigError(
                f"Response '{name}' needs a scalar spec naming a member output."
            )
        member = spec.get("member")
        if not isinstance(member, str) or member not in outputs_by_member:
            raise OptimisationConfigError(
                f"Response '{name}' names member {member!r}, which isn't a tool in this workflow."
            )
        output = spec.get("name", name)
        if output not in outputs_by_member[member]:
            raise OptimisationConfigError(
                f"Response '{name}' maps to {member}.{output}, which that member doesn't produce."
            )
    return optimised, names


def _validate_acquisition(config: Mapping[str, Any], strategy: str) -> None:
    """The ``bo`` strategy's acquisition mode must be a known one (amendment)."""
    if strategy != "bo":
        return
    acquisition = config.get("acquisition", "balanced")
    if acquisition not in ACQUISITION_MODES:
        raise OptimisationConfigError(
            f"Acquisition mode {acquisition!r} isn't supported; "
            "use 'optimise', 'balanced', or 'explore'."
        )


def _validate_surrogate_pool(config: Mapping[str, Any]) -> None:
    """The ``bo`` candidate-pool size, when set, must be a sane whole number.

    Caps how many points the surrogate scores per ``ask`` before picking the
    next run(s). Absent → the strategy default. Validated regardless of
    strategy (harmless for strategies that ignore it).
    """
    pool = config.get("surrogate_candidate_pool")
    if pool is None:
        return
    if not isinstance(pool, int) or isinstance(pool, bool) or not (16 <= pool <= 1_000_000):
        raise OptimisationConfigError(
            "'surrogate_candidate_pool' must be a whole number between 16 and 1000000."
        )


def _validate_mop(config: Mapping[str, Any], strategy: str) -> None:
    """Rule 10 — MOP/CoP is a ``bo``-only flag (§5.7, amendment 2026-06-08).

    MOP/CoP needs a metamodel it can rank + measure factor importance on;
    the surrogate-search (``bo``) strategy is the only one that builds one
    (random / grid / the evolutionary spectrum don't), so the flag is
    rejected for any other strategy.
    """
    mop = config.get("mop")
    if mop is None:
        return
    if not isinstance(mop, Mapping):
        raise OptimisationConfigError("'mop' must be a mapping.")
    if not mop.get("enabled"):
        return
    if strategy != "bo":
        raise OptimisationConfigError(
            "MOP/CoP variable reduction needs the surrogate-search ('bo') strategy — "
            "it's the only one that builds a metamodel to measure factor importance."
        )
    cutoff = mop.get("cop_cutoff")
    if cutoff is not None and (
        not isinstance(cutoff, int | float)
        or isinstance(cutoff, bool)
        or not 0.0 < float(cutoff) < 1.0
    ):
        raise OptimisationConfigError("'mop.cop_cutoff' must be a number strictly between 0 and 1.")


def _validate_factors(config: Mapping[str, Any], inputs_by_name: Mapping[str, Any]) -> set[str]:
    """Rule 1 — ≥1 factor, each in the descriptor, types + bounds sane."""
    factors = config.get("factors")
    if not isinstance(factors, Sequence) or not factors:
        raise OptimisationConfigError("An optimisation study needs at least one factor to vary.")

    names: set[str] = set()
    for factor in factors:
        if not isinstance(factor, Mapping):
            raise OptimisationConfigError("Each factor must be a {name, type, ...} entry.")
        name = factor.get("name")
        if not isinstance(name, str) or not name:
            raise OptimisationConfigError("Every factor needs a name.")
        if name in names:
            raise OptimisationConfigError(f"Factor '{name}' is listed more than once.")
        names.add(name)

        field = inputs_by_name.get(name)
        if field is None:
            raise OptimisationConfigError(f"Factor '{name}' isn't an input of the chosen tool.")
        ftype = factor.get("type")
        expected = _FACTOR_TYPE_TO_DESCRIPTOR.get(ftype) if isinstance(ftype, str) else None
        if expected is None:
            raise OptimisationConfigError(
                f"Factor '{name}' has an unsupported type "
                f"{ftype!r}; use 'float', 'int', or 'categorical'."
            )
        if field.type != expected:
            raise OptimisationConfigError(
                f"Factor '{name}' is declared {ftype!r} but the tool input is "
                f"a {field.type!r} field."
            )
        if ftype == "categorical":
            _validate_categorical_factor(name, factor, field)
        else:
            _validate_numeric_factor(name, factor, field)
    return names


def factor_precisions(
    config: Mapping[str, Any], descriptor: ToolDescriptor
) -> dict[str, float]:
    """Each float factor's declared step, by name (empty when none declare one).

    Frozen into the config by ``configure`` so the runtime snaps candidates
    without reloading the descriptor — the same reason ``sum_groups`` is frozen.
    Integer factors are excluded: ``snap_int`` already owns those.
    """
    fields = {f.name: f for f in descriptor.inputs}
    out: dict[str, float] = {}
    for factor in config.get("factors") or []:
        if not isinstance(factor, Mapping) or factor.get("type") != "float":
            continue
        field = fields.get(factor.get("name"))
        precision = getattr(field, "precision", None)
        if isinstance(precision, int | float) and precision > 0:
            out[str(factor["name"])] = float(precision)
    return out


def _validate_numeric_factor(name: str, factor: Mapping[str, Any], field: Any) -> None:
    """Rule 1 (numeric) — finite ``min ≤ max`` within the descriptor box."""
    lo = factor.get("min")
    hi = factor.get("max")
    if not isinstance(lo, int | float) or not isinstance(hi, int | float):
        raise OptimisationConfigError(f"Factor '{name}' needs numeric 'min' and 'max' bounds.")
    if hi < lo:
        raise OptimisationConfigError(f"Factor '{name}' has max < min ({hi} < {lo}).")
    if field.minimum is not None and lo < field.minimum:
        raise OptimisationConfigError(
            f"Factor '{name}' lower bound {lo} is below the tool's minimum "
            f"{field.minimum}; a factor range may be tightened but never widened."
        )
    if field.maximum is not None and hi > field.maximum:
        raise OptimisationConfigError(
            f"Factor '{name}' upper bound {hi} exceeds the tool's maximum "
            f"{field.maximum}; a factor range may be tightened but never widened."
        )
    if field.type == "integer":
        for label, bound in (("min", lo), ("max", hi)):
            if not float(bound).is_integer():
                raise OptimisationConfigError(
                    f"Factor '{name}' is an integer input — its {label} must be a "
                    f"whole number; got {bound}."
                )


def _validate_categorical_factor(name: str, factor: Mapping[str, Any], field: Any) -> None:
    """Rule 1 (categorical) — non-empty choices that the tool offers."""
    choices = factor.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, str) or not choices:
        raise OptimisationConfigError(
            f"Categorical factor '{name}' needs a non-empty 'choices' list."
        )
    allowed = set(field.options or [])
    unknown = [c for c in choices if c not in allowed]
    if unknown:
        raise OptimisationConfigError(
            f"Categorical factor '{name}' includes choice(s) the tool doesn't offer: "
            + ", ".join(repr(c) for c in unknown)
            + "."
        )


def _validate_responses(config: Mapping[str, Any], output_names: set[str]) -> tuple[int, set[str]]:
    """Rule 2 — responses well-formed + resolvable; count optimised ones."""
    responses = config.get("responses")
    if not isinstance(responses, Sequence) or not responses:
        raise OptimisationConfigError("An optimisation study needs at least one response.")

    names: set[str] = set()
    optimised = 0
    for response in responses:
        if not isinstance(response, Mapping):
            raise OptimisationConfigError("Each response must be a {name, spec, goal} entry.")
        name = response.get("name")
        if not isinstance(name, str) or not name:
            raise OptimisationConfigError("Every response needs a name.")
        if name in names:
            raise OptimisationConfigError(f"Response '{name}' is listed more than once.")
        names.add(name)

        goal = response.get("goal")
        if goal not in RESPONSE_GOALS:
            raise OptimisationConfigError(
                f"Response '{name}' has goal {goal!r}; use 'minimise', 'maximise', or 'track'."
            )
        if goal in _OPTIMISED_GOALS:
            optimised += 1
            _validate_weight(name, response)
        _validate_response_spec(name, response.get("spec"), output_names)
    return optimised, names


def _validate_weight(name: str, response: Mapping[str, Any]) -> None:
    """Rule 2 (weights) — an optional per-objective weight must be a positive number.

    Default (absent) is 1.0. How the weight is used is strategy-dependent
    (weighted-sum scalarisation vs Pareto normalisation/preference); this gate
    only checks the value is sane.
    """
    weight = response.get("weight")
    if weight is None:
        return
    if not isinstance(weight, int | float) or isinstance(weight, bool) or float(weight) <= 0:
        raise OptimisationConfigError(
            f"Response '{name}' weight must be a positive number (got {weight!r})."
        )


def _validate_response_spec(name: str, spec: Any, output_names: set[str]) -> None:
    """Rule 2 — a response resolves to a real scalar output or a CSV column."""
    if not isinstance(spec, Mapping):
        raise OptimisationConfigError(f"Response '{name}' has no spec.")
    kind = spec.get("kind")
    if kind == "scalar":
        # A scalar response names a tool output directly (its own name
        # unless the spec overrides it).
        target = spec.get("name", name)
        if target not in output_names:
            raise OptimisationConfigError(
                f"Response '{name}' maps to output {target!r}, which the tool doesn't produce."
            )
    elif kind == "csv_column":
        if not spec.get("file") or spec.get("column") is None:
            raise OptimisationConfigError(
                f"Response '{name}' is a CSV-column response but is missing 'file' or 'column'."
            )
    else:
        raise OptimisationConfigError(
            f"Response '{name}' has spec kind {kind!r}; use 'scalar' or 'csv_column'."
        )


def _validate_strategy_shape(config: Mapping[str, Any], optimised_count: int) -> str:
    """Rule 3 — strategy exists + supports the responses' objective shape."""
    strategy = config.get("strategy")
    if not isinstance(strategy, str) or strategy not in STRATEGY_OBJECTIVE_SHAPES:
        raise OptimisationConfigError(f"Strategy {strategy!r} isn't one MATOFX offers.")
    supported = STRATEGY_OBJECTIVE_SHAPES[strategy]

    if supported == frozenset({"info_gain"}):
        # Active-learning strategies improve a surrogate; they take no
        # optimised response (§5.4).
        if optimised_count:
            raise OptimisationConfigError(
                f"Strategy {strategy!r} improves a surrogate and takes no "
                "minimise/maximise response. Remove the response goals or pick "
                "an optimising strategy."
            )
        return strategy

    if optimised_count == 0:
        raise OptimisationConfigError(
            "An optimisation study needs at least one response to minimise or maximise."
        )
    shape = "single" if optimised_count == 1 else "multi"
    # A single-objective strategy with several objectives is a *weighted-sum*
    # search — the objectives are scalarised via their weights (§ amendment),
    # so it's allowed rather than rejected.
    if shape == "multi" and supported == frozenset({"single"}):
        return strategy
    if shape not in supported:
        nice = "a single objective" if shape == "single" else "multiple objectives"
        raise OptimisationConfigError(
            f"Strategy {strategy!r} doesn't support {nice}. "
            f"You have {optimised_count} optimised response(s)."
        )
    return strategy


def _validate_constraints(
    config: Mapping[str, Any], factor_names: set[str], response_names: set[str]
) -> None:
    """Rules 4 + 5 — cross-field compiles over factors; output names a response."""
    constraints = config.get("constraints")
    if constraints is None:
        return
    if not isinstance(constraints, Sequence):
        raise OptimisationConfigError("'constraints' must be a list.")

    for constraint in constraints:
        if not isinstance(constraint, Mapping):
            raise OptimisationConfigError("Each constraint must be a mapping.")
        kind = constraint.get("kind")
        if kind not in _CONSTRAINT_KINDS:
            raise OptimisationConfigError(
                f"Constraint kind {kind!r} isn't supported; use 'box', 'cross_field', or 'output'."
            )
        if kind == "cross_field":
            _validate_cross_field_constraint(constraint, factor_names)
        elif kind == "output":
            _validate_output_constraint(constraint, response_names)


def _validate_cross_field_constraint(constraint: Mapping[str, Any], factor_names: set[str]) -> None:
    """Rule 4 — the ``safe_expr`` compiles and references only declared factors."""
    from matofx.tools.forms import safe_expr  # noqa: PLC0415

    expr = constraint.get("expr")
    if not isinstance(expr, str) or not expr.strip():
        raise OptimisationConfigError("A cross-field constraint needs an 'expr'.")
    try:
        tree = safe_expr.parse(expr)
    except safe_expr.UnsafeExpressionError as exc:
        raise OptimisationConfigError(f"The cross-field constraint won't compile: {exc}") from exc
    unknown = sorted(safe_expr.referenced_names(tree) - factor_names)
    if unknown:
        raise OptimisationConfigError(
            "The cross-field constraint references name(s) that aren't factors: "
            + ", ".join(unknown)
            + "."
        )


def _validate_output_constraint(constraint: Mapping[str, Any], response_names: set[str]) -> None:
    """Rule 5 — an output constraint names a declared response with op + value."""
    response = constraint.get("response")
    if response not in response_names:
        raise OptimisationConfigError(
            f"The output constraint names undeclared response {response!r}."
        )
    if constraint.get("op") not in {"<=", "<", ">=", ">", "==", "!="}:
        raise OptimisationConfigError(
            f"The output constraint on {response!r} needs a comparison operator."
        )
    if not isinstance(constraint.get("value"), int | float):
        raise OptimisationConfigError(
            f"The output constraint on {response!r} needs a numeric value."
        )


def _validate_run_mode(config: Mapping[str, Any]) -> None:
    """Run-mode shape + the rule-7 surrogate-attachment basic check.

    The deeper rule-7 checks (version exists / visible / tool-matches /
    factor-superset) need the DB and run at ``configure`` time in the
    service. ``surrogate_assisted_real`` is refused here for now — its
    per-batch refit-on-study-data semantics are a later build slice.
    """
    run_mode = config.get("run_mode")
    if run_mode not in RUN_MODES:
        raise OptimisationConfigError(
            f"Run mode {run_mode!r} isn't supported; use 'real', 'surrogate', "
            "or 'surrogate_assisted_real'."
        )
    if run_mode == "surrogate_assisted_real":
        raise OptimisationConfigError(
            "Surrogate-assisted-real mode isn't available yet. Use 'real' or 'surrogate'."
        )
    if run_mode in _SURROGATE_RUN_MODES and config.get("surrogate_model_version_id") is None:
        raise OptimisationConfigError(
            "A surrogate run mode needs an attached surrogate model version."
        )


def _validate_stopping(config: Mapping[str, Any], optimised_count: int) -> None:
    """Rule 6 — mandatory ``max_runs``; convergence metric sane."""
    criteria = config.get("stopping_criteria")
    if not isinstance(criteria, Sequence) or not criteria:
        raise OptimisationConfigError(
            "An optimisation study needs a stopping budget — at least a 'max_runs' criterion."
        )
    has_max_runs = False
    for criterion in criteria:
        if not isinstance(criterion, Mapping):
            raise OptimisationConfigError("Each stopping criterion must be a mapping.")
        kind = criterion.get("kind")
        if kind == "max_runs":
            value = criterion.get("value")
            if not isinstance(value, int) or value < 1:
                raise OptimisationConfigError("'max_runs' must be a positive whole number.")
            has_max_runs = True
        elif kind == "convergence":
            metric = criterion.get("metric")
            if metric not in _CONVERGENCE_METRICS:
                raise OptimisationConfigError(
                    f"Convergence metric {metric!r} must be 'best_so_far' or 'hypervolume'."
                )
            if metric == "hypervolume" and optimised_count < _MIN_MULTI_OBJECTIVE:
                raise OptimisationConfigError(
                    "The 'hypervolume' convergence metric only applies to "
                    "multi-objective studies (two or more optimised responses)."
                )
    if not has_max_runs:
        raise OptimisationConfigError(
            "An optimisation study must declare a 'max_runs' budget — no study runs unbounded."
        )


def _validate_batch_and_seed(config: Mapping[str, Any], strategy: str) -> None:
    """Rule 8 — batch / concurrency / seed sizes are positive."""
    batch_size = config.get("batch_size", 1)
    if not isinstance(batch_size, int) or batch_size < 1:
        raise OptimisationConfigError("'batch_size' must be a positive whole number.")
    concurrency_cap = config.get("concurrency_cap", 1)
    if not isinstance(concurrency_cap, int) or concurrency_cap < 1:
        raise OptimisationConfigError("'concurrency_cap' must be a positive whole number.")

    seed = config.get("seed")
    needs_seed = config.get("run_mode") != "surrogate" and strategy not in _SELF_SEEDING_STRATEGIES
    if seed is not None:
        if not isinstance(seed, Mapping):
            raise OptimisationConfigError("'seed' must be a {kind, n} mapping.")
        n = seed.get("n")
        if not isinstance(n, int) or n < 1:
            raise OptimisationConfigError("The seed batch size 'seed.n' must be at least 1.")
    elif needs_seed:
        raise OptimisationConfigError(
            "This strategy needs an initial seed batch — declare 'seed' with a point count."
        )

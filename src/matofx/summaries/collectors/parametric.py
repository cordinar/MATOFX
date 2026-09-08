"""Fact collection for a parametric study.

Composes the study row + its per-child results + the **same engineering
statistics the Results explorer uses** (``matofx.studies.analysis``) into a
:class:`SummaryFacts`. The analysis is **method-aware** (design §3.1): the
``details["analysis"]`` block is tagged by ``kind`` chosen from the study's
design mode, so the prose depth scales with the methodology rather than
flattening every study to one line.

Best-effort throughout: where a richer fit can't be computed it degrades to the
cheaper standardised-regression ranking, then to plain ranges. All display
strings carry descriptor labels, never internal names.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from matofx.studies import (
    anova_contributions,
    compute_sensitivity,
    fit_rsm,
    main_effects,
    summarize,
    varied_columns,
)
from matofx.studies.orm import ParametricStudy
from matofx.studies.results_service import StudyChildResult, StudyResultsView
from matofx.summaries.collectors._descriptor import DescriptorMaps, descriptor_maps
from matofx.summaries.errors import NonTerminalResultError
from matofx.summaries.facts import HeadlineNumber, SummaryFacts, is_terminal

_MAX_RESPONSES = 4
_MIN_SCREENING_LEVELS = 2
_TOP_DESIGN_INPUTS = 4


async def collect_parametric_facts(
    session: AsyncSession,
    *,
    study: ParametricStudy,
    results: StudyResultsView,
) -> SummaryFacts:
    """Build :class:`SummaryFacts` for a terminal parametric study."""
    if not is_terminal(study.state):
        raise NonTerminalResultError(f"study {study.id} is in state {study.state!r}")

    children = results.children
    maps = await descriptor_maps(session, study.tool_version_id)

    factor_columns = _numeric_columns(children, attr="inputs")
    varied = varied_columns(factor_columns)
    varied_factor_columns = {name: factor_columns[name] for name in varied}

    response_columns = _numeric_columns(children, attr="scalars")
    response_names = _ordered_responses(response_columns, maps.output_order)
    responses_facts = []
    for name in response_names[:_MAX_RESPONSES]:
        fact = _response_facts(name, response_columns[name], children, varied, maps)
        fact["top_driver"] = _top_driver(
            varied_factor_columns, response_columns[name], maps.input_label
        )
        responses_facts.append(fact)

    analysis: dict[str, Any] = {"kind": "none"}
    if response_names and varied:
        primary = response_names[0]
        analysis = _build_analysis(
            mode=study.mode,
            factor_columns=varied_factor_columns,
            response=response_columns[primary],
            primary_label=maps.output_label(primary),
            primary_unit=maps.output_unit(primary),
            label_of=maps.input_label,
        )

    details: dict[str, Any] = {
        "design_mode": study.mode,
        "generated": study.generated_count,
        "completed": sum(1 for c in children if c.state == "complete"),
        "skipped_invalid": study.skipped_invalid_count,
        "run_cap": study.run_cap,
        "seed": study.seed,
        "factors": [maps.input_label(n) for n in varied],
        "responses": responses_facts,
        "analysis": analysis,
    }

    headline = tuple(
        HeadlineNumber(
            label=r["label"],
            value=_round(r["max"]) if r["max"] is not None else None,
            unit=r["unit"],
        )
        for r in responses_facts[:2]
    )

    return SummaryFacts(
        surface_kind="parametric_study",
        surface_id=study.id,
        title=study.name or f"Parametric study {study.id}",
        state=study.state,
        headline=headline,
        submitted_by=None,
        started_at=study.created_at.isoformat() if study.created_at else None,
        finished_at=(
            study.state_changed_at.isoformat() if study.state_changed_at else None
        ),
        duration_seconds=_duration(study),
        details=details,
    )


# --- analysis block (method-aware) -------------------------------------------


def _build_analysis(
    *,
    mode: str,
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
    primary_label: str,
    primary_unit: str | None,
    label_of: Callable[[str], str],
) -> dict[str, Any]:
    """Pick + compute the analysis block whose ``kind`` matches the design mode."""
    if mode in ("ccd", "box_behnken"):
        block = _response_surface(
            factor_columns, response, primary_label, primary_unit, label_of
        )
    elif mode in ("plackett_burman", "frac_factorial"):
        block = _screening(
            factor_columns, response, primary_label, primary_unit, label_of, mode
        )
    elif mode == "grid":
        block = _anova(factor_columns, response, primary_label, label_of)
    else:
        block = None  # lhs / sobol / random / table / list → regression below

    if block is None:
        block = _src_regression(factor_columns, response, primary_label, label_of)
    return block if block is not None else {"kind": "none"}


def _response_surface(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
    primary_label: str,
    primary_unit: str | None,
    label_of: Callable[[str], str],
) -> dict[str, Any] | None:
    fit = fit_rsm(factor_columns, response)
    if fit.r_squared is None:
        return None
    stationary = (
        {label_of(name): _round(value) for name, value in fit.stationary.items()}
        if fit.stationary
        else None
    )
    return {
        "kind": "response_surface",
        "response": primary_label,
        "unit": primary_unit,
        "r2": _round(fit.r_squared),
        "r2_adj": _round(fit.adj_r_squared),
        "stationary": stationary,
        "stationary_kind": fit.stationary_kind,
        "stationary_value": _round(fit.stationary_value),
    }


def _screening(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
    primary_label: str,
    primary_unit: str | None,
    label_of: Callable[[str], str],
    mode: str,
) -> dict[str, Any] | None:
    effects_by_factor = main_effects(factor_columns, response)
    effects: list[dict[str, Any]] = []
    for name, levels in effects_by_factor.items():
        if len(levels) >= _MIN_SCREENING_LEVELS:
            effect = levels[-1][1] - levels[0][1]  # mean(high) - mean(low)
            effects.append({"factor": label_of(name), "effect": _round(effect)})
    if not effects:
        return None
    effects.sort(key=lambda e: abs(e["effect"] or 0.0), reverse=True)
    return {
        "kind": "screening_effects",
        "response": primary_label,
        "unit": primary_unit,
        "effects": effects,
        "aliased": mode == "frac_factorial",
    }


def _anova(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
    primary_label: str,
    label_of: Callable[[str], str],
) -> dict[str, Any] | None:
    result = anova_contributions(factor_columns, response)
    if not result.terms:
        return None
    return {
        "kind": "anova",
        "response": primary_label,
        "terms": [
            {"term": _relabel_term(t.term, label_of), "contribution": _round(t.contribution)}
            for t in result.terms
        ],
        "residual": _round(result.residual),
    }


def _top_driver(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
    label_of: Callable[[str], str],
) -> dict[str, Any] | None:
    """The single most influential varied factor for one response (label + sign).

    A lightweight per-response sensitivity so the summary can say what drives
    *each* response, not only the primary one. ``None`` when there's too little
    signal to rank.
    """
    if not factor_columns:
        return None
    result = compute_sensitivity("r", list(factor_columns), factor_columns, response)
    if not result.sufficient or not result.factors:
        return None
    top = result.factors[0]
    if top.std_coef is None:
        return None
    return {
        "label": label_of(top.factor),
        "sign": "positive" if top.std_coef >= 0 else "negative",
    }


def _src_regression(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
    primary_label: str,
    label_of: Callable[[str], str],
) -> dict[str, Any] | None:
    result = compute_sensitivity(
        primary_label, list(factor_columns), factor_columns, response
    )
    if not result.sufficient or not result.factors:
        return None
    factors = [
        {
            "factor": label_of(f.factor),
            "src": _round(abs(f.std_coef)),
            "sign": "positive" if f.std_coef >= 0 else "negative",
        }
        for f in result.factors
        if f.std_coef is not None
    ]
    if not factors:
        return None
    return {
        "kind": "src_regression",
        "response": primary_label,
        "r2": _round(result.r_squared),
        "factors": factors,
    }


# --- helpers -----------------------------------------------------------------


def _numeric_columns(
    children: list[StudyChildResult], *, attr: str
) -> dict[str, list[float | None]]:
    """Build ``{name: [value-per-child]}`` over numeric values only."""
    names: list[str] = []
    seen: set[str] = set()
    for child in children:
        for key in getattr(child, attr):
            if key not in seen:
                seen.add(key)
                names.append(key)
    columns: dict[str, list[float | None]] = {}
    for name in names:
        column = [_numeric(getattr(child, attr).get(name)) for child in children]
        if any(v is not None for v in column):
            columns[name] = column
    return columns


def _ordered_responses(
    response_columns: Mapping[str, Sequence[float | None]], out_order: list[str]
) -> list[str]:
    """Responses ordered by descriptor declaration, then any extras by name."""
    ordered = [n for n in out_order if n in response_columns]
    extras = sorted(n for n in response_columns if n not in ordered)
    return ordered + extras


def _response_facts(
    name: str,
    column: Sequence[float | None],
    children: list[StudyChildResult],
    varied: list[str],
    maps: DescriptorMaps,
) -> dict[str, Any]:
    stats = summarize(column)
    min_idx = _extreme_index(column, want_max=False)
    max_idx = _extreme_index(column, want_max=True)
    return {
        "label": maps.output_label(name),
        "unit": maps.output_unit(name),
        "min": _round(stats.minimum),
        "max": _round(stats.maximum),
        "mean": _round(stats.mean),
        "std": _round(stats.std),
        "min_run": children[min_idx].job_id if min_idx is not None else None,
        "max_run": children[max_idx].job_id if max_idx is not None else None,
        "min_design": _design(children, min_idx, varied, maps),
        "max_design": _design(children, max_idx, varied, maps),
    }


def _extreme_index(
    column: Sequence[float | None], *, want_max: bool
) -> int | None:
    best_idx: int | None = None
    best_val: float | None = None
    for i, v in enumerate(column):
        if v is None:
            continue
        if best_val is None or (v > best_val if want_max else v < best_val):
            best_val = v
            best_idx = i
    return best_idx


def _design(
    children: list[StudyChildResult],
    idx: int | None,
    varied: list[str],
    maps: DescriptorMaps,
) -> list[dict[str, Any]]:
    """The varied-factor values of one run (its design coordinates)."""
    if idx is None:
        return []
    inputs = children[idx].inputs or {}
    out = []
    for factor in varied[:_TOP_DESIGN_INPUTS]:
        value = _numeric(inputs.get(factor))
        if value is not None:
            out.append(
                {
                    "label": maps.input_label(factor),
                    "value": _round(value),
                    "unit": maps.input_unit(factor),
                }
            )
    return out


def _relabel_term(term: str, label_of: Callable[[str], str]) -> str:
    if " x " in term:
        left, right = term.split(" x ", 1)
        return f"{label_of(left)} x {label_of(right)}"
    return label_of(term)


def _numeric(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _round(value: float | None, sig: int = 4) -> float | None:
    """Round to ``sig`` significant figures (cap-style display rounding)."""
    if value is None or not math.isfinite(value):
        return None
    if value == 0:
        return 0.0
    digits = -math.floor(math.log10(abs(value))) + (sig - 1)
    return round(value, digits)


def _duration(study: ParametricStudy) -> float | None:
    if study.created_at is None or study.state_changed_at is None:
        return None
    return (study.state_changed_at - study.created_at).total_seconds()

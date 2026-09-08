"""Deterministic templated narrative — the always-on, air-gap-safe baseline.

Pure functions, no I/O and no network: given a :class:`SummaryFacts` they
return a :class:`SummaryText` (a title + a few short paragraphs of plain
prose). This is the summary that ships enabled-by-default and the only summary
an offline install ever produces. Because every number originates in the
facts, the templated text is also the ground truth the enrichment guard stack
checks the model against.

Per-surface generators (``render_job``, ``render_workflow``,
``render_parametric``, ``render_optimisation``) are added by the slices that
own each surface; this module owns the output shape and the shared phrasing
helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from matofx.summaries.facts import HeadlineNumber, SummaryFacts

_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_PAIR = 2


@dataclass(frozen=True)
class SummaryText:
    """A rendered narrative: a title plus one to four short paragraphs."""

    title: str
    paragraphs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for JSON serialisation and caching."""
        return {"title": self.title, "paragraphs": list(self.paragraphs)}

    @classmethod
    def from_dict(cls, data: Any) -> SummaryText:
        """Rebuild a :class:`SummaryText` from its :meth:`to_dict` form."""
        return cls(
            title=str(data["title"]),
            paragraphs=tuple(str(p) for p in data["paragraphs"]),
        )


# --- shared phrasing helpers -------------------------------------------------


def format_number(value: float | int | str | None, unit: str | None = None) -> str:
    """Render a headline value to 4 significant figures plus its unit.

    Integers render without a decimal; ``None`` renders "not available";
    strings pass through. A unit, when present, is appended after a space
    (matching the descriptor unit so the enrichment unit-guard can pair them).
    """
    if value is None:
        return "not available"
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        rendered = str(value)
    else:
        rendered = f"{value:.4g}"
    if unit:
        return f"{rendered} {unit}"
    return rendered


def humanise_duration(seconds: float | None) -> str:
    """Render a duration like "4 min 12 s" / "11 min" / "2 h 03 min"."""
    if seconds is None or seconds < 0:
        return "an unknown time"
    total = round(seconds)
    if total < _SECONDS_PER_MINUTE:
        return f"{total} s"
    minutes, secs = divmod(total, _SECONDS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes} min {secs:02d} s" if secs else f"{minutes} min"
    hours, mins = divmod(minutes, _MINUTES_PER_HOUR)
    return f"{hours} h {mins:02d} min" if mins else f"{hours} h"


def join_list(items: list[str]) -> str:
    """Join labels into a natural-language list ("a, b and c")."""
    cleaned = [i for i in items if i]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == _PAIR:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])} and {cleaned[-1]}"


def pluralise(count: int, noun: str) -> str:
    """Render "1 file" / "3 files" / "8 batches" (handles -ch/-sh/-s/-x/-z)."""
    if count == 1:
        return f"{count} {noun}"
    suffix = "es" if noun.endswith(("ch", "sh", "s", "x", "z")) else "s"
    return f"{count} {noun}{suffix}"


def headline_clause(headline: tuple[HeadlineNumber, ...]) -> str:
    """Comma-join the headline figures that have a value ("x 9 °C, y 2 kg")."""
    parts = [
        f"{h.label.lower()} {format_number(h.value, h.unit)}"
        for h in headline
        if h.value is not None
    ]
    return ", ".join(parts)


# --- per-surface generators --------------------------------------------------


def render_job(facts: SummaryFacts) -> SummaryText:
    """Templated narrative for a single job.

    Leads with the outcome + duration; on success adds the headline figures
    and an output tally; on failure leads with the failure and exit code.
    Every number originates in ``facts`` so the guard stack can verify any
    enrichment against this text.
    """
    details = facts.details
    tool = str(details.get("tool_label") or "tool")
    duration = humanise_duration(facts.duration_seconds)
    paragraphs: list[str] = []

    if facts.state == "complete":
        opening = f"The {tool} job completed successfully in {duration}."
        figures = headline_clause(facts.headline)
        if figures:
            opening += f" Headline results: {figures}."
        paragraphs.append(opening)
        tally = _output_tally(details)
        if tally is not None:
            paragraphs.append(tally)
    elif facts.state == "failed":
        exit_code = details.get("exit_code")
        suffix = f" (exit code {exit_code})" if exit_code is not None else ""
        paragraphs.append(
            f"The {tool} job failed after {duration}{suffix}. No headline results "
            "were produced. See the job log for the failure detail."
        )
    elif facts.state == "cancelled":
        paragraphs.append(f"The {tool} job was cancelled after {duration}.")
    else:
        paragraphs.append(f"The {tool} job finished in state {facts.state!r} after {duration}.")

    return SummaryText(title="Summary", paragraphs=tuple(paragraphs))


def _output_tally(details: Any) -> str | None:
    if "output_file_count" not in details:
        return None
    files = int(details["output_file_count"])
    scalars = int(details.get("output_scalar_count", 0))
    return (
        f"It produced {pluralise(files, 'output file')} and {pluralise(scalars, 'scalar result')}."
    )


def render_workflow(facts: SummaryFacts) -> SummaryText:
    """Templated narrative for a workflow run.

    Complete: all steps succeeded, listing the chain. Failed: names the first
    failing step and its position. Cancelled: how far it got. Every count comes
    from ``facts.details``.
    """
    details = facts.details
    name = str(details.get("workflow_name") or facts.title or "workflow")
    duration = humanise_duration(facts.duration_seconds)
    members = list(details.get("members") or [])
    total = int(details.get("member_count", len(members)))
    chain = join_arrow([str(m.get("label", "")) for m in members])
    paragraphs: list[str] = []

    if facts.state == "complete":
        opening = f"Workflow {name} completed: all {pluralise(total, 'step')} succeeded"
        opening += f" ({chain})." if chain else "."
        opening += f" Total time {duration}."
        paragraphs.append(opening)
        final = _final_results_sentence(details)
        if final:
            paragraphs.append(final)
    elif facts.state == "failed":
        position = details.get("first_failed_position")
        failed_label = details.get("first_failed_label")
        if position and failed_label:
            lead = (
                f"Workflow {name} failed: step {position} of {total} "
                f"({failed_label}) did not complete."
            )
        else:
            lead = f"Workflow {name} failed before completing all {total} steps."
        completed = int(details.get("completed_count", 0))
        lead += (
            f" {pluralise(completed, 'step')} completed before the halt; elapsed time {duration}."
        )
        paragraphs.append(lead)
    elif facts.state == "cancelled":
        completed = int(details.get("completed_count", 0))
        paragraphs.append(
            f"Workflow {name} was cancelled after {duration}; "
            f"{completed} of {total} steps had completed."
        )
    else:
        paragraphs.append(f"Workflow {name} finished in state {facts.state!r} after {duration}.")

    return SummaryText(title="Summary", paragraphs=tuple(paragraphs))


def join_arrow(labels: list[str]) -> str:
    """Join ordered step labels with arrows ("A → B → C")."""
    cleaned = [label for label in labels if label]
    return " → ".join(cleaned)


def _final_results_sentence(details: Any) -> str:
    results = list(details.get("final_results") or [])
    if not results:
        return ""
    label = str(details.get("final_step_label") or "final step")
    figures = ", ".join(
        f"{str(r['label']).lower()} {format_number(r['value'], r.get('unit'))}" for r in results
    )
    return f"The final step ({label}) produced {figures}."


def unit_suffix(unit: Any) -> str:
    """Render " <unit>" for a present unit, else "" (for inline number+unit)."""
    return f" {unit}" if unit else ""


_MODE_LABELS = {
    "lhs": "LHS",
    "sobol": "Sobol",
    "ccd": "central-composite",
    "box_behnken": "Box-Behnken",
    "plackett_burman": "Plackett-Burman",
    "frac_factorial": "fractional-factorial",
    "grid": "grid",
    "random": "random",
    "table": "table",
    "list": "list",
}
_TOP_FACTORS = 3


def render_parametric(facts: SummaryFacts) -> SummaryText:
    """Templated narrative for a parametric study.

    Paragraph one states what ran + the primary response's range; paragraph two
    is **chosen by ``analysis.kind``** (design §3.1), so the prose depth scales
    with the DOE/analysis method. Every figure comes from ``facts.details``.
    """
    details = facts.details
    mode = str(details.get("design_mode", ""))
    mode_label = _MODE_LABELS.get(mode, mode or "parametric")
    completed = int(details.get("completed") or 0)
    generated = int(details.get("generated") or 0)
    factors = [str(f) for f in (details.get("factors") or [])]
    responses = list(details.get("responses") or [])

    opening = f"This {mode_label} study ran {completed} of {generated} points"
    if factors:
        opening += f", varying {join_list(factors)}"
    skipped = int(details.get("skipped_invalid") or 0)
    if skipped > 0:
        opening += f" ({pluralise(skipped, 'candidate point')} skipped as failing input validation)"
    opening += "."
    if responses:
        clause = _range_clause(responses[0])
        if clause:
            opening += " " + clause
    paragraphs = [opening]

    analysis_paragraph = _render_parametric_analysis(details.get("analysis") or {})
    if analysis_paragraph:
        paragraphs.append(analysis_paragraph)

    secondary = _secondary_responses_paragraph(responses[1:])
    if secondary:
        paragraphs.append(secondary)

    return SummaryText(title="Summary", paragraphs=tuple(paragraphs))


def _secondary_responses_paragraph(responses: list[Any]) -> str:
    """Range + dominant driver for each non-primary response."""
    clauses = []
    for r in responses:
        label = str(r.get("label", "")).lower()
        minimum, maximum = r.get("min"), r.get("max")
        if not label or minimum is None or maximum is None:
            continue
        suffix = unit_suffix(r.get("unit"))
        clause = f"{label} ranged {format_number(minimum)} to {format_number(maximum)}{suffix}"
        driver = r.get("top_driver")
        if isinstance(driver, dict) and driver.get("label"):
            verb = "raised by" if driver.get("sign") == "positive" else "lowered by"
            clause += f" ({verb} {str(driver['label']).lower()})"
        clauses.append(clause)
    if not clauses:
        return ""
    return "Other recorded responses: " + join_list(clauses) + "."


def _range_clause(response: Any) -> str:
    label = str(response.get("label", "the response"))
    unit = response.get("unit")
    suffix = f" {unit}" if unit else ""
    minimum, maximum = response.get("min"), response.get("max")
    if minimum is None or maximum is None:
        return ""
    clause = f"{label} ranged {format_number(minimum)} to {format_number(maximum)}{suffix}"
    mean = response.get("mean")
    if mean is not None:
        clause += f" (mean {format_number(mean)}{suffix})"
    clause += "."
    min_run, max_run = response.get("min_run"), response.get("max_run")
    if min_run and max_run and min_run != max_run:
        clause += (
            f" The lowest value came from run #{min_run}"
            f"{_design_clause(response.get('min_design'))}, the highest from run "
            f"#{max_run}{_design_clause(response.get('max_design'))}."
        )
    return clause


def _design_clause(design: Any) -> str:
    """Render " (x 12 mm, y 3 mm)" for a run's design coordinates, or ""."""
    coords = [
        f"{str(d['label']).lower()} {format_number(d['value'], d.get('unit'))}"
        for d in (design or [])
    ]
    return f" ({join_list(coords)})" if coords else ""


def _render_parametric_analysis(analysis: Any) -> str:
    dispatch = {
        "src_regression": _src_paragraph,
        "sobol_global": _sobol_paragraph,
        "response_surface": _rsm_paragraph,
        "screening_effects": _screening_paragraph,
        "anova": _anova_paragraph,
        "pareto": _pareto_paragraph,
    }
    handler = dispatch.get(str(analysis.get("kind")))
    return handler(analysis) if handler is not None else ""


def _src_paragraph(analysis: Any) -> str:
    factors = list(analysis.get("factors") or [])
    if not factors:
        return ""
    top = factors[:_TOP_FACTORS]
    parts = [f"{top[0]['factor']} first (|SRC| = {format_number(top[0]['src'])})"]
    parts += [f"{f['factor']} ({format_number(f['src'])})" for f in top[1:]]
    r2 = analysis.get("r2")
    quality = f" (model R-squared {format_number(r2)})" if r2 is not None else ""
    sentence = f"A standardised-regression sensitivity ranking{quality} puts {join_list(parts)}."
    sentence += _direction_clause(top[0], str(analysis.get("response", "the response")))
    return sentence


def _direction_clause(driver: Any, response_label: str) -> str:
    """State the sign of the strongest effect: raising the input raises/lowers it."""
    sign = driver.get("sign")
    if sign not in ("positive", "negative"):
        return ""
    verb = "raises" if sign == "positive" else "lowers"
    return f" Raising {str(driver.get('factor', 'it')).lower()} {verb} {response_label.lower()}."


def _sobol_paragraph(analysis: Any) -> str:
    indices = list(analysis.get("indices") or [])
    if not indices:
        return ""
    top = indices[:_TOP_FACTORS]
    lead = top[0]
    text = (
        f"Global sensitivity indices rank {lead['factor']} as the dominant driver "
        f"(first-order S1 = {format_number(lead['s1'])}, "
        f"total-order St = {format_number(lead['st'])})"
    )
    if len(top) > 1:
        rest = join_list(
            [
                f"{x['factor']} (S1 = {format_number(x['s1'])}, St = {format_number(x['st'])})"
                for x in top[1:]
            ]
        )
        text += f", then {rest}"
    text += "."
    return text


def _rsm_paragraph(analysis: Any) -> str:
    response = str(analysis.get("response", "the response")).lower()
    r2 = analysis.get("r2")
    if r2 is None:
        sentence = "A quadratic response surface was fitted."
    else:
        adj = analysis.get("r2_adj")
        adj_text = f", adjusted {format_number(adj)}" if adj is not None else ""
        sentence = (
            f"A quadratic model fits {response} with R-squared {format_number(r2)}{adj_text}."
        )
    stationary = analysis.get("stationary")
    value = analysis.get("stationary_value")
    if stationary and value is not None:
        unit = analysis.get("unit")
        suffix = f" {unit}" if unit else ""
        coords = join_list([f"{name} {format_number(v)}" for name, v in stationary.items()])
        kind = str(analysis.get("stationary_kind") or "stationary")
        sentence += (
            f" The fitted surface places a {kind} point at {coords}, "
            f"with a predicted {response} of {format_number(value)}{suffix}."
        )
    return sentence


def _screening_paragraph(analysis: Any) -> str:
    effects = list(analysis.get("effects") or [])
    if not effects:
        return ""
    unit = analysis.get("unit")
    suffix = f" {unit}" if unit else ""
    active = effects[:_TOP_FACTORS]
    active_text = join_list(
        [f"{e['factor']} (effect {format_number(e['effect'])}{suffix})" for e in active]
    )
    text = f"The most influential factors are {active_text}"
    remaining = len(effects) - len(active)
    if remaining > 0:
        text += (
            f"; the remaining {pluralise(remaining, 'factor')} show effects below "
            "the noise threshold"
        )
    text += (
        ". As a screening design these are main-effect estimates only and do not "
        "resolve interactions."
    )
    if analysis.get("aliased"):
        text += " Effects may be aliased with interactions in this fractional design."
    return text


def _anova_paragraph(analysis: Any) -> str:
    terms = list(analysis.get("terms") or [])
    if not terms:
        return ""
    top = terms[:_TOP_FACTORS]
    parts = [f"{t['term']} ({format_number(t['contribution'])}%)" for t in top]
    text = "ANOVA attributes the variation mainly to " + join_list(parts)
    residual = analysis.get("residual")
    if residual is not None:
        text += f", with {format_number(residual)}% unexplained"
    return text + "."


def _pareto_paragraph(analysis: Any) -> str:
    size = analysis.get("front_size")
    if not size:
        return ""
    return f"The study surfaces a Pareto front of {pluralise(int(size), 'non-dominated design')}."


_STRATEGY_LABELS = {
    "bo": "Bayesian-optimisation",
    "cmaes": "CMA-ES",
    "de": "differential-evolution",
    "pso": "particle-swarm",
    "ga": "genetic-algorithm",
    "nsga2": "NSGA-II",
    "nsga3": "NSGA-III",
    "moea_d": "MOEA/D",
    "random": "random-search",
    "grid": "grid-search",
}
_RUN_MODE_LABELS = {
    "real": "real",
    "surrogate": "surrogate",
    "surrogate_assisted_real": "surrogate-assisted real",
}
_STOP_REASON_PHRASES = {
    "converged": "convergence",
    "budget_exhausted": "the evaluation budget being reached",
    "target_met": "the target being met",
    "user_halted": "a user halt",
    "grid_exhausted": "the grid being exhausted",
    "space_exhausted": "the design space being fully explored",
    "all_children_failed": "all runs failing",
}
_OPERATOR_WORDS = {
    "<=": "at most",
    "<": "below",
    ">=": "at least",
    ">": "above",
    "==": "equal to",
    "!=": "not equal to",
}


def render_optimisation(facts: SummaryFacts) -> SummaryText:
    """Templated narrative for an optimisation study.

    Four short paragraphs, each surfacing insight rather than just setup: what
    was optimised + how it ran; the **best design** (objective value, whether
    each constraint is binding, the design coordinates) or the **Pareto spread**;
    the **CoP sensitivity** drivers; and how far the search **moved**. Every
    figure comes from ``facts.details``.
    """
    details = facts.details
    raw_strategy = str(details.get("strategy") or "search")
    strategy = _STRATEGY_LABELS.get(raw_strategy, raw_strategy)

    if facts.state == "failed":
        return SummaryText(
            title="Summary",
            paragraphs=(
                f"This {strategy} optimisation study failed before producing a "
                "result; every run failed.",
            ),
        )

    paragraphs = [_optimisation_setup_sentence(facts, strategy)]
    for builder in (
        _optimisation_outcome,
        _optimisation_sensitivity,
        _optimisation_convergence,
    ):
        para = builder(facts)
        if para:
            paragraphs.append(para)
    return SummaryText(title="Summary", paragraphs=tuple(paragraphs))


def _optimisation_setup_sentence(facts: SummaryFacts, strategy: str) -> str:
    details = facts.details
    goal_clause = _goal_clause(list(details.get("objectives") or []))
    constraint_clause = _constraint_clause(list(details.get("constraints") or []))
    evaluations = int(details.get("evaluations") or 0)
    raw_mode = str(details.get("run_mode") or "real")
    run_mode = _RUN_MODE_LABELS.get(raw_mode, raw_mode)

    sentence = f"This {strategy} study {goal_clause}"
    if constraint_clause:
        sentence += f" subject to {constraint_clause}"
    sentence += f", over {pluralise(evaluations, 'evaluation')} in {run_mode} mode"
    if facts.state == "cancelled":
        return sentence + " before being cancelled."
    reason = _STOP_REASON_PHRASES.get(str(details.get("stop_reason")))
    return sentence + (f", stopping on {reason}." if reason else ".")


def _goal_clause(objectives: list[Any]) -> str:
    if not objectives:
        return "ran"
    labels = [str(o.get("label", "the objective")).lower() for o in objectives]
    senses = {o.get("sense") for o in objectives}
    if len(senses) == 1:
        verb = "minimised" if "minimise" in senses else "maximised"
        return f"{verb} {join_list(labels)}"
    return f"optimised {join_list(labels)}"


def _constraint_clause(constraints: list[Any]) -> str:
    parts = []
    for c in constraints:
        label = str(c.get("label", "")).lower()
        word = _OPERATOR_WORDS.get(str(c.get("op")), "")
        limit = c.get("value")
        if not label or not word or limit is None:
            continue
        suffix = unit_suffix(c.get("unit"))
        parts.append(f"{label} {word} {format_number(limit)}{suffix}")
    return join_list(parts)


def _optimisation_outcome(facts: SummaryFacts) -> str:
    details = facts.details
    if details.get("multi_objective"):
        return _pareto_outcome(details)
    return _best_design_outcome(details)


def _best_design_outcome(details: Any) -> str:
    objectives = list(details.get("objectives") or [])
    best = details.get("best_value")
    if not objectives or best is None:
        feas = _feasibility_clause(details)
        return f"{feas[0].upper()}{feas[1:]}." if feas else ""
    obj = objectives[0]
    suffix = unit_suffix(obj.get("unit"))
    sentence = (
        f"The best design reaches {str(obj.get('label', 'the objective')).lower()} "
        f"{format_number(best)}{suffix}"
    )
    activity = _constraint_activity_clause(list(details.get("best_constraints") or []))
    if activity:
        sentence += f", with {activity}"
    sentence += "."

    design = list(details.get("best_design") or [])
    if design:
        coords = join_list(
            [
                f"{str(d['label']).lower()} {format_number(d['value'])}{unit_suffix(d.get('unit'))}"
                for d in design
            ]
        )
        sentence += f" It sits at {coords}."
    feas = _feasibility_clause(details)
    if feas:
        sentence += f" {feas[0].upper()}{feas[1:]}."
    return sentence


def _constraint_activity_clause(best_constraints: list[Any]) -> str:
    clauses = []
    for c in best_constraints:
        label = str(c.get("label", "")).lower()
        value = c.get("value")
        limit = c.get("limit")
        if not label or value is None or limit is None:
            continue
        suffix = unit_suffix(c.get("unit"))
        if c.get("binding"):
            clauses.append(
                f"{label} binding at {format_number(value)}{suffix} "
                f"(limit {format_number(limit)}{suffix})"
            )
        else:
            clauses.append(
                f"{label} comfortably within its {format_number(limit)}{suffix} "
                f"limit at {format_number(value)}{suffix}"
            )
    return join_list(clauses)


def _pareto_outcome(details: Any) -> str:
    size = details.get("pareto_size")
    if not size:
        feas = _feasibility_clause(details)
        return f"{feas[0].upper()}{feas[1:]}." if feas else ""
    sentence = f"The search returned a {int(size)}-point Pareto front"
    ranges = list(details.get("pareto_ranges") or [])
    range_clauses = [
        f"{str(r['label']).lower()} spans {format_number(r['min'])} to "
        f"{format_number(r['max'])}{unit_suffix(r.get('unit'))}"
        for r in ranges
        if r.get("min") is not None and r.get("max") is not None
    ]
    if range_clauses:
        sentence += ": " + join_list(range_clauses)
    sentence += "."
    feas = _feasibility_clause(details)
    if feas:
        sentence += f" {feas[0].upper()}{feas[1:]}."
    return sentence


def _optimisation_sensitivity(facts: SummaryFacts) -> str:
    details = facts.details
    sens = details.get("sensitivity") or {}
    drivers = list(sens.get("drivers") or [])
    frozen = [str(f) for f in (details.get("frozen_factors") or [])]
    if drivers:
        clauses = [f"{str(d['label']).lower()} (CoP {format_number(d['cop'])})" for d in drivers]
        sentence = f"Sensitivity analysis attributes most of the response to {join_list(clauses)}"
        q2 = sens.get("q2")
        if q2 is not None:
            sentence += f" (metamodel quality Q-squared {format_number(q2)})"
        sentence += "."
        if frozen:
            sentence += (
                f" Low-influence factors ({join_list(frozen)}) were frozen before the search."
            )
        return sentence
    if frozen:
        return (
            f"Variable-reduction analysis froze {join_list(frozen)} as low-influence "
            "before searching the reduced space."
        )
    return ""


def _optimisation_convergence(facts: SummaryFacts) -> str:
    conv = facts.details.get("convergence") or {}
    first, best = conv.get("first"), conv.get("best")
    batches = conv.get("batches")
    if first is None or best is None or first == best or not batches:
        return ""
    metric = (
        "the hypervolume" if facts.details.get("multi_objective") else "the best objective value"
    )
    return (
        f"Over {pluralise(int(batches), 'batch')}, {metric} moved from "
        f"{format_number(first)} to {format_number(best)}."
    )


def _feasibility_clause(details: Any) -> str:
    feasible = details.get("n_feasible")
    total = details.get("evaluations")
    if feasible is None or not total:
        return ""
    return f"{feasible} of {total} evaluations were feasible"

"""Plotting a factor that is a *choice* rather than a number.

Every scatter surface in the dashboard reached its axis values through a
numeric coercion that returns ``None`` for a string, so an input with declared
options — a material, a solver, a coolant — failed twice over:

- as the **colour**, ``... or 0.0`` gave every run the value 0, so two
  materials drew as one flat colour on a continuous bar;
- as an **axis**, every point was skipped and the panel announced it had no
  numeric runs to plot — truthful about what it did, wrong about why, and the
  worse failure because an archive full of runs looks empty.

Levels take evenly-spaced positions instead, and the tick carries the level's
*name*. Shared by the optimisation design-space explorer and the parametric
study Explore plots, which hit the same defect independently.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from plotly.colors import sample_colorscale

UNSET_LEVEL = "(not set)"
"""Column for a run with no value for this factor — visible, not silently gone."""

_CATEGORY_JITTER = 6
"""Runs per row of the sideways nudge, when a nudge is warranted at all."""


def axis_categories(declared: Sequence[str], observed: Iterable[Any]) -> list[str]:
    """Ordered tick labels for a categorical axis.

    Declared order comes first — the tool author's order, so ``Low / Medium /
    High`` is not sorted into ``High / Low / Medium`` — and it keeps levels
    **nobody ran**, because an empty column is the point: it says the search
    never tried that option, which is the same coverage story the cohort
    panels tell.

    A level that was run but never declared is appended rather than dropped: a
    run that exists has to be plottable.
    """
    out = [str(level) for level in declared]
    seen = set(out)
    for value in observed:
        text = str(value)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def category_positions(
    categories: Sequence[str], values: Sequence[Any], *, jitter: bool = False
) -> list[float | None]:
    """Level -> tick index; ``None`` for a value with no column of its own.

    ``jitter`` nudges runs of one level sideways, **deterministically**
    (position within the level, not random, so two screenshots of one archive
    match). It is off by default, unlike the cohort coverage strips: those
    exist to show density, while the explorer is for reading *individual* runs
    — hover, Pareto rings, feasibility crosses — and a nudged marker sits
    where it does not belong. It is turned on only when every positional axis
    is categorical, where the cloud would otherwise collapse onto a handful of
    dots and the exact position carries no information anyway.
    """
    index = {level: i for i, level in enumerate(categories)}
    seen: dict[str, int] = {}
    out: list[float | None] = []
    for value in values:
        key = str(value)
        base = index.get(key)
        if base is None:
            out.append(None)
            continue
        if not jitter:
            out.append(float(base))
            continue
        rank = seen.get(key, 0)
        seen[key] = rank + 1
        out.append(base + ((rank % _CATEGORY_JITTER) - (_CATEGORY_JITTER - 1) / 2) / 24.0)
    return out


def category_colours(count: int) -> list[str]:
    """One colour per level, evenly sampled from the scale the plot already uses.

    Sampling Viridis rather than introducing a qualitative palette keeps the
    explorer consistent with its own continuous colouring (batch, objectives)
    and avoids inventing a second set of chart colours for the design system.
    """
    if count <= 0:
        return []
    if count == 1:
        return list(sample_colorscale("Viridis", [0.5]))
    return list(sample_colorscale("Viridis", [i / (count - 1) for i in range(count)]))


def stepped_colorscale(colours: Sequence[str]) -> list[list[Any]]:
    """Flat equal bands, one per level, for a categorical colour axis.

    A continuous ramp over category indices invites the eye to interpolate
    between two materials, which means nothing. Equal bands, each labelled
    with its level, do not.
    """
    total = len(colours)
    scale: list[list[Any]] = []
    for i, colour in enumerate(colours):
        scale.append([i / total, colour])
        scale.append([(i + 1) / total, colour])
    return scale


def category_axis(categories: Sequence[str]) -> dict[str, Any]:
    """Plotly axis settings that put level *names* on the ticks, never indices."""
    return {
        "tickmode": "array",
        "tickvals": list(range(len(categories))),
        "ticktext": list(categories),
        "range": [-0.5, len(categories) - 0.5],
    }

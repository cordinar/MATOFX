"""The coverage panel's words and layout decisions (ADR-0020 §11).

Pure helpers, kept out of the page module so the *wording* — which is where
``feedback_users_are_not_modellers`` actually bites — is unit-testable without
a NiceGUI client.

Every string here is written for an engineer who is not a modeller. No "Q²",
no "VIF = 8.4", no "covering radius": what is thin, by how much, and what
would fix it. Where an axis cannot be fixed by a design point (a snapshot
index, an input held constant), the remedy that *does* apply is named instead
— saying the wrong thing would be worse than saying nothing.
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_VERDICT_CHIPS: dict[str, tuple[str, str]] = {
    "good": ("Well covered", "positive"),
    "thin": ("Patchy", "warning"),
    "poor": ("Poorly covered", "negative"),
    # Deliberately not `negative`: "we cannot tell" and "we can tell, and it
    # is bad" are different answers, and colouring them alike would conflate
    # them exactly as the ADR warns against.
    "insufficient": ("Not enough to tell", "grey-6"),
}

_UNKNOWN_CHIP: tuple[str, str] = ("Not assessed", "grey-6")

_NOT_VARIED_CHIP: tuple[str, str] = ("Not varied", "grey-6")
"""An input held at one value throughout.

**It is not "Well covered", and a green badge saying so is nonsense.** The
engine scores a pinned axis 1.0 because there is nothing about it left to
cover -- a defensible internal number that rendered as the most reassuring
badge on the panel, directly above a sentence explaining that the model cannot
vary the input at all. Coverage was never *assessed* here, so the badge is
grey and says exactly that."""


def row_chip(factor: dict[str, Any]) -> tuple[str, str]:
    """``(label, colour)`` for one per-factor row.

    Wraps :func:`verdict_chip` with the one case a bare verdict cannot
    express: an axis that never varied has no coverage verdict to give, and
    the engine's internal 1.0 must not surface as "Well covered".
    """
    if str(factor.get("measure") or "") == "pinned":
        return _NOT_VARIED_CHIP
    return verdict_chip(str(factor.get("verdict") or ""))


def pair_chip(pair: dict[str, Any]) -> tuple[str, str]:
    """``(label, colour)`` for the *density* half of a pairwise row.

    **The words used to claim something the number does not measure.**
    "Varied together / Rarely varied together / Barely varied together"
    describes how a pair is *arranged*; this score divides the pair's
    covering radius by a fixed 60-run yardstick, so it measures whether there
    are *enough runs*. On a twenty-run Latin hypercube the two readings point
    opposite ways, and the panel showed only the one that read badly —
    reported as "the cohort was made from an LHS of 20 runs, so they should
    have been varied together", with the Runs column saying 20 on every row.

    So this half now says what it measures, and :func:`pair_spread_chip` says
    the other thing.
    """
    verdict = str(pair.get("verdict") or "")
    return {
        "good": ("Enough", "positive"),
        "thin": ("Not many", "warning"),
        "poor": ("Very few", "negative"),
    }.get(verdict, _UNKNOWN_CHIP)


def pair_spread_chip(pair: Mapping[str, Any]) -> tuple[str, str]:
    """``(label, colour)`` for the *arrangement* half of a pairwise row.

    Measured against the best those same runs could have done, so it is the
    half that answers "is this design any good" rather than "is there enough
    of it". A dash where a stored report predates the reading: absent is not
    the same as bad.
    """
    if pair.get("spread_score") is None:
        return ("—", "grey-6")
    return {
        "good": ("Well spread", "positive"),
        "thin": ("Unevenly spread", "warning"),
        "poor": ("Barely spread", "negative"),
    }.get(str(pair.get("spread_verdict") or ""), _UNKNOWN_CHIP)


def pair_spread_header(pairs: Sequence[Mapping[str, Any]]) -> str:
    """The second column's heading, naming the run count it compares against.

    Put in the header rather than the legend so the comparison being made is
    on screen. Where pairs carry different counts — a run missing one input
    can still be placed on a pair that excludes it — the header stays generic
    rather than quoting a number that is wrong for some rows.
    """
    counts = {int(p.get("n_points") or 0) for p in pairs}
    if len(counts) == 1:
        return f"Well spread for {counts.pop()} runs?"
    return "Well spread for the runs there are?"


def pair_abstains(pair: Mapping[str, Any]) -> bool:
    """Whether this row is shown but not allowed to set the traffic light."""
    return not bool(pair.get("discriminating", True))


def verdict_chip(verdict: str) -> tuple[str, str]:
    """``(label, colour)`` for a verdict — plain English, never the raw slug.

    An unrecognised value degrades to "Not assessed" rather than raising: a
    stored report from a newer release must not break an older page.
    """
    return _VERDICT_CHIPS.get(verdict, _UNKNOWN_CHIP)


def headline(report: dict[str, Any]) -> str:
    """The verdict card's sentence, plus where the missing runs went.

    "62 of 100" is never a surprise (§11.2): where the validity gate or the
    user's own filters dropped runs, the card says so in the same breath as
    the verdict rather than leaving the count to be discovered.
    """
    message = str(report.get("message") or "").strip()
    diagnostics = report.get("diagnostics") or {}
    kept = int(diagnostics.get("n_points") or 0)
    dropped = int(diagnostics.get("n_excluded_invalid") or 0) + int(
        diagnostics.get("n_excluded_filtered") or 0
    )
    if not message:
        message = "Coverage hasn't been assessed for these runs."
    if dropped:
        message = f"{message} Measured on {kept} of {kept + dropped} runs."
    # A run that could not be *placed* — a missing value on an input being
    # measured — is a third category, and it has to be said out loud. It is
    # not "excluded" (nothing rejected it) and not "outside the range" (we
    # cannot tell where it is), so neither existing sentence covers it, and a
    # run that silently vanishes from a coverage count is precisely the
    # failure this panel exists to catch.
    unmeasurable = int(diagnostics.get("n_unmeasurable") or 0)
    if unmeasurable:
        runs = "run" if unmeasurable == 1 else "runs"
        message = (
            f"{message} {unmeasurable} {runs} couldn't be included because a value was missing."
        )
    return message


def display_name(factor: dict[str, Any]) -> str:
    """What to call an axis on screen — its label, never its internal name.

    **The single read-side rule** ([[feedback_use_labels_not_names]]). The
    engine resolves the label once, onto the envelope; every surface asks
    here. The ``name`` fallback covers a report written before labels were
    carried, and an ad-hoc cohort whose tool has since been removed.
    """
    return str(factor.get("label") or factor.get("name") or "")


def factor_labels(envelope: list[dict[str, Any]]) -> dict[str, str]:
    """``{internal name: display label}`` for every axis in the envelope.

    For the surfaces that must key data by name (the scatter matrix indexes
    the point dicts; the envelope-override payload is keyed by name) but
    must *show* a label.
    """
    return {str(f.get("name") or ""): display_name(f) for f in envelope}


def factor_row_text(factor: dict[str, Any]) -> str:
    """One line of the per-factor strip, in the measure that suits its kind."""
    name = display_name(factor)
    measure = str(factor.get("measure") or "")
    value = float(factor.get("value") or 0.0)
    missing = [str(v) for v in (factor.get("missing_values") or [])]
    writer = _ROW_WRITERS.get(measure, _continuous_row)
    return writer(name, value, missing, factor)


def _pinned_row(name: str, value: float, missing: list[str], factor: dict[str, Any]) -> str:
    """An axis held at one value — no design point can move it."""
    del value, missing, factor
    return f"{name}: the same in every run, so the model can't vary it."


def _span_row(name: str, value: float, missing: list[str], factor: dict[str, Any]) -> str:
    """A derived axis, measured as the fraction of the declared span trained.

    Speaks in **snapshots**, never seconds: the column is a 0-based snapshot
    index, so the platform has no seconds to quote and must not pretend
    otherwise.
    """
    del missing, factor
    return (
        f"{name}: trained over {_pct(value)} of the snapshots you've asked it "
        f"to cover. A longer run or keeping more snapshots would extend it."
    )


def _lattice_row(name: str, value: float, missing: list[str], factor: dict[str, Any]) -> str:
    """A small integer axis — occupancy, with the gaps named."""
    del factor
    if not missing:
        return f"{name}: every whole-number value in range has been run."
    return (
        f"{name}: {_pct(value)} of the values in range have been run "
        f"— missing {', '.join(missing)}."
    )


def _balance_row(name: str, value: float, missing: list[str], factor: dict[str, Any]) -> str:
    """A categorical axis — the silent failure, so it is named explicitly."""
    del value, factor
    if missing:
        return f"{name}: barely any runs used {', '.join(missing)}."
    return f"{name}: the options are used evenly across the runs."


def _continuous_row(name: str, value: float, missing: list[str], factor: dict[str, Any]) -> str:
    """A continuous (or dense-integer) axis — its biggest untried stretch.

    "98% covered across its range" was a number with no stated meaning, and
    users reasonably read it as "98% of the space is covered" — which is not
    what it says and is far more flattering than the truth. It is a **1-D**
    reading of *this input alone*: the score is ``1 - 2 x`` the largest gap
    between consecutive values, so 98% means the biggest untried stretch is
    1% of the range. Quoting the gap instead makes the sentence self-
    explanatory, and makes plain that it says nothing about the combinations
    — which is where the overall verdict lives, and where the apparent
    contradiction came from.
    """
    del missing
    scale_note = " (measured on a log scale)" if factor.get("scale") == "log" else ""
    gap = max(0.0, (1.0 - max(0.0, min(1.0, value))) / 2.0)
    return (
        f"{name}: values are spread across the simulated range{scale_note} — the "
        f"biggest untried stretch is {_pct(gap)} of it."
    )


_RowWriter = Callable[[str, float, list[str], dict[str, Any]], str]

_ROW_WRITERS: dict[str, _RowWriter] = {
    "pinned": _pinned_row,
    "span": _span_row,
    "lattice_occupancy": _lattice_row,
    "level_balance": _balance_row,
    "covering_radius": _continuous_row,
}


@dataclass(frozen=True, slots=True)
class EnvelopeRow:
    """One row of the envelope form (§11.1)."""

    name: str
    """Internal input name — the key the override payload is sent under."""
    label: str
    """What to show ([[feedback_use_labels_not_names]])."""
    kind: str
    lo: float | None
    hi: float | None
    scale: str
    editable: bool
    note: str
    precision: float | None = None
    """The input's own increment, so the box steps and reads like the tool.

    The bounds start at the observed spread, which is a raw float — an
    envelope row for an input stepping in tenths was offering ``0.5649999``
    as its lowest value. Quoting a bound to more digits than the tool has is
    the same defect as suggesting a run at those digits: it is not a display
    blemish, it is a number nobody could have typed."""


def envelope_rows(envelope: list[dict[str, Any]]) -> list[EnvelopeRow]:
    """The envelope form's rows, the ones the user can answer first.

    The form asks a plain question — *"over what range should this model be
    trusted?"* — so the axes they can actually answer for come first, and the
    read-only ones follow with the reason they are read-only.
    """
    rows = [_envelope_row(f) for f in envelope]
    # Ordered by what the user reads, not by the internal name — an
    # alphabetical-by-name order looks arbitrary once labels are shown.
    return sorted(rows, key=lambda r: (not r.editable, r.label.lower()))


def _envelope_row(factor: dict[str, Any]) -> EnvelopeRow:
    """One factor as a form row, with the note that explains it."""
    name = str(factor.get("name") or "")
    label = display_name(factor)
    precision = _as_float(factor.get("precision"))
    kind = str(factor.get("kind") or "continuous")
    levels = [str(v) for v in (factor.get("levels") or [])]

    if kind == "pinned":
        note = (
            f"Held at {factor.get('pinned_value')!r} in every run — the model can't "
            f"vary it, so it isn't part of the range you're declaring."
        )
    elif kind == "derived":
        note = (
            "Comes from the solver, not the design. Covering more of it needs a "
            "longer run or more snapshots kept, not a new job."
        )
    elif kind == "categorical":
        note = f"Options in scope: {', '.join(levels) if levels else '(none seen)'}."
    elif kind == "integer":
        note = "Whole numbers only — suggestions will never propose a fraction."
    else:
        note = ""

    return EnvelopeRow(
        name=name,
        label=label,
        precision=precision,
        kind=kind,
        # Rounded to what the tool can actually accept, not to what a
        # float happens to hold.
        lo=round_to_precision(_as_float(factor.get("lo")), precision),
        hi=round_to_precision(_as_float(factor.get("hi")), precision),
        scale=str(factor.get("scale") or "linear"),
        # Only a numeric axis has bounds to type. Categorical levels are
        # shown rather than edited: the domain is the tool's, not the
        # user's, and a typed level list would invite typos that read as
        # "this level was never run".
        editable=kind in ("continuous", "integer"),
        note=note,
    )


def pair_row_text(pair: dict[str, Any]) -> str:
    """One line of the pairwise strip (§4.6), in the same shape as a factor row.

    Says what was actually measured -- how much of *this pair's* combinations
    were tried -- rather than quoting a ratio nobody can act on.
    """
    a = str(pair.get("a_label") or pair.get("a") or "")
    b = str(pair.get("b_label") or pair.get("b") or "")
    n = int(pair.get("n_points") or 0)
    # Keyed on the engine's verdict, never on a threshold restated here: the
    # floors live in `CoverageSettings`, and a second copy in the UI would
    # drift the moment either moved.
    if str(pair.get("verdict") or "") == "good":
        return f"{a} vs {b}: varied across each other over {n} runs."
    return (
        f"{a} vs {b}: {n} runs, but they were rarely changed together — most "
        f"combinations of the two have never been tried."
    )


def pairs_note(report: dict[str, Any]) -> str:
    """What the pairwise sweep covered, including what it did not.

    A capped sweep presented as a full one is the kind of silent truncation
    §3's F2 is about.
    """
    examined = int(report.get("n_pairs_examined") or 0)
    total = int(report.get("n_pairs_total") or 0)
    if not examined:
        # Nothing scored is a *result*, and a different one from "all fine".
        # Rendering an empty section left the reader to guess which — the exact
        # confusion this feature exists to remove — so say it, and say how many
        # more runs would change the answer.
        if not total:
            return ""
        floor = int(report.get("min_points_per_pair") or 0)
        need = (
            f" A pair is only scored once at least {floor} runs vary both of its inputs."
            if floor
            else ""
        )
        return (
            f"Not enough runs yet to judge how the inputs combine: none of the {total} "
            f"pairs could be scored.{need} This is not a clean bill of health — it means "
            "the question cannot be answered from these runs."
        )
    base = (
        "Each input can look well covered on its own while two of them were "
        "never really varied together — that is what these rows measure."
    )
    if total > examined:
        return (
            f"{base} Showing the {examined} pairs among the least-covered inputs, "
            f"of {total} in total."
        )
    return f"{base} All {examined} pairs were checked."


def round_to_precision(value: float | None, precision: float | None) -> float | None:
    """``value`` snapped to the input's own increment, or unchanged.

    The same rule the suggestion path applies to a proposed run, because it
    is the same requirement: a number offered to the user has to be one the
    tool would accept.
    """
    if value is None:
        return None
    if precision is None or precision <= 0.0:
        # No stated increment. Round to six significant figures anyway: that
        # clears float noise (54.696048050517625 from a stored 54.696048)
        # without inventing precision the tool never claimed, and it behaves
        # the same at 0.0001 as at 400.
        if value == 0.0 or not math.isfinite(value):
            return value
        digits = 6 - 1 - math.floor(math.log10(abs(value)))
        return float(round(value, digits))
    snapped = round(value / precision) * precision
    return float(round(snapped, decimals_for(precision) + 1))


def scatter_axes(
    envelope: list[dict[str, Any]],
    per_factor: list[dict[str, Any]] | None = None,
    *,
    cap: int,
) -> list[str]:
    """The axes worth drawing, worst-covered first.

    **Which axes get plotted is a decision, and it used to be made by the
    alphabet.** The old rule took the first ``cap`` names in envelope order —
    which is sorted by internal name — so on the demo heat-sink archive it
    drew air_velocity, base_width, fin_count and fin_height, and never
    thermal_conductivity: the one axis the headline named as the thinnest.
    Naming an input as the problem and then refusing to show it is the worst
    of both.

    So the axes are ranked by their own coverage, thinnest first. The scatter
    matrix exists to show *where the holes are*; the axes with the holes are
    the ones that earn the space. Ties fall back to the label so the choice is
    stable between reloads — a matrix that reshuffles under the user is its
    own bug.
    """
    eligible = [
        f
        for f in envelope
        if f.get("actionable", True) and f.get("kind") in ("continuous", "integer")
    ]
    scores = {str(r.get("name")): float(r.get("value") or 0.0) for r in (per_factor or [])}
    ranked = sorted(
        eligible,
        key=lambda f: (scores.get(str(f.get("name")), 1.0), display_name(f).lower()),
    )
    return [str(f.get("name")) for f in ranked[:cap]]


def scatter_pairs(
    envelope: list[dict[str, Any]],
    per_factor: list[dict[str, Any]] | None = None,
    *,
    cap: int,
    per_pair: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str]]:
    """The pairs to plot, worst-covered first.

    **Driven by the pairwise scores when they exist.** Selecting axes and then
    taking every combination of them cannot show the worst *pair* unless both
    its halves happen to rank highly on their own -- and the whole point of
    §4.6 is that a pair can fail while both halves look fine. Ranking pairs
    directly puts the failure on screen; it is also the answer to "why is this
    input not plotted anywhere?", which is now a question the caption answers.

    Falls back to the axis ranking where no pairwise pass ran (a stored report
    from before §4.6), and to every combination of the movable axes below that.
    """
    if per_pair:
        ordered = sorted(per_pair, key=lambda p: float(p.get("coverage_score") or 0.0))
        n_panels = max(1, cap * (cap - 1) // 2)
        return [(str(p.get("a") or ""), str(p.get("b") or "")) for p in ordered[:n_panels]]
    return list(itertools.combinations(scatter_axes(envelope, per_factor, cap=cap), 2))


def _pct(value: float) -> str:
    """A 0..1 score as a whole-number percentage."""
    return f"{round(max(0.0, min(1.0, value)) * 100)}%"


def _as_float(value: Any) -> float | None:
    """``float(value)`` or ``None`` — form bounds may legitimately be absent."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def suggestion_summary(batch: dict[str, Any]) -> str:
    """What the proposed batch is, in one line (§11.5).

    Names the response where one drove the proposal — *"these runs improve
    peak temperature"* is actionable, an unattributed list is not — and stays
    silent about it at cohort preview, where nothing has been fitted and no
    response has been chosen.
    """
    points = batch.get("points") or []
    if not points:
        return "There are no runs to suggest here."
    plural = "" if len(points) == 1 else "s"
    response = str(batch.get("response_name") or "").strip()
    where = f" for {response}" if response else ""
    return (
        f"{len(points)} run{plural} proposed{where}, chosen to sit where the "
        f"existing runs leave the biggest gaps."
    )


def suggestion_columns(points: list[dict[str, Any]]) -> list[str]:
    """Column order for the proposed-runs preview table.

    The inputs that actually **differ** across the batch come first — those
    are what the user is being asked to approve — with the ones every row
    shares after them, since they are context rather than choice.
    """
    if not points:
        return []
    names: list[str] = []
    for point in points:
        for key in point:
            if key not in names:
                names.append(key)
    varied = [n for n in names if len({str(p.get(n)) for p in points}) > 1]
    return varied + [n for n in names if n not in varied]


_SUPPORT_CHIPS: dict[str, tuple[str, str]] = {
    "inside": ("Well supported", "positive"),
    "edge": ("Near the edge", "warning"),
    "outside": ("Outside the trained range", "negative"),
}


def support_chip(status: str) -> tuple[str, str]:
    """``(label, colour)`` for a predict-time support status (ADR-0020 §8).

    Plain English, and phrased as a statement about *this prediction* rather
    than about the model: a user reading it has already got their number and
    needs to know how much to trust it.
    """
    return _SUPPORT_CHIPS.get(status, ("Not assessed", "grey-6"))


def support_note(support: dict[str, Any]) -> str:
    """The one-line explanation under a support badge.

    The engine's ``reason`` names the offending input, which is the part a
    user can act on — "outside the trained range" alone is not something
    anybody can do anything about.
    """
    reason = str(support.get("reason") or "").strip()
    if reason:
        return reason
    return "This model doesn't record what range it was trained over."


def support_view(support: Mapping[str, Any] | None) -> tuple[str, str, str] | None:
    """``(label, colour, note)`` for a support verdict, or ``None`` to draw nothing.

    **The whole badge decision, in one function.** Two surfaces show this --
    the job record of an exported surrogate, and the predicted-field viewer --
    and they receive it in different shapes, so each adapts its payload onto
    this rather than re-deciding. A rule implemented twice drifts; that is
    exactly how the guard came to reject 138 of 143 real training runs.

    ``None`` means **draw nothing**: a version fitted before coverage existed
    has no verdict, and an empty badge invites the reader to assume the blank
    means fine.
    """
    if support is None or not support_is_reportable(support):
        return None
    label, colour = support_chip(str(support.get("status") or ""))
    return label, colour, support_note(dict(support))


def support_is_reportable(support: Mapping[str, Any] | None) -> bool:
    """Whether there is a support verdict worth showing at all.

    A version fitted before coverage existed has none, and an empty badge is
    worse than no badge: it invites the reader to assume the blank means fine.

    An **empty** ``available_statuses`` also counts as nothing to show: the
    model records no envelope, so its ``outside`` is a conservative
    placeholder for "we cannot tell" rather than a measurement, and a red
    "Outside the trained range" would assert something nobody checked. The
    key being *absent* is different — a payload that simply does not carry
    the field (an exported tool's job outputs) keeps its verdict.
    """
    if not support or not support.get("status"):
        return False
    statuses = support.get("available_statuses")
    return not (isinstance(statuses, list | tuple) and len(statuses) == 0)


# --- the two-range per-input table (§4.5) --------------------------------

_DECLARED_CHIPS: dict[str, tuple[str, str]] = {
    "good": ("Well covered", "positive"),
    "thin": ("Partly covered", "warning"),
    "poor": ("Barely covered", "negative"),
}

_NO_DECLARED_RANGE: tuple[str, str] = ("No stated range", "grey-6")
"""The tool declares no bounds for this input, so there is no denominator.

Distinct from "poorly covered": a percentage of an unknown range would be
invented, and inventing one here would be the same overclaim the whole
feature exists to prevent."""


def is_option_axis(factor: Mapping[str, Any]) -> bool:
    """Whether this axis's range is a set of options rather than an interval.

    **``pinned`` is not the test.** A *numeric* input held at one value is
    pinned too, and it still has declared numeric bounds — reading ``pinned``
    as "categorical" threw those away and reported "No stated range" for an
    input with a perfectly good 10-120 range.

    The honest test is whether an option set exists: a varying categorical
    always has one, a pinned ``select`` has one from the descriptor, and a
    pinned number has neither.
    """
    if str(factor.get("kind") or "") == "categorical":
        return True
    return bool(factor.get("declared_levels"))


def levels_text(levels: Sequence[Any]) -> str:
    """The options, listed. Long lists are ugly; a wrong summary is worse."""
    named = [str(level) for level in levels if str(level)]
    return ", ".join(named) if named else "—"


def declared_chip(factor: Mapping[str, Any]) -> tuple[str, str]:
    """``(label, colour)`` for how much of the **tool's** range was explored.

    Phrased differently from the simulated-range chip on purpose. "Well
    covered" against the simulated range means *evenly spread across what you
    ran*; against the declared range it means *you have tried most of what
    this tool accepts*. Same words would invite the two to be read as one
    number, which is exactly the conflation §4.5 exists to undo.

    A **categorical** axis is counted, not measured: an interval verdict has no
    meaning over a set of options, and how many of them were tried is both the
    honest reading and the actionable one.
    """
    if is_option_axis(factor):
        declared = [str(v) for v in (factor.get("declared_levels") or ())]
        if not declared:
            return _NO_DECLARED_RANGE
        observed = {str(v) for v in (factor.get("observed_levels") or ())}
        tried = len(observed & set(declared))
        if tried >= len(declared):
            return (f"All {len(declared)} options tried", "positive")
        return (f"{tried} of {len(declared)} options tried", "warning" if tried > 1 else "negative")
    if factor.get("declared_lo") is None or factor.get("declared_hi") is None:
        return _NO_DECLARED_RANGE
    return _DECLARED_CHIPS.get(str(factor.get("declared_verdict") or ""), _UNKNOWN_CHIP)


def decimals_for(precision: float | None) -> int:
    """Digits to show for an input stepping by ``precision``.

    An input stepping in hundredths is quoted to two places -- never to the
    fifteen that a float carries and the tool does not have.
    """
    if precision is None or precision <= 0.0:
        return 3
    if precision >= 1.0:
        return 0
    return min(6, max(0, -math.floor(math.log10(precision))))


def format_value(value: Any, precision: float | None = None) -> str:
    """One number, at the input's own precision."""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "—"
    return f"{number:,.{decimals_for(precision)}f}"


def format_range(lo: Any, hi: Any, precision: float | None = None) -> str:
    """A low-high pair as one string, or the single value where they coincide."""
    if lo is None and hi is None:
        return "—"
    low, high = format_value(lo, precision), format_value(hi, precision)
    # En dash between thin spaces: the typographic form for a numeric
    # range, and visibly not a minus sign on a value that may be negative.
    return low if low == high else f"{low} – {high}"  # noqa: RUF001


def factor_table_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per input for the two-range table (§4.5).

    **Two questions, side by side, because they have different answers.**
    Every figure the panel used to show was measured against the range the
    runs happened to span, which makes it self-referential: "98% covered" has
    always meant *98% of the range you chose to run*. It cannot say that you
    explored a tenth of what the tool accepts, and on the demo archive
    ``Base temperature rise`` sat at a single value out of a declared 10-120
    while every reading called it fine.
    """
    # The levels live on the envelope, the scores on ``per_factor``; a
    # categorical row needs both. Joined here rather than in the caller so every
    # surface reading these rows sees one answer.
    envelope_by_name = {
        str(f.get("name") or ""): f
        for f in (report.get("envelope") or [])
        if isinstance(f, Mapping)
    }
    rows: list[dict[str, Any]] = []
    for factor in report.get("per_factor") or []:
        precision = factor.get("precision")
        axis = envelope_by_name.get(str(factor.get("name") or "")) or {}
        merged = {
            **dict(factor),
            "declared_levels": list(axis.get("declared_levels") or ()),
            "observed_levels": _observed_levels(axis),
        }
        sim_label, sim_colour = row_chip(dict(factor))
        dec_label, dec_colour = declared_chip(merged)
        categorical = is_option_axis(merged)
        rows.append(
            {
                "name": str(factor.get("name") or ""),
                "label": display_name(dict(factor)),
                # An option set is not an interval: quoting "—" for a range that
                # is simply enumerated elsewhere reads as "unknown", when the
                # options are the range and are perfectly well known.
                "simulated_range": (
                    levels_text(merged["observed_levels"])
                    if categorical
                    else format_range(
                        factor.get("simulated_lo"), factor.get("simulated_hi"), precision
                    )
                ),
                "simulated_chip": sim_label,
                "simulated_colour": sim_colour,
                "declared_range": (
                    levels_text(merged["declared_levels"])
                    if categorical
                    else format_range(
                        factor.get("declared_lo"), factor.get("declared_hi"), precision
                    )
                ),
                "declared_chip": dec_label,
                "declared_colour": dec_colour,
                "precision": precision,
                "factor": merged,
            }
        )
    return rows


def _observed_levels(axis: Mapping[str, Any]) -> list[str]:
    """The levels these runs used — the varying set, or the one pinned value."""
    levels = axis.get("levels")
    if levels:
        return [str(level) for level in levels]
    pinned = axis.get("pinned_value")
    return [str(pinned)] if pinned is not None else []


# --- histogram bins ------------------------------------------------------

_NICE_STEPS: tuple[float, ...] = (1.0, 2.0, 2.5, 5.0, 10.0)


def nice_bins(lo: float, hi: float, n_values: int, *, target: int = 0) -> list[float]:
    """Bin edges a person would have chosen, spanning exactly ``lo``..``hi``.

    Two rules, and they pull against each other. The edges have to land on
    **round numbers** -- 0, 0.5, 1.0 rather than 0.317, 0.794 -- or the axis
    is unreadable. And the histogram has to span exactly the range it claims
    to be about, because the whole point of showing two of them is that one
    covers what was simulated and the other what the tool allows; a bin grid
    that quietly extended past either would misstate both.

    So the *step* is snapped to a nice number and the edges are laid from
    ``lo``, with the last bin closing on ``hi``. Bin count follows
    ``sqrt(n)``, bounded: two bins say nothing and eighty is noise.
    """
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return [lo, lo + 1.0] if math.isfinite(lo) else [0.0, 1.0]

    wanted = target or int(min(40, max(6, round(math.sqrt(max(n_values, 1))))))
    raw_step = (hi - lo) / wanted
    magnitude = 10.0 ** math.floor(math.log10(raw_step)) if raw_step > 0 else 1.0
    step = next(
        (magnitude * s for s in _NICE_STEPS if magnitude * s >= raw_step),
        magnitude * 10.0,
    )

    edges = [lo]
    # Start from the first nice multiple strictly inside the range, so the
    # first and last bins may be short but the span is exactly [lo, hi].
    first = math.ceil(lo / step) * step
    value = first if first > lo else first + step
    while value < hi - step * 1e-9:
        edges.append(round(value, 12))
        value += step
    edges.append(hi)
    return edges


def histogram_counts(values: Sequence[float], edges: Sequence[float]) -> list[int]:
    """How many values fall in each bin; the last bin is closed at the top."""
    counts = [0] * max(0, len(edges) - 1)
    if not counts:
        return counts
    for value in values:
        if not math.isfinite(value) or value < edges[0] or value > edges[-1]:
            continue
        placed = min(
            len(counts) - 1,
            max(0, bisect.bisect_right(list(edges), value) - 1),
        )
        counts[placed] += 1
    return counts


def values_for(points: Sequence[Mapping[str, Any]], name: str) -> list[float]:
    """Every finite numeric value recorded for ``name`` across the runs."""
    out: list[float] = []
    for point in points:
        value = point.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        number = float(value)
        if math.isfinite(number):
            out.append(number)
    return out


# --- the pairwise legend -------------------------------------------------


def pair_legend() -> list[tuple[str, str, str]]:
    """``(label, colour, meaning)`` for every pairwise tag, both columns.

    Outside the table rather than repeated in it: the same sentences against
    fifteen rows is a wall of text, and a tag whose meaning is only
    discoverable by hovering is a tag nobody reads.
    """
    return [
        (
            "Enough",
            "positive",
            "There are as many runs on these two inputs as an evenly spread "
            "60-run design would have — 30 per input, and a pair has two.",
        ),
        (
            "Not many",
            "warning",
            "Fewer runs than that standard. On its own this says nothing about "
            "how they are arranged — a well-designed small cohort lands here.",
        ),
        (
            "Very few",
            "negative",
            "Far fewer than the standard. Whatever the arrangement, a model has "
            "little to learn how these two act together from.",
        ),
        (
            "Well spread",
            "positive",
            "The runs that exist are close to the best arrangement that many "
            "runs could have over this pair.",
        ),
        (
            "Unevenly spread",
            "warning",
            "Better arrangements of the same number of runs exist — some "
            "combinations are clustered and others left empty.",
        ),
        (
            "Barely spread",
            "negative",
            "Typically one input was held near a nominal value while the other "
            "moved. A model can follow the lines that were run and is guessing "
            "between them — and more runs of the same shape will not fix it.",
        ),
        (
            "—",
            "grey-6",
            "This reading was added after the report was saved. Re-open the cohort to measure it.",
        ),
        (
            "Not assessed",
            "grey-6",
            "Too few runs carry a usable value for both inputs to judge this pair at all.",
        ),
    ]


PAIR_COLUMNS_NOTE = (
    "Both columns are measured over the range you have declared for the two "
    "inputs — they differ only in what they are compared against. Enough runs? "
    "is against an evenly spread 60-run design. Well spread? is against the "
    "best your own runs could manage. A small cohort of a good design reads "
    "“not many runs” and “well spread”, which together mean: the design is "
    "fine, there is just not much of it yet."
)
"""The sentence above the pair table.

Without it the two columns invite the reader to work out the difference
themselves, and the most natural wrong guess -- that one covers the range the
runs span and the other the range the tool allows -- is a distinction the
panel really does make elsewhere, under *Fill across*."""


def pair_abstain_note(pairs: Sequence[Mapping[str, Any]], *, floor: int) -> str:
    """Why some rows are shown without counting, or "" when they all count.

    A row that is displayed and silently ignored is worse than one that is
    absent: the reader has no way to know the verdict did not consider it.

    **Quotes the actual counts.** Without them this sat next to a row tagged
    "Enough" on a 56-run cohort and read as a contradiction — two different
    senses of enough, one about the 60-run yardstick and one about whether
    the reading can order two designs at all.
    """
    abstaining = [p for p in pairs if pair_abstains(p)]
    if not abstaining or not floor:
        return ""
    counts = sorted({int(p.get("n_points") or 0) for p in abstaining})
    have = str(counts[0]) if len(counts) == 1 else f"{counts[0]} to {counts[-1]}"
    if len(abstaining) == len(pairs):
        return (
            f"The pairwise check is not setting the verdict here. It needs about "
            f"{floor} runs on a pair before it can tell a well-arranged design from "
            f"a badly arranged one, and these have {have}. The columns above are "
            f"still what they say they are — they just cannot be leaned on yet."
        )
    return (
        f"{len(abstaining)} of these pairs are not setting the verdict: the check needs "
        f"about {floor} runs on a pair to order a good design against a bad one, and "
        f"they have {have}."
    )


__all__ = [
    "EnvelopeRow",
    "decimals_for",
    "declared_chip",
    "display_name",
    "envelope_rows",
    "factor_labels",
    "factor_row_text",
    "factor_table_rows",
    "format_range",
    "format_value",
    "headline",
    "histogram_counts",
    "nice_bins",
    "pair_chip",
    "pair_legend",
    "pair_row_text",
    "pairs_note",
    "row_chip",
    "scatter_axes",
    "scatter_pairs",
    "suggestion_columns",
    "suggestion_summary",
    "support_chip",
    "support_is_reportable",
    "support_note",
    "support_view",
    "values_for",
    "verdict_chip",
]


# --- what a POD mode is, in one place ------------------------------------

MODES_EXPLAINER: str = (
    "The model does not learn the field point by point. It first finds the handful of "
    "patterns the field actually varies in across your runs — a mode is one such pattern "
    "— then learns how much of each pattern a given design needs. Predicting is adding "
    "those patterns back together."
)
"""Plain-English answer to "what is a mode?", shown wherever modes are.

Two bar charts labelled "POD mode" mean nothing to a reader who has not met
proper-orthogonal decomposition, and the count is one of the more informative
numbers a field fit produces — so leaving it unexplained wasted it. Plain text,
not markdown: these panels render with ``ui.label``."""


def modes_count_note(modes: int | None, energy: float | None = None) -> str:
    """What *this* fit's mode count says about the archive it was built on.

    The count is a property of the **data**, not a setting: modes are retained
    until they account for the required share of the variance, so it reports how
    many independent ways the field moved across the runs. One mode means every
    field was the same shape scaled up and down; more means the shape itself
    changed. That makes it a coverage signal as much as a fit statistic.
    """
    if not modes:
        return ""
    if modes == 1:
        share = f" It accounts for {energy * 100:.0f}% of the variation." if energy else ""
        return (
            "One pattern was enough, so across these runs the field kept essentially the "
            "same shape and changed only in magnitude. A wider spread of runs usually "
            "needs more." + share
        )
    share = f" Together they account for {energy * 100:.0f}% of the variation." if energy else ""
    return (
        f"{modes} patterns were needed, so the field changes shape across these runs, not "
        f"just magnitude — each is an independent way it moved.{share}"
    )


# --- categorical axes in the pairwise matrix ------------------------------

_JITTER_SPREAD = 9
"""How many distinct offsets a strip column cycles through."""


def strip_positions(levels: Sequence[str], values: Sequence[Any]) -> list[float | None]:
    """Category index per value, nudged sideways so overlapping runs separate.

    A strip plot without a nudge draws every run of one level at the same x,
    so twenty runs and two are the same single dot — and "twenty magnesium
    runs all at one fin count" is exactly the thing worth seeing.

    The offset is **deterministic** (position within the level, not random) so
    the same archive draws the same picture twice; a screenshot taken today and
    one taken in October have to match.
    """
    index = {level: i for i, level in enumerate(levels)}
    seen: dict[str, int] = {}
    out: list[float | None] = []
    for value in values:
        key = str(value)
        base = index.get(key)
        if base is None:
            out.append(None)
            continue
        rank = seen.get(key, 0)
        seen[key] = rank + 1
        offset = ((rank % _JITTER_SPREAD) - (_JITTER_SPREAD - 1) / 2) / 24.0
        out.append(base + offset)
    return out


def level_counts(
    points: Sequence[Mapping[str, Any]],
    x_levels: Sequence[str],
    y_levels: Sequence[str],
    x_name: str,
    y_name: str,
) -> list[list[int]]:
    """Runs per level combination, as ``z[y][x]`` for a heatmap.

    Two categoricals have no distance between them, so the honest picture is a
    tally: a zero cell is a combination nobody has ever run, which is the
    categorical form of the empty corner the numeric panels look for.
    """
    grid = [[0 for _ in x_levels] for _ in y_levels]
    xi = {level: i for i, level in enumerate(x_levels)}
    yi = {level: i for i, level in enumerate(y_levels)}
    for point in points:
        col = xi.get(str(point.get(x_name)))
        row = yi.get(str(point.get(y_name)))
        if col is not None and row is not None:
            grid[row][col] += 1
    return grid


def grid_axes(envelope: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every axis a run carries a value for — the plots' one axis list.

    One definition, because there are two matrices — the cohort's coverage
    grid and the fill-preview's existing-against-proposed grid — and they had
    drifted: the second still filtered to ``continuous`` / ``integer``, so a
    material appeared in the first and not the second.

    **A pinned axis is included whether it holds a number or an option.** The
    rule used to admit a pinned *choice* and refuse a pinned *number*, on the
    grounds that a number held at one value draws a single meaningless tick
    while "every run used aluminium and magnesium has never been tried" is
    the most extreme imbalance there is. Fair when the only pinned axes were
    ones the *archive* held constant — and wrong the moment §11.5's tick list
    let a user pin a number themselves, because it produced exactly the
    backwards result that was reported: untick Fin height and it vanishes
    from both plots, while Plate material, left unticked, stays in both.

    The input you have just pinned is the one whose spike you most need to
    see; it is what the "held at" control exists to disclose. So nothing that
    a proposed run carries a value for is hidden, and the cost is accepted: a
    pinned axis draws a flat histogram and a straight line in the matrix,
    which is a true picture of what the batch does to it.

    ``derived`` is still excluded — a snapshot index is not a design input,
    and no run can be placed at one.
    """
    return [
        str(factor.get("name"))
        for factor in envelope
        if factor.get("kind") in ("continuous", "integer", "categorical", "pinned")
    ]


def with_pinned_values(
    points: Sequence[Mapping[str, Any]], envelope: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Fill each point's held-constant axes back in, for plotting only.

    ``resolve_points`` builds a row from the factors that actually **vary**; an
    input the archive held constant is carried once, on the envelope, as
    ``pinned_value``. So a pinned material is missing from every point, and a
    strip drawn from those points lands nothing on its column — the axis
    appears and the runs do not.

    Rather than widen the point rows (they feed the metrics, the suggestions
    and the GP conditioning, none of which should change because a chart wants
    a value), the constant is put back here, at the edge, for display.
    """
    pinned = {
        str(f.get("name")): f.get("pinned_value")
        for f in envelope
        if f.get("pinned_value") is not None and f.get("name")
    }
    if not pinned:
        return [dict(point) for point in points]
    return [{**pinned, **dict(point)} for point in points]


def axis_levels(envelope: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """``{name: [level, …]}`` for every categorical axis in the envelope.

    Declared levels where the descriptor gave them, so a level nobody ran still
    gets a column — an empty column is the point.

    Keyed on :func:`is_option_axis`, not on ``kind == "categorical"``. A choice
    that never varied is recorded as ``pinned``, and testing the kind gave it
    no levels at all — so the grid tried to plot a material on a number line
    and drew nothing. A pinned *number* still returns nothing here, which is
    correct: it has bounds, not levels.
    """
    out: dict[str, list[str]] = {}
    for factor in envelope:
        if not is_option_axis(factor):
            continue
        declared = [str(v) for v in (factor.get("declared_levels") or []) if str(v)]
        observed = [str(v) for v in (factor.get("levels") or []) if str(v)]
        levels = declared or observed
        if levels:
            out[str(factor.get("name") or "")] = levels
    return out


# --- the batch-size table's own wording (§11.5) ----------------------------


def plan_today_line(plan: Mapping[str, Any]) -> str:
    """What "Today" says beside the current verdict.

    A space made only of choices has **no numeric geometry**, so its covering
    radius is ``0.0`` and the usual sentence reads "biggest untried gap is 0%
    of the space" — the most flattering thing the table could say about an
    archive that has never run half its options. The shortfall count is the
    figure that means something there, so that is the one quoted.
    """
    if int(plan.get("measured_axes") or 0) > 0:
        return f"biggest untried gap is {format_percent(plan.get('current_gap'))} of the space."
    short = int(plan.get("current_levels_short") or 0)
    if not short:
        return "every option has been run enough times to learn from."
    return f"{short} option{'' if short == 1 else 's'} still have too few runs to learn from."


def plan_gap_note(plan: Mapping[str, Any]) -> str:
    """The paragraph explaining what the gap column is — or that it is silent.

    The bare percentage meant nothing on its own: "29% of the space" reads
    like "29% is covered", which is both wrong and far more flattering.
    """
    if int(plan.get("measured_axes") or 0) > 0:
        return (
            "The gap is how far the emptiest spot in the design space sits from its "
            "nearest run, as a share of the whole space. 0% would mean every "
            "combination has a run close to it; a large figure means somewhere in "
            "the space a model would be predicting with nothing nearby to learn from."
        )
    return (
        "The inputs being explored here only take a fixed set of options, so there "
        "is no distance to measure and the gap column stays at 0% however many runs "
        "you add. What these runs buy is shown by the option counts instead: how "
        "many options still have too few runs to learn from."
    )


def unmovable_note(labels: Sequence[str]) -> str:
    """Why no rung reaches *well covered* when inputs were left out.

    The default reading keeps reporting an unticked input's gaps, which is
    the honest thing to do — but it means the batch is being marked against a
    space it cannot reach into, so every row can read "Patchy" and the gap
    can sit still at every size. Without this the table looks broken rather
    than truthful, and a bigger batch is the obvious wrong conclusion.
    """
    if not labels:
        return ""
    named = ", ".join(labels[:-1]) + f" and {labels[-1]}" if len(labels) > 1 else labels[0]
    return (
        f"These figures still cover {named}, which this batch will not move — you "
        f"unticked {'them' if len(labels) > 1 else 'it'} above. Their gaps cannot "
        f"close at any batch size, so the rows below may barely improve and none of "
        f"them may reach well covered. Switch Coverage figures describe to \u201cOnly "
        f"the ones I ticked\u201d to read what this batch does buy."
    )


def format_percent(value: Any) -> str:
    """A 0..1 fraction as a whole-number percentage."""
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return "0%"
    return f"{round(max(0.0, min(1.0, number)) * 100)}%"


def fill_scope_caveats(*, has_options: bool, promoted: Sequence[str]) -> str:
    """The two things *Fill across* does not govern, said rather than implied.

    Both are right behaviours with a label that overclaims, and both surface
    as a proposed run somebody did not expect to see.

    An **option-valued** input always spans the tool's whole option list:
    ``_levels`` prefers the descriptor's options to the observed ones on
    purpose, because a level nobody has run must show as a zero count rather
    than vanish. So "stay inside the range these runs already cover" will
    still propose the material nobody has ever tried.

    A **promoted** held-constant input has no observed range to stay inside,
    so it can only sweep the tool's own — whichever scope is chosen.
    """
    parts: list[str] = []
    if has_options:
        parts.append(
            "Inputs that take a fixed set of options always span every option the "
            "tool offers, whichever range you pick here — an option nobody has run "
            "is a gap, not something outside the range."
        )
    if promoted:
        named = (
            ", ".join(promoted[:-1]) + f" and {promoted[-1]}" if len(promoted) > 1 else promoted[0]
        )
        parts.append(
            f"{named} had the same value in every run, so there is no covered range "
            f"to stay inside: the batch sweeps the tool's own range for "
            f"{'them' if len(promoted) > 1 else 'it'} either way."
        )
    return " ".join(parts)


def measured_over_note(*, widened: bool, narrowed: bool) -> str:
    """What space the table's "Today" figure was taken over.

    It is measured over the space the *figures* describe, while the verdict
    badge at the top of the panel is measured over the panel's own report.
    Under a widened scope the two routinely disagree, and a reader comparing
    them with nothing to explain the difference concludes one of them is
    wrong.
    """
    if narrowed:
        return "Measured over the inputs you ticked, not the whole archive."
    if widened:
        return (
            "Measured over the full range the tool allows, so these read worse "
            "than the verdict at the top of the panel, which covers only the "
            "range your runs actually span."
        )
    return ""


_ARRANGEMENT_OPTIMAL = 0.9
_ARRANGEMENT_FAIR = 0.7
"""Where the arrangement sentence changes its wording.

Not verdict floors, and deliberately not reusing them: this sentence never
sets a light, it only decides how much the reader should trust the spread of
what they already have."""


def arrangement_note(report: Mapping[str, Any]) -> str:
    """Whether the runs are well arranged, as distinct from plentiful.

    ``efficiency_score`` measures the covering radius against what the
    archive's *own* run count could achieve, where ``coverage_score``
    measures it against a fixed 30-per-input standard. So the verdict answers
    "have you enough runs" while this answers "are the runs you have arranged
    well" — and with only the first on screen, a twenty-run Latin hypercube
    read "Patchy" with nothing anywhere to say its arrangement was optimal.

    **Silent below the density where that comparison means anything**, and
    the first version was not. Measured across the same seven design families
    at 5 runs per input, the margin between the worst good design and the best
    bad one runs from -0.05 to +0.06; it only separates from about 20 per
    input. Worse, ``efficiency_score`` is taken over the *declared* envelope,
    which defaults to the range the runs happen to span — so a tight cluster
    is measured over its own bounding box and scored **0.957, higher than the
    clean hypercube's 0.902**, and was told "the runs themselves are well
    arranged; what is missing is runs, not a better design". The exact
    opposite of the truth, phrased as advice, on the archive least able to
    survive it.

    **And it no longer says what to do about it.** "What is missing is runs,
    not a better design" is a causal claim, and this measure cannot carry one
    even where it discriminates: it compares against the best arrangement of
    that many runs, which says nothing about whether more runs would help.
    It reports; the reader decides.
    """
    if not report.get("density_discriminating", True):
        return ""
    try:
        efficiency = float(report.get("efficiency_score") or 0.0)
    except (TypeError, ValueError):
        return ""
    verdict = str(report.get("verdict") or "")
    if verdict in ("good", "insufficient") or not efficiency:
        return ""
    n = int((report.get("diagnostics") or {}).get("n_points") or 0)
    runs = f"{n} runs" if n else "these runs"
    if efficiency >= _ARRANGEMENT_OPTIMAL:
        return (
            f"Separately from the verdict: the runs are about as well spread as {runs} "
            f"could be. What is thin here is the number of them, not their placement."
        )
    if efficiency >= _ARRANGEMENT_FAIR:
        return (
            f"Separately from the verdict: the runs are reasonably well placed for "
            f"{runs} — a better-spread set of the same size would not gain a great deal."
        )
    return (
        f"Separately from the verdict: a better-spread set of {runs} would cover "
        f"noticeably more than this one does, so the placement is part of the problem "
        f"and not only the count."
    )


_SPACE_FILLING_LABELS: dict[str, str] = {
    "lhs": "a Latin hypercube study",
    "sobol": "a Sobol sequence study",
    "random": "a random-sampling study",
}
"""The sampling modes that are space-filling **by construction**.

Named rather than tested for, because the claim below is only defensible for
these three: a grid, a run table or a screening design has a shape the reader
should judge for themselves, and a value list is whatever somebody typed.
"""


def provenance_note(report: Mapping[str, Any]) -> str:
    """What is known about how these runs were chosen, when measuring cannot say.

    **The only honest answer to "is this design any good" on a small cohort.**
    Below about twenty runs per input every geometric reading here stops
    separating a Latin hypercube from a cluster, so the panel cannot measure
    that an archive is well arranged. Where every run came from a
    space-filling study it does not need to: that is a fact about how the runs
    were commissioned, and it holds at any size.

    Shown only where it adds something — when the density gate has silenced
    the measured reading, and only for modes that are space-filling by
    construction. Says nothing at all when provenance is unknown, because an
    empty list means "we cannot establish it", never "it was not designed".
    """
    modes = [str(m) for m in (report.get("sampling_modes") or ())]
    if not modes or report.get("density_discriminating", True):
        return ""
    # Nothing to reassure about once the figures have cleared the bar on
    # their own — "what they are short of is runs" would be false there.
    if str(report.get("verdict") or "") in ("good", "insufficient"):
        return ""
    if not all(m in _SPACE_FILLING_LABELS for m in modes):
        return ""
    n = int((report.get("diagnostics") or {}).get("n_points") or 0)
    runs = f"These {n} runs" if n else "These runs"
    if len(modes) == 1:
        source = _SPACE_FILLING_LABELS[modes[0]]
        return (
            f"{runs} all came from {source}, so they are space-filling by construction. "
            f"What the figures above are short of is runs, not arrangement."
        )
    named = ", ".join(_SPACE_FILLING_LABELS[m] for m in modes[:-1])
    return (
        f"{runs} all came from space-filling studies ({named} and "
        f"{_SPACE_FILLING_LABELS[modes[-1]]}), so their placement is by construction. "
        f"What the figures above are short of is runs, not arrangement."
    )

"""The density blind spot: what it gates, and what it deliberately does not.

Two blind spots, and they are opposite in character. **Width** — above six
varying inputs — is *flattering*: a cluster scores 0.79 and reads ``good``, so
the joint figure stops setting the light there. **Density** — few runs per
input — is *conservative*: at six inputs or fewer, best-bad plateaus in the
0.50-0.65 band, so a hypercube and a cluster both read ``thin`` and nothing
ever overclaims.

That difference decides the treatment. Standing the verdict down for density
would replace a true statement ("this archive is thin") with a grey shrug. So
the verdict is untouched, and what gets gated is any claim about *arrangement*
— which at low density is unsupportable in either direction.
"""

from __future__ import annotations

import numpy as np

from matofx.config import CoverageSettings
from matofx.studies.coverage import FactorOverride, analyse_design, build_envelope, resolve_points
from matofx.studies.coverage.report import DesignCoverage
from matofx.ui._coverage_panel import arrangement_note, provenance_note
from tests.studies._coverage_fixtures import descriptor, num, training

_D = 4
_NAMES = [f"x{j}" for j in range(_D)]
_SPEC = descriptor(*[num(n, 0.0, 10.0) for n in _NAMES])


def _report(u: np.ndarray, *, modes: tuple[str, ...] = ()) -> dict:
    rows = [{_NAMES[j]: 10.0 * u[i, j] for j in range(_D)} for i in range(u.shape[0])]
    points = resolve_points(training(rows), descriptor=_SPEC)
    envelope = build_envelope(
        points,
        descriptor=_SPEC,
        overrides={n: FactorOverride(lo=0.0, hi=10.0) for n in _NAMES},
    )
    design = analyse_design(points, envelope, settings=CoverageSettings(), sampling_modes=modes)
    return design.to_json_obj()


def _lhs(n: int, seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.column_stack([(rng.permutation(n) + rng.random(n)) / n for _ in range(_D)])


def _cluster(n: int, seed: int = 11) -> np.ndarray:
    return np.random.default_rng(seed).random((n, _D)) * 0.15 + 0.4


# --- the verdict is deliberately untouched ---------------------------------


def test_a_thin_archive_still_reads_thin_rather_than_unmeasurable() -> None:
    """The finding is true and must not be thrown away. Twenty runs over four
    inputs genuinely is patchy; the figure simply cannot also say whether the
    design is any good."""
    assert _report(_lhs(20))["verdict"] == "thin"


def test_a_bad_design_never_reads_good_at_low_density() -> None:
    """Why the verdict is safe to leave alone: the blind spot is conservative.
    Nothing here overclaims, so nothing needs standing down."""
    for n in (20, 40, 60):
        assert _report(_cluster(n))["verdict"] != "good"


# --- what it does gate: any claim about arrangement ------------------------


def test_no_arrangement_claim_is_made_at_low_density() -> None:
    """A tight cluster scored **0.957** on efficiency — higher than the clean
    hypercube's 0.902 — because efficiency is measured over the range the runs
    happen to span, which for a cluster is its own bounding box. It was told
    "the runs themselves are well arranged; what is missing is runs, not a
    better design": the exact opposite of the truth, phrased as advice.
    """
    assert arrangement_note(_report(_cluster(20))) == ""
    assert arrangement_note(_report(_lhs(20))) == ""


def test_the_arrangement_claim_returns_once_it_can_discriminate() -> None:
    """At twenty runs per input the margin is real, and a badly-placed set is
    told so."""
    note = arrangement_note(_report(_cluster(80)))
    assert note, "above the density floor the reading is worth making"
    assert "better-spread set" in note


def test_the_arrangement_claim_no_longer_says_what_to_do() -> None:
    """ "What is missing is runs, not a better design" is a causal claim, and
    this measure cannot carry one: it compares against the best arrangement of
    that many runs, which says nothing about whether more would help."""
    note = arrangement_note(_report(_cluster(80)))
    assert "not a better design" not in note


# --- the caveat that replaces it -------------------------------------------


def _codes(report: dict) -> set[str]:
    return {w["code"] for w in report["warnings"]}


def test_a_low_density_archive_is_told_why_the_figure_is_limited() -> None:
    report = _report(_lhs(20))
    assert "low_density" in _codes(report)
    (message,) = [w["message"] for w in report["warnings"] if w["code"] == "low_density"]
    assert "20 runs across 4 varying inputs" in message
    assert "around 80" in message, "the reader is told what would fix it"
    assert "never reads better than this for a bad design" in message, (
        "and that nothing is being hidden — the blind spot is conservative"
    )


def test_no_caveat_once_the_figure_has_cleared_the_bar() -> None:
    """The blind spot is that a good design cannot be *praised* at low
    density. It cannot make a bad one read good, so a `good` verdict is sound
    however few runs earned it — warning there would undermine it."""
    report = _report(_lhs(60))
    assert report["verdict"] == "good"
    assert "low_density" not in _codes(report)


# --- provenance, the one thing that still answers the question -------------


def test_a_small_hypercube_cohort_is_told_its_runs_are_space_filling() -> None:
    """No geometric reading can establish this at twenty runs. Provenance can:
    a cohort whose runs all came from an LHS study is space-filling by
    construction, whatever twenty points look like to a covering radius."""
    note = provenance_note(_report(_lhs(20), modes=("lhs",)))
    assert "Latin hypercube study" in note
    assert "short of is runs, not arrangement" in note


def test_nothing_is_claimed_when_the_provenance_is_mixed() -> None:
    """A cohort of study runs and ad-hoc jobs is not a designed set, and the
    service reports no modes at all for it. Empty means "cannot establish",
    never "was not designed"."""
    assert provenance_note(_report(_lhs(20), modes=())) == ""


def test_nothing_is_claimed_for_a_mode_that_is_not_space_filling() -> None:
    """A grid or a run table has a shape the reader should judge themselves."""
    assert provenance_note(_report(_lhs(20), modes=("grid",))) == ""
    assert provenance_note(_report(_lhs(20), modes=("lhs", "table"))) == ""


def test_provenance_is_silent_where_the_measurement_can_speak() -> None:
    """It exists to fill the gap the density floor leaves, not to duplicate a
    reading that works."""
    assert provenance_note(_report(_lhs(80), modes=("lhs",))) == ""


def test_provenance_is_silent_once_the_verdict_is_good() -> None:
    """ "What the figures are short of is runs" is false when they are short of
    nothing."""
    report = _report(_lhs(60), modes=("lhs",))
    assert report["verdict"] == "good"
    assert provenance_note(report) == ""


def test_the_modes_survive_a_round_trip() -> None:
    raw = _report(_lhs(20), modes=("lhs",))
    assert DesignCoverage.from_json_obj(raw).sampling_modes == ("lhs",)
    assert DesignCoverage.from_json_obj({}).sampling_modes == ()

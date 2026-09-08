"""A repeated integer value is not a repeated design point (2026-09-03).

Reported from the UI: choosing *the range these runs already cover* and asking
how many runs were needed answered

    "Every value of fin_count inside the range you set has already been run,
    so there is nothing new to suggest on that input."

Three things wrong with that, in increasing order of seriousness.

It used the **internal name**, not the descriptor label. It **singled out one
input** as though the other three had no room left either. And underneath, it
was not true: the candidate filter treated a point's small-integer coordinates
as the point's *identity*, so a candidate was thrown away for repeating a fin
count even when its fin height, base thickness and fan speed were completely
different. With seven attainable fin counts all already run, that rejected
**every** candidate and the panel proposed nothing at all.

The maintainer's question — *"what would happen if the cohort range was over
two distinct clusters? We would need to fill in the space in between"* — is
exactly the case the filter broke: the gap between two clusters is reachable
only by re-using integer values that already exist somewhere in the archive.
"""

from __future__ import annotations

from matofx.config import CoverageSettings
from matofx.studies.coverage.envelope import Envelope, FactorEnvelope
from matofx.studies.coverage.gp import suggest_design_points
from matofx.studies.coverage.points import PointSet


def _points(rows: list[dict]) -> PointSet:
    return PointSet(
        rows=tuple(rows),
        factor_names=tuple(rows[0]),
        numeric_names=frozenset(rows[0]),
    )


def _mixed_envelope() -> Envelope:
    """One small integer axis and one continuous — the demo's shape."""
    return Envelope(
        factors=(
            FactorEnvelope(name="fin_count", kind="integer", lo=18.0, hi=24.0, label="Fin count"),
            FactorEnvelope(
                name="fin_height", kind="continuous", lo=15.0, hi=25.0, label="Fin height"
            ),
        )
    )


def _integer_only_envelope() -> Envelope:
    return Envelope(
        factors=(
            FactorEnvelope(name="a", kind="integer", lo=1.0, hi=3.0, label="A"),
            FactorEnvelope(name="b", kind="integer", lo=1.0, hi=3.0, label="B"),
        )
    )


def test_an_exhausted_integer_axis_does_not_block_the_batch() -> None:
    """The reported symptom. Every fin count in range has been run, but the
    continuous axes have plenty of room, so there is plenty to propose."""
    rows = [{"fin_count": float(18 + i % 7), "fin_height": 15.0 + i * 0.1} for i in range(20)]
    batch = suggest_design_points(
        _points(rows), _mixed_envelope(), n=10, seed=0, settings=CoverageSettings()
    )

    assert len(batch.points) == 10
    assert not batch.warnings


def test_the_space_between_two_clusters_can_be_filled() -> None:
    """The maintainer's question. Runs at one end and the other, nothing in the
    middle — and reaching the middle means re-using integer values that already
    exist at the edges."""
    rows = [{"fin_count": 18.0, "fin_height": 15.0 + i * 0.1} for i in range(10)]
    rows += [{"fin_count": 24.0, "fin_height": 24.0 + i * 0.1} for i in range(10)]

    batch = suggest_design_points(
        _points(rows), _mixed_envelope(), n=12, seed=0, settings=CoverageSettings()
    )
    middles = [p for p in batch.points if 17.0 < float(p["fin_height"]) < 23.0]
    assert middles, "nothing was proposed between the two clusters"


def test_an_all_integer_design_still_refuses_duplicates() -> None:
    """The original rule, kept. With no continuous axis the lattice key **is**
    the point, so re-proposing one is proposing a run that already exists.
    """
    rows = [{"a": float(a), "b": float(b)} for a in (1, 2, 3) for b in (1, 2, 3)]
    batch = suggest_design_points(
        _points(rows), _integer_only_envelope(), n=5, seed=0, settings=CoverageSettings()
    )

    assert not batch.points
    assert [w.code for w in batch.warnings] == ["lattice_exhausted"]


def test_the_exhausted_message_uses_the_label() -> None:
    """[[feedback_use_labels_not_names]] — reported as "you used name not
    label (again!!!)"."""
    rows = [{"a": float(a), "b": float(b)} for a in (1, 2, 3) for b in (1, 2, 3)]
    batch = suggest_design_points(
        _points(rows), _integer_only_envelope(), n=5, seed=0, settings=CoverageSettings()
    )
    message = batch.warnings[0].message

    assert "A" in message
    assert "B" in message
    assert "'a'" not in message


def test_several_axes_are_described_as_a_combination() -> None:
    """It is the *combination* that is used up. "Every value of A, B and C"
    claims something stronger, and probably false, about each separately."""
    rows = [{"a": float(a), "b": float(b)} for a in (1, 2, 3) for b in (1, 2, 3)]
    batch = suggest_design_points(
        _points(rows), _integer_only_envelope(), n=5, seed=0, settings=CoverageSettings()
    )

    assert "combination" in batch.warnings[0].message


def test_the_message_says_what_to_do_about_it() -> None:
    """A dead end with no way out is a worse message than no message."""
    rows = [{"a": float(a), "b": float(b)} for a in (1, 2, 3) for b in (1, 2, 3)]
    batch = suggest_design_points(
        _points(rows), _integer_only_envelope(), n=5, seed=0, settings=CoverageSettings()
    )

    assert "full range the tool allows" in batch.warnings[0].message

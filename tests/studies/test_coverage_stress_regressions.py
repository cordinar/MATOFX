"""Regressions for the defects the adversarial stress pass found (ADR-0020 §15).

Every test here pins a bug that **passed the whole build-time suite** and was
only caught by running the feature against the real demo archive and against
deliberately hostile inputs. They are grouped by the shape of the mistake
rather than by module, because the shape is the reusable lesson:

* a value the fixtures never contained (a dict-valued ``material`` input);
* a surface that re-implemented a rule instead of calling it;
* a missing number treated as a number;
* a stored row read back by a release that did not write it.

None of these was a typing error, so none was caught by ``mypy --strict``;
none produced an exception in the fixture suite, so none was caught by the
existing tests. They are cheap to keep and expensive to rediscover.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import math
import textwrap
from typing import Any

import pytest

from matofx.models.tool_synthesis import synthesise_descriptor, synthesise_orchestrator
from matofx.studies.coverage import (
    CoverageReport,
    analyse_design,
    build_envelope,
    build_report,
    evaluate_support,
    resolve_points,
    suggest_design_points,
)
from matofx.studies.coverage.envelope import level_label
from matofx.studies.surrogate import SourceRef
from matofx.studies.surrogate.ingest import TrainingData
from matofx.ui._coverage_panel import headline
from matofx.vv.coverage_range import applicable_range_from_report

_NOW = dt.datetime(2026, 8, 25, tzinfo=dt.UTC)

_IRON: dict[str, Any] = {
    "material_id": 1,
    "name": "Iron",
    "class": "Metals",
    "properties": {"density": {"value": 7870.0}},
}
_COPPER: dict[str, Any] = {
    "material_id": 3,
    "name": "Copper",
    "class": "Metals",
    "properties": {"density": {"value": 8960.0}},
}


def _training(
    numeric: dict[str, list[float]],
    categorical: dict[str, list[Any]] | None = None,
    constants: dict[str, Any] | None = None,
) -> TrainingData:
    n = len(next(iter(numeric.values()))) if numeric else 0
    return TrainingData(
        factor_columns=numeric,
        categorical_columns=categorical or {},
        response=[1.0] * n,
        n_rows=n,
        n_complete=n,
        source_ref=SourceRef(source_kind="cohort", source_id=1, n_points=n),
        input_constants=constants or {},
    )


def _analyse(training: TrainingData):
    points = resolve_points(training)
    envelope = build_envelope(points)
    return points, envelope, analyse_design(points, envelope)


# --------------------------------------------------------------------------
# A value the fixtures never contained
# --------------------------------------------------------------------------


def test_a_dict_valued_input_does_not_crash_the_analysis() -> None:
    """A ``material`` input (ADR-0007) is a **dict**, and dicts are unhashable.

    Grouping input values through a ``set`` raised ``TypeError`` and took the
    whole cohort panel down for every tool that uses the materials database —
    143 runs in the demo archive do exactly this.
    """
    training = _training(
        {"thickness": [float(i) for i in range(20)]},
        {"material": [_IRON if i % 2 else _COPPER for i in range(20)]},
    )
    _points, _envelope, design = _analyse(training)
    assert design.verdict in {"good", "thin", "poor", "insufficient"}


def test_a_dict_level_is_named_by_its_human_name() -> None:
    """The level shown to a user is "Iron", never the whole record.

    These strings appear in the level-balance row and in the "barely any runs
    used X" warning, so ``str(dict)`` would put an unreadable repr in front of
    a user who is, by design, not a modeller.
    """
    training = _training(
        {"thickness": [float(i) for i in range(20)]},
        {"material": [_IRON if i % 2 else _COPPER for i in range(20)]},
    )
    _points, envelope, _design = _analyse(training)
    assert set(envelope.by_name("material").levels or ()) == {"Iron", "Copper"}


def test_every_surface_keys_a_level_the_same_way() -> None:
    """The envelope, the diagnostics and the guard must agree on the label.

    They did not: the envelope keyed levels by :func:`level_label` while the
    guard keyed them by ``str()``, so the guard rejected 138 of 143 real
    training runs as a value "this model never saw" — a model refusing to
    vouch for the very runs it was fitted on.
    """
    training = _training(
        {"thickness": [float(i) for i in range(20)]},
        {"material": [_IRON if i % 2 else _COPPER for i in range(20)]},
    )
    points, envelope, design = _analyse(training)

    diagnosed = design.diagnostics.level_counts.get("material", {})
    assert set(diagnosed) == set(envelope.by_name("material").levels or ())

    for row in points.rows:
        support = evaluate_support(envelope, dict(row))
        assert support.status != "outside", support.reason


def test_the_guard_still_rejects_a_level_never_trained_on() -> None:
    """Agreeing on the label must not blunt the guard."""
    training = _training(
        {"thickness": [float(i) for i in range(20)]},
        {"material": [_IRON] * 20},
    )
    _points, envelope, _design = _analyse(training)
    support = evaluate_support(envelope, {"thickness": 5.0, "material": _COPPER})
    assert support.status == "outside"
    assert "Copper" in support.reason
    assert "material_id" not in support.reason  # not the raw record


# --------------------------------------------------------------------------
# A missing number is not a number
# --------------------------------------------------------------------------


def test_a_missing_value_does_not_become_a_run_at_the_axis_minimum() -> None:
    """A non-finite coordinate placed the row at the bottom of the axis.

    That is a run which does not exist, sitting in exactly the region the
    analysis is supposed to report as *empty* — so a missing value **improved**
    the score. It must do the opposite: one fewer usable run.
    """
    clean = _training(
        {
            "a": [float(i) for i in range(20)],
            "b": [float(i) for i in range(20)],
        }
    )
    holed = _training(
        {
            "a": [float(i) for i in range(20)],
            "b": [float("nan") if i == 19 else float(i) for i in range(20)],
        }
    )
    _p1, _e1, good = _analyse(clean)
    _p2, _e2, gappy = _analyse(holed)

    assert gappy.diagnostics.n_unmeasurable == 1
    assert good.diagnostics.n_unmeasurable == 0
    assert gappy.coverage_score <= good.coverage_score
    assert math.isfinite(gappy.covering_radius)


def test_an_unmeasurable_run_is_not_reported_as_out_of_range() -> None:
    """"We cannot place this run" and "this run is outside the range" are
    different answers, and conflating them would tell a user to widen an
    envelope when the real problem is missing data."""
    training = _training(
        {
            "a": [float(i) for i in range(20)],
            "b": [float("nan") if i == 5 else float(i) for i in range(20)],
        }
    )
    _points, _envelope, design = _analyse(training)
    assert design.diagnostics.n_unmeasurable == 1
    assert design.diagnostics.n_outside_envelope == 0


def test_an_all_missing_input_is_not_pinned_at_nan() -> None:
    """``NaN != NaN``, so a NaN-pinned axis could never contain anything again
    — the tool's own suggestions failed its own guard."""
    training = _training(
        {
            "a": [float(i) for i in range(20)],
            "b": [float(i) for i in range(20)],
            "dead": [float("nan")] * 20,
        }
    )
    points, envelope, _design = _analyse(training)
    batch = suggest_design_points(points, envelope, n=4, seed=1)
    assert batch.points
    for candidate in batch.points:
        assert envelope.contains(candidate)


def test_the_headline_says_when_runs_could_not_be_included() -> None:
    """A run silently dropped from a coverage count is the exact failure this
    feature exists to catch, so the panel has to say it out loud."""
    text = headline(
        {
            "message": "These runs cover the range well.",
            "diagnostics": {"n_points": 19, "n_unmeasurable": 1},
        }
    )
    assert "1 run couldn't be included" in text
    assert "value was missing" in text


# --------------------------------------------------------------------------
# Suggestions must be runs somebody can actually submit
# --------------------------------------------------------------------------


def test_a_suggested_run_carries_every_input_the_tool_needs() -> None:
    """Suggestions omitted categorical axes entirely, so a suggested point
    failed the envelope's own ``contains`` check and was missing an input the
    tool requires — a run nobody could submit."""
    training = _training(
        {"thickness": [float(i) for i in range(20)]},
        {"material": [_IRON if i % 2 else _COPPER for i in range(20)]},
        {"held": 3.0},
    )
    points, envelope, _design = _analyse(training)
    batch = suggest_design_points(points, envelope, n=6, seed=2)
    assert batch.points
    for candidate in batch.points:
        assert "material" in candidate
        assert "held" in candidate
        assert envelope.contains(candidate)
        assert evaluate_support(envelope, candidate).status != "outside"


def test_a_batch_spreads_across_the_options_rather_than_fixing_one() -> None:
    """An imbalanced categorical axis is fixed by *varying* it."""
    training = _training(
        {"thickness": [float(i) for i in range(20)]},
        {"material": [_IRON if i % 2 else _COPPER for i in range(20)]},
    )
    points, envelope, _design = _analyse(training)
    batch = suggest_design_points(points, envelope, n=6, seed=2)
    chosen = {level_label(p["material"]) for p in batch.points}
    assert len(chosen) > 1


# --------------------------------------------------------------------------
# An exported tool must be able to answer its own guard
# --------------------------------------------------------------------------


def test_an_exported_tool_can_supply_every_envelope_axis() -> None:
    """The synthesised form omitted categorical factors and non-numeric held
    values, so the guard received an incomplete point — and an incomplete
    point can only be reported as unsupported. Every prediction of every such
    tool read ``outside``, naming an input the form did not have."""
    training = _training(
        {"thickness": [float(i) for i in range(20)]},
        {"material": [_IRON if i % 2 else _COPPER for i in range(20)]},
        {"mesh": "coarse.vtu", "t_ref": 25.0},
    )
    points, envelope, design = _analyse(training)
    report = build_report(
        design, envelope=envelope, source_kind="cohort", computed_at=_NOW, points=points
    ).to_json_obj()

    descriptor = synthesise_descriptor(
        source_tool_alias="bracket",
        model_id=1,
        version_id=1,
        name="Bracket surrogate",
        description="stress",
        factors=[{"name": "thickness", "min": 0.0, "max": 19.0}],
        responses=[{"name": "y", "kind": "scalar"}],
        version_ordinal=1,
        constants=[{"name": "mesh", "value": "coarse.vtu"}, {"name": "t_ref", "value": 25.0}],
        coverage_report=report,
    )

    supplied = {i.name for i in descriptor.inputs}
    for factor in envelope.factors:
        assert factor.name in supplied, f"{factor.name} has no input on the exported form"

    # And a submitted form -- defaults untouched -- must read `inside`.
    point: dict[str, Any] = {
        i.name: i.default for i in descriptor.inputs if i.default is not None
    }
    point["thickness"] = 10.0
    assert evaluate_support(envelope, point).status != "outside"


def test_the_generated_orchestrator_does_not_float_every_input() -> None:
    """``float(e["value"])`` over every input raised on the first categorical
    one, once the form carried them."""
    source = synthesise_orchestrator(
        version_id=1, bundle_path="b", bundle_sha256="s", tool_alias="t"
    )
    ast.parse(source)

    body = source[source.index("    point = {}") : source.index("    return run_surrogate_tool")]
    namespace: dict[str, Any] = {}
    payload = {
        "inputs": [
            {"name": "thickness", "value": 4.0},
            {"name": "material", "value": "Copper"},
            {"name": "flag", "value": True},
            {"name": "nothing", "value": None},
        ]
    }
    exec(textwrap.dedent(body), {"payload": payload}, namespace)  # noqa: S102
    assert namespace["point"] == {
        "thickness": 4.0,
        "material": "Copper",
        "flag": True,  # a bool is not a number, matching the guard's own rule
        "nothing": None,
    }


# --------------------------------------------------------------------------
# A stored report outlives the release that wrote it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"nonsense": 1},
        {"envelope": "not a list", "verdict": "good"},
        {"envelope": ["not a dict"]},
        {"envelope": [{"kind": "continuous", "lo": 1, "hi": 2}]},  # no name
        {"envelope": [{"name": "a", "kind": "continuous"}]},  # no bounds
        {"envelope": [{"name": "a", "kind": "continuous", "lo": float("nan"), "hi": 2.0}]},
        {"verdict": "excellent", "envelope": []},  # a verdict from the future
    ],
    ids=[
        "empty",
        "garbage-keys",
        "envelope-not-a-list",
        "axis-not-a-dict",
        "axis-without-a-name",
        "axis-without-bounds",
        "nan-bound",
        "unknown-verdict",
    ],
)
def test_a_malformed_stored_report_degrades_rather_than_raising(
    payload: dict[str, Any],
) -> None:
    """A stored report is a database row that outlives the release that wrote
    it. On a read path it must vouch for nothing, not raise."""
    report = CoverageReport.from_json_obj(payload)
    assert report.verdict in {"good", "thin", "poor", "insufficient"}
    evaluate_support(report.envelope, {"a": 1.0})  # must not raise
    offer = applicable_range_from_report(payload)
    assert isinstance(offer.ranges, dict)


def test_the_yardstick_survives_a_round_trip() -> None:
    """``coverage_score`` is a **ratio**, so a stored score alone is not
    comparable between two reports: adding runs raises the bar it is measured
    against. Without the yardstick, nobody reading two reports a month apart
    can tell which of the two moved."""
    training = _training(
        {"a": [float(i) for i in range(20)], "b": [float(i % 5) for i in range(20)]}
    )
    points, envelope, design = _analyse(training)
    assert design.reference_radius > 0.0

    payload = build_report(
        design, envelope=envelope, source_kind="cohort", computed_at=_NOW, points=points
    ).to_json_obj()
    json.dumps(payload)  # must stay serialisable
    back = CoverageReport.from_json_obj(payload)
    assert back.design.reference_radius == pytest.approx(design.reference_radius)
    assert back.design.diagnostics.n_unmeasurable == design.diagnostics.n_unmeasurable


def test_a_missing_categorical_value_is_counted_too() -> None:
    """Findings 66-68 fixed this for numeric axes and missed the categorical
    half — a categorical contributes no column to the normalised matrix, so a
    run whose ``material`` is ``None`` was measured as though nothing were
    wrong.

    Meanwhile the predict-time guard called that same run **outside**, because
    ``None`` is not a level the model ever saw: one real run in the demo
    archive, and the two surfaces disagreeing about it. A run that cannot be
    placed is unmeasurable on whichever kind of axis defeats it.
    """
    training = _training(
        {"thickness": [float(i) for i in range(20)]},
        {"material": [None if i == 7 else (_IRON if i % 2 else _COPPER) for i in range(20)]},
    )
    points, envelope, design = _analyse(training)

    assert design.diagnostics.n_unmeasurable == 1

    # And the two surfaces now agree about which run it is.
    refused = [
        row
        for row in points.rows
        if evaluate_support(envelope, {**points.constants, **dict(row)}).status == "outside"
    ]
    assert len(refused) == design.diagnostics.n_unmeasurable


def test_a_complete_categorical_archive_counts_none_unmeasurable() -> None:
    """The converse — the count must not fire on healthy data."""
    training = _training(
        {"thickness": [float(i) for i in range(20)]},
        {"material": [_IRON if i % 2 else _COPPER for i in range(20)]},
    )
    _points, _envelope, design = _analyse(training)
    assert design.diagnostics.n_unmeasurable == 0

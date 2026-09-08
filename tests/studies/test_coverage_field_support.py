"""The predicted-field viewer's support verdict (ADR-0020 §8, §16.81).

The 3-D viewer is a **live** prediction and the one place a user actively
drags controls out of the region the runs covered — pushing ``time`` past the
last trained snapshot is the textbook §8 case. It had no support statement at
all; the guard reached users only through an exported tool's job outputs.

Driven through the real service helper rather than a re-implementation of it,
because the interesting question is not "does ``evaluate_support`` work" (that
is settled elsewhere) but "does the verdict the viewer would draw arrive
intact".
"""

from __future__ import annotations

import datetime as dt
import types
from typing import Any

import numpy as np
import pytest

from matofx.models.service import _field_support
from matofx.studies.coverage import (
    Envelope,
    FactorEnvelope,
    analyse_design,
    build_envelope,
    build_report,
    edge_threshold_from,
    evaluate_support,
    resolve_points,
)
from matofx.studies.coverage.gp import ResponseModel, relative_sigma
from matofx.studies.surrogate import SourceRef
from matofx.studies.surrogate.field import fit_field_surrogate
from matofx.studies.surrogate.ingest import FieldTrainingData
from matofx.ui._coverage_panel import support_view

_NOW = dt.datetime(2026, 8, 25, tzinfo=dt.UTC)
_N_NODES = 24


def _field_fixture(
    thicknesses: tuple[float, ...] = (1.0, 2.0, 3.0, 4.0),
    loads: tuple[float, ...] = (10.0, 20.0, 30.0),
    snapshots: int = 5,
) -> tuple[Any, dict[str, Any], list[dict[str, float]]]:
    """A small time-resolved field surrogate, its stored report, and its runs."""
    rng = np.random.default_rng(7)
    rows: list[dict[str, float]] = []
    fields: list[np.ndarray] = []
    base = np.linspace(0.0, 1.0, _N_NODES)
    for thickness in thicknesses:
        for load in loads:
            for snap in range(snapshots):
                rows.append({"thickness": thickness, "load": load, "time": float(snap)})
                fields.append(
                    base * thickness + load * 0.01 * snap + rng.normal(0, 1e-6, _N_NODES)
                )

    training = FieldTrainingData(
        factor_columns={k: [float(r[k]) for r in rows] for k in ("thickness", "load", "time")},
        snapshots=list(fields),
        n_rows=len(rows),
        n_complete=len(rows),
        source_ref=SourceRef(source_kind="cohort", source_id=1, n_points=len(rows)),
        time_resolved=True,
        n_nodes=_N_NODES,
    )
    points = resolve_points(training)
    envelope = build_envelope(points)
    design = analyse_design(points, envelope)
    report = build_report(
        design, envelope=envelope, source_kind="cohort", computed_at=_NOW, points=points
    ).to_json_obj()
    surrogate = fit_field_surrogate(fields, training.factor_columns, surrogate_kind="gp")
    return surrogate, report, rows


def _verdict(surrogate: Any, stored: Any, point: dict[str, float]) -> dict[str, Any] | None:
    version = types.SimpleNamespace(coverage_report_json=stored)
    return _field_support(version, "temperature", surrogate, point)


# --------------------------------------------------------------------------
# The verdict itself
# --------------------------------------------------------------------------


def test_the_viewer_vouches_for_its_own_training_points() -> None:
    """The reflexive case, and the one assertion that caught two separate
    cross-surface defects earlier in this work: a model must not refuse to
    vouch for the runs it was fitted on."""
    surrogate, report, rows = _field_fixture()
    for row in (rows[0], rows[len(rows) // 2], rows[-1]):
        support = _verdict(surrogate, report, dict(row))
        assert support is not None
        assert support["status"] != "outside", support["reason"]


@pytest.mark.parametrize("snapshot", [5.0, 12.0, 500.0])
def test_a_snapshot_past_the_trained_span_is_outside(snapshot: float) -> None:
    """The textbook §8 case: the viewer's ``time`` slider is exactly how a
    user leaves the trained region without noticing."""
    surrogate, report, _rows = _field_fixture()
    support = _verdict(surrogate, report, {"thickness": 2.0, "load": 20.0, "time": snapshot})
    assert support is not None
    assert support["status"] == "outside"
    # Snapshots, never seconds — the platform has no seconds to quote here.
    assert "snapshot" in support["reason"].lower()
    assert "second" not in support["reason"].lower()


@pytest.mark.parametrize(
    ("point", "named"),
    [
        ({"thickness": 99.0, "load": 20.0, "time": 1.0}, "thickness"),
        ({"thickness": 2.0, "load": -5.0, "time": 1.0}, "load"),
    ],
)
def test_an_out_of_range_factor_is_named(point: dict[str, float], named: str) -> None:
    """"Outside the trained range" alone is not something anybody can act on."""
    surrogate, report, _rows = _field_fixture()
    support = _verdict(surrogate, report, point)
    assert support is not None
    assert support["status"] == "outside"
    assert named in support["reason"]


def test_edge_is_reachable_on_a_field_response() -> None:
    """A field response has no single GP, so its sigma is the **energy-weighted**
    aggregate over the mode surrogates (§2.2.1). If that did not come through,
    ``edge`` would be silently unreachable and the viewer would only ever say
    inside-or-outside — losing the half of §8 that catches a point which is
    technically in range but far from any actual run.
    """
    # A deliberately clustered design, so somewhere inside the envelope is
    # genuinely under-supported.
    surrogate, report, rows = _field_fixture(
        thicknesses=(1.0, 1.1, 1.2, 1.3, 4.0), loads=(10.0, 10.5, 11.0, 30.0), snapshots=4
    )
    model = ResponseModel.field("temperature", surrogate)
    assert model.has_variance

    trained = relative_sigma(model, [dict(r) for r in rows])
    assert trained is not None
    report["per_response"] = [
        {"response_name": "temperature", "edge_sigma_threshold": float(np.percentile(trained, 99))}
    ]

    in_the_gap = _verdict(surrogate, report, {"thickness": 2.5, "load": 20.0, "time": 2.0})
    at_a_run = _verdict(surrogate, report, dict(rows[0]))
    assert in_the_gap is not None
    assert at_a_run is not None
    assert at_a_run["status"] == "inside"
    assert in_the_gap["status"] == "edge"
    assert in_the_gap["sigma"] > at_a_run["sigma"]


def test_the_threshold_read_is_this_response_s_own() -> None:
    """The viewer knows which response it is reconstructing, so it uses that
    response's threshold rather than the first one that happens to have one."""
    stored = {
        "per_response": [
            {"response_name": "stress", "edge_sigma_threshold": 1.0},
            {"response_name": "temperature", "edge_sigma_threshold": 9.0},
        ]
    }
    assert edge_threshold_from(stored, response_name="temperature") == 9.0
    assert edge_threshold_from(stored, response_name="stress") == 1.0
    # No name: the first available stands for the point, which is what an
    # exported tool does — the factor space is shared.
    assert edge_threshold_from(stored) == 1.0
    # A named response with no threshold of its own falls back rather than
    # refusing to tell `edge` from `inside` at all.
    assert edge_threshold_from(stored, response_name="unknown") == 1.0


# --------------------------------------------------------------------------
# Silence, where silence is the honest answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stored", [None, {}, ""], ids=["null", "empty", "blank"])
def test_a_pre_coverage_version_draws_no_badge(stored: Any) -> None:
    """Silence about support and a clean bill of health are opposite claims."""
    surrogate, _report, _rows = _field_fixture()
    assert _verdict(surrogate, stored, {"thickness": 2.0, "load": 20.0, "time": 1.0}) is None


@pytest.mark.parametrize(
    "stored",
    [{"envelope": "nope"}, {"per_response": "nope"}, {"envelope": [1, 2, 3]}],
    ids=["envelope-not-a-list", "responses-not-a-list", "axes-not-dicts"],
)
def test_a_malformed_stored_report_does_not_break_the_viewer(stored: dict[str, Any]) -> None:
    """A guard that could raise would turn a caveat into a broken viewer."""
    surrogate, _report, _rows = _field_fixture()
    support = _verdict(surrogate, stored, {"thickness": 2.0, "load": 20.0, "time": 1.0})
    assert support is None or support["status"] in {"inside", "edge", "outside"}


def test_no_recorded_envelope_shows_nothing_rather_than_a_red_badge() -> None:
    """``outside`` here means **"we cannot tell"**, not "your point is out of
    range" — finding 67's shape, where "cannot be placed" was being counted as
    "outside the declared range".

    The conservative status stays, because an ADR-0017 validity rule should
    act on it. The badge does not, because "Outside the trained range" would
    assert something nobody checked.
    """
    verdict = evaluate_support(Envelope(), {"a": 1.0}).to_json_obj()
    assert verdict["status"] == "outside"
    assert verdict["available_statuses"] == []
    assert support_view(verdict) is None


def test_a_genuine_out_of_range_still_shows_its_red_badge() -> None:
    """Suppressing the un-assessable case must not blunt the real one."""
    envelope = Envelope(factors=(FactorEnvelope(name="a", kind="continuous", lo=0.0, hi=10.0),))
    verdict = evaluate_support(envelope, {"a": 99.0}).to_json_obj()
    view = support_view(verdict)
    assert view is not None
    assert view[0] == "Outside the trained range"
    assert "a" in view[2]


def test_a_payload_without_available_statuses_keeps_its_verdict() -> None:
    """An exported tool's job outputs carry ``value`` and ``note`` and no
    status list; absent is not the same as empty."""
    view = support_view({"status": "outside", "reason": "'thickness' is 42"})
    assert view is not None
    assert view[0] == "Outside the trained range"

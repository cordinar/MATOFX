"""The predict-time extrapolation guard (ADR-0020 §8, slice 6).

**The highest-value half of the feature.** A build-time check fires once; a
prediction guard fires every time someone leans on the model — including
inside an exported surrogate-tool running unattended in a workflow, where
nobody is watching a badge.

Three statuses, and which are available depends on the fit:

- ``outside`` — fails ``envelope.contains``. Deterministic, cheap, no model
  needed, and it works for **every** method. This is the half that catches the
  genuinely dangerous case.
- ``edge`` — inside the envelope but past the stored sigma threshold. Needs a
  posterior variance, so a ``poly`` / ``gbm`` response never returns it and
  says so rather than being silently unable.
- ``inside`` — the model is interpolating.

The threshold is **read from the stored report**, never recomputed: doing it
per prediction would cost a training-set evaluation on every call, and is
impossible on a version whose ``keep_training_data`` was off.
"""

from __future__ import annotations

import pytest

from matofx.studies.coverage import (
    FactorOverride,
    build_envelope,
    evaluate_support,
    resolve_points,
)
from tests.studies._coverage_fixtures import (
    descriptor,
    field_training,
    integer,
    num,
    training,
    two_factor_rows,
)


def _envelope(rows, **kwargs):
    resolve_kwargs = {k: kwargs.pop(k) for k in ("descriptor", "factor_spec") if k in kwargs}
    points = resolve_points(training(rows), **resolve_kwargs)
    return build_envelope(points, **resolve_kwargs, **kwargs)


# --- outside: the half that works for every method -----------------------


def test_a_point_beyond_the_envelope_is_outside() -> None:
    envelope = _envelope(two_factor_rows(4, 4))  # both axes over [1, 9]
    support = evaluate_support(envelope, {"thickness": 40.0, "heat_load": 5.0})
    assert support.status == "outside"
    assert support.reason


def test_outside_wins_regardless_of_sigma() -> None:
    # §8: "outside — the point fails envelope.contains, *including* ... —
    # deterministic, cheap, no model needed". A low sigma must not override it.
    envelope = _envelope(two_factor_rows(4, 4))
    support = evaluate_support(
        envelope, {"thickness": 40.0, "heat_load": 5.0}, sigma=0.0, edge_threshold=1.0
    )
    assert support.status == "outside"


def test_a_pinned_factor_at_a_different_value_is_outside() -> None:
    # The real scenario: someone reuses a model on a job with a different mesh
    # density. Predicting along a never-varied input is pure extrapolation.
    rows = [{"thickness": float(i), "mesh_density": "fine"} for i in range(1, 9)]
    envelope = _envelope(rows)

    ok = evaluate_support(envelope, {"thickness": 4.0, "mesh_density": "fine"})
    assert ok.status != "outside"
    assert (
        evaluate_support(envelope, {"thickness": 4.0, "mesh_density": "coarse"}).status == "outside"
    )


def test_a_derived_axis_beyond_its_trained_span_is_outside() -> None:
    # A field predicted at snapshot 500 from runs that kept 6 is extrapolation.
    rows = [
        {"thickness": float(t), "time": float(s)} for t in (1.0, 3.0, 5.0, 7.0) for s in range(6)
    ]
    points = resolve_points(field_training(rows))
    envelope = build_envelope(points)

    assert evaluate_support(envelope, {"thickness": 4.0, "time": 3.0}).status != "outside"
    assert evaluate_support(envelope, {"thickness": 4.0, "time": 500.0}).status == "outside"


def test_a_missing_factor_is_outside_rather_than_assumed() -> None:
    envelope = _envelope(two_factor_rows(4, 4))
    assert evaluate_support(envelope, {"thickness": 4.0}).status == "outside"


def test_the_reason_names_the_offending_input() -> None:
    # Plain-English and specific: "outside the range" without saying which
    # input is useless to the person reading it.
    envelope = _envelope(two_factor_rows(4, 4))
    reason = evaluate_support(envelope, {"thickness": 40.0, "heat_load": 5.0}).reason
    assert "thickness" in reason


def test_a_narrowed_envelope_is_what_the_guard_enforces() -> None:
    # The envelope is declared, so narrowing it narrows the guard too — that
    # is the point of §10 making it enforceable rather than documentary.
    rows = [{"thickness": float(t), "heat_load": float(t)} for t in range(1, 10)]
    envelope = _envelope(rows, overrides={"thickness": FactorOverride(lo=3.0, hi=6.0)})

    assert evaluate_support(envelope, {"thickness": 4.0, "heat_load": 4.0}).status != "outside"
    assert evaluate_support(envelope, {"thickness": 8.0, "heat_load": 4.0}).status == "outside"


def test_an_integer_axis_is_bounded_like_any_other() -> None:
    rows = [{"ply_count": float(n), "thickness": float(n)} for n in (1, 2, 3, 4, 5)]
    envelope = _envelope(
        rows, descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 9.0))
    )
    assert evaluate_support(envelope, {"ply_count": 3.0, "thickness": 3.0}).status != "outside"
    assert evaluate_support(envelope, {"ply_count": 9.0, "thickness": 3.0}).status == "outside"


# --- edge: only where a posterior variance exists ------------------------


def test_inside_the_envelope_and_under_the_threshold_is_inside() -> None:
    envelope = _envelope(two_factor_rows(4, 4))
    support = evaluate_support(
        envelope, {"thickness": 5.0, "heat_load": 5.0}, sigma=0.2, edge_threshold=1.0
    )
    assert support.status == "inside"


def test_inside_the_envelope_but_over_the_threshold_is_edge() -> None:
    envelope = _envelope(two_factor_rows(4, 4))
    support = evaluate_support(
        envelope, {"thickness": 5.0, "heat_load": 5.0}, sigma=2.0, edge_threshold=1.0
    )
    assert support.status == "edge"
    assert "less certain" in support.reason.lower() or "edge" in support.reason.lower()


def test_without_a_threshold_a_point_inside_reads_inside_not_edge() -> None:
    # A `poly` / `gbm` response has no posterior variance, so `edge` is not
    # computable. It must not silently never fire — it must not exist.
    envelope = _envelope(two_factor_rows(4, 4))
    support = evaluate_support(envelope, {"thickness": 5.0, "heat_load": 5.0})
    assert support.status == "inside"
    assert support.available_statuses == ("inside", "outside")


def test_with_a_threshold_all_three_statuses_are_available() -> None:
    envelope = _envelope(two_factor_rows(4, 4))
    support = evaluate_support(
        envelope, {"thickness": 5.0, "heat_load": 5.0}, sigma=0.1, edge_threshold=1.0
    )
    assert support.available_statuses == ("inside", "edge", "outside")


def test_sigma_is_reported_even_when_the_status_is_outside() -> None:
    # §8.1: the *numeric* sigma is the checkable quantity — ADR-0017 validity
    # works on numbers, not status strings — so it is always carried when it
    # is known.
    envelope = _envelope(two_factor_rows(4, 4))
    support = evaluate_support(
        envelope, {"thickness": 40.0, "heat_load": 5.0}, sigma=3.5, edge_threshold=1.0
    )
    assert support.status == "outside"
    assert support.sigma == pytest.approx(3.5)


def test_sigma_is_none_when_the_method_has_none() -> None:
    envelope = _envelope(two_factor_rows(4, 4))
    assert evaluate_support(envelope, {"thickness": 5.0, "heat_load": 5.0}).sigma is None


def test_the_boundary_is_inclusive_of_the_threshold() -> None:
    envelope = _envelope(two_factor_rows(4, 4))
    at = evaluate_support(
        envelope, {"thickness": 5.0, "heat_load": 5.0}, sigma=1.0, edge_threshold=1.0
    )
    assert at.status == "inside"  # not yet past it


# --- shape ---------------------------------------------------------------


def test_support_is_json_safe_for_the_predict_response() -> None:
    import json  # noqa: PLC0415

    envelope = _envelope(two_factor_rows(4, 4))
    payload = evaluate_support(
        envelope, {"thickness": 5.0, "heat_load": 5.0}, sigma=0.4, edge_threshold=1.0
    ).to_json_obj()
    assert json.loads(json.dumps(payload)) == payload
    assert set(payload) >= {"status", "sigma", "reason", "available_statuses"}


def test_an_empty_envelope_cannot_vouch_for_anything() -> None:
    from matofx.studies.coverage import Envelope  # noqa: PLC0415

    support = evaluate_support(Envelope(), {"thickness": 5.0})
    assert support.status == "outside"


def test_the_guard_never_raises_on_odd_input() -> None:
    envelope = _envelope(two_factor_rows(4, 4))
    for point in ({}, {"thickness": None}, {"thickness": "big", "heat_load": 3.0}):
        assert evaluate_support(envelope, point).status == "outside"  # type: ignore[arg-type]

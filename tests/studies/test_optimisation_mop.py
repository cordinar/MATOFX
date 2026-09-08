"""Unit tests for the MOP / CoP engine (ADR-0009 §5.7).

Pure functions over synthetic ``(factors, response)`` data. The bowl
``y = 10*x1 + 0.001*x2`` is dominated by ``x1``, so the auto-best
metamodel should attribute almost all CoP to ``x1`` and flag ``x2`` for
variable reduction.
"""

from __future__ import annotations

import math

from matofx.studies.optimisation.mop import (
    ALL_SURROGATE_KINDS,
    DEFAULT_COP_CUTOFF,
    fit_best_metamodel,
)


def _dominated_dataset() -> tuple[dict[str, list[float | None]], list[float | None]]:
    """40 runs of ``y = 10*x1 + 0.001*x2`` over a regular-ish grid."""
    xs1: list[float | None] = []
    xs2: list[float | None] = []
    ys: list[float | None] = []
    for i in range(40):
        x1 = (i % 8) / 7.0
        x2 = (i // 8) / 4.0
        xs1.append(x1)
        xs2.append(x2)
        ys.append(10.0 * x1 + 0.001 * x2)
    return {"x1": xs1, "x2": xs2}, ys


def test_fit_best_metamodel_attributes_cop_to_the_driver() -> None:
    factors, response = _dominated_dataset()
    result = fit_best_metamodel(factors, response)

    assert result.best_kind in ALL_SURROGATE_KINDS
    assert result.q2 is not None
    assert result.q2 > 0.5  # the surface is highly predictable
    cop_by_factor = {c.factor: c.cop for c in result.cop}
    assert set(cop_by_factor) == {"x1", "x2"}
    # x1 drives the response; x2 barely moves it.
    assert cop_by_factor["x1"] > cop_by_factor["x2"]
    assert cop_by_factor["x1"] > 0.5


def test_prunable_flags_the_inert_factor() -> None:
    factors, response = _dominated_dataset()
    result = fit_best_metamodel(factors, response)
    prunable = result.prunable(DEFAULT_COP_CUTOFF)
    assert "x2" in prunable
    assert "x1" not in prunable


def test_cop_is_sobol_total_effect_times_q2() -> None:
    factors, response = _dominated_dataset()
    result = fit_best_metamodel(factors, response)
    assert result.q2 is not None
    weight = max(0.0, result.q2)
    for c in result.cop:
        assert math.isclose(c.cop, min(1.0, max(0.0, c.total_effect * weight)), abs_tol=1e-9)


def test_kind_q2_records_every_kind_tried() -> None:
    factors, response = _dominated_dataset()
    result = fit_best_metamodel(factors, response)
    assert set(result.kind_q2) == set(ALL_SURROGATE_KINDS)


def test_insufficient_data_returns_message() -> None:
    result = fit_best_metamodel({"x1": [0.0, 1.0]}, [1.0, 2.0])
    assert result.best_kind is None
    assert result.q2 is None
    assert result.cop == ()
    assert result.message

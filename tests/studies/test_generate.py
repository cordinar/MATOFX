"""Exhaustive tests for the sampling-plan generator (H40, design §4).

Pure — no DB. Covers every v1 mode's shape + value mapping, RSM
coded->real, integer coercion, cross-field filtering with skip
accounting, the run-cap refusal, determinism of the stochastic modes,
and plain-English translation of pyDOE3 "not possible" errors.
"""

from __future__ import annotations

import pytest

from matofx.studies import (
    FactorSpec,
    FactorSpecError,
    ModeNotApplicableError,
    RunCapExceededError,
    SamplingParams,
    generate,
)
from matofx.tools.descriptor import (
    CrossFieldConstraint,
    InputField,
    SumCheck,
    ToolDescriptor,
)


def _desc(
    *inputs: InputField, constraints: list[CrossFieldConstraint] | None = None
) -> ToolDescriptor:
    return ToolDescriptor(
        alias="demo",
        name="Demo",
        version="1.0.0",
        description="generator test",
        main_script="static/main.py",
        inputs=list(inputs),
        cross_field_constraints=constraints or [],
    )


def _f(name: str, ftype: str = "float", **kw: object) -> InputField:
    return InputField(name=name, label=name.title(), type=ftype, **kw)  # type: ignore[arg-type]


def _range(lo: float, hi: float) -> dict[str, object]:
    return {"role": "vary", "type": "numeric", "vary": {"kind": "range", "min": lo, "max": hi}}


def _list(*vals: float) -> dict[str, object]:
    return {"role": "vary", "type": "numeric", "vary": {"kind": "list", "values": list(vals)}}


_SP = SamplingParams()


# --- enumerated modes -----------------------------------------------------


def test_list_mode_full_cross_product() -> None:
    desc = _desc(_f("a"), _f("b"))
    spec = FactorSpec.from_obj({"a": _list(1, 2), "b": _list(3, 4)})
    res = generate(desc, spec, "list", _SP, run_cap=100)
    assert res.raw_count == 4
    assert res.generated_count == 4
    assert sorted((p["a"], p["b"]) for p in res.points) == [
        (1.0, 3.0),
        (1.0, 4.0),
        (2.0, 3.0),
        (2.0, 4.0),
    ]


def test_grid_mode_discretises_range() -> None:
    desc = _desc(_f("x"), _f("y"))
    spec = FactorSpec.from_obj({"x": _range(0, 10), "y": _list(7)})
    res = generate(desc, spec, "grid", SamplingParams(points_per_factor=3), run_cap=100)
    assert sorted(p["x"] for p in res.points) == [0.0, 5.0, 10.0]
    assert {p["y"] for p in res.points} == {7.0}


# --- screening ------------------------------------------------------------


def test_frac_factorial_explicit_generator() -> None:
    desc = _desc(_f("a"), _f("b"), _f("c"))
    spec = FactorSpec.from_obj({"a": _range(0, 10), "b": _range(0, 10), "c": _range(0, 10)})
    res = generate(desc, spec, "frac_factorial", SamplingParams(frac_gen="a b ab"), run_cap=100)
    assert res.raw_count == 4
    for p in res.points:
        assert set(p) == {"a", "b", "c"}
        assert all(v in (0.0, 10.0) for v in p.values())


def test_frac_factorial_by_resolution_happy() -> None:
    desc = _desc(_f("a"), _f("b"), _f("c"), _f("d"))
    spec = FactorSpec.from_obj({c: _range(0, 1) for c in "abcd"})
    res = generate(desc, spec, "frac_factorial", SamplingParams(frac_resolution=3), run_cap=100)
    assert res.raw_count == 8


def test_frac_factorial_impossible_design_is_plain_english() -> None:
    desc = _desc(_f("a"), _f("b"), _f("c"))
    spec = FactorSpec.from_obj({c: _range(0, 1) for c in "abc"})
    with pytest.raises(FactorSpecError) as exc:
        generate(desc, spec, "frac_factorial", SamplingParams(frac_resolution=3), run_cap=100)
    assert "design not possible" not in str(exc.value)
    assert "resolution-3" in str(exc.value)


def test_plackett_burman() -> None:
    desc = _desc(_f("a"), _f("b"), _f("c"))
    spec = FactorSpec.from_obj({c: _range(-1, 1) for c in "abc"})
    res = generate(desc, spec, "plackett_burman", _SP, run_cap=100)
    assert res.raw_count == 4
    for p in res.points:
        assert all(v in (-1.0, 1.0) for v in p.values())


# --- RSM ------------------------------------------------------------------


def test_ccd_faced_stays_in_range() -> None:
    desc = _desc(_f("a"), _f("b"))
    spec = FactorSpec.from_obj({"a": _range(0, 10), "b": _range(0, 10)})
    res = generate(desc, spec, "ccd", SamplingParams(ccd_center=(1, 1)), run_cap=100)
    assert res.raw_count > 0
    for p in res.points:
        assert 0.0 <= p["a"] <= 10.0
        assert 0.0 <= p["b"] <= 10.0
    assert any(p["a"] == 5.0 and p["b"] == 5.0 for p in res.points)  # centre


def test_box_behnken_needs_three_factors() -> None:
    desc = _desc(_f("a"), _f("b"))
    spec = FactorSpec.from_obj({"a": _range(0, 1), "b": _range(0, 1)})
    with pytest.raises(FactorSpecError, match="at least 3"):
        generate(desc, spec, "box_behnken", _SP, run_cap=100)


def test_box_behnken_three_factors() -> None:
    desc = _desc(_f("a"), _f("b"), _f("c"))
    spec = FactorSpec.from_obj({c: _range(0, 10) for c in "abc"})
    res = generate(desc, spec, "box_behnken", SamplingParams(bb_center=1), run_cap=100)
    assert res.raw_count == 13
    for p in res.points:
        assert all(v in (0.0, 5.0, 10.0) for v in p.values())


# --- space-filling + determinism -----------------------------------------


@pytest.mark.parametrize("mode", ["lhs", "sobol", "random"])
def test_space_filling_shape_and_bounds(mode: str) -> None:
    desc = _desc(_f("a"), _f("b"))
    spec = FactorSpec.from_obj({"a": _range(0, 10), "b": _range(-5, 5)})
    res = generate(desc, spec, mode, SamplingParams(n_samples=16), run_cap=100, seed=42)  # type: ignore[arg-type]
    assert res.raw_count == 16
    for p in res.points:
        assert 0.0 <= p["a"] <= 10.0
        assert -5.0 <= p["b"] <= 5.0


@pytest.mark.parametrize("mode", ["lhs", "sobol", "random"])
def test_space_filling_deterministic_with_seed(mode: str) -> None:
    desc = _desc(_f("a"))
    spec = FactorSpec.from_obj({"a": _range(0, 1)})
    sp = SamplingParams(n_samples=12)
    r1 = generate(desc, spec, mode, sp, run_cap=100, seed=7)  # type: ignore[arg-type]
    r2 = generate(desc, spec, mode, sp, run_cap=100, seed=7)  # type: ignore[arg-type]
    r3 = generate(desc, spec, mode, sp, run_cap=100, seed=8)  # type: ignore[arg-type]
    assert [p["a"] for p in r1.points] == [p["a"] for p in r2.points]
    assert [p["a"] for p in r1.points] != [p["a"] for p in r3.points]


def test_distribution_normal_sampled() -> None:
    desc = _desc(_f("a"))
    spec = FactorSpec.from_obj(
        {
            "a": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "distribution", "dist": "normal", "mean": 0, "std": 1},
            }
        }
    )
    res = generate(desc, spec, "lhs", SamplingParams(n_samples=200), run_cap=1000, seed=1)
    vals = [p["a"] for p in res.points]
    assert min(vals) < 0 < max(vals)  # spans the mean


# --- domain bounds (LHS "points at one edge" root cause) ------------------


def test_range_outside_declared_bounds_rejected() -> None:
    """A Vary range wider than the input's [min,max] is refused, not clamped.

    The old behaviour clamped every out-of-bounds sample onto the limit,
    piling points on one edge and distorting the distribution.
    """
    desc = _desc(_f("x", "float", minimum=0, maximum=50))
    spec = FactorSpec.from_obj({"x": _range(0, 100)})
    with pytest.raises(FactorSpecError, match="outside the values the tool allows"):
        generate(desc, spec, "lhs", SamplingParams(n_samples=20), run_cap=100, seed=1)


def test_list_values_outside_bounds_rejected() -> None:
    desc = _desc(_f("x", "float", minimum=0, maximum=10))
    spec = FactorSpec.from_obj({"x": _list(1, 5, 500)})
    with pytest.raises(FactorSpecError, match="outside the values the tool allows"):
        generate(desc, spec, "list", _SP, run_cap=100)


def test_in_bounds_lhs_is_uniform_no_pileup() -> None:
    """In-bounds LHS spreads evenly — no clamp pile-up on a bound."""
    desc = _desc(_f("a", "float", minimum=0, maximum=100))
    spec = FactorSpec.from_obj({"a": _range(0, 100)})
    res = generate(desc, spec, "lhs", SamplingParams(n_samples=50), run_cap=1000, seed=1)
    counts = [0] * 10
    for p in res.points:
        counts[min(int(p["a"] // 10), 9)] += 1
    assert counts == [5] * 10  # one sample per decile — textbook LHS


def test_normal_truncated_to_bounds() -> None:
    """A normal distribution is truncated to the input's bounds — every
    sample stays inside and nothing piles on a limit."""
    desc = _desc(_f("t", "float", minimum=0, maximum=10))
    spec = FactorSpec.from_obj(
        {
            "t": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "distribution", "dist": "normal", "mean": 5, "std": 4},
            }
        }
    )
    res = generate(desc, spec, "lhs", SamplingParams(n_samples=200), run_cap=1000, seed=2)
    vals = [p["t"] for p in res.points]
    assert all(0.0 <= v <= 10.0 for v in vals)
    # No pile-up: fewer than 5% of samples land within 0.1% of either bound.
    on_edge = sum(1 for v in vals if v <= 0.01 or v >= 9.99)
    assert on_edge < 0.05 * len(vals)


# --- coercion -------------------------------------------------------------


def test_integer_field_coerced_to_int_within_bounds() -> None:
    desc = _desc(_f("n", "integer", minimum=0, maximum=1))
    spec = FactorSpec.from_obj({"n": _range(0, 1)})
    res = generate(desc, spec, "grid", SamplingParams(points_per_factor=3), run_cap=100)
    for p in res.points:
        assert isinstance(p["n"], int)
        assert 0 <= p["n"] <= 1


# --- cross-field filtering + run cap -------------------------------------


def _sumcheck_desc() -> ToolDescriptor:
    return _desc(
        InputField(
            name="a",
            label="A pct",
            type="float",
            sum_check=SumCheck(value=100.0, sum_parameters=["a", "b"]),
        ),
        _f("b"),
    )


def test_cross_field_filtering_drops_and_counts() -> None:
    spec = FactorSpec.from_obj({"a": _list(10, 20), "b": _list(80, 90)})
    res = generate(_sumcheck_desc(), spec, "list", _SP, run_cap=100)
    assert res.raw_count == 4
    assert res.generated_count == 2  # 10+90, 20+80
    assert res.skipped_invalid_count == 2
    assert sum(res.skip_reasons.values()) == 2
    assert any("add up to 100" in msg for msg in res.skip_reasons)
    for p in res.points:
        assert p["a"] + p["b"] == 100.0


def test_run_cap_exceeded_is_plain_english() -> None:
    spec = FactorSpec.from_obj({"a": _list(10, 20), "b": _list(80, 90)})
    with pytest.raises(RunCapExceededError) as exc:
        generate(_sumcheck_desc(), spec, "list", _SP, run_cap=1)
    msg = str(exc.value)
    assert "2 simulations" in msg
    assert "limit is 1" in msg


def test_unknown_factor_name_rejected() -> None:
    desc = _desc(_f("a"))
    spec = FactorSpec.from_obj({"a": _list(1), "ghost": _list(2)})
    with pytest.raises(FactorSpecError, match="doesn't have"):
        generate(desc, spec, "list", _SP, run_cap=100)


def test_mode_gate_bubbles_from_validate_for_mode() -> None:
    desc = _desc(_f("g", "file"))
    spec = FactorSpec.from_obj(
        {"g": {"role": "vary", "type": "file", "vary": {"kind": "file_list", "cas_hashes": ["h"]}}}
    )
    with pytest.raises(ModeNotApplicableError):
        generate(desc, spec, "sobol", _SP, run_cap=100)

"""Exhaustive tests for the 2026-05-19 amendment (H40, design §3/§4/§9).

Pure — no DB. Covers: the ``table`` run-matrix engine; per-factor grid
``levels`` + the five ``spacing`` warps (incl. the dropped ``min>0``
constraint); integer-level de-duplication + plain-English warning; the
run-table ⟺ column rule; and ``available_modes`` selector pre-filter.
"""

from __future__ import annotations

import itertools

import pytest

from matofx.studies import (
    STUDY_MODES,
    FactorSpec,
    FactorSpecError,
    ModeNotApplicableError,
    RunCapExceededError,
    SamplingParams,
    available_modes,
    generate,
)
from matofx.tools.descriptor import InputField, SumCheck, ToolDescriptor


def _desc(*inputs: InputField) -> ToolDescriptor:
    return ToolDescriptor(
        alias="demo",
        name="Demo",
        version="1.0.0",
        description="amendment test",
        main_script="static/main.py",
        inputs=list(inputs),
        cross_field_constraints=[],
    )


def _f(name: str, ftype: str = "float", **kw: object) -> InputField:
    return InputField(name=name, label=name.title(), type=ftype, **kw)  # type: ignore[arg-type]


def _grange(
    lo: float, hi: float, *, levels: int | None = None, spacing: str | None = None
) -> dict[str, object]:
    vary: dict[str, object] = {"kind": "range", "min": lo, "max": hi}
    if levels is not None:
        vary["levels"] = levels
    if spacing is not None:
        vary["spacing"] = spacing
    return {"role": "vary", "type": "numeric", "vary": vary}


def _col(ftype: str = "numeric") -> dict[str, object]:
    return {"role": "vary", "type": ftype, "vary": {"kind": "column"}}


def _xs(res: object, key: str) -> list[float]:
    return sorted(p[key] for p in res.points)  # type: ignore[attr-defined]


def _diffs(xs: list[float]) -> list[float]:
    return [b - a for a, b in itertools.pairwise(xs)]


# --- per-factor levels ----------------------------------------------------


def test_grid_per_factor_levels_independent() -> None:
    desc = _desc(_f("x"), _f("y"))
    spec = FactorSpec.from_obj({"x": _grange(0, 1, levels=3), "y": _grange(0, 1, levels=4)})
    res = generate(desc, spec, "grid", SamplingParams(), run_cap=100)
    assert res.raw_count == 12
    assert len({p["x"] for p in res.points}) == 3
    assert len({p["y"] for p in res.points}) == 4


def test_levels_default_falls_back_to_points_per_factor() -> None:
    desc = _desc(_f("x"))
    spec = FactorSpec.from_obj({"x": _grange(0, 1)})
    res = generate(desc, spec, "grid", SamplingParams(points_per_factor=5), run_cap=100)
    assert len(res.points) == 5


# --- spacing warps --------------------------------------------------------


def test_linear_evenly_spaced() -> None:
    desc = _desc(_f("x"))
    spec = FactorSpec.from_obj({"x": _grange(0, 10, levels=5, spacing="linear")})
    res = generate(desc, spec, "grid", SamplingParams(), run_cap=100)
    assert _xs(res, "x") == pytest.approx([0.0, 2.5, 5.0, 7.5, 10.0])


@pytest.mark.parametrize("spacing", ["linear", "log_min", "log_max", "edges", "center"])
def test_endpoints_exact_and_strictly_increasing(spacing: str) -> None:
    desc = _desc(_f("x"))
    spec = FactorSpec.from_obj({"x": _grange(-3, 7, levels=6, spacing=spacing)})
    res = generate(desc, spec, "grid", SamplingParams(), run_cap=100)
    xs = _xs(res, "x")
    assert len(xs) == 6
    assert xs[0] == -3.0
    assert xs[-1] == 7.0
    assert all(a < b for a, b in itertools.pairwise(xs))


def test_log_min_dense_near_min() -> None:
    desc = _desc(_f("x"))
    spec = FactorSpec.from_obj({"x": _grange(0, 1, levels=6, spacing="log_min")})
    res = generate(desc, spec, "grid", SamplingParams(), run_cap=100)
    d = _diffs(_xs(res, "x"))
    assert all(a < b for a, b in itertools.pairwise(d))  # gaps grow -> dense at min


def test_log_max_dense_near_max() -> None:
    desc = _desc(_f("x"))
    spec = FactorSpec.from_obj({"x": _grange(0, 1, levels=6, spacing="log_max")})
    res = generate(desc, spec, "grid", SamplingParams(), run_cap=100)
    d = _diffs(_xs(res, "x"))
    assert all(a > b for a, b in itertools.pairwise(d))  # gaps shrink -> dense at max


def test_edges_dense_at_both_ends() -> None:
    desc = _desc(_f("x"))
    spec = FactorSpec.from_obj({"x": _grange(0, 1, levels=7, spacing="edges")})
    res = generate(desc, spec, "grid", SamplingParams(), run_cap=100)
    d = _diffs(_xs(res, "x"))
    assert d[0] < d[len(d) // 2]  # first gap < middle gap
    assert d[-1] < d[len(d) // 2]


def test_center_dense_in_middle() -> None:
    desc = _desc(_f("x"))
    spec = FactorSpec.from_obj({"x": _grange(0, 1, levels=7, spacing="center")})
    res = generate(desc, spec, "grid", SamplingParams(), run_cap=100)
    d = _diffs(_xs(res, "x"))
    assert d[len(d) // 2] < d[0]  # middle gap < end gap


@pytest.mark.parametrize("spacing", ["log_min", "log_max"])
def test_log_spacing_has_no_min_constraint(spacing: str) -> None:
    # Spacing warps the position, not the values — works on a range that
    # spans zero and on a negative-only range (the dropped min>0 rule).
    desc = _desc(_f("x"))
    spec = FactorSpec.from_obj({"x": _grange(-50, 50, levels=5, spacing=spacing)})
    res = generate(desc, spec, "grid", SamplingParams(), run_cap=100)
    xs = _xs(res, "x")
    assert xs[0] == -50.0
    assert xs[-1] == 50.0
    assert len(xs) == 5

    neg = FactorSpec.from_obj({"x": _grange(-100, -10, levels=4, spacing=spacing)})
    res2 = generate(desc, neg, "grid", SamplingParams(), run_cap=100)
    assert _xs(res2, "x")[0] == -100.0
    assert _xs(res2, "x")[-1] == -10.0


# --- integer level de-duplication + warning -------------------------------


def test_integer_levels_dedup_and_warn() -> None:
    desc = _desc(_f("k", "integer"))
    spec = FactorSpec.from_obj({"k": _grange(0, 2, levels=8, spacing="linear")})
    res = generate(desc, spec, "grid", SamplingParams(), run_cap=100)
    ks = {p["k"] for p in res.points}
    assert ks <= {0, 1, 2}
    assert all(isinstance(v, int) for v in ks)
    assert res.generated_count == len(ks)
    assert res.level_warnings
    assert "k" in res.level_warnings[0]
    assert "8 levels requested" in res.level_warnings[0]


def test_integer_levels_no_warn_when_enough_distinct() -> None:
    desc = _desc(_f("k", "integer"))
    spec = FactorSpec.from_obj({"k": _grange(0, 30, levels=4, spacing="linear")})
    res = generate(desc, spec, "grid", SamplingParams(), run_cap=100)
    assert res.level_warnings == ()
    assert res.generated_count == 4


# --- table mode -----------------------------------------------------------


def test_table_runs_exactly_as_entered() -> None:
    desc = _desc(_f("a"), _f("b"))
    spec = FactorSpec.from_obj({"a": _col(), "b": _col()})
    sp = SamplingParams(runs=[{"a": 1, "b": 2}, {"a": 3, "b": 4}])
    res = generate(desc, spec, "table", sp, run_cap=100)
    assert res.raw_count == 2
    assert [(p["a"], p["b"]) for p in res.points] == [(1.0, 2.0), (3.0, 4.0)]


def test_table_merges_constants() -> None:
    desc = _desc(_f("a"), _f("b"), _f("c"))
    spec = FactorSpec.from_obj({"a": _col(), "b": _col(), "c": {"role": "constant", "value": 9.0}})
    sp = SamplingParams(runs=[{"a": 1, "b": 2}, {"a": 5, "b": 6}])
    res = generate(desc, spec, "table", sp, run_cap=100)
    assert all(p["c"] == 9.0 for p in res.points)
    assert [p["a"] for p in res.points] == [1.0, 5.0]


def test_table_missing_column_raises() -> None:
    desc = _desc(_f("a"), _f("b"))
    spec = FactorSpec.from_obj({"a": _col(), "b": _col()})
    with pytest.raises(FactorSpecError, match="missing a value"):
        generate(desc, spec, "table", SamplingParams(runs=[{"a": 1}]), run_cap=100)


def test_table_unexpected_column_raises() -> None:
    desc = _desc(_f("a"))
    spec = FactorSpec.from_obj({"a": _col()})
    with pytest.raises(FactorSpecError, match="unexpected column"):
        generate(desc, spec, "table", SamplingParams(runs=[{"a": 1, "z": 9}]), run_cap=100)


@pytest.mark.parametrize("runs", [None, []])
def test_table_empty_raises(runs: list[dict[str, object]] | None) -> None:
    desc = _desc(_f("a"))
    spec = FactorSpec.from_obj({"a": _col()})
    with pytest.raises(FactorSpecError, match="at least one run"):
        generate(desc, spec, "table", SamplingParams(runs=runs), run_cap=100)


def test_table_cross_field_skip_then_cap() -> None:
    desc = _desc(
        InputField(
            name="a",
            label="A pct",
            type="float",
            sum_check=SumCheck(value=100.0, sum_parameters=["a", "b"]),
        ),
        _f("b"),
    )
    spec = FactorSpec.from_obj({"a": _col(), "b": _col()})
    runs = [{"a": 50, "b": 50}, {"a": 10, "b": 10}, {"a": 60, "b": 40}]
    res = generate(desc, spec, "table", SamplingParams(runs=runs), run_cap=100)
    assert res.generated_count == 2
    assert res.skipped_invalid_count == 1
    assert sum(res.skip_reasons.values()) == 1

    with pytest.raises(RunCapExceededError):
        generate(desc, spec, "table", SamplingParams(runs=runs), run_cap=1)


def test_table_file_column_passthrough() -> None:
    desc = _desc(_f("g", "file"))
    spec = FactorSpec.from_obj({"g": _col("file")})
    sp = SamplingParams(runs=[{"g": "hashA"}, {"g": "hashB"}])
    res = generate(desc, spec, "table", sp, run_cap=100)
    assert [p["g"] for p in res.points] == ["hashA", "hashB"]


def test_table_integer_coerced_and_clamped() -> None:
    desc = _desc(_f("n", "integer", minimum=0, maximum=5))
    spec = FactorSpec.from_obj({"n": _col()})
    sp = SamplingParams(runs=[{"n": 2.4}, {"n": 3.6}, {"n": 99}])
    res = generate(desc, spec, "table", sp, run_cap=100)
    assert [p["n"] for p in res.points] == [2, 4, 5]
    assert all(isinstance(p["n"], int) for p in res.points)


# --- run-table ⟺ column rule ---------------------------------------------


def test_column_only_valid_in_table_mode() -> None:
    desc = _desc(_f("a"))
    spec = FactorSpec.from_obj({"a": _col()})
    with pytest.raises(ModeNotApplicableError, match="run-table column"):
        generate(desc, spec, "list", SamplingParams(), run_cap=100)


def test_table_rejects_non_column_factor() -> None:
    desc = _desc(_f("a"))
    spec = FactorSpec.from_obj({"a": _grange(0, 1)})
    with pytest.raises(ModeNotApplicableError, match="run-table column"):
        generate(desc, spec, "table", SamplingParams(runs=[{"a": 1}]), run_cap=100)


# --- available_modes pre-filter (design §9.3) -----------------------------


def test_available_modes_covers_every_study_mode() -> None:
    assert set(available_modes(_desc(_f("a")))) == set(STUDY_MODES)


def test_available_modes_numeric_only_offers_all() -> None:
    am = available_modes(_desc(_f("a"), _f("b"), _f("c")))
    assert am["table"] is None
    assert am["list"] is None
    assert am["grid"] is None
    assert am["frac_factorial"] is None
    assert am["ccd"] is None
    assert am["box_behnken"] is None  # 3 numeric
    assert am["lhs"] is None


def test_available_modes_box_behnken_needs_three_numeric() -> None:
    am = available_modes(_desc(_f("a"), _f("b")))
    assert am["frac_factorial"] is None
    assert am["ccd"] is None
    reason = am["box_behnken"]
    assert reason is not None
    assert "at least 3" in reason
    assert "has 2" in reason


def test_available_modes_categorical_only_greys_screening_rsm() -> None:
    am = available_modes(_desc(_f("s", "select", options=["x", "y"]), _f("flag", "boolean")))
    assert am["table"] is None
    assert am["list"] is None
    assert am["grid"] is None
    assert am["lhs"] is None  # categorical is sampleable (binned)
    for m in ("frac_factorial", "plackett_burman", "ccd", "box_behnken"):
        assert am[m] is not None
        assert "continuous numeric" in am[m]


def test_available_modes_file_only_greys_sampling() -> None:
    am = available_modes(_desc(_f("g", "file")))
    assert am["table"] is None
    assert am["list"] is None
    assert am["grid"] is None
    for m in ("frac_factorial", "plackett_burman", "ccd", "box_behnken"):
        assert am[m] is not None
    for m in ("lhs", "sobol", "random"):
        assert am[m] is not None
        assert "numeric or categorical" in am[m]

"""Tests for surrogate-as-tool synthesis core (ADR-0008 §7, Phase 6a).

Pure functions — no DB. Covers descriptor generation (inputs/outputs +
``<response>_std`` siblings, ``block_extrapolation`` bound-tightening),
the orchestrator source (baked version id + sha256), and the coverage-
gauge classification + worst-class summary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from matofx.models import load_surrogate_model_version
from matofx.models.bundle import write_scalar_bundle
from matofx.models.errors import ModelBundleCorruptError
from matofx.models.tool_synthesis import (
    classify_coverage,
    surrogate_tool_alias,
    synthesise_descriptor,
    synthesise_orchestrator,
    worst_coverage,
)
from matofx.studies import fit_surrogate

_FACTORS = [{"name": "fin_h", "min": 5.0, "max": 40.0}, {"name": "fin_n", "min": 4.0, "max": 60.0}]
_RESPONSES = [{"name": "mass", "kind": "scalar"}, {"name": "max_temp", "kind": "scalar"}]


def _descriptor(**overrides):
    kwargs = {
        "source_tool_alias": "heat_sink",
        "model_id": 7,
        "version_id": 31,
        "name": "Heat sink surrogate",
        "description": "A GP surrogate.",
        "factors": _FACTORS,
        "responses": _RESPONSES,
        "version_ordinal": 2,
    }
    kwargs.update(overrides)
    return synthesise_descriptor(**kwargs)


def test_descriptor_inputs_outputs_and_std() -> None:
    d = _descriptor()
    assert d.alias == surrogate_tool_alias("heat_sink", 7) == "surrogate_heat_sink_7"
    assert d.main_script == "surrogate_orchestrator.py"
    assert [i.name for i in d.inputs] == ["fin_h", "fin_n"]
    assert all(i.type == "float" for i in d.inputs)
    # One numeric output per response + a 1-sigma sibling.
    assert [o.name for o in d.outputs] == ["mass", "mass_std", "max_temp", "max_temp_std"]
    assert all(o.type == "number" for o in d.outputs)
    # Each std sibling names its value output so the results panel folds it
    # into a ``value ± std`` display (§7.1).
    mass_std = next(o for o in d.outputs if o.name == "mass_std")
    assert mass_std.uncertainty_for == "mass"
    assert next(o for o in d.outputs if o.name == "mass").uncertainty_for is None
    # The descriptor pins the fitted version (§7.4) so submit can tag the job.
    assert d.surrogate_model_version_id == 31


def test_descriptor_carries_source_labels_and_units() -> None:
    # When the caller enriches factors/responses with the source tool's
    # label + unit, the exported form reads like the original (names are
    # identifiers, labels are UX). The _std sibling inherits the base label.
    d = synthesise_descriptor(
        source_tool_alias="heat_sink",
        model_id=7,
        version_id=31,
        name="Heat sink surrogate",
        description="A GP surrogate.",
        factors=[{"name": "fin_h", "min": 5.0, "max": 40.0, "label": "Fin height", "unit": "mm"}],
        responses=[{"name": "mass", "kind": "scalar", "label": "Mass", "unit": "kg"}],
        version_ordinal=2,
    )
    fin = next(i for i in d.inputs if i.name == "fin_h")
    assert fin.label == "Fin height"
    assert fin.unit == "mm"
    mass = next(o for o in d.outputs if o.name == "mass")
    assert mass.label == "Mass"
    assert mass.unit == "kg"
    mass_std = next(o for o in d.outputs if o.name == "mass_std")
    assert mass_std.label == "Mass (1-sigma)"
    assert mass_std.unit == "kg"


def test_descriptor_falls_back_to_names_without_labels() -> None:
    # No label key → label defaults to the name (back-compat for callers
    # that don't enrich, e.g. the unit tests above).
    d = _descriptor()
    assert next(i for i in d.inputs if i.name == "fin_h").label == "fin_h"
    assert next(o for o in d.outputs if o.name == "mass").label == "mass"


def test_no_constants_keeps_flat_ungrouped_form() -> None:
    # No constants → the form stays flat (no groups), the back-compat shape.
    d = _descriptor()
    assert d.input_groups == []
    assert all(i.group is None for i in d.inputs)


def test_constants_split_into_varied_and_held_groups() -> None:
    # The varied factors + held constants become inputs in two groups
    # (§7.6). A **non-numeric** constant is carried too, as a single-option
    # select: it is an axis of the declared envelope, so the predict-time
    # guard checks every prediction against it, and leaving it out of the
    # form handed the guard an incomplete point -- which it can only report
    # as unsupported. Measured on the real archive that was *every*
    # prediction of every exported surrogate with a categorical input.
    d = synthesise_descriptor(
        source_tool_alias="heat_sink",
        model_id=7,
        version_id=31,
        name="Heat sink surrogate",
        description="A GP surrogate.",
        factors=[{"name": "fin_h", "min": 5.0, "max": 40.0, "label": "Fin height", "unit": "mm"}],
        responses=[{"name": "mass", "kind": "scalar"}],
        version_ordinal=2,
        constants=[
            {"name": "conductivity", "value": 200.0, "label": "Conductivity", "unit": "W/m/K"},
            {"name": "material", "value": "aluminium"},  # non-numeric → carried
        ],
    )
    assert [i.name for i in d.inputs] == ["fin_h", "conductivity", "material"]
    material = next(i for i in d.inputs if i.name == "material")
    assert material.type == "select"
    assert material.options == ["aluminium"]
    assert material.default == "aluminium"
    assert material.group == "held_constant"
    assert material.required is False
    assert {g.name for g in d.input_groups} == {"varied", "held_constant"}
    assert next(i for i in d.inputs if i.name == "fin_h").group == "varied"
    held = next(i for i in d.inputs if i.name == "conductivity")
    assert held.group == "held_constant"
    assert held.default == 200.0
    assert held.unit == "W/m/K"
    assert held.required is False
    # Both sections show expanded (no "open every group" gate).
    assert d.input_groups_start_collapsed is False


def test_with_std_false_omits_uncertainty_outputs() -> None:
    d = _descriptor(with_std=False)
    assert [o.name for o in d.outputs] == ["mass", "max_temp"]


def test_advisory_default_leaves_inputs_unbounded() -> None:
    # block_extrapolation off (default): the gauge advises, the form does
    # not reject — so the inputs carry NO hard bounds (§7.3).
    d = _descriptor()
    fin_h = next(i for i in d.inputs if i.name == "fin_h")
    assert fin_h.minimum is None
    assert fin_h.maximum is None
    assert "trained over 5.0 to 40.0" in (fin_h.help_text or "").lower()


def test_block_extrapolation_tightens_bounds() -> None:
    d = _descriptor(block_extrapolation=True)
    fin_h = next(i for i in d.inputs if i.name == "fin_h")
    assert fin_h.minimum == 5.0
    assert fin_h.maximum == 40.0  # bounds present → form validation enforces them


def test_orchestrator_is_a_valid_v2_tool() -> None:
    src = synthesise_orchestrator(
        version_id=42,
        bundle_path="/store/model_7/v1",
        bundle_sha256="deadbeef",
        tool_alias="surrogate_demo_7",
    )
    assert "_VERSION_ID = 42" in src
    assert '"deadbeef"' in src
    assert '"/store/model_7/v1"' in src
    # Delegates to the runtime entry point, which loads the pinned bundle
    # and verifies its sha256 (the run subprocess has no DB). This used to
    # assert `load_surrogate_model_version` — that call moved *inside*
    # `run_surrogate_tool`, so the old assertion pinned an implementation
    # detail rather than the contract.
    assert "run_surrogate_tool" in src
    assert "do not edit" in src
    # The v2 tool contract: read inputs/inputs.json, write into outputs/.
    # The orchestrator names the output *directory*; `run_surrogate_tool`
    # writes outputs.json inside it, so asserting the filename here would
    # again be pinning something that lives on the other side of the call.
    assert "inputs/inputs.json" in src
    assert 'out_dir="outputs"' in src
    assert "def main() -> int:" in src
    compile(src, "surrogate_orchestrator.py", "exec")  # must parse


def test_classify_coverage_all_classes() -> None:
    assert classify_coverage(20.0, 5.0, 40.0) == "in_range"
    assert classify_coverage(6.0, 5.0, 40.0) == "near_edge"  # within 10% of the low edge
    assert classify_coverage(39.0, 5.0, 40.0) == "near_edge"  # within 10% of the high edge
    assert classify_coverage(50.0, 5.0, 40.0) == "extrapolation"  # 10 over, span 35
    assert classify_coverage(100.0, 5.0, 40.0) == "far_extrapolation"  # 60 over > 0.5*35
    assert classify_coverage(-20.0, 5.0, 40.0) == "far_extrapolation"


def test_classify_coverage_zero_span() -> None:
    assert classify_coverage(5.0, 5.0, 5.0) == "in_range"
    assert classify_coverage(6.0, 5.0, 5.0) == "far_extrapolation"


def test_worst_coverage() -> None:
    assert worst_coverage([]) == "in_range"
    assert worst_coverage(["in_range", "near_edge"]) == "near_edge"
    assert (
        worst_coverage(["near_edge", "far_extrapolation", "extrapolation"]) == "far_extrapolation"
    )


# --- runtime loader + end-to-end orchestrator run (§7.2) ----------------


def _write_bundle(tmp_path: Path):
    """Fit y=2x over 6 points and write a scalar bundle; return (dir, sha)."""
    surr = fit_surrogate({"x": [float(i) for i in range(1, 7)]}, [2.0 * i for i in range(1, 7)])
    bundle_dir = tmp_path / "bundle"
    sha = write_scalar_bundle(
        bundle_dir,
        {"y": surr},
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "y", "kind": "scalar"}],
    )
    return bundle_dir, sha


def test_runtime_loads_and_predicts(tmp_path) -> None:
    bundle_dir, sha = _write_bundle(tmp_path)
    model = load_surrogate_model_version(bundle_path=bundle_dir, expected_sha256=sha)
    assert model.responses == ["y"]
    pred = model.predict({"x": 3.0})
    assert abs(pred["y"] - 6.0) < 1.0  # y = 2x
    assert model.predict_std({"x": 3.0})["y"] is not None  # GP uncertainty


def test_runtime_rejects_corrupt_bundle(tmp_path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    with pytest.raises(ModelBundleCorruptError):
        load_surrogate_model_version(bundle_path=bundle_dir, expected_sha256="0" * 64)


def test_generated_orchestrator_runs_end_to_end(tmp_path) -> None:
    """The synthesised orchestrator reads inputs.json + writes outputs.json (§7.2)."""
    bundle_dir, sha = _write_bundle(tmp_path)
    src = synthesise_orchestrator(
        version_id=1, bundle_path=str(bundle_dir), bundle_sha256=sha, tool_alias="surrogate_demo_1"
    )
    wd = tmp_path / "wd"
    (wd / "inputs").mkdir(parents=True)
    (wd / "inputs" / "inputs.json").write_text(
        json.dumps({"inputs": [{"name": "x", "value": 4.0}]}), encoding="utf-8"
    )

    namespace: dict = {"__name__": "orchestrator"}
    exec(compile(src, "surrogate_orchestrator.py", "exec"), namespace)  # noqa: S102
    cwd = Path.cwd()
    try:
        os.chdir(wd)
        assert namespace["main"]() == 0
    finally:
        os.chdir(cwd)

    doc = json.loads((wd / "outputs" / "outputs.json").read_text(encoding="utf-8"))
    by_name = {o["name"]: o["value"] for o in doc["outputs"]}
    assert abs(by_name["y"] - 8.0) < 1.0  # y = 2x at x=4
    assert "y_std" in by_name  # GP uncertainty sibling written

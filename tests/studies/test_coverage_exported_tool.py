"""The guard inside an exported surrogate tool (ADR-0020 §8.1, slice 6).

**The case that matters most.** An exported surrogate runs unattended inside a
workflow, where nobody is watching a badge — so the guard has to reach the
job's ``outputs.json`` as declared outputs, not as a UI decoration.

Two outputs, and the split is deliberate:

- ``support_sigma`` is **numeric**, because ADR-0017 output validity operates
  on numeric scalars and field arrays, **not** status strings. Only a number
  can carry a declared maximum and flag a runaway extrapolation automatically.
- ``support_status`` is a string, for a human reading the job record.

Adding outputs changes the synthesised descriptor, so an existing exported
tool gains them only on **re-export** — a new ``ToolVersion`` through the
normal ``test → released`` gate. Existing exports keep working untouched.

There is already a second extrapolation control here: ``block_extrapolation``
(ADR-0008 §7.1) *tightens the input bounds* so form validation hard-rejects.
The two are complementary — that one prevents a submission, this one reports
what a submission turned out to be — but they must not disagree about what
"the range" is.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from matofx.studies.coverage import (
    CoverageReport,
    FactorOverride,
    analyse_design,
    build_envelope,
    build_report,
    resolve_points,
)
from matofx.tools.descriptor import ToolDescriptor
from tests.studies._coverage_fixtures import fit, training, two_factor_rows

_NOW = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)


def _report_json(**overrides) -> dict:
    rows = two_factor_rows(5, 5)  # thickness / heat_load over [1, 9]
    points = resolve_points(training(rows))
    envelope = build_envelope(points, overrides=overrides or None)
    return build_report(
        analyse_design(points, envelope),
        envelope=envelope,
        source_kind="cohort",
        computed_at=_NOW,
    ).to_json_obj()


def _descriptor(*, report: dict | None = None, responses=None, **kwargs):
    from matofx.models.tool_synthesis import synthesise_descriptor  # noqa: PLC0415

    return synthesise_descriptor(
        source_tool_alias="bracket_stress",
        model_id=1,
        version_id=1,
        name="Surrogate probe",
        description="a synthesised surrogate tool",
        factors=[
            {"name": "thickness", "min": 1.0, "max": 9.0},
            {"name": "heat_load", "min": 1.0, "max": 9.0},
        ],
        responses=responses or [{"name": "mass", "kind": "scalar", "label": "Mass", "unit": "kg"}],
        version_ordinal=1,
        coverage_report=report,
        **kwargs,
    )


# --- §8.1 the synthesised descriptor -------------------------------------


def test_a_re_export_declares_both_support_outputs() -> None:
    names = {o.name: o for o in _descriptor(report=_report_json()).outputs}
    assert names["support_sigma"].type == "number"
    assert names["support_status"].type == "string"


def test_the_descriptor_still_validates_with_a_string_output_beside_a_numeric() -> None:
    """§19 asserted this without verifying it; ``OutputFieldType`` allows both.

    A synthesised descriptor is constructed rather than hand-written, so a type
    the model rejected would have surfaced only at export time on a customer
    install.
    """
    descriptor = _descriptor(report=_report_json())
    assert ToolDescriptor.model_validate(json.loads(descriptor.model_dump_json()))


def test_an_export_without_a_coverage_report_declares_neither() -> None:
    # A version fitted before coverage existed has no report; its export must
    # not promise outputs the orchestrator cannot produce.
    names = {o.name for o in _descriptor().outputs}
    assert "support_sigma" not in names
    assert "support_status" not in names


def test_the_support_outputs_do_not_collide_with_a_response_of_the_same_name() -> None:
    # A tool whose own response is called `support_status` would otherwise
    # produce two outputs sharing one name.
    descriptor = _descriptor(
        report=_report_json(),
        responses=[{"name": "support_status", "kind": "scalar", "label": "Support", "unit": ""}],
    )
    names = [o.name for o in descriptor.outputs]
    assert len(names) == len(set(names)), names


def test_the_responses_own_outputs_are_untouched() -> None:
    names = {o.name for o in _descriptor(report=_report_json()).outputs}
    assert "mass" in names
    assert "mass_std" in names


# --- the two extrapolation controls must agree on "the range" ------------


def test_blocking_extrapolation_uses_the_declared_envelope_not_the_raw_range() -> None:
    """The user narrowed the range they trust; the hard gate must honour that.

    ``block_extrapolation`` predates coverage and tightened inputs to the raw
    *training* range. Once an envelope is declared — and narrowing it is the
    expected edit (§1.3) — two controls disagreeing about what "the range" is
    would let the form accept a point the guard then reports as ``outside``.
    """
    narrowed = _report_json(thickness=FactorOverride(lo=3.0, hi=6.0))
    descriptor = _descriptor(report=narrowed, block_extrapolation=True)
    thickness = next(i for i in descriptor.inputs if i.name == "thickness")
    assert (thickness.minimum, thickness.maximum) == (3.0, 6.0)


def test_without_blocking_the_inputs_stay_unbounded() -> None:
    # Advisory by default (ADR-0008 non-negotiable #8): the user may
    # extrapolate; the guard reports it rather than preventing it.
    descriptor = _descriptor(report=_report_json(), block_extrapolation=False)
    thickness = next(i for i in descriptor.inputs if i.name == "thickness")
    assert thickness.minimum is None
    assert thickness.maximum is None


# --- the orchestrator writes them ----------------------------------------


def _bundle(tmp_path, report=None):
    from matofx.models.bundle import write_scalar_bundle  # noqa: PLC0415

    rows = two_factor_rows(5, 5)
    surrogate = fit(rows, [r["thickness"] * 2.0 for r in rows])
    bundle = tmp_path / "bundle"
    sha = write_scalar_bundle(
        bundle,
        {"mass": surrogate},
        factors=[{"name": n} for n in surrogate.factors],
        responses=[{"name": "mass", "kind": "scalar"}],
        coverage_report=report,
    )
    return bundle, sha


def _run(tmp_path, bundle, sha, point, name="out"):
    from matofx.models.runtime import run_surrogate_tool  # noqa: PLC0415

    out = tmp_path / name
    run_surrogate_tool(
        bundle_path=bundle,
        expected_sha256=sha,
        version_id=1,
        tool_alias="surrogate_probe",
        point=point,
        out_dir=out,
    )
    payload = json.loads((out / "outputs.json").read_text(encoding="utf-8"))
    return {o["name"]: o for o in payload["outputs"]}


def test_a_run_inside_the_range_reports_inside(tmp_path) -> None:
    bundle, sha = _bundle(tmp_path, _report_json())
    outputs = _run(tmp_path, bundle, sha, {"thickness": 5.0, "heat_load": 5.0})

    assert outputs["support_status"]["value"] == "inside"
    assert isinstance(outputs["support_sigma"]["value"], float)


def test_a_run_outside_the_range_reports_outside(tmp_path) -> None:
    # The unattended case: nobody is watching, so this has to land in the job
    # record on its own.
    bundle, sha = _bundle(tmp_path, _report_json())
    outputs = _run(tmp_path, bundle, sha, {"thickness": 400.0, "heat_load": 5.0})
    assert outputs["support_status"]["value"] == "outside"


def test_support_sigma_is_a_number_so_validity_rules_can_bound_it(tmp_path) -> None:
    # §8.1: ADR-0017 works on numbers, never on status strings — which is the
    # whole reason a numeric output exists beside the readable one.
    bundle, sha = _bundle(tmp_path, _report_json())
    outputs = _run(tmp_path, bundle, sha, {"thickness": 5.0, "heat_load": 5.0})

    assert outputs["support_sigma"]["type"] == "number"
    assert float(outputs["support_sigma"]["value"]) >= 0.0


def test_sigma_grows_when_the_point_moves_away_from_the_runs(tmp_path) -> None:
    bundle, sha = _bundle(tmp_path, _report_json())
    near = _run(tmp_path, bundle, sha, {"thickness": 5.0, "heat_load": 5.0}, name="near")
    far = _run(tmp_path, bundle, sha, {"thickness": 200.0, "heat_load": 200.0}, name="far")
    assert far["support_sigma"]["value"] > near["support_sigma"]["value"]


def test_an_existing_export_without_a_report_still_runs(tmp_path) -> None:
    # "Existing exported tools are unaffected and keep working."
    bundle, sha = _bundle(tmp_path, report=None)
    outputs = _run(tmp_path, bundle, sha, {"thickness": 5.0, "heat_load": 5.0})

    assert "mass" in outputs
    assert "support_status" not in outputs
    assert "support_sigma" not in outputs


def test_the_predicted_response_is_still_produced_when_outside(tmp_path) -> None:
    # The guard annotates; it does not refuse. A user may knowingly predict
    # outside the range — they may not do so unknowingly.
    bundle, sha = _bundle(tmp_path, _report_json())
    outputs = _run(tmp_path, bundle, sha, {"thickness": 400.0, "heat_load": 5.0})

    assert "mass" in outputs
    assert outputs["support_status"]["value"] == "outside"


def test_the_baked_report_round_trips_out_of_the_bundle(tmp_path) -> None:
    from matofx.models.bundle import load_coverage_report  # noqa: PLC0415

    bundle, _sha = _bundle(tmp_path, _report_json())
    stored = load_coverage_report(bundle)
    assert stored is not None
    axis = CoverageReport.from_json_obj(stored).envelope.by_name("thickness")
    assert axis.hi == pytest.approx(9.0)


def test_a_narrowed_envelope_is_what_the_exported_tool_enforces(tmp_path) -> None:
    bundle, sha = _bundle(tmp_path, _report_json(thickness=FactorOverride(lo=3.0, hi=6.0)))
    inside = _run(tmp_path, bundle, sha, {"thickness": 4.0, "heat_load": 5.0}, name="in")
    outside = _run(tmp_path, bundle, sha, {"thickness": 8.0, "heat_load": 5.0}, name="out2")

    assert inside["support_status"]["value"] == "inside"
    assert outside["support_status"]["value"] == "outside"

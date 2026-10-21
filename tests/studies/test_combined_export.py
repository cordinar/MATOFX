"""Exported combined/field surrogate tool: synthesis + runtime (ADR-0018 slice 5).

Pure on-disk (no DB): a combined bundle is written, then ``run_surrogate_tool``
(the runtime the synthesised orchestrator calls) predicts the scalars and
reconstructs + writes the field file(s) alongside ``outputs.json`` — the v2
tool output contract. Also checks the synthesised descriptor declares a field
output and the orchestrator calls the unified runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from matofx.models.bundle import (
    load_coverage_report,
    write_combined_bundle,
    write_field_bundle,
    write_scalar_bundle,
)
from matofx.models.runtime import run_surrogate_tool
from matofx.models.tool_synthesis import synthesise_descriptor, synthesise_orchestrator
from matofx.studies.surrogate import fit_surrogate
from matofx.studies.surrogate.field import fit_field_surrogate
from matofx.tools import OutputsDocument

_N_NODES = 20
_XS = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


def _scalar():
    return fit_surrogate({"x": _XS}, [2.0 * x for x in _XS], kind="gp")


def _field():
    t = np.linspace(0.0, np.pi, _N_NODES)
    mode1, mode2 = np.sin(t), np.cos(t)
    return fit_field_surrogate([x * mode1 + 0.5 * x * mode2 for x in _XS], {"x": _XS})


def _outputs_by_name(out_dir: Path) -> dict[str, dict]:
    doc = json.loads((out_dir / "outputs.json").read_text(encoding="utf-8"))
    return {o["name"]: o for o in doc["outputs"]}


def test_run_surrogate_tool_combined_npy(tmp_path: Path) -> None:
    """A combined bundle predicts the scalar + writes an npy field via the runtime."""
    bundle = tmp_path / "bundle"
    sha = write_combined_bundle(
        bundle,
        {"peak": _scalar()},
        {"temperature": _field()},
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "peak", "kind": "scalar"}, {"name": "temperature", "kind": "field"}],
        field_formats={"temperature": "npy"},
    )
    out = tmp_path / "outputs"
    rc = run_surrogate_tool(
        bundle_path=str(bundle),
        expected_sha256=sha,
        version_id=1,
        tool_alias="surrogate_x_1",
        point={"x": 3.0},
        out_dir=str(out),
    )
    assert rc == 0
    outs = _outputs_by_name(out)
    # Scalar response (+ GP std) present as number outputs.
    assert outs["peak"]["type"] == "number"
    assert "peak_std" in outs
    # Field response written as a file output + the npy on disk. The field
    # METADATA (file_type / field_format / field_node_array_name) belongs to the
    # descriptor, not this document — ``OutputValue`` forbids those keys.
    assert outs["temperature"]["type"] == "file"
    assert outs["temperature"]["filename"] == "outputs/temperature.npy"
    assert "file_type" not in outs["temperature"]
    arr = np.load(out / "temperature.npy")
    assert arr.shape[0] == _N_NODES


def test_run_surrogate_tool_writes_vtp_field(tmp_path: Path) -> None:
    """A geometry-bearing field is written as the reference mesh + the field array."""
    import pyvista as pv  # noqa: PLC0415

    mesh_src = tmp_path / "ref.vtp"
    pts = np.column_stack([np.linspace(0.0, 1.0, _N_NODES), np.zeros(_N_NODES), np.zeros(_N_NODES)])
    pv.PolyData(pts).save(mesh_src)

    bundle = tmp_path / "bundle"
    sha = write_combined_bundle(
        bundle,
        {},
        {"temperature": _field()},
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "temperature", "kind": "field"}],
        field_formats={"temperature": "vtp"},
        reference_meshes={"temperature": mesh_src},
    )
    out = tmp_path / "outputs"
    run_surrogate_tool(
        bundle_path=str(bundle),
        expected_sha256=sha,
        version_id=1,
        tool_alias="surrogate_x_1",
        point={"x": 3.0},
        out_dir=str(out),
    )
    outs = _outputs_by_name(out)
    assert outs["temperature"]["filename"] == "outputs/temperature.vtp"
    written = pv.read(str(out / "temperature.vtp"))
    assert written.n_points == _N_NODES
    assert "temperature" in written.point_data
    assert written.point_data["temperature"].shape[0] == _N_NODES


def test_run_surrogate_tool_drops_training_run_arrays(tmp_path: Path) -> None:
    """The written mesh carries the prediction only, never the reference run's data.

    Regression: the reference mesh is a training run's output, so its solver
    ``temperature`` array rode along beside the ``temperature_field``
    prediction — the same values whatever the inputs, and the one ParaView
    or a by-name consumer would read.
    """
    import pyvista as pv  # noqa: PLC0415

    mesh_src = tmp_path / "ref.vtp"
    pts = np.column_stack([np.linspace(0.0, 1.0, _N_NODES), np.zeros(_N_NODES), np.zeros(_N_NODES)])
    ref = pv.PolyData(pts)
    ref.point_data["temperature"] = np.full(_N_NODES, 999.0)
    ref.cell_data["region"] = np.zeros(ref.n_cells)
    ref.field_data["solver_version"] = ["stale"]
    ref.save(mesh_src)

    bundle = tmp_path / "bundle"
    sha = write_combined_bundle(
        bundle,
        {},
        {"temperature_field": _field()},
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "temperature_field", "kind": "field"}],
        field_formats={"temperature_field": "vtp"},
        reference_meshes={"temperature_field": mesh_src},
    )
    out = tmp_path / "outputs"
    run_surrogate_tool(
        bundle_path=str(bundle),
        expected_sha256=sha,
        version_id=1,
        tool_alias="surrogate_x_1",
        point={"x": 3.0},
        out_dir=str(out),
    )
    written = pv.read(str(out / "temperature_field.vtp"))
    assert list(written.point_data.keys()) == ["temperature_field"]
    assert len(written.cell_data.keys()) == 0
    assert len(written.field_data.keys()) == 0
    assert float(np.max(written.point_data["temperature_field"])) < 999.0


def test_synthesise_descriptor_emits_field_output() -> None:
    """A field response becomes a field-file output; scalars stay number outputs."""
    descriptor = synthesise_descriptor(
        source_tool_alias="bracket",
        model_id=7,
        version_id=42,
        name="Combined",
        description="",
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[
            {"name": "peak", "kind": "scalar"},
            {"name": "failure_field", "kind": "field", "file_format": "vtu"},
        ],
        version_ordinal=1,
        with_std=False,
    )
    by_name = {o.name: o for o in descriptor.outputs}
    assert by_name["peak"].type == "number"
    fld = by_name["failure_field"]
    assert fld.type == "file"
    assert fld.file_type == "field"
    assert fld.field_format == "vtu"
    assert fld.filename == "outputs/failure_field.vtu"
    assert fld.field_node_array_name == "failure_field"


def test_synthesised_orchestrator_calls_unified_runtime() -> None:
    src = synthesise_orchestrator(
        version_id=42, bundle_path="/x", bundle_sha256="abc", tool_alias="surrogate_bracket_7"
    )
    assert "run_surrogate_tool" in src
    assert "from matofx.models.runtime import run_surrogate_tool" in src


# --- the runner must be able to PARSE what the orchestrator writes ---------
#
# Every assertion above reads ``outputs.json`` as raw JSON, which is how a
# document the runner rejects went unnoticed: the orchestrator wrote field
# metadata keys (``file_type`` / ``field_format`` / ``field_node_array_name``)
# that ``OutputValue`` forbids, so ``OutputsDocument`` validation failed, the
# runner recorded an empty result, and every optimisation evaluation against an
# exported surrogate was silently dropped — a completed multi-objective study
# with an empty Pareto front and no error anywhere.


def _validated(out_dir: Path) -> OutputsDocument:
    """Parse exactly as the runner does. Raises if the document is illegal."""
    raw = json.loads((out_dir / "outputs.json").read_text(encoding="utf-8"))
    return OutputsDocument.model_validate(raw)


def _coverage_report() -> dict:
    """A minimal ADR-0020 report with a one-factor envelope."""
    return {
        "schema": 1,
        "verdict": "good",
        "envelope": [{"name": "x", "kind": "continuous", "scale": "linear", "lo": 1.0, "hi": 6.0}],
    }


def test_combined_outputs_document_validates(tmp_path: Path) -> None:
    """A combined (scalar + field) prediction must parse as an OutputsDocument."""
    bundle = tmp_path / "bundle"
    sha = write_combined_bundle(
        bundle,
        {"peak": _scalar()},
        {"temperature": _field()},
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "peak", "kind": "scalar"}, {"name": "temperature", "kind": "field"}],
        field_formats={"temperature": "npy"},
    )
    out = tmp_path / "outputs"
    run_surrogate_tool(
        bundle_path=str(bundle),
        expected_sha256=sha,
        version_id=1,
        tool_alias="surrogate_x_1",
        point={"x": 3.0},
        out_dir=str(out),
    )
    doc = _validated(out)
    by_name = {o.name: o for o in doc.outputs}
    # The scalars the optimiser reads must survive the round-trip.
    assert isinstance(by_name["peak"].value, float)
    assert by_name["temperature"].filename == "outputs/temperature.npy"


def test_scalar_outputs_document_validates_with_support(tmp_path: Path) -> None:
    """With a coverage report the support outputs appear AND stay parseable.

    ``support_status`` previously carried a ``note`` key, which would have failed
    the same validation and taken every scalar in the document down with it.
    """
    bundle = tmp_path / "bundle"
    sha = write_scalar_bundle(
        bundle,
        {"peak": _scalar()},
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "peak", "kind": "scalar"}],
        coverage_report=_coverage_report(),
    )
    out = tmp_path / "outputs"
    run_surrogate_tool(
        bundle_path=str(bundle),
        expected_sha256=sha,
        version_id=1,
        tool_alias="surrogate_x_1",
        point={"x": 3.0},
        out_dir=str(out),
    )
    by_name = {o.name: o for o in _validated(out).outputs}
    assert "support_status" in by_name
    assert isinstance(by_name["support_status"].value, str)


def test_combined_and_field_bundles_carry_the_coverage_report(tmp_path: Path) -> None:
    """Every bundle kind must bake in the envelope, not just ``scalar``.

    Descriptor synthesis reads the report from the database and declares
    ``support_status`` / ``support_sigma`` for any kind. If the bundle does not
    carry it, the orchestrator cannot evaluate support and the exported tool
    declares two outputs it never writes.
    """
    report = _coverage_report()

    combined = tmp_path / "combined"
    write_combined_bundle(
        combined,
        {"peak": _scalar()},
        {"temperature": _field()},
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "peak", "kind": "scalar"}, {"name": "temperature", "kind": "field"}],
        field_formats={"temperature": "npy"},
        coverage_report=report,
    )
    assert load_coverage_report(combined) == report

    field_only = tmp_path / "field_only"
    write_field_bundle(
        field_only,
        _field(),
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "temperature", "kind": "field"}],
        file_format="npy",
        coverage_report=report,
    )
    assert load_coverage_report(field_only) == report


def test_combined_export_emits_support_outputs(tmp_path: Path) -> None:
    """The end-to-end consequence: a combined export can now report its support."""
    bundle = tmp_path / "bundle"
    sha = write_combined_bundle(
        bundle,
        {"peak": _scalar()},
        {"temperature": _field()},
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "peak", "kind": "scalar"}, {"name": "temperature", "kind": "field"}],
        field_formats={"temperature": "npy"},
        coverage_report=_coverage_report(),
    )
    out = tmp_path / "outputs"
    run_surrogate_tool(
        bundle_path=str(bundle),
        expected_sha256=sha,
        version_id=1,
        tool_alias="surrogate_x_1",
        point={"x": 99.0},  # far outside the 1..6 envelope
        out_dir=str(out),
    )
    by_name = {o.name: o for o in _validated(out).outputs}
    assert "support_status" in by_name
    assert "outside" in str(by_name["support_status"].value).lower()

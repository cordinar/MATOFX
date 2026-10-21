"""End-to-end smoke tests for the bundled example tools.

Each test:

1. Loads the descriptor via :func:`matofx.tools.load_descriptor`.
2. Builds a real :class:`InputsDocument` with sample values that
   satisfy the descriptor's constraints.
3. Stages a working directory via :func:`stage_working_dir`.
4. Submits the launcher via :class:`LocalTarget` and polls the
   heartbeat until terminal state.
5. Validates the launcher wrote a parseable :class:`OutputsDocument`
   under ``outputs/outputs.json`` and that every declared output is
   present in the document.

This exercises the descriptor → staging → launcher → tool →
heartbeat round-trip end-to-end on a real tool, not a stub.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from matofx.tools import (
    InputsDocument,
    LocalTarget,
    OutputsDocument,
    ResourceRequest,
    StagedWorkingDir,
    stage_working_dir,
)
from matofx.tools.descriptor import ToolDescriptor
from matofx.tools.loader import load_descriptor

# Bundled example tools live next to the matofx project root.
_BUNDLES_DIR: Path = Path(__file__).resolve().parents[2] / "example_tools"


def _staged_for(
    bundle: Path,
    descriptor: ToolDescriptor,
    *,
    tmp_path: Path,
    inputs: list[dict[str, Any]],
) -> StagedWorkingDir:
    mount = tmp_path / "mount"
    mount.mkdir()
    inputs_doc = InputsDocument(
        job_id=1,
        tool_alias=descriptor.alias,
        tool_version=descriptor.version,
        inputs=inputs,  # type: ignore[arg-type]  # pydantic coerces dicts → InputValue
    )
    return stage_working_dir(
        mount_path=str(mount),
        working_dir_relative=f"jobs/{descriptor.alias}",
        bundle_path=str(bundle),
        inputs_document=inputs_doc,
        interpreter_path=sys.executable,
        main_script_relative=descriptor.main_script,
        cancel_grace_seconds=5,
    )


async def _run_to_terminal(
    staged: StagedWorkingDir,
    *,
    timeout_s: float = 90.0,
) -> str:
    target = LocalTarget()
    submit = await target.submit(staged=staged, resource_request=ResourceRequest())
    assert submit.pid is not None
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        observation = await target.poll(staged=staged)
        if observation.heartbeat_state in ("complete", "failed"):
            assert observation.heartbeat_state is not None
            return observation.heartbeat_state
        await asyncio.sleep(0.1)
    pytest.fail(f"job did not reach a terminal heartbeat within {timeout_s} s")


def _outputs_document_for(staged: StagedWorkingDir) -> OutputsDocument:
    payload = json.loads((staged.outputs_dir / "outputs.json").read_text(encoding="utf-8"))
    return OutputsDocument.model_validate(payload)


# --- material_properties (pure stdlib) -------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_material_properties_runs_end_to_end(tmp_path: Path) -> None:
    bundle = _BUNDLES_DIR / "material_properties"
    descriptor = load_descriptor(bundle)
    assert descriptor.alias == "material_properties"

    staged = _staged_for(
        bundle,
        descriptor,
        tmp_path=tmp_path,
        inputs=[
            {
                "name": "component_1",
                "label": "Component 1",
                "type": "select",
                "value": "Component A",
            },
            {
                "name": "component_2",
                "label": "Component 2",
                "type": "select",
                "value": "Component C",
            },
            {
                "name": "component_3",
                "label": "Component 3",
                "type": "select",
                "value": "Component E",
            },
            {
                "name": "component_1_percent",
                "label": "Percentage 1",
                "type": "float",
                "unit": "%",
                "value": 50.0,
            },
            {
                "name": "component_2_percent",
                "label": "Percentage 2",
                "type": "float",
                "unit": "%",
                "value": 30.0,
            },
            {
                "name": "component_3_percent",
                "label": "Percentage 3",
                "type": "float",
                "unit": "%",
                "value": 20.0,
            },
        ],
    )

    final = await _run_to_terminal(staged)
    assert final == "complete"

    doc = _outputs_document_for(staged)
    assert doc.tool_alias == "material_properties"
    by_name = {o.name: o for o in doc.outputs}
    assert {"thermal_conductivity", "heat_capacity"} <= by_name.keys()
    # Hand-checked: 0.5 * 0.5 + 1.0 * 0.3 + 0.625 * 0.2 = 0.675 W/m/K
    assert by_name["thermal_conductivity"].value == pytest.approx(0.675, abs=1e-3)
    # 1500 * 0.5 + 1740 * 0.3 + 1950 * 0.2 = 1662 J/kg/K
    assert by_name["heat_capacity"].value == pytest.approx(1662.0, abs=1e-2)


# --- colour_check (Pillow) -------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_colour_check_runs_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    from PIL import Image  # noqa: PLC0415

    bundle = _BUNDLES_DIR / "colour_check"
    descriptor = load_descriptor(bundle)
    assert descriptor.alias == "colour_check"

    # Build a small synthetic BMP: 4 quadrants of distinct colours.
    img_path = tmp_path / "input.bmp"
    img = Image.new("RGB", (10, 10))
    pixels = img.load()
    assert pixels is not None
    for y in range(10):
        for x in range(10):
            if x < 5 and y < 5:
                pixels[x, y] = (255, 0, 0)
            elif x >= 5 and y < 5:
                pixels[x, y] = (0, 255, 0)
            elif x < 5 and y >= 5:
                pixels[x, y] = (0, 0, 255)
            else:
                pixels[x, y] = (255, 255, 255)
    img.save(img_path)

    staged = _staged_for(
        bundle,
        descriptor,
        tmp_path=tmp_path,
        inputs=[
            {"name": "input_image", "label": "Input image", "type": "file", "value": str(img_path)},
        ],
    )

    final = await _run_to_terminal(staged)
    assert final == "complete"

    doc = _outputs_document_for(staged)
    assert doc.tool_alias == "colour_check"
    by_name = {o.name: o for o in doc.outputs}
    assert by_name["inverted_image"].filename == "transformed_image.bmp"
    assert by_name["colours_list"].filename == "colours_list.csv"
    assert (staged.outputs_dir / "transformed_image.bmp").is_file()
    assert (staged.outputs_dir / "colours_list.csv").is_file()
    # The four input colours each occupy 25 % → all should appear in the CSV.
    csv_text = (staged.outputs_dir / "colours_list.csv").read_text(encoding="utf-8")
    assert csv_text.count("\n") >= 5  # header + 4 rows


# --- heat_transfer (numpy + pandas + plotly) -------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_heat_transfer_runs_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("pandas")
    pytest.importorskip("plotly")

    bundle = _BUNDLES_DIR / "heat_transfer"
    descriptor = load_descriptor(bundle)
    assert descriptor.alias == "heat_transfer"

    # Use the descriptor's defaults but with a very short simulation
    # time so the test finishes quickly. The full simulation grid still
    # exercises the solver, the boundary conditions, and the file
    # outputs — only the wall-clock duration is shrunk.
    staged = _staged_for(
        bundle,
        descriptor,
        tmp_path=tmp_path,
        inputs=[
            {
                "name": "object_temperature",
                "label": "Starting temperature",
                "type": "float",
                "unit": "degC",
                "value": 25.0,
            },
            {"name": "width", "label": "Width", "type": "float", "unit": "m", "value": 0.2},
            {"name": "height", "label": "Height", "type": "float", "unit": "m", "value": 0.1},
            {
                "name": "density_left",
                "label": "Density - left",
                "type": "float",
                "unit": "kg/m3",
                "value": 1000.0,
            },
            {
                "name": "density_right",
                "label": "Density - right",
                "type": "float",
                "unit": "kg/m3",
                "value": 1000.0,
            },
            {
                "name": "thermal_conductivity",
                "label": "Thermal conductivity",
                "type": "float",
                "unit": "W/m/K",
                "value": 0.5,
            },
            {
                "name": "heat_capacity",
                "label": "Heat capacity",
                "type": "float",
                "unit": "J/kg/K",
                "value": 1500.0,
            },
            {
                "name": "air_temperature",
                "label": "Air temperature",
                "type": "float",
                "unit": "degC",
                "value": 120.0,
            },
            {
                "name": "air_speed",
                "label": "Air speed",
                "type": "float",
                "unit": "m/s",
                "value": 2.0,
            },
            {
                "name": "simulation_time",
                "label": "Simulation time",
                "type": "float",
                "unit": "s",
                "value": 10.0,
            },
        ],
    )

    final = await _run_to_terminal(staged, timeout_s=180.0)
    assert final == "complete"

    doc = _outputs_document_for(staged)
    assert doc.tool_alias == "heat_transfer"
    by_name = {o.name: o for o in doc.outputs}
    expected = {
        "T_core",
        "T_0_0",
        "T_0_1",
        "T_1_0",
        "T_1_1",
        "temp_gradient_profile",
        "avg_temp_profile",
        "top_temp_profile",
    }
    assert expected <= by_name.keys()
    # Air is hot (120 C) and the object started at 25 C; after 10 s the
    # corners (closest to the boundary) should be warmer than ambient
    # but well below it.
    for corner in ("T_0_0", "T_0_1", "T_1_0", "T_1_1"):
        v = by_name[corner].value
        assert isinstance(v, int | float)
        assert 25.0 < v < 120.0
    assert (staged.outputs_dir / "temperature_field.html").is_file()
    assert (staged.outputs_dir / "average_temperature_profile.csv").is_file()
    assert (staged.outputs_dir / "top_temperature_profile.csv").is_file()


# --- composite_laminate (numpy + plotly) -----------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_composite_laminate_runs_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("plotly")

    bundle = _BUNDLES_DIR / "composite_laminate"
    descriptor = load_descriptor(bundle)
    assert descriptor.alias == "composite_laminate"

    staged = _staged_for(
        bundle,
        descriptor,
        tmp_path=tmp_path,
        inputs=[
            {"name": "material", "label": "Lamina material", "type": "select", "value": "CFRP"},
            {"name": "theta1", "label": "θ₁", "type": "float", "unit": "deg", "value": 45.0},
            {"name": "theta2", "label": "θ₂", "type": "float", "unit": "deg", "value": 0.0},
            {"name": "thickness", "label": "t", "type": "float", "unit": "mm", "value": 4.0},
        ],
    )

    final = await _run_to_terminal(staged)
    assert final == "complete"

    doc = _outputs_document_for(staged)
    assert doc.tool_alias == "composite_laminate"
    by_name = {o.name: o for o in doc.outputs}
    # Every descriptor-declared output is present in the document.
    assert {o.name for o in descriptor.outputs} <= by_name.keys()
    # Measured CFRP lamina card flows through verbatim (T300/5208).
    assert by_name["e1"].value == pytest.approx(181.0, abs=0.1)
    assert by_name["xt"].value == pytest.approx(1500.0, abs=1.0)
    # Fixed 8-ply stack at 4 mm -> ~49.55 g (rho * midsurface_area * t).
    assert by_name["n_plies"].value == 8
    assert by_name["thickness"].value == pytest.approx(4.0, abs=1e-3)
    assert by_name["mass"].value == pytest.approx(49.55, abs=0.1)
    # Effective stiffness sits between transverse and UD fibre direction.
    assert 10.3 < by_name["ex"].value < 181.0
    assert (staged.outputs_dir / "ply_stack.html").is_file()
    assert (staged.outputs_dir / "polar_modulus.html").is_file()


# --- thermal_soak (scikit-fem transient conduction) ------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_thermal_soak_runs_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("skfem")

    bundle = _BUNDLES_DIR / "thermal_soak"
    descriptor = load_descriptor(bundle)
    assert descriptor.alias == "thermal_soak"

    staged = _staged_for(
        bundle,
        descriptor,
        tmp_path=tmp_path,
        inputs=[
            {"name": "kx", "label": "kx", "type": "float", "value": 4.0},
            {"name": "ky", "label": "ky", "type": "float", "value": 4.0},
            {"name": "kz", "label": "kz", "type": "float", "value": 0.6},
            {"name": "rho", "label": "rho", "type": "float", "value": 1600.0},
            {"name": "cp", "label": "cp", "type": "float", "value": 800.0},
            {"name": "thickness", "label": "t", "type": "float", "value": 4.0},
            {"name": "heat_load", "label": "Q", "type": "float", "value": 6.0},
            {"name": "h_conv", "label": "h", "type": "float", "value": 30.0},
            {"name": "ambient", "label": "amb", "type": "float", "value": 25.0},
            {"name": "duration", "label": "dur", "type": "float", "value": 600.0},
        ],
    )

    final = await _run_to_terminal(staged, timeout_s=120.0)
    assert final == "complete"

    doc = _outputs_document_for(staged)
    assert doc.tool_alias == "thermal_soak"
    by_name = {o.name: o for o in doc.outputs}
    # CFRP baseline soaks to a peak near (but under) the ~120 C resin limit.
    assert 60.0 < by_name["t_max"].value < 150.0
    assert by_name["t_avg"].value < by_name["t_max"].value
    # The wired thermal load + the animated series both land on disk.
    assert (staged.outputs_dir / "temperature_final.vtu").is_file()
    series = sorted(staged.outputs_dir.glob("temperature_step_*.vtu"))
    assert len(series) >= 2


# The production mesh solves in ~21 s by design (`designs/finned-plate-tool.md` R3:
# slow enough that swapping in a millisecond surrogate is a fair comparison). That
# leaves too little headroom under the 60 s global timeout on a loaded machine, so
# this one test gets its own.
@pytest.mark.timeout(180)
@pytest.mark.slow
@pytest.mark.asyncio
async def test_finned_plate_runs_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("skfem")

    bundle = _BUNDLES_DIR / "finned_plate"
    descriptor = load_descriptor(bundle)
    assert descriptor.alias == "finned_plate"

    staged = _staged_for(
        bundle,
        descriptor,
        tmp_path=tmp_path,
        inputs=[
            {"name": "fin_count", "label": "N", "type": "integer", "value": 16},
            {"name": "fin_height", "label": "H", "type": "float", "value": 20.0},
            {"name": "base_thickness", "label": "t", "type": "float", "value": 6.0},
            {"name": "fan_duty", "label": "duty", "type": "float", "value": 70.0},
            {"name": "material", "label": "mat", "type": "select", "value": "Aluminium"},
        ],
    )

    final = await _run_to_terminal(staged, timeout_s=300.0)
    assert final == "complete"

    doc = _outputs_document_for(staged)
    assert doc.tool_alias == "finned_plate"
    by_name = {o.name: o for o in doc.outputs}
    # A mid-box aluminium design sits comfortably under the 85 degC limit.
    assert 35.0 < by_name["t_source"].value < 85.0
    assert by_name["t_mean"].value < by_name["t_source"].value
    assert 100.0 < by_name["mass"].value < 400.0
    # Velocity is DERIVED from the fan operating point, never set by the user.
    assert by_name["air_velocity"].value > 0.0
    assert by_name["fan_power"].value > 0.0
    # The solved field and the design picture both land on disk.
    assert (staged.outputs_dir / "temperature_field.vtu").is_file()
    assert (staged.outputs_dir / "design_schematic.png").is_file()

    # Every output the descriptor DECLARES must actually be emitted, and every file
    # output must exist. A declared-but-unwritten output renders as a blank panel in
    # the UI rather than raising, so nothing else catches it.
    declared = {o.name for o in descriptor.outputs}
    assert declared == set(by_name), f"declared != emitted: {declared ^ set(by_name)}"
    for output in descriptor.outputs:
        if output.type == "file":
            emitted = by_name[output.name].filename
            assert emitted is not None
            assert (staged.working_dir / emitted).is_file(), f"{output.name} not written"


# --- showcase chain: wired, yet each tool still runs alone ------------


def test_showcase_tools_ship_wired_and_still_run_alone() -> None:
    """The composite chain ships pre-wired: Laminate → Soak → Stress.

    A fresh install gets the coupled workflow without hand-wiring. Each link
    must be consistent on both sides — the upstream lists the downstream in
    ``downstream_tools`` and the wired output really exists upstream — and
    every wired input keeps a default, so each tool still runs stand-alone
    (the single-tool run form ignores the wiring).
    """
    aliases = ("composite_laminate", "thermal_soak", "bracket_stress")
    descs = {alias: load_descriptor(_BUNDLES_DIR / alias) for alias in aliases}
    assert descs["composite_laminate"].downstream_tools == ["thermal_soak", "bracket_stress"]
    assert descs["thermal_soak"].downstream_tools == ["bracket_stress"]
    wired_counts = {}
    for alias, desc in descs.items():
        wired = [f for f in desc.inputs if f.downstream_input]
        wired_counts[alias] = len(wired)
        for f in wired:
            upstream = descs[f.upstream_tool]
            assert alias in upstream.downstream_tools, f"{f.upstream_tool} doesn't list {alias}"
            assert f.upstream_output in {o.name for o in upstream.outputs}, (alias, f.name)
            if f.type != "file":
                assert f.default is not None, f"{alias}.{f.name} has no stand-alone default"
    assert wired_counts == {"composite_laminate": 0, "thermal_soak": 6, "bracket_stress": 16}


# --- descriptor-only tests (no run) ----------------------------------


@pytest.mark.parametrize(
    "alias",
    [
        "material_properties",
        "colour_check",
        "heat_transfer",
        "composite_laminate",
        "thermal_soak",
        "bracket_stress",
        "finned_plate",
    ],
)
def test_example_descriptor_loads_and_validates(alias: str) -> None:
    """Each example bundle's descriptor parses + validates without errors."""
    descriptor = load_descriptor(_BUNDLES_DIR / alias)
    assert descriptor.alias == alias
    assert descriptor.version
    assert descriptor.main_script
    assert descriptor.execution_backend.kind == "subprocess"
    assert descriptor.resources.gpu_memory_mb == 0  # no GPU on these demos

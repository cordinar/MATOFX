"""Batch Mixing — central orchestrator (the MATOFX tool ``main_script``).

Runs in the job working directory (``inputs/inputs.json`` in, ``outputs/`` out),
exactly like the pure-Python example tools — but here the "compute" is
orchestration across the Windows<->WSL boundary (designs/cstr-mixing-demo.md §10):

    inputs.json
      -> FreeCAD (geometry)         : build_geometry.py  -> STLs + geometry_meta
      -> geometry preview            : render_preview.py  -> geometry_preview.png
      -> render_case.py              : fill the OpenFOAM case from meta + inputs
      -> run_openfoam.sh (WSL)       : mesh -> matofxMixingFoam (MRF) -> VTK
      -> parse_results.py            : mixing_history.csv + t95/power scalars
                                       + alpha field series (fixed grid)
      -> outputs/outputs.json

Every stage maps failure to a clean error so a bad run is Failed, never a silent
non-physical result (ADR-0017 posture).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import paths  # tool-local config

sys.path.insert(0, str(paths.TOOL_DIR / "openfoam"))
sys.path.insert(0, str(paths.TOOL_DIR / "post"))
import make_animation  # noqa: E402
import parse_results  # noqa: E402
import render_case  # noqa: E402

_T0 = time.time()


def _log(msg: str) -> None:
    """Emit a timestamped progress line to stdout AND a flushed progress file.

    A long CFD run can be killed mid-stage (walltime, cancel); Python buffers
    stdout to the launcher's log so those lines can be lost. We therefore also
    append to ``progress.log`` and fsync it, so the last stage reached is always
    on disk for diagnosis.
    """
    line = "[batch_mixing +%6.1fs] %s" % (time.time() - _T0, msg)
    print(line, flush=True)
    try:
        with open("progress.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass

# Env vars that would leak the MATOFX venv into FreeCAD's *bundled* Python and
# can break it — stripped when launching FreeCADCmd (defensive hygiene).
_FREECAD_STRIP_ENV = {
    "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
    "PYTHONNOUSERSITE", "__PYVENV_LAUNCHER__", "VIRTUAL_ENV",
}

GEOMETRY_KEYS = {
    "tank_diameter", "liquid_height", "impeller_diameter_ratio",
    "impeller_clearance_ratio", "blade_count", "blade_angle_deg", "shaft_diameter",
    "hub_diameter", "blade_width", "blade_thickness", "baffle_count", "baffle_width",
    "baffle_wall_gap", "baffle_thickness", "baffle_bottom", "baffle_top",
    "additive_volume_fraction",
}


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {e["name"]: e["value"] for e in payload["inputs"]}


def _fail(msg: str) -> "NoReturn":  # noqa: F821
    """Abort the job cleanly (MATOFX marks it Failed; no partial outputs.json)."""
    raise SystemExit(f"batch_mixing: {msg}")


def _run(cmd: list[str], *, env: dict | None = None, stage: str) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]
        _fail(f"{stage} failed (exit {proc.returncode}):\n{tail}")
    return proc.stdout or ""


def main() -> int:
    inputs = _read_inputs()
    job = Path.cwd()
    work = job / "work"
    geom = work / "geom"
    case = work / "case"
    out = job / "outputs"
    for d in (geom, out):
        d.mkdir(parents=True, exist_ok=True)
    _log("start: %d inputs (job %s)" % (len(inputs), job.name))

    # --- 1. Geometry (FreeCAD, headless) --------------------------------------
    build_geom = paths.TOOL_DIR / "geometry" / "build_geometry.py"
    if not Path(paths.FREECADCMD).exists():
        _fail(f"FreeCAD not found at {paths.FREECADCMD!r} — set MATOFX_FREECADCMD "
              f"to freecadcmd.exe on this machine.")
    if not build_geom.exists():
        _fail(f"build_geometry.py missing at {build_geom}. The tool bundle is "
              f"incomplete — the geometry/ folder was not installed with the tool.")
    geom_params = {k: inputs[k] for k in GEOMETRY_KEYS if k in inputs}
    geom_params["output_dir"] = str(geom)
    params_path = geom / "geometry_params.json"
    params_path.write_text(json.dumps(geom_params, indent=2), encoding="utf-8")
    # Clean env for FreeCAD's bundled Python (strip venv/PYTHON* leakage) + params.
    fc_env = {k: v for k, v in os.environ.items() if k.upper() not in _FREECAD_STRIP_ENV}
    fc_env["MATOFX_GEOM_PARAMS"] = str(params_path)
    _log("geometry: running FreeCAD (build_geometry.py)")
    proc = subprocess.run(
        [paths.FREECADCMD, str(build_geom)], capture_output=True, text=True, env=fc_env
    )
    meta_path = geom / "geometry_meta.json"
    if not meta_path.exists():
        detail = (
            f"FreeCAD exit {proc.returncode}\n"
            f"--- FreeCAD stdout ---\n{(proc.stdout or '')[-1800:]}\n"
            f"--- FreeCAD stderr ---\n{(proc.stderr or '')[-1800:]}"
        )
        errf = geom / "geometry_error.txt"
        if errf.exists():
            detail += f"\n--- geometry_error.txt ---\n{errf.read_text(encoding='utf-8')[-1800:]}"
        _fail(f"geometry produced no geometry_meta.json.\n{detail}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    _log("geometry: OK (STLs + geometry_meta.json)")

    # --- 2. Geometry preview (venv/matplotlib) --------------------------------
    try:
        _run(
            [sys.executable, str(paths.TOOL_DIR / "geometry" / "render_preview.py"), str(geom)],
            stage="geometry preview",
        )
        (out / "geometry_preview.png").write_bytes((geom / "geometry_preview.png").read_bytes())
        _log("geometry preview: OK")
    except SystemExit:
        _log("geometry preview: skipped (non-fatal)")

    # --- 3. Render the OpenFOAM case ------------------------------------------
    _log("render_case: filling OpenFOAM case from meta + inputs")
    render_case.build_case(
        paths.TOOL_DIR / "openfoam" / "case_template", meta_path, inputs, case, geom
    )
    _log("render_case: OK")

    # --- 4. Run OpenFOAM in WSL (mesh + solve + export) -----------------------
    _log("openfoam: WSL mesh + MRF solve + export starting (the long step; "
         "end_time=%s s, %d procs)" % (inputs.get("end_time", "?"), paths.N_PROCS))
    script = paths.TOOL_DIR / "openfoam" / "run_openfoam.sh"
    result = _run(
        ["wsl.exe", "-d", paths.WSL_DISTRO, "bash", paths.to_wsl(script),
         paths.to_wsl(case), str(paths.N_PROCS)],
        env={**os.environ, "OF_BASHRC": paths.OF_BASHRC},
        stage="OpenFOAM run (WSL)",
    )
    _log("openfoam: finished -> %s" % (result.strip().splitlines()[-1] if result.strip() else "?"))
    if "OK" not in result:
        _fail(f"OpenFOAM run did not report OK:\n{result[-1500:]}")

    vtk_dir = case / "VTK"
    moment = case / "postProcessing" / "impellerForces" / "0" / "moment.dat"
    if not vtk_dir.exists():
        _fail("run produced no VTK snapshots")

    # --- 5. Derived scalars + mixing history ----------------------------------
    rpm = float(inputs.get("impeller_rpm", 120.0))
    omega = rpm * 2.0 * math.pi / 60.0
    threshold = float(inputs.get("homogeneity_threshold", 0.05))
    res = parse_results.parse_run(vtk_dir, moment, omega, threshold)
    parse_results.write_history_csv(res, out / "mixing_history.csv")
    _log("parse: t95=%s power=%.4g W cov_final=%.4g (mixing_history.csv written)"
         % (res["t95"], res["power_avg"], res["cov_final"]))

    # --- 6. Blend animation (MP4 — plays inline via <video controls>) ---------
    anim_ok = False
    try:
        make_animation.make_movie(vtk_dir, out / "blend.mp4", fps=12)
        anim_ok = (out / "blend.mp4").exists()
    except Exception as exc:  # noqa: BLE001 - animation is non-critical
        print(f"animation skipped: {exc}")
    _log("animation: %s" % ("blend.mp4 written" if anim_ok else "skipped"))

    # --- 7. Regime indicator (Richardson number) ------------------------------
    D = float(meta["impeller"]["diameter"])
    H = float(meta["tank"]["liquid_height"])
    rho_b = float(inputs.get("rho_bulk", 1000.0))
    d_rho = abs(float(inputs.get("rho_additive", 1050.0)) - rho_b)
    u_tip = omega * D / 2.0
    richardson = (9.81 * d_rho * H / (rho_b * u_tip ** 2)) if u_tip > 1e-9 else 0.0

    t95 = res["t95"]
    power = res["power_avg"]
    blend_energy = (power * t95) if (t95 and power == power) else float("nan")

    # --- 8. outputs.json ------------------------------------------------------
    def num(v: float | None) -> float | None:
        return None if v is None or (isinstance(v, float) and v != v) else round(float(v), 6)

    outputs = [
        {"name": "t95", "label": "Time to 95% homogeneity", "type": "number",
         "unit": "s", "value": num(t95)},
        {"name": "impeller_power", "label": "Impeller power", "type": "number",
         "unit": "W", "value": num(power)},
        {"name": "blend_energy", "label": "Blend energy", "type": "number",
         "unit": "J", "value": num(blend_energy)},
        {"name": "richardson", "label": "Richardson number", "type": "number",
         "value": num(richardson)},
        {"name": "cov_final", "label": "Final CoV", "type": "number",
         "value": num(res["cov_final"])},
        {"name": "blended_within_horizon", "label": "Blended within horizon",
         "type": "number", "value": 1 if res["blended"] else 0},
        {"name": "mixing_homogeneity", "label": "Homogeneity vs time",
         "type": "file", "filename": "outputs/mixing_history.csv"},
        {"name": "mixing_power", "label": "Impeller power vs time",
         "type": "file", "filename": "outputs/mixing_history.csv"},
    ]
    if anim_ok:
        outputs.append({
            "name": "blend_animation", "label": "Blend animation",
            "type": "file", "filename": "outputs/blend.mp4",
        })
    if (out / "geometry_preview.png").exists():
        outputs.append({
            "name": "geometry_preview", "label": "Tank geometry",
            "type": "file", "filename": "outputs/geometry_preview.png",
        })

    (out / "outputs.json").write_text(
        json.dumps(
            {"schema_version": 1, "tool_alias": "batch_mixing", "outputs": outputs},
            indent=2,
        ),
        encoding="utf-8",
    )
    _log("outputs.json written — DONE (t95=%s power=%.4f W blended=%s)"
         % (t95, power, res["blended"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

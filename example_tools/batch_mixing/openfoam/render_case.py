"""Render a concrete OpenFOAM case from the template + geometry_meta + inputs.

Part of the Batch Mixing tool's OpenFOAM stage (designs/cstr-mixing-demo.md §10).
Runs on the orchestrator side (plain Python, no OpenFOAM needed): it copies the
tokenised ``case_template`` into the run directory, drops the FreeCAD STLs into
``constant/triSurface``, and substitutes every ``@TOKEN@`` from the geometry
metadata (envelope, MRF cylinder, locationInMesh, additive charge) and the user
inputs (fluid properties, rpm, turbulence, run controls). Everything stays in SI
metres, matching the STL export.

Tokens filled:
  geometry  @XMIN@ @XMAX@ @YMIN@ @YMAX@ @ZMIN@ @ZMAX@ @NX@ @NY@ @NZ@
            @MRF_Z1@ @MRF_Z2@ @MRF_R@ @LOC_X@ @LOC_Y@ @LOC_Z@
            @ADD_Z1@ @ADD_Z2@ @ADD_R@
  physics   @OMEGA@ @RHO_ADDITIVE@ @NU_ADDITIVE@ @RHO_BULK@ @NU_BULK@
            @DAB@ @ALPHATAB@ @RAS_MODEL@ @K_INIT@ @EPS_INIT@
  run       @END_TIME@ @DELTA_T@ @WRITE_INTERVAL@ @MAX_DELTAT@
"""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
from pathlib import Path
from typing import Any


def _force_rmtree(path: Path) -> None:
    """rmtree that survives Windows read-only files (OneDrive copies them so)."""
    def _onexc(func: Any, p: str, _exc: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
        except OSError:
            pass
        func(p)

    try:  # py>=3.12
        shutil.rmtree(path, onexc=_onexc)
    except TypeError:  # older signature
        shutil.rmtree(path, onerror=lambda f, p, e: _onexc(f, p, e))

# Input defaults (physics + run + mesh). Every one is overridable from inputs.
DEFAULTS: dict[str, Any] = {
    "impeller_rpm": 120.0,
    "rho_additive": 1050.0,
    "nu_additive": 5e-6,
    "rho_bulk": 1000.0,
    "nu_bulk": 1e-6,
    "turbulent_schmidt": 0.7,      # -> alphatab = 1/Sc_t
    "molecular_diffusivity": 1e-9,  # Dab
    "turbulence_model": "realizableKE",
    "end_time": 30.0,
    "write_interval": 0.5,
    "mesh_base_cell": 0.0125,       # m, background hex size
    "n_subdomains": 8,
}


def _g(v: float) -> str:
    """Format a number for an OpenFOAM dict (compact, no locale surprises)."""
    return f"{v:.8g}"


def compute_tokens(meta: dict[str, Any], inputs: dict[str, Any]) -> dict[str, str]:
    p = {**DEFAULTS, **{k: v for k, v in inputs.items() if v is not None}}

    R = float(meta["tank"]["radius"])
    H = float(meta["tank"]["liquid_height"])
    bb = meta["background_box"]
    base = float(p["mesh_base_cell"])

    xmin, ymin = bb["min"][0], bb["min"][1]
    xmax, ymax = bb["max"][0], bb["max"][1]
    zmin = bb["min"][2]
    zmax = H                                  # top face IS the free-slip lid
    nx = max(1, round((xmax - xmin) / base))
    ny = max(1, round((ymax - ymin) / base))
    nz = max(1, round((zmax - zmin) / base))

    mrf = meta["mrf_zone"]
    loc = meta["location_in_mesh"]
    add = meta["additive_region"]

    rpm = float(p["impeller_rpm"])
    omega = rpm * 2.0 * math.pi / 60.0
    D = float(meta["impeller"]["diameter"])
    u_tip = omega * D / 2.0

    # Turbulence seed from tip speed (I=10%); floored so rpm=0 stays finite.
    k0 = max(1.5 * (0.1 * u_tip) ** 2, 1e-4)
    eps0 = max(0.09 ** 0.75 * k0 ** 1.5 / (0.07 * (2 * R)), 1e-4)

    alphatab = 1.0 / float(p["turbulent_schmidt"])

    return {
        "@XMIN@": _g(xmin), "@XMAX@": _g(xmax),
        "@YMIN@": _g(ymin), "@YMAX@": _g(ymax),
        "@ZMIN@": _g(zmin), "@ZMAX@": _g(zmax),
        "@NX@": str(nx), "@NY@": str(ny), "@NZ@": str(nz),
        "@MRF_Z1@": _g(mrf["point1"][2]), "@MRF_Z2@": _g(mrf["point2"][2]),
        "@MRF_R@": _g(mrf["radius"]),
        "@LOC_X@": _g(loc[0]), "@LOC_Y@": _g(loc[1]), "@LOC_Z@": _g(loc[2]),
        "@ADD_Z1@": _g(add["point1"][2]), "@ADD_Z2@": _g(add["point2"][2]),
        "@ADD_R@": _g(add["radius"]),
        "@OMEGA@": _g(omega),
        "@RHO_ADDITIVE@": _g(float(p["rho_additive"])),
        "@NU_ADDITIVE@": _g(float(p["nu_additive"])),
        "@RHO_BULK@": _g(float(p["rho_bulk"])),
        "@NU_BULK@": _g(float(p["nu_bulk"])),
        "@DAB@": _g(float(p["molecular_diffusivity"])),
        "@ALPHATAB@": _g(alphatab),
        "@RAS_MODEL@": str(p["turbulence_model"]),
        "@K_INIT@": _g(k0), "@EPS_INIT@": _g(eps0),
        "@END_TIME@": _g(float(p["end_time"])),
        "@DELTA_T@": _g(min(5e-4, float(p["end_time"]) / 100.0)),
        "@WRITE_INTERVAL@": _g(float(p["write_interval"])),
        "@MAX_DELTAT@": _g(max(1e-3, float(p["write_interval"]) / 10.0)),
    }, int(p["n_subdomains"])


def _substitute(path: Path, tokens: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    for k, v in tokens.items():
        text = text.replace(k, v)
    path.write_text(text, encoding="utf-8")


def build_case(
    template_dir: str | Path,
    geometry_meta_path: str | Path,
    inputs: dict[str, Any],
    out_case_dir: str | Path,
    stl_dir: str | Path,
) -> Path:
    """Materialise a runnable OpenFOAM case. Returns the case directory."""
    template_dir = Path(template_dir)
    out = Path(out_case_dir)
    if out.exists():
        _force_rmtree(out)
    for sub in ("system", "constant", "0.orig"):
        shutil.copytree(template_dir / sub, out / sub)

    tri = out / "constant" / "triSurface"
    tri.mkdir(parents=True, exist_ok=True)
    for name in ("tankWall", "baffles", "impeller", "shaft"):
        shutil.copy(Path(stl_dir) / f"{name}.stl", tri / f"{name}.stl")

    meta = json.loads(Path(geometry_meta_path).read_text(encoding="utf-8"))
    tokens, n_sub = compute_tokens(meta, inputs)

    # Substitute in every dict text file (skip the STL triSurface payloads).
    for path in out.rglob("*"):
        if path.is_file() and path.suffix != ".stl" and "triSurface" not in path.parts:
            _substitute(path, tokens)

    # decomposeParDict (not a template — depends only on n_subdomains).
    (out / "system" / "decomposeParDict").write_text(
        "FoamFile { version 2.0; format ascii; class dictionary; "
        "object decomposeParDict; }\n"
        f"numberOfSubdomains {n_sub};\nmethod scotch;\n",
        encoding="utf-8",
    )
    return out


def _read_matofx_inputs(inputs_json: str | Path) -> dict[str, Any]:
    """Read a MATOFX inputs.json ({'inputs':[{name,value}]}) into a flat dict."""
    payload = json.loads(Path(inputs_json).read_text(encoding="utf-8"))
    return {e["name"]: e["value"] for e in payload.get("inputs", [])}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Render a Batch Mixing OpenFOAM case.")
    ap.add_argument("--template", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--stl", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--inputs", help="MATOFX inputs.json (optional; else defaults)")
    a = ap.parse_args()
    ins = _read_matofx_inputs(a.inputs) if a.inputs else {}
    case = build_case(a.template, a.meta, ins, a.out, a.stl)
    print("rendered case ->", case)

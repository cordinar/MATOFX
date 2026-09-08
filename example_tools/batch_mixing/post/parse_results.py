"""Post-process a finished Batch Mixing run into MATOFX-native outputs.

Reads the exported alpha snapshots (foamToVTK) + the impeller moment history and
produces:

* ``mixing_history.csv`` — one tidy table the MATOFX ``csv_plot`` renderer turns
  into interactive Plotly charts. Columns (0-indexed for the descriptor):
      0  Time (s)
      1  Homogeneity (%)      100*(1 - min(CoV, 1)); crosses 95 % at t95
      2  CoV                  coefficient of variation of the additive fraction
      3  Impeller power (W)   |torque_z| * omega, interpolated onto the time grid
  Two ``csv_plot`` outputs point at this one file (y = col 1, and y = col 3) —
  the tidy-CSV-with-extra-column pattern, no static PNGs.
* a scalars dict (t95, power, blend energy, final CoV, validity) for outputs.json.

Homogeneity is the reader-friendly curve (rises to ~100 %); CoV is the rigorous
metric retained for the surrogate. Volume-weighted throughout.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv

FIELD = "alpha.additive"


def _cov_at(vtu: str) -> tuple[float, float]:
    """Return (mean, CoV) of the additive fraction, volume-weighted."""
    m = pv.read(vtu)
    if FIELD in m.cell_data:
        a = np.asarray(m.cell_data[FIELD], float)
    else:
        m = m.point_data_to_cell_data()
        a = np.asarray(m.cell_data[FIELD], float)
    m = m.compute_cell_sizes(length=False, area=False, volume=True)
    V = np.asarray(m.cell_data["Volume"], float)
    Vt = V.sum()
    mean = float((V * a).sum() / Vt)
    if mean <= 1e-9:
        return mean, float("nan")
    var = float((V * (a - mean) ** 2).sum() / Vt)
    return mean, float(np.sqrt(var) / mean)


def _power_series(moment_dat: str, omega: float) -> tuple[np.ndarray, np.ndarray]:
    """Parse OpenFOAM moment.dat -> (time, power). power = |moment_z|*omega."""
    ts, pw = [], []
    if not os.path.exists(moment_dat):
        return np.array([]), np.array([])
    for line in open(moment_dat, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        nums = [float(x) for x in line.replace("(", " ").replace(")", " ").split()]
        if len(nums) >= 4:
            ts.append(nums[0])
            pw.append(abs(nums[3]) * omega)   # total moment z-component
    return np.array(ts), np.array(pw)


def parse_run(
    vtk_dir: str | Path,
    moment_dat: str | Path,
    omega: float,
    threshold: float = 0.05,
) -> dict[str, Any]:
    """Build the mixing history + scalar results from a finished run."""
    vtk_dir = Path(vtk_dir)
    series = glob.glob(str(vtk_dir / "*.vtm.series"))
    pairs: list[tuple[float, str]] = []
    if series:
        js = json.loads(Path(series[0]).read_text(encoding="utf-8"))
        for f in js["files"]:
            d = vtk_dir / f["name"].replace(".vtm", "")
            if (d / "internal.vtu").is_file():
                pairs.append((float(f["time"]), str(d / "internal.vtu")))
    pairs.sort()

    times = np.array([t for t, _ in pairs])
    covs = np.array([_cov_at(v) for _, v in pairs])[:, 1] if pairs else np.array([])
    homog = 100.0 * (1.0 - np.minimum(covs, 1.0))

    # power interpolated onto the snapshot time grid
    tp, pw = _power_series(str(moment_dat), omega)
    power = np.interp(times, tp, pw) if len(tp) else np.full_like(times, np.nan)

    # t95: first time homogeneity >= 95 (CoV <= threshold) and it stays there
    t95: float | None = None
    for i in range(len(times)):
        if covs[i] <= threshold and np.all(covs[i:] <= threshold * 1.5):
            t95 = float(times[i])
            break

    developed = tp > tp.max() * 0.3 if len(tp) else np.array([], bool)
    power_avg = float(np.mean(pw[developed])) if developed.any() else float("nan")

    return {
        "times": times, "cov": covs, "homogeneity": homog, "power": power,
        "t95": t95, "power_avg": power_avg,
        "cov_final": float(covs[-1]) if len(covs) else float("nan"),
        "blended": t95 is not None,
    }


def sample_field_series(
    vtk_dir: str | Path,
    meta: dict[str, Any],
    out_dir: str | Path,
    dims: tuple[int, int, int] = (24, 24, 32),
    max_snapshots: int = 25,
) -> list[str]:
    """Resample every alpha snapshot onto a FIXED Cartesian grid over the tank.

    The tank envelope is invariant across runs (only baffles/impeller vary), so a
    grid spanning it is a common basis for all runs — the POD field surrogate can
    then fit across designs (designs/cstr-mixing-demo.md §4), and the same series
    drives the in-app field viewer's time slider. Writes ``alpha_step_NNNN.vtu``.
    """
    vtk_dir = Path(vtk_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    R = float(meta["tank"]["radius"])
    H = float(meta["tank"]["liquid_height"])
    nx, ny, nz = dims
    grid = pv.ImageData(
        dimensions=dims,
        spacing=(2 * R / (nx - 1), 2 * R / (ny - 1), H / (nz - 1)),
        origin=(-R, -R, 0.0),
    )

    series = glob.glob(str(vtk_dir / "*.vtm.series"))
    pairs: list[tuple[float, str]] = []
    if series:
        js = json.loads(Path(series[0]).read_text(encoding="utf-8"))
        for f in js["files"]:
            d = vtk_dir / f["name"].replace(".vtm", "")
            if (d / "internal.vtu").is_file():
                pairs.append((float(f["time"]), str(d / "internal.vtu")))
    pairs.sort()

    # Cap the number of exported snapshots (even stride, keeping both ends): the
    # in-app viewer + POD surrogate need ~20-25 times, not the full solver write
    # count. Fewer/lighter .vtu files keep the field viewer fast to load.
    if len(pairs) > max_snapshots >= 2:
        step = (len(pairs) - 1) / (max_snapshots - 1)
        keep = sorted({round(i * step) for i in range(max_snapshots)})
        pairs = [pairs[i] for i in keep]

    written: list[str] = []
    for i, (_t, vtu) in enumerate(pairs):
        mesh = pv.read(vtu)
        sampled = grid.sample(mesh)              # mesh data -> fixed grid points
        if FIELD not in sampled.point_data:
            continue
        # zero the additive fraction outside the tank (invalid sample points)
        if "vtkValidPointMask" in sampled.point_data:
            mask = np.asarray(sampled.point_data["vtkValidPointMask"], bool)
            a = np.asarray(sampled.point_data[FIELD], float)
            a[~mask] = 0.0
            sampled.point_data[FIELD] = a
        ug = sampled.cast_to_unstructured_grid()
        path = out_dir / f"alpha_step_{i:04d}.vtu"
        ug.save(str(path))
        written.append(str(path))
    return written


def build_viewer_field_series(
    vtk_dir: str | Path,
    out_dir: str | Path,
    max_snapshots: int = 24,
) -> list[str]:
    """Field series for the in-app viewer — a y=0 SLICE of the real mesh.

    The fixed-grid sampler (``sample_field_series``) suits a cross-run POD
    surrogate but renders as a blocky voxel box. For the viewer we instead take a
    planar y=0 slice through the actual reconstructed mesh: it keeps the real mesh
    resolution (no voxel steps), shows the additive blending on the cut plane, and
    is only ~a few thousand nodes per frame — so ~24 frames load in about a second.
    """
    vtk_dir = Path(vtk_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    series = glob.glob(str(vtk_dir / "*.vtm.series"))
    pairs: list[tuple[float, str]] = []
    if series:
        js = json.loads(Path(series[0]).read_text(encoding="utf-8"))
        for f in js["files"]:
            d = vtk_dir / f["name"].replace(".vtm", "")
            if (d / "internal.vtu").is_file():
                pairs.append((float(f["time"]), str(d / "internal.vtu")))
    pairs.sort()
    if len(pairs) > max_snapshots >= 2:
        step = (len(pairs) - 1) / (max_snapshots - 1)
        pairs = [pairs[i] for i in sorted({round(i * step) for i in range(max_snapshots)})]

    zc = None
    written: list[str] = []
    for i, (_t, vtu) in enumerate(pairs):
        mesh = pv.read(vtu)
        if FIELD in mesh.cell_data and FIELD not in mesh.point_data:
            mesh = mesh.cell_data_to_point_data()
        if zc is None:
            zc = (mesh.bounds[4] + mesh.bounds[5]) / 2.0
        surf = mesh.slice(normal="y", origin=(0, 0, zc)).triangulate()
        if surf.n_points == 0 or FIELD not in surf.point_data:
            continue
        path = out_dir / f"alpha_step_{i:04d}.vtu"
        surf.cast_to_unstructured_grid().save(str(path))
        written.append(str(path))
    return written


def write_history_csv(res: dict[str, Any], path: str | Path) -> None:
    """Write the tidy mixing_history.csv (headers become Plotly axis labels)."""
    rows = np.column_stack([res["times"], res["homogeneity"], res["cov"], res["power"]])
    header = "Time (s),Homogeneity (%),CoV,Impeller power (W)"
    np.savetxt(path, rows, delimiter=",", header=header, comments="", fmt="%.6g")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Parse a Batch Mixing run into MATOFX outputs.")
    ap.add_argument("--vtk", required=True, help="dir with foamToVTK time snapshots")
    ap.add_argument("--moment", required=True, help="postProcessing/.../moment.dat")
    ap.add_argument("--omega", type=float, required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--threshold", type=float, default=0.05)
    a = ap.parse_args()
    r = parse_run(a.vtk, a.moment, a.omega, a.threshold)
    write_history_csv(r, a.out_csv)
    print(json.dumps({k: r[k] for k in ("t95", "power_avg", "cov_final", "blended")}, indent=2))
    print("wrote", a.out_csv)

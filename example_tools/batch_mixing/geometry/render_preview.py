"""Render a clean geometry preview PNG from the patch STLs + geometry_meta.json.

This image is surfaced as a **tool output** (an input-guide / results picture), so
it must be legible and un-clipped, not just a dev sketch. Runs under the MATOFX
venv (numpy + matplotlib), separate from the FreeCAD interpreter that writes STLs:

    python render_preview.py <geometry_dir>   # dir holding *.stl + geometry_meta.json

Two panels:
  * a 3-D **perspective** — every patch's triangles merged into ONE
    ``Poly3DCollection`` so matplotlib depth-sorts the whole scene face-by-face
    (rendering patches as separate collections mis-occludes the central impeller);
  * a 2-D **plan** (top-down projection) drawn per patch for clean occlusion and
    uncluttered axes.

Layout notes: matplotlib's ``tight_layout``/``constrained_layout`` both clip 3-D
axes (the reported cut-off tick labels), so we set explicit margins and save with
``bbox_inches="tight"`` — that is what guarantees no text is cropped.
"""

from __future__ import annotations

import glob
import json
import os
import struct
import sys

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import PolyCollection  # noqa: E402
from matplotlib.patches import Circle, Patch  # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402

# MATOFX palette (project_matofx_design_system): navy structure, teal accents.
# (hex, 3-D alpha, 2-D-plan alpha, plan draw order). topLid is skipped in plan so
# it does not cover the interior when viewed from above.
PATCH_STYLE = {
    # plan alpha 0 for the shell patches: the tank is drawn as a clean circle in
    # plan instead of projecting its triangulated wall/floor (whose fan-meshed
    # bottom disc otherwise shows as spurious radial spokes from one rim vertex).
    "tankWall": ("#9fb8c8", 0.22, 0.00, 1),
    "topLid": ("#c8d6df", 0.14, 0.00, 1),
    "baffles": ("#07203F", 0.60, 0.70, 2),
    "shaft": ("#6DCDC4", 0.95, 0.90, 3),
    "impeller": ("#40A1A4", 1.00, 1.00, 4),
}
PATCH_LABEL = {
    "tankWall": "tank wall",
    "topLid": "free-slip lid",
    "baffles": "baffles",
    "shaft": "shaft",
    "impeller": "impeller",
}


def read_stl(path: str) -> np.ndarray:
    """Return an (n, 3, 3) array of triangle vertices from a binary STL."""
    with open(path, "rb") as fh:
        fh.read(80)
        n = struct.unpack("<I", fh.read(4))[0]
        tris = np.empty((n, 3, 3), dtype=np.float64)
        for t in range(n):
            fh.read(12)  # normal
            for v in range(3):
                tris[t, v] = struct.unpack("<3f", fh.read(12))
            fh.read(2)  # attribute byte count
    return tris


def _rgba(hex_color: str, alpha: float):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, alpha)


def load_patches(geo_dir: str):
    """Return an ordered list of (name, tris) for every patch STL present."""
    out = []
    for stl in sorted(glob.glob(os.path.join(geo_dir, "*.stl"))):
        name = os.path.splitext(os.path.basename(stl))[0]
        out.append((name, read_stl(stl)))
    return out


def _trim_ticks(ax, R, H, three_d=True):
    ticks = [-round(R, 2), 0.0, round(R, 2)]
    ax.set_xticks(ticks)
    ax.set_yticks(ticks)
    if three_d:
        ax.set_zticks([0.0, round(H / 2, 2), round(H, 2)])


def main() -> int:
    geo_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    meta = json.load(open(os.path.join(geo_dir, "geometry_meta.json"), encoding="utf-8"))
    R = meta["tank"]["radius"]
    H = meta["tank"]["liquid_height"]
    patches = load_patches(geo_dir)

    fig = plt.figure(figsize=(12, 6.4))

    # ---- Panel 1: 3-D perspective (single merged collection) ----------------
    ax3 = fig.add_subplot(1, 2, 1, projection="3d")
    all_tris, all_fc, all_ec = [], [], []
    for name, tris in patches:
        color, a3, _a2, _z = PATCH_STYLE.get(name, ("#888888", 0.4, 0.4, 1))
        all_tris.append(tris)
        all_fc.extend([_rgba(color, a3)] * len(tris))
        # No wireframe on the shell patches — their fan/strip triangulation would
        # otherwise show as spurious mesh lines. Faint edges only on the solids.
        edge = (0, 0, 0, 0.0) if name in ("tankWall", "topLid") else (0, 0, 0, 0.18)
        all_ec.extend([edge] * len(tris))
    merged = np.concatenate(all_tris, axis=0)
    pc3 = Poly3DCollection(
        merged, facecolors=all_fc, edgecolors=all_ec, linewidths=0.1
    )
    ax3.add_collection3d(pc3)
    ax3.set_xlim(-R * 1.05, R * 1.05)
    ax3.set_ylim(-R * 1.05, R * 1.05)
    ax3.set_zlim(0, H * 1.06)
    ax3.set_box_aspect((2 * R, 2 * R, H))
    ax3.view_init(elev=20, azim=-60)
    _trim_ticks(ax3, R, H, three_d=True)
    ax3.tick_params(labelsize=8, pad=1)
    ax3.set_xlabel("x (m)", fontsize=9, labelpad=2)
    ax3.set_ylabel("y (m)", fontsize=9, labelpad=2)
    ax3.set_zlabel("z (m)", fontsize=9, labelpad=2)
    ax3.set_title("perspective", fontsize=10)

    # ---- Panel 2: 2-D plan (per-patch, clean axes) --------------------------
    ax2 = fig.add_subplot(1, 2, 2)
    # Tank drawn as a clean circle (the projected wall/floor mesh is skipped).
    ax2.add_patch(
        Circle((0, 0), R, facecolor=_rgba("#9fb8c8", 0.12),
               edgecolor="#07203F", linewidth=1.5, zorder=0)
    )
    for name, tris in patches:
        color, _a3, a2, zo = PATCH_STYLE.get(name, ("#888888", 0.4, 0.4, 1))
        if a2 <= 0:
            continue
        verts = [tri[:, :2] for tri in tris]  # project to x-y
        pcoll = PolyCollection(
            verts, facecolors=_rgba(color, a2), edgecolors=(0, 0, 0, 0.12),
            linewidths=0.1, zorder=zo,
        )
        ax2.add_collection(pcoll)
    ax2.set_xlim(-R * 1.05, R * 1.05)
    ax2.set_ylim(-R * 1.05, R * 1.05)
    ax2.set_aspect("equal")
    _trim_ticks(ax2, R, H, three_d=False)
    ax2.tick_params(labelsize=8)
    ax2.set_xlabel("x (m)", fontsize=9)
    ax2.set_ylabel("y (m)", fontsize=9)
    ax2.set_title("plan (top-down)", fontsize=10)

    # ---- Legend (shared, below the panels) ----------------------------------
    handles = [
        Patch(facecolor=PATCH_STYLE[n][0], edgecolor="k", linewidth=0.3, label=PATCH_LABEL[n])
        for n, _ in patches
        if n in PATCH_LABEL
    ]
    fig.legend(
        handles=handles, loc="lower center", ncol=len(handles), fontsize=9,
        frameon=False, bbox_to_anchor=(0.5, -0.02),
    )

    imp = meta["impeller"]
    fig.suptitle(
        "Batch Mixing geometry — T=%.2f m, H=%.2f m, D=%.0f mm, %d blades @%.0f°, "
        "hub Ø%.0f mm, %d baffles"
        % (
            meta["tank"]["diameter"], H, imp["diameter"] * 1000, imp["blade_count"],
            imp["blade_angle_deg"], imp["hub_radius"] * 2000, meta["baffles"]["count"],
        ),
        fontsize=11, y=0.99,
    )

    # Explicit margins + bbox_inches="tight" -> nothing clipped (3-D tick labels
    # in particular). tight_layout() is deliberately NOT used (it crops 3-D axes).
    fig.subplots_adjust(left=0.04, right=0.98, top=0.90, bottom=0.12, wspace=0.12)
    out = os.path.join(geo_dir, "geometry_preview.png")
    fig.savefig(out, dpi=140, bbox_inches="tight", pad_inches=0.35)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

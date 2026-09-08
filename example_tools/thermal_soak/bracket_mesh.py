"""Shared procedural bracket mesh for the NAFEMS showcase FE tools.

Builds the **fixed-topology, layered-hex L-bracket** that Thermal Soak and
Bracket Stress both solve on (`designs/showcase-tools.md` §4.4). Pure
scikit-fem + numpy (BSD-3) — **no gmsh** (GPL), **no tetgen** (AGPL).

The bracket is a thin-walled L-section: two flanges meeting at 90° through a
**smooth quarter-circle fillet** (a real stress concentration, *not* a voxel
stair-step). The mesh is a mapped (transfinite) structured grid in three
curvilinear directions:

- ``i`` — arc-length along the L mid-path (flange A → fillet → flange B),
- ``j`` — across the depth (the +Y direction),
- ``k`` — through the wall thickness, along the in-plane normal (the **ply
  layers**: ``n_layers`` element layers).

Key properties the chain relies on:

- **Fixed topology / thickness z-morph** — the connectivity and node ordering
  depend only on ``(n_span, n_depth, n_layers)``; total ``thickness`` merely
  scales the through-thickness offsets. So two meshes built at different
  thicknesses share bit-identical node ordering, and a temperature field maps
  node-for-node onto the structural model.
- **Material rosette** — per element a right-handed frame ``(e1, e2, e3)`` with
  ``e1`` = the mid-path tangent (the fibre 0° axis, **following the flanges
  around the fold**), ``e2`` = +Y (depth), ``e3`` = the through-thickness
  normal. Bracket Stress rotates each ply's constitutive into this frame.
- **Named facet sets** — ``clamp`` (flange A end face), ``load`` (flange B end
  face), ``heat`` (an outer-surface patch on flange B near its end), and
  ``convection`` (the remaining exterior).

Geometry constants are the single source of truth for the bracket; the
Composite Laminate mass uses the same mid-surface area (see ``laminate_clt``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from skfem import MeshHex

# --- Geometry (metres) -----------------------------------------------------
FLANGE_LENGTH_M: float = 0.080  # each flange, mid-line, from the fold
DEPTH_M: float = 0.050  # bracket depth (+Y)
FILLET_RADIUS_M: float = 0.012  # mid-line fillet radius at the fold

#: Mid-surface arc length: two flanges (shortened by the fillet) + the
#: quarter-circle arc. ``area = path × depth`` is the bracket's mid-surface.
PATH_LENGTH_M: float = (
    2.0 * (FLANGE_LENGTH_M - FILLET_RADIUS_M) + (math.pi / 2.0) * FILLET_RADIUS_M
)
MIDSURFACE_AREA_M2: float = PATH_LENGTH_M * DEPTH_M


@dataclass(frozen=True)
class BracketMesh:
    """A built L-bracket mesh plus the selectors the FE tools need."""

    mesh: MeshHex
    thickness_m: float
    n_span: int
    n_depth: int
    n_layers: int
    clamp_facets: np.ndarray
    load_facets: np.ndarray
    heat_facets: np.ndarray
    convection_facets: np.ndarray
    elem_e1: np.ndarray  # (3, nelems) — fibre 0° axis (mid-path tangent)
    elem_e2: np.ndarray  # (3, nelems) — depth axis (+Y)
    elem_e3: np.ndarray  # (3, nelems) — through-thickness normal
    elem_layer: np.ndarray  # (nelems,) — through-thickness ply-layer index 0..n_layers-1

    @property
    def n_nodes(self) -> int:
        return int(self.mesh.p.shape[1])

    @property
    def n_elements(self) -> int:
        return int(self.mesh.t.shape[1])


def _midpath_frame(u: float) -> tuple[float, float, float, float]:
    """At arc-length ``u`` along the L mid-path return ``(x, z, nx, nz)``.

    ``(x, z)`` is the mid-line point in the X–Z plane; ``(nx, nz)`` the unit
    in-plane normal (through-thickness direction), continuous across the
    straight/arc joins. The path runs flange A free end → fillet → flange B
    free end.
    """
    length, radius = FLANGE_LENGTH_M, FILLET_RADIUS_M
    seg_a = length - radius
    arc = (math.pi / 2.0) * radius
    if u <= seg_a:  # flange A: horizontal mid-line at z=0, x from L to R
        x, z = length - u, 0.0
        nx, nz = 0.0, -1.0
    elif u <= seg_a + arc:  # quarter-circle fillet, centre (R, R)
        phi = -math.pi / 2.0 - (u - seg_a) / radius
        x = radius + radius * math.cos(phi)
        z = radius + radius * math.sin(phi)
        nx, nz = math.cos(phi), math.sin(phi)  # outward radial = continuous normal
    else:  # flange B: vertical mid-line at x=0, z from R to L
        x, z = 0.0, radius + (u - seg_a - arc)
        nx, nz = -1.0, 0.0
    return x, z, nx, nz


def build_bracket(
    thickness_m: float,
    *,
    n_span: int = 60,
    n_depth: int = 12,
    n_layers: int = 8,
    heat_fraction: float = 0.4,
) -> BracketMesh:
    """Build the layered-hex L-bracket at ``thickness_m``.

    ``n_span`` / ``n_depth`` / ``n_layers`` are the element counts along the
    path / depth / thickness; ``n_layers`` is the number of ply-group layers.
    ``heat_fraction`` sizes the flange-B heat patch (fraction of the path
    nearest the flange-B end).
    """
    if thickness_m <= 0:
        raise ValueError("thickness_m must be positive")
    if thickness_m >= 2.0 * FILLET_RADIUS_M:
        raise ValueError(
            f"thickness_m ({thickness_m}) must stay below 2·fillet_radius "
            f"({2 * FILLET_RADIUS_M}) so the inner fillet doesn't fold"
        )

    ns, nd, nt = n_span + 1, n_depth + 1, n_layers + 1
    # Structured hex skeleton: correct connectivity + node order for free.
    skeleton = MeshHex.init_tensor(
        np.arange(ns, dtype=float), np.arange(nd, dtype=float), np.arange(nt, dtype=float)
    )
    ijk = np.rint(skeleton.p).astype(int)  # node -> (i, j, k) integer grid coords

    # Precompute the mid-path frame at each i-station.
    u = np.linspace(0.0, PATH_LENGTH_M, ns)
    frame = np.array([_midpath_frame(float(ui)) for ui in u])  # (ns, 4): x, z, nx, nz
    y = np.linspace(0.0, DEPTH_M, nd)
    # Through-thickness offset per k-plane, centred on the mid-surface.
    off = (np.arange(nt) / float(n_layers) - 0.5) * thickness_m

    # Remap every node from its (i, j, k) to its real 3-D position.
    i_idx, j_idx, k_idx = ijk[0], ijk[1], ijk[2]
    px, pz, pnx, pnz = frame[i_idx, 0], frame[i_idx, 1], frame[i_idx, 2], frame[i_idx, 3]
    points = np.vstack(
        [
            px + pnx * off[k_idx],  # X
            y[j_idx],  # Y (depth)
            pz + pnz * off[k_idx],  # Z
        ]
    )
    mesh = MeshHex(points, skeleton.t)

    # --- Named facet sets, classified by the structured (i, j, k) of their nodes.
    facet_nodes = mesh.facets  # (4, n_facets)
    boundary = mesh.boundary_facets()
    fi = ijk[0][facet_nodes]  # (4, n_facets) i-coord of each facet's nodes
    fk = ijk[2][facet_nodes]  # k-coord
    i_heat_lo = _flange_b_start_index(ns) + int(
        heat_fraction * (n_span - _flange_b_start_index(ns))
    )

    def _all(arr: np.ndarray, val: int) -> np.ndarray:
        return np.all(arr == val, axis=0)

    is_clamp = _all(fi, 0)
    is_load = _all(fi, n_span)
    is_outer = _all(fk, n_layers)  # one through-thickness free surface
    is_heat = is_outer & (np.min(fi, axis=0) >= i_heat_lo) & ~is_load

    clamp = boundary[is_clamp[boundary]]
    load = boundary[is_load[boundary]]
    heat = boundary[is_heat[boundary]]
    used = np.concatenate([clamp, load, heat])
    convection = np.setdiff1d(boundary, used)

    e1, e2, e3 = _element_rosette(mesh, ijk, frame)
    elem_layer = ijk[2][mesh.t].min(axis=0)  # the k-index of each element's bottom face
    return BracketMesh(
        mesh=mesh,
        thickness_m=thickness_m,
        n_span=n_span,
        n_depth=n_depth,
        n_layers=n_layers,
        clamp_facets=clamp,
        load_facets=load,
        heat_facets=heat,
        convection_facets=convection,
        elem_e1=e1,
        elem_e2=e2,
        elem_e3=e3,
        elem_layer=elem_layer,
    )


def _flange_b_start_index(ns: int) -> int:
    """The i-station where flange B begins (just past the fillet arc)."""
    seg_a = FLANGE_LENGTH_M - FILLET_RADIUS_M
    arc = (math.pi / 2.0) * FILLET_RADIUS_M
    return int(round((seg_a + arc) / PATH_LENGTH_M * (ns - 1)))


def _element_rosette(
    mesh: MeshHex, ijk: np.ndarray, frame: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-element ``(e1, e2, e3)`` material frame (each (3, nelems))."""
    elem_i = ijk[0][mesh.t]  # (8, nelems) i of each element node
    i_mid = np.rint(elem_i.mean(axis=0)).astype(int)
    i_mid = np.clip(i_mid, 0, frame.shape[0] - 1)
    tx, tz = frame[i_mid, 2], frame[i_mid, 3]  # normal components
    # e1 (fibre/tangent) = +90° rotation of the in-plane normal in X–Z:
    # normal (nx, nz) -> tangent (nz, -nx); both unit and continuous.
    e1 = np.vstack([frame[i_mid, 3], np.zeros_like(tx), -frame[i_mid, 2]])
    e3 = np.vstack([tx, np.zeros_like(tx), tz])
    e2 = np.vstack([np.zeros_like(tx), np.ones_like(tx), np.zeros_like(tx)])
    return e1, e2, e3

"""Fixed-topology hex mesh for the Finned Cold Plate showcase tool.

Builds the **baseplate only** — a structured hex box (`designs/finned-plate-tool.md`
§4). The fins are deliberately *not* meshed: they enter the solve as an effective
convective coefficient on the top face (see :mod:`fin_model`), which is what keeps the
mesh topology constant while ``fin_count`` and ``fin_height`` vary.

Pure scikit-fem + numpy (BSD-3) — no gmsh (GPL), no tetgen (AGPL).

Key properties the demo relies on:

- **Fixed topology / thickness z-morph** — connectivity and node ordering depend only
  on ``(n_x, n_y, n_z)``. ``base_thickness_m`` merely scales the through-thickness
  coordinate, so two meshes built at different thicknesses share bit-identical node
  ordering and their temperature fields are directly comparable node-for-node. This is
  what makes a POD field surrogate possible at all (ADR-0008).
- **Named facet sets** — ``component`` (the square heat-source footprint on the
  underside) and ``top`` (the finned upper surface carrying the Robin condition).
  Everything else is left bare, which in a finite-element assembly *is* the adiabatic
  condition: the plate is taken to be mounted against an insulating enclosure.

Geometry constants here are the single source of truth for the plate; :mod:`fin_model`
imports them so the fin array and the mesh can never disagree about the plate's size.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from skfem import MeshHex

# --- Geometry (metres) -----------------------------------------------------
PLATE_LENGTH_M: float = 0.100  # along the flow direction (X)
PLATE_WIDTH_M: float = 0.100  # across the flow, the direction fins are pitched in (Y)
COMPONENT_SIDE_M: float = 0.030  # square component footprint on the underside

#: Plan area of the plate — the fin array's reference area for ``h_eff``.
PLATE_AREA_M2: float = PLATE_LENGTH_M * PLATE_WIDTH_M

#: Fraction of the plate span from an edge to the component footprint.
_MARGIN_FRACTION: float = (1.0 - COMPONENT_SIDE_M / PLATE_LENGTH_M) / 2.0

#: Tolerance on placing that footprint exactly on element boundaries.
_ALIGN_TOL: float = 1.0e-9


@dataclass(frozen=True)
class PlateMesh:
    """A built baseplate mesh plus the facet selectors the solver needs."""

    mesh: MeshHex
    base_thickness_m: float
    n_x: int
    n_y: int
    n_z: int
    component_facets: np.ndarray
    top_facets: np.ndarray

    @property
    def n_nodes(self) -> int:
        """Number of mesh nodes — invariant to every design variable."""
        return int(self.mesh.p.shape[1])

    @property
    def n_elements(self) -> int:
        """Number of hex elements — invariant to every design variable."""
        return int(self.mesh.t.shape[1])


def build_plate(
    base_thickness_m: float,
    *,
    n_x: int = 80,
    n_y: int = 80,
    n_z: int = 8,
) -> PlateMesh:
    """Build the baseplate box mesh at ``base_thickness_m``.

    ``n_x`` / ``n_y`` / ``n_z`` are element counts along the flow, across the flow and
    through the thickness. They set the solve cost and **must be identical for every
    run in a study**, since the field surrogate compares fields node-for-node.

    ``n_x`` and ``n_y`` should be multiples of 20 so the component footprint lands
    exactly on element boundaries (100 mm plate, 30 mm component, 35 mm margin).
    """
    if base_thickness_m <= 0:
        raise ValueError("base_thickness_m must be positive")
    if min(n_x, n_y, n_z) < 1:
        raise ValueError("element counts must be >= 1")

    nx, ny, nz = n_x + 1, n_y + 1, n_z + 1
    # Structured hex skeleton on the integer grid: correct connectivity and a node
    # ordering that depends only on the element counts (never on the thickness).
    skeleton = MeshHex.init_tensor(
        np.arange(nx, dtype=float), np.arange(ny, dtype=float), np.arange(nz, dtype=float)
    )
    ijk = np.rint(skeleton.p).astype(int)  # node -> (i, j, k) integer grid coords

    x = np.linspace(0.0, PLATE_LENGTH_M, nx)
    y = np.linspace(0.0, PLATE_WIDTH_M, ny)
    z = np.linspace(0.0, base_thickness_m, nz)  # thickness scales z, nothing else

    i_idx, j_idx, k_idx = ijk[0], ijk[1], ijk[2]
    points = np.vstack([x[i_idx], y[j_idx], z[k_idx]])
    mesh = MeshHex(points, skeleton.t)

    # --- Named facet sets, classified by the structured (i, j, k) of their nodes.
    facet_nodes = mesh.facets  # (4, n_facets)
    boundary = mesh.boundary_facets()
    fi = ijk[0][facet_nodes]
    fj = ijk[1][facet_nodes]
    fk = ijk[2][facet_nodes]

    def _all(arr: np.ndarray, val: int) -> np.ndarray:
        return np.all(arr == val, axis=0)

    i_lo, i_hi = _component_span(n_x)
    j_lo, j_hi = _component_span(n_y)

    is_bottom = _all(fk, 0)
    is_top = _all(fk, n_z)
    within = (
        (np.min(fi, axis=0) >= i_lo)
        & (np.max(fi, axis=0) <= i_hi)
        & (np.min(fj, axis=0) >= j_lo)
        & (np.max(fj, axis=0) <= j_hi)
    )
    is_component = is_bottom & within

    component = boundary[is_component[boundary]]
    top = boundary[is_top[boundary]]
    if component.size == 0:
        raise ValueError(
            "the component footprint selected no facets — n_x / n_y are too coarse "
            "to resolve a 30 mm patch on a 100 mm plate (use multiples of 20)"
        )
    return PlateMesh(
        mesh=mesh,
        base_thickness_m=base_thickness_m,
        n_x=n_x,
        n_y=n_y,
        n_z=n_z,
        component_facets=component,
        top_facets=top,
    )


def cross_section_grid(plate: PlateMesh, temperature: np.ndarray) -> np.ndarray:
    """Temperature on the mid-plane slice through the component, as a ``(nz, ny)`` grid.

    The schematic draws the plate end-on, so the slice it needs is the plane at the
    middle of the flow direction — which is exactly the plane the component sits on.
    Row 0 is the **top** (finned) face and the last row is the underside, so the array
    can be handed straight to an image without flipping.

    The plate is a structured grid, so this is a selection rather than an
    interpolation: the mid-plane is a real node plane whenever ``n_x`` is even, which
    :func:`build_plate` already guarantees by requiring a multiple of 20.
    """
    points = plate.mesh.p
    x = points[0]
    centre = 0.5 * (float(x.min()) + float(x.max()))
    plane = float(x[int(np.argmin(np.abs(x - centre)))])
    selected = x == plane
    if not selected.any():  # pragma: no cover - unreachable for a built plate
        raise ValueError("no nodes found on the mid-plane")

    y = points[1][selected]
    z = points[2][selected]
    values = np.asarray(temperature)[selected]

    ys = np.unique(y)
    zs = np.unique(z)
    grid = np.full((zs.size, ys.size), np.nan)
    grid[np.searchsorted(zs, z), np.searchsorted(ys, y)] = values
    if np.isnan(grid).any():  # pragma: no cover - structured grid is always complete
        raise ValueError("the mid-plane slice is not a complete structured grid")
    return grid[::-1]  # +z is up, but image row 0 is the top


def _component_span(n: int) -> tuple[int, int]:
    """Grid-index range of the centred component footprint along one axis.

    The footprint must land **exactly** on element boundaries. If it does not, the
    solve still runs and still looks plausible — it just applies the heat load over a
    patch of the wrong size, which is a silently wrong answer of precisely the kind
    this showcase exists to warn about. So it is rejected rather than rounded.
    """
    exact = _MARGIN_FRACTION * n
    lo = round(exact)
    if abs(exact - lo) > _ALIGN_TOL:
        raise ValueError(
            f"n = {n} cannot place the {COMPONENT_SIDE_M * 1000:.0f} mm component "
            f"footprint on element boundaries of a {PLATE_LENGTH_M * 1000:.0f} mm "
            f"plate (it would land at {exact:.3f} elements from the edge). Use a "
            "multiple of 20."
        )
    return lo, n - lo

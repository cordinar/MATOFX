"""Parametric stirred-tank geometry builder — FreeCAD side of the Batch Mixing tool.

Phase 1 of `designs/cstr-mixing-demo.md` §10. This script is the **geometry stage**
called by the central orchestrator (`batch_mixing.py`) as a *separate process*:

    FreeCADCmd.exe build_geometry.py geometry_params.json

It runs under **FreeCAD's bundled interpreter** (FreeCAD 1.0, Python 3.11), NOT the
MATOFX venv, so it must be self-contained — only `FreeCAD`, `Part`, `Mesh`,
`MeshPart` and the stdlib are available. Parameters arrive as a JSON file (argv[1]
or ``geometry_params.json`` in the cwd, or the ``MATOFX_GEOM_PARAMS`` env var);
results leave as a set of **named STL surfaces** (one per OpenFOAM patch) plus a
**``geometry_meta.json``** the OpenFOAM stage consumes (MRF cell-zone cylinder for
``topoSet``, ``locationInMesh`` seed, the additive-layer ``setFields`` region, the
blockMesh background box, and the patch list).

Everything is built in **SI metres** end-to-end (design §10.1) so the exported STLs
drop straight into snappyHexMesh with no unit scaling. In the FreeCAD GUI the parts
will therefore look ~0.3 "mm" big — that is expected; use View → Fit all.

Coordinate frame: tank axis = +z, liquid floor at z = 0, free surface (slip lid) at
z = H, origin on the axis at the floor.
"""

import json
import math
import os
import sys

import FreeCAD as App  # noqa: N813  (FreeCAD's own conventional alias)
import Mesh
import MeshPart
import Part
from FreeCAD import Vector


# --------------------------------------------------------------------------- #
# Parameter handling
# --------------------------------------------------------------------------- #
def _params_path():
    if len(sys.argv) > 1 and sys.argv[-1].lower().endswith(".json"):
        return sys.argv[-1]
    env = os.environ.get("MATOFX_GEOM_PARAMS")
    if env:
        return env
    return "geometry_params.json"


def _load_params(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _get(params, key, default):
    """Fetch a parameter, treating null/missing as 'use the derived default'."""
    val = params.get(key, None)
    return default if val is None else val


# --------------------------------------------------------------------------- #
# Geometry construction (all lengths in metres)
# --------------------------------------------------------------------------- #
def build(params):
    """Return (solids_by_patch, meta) for the given parameters.

    ``solids_by_patch`` maps an OpenFOAM patch name -> a single Part.Shape whose
    tessellation becomes ``<patch>.stl``. ``meta`` is the geometry_meta dict.
    """
    # --- Tank ---------------------------------------------------------------
    T = float(params["tank_diameter"])          # tank (= liquid) diameter [m]
    H = float(_get(params, "liquid_height", T))  # liquid height [m]; default H = T
    R = T / 2.0

    # --- Impeller (pitched-blade turbine) -----------------------------------
    d_ratio = float(_get(params, "impeller_diameter_ratio", 0.33))  # D/T
    c_ratio = float(_get(params, "impeller_clearance_ratio", 0.30))  # C/T (off-bottom)
    n_blades = int(_get(params, "blade_count", 4))
    blade_angle = float(_get(params, "blade_angle_deg", 45.0))       # pitch from horizontal
    shaft_d = float(_get(params, "shaft_diameter", 0.06 * T))

    D = d_ratio * T
    C = c_ratio * T                       # impeller mid-height off the floor
    r_shaft = shaft_d / 2.0
    # Hub radius: explicit ``hub_diameter`` if given and in range, else derived
    # (~0.18 D dia). The hub must enclose the shaft and leave radial room for the
    # blades (shaft < hub < 0.9 D); an out-of-range value falls back to the
    # derived hub with a warning rather than failing the whole run.
    derived_hub = max(1.4 * r_shaft, 0.09 * D)
    hub_d = _get(params, "hub_diameter", None)
    r_hub = derived_hub if hub_d is None else float(hub_d) / 2.0
    if not (r_shaft < r_hub < 0.45 * D):
        if hub_d is not None:
            App.Console.PrintWarning(
                "batch_mixing geometry: hub_diameter %.4g m is out of range "
                "(must be between shaft %.4g m and 0.9*D %.4g m); using the "
                "derived hub %.4g m instead.\n"
                % (2.0 * r_hub, shaft_d, 0.9 * D, 2.0 * derived_hub)
            )
        r_hub = min(max(derived_hub, r_shaft * 1.2), 0.44 * D)
    # Blade proportions (derived; refined visually in Phase 1).
    blade_len = D / 2.0 - r_hub           # radial span, hub -> tip
    blade_w = float(_get(params, "blade_width", 0.20 * D))  # chord (axial-ish) width
    blade_t = float(_get(params, "blade_thickness", max(0.02 * D, 0.003)))
    hub_h = 1.1 * blade_w

    # --- Baffles ------------------------------------------------------------
    n_baffles = int(_get(params, "baffle_count", 4))
    baffle_w = float(_get(params, "baffle_width", 0.10 * T))       # radial width (T/10)
    baffle_gap = float(_get(params, "baffle_wall_gap", 0.02 * T))  # gap wall->baffle
    baffle_t = float(_get(params, "baffle_thickness", max(0.01 * T, 0.003)))
    baffle_z0 = float(_get(params, "baffle_bottom", 0.03 * H))
    baffle_z1 = float(_get(params, "baffle_top", 0.98 * H))

    # --- Additive layer on top (setFields region) ---------------------------
    add_frac = float(_get(params, "additive_volume_fraction", 0.10))  # 0..1 of liquid
    add_thickness = add_frac * H  # flat top layer of thickness add_frac * H

    solids = {}

    # Tank wall: a solid cylinder we take the *surface* of. snappy keeps the
    # interior (locationInMesh) and treats these faces as the wall patch. We
    # split the closed cylinder into the side+bottom ('tankWall', no-slip) and
    # the top disc ('topLid', free-slip approx of the flat free surface).
    tank_cyl = Part.makeCylinder(R, H, Vector(0, 0, 0), Vector(0, 0, 1))
    top_faces, side_bottom_faces = [], []
    for f in tank_cyl.Faces:
        # The planar face at z = H is the lid; everything else is side/bottom.
        com = f.CenterOfMass
        if isinstance(f.Surface, Part.Plane) and abs(com.z - H) < 1e-9:
            top_faces.append(f)
        else:
            side_bottom_faces.append(f)
    solids["tankWall"] = Part.makeShell(side_bottom_faces)
    solids["topLid"] = Part.makeShell(top_faces)

    # Baffles: thin radial plates at the wall, staggered so azimuth 0 stays in
    # the fluid (keeps the locationInMesh seed clean).
    az_offset = math.pi / n_baffles  # half a spacing -> nothing sits at az = 0
    baffle_solids = []
    r_outer = R - baffle_gap
    r_inner = r_outer - baffle_w
    for k in range(n_baffles):
        az = az_offset + k * 2.0 * math.pi / n_baffles
        # Build the plate along +x (radial), thin in y, then rotate by az about z.
        plate = Part.makeBox(
            baffle_w, baffle_t, baffle_z1 - baffle_z0,
            Vector(r_inner, -baffle_t / 2.0, baffle_z0),
        )
        plate.rotate(Vector(0, 0, 0), Vector(0, 0, 1), math.degrees(az))
        baffle_solids.append(plate)
    solids["baffles"] = _fuse(baffle_solids)

    # Impeller: hub + pitched blades, fused into one 'impeller' patch (the
    # rotating wetted surface used for the torque -> power integral).
    hub = Part.makeCylinder(r_hub, hub_h, Vector(0, 0, C - hub_h / 2.0), Vector(0, 0, 1))
    impeller_solids = [hub]
    for k in range(n_blades):
        az = k * 2.0 * math.pi / n_blades
        # Flat plate: radial length x chord (blade_w) x thickness. Centre it on
        # z = C, tilt by the pitch about the radial (x) axis, push out to the hub,
        # then spin to the blade's azimuth.
        blade = Part.makeBox(
            blade_len, blade_w, blade_t,
            Vector(0, -blade_w / 2.0, -blade_t / 2.0),
        )
        blade.rotate(Vector(0, 0, 0), Vector(1, 0, 0), blade_angle)  # pitch
        blade.translate(Vector(r_hub, 0, C))                          # to hub, mid-height
        blade.rotate(Vector(0, 0, 0), Vector(0, 0, 1), math.degrees(az))
        impeller_solids.append(blade)
    solids["impeller"] = _fuse(impeller_solids)

    # Shaft: from the hub up through the lid (and a little beyond, so it is a
    # clean through-cut at the top boundary).
    shaft_bottom = C + hub_h / 2.0
    shaft = Part.makeCylinder(
        r_shaft, (H + 0.05 * H) - shaft_bottom, Vector(0, 0, shaft_bottom), Vector(0, 0, 1)
    )
    solids["shaft"] = shaft

    # --- geometry_meta ------------------------------------------------------
    # MRF cell-zone: a cylinder comfortably enclosing the impeller swept volume.
    r_mrf = min(0.60 * D, R - baffle_gap - baffle_w - 0.01 * T)  # stay clear of baffles
    z_mrf0 = max(0.02 * H, C - 1.1 * blade_w)
    z_mrf1 = min(0.98 * H, C + 1.1 * blade_w)

    # locationInMesh: mid-radius, azimuth 0 (guaranteed between baffles), above
    # the impeller -> unambiguously in the fluid, outside every solid.
    loc = [0.5 * R, 0.0, min(0.75 * H, C + 0.35 * H)]

    meta = {
        "schema_version": 1,
        "units": "m",
        "tank": {"diameter": T, "radius": R, "liquid_height": H},
        "impeller": {
            "diameter": D, "clearance": C, "blade_count": n_blades,
            "blade_angle_deg": blade_angle, "hub_radius": r_hub, "shaft_radius": r_shaft,
        },
        "baffles": {"count": n_baffles, "width": baffle_w, "wall_gap": baffle_gap},
        # MRF zone for `topoSet` cylinderToCell (axis endpoints + radius).
        "mrf_zone": {
            "point1": [0.0, 0.0, z_mrf0],
            "point2": [0.0, 0.0, z_mrf1],
            "radius": r_mrf,
        },
        "location_in_mesh": loc,
        # Additive-on-top region for `setFields` (alpha = 1): a disc-topped
        # cylinder of the full tank radius, thickness add_thickness under the lid.
        "additive_region": {
            "type": "cylinder",
            "point1": [0.0, 0.0, H - add_thickness],
            "point2": [0.0, 0.0, H],
            "radius": R,
            "thickness": add_thickness,
            "volume_fraction": add_frac,
        },
        # blockMesh background box (a hair larger than the tank bbox so snappy
        # has a cell margin to snap onto the wall).
        "background_box": {
            "min": [-R * 1.02, -R * 1.02, -0.02 * H],
            "max": [R * 1.02, R * 1.02, H * 1.02],
        },
        "patches": {
            "tankWall": "wall",
            "topLid": "slip",
            "baffles": "wall",
            "impeller": "wall",
            "shaft": "wall",
        },
    }
    return solids, meta


def _fuse(shapes):
    if not shapes:
        return None
    if len(shapes) == 1:
        return shapes[0]
    return shapes[0].multiFuse(shapes[1:])


# --------------------------------------------------------------------------- #
# STL export
# --------------------------------------------------------------------------- #
def _export_stl(shape, path, linear_deflection):
    """Tessellate a Part.Shape and write a binary STL (metres)."""
    mesh = MeshPart.meshFromShape(
        Shape=shape,
        LinearDeflection=linear_deflection,
        AngularDeflection=0.5,
        Relative=False,
    )
    Mesh.export([_as_doc_object(mesh)], path)


def _as_doc_object(mesh):
    """MeshPart returns a Mesh.Mesh; Mesh.export wants a document object."""
    doc = App.ActiveDocument or App.newDocument("batch_mixing")
    obj = doc.addObject("Mesh::Feature", "surf")
    obj.Mesh = mesh
    return obj


def main():
    params = _load_params(_params_path())
    out_dir = params.get("output_dir", ".")
    os.makedirs(out_dir, exist_ok=True)

    solids, meta = build(params)

    # Deflection scaled to the model so every patch tessellates consistently.
    ref = meta["tank"]["radius"]
    deflection = max(ref * 2.0e-3, 2.0e-4)

    written = []
    for patch, shape in solids.items():
        if shape is None:
            continue
        stl_path = os.path.join(out_dir, patch + ".stl")
        _export_stl(shape, stl_path, deflection)
        written.append(patch + ".stl")

    meta["stl_files"] = written
    meta["stl_deflection"] = deflection
    with open(os.path.join(out_dir, "geometry_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    # Console breadcrumb for the orchestrator log.
    App.Console.PrintMessage(
        "batch_mixing geometry: wrote %d STL(s) + geometry_meta.json to %s\n"
        % (len(written), os.path.abspath(out_dir))
    )
    return 0


# FreeCADCmd execs this file with ``__name__`` != "__main__", so the usual guard
# would never fire. This module is only ever *executed* (never imported) in the
# FreeCAD context, so run unconditionally and surface any error to the console
# (a bare traceback from FreeCADCmd is easy to miss in the orchestrator log).
try:
    App.Console.PrintMessage("batch_mixing geometry: start\n")
    _rc = main()
    App.Console.PrintMessage("batch_mixing geometry: done rc=%d\n" % _rc)
except Exception as exc:  # noqa: BLE001 - report then re-raise for a non-zero exit
    import traceback

    tb = traceback.format_exc()
    App.Console.PrintError("batch_mixing geometry FAILED: %s\n%s\n" % (exc, tb))
    # Also drop a traceback file next to where the outputs would go, so the
    # orchestrator can surface it (FreeCADCmd often exits 0 despite this raise,
    # which would otherwise leave only a generic "no geometry_meta.json").
    try:
        _od = "."
        try:
            _od = _load_params(_params_path()).get("output_dir", ".") or "."
        except Exception:
            pass
        os.makedirs(_od, exist_ok=True)
        with open(os.path.join(_od, "geometry_error.txt"), "w", encoding="utf-8") as _fh:
            _fh.write(tb)
    except Exception:
        pass
    raise

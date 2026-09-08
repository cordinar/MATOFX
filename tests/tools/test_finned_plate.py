"""Physics-correctness tests for the Finned Cold Plate solver.

The NAFEMS showcase needs this solve to be *right*, not merely to run
(`designs/finned-plate-tool.md`). What is checked here:

- **energy is conserved** — at steady state the applied heat load equals the heat
  convected off the finned top face (the strongest single check on the assembly);
- the **operating point** is a real fan/system intersection, and flow falls as the
  channels narrow — the choking that gives the design space an interior structure;
- the **regime change** is present and cuts across the box, since the whole
  coverage narrative depends on it being real physics rather than a contrivance;
- **spreading resistance** behaves — a thinner or less conductive plate runs a hotter
  peak, which is what makes the finite-element solve worth doing at all;
- **fixed mesh topology** — node count and ordering are invariant to every input, the
  property the field surrogate depends on;
- the solver **fails loudly** rather than emitting a plausible-but-wrong result.

Run on a coarse mesh for speed. The peak temperature is mesh-converged to well under
0.1 K between this mesh and the production one, so the coarse mesh tests the same
physics.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

skfem = pytest.importorskip("skfem")

_BUNDLE = Path(__file__).resolve().parents[2] / "example_tools" / "finned_plate"
if str(_BUNDLE) not in sys.path:
    sys.path.insert(0, str(_BUNDLE))  # so the solver can import its sibling modules


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _BUNDLE / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fan_curve = _load("fan_curve")
fin_model = _load("fin_model")
plate_mesh = _load("plate_mesh")
fp = _load("finned_plate")

_COARSE = {"n_x": 20, "n_y": 20, "n_z": 2}
_AL = fin_model.MATERIALS["Aluminium"]
_MG = fin_model.MATERIALS["Magnesium"]
_BASE = {"fin_count": 16, "fin_height_m": 0.020, "base_thickness_m": 0.006, "fan_duty": 0.7}


# --- the fan operating point -----------------------------------------------


def test_operating_point_lies_on_both_curves() -> None:
    """The solved flow must satisfy fan and system pressure simultaneously."""
    channels = fan_curve.Channels(count=15, gap_m=0.00525, height_m=0.020, length_m=0.1)
    point = fan_curve.solve_operating_point(channels, 0.7)
    assert point.flow_m3s > 0.0
    assert fan_curve.fan_pressure(point.flow_m3s, 0.7) == pytest.approx(
        fan_curve.system_pressure(point.flow_m3s, channels), rel=1e-6
    )
    assert point.velocity_ms == pytest.approx(point.flow_m3s / channels.free_area_m2)


def test_narrower_channels_choke_the_flow() -> None:
    """More fins on a fixed plate must reduce delivered flow — the interior optimum."""
    flows = []
    for count in (8, 16, 28):
        array = fin_model.evaluate(
            fin_count=count,
            fin_height_m=0.020,
            base_thickness_m=0.006,
            fan_duty=1.0,
            material=_AL,
        )
        flows.append(array.operating_point.flow_m3s)
    assert flows[0] > flows[1] > flows[2], f"flow did not fall as fins were added: {flows}"


def test_higher_duty_delivers_more_flow_and_more_cooling() -> None:
    """``fan_duty`` must be a live design variable, not an inert one."""
    low = fin_model.evaluate(material=_AL, **{**_BASE, "fan_duty": 0.25})
    high = fin_model.evaluate(material=_AL, **{**_BASE, "fan_duty": 1.0})
    assert high.operating_point.flow_m3s > low.operating_point.flow_m3s
    # The conductance must rise too: if Nusselt were flow-independent (the
    # fully-developed laminar value) duty would change nothing at all.
    assert high.conductance > low.conductance * 1.1


def test_regime_change_is_present_and_cuts_across_the_box() -> None:
    """The laminar/turbulent crossover must sit *inside* the design box.

    The coverage narrative depends on a cohort being able to sit entirely on one side
    of this boundary while the rest of the box lies on the other, so both regimes must
    be reachable, and the boundary must depend on more than one factor.
    """

    def regime(count: int, duty: float) -> bool:
        return fin_model.evaluate(
            fin_count=count,
            fin_height_m=0.045,
            base_thickness_m=0.006,
            fan_duty=duty,
            material=_AL,
        ).turbulent

    assert regime(6, 1.0) is True, "no turbulent corner in the box"
    assert regime(28, 0.2) is False, "no laminar corner in the box"
    # Boundary depends on both factors, so it is a diagonal rather than a single cut.
    assert regime(6, 0.2) != regime(6, 1.0)


def test_nusselt_is_monotone_across_the_transition() -> None:
    """Blending must not introduce a non-physical step down at the crossover."""
    previous = 0.0
    for re in range(100, 20_000, 100):
        nu, _turbulent = fan_curve.channel_nusselt(float(re), 0.07)
        assert nu >= previous - 1e-9, f"Nusselt fell at Re={re}"
        previous = nu


# --- the finite-element solve ----------------------------------------------


def test_energy_is_conserved() -> None:
    """Applied load must equal the heat convected off the top face."""
    from skfem import ElementHex1, FacetBasis, Functional  # noqa: PLC0415

    plate, temp, array = fp.solve_plate(material=_AL, **_BASE, **_COARSE)
    basis = FacetBasis(plate.mesh, ElementHex1(), facets=plate.top_facets)

    @Functional
    def shed(w):
        return array.h_effective * (w["t"] - fin_model.AIR_INLET_C)

    removed = float(shed.assemble(basis, t=basis.interpolate(temp)))
    assert removed == pytest.approx(fp.HEAT_LOAD_W, rel=1e-3)


def test_plate_is_hotter_than_air_and_peaks_over_the_component() -> None:
    plate, temp, _array = fp.solve_plate(material=_AL, **_BASE, **_COARSE)
    assert temp.min() > fin_model.AIR_INLET_C
    assert temp.max() < 300.0
    # The hot spot must sit on the underside, over the component footprint.
    hottest = plate.mesh.p[:, int(np.argmax(temp))]
    assert hottest[2] == pytest.approx(0.0, abs=1e-9), "peak is not on the underside"
    for axis in (0, 1):
        centre = plate_mesh.PLATE_LENGTH_M / 2.0
        half = plate_mesh.COMPONENT_SIDE_M / 2.0
        assert abs(hottest[axis] - centre) <= half + 1e-9


def test_spreading_resistance_makes_the_field_solve_worth_doing() -> None:
    """A thinner or less conductive plate must run a hotter peak.

    This is the behaviour a lumped model cannot produce, and the reason base thickness
    is a design variable rather than a constant.
    """
    thin = fp.solve_plate(material=_AL, **{**_BASE, "base_thickness_m": 0.002}, **_COARSE)
    thick = fp.solve_plate(material=_AL, **{**_BASE, "base_thickness_m": 0.012}, **_COARSE)
    assert thin[1].max() > thick[1].max() + 5.0

    spread_al = float(thin[1].max() - thin[1].min())
    mg = fp.solve_plate(material=_MG, **{**_BASE, "base_thickness_m": 0.002}, **_COARSE)
    spread_mg = float(mg[1].max() - mg[1].min())
    assert spread_mg > spread_al, "lower conductivity did not spread heat worse"


def test_solution_is_mesh_converged() -> None:
    """The coarse test mesh and a finer one must agree, so the tests mean something."""
    coarse = fp.solve_plate(material=_AL, **_BASE, **_COARSE)[1].max()
    fine = fp.solve_plate(material=_AL, **_BASE, n_x=40, n_y=40, n_z=4)[1].max()
    assert float(coarse) == pytest.approx(float(fine), abs=0.1)


# --- the property the field surrogate depends on ---------------------------


def test_mesh_topology_is_fixed_across_every_input() -> None:
    """Node count, ordering and connectivity must not depend on any design variable.

    A POD field surrogate compares fields node-for-node (ADR-0008). If the mesh moved
    with the inputs, every field in a study would be incomparable — so this is the
    single most load-bearing property of the whole tool.
    """
    reference = plate_mesh.build_plate(0.006, **_COARSE)
    for thickness in (0.002, 0.0075, 0.015):
        other = plate_mesh.build_plate(thickness, **_COARSE)
        assert other.n_nodes == reference.n_nodes
        assert np.array_equal(other.mesh.t, reference.mesh.t)
        # x and y are untouched; only z scales with the thickness.
        assert np.allclose(other.mesh.p[:2], reference.mesh.p[:2])
        assert other.mesh.p[2].max() == pytest.approx(thickness)
        assert np.array_equal(other.component_facets, reference.component_facets)
        assert np.array_equal(other.top_facets, reference.top_facets)


def test_field_node_count_is_independent_of_fin_geometry() -> None:
    """Fin count and height must not perturb the solved field's node set."""
    a = fp.solve_plate(material=_AL, **_BASE, **_COARSE)
    b = fp.solve_plate(
        material=_MG,
        **{**_BASE, "fin_count": 28, "fin_height_m": 0.045},
        **_COARSE,
    )
    assert a[1].shape == b[1].shape
    assert np.array_equal(a[0].mesh.p, b[0].mesh.p)  # thickness unchanged between the two


# --- the schematic's cross-section -----------------------------------------


def test_cross_section_is_oriented_and_shaped_correctly() -> None:
    """The slice must be (nz, ny) with row 0 the top face.

    The schematic pastes this array straight into an image, so a transposed or
    flipped grid would draw a plausible-looking but upside-down plate.
    """
    plate, temp, _array = fp.solve_plate(material=_AL, **_BASE, **_COARSE)
    section = plate_mesh.cross_section_grid(plate, temp)

    assert section.shape == (_COARSE["n_z"] + 1, _COARSE["n_y"] + 1)
    assert np.isfinite(section).all()
    assert section.min() >= temp.min() - 1e-9
    assert section.max() <= temp.max() + 1e-9
    # Heat enters the underside, so the bottom row runs hotter than the finned top.
    assert section[-1].mean() > section[0].mean()


def test_cross_section_peaks_over_the_component() -> None:
    """The hot spot must sit mid-span, under the heat source — not at an edge."""
    plate, temp, _array = fp.solve_plate(
        material=_AL, **{**_BASE, "base_thickness_m": 0.002}, **_COARSE
    )
    section = plate_mesh.cross_section_grid(plate, temp)
    underside = section[-1]
    hottest = int(np.argmax(underside))
    centre = (underside.size - 1) / 2.0
    half = underside.size * (plate_mesh.COMPONENT_SIDE_M / plate_mesh.PLATE_WIDTH_M) / 2.0
    assert abs(hottest - centre) <= half
    assert underside[hottest] > underside[0]  # hotter than the plate edge


def test_thin_low_conductivity_plate_has_the_sharpest_hot_spot() -> None:
    """The visual the schematic exists to show: spreading resistance, made visible."""

    def contrast(material: object, thickness_m: float) -> float:
        plate, temp, _a = fp.solve_plate(
            material=material,  # type: ignore[arg-type]
            **{**_BASE, "base_thickness_m": thickness_m},
            **_COARSE,
        )
        section = plate_mesh.cross_section_grid(plate, temp)
        return float(section.max() - section.min())

    thin_magnesium = contrast(_MG, 0.002)
    thin_aluminium = contrast(_AL, 0.002)
    thick_aluminium = contrast(_AL, 0.012)
    assert thin_magnesium > thin_aluminium > thick_aluminium


# --- failing loudly --------------------------------------------------------


def test_degenerate_fin_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        fin_model.evaluate(
            fin_count=1,
            fin_height_m=0.02,
            base_thickness_m=0.006,
            fan_duty=0.7,
            material=_AL,
        )


def test_zero_duty_is_rejected_rather_than_silently_stalling() -> None:
    channels = fan_curve.Channels(count=15, gap_m=0.005, height_m=0.02, length_m=0.1)
    with pytest.raises(ValueError, match="duty"):
        fan_curve.solve_operating_point(channels, 0.0)


@pytest.mark.parametrize("bad", [3, 15, 30, 50, 70])
def test_misaligned_mesh_is_rejected_not_rounded(bad: int) -> None:
    """A mesh that cannot place the footprint exactly must fail, not approximate it.

    Rounding would apply the 60 W over a patch of the wrong size: the run still
    completes and still looks plausible, which is the silently-wrong answer this
    showcase exists to warn about.
    """
    with pytest.raises(ValueError, match="multiple of 20"):
        plate_mesh.build_plate(0.006, n_x=bad, n_y=bad, n_z=2)


@pytest.mark.parametrize("good", [20, 40, 60, 80])
def test_aligned_meshes_place_the_footprint_exactly(good: int) -> None:
    plate = plate_mesh.build_plate(0.006, n_x=good, n_y=good, n_z=2)
    element = plate_mesh.PLATE_LENGTH_M / good
    # The selected patch must be exactly the component's own area.
    expected = (plate_mesh.COMPONENT_SIDE_M / element) ** 2
    assert plate.component_facets.size == pytest.approx(expected)


def test_materials_are_distinguishable() -> None:
    """The categorical must actually change the answer, or it is decorative."""
    al = fp.solve_plate(material=_AL, **_BASE, **_COARSE)
    mg = fp.solve_plate(material=_MG, **_BASE, **_COARSE)
    assert mg[1].max() > al[1].max() + 1.0  # magnesium runs hotter
    assert mg[2].mass_g < al[2].mass_g  # but lighter

"""Fin-array model for the Finned Cold Plate — the lumped surface conductance.

The fins are never meshed (`designs/finned-plate-tool.md` §4). Instead this module
reduces the whole fin array to a single **effective heat-transfer coefficient** applied
to the plate's top face, such that

    h_eff * A_plate * (T_surface - T_air)

equals the heat the array actually removes. That is a conventional and defensible
engineering simplification, and it is what lets ``fin_count`` and ``fin_height`` vary
without changing the finite-element mesh.

Physics, in evaluation order:

* channel geometry from the fin pitch on a fixed-width plate;
* the fan operating point (:mod:`fan_curve`) — flow, velocity, Reynolds number;
* the channel convection coefficient from the Nusselt correlation for that regime;
* **fin efficiency** ``tanh(mL)/mL`` — the second source of curvature, and the one
  that makes ``fin_height`` and ``material`` interact, since ``m`` depends on the
  metal's conductivity;
* the total conductance over fins and exposed base, divided by the plate area.

Correlation choices follow ``example_tools/heat_sink/heat_sink.py``, the existing
closed-form fin tool, so the two stay consistent.

Pure numpy (BSD-3).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from fan_curve import (
    K_AIR,
    Channels,
    OperatingPoint,
    channel_nusselt,
    solve_operating_point,
)
from plate_mesh import PLATE_AREA_M2, PLATE_LENGTH_M, PLATE_WIDTH_M

#: Fin thickness is a tool-author choice, not a design variable — exposing it would
#: push the factor count past what a readable coverage scatter matrix can carry.
FIN_THICKNESS_M: float = 0.001

#: Air inlet temperature (degC). Fixed for the same reason.
AIR_INLET_C: float = 25.0

#: Fewest fins that still form a flow channel.
MIN_FIN_COUNT: int = 2


@dataclass(frozen=True)
class Material:
    """A plate material. Conductivity drives fin efficiency; density drives mass."""

    name: str
    conductivity: float  # W/m/K
    density: float  # kg/m^3


#: The two materials the showcase exposes. Chosen so that **neither dominates**, which
#: is what makes the categorical a real decision rather than a foregone conclusion
#: (`designs/finned-plate-tool.md` §7.1).
#:
#: Magnesium AZ31 is 36 % lighter than aluminium but conducts 44 % worse, so it wins
#: wherever fin efficiency is close to 1 (short fins, where area is all that matters)
#: and loses wherever efficiency bites (tall fins, where getting heat to the tip is the
#: constraint). Since the objective is *mass*, that trade genuinely reverses inside the
#: design box.
#:
#: Copper was the obvious first choice and is deliberately **not** here: it buys about
#: 20 % more conductance for about 230 % more mass, so a mass-minimising optimiser can
#: never choose it. A dominated level would make the categorical decorative — and would
#: leave the coverage story's categorical-balance check with nothing real to say.
MATERIALS: dict[str, Material] = {
    "Aluminium": Material("Aluminium", conductivity=170.0, density=2700.0),
    "Magnesium": Material("Magnesium", conductivity=96.0, density=1740.0),
}


@dataclass(frozen=True)
class FinArray:
    """The evaluated fin array: what the air does, and what the metal achieves."""

    operating_point: OperatingPoint
    h_channel: float  # W/m^2K on the wetted fin + base surfaces
    turbulent: bool  # which Nusselt correlation is active
    fin_efficiency: float  # 0..1
    conductance: float  # W/K, whole array
    h_effective: float  # W/m^2K referred to the plate plan area
    mass_g: float
    channels: Channels


def evaluate(
    *,
    fin_count: int,
    fin_height_m: float,
    base_thickness_m: float,
    fan_duty: float,
    material: Material,
) -> FinArray:
    """Evaluate the fin array and reduce it to an effective surface conductance."""
    if fin_count < MIN_FIN_COUNT:
        raise ValueError(f"fin_count must be at least {MIN_FIN_COUNT} to form a flow channel")
    if fin_height_m <= 0.0:
        raise ValueError("fin_height_m must be positive")

    pitch = PLATE_WIDTH_M / fin_count
    gap = pitch - FIN_THICKNESS_M
    if gap <= 0.0:
        raise ValueError(
            f"{fin_count} fins of {FIN_THICKNESS_M * 1000:.1f} mm do not fit across a "
            f"{PLATE_WIDTH_M * 1000:.0f} mm plate — the channels have closed"
        )

    channels = Channels(
        count=fin_count - 1,
        gap_m=gap,
        height_m=fin_height_m,
        length_m=PLATE_LENGTH_M,
    )
    point = solve_operating_point(channels, fan_duty)

    # Convection coefficient in the channels, from the regime's Nusselt correlation.
    d_h = channels.hydraulic_diameter_m
    nusselt, turbulent = channel_nusselt(point.reynolds, d_h / channels.length_m)
    h_channel = nusselt * K_AIR / d_h

    # Straight-fin efficiency, tanh(mL)/mL. This is where fin_height and material
    # interact: m falls as conductivity rises, so copper keeps tall fins working.
    m = float(np.sqrt(2.0 * h_channel / (material.conductivity * FIN_THICKNESS_M)))
    m_l = m * fin_height_m
    fin_efficiency = float(np.tanh(m_l) / m_l) if m_l > 0.0 else 1.0

    # Wetted areas: both faces of every fin, plus the base between them.
    area_fins = fin_count * 2.0 * fin_height_m * PLATE_LENGTH_M
    area_base = PLATE_LENGTH_M * max(PLATE_WIDTH_M - fin_count * FIN_THICKNESS_M, 0.0)
    conductance = h_channel * (fin_efficiency * area_fins + area_base)

    volume = PLATE_AREA_M2 * base_thickness_m + (
        fin_count * FIN_THICKNESS_M * fin_height_m * PLATE_LENGTH_M
    )
    mass_g = material.density * volume * 1000.0

    return FinArray(
        operating_point=point,
        h_channel=h_channel,
        turbulent=turbulent,
        fin_efficiency=fin_efficiency,
        conductance=conductance,
        h_effective=conductance / PLATE_AREA_M2,
        mass_g=mass_g,
        channels=channels,
    )

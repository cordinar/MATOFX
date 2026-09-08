"""Fan + system curve intersection for the Finned Cold Plate.

The tool models a **real fan**, not an imposed air velocity
(`designs/finned-plate-tool.md` §5.1). That distinction is the whole reason the design
space has an interior optimum: narrowing the channels raises the pressure drop, which
slides the operating point down the fan curve until the flow collapses. Adding fins
helps until it doesn't.

The model, in the order it is evaluated:

* **Fan curve** — ``dp_fan(Q) = dp_max * (1 - (Q / Q_max)**2)``, a standard quadratic
  approximation to an axial fan's static-pressure characteristic.
* **Affinity laws** scale it with fan duty ``d``: ``Q_max ∝ d``, ``dp_max ∝ d²``.
* **System curve** — Darcy friction through the fin channels plus entrance/exit minor
  losses, with the friction factor switching between the laminar and turbulent
  correlations at ``Re = 2300``.
* **Operating point** — the unique ``Q`` where the two curves cross, found by bisection
  on a guaranteed-bracketed root.

**Why the transition matters.** These channels are short enough that laminar flow
never thermally develops, so heat transfer is entry-limited and saturates as the cube
root of the Graetz number. Turbulent transfer instead keeps climbing as ``Re**0.8``.
Heat transfer therefore changes *character* across the crossover, not merely magnitude
— which is exactly what makes a surrogate fitted on one side extrapolate confidently
and wrongly onto the other (§7.3). The demo depends on that being real physics rather
than a contrivance. See :func:`channel_nusselt`.

Pure numpy + scipy (BSD-3).
"""

from __future__ import annotations

from dataclasses import dataclass

from scipy.optimize import brentq

# --- Air properties at ~40 degC --------------------------------------------
RHO_AIR: float = 1.13  # kg/m^3
MU_AIR: float = 1.90e-5  # Pa.s
K_AIR: float = 0.0271  # W/m/K
PR_AIR: float = 0.71  # Prandtl number

# --- Fan characteristic (an 80 mm axial fan at 100 % duty) -----------------
# Illustrative but representative. These two numbers set where the choking bites
# and where the laminar/turbulent diagonal falls across the design box, so they are
# the first thing to re-tune if the response surface comes out flat.
DP_MAX_PA: float = 120.0  # static pressure at zero flow
Q_MAX_M3S: float = 0.038  # free delivery at zero back-pressure (~80 CFM)
FAN_EFFICIENCY: float = 0.30  # electrical -> air power, for the reported draw

#: Reynolds number at which the friction/Nusselt correlations switch.
RE_TRANSITION: float = 2300.0

#: Combined entrance + exit loss coefficient for the fin channels.
K_MINOR: float = 1.5

_MIN_FLOW_M3S: float = 1.0e-7  # numerical floor for the bracket's lower end


@dataclass(frozen=True)
class Channels:
    """Geometry of the flow passages between fins."""

    count: int  # number of channels (one fewer than the fin count)
    gap_m: float  # clear spacing between adjacent fins
    height_m: float  # fin height == channel depth
    length_m: float  # channel length along the flow

    @property
    def free_area_m2(self) -> float:
        """Total free cross-section presented to the air."""
        return self.count * self.gap_m * self.height_m

    @property
    def hydraulic_diameter_m(self) -> float:
        """``4 A / P`` for a rectangular duct."""
        return 2.0 * self.gap_m * self.height_m / (self.gap_m + self.height_m)


@dataclass(frozen=True)
class OperatingPoint:
    """Where the fan curve meets the system curve."""

    flow_m3s: float
    velocity_ms: float
    pressure_pa: float
    reynolds: float
    friction_turbulent: bool  # governs the friction factor, not the Nusselt regime
    fan_power_w: float


def fan_pressure(flow_m3s: float, duty: float) -> float:
    """Static pressure the fan delivers at ``flow_m3s``, at fractional ``duty``."""
    q_max = Q_MAX_M3S * duty
    dp_max = DP_MAX_PA * duty * duty
    if q_max <= 0.0:
        return 0.0
    ratio = flow_m3s / q_max
    return dp_max * (1.0 - ratio * ratio)


def reynolds(flow_m3s: float, channels: Channels) -> float:
    """Channel Reynolds number at ``flow_m3s``."""
    velocity = flow_m3s / channels.free_area_m2
    return RHO_AIR * velocity * channels.hydraulic_diameter_m / MU_AIR


def friction_factor(re: float) -> float:
    """Darcy friction factor, switching correlation at ``RE_TRANSITION``."""
    if re < 1.0:  # vanishing flow — keep the system curve finite
        return 64.0
    if re < RE_TRANSITION:
        return 64.0 / re  # fully developed laminar
    return 0.316 * re**-0.25  # Blasius, smooth turbulent


def system_pressure(flow_m3s: float, channels: Channels) -> float:
    """Pressure the fin channels demand to pass ``flow_m3s``."""
    velocity = flow_m3s / channels.free_area_m2
    dynamic = 0.5 * RHO_AIR * velocity * velocity
    re = reynolds(flow_m3s, channels)
    d_h = channels.hydraulic_diameter_m
    major = friction_factor(re) * (channels.length_m / d_h) * dynamic
    minor = K_MINOR * dynamic
    return major + minor


def solve_operating_point(channels: Channels, duty: float) -> OperatingPoint:
    """Find where the fan curve crosses the system curve.

    The two curves are monotone in opposite directions — fan pressure falls with flow,
    system demand rises — so exactly one crossing exists in ``(0, Q_max]`` and it is
    bracketed by construction. **Convergence is asserted, never assumed**: a silently
    non-converged root would yield a plausible-looking but wrong ``h_eff``, and a
    confidently wrong solver is precisely the failure this demo exists to warn about.
    """
    if not 0.0 < duty <= 1.0:
        raise ValueError(f"fan duty must be in (0, 1], got {duty}")
    if channels.count < 1 or channels.gap_m <= 0.0 or channels.height_m <= 0.0:
        raise ValueError("channel geometry is degenerate — no flow passage")

    q_max = Q_MAX_M3S * duty

    def residual(q: float) -> float:
        return fan_pressure(q, duty) - system_pressure(q, channels)

    lo, hi = _MIN_FLOW_M3S, q_max
    r_lo, r_hi = residual(lo), residual(hi)
    if not (r_lo > 0.0 > r_hi):
        raise RuntimeError(
            "the fan and system curves are not bracketed over (0, Q_max]: "
            f"residual({lo:.3e}) = {r_lo:.4f}, residual({hi:.3e}) = {r_hi:.4f}. "
            "The operating point could not be established, so no result was written."
        )

    flow = float(brentq(residual, lo, hi, xtol=1.0e-12, rtol=1.0e-10, maxiter=200))
    if not _MIN_FLOW_M3S <= flow <= q_max:
        raise RuntimeError(f"operating-point solve returned an out-of-range flow: {flow}")

    velocity = flow / channels.free_area_m2
    pressure = system_pressure(flow, channels)
    re = reynolds(flow, channels)
    if not all(map(_finite, (flow, velocity, pressure, re))):
        raise RuntimeError("the operating-point solve produced a non-finite value")

    return OperatingPoint(
        flow_m3s=flow,
        velocity_ms=velocity,
        pressure_pa=pressure,
        reynolds=re,
        friction_turbulent=re >= RE_TRANSITION,
        fan_power_w=pressure * flow / FAN_EFFICIENCY,
    )


def channel_nusselt(re: float, d_h_over_l: float) -> tuple[float, bool]:
    """Channel Nusselt number — **the regime change the demo turns on**.

    Returns ``(Nu, turbulent)``.

    These channels are *short*: at the flows this fan delivers, the thermal entry
    length is comparable to the 100 mm channel, so the flow never fully develops. Using
    the fully-developed laminar value (a constant 8.23) would therefore be wrong for
    this geometry — and would also make the answer independent of flow rate throughout
    the laminar regime, which is both unphysical here and would render ``fan_duty``
    inert over most of the design box.

    So:

    * **Laminar** — a Hausen-type thermal-entry correlation in the Graetz number
      ``Gz = (D_h/L) Re Pr``, asymptoting to 8.23 for long channels and rising with
      flow for short ones.
    * **Turbulent** — Dittus-Boelter, climbing as ``Re**0.8``.

    The two are combined by taking the larger, which is standard practice for blending
    correlations across a transition: it keeps ``Nu`` monotone in ``Re`` instead of
    introducing a non-physical downward step where the two expressions cross.

    What survives that blending — and what the demo actually needs — is the change in
    *character*: entry-limited laminar transfer saturates as ``Gz**(1/3)``, while
    turbulent transfer keeps climbing as ``Re**0.8``. A surrogate fitted entirely on one
    side of that crossover has no way to know the other exists.
    """
    graetz = d_h_over_l * re * PR_AIR
    nu_lam = 8.23 + 0.0668 * graetz / (1.0 + 0.04 * graetz ** (2.0 / 3.0))
    nu_turb = 0.023 * re**0.8 * PR_AIR**0.4
    if nu_turb > nu_lam:
        return nu_turb, True
    return nu_lam, False


def _finite(value: float) -> bool:
    return value == value and abs(value) != float("inf")  # noqa: PLR0124 - NaN check

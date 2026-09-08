"""Classical Lamination Theory engine for the Composite Laminate tool.

Pure numpy (BSD-3). From a symmetric balanced layup of a **measured** UD
lamina it computes the laminate ABD matrices, effective engineering
constants, effective CTE and in-plane conductivity, thickness and mass —
the material card the downstream Thermal Soak / Bracket Stress solvers
apply per ply (NAFEMS showcase chain, `designs/showcase-tools.md`).

Lamina data are **measured** values from Daniel & Ishai, *Engineering
Mechanics of Composite Materials* — CFRP T300/5208 (Vf 0.70) and GFRP
E-glass/epoxy (Vf 0.55). These canonical textbook values are used directly
(not micromechanics estimates) so the reported numbers are defensible to a
composites audience.

The maths is standard CLT (see Jones, *Mechanics of Composite Materials*,
or Daniel & Ishai ch. 5):

- lamina reduced stiffness ``Q`` (plane stress),
- transformed stiffness ``Qbar(θ)`` via the closed-form rotation,
- ``A,B,D`` by through-thickness integration of ``Qbar`` over the plies,
- effective in-plane constants from ``A⁻¹``,
- effective CTE from the thermal force resultant ``N_T``,
- effective in-plane conductivity by thickness-weighted ply averaging.

A symmetric balanced ``[(±θ₁/±θ₂)ₙ]s`` layup gives ``B≈0`` (no
bend–stretch coupling) and ``A₁₆=A₂₆≈0`` (no shear–extension coupling);
:func:`build_layup` constructs exactly that stack.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# --- Bracket geometry (fixed mid-surface; thickness is the mass lever) ----
# The L-bracket is two flanges meeting through a quarter-circle fillet;
# mass = ρ · (mid-surface area) · thickness, so mass scales linearly with
# laminate thickness — the real "drop plies to save weight" lever.
# These constants MUST match example_tools/_shared/bracket_mesh.py so the
# laminate mass equals the procedurally-meshed bracket's mass.
FLANGE_LENGTH_M: float = 0.080  # each flange, mid-line, m
DEPTH_M: float = 0.050  # bracket depth (into the page), m
FILLET_RADIUS_M: float = 0.012  # mid-line fillet radius at the fold, m
# Mid-surface arc length: two flanges (shortened by the fillet) + the
# quarter-circle arc.
_PATH_LENGTH_M: float = (
    2.0 * (FLANGE_LENGTH_M - FILLET_RADIUS_M) + (math.pi / 2.0) * FILLET_RADIUS_M
)
MIDSURFACE_AREA_M2: float = _PATH_LENGTH_M * DEPTH_M


@dataclass(frozen=True)
class Lamina:
    """Measured UD lamina properties (one cured ply). Stiffness in GPa,
    strengths in MPa, CTE in 1e-6/°C, conductivity in W/m·K."""

    name: str
    e1: float
    e2: float
    g12: float
    nu12: float
    alpha1: float
    alpha2: float
    xt: float
    xc: float
    yt: float
    yc: float
    s: float
    k1: float
    k2: float
    rho: float  # kg/m³
    cp: float  # J/kg·K


# Daniel & Ishai canonical lamina data.
LAMINA: dict[str, Lamina] = {
    "CFRP": Lamina(
        name="CFRP — T300/5208 (Vf 0.70)",
        e1=181.0, e2=10.3, g12=7.17, nu12=0.28,
        alpha1=0.02, alpha2=22.5,
        xt=1500.0, xc=1500.0, yt=40.0, yc=246.0, s=68.0,
        k1=5.0, k2=0.6, rho=1600.0, cp=800.0,
    ),
    "GFRP": Lamina(
        name="GFRP — E-glass/epoxy (Vf 0.55)",
        e1=39.0, e2=8.6, g12=3.8, nu12=0.28,
        alpha1=7.0, alpha2=21.0,
        xt=1080.0, xc=620.0, yt=39.0, yc=128.0, s=89.0,
        k1=1.0, k2=0.5, rho=2100.0, cp=1000.0,
    ),
}

_GPA = 1.0e9


N_PLIES: int = 8  # fixed symmetric balanced laminate [θ1/−θ1/θ2/−θ2]s


def build_layup(theta1: float, theta2: float) -> list[float]:
    """Return the ply angles (deg) of the fixed ``[θ₁/−θ₁/θ₂/−θ₂]s`` laminate.

    Eight plies: the balanced unit ``[+θ₁, −θ₁, +θ₂, −θ₂]`` mirrored about the
    mid-plane (``s``, symmetric). Balanced ⇒ ``A₁₆=A₂₆=0``; symmetric ⇒
    ``B=0``. Total **thickness is a continuous design variable** (the plies
    scale), so the structural solver resolves exactly one element per ply.
    """
    unit = [theta1, -theta1, theta2, -theta2]
    return unit + unit[::-1]


def _reduced_stiffness(lam: Lamina) -> np.ndarray:
    """Lamina plane-stress reduced stiffness ``Q`` (3×3, Pa)."""
    e1, e2, g12, nu12 = lam.e1 * _GPA, lam.e2 * _GPA, lam.g12 * _GPA, lam.nu12
    nu21 = nu12 * e2 / e1
    d = 1.0 - nu12 * nu21
    return np.array(
        [
            [e1 / d, nu12 * e2 / d, 0.0],
            [nu12 * e2 / d, e2 / d, 0.0],
            [0.0, 0.0, g12],
        ]
    )


def _transformed_stiffness(q: np.ndarray, theta_deg: float) -> np.ndarray:
    """Transformed reduced stiffness ``Qbar(θ)`` (3×3, Pa) — closed form."""
    t = math.radians(theta_deg)
    m, n = math.cos(t), math.sin(t)
    q11, q12, q22, q66 = q[0, 0], q[0, 1], q[1, 1], q[2, 2]
    m2, n2 = m * m, n * n
    m4, n4 = m2 * m2, n2 * n2
    qb11 = q11 * m4 + 2 * (q12 + 2 * q66) * m2 * n2 + q22 * n4
    qb22 = q11 * n4 + 2 * (q12 + 2 * q66) * m2 * n2 + q22 * m4
    qb12 = (q11 + q22 - 4 * q66) * m2 * n2 + q12 * (m4 + n4)
    qb66 = (q11 + q22 - 2 * q12 - 2 * q66) * m2 * n2 + q66 * (m4 + n4)
    qb16 = (q11 - q12 - 2 * q66) * m**3 * n + (q12 - q22 + 2 * q66) * m * n**3
    qb26 = (q11 - q12 - 2 * q66) * m * n**3 + (q12 - q22 + 2 * q66) * m**3 * n
    return np.array([[qb11, qb12, qb16], [qb12, qb22, qb26], [qb16, qb26, qb66]])


def abd_matrices(
    lam: Lamina, angles: list[float], thickness_m: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Return ``(A, B, D, h)`` — laminate stiffness matrices (SI) + thickness ``h`` (m).

    Plies share the total ``thickness_m`` equally (``h/len(angles)`` each).
    """
    h = thickness_m
    z = np.linspace(-h / 2.0, h / 2.0, len(angles) + 1)
    q = _reduced_stiffness(lam)
    a = np.zeros((3, 3))
    b = np.zeros((3, 3))
    d = np.zeros((3, 3))
    for k, theta in enumerate(angles):
        qb = _transformed_stiffness(q, theta)
        a += qb * (z[k + 1] - z[k])
        b += 0.5 * qb * (z[k + 1] ** 2 - z[k] ** 2)
        d += (1.0 / 3.0) * qb * (z[k + 1] ** 3 - z[k] ** 3)
    return a, b, d, h


def effective_constants(a: np.ndarray, h: float) -> tuple[float, float, float, float]:
    """Effective in-plane ``(Ex, Ey, Gxy, νxy)`` from ``A`` (Ex/Ey/Gxy in GPa)."""
    inv = np.linalg.inv(a)
    ex = 1.0 / (h * inv[0, 0])
    ey = 1.0 / (h * inv[1, 1])
    gxy = 1.0 / (h * inv[2, 2])
    nuxy = -inv[0, 1] / inv[0, 0]
    return ex / _GPA, ey / _GPA, gxy / _GPA, nuxy


def effective_cte(
    lam: Lamina, angles: list[float], a: np.ndarray, thickness_m: float
) -> tuple[float, float]:
    """Effective laminate CTE ``(αx, αy)`` in 1e-6/°C, via the thermal resultant."""
    h = thickness_m
    z = np.linspace(-h / 2.0, h / 2.0, len(angles) + 1)
    a1, a2 = lam.alpha1 * 1e-6, lam.alpha2 * 1e-6
    q = _reduced_stiffness(lam)
    n_t = np.zeros(3)  # thermal force resultant per unit ΔT
    for k, theta in enumerate(angles):
        m, n = math.cos(math.radians(theta)), math.sin(math.radians(theta))
        ax = a1 * m * m + a2 * n * n
        ay = a1 * n * n + a2 * m * m
        axy = 2.0 * (a1 - a2) * m * n  # engineering shear CTE
        qb = _transformed_stiffness(q, theta)
        n_t += qb.dot([ax, ay, axy]) * (z[k + 1] - z[k])
    alpha_eff = np.linalg.inv(a).dot(n_t)  # strain/°C
    return alpha_eff[0] * 1e6, alpha_eff[1] * 1e6


def effective_conductivity(lam: Lamina, angles: list[float]) -> tuple[float, float, float]:
    """Effective ``(kx, ky, kz)`` (W/m·K): in-plane thickness-weighted, kz transverse."""
    kx = ky = 0.0
    for theta in angles:
        m, n = math.cos(math.radians(theta)), math.sin(math.radians(theta))
        kx += lam.k1 * m * m + lam.k2 * n * n
        ky += lam.k1 * n * n + lam.k2 * m * m
    count = len(angles)
    # Through-thickness: plies in series, all ≈ transverse k2.
    return kx / count, ky / count, lam.k2


def directional_modulus(lam: Lamina, angles: list[float], phi_deg: float) -> float:
    """Effective modulus (GPa) when the laminate is loaded at off-axis ``φ``.

    Loading the laminate at ``φ`` equals rotating every ply by ``−φ`` and
    loading along x — so we reuse :func:`effective_constants` on the shifted
    stack. Drives the iconic directional-modulus polar plot.
    """
    shifted = [theta - phi_deg for theta in angles]
    a, _b, _d, h = abd_matrices(lam, shifted, 1.0)  # modulus is thickness-independent
    ex, _ey, _gxy, _nuxy = effective_constants(a, h)
    return ex


@dataclass(frozen=True)
class LaminateSummary:
    """Everything the tool emits — the per-ply card + laminate diagnostics."""

    material: str
    lamina: Lamina
    angles: list[float]
    n_plies: int
    thickness_mm: float
    mass_g: float
    areal_density: float  # kg/m²
    ex: float
    ey: float
    gxy: float
    nuxy: float
    alphax: float
    alphay: float
    kx: float
    ky: float
    kz: float


def summarise(
    material: str, theta1: float, theta2: float, thickness_mm: float
) -> LaminateSummary:
    """Full laminate evaluation for the given layup + total thickness."""
    if material not in LAMINA:
        raise ValueError(f"Unknown material {material!r}; expected one of {sorted(LAMINA)}")
    for name, val in (("theta1", theta1), ("theta2", theta2)):
        if not 0.0 <= val <= 90.0:
            raise ValueError(f"{name} must be in [0, 90] degrees (got {val})")
    if thickness_mm <= 0.0:
        raise ValueError(f"thickness_mm must be positive (got {thickness_mm})")
    lam = LAMINA[material]
    angles = build_layup(theta1, theta2)
    thickness_m = thickness_mm / 1000.0
    a, _b, _d, h = abd_matrices(lam, angles, thickness_m)
    ex, ey, gxy, nuxy = effective_constants(a, h)
    alphax, alphay = effective_cte(lam, angles, a, thickness_m)
    kx, ky, kz = effective_conductivity(lam, angles)
    mass = lam.rho * MIDSURFACE_AREA_M2 * h
    return LaminateSummary(
        material=material,
        lamina=lam,
        angles=angles,
        n_plies=len(angles),
        thickness_mm=thickness_mm,
        mass_g=mass * 1000.0,
        areal_density=lam.rho * h,
        ex=ex, ey=ey, gxy=gxy, nuxy=nuxy,
        alphax=alphax, alphay=alphay,
        kx=kx, ky=ky, kz=kz,
    )

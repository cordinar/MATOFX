"""Orthotropic layered-solid constitutive for the Bracket Stress tool.

Pure numpy (BSD-3). Builds the 3-D orthotropic stiffness of a UD ply from its
engineering constants, rotates it per element into the global frame (the
bracket rosette × the ply angle), and provides the thermal-stress and
first-ply-failure (max-stress + Tsai-Wu) recovery the structural solver needs.

To sidestep the classic Voigt factor-of-2 pitfalls, everything is done with
the **full 4th-order stiffness tensor** ``C_ijkl`` (so ``sigma_ij = C_ijkl
eps_kl`` with *tensor* strains) and rotated by the ordinary tensor rule
``C'_ijkl = Q_ia Q_jb Q_kc Q_ld C_abcd``. Transverse isotropy is assumed for
the through-thickness (3) direction (``E3=E2``, ``G13=G12``, ``nu13=nu12``),
with a matrix-dominated ``nu23``.

The ply material axes are ``1`` = fibre, ``2`` = in-plane transverse, ``3`` =
through-thickness.
"""

from __future__ import annotations

import numpy as np

_GPA = 1.0e9
_MPA = 1.0e6


def ply_stiffness_tensor(
    e1: float, e2: float, g12: float, nu12: float, nu23: float = 0.4
) -> np.ndarray:
    """Full 4th-order stiffness ``C_ijkl`` (3×3×3×3, Pa) of a UD ply.

    Inputs in GPa; transverse isotropy in the 2–3 plane. Returns SI (Pa).
    """
    e1p, e2p, g12p = e1 * _GPA, e2 * _GPA, g12 * _GPA
    e3p = e2p
    g13p = g12p
    nu13 = nu12
    g23p = e2p / (2.0 * (1.0 + nu23))
    # Normal-strain 3×3 compliance block (engineering), then invert → stiffness.
    s_n = np.array(
        [
            [1.0 / e1p, -nu12 / e1p, -nu13 / e1p],
            [-nu12 / e1p, 1.0 / e2p, -nu23 / e2p],
            [-nu13 / e1p, -nu23 / e2p, 1.0 / e3p],
        ]
    )
    c_n = np.linalg.inv(s_n)  # 3×3 normal stiffness block
    c = np.zeros((3, 3, 3, 3))
    for i in range(3):
        for j in range(3):
            c[i, i, j, j] = c_n[i, j]
    for (a, b), g in {(0, 1): g12p, (0, 2): g13p, (1, 2): g23p}.items():
        c[a, b, a, b] = c[b, a, b, a] = c[a, b, b, a] = c[b, a, a, b] = g
    return c


def rotation_ply_to_global(e1_dir: np.ndarray, e2_dir: np.ndarray, e3_dir: np.ndarray, theta_deg: float) -> np.ndarray:
    """Direction-cosine matrix ``Q`` (3×3) mapping ply axes → global.

    Columns are the ply axes in global coordinates: the fibre (ply-1) axis is
    the element tangent ``e1`` rotated by ``theta`` about the through-thickness
    normal ``e3``; ply-2 is the in-plane perpendicular; ply-3 = ``e3``.
    """
    c, s = np.cos(np.radians(theta_deg)), np.sin(np.radians(theta_deg))
    ply1 = c * e1_dir + s * e2_dir
    ply2 = -s * e1_dir + c * e2_dir
    return np.column_stack([ply1, ply2, e3_dir])


def rotate_stiffness(c_mat: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Rotate a stiffness tensor from material to global axes (``Q`` ply→global)."""
    return np.einsum("ia,jb,kc,ld,abcd->ijkl", q, q, q, q, c_mat)


def global_stiffness_field(
    c_mat: np.ndarray, q_per_elem: np.ndarray
) -> np.ndarray:
    """Per-element global stiffness ``(3,3,3,3,nelem)`` from per-element ``Q`` ``(3,3,nelem)``."""
    return np.einsum("iae,jbe,kce,lde,abcd->ijkle", q_per_elem, q_per_elem, q_per_elem, q_per_elem, c_mat)


def thermal_stress_per_degree(
    c_mat: np.ndarray, q: np.ndarray, alpha1: float, alpha2: float
) -> np.ndarray:
    """Global thermal stress tensor per unit ΔT (3×3) for one ply orientation.

    ``alpha`` in 1e-6/°C. ``beta_ij = C_ijkl * alpha_kl`` with the CTE tensor
    rotated to global; the structural load is ``beta * (T - T_ref)``.
    """
    alpha_mat = np.diag([alpha1, alpha2, alpha2]) * 1e-6  # transverse isotropy
    alpha_glob = q @ alpha_mat @ q.T  # 2nd-order rotation
    c_glob = rotate_stiffness(c_mat, q)
    return np.einsum("ijkl,kl->ij", c_glob, alpha_glob)


def max_stress_index(
    s1: float, s2: float, t12: float, *, xt: float, xc: float, yt: float, yc: float, s: float
) -> float:
    """Max-stress failure index (≥1 ⇒ first-ply failure). Stresses in Pa, strengths MPa."""
    s1m, s2m, t12m = s1 / _MPA, s2 / _MPA, t12 / _MPA
    fibre = s1m / xt if s1m >= 0 else -s1m / xc
    trans = s2m / yt if s2m >= 0 else -s2m / yc
    shear = abs(t12m) / s
    return max(fibre, trans, shear)


def tsai_wu_index(
    s1: float, s2: float, t12: float, *, xt: float, xc: float, yt: float, yc: float, s: float
) -> float:
    """Tsai-Wu failure index (the quadratic LHS; ≥1 ⇒ first-ply failure).

    Standard ``F12 = -0.5 sqrt(F11 F22)`` interaction assumption. Stresses in
    Pa, strengths in MPa.
    """
    s1m, s2m, t12m = s1 / _MPA, s2 / _MPA, t12 / _MPA
    f1 = 1.0 / xt - 1.0 / xc
    f2 = 1.0 / yt - 1.0 / yc
    f11 = 1.0 / (xt * xc)
    f22 = 1.0 / (yt * yc)
    f66 = 1.0 / (s * s)
    f12 = -0.5 * np.sqrt(f11 * f22)
    return (
        f1 * s1m
        + f2 * s2m
        + f11 * s1m**2
        + f22 * s2m**2
        + f66 * t12m**2
        + 2.0 * f12 * s1m * s2m
    )

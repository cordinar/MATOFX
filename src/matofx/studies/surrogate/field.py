"""Field-output surrogate engine — POD/PCA over a snapshot ensemble (ADR-0008 §4.3).

A *field* surrogate predicts a whole nodal field (a temperature map, a
stress field) at an unsampled input point, not just a scalar. The
pipeline is proper-orthogonal-decomposition (POD, i.e. PCA via SVD):

1. load each run's field snapshot to a 1-D nodal array (``load_field_snapshot``);
2. require a **common mesh** (same node count) across the ensemble;
3. centre the stack by the mean field and SVD it;
4. keep the first ``k`` modes capturing ``mode_energy_cutoff`` of the
   variance (or ``max_modes``);
5. fit one **scalar** surrogate per mode over the modal coefficients —
   reusing the unchanged :func:`matofx.studies.surrogate.fit_surrogate`;
6. predict by predicting each mode's coefficient and reconstructing
   ``mu + Σ coeff_m · basis_m``, with per-node 1-sigma propagated from the
   per-mode GPs (mode independence — true for orthogonal POD modes).

The engine is pure numpy/scipy + the existing scalar engine, so it is
testable from in-memory arrays with no mesh-format dependency. The
multi-format loaders (VTK / CGNS / Ensight) lazily import ``pyvista`` and
raise a clear, plain-English error until that dependency is vendored
(ADR-0008 §15 — a maintainer SBOM step); ``.npy`` works today on numpy
alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import ArrayLike

from matofx.studies.surrogate import Surrogate, fit_surrogate

_STREAMING_SVD_NODES = 50_000
"""Above this node count the truncated SVD uses ``scipy.sparse.linalg.svds``
rather than dense ``numpy.linalg.svd`` (ADR-0008 §4.3 step 4)."""

_NPY = "npy"
_PYVISTA_FORMATS: frozenset[str] = frozenset({"vtk", "vtu", "vtp", "cgns", "case"})

_MIN_SNAPSHOTS = 2
"""Below two snapshots there is no field variation to decompose."""


class FieldSurrogateError(Exception):
    """A field surrogate couldn't be fitted / loaded (plain-English, F30)."""


@dataclass(frozen=True, slots=True)
class FieldQuality:
    """How well the truncated modal model reconstructs the training fields."""

    n: int
    modes_retained: int
    mode_energy: float
    """Cumulative explained-variance ratio of the retained modes (0-1)."""
    recon_rmse: float | None
    """RMSE of reconstructing the training snapshots from the retained
    modes (truncation error) — the honest "how much did we throw away"."""
    mode_q2: list[float | None]
    """Per-mode cross-validated Q² of the coefficient surrogates."""
    field_q2: float | None = None
    """Energy-weighted mean of the per-mode Q² — a single 'how good is this
    field surrogate' score for ranking methods in a bake-off (ADR-0018).
    Weighting by mode energy makes the dominant patterns count most."""
    cv_field_actual: list[float] = dc_field(default_factory=list)
    """Actual mean-over-nodes field value per training run."""
    cv_field_predicted: list[float] = dc_field(default_factory=list)
    """Cross-validated (held-out) predicted mean field per run — the field
    analogue of the scalar predicted-vs-actual parity scatter. Empty when the
    per-mode CV wasn't available for every retained mode."""


@dataclass(frozen=True, slots=True)
class FieldPrediction:
    """A reconstructed field at one input point (ADR-0008 §4.3)."""

    field: np.ndarray
    """Reconstructed nodal field, shape ``(n_nodes,)``."""
    field_std: np.ndarray | None
    """Per-node 1-sigma propagated from the per-mode GPs, or ``None`` (poly)."""
    mode_coefficients: list[float]
    mode_coefficient_stds: list[float] | None


class FieldSurrogate:
    """A fitted POD field surrogate over the run ensemble's varied inputs."""

    def __init__(
        self,
        *,
        factors: list[str],
        mu: np.ndarray,
        modal_basis: np.ndarray,
        per_mode_surrogates: list[Surrogate],
        mode_energy_spectrum: list[float],
        quality: FieldQuality,
    ) -> None:
        """Hold the mean field, retained modal basis, and per-mode models."""
        self.factors = factors
        self.mu = mu
        self.modal_basis = modal_basis  # (k, n_nodes)
        self.per_mode_surrogates = per_mode_surrogates
        self.mode_energy_spectrum = mode_energy_spectrum
        self.quality = quality

    @property
    def n_nodes(self) -> int:
        """Mesh node count the field is defined over."""
        return int(self.mu.shape[0])

    @property
    def n_modes(self) -> int:
        """Retained POD modes (= number of per-mode surrogates)."""
        return len(self.per_mode_surrogates)

    def predict(self, point: Mapping[str, float]) -> FieldPrediction:
        """Reconstruct the field at one ``{factor: value}`` point (§4.3)."""
        coeffs: list[float] = []
        stds: list[float] = []
        have_std = True
        for surrogate in self.per_mode_surrogates:
            means, sigma = surrogate.predict([point])
            coeffs.append(means[0] if means else 0.0)
            if sigma:
                stds.append(sigma[0])
            else:
                have_std = False
        coeff_arr = np.asarray(coeffs)
        field = self.mu + coeff_arr @ self.modal_basis
        field_std: np.ndarray | None = None
        if have_std and stds:
            std_arr = np.asarray(stds)
            # Per-node variance = Σ (basis_m · std_m)^2 (mode independence).
            field_std = np.sqrt((std_arr[:, None] ** 2 * self.modal_basis**2).sum(axis=0))
        return FieldPrediction(
            field=field,
            field_std=field_std,
            mode_coefficients=coeffs,
            mode_coefficient_stds=stds if have_std and stds else None,
        )


def load_field_snapshot(
    path: str | Path,
    *,
    file_format: str,
    field_node_array_name: str | None = None,
) -> np.ndarray:
    """Load one run's field snapshot to a 1-D nodal array (§4.3 step 1).

    ``.npy`` needs only numpy. VTK / CGNS / Ensight Gold formats read via
    ``pyvista`` (lazily imported); until that dependency is vendored
    (ADR-0008 §15) those formats raise a plain-English error.
    """
    fmt = file_format.lower().lstrip(".")
    p = Path(path)
    if fmt == _NPY:
        return np.asarray(np.load(p), dtype=float).ravel()
    if fmt in _PYVISTA_FORMATS:
        try:
            import pyvista  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - dep not vendored yet
            raise FieldSurrogateError(
                "Reading VTK / CGNS / Ensight field snapshots needs the "
                "'pyvista' package, which isn't installed on this server yet. "
                "Use a '.npy' field export, or ask your administrator to add "
                "pyvista."
            ) from exc
        mesh = pyvista.read(str(p))
        if field_node_array_name:
            return np.asarray(mesh.point_data[field_node_array_name], dtype=float).ravel()
        active = mesh.active_scalars
        if active is None:
            raise FieldSurrogateError(
                f"{p.name} has no active point-data array; declare "
                "'field_node_array_name' in the descriptor."
            )
        return np.asarray(active, dtype=float).ravel()
    raise FieldSurrogateError(
        f"Unsupported field format '{file_format}'. Supported: npy, vtk, vtu, vtp, cgns, case."
    )


def _truncated_svd(centred: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(U, S, Vt)`` — dense for small meshes, sparse ``svds`` for large ones."""
    n_snap, n_nodes = centred.shape
    max_rank = min(n_snap, n_nodes)
    if n_nodes <= _STREAMING_SVD_NODES or max_rank <= 1:
        u, s, vt = np.linalg.svd(centred, full_matrices=False)
        return u, s, vt
    from scipy.sparse.linalg import svds  # noqa: PLC0415

    k = max_rank - 1  # svds returns at most min(shape)-1 singular triplets
    u, s, vt = svds(centred, k=k)
    order = np.argsort(s)[::-1]  # svds returns ascending — flip to descending
    return u[:, order], s[order], vt[order, :]


_AUTO_CANDIDATES: tuple[str, ...] = ("gp", "gp_matern", "poly", "gbm")
"""Regressors tried per POD mode when ``surrogate_kind='auto'`` (ADR-0018 Tier 1
opt-in): the best cross-validated Q² wins, mode by mode."""


def _fit_mode(
    factor_columns: Mapping[str, Sequence[float | None]],
    coeffs: list[float],
    surrogate_kind: str,
    categorical_columns: Mapping[str, Sequence[Any]] | None = None,
) -> Surrogate:
    """Fit one modal-coefficient regressor; ``'auto'`` picks the best by CV Q².

    ``auto`` fits every candidate and keeps the highest-Q² fit (mode by mode —
    different modes may prefer different regressors); falls back to GP if none
    reports a score. Any explicit kind is fitted directly.
    """
    if surrogate_kind != "auto":
        return fit_surrogate(
            factor_columns,
            coeffs,
            kind=surrogate_kind,  # type: ignore[arg-type]
            categorical_columns=categorical_columns,
        )
    best: Surrogate | None = None
    for kind in _AUTO_CANDIDATES:
        candidate = fit_surrogate(
            factor_columns,
            coeffs,
            kind=kind,  # type: ignore[arg-type]
            categorical_columns=categorical_columns,
        )
        if not candidate.fitted:
            continue
        if best is None:
            best = candidate
            continue
        cq, bq = candidate.quality.q2, best.quality.q2
        if cq is not None and (bq is None or cq > bq):
            best = candidate
    if best is not None:
        return best
    return fit_surrogate(factor_columns, coeffs, kind="gp", categorical_columns=categorical_columns)


def _field_quality(
    *,
    matrix: np.ndarray,
    mu: np.ndarray,
    modal_basis: np.ndarray,
    coeff_matrix: np.ndarray,
    energy: np.ndarray,
    total: float,
    ratio: np.ndarray,
    k: int,
    per_mode: list[Surrogate],
) -> FieldQuality:
    """Assemble :class:`FieldQuality` — RMSE, Field Q², and mean-field parity (ADR-0018).

    Field Q² weights each retained mode's cross-validated Q² by its energy share.
    The parity reconstructs each run's predicted mean field from the per-mode CV
    coefficients: ``mean(field) = mean(mu) + Σ_m coeff_m · mean(basis_m)`` — a
    cheap linear combination, so no extra fits. Parity arrays stay empty unless
    every retained mode carried aligned CV predictions.
    """
    recon = mu + coeff_matrix @ modal_basis
    recon_rmse = float(np.sqrt(np.mean((matrix - recon) ** 2)))
    mode_q2 = [s.quality.q2 for s in per_mode]
    weights = [float(e / total) for e in energy[:k]]
    wq = [(w, q) for w, q in zip(weights, mode_q2, strict=True) if q is not None]
    den = sum(w for w, _ in wq)
    field_q2 = (sum(w * q for w, q in wq) / den) if den > 0 else None

    cv_field_actual: list[float] = []
    cv_field_predicted: list[float] = []
    cv_preds = [list(s.quality.predicted) for s in per_mode]
    n_snap = int(matrix.shape[0])
    if per_mode and all(len(p) == n_snap for p in cv_preds):
        mode_node_mean = modal_basis.mean(axis=1)  # (k,)
        pred_means = float(mu.mean()) + mode_node_mean @ np.asarray(cv_preds)
        cv_field_actual = [float(v) for v in matrix.mean(axis=1)]
        cv_field_predicted = [float(v) for v in pred_means]

    return FieldQuality(
        n=n_snap,
        modes_retained=k,
        mode_energy=float(ratio[k - 1]),
        recon_rmse=recon_rmse,
        mode_q2=mode_q2,
        field_q2=field_q2,
        cv_field_actual=cv_field_actual,
        cv_field_predicted=cv_field_predicted,
    )


def fit_field_surrogate(
    field_snapshots: Sequence[ArrayLike],
    factor_columns: Mapping[str, Sequence[float | None]],
    *,
    mode_energy_cutoff: float = 0.99,
    max_modes: int | None = None,
    surrogate_kind: str = "gp",
    categorical_columns: Mapping[str, Sequence[Any]] | None = None,
) -> FieldSurrogate:
    """Fit a POD field surrogate over the snapshot ensemble (ADR-0008 §4.3).

    ``field_snapshots[i]`` is run ``i``'s nodal field (a 1-D sequence);
    all must share a node count (the common-mesh assumption, §4.3 step 2).
    ``factor_columns`` are the same varied inputs ``fit_surrogate`` takes,
    aligned row-for-row with the snapshots. ``surrogate_kind`` selects the
    per-mode coefficient regressor — the same registry the scalar path uses
    (``gp`` / ``gp_matern`` / ``poly`` / ``gbm``), or ``'auto'`` to pick the
    best regressor **per mode** by cross-validated Q²; POD-SVD is always the
    basis. Raises :class:`FieldSurrogateError` on a mesh mismatch / too few
    snapshots.
    """
    snapshots = [np.asarray(s, dtype=float).ravel() for s in field_snapshots]
    if len(snapshots) < _MIN_SNAPSHOTS:
        raise FieldSurrogateError(
            "A field surrogate needs at least two run snapshots to find modes."
        )
    n_nodes = snapshots[0].shape[0]
    for i, snap in enumerate(snapshots):
        if snap.shape[0] != n_nodes:
            raise FieldSurrogateError(
                f"Mismatched mesh: snapshot {i} has {snap.shape[0]} nodes; "
                f"expected {n_nodes}. Field surrogates assume a common mesh."
            )

    # Drop runs whose snapshot has non-finite values (NaN/Inf) — e.g. a tool
    # that wrote a degenerate / ill-conditioned solve. A single such row makes
    # the POD SVD diverge ("SVD did not converge"). Keep the aligned factor
    # columns in lockstep so the per-mode regressions still line up.
    finite_mask = [bool(np.isfinite(snap).all()) for snap in snapshots]
    n_dropped = finite_mask.count(False)
    if n_dropped:
        snapshots = [snap for snap, ok in zip(snapshots, finite_mask, strict=True) if ok]
        factor_columns = {
            key: [v for v, ok in zip(col, finite_mask, strict=True) if ok]
            for key, col in factor_columns.items()
        }
        if len(snapshots) < _MIN_SNAPSHOTS:
            raise FieldSurrogateError(
                f"Only {len(snapshots)} run snapshot(s) had finite field values "
                f"({n_dropped} dropped as non-finite); need at least {_MIN_SNAPSHOTS}. "
                "The source runs likely include non-physical results — clean or "
                "exclude them and refit."
            )

    matrix = np.vstack(snapshots)  # (n_snap, n_nodes)
    mu = matrix.mean(axis=0)
    centred = matrix - mu
    try:
        u, s, vt = _truncated_svd(centred)
    except np.linalg.LinAlgError as exc:
        raise FieldSurrogateError(
            "The field decomposition (SVD) did not converge — the snapshots "
            "contain extreme or near-degenerate values, usually a few runs with "
            "non-physical results. Exclude those runs (or fix the tool that "
            "produced them) and refit."
        ) from exc

    with np.errstate(over="ignore", invalid="ignore"):
        energy = s**2
        total = float(energy.sum())
    if not np.isfinite(total):
        # Singular values so large their square overflows float64 — the
        # snapshots hold extreme (finite but non-physical) values, so the POD
        # would be meaningless. Better a clear error than a garbage "success".
        raise FieldSurrogateError(
            "The field snapshots hold extreme values that overflow the "
            "decomposition — usually a few runs with non-physical results. "
            "Exclude those runs (or fix the tool that produced them) and refit."
        )
    if total <= 0.0:
        raise FieldSurrogateError(
            "Every snapshot is identical — there is no field variation to model."
        )
    ratio = np.cumsum(energy) / total
    k = int(np.searchsorted(ratio, mode_energy_cutoff) + 1)
    k = min(k, len(s))
    if max_modes is not None:
        k = min(k, max_modes)
    k = max(k, 1)

    modal_basis = vt[:k]  # (k, n_nodes)
    # Modal coefficient of snapshot i, mode m = U[i, m] * S[m].
    coeff_matrix = u[:, :k] * s[:k]  # (n_snap, k)

    per_mode: list[Surrogate] = []
    for m in range(k):
        coeffs = [float(v) for v in coeff_matrix[:, m]]
        per_mode.append(_fit_mode(factor_columns, coeffs, surrogate_kind, categorical_columns))

    quality = _field_quality(
        matrix=matrix,
        mu=mu,
        modal_basis=modal_basis,
        coeff_matrix=coeff_matrix,
        energy=energy,
        total=total,
        ratio=ratio,
        k=k,
        per_mode=per_mode,
    )
    return FieldSurrogate(
        factors=list(factor_columns.keys()),
        mu=mu,
        modal_basis=modal_basis,
        per_mode_surrogates=per_mode,
        mode_energy_spectrum=[float(e / total) for e in energy[:k]],
        quality=quality,
    )


__all__ = [
    "FieldPrediction",
    "FieldQuality",
    "FieldSurrogate",
    "FieldSurrogateError",
    "fit_field_surrogate",
    "load_field_snapshot",
]

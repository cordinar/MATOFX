"""On-disk surrogate-model bundle: write, hash, and integrity-checked load.

A bundle is the governed artefact a :class:`SurrogateModelVersion` points
at (ADR-0008 §5.3). Layout for a scalar model::

    <bundle_dir>/
      manifest.json                 # factors + responses + per-file sha256
      scalar/
        surrogate_<response>.joblib  # one fitted Surrogate per response

A **field** model (POD surrogate, §4.3) lays out its parts under ``field/``::

    <bundle_dir>/
      manifest.json                 # + field meta (n_nodes / n_modes / format)
      field/
        mu.npy                       # mean field
        modal_basis.npy              # retained POD modes (k, n_nodes)
        mode_energy.npy              # explained-variance spectrum
        mode_<m>.joblib              # one scalar Surrogate per mode coefficient
        reference_mesh.<ext>         # optional geometry source for the viewer

A **combined** model (ADR-0018) holds both in one bundle — ``scalar/`` plus a
per-field ``field/<response>/`` subtree — under one manifest, one sha::

    <bundle_dir>/
      manifest.json                 # kind:"combined"; scalar + per-field meta
      scalar/
        surrogate_<response>.joblib
      field/
        <response>/                 # one subtree per field output
          mu.npy / modal_basis.npy / mode_energy.npy / mode_<m>.joblib / reference_mesh.<ext>

``bundle_sha256`` covers the load-bearing bytes; it is recomputed and
compared on **every** load (non-negotiable #2) — a mismatch raises
:class:`ModelBundleCorruptError` rather than returning bad predictions.
Serialisation is ``joblib`` only; raw ``pickle.load`` is never called
(non-negotiable #3) and the runner-side load happens in a subprocess
context (non-negotiable #9), never the web process.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from matofx.models.errors import ModelBundleCorruptError
from matofx.studies.surrogate import Surrogate
from matofx.studies.surrogate.field import FieldQuality, FieldSurrogate

_MANIFEST = "manifest.json"
_SCALAR_DIR = "scalar"
_FIELD_DIR = "field"
_MU = "mu.npy"
_BASIS = "modal_basis.npy"
_ENERGY = "mode_energy.npy"


def _joblib_name(response: str) -> str:
    """Sanitised per-response bundle filename (traversal-safe)."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in response)
    return f"surrogate_{safe}.joblib"


def _mode_name(mode_index: int) -> str:
    """Per-mode coefficient-surrogate filename inside ``field/``."""
    return f"mode_{mode_index:04}.joblib"


def _safe_segment(name: str) -> str:
    """Traversal-safe path segment for a per-field subdir name."""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)


def _bundle_sha256(directory: Path, filenames: list[str]) -> str:
    """Stable sha over the bundle files (sorted) — name + bytes."""
    h = hashlib.sha256()
    for name in sorted(filenames):
        h.update(name.encode("utf-8"))
        h.update((directory / name).read_bytes())
    return h.hexdigest()


def _combined_sha256(bundle_dir: Path, rel_files: list[str]) -> str:
    """Stable sha over bundle files addressed by ``/``-relative path (sorted).

    Generalises :func:`_bundle_sha256` across the combined bundle's ``scalar/``
    and per-field ``field/<response>/`` subtrees — the relative path is part of
    the hash so a file moving between subtrees changes the sha.
    """
    h = hashlib.sha256()
    for rel in sorted(rel_files):
        h.update(rel.encode("utf-8"))
        h.update((bundle_dir / rel).read_bytes())
    return h.hexdigest()


def write_scalar_bundle(
    bundle_dir: Path,
    surrogates: dict[str, Surrogate],
    *,
    factors: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    coverage_report: dict[str, Any] | None = None,
) -> str:
    """Write one ``joblib`` per response + a manifest; return ``bundle_sha256``.

    The caller persists the returned sha256 on the version row; ``load_scalar_bundle``
    re-verifies it. Raises ``ValueError`` if no surrogates are given (a
    caller bug — validate before writing, [[feedback_validate_before_writeonce_bytes]]).
    """
    if not surrogates:
        raise ValueError("Refusing to write an empty model bundle.")
    scalar_dir = bundle_dir / _SCALAR_DIR
    scalar_dir.mkdir(parents=True, exist_ok=True)
    file_map: dict[str, str] = {}
    for response, surrogate in surrogates.items():
        fname = _joblib_name(response)
        joblib.dump(surrogate, scalar_dir / fname)
        file_map[response] = fname
    sha = _bundle_sha256(scalar_dir, list(file_map.values()))
    manifest = {
        "kind": "scalar",
        "factors": factors,
        "responses": responses,
        "files": file_map,
        "bundle_sha256": sha,
    }
    # The declared envelope + stored edge threshold travel *with* the bundle
    # (ADR-0020 §8.1). An exported tool runs in its own subprocess with no
    # database, so the guard has nothing else to read — and the envelope has
    # to be the one the analysis produced, not a copy that can drift.
    if coverage_report:
        manifest["coverage_report"] = coverage_report
    (bundle_dir / _MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return sha


def load_coverage_report(bundle_dir: Path) -> dict[str, Any] | None:
    """The coverage report baked into a bundle, or ``None`` (ADR-0020 §8.1).

    ``None`` for a bundle written before coverage existed, which is why the
    exported tool omits both support outputs rather than emitting empty ones:
    an output a descriptor declares but the orchestrator never writes is worse
    than no output at all.

    Read from the manifest rather than the sha-verified payload deliberately —
    the report is metadata *about* the fit, not part of the model whose bytes
    the checksum protects.
    """
    manifest_path = bundle_dir / _MANIFEST
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    report = manifest.get("coverage_report")
    return report if isinstance(report, dict) else None


def load_scalar_bundle(bundle_dir: Path, expected_sha256: str) -> dict[str, Surrogate]:
    """Verify the bundle sha then load the per-response surrogates (§5.3).

    Raises :class:`ModelBundleCorruptError` when the manifest is missing /
    unreadable or the recomputed sha doesn't match ``expected_sha256``.
    """
    manifest_path = bundle_dir / _MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelBundleCorruptError(
            f"Model bundle manifest is missing or unreadable at {bundle_dir}."
        ) from exc
    file_map: dict[str, str] = dict(manifest.get("files") or {})
    scalar_dir = bundle_dir / _SCALAR_DIR
    actual = _bundle_sha256(scalar_dir, list(file_map.values()))
    if actual != expected_sha256:
        raise ModelBundleCorruptError(
            f"Model bundle at {bundle_dir} failed its integrity check "
            "(sha256 mismatch); refusing to load."
        )
    out: dict[str, Surrogate] = {}
    for response, fname in file_map.items():
        out[response] = joblib.load(scalar_dir / fname)
    return out


def _field_bundle_files(mode_files: list[str], reference_mesh_filename: str | None) -> list[str]:
    """Every load-bearing file in a field bundle's ``field/`` dir (sha inputs)."""
    files = [_MU, _BASIS, _ENERGY, *mode_files]
    if reference_mesh_filename:
        files.append(reference_mesh_filename)
    return files


def _write_one_field(
    field_dir: Path,
    surrogate: FieldSurrogate,
    *,
    file_format: str,
    reference_mesh: Path | None,
) -> tuple[list[str], str | None]:
    """Write one POD field surrogate's parts into ``field_dir``.

    Shared by the field-only and combined writers. Returns
    ``(mode_files, reference_mesh_filename)``. Raises ``ValueError`` on an empty
    model ([[feedback_validate_before_writeonce_bytes]]).
    """
    if not surrogate.per_mode_surrogates:
        raise ValueError("Refusing to write a field bundle with no POD modes.")
    field_dir.mkdir(parents=True, exist_ok=True)
    np.save(field_dir / _MU, np.asarray(surrogate.mu, dtype=float))
    np.save(field_dir / _BASIS, np.asarray(surrogate.modal_basis, dtype=float))
    np.save(field_dir / _ENERGY, np.asarray(surrogate.mode_energy_spectrum, dtype=float))
    mode_files: list[str] = []
    for m, sub in enumerate(surrogate.per_mode_surrogates):
        fname = _mode_name(m)
        joblib.dump(sub, field_dir / fname)
        mode_files.append(fname)
    reference_mesh_filename: str | None = None
    if reference_mesh is not None and reference_mesh.is_file():
        ext = reference_mesh.suffix or f".{file_format.lstrip('.')}"
        reference_mesh_filename = f"reference_mesh{ext}"
        shutil.copyfile(reference_mesh, field_dir / reference_mesh_filename)
    return mode_files, reference_mesh_filename


def _field_meta(
    surrogate: FieldSurrogate, *, file_format: str, reference_mesh_filename: str | None
) -> dict[str, Any]:
    """The per-field manifest block (shared by field-only + combined writers)."""
    return {
        "factors": list(surrogate.factors),
        "n_nodes": surrogate.n_nodes,
        "n_modes": surrogate.n_modes,
        "file_format": file_format.lstrip("."),
        "reference_mesh_filename": reference_mesh_filename,
        "quality": {
            "n": surrogate.quality.n,
            "modes_retained": surrogate.quality.modes_retained,
            "mode_energy": surrogate.quality.mode_energy,
            "recon_rmse": surrogate.quality.recon_rmse,
            "mode_q2": list(surrogate.quality.mode_q2),
        },
    }


def _reconstruct_field(
    field_dir: Path, mode_files: list[str], meta: dict[str, Any]
) -> FieldSurrogate:
    """Rebuild a :class:`FieldSurrogate` from a (verified) field subtree."""
    mu = np.load(field_dir / _MU)
    modal_basis = np.load(field_dir / _BASIS)
    mode_energy = np.load(field_dir / _ENERGY)
    per_mode = [joblib.load(field_dir / fname) for fname in mode_files]
    q = dict(meta.get("quality") or {})
    quality = FieldQuality(
        n=int(q.get("n", len(per_mode))),
        modes_retained=int(q.get("modes_retained", len(per_mode))),
        mode_energy=float(q.get("mode_energy", 0.0)),
        recon_rmse=q.get("recon_rmse"),
        mode_q2=list(q.get("mode_q2") or []),
    )
    return FieldSurrogate(
        factors=list(meta.get("factors") or []),
        mu=mu,
        modal_basis=modal_basis,
        per_mode_surrogates=per_mode,
        mode_energy_spectrum=[float(v) for v in mode_energy.tolist()],
        quality=quality,
    )


def write_field_bundle(
    bundle_dir: Path,
    surrogate: FieldSurrogate,
    *,
    factors: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    file_format: str,
    reference_mesh: Path | None = None,
    coverage_report: dict[str, Any] | None = None,
) -> str:
    """Write a POD field surrogate's parts + a manifest; return ``bundle_sha256``.

    Stores the mean field, modal basis, energy spectrum, and per-mode
    coefficient surrogates under ``field/`` (§4.3). When ``reference_mesh``
    is a real file (a representative snapshot in a geometry-bearing format),
    it is copied in so the predicted-field viewer has a mesh to colour;
    ``.npy`` fields have no geometry and pass ``reference_mesh=None``. Raises
    ``ValueError`` on an empty model ([[feedback_validate_before_writeonce_bytes]]).
    """
    field_dir = bundle_dir / _FIELD_DIR
    mode_files, reference_mesh_filename = _write_one_field(
        field_dir, surrogate, file_format=file_format, reference_mesh=reference_mesh
    )
    sha = _bundle_sha256(field_dir, _field_bundle_files(mode_files, reference_mesh_filename))
    manifest = {
        "kind": "field",
        "factors": factors,
        "responses": responses,
        "field": _field_meta(
            surrogate, file_format=file_format, reference_mesh_filename=reference_mesh_filename
        ),
        "mode_files": mode_files,
        "bundle_sha256": sha,
    }
    if coverage_report:
        manifest["coverage_report"] = coverage_report
    (bundle_dir / _MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return sha


def load_field_bundle(bundle_dir: Path, expected_sha256: str) -> FieldSurrogate:
    """Verify the bundle sha then reconstruct the POD field surrogate (§5.3).

    Raises :class:`ModelBundleCorruptError` when the manifest is missing /
    unreadable or the recomputed sha doesn't match ``expected_sha256``.
    """
    manifest_path = bundle_dir / _MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelBundleCorruptError(
            f"Field bundle manifest is missing or unreadable at {bundle_dir}."
        ) from exc
    field_dir = bundle_dir / _FIELD_DIR
    field_meta: dict[str, Any] = dict(manifest.get("field") or {})
    mode_files: list[str] = list(manifest.get("mode_files") or [])
    reference_mesh_filename = field_meta.get("reference_mesh_filename")
    actual = _bundle_sha256(field_dir, _field_bundle_files(mode_files, reference_mesh_filename))
    if actual != expected_sha256:
        raise ModelBundleCorruptError(
            f"Field bundle at {bundle_dir} failed its integrity check "
            "(sha256 mismatch); refusing to load."
        )
    return _reconstruct_field(field_dir, mode_files, field_meta)


def write_combined_bundle(
    bundle_dir: Path,
    surrogates: dict[str, Surrogate],
    field_surrogates: dict[str, FieldSurrogate],
    *,
    factors: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    field_formats: dict[str, str],
    reference_meshes: dict[str, Path | None] | None = None,
    coverage_report: dict[str, Any] | None = None,
) -> str:
    """Write a combined scalar+field bundle; return ``bundle_sha256`` (ADR-0018).

    Scalars go under ``scalar/`` (one joblib each); each field goes under
    ``field/<safe-response>/`` (mu / basis / energy / per-mode + optional mesh).
    One manifest lists both and the sha covers every file across both subtrees.
    Raises ``ValueError`` if nothing is supplied
    ([[feedback_validate_before_writeonce_bytes]]).
    """
    if not surrogates and not field_surrogates:
        raise ValueError("Refusing to write an empty combined bundle.")
    reference_meshes = reference_meshes or {}
    rel_files: list[str] = []

    scalar_map: dict[str, str] = {}
    if surrogates:
        scalar_dir = bundle_dir / _SCALAR_DIR
        scalar_dir.mkdir(parents=True, exist_ok=True)
        for response, surrogate in surrogates.items():
            fname = _joblib_name(response)
            joblib.dump(surrogate, scalar_dir / fname)
            scalar_map[response] = fname
            rel_files.append(f"{_SCALAR_DIR}/{fname}")

    field_models: dict[str, dict[str, Any]] = {}
    for response, fs in field_surrogates.items():
        safe = _safe_segment(response)
        subdir_rel = f"{_FIELD_DIR}/{safe}"
        fmt = field_formats.get(response, "npy")
        mode_files, ref_name = _write_one_field(
            bundle_dir / subdir_rel,
            fs,
            file_format=fmt,
            reference_mesh=reference_meshes.get(response),
        )
        for f in _field_bundle_files(mode_files, ref_name):
            rel_files.append(f"{subdir_rel}/{f}")
        meta = _field_meta(fs, file_format=fmt, reference_mesh_filename=ref_name)
        meta["subdir"] = subdir_rel
        meta["mode_files"] = mode_files
        field_models[response] = meta

    sha = _combined_sha256(bundle_dir, rel_files)
    manifest = {
        "kind": "combined",
        "factors": factors,
        "responses": responses,
        "scalar_files": scalar_map,
        "field_models": field_models,
        "bundle_sha256": sha,
    }
    # As for the scalar bundle: the envelope has to travel *with* the model so an
    # exported tool's support guard has something to read in its own subprocess.
    # Omitting it here meant a combined or field export declared ``support_status``
    # / ``support_sigma`` in its descriptor (synthesis reads the report from the
    # database) while the orchestrator could never produce them.
    if coverage_report:
        manifest["coverage_report"] = coverage_report
    (bundle_dir / _MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return sha


def load_combined_bundle(
    bundle_dir: Path, expected_sha256: str
) -> tuple[dict[str, Surrogate], dict[str, FieldSurrogate]]:
    """Verify the sha then load a combined bundle's scalar + field surrogates.

    Returns ``(scalar_surrogates, field_surrogates)``. Raises
    :class:`ModelBundleCorruptError` on a missing manifest or sha mismatch.
    """
    manifest_path = bundle_dir / _MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelBundleCorruptError(
            f"Combined bundle manifest is missing or unreadable at {bundle_dir}."
        ) from exc
    scalar_map: dict[str, str] = dict(manifest.get("scalar_files") or {})
    field_models: dict[str, dict[str, Any]] = dict(manifest.get("field_models") or {})

    rel_files: list[str] = [f"{_SCALAR_DIR}/{fname}" for fname in scalar_map.values()]
    for meta in field_models.values():
        subdir_rel = str(meta.get("subdir"))
        for f in _field_bundle_files(
            list(meta.get("mode_files") or []), meta.get("reference_mesh_filename")
        ):
            rel_files.append(f"{subdir_rel}/{f}")
    actual = _combined_sha256(bundle_dir, rel_files)
    if actual != expected_sha256:
        raise ModelBundleCorruptError(
            f"Combined bundle at {bundle_dir} failed its integrity check "
            "(sha256 mismatch); refusing to load."
        )

    scalars = {r: joblib.load(bundle_dir / _SCALAR_DIR / fname) for r, fname in scalar_map.items()}
    fields = {
        response: _reconstruct_field(
            bundle_dir / str(meta.get("subdir")), list(meta.get("mode_files") or []), meta
        )
        for response, meta in field_models.items()
    }
    return scalars, fields


def combined_field_reference_mesh_path(bundle_dir: Path, response: str) -> Path | None:
    """Absolute path to one field response's reference mesh in a combined bundle.

    ``None`` when that field has no geometry (``.npy``) or the file is absent.
    """
    manifest_path = bundle_dir / _MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    meta = (manifest.get("field_models") or {}).get(response) or {}
    fname = meta.get("reference_mesh_filename")
    subdir = meta.get("subdir")
    if not fname or not subdir:
        return None
    path = bundle_dir / str(subdir) / str(fname)
    return path if path.is_file() else None


def field_reference_mesh_path(bundle_dir: Path) -> Path | None:
    """Absolute path to the bundle's reference mesh, if it has geometry.

    The predicted-field viewer reads this to render the coloured mesh;
    ``None`` for ``.npy`` fields (no geometry), where the viewer falls back
    to the scree / coefficient panels.
    """
    manifest_path = bundle_dir / _MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    fname = (manifest.get("field") or {}).get("reference_mesh_filename")
    if not fname:
        return None
    path = bundle_dir / _FIELD_DIR / str(fname)
    return path if path.is_file() else None


def extract_field_geometry(
    reference_mesh_path: Path,
) -> tuple[list[list[float]], list[int], list[int], list[int]] | None:
    """Surface triangulation of a reference mesh for the 3-D field viewer.

    Returns ``(points, i, j, k)`` where ``points`` is the **full** node
    coordinate array (so a per-node field aligns by index for Plotly
    ``Mesh3d`` ``intensity``) and ``i``/``j``/``k`` are the surface
    triangles' vertex indices into that array. ``None`` when the mesh has no
    surface or the surface points can't be aligned back to the full node set
    (the viewer then falls back to the value-vs-node line). Reads via
    ``pyvista`` (lazily); pure geometry — no field values touched here.
    """
    try:
        import pyvista  # noqa: PLC0415
    except ImportError:  # pragma: no cover - dep vendored alongside the field path
        return None

    mesh = pyvista.read(str(reference_mesh_path))
    if mesh.n_points == 0:
        return None
    # ``dataset_surface`` is the algorithm that attaches ``vtkOriginalPointIds``
    # (so a per-node field maps back to the full point set); pin it explicitly
    # rather than rely on the changing pyvista default.
    surface = mesh.extract_surface(pass_pointid=True, algorithm="dataset_surface").triangulate()
    faces = np.asarray(surface.faces)
    if faces.size == 0:
        return None
    tris = faces.reshape(-1, 4)[:, 1:]  # drop the per-face vertex-count prefix (3)
    original_ids = surface.point_data.get("vtkOriginalPointIds")
    if original_ids is not None:
        tris = np.asarray(original_ids)[tris]  # map surface-local → full-mesh indices
    elif surface.n_points != mesh.n_points:
        return None  # can't align a per-node field to these surface points
    points = np.asarray(mesh.points, dtype=float)
    return (
        points.tolist(),
        tris[:, 0].astype(int).tolist(),
        tris[:, 1].astype(int).tolist(),
        tris[:, 2].astype(int).tolist(),
    )
